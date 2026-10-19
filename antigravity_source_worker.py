"""Hidden worker for one Antigravity local-audio transcription.

The dashboard starts this file through WMI so it runs in the signed-in Windows
account's normal environment, outside an app-container environment.  Its only
durable output is a small terminal JSON receipt; stdout/stderr stay in the
caller-owned scratch directory and are removed by the caller.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time


SIGN_IN = re.compile(
    r"\bsign(?:ing)?[ -]?in\b|\blog(?:ging)?[ -]?in\b|authentication (?:required|expired|failed)|log back in|accounts\.google\.com|"
    r"visit the URL to log in|paste the authorization code",
    re.IGNORECASE,
)
REMOVED_ENV = (
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION", "GOOGLE_GEMINI_BASE_URL", "GEMINI_BASE_URL", "AGY_ADC_AUTH",
    "AGY_LLM_GATEWAY_URL", "SUPADATA_API_KEY", "FETCHTRANSCRIPT_API_KEY",
)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def sign_in_chunk(tail: str, chunk: bytes) -> tuple[bool, str]:
    """Scan stderr incrementally, retaining enough text to match a phrase split across chunks."""
    text = tail + chunk.decode("utf-8", errors="replace")
    return bool(SIGN_IN.search(text)), text[-512:]


def plain_requests_sign_in(text: str) -> bool:
    plain = ANSI.sub("", text).lstrip()
    return bool(plain and plain[0].isalpha() and SIGN_IN.search(plain))


def event_requests_sign_in(event: dict) -> bool:
    """Inspect CLI control fields while excluding user/model response content."""
    name = str(event.get("event") or "").lower()
    result = event.get("result")
    if name == "result":
        return isinstance(result, dict) and bool(SIGN_IN.search(str(result.get("error") or "")))
    if name in {"user", "assistant", "model_response", "agent_response"}:
        return False
    omitted = {"response", "content", "text", "text_delta", "delta", "output"}
    def without_content(value):
        if isinstance(value, dict):
            return {key: without_content(child) for key, child in value.items()
                    if str(key).lower() not in omitted}
        if isinstance(value, list):
            return [without_content(child) for child in value]
        return value
    return bool(SIGN_IN.search(json.dumps(without_content(event), ensure_ascii=False)))


class StdoutGuard:
    """Reassemble stream-JSON lines before inspecting auth or tool-control fields."""
    def __init__(self, audio: Path):
        self.audio = audio
        self.pending = bytearray()

    def _line(self, raw: bytes) -> tuple[bool, bool]:
        text = raw.decode("utf-8", errors="replace")
        try:
            event = json.loads(text)
        except (ValueError, UnicodeError):
            # Never inspect a JSON-looking result fragment before it can be classified.
            return plain_requests_sign_in(text), False
        if not isinstance(event, dict):
            return False, False
        return event_requests_sign_in(event), forbidden_tool_event(event, self.audio)

    def feed(self, chunk: bytes, final: bool = False) -> tuple[bool, bool]:
        self.pending.extend(chunk)
        auth = unsafe = False
        while b"\n" in self.pending:
            raw, _, remaining = self.pending.partition(b"\n")
            self.pending = bytearray(remaining)
            found_auth, found_unsafe = self._line(bytes(raw))
            auth, unsafe = auth or found_auth, unsafe or found_unsafe
        if final and self.pending:
            found_auth, found_unsafe = self._line(bytes(self.pending))
            self.pending.clear()
            auth, unsafe = auth or found_auth, unsafe or found_unsafe
        elif self.pending:
            # A non-JSON CLI sign-in prompt may wait for input without ending its line.
            partial = bytes(self.pending).decode("utf-8", errors="replace")
            auth = auth or plain_requests_sign_in(partial)
        return auth, unsafe


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        process.kill()


def agy_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    installed = Path(local) / "agy" / "bin" / "agy.exe" if local else None
    if installed and installed.is_file():
        return installed
    raise RuntimeError("Antigravity CLI was not found. Install and sign in to it first.")


def forbidden_tool_event(event: dict, audio: Path) -> bool:
    """Whether one parsed event records any tool except view_file on the staged audio."""
    found = []
    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("tool_info"), dict):
                found.append(value["tool_info"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(event)
    expected = os.path.normcase(str(audio.resolve()))
    for tool in found:
        parameters = tool.get("parameters") if isinstance(tool, dict) else None
        path = parameters.get("AbsolutePath") if isinstance(parameters, dict) else None
        if tool.get("name") != "view_file" or not isinstance(path, str) or os.path.normcase(str(Path(path).resolve())) != expected:
            return True
    return False


def forbidden_tool(line: str, audio: Path) -> bool:
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        return False
    return isinstance(event, dict) and forbidden_tool_event(event, audio)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--cancel", required=True, type=Path)
    args = parser.parse_args()
    process = None
    result = {"status": "error", "message": "Antigravity worker did not finish."}
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        work = Path(request["work_dir"]).resolve()
        audio = (work / request["audio_name"]).resolve()
        if audio.parent != work or not audio.is_file() or set(work.iterdir()) != {audio}:
            raise RuntimeError("The Antigravity audio folder must contain only its one local audio file.")
        prompt = str(request["prompt"])
        command = [str(agy_path()), "--input-format", "stream-json", "--output-format", "stream-json",
                   "--disable-slash-commands"]
        model = str(request.get("model") or "")
        if model:
            command += ["--model", model]
        environment = {key: value for key, value in os.environ.items() if key not in REMOVED_ENV}
        environment["PYTHONIOENCODING"] = "utf-8"
        stdin = (json.dumps({"event": "user", "message": {"content": prompt}}) + "\n").encode("ascii")
        stdout_path, stderr_path = args.result.parent / "agy.stdout", args.result.parent / "agy.stderr"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(command, cwd=work, env=environment, stdin=subprocess.PIPE,
                                       stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
            assert process.stdin is not None
            process.stdin.write(stdin)
            process.stdin.close()
            stdout_at = stderr_at = 0
            stdout_guard = StdoutGuard(audio)
            stderr_sign_tail = ""
            signed_in = False
            while process.poll() is None:
                if args.cancel.exists():
                    terminate(process)
                    result = {"status": "canceled", "pid": process.pid}
                    break
                size = stdout.tell() + stderr.tell()
                if size > 8_000_000:
                    terminate(process)
                    result = {"status": "error", "message": "Antigravity response exceeded the 8 MB limit.", "pid": process.pid}
                    break
                stdout.flush(); stderr.flush()
                with stdout_path.open("rb") as stream:
                    stream.seek(stdout_at); fresh_out = stream.read(); stdout_at += len(fresh_out)
                with stderr_path.open("rb") as stream:
                    stream.seek(stderr_at); fresh_err = stream.read(); stderr_at += len(fresh_err)
                found_out, unsafe = stdout_guard.feed(fresh_out)
                found_err, stderr_sign_tail = sign_in_chunk(stderr_sign_tail, fresh_err)
                if found_out or found_err:
                    signed_in = True
                    terminate(process)
                    result = {"status": "sign_in", "pid": process.pid}
                    break
                if unsafe:
                    terminate(process)
                    result = {"status": "unsafe_tools", "pid": process.pid}
                    break
                time.sleep(.2)
            else:
                stdout.flush(); stderr.flush()
                final_out = stdout_path.read_bytes()[stdout_at:]
                final_err = stderr_path.read_bytes()[stderr_at:]
                found_out, unsafe = stdout_guard.feed(final_out, final=True)
                found_err, stderr_sign_tail = sign_in_chunk(stderr_sign_tail, final_err)
                if found_out or found_err:
                    result = {"status": "sign_in", "pid": process.pid}
                elif unsafe:
                    result = {"status": "unsafe_tools", "pid": process.pid}
                else:
                    result = {"status": "finished", "returncode": process.returncode, "pid": process.pid,
                              "stdout": str(stdout_path), "stderr": str(stderr_path), "signed_in": signed_in}
        if result["status"] == "finished" and set(work.iterdir()) != {audio}:
            result = {"status": "error", "message": "Antigravity left an unexpected file beside the isolated audio.",
                      "pid": result["pid"]}
        if result["status"] == "finished" and args.cancel.exists():
            result = {"status": "canceled", "pid": result["pid"]}
    except Exception as error:
        result = {"status": "error", "message": str(error)[:1200] or type(error).__name__}
    finally:
        if process is not None and process.poll() is None:
            terminate(process)
        atomic_json(args.result, result)
    return 0 if result["status"] == "finished" else 1


if __name__ == "__main__":
    raise SystemExit(main())
