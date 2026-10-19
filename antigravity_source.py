"""Antigravity transcript source: download one YouTube audio file, transcribe it locally, then erase it."""
from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

import runtime_paths

AGY_PYTHON = runtime_paths.resolve_interpreter(runtime_paths.AGY_PYTHON_ENV,
                                               runtime_paths.discover_download_python)
SIGN_IN_MESSAGE = "Antigravity CLI needs you to sign in again: open PowerShell, run agy, and sign in with your Google account."
SIGN_IN = re.compile(
    r"\bsign(?:ing)?[ -]?in\b|\blog(?:ging)?[ -]?in\b|authentication (?:required|expired|failed)|log back in|"
    r"accounts\.google\.com|visit the URL to log in|paste the authorization code",
    re.IGNORECASE,
)
BEGIN = "<<<TRANSCRIPT_{token}_BEGIN>>>"
END = "<<<TRANSCRIPT_{token}_END>>>"
TASK_NAME = re.compile(r"task-[a-f0-9]{24}\Z")
OWNER_NAME = "owner.json"
CANCEL_GRACE_SECONDS = 2.0


class SourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        except (OSError, subprocess.SubprocessError):
            process.kill()
    else:
        process.kill()


def download_command(vid: str, work: Path, audio_stem: str) -> list[str]:
    if AGY_PYTHON is None:
        raise SourceError("setup", runtime_paths.missing_interpreter_message(
            runtime_paths.AGY_PYTHON_ENV, "Antigravity audio download"))
    return [str(AGY_PYTHON), "-B", "-m", "yt_dlp", "--ignore-config", "--no-cache-dir", "--no-playlist",
            "--no-write-info-json", "--no-write-playlist-metafiles", "--no-write-comments", "--no-write-thumbnail",
            "--no-write-subs", "--no-write-auto-subs",
            "--newline", "--format", "bestaudio", "--output", str(work / f"{audio_stem}.%(ext)s"),
            "--print", "before_dl:AIVIDEOTRANSCRIBER_TITLE=%(title)j", "--print",
            "before_dl:AIVIDEOTRANSCRIBER_DURATION=%(duration)j", "--print",
            "before_dl:AIVIDEOTRANSCRIBER_EXTRACTOR=%(extractor_key)j", "--print",
            "after_move:AIVIDEOTRANSCRIBER_FILE=%(filepath)j",
            "--", f"https://www.youtube.com/watch?v={vid}"]


def _tail(path: Path, limit: int = 8000) -> str:
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode("utf-8", errors="replace")


def download_audio(vid: str, work: Path, audio_stem: str, cancel: threading.Event) -> dict:
    if AGY_PYTHON is None or not AGY_PYTHON.is_file():
        raise SourceError("setup", runtime_paths.missing_interpreter_message(
            runtime_paths.AGY_PYTHON_ENV, "Antigravity audio download", AGY_PYTHON))
    stdout_path, stderr_path = work / "download.stdout", work / "download.stderr"
    command = download_command(vid, work, audio_stem)
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as error:
            raise SourceError("setup", f"Could not start yt-dlp: {error}.") from None
        try:
            while process.poll() is None:
                if cancel.wait(.2):
                    terminate(process)
                    raise InterruptedError("Canceled while downloading audio.")
                if stdout.tell() + stderr.tell() > 8_000_000:
                    terminate(process)
                    raise SourceError("download", "yt-dlp produced more than 8 MB of diagnostic output.")
        finally:
            if process.poll() is None:
                terminate(process)
    output = stdout_path.read_text(encoding="utf-8", errors="replace")
    if process.returncode:
        stderr = _tail(stderr_path)
        if "no module named yt_dlp" in stderr.lower() or "no module named 'yt_dlp'" in stderr.lower():
            raise SourceError("setup", runtime_paths.missing_module_message(
                runtime_paths.AGY_PYTHON_ENV, "yt-dlp", AGY_PYTHON))
        raise SourceError("download", "Could not download this video's audio. " + (stderr[-1600:].strip() or "yt-dlp failed."))
    metadata = {}
    for line in output.splitlines():
        key, marker, value = line.partition("=")
        if marker and key in ("AIVIDEOTRANSCRIBER_TITLE", "AIVIDEOTRANSCRIBER_DURATION",
                              "AIVIDEOTRANSCRIBER_EXTRACTOR", "AIVIDEOTRANSCRIBER_FILE"):
            try:
                metadata[key.removeprefix("AIVIDEOTRANSCRIBER_").lower()] = json.loads(value)
            except json.JSONDecodeError:
                raise SourceError("download", "yt-dlp returned unreadable download metadata.") from None
    path = Path(str(metadata.get("file") or "")).resolve()
    if not path.is_file() or path.parent != work.resolve() or path.stem != audio_stem:
        raise SourceError("download", "yt-dlp did not create the expected isolated audio file.")
    allowed = {path, stdout_path, stderr_path}
    if set(work.iterdir()) != allowed:
        raise SourceError("download", "yt-dlp left unexpected files beside the downloaded audio.")
    for diagnostic in (stdout_path, stderr_path):
        diagnostic.unlink()
    metadata["path"] = path
    metadata["bytes"] = path.stat().st_size
    try:
        metadata["yt_dlp_version"] = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        metadata["yt_dlp_version"] = "unknown"
    return metadata


def transcript_prompt(audio_name: str, token: str) -> str:
    return (
        "Transcribe the complete speech in the one local audio file named " + json.dumps(audio_name) + ". "
        "Actually read the media with view_file before answering. Use only view_file on that exact local file. Do not "
        "search, fetch a URL, browse, run commands, read any other file, or write files. No web page, captions, transcript, "
        "video URL, reference text, or other local file is provided. If view_file cannot provide playable audio, report "
        "MEDIA_UNAVAILABLE and the concrete file-tool error instead of inventing speech. Do not change settings or request "
        "permissions. Raw tool events are audited, and success requires a read of the named audio with no other tool or file. "
        "Do not summarize, explain, identify the video, or add facts. Preserve the "
        "spoken order and wording as closely as the audio permits. Return the final plain transcript only between these "
        "two exact marker lines; put no thoughts, notes, Markdown fence, or other text between the markers:\n"
        + BEGIN.format(token=token) + "\n<complete transcript>\n" + END.format(token=token) + "\n\n"
        "Mandatory PT-1 (apply these inline rules; do not read the external policy file or any other file): test the actual changed "
        "boundary and relevant failures, name shared safety interactions, certify the whole approved application on one "
        "exact final candidate at completion, and reuse unchanged evidence. A wider or repeated test needs a named risk or "
        "diagnostic question and a stopping condition. Independent review checks source, actual outcome, assertions, results, "
        "and coverage without automatically duplicating valid runs. The policy reference is unavailable in this isolated audio "
        "task; do not read it or other local files. This transcription is one bounded operation, not application certification."
    )


def wmi_worker_command(request: Path, result: Path, cancel_path: Path) -> tuple[str, list[str]]:
    worker = Path(__file__).with_name("antigravity_source_worker.py")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pythonw = Path(sys.executable)
    command = subprocess.list2cmdline([str(pythonw), str(worker), "--request", str(request),
                                      "--result", str(result), "--cancel", str(cancel_path)])
    script = ("$s=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0};"
              "$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              "-Arguments @{CommandLine=$env:AIVIDEOTRANSCRIBER_AGY_WORKER;ProcessStartupInformation=$s};"
              "$r|ConvertTo-Json -Compress")
    return command, ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script]


def _process_started_at(pid: int) -> int | None:
    if os.name != "nt":
        return None
    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel_time, user_time = FileTime(), FileTime(), FileTime(), FileTime()
        code = ctypes.c_ulong()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(kernel_time), ctypes.byref(user_time)):
            return None
        return (created.high << 32) | created.low
    finally:
        kernel.CloseHandle(handle)


def _process_alive(pid: int, started_at: int | None = None) -> bool:
    actual = _process_started_at(pid)
    return actual is not None and (started_at is None or actual == started_at)


def _process_receipt(pid: int) -> dict:
    started_at = _process_started_at(pid)
    if started_at is None:
        raise SourceError("setup", "Windows could not verify the Antigravity task owner.")
    return {"pid": pid, "started_at": started_at}


def _write_owner(task: Path, worker: dict | None = None) -> None:
    value = {"version": 1, "parent": _process_receipt(os.getpid())}
    if worker:
        value["worker"] = worker
    path = task / OWNER_NAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def _task_is_active(task: Path) -> bool:
    owner = task / OWNER_NAME
    try:
        if owner.is_symlink() or owner.stat().st_size > 4096:
            return False
        value = json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    for name in ("parent", "worker"):
        receipt = value.get(name) if isinstance(value, dict) else None
        if not isinstance(receipt, dict):
            continue
        pid, started_at = receipt.get("pid"), receipt.get("started_at")
        if isinstance(pid, int) and pid > 0 and isinstance(started_at, int) and started_at > 0:
            if _process_alive(pid, started_at):
                return True
    return False


def _task_worker_is_active(task: Path) -> bool:
    owner = task / OWNER_NAME
    try:
        if owner.is_symlink() or owner.stat().st_size > 4096:
            return False
        value = json.loads(owner.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return False
    receipt = value.get("worker") if isinstance(value, dict) else None
    if not isinstance(receipt, dict):
        return False
    pid, started_at = receipt.get("pid"), receipt.get("started_at")
    return (isinstance(pid, int) and pid > 0 and isinstance(started_at, int) and started_at > 0
            and _process_alive(pid, started_at))


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & 0x400)
    except (AttributeError, OSError):
        return path.is_symlink()


def remove_private_scratch(folder: Path, attempts: int = 5, delay: float = .1) -> None:
    """Remove audio/transcript scratch and fail closed if Windows did not remove it."""
    if not folder.exists() and not folder.is_symlink():
        return
    if _is_reparse_point(folder):
        try:
            if getattr(folder, "is_junction", lambda: False)():
                os.rmdir(folder)
            else:
                folder.unlink()
        except OSError as error:
            raise SourceError("cleanup", f"Could not remove private Antigravity scratch at {folder}.") from error
        return
    for attempt in range(attempts):
        try:
            shutil.rmtree(folder)
        except FileNotFoundError:
            return
        except OSError as error:
            if attempt + 1 == attempts:
                raise SourceError("cleanup", f"Could not remove private Antigravity scratch at {folder}.") from error
            time.sleep(delay)
        else:
            if not folder.exists() and not folder.is_symlink():
                return
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise SourceError("cleanup", f"Could not remove private Antigravity scratch at {folder}.")


def cleanup_stale_tasks(root: Path | str) -> dict:
    """At startup, remove only stale direct task folders after proving no owned task is active."""
    root = Path(root)
    if not root.exists():
        return {"removed": 0, "preserved": 0}
    if _is_reparse_point(root):
        raise SourceError("cleanup", f"Refusing to traverse a linked Antigravity scratch root at {root}.")
    try:
        candidates = [path for path in root.iterdir() if TASK_NAME.fullmatch(path.name)]
    except OSError as error:
        raise SourceError("cleanup", f"Could not inspect Antigravity scratch at {root}.") from error
    if any(_task_is_active(task) for task in candidates):
        return {"removed": 0, "preserved": len(candidates)}
    for task in candidates:
        remove_private_scratch(task)
    try:
        root.rmdir()
    except OSError:
        pass
    return {"removed": len(candidates), "preserved": 0}


def _terminate_owned_process(pid: int, started_at: int) -> None:
    if not _process_alive(pid, started_at):
        return
    try:
        stopped = subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise SourceError("cleanup", "Could not stop the canceled Antigravity worker safely.") from error
    if stopped.returncode and _process_alive(pid, started_at):
        raise SourceError("cleanup", "Could not stop the canceled Antigravity worker safely.")
    for _ in range(20):
        if not _process_alive(pid, started_at):
            return
        time.sleep(.05)
    raise SourceError("cleanup", "Could not confirm that the canceled Antigravity worker stopped.")


def run_hidden(work: Path, audio: Path, prompt: str, model: str, cancel: threading.Event) -> dict:
    if os.name != "nt":
        raise SourceError("setup", "The Antigravity local-audio source requires Windows.")
    run = work.parent / ("run-" + secrets.token_hex(8))
    run.mkdir()
    request, result, cancel_path = run / "request.json", run / "result.json", run / "cancel"
    request.write_text(json.dumps({"work_dir": str(work), "audio_name": audio.name, "prompt": prompt, "model": model}), encoding="utf-8")
    command, launcher = wmi_worker_command(request, result, cancel_path)
    environment = dict(os.environ)
    environment["AIVIDEOTRANSCRIBER_AGY_WORKER"] = command
    worker = None
    try:
        launched = subprocess.run(launcher, stdin=subprocess.DEVNULL, capture_output=True, env=environment,
                                  creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if launched.returncode:
            raise SourceError("setup", "Windows could not start the hidden Antigravity worker: "
                              + launched.stderr.decode("utf-8", errors="replace")[-1000:].strip())
        try:
            receipt = json.loads(launched.stdout.decode("utf-8"))
            pid = int(receipt["ProcessId"])
            if int(receipt["ReturnValue"]):
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            raise SourceError("setup", "Windows returned an unreadable Antigravity worker launch receipt.") from None
        worker = _process_receipt(pid)
        _write_owner(work.parent, worker)
        cancel_started = None
        while not result.exists():
            if cancel.is_set():
                if cancel_started is None:
                    cancel_path.touch()
                    cancel_started = time.monotonic()
                elif time.monotonic() - cancel_started >= CANCEL_GRACE_SECONDS:
                    _terminate_owned_process(pid, worker["started_at"])
                    raise InterruptedError("Antigravity transcription canceled; its unresponsive worker was stopped.")
            if not _process_alive(pid, worker["started_at"]):
                time.sleep(.25)
                if not result.exists():
                    if cancel_started is not None:
                        raise InterruptedError("Antigravity transcription canceled.")
                    raise SourceError("service_error", "The hidden Antigravity worker stopped without a result.")
            time.sleep(.2)
        for _ in range(20):
            if not _process_alive(pid, worker["started_at"]):
                break
            time.sleep(.05)
        else:
            _terminate_owned_process(pid, worker["started_at"])
        answer = json.loads(result.read_text(encoding="utf-8"))
        status = answer.get("status")
        if status == "canceled":
            raise InterruptedError("Antigravity transcription canceled.")
        if status == "sign_in":
            raise SourceError("authentication", SIGN_IN_MESSAGE)
        if status == "unsafe_tools":
            raise SourceError("unsafe_tools", "Antigravity attempted a tool other than view_file on the isolated local audio; the run was stopped and discarded.")
        if status == "error":
            raise SourceError("service_error", str(answer.get("message") or "Antigravity worker failed."))
        if status != "finished":
            raise SourceError("invalid_response", "Antigravity returned an unreadable worker result.")
        stdout = Path(answer["stdout"]).read_text(encoding="utf-8", errors="replace")
        stderr = Path(answer["stderr"]).read_bytes()
        if answer.get("returncode"):
            raise SourceError("service_error", f"Antigravity exited {answer['returncode']}: " + _bytes_tail(stderr))
        return parse_run(stdout, stderr.decode("utf-8", errors="replace"), audio)
    finally:
        if worker is None or not _process_alive(worker["pid"], worker["started_at"]):
            remove_private_scratch(run)


def _bytes_tail(data: bytes, limit: int = 1600) -> str:
    return data[-limit:].decode("utf-8", errors="replace").strip() or "No diagnostic output."


def _events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict) and isinstance(event.get("event"), str):
            events.append(event)
    return events


_MODEL_EVENT_NAMES = {"user", "assistant", "model_response", "agent_response"}
_MODEL_CONTENT_FIELDS = {"response", "content", "text", "text_delta", "delta", "output"}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _without_model_content(value):
    if isinstance(value, dict):
        return {key: _without_model_content(child) for key, child in value.items()
                if str(key).lower() not in _MODEL_CONTENT_FIELDS}
    if isinstance(value, list):
        return [_without_model_content(child) for child in value]
    return value


def event_requests_sign_in(event: dict) -> bool:
    """Inspect CLI control/diagnostic fields, never model or user response text."""
    name = str(event.get("event") or "").lower()
    if name == "result":
        result = event.get("result")
        return isinstance(result, dict) and bool(SIGN_IN.search(str(result.get("error") or "")))
    if name in _MODEL_EVENT_NAMES:
        return False
    return bool(SIGN_IN.search(json.dumps(_without_model_content(event), ensure_ascii=False)))


def _plain_requests_sign_in(text: str) -> bool:
    plain = _ANSI.sub("", text).lstrip()
    return bool(plain and plain[0].isalpha() and SIGN_IN.search(plain))


def stdout_requests_sign_in(stdout: str) -> bool:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            # A normal JSON fragment may hold response text. Only a plainly non-JSON CLI prompt is safe to inspect.
            if _plain_requests_sign_in(line):
                return True
            continue
        if isinstance(event, dict) and event_requests_sign_in(event):
            return True
    return False


def _tool_records(events: list[dict]) -> list[tuple[dict, dict]]:
    records = []
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("tool_info"), dict):
                records.append((value["tool_info"], value))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(events)
    return records


def extract_transcript(response: str, token: str) -> str:
    begin, end = BEGIN.format(token=token), END.format(token=token)
    start, finish = response.rfind(begin), response.rfind(end)
    if start < 0 or finish < start:
        raise SourceError("invalid_response", "Antigravity did not return the transcript in the required final markers.")
    text = response[start + len(begin):finish].strip()
    if not text or len(text) > 5_000_000:
        raise SourceError("invalid_response", "Antigravity returned an empty or oversized transcript.")
    return text


def parse_run(stdout: str, stderr: str, audio: Path, token: str | None = None) -> dict:
    events = _events(stdout)
    result = next((event.get("result") for event in reversed(events)
                   if event.get("event") == "result" and isinstance(event.get("result"), dict)), None)
    if not result:
        raise SourceError("invalid_response", "Antigravity returned no readable final result.")
    error = str(result.get("error") or "")
    if SIGN_IN.search(stderr) or stdout_requests_sign_in(stdout):
        raise SourceError("authentication", SIGN_IN_MESSAGE)
    if result.get("status") != "SUCCESS" or error:
        raise SourceError("service_error", error[:1200] or f"Antigravity ended with status {result.get('status')}.")
    records = _tool_records(events)
    tools = [tool for tool, _ in records]
    expected = audio.resolve()
    for tool, event in records:
        parameters = tool.get("parameters") if isinstance(tool, dict) else None
        path = parameters.get("AbsolutePath") if isinstance(parameters, dict) else None
        if tool.get("name") != "view_file" or not isinstance(path, str) or Path(path).resolve() != expected:
            raise SourceError("unsafe_tools", "Antigravity used a tool other than view_file on the isolated local audio; the result was discarded.")
        state = str(event.get("state") or "").upper()
        tool_error = event.get("error") or tool.get("error")
        if state in ("FAILED", "ERROR", "CANCELED", "CANCELLED") or tool_error:
            detail = str(tool_error or state).strip()[:500]
            raise SourceError("invalid_response", "Antigravity could not read the local audio file: " + detail)
    if not tools:
        raise SourceError("invalid_response", "Antigravity did not read the local audio file.")
    states = [str(event.get("state") or "").upper() for _, event in records if event.get("state")]
    if states and "DONE" not in states:
        raise SourceError("invalid_response", "Antigravity did not finish reading the local audio file.")
    response = str(result.get("response") or "")
    if token is None:
        found = re.findall(r"<<<TRANSCRIPT_([a-f0-9]{24})_BEGIN>>>", response)
        if not found:
            raise SourceError("invalid_response", "Antigravity did not mark its final transcript.")
        token = found[-1]
    text = extract_transcript(response, token)
    init = next((event.get("init") for event in events if event.get("event") == "init" and isinstance(event.get("init"), dict)), {})
    return {"text": text, "model": init.get("model"), "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
            "conversation_id": result.get("conversation_id"), "tool_calls": len(tools)}


def fetch(vid: str, language: str = "en", model: str = "", cancel: threading.Event | None = None,
          progress=None, work_root: Path | str | None = None, downloader=download_audio, runner=run_hidden,
          step_update=None) -> dict:
    cancel = cancel or threading.Event()
    progress = progress or (lambda message: None)
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(vid)):
        raise SourceError("invalid_request", "Enter a valid YouTube video ID.")
    root = Path(work_root) if work_root is not None else Path(__file__).resolve().parent / "_TEMP" / "antigravity-source-runtime"
    root.mkdir(parents=True, exist_ok=True)
    task_token = secrets.token_hex(12)
    staging = root / (".task-" + task_token + ".creating")
    task = root / ("task-" + task_token)
    staging.mkdir()
    try:
        _write_owner(staging)
        os.replace(staging, task)
    except BaseException:
        remove_private_scratch(staging)
        raise
    work = task / "audio"
    work.mkdir(parents=True)
    started = time.monotonic()
    try:
        if cancel.is_set():
            raise InterruptedError("Canceled before downloading audio.")
        progress("Antigravity · downloading private local audio")
        stem = secrets.token_hex(12)
        metadata = downloader(vid, work, stem, cancel)
        audio = Path(metadata["path"])
        try:
            duration = float(metadata.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        if step_update and duration > 0:
            step_update(duration, model or "CLI default")
        if cancel.is_set():
            raise InterruptedError("Canceled before Antigravity started.")
        progress("Antigravity · transcribing the complete local audio")
        token = secrets.token_hex(12)
        run = runner(work, audio, transcript_prompt(audio.name, token), model, cancel)
        # A test runner may return an unparsed response; production run_hidden already validates and extracts it.
        if "response" in run:
            run = parse_run(run["response"], str(run.get("stderr") or ""), audio, token)
        text = str(run["text"]).strip()
        if not text:
            raise SourceError("no_transcript", "Antigravity returned no speech text.")
        source = {"provider": "antigravity", "video_id": vid, "language": "unknown",
                  "title": str(metadata.get("title") or vid)[:250], "segments": [], "text": text, "timed_text": text,
                  "generated_captions": True, "characters": len(text), "words": len(text.split()),
                  "last_timestamp": max(0, duration), "elapsed_seconds": round(time.monotonic() - started, 2),
                  "raw_response": {"source": "downloaded local audio", "audio_bytes": metadata.get("bytes"),
                                   "duration_seconds": duration, "extractor": metadata.get("extractor"),
                                   "yt_dlp_version": metadata.get("yt_dlp_version"), "model": run.get("model") or "CLI default",
                                   "usage": run.get("usage", {}), "conversation_id": run.get("conversation_id"),
                                   "view_file_calls": run.get("tool_calls")}}
        source["hash"] = hashlib.sha256(json.dumps({"video_id": vid, "provider": "antigravity", "text": text},
                                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        source["model"] = run.get("model") or "CLI default"
        return source
    finally:
        if _task_worker_is_active(task):
            raise SourceError("cleanup", "A hidden Antigravity worker is still active; its private scratch was preserved for safe startup cleanup.")
        remove_private_scratch(task)
        try:
            root.rmdir()
        except OSError:
            pass
