"""Isolated local-file transcription worker for AI Video Transcriber."""
from __future__ import annotations

import argparse
import ctypes
import errno
from functools import wraps
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
import urllib.error

VENV_PYTHON = Path(sys.executable)
VENV_ROOT = Path(sys.prefix)
MODEL_ROOT = VENV_ROOT / "models"
DLL_DIRECTORIES = tuple(
    VENV_ROOT / "Lib" / "site-packages" / "nvidia" / package / "bin"
    for package in ("cublas", "cudnn", "cuda_nvrtc")
)
MODEL_ID = "Systran/faster-whisper-large-v3"
MODEL_DOWNLOAD_TRIES = 10
MODEL_RETRY_DELAYS = (2, 4, 8, 15, 30, 30, 30, 30, 30)
MODEL_REQUIRED_FILES = (
    "model.bin", "config.json", "tokenizer.json", "vocabulary.json", "preprocessor_config.json",
)
_dll_handles = []
_loaded_dlls = []


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def add_dll_directories():
    missing = [str(folder) for folder in DLL_DIRECTORIES if not folder.is_dir()]
    if missing:
        raise RuntimeError("Required NVIDIA DLL directory is missing: " + ", ".join(missing))
    for folder in DLL_DIRECTORIES:
        _dll_handles.append(os.add_dll_directory(str(folder)))
    if os.name == "nt":
        for name in ("cublas64_12.dll", "cublasLt64_12.dll", "cudnn64_9.dll", "nvrtc64_120_0.dll"):
            _loaded_dlls.append(ctypes.WinDLL(name))


def install_av_open_compatibility():
    """Handle PyAV versions that removed faster-whisper's metadata_errors option."""
    import av
    original_open = av.open
    if getattr(original_open, "_aivideotranscriber_metadata_errors_compat", False) is True:
        return

    @wraps(original_open)
    def compatible_open(*args, **kwargs):
        try:
            return original_open(*args, **kwargs)
        except TypeError as error:
            if ("metadata_errors" not in kwargs or not str(error).endswith(
                    "got an unexpected keyword argument 'metadata_errors'")):
                raise
            kwargs.pop("metadata_errors")
            return original_open(*args, **kwargs)

    compatible_open._aivideotranscriber_metadata_errors_compat = True
    av.open = compatible_open


def load_runtime():
    add_dll_directories()
    install_av_open_compatibility()
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    import ctranslate2
    devices = int(ctranslate2.get_cuda_device_count())
    return WhisperModel, download_model, devices


def is_connection_error(error):
    pending = [error]
    seen = set()
    connection_errnos = {errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
                         errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EPIPE}
    connection_winerrors = {10053, 10054, 10060, 10061, 10065}
    connection_class_names = {"ConnectError", "ConnectTimeout", "ReadError", "ReadTimeout",
                              "WriteError", "WriteTimeout", "NetworkError", "ConnectionError",
                              "Timeout", "TimeoutException", "RemoteProtocolError"}
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError, socket.timeout)):
            return True
        if isinstance(current, urllib.error.HTTPError):
            continue
        if isinstance(current, urllib.error.URLError):
            reason = current.reason
            if isinstance(reason, BaseException):
                pending.append(reason)
            continue
        cls = type(current)
        module = cls.__module__
        if cls.__name__ in connection_class_names and module.startswith(("httpx", "httpcore", "requests")):
            return True
        if getattr(current, "winerror", None) in connection_winerrors:
            return True
        if getattr(current, "errno", None) in connection_errnos:
            return True
        message = str(current)
        if ("ConnectError:" in message or "ConnectionError:" in message or "ReadError:" in message) and any(
                marker in message for marker in ("WinError 100", "connection reset", "forcibly closed", "timed out")):
            return True
        for nested in (current.__cause__, current.__context__, getattr(current, "reason", None), *current.args):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def wait_for_model_retry(cancel_path, seconds, sleep=None, monotonic=None):
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    deadline = monotonic() + seconds
    while True:
        if cancel_path.exists():
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return cancel_path.exists()
        sleep(min(0.25, remaining))


def download_model_with_retry(download_model, cancel_path, sleep=None, monotonic=None):
    for attempt in range(1, MODEL_DOWNLOAD_TRIES + 1):
        if cancel_path.exists():
            emit({"type": "canceled"})
            return None
        message = "Downloading the Whisper voice model (about 2.9 GB, first time only)"
        message += f" · try {attempt} of {MODEL_DOWNLOAD_TRIES}"
        if attempt > 1:
            message += " after a dropped connection"
        emit({"type": "progress", "message": message})
        try:
            return download_model("large-v3", cache_dir=str(MODEL_ROOT))
        except Exception as error:
            if not is_connection_error(error) or attempt == MODEL_DOWNLOAD_TRIES:
                raise
            delay = MODEL_RETRY_DELAYS[attempt - 1]
            if wait_for_model_retry(cancel_path, delay, sleep=sleep, monotonic=monotonic):
                emit({"type": "canceled"})
                return None
    raise AssertionError("The bounded model retry loop ended unexpectedly.")


def resolve_model_path(download_model, cancel_path):
    try:
        cached_path = download_model("large-v3", cache_dir=str(MODEL_ROOT), local_files_only=True)
        if cached_path and all((Path(cached_path) / name).is_file() for name in MODEL_REQUIRED_FILES):
            emit({"type": "progress", "message": "Loading the Whisper voice model"})
            return cached_path
    except Exception:
        pass
    return download_model_with_retry(download_model, cancel_path)


def gpu_name():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0:
            return next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def check():
    if not VENV_PYTHON.is_file():
        emit({"status": "not_installed", "venv_path": str(VENV_PYTHON)})
        return 0
    try:
        _, _, devices = load_runtime()
    except ModuleNotFoundError as error:
        if error.name == "faster_whisper":
            emit({"status": "not_installed", "venv_path": str(VENV_PYTHON)})
        else:
            emit({"status": "broken", "reason": f"Missing Python module: {error.name}"})
        return 0
    except Exception as error:
        emit({"status": "broken", "reason": str(error)[:300] or type(error).__name__})
        return 0
    if devices:
        emit({"status": "ready", "devices": devices, "gpu_name": gpu_name()})
    else:
        emit({"status": "cpu_only", "devices": 0, "label": "CPU, slow"})
    return 0


def transcribe(path, cancel_path):
    started = time.monotonic()
    try:
        WhisperModel, download_model, devices = load_runtime()
        device = "cuda" if devices > 0 else "cpu"
        compute_type = "float16" if devices > 0 else "int8"
        model_setup_started = time.monotonic()
        model_path = resolve_model_path(download_model, cancel_path)
        if model_path is None:
            return 2
        model = WhisperModel(str(model_path), device=device, compute_type=compute_type)
        model_setup_elapsed = time.monotonic() - model_setup_started
        if cancel_path.exists():
            emit({"type": "canceled"})
            return 2
        inference_started = time.monotonic()
        segments_iterator, info = model.transcribe(str(path), beam_size=5, vad_filter=True)
        total = max(0.0, float(getattr(info, "duration", 0.0) or 0.0))
        segments = []
        for segment in segments_iterator:
            if cancel_path.exists():
                emit({"type": "canceled"})
                return 2
            item = {"start": float(segment.start), "end": float(segment.end), "text": str(segment.text)}
            segments.append(item)
            emit({"type": "progress", "completed_seconds": round(item["end"], 2),
                  "total_seconds": round(total, 2),
                  "message": f"Transcribing {item['end']:.1f}/{total:.1f} seconds"})
        result = {
            "type": "result",
            "segments": segments,
            "language": str(getattr(info, "language", "unknown") or "unknown"),
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
            "duration": total,
            "model": "large-v3",
            "model_path": str(model_path),
            "device": gpu_name() if devices else "CPU, slow",
            "compute_type": compute_type,
            "model_setup_elapsed_seconds": round(model_setup_elapsed, 2),
            "inference_elapsed_seconds": round(time.monotonic() - inference_started, 2),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        emit(result)
        return 0
    except InterruptedError:
        emit({"type": "canceled"})
        return 2
    except Exception as error:
        emit({"type": "error", "message": str(error)[:1000] or type(error).__name__})
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--cancel-file", type=Path)
    args = parser.parse_args()
    if args.check:
        return check()
    if args.file is None or args.cancel_file is None:
        parser.error("--file and --cancel-file are required")
    return transcribe(args.file, args.cancel_file)


if __name__ == "__main__":
    raise SystemExit(main())
