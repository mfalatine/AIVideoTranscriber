"""Focused OpenCode CLI, settings, HTTP and cleanup checks. All process and provider output is synthetic."""
import io
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_cli
import opencode_cli
from server import Application, Server
from storage import Store


FREE = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
DEFAULT = FREE
TEMP_PARENT = Path(os.environ.get("TMP") or Path(__file__).resolve().parents[1] / "_TEMP")
MODELS = [{"id": DEFAULT, "label": "Nemotron · Free", "free": True},
          {"id": "openrouter/test/model~variant", "label": "Test · $1.00/M in · $2.00/M out", "free": False}]


def event(value):
    return json.dumps(value, separators=(",", ":")) + "\n"


def successful_events(session="ses_fixture_001", answer="Fixture answer.", model=FREE):
    return (event({"type": "session", "session": {"id": session}})
            + event({"type": "text", "part": {"text": answer}})
            + event({"type": "step_finish", "part": {"model": {"providerID": "openrouter", "id": "nvidia/nemotron-3-super-120b-a12b:free"},
                     "tokens": {"input": 17, "output": 5, "cache": {"read": 2}}, "cost": 0.0}}))


class OpenCodeSecurityTests(unittest.TestCase):
    def test_gate_security_config_denies_global_and_build_tools_and_pins_maintenance_models(self):
        self.assertEqual(opencode_cli.DEFAULT_MODEL, FREE)
        self.assertEqual(opencode_cli.security_config()["model"], FREE)
        config = opencode_cli.security_config(FREE)
        deny = {"action": "*", "resource": "*", "effect": "deny"}
        self.assertEqual(config["permissions"], [deny])
        self.assertEqual(config["agents"]["build"]["permissions"], [deny])
        for name in ("build", "plan", "general", "explore", "title", "summary", "compaction"):
            self.assertEqual(config["agents"][name]["model"], FREE)
        self.assertEqual((config["snapshots"], config["formatter"], config["warming"],
                          config["compaction"]["auto"], config["update"]), (False, False, False, False, "disable"))

    def test_environment_drops_secret_names_without_reading_values_and_keeps_saved_login_profile(self):
        class SecretGuard(dict):
            def __getitem__(self, key):
                if "KEY" in key or "PASSWORD" in key or "TOKEN" in key:
                    raise AssertionError("secret value accessed")
                return super().__getitem__(key)
        supplied = SecretGuard({"HOME": "fixture-home", "XDG_CONFIG_HOME": "fixture-xdg",
                                "OPENAI_API_KEY": "hidden", "CUSTOM_TOKEN": "hidden",
                                "OPENCODE_CONFIG_CONTENT": "old", "CLAUDECODE": "old",
                                "PATH": "fixture-path"})
        with mock.patch.object(opencode_cli.os, "environ", supplied):
            environment = opencode_cli.security_environment("fixture-temp", "private-password", FREE)
        self.assertEqual((environment["HOME"], environment["XDG_CONFIG_HOME"]), ("fixture-home", "fixture-xdg"))
        self.assertEqual((environment["TMP"], environment["TEMP"], environment["TMPDIR"]),
                         ("fixture-temp", "fixture-temp", "fixture-temp"))
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CUSTOM_TOKEN", environment)
        self.assertNotIn("CLAUDECODE", environment)
        self.assertEqual(environment["OPENCODE_SERVER_PASSWORD"], "private-password")
        self.assertEqual(json.loads(environment["OPENCODE_CONFIG_CONTENT"])["model"], FREE)

    def test_prompt_is_utf8_stdin_with_eof_contract_and_never_a_command_argument(self):
        prompt = "café " + "x" * 60_000 + " END_MARKER"
        payload = opencode_cli.stdin_payload(prompt)
        self.assertTrue(payload.startswith("café".encode("utf-8")))
        self.assertTrue(payload.endswith(b"END_MARKER\n"))
        self.assertGreater(len(payload), 60_000)
        args = opencode_cli.command(["opencode.exe"], FREE, "http://127.0.0.1:43210", "summary")
        self.assertEqual(args, ["opencode.exe", "run", "--server", "http://127.0.0.1:43210", "--model", FREE,
                                "--format", "json", "--agent", "build", "--title", "AI Video Transcriber summary"])
        self.assertNotIn(prompt, args)
        with self.assertRaises(ValueError):
            opencode_cli.command(["opencode.exe"], "openrouter/test/model;bad", "http://127.0.0.1:43210", "summary")
        with self.assertRaises(ValueError):
            opencode_cli.command(["opencode.exe"], FREE, "http://127.0.0.1:8776", "summary")

    def test_executable_uses_path_before_desktop_bundle(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as folder:
            root = Path(folder)
            path_cli = root / "path-opencode.exe"
            path_cli.touch()
            bundled = root / "appdata" / "Programs" / "@opencodedesktop" / "resources" / "opencode-cli.exe"
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            with mock.patch.object(opencode_cli.shutil, "which", return_value=str(path_cli)), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "appdata")}):
                self.assertEqual(opencode_cli.executable(), [str(path_cli.resolve())])
            with mock.patch.object(opencode_cli.shutil, "which", return_value=None), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "appdata")}):
                self.assertEqual(opencode_cli.executable(), [str(bundled.resolve())])

    def test_parser_joins_text_projects_session_model_tokens_and_cost(self):
        answer, receipt = opencode_cli.parse_output(successful_events(), FREE)
        self.assertEqual(answer, "Fixture answer.")
        self.assertEqual(receipt["session_id"], "ses_fixture_001")
        self.assertEqual(receipt["session_ids"], ["ses_fixture_001"])
        self.assertEqual(receipt["requested_model"], FREE)
        self.assertEqual(receipt["actual_model"], FREE)
        self.assertEqual(receipt["usage"], {"input_tokens": 17, "output_tokens": 5, "cache_read_tokens": 2})
        self.assertEqual(receipt["cost_usd"], 0)

    def test_parser_preserves_sanitized_provider_error_and_rejects_any_tool_event(self):
        api_key = "sk-or-v1-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        password = "Bearer abcdefghijklmnopqrstuvwxyz012345"
        short_key = "sk-a"
        diagnostic = event({"type": "error", "error": {"name": "FreeTierError", "providerID": "openrouter",
                           "data": {"statusCode": 403,
                                     "message": f"\x1b[31mFree tier denied; Bearer x; Authorization: {password}; "
                                                f"api_key={api_key}; OPENROUTER_API_KEY={short_key}\x1b[0m"}}})
        with self.assertRaises(RuntimeError) as error:
            opencode_cli.parse_output(diagnostic, FREE)
        text = str(error.exception)
        self.assertIn("403", text)
        self.assertIn("FreeTierError", text)
        self.assertIn("openrouter", text)
        self.assertIn("Free tier denied", text)
        self.assertNotIn(api_key, text)
        self.assertNotIn(short_key, text)
        self.assertNotIn("Bearer x", text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("choose another listed model", text)
        self.assertLessEqual(len(text.split("Provider: ", 1)[1]), 300)
        with self.assertRaisesRegex(RuntimeError, "tool event"):
            opencode_cli.parse_output(event({"type": "tool", "part": {"type": "tool", "name": "shell"}}), FREE)


class OpenCodeCatalogTests(unittest.TestCase):
    def setUp(self):
        opencode_cli.reset_model_cache()

    def tearDown(self):
        opencode_cli.reset_model_cache()

    def test_cli_model_parser_rejects_shell_syntax_and_keeps_only_safe_ids(self):
        models = opencode_cli._parse_cli_models("openrouter/acme/model~v2\tAcme V2\n"
                                                "openrouter/bad/model;rm\nnot a model\n"
                                                "openrouter/acme/model~v2\tduplicate\n")
        self.assertEqual(models, [{"id": "openrouter/acme/model~v2", "label": "Acme V2"}])

    def test_cli_model_parser_filters_all_zen_free_ids_and_keeps_openrouter_models(self):
        zen_ids = (
            "opencode/beam-free",
            "opencode/big-pickle",
            "opencode/deepseek-v4.1-flash-free",
            "opencode/glm-5.3-flash-free",
            "opencode/longcat-2.5-preview-free",
            "opencode/mimo-v2.6-flash-free",
            "opencode/nemotron-3-ultra-free",
            "opencode/nemotron-3.5-lightning-free",
            "opencode/nemotron-3.5-super-vl-free",
            "opencode/space-bunny-free",
        )
        paid = "openrouter/acme/paid-model"
        raw = "\n".join((*zen_ids, DEFAULT, paid))
        models = opencode_cli._parse_cli_models(raw)
        self.assertEqual([model["id"] for model in models], [DEFAULT, paid])

    def test_openrouter_prices_map_provider_prefix_free_ids_and_sort_valid_costs(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.close()
        requested = []
        payload = {"data": [
            {"id": "test/paid", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
            {"id": "test/costly", "pricing": {"prompt": "0.000010", "completion": "0.000020"}},
            {"id": "test/bad", "pricing": {"prompt": "NaN", "completion": "-1"}}]}
        def opener(request, timeout):
            requested.append((request.full_url, request.headers, timeout))
            return Response(json.dumps(payload).encode())
        prices = opencode_cli._fetch_prices(opener)
        catalog = opencode_cli._catalog_models([
            {"id": "openrouter/test/costly", "label": "Costly"},
            {"id": "openrouter/test/paid", "label": "Paid"},
            {"id": "openrouter/test/free:free", "label": "Free"},
            {"id": "openrouter/test/bad", "label": "Unpriced"}], prices)
        self.assertEqual(requested[0][0], opencode_cli.MODEL_CATALOG_URL)
        self.assertNotIn("Authorization", requested[0][1])
        self.assertEqual(requested[0][2], 20)
        self.assertEqual([item["id"] for item in catalog], ["openrouter/test/free:free",
                         "openrouter/test/paid", "openrouter/test/costly", "openrouter/test/bad"])
        self.assertIn("Free", catalog[0]["label"])
        self.assertIn("$1.00/M in", catalog[1]["label"])
        self.assertIsNone(catalog[-1]["input_price_per_million"])

    def test_pricing_failure_does_not_discard_models(self):
        models = opencode_cli._parse_cli_models("openrouter/test/model\n")
        self.assertEqual(opencode_cli._catalog_models(models, {})[0]["id"], "openrouter/test/model")

    def test_cache_is_ten_minutes_and_fixture_info_never_starts_model_discovery(self):
        clock = [100.0]
        def now():
            return clock[0]
        def fake_run(args, moment):
            return {"at": moment, "executable": tuple(args), "models": MODELS, "error": ""}
        with mock.patch.object(opencode_cli, "executable", return_value=["opencode.exe"]), \
                mock.patch.object(opencode_cli, "_run_models", side_effect=fake_run) as run:
            first = opencode_cli.model_list(clock=now)
            clock[0] = 699.9
            self.assertEqual(opencode_cli.model_list(clock=now), first)
            self.assertEqual(run.call_count, 1)
            clock[0] = 700.1
            opencode_cli.model_list(clock=now)
            self.assertEqual(run.call_count, 2)
        with mock.patch.object(opencode_cli, "model_list", side_effect=AssertionError("fixture info spawned CLI")):
            self.assertEqual(opencode_cli.info("fixture-home")["models"], [])

    def test_model_cache_lookup_rejects_unknown_and_expired_ids(self):
        opencode_cli._MODEL_CACHE.update(at=time.monotonic(), executable=("fixture",), models=MODELS, error="")
        self.assertTrue(opencode_cli.model_is_listed(DEFAULT))
        self.assertFalse(opencode_cli.model_is_listed("openrouter/test/unknown"))
        opencode_cli._MODEL_CACHE["at"] = time.monotonic() - opencode_cli.MODEL_CACHE_SECONDS - 1
        self.assertFalse(opencode_cli.model_is_listed(DEFAULT))

    def test_model_list_command_is_hidden_cached_output_and_uses_the_safe_environment(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as folder:
            root = Path(folder)
            class ListingProcess:
                returncode = 0
                def __init__(self, args, **kwargs):
                    self.args, self.kwargs = args, kwargs
                def wait(self):
                    self.kwargs["stdout"].write(
                        f"opencode/glm-5.3-flash-free\n{DEFAULT}\tNemotron\n"
                        "openrouter/test/paid-model\tPaid\n".encode())
                    self.kwargs["stdout"].flush()
                    return 0
            created = []
            def spawn(args, **kwargs):
                process = ListingProcess(args, **kwargs)
                created.append(process)
                return process
            with mock.patch.object(opencode_cli, "TEMP_ROOT", root), \
                    mock.patch.object(opencode_cli, "_spawn_owned", side_effect=spawn), \
                    mock.patch.object(opencode_cli, "_fetch_prices", return_value={}), \
                    mock.patch.object(opencode_cli, "_stop_owned", return_value={"stopped": True, "natural_exit": True}):
                result = opencode_cli._run_models(["opencode.exe"], 100.0)
            process, = created
            self.assertEqual(process.args, ["opencode.exe", "models"])
            self.assertIs(process.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(process.kwargs["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
            self.assertEqual([model["id"] for model in result["models"]],
                             [DEFAULT, "openrouter/test/paid-model"])
            self.assertEqual(list(root.iterdir()), [])


class OpenCodeLifecycleTests(unittest.TestCase):
    def test_private_server_is_owned_loopback_and_checks_generated_and_wrong_passwords(self):
        with tempfile.TemporaryDirectory(dir=TEMP_PARENT) as folder:
            process = FakeProcess()
            environment = {"OPENCODE_SERVER_PASSWORD": "fixture-private-password"}
            calls = []
            def request(base, password, path, method="GET", timeout=5):
                # _start_server bounds each private readiness request by its remaining call timeout.
                calls.append((base, password, path, method))
                return 200 if password == environment["OPENCODE_SERVER_PASSWORD"] else 401
            with mock.patch.object(opencode_cli, "_free_port", return_value=43210), \
                    mock.patch.object(opencode_cli, "_spawn_owned", return_value=process) as spawn, \
                    mock.patch.object(opencode_cli, "_http_request", side_effect=request):
                started, base = opencode_cli._start_server(["opencode.exe"], environment, Path(folder), threading.Event())
            self.assertIs(started, process)
            self.assertEqual(base, "http://127.0.0.1:43210")
            args = spawn.call_args.args[0]
            self.assertEqual(args, ["opencode.exe", "serve", "--hostname", "127.0.0.1", "--port", "43210"])
            self.assertEqual([row[1] for row in calls], ["fixture-private-password", "invalid-private-probe"])
            self.assertEqual([row[0] for row in calls], [base, base])

    def test_private_server_auth_failure_stops_only_the_process_it_started(self):
        process = FakeProcess()
        def stop_owned(found):
            found.returncode = 0
            return {"stopped": True, "natural_exit": True}
        with mock.patch.object(opencode_cli, "_free_port", return_value=43211), \
                mock.patch.object(opencode_cli, "_spawn_owned", return_value=process), \
                mock.patch.object(opencode_cli, "_http_request", return_value=200), \
                mock.patch.object(opencode_cli, "_stop_owned", side_effect=stop_owned) as stop:
            with self.assertRaisesRegex(RuntimeError, "did not enforce authentication"):
                opencode_cli._start_server(["opencode.exe"], {"OPENCODE_SERVER_PASSWORD": "fixture-password"},
                                           Path(TEMP_PARENT), threading.Event())
        stop.assert_called_once_with(process)


class FakeProcess:
    next_pid = 100

    def __init__(self, output="", cancel=None, add_file=False, timeout_after_output=False, exit_code=0, stderr=""):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        self.output = output.encode("utf-8")
        self.cancel = cancel
        self.add_file = add_file
        self.timeout_after_output = timeout_after_output
        self.exit_code = exit_code
        self.stderr = stderr.encode("utf-8")
        self.stdin_data = None
        self.kwargs = None
        process = self
        class Input(io.BytesIO):
            def close(self):
                if not self.closed:
                    process.stdin_data = self.getvalue()
                super().close()
        self.stdin = Input()

    def poll(self):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.stdin_data = input
        if self.kwargs is not None:
            if self.output:
                self.kwargs["stdout"].write(self.output)
                self.kwargs["stdout"].flush()
                self.output = b""
            if self.stderr:
                self.kwargs["stderr"].write(self.stderr)
                self.kwargs["stderr"].flush()
                self.stderr = b""
            if self.add_file:
                (Path(self.kwargs["cwd"]) / "unapproved.txt").write_text("fixture", encoding="utf-8")
                self.add_file = False
        if self.timeout_after_output:
            self.timeout_after_output = False
            time.sleep(timeout + 0.02)
            raise subprocess.TimeoutExpired("opencode", timeout)
        if self.cancel is not None:
            self.cancel.set()
            raise subprocess.TimeoutExpired("opencode", timeout)
        self.returncode = self.exit_code
        return None, None


class OpenCodeInvokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_PARENT)
        self.root = Path(self.temp.name)
        self.server = FakeProcess()
        self.run = FakeProcess(successful_events())
        self.http = []
        def request(base, password, path, method="GET"):
            self.http.append((base, password, path, method))
            return 204 if method == "DELETE" else 404
        def stop(process):
            process.returncode = 0
            return {"stopped": True, "natural_exit": True}
        self.patches = [
            mock.patch.object(opencode_cli, "TEMP_ROOT", self.root),
            mock.patch.object(opencode_cli, "executable", return_value=["opencode.exe"]),
            mock.patch.object(opencode_cli, "_start_server", return_value=(self.server, "http://127.0.0.1:43210")),
            mock.patch.object(opencode_cli, "_http_request", side_effect=request),
            mock.patch.object(opencode_cli, "_stop_owned", side_effect=stop),
            mock.patch.object(opencode_cli, "_session_model", return_value=FREE),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.temp.cleanup()

    def fake_popen(self, args, **kwargs):
        self.assertEqual(args[1], "run")
        self.assertIn("--server", args)
        self.assertIn(FREE, args)
        self.assertNotIn("long prompt", args)
        self.assertEqual(list(Path(kwargs["cwd"]).iterdir()), [])
        self.run.kwargs = kwargs
        return self.run

    def test_invoke_delivers_long_prompt_on_stdin_and_deletes_only_observed_session(self):
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen):
            answer, receipt = opencode_cli.invoke("long prompt " + "x" * 60_000 + " END_MARKER", model=FREE, kind="summary")
        self.assertEqual(answer, "Fixture answer.")
        self.assertGreater(receipt["stdin_bytes"], 60_000)
        self.assertEqual(receipt["session_id"], "ses_fixture_001")
        self.assertEqual(receipt["requested_model"], FREE)
        self.assertEqual(receipt["actual_model"], FREE)
        self.assertEqual(receipt["cleanup"]["sessions"], [{"id": "ses_fixture_001", "delete_status": 204,
                                                             "verify_status": 404, "removed": True}])
        self.assertTrue(receipt["cleanup"]["verified"])
        self.assertTrue(receipt["cleanup"]["workdir_empty"])
        self.assertTrue(receipt["cleanup"]["temporary_folder_removed"])
        self.assertTrue(self.run.stdin_data.endswith(b"END_MARKER\n"))
        self.assertEqual([(row[2], row[3]) for row in self.http],
                         [("/api/session/ses_fixture_001", "DELETE"), ("/api/session/ses_fixture_001", "GET")])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_tool_event_fails_and_its_observed_session_is_still_deleted(self):
        self.run.output = (event({"type": "session", "session": {"id": "ses_fixture_002"}})
                           + event({"type": "tool", "part": {"type": "tool", "name": "shell"}})).encode()
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen):
            with self.assertRaisesRegex(RuntimeError, "tool event"):
                opencode_cli.invoke("fixture prompt", model=FREE)
        self.assertEqual([(row[2], row[3]) for row in self.http],
                         [("/api/session/ses_fixture_002", "DELETE"), ("/api/session/ses_fixture_002", "GET")])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancellation_cleans_the_exact_session_from_partial_output(self):
        cancel = threading.Event()
        self.run.output = event({"type": "session", "session": {"id": "ses_fixture_003"}}).encode()
        self.run.cancel = cancel
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen):
            with self.assertRaises(InterruptedError):
                opencode_cli.invoke("fixture prompt", model=FREE, cancel=cancel)
        self.assertEqual([(row[2], row[3]) for row in self.http],
                         [("/api/session/ses_fixture_003", "DELETE"), ("/api/session/ses_fixture_003", "GET")])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_timeout_during_private_server_startup_stops_that_server_and_removes_task_folder(self):
        self.patches[2].stop()
        self.patches[3].stop()
        calls = []
        def readiness(base, password, path, method="GET", timeout=5):
            calls.append((base, password, path, method, timeout))
            time.sleep(timeout + 0.02)
            return 503
        with mock.patch.object(opencode_cli, "_free_port", return_value=43212), \
                mock.patch.object(opencode_cli, "_spawn_owned", return_value=self.server) as spawn, \
                mock.patch.object(opencode_cli, "_http_request", side_effect=readiness):
            with self.assertRaises(TimeoutError) as error:
                opencode_cli.invoke("fixture prompt", model=FREE, timeout=0.2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2].split("?", 1)[0], "/api/agent")
        self.assertGreater(calls[0][4], 0)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(spawn.call_args.args[0][1], "serve")
        self.assertTrue(self.server.returncode is not None)
        cleanup = error.exception.opencode_cleanup
        self.assertTrue(cleanup["verified"])
        self.assertTrue(cleanup["server_process"]["stopped"])
        self.assertTrue(cleanup["temporary_folder_removed"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_timeout_during_run_stops_both_jobs_closes_pipe_and_deletes_partial_session(self):
        session = "ses_timeout_partial"
        self.run = FakeProcess(event({"type": "session", "session": {"id": session}}),
                               timeout_after_output=True)
        input_pipe = self.run.stdin
        def spawn_with_session_event(args, **kwargs):
            process = self.fake_popen(args, **kwargs)
            process.kwargs["stdout"].write(process.output)
            process.kwargs["stdout"].flush()
            process.output = b""
            return process
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=spawn_with_session_event):
            with self.assertRaises(TimeoutError) as error:
                opencode_cli.invoke("fixture prompt", model=FREE, timeout=0.5)
        cleanup = error.exception.opencode_cleanup
        self.assertTrue(cleanup["verified"])
        self.assertTrue(cleanup["run_process"]["stopped"])
        self.assertTrue(cleanup["server_process"]["stopped"])
        self.assertTrue(cleanup["temporary_folder_removed"])
        self.assertTrue(input_pipe.closed)
        self.assertEqual([(row[2], row[3]) for row in self.http], [
            (f"/api/session/{session}", "DELETE"), (f"/api/session/{session}", "GET")])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_nonzero_run_uses_bounded_sanitized_stderr_reason(self):
        api_key = "sk-or-v1-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        self.run = FakeProcess(successful_events(), exit_code=1,
                               stderr=f"\x1b[31mHTTP 429 RateLimitError: rate limit reached; token={api_key}; "
                                      + "x" * 500 + "\x1b[0m")
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen):
            with self.assertRaises(RuntimeError) as error:
                opencode_cli.invoke("fixture prompt", model=FREE)
        text = str(error.exception)
        self.assertIn("choose another listed model", text)
        diagnostic = text.split("Provider: ", 1)[1]
        self.assertIn("429", diagnostic)
        self.assertIn("RateLimitError", diagnostic)
        self.assertIn("rate limit reached", diagnostic)
        self.assertLessEqual(len(diagnostic), 300)
        self.assertNotIn(api_key, text)
        self.assertNotIn("\x1b", text)
        self.assertTrue(error.exception.opencode_cleanup["verified"])

    def test_unapproved_workdir_file_fails_even_when_the_model_returns_text(self):
        self.run.add_file = True
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen):
            with self.assertRaisesRegex(RuntimeError, "created files"):
                opencode_cli.invoke("fixture prompt", model=FREE)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancel_before_start_does_not_create_a_task_or_spawn_process(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(opencode_cli, "_spawn_owned") as spawn:
            with self.assertRaises(InterruptedError):
                opencode_cli.invoke("fixture prompt", model=FREE, cancel=cancel)
        spawn.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_multiple_sessions_fail_but_every_observed_session_is_deleted(self):
        self.run.output = (successful_events() + event({"type": "session", "sessionID": "ses_second"})).encode()
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen):
            with self.assertRaisesRegex(RuntimeError, "one exact session"):
                opencode_cli.invoke("fixture prompt", model=FREE)
        self.assertEqual([(row[2], row[3]) for row in self.http], [
            ("/api/session/ses_fixture_001", "DELETE"), ("/api/session/ses_fixture_001", "GET"),
            ("/api/session/ses_second", "DELETE"), ("/api/session/ses_second", "GET")])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_error_and_missing_metadata_still_delete_observed_sessions(self):
        for mode in ("provider-error", "missing-model"):
            with self.subTest(mode=mode):
                self.http.clear()
                self.run = FakeProcess(successful_events() + (event({"type": "error", "error": {
                                                            "name": "FreeTierError", "data": {
                                                                "statusCode": 403,
                                                                "message": "provider access denied"}}})
                                                            if mode == "provider-error" else ""))
                with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen), \
                        mock.patch.object(opencode_cli, "_session_model", side_effect=(
                            RuntimeError("OpenCode session model metadata could not be verified.")
                            if mode == "missing-model" else None), return_value=FREE):
                    with self.assertRaises(RuntimeError) as error:
                        opencode_cli.invoke("fixture prompt", model=FREE)
                if mode == "provider-error":
                    self.assertIn("403 FreeTierError", str(error.exception))
                    self.assertIn("provider access denied", str(error.exception))
                self.assertEqual([(row[2], row[3]) for row in self.http], [
                    ("/api/session/ses_fixture_001", "DELETE"), ("/api/session/ses_fixture_001", "GET")])
                self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_session_and_delete_verification_failure_never_return_an_answer(self):
        self.run.output = event({"type": "text", "text": "Synthetic answer", "model": FREE}).encode()
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen):
            with self.assertRaisesRegex(RuntimeError, "one exact session"):
                opencode_cli.invoke("fixture prompt", model=FREE)
        self.assertEqual(self.http, [])
        self.run = FakeProcess(successful_events())
        with mock.patch.object(opencode_cli, "_spawn_owned", side_effect=self.fake_popen), \
                mock.patch.object(opencode_cli, "_http_request", return_value=200):
            with self.assertRaisesRegex(RuntimeError, "cleanup could not be verified"):
                opencode_cli.invoke("fixture prompt", model=FREE)
        self.assertEqual(list(self.root.iterdir()), [])


class OpenCodeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_PARENT)
        self.store = Store(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.database.close)

    def test_saved_opencode_model_must_be_listed_and_effort_is_rejected(self):
        listed = {DEFAULT, FREE, "openrouter/test/model~variant"}
        with mock.patch.object(opencode_cli, "model_is_listed", side_effect=lambda model: model in listed):
            saved = self.store.save_settings({"ai_provider": "opencode", "ai_model": FREE})
            self.assertEqual((saved["ai_provider"], saved["ai_model"], saved["ai_effort"]), ("opencode", FREE, ""))
            with self.assertRaisesRegex(ValueError, "current model list"):
                self.store.save_settings({"ai_provider": "opencode", "ai_model": "openrouter/unknown"})
            with self.assertRaisesRegex(ValueError, "no reasoning effort"):
                self.store.save_settings({"ai_provider": "opencode", "ai_model": DEFAULT, "ai_effort": "high"})
        with self.assertRaisesRegex(ValueError, "unsupported characters"):
            self.store.save_settings({"ai_provider": "opencode", "ai_model": "model with spaces"})
        self.assertEqual(self.store.config["ai_model"], FREE)

    def test_opencode_is_registered_without_changing_other_provider_defaults(self):
        self.assertIn("opencode", ai_cli.CLIS)
        self.assertEqual(ai_cli.CLIS["opencode"]["label"], "OpenCode CLI")
        self.assertEqual(self.store.config["ai_provider"], "codex")
        with mock.patch.object(opencode_cli, "executable", return_value=["opencode.exe"]):
            args = ai_cli.command("opencode", FREE, server="http://127.0.0.1:43210", kind="ask")
            with self.assertRaisesRegex(ValueError, "no reasoning effort"):
                ai_cli.command("opencode", FREE, "high", server="http://127.0.0.1:43210")
        self.assertEqual(args[-2:], ["--title", "AI Video Transcriber ask"])
        with mock.patch.object(opencode_cli, "invoke", return_value=("answer", {"requested_model": FREE})) as invoke:
            self.assertEqual(ai_cli.invoke("opencode", "prompt", "ignored", model=FREE, kind="summary", timeout=42),
                             ("answer", {"requested_model": FREE}))
        invoke.assert_called_once_with("prompt", model=FREE, kind="summary", cancel=mock.ANY, timeout=42)

    def test_ai_job_passes_task_kind_to_opencode_and_persists_the_existing_result_shape(self):
        calls = []
        def runner(provider, prompt, workdir, **options):
            calls.append((provider, prompt, workdir, options))
            return "Synthetic summary.", {"requested_model": options["model"], "session_id": "ses_fixture_job"}
        app = Application(Path(self.temp.name) / "job-data", ai_runner=runner, start_worker=False,
                          start_readiness_check=False,
                          antigravity_work_root=Path(self.temp.name) / "agy-runtime")
        self.addCleanup(app.store.database.close)
        with mock.patch.object(opencode_cli, "model_is_listed", return_value=True):
            app.store.save_settings({"ai_provider": "opencode", "ai_model": FREE})
        video = app.import_text({"title": "Fixture", "text": "Six garden beds have twelve seedlings."})
        job_id = app.submit(video["id"], "summary", enqueue=False)
        job = app.store.get("job", job_id)
        app.ai_job(job, threading.Event())
        provider, prompt, workdir, options = calls[0]
        self.assertEqual(provider, "opencode")
        self.assertIn("TASK: summary", prompt)
        self.assertEqual(options["kind"], "summary")
        self.assertEqual(options["model"], FREE)
        saved = app.store.get("video", video["id"])["outputs"]["summary"]
        self.assertEqual((saved["text"], saved["provider"], saved["model"]), ("Synthetic summary.", "opencode", FREE))
        self.assertEqual(saved["receipts"][0]["session_id"], "ses_fixture_job")


class OpenCodeHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_PARENT)
        self.addCleanup(self.temp.cleanup)
        real_availability = ai_cli.availability
        self.availability = mock.patch.object(ai_cli, "availability", lambda home=None: real_availability(self.temp.name))
        self.availability.start()
        self.addCleanup(self.availability.stop)
        self.app = Application(self.temp.name, start_worker=False, start_readiness_check=False,
                               antigravity_work_root=Path(self.temp.name) / "agy-runtime")
        self.server = Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.app.stop.set()
        self.app.store.database.close()

    def request(self, method, path, payload=None, token=True):
        import http.client
        headers = {"Content-Type": "application/json", "Origin": self.origin}
        if token:
            headers["X-AIVideoTranscriber-Token"] = self.app.token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw
            return response.status, body
        finally:
            connection.close()

    def test_bootstrap_and_page_do_not_run_model_discovery_but_protected_route_does(self):
        with mock.patch.object(ai_cli, "opencode_model_list", return_value={"models": MODELS, "error": ""}) as list_models:
            self.assertEqual(self.request("GET", "/api/bootstrap")[0], 200)
            self.assertEqual(self.request("GET", "/")[0], 200)
            list_models.assert_not_called()
            self.assertEqual(self.request("POST", "/api/opencode-models", {}, token=False)[0], 403)
            status, result = self.request("POST", "/api/opencode-models", {})
            self.assertEqual(status, 200)
            self.assertEqual([model["id"] for model in result["models"]], [model["id"] for model in MODELS])
            list_models.assert_called_once_with(False)

    def test_catalog_remains_usable_when_price_fetch_failed(self):
        unavailable_prices = [{"id": DEFAULT, "label": "Nemotron · Free", "free": True}]
        with mock.patch.object(ai_cli, "opencode_model_list", return_value={"models": unavailable_prices, "error": ""}):
            status, result = self.request("POST", "/api/opencode-models", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["models"][0]["id"], DEFAULT)

    def test_settings_route_accepts_a_listed_model_and_rejects_an_unknown_one(self):
        with mock.patch.object(opencode_cli, "model_is_listed", side_effect=lambda model: model in (DEFAULT, FREE)):
            status, saved = self.request("POST", "/api/settings", {"ai_provider": "opencode", "ai_model": FREE})
            self.assertEqual(status, 200)
            self.assertEqual((saved["ai_provider"], saved["ai_model"]), ("opencode", FREE))
            status, error = self.request("POST", "/api/settings", {"ai_provider": "opencode", "ai_model": "openrouter/unknown"})
            self.assertEqual(status, 400)
            self.assertIn("current model list", error["error"])
            self.assertEqual(self.app.store.config["ai_model"], FREE)

    def test_job_route_rejects_a_model_missing_from_cache_without_starting_model_discovery(self):
        with mock.patch.object(opencode_cli, "model_is_listed", return_value=True):
            self.request("POST", "/api/settings", {"ai_provider": "opencode", "ai_model": FREE})
        video = self.app.import_text({"title": "Fixture", "text": "Six garden beds have twelve seedlings."})
        with mock.patch.object(opencode_cli, "model_is_listed", return_value=False), \
                mock.patch.object(ai_cli, "opencode_model_list", side_effect=AssertionError("job route spawned discovery")):
            status, error = self.request("POST", "/api/ai", {"id": video["id"], "kind": "summary"})
        self.assertEqual(status, 400)
        self.assertIn("current model list", error["error"])
        self.assertEqual(self.app.store.list("job"), [])


if __name__ == "__main__":
    unittest.main()
