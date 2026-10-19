"""Local video upload, lifecycle and Google Files API tests; provider calls use fixtures only."""
import http.client
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gemini
import providers
import server as server_module
from server import Application, Server
from storage import Store

FIXTURE_KEY = "fixture-local-gemini-key-not-real"


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload or {}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        if limit < 0:
            limit = len(self.body)
        body, self.body = self.body[:limit], self.body[limit:]
        return body


def interaction_reply():
    answer = {"language": "en", "segments": [{"start": 0, "text": "Local fixture speech."}],
              "video_ends_in_this_clip": True}
    return Response({"status": "completed",
                     "usage": {"input_tokens_by_modality": [{"modality": "video", "tokens": 40},
                                                              {"modality": "audio", "tokens": 20}],
                               "total_input_tokens": 60, "total_output_tokens": 8},
                     "steps": [{"type": "model_output", "content": [{"type": "text", "text": json.dumps(answer)}]}]})


class FilesOpener:
    def __init__(self, cancel_on_status=False, delete_status=204, status="ACTIVE"):
        self.requests = []
        self.cancel_on_status = cancel_on_status
        self.delete_status = delete_status
        self.status = status
        self.cancel = threading.Event()
        self.uploaded = b""

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        url = request.full_url
        if url == gemini.FILES_UPLOAD_ENDPOINT:
            self.assert_header(request, "X-Goog-Upload-Protocol", "resumable")
            self.assert_header(request, "X-Goog-Upload-Command", "start")
            return Response({}, headers={"X-Goog-Upload-URL":
                "https://generativelanguage.googleapis.com/upload/v1beta/files/session?upload_id=fixture"})
        if "upload_id=fixture" in url:
            self.assert_header(request, "X-Goog-Upload-Offset", "0")
            self.assert_header(request, "X-Goog-Upload-Command", "upload, finalize")
            body = request.data
            self.uploaded = b"".join(body) if not isinstance(body, bytes) else body
            return Response({"file": {"name": "files/local-fixture", "uri":
                            "https://generativelanguage.googleapis.com/v1beta/files/local-fixture",
                            "mimeType": "video/mp4"}})
        if request.get_method() == "DELETE":
            return Response({}, status=self.delete_status)
        if url.startswith(gemini.FILES_ENDPOINT + "/"):
            if self.cancel_on_status:
                self.cancel.set()
            state = "PROCESSING" if self.cancel_on_status else self.status
            return Response({"name": "files/local-fixture", "state": state,
                             "videoMetadata": {"videoDuration": "60s"}})
        if url == gemini.ENDPOINT:
            body = json.loads(request.data)
            video = body["input"][0]
            self.video_input = video
            return interaction_reply()
        raise AssertionError("Unexpected fake Google request: " + url)

    @staticmethod
    def assert_header(request, name, expected):
        headers = {key.lower(): value for key, value in request.header_items()}
        if headers.get(name.lower()) != expected:
            raise AssertionError(f"Unexpected {name} header.")

    def check_auth_headers(self):
        for request, _ in self.requests:
            headers = {key.lower(): value for key, value in request.header_items()}
            assert headers.get("x-goog-api-key") == FIXTURE_KEY
            assert FIXTURE_KEY not in request.full_url


class LocalFileHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(self.temp.name, start_worker=False, start_readiness_check=False,
                               whisper_python=Path(self.temp.name) / "venv" / "python.exe",
                               antigravity_work_root=Path(self.temp.name) / "antigravity-runtime")
        self.app.store.keys["gemini"] = FIXTURE_KEY
        self.set_readiness("ready")
        self.server = Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"
        self.worker_thread = None

    def set_readiness(self, status, reason=None):
        self.app.whisper_readiness = {"status": status, "venv_path": str(self.app.whisper_python)}
        if status == "ready":
            self.app.whisper_readiness["gpu_name"] = "Fixture GPU"
        if reason:
            self.app.whisper_readiness["reason"] = reason
        self.app.whisper_readiness_checked = time.monotonic()
        self.app.whisper_readiness_done.set()
        self.app.whisper_readiness_thread = None

    def tearDown(self):
        self.app.stop.set()
        if self.worker_thread:
            self.worker_thread.join(3)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.app.store.database.close()
        self.temp.cleanup()

    def post(self, body=b"video", route="whisper", name="clip.mp4", token=None, origin=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Content-Type": "video/mp4", "X-AIVideoTranscriber-Token": token or self.app.token,
                   "Origin": origin or self.origin, "X-AIVideoTranscriber-Filename": name,
                   "X-AIVideoTranscriber-File-Route": route}
        if host:
            headers["Host"] = host
        try:
            connection.request("POST", "/api/local-file", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def upload(self, route="whisper", body=b"video", name="clip.mp4"):
        status, result = self.post(body=body, route=route, name=name)
        self.assertEqual(status, 200, result)
        return result

    def preflight(self, filename="clip.mp4", size=5, route="whisper", token=None,
                  origin=None, host=None, raw_body=None, content_length=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Content-Type": "application/json", "X-AIVideoTranscriber-Token": token or self.app.token,
                   "Origin": origin or self.origin}
        if host:
            headers["Host"] = host
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
            body = None
        else:
            body = raw_body if raw_body is not None else json.dumps(
                {"filename": filename, "size": size, "route": route}).encode()
        try:
            connection.request("POST", "/api/local-file/preflight", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def start_worker(self):
        self.worker_thread = threading.Thread(target=self.app.work, daemon=True)
        self.worker_thread.start()

    def wait_job(self, job_id, expected=None):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            job = self.app.store.get("job", job_id)
            if job and job["status"] not in ("queued", "running"):
                if expected:
                    self.assertEqual(job["status"], expected, job)
                return job
            time.sleep(.02)
        self.fail("Local file job did not reach a terminal state.")

    def assert_no_upload_files(self):
        root = self.app.local_files_root
        self.assertFalse(root.exists() and any(root.iterdir()))

    def get_json(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_readiness_endpoint_reports_cached_whisper_and_configured_gemini(self):
        status, result = self.get_json("/api/local-file/readiness")
        self.assertEqual(status, 200)
        self.assertEqual(result["whisper"]["status"], "ready")
        self.assertEqual(result["whisper"]["gpu_name"], "Fixture GPU")
        self.assertEqual(result["gemini"]["status"], "ready")
        self.assertEqual(result["gemini_file_cap_bytes"], 2_000_000_000)

    def test_readiness_recheck_is_async_and_cached(self):
        entered, release = threading.Event(), threading.Event()
        response = []
        self.app.whisper_python = Path(__file__)

        def slow_check(*args, **kwargs):
            entered.set()
            release.wait()
            return mock.Mock(returncode=0, stdout=json.dumps(
                {"status": "ready", "devices": 1, "gpu_name": "Fresh fixture GPU"}))

        with mock.patch.object(server_module.subprocess, "run", side_effect=slow_check) as check:
            request = threading.Thread(
                target=lambda: response.append(self.get_json("/api/local-file/readiness?recheck=1")))
            request.start()
            try:
                self.assertTrue(entered.wait(1))
                request.join(.5)
                self.assertFalse(request.is_alive(), "readiness endpoint waited for the worker check")
                self.assertEqual(response[0][0], 200)
                self.assertEqual(response[0][1]["whisper"]["status"], "checking")
                release.set()
                self.assertTrue(self.app.whisper_readiness_done.wait(1))
                self.assertEqual(self.app.file_readiness()["whisper"]["gpu_name"], "Fresh fixture GPU")
                self.assertEqual(check.call_count, 1)
            finally:
                release.set()
                request.join(1)

    def test_whisper_is_not_a_youtube_provider_or_settings_service(self):
        self.assertNotIn("whisper", providers.PROVIDERS)
        self.assertNotIn("whisper", providers.KEYED_PROVIDERS)
        self.assertNotIn("whisper", self.app.store.public_settings()["keys_configured"])

    def test_upload_uses_host_origin_and_token_guards_and_sanitizes_filename(self):
        status, _ = self.post(token="bad-token")
        self.assertEqual(status, 403)
        status, _ = self.post(origin="http://evil.example")
        self.assertEqual(status, 403)
        status, _ = self.post(host="evil.example")
        self.assertEqual(status, 403)
        accepted = self.upload(name="..%2Fprivate%3Fclip.mp4")
        job = self.app.store.get("job", accepted["job_id"])
        video = self.app.store.get("video", accepted["video_id"])
        self.assertEqual(job["file_name"], "private_clip.mp4")
        self.assertEqual(set(video["local_file"]), {"name", "size_bytes", "duration_seconds"})
        self.assertEqual(video["local_file"]["size_bytes"], 5)
        self.assertTrue((self.app.local_task_dir(job["id"]) / job["file_name"]).is_file())
        self.assertTrue(self.app.cancel(job["id"]))
        self.assertFalse(self.app.local_task_dir(job["id"]).exists())
        self.assertEqual(self.app.store.get("video", video["id"])["file_retry_message"],
                         "Choose the file again to retry.")

    def test_startup_removes_leftover_file_job_folders_and_marks_interrupted_entry(self):
        data = Path(self.temp.name) / "restart-data"
        store = Store(data)
        job_id, video_id = "a" * 32, "file-0123456789ab"
        folder = data / "local-files" / job_id
        folder.mkdir(parents=True)
        (folder / "interrupted.mp4").write_bytes(b"leftover")
        store.put("video", video_id, {"id": video_id, "title": "interrupted", "url": "", "sources": {},
                    "attempts": [], "outputs": {}, "conversation": [], "selected_provider": None,
                    "local_file": {"name": "interrupted.mp4", "size_bytes": 8, "duration_seconds": None}})
        store.put("job", job_id, {"id": job_id, "video_id": video_id, "kind": "file", "status": "running"})
        store.database.close()
        restarted = Application(data, start_worker=False, start_readiness_check=False,
                                antigravity_work_root=data / "antigravity-runtime")
        try:
            self.assertFalse(folder.exists())
            self.assertEqual(restarted.store.get("job", job_id)["status"], "interrupted")
            self.assertEqual(restarted.store.get("video", video_id)["file_retry_message"],
                             "Choose the file again to retry.")
        finally:
            restarted.store.database.close()

    def test_short_body_leaves_no_job_and_no_stored_file(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.connect()
        connection.putrequest("POST", "/api/local-file")
        for name, value in {"Host": f"127.0.0.1:{self.port}", "Origin": self.origin,
                            "X-AIVideoTranscriber-Token": self.app.token,
                            "X-AIVideoTranscriber-Filename": "short.mp4", "X-AIVideoTranscriber-File-Route": "whisper",
                            "Content-Type": "video/mp4", "Content-Length": "10"}.items():
            connection.putheader(name, value)
        connection.endheaders()
        connection.send(b"xx")
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()
        self.assertEqual(self.app.store.list("job"), [])
        self.assertEqual(self.app.store.list("video"), [])
        self.assert_no_upload_files()

    def test_windows_reserved_file_name_is_sanitized(self):
        result = self.upload(name="CON.mp4")
        job = self.app.store.get("job", result["job_id"])
        self.assertEqual(job["file_name"], "_CON.mp4")
        self.assertTrue((self.app.local_files_root / job["id"] / "_CON.mp4").is_file())

    def test_gemini_file_types_use_documented_mimes_and_unsupported_files_have_no_fallback(self):
        self.assertEqual(server_module.local_file_mime("clip.mov"), "video/mov")
        self.assertEqual(server_module.local_file_mime("clip.avi"), "video/avi")
        self.assertEqual(server_module.local_file_mime("clip.wmv"), "video/wmv")
        status, body = self.post(body=b"video", route="gemini", name="clip.mkv")
        self.assertEqual(status, 400)
        self.assertIn("does not support this video file type", body["error"])
        self.assertFalse(self.app.local_files_root.exists())

        status, result = self.post(body=b"video", route="whisper_then_gemini", name="clip.mkv")
        self.assertEqual(status, 200)
        self.assertIn("no Gemini fallback", result["warning"])
        job = self.app.store.get("job", result["job_id"])
        self.assertEqual(job["file_mime"], "video/x-matroska")

    def test_disk_space_refusal_happens_before_reading_the_body(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.connect()
            connection.putrequest("POST", "/api/local-file")
            headers = {"Host": f"127.0.0.1:{self.port}", "Origin": self.origin,
                       "X-AIVideoTranscriber-Token": self.app.token, "X-AIVideoTranscriber-Filename": "large.mp4",
                       "X-AIVideoTranscriber-File-Route": "whisper", "Content-Type": "video/mp4", "Content-Length": "5"}
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            with mock.patch.object(server_module.shutil, "disk_usage", return_value=mock.Mock(free=0)):
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn("not enough free disk space", json.loads(response.read())["error"])
        finally:
            connection.close()
        self.assertEqual(self.app.store.list("job"), [])
        self.assert_no_upload_files()

    def test_google_size_refusal_and_whisper_then_gemini_warning(self):
        with mock.patch.object(server_module, "GEMINI_LOCAL_FILE_CAP", 3):
            status, body = self.post(body=b"four", route="gemini")
            self.assertEqual(status, 400)
            self.assertIn("Use Whisper", body["error"])
            status, body = self.post(body=b"four", route="whisper_then_gemini")
            self.assertEqual(status, 200)
            self.assertIn("no Gemini fallback", body["warning"])
            job = self.app.store.get("job", body["job_id"])
            self.assertEqual(job["kind"], "file")
            self.assertTrue(self.app.cancel(job["id"]))
            self.assertFalse(self.app.local_task_dir(job["id"]).exists())

    def test_preflight_validates_without_creating_a_job_or_upload_file(self):
        status, result = self.preflight(filename="..%2Fpanel.mp4", size=5)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": True, "filename": "panel.mp4", "size": 5, "warning": ""})
        self.assertEqual(self.app.store.list("job"), [])
        self.assertEqual(self.app.store.list("video"), [])
        self.assertFalse(self.app.local_files_root.exists())

    def test_preflight_refuses_invalid_route_name_and_size(self):
        for filename, size, route, message in (
                ("clip.mp4", 5, "unknown", "Choose Whisper"),
                ("clip.txt", 5, "whisper", "Choose a supported video file"),
                ("clip.mp4", 0, "whisper", "non-empty video file"),
                ("clip.mp4", True, "whisper", "valid file size")):
            with self.subTest(filename=filename, size=size, route=route):
                status, result = self.preflight(filename=filename, size=size, route=route)
                self.assertEqual(status, 400)
                self.assertIn(message, result["error"])
        self.assertEqual(self.app.store.list("job"), [])
        self.assertFalse(self.app.local_files_root.exists())

    def test_preflight_refuses_unready_routes_and_disk_shortage(self):
        self.set_readiness("broken", "Fixture CUDA is unavailable")
        status, result = self.preflight(route="whisper")
        self.assertEqual(status, 400)
        self.assertIn("Fixture CUDA is unavailable", result["error"])
        self.set_readiness("ready")
        self.app.store.keys["gemini"] = ""
        status, result = self.preflight(route="gemini")
        self.assertEqual(status, 400)
        self.assertIn("Nothing in the selected route is ready", result["error"])
        self.app.store.keys["gemini"] = FIXTURE_KEY
        with mock.patch.object(server_module.shutil, "disk_usage", return_value=mock.Mock(free=0)):
            status, result = self.preflight(size=5)
        self.assertEqual(status, 400)
        self.assertIn("not enough free disk space", result["error"])
        self.assertEqual(self.app.store.list("job"), [])
        self.assertFalse(self.app.local_files_root.exists())

    def test_preflight_gemini_size_and_type_refusals_keep_exact_server_messages(self):
        with mock.patch.object(server_module, "GEMINI_LOCAL_FILE_CAP", 3):
            status, result = self.preflight(size=4, route="gemini")
            self.assertEqual(status, 400)
            self.assertEqual(result["error"],
                             "Gemini supports local files up to 2,000,000,000 bytes. Use Whisper.")
        status, result = self.preflight(filename="clip.mkv", route="gemini")
        self.assertEqual(status, 400)
        self.assertIn("Gemini does not support this video file type", result["error"])
        self.assertEqual(self.app.store.list("job"), [])
        self.assertFalse(self.app.local_files_root.exists())

    def test_preflight_returns_fallback_warning_without_starting_upload(self):
        with mock.patch.object(server_module, "GEMINI_LOCAL_FILE_CAP", 3):
            status, result = self.preflight(size=4, route="whisper_then_gemini")
        self.assertEqual(status, 200)
        self.assertEqual(result["warning"],
                         "This file exceeds Gemini's 2,000,000,000-byte cap. There will be no Gemini fallback; the server skips Gemini.")
        self.assertEqual(self.app.store.list("job"), [])
        self.assertFalse(self.app.local_files_root.exists())

    def test_preflight_uses_existing_host_origin_token_and_json_size_protections(self):
        for options in ({"host": "untrusted.example"},
                        {"origin": "https://untrusted.example"},
                        {"token": "invalid-page-token"}):
            with self.subTest(options=options):
                status, result = self.preflight(**options)
                self.assertEqual(status, 403)
                self.assertIn("Request rejected", result["error"])
        status, result = self.preflight(content_length=2_000_001)
        self.assertEqual(status, 400)
        self.assertIn("between 1 byte and 2 MB", result["error"])
        self.assertEqual(self.app.store.list("job"), [])
        self.assertFalse(self.app.local_files_root.exists())

    def test_completed_file_item_shape_export_heading_and_cleanup(self):
        payload = {"segments": [{"start": 0, "end": 2, "text": "Known words."}], "language": "en",
                   "language_probability": .99, "duration": 2, "model": "large-v3", "device": "Fixture GPU",
                   "compute_type": "float16", "model_setup_elapsed_seconds": .1,
                   "inference_elapsed_seconds": .3, "elapsed_seconds": .4}
        self.app.run_whisper_worker = lambda job, cancel, path: payload
        self.app.file_fetcher = mock.Mock(side_effect=AssertionError("Whisper success must not call Gemini"))
        accepted = self.upload(name="known-speech.mp4", route="whisper_then_gemini")
        job = self.app.store.get("job", accepted["job_id"])
        self.start_worker()
        self.wait_job(job["id"], "completed")
        video = self.app.store.get("video", accepted["video_id"])
        self.assertEqual((video["title"], video["url"], video["selected_provider"]),
                         ("known-speech", "", "whisper"))
        self.assertEqual(video["local_file"], {"name": "known-speech.mp4", "size_bytes": 5,
                                               "duration_seconds": 2.0})
        source = video["sources"]["whisper"]
        for key in ("segments", "text", "timed_text", "words", "characters", "language",
                    "last_timestamp", "hash", "model_setup_elapsed_seconds",
                    "inference_elapsed_seconds", "elapsed_seconds", "fetched_at", "model"):
            self.assertIn(key, source)
        self.assertEqual(source["inference_elapsed_seconds"], .3)
        text, _ = self.app.export(video["id"], "raw")
        self.assertIn("Source: known-speech.mp4 (file on this PC)", text)
        cleanup_deadline = time.monotonic() + 5
        while time.monotonic() < cleanup_deadline:
            if not self.app.local_task_dir(job["id"]).exists():
                break
            time.sleep(min(0.05, cleanup_deadline - time.monotonic()))
        self.assertFalse(self.app.local_task_dir(job["id"]).exists())
        self.app.file_fetcher.assert_not_called()

    def test_unsupported_gemini_fallback_type_is_recorded_and_whisper_runs(self):
        self.app.run_whisper_worker = lambda job, cancel, path: {
            "segments": [{"start": 0, "end": 2, "text": "Known words."}], "language": "en",
            "duration": 2, "model": "large-v3", "elapsed_seconds": .4}
        self.app.file_fetcher = mock.Mock(side_effect=AssertionError("Unsupported Gemini type must be skipped"))
        accepted = self.upload(name="known-speech.mkv", route="whisper_then_gemini")
        job = self.app.store.get("job", accepted["job_id"])
        self.start_worker()
        self.wait_job(job["id"], "completed")
        video = self.app.store.get("video", accepted["video_id"])
        gemini_attempt = next(item for item in video["attempts"] if item["provider"] == "gemini")
        self.assertEqual((gemini_attempt["status"], gemini_attempt["code"]), ("skipped", "unsupported_file_type"))
        self.assertEqual(video["selected_provider"], "whisper")
        self.app.file_fetcher.assert_not_called()

    def test_whisper_worker_seconds_progress_is_saved_on_the_job_for_activity(self):
        output = "\n".join((
            json.dumps({"type": "progress", "completed_seconds": 750, "total_seconds": 2710,
                       "message": "Transcribing 750.0/2710.0 seconds"}),
            json.dumps({"type": "result", "segments": [], "language": "en", "duration": 60,
                       "model": "large-v3"}), ""))
        process = mock.Mock(pid=12345, stdout=io.StringIO(output))
        process.poll.return_value = 0
        process.wait.return_value = 0
        job = {"id": "c" * 32, "kind": "file", "status": "running"}
        self.app.store.put("job", job["id"], job)
        self.app.whisper_python = Path(sys.executable)
        with mock.patch.object(server_module.subprocess, "Popen", return_value=process), \
             mock.patch.object(server_module.antigravity_source, "_process_receipt", return_value=None):
            self.app.run_whisper_worker(job, threading.Event(), Path(self.temp.name) / "clip.mp4")
        saved = self.app.store.get("job", job["id"])
        self.assertEqual(saved["message"], "Whisper · 12:30 of 45:10")
        self.assertEqual(saved["file_seconds_done"], 750.0)
        self.assertEqual(saved["file_seconds_total"], 2710.0)

    def test_gemini_fallback_runs_after_whisper_failure_and_receives_local_file(self):
        self.app.run_whisper_worker = mock.Mock(side_effect=RuntimeError("Fixture CUDA failure"))
        def fake_gemini(path, key, **options):
            self.assertEqual(key, FIXTURE_KEY)
            self.assertTrue(path.is_file())
            self.assertEqual(options["video_id"].split("-")[0], "file")
            payload = {"language": "en", "metadata": {"title": "fixture"},
                       "segments": [{"text": "Gemini fallback words.", "start": 0, "duration": 2}]}
            return {**providers.normalize("gemini", payload, options["video_id"]),
                    "model": "fixture-model", "elapsed_seconds": .1, "input": "local file",
                    "video_duration_seconds": 2}
        self.app.file_fetcher = mock.Mock(side_effect=fake_gemini)
        accepted = self.upload(route="whisper_then_gemini")
        job = self.app.store.get("job", accepted["job_id"])
        self.start_worker()
        self.wait_job(job["id"], "completed")
        video = self.app.store.get("video", accepted["video_id"])
        self.assertEqual([(item["provider"], item["status"]) for item in video["attempts"]],
                         [("whisper", "failed"), ("gemini", "success")])
        self.assertEqual(video["sources"]["gemini"]["input"], "local file")
        self.assertEqual(video["selected_provider"], "gemini")

    def test_unready_whisper_uses_gemini_only_when_selected_fallback_is_allowed(self):
        self.set_readiness("not_installed")
        self.app.run_whisper_worker = mock.Mock(side_effect=AssertionError("Unready Whisper must not start"))
        def fake_gemini(path, key, **options):
            payload = {"language": "en", "metadata": {"title": "fixture"},
                       "segments": [{"text": "Fallback words.", "start": 0, "duration": 1}]}
            return {**providers.normalize("gemini", payload, options["video_id"]),
                    "model": "fixture-model", "elapsed_seconds": .1, "video_duration_seconds": 1}
        self.app.file_fetcher = mock.Mock(side_effect=fake_gemini)
        accepted = self.upload(route="whisper_then_gemini")
        job = self.app.store.get("job", accepted["job_id"])
        self.start_worker()
        self.wait_job(job["id"], "completed")
        self.app.run_whisper_worker.assert_not_called()
        self.app.file_fetcher.assert_called_once()

    def test_failed_gemini_job_keeps_library_entry_and_cleans_copy(self):
        self.app.file_fetcher = lambda *args, **kwargs: (_ for _ in ()).throw(
            providers.ProviderError("fixture_failure", "Fixture Gemini failure"))
        accepted = self.upload(route="gemini")
        job = self.app.store.get("job", accepted["job_id"])
        self.start_worker()
        finished = self.wait_job(job["id"], "failed")
        self.assertIn("Choose the file again to retry", finished["message"])
        video = self.app.store.get("video", accepted["video_id"])
        self.assertEqual(video["file_retry_message"], "Choose the file again to retry.")
        self.assertIsNotNone(video["local_file"])
        self.assertFalse(self.app.local_task_dir(job["id"]).exists())

    def test_canceled_running_file_job_keeps_library_entry_and_cleans_copy(self):
        entered = threading.Event()
        def wait_for_cancel(job, cancel, path):
            entered.set()
            cancel.wait(3)
            raise InterruptedError("Canceled during fixture transcription.")
        self.app.run_whisper_worker = wait_for_cancel
        accepted = self.upload()
        job = self.app.store.get("job", accepted["job_id"])
        self.start_worker()
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.app.cancel(job["id"]))
        finished = self.wait_job(job["id"], "canceled")
        self.assertIn("Choose the file again to retry", finished["message"])
        self.assertIsNotNone(self.app.store.get("video", accepted["video_id"]))
        self.assertFalse(self.app.local_task_dir(job["id"]).exists())


class GeminiLocalFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / "clip.mp4"
        self.file.write_bytes(b"fixture video bytes")

    def tearDown(self):
        self.temp.cleanup()

    def transcribe(self, opener, cancel=None):
        return gemini.fetch_file(self.file, FIXTURE_KEY, mime_type="video/mp4", title="clip.mp4",
                                 video_id="file-0123456789ab", model="gemini-test", clip_seconds=60,
                                 opener=opener, cancel=cancel, sleep=lambda _: None)

    def test_resumable_upload_streams_and_deletes_with_api_key_only_in_headers(self):
        opener = FilesOpener()
        result = self.transcribe(opener)
        self.assertEqual(opener.uploaded, b"fixture video bytes")
        self.assertEqual(result["input"], "local file")
        self.assertEqual(result["duration_source"], "file_metadata")
        self.assertEqual(result["video_duration_seconds"], 60)
        self.assertEqual(opener.video_input["uri"], "https://generativelanguage.googleapis.com/v1beta/files/local-fixture")
        self.assertEqual(opener.video_input["mime_type"], "video/mp4")
        self.assertEqual(opener.video_input["processing"]["end_offset"], "60s")
        expected_file_url = "https://generativelanguage.googleapis.com/v1beta/files/local-fixture"
        self.assertEqual(opener.requests[2][0].full_url, expected_file_url)
        self.assertEqual(opener.requests[-1][0].get_method(), "DELETE")
        self.assertEqual(opener.requests[-1][0].full_url, expected_file_url)
        opener.check_auth_headers()

    def test_files_api_deletes_after_processing_failure_and_cancel(self):
        failed = FilesOpener(status="FAILED")
        with self.assertRaises(providers.ProviderError):
            self.transcribe(failed)
        self.assertEqual(failed.requests[-1][0].get_method(), "DELETE")
        failed.check_auth_headers()
        canceled = FilesOpener(cancel_on_status=True)
        with self.assertRaises(InterruptedError):
            self.transcribe(canceled, canceled.cancel)
        self.assertEqual(canceled.requests[-1][0].get_method(), "DELETE")
        canceled.check_auth_headers()

    def test_delete_failure_reports_google_auto_expiry(self):
        opener = FilesOpener(delete_status=500)
        with self.assertRaisesRegex(providers.ProviderError, "auto-deletes uploaded files after 48 hours"):
            self.transcribe(opener)
        self.assertEqual(opener.requests[-1][0].get_method(), "DELETE")


if __name__ == "__main__":
    unittest.main()
