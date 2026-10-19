"""Bounded offline regression checks for the Job11 integration correction."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import ai_cli
import opencode_cli
import opencode_process
import opencode_review_fixture as live_proof

ROOT = Path(__file__).resolve().parents[1]
TEMP_PARENT = Path(os.environ.get("TMP") or ROOT / "_TEMP")
FREE = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
MODELS = [{"id": FREE, "label": "Fixture", "free": True}]


class CacheReviewTests(unittest.TestCase):
    def tearDown(self):
        opencode_cli.reset_model_cache()

    def test_bootstrap_cache_readers_finish_while_discovery_is_held(self):
        entered, release, reader_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        opencode_cli._MODEL_CACHE.update(at=time.monotonic(), executable=("fixture",), models=MODELS, error="")
        def discover(args, now):
            entered.set()
            release.wait()
            return {"at": now, "executable": tuple(args), "models": MODELS, "error": ""}
        def reader():
            try:
                self.assertEqual(opencode_cli.info()["models"], MODELS)
                self.assertTrue(opencode_cli.model_is_listed(FREE))
                self.assertFalse(opencode_cli.model_is_listed("openrouter/unknown"))
            except BaseException as error:
                errors.append(error)
            finally:
                reader_done.set()
        with mock.patch.object(opencode_cli, "executable", return_value=["fixture"]), \
                mock.patch.object(opencode_cli, "_run_models", side_effect=discover):
            worker = threading.Thread(target=lambda: opencode_cli.model_list(refresh=True))
            worker.start()
            try:
                self.assertTrue(entered.wait(3), "discovery did not reach the controlled barrier")
                reading = threading.Thread(target=reader)
                reading.start()
                self.assertTrue(reader_done.wait(3), "cache reader blocked on discovery I/O")
            finally:
                release.set()
                worker.join()
                if 'reading' in locals():
                    reading.join()
        self.assertEqual(errors, [])

    def test_failures_cache_for_ten_minutes_refresh_and_success_age_starts_after_io(self):
        clock = [100.0]
        def failure(args, now):
            clock[0] += 20
            return {"executable": tuple(args), "models": [], "error": "fixture failure"}
        with mock.patch.object(opencode_cli, "executable", return_value=["fixture"]), \
                mock.patch.object(opencode_cli, "_run_models", side_effect=failure) as run:
            opencode_cli.model_list(clock=lambda: clock[0])
            clock[0] = 719.9
            self.assertEqual(opencode_cli.model_list(clock=lambda: clock[0])["error"], "fixture failure")
            self.assertEqual(run.call_count, 1)
            clock[0] = 720.1
            opencode_cli.model_list(clock=lambda: clock[0])
            self.assertEqual(run.call_count, 2)
            opencode_cli.model_list(refresh=True, clock=lambda: clock[0])
            self.assertEqual(run.call_count, 3)

    def test_shared_model_identifier_accepts_catalog_tilde_but_not_hash(self):
        import re
        for pattern in (ai_cli.MODEL_ID, opencode_cli.MODEL_ID):
            self.assertIsNotNone(re.fullmatch(pattern, "openrouter/test/model~variant"))
            self.assertIsNone(re.fullmatch(pattern, "openrouter/test/model#variant"))


class SessionReviewTests(unittest.TestCase):
    def context(self):
        return {"data": [{"type": "assistant", "agent": "build", "model": FREE,
                          "content": [{"type": "text", "text": "Synthetic"}]},
                         {"type": "idle", "outcome": "succeeded"}]}

    def test_actual_session_metadata_has_model_agent_terminal_and_tool_guards(self):
        good = self.context()
        cases = [(good, True), ({}, False), ({"data": []}, False)]
        for change in ({"model": "openrouter/wrong"}, {"model": None}, {"agent": "plan"},
                       {"error": {"message": "PRIVATE"}}, {"content": [{"type": "tool"}]}):
            value = self.context()
            value["data"][0].update(change)
            cases.append((value, False))
        value = self.context()
        value["data"][-1]["outcome"] = "failed"
        cases.append((value, False))
        for value, passed in cases:
            with self.subTest(value=value), mock.patch.object(opencode_cli, "_http_response",
                    return_value=(200, json.dumps(value).encode())) as request:
                if passed:
                    self.assertEqual(opencode_cli._session_model("http://127.0.0.1:43210", "fixture", "ses_owned", FREE), FREE)
                    self.assertEqual(request.call_args.args[-1], "/api/session/ses_owned/context")
                else:
                    with self.assertRaises(RuntimeError) as error:
                        opencode_cli._session_model("http://127.0.0.1:43210", "fixture", "ses_owned", FREE)
                    self.assertNotIn("PRIVATE", str(error.exception))

    def test_parser_rejects_missing_wrong_and_conflicting_model_metadata(self):
        text = {"type": "text", "sessionID": "ses_owned", "text": "Synthetic"}
        for models in ([], ["openrouter/wrong"], ["openrouter/wrong", FREE]):
            raw = "\n".join(json.dumps(row) for row in [text] + [{"type": "model", "model": model} for model in models])
            with self.subTest(models=models), self.assertRaisesRegex(RuntimeError, "requested model"):
                opencode_cli.parse_output(raw, FREE)

    def test_delete_requires_204_and_get_404_for_every_observed_id(self):
        for statuses, removed in (([204, 404, 204, 404], True), ([200, 404, 204, 404], False),
                                  ([204, 200, 204, 404], False), ([500, 404, 204, 404], False)):
            with mock.patch.object(opencode_cli, "_http_request", side_effect=statuses) as request:
                rows = opencode_cli._delete_sessions("http://127.0.0.1:43210", "fixture", ["ses_one", "ses_two"])
            self.assertEqual(all(row["removed"] for row in rows), removed)
            self.assertEqual(request.call_count, 4)

    def test_bounded_api_failure_does_not_skip_other_session_cleanup(self):
        with mock.patch.object(opencode_cli, "_http_request", side_effect=[
                RuntimeError("OpenCode private API response exceeded the output limit."), 204, 404]) as request:
            rows = opencode_cli._delete_sessions("http://127.0.0.1:43210", "fixture", ["ses_one", "ses_two"])
        self.assertFalse(rows[0]["removed"])
        self.assertTrue(rows[1]["removed"])
        self.assertEqual(request.call_args_list[-2].args[-1], "DELETE")
        self.assertEqual(request.call_count, 3)


@unittest.skipUnless(os.name == "nt", "Windows exact owned jobs")
class OwnedProcessReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_PARENT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.processes = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for process in reversed(self.processes):
            opencode_process.stop(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()

    def start(self, code):
        process = opencode_process.start([sys.executable, "-B", "-c", code], cwd=self.root,
            env={**os.environ, "TMP": str(self.root), "TEMP": str(self.root)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def child_code(self, output=""):
        child = "import sys,threading; print('READY',flush=True); threading.Event().wait()"
        return ("import subprocess,sys; "
            f"p=subprocess.Popen([sys.executable,'-B','-c',{child!r}],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW); "
            "assert p.stdout.readline().strip()==b'READY'; p.stdout.close(); "
            f"print({output!r} or p.pid,flush=True)")

    def test_real_root_exit_surviving_child_is_rejected_and_unrelated_job_is_untouched(self):
        unrelated = self.start("import threading; print('READY',flush=True); threading.Event().wait()")
        self.assertEqual(unrelated.stdout.readline().strip(), b"READY")
        process = self.start(self.child_code())
        stdout, stderr = process.communicate()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, b"")
        child_pid = int(stdout)
        self.assertIn(child_pid, process._owned_job.pids())
        result = opencode_process.stop(process)
        self.assertTrue(result["stopped"])
        self.assertFalse(result["natural_exit"])
        self.assertIn({"pid": child_pid, "state": "live_payload"}, result["exit_states"])
        self.assertIsNone(process._owned_job.handle)
        self.assertIsNone(unrelated.poll())

    def test_real_natural_root_exit_is_not_classified_as_a_survivor(self):
        process = self.start("print('fixture',flush=True)")
        self.assertEqual(process.communicate()[0], b"fixture\r\n")
        result = opencode_process.stop(process)
        self.assertTrue(result["natural_exit"])
        self.assertTrue(result["stopped"])

    def test_assignment_failure_never_resumes_unowned_root(self):
        marker = self.root / "must-not-exist"
        real_popen = subprocess.Popen
        created = []
        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            created.append(process)
            return process
        with mock.patch.object(opencode_process.subprocess, "Popen", side_effect=popen), \
                mock.patch.object(opencode_process, "OwnedJob", side_effect=RuntimeError("fixture assignment failure")):
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                opencode_process.start([sys.executable, "-B", "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('escaped')"], cwd=self.root,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertFalse(marker.exists())
        self.assertIsNotNone(created[0].returncode)
        for stream in (created[0].stdin, created[0].stdout, created[0].stderr):
            self.assertTrue(stream.closed)

    def test_exit_state_signaled_conhost_transient_and_unknown_are_finite(self):
        job = opencode_process.OwnedJob.__new__(opencode_process.OwnedJob)
        job.kernel = mock.Mock()
        job.pids = mock.Mock()
        def system(buffer, length):
            buffer.value = r"C:\Windows\System32"
            return len(buffer.value)
        def image(handle, flags, buffer, length):
            buffer.value = r"C:\Windows\System32\conhost.exe"
            return True
        job.kernel.GetSystemDirectoryW.side_effect = system
        for waits, opened, member, image_ok, expected in (
                ([], False, False, True, "departed"), ([], False, True, True, "unresolved"),
                ([0], True, True, True, "signaled"), ([258, 0], True, True, False, "signaled"),
                ([258, 258], True, True, True, "console_host"),
                ([258, 258], True, True, False, "unresolved"), ([0xFFFFFFFF], True, True, True, "unresolved")):
            with self.subTest(state=expected, waits=waits):
                job.kernel.OpenProcess.return_value = 123 if opened else None
                job.pids.return_value = [77] if member else []
                job.kernel.WaitForSingleObject.side_effect = waits
                job.kernel.QueryFullProcessImageNameW.side_effect = image if image_ok else None
                job.kernel.QueryFullProcessImageNameW.return_value = image_ok
                held = []
                self.assertEqual(job.exit_state(77, held), expected)
                self.assertEqual(held, [123] if opened else [])

    def test_cleanup_classification_and_failed_query_drain_handles_without_claiming_success(self):
        for state in ("departed", "signaled", "console_host", "live_payload", "unresolved"):
            with self.subTest(state=state):
                job = opencode_process.OwnedJob.__new__(opencode_process.OwnedJob)
                job.handle, job.killed, job.exit_states = 456, False, []
                job.kernel = mock.Mock()
                job.kernel.WaitForSingleObject.return_value = 0
                job.pids = mock.Mock(side_effect=[[77], []])
                def classify(pid, held):
                    held.append(123)
                    return state
                job.exit_state = classify
                job.close()
                self.assertEqual(job.killed, state in {"live_payload", "unresolved"})
                self.assertIsNone(job.handle)
                self.assertEqual(job.kernel.CloseHandle.call_args_list, [mock.call(123), mock.call(456)])
        process = mock.Mock()
        process._owned_cleanup = None
        process._owned_job.seen = {77}
        process._owned_job.exit_states = []
        process._owned_job.close.side_effect = RuntimeError("fixture query failure")
        result = opencode_process.stop(process)
        self.assertFalse(result["stopped"])
        self.assertFalse(result["natural_exit"])
        self.assertIs(opencode_process.stop(process), result)
        process.wait.assert_called_once()
        process._handle.Close.assert_called_once()

    def test_model_listing_surviving_child_rejects_catalog_and_cleans_exact_job(self):
        script = self.root / "list-fixture.py"
        script.write_text(self.child_code(FREE), encoding="utf-8")
        processes = []
        def start(*args, **kwargs):
            process = opencode_process.start(*args, **kwargs)
            processes.append(process)
            return process
        with mock.patch.object(opencode_cli, "TEMP_ROOT", self.root / "tasks"), \
                mock.patch.object(opencode_cli, "_spawn_owned", side_effect=start), \
                mock.patch.object(opencode_cli, "_fetch_prices", side_effect=AssertionError("Rejected catalog must not fetch prices")):
            result = opencode_cli._run_models([sys.executable, "-B", str(script)], 123.0)
        self.assertEqual(result["models"], [])
        self.assertTrue(result["error"])
        self.assertTrue(processes[0]._owned_cleanup["stopped"])
        self.assertFalse(processes[0]._owned_cleanup["natural_exit"])
        self.assertIsNone(processes[0]._owned_job.handle)
        self.assertEqual(list((self.root / "tasks").iterdir()), [])

    def test_private_server_startup_root_exit_still_cleans_surviving_child(self):
        script = self.root / "server-fixture.py"
        script.write_text(self.child_code(), encoding="utf-8")
        processes = []
        def start(*args, **kwargs):
            process = opencode_process.start(*args, **kwargs)
            processes.append(process)
            return process
        import urllib.error
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=start), \
                mock.patch.object(opencode_cli, "_http_request", side_effect=urllib.error.URLError("fixture unavailable")):
            with self.assertRaisesRegex(RuntimeError, "exited before it was ready"):
                opencode_cli._start_server([sys.executable, "-B", str(script)],
                    {**os.environ, "OPENCODE_SERVER_PASSWORD": "fixture-password"}, self.root, threading.Event())
        self.assertTrue(processes[0]._owned_cleanup["stopped"])
        self.assertFalse(processes[0]._owned_cleanup["natural_exit"])

    def test_work_inventory_detects_junction_without_reading_its_target(self):
        import _winapi
        target, work = self.root / "target", self.root / "work"
        target.mkdir()
        work.mkdir()
        (target / "private-fixture").write_text("must not read", encoding="utf-8")
        _winapi.CreateJunction(str(target), str(work / "junction"))
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Reparse target read")):
            self.assertEqual(opencode_cli._inventory(work), (("junction", "reparse"),))

    def test_production_invoke_rejects_real_child_even_after_successful_root(self):
        events = "\n".join(json.dumps(row) for row in [
            {"type": "text", "sessionID": "ses_realchild", "text": "Synthetic answer"},
            {"type": "model", "model": FREE}])
        script = self.root / "fixture.py"
        script.write_text(self.child_code(events), encoding="utf-8")
        server = self.start("import threading; print('READY',flush=True); threading.Event().wait()")
        self.assertEqual(server.stdout.readline().strip(), b"READY")
        calls = []
        def request(base, password, path, method="GET"):
            calls.append((path, method))
            return 204 if method == "DELETE" else 404
        with mock.patch.object(opencode_cli, "TEMP_ROOT", self.root / "tasks"), \
                mock.patch.object(opencode_cli, "executable", return_value=[sys.executable, "-B", str(script)]), \
                mock.patch.object(opencode_cli, "_start_server", return_value=(server, "http://127.0.0.1:43210")), \
                mock.patch.object(opencode_cli, "_session_model", return_value=FREE), \
                mock.patch.object(opencode_cli, "_http_request", side_effect=request):
            with self.assertRaisesRegex(RuntimeError, "surviving owned process"):
                opencode_cli.invoke("Synthetic prompt", model=FREE)
        self.assertEqual(calls, [("/api/session/ses_realchild", "DELETE"), ("/api/session/ses_realchild", "GET")])
        self.assertEqual(list((self.root / "tasks").iterdir()), [])

    def test_production_long_stdin_and_eof_succeed_with_real_hidden_fixture(self):
        script = self.root / "stdin-fixture.py"
        script.write_text("import sys,json; payload=sys.stdin.buffer.read(); "
            "print(json.dumps({'type':'text','sessionID':'ses_stdin','text':payload.decode().strip().split()[-1]}),flush=True)",
            encoding="utf-8")
        server = self.start("import threading; print('READY',flush=True); threading.Event().wait()")
        self.assertEqual(server.stdout.readline().strip(), b"READY")
        prompt = "Synthetic padding " * 4000 + "END_MARKER"
        with mock.patch.object(opencode_cli, "TEMP_ROOT", self.root / "tasks"), \
                mock.patch.object(opencode_cli, "executable", return_value=[sys.executable, "-B", str(script)]), \
                mock.patch.object(opencode_cli, "_start_server", return_value=(server, "http://127.0.0.1:43210")), \
                mock.patch.object(opencode_cli, "_session_model", return_value=FREE), \
                mock.patch.object(opencode_cli, "_http_request", side_effect=lambda base, password, path, method="GET": 204 if method == "DELETE" else 404):
            answer, receipt = opencode_cli.invoke(prompt, model=FREE)
        self.assertEqual(answer, "END_MARKER")
        self.assertTrue(receipt["stdin_delivered"])
        self.assertEqual(receipt["stdin_sha256"], hashlib.sha256((prompt + "\n").encode()).hexdigest())
        self.assertGreater(receipt["stdin_bytes"], 60000)
        self.assertTrue(receipt["cleanup"]["run_process"]["natural_exit"])
        self.assertTrue(receipt["cleanup"]["verified"])
        self.assertEqual(list((self.root / "tasks").iterdir()), [])

    def test_cancel_releases_large_stdin_writer_when_real_fixture_never_reads(self):
        script = self.root / "cancel-fixture.py"
        ready = self.root / "cancel-fixture-ready"
        script.write_text("import json,threading; from pathlib import Path; "
            "print(json.dumps({'type':'session','sessionID':'ses_cancel'}),flush=True); "
            f"Path({str(ready)!r}).write_text('ready'); threading.Event().wait()", encoding="utf-8")
        server = self.start("import threading; print('READY',flush=True); threading.Event().wait()")
        self.assertEqual(server.stdout.readline().strip(), b"READY")
        cancel = mock.Mock()
        cancel.is_set.side_effect = [False, False, True]
        calls = []
        def request(base, password, path, method="GET"):
            calls.append((path, method))
            return 204 if method == "DELETE" else 404
        def start(*args, **kwargs):
            process = opencode_process.start(*args, **kwargs)
            self.processes.append(process)
            # Explicit fixture readiness, not a guessed startup delay.
            while not ready.exists():
                self.assertIsNone(process.poll(), "synthetic fixture exited before readiness")
                threading.Event().wait(0.01)
            return process
        with mock.patch.object(opencode_cli, "TEMP_ROOT", self.root / "tasks"), \
                mock.patch.object(opencode_cli, "executable", return_value=[sys.executable, "-B", str(script)]), \
                mock.patch.object(opencode_cli, "_spawn_owned", side_effect=start), \
                mock.patch.object(opencode_cli, "_start_server", return_value=(server, "http://127.0.0.1:43210")), \
                mock.patch.object(opencode_cli, "_http_request", side_effect=request):
            with self.assertRaises(InterruptedError):
                opencode_cli.invoke("x" * 240000, model=FREE, cancel=cancel)
        self.assertEqual(calls, [("/api/session/ses_cancel", "DELETE"), ("/api/session/ses_cancel", "GET")])
        self.assertEqual(list((self.root / "tasks").iterdir()), [])


class LiveProofOfflineReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_PARENT)
        self.addCleanup(self.temp.cleanup)
        self.proof = live_proof
        self.addCleanup(opencode_cli.reset_model_cache)

    def test_execute_uses_real_http_import_worker_saved_output_and_cleanup_offline(self):
        self.run_offline(False)

    def test_execute_records_safe_phase_on_generation_failure_and_cleans_worker(self):
        self.run_offline(True)

    def test_only_fixed_generation_diagnostics_are_persisted(self):
        self.assertEqual(self.proof._safe_failure_code("OpenCode session model metadata could not be verified."), "model_metadata_unverified")
        self.assertIsNone(self.proof._safe_failure_code("PRIVATE arbitrary provider error"))

    def run_offline(self, fail_summary):
        calls, apps = [], []
        def invoke(provider, prompt, workdir, **options):
            self.assertEqual(provider, "opencode")
            self.assertEqual(options["model"], FREE)
            self.assertIn(self.proof.PT1, prompt)
            self.assertTrue(Path(workdir).parent.exists())
            calls.append((prompt, options))
            if len(calls) == 2 and fail_summary:
                raise RuntimeError("PRIVATE fixture detail must not be logged")
            payload = opencode_cli.stdin_payload(prompt)
            answer = prompt.rsplit("\n", 1)[-1] if len(calls) == 1 else "The fixture describes six beds and twelve seedlings."
            return answer, {"requested_model": FREE, "actual_model": FREE, "model": [FREE],
                "session_id": "ses_offline", "stdin_bytes": len(payload), "stdin_sha256": hashlib.sha256(payload).hexdigest(),
                "cleanup": {"verified": True, "sessions": [{"id": "ses_offline", "delete_status": 204,
                "verify_status": 404, "removed": True}], "processes_stopped": True, "workdir_empty": True}}
        real_app = self.proof.Application
        def application(*args, **kwargs):
            app = real_app(*args, **kwargs)
            apps.append(app)
            return app
        def catalog(refresh=False):
            opencode_cli._MODEL_CACHE.update(at=time.monotonic(), executable=("offline",), models=MODELS, error="")
            return {"models": MODELS, "error": ""}
        output = Path(self.temp.name) / "proof"
        with mock.patch.object(ai_cli, "invoke", side_effect=invoke), \
                mock.patch.object(ai_cli, "availability", return_value={}), \
                mock.patch.object(ai_cli, "opencode_model_list", side_effect=catalog), \
                mock.patch.object(self.proof, "Application", side_effect=application), \
                mock.patch.object(opencode_cli, "_spawn_owned", side_effect=AssertionError("No real CLI allowed")), \
                mock.patch.object(opencode_cli.urllib.request, "urlopen", side_effect=AssertionError("No public network allowed")):
            result = self.proof.execute(output)
        receipt = json.loads((output / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(result, int(fail_summary), receipt)
        self.assertEqual(len(calls), 2)
        self.assertGreater(len(calls[0][0]), 60000)
        self.assertTrue(receipt["checks"]["long_prompt"]["marker_returned"])
        for key in ("fixture_data_removed", "server_closed", "worker_stopped"):
            self.assertTrue(receipt["cleanup"][key], receipt)
        self.assertFalse((output / "fixture-data").exists())
        self.assertFalse(apps[0].worker.is_alive())
        if fail_summary:
            self.assertEqual(receipt["phase"], "fixture-summary-running")
            self.assertEqual(receipt["failure_category"], "summary_job")
            self.assertEqual(receipt["generation_failure_category"], "unknown_generation_failure")
            self.assertNotIn("PRIVATE", (output / "receipt.json").read_text(encoding="utf-8"))
        else:
            saved = receipt["checks"]["http_summary"]
            self.assertEqual((saved["provider"], saved["model"]), ("opencode", FREE))
            self.assertTrue(saved["request"]["cleanup"]["verified"])
