"""AI Video Transcriber: a loopback-only HTML dashboard. Python 3.11+, no pip dependencies."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app_version
import ai_cli
import antigravity_source
import gemini
import providers
import runtime_paths
from storage import Store, now

ROOT = Path(__file__).resolve().parent
SERVICE = "AIVideoTranscriber"
MODEL_CACHE_SECONDS = 600
WHISPER_READINESS_CACHE_SECONDS = 600
WHISPER_PYTHON = runtime_paths.resolve_interpreter(runtime_paths.WHISPER_PYTHON_ENV,
                                                   runtime_paths.discover_whisper_python)
WHISPER_WORKER = ROOT / "whisper_worker.py"
GEMINI_LOCAL_FILE_CAP = 2_000_000_000
UPLOAD_CHUNK_SIZE = 1024 * 1024
LOCAL_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".mpeg", ".mpg", ".mpe",
    ".mp2", ".m2v", ".3gp", ".3g2", ".wmv", ".flv", ".ts", ".mts", ".m2ts", ".ogv",
})
LOCAL_VIDEO_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mov", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/avi", ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg", ".mpe": "video/mpeg", ".mp2": "video/mpeg", ".m2v": "video/mpeg",
    ".3gp": "video/3gpp", ".3g2": "video/3gpp2", ".wmv": "video/wmv",
    ".flv": "video/x-flv", ".ts": "video/mp2t", ".mts": "video/mp2t", ".m2ts": "video/mp2t",
    ".ogv": "video/ogg",
}
GEMINI_LOCAL_FILE_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".webm", ".avi", ".mpeg", ".mpg", ".mpe", ".mp2", ".m2v",
    ".3gp", ".wmv", ".flv",
})
# HTTP status the dashboard gets for a failed Google model-list call. Google's own 401/403 are not passed on:
# the dashboard treats a 403 as an expired page token.
MODEL_ERROR_STATUS = {"missing_key": 400, "authentication": 400, "invalid_request": 400,
                      "rate_limit": 429, "unspecified_quota": 429}


def selected(video):
    return video.get("sources", {}).get(video.get("selected_provider"))


def clip_model_metadata(result):
    """Compact, durable clip/model provenance derived from Gemini's private raw response."""
    raw = result.get("raw_response") if isinstance(result, dict) else None
    clips = raw.get("clips") if isinstance(raw, dict) else None
    if not isinstance(clips, list) or len(clips) <= 1:
        return []
    fallback = str(result.get("model") or "unknown model")
    return [{"clip": index, "model": str(clip.get("model") or fallback)}
            for index, clip in enumerate(clips, 1) if isinstance(clip, dict)]


def clip_model_summary(entries):
    if not entries:
        return ""
    groups = []
    for entry in entries:
        clip, model = entry["clip"], entry["model"]
        if groups and groups[-1][2] == model and groups[-1][1] + 1 == clip:
            groups[-1][1] = clip
        else:
            groups.append([clip, clip, model])
    labels = [(f"clip {start}" if start == end else f"clips {start}-{end}") + f": {model}"
              for start, end, model in groups]
    return "; ".join(labels)


def capacity_switch_summary(failures):
    """One durable Activity label for each high-demand model switch."""
    if not isinstance(failures, list):
        return ""
    labels = []
    for failure in failures:
        if not isinstance(failure, dict) or not failure.get("model") or not failure.get("next_model"):
            continue
        labels.append(f"{failure['model']} busy at {gemini.clip_label(failure.get('clip_start', 0))} → "
                      f"{failure['next_model']}")
    return "; ".join(labels)


def unspecified_quota_switch_summary(failures):
    """One durable Activity label for each switch after an exceeded quota with unknown scope."""
    if not isinstance(failures, list):
        return ""
    labels = []
    for failure in failures:
        if not isinstance(failure, dict) or not failure.get("model") or not failure.get("next_model"):
            continue
        labels.append(f"{failure['model']} unavailable (quota type unspecified) at "
                      f"{gemini.clip_label(failure.get('clip_start', 0))} → {failure['next_model']}")
    return "; ".join(labels)


def brief(video):
    source = selected(video) or {}
    return {"id": video["id"], "title": video["title"], "url": video["url"],
            "updated": video.get("updated"), "saved": source.get("fetched_at"), "provider": video.get("selected_provider"),
            "language": source.get("language"), "words": source.get("words", 0),
            "has_transcript": bool(source), "is_sample": video.get("is_sample", False),
            "local_file": video.get("local_file"), "file_retry_message": video.get("file_retry_message")}


def safe_local_filename(value):
    value = urllib.parse.unquote(str(value or ""), errors="strict").replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not value or value in (".", ".."):
        raise ValueError("Choose a video file with a valid filename.")
    suffix = Path(value).suffix.lower()
    if suffix not in LOCAL_VIDEO_EXTENSIONS:
        raise ValueError("Choose a supported video file (MP4, MOV, WebM, MKV, AVI, MPEG, 3GP, WMV, FLV, TS, or OGV).")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", Path(value).stem, re.IGNORECASE):
        value = "_" + value
    if len(value) > 240:
        extension = Path(value).suffix
        value = value[:240 - len(extension)] + extension
    return value


def local_file_mime(filename, supplied=""):
    suffix = Path(filename).suffix.lower()
    return LOCAL_VIDEO_MIME.get(suffix) or (supplied.split(";", 1)[0].strip().lower()
                                             if supplied.lower().startswith("video/") else "") or \
        mimetypes.guess_type(filename)[0] or "application/octet-stream"


def gemini_local_file_supported(filename):
    return Path(filename).suffix.lower() in GEMINI_LOCAL_FILE_EXTENSIONS


def file_progress_time(value):
    seconds = max(0, int(float(value or 0)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def recorded_model(entry):
    """The model an AI output or answer recorded, or None when it only says "CLI default" and the CLI reported none."""
    model = entry.get("model")
    if isinstance(model, str) and model and model != "CLI default":
        return model
    for receipt in reversed(entry.get("receipts") or []):
        used = receipt.get("model") if isinstance(receipt, dict) else None  # Claude's and Antigravity's receipts list the models they used
        used = used[0] if isinstance(used, list) and used else used
        if isinstance(used, str) and used:
            return used
    return None


class Application:
    def __init__(self, data_dir, fetcher=None, ai_runner=None, start_worker=True, model_lister=None,
                 antigravity_work_root=None, file_fetcher=None, whisper_python=None,
                 start_readiness_check=True):
        self.antigravity_work_root = Path(antigravity_work_root or ROOT / "_TEMP" / "antigravity-source-runtime")
        antigravity_source.cleanup_stale_tasks(self.antigravity_work_root)
        self.store = Store(data_dir)
        self.local_files_root = self.store.root / "local-files"
        self.cleanup_local_files_startup()
        self.token = secrets.token_urlsafe(32)
        self.fetcher = fetcher or providers.fetch
        self.file_fetcher = file_fetcher or gemini.fetch_file
        self.ai_runner = ai_runner or ai_cli.invoke
        self.model_lister = model_lister or gemini.list_models
        self.whisper_python = Path(whisper_python) if whisper_python else WHISPER_PYTHON
        self.model_cache = {}
        self.gemini_batches = {}
        self.clock = time.monotonic
        self.queue = queue.Queue()
        self.cancel_events = {}
        self.submit_lock = threading.Lock()
        self.last_call = {}
        self.stop = threading.Event()
        self.whisper_readiness_lock = threading.Lock()
        self.whisper_readiness = {"status": "checking", "venv_path": str(self.whisper_python or "")}
        self.whisper_readiness_checked = 0.0
        self.whisper_readiness_done = threading.Event()
        self.whisper_readiness_thread = None
        if start_readiness_check:
            self.recheck_whisper_readiness()
        if start_worker:
            self.worker = threading.Thread(target=self.work, name="aivideotranscriber-worker", daemon=True)
            self.worker.start()

    def cleanup_local_files_startup(self):
        for job in self.store.list("job"):
            if job.get("kind") == "file" and job.get("status") in ("interrupted", "failed", "canceled"):
                video = self.store.get("video", job.get("video_id", ""))
                if video and not selected(video):
                    video["file_retry_message"] = "Choose the file again to retry."
                    self.store.put("video", video["id"], video)
        if not self.local_files_root.exists():
            return
        if self.local_files_root.is_symlink():
            raise RuntimeError("Refusing to traverse a linked local-files directory.")
        for folder in self.local_files_root.iterdir():
            if folder.is_symlink():
                folder.unlink()
            elif folder.is_dir():
                shutil.rmtree(folder)
            else:
                folder.unlink()

    def local_task_dir(self, job_id):
        if not re.fullmatch(r"[0-9a-f]{32}", str(job_id)):
            raise ValueError("Invalid local file job identifier.")
        return self.local_files_root / job_id

    def cleanup_local_job(self, job):
        if job.get("kind") != "file":
            return
        folder = self.local_task_dir(job["id"])
        if folder.exists() and not folder.is_symlink():
            for attempt in range(5):
                try:
                    shutil.rmtree(folder)
                except FileNotFoundError:
                    return
                except OSError:
                    if attempt == 4:
                        raise
                    time.sleep(.1)
                else:
                    if not folder.exists():
                        return
            raise OSError(f"Could not remove local file job folder {folder}.")
        elif folder.is_symlink():
            folder.unlink()

    def recheck_whisper_readiness(self):
        with self.whisper_readiness_lock:
            if self.whisper_readiness_thread and self.whisper_readiness_thread.is_alive():
                return
            self.whisper_readiness = {"status": "checking", "venv_path": str(self.whisper_python or "")}
            self.whisper_readiness_done.clear()
            self.whisper_readiness_thread = threading.Thread(
                target=self._check_whisper_readiness, name="aivideotranscriber-whisper-readiness", daemon=True)
            self.whisper_readiness_thread.start()

    def _check_whisper_readiness(self):
        result = {"status": "broken", "reason": "Whisper readiness check failed."}
        try:
            if self.whisper_python is None or not self.whisper_python.is_file():
                result = {"status": "not_installed",
                          "reason": runtime_paths.missing_interpreter_message(
                              runtime_paths.WHISPER_PYTHON_ENV, "Local Whisper", self.whisper_python),
                          "venv_path": str(self.whisper_python or "")}
            else:
                completed = subprocess.run(
                    [str(self.whisper_python), str(WHISPER_WORKER), "--check"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=120)
                lines = [line for line in completed.stdout.splitlines() if line.strip()]
                answer = json.loads(lines[-1]) if lines else {}
                if answer.get("status") in ("ready", "cpu_only", "not_installed", "broken"):
                    result = answer
                elif completed.returncode:
                    result = {"status": "broken", "reason": (completed.stdout.strip() or
                                                               f"Whisper check exited {completed.returncode}.")[-300:]}
                else:
                    result = {"status": "broken", "reason": "Whisper returned an unreadable readiness result."}
        except subprocess.TimeoutExpired:
            result = {"status": "broken", "reason": "Whisper readiness check did not finish."}
        except Exception as error:
            result = {"status": "broken", "reason": str(error)[:300] or type(error).__name__}
        result.setdefault("venv_path", str(self.whisper_python or ""))
        with self.whisper_readiness_lock:
            self.whisper_readiness = result
            self.whisper_readiness_checked = time.monotonic()
            self.whisper_readiness_done.set()

    def file_readiness(self, recheck=False):
        if recheck:
            self.recheck_whisper_readiness()
        else:
            with self.whisper_readiness_lock:
                stale = (not self.whisper_readiness_checked or
                         time.monotonic() - self.whisper_readiness_checked >= WHISPER_READINESS_CACHE_SECONDS)
                checking = self.whisper_readiness_thread and self.whisper_readiness_thread.is_alive()
            if stale and not checking:
                self.recheck_whisper_readiness()
        with self.whisper_readiness_lock:
            whisper = dict(self.whisper_readiness)
        gemini_ready = self.store.public_settings()["keys_configured"].get("gemini", False)
        return {"whisper": whisper,
                "gemini": {"status": "ready" if gemini_ready else "not_configured"},
                "gemini_file_cap_bytes": GEMINI_LOCAL_FILE_CAP}

    def wait_whisper_readiness(self, cancel):
        while not self.whisper_readiness_done.wait(.2):
            if cancel.is_set():
                raise InterruptedError("Canceled while checking Whisper readiness.")
        with self.whisper_readiness_lock:
            return dict(self.whisper_readiness)

    @staticmethod
    def _estimated_job_percent(job):
        if job.get("kind") == "file":
            try:
                done, total = float(job.get("file_seconds_done")), float(job.get("file_seconds_total"))
                if total > 0:
                    return min(99, max(0, done * 100 / total))
            except (TypeError, ValueError):
                pass
        try:
            total = int(job.get("steps_total"))
            index = int(job.get("step_index"))
            expected = float(job.get("step_expected_seconds"))
            started = datetime.fromisoformat(str(job.get("step_started", "")).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = max(0, (datetime.now(timezone.utc) - started).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0
        if total < 1 or not 1 <= index <= total or expected <= 0:
            return 0
        return min(95, round(((index - 1 + min(elapsed / expected, 1)) / total) * 100))

    @staticmethod
    def _source_video_size(video, provider):
        sources = (video or {}).get("sources", {})
        source = sources.get(provider, {}) if isinstance(sources, dict) else {}
        if not isinstance(source, dict):
            source = {}
        raw = source.get("raw_response", {}) if isinstance(source, dict) else {}
        if not isinstance(raw, dict):
            raw = {}
        if provider == "antigravity":
            value = raw.get("duration_seconds") or source.get("last_timestamp")
            try:
                value = float(value)
                return value if math.isfinite(value) and value > 1 else None
            except (TypeError, ValueError, OverflowError):
                return None
        return 1

    def _start_timing_step(self, job_id, *, step_index, steps_total, kind, provider, model, size,
                           remaining=(), task=None):
        try:
            size = float(size) if size is not None else None
        except (TypeError, ValueError, OverflowError):
            size = None
        if size is not None and (not math.isfinite(size) or size <= 0):
            size = None
        expected = self.store.estimate_seconds(kind, provider, model, size, task=task)
        def estimate_remaining(item):
            item_kind, item_provider, item_model, item_size = item[:4]
            item_task = item[4] if len(item) > 4 else None
            return self.store.estimate_seconds(item_kind, item_provider, item_model, item_size,
                                               task=item_task)
        remaining_seconds = sum(value for value in (
            estimate_remaining(item) for item in remaining) if value is not None)
        with self.store.lock:
            job = self.store.get("job", job_id)
            if not job:
                return expected
            floor = max(float(job.get("progress_floor") or 0), self._estimated_job_percent(job))
            self.store.update("job", job_id, step_index=step_index, steps_total=steps_total,
                              step_started=now(), step_expected_seconds=expected,
                              expected_remaining_seconds=remaining_seconds, step_kind=kind,
                              step_provider=provider, step_model=str(model or ""), step_size=size,
                              step_task=task,
                              progress_floor=min(95, floor))
        return expected

    def _resize_timing_step(self, job_id, *, kind, provider, model, size, remaining=None, task=None):
        try:
            size = float(size) if size is not None else None
        except (TypeError, ValueError, OverflowError):
            size = None
        if size is not None and (not math.isfinite(size) or size <= 0):
            size = None
        expected = self.store.estimate_seconds(kind, provider, model, size, task=task)
        with self.store.lock:
            job = self.store.get("job", job_id)
            if not job:
                return
            floor = max(float(job.get("progress_floor") or 0), self._estimated_job_percent(job))
            values = {"step_expected_seconds": expected, "step_kind": kind, "step_provider": provider,
                      "step_model": str(model or ""), "step_size": size, "step_task": task,
                      "progress_floor": min(95, floor)}
            if remaining is not None:
                def estimate_remaining(item):
                    item_kind, item_provider, item_model, item_size = item[:4]
                    item_task = item[4] if len(item) > 4 else None
                    return self.store.estimate_seconds(item_kind, item_provider, item_model, item_size,
                                                       task=item_task)
                values["expected_remaining_seconds"] = sum(value for value in (
                    estimate_remaining(item) for item in remaining) if value is not None)
            elif job.get("expected_remaining_seconds") is None:
                values["expected_remaining_seconds"] = 0
            self.store.update("job", job_id, **values)

    def run_whisper_worker(self, job, cancel, file_path):
        if self.whisper_python is None or not self.whisper_python.is_file():
            raise RuntimeError(runtime_paths.missing_interpreter_message(
                runtime_paths.WHISPER_PYTHON_ENV, "Local Whisper", self.whisper_python))
        folder = self.local_task_dir(job["id"])
        cancel_path = folder / "cancel"
        command = [str(self.whisper_python), str(WHISPER_WORKER), "--file", str(file_path),
                   "--cancel-file", str(cancel_path)]
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise RuntimeError(f"Could not start the Whisper worker: {error}") from None
        lines = queue.Queue()
        def collect():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)
        reader = threading.Thread(target=collect, name="aivideotranscriber-whisper-output", daemon=True)
        reader.start()
        try:
            receipt = antigravity_source._process_receipt(process.pid)
        except Exception:
            receipt = None
        cancel_started, stream_done, answer = None, False, None
        try:
            while not stream_done or process.poll() is None:
                if cancel.is_set():
                    if cancel_started is None:
                        cancel_path.touch(exist_ok=True)
                        cancel_started = time.monotonic()
                    elif time.monotonic() - cancel_started >= 5 and process.poll() is None:
                        if receipt is None:
                            raise RuntimeError("Could not verify the Whisper worker process for safe cancellation.")
                        antigravity_source._terminate_owned_process(receipt["pid"], receipt["started_at"])
                try:
                    line = lines.get(timeout=.2)
                except queue.Empty:
                    continue
                if line is None:
                    stream_done = True
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("type") == "progress":
                    message = str(item.get("message") or "Transcribing local file")[:500]
                    progress_values = {"active_provider": "whisper"}
                    if item.get("completed_seconds") is not None:
                        completed_seconds = float(item["completed_seconds"])
                        total_seconds = float(item.get("total_seconds") or 0.0)
                        message = (f"Whisper · {file_progress_time(item['completed_seconds'])} of "
                                   f"{file_progress_time(item.get('total_seconds'))}")
                        progress_values.update(file_seconds_done=completed_seconds,
                                               file_seconds_total=total_seconds)
                    else:
                        message = "Whisper · " + message
                    progress_values["message"] = message
                    self.store.update("job", job["id"], **progress_values)
                elif item.get("type") == "result":
                    answer = item
                elif item.get("type") == "error":
                    raise RuntimeError("Whisper failed: " + str(item.get("message") or "unknown worker error")[:900])
                elif item.get("type") == "canceled":
                    raise InterruptedError("Canceled while Whisper was transcribing the local file.")
            return_code = process.wait()
            if cancel.is_set():
                raise InterruptedError("Canceled while Whisper was transcribing the local file.")
            if answer is None or return_code != 0:
                raise RuntimeError(f"Whisper worker exited {return_code} without a transcript result.")
            return answer
        finally:
            if process.poll() is None:
                if receipt:
                    antigravity_source._terminate_owned_process(receipt["pid"], receipt["started_at"])
                else:
                    process.kill()
            if process.stdout:
                process.stdout.close()

    @staticmethod
    def whisper_source(payload, video_id, title):
        segments = []
        for item in payload.get("segments", []):
            start, end = float(item.get("start", 0)), float(item.get("end", 0))
            if start < 0 or end < start:
                continue
            segments.append({"text": str(item.get("text", "")), "start": start,
                             "duration": max(0.0, end - start)})
        text = "\n".join(segment["text"] for segment in segments)
        if not text.strip():
            raise RuntimeError("Whisper returned no speech text.")
        language = str(payload.get("language") or "unknown")
        digest = hashlib.sha256(json.dumps(
            {"segments": segments, "language": language, "video_id": video_id},
            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"provider": "whisper", "video_id": video_id, "title": title, "language": language,
                "segments": segments, "text": text,
                "timed_text": "\n".join(f"[{providers.stamp(s['start'])}] {s['text']}" for s in segments),
                "words": len(text.split()), "characters": len(text),
                "last_timestamp": max((s["start"] + s["duration"] for s in segments), default=0),
                "hash": digest, "elapsed_seconds": float(payload.get("elapsed_seconds") or 0),
                "model_setup_elapsed_seconds": float(payload.get("model_setup_elapsed_seconds") or 0),
                "inference_elapsed_seconds": float(payload.get("inference_elapsed_seconds") or 0),
                "fetched_at": now(), "model": payload.get("model", "large-v3"),
                "language_probability": payload.get("language_probability"),
                "duration_seconds": float(payload.get("duration") or 0),
                "device": payload.get("device"), "compute_type": payload.get("compute_type")}

    def file_job(self, job, cancel):
        file_path = self.local_task_dir(job["id"]) / job["file_name"]
        video = self.store.get("video", job["video_id"])
        if not video or not file_path.is_file():
            raise RuntimeError("The uploaded local file is no longer available. Choose the file again to retry.")
        name = job["file_name"]
        title = Path(name).stem[:250]
        route_name = job["file_route"]
        whisper_status = (self.file_readiness()["whisper"] if route_name == "gemini"
                          else self.wait_whisper_readiness(cancel))
        whisper_ready = whisper_status.get("status") in ("ready", "cpu_only")
        route = (["whisper"] if route_name == "whisper" else ["gemini"] if route_name == "gemini"
                 else ["whisper", "gemini"])
        attempts = []
        if route_name == "whisper_then_gemini" and job["file_size"] > GEMINI_LOCAL_FILE_CAP:
            skipped = {"provider": "gemini", "at": now(), "job_id": job["id"], "status": "skipped",
                       "code": "file_too_large", "seconds": 0,
                       "message": "Gemini fallback skipped because this file exceeds 2,000,000,000 bytes; use Whisper."}
            attempts.append(skipped)
            with self.store.lock:
                video = self.store.get("video", job["video_id"])
                video["attempts"] = (video.get("attempts", []) + [skipped])[-100:]
                self.store.put("video", video["id"], video)
                self.store.put("provider", "gemini", {**skipped, "updated": now()})
            route = ["whisper"]
        elif route_name == "whisper_then_gemini" and not gemini_local_file_supported(name):
            skipped = {"provider": "gemini", "at": now(), "job_id": job["id"], "status": "skipped",
                       "code": "unsupported_file_type", "seconds": 0,
                       "message": "Gemini does not support this video file type; use Whisper."}
            attempts.append(skipped)
            with self.store.lock:
                video = self.store.get("video", job["video_id"])
                video["attempts"] = (video.get("attempts", []) + [skipped])[-100:]
                self.store.put("video", video["id"], video)
                self.store.put("provider", "gemini", {**skipped, "updated": now()})
            route = ["whisper"]
        succeeded = None
        for provider in route:
            if cancel.is_set():
                raise InterruptedError("Canceled. Choose the file again to retry.")
            started = time.monotonic()
            attempt = {"provider": provider, "at": now(), "job_id": job["id"]}
            if provider == "gemini" and job["file_size"] > GEMINI_LOCAL_FILE_CAP:
                attempt.update(status="skipped", code="file_too_large", seconds=0,
                               message="Gemini fallback skipped because this file exceeds 2,000,000,000 bytes; use Whisper.")
            elif provider == "whisper" and not whisper_ready:
                state = whisper_status.get("status", "broken")
                reason = whisper_status.get("reason") or whisper_status.get("venv_path") or state
                attempt.update(status="failed", code="not_ready", seconds=0,
                               message=f"Whisper is {state}: {reason}")
            elif provider == "gemini" and not self.store.key("gemini"):
                attempt.update(status="failed", code="missing_key", seconds=0,
                               message="Add a Gemini API key in Settings.")
            else:
                self.store.update("job", job["id"], active_provider=provider,
                                  message=("Whisper · starting local transcription" if provider == "whisper"
                                           else "Gemini · preparing local file upload"))
                try:
                    if provider == "whisper":
                        result = self.whisper_source(self.run_whisper_worker(job, cancel, file_path),
                                                     job["video_id"], title)
                    else:
                        config = job["config"]
                        def gemini_file_step_started(step_index, steps_total, size, model, remaining_sizes):
                            remaining = [("gemini_clip", "gemini", model, future_size)
                                         for future_size in remaining_sizes]
                            self._start_timing_step(job["id"], step_index=step_index, steps_total=steps_total,
                                                    kind="gemini_clip", provider="gemini", model=model, size=size,
                                                    remaining=remaining)
                        def gemini_file_step_completed(model, size, elapsed):
                            self.store.record_timing("gemini_clip", "gemini", model, size, elapsed)
                        def gemini_file_step_model_changed(model, size, remaining_sizes):
                            self._resize_timing_step(job["id"], kind="gemini_clip", provider="gemini",
                                                     model=model, size=size,
                                                     remaining=[("gemini_clip", "gemini", model, value)
                                                                for value in remaining_sizes])
                        result = self.file_fetcher(
                            file_path, self.store.key("gemini"), mime_type=job["file_mime"], title=name,
                            video_id=job["video_id"], model=config.get("gemini_model"),
                            fallback_models=config.get("gemini_fallback_models"),
                            clip_seconds=config.get("gemini_clip_seconds"),
                            model_limits=self.cached_gemini_models(), cancel=cancel,
                            progress=lambda message: self.store.update("job", job["id"], message=message),
                            step_started=gemini_file_step_started, step_completed=gemini_file_step_completed,
                            step_model_changed=gemini_file_step_model_changed)
                        result["provider"] = "gemini"
                        result["input"] = "local file"
                    if cancel.is_set():
                        raise InterruptedError("Canceled. Choose the file again to retry.")
                    result["fetched_at"] = now()
                    attempt.update(status="success", message="Transcript retrieved", words=result["words"],
                                   segments=len(result["segments"]), language=result["language"],
                                   seconds=round(time.monotonic() - started, 2),
                                   characters=result["characters"], last_timestamp=result["last_timestamp"],
                                   **{k: result[k] for k in ("model", "models", "clips", "requests", "input_tokens",
                                                             "output_tokens", "availability_failures", "quota_failures",
                                                             "quota_unknown_failures", "end_confirmed", "completion_warning")
                                      if result.get(k) is not None})
                    if provider == "gemini":
                        result["input"] = "local file"
                    succeeded = (provider, result)
                    attempt["seconds"] = round(time.monotonic() - started, 2)
                except InterruptedError:
                    raise
                except Exception as error:
                    attempt.update(status="failed", code=getattr(error, "code", "provider_error"),
                                   seconds=round(time.monotonic() - started, 2), message=self.scrub(error))
            attempts.append(attempt)
            with self.store.lock:
                video = self.store.get("video", job["video_id"])
                video["attempts"] = (video.get("attempts", []) + [attempt])[-100:]
                video["updated"] = now()
                if succeeded and succeeded[0] == provider:
                    provider_name, source = succeeded
                    source["provider"] = provider_name
                    source["title"] = title
                    source["video_id"] = job["video_id"]
                    video.setdefault("sources", {})[provider_name] = source
                    video["selected_provider"] = provider_name
                    video["local_file"]["duration_seconds"] = float(
                        source.get("duration_seconds") or source.get("video_duration_seconds") or source.get("last_timestamp") or 0)
                    video.pop("file_retry_message", None)
                self.store.put("video", video["id"], video)
                self.store.put("provider", provider, {**attempt, "updated": now()})
            if succeeded and succeeded[0] == provider:
                break
        if not succeeded:
            messages = "; ".join(f"{item['provider']}: {item['message']}" for item in attempts)
            with self.store.lock:
                video = self.store.get("video", job["video_id"])
                video["file_retry_message"] = "Choose the file again to retry."
                video["updated"] = now()
                self.store.put("video", video["id"], video)
            raise RuntimeError((messages or "No selected file transcription route was available.")
                               + " Choose the file again to retry.")
        provider_name, source = succeeded
        messages = "Saved transcript from " + ("Whisper (this PC)" if provider_name == "whisper" else "Gemini")
        self.store.update("job", job["id"], message=messages,
                          provider_attempts=[{k: value for k, value in item.items()
                                              if k in ("provider", "status", "model", "clips", "requests")}
                                             for item in attempts])

    def scrub(self, text):
        text = str(text)
        for provider in providers.PROVIDERS:
            key = self.store.key(provider)
            if key:
                text = text.replace(key, "[API key redacted]")
        return text

    def gemini_models(self, refresh=False):
        """Google's list of Gemini models for the saved key, kept for 10 minutes per key unless `refresh`."""
        key = self.store.key("gemini")
        if not key:
            raise providers.ProviderError("missing_key", "Save a Gemini key first.")
        token = hashlib.sha256(key.encode("utf-8")).hexdigest()
        cached, moment = self.model_cache.get(token), self.clock()
        if cached and not refresh and moment - cached[0] < MODEL_CACHE_SECONDS:
            return cached[1]
        models = self.model_lister(key)
        self.model_cache = {t: v for t, v in self.model_cache.items() if t != token and moment - v[0] < MODEL_CACHE_SECONDS}
        self.model_cache[token] = (moment, models)
        return models

    def cached_gemini_models(self):
        """The last listed model metadata for the current key, without making a network call."""
        key = self.store.key("gemini")
        if not key:
            return {}
        token = hashlib.sha256(key.encode("utf-8")).hexdigest()
        cached = self.model_cache.get(token)
        return {model["id"]: model for model in cached[1]} if cached else {}

    def last_models(self):
        """{cli: model} from the newest saved AI output or Ask AI answer of each CLI in the library."""
        newest = {}
        for video in self.store.list("video"):
            for entry in [*video.get("outputs", {}).values(), *video.get("conversation", [])]:
                provider, model, created = entry.get("provider"), recorded_model(entry), str(entry.get("created", ""))
                if provider and model and created >= newest.get(provider, ("", ""))[0]:
                    newest[provider] = (created, model)
        return {provider: model for provider, (created, model) in newest.items()}

    def ai_info(self):
        """ai_cli.availability() plus last_model: the model each CLI last answered with in this library, or None."""
        info, last = ai_cli.availability(), self.last_models()
        for provider, entry in info.items():
            entry["last_model"] = last.get(provider)
        return info

    def submit(self, vid, kind, enqueue=True, **extra):
        with self.submit_lock:
            # The video check belongs under the same lock as deletion.  A request that passed an earlier HTTP
            # lookup must not be able to queue work after another request deleted the video.
            if not self.store.get("video", vid):
                raise ValueError("Video not found.")
            if any(j["video_id"] == vid and j["kind"] == kind and j["status"] in ("queued", "running") for j in self.store.list("job")):
                return None
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "video_id": vid, "kind": kind, "status": "queued",
                   "created": now(), "message": "Queued", "progress": 0,
                   "config": copy.deepcopy(self.store.config), **extra}
            self.store.put("job", job_id, job)
            self.cancel_events[job_id] = threading.Event()
            if enqueue:
                self.queue.put(job_id)
            return job_id

    def discard_job(self, job):
        """Remove a durable job and release its in-memory state without disturbing another active batch member."""
        self.cleanup_local_job(job)
        self.store.delete("job", job["id"])
        self.cancel_events.pop(job["id"], None)
        batch_id = job.get("batch_id")
        if batch_id and not any(candidate.get("batch_id") == batch_id
                                and candidate.get("status") in ("queued", "running")
                                for candidate in self.store.list("job")):
            self.gemini_batches.pop(batch_id, None)

    def add_batch(self, text, mode="auto", compare=False, refetch=False, force=False):
        if mode not in ("auto", *providers.PROVIDERS):
            raise ValueError("Unknown retrieval mode.")
        rerun = bool((refetch or force) and not compare)
        lines = [s.strip() for s in str(text).splitlines() if s.strip()]
        if not 1 <= len(lines) <= 100:
            raise ValueError("Paste between 1 and 100 individual video links, one per line.")
        identifiers, errors = [], []
        for line in lines:
            try:
                vid = providers.video_id(line)
                if vid not in identifiers:
                    identifiers.append(vid)
            except ValueError as error:
                errors.append({"input": line[:200], "error": str(error)})
        jobs, reused = [], 0
        for vid in identifiers:
            video = self.store.get("video", vid)
            if not video:
                video = {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
                         "title": vid, "sources": {}, "attempts": [], "outputs": {},
                         "conversation": [], "created": now(), "updated": now()}
                self.store.put("video", vid, video)
            # A pasted transcript is not proof that captions were retrieved from this URL.
            if (not rerun and not compare and video.get("selected_provider") in providers.PROVIDERS
                    and selected(video) and (mode == "auto" or video.get("selected_provider") == mode)):
                reused += 1
                continue
            job = self.submit(vid, "fetch", enqueue=False, mode=mode, compare=bool(compare), rerun=rerun)
            if job:
                jobs.append(job)
        batch_id = uuid.uuid4().hex
        for index, job_id in enumerate(jobs, 1):
            self.store.update("job", job_id, batch_id=batch_id, batch_index=index, batch_total=len(jobs))
        if jobs:
            self.gemini_batches[batch_id] = {"exhausted": {}, "busy": set(), "quota_unknown": set(),
                                             "remaining": len(jobs)}
            for job_id in jobs:
                self.queue.put(job_id)
        return {"jobs": jobs, "reused": reused, "errors": errors}

    def cancel(self, job_id):
        event = self.cancel_events.get(job_id)
        job = self.store.get("job", job_id)
        if not event or not job or job["status"] not in ("queued", "running"):
            return False
        event.set()
        if job["status"] == "queued":
            message = "Canceled. Choose the file again to retry." if job.get("kind") == "file" else "Canceled"
            self.store.update("job", job_id, status="canceled", message=message, finished=now())
            if job.get("kind") == "file":
                video = self.store.get("video", job["video_id"])
                if video:
                    video["file_retry_message"] = "Choose the file again to retry."
                    self.store.put("video", video["id"], video)
            self.cleanup_local_job(job)
        else:
            self.store.update("job", job_id, message="Cancel requested. The current provider is stopping safely; an in-flight request may take a moment.")
        return True

    def delete_video(self, vid):
        # Under the submit lock, so no job for this video can be queued while it is being removed.
        with self.submit_lock:
            if not self.store.get("video", vid):
                raise ValueError("Video not found.")
            jobs = [j for j in self.store.list("job") if j["video_id"] == vid]
            if any(j["status"] in ("queued", "running") for j in jobs):
                raise ValueError("This video has a task queued or running. Cancel it first, then delete.")
            for job in jobs:
                self.discard_job(job)
            self.store.delete("video", vid)
        return {"deleted": vid}

    def remove_activity(self, job_id="", finished=False):
        """Remove finished entries from the Activity list: one job, or every finished one. Saved transcripts and
        results live on the video and stay; a queued or running task is never removed."""
        with self.submit_lock:
            jobs = self.store.list("job") if finished else [j for j in [self.store.get("job", job_id)] if j]
            if not finished and not jobs:
                raise ValueError("That Activity entry no longer exists.")
            if not finished and jobs[0]["status"] in ("queued", "running"):
                raise ValueError("This task is still queued or running. Cancel it first.")
            removed = [j for j in jobs if j["status"] not in ("queued", "running")]
            for job in removed:
                self.discard_job(job)
        return {"removed": len(removed)}

    def work(self):
        while not self.stop.is_set():
            try:
                job_id = self.queue.get(timeout=0.3)
            except queue.Empty:
                continue
            job = self.store.get("job", job_id)
            cancel = self.cancel_events.get(job_id)
            # A queued task can be canceled and then removed from Activity, or its video can be deleted, before
            # the worker reaches its queue entry.  That stale entry is expected and must not terminate the worker.
            if not job or cancel is None:
                self.queue.task_done()
                continue
            try:
                if cancel.is_set():
                    continue
                self.store.update("job", job_id, status="running", started=now(), message="Starting")
                if job["kind"] == "fetch":
                    self.fetch_job(job, cancel)
                elif job["kind"] == "file":
                    self.file_job(job, cancel)
                else:
                    self.ai_job(job, cancel)
                if cancel.is_set():
                    raise InterruptedError("Canceled")
                self.store.update("job", job_id, status="completed", finished=now(), progress=100)
            except InterruptedError as error:
                message = str(error) or "Canceled. Existing saved results were kept."
                if job.get("kind") == "file":
                    message += (" " if "Choose the file again to retry" in message else
                               " Choose the file again to retry.")
                    video = self.store.get("video", job["video_id"])
                    if video:
                        video["file_retry_message"] = "Choose the file again to retry."
                        video["updated"] = now()
                        self.store.put("video", video["id"], video)
                self.store.update("job", job_id, status="canceled", message=message, finished=now())
            except Exception as error:
                message = self.scrub(error)
                if job.get("kind") == "file":
                    message += (" " if "Choose the file again to retry" in message else
                               " Choose the file again to retry.")
                    video = self.store.get("video", job["video_id"])
                    if video:
                        video["file_retry_message"] = "Choose the file again to retry."
                        video["updated"] = now()
                        self.store.put("video", video["id"], video)
                self.store.update("job", job_id, status="failed", message=message[:1800], finished=now())
            finally:
                try:
                    self.cleanup_local_job(job)
                except Exception as error:
                    if job.get("kind") == "file":
                        video = self.store.get("video", job["video_id"])
                        if video:
                            video["file_retry_message"] = "Choose the file again to retry."
                            self.store.put("video", video["id"], video)
                        self.store.update("job", job_id, status="failed",
                                          message="Could not remove the app's stored local video copy: "
                                          + self.scrub(error)[:1200] + " Choose the file again to retry.", finished=now())
                batch_id = job.get("batch_id")
                state = self.gemini_batches.get(batch_id)
                if state:
                    state["remaining"] -= 1
                    if state["remaining"] <= 0:
                        self.gemini_batches.pop(batch_id, None)
                self.cancel_events.pop(job_id, None)
                if batch_id and not any(candidate.get("batch_id") == batch_id
                                        and candidate.get("status") in ("queued", "running")
                                        for candidate in self.store.list("job")):
                    self.gemini_batches.pop(batch_id, None)
                self.queue.task_done()

    def fetch_job(self, job, cancel):
        config = job["config"]
        route = [config["primary"], config["fallback"]] if job["mode"] == "auto" else [job["mode"]]
        if job["compare"]:
            route = [config["primary"], *providers.PROVIDERS]
        route = list(dict.fromkeys(p for p in route if p in providers.PROVIDERS))
        if job["compare"]:
            # Compare the services that have a key. With fewer than two, try them all so the missing keys are reported.
            available = [p for p in route if p == "antigravity" or self.store.key(p)]
            if len(available) >= 2:
                route = available
        def progress(message):
            if not cancel.is_set():
                self.store.update("job", job["id"], message=message)
        gemini_route_index = route.index("gemini") if "gemini" in route else None
        gemini_step_count = None

        def future_fetch_estimates(video, future_providers):
            sources = (video or {}).get("sources", {})
            if not isinstance(sources, dict):
                sources = {}
            remaining = []
            for future_provider in future_providers:
                future_source = sources.get(future_provider, {})
                if not isinstance(future_source, dict):
                    future_source = {}
                remaining.append(("fetch", future_provider, future_source.get("model") or "",
                                  self._source_video_size(video, future_provider)))
            return remaining

        def compare_step(index):
            if not job.get("compare"):
                return 1, 1
            if gemini_route_index is not None and index > gemini_route_index and gemini_step_count:
                return index + gemini_step_count, len(route) - 1 + gemini_step_count
            return index + 1, len(route)

        successful = 0
        attempted = []
        for index, provider in enumerate(route):
            if cancel.is_set():
                raise InterruptedError()
            label = providers.LABELS[provider]
            with self.store.lock:
                current_job = self.store.get("job", job["id"])
                floor = max(float(current_job.get("progress_floor") or 0),
                            self._estimated_job_percent(current_job))
                self.store.update("job", job["id"], active_provider=provider,
                                  message=f"Requesting {label}" + (" (fallback)" if index and not job["compare"] else ""),
                                  progress=int(index * 100 / len(route)) if job["compare"] else 0,
                                  step_index=None, steps_total=None,
                                  step_started=None, step_expected_seconds=None, expected_remaining_seconds=None,
                                  step_kind=None, step_provider=None, step_model=None, step_size=None,
                                  progress_floor=min(95, floor))
            delay = max(0, 1.1 - (time.monotonic() - self.last_call.get(provider, 0)))
            if cancel.wait(delay):
                raise InterruptedError()
            started = time.monotonic()
            attempt = {"provider": provider, "at": now(), "job_id": job["id"]}
            try:
                key = self.store.key(provider)
                batch = self.gemini_batches.get(job.get("batch_id"),
                                                {"exhausted": {}, "busy": set(), "quota_unknown": set()})
                options = {"cancel": cancel, "progress": progress}
                antigravity_size = None
                if provider == "gemini":
                    # Capacity and unspecified quota are transient: every new video gets one fresh
                    # try of the preferred model. Confirmed daily exhaustion remains batch-shared.
                    batch["busy"].clear()
                    batch["quota_unknown"].clear()
                    options.update(model=config.get("gemini_model"),
                                   fallback_models=config.get("gemini_fallback_models"),
                                   clip_seconds=config.get("gemini_clip_seconds"),
                                   model_limits=self.cached_gemini_models(),
                                   exhausted_models=batch["exhausted"],
                                   busy_models=batch["busy"],
                                   quota_unknown_models=batch["quota_unknown"])
                    def gemini_step_started(step_index, steps_total, size, model, remaining_sizes):
                        nonlocal gemini_step_count
                        if steps_total is not None:
                            gemini_step_count = int(steps_total)
                        overall_index = index + step_index if job.get("compare") else step_index
                        overall_total = (len(route) - 1 + gemini_step_count
                                         if job.get("compare") and gemini_step_count else steps_total)
                        remaining = [("gemini_clip", "gemini", model, future_size)
                                     for future_size in remaining_sizes]
                        if job.get("compare"):
                            remaining.extend(future_fetch_estimates(
                                self.store.get("video", job["video_id"]), route[index + 1:]))
                        self._start_timing_step(job["id"], step_index=overall_index, steps_total=overall_total,
                                                kind="gemini_clip", provider="gemini", model=model, size=size,
                                                remaining=remaining)
                    def gemini_step_completed(model, size, elapsed):
                        if size is not None:
                            self.store.record_timing("gemini_clip", "gemini", model, size, elapsed)
                    def gemini_step_model_changed(model, size, remaining_sizes):
                        remaining = [("gemini_clip", "gemini", model, value) for value in remaining_sizes]
                        if job.get("compare"):
                            remaining.extend(future_fetch_estimates(
                                self.store.get("video", job["video_id"]), route[index + 1:]))
                        self._resize_timing_step(job["id"], kind="gemini_clip", provider="gemini",
                                                 model=model, size=size, remaining=remaining)
                    options.update(step_started=gemini_step_started, step_completed=gemini_step_completed,
                                   step_model_changed=gemini_step_model_changed)
                elif provider == "antigravity":
                    options.update(antigravity_model="",
                                   work_root=self.antigravity_work_root)
                    video = self.store.get("video", job["video_id"])
                    size = self._source_video_size(video, provider)
                    sources = (video or {}).get("sources", {})
                    source = sources.get(provider, {}) if isinstance(sources, dict) else {}
                    model = (source.get("model") if isinstance(source, dict) else None) or "CLI default"
                    remaining = future_fetch_estimates(video, route[index + 1:]) if job.get("compare") else []
                    step_index, steps_total = compare_step(index)
                    self._start_timing_step(job["id"], step_index=step_index, steps_total=steps_total,
                                            kind="fetch", provider=provider, model=model, size=size,
                                            remaining=remaining)
                    def antigravity_step_update(updated_size, updated_model):
                        nonlocal antigravity_size
                        try:
                            known_size = float(updated_size)
                        except (TypeError, ValueError, OverflowError):
                            return
                        if not math.isfinite(known_size) or known_size <= 0:
                            return
                        antigravity_size = known_size
                        self._resize_timing_step(job["id"], kind="fetch", provider="antigravity",
                                                 model=updated_model or model, size=known_size)
                    options["step_update"] = antigravity_step_update
                else:
                    video = self.store.get("video", job["video_id"])
                    remaining = future_fetch_estimates(video, route[index + 1:]) if job.get("compare") else []
                    step_index, steps_total = compare_step(index)
                    self._start_timing_step(job["id"], step_index=step_index, steps_total=steps_total,
                                            kind="fetch", provider=provider, model="", size=1,
                                            remaining=remaining)
                try:
                    result = self.fetcher(provider, job["video_id"], key, config["language"], **options)
                except InterruptedError:
                    raise
                except providers.ProviderError:
                    raise
                except Exception:
                    raise providers.ProviderError(
                        "provider_error", f"{label} failed unexpectedly. The fallback will be tried when configured.") from None
                elapsed = max(0, time.monotonic() - started)
                if provider != "gemini":
                    timing_size = 1
                    if provider == "antigravity":
                        raw = result.get("raw_response", {}) if isinstance(result.get("raw_response"), dict) else {}
                        try:
                            result_size = float(raw.get("duration_seconds") or result.get("last_timestamp"))
                            timing_size = result_size if math.isfinite(result_size) and result_size > 0 else antigravity_size
                        except (TypeError, ValueError, OverflowError):
                            timing_size = antigravity_size
                    timing_model = result.get("model") or ""
                    if timing_size is not None:
                        self.store.record_timing("fetch", provider, timing_model, timing_size, elapsed)
                elif job.get("compare") and gemini_step_count is None:
                    try:
                        gemini_step_count = max(1, int(result.get("clips") or 1))
                    except (TypeError, ValueError, OverflowError):
                        gemini_step_count = None
                self.last_call[provider] = time.monotonic()
                result["fetched_at"] = now()
                clip_models = clip_model_metadata(result)
                if clip_models:
                    result["clip_models"] = clip_models
                    result["clip_model_summary"] = clip_model_summary(clip_models)
                completion_warning = result.get("completion_warning")
                attempt.update(status="success", message="Transcript retrieved" +
                               (f" · {completion_warning}" if completion_warning else ""), words=result["words"],
                               segments=len(result["segments"]), language=result["language"],
                               seconds=result["elapsed_seconds"], characters=result["characters"],
                               last_timestamp=result["last_timestamp"],
                               credits_remaining=result.get("credits_remaining"),
                               billable_credits=result.get("billable_credits"),
                               **{k: result[k] for k in ("model", "models", "clips", "requests", "input_tokens", "output_tokens",
                                                         "clip_models", "clip_model_summary", "availability_failures",
                                                         "quota_failures", "quota_unknown_failures", "end_confirmed",
                                                         "completion_warning", "end_detected_by")
                                  if result.get(k) is not None})
                with self.store.lock:
                    video = self.store.get("video", job["video_id"])
                    video["sources"][provider] = result
                    if job.get("rerun") or video.get("selected_provider") in (None, "import"):
                        video["selected_provider"] = provider
                    if result["title"] != video["id"]:
                        video["title"] = result["title"]
                    self.store.put("video", video["id"], video)
                successful += 1
            except InterruptedError:
                raise
            except providers.ProviderError as error:
                self.last_call[provider] = time.monotonic()
                attempt.update(status="failed", code=error.code, message=self.scrub(error),
                               seconds=round(time.monotonic() - started, 2), http_status=error.status)
                if getattr(error, "requests", None) is not None:
                    attempt["requests"] = error.requests
                for name in ("availability_failures", "quota_failures", "quota_unknown_failures"):
                    if getattr(error, name, None):
                        attempt[name] = getattr(error, name)
            attempted.append(attempt)
            with self.store.lock:
                video = self.store.get("video", job["video_id"])
                video["attempts"] = (video["attempts"] + [attempt])[-100:]
                video["updated"] = now()
                self.store.put("video", video["id"], video)
                self.store.put("provider", provider, {**attempt, "updated": now()})
            if successful and not job["compare"]:
                break
        if not successful:
            raise RuntimeError("; ".join(f"{providers.LABELS[a['provider']]}: {a['message']}" for a in attempted))
        if job["compare"]:
            message = f"Comparison finished: {successful}/{len(route)} services returned transcripts. See Provider checks."
            warnings = list(dict.fromkeys(a["completion_warning"] for a in attempted
                                          if a.get("status") == "success" and a.get("completion_warning")))
            if warnings:
                message += " · " + " · ".join(warnings)
        else:
            last = attempted[-1]
            model = f" · {' → '.join(last['models'])}" if last.get("models") else ""
            clips = f" · {last['clip_model_summary']}" if last.get("clip_model_summary") else ""
            capacity = capacity_switch_summary(last.get("availability_failures"))
            capacity = f" · capacity switch: {capacity}" if capacity else ""
            quota_unknown = unspecified_quota_switch_summary(last.get("quota_unknown_failures"))
            quota_unknown = f" · quota switch: {quota_unknown}" if quota_unknown else ""
            completion = f" · {last['completion_warning']}" if last.get("completion_warning") else ""
            message = (f"Saved transcript from {providers.LABELS[last['provider']]}{model}{clips}"
                       f"{capacity}{quota_unknown}{completion}")
        summaries = [{k: attempt[k] for k in ("provider", "status", "model", "models", "clips", "requests",
                                                    "clip_models", "clip_model_summary", "availability_failures",
                                                    "quota_failures", "quota_unknown_failures", "end_confirmed",
                                                    "completion_warning", "end_detected_by") if k in attempt}
                     for attempt in attempted]
        saved = next((attempt for attempt in reversed(attempted) if attempt.get("status") == "success"), {})
        details = {k: saved[k] for k in ("provider", "model", "models", "clips", "requests",
                                                "clip_models", "clip_model_summary", "availability_failures",
                                                "quota_failures", "quota_unknown_failures", "end_confirmed",
                                                "completion_warning", "end_detected_by") if k in saved}
        self.store.update("job", job["id"], message=message, provider_attempts=summaries, **details)

    def ai_job(self, job, cancel):
        video = self.store.get("video", job["video_id"])
        source = selected(video)
        if not source:
            raise ValueError("Retrieve or import a transcript first.")
        config = job["config"]
        provider = config["ai_provider"]
        kind = job["kind"]
        question = job.get("question", "")
        history = [{"question": c["question"], "answer": c["answer"]} for c in video.get("conversation", [])
                   if c.get("source_hash") == source["hash"]][-6:]
        parts = ai_cli.chunks(source["timed_text"])
        output, receipts = [], []
        timing_enabled = provider in ("codex", "claude", "antigravity", "opencode")
        model = config["ai_model"] or "CLI default"
        combines = len(parts) > 1 and kind in ("summary", "ask")
        steps_total = len(parts) + int(combines)
        combined_characters = None
        for index, part in enumerate(parts):
            label = f"{provider.title()} · {kind} · part {index + 1}/{len(parts)}"
            self.store.update("job", job["id"], active_provider=provider, message=label,
                              progress=int(index * 85 / len(parts)))
            if timing_enabled:
                remaining = [("ai_part", provider, model, len(future), kind)
                             for future in parts[index + 1:]]
                self._start_timing_step(job["id"], step_index=index + 1, steps_total=steps_total,
                                        kind="ai_part", provider=provider, model=model, size=len(part),
                                        remaining=remaining, task=kind)
            part_label = f"Part {index + 1} of {len(parts)}; do not assume omitted parts." if len(parts) > 1 else "Complete transcript"
            prompt = ai_cli.prompt_for(kind, part, config, question, history, part_label)
            if kind == "ask" and len(parts) > 1:
                prompt += "\nCollect evidence relevant to the question from this part. If absent, say no relevant evidence in this part. Do not infer the contents of other parts."
            options = {"model": config["ai_model"], "effort": config["ai_effort"], "cancel": cancel}
            if provider == "opencode":
                options["kind"] = kind
            runner_started = time.monotonic()
            answer, receipt = self.ai_runner(provider, prompt, self.store.root / "ai-work", **options)
            measured_elapsed = max(0, time.monotonic() - runner_started)
            receipt = dict(receipt or {})
            try:
                elapsed = float(receipt.get("elapsed_seconds") or measured_elapsed)
            except (TypeError, ValueError):
                elapsed = measured_elapsed
            if elapsed <= 0:
                elapsed = measured_elapsed
            receipt_model = receipt.get("requested_model") or model
            if timing_enabled:
                receipt.update(timing_kind="ai_part", timing_task=kind, timing_size=len(part), timing_model=receipt_model,
                               provider=provider)
                self.store.record_timing("ai_part", provider, receipt_model, len(part), elapsed, task=kind)
            if cancel.is_set():
                raise InterruptedError()
            output.append(answer)
            receipts.append(receipt)
            self.store.update("job", job["id"], partial="\n\n".join(output), parts_done=index + 1, parts_total=len(parts))
        if combines:
            self.store.update("job", job["id"], message=f"{provider.title()} · combining evidence from all {len(parts)} parts", progress=90)
            combined = "\n\n".join(f"PART {i+1}\n{answer}" for i, answer in enumerate(output))
            if timing_enabled:
                self._start_timing_step(job["id"], step_index=len(parts) + 1, steps_total=steps_total,
                                        kind="ai_combine", provider=provider, model=model,
                                        size=len(combined), task=kind)
            prompt = ai_cli.prompt_for(kind, combined, config, question, history,
                                      "These are AI-produced notes covering every source part. Synthesize them; do not invent missing evidence.")
            options = {"model": config["ai_model"], "effort": config["ai_effort"], "cancel": cancel}
            if provider == "opencode":
                options["kind"] = kind
            runner_started = time.monotonic()
            answer, receipt = self.ai_runner(provider, prompt, self.store.root / "ai-work", **options)
            measured_elapsed = max(0, time.monotonic() - runner_started)
            receipt = dict(receipt or {})
            try:
                elapsed = float(receipt.get("elapsed_seconds") or measured_elapsed)
            except (TypeError, ValueError):
                elapsed = measured_elapsed
            if elapsed <= 0:
                elapsed = measured_elapsed
            receipt_model = receipt.get("requested_model") or model
            if timing_enabled:
                receipt.update(timing_kind="ai_combine", timing_task=kind, timing_size=len(combined),
                               timing_model=receipt_model, provider=provider)
                self.store.record_timing("ai_combine", provider, receipt_model, len(combined), elapsed, task=kind)
            output = [answer]
            receipts.append(receipt)
            combined_characters = len(combined)
        if cancel.is_set():
            raise InterruptedError()
        result = {"text": "\n\n".join(output), "provider": provider,
                  "model": receipts[-1].get("requested_model", config["ai_model"] or "CLI default"),
                  "created": now(), "source_hash": source["hash"], "source_provider": source["provider"],
                  "parts": len(parts), "part_sizes": [len(part) for part in parts],
                  "source_characters": len(source["timed_text"]), "combined_characters": combined_characters,
                  "receipts": receipts,
                  "instructions": config["instructions"],
                  "task_instructions": config[{"formatted": "format_instructions", "summary": "summary_instructions", "ask": "ask_instructions"}[kind]]}
        with self.store.lock:
            current = self.store.get("video", video["id"])
            if kind == "ask":
                current["conversation"].append({"question": question, "answer": result.pop("text"), **result})
            else:
                current["outputs"][kind] = result
            current["updated"] = now()
            self.store.put("video", current["id"], current)
        self.store.update("job", job["id"], message=f"{kind.title()} saved · {provider.title()}", partial="")

    def import_text(self, data):
        text = str(data.get("text", ""))
        if not text.strip() or len(text) > 1_000_000:
            raise ValueError("Import between 1 and 1,000,000 characters of transcript text.")
        vid = providers.video_id(data["url"]) if data.get("url") else "import-" + uuid.uuid4().hex[:12]
        title = str(data.get("title") or "Imported transcript")[:250]
        existing = self.store.get("video", vid)
        if existing and selected(existing) and existing.get("selected_provider") != "import":
            raise ValueError("That video already has a saved transcript. Import without a URL to create a separate entry.")
        source = {"provider": "import", "text": text, "timed_text": text, "segments": [],
                  "hash": hashlib.sha256(text.encode()).hexdigest(), "language": "unknown",
                  "words": len(text.split()), "characters": len(text), "title": title, "fetched_at": now()}
        video = existing or {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}" if data.get("url") else "",
                             "sources": {}, "attempts": [], "outputs": {}, "conversation": [], "created": now()}
        video.update(title=title, selected_provider="import", updated=now(), is_sample=bool(data.get("is_sample")))
        video["sources"]["import"] = source
        self.store.put("video", vid, video)
        return {"id": vid}

    def export(self, vid, tab, timestamps=False):
        video = self.store.get("video", vid)
        if not video:
            raise ValueError("Transcript not found.")
        source = selected(video)
        if not source:
            raise ValueError("This video has no saved transcript.")
        if tab == "raw":
            body = source["timed_text" if timestamps else "text"]
        elif tab in ("formatted", "summary"):
            result = video["outputs"].get(tab)
            if not result:
                raise ValueError("Generate this output first.")
            body = result["text"]
            if result["source_hash"] != source["hash"]:
                body = "NOTE: This output was generated from an earlier/different transcript source.\n\n" + body
        elif tab == "ask":
            body = "\n\n".join("Q: " + c["question"] + "\n\n" + c["answer"] for c in video["conversation"])
        elif tab == "json":
            return json.dumps(video, ensure_ascii=False, indent=2), safe_filename(video["title"], vid, "json")
        else:
            raise ValueError("Unknown export tab.")
        completeness = (f"Transcript completeness: {source['completion_warning']}\n"
                        if source.get("completion_warning") else "")
        source_name = (f"{video['local_file']['name']} (file on this PC)" if video.get("local_file")
                       else video["url"] or "Manual import")
        heading = f"# {video['title']}\n\nSource: {source_name}\nTranscript service: {providers.LABELS.get(source['provider'], source['provider'])}\n{completeness}View: {tab}\n\n"
        return heading + body, safe_filename(video["title"], vid + "-" + tab, "md")

    def archive(self):
        stream = io.BytesIO()
        combined = []
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for video in reversed(self.store.list("video")):
                if not selected(video):
                    continue
                for tab in ("raw", *video["outputs"].keys(), "ask"):
                    if tab == "ask" and not video["conversation"]:
                        continue
                    text, filename = self.export(video["id"], tab, timestamps=True)
                    archive.writestr(filename, text)
                    if tab == "raw":
                        combined.append(text)
            archive.writestr("all-transcripts.md", "\n\n---\n\n".join(combined))
        return stream.getvalue()


def safe_filename(title, suffix, extension):
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(". ")[:100] or "transcript"
    return f"{title}-{suffix}.{extension}"


class Handler(BaseHTTPRequestHandler):
    server_version = f"AIVideoTranscriber/{app_version.VERSION}"

    @property
    def app(self):
        return self.server.app

    def log_message(self, format, *args):
        # Do not record prompts, API keys, or query strings in the HTTP log.
        pass

    def allowed_host(self):
        return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def reply(self, payload, status=200, mime="application/json; charset=utf-8", filename=None):
        raw = json.dumps(payload, ensure_ascii=False).encode() if isinstance(payload, (dict, list)) else payload
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def validate_local_file_upload(self, route, filename, size):
        if route not in ("whisper_then_gemini", "whisper", "gemini"):
            raise ValueError("Choose Whisper, then Gemini; Whisper only; or Gemini only.")
        if isinstance(size, bool) or not isinstance(size, int):
            raise ValueError("The browser must provide a valid file size.")
        if size <= 0:
            raise ValueError("Choose a non-empty video file.")
        name = safe_local_filename(filename)
        readiness = self.app.file_readiness()
        whisper_status = readiness["whisper"].get("status")
        whisper_ready = whisper_status in ("ready", "cpu_only")
        gemini_ready = readiness["gemini"].get("status") == "ready"
        gemini_file_supported = gemini_local_file_supported(name)
        cap = GEMINI_LOCAL_FILE_CAP
        if route == "gemini" and size > cap:
            raise ValueError("Gemini supports local files up to 2,000,000,000 bytes. Use Whisper.")
        if route == "gemini" and not gemini_file_supported:
            raise ValueError("Gemini does not support this video file type. Use Whisper.")
        if route == "whisper" and not whisper_ready:
            reason = readiness["whisper"].get("reason") or readiness["whisper"].get("venv_path") or whisper_status
            raise ValueError(f"Whisper is not ready ({reason}). Recheck Whisper readiness.")
        if route == "whisper_then_gemini" and size > cap and not whisper_ready:
            reason = readiness["whisper"].get("reason") or readiness["whisper"].get("venv_path") or whisper_status
            raise ValueError(f"This file exceeds Gemini's 2,000,000,000-byte cap and Whisper is not ready ({reason}). Use Whisper.")
        if route == "whisper_then_gemini" and not gemini_file_supported and not whisper_ready:
            reason = readiness["whisper"].get("reason") or readiness["whisper"].get("venv_path") or whisper_status
            raise ValueError(f"Gemini does not support this video file type and Whisper is not ready ({reason}).")
        route_ready = (whisper_ready if route == "whisper" else
                       gemini_ready if route == "gemini" else
                       whisper_ready or (gemini_ready and size <= cap and gemini_file_supported))
        if not route_ready:
            raise ValueError("Nothing in the selected route is ready. Set up Whisper or add a Gemini key in Settings.")
        try:
            free = shutil.disk_usage(self.app.store.root).free
        except OSError as error:
            raise ValueError(f"Could not check free disk space before upload: {error}") from None
        if free < size:
            raise ValueError("There is not enough free disk space to store this video upload.")
        warning = ""
        if route == "whisper_then_gemini" and size > cap:
            warning = "This file exceeds Gemini's 2,000,000,000-byte cap. There will be no Gemini fallback; the server skips Gemini."
        elif route == "whisper_then_gemini" and not gemini_file_supported:
            warning = "Gemini does not support this video file type. There will be no Gemini fallback; the server skips Gemini."
        return {"route": route, "name": name, "size": size, "warning": warning}

    def preflight_local_file(self, data):
        filename = data.get("filename")
        size = data.get("size")
        route = data.get("route")
        if not isinstance(filename, str):
            raise ValueError("Choose a video file with a valid filename.")
        validated = self.validate_local_file_upload(route, filename, size)
        return self.reply({"ok": True, "filename": validated["name"],
                           "size": validated["size"], "warning": validated["warning"]})

    def upload_local_file(self):
        route = self.headers.get("X-AIVideoTranscriber-File-Route", "")
        if route not in ("whisper_then_gemini", "whisper", "gemini"):
            raise ValueError("Choose Whisper, then Gemini; Whisper only; or Gemini only.")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            raise ValueError("The browser must provide a valid Content-Length.") from None
        validated = self.validate_local_file_upload(
            route, self.headers.get("X-AIVideoTranscriber-Filename", ""), size)
        name = validated["name"]
        job_id = uuid.uuid4().hex
        video_id = "file-" + uuid.uuid4().hex[:12]
        folder = self.app.local_task_dir(job_id)
        file_path = folder / name
        video_created = False
        try:
            folder.mkdir(parents=True, exist_ok=False)
            received = 0
            with file_path.open("xb") as output:
                while received < size:
                    block = self.rfile.read(min(UPLOAD_CHUNK_SIZE, size - received))
                    if not block:
                        raise ValueError("The video upload ended before Content-Length bytes arrived.")
                    output.write(block)
                    received += len(block)
                output.flush()
            if received != size:
                raise ValueError("The video upload size did not match Content-Length.")
            title = Path(name).stem[:250]
            video = {"id": video_id, "title": title, "url": "", "sources": {}, "attempts": [],
                     "outputs": {}, "conversation": [], "created": now(), "updated": now(),
                     "selected_provider": None,
                     "local_file": {"name": name, "size_bytes": size, "duration_seconds": None}}
            job = {"id": job_id, "video_id": video_id, "kind": "file", "status": "queued",
                   "created": now(), "message": "Queued local video transcription", "progress": 0,
                   "config": copy.deepcopy(self.app.store.config), "file_name": name,
                   "file_mime": local_file_mime(name, self.headers.get("Content-Type", "")),
                   "file_size": size, "file_route": route}
            with self.app.submit_lock:
                self.app.store.put("video", video_id, video)
                video_created = True
                self.app.store.put("job", job_id, job)
                self.app.cancel_events[job_id] = threading.Event()
                self.app.queue.put(job_id)
        except Exception:
            if video_created:
                self.app.store.delete("video", video_id)
                self.app.store.delete("job", job_id)
                self.app.cancel_events.pop(job_id, None)
            if folder.exists() or folder.is_symlink():
                self.app.cleanup_local_job({"kind": "file", "id": job_id})
            raise
        return self.reply({"job_id": job_id, "video_id": video_id, "warning": validated["warning"]})

    def do_GET(self):
        if not self.allowed_host():
            return self.reply({"error": "Invalid Host."}, 403)
        path = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(path.query)
        try:
            if path.path == "/health":
                return self.reply({"service": SERVICE, "version": app_version.VERSION, "pid": os.getpid()})
            if path.path == "/api/bootstrap":
                return self.reply({"token": self.app.token, "settings": self.app.store.public_settings(),
                                   "ai": self.app.ai_info(), "data_dir": str(self.app.store.root),
                                   "version": app_version.VERSION})
            if path.path == "/api/local-file/readiness":
                return self.reply(self.app.file_readiness(query.get("recheck", [""])[0] == "1"))
            if path.path == "/api/state":
                jobs = [{k: v for k, v in j.items() if k != "config"}
                        for index, j in enumerate(self.app.store.list("job"))
                        if index < 60 or j["status"] in ("queued", "running")]
                return self.reply({"videos": [brief(v) for v in self.app.store.list("video")], "jobs": jobs,
                                   "providers": self.app.store.list("provider")})
            if path.path == "/api/video":
                video = self.app.store.get("video", query.get("id", [""])[0])
                if not video:
                    return self.reply({"error": "Video not found."}, 404)
                for source in video["sources"].values():
                    source.pop("raw_response", None)
                return self.reply(video)
            if path.path == "/api/export":
                tab = query.get("tab", ["raw"])[0]
                text, filename = self.app.export(query.get("id", [""])[0], tab, query.get("timestamps", ["0"])[0] == "1")
                return self.reply(text, mime="application/json" if tab == "json" else "text/markdown; charset=utf-8", filename=filename)
            if path.path == "/api/export-all":
                return self.reply(self.app.archive(), mime="application/zip", filename="ai-video-transcriber.zip")
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/activity.js": ("activity.js", "text/javascript; charset=utf-8"),
                     "/choices.js": ("choices.js", "text/javascript; charset=utf-8"),
                     "/markdown.js": ("markdown.js", "text/javascript; charset=utf-8"),
                     "/urls.js": ("urls.js", "text/javascript; charset=utf-8"),
                     "/selection.js": ("selection.js", "text/javascript; charset=utf-8"),
                     "/readaloud.js": ("readaloud.js", "text/javascript; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8"),
                     "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path.path in files:
                file, mime = files[path.path]
                return self.reply((ROOT / "static" / file).read_bytes(), mime=mime)
            self.reply({"error": "Not found."}, 404)
        except ValueError as error:
            self.reply({"error": str(error)}, 400)

    def do_POST(self):
        origin = self.headers.get("Origin")
        allowed_origins = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if not self.allowed_host() or (origin and origin not in allowed_origins) or not secrets.compare_digest(self.headers.get("X-AIVideoTranscriber-Token", ""), self.app.token):
            return self.reply({"error": "Request rejected. Reload this local dashboard."}, 403)
        try:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/api/local-file":
                return self.upload_local_file()
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2_000_000:
                raise ValueError("Request size must be between 1 byte and 2 MB.")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            if path == "/api/local-file/preflight":
                return self.preflight_local_file(data)
            if path == "/api/settings":
                return self.reply(self.app.store.save_settings(data))
            if path == "/api/gemini-models":
                try:
                    return self.reply({"models": self.app.gemini_models(bool(data.get("refresh")))})
                except providers.ProviderError as error:
                    return self.reply({"error": self.app.scrub(error)}, MODEL_ERROR_STATUS.get(error.code, 502))
            if path == "/api/antigravity-models":
                # Runs `agy models` (kept 10 minutes). Only the Settings dialog asks, while Antigravity CLI is picked there,
                # because a signed-out CLI may open a Google sign-in page; /api/bootstrap gives only the last list.
                return self.reply(ai_cli.antigravity_model_list())
            if path == "/api/opencode-models":
                # Explicit Settings action only; availability/bootstrap read the ten-minute process cache.
                return self.reply(ai_cli.opencode_model_list(bool(data.get("refresh"))))
            if path == "/api/batch":
                return self.reply(self.app.add_batch(data.get("urls", ""), data.get("mode", "auto"),
                                                     data.get("compare", False),
                                                     refetch=data.get("refetch", False), force=data.get("force", False)))
            if path == "/api/import":
                return self.reply(self.app.import_text(data))
            if path == "/api/cancel":
                return self.reply({"canceled": self.app.cancel(str(data.get("job_id", "")))})
            if path == "/api/activity-remove":
                return self.reply(self.app.remove_activity(str(data.get("job_id", "")), data.get("finished") is True))
            if path == "/api/delete":
                return self.reply(self.app.delete_video(str(data.get("id", ""))))
            if path == "/api/source":
                video = self.app.store.get("video", str(data.get("id", "")))
                provider = data.get("provider")
                if not video or provider not in video["sources"]:
                    raise ValueError("That source is not available for this video.")
                self.app.store.update("video", video["id"], selected_provider=provider, updated=now())
                return self.reply({"ok": True})
            if path == "/api/ai":
                vid, kind = str(data.get("id", "")), data.get("kind")
                if kind not in ("formatted", "summary", "ask"):
                    raise ValueError("Unknown AI task.")
                if self.app.store.config["ai_provider"] == "opencode":
                    model = self.app.store.config["ai_model"] or ai_cli.opencode_default_model()
                    if not ai_cli.opencode_model_is_listed(model):
                        raise ValueError("Load OpenCode's current model list and choose a listed model before starting AI work.")
                video = self.app.store.get("video", vid)
                if not video or not selected(video):
                    raise ValueError("Select a video with a saved transcript first.")
                source = selected(video)
                if source["provider"] == "import" and source["words"] < 2:
                    raise ValueError("This manual import contains only one word. Paste the full transcript before using AI.")
                question = str(data.get("question", "")).strip()
                if kind == "ask" and not 1 <= len(question) <= 10000:
                    raise ValueError("Enter a question of at most 10,000 characters.")
                return self.reply({"job_id": self.app.submit(vid, kind, question=question)})
            self.reply({"error": "Not found."}, 404)
        except (ValueError, TypeError, KeyError) as error:
            self.reply({"error": self.app.scrub(error)}, 400)
        except Exception as error:
            self.reply({"error": self.app.scrub(error)[:1000]}, 500)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, app):
        self.app = app
        super().__init__(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8776)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    app = Application(args.data_dir)
    try:
        server = Server(args.port, app)
    except OSError as error:
        raise SystemExit(f"Could not bind 127.0.0.1:{args.port}: {error}. No other process was stopped.")
    print(f"AI Video Transcriber: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop.set()
        for event in app.cancel_events.values():
            event.set()
        server.server_close()


if __name__ == "__main__":
    main()
