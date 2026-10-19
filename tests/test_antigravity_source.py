import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import antigravity_source
import antigravity_source_worker
import ai_cli
import providers


VID = "jNQXAC9IVRw"
TOKEN = "0123456789abcdef01234567"


def event(name, **values):
    return json.dumps({"event": name, **values})


def output(response, tools=None, status="SUCCESS", error=""):
    lines = [event("init", init={"model": "fixture-model"})]
    for tool in tools or []:
        lines.append(event("tool", tool_info=tool))
    lines.append(event("result", result={"status": status, "error": error, "response": response,
                                           "conversation_id": "fixture-conversation", "usage": {"input_tokens": 4}}))
    return "\n".join(lines)


class AntigravitySourceTests(unittest.TestCase):
    def folder(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def audio(self):
        audio = self.folder() / "random.webm"
        audio.write_bytes(b"fixture audio")
        return audio

    def tool(self, audio, name="view_file", path=None):
        return {"name": name, "parameters": {"AbsolutePath": str(path or audio)}}

    def test_download_command_is_the_approved_python_module_audio_only_invocation(self):
        work = self.folder()
        with mock.patch.object(antigravity_source, "AGY_PYTHON", Path(sys.executable)):
            command = antigravity_source.download_command(VID, work, "random")
        self.assertEqual(command[:4], [str(Path(sys.executable)), "-B", "-m", "yt_dlp"])
        self.assertIn("bestaudio", command)
        self.assertIn("--ignore-config", command)
        self.assertIn("--no-cache-dir", command)
        self.assertIn("--no-write-auto-subs", command)
        self.assertIn("before_dl:AIVIDEOTRANSCRIBER_TITLE=%(title)j", command)
        self.assertIn("after_move:AIVIDEOTRANSCRIBER_FILE=%(filepath)j", command)
        self.assertEqual(command[-2:], ["--", "https://www.youtube.com/watch?v=" + VID])
        self.assertNotIn("--cookies", command)
        self.assertIn(str(work / "random.%(ext)s"), command)

    def test_missing_download_interpreter_names_its_setup_variable(self):
        with mock.patch.object(antigravity_source, "AGY_PYTHON", None), \
                self.assertRaisesRegex(antigravity_source.SourceError, "AIVIDEOTRANSCRIBER_AGY_PYTHON"):
            antigravity_source.download_command(VID, self.folder(), "random")

    def test_download_metadata_uses_transcriber_markers(self):
        work = self.folder()
        audio_path = work / "random.webm"
        rows = [
            "AIVIDEOTRANSCRIBER_TITLE=" + json.dumps("Fixture title"),
            "AIVIDEOTRANSCRIBER_DURATION=12",
            "AIVIDEOTRANSCRIBER_EXTRACTOR=" + json.dumps("Youtube"),
            "AIVIDEOTRANSCRIBER_FILE=" + json.dumps(str(audio_path)),
        ]

        def fake_popen(*args, **kwargs):
            kwargs["stdout"].write(("\n".join(rows) + "\n").encode("utf-8"))
            audio_path.write_bytes(b"fixture audio")
            process = mock.Mock()
            process.poll.return_value = 0
            process.returncode = 0
            return process

        with mock.patch.object(antigravity_source, "AGY_PYTHON", Path(sys.executable)), \
                mock.patch.object(antigravity_source.subprocess, "Popen", side_effect=fake_popen), \
                mock.patch.object(antigravity_source.importlib.metadata, "version", return_value="fixture-version"):
            result = antigravity_source.download_audio(VID, work, "random", threading.Event())

        self.assertEqual((result["title"], result["duration"], result["extractor"]),
                         ("Fixture title", 12, "Youtube"))
        self.assertEqual(result["path"], audio_path)
        self.assertEqual(result["yt_dlp_version"], "fixture-version")

    def test_final_markers_remove_thought_text_and_only_local_view_file_is_allowed(self):
        audio = self.audio()
        response = "I should inspect the file first.\n" + antigravity_source.BEGIN.format(token=TOKEN) + \
                   "\nFinal plain transcript.\n" + antigravity_source.END.format(token=TOKEN) + "\nExtra note"
        result = antigravity_source.parse_run(output(response, [self.tool(audio), self.tool(audio)]), "", audio, TOKEN)
        self.assertEqual(result["text"], "Final plain transcript.")
        self.assertEqual((result["model"], result["tool_calls"]), ("fixture-model", 2))
        self.assertNotIn("inspect", result["text"])
        for bad in [self.tool(audio, "web_search"), self.tool(audio, path=self.folder() / "other.webm")]:
            with self.subTest(bad=bad), self.assertRaisesRegex(antigravity_source.SourceError, "result was discarded"):
                antigravity_source.parse_run(output(response, [bad]), "", audio, TOKEN)

    def test_hidden_worker_detects_forbidden_tools_as_stream_events_arrive(self):
        audio = self.audio()
        allowed = event("tool", tool_info=self.tool(audio))
        search = event("tool", tool_info=self.tool(audio, "web_search"))
        other_file = event("tool", tool_info=self.tool(audio, path=self.folder() / "other.webm"))
        self.assertFalse(antigravity_source_worker.forbidden_tool(allowed, audio))
        self.assertTrue(antigravity_source_worker.forbidden_tool(search, audio))
        self.assertTrue(antigravity_source_worker.forbidden_tool(other_file, audio))

    def test_missing_view_file_markers_errors_and_sign_in_are_clear(self):
        audio = self.audio()
        marked = antigravity_source.BEGIN.format(token=TOKEN) + "\ntext\n" + antigravity_source.END.format(token=TOKEN)
        with self.assertRaisesRegex(antigravity_source.SourceError, "did not read"):
            antigravity_source.parse_run(output(marked), "", audio, TOKEN)
        with self.assertRaisesRegex(antigravity_source.SourceError, "required final markers"):
            antigravity_source.parse_run(output("thought then transcript", [self.tool(audio)]), "", audio, TOKEN)
        with self.assertRaises(antigravity_source.SourceError) as caught:
            antigravity_source.parse_run(output("", status="ERROR", error="Authentication expired. Please log back in."), "", audio, TOKEN)
        self.assertEqual((caught.exception.code, str(caught.exception)), ("authentication", antigravity_source.SIGN_IN_MESSAGE))

        spoken = "The speaker says please log in to your account before continuing."
        response = antigravity_source.BEGIN.format(token=TOKEN) + "\n" + spoken + "\n" + antigravity_source.END.format(token=TOKEN)
        model_event = event("model_response", response={"content": spoken})
        parsed = antigravity_source.parse_run("\n".join([model_event, output(response, [self.tool(audio)])]), "", audio, TOKEN)
        self.assertEqual(parsed["text"], spoken)
        for control_output, stderr in [
                (event("init", init={"error": "Please log in to your account"}) + "\n" + output(response, [self.tool(audio)]), ""),
                (output(response, [self.tool(audio)]), "Authentication required. Please log in to your account"),
                ("Please log in to your account\n" + output(response, [self.tool(audio)]), "")]:
            with self.subTest(control_output=control_output[:30], stderr=stderr), \
                    self.assertRaises(antigravity_source.SourceError) as sign_in:
                antigravity_source.parse_run(control_output, stderr, audio, TOKEN)
            self.assertEqual(sign_in.exception.code, "authentication")

        failed_tool = event("step_update", step_update={"state": "FAILED", "error": "fixture decoder rejected audio",
                                                           "tool_info": self.tool(audio)})
        with self.assertRaisesRegex(antigravity_source.SourceError, "fixture decoder rejected audio"):
            antigravity_source.parse_run("\n".join([failed_tool, output(marked, [self.tool(audio)])]), "", audio, TOKEN)
        active_only = event("step_update", step_update={"state": "ACTIVE", "tool_info": self.tool(audio)})
        with self.assertRaisesRegex(antigravity_source.SourceError, "did not finish reading"):
            antigravity_source.parse_run("\n".join([active_only, output(marked)]), "", audio, TOKEN)

    def test_prompt_has_the_media_and_inline_pt1_guards_without_requesting_another_file(self):
        prompt = antigravity_source.transcript_prompt("one.webm", TOKEN)
        self.assertIn("MEDIA_UNAVAILABLE", prompt)
        self.assertIn("Mandatory PT-1", prompt)
        self.assertIn("do not read the external policy file or any other file", prompt)
        self.assertIn("The policy reference is unavailable", prompt)

    def test_fetch_persists_provenance_and_erases_audio_on_success_failure_and_cancel(self):
        root = self.folder() / "source-work"
        seen = []
        def downloader(vid, work, stem, cancel):
            audio = work / (stem + ".webm")
            audio.write_bytes(b"private audio")
            seen.append(audio)
            return {"path": audio, "title": "Fixture title", "duration": 42, "bytes": 13,
                    "extractor": "Youtube", "yt_dlp_version": "fixture"}
        def runner(work, audio, prompt, model, cancel):
            self.assertEqual(set(work.iterdir()), {audio})
            self.assertIn(audio.name, prompt)
            self.assertNotIn(VID, prompt)
            return {"text": "A complete transcript.", "model": "fixture-model", "usage": {"input_tokens": 2},
                    "conversation_id": "c-1", "tool_calls": 2}
        timing_steps = []
        result = antigravity_source.fetch(VID, work_root=root, downloader=downloader, runner=runner,
                                          step_update=lambda size, model: timing_steps.append((size, model)))
        self.assertEqual((result["provider"], result["title"], result["words"]), ("antigravity", "Fixture title", 3))
        self.assertEqual(timing_steps, [(42, "CLI default")])
        self.assertEqual(result["raw_response"]["view_file_calls"], 2)
        self.assertEqual(result["model"], "fixture-model")
        self.assertNotIn("thought", json.dumps(result))
        self.assertFalse(seen[-1].exists())
        self.assertFalse(root.exists())

        def failure(*args):
            raise antigravity_source.SourceError("service_error", "fixture failure")
        with self.assertRaises(antigravity_source.SourceError):
            antigravity_source.fetch(VID, work_root=root, downloader=downloader, runner=failure)
        self.assertFalse(seen[-1].exists())
        self.assertFalse(root.exists())

        def canceled_runner(*args):
            raise InterruptedError("fixture canceled after download")
        with self.assertRaises(InterruptedError):
            antigravity_source.fetch(VID, work_root=root, downloader=downloader, runner=canceled_runner)
        self.assertFalse(seen[-1].exists())
        self.assertFalse(root.exists())

        canceled = threading.Event(); canceled.set()
        with self.assertRaises(InterruptedError):
            antigravity_source.fetch(VID, cancel=canceled, work_root=root, downloader=downloader, runner=runner)
        self.assertFalse(root.exists())

    def test_unknown_audio_duration_does_not_publish_a_timing_size(self):
        root = self.folder() / "unknown-duration-work"
        updates = []

        def downloader(vid, work, stem, cancel):
            audio = work / (stem + ".webm")
            audio.write_bytes(b"fixture audio")
            return {"path": audio, "title": "Fixture", "duration": None, "bytes": 13}

        def runner(work, audio, prompt, model, cancel):
            return {"text": "A fixture transcript.", "model": "fixture-model", "tool_calls": 1}

        result = antigravity_source.fetch(VID, work_root=root, downloader=downloader, runner=runner,
                                          step_update=lambda size, model: updates.append((size, model)))
        self.assertEqual(updates, [])
        self.assertEqual(result["raw_response"]["duration_seconds"], 0)
        self.assertFalse(root.exists())

    def test_cleanup_failure_is_reported_instead_of_claiming_private_audio_was_removed(self):
        root = self.folder() / "source-work"
        def downloader(vid, work, stem, cancel):
            audio = work / (stem + ".webm"); audio.write_bytes(b"private audio")
            return {"path": audio, "title": "Fixture", "duration": 1, "bytes": 13}
        def runner(*args):
            return {"text": "Transcript.", "tool_calls": 1}
        with mock.patch.object(antigravity_source.shutil, "rmtree", return_value=None), \
                self.assertRaises(antigravity_source.SourceError) as caught:
            antigravity_source.fetch(VID, work_root=root, downloader=downloader, runner=runner)
        self.assertEqual(caught.exception.code, "cleanup")
        self.assertIn("Could not remove private Antigravity scratch", str(caught.exception))

    def test_cleanup_retries_a_held_file_and_startup_preserves_any_live_owned_task(self):
        root = self.folder() / "antigravity-runtime"
        stale = root / ("task-" + "1" * 24)
        active = root / ("task-" + "2" * 24)
        stale.mkdir(parents=True)
        active.mkdir()
        (stale / "private.webm").write_bytes(b"do not inspect")
        (active / antigravity_source.OWNER_NAME).write_text(json.dumps({
            "version": 1, "parent": {"pid": 41, "started_at": 4100},
            "worker": {"pid": 42, "started_at": 4200}}), encoding="utf-8")
        with mock.patch.object(antigravity_source, "_process_alive",
                               side_effect=lambda pid, started: (pid, started) == (42, 4200)):
            receipt = antigravity_source.cleanup_stale_tasks(root)
        self.assertEqual(receipt, {"removed": 0, "preserved": 2})
        self.assertTrue(stale.exists())

        real_rmtree = antigravity_source.shutil.rmtree
        calls = []
        def held_once(path):
            calls.append(Path(path))
            if len(calls) == 1:
                raise PermissionError("fixture lock")
            real_rmtree(path)
        with mock.patch.object(antigravity_source, "_process_alive", return_value=False), \
                mock.patch.object(antigravity_source.shutil, "rmtree", side_effect=held_once), \
                mock.patch.object(antigravity_source.time, "sleep"):
            receipt = antigravity_source.cleanup_stale_tasks(root)
        self.assertEqual(receipt, {"removed": 2, "preserved": 0})
        self.assertGreaterEqual(len(calls), 3)
        self.assertFalse(root.exists())
        with mock.patch.object(antigravity_source, "_process_started_at", return_value=4300):
            self.assertFalse(antigravity_source._process_alive(42, 4200), "a reused PID is not ownership proof")

    def test_startup_cleanup_unlinks_a_task_symlink_without_following_its_target(self):
        folder = self.folder()
        root, outside = folder / "runtime", folder / "outside"
        root.mkdir(); outside.mkdir()
        marker = outside / "keep.txt"; marker.write_text("keep", encoding="utf-8")
        link = root / ("task-" + "3" * 24)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        receipt = antigravity_source.cleanup_stale_tasks(root)
        self.assertEqual(receipt["removed"], 1)
        self.assertTrue(marker.is_file())
        self.assertFalse(link.exists())

    def test_reparse_cleanup_never_uses_recursive_removal(self):
        task = self.folder() / ("task-" + "6" * 24)
        task.mkdir()
        with mock.patch.object(antigravity_source, "_is_reparse_point", return_value=True), \
                mock.patch.object(Path, "unlink") as unlink, \
                mock.patch.object(antigravity_source.shutil, "rmtree") as rmtree:
            antigravity_source.remove_private_scratch(task)
        unlink.assert_called_once_with()
        rmtree.assert_not_called()

    def test_provider_dispatch_needs_no_key_and_maps_source_errors(self):
        source = {"provider": "antigravity", "text": "answer"}
        with mock.patch.object(antigravity_source, "fetch", return_value=source) as fetch:
            self.assertIs(providers.fetch("antigravity", VID, ""), source)
            fetch.assert_called_once()
        step_update = mock.Mock()
        with mock.patch.object(antigravity_source, "fetch", return_value=source) as fetch:
            self.assertIs(providers.fetch("antigravity", VID, "", step_update=step_update), source)
            fetch.assert_called_once_with(VID, "en", model="", cancel=None, progress=None,
                                          work_root=None, step_update=step_update)
        with mock.patch.object(antigravity_source, "fetch", side_effect=antigravity_source.SourceError("unsafe_tools", "No web tools.")):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.fetch("antigravity", VID, "")
        self.assertEqual((caught.exception.code, str(caught.exception)), ("unsafe_tools", "No web tools."))

    def test_wmi_worker_command_is_hidden_and_carries_no_prompt_or_video_id(self):
        folder = self.folder()
        command, launcher = antigravity_source.wmi_worker_command(folder / "request.json", folder / "result.json", folder / "cancel")
        self.assertEqual(launcher[:5], ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden"])
        self.assertIn("Invoke-CimMethod", launcher[-1])
        self.assertIn("ShowWindow=[uint16]0", launcher[-1])
        self.assertNotIn(VID, command + " ".join(launcher))
        self.assertNotIn("complete speech", command + " ".join(launcher))

    def test_sign_in_guard_is_broad_and_handles_stdout_or_stderr_chunk_boundaries(self):
        for phrase in (b"sign in", b"signing-in", b"log in", b"logging in", b"accounts.google.com"):
            found, tail = antigravity_source_worker.sign_in_chunk("", phrase[:3])
            self.assertFalse(found)
            found, _ = antigravity_source_worker.sign_in_chunk(tail, phrase[3:])
            self.assertTrue(found, phrase)

    def test_worker_reassembles_json_and_only_terminates_for_cli_auth_controls(self):
        def run(chunks, stderr_chunks=()):
            folder = self.folder()
            work = folder / "audio"; work.mkdir()
            audio = work / "fixture.webm"; audio.write_bytes(b"fixture")
            request, result, cancel = folder / "request.json", folder / "result.json", folder / "cancel"
            request.write_text(json.dumps({"work_dir": str(work), "audio_name": audio.name,
                                           "prompt": "fixture", "model": ""}), encoding="utf-8")
            terminated = []
            class Input:
                def write(self, value):
                    pass
                def close(self):
                    pass
            class Process:
                pid = 4321
                returncode = None
                stdin = Input()
                def __init__(self, stdout, stderr):
                    self.stdout, self.stderr = stdout, stderr
                    self.out = list(chunks); self.err = list(stderr_chunks)
                def poll(self):
                    if self.returncode is not None:
                        return self.returncode
                    if self.out or self.err:
                        if self.out:
                            self.stdout.write(self.out.pop(0))
                        if self.err:
                            self.stderr.write(self.err.pop(0))
                        return None
                    self.returncode = 0
                    return 0
            def popen(*args, **kwargs):
                return Process(kwargs["stdout"], kwargs["stderr"])
            def terminate(process):
                terminated.append(process.pid)
                process.returncode = -9
            argv = ["worker", "--request", str(request), "--result", str(result), "--cancel", str(cancel)]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(antigravity_source_worker, "agy_path", return_value=Path("agy.exe")), \
                    mock.patch.object(antigravity_source_worker.subprocess, "Popen", side_effect=popen), \
                    mock.patch.object(antigravity_source_worker, "terminate", side_effect=terminate), \
                    mock.patch.object(antigravity_source_worker.time, "sleep"):
                antigravity_source_worker.main()
            return json.loads(result.read_text(encoding="utf-8")), terminated

        spoken = "Café: please log in to your account."
        response = output(antigravity_source.BEGIN.format(token=TOKEN) + "\n" + spoken + "\n" +
                          antigravity_source.END.format(token=TOKEN)) + "\n"
        response = "\n".join(json.dumps(json.loads(line), ensure_ascii=False) for line in response.splitlines()) + "\n"
        encoded = response.encode("utf-8")
        cafe_split = encoded.index("Café".encode("utf-8")) + 4
        phrase_split = encoded.index(b"please log in") + 8
        receipt, terminated = run([encoded[:cafe_split], encoded[cafe_split:phrase_split], encoded[phrase_split:]])
        self.assertEqual((receipt["status"], terminated), ("finished", []))

        auth = (event("init", init={"error": "Please log in to your account"}) + "\n").encode()
        receipt, terminated = run([auth[:17], auth[17:]])
        self.assertEqual((receipt["status"], terminated), ("sign_in", [4321]))
        receipt, terminated = run([], [b"Authentication required. Please lo", b"g in to your account"])
        self.assertEqual((receipt["status"], terminated), ("sign_in", [4321]))
        receipt, terminated = run([b"Please log ", b"in to your account"])
        self.assertEqual((receipt["status"], terminated), ("sign_in", [4321]))

    def test_cancel_signals_then_stops_only_the_verified_unresponsive_worker(self):
        task = self.folder() / ("task-" + "4" * 24)
        work = task / "audio"; work.mkdir(parents=True)
        audio = work / "fixture.webm"; audio.write_bytes(b"fixture")
        cancel = threading.Event(); cancel.set()
        launch = mock.Mock(returncode=0, stdout=b'{"ProcessId":321,"ReturnValue":0}', stderr=b"")
        receipts = lambda pid: {"pid": pid, "started_at": pid * 10}
        with mock.patch.object(antigravity_source.subprocess, "run", return_value=launch), \
                mock.patch.object(antigravity_source, "_process_receipt", side_effect=receipts), \
                mock.patch.object(antigravity_source, "_process_alive", return_value=True), \
                mock.patch.object(antigravity_source, "_terminate_owned_process") as terminate, \
                mock.patch.object(antigravity_source.time, "monotonic", side_effect=[10.0, 13.0]), \
                mock.patch.object(antigravity_source.time, "sleep"):
            with self.assertRaises(InterruptedError):
                antigravity_source.run_hidden(work, audio, "prompt", "", cancel)
        terminate.assert_called_once_with(321, 3210)
        self.assertEqual(json.loads((task / antigravity_source.OWNER_NAME).read_text(encoding="utf-8"))["worker"],
                         {"pid": 321, "started_at": 3210})

    def test_ai_cli_diagnostics_keep_the_useful_end_not_the_noisy_beginning(self):
        diagnostic = ai_cli.diagnostic_tail(b"old noise\n" + b"x" * 9000 + b"\nUSEFUL FINAL FAILURE")
        self.assertNotIn("old noise", diagnostic)
        self.assertTrue(diagnostic.endswith("USEFUL FINAL FAILURE"))


if __name__ == "__main__":
    unittest.main()
