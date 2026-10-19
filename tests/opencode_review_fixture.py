"""Offline review fixture extracted from the Job 11 live proof."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import threading
import time
import urllib.parse

import ai_cli
from server import Application, Server

MODEL = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
PT1 = ("During development, test changed behavior and affected interactions; reuse valid evidence. "
       "At final validation, verify the builder's test results and coverage without duplicating a full run. "
       "Any additional run needs a named risk or diagnostic question and a stopping condition. "
       "Include this requirement in every outward AI prompt you create.")
def _now():
    return datetime.now(timezone.utc).isoformat()


def _safe_failure_code(value):
    # Only fixed adapter messages become durable categories; free text is discarded.
    text = str(value)
    for prefix, code in (
            ("OpenCode session model metadata could not be verified.", "model_metadata_unverified"),
            ("OpenCode did not verify the requested model", "model_mismatch"),
            ("OpenCode reported a tool event", "tool_event"),
            ("OpenCode process ownership could not be established.", "process_ownership"),
            ("OpenCode left a surviving owned process", "surviving_owned_process"),
            ("OpenCode session or process cleanup could not be verified", "cleanup_unverified"),
            ("OpenCode private-server process cleanup could not be verified", "cleanup_unverified"),
            ("OpenCode prompt delivery could not be verified.", "stdin_delivery_unverified"),
            ("OpenCode did not report one exact session ID", "session_identity_unverified"),
            ("OpenCode created files in its empty work folder", "workdir_changed"),
            ("OpenCode could not complete the request.", "generation_failed")):
        if text.startswith(prefix):
            return code
    return None


def _write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


class _LocalApi:
    """Send fixture API requests over isolated loopback HTTP."""

    def __init__(self, server, app):
        self.server = server
        self.app = app

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Origin": f"http://127.0.0.1:{self.server.server_port}",
                   "Content-Type": "application/json",
                   "X-AIVideoTranscriber-Token": self.app.token}
        try:
            connection.request(method, path, None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse()
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000 or response.status != 200:
                raise RuntimeError(f"Fixture AI Video Transcriber route returned HTTP {response.status}.")
            return json.loads(raw)
        finally:
            connection.close()


def execute(outputdir):
    """Run fixture-only long-stdin and normal-application summary checks; return 0 only for verified cleanup."""
    output = Path(outputdir).resolve()
    if output.exists():
        if any(output.iterdir()):
            raise FileExistsError("Live proof output directory must be empty.")
    else:
        output.mkdir(parents=True)
    fixture = output / "fixture-data"
    data_dir = fixture / "library"
    receipt_path, progress_path = output / "receipt.json", output / "progress.json"
    receipt = {"status": "running", "model": MODEL, "started_utc": _now(), "checks": {}, "cleanup": {}}
    app = server = server_thread = None
    fixture_removed = False

    def phase(name):
        receipt["phase"] = name
        receipt["updated_utc"] = _now()
        _write(receipt_path, receipt)
        _write(progress_path, {"phase": name, "updated_utc": receipt["updated_utc"], "stall_reported": False})

    try:
        phase("long-stdin-starting")
        fixture.mkdir(parents=True, exist_ok=False)
        marker = "TD_OPENCODE_LONG_PROMPT_" + hashlib.sha256(_now().encode()).hexdigest()[:16]
        prompt = (PT1 + "\n\nReturn exactly the final marker and no other text. The transcript body is synthetic padding.\n"
                  + ("Fixture transcript padding. " * 2_300) + "\n\n" + marker)
        if len(prompt) < 60_000 or not prompt.endswith(marker):
            raise RuntimeError("Long prompt fixture did not meet its size and final-marker checks.")
        answer, long_receipt = ai_cli.invoke("opencode", prompt, fixture / "long-work", model=MODEL, kind="formatted")
        if marker not in answer:
            raise RuntimeError("OpenCode did not return the required long-prompt marker.")
        expected = (prompt + "\n").encode("utf-8")
        if (long_receipt.get("stdin_bytes") != len(expected)
                or long_receipt.get("stdin_sha256") != hashlib.sha256(expected).hexdigest()
                or long_receipt.get("actual_model") != MODEL
                or not long_receipt.get("cleanup", {}).get("verified")):
            raise RuntimeError("Long-prompt delivery, selected model or cleanup was not verified.")
        receipt["checks"]["long_prompt"] = {"passed": True, "prompt_characters": len(prompt),
            "stdin_bytes": long_receipt.get("stdin_bytes"), "stdin_sha256": long_receipt.get("stdin_sha256"),
            "marker_returned": True, "request": long_receipt}

        phase("fixture-server-starting")
        app = Application(data_dir, start_worker=True, antigravity_work_root=fixture / "agy-runtime",
                          start_readiness_check=False)
        server = Server(0, app)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        api = _LocalApi(server, app)
        api.request("GET", "/api/bootstrap")

        phase("model-catalog-loading")
        catalog = api.request("POST", "/api/opencode-models", {})
        if not any(model.get("id") == MODEL for model in catalog.get("models", [])):
            raise RuntimeError("The free Nemotron model is not in OpenCode's current model list.")
        api.request("POST", "/api/settings", {"ai_provider": "opencode", "ai_model": MODEL, "ai_effort": "",
                    "instructions": app.store.config["instructions"] + "\n" + PT1})

        phase("fixture-transcript-submitting")
        imported = api.request("POST", "/api/import", {"title": "OpenCode integration fixture", "text":
            "This synthetic test transcript describes six blue garden beds and twelve seedlings. "
            "It contains no user data. The required count is six beds and twelve seedlings."})
        video_id = imported["id"]
        queued = api.request("POST", "/api/ai", {"id": video_id, "kind": "summary"})
        job_id = queued.get("job_id")
        if not job_id:
            raise RuntimeError("The fixture summary job was not queued.")

        phase("fixture-summary-running")
        while True:
            state = api.request("GET", "/api/state")
            job = next((item for item in state["jobs"] if item.get("id") == job_id), None)
            if job is None:
                raise RuntimeError("The fixture summary job disappeared from Activity.")
            if job.get("status") not in ("queued", "running"):
                if job.get("status") != "completed":
                    receipt["generation_failure_category"] = _safe_failure_code(job.get("message", "")) or "unknown_generation_failure"
                    raise RuntimeError("The fixture summary job did not complete.")
                break
            phase("fixture-summary-running")
            time.sleep(1)

        phase("fixture-summary-verifying")
        video = api.request("GET", "/api/video?" + urllib.parse.urlencode({"id": video_id}))
        summary = video.get("outputs", {}).get("summary")
        if not isinstance(summary, dict) or not isinstance(summary.get("text"), str) or not summary["text"].strip():
            raise RuntimeError("The normal AI Video Transcriber summary route did not save an answer.")
        if summary.get("provider") != "opencode" or summary.get("model") != MODEL:
            raise RuntimeError("The fixture summary did not record the selected provider and free model.")
        summaries = summary.get("receipts") or []
        if not summaries or not summaries[-1].get("cleanup", {}).get("verified"):
            raise RuntimeError("The fixture summary did not verify OpenCode session and process cleanup.")
        receipt["checks"]["http_summary"] = {"passed": True, "provider": summary["provider"],
            "model": summary["model"], "summary_characters": len(summary["text"]), "request": summaries[-1]}
        receipt["status"] = "passed"
    except BaseException as error:
        receipt["status"] = "failed"
        receipt["failure_class"] = type(error).__name__ if isinstance(error, (RuntimeError, OSError, ValueError, KeyError, TypeError, InterruptedError)) else "UnexpectedFailure"
        receipt["failure_category"] = _safe_failure_code(error) or {"long-stdin-starting": "long_prompt_or_generation",
            "fixture-server-starting": "fixture_startup", "model-catalog-loading": "catalog_or_settings",
            "fixture-transcript-submitting": "import_or_queue", "fixture-summary-running": "summary_job",
            "fixture-summary-verifying": "saved_summary_or_receipt"}.get(receipt.get("phase"), "orchestration")
    finally:
        if app is not None:
            app.stop.set()
            for event in list(app.cancel_events.values()):
                event.set()
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                receipt["cleanup"]["server_shutdown_error"] = True
            try:
                server.server_close()
            except Exception:
                receipt["cleanup"]["server_close_error"] = True
        if server_thread is not None:
            try:
                server_thread.join()
            except RuntimeError:
                receipt["cleanup"]["server_join_error"] = True
        if app is not None:
            try:
                app.worker.join()
            except RuntimeError:
                receipt["cleanup"]["worker_join_error"] = True
            try:
                app.store.database.close()
            except Exception:
                receipt["cleanup"]["database_close_error"] = True
        try:
            if fixture.exists():
                shutil.rmtree(fixture)
            fixture_removed = not fixture.exists()
        except OSError:
            fixture_removed = False
        receipt["cleanup"].update({"fixture_data_removed": fixture_removed,
                                  "worker_stopped": app is None or not app.worker.is_alive(),
                                  "server_closed": server is None or (server_thread is not None and not server_thread.is_alive())})
        if not fixture_removed and fixture.exists():
            receipt["cleanup"]["fixture_data_path"] = str(fixture)
        receipt["finished_utc"] = _now()
        if (not fixture_removed or not receipt["cleanup"]["server_closed"]
                or not receipt["cleanup"]["worker_stopped"]
                or any(value for name, value in receipt["cleanup"].items() if name.endswith("_error"))):
            receipt["status"] = "failed"
            receipt["failure_class"] = "CleanupUnverified"
        _write(receipt_path, receipt)
        _write(progress_path, {"phase": receipt["status"], "updated_utc": receipt["finished_utc"],
                               "stall_reported": False})
    return 0 if receipt["status"] == "passed" else 1
