"""OpenCode CLI adapter: private loopback server, stdin prompts and bounded model discovery."""
from __future__ import annotations

import base64
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from opencode_process import start as _spawn_owned, stop as _stop_owned

ROOT = Path(__file__).resolve().parent
TEMP_ROOT = ROOT / "_TEMP"
DEFAULT_MODEL = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
MODEL_ID = re.compile(r"[A-Za-z0-9_.:/+~\-]{1,120}\Z")
SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]{1,120}\Z")
ANSI_CODES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
MODEL_CACHE_SECONDS = 600
MODEL_CATALOG_URL = "https://openrouter.ai/api/v1/models"
SAFE_FAILURE = ("OpenCode could not complete the request. Check provider access or choose another listed model in Settings.")
SAFE_TOOL_FAILURE = "OpenCode reported a tool event; the request was stopped and its session was cleaned up."
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:api[_ -]?key|access[_ -]?token|authorization|token|secret|password|passwd))\b"
    r"(\s*[:=]\s*)(?:(?:bearer|basic)\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_KEY_TOKEN = re.compile(r"(?i)\b(?:sk-(?:or-v1-)?|sk-ant\x2d|ghp\x5f|github_pat\x5f|xox[baprs]-|AI(?:za))[A-Za-z0-9_-]+\b")
_OPAQUE_SECRET = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9])")
_CACHE_LOCK = threading.RLock()
_DISCOVERY_LOCK = threading.Lock()
_MODEL_CACHE = {"at": 0.0, "executable": None, "models": [], "error": ""}


def executable():
    """PATH first, then the bundled OpenCode Desktop CLI."""
    found = shutil.which("opencode")
    candidates = [found] if found else []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.extend((str(Path(local) / "Programs" / "@opencodedesktop" / "resources" / "opencode-cli.exe"),
                           str(Path(local) / "@opencodedesktop" / "resources" / "opencode-cli.exe")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            path = str(Path(candidate).resolve())
            if Path(path).suffix.lower() in (".cmd", ".bat"):
                return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", path]
            return [path]
    raise RuntimeError("OpenCode CLI was not found. Install OpenCode Desktop or add opencode to PATH.")


def security_config(model=DEFAULT_MODEL):
    """Mirror the passed gate adapter: all tools denied and every agent pinned to the selected model."""
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ValueError("Select a valid OpenCode model.")
    deny = {"action": "*", "resource": "*", "effect": "deny"}
    config = {"model": model,
              "agents": {name: {"model": model} for name in
                         ("build", "plan", "general", "explore", "title", "summary", "compaction")},
              "snapshots": False, "formatter": False, "warming": False,
              "compaction": {"auto": False}, "update": "disable",
              "permissions": [deny]}
    config["agents"]["build"]["permissions"] = [dict(deny)]
    return config


def security_environment(tempdir, password, model=DEFAULT_MODEL):
    # Exclude by variable name first: values of inherited credentials are never read.
    secret_name = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|AUTHORIZATION|COOKIE", re.I)
    environment = {key: os.environ[key] for key in os.environ
                   if not secret_name.search(key) and not key.startswith("OPENCODE_")
                   and key not in ("CLAUDECODE", "NODE_OPTIONS", "BUN_OPTIONS")}
    environment.update(TMP=str(tempdir), TEMP=str(tempdir), TMPDIR=str(tempdir),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
                       OPENCODE_DISABLE_PROJECT_CONFIG="1", OPENCODE_CONFIG_PROJECT_DISABLE="1",
                       OPENCODE_DISABLE_AUTOUPDATE="1", OPENCODE_SERVER_USERNAME="opencode",
                       OPENCODE_SERVER_PASSWORD=password)
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(security_config(model), separators=(",", ":"))
    return environment


def stdin_payload(prompt):
    if not isinstance(prompt, str):
        raise ValueError("OpenCode prompt must be text.")
    payload = prompt.encode("utf-8")
    if payload and not payload.endswith(b"\n"):
        payload += b"\n"
    if not payload.strip() or len(payload) > 256_000:
        raise ValueError("OpenCode prompt must be between 1 and 256,000 UTF-8 bytes.")
    return payload


def _write_stdin(stream, payload, state):
    try:
        stream.write(payload)
        stream.flush()
        stream.close()  # EOF is part of the CLI input contract.
        state["delivered"] = True
    except (OSError, ValueError):
        state["delivered"] = False
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def command(executable_path, model, server, kind):
    if not isinstance(model, str) or not MODEL_ID.fullmatch(model):
        raise ValueError("Select a valid OpenCode model.")
    if kind not in ("formatted", "summary", "ask"):
        raise ValueError("Select a valid AI task.")
    if not isinstance(server, str) or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", server):
        raise ValueError("OpenCode must use its owned loopback server.")
    port = int(server.rsplit(":", 1)[1])
    if not 1 <= port <= 65535 or port in (49374, 8776):
        raise ValueError("OpenCode must use its owned loopback server.")
    return [*executable_path, "run", "--server", server, "--model", model,
            "--format", "json", "--agent", "build", "--title", "AI Video Transcriber " + kind]


def _event_rows(text):
    rows = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            rows.append(event)
    return rows


def _session_ids(events):
    found = set()
    for event in events:
        for source in (event, event.get("part"), event.get("session")):
            if not isinstance(source, dict):
                continue
            for key in ("sessionID", "session_id", "id"):
                value = source.get(key)
                if isinstance(value, str) and SESSION_ID.fullmatch(value):
                    found.add(value)
    return sorted(found)


def _model_value(value):
    if isinstance(value, dict):
        provider, identifier = value.get("providerID"), value.get("id", value.get("model", value.get("modelID")))
        if isinstance(provider, str) and isinstance(identifier, str):
            value = provider + "/" + identifier
    return value if isinstance(value, str) and MODEL_ID.fullmatch(value) else None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    if not finite or value < 0:
        return None
    return value


def _redact_diagnostic(value):
    text = ANSI_CODES.sub("", str(value or ""))
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    text = _BEARER_SECRET.sub("Bearer [REDACTED]", text)
    text = _KEY_TOKEN.sub("[REDACTED]", text)
    text = _OPAQUE_SECRET.sub("[REDACTED]", text)
    return "".join(char if char in "\r\n\t" or ord(char) >= 32 else " " for char in text)


def _sanitize_diagnostic(value, limit=300):
    return " ".join(_redact_diagnostic(value).split())[:limit]


def _error_event_diagnostic(events, prompt=""):
    for event in events:
        if event.get("type", event.get("event")) != "error":
            continue
        error = event.get("error")
        sources = [event]
        if isinstance(error, dict):
            sources.append(error)
            data = error.get("data")
            if isinstance(data, dict):
                sources.append(data)
        elif isinstance(error, str):
            sources.append({"message": error})
        status = name = provider = message = ""
        for source in sources:
            if not status:
                for key in ("status", "statusCode", "status_code", "httpStatus"):
                    value = source.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                        status = str(value)
                        break
                    if isinstance(value, str) and re.fullmatch(r"(?:HTTP\s*)?[1-5][0-9]{2}", value.strip(), re.I):
                        status = re.sub(r"(?i)^HTTP\s*", "", value.strip())
                        break
            if not name:
                name = next((str(source[key]).strip() for key in ("name", "type", "errorType")
                             if isinstance(source.get(key), str) and source[key].strip()
                             and not (key == "type" and source[key].strip().lower() == "error")), "")
            if not provider:
                provider = next((str(source[key]).strip() for key in ("provider", "providerID", "provider_id")
                                 if isinstance(source.get(key), str) and source[key].strip()), "")
            if not message:
                message = next((source[key].strip() for key in ("message", "detail")
                                if isinstance(source.get(key), str) and source[key].strip()), "")
        fields = [value for value in (status, provider, name) if value]
        if message:
            fields.append(message)
        if fields:
            prefix = " ".join(fields[:-1])
            diagnostic = (prefix + ": " if prefix and message else prefix) + (message or "")
            if prompt:
                diagnostic = diagnostic.replace(prompt, "[REDACTED PROMPT]")
            return _sanitize_diagnostic(diagnostic)
    return ""


def _provider_failure(diagnostic=""):
    safe = _sanitize_diagnostic(diagnostic)
    return SAFE_FAILURE + ("\nProvider: " + safe if safe else "")


def _stderr_diagnostic(path, prompt=""):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 4096))
            tail = stream.read(4096).decode("utf-8", errors="replace")
    except OSError:
        return ""
    cleaned = _redact_diagnostic(tail)
    if prompt:
        cleaned = cleaned.replace(prompt, "[REDACTED PROMPT]")
    lines = [" ".join(line.split()) for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    relevant = [line for line in lines if re.search(
        r"(?i)\b(?:[1-5][0-9]{2}|error|exception|denied|rate.?limit|quota|provider|failed)\b", line)]
    return _sanitize_diagnostic(" | ".join((relevant or lines)[-3:]))


def _remaining(deadline, timeout):
    if deadline is None:
        return 5.0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"OpenCode exceeded the {timeout}-second call limit.")
    return remaining


def _attach_cleanup(error, cleanup):
    try:
        error.opencode_cleanup = cleanup
    except Exception:
        pass
    return error


def parse_output(text, requested_model=DEFAULT_MODEL, *, verified_model=None, prompt=""):
    events = _event_rows(text)
    messages, actual_models, errors = [], [], False
    usage, costs = {}, []
    for event in events:
        kind = event.get("type", event.get("event"))
        part = event.get("part") if isinstance(event.get("part"), dict) else event
        if kind == "error":
            errors = True
        if (isinstance(kind, str) and "tool" in kind.lower()) or part.get("type") == "tool" \
                or any(key in source for source in (event, part) for key in ("tool", "tool_use", "tool_call")):
            raise RuntimeError(SAFE_TOOL_FAILURE)
        if kind == "text":
            value = part.get("text", event.get("text"))
            if isinstance(value, str):
                messages.append(value)
        for source in (event, part):
            actual = _model_value(source.get("model"))
            if "model" in source and actual is None:
                raise RuntimeError("OpenCode did not verify the requested model in its response.")
            if actual:
                actual_models.append(actual)
        tokens = part.get("tokens", event.get("tokens", {}))
        if isinstance(tokens, dict):
            mapping = {"input": "input_tokens", "output": "output_tokens", "reasoning": "reasoning_tokens",
                       "total": "total_tokens"}
            for key, target in mapping.items():
                number = _number(tokens.get(key))
                if number is not None:
                    usage[target] = usage.get(target, 0) + number
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                for key, target in (("read", "cache_read_tokens"), ("write", "cache_write_tokens")):
                    number = _number(cache.get(key))
                    if number is not None:
                        usage[target] = usage.get(target, 0) + number
        cost = _number(part.get("cost", event.get("cost")))
        if cost is not None:
            costs.append(cost)
    if errors:
        raise RuntimeError(_provider_failure(_error_event_diagnostic(events, prompt)))
    answer = "".join(messages).strip()
    if not answer:
        raise RuntimeError(SAFE_FAILURE)
    sessions = _session_ids(events)
    if verified_model is not None:
        actual_models.append(verified_model)
    actual = actual_models[-1] if actual_models else None
    if not actual_models or any(model != requested_model for model in actual_models):
        raise RuntimeError("OpenCode did not verify the requested model in its response.")
    receipt = {"session_id": sessions[-1] if len(sessions) == 1 else None,
               "session_ids": sessions, "requested_model": requested_model,
               "actual_model": actual, "model": [actual] if actual else [],
               "usage": usage}
    if costs:
        receipt["cost_usd"] = sum(costs)
    return answer, receipt


def _inventory(root):
    rows = []
    def fail(error):
        raise error
    def reparse(path):
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400) or path.is_symlink()
    if reparse(root):
        return ((".", "reparse"),)
    for folder, directories, files in os.walk(root, followlinks=False, onerror=fail):
        for name in list(directories):
            path = Path(folder) / name
            if reparse(path):
                directories.remove(name)
                rows.append((path.relative_to(root).as_posix(), "reparse"))
        for name in files:
            path = Path(folder) / name
            if reparse(path):
                rows.append((path.relative_to(root).as_posix(), "reparse"))
            else:
                data = path.read_bytes()
                rows.append((path.relative_to(root).as_posix(), len(data), hashlib.sha256(data).hexdigest()))
    return tuple(sorted(rows))


def _http_response(base, password, path, method="GET", timeout=5):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    token = base64.b64encode(("opencode:" + password).encode("utf-8")).decode("ascii")
    request = urllib.request.Request(base + path, method=method, headers={"Authorization": "Basic " + token})
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                raise RuntimeError("OpenCode private API response exceeded the output limit.")
            return response.status, data
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, b""


def _http_request(base, password, path, method="GET", timeout=5):
    return _http_response(base, password, path, method, timeout)[0]


def _session_model(base, password, session_id, requested_model, timeout=5):
    """Verify actual assistant metadata on this owned session before deleting it."""
    try:
        status, raw = _http_response(base, password, "/api/session/" + session_id + "/context", timeout=timeout)
        payload = json.loads(raw)
        messages = payload.get("data") if isinstance(payload, dict) else None
        if status != 200 or not isinstance(messages, list) or not messages:
            raise ValueError()
        assistants = [message for message in messages if isinstance(message, dict) and message.get("type") == "assistant"]
        if not assistants or not isinstance(messages[-1], dict) or messages[-1].get("type") != "idle" \
                or messages[-1].get("outcome") != "succeeded" or messages[-1].get("error"):
            raise ValueError()
        for message in assistants:
            if message.get("agent") != "build" or _model_value(message.get("model")) != requested_model or message.get("error"):
                raise ValueError()
            parts = message.get("content")
            if not isinstance(parts, list):
                raise ValueError()
            if any(isinstance(part, dict) and "tool" in str(part.get("type", "")).lower() for part in parts):
                raise RuntimeError(SAFE_TOOL_FAILURE)
        return requested_model
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        raise RuntimeError("OpenCode session model metadata could not be verified.") from None


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port in (49374, 8776):
        raise RuntimeError("Could not select a private OpenCode port.")
    return port


def _start_server(executable_path, environment, cwd, cancel, *, deadline=None, timeout=300, state=None):
    port = _free_port()
    args = [*executable_path, "serve", "--hostname", "127.0.0.1", "--port", str(port)]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        process = _spawn_owned(args, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
    except OSError:
        raise RuntimeError("OpenCode's private server could not be started.") from None
    if state is not None:
        state["process"] = process
    base = f"http://127.0.0.1:{port}"
    password = environment["OPENCODE_SERVER_PASSWORD"]
    location = urllib.parse.urlencode({"location[directory]": str(cwd.resolve())})
    try:
        while True:
            if cancel.is_set():
                raise InterruptedError("AI call canceled.")
            if process.poll() is not None:
                raise RuntimeError("OpenCode's private server exited before it was ready.")
            remaining = _remaining(deadline, timeout)
            try:
                status = _http_request(base, password, "/api/agent?" + location, timeout=min(5.0, remaining))
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                status = None
            _remaining(deadline, timeout)
            if status == 200:
                remaining = _remaining(deadline, timeout)
                try:
                    denied = _http_request(base, "invalid-private-probe", "/api/agent", timeout=min(5.0, remaining))
                except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                    _remaining(deadline, timeout)
                    raise RuntimeError("OpenCode's private server authentication could not be verified.") from None
                _remaining(deadline, timeout)
                if denied not in (401, 403):
                    raise RuntimeError("OpenCode's private server did not enforce authentication.")
                return process, base
            if status in (401, 403):
                raise RuntimeError("OpenCode's private server rejected its generated credentials.")
            cancel.wait(min(0.25, _remaining(deadline, timeout)))
    except BaseException:
        stopped = False
        result = None
        try:
            result = _stop_owned(process)
            stopped = result["stopped"]
        except Exception:
            stopped = False
        if state is not None:
            state["cleanup"] = result or {"stopped": False, "natural_exit": False}
        if not stopped:
            raise RuntimeError("OpenCode private-server process cleanup could not be verified.") from None
        raise


def _delete_sessions(base, password, session_ids):
    rows = []
    for session_id in session_ids:
        path = "/api/session/" + urllib.parse.quote(session_id, safe="")
        try:
            deleted = _http_request(base, password, path, "DELETE")
            verified = _http_request(base, password, path, "GET")
            rows.append({"id": session_id, "delete_status": deleted, "verify_status": verified,
                         "removed": deleted == 204 and verified == 404})
        except (OSError, ValueError, RuntimeError, urllib.error.URLError, TimeoutError):
            rows.append({"id": session_id, "removed": False})
    return rows


def invoke(prompt, model="", kind="formatted", cancel=None, timeout=300):
    """Run one model call in a fresh empty directory and clean its exact observed sessions."""
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise InterruptedError("Canceled before the AI call started.")
    selected_model = model or DEFAULT_MODEL
    if not MODEL_ID.fullmatch(selected_model):
        raise ValueError("Select a valid OpenCode model.")
    payload = stdin_payload(prompt)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    task_root = Path(tempfile.mkdtemp(prefix="opencode-invoke-", dir=TEMP_ROOT))
    cwd = task_root / "work"
    before = ()
    password = secrets.token_urlsafe(32)
    out_path, err_path = task_root / "stdout.bin", task_root / "stderr.bin"
    started = time.monotonic()
    deadline = started + timeout
    server_process = run_process = None
    writer = None
    stdin_state = {"delivered": False}
    base = None
    answer = receipt = None
    failure = None
    cleanup_verified = False
    server_state = {}
    server_cleanup = None
    try:
        cwd.mkdir()
        before = _inventory(cwd)
        if before:
            raise RuntimeError("OpenCode work folder must be empty before invocation.")
        environment = security_environment(task_root, password, selected_model)
        executable_path = executable()
        _remaining(deadline, timeout)
        server_process, base = _start_server(executable_path, environment, cwd, cancel,
                                             deadline=deadline, timeout=timeout, state=server_state)
        _remaining(deadline, timeout)
        args = command(executable_path, selected_model, base, kind)
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with out_path.open("w+b") as out, err_path.open("w+b") as err:
            try:
                run_process = _spawn_owned(args, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                              stdout=out, stderr=err, **options)
            except OSError:
                raise RuntimeError(SAFE_FAILURE) from None
            stream = run_process.stdin
            run_process.stdin = None  # communicate must not flush/close the writer's pipe.
            writer = threading.Thread(target=_write_stdin, args=(stream, payload, stdin_state), daemon=True)
            writer.start()
            while True:
                if cancel.is_set():
                    raise InterruptedError("AI call canceled.")
                if out.tell() + err.tell() > 8_000_000:
                    raise RuntimeError("OpenCode response exceeded the output limit.")
                try:
                    run_process.communicate(timeout=min(0.5, _remaining(deadline, timeout)))
                    break
                except subprocess.TimeoutExpired:
                    pass
            _remaining(deadline, timeout)
            out.seek(0)
            output = out.read(8_000_001)
            if len(output) > 8_000_000 or out_path.stat().st_size + err_path.stat().st_size > 8_000_000:
                raise RuntimeError("OpenCode response exceeded the output limit.")
            raw = output.decode("utf-8", errors="replace")
            event_diagnostic = _error_event_diagnostic(_event_rows(raw), prompt)
            stderr_diagnostic = _stderr_diagnostic(err_path, prompt)
            if run_process.returncode:
                raise RuntimeError(_provider_failure(event_diagnostic or stderr_diagnostic))
            if event_diagnostic:
                raise RuntimeError(_provider_failure(event_diagnostic))
            observed = _session_ids(_event_rows(raw))
            if len(observed) != 1:
                raise RuntimeError("OpenCode did not report one exact session ID; cleanup could not be verified.")
            # CLI JSON can omit model metadata. Use the owned session's actual assistant context.
            try:
                actual = _session_model(base, password, observed[0], selected_model,
                                        timeout=min(5.0, _remaining(deadline, timeout)))
            except RuntimeError:
                _remaining(deadline, timeout)
                raise
            _remaining(deadline, timeout)
            answer, receipt = parse_output(raw, selected_model, verified_model=actual, prompt=prompt)
            _remaining(deadline, timeout)
            receipt["model_metadata_source"] = "owned_session_assistant_context"
    except TimeoutError as error:
        failure = error
    except InterruptedError as error:
        failure = error
    except RuntimeError as error:
        failure = error
    except (OSError, subprocess.SubprocessError, ValueError):
        failure = RuntimeError(SAFE_FAILURE)
    finally:
        processes_stopped = True
        if run_process is not None:
            try:
                run_cleanup = _stop_owned(run_process)
                processes_stopped = run_cleanup["stopped"]
                if not run_cleanup["natural_exit"] and failure is None:
                    failure = RuntimeError("OpenCode left a surviving owned process; the request was rejected.")
            except Exception:
                processes_stopped = False
        if writer is not None:
            writer.join()  # Exact tree termination releases any blocked pipe writer.
            if not stdin_state["delivered"] and failure is None:
                failure = RuntimeError("OpenCode prompt delivery could not be verified.")
        try:
            with out_path.open("rb") as stream:
                raw = stream.read(8_000_001).decode("utf-8", errors="replace")
        except OSError:
            raw = ""
        sessions = _session_ids(_event_rows(raw))
        try:
            cleanup = _delete_sessions(base, password, sessions) if base else []
        except Exception:
            cleanup = [{"id": session_id, "removed": False} for session_id in sessions]
        if server_process is None:
            server_process = server_state.get("process")
        if server_process is not None:
            server_cleanup = server_state.get("cleanup")
            if server_cleanup is None:
                try:
                    server_cleanup = _stop_owned(server_process)
                    processes_stopped = server_cleanup["stopped"] and processes_stopped
                except Exception:
                    server_cleanup = {"stopped": False, "natural_exit": False}
                    processes_stopped = False
            else:
                processes_stopped = server_cleanup.get("stopped", False) and processes_stopped
        try:
            after = _inventory(cwd)
        except OSError:
            after = (("<inventory-unavailable>", "error"),)
        clean_workdir = before == after == ()
        temporary_folder_removed = False
        try:
            shutil.rmtree(task_root)
            temporary_folder_removed = not task_root.exists()
        except OSError:
            temporary_folder_removed = False
        cleanup_verified = all(row["removed"] for row in cleanup) and processes_stopped and temporary_folder_removed
        cleanup_report = {"sessions": cleanup, "verified": cleanup_verified,
                          "processes_stopped": processes_stopped, "workdir_empty": clean_workdir,
                          "run_process": locals().get("run_cleanup"), "server_process": server_cleanup,
                          "temporary_folder_removed": temporary_folder_removed}
        if receipt is not None:
            receipt.update(session_id=sessions[-1] if len(sessions) == 1 else None,
                           session_ids=sessions, cleanup=cleanup_report,
                           stdin_bytes=len(payload), stdin_sha256=hashlib.sha256(payload).hexdigest(),
                           stdin_delivered=stdin_state["delivered"])
    if not cleanup_verified:
        live = [str(process.pid) for process in (run_process, server_process)
                if process is not None and process.poll() is None]
        unremoved = [row["id"] for row in cleanup if not row.get("removed")]
        details = []
        if live:
            details.append("live owned PID(s): " + ", ".join(live))
        if unremoved:
            details.append("session(s): " + ", ".join(unremoved))
        if not temporary_folder_removed and task_root.exists():
            details.append("temporary folder: " + str(task_root))
        error = RuntimeError("OpenCode session or process cleanup could not be verified"
                             + (" (" + "; ".join(details) + ")" if details else "."))
        raise _attach_cleanup(error, cleanup_report)
    if failure is not None:
        raise _attach_cleanup(failure, cleanup_report)
    if not receipt["cleanup"]["workdir_empty"]:
        error = RuntimeError("OpenCode created files in its empty work folder; the request was rejected.")
        raise _attach_cleanup(error, cleanup_report)
    receipt.update(provider="opencode", requested_model=selected_model,
                   elapsed_seconds=round(time.monotonic() - started, 2))
    return answer, receipt


def _parse_cli_models(text):
    models, seen = [], set()
    for raw in ANSI_CODES.sub("", text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.fullmatch(r"(.+?)(?:\t+|\s{2,}| - )(.+)", line)
        first, label = (match.group(1).strip(), match.group(2).strip()) if match else (line, line)
        if MODEL_ID.fullmatch(first) and first not in seen:
            # Zen free IDs return provider 403 because they can only be used within OpenCode.
            if first.startswith("opencode/"):
                continue
            models.append({"id": first, "label": (label or first)[:200]})
            seen.add(first)
    return models


def _price(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and Decimal(0) <= result <= Decimal("1000000") else None


def _fetch_prices(opener=None):
    request = urllib.request.Request(MODEL_CATALOG_URL, headers={"Accept": "application/json"})
    try:
        response = (opener or urllib.request.urlopen)(request, timeout=20)
        with response:
            raw = response.read(16_000_001)
        if len(raw) > 16_000_000:
            return {}
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError, urllib.error.URLError, TimeoutError):
        return {}
    rows = payload.get("data") if isinstance(payload, dict) else None
    result = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            continue
        prompt, completion = _price(pricing.get("prompt")), _price(pricing.get("completion"))
        if prompt is not None and completion is not None:
            result[row["id"]] = (prompt, completion)
    return result


def _catalog_models(models, prices):
    result = []
    for item in models:
        identifier = item["id"]
        price_id = identifier[len("openrouter/"):] if identifier.startswith("openrouter/") else identifier
        price = prices.get(price_id) or prices.get(identifier)
        free_limited = identifier.startswith("opencode/")
        free_suffix = identifier.endswith(":free")
        if free_limited or free_suffix:
            price = (Decimal(0), Decimal(0))
        label = str(item.get("label") or identifier)
        if free_limited:
            label += " · Free (limited time)"
        elif free_suffix:
            label += " · Free"
        if price is not None:
            in_m, out_m = price[0] * 1_000_000, price[1] * 1_000_000
            if not free_limited and not free_suffix:
                label += f" · ${in_m:.2f}/M in · ${out_m:.2f}/M out"
        free = free_limited or free_suffix or (price is not None and price == (Decimal(0), Decimal(0)))
        cost = float(sum(price)) if price is not None else math.inf
        result.append({"id": identifier, "label": label, "free": free,
                       "input_price_per_million": float(price[0] * 1_000_000) if price is not None else None,
                       "output_price_per_million": float(price[1] * 1_000_000) if price is not None else None,
                       "_sort": (0 if identifier.startswith("openrouter/") and free_suffix else
                                 1 if free_limited else 2 if free else 3, cost, identifier)})
    result.sort(key=lambda model: model["_sort"])
    for model in result:
        model.pop("_sort", None)
    return result


def _run_models(executable_path, now):
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    task_root = Path(tempfile.mkdtemp(prefix="opencode-models-", dir=TEMP_ROOT))
    models, error = [], ""
    process = None
    try:
        cwd = task_root / "work"
        cwd.mkdir()
        password = secrets.token_urlsafe(32)
        environment = security_environment(task_root, password, DEFAULT_MODEL)
        stdout_path, stderr_path = task_root / "stdout.bin", task_root / "stderr.bin"
        args = [*executable_path, "models"]
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with stdout_path.open("w+b") as out, stderr_path.open("w+b") as err:
            try:
                process = _spawn_owned(args, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=out, stderr=err, **options)
                process.wait()
                if not _stop_owned(process)["natural_exit"]:
                    raise RuntimeError("OpenCode model discovery left a surviving owned process.")
                if _inventory(cwd):
                    raise RuntimeError("OpenCode model discovery changed its empty work folder.")
                out.seek(0)
                output = out.read(16_000_001)
            except (OSError, RuntimeError):
                error = "OpenCode could not list models."
                output = b""
            if len(output) > 16_000_000:
                error, output = "OpenCode returned an oversized model list.", b""
            elif not error and process.returncode:
                error = "OpenCode could not list models."
            elif not error:
                models = _parse_cli_models(output.decode("utf-8", errors="replace"))
                if not models:
                    error = "OpenCode returned no usable models."
        if models:
            models = _catalog_models(models, _fetch_prices())
    finally:
        if process is not None:
            try:
                if not _stop_owned(process)["stopped"]:
                    raise RuntimeError("cleanup_unverified")
            except Exception:
                error, models = "OpenCode model-list process cleanup could not be verified.", []
        try:
            shutil.rmtree(task_root)
        except OSError:
            error = f"OpenCode model-list temporary cleanup could not be verified: {task_root}"
            models = []
    return {"at": now, "executable": tuple(executable_path), "models": models, "error": error}


def model_list(*, refresh=False, clock=None):
    """Run hidden `opencode models` only on the explicit async API route; cache success and failure for ten minutes."""
    clock = clock or time.monotonic
    # Only catalog requests serialize. Page loads and validation never wait for I/O.
    with _DISCOVERY_LOCK:
        try:
            args = executable()
        except RuntimeError:
            args = None
        moment = clock()
        key = tuple(args) if args else None
        with _CACHE_LOCK:
            if not refresh and _MODEL_CACHE["at"] > 0 and _MODEL_CACHE["executable"] == key \
                    and moment - _MODEL_CACHE["at"] < MODEL_CACHE_SECONDS:
                return {"models": [dict(model) for model in _MODEL_CACHE["models"]], "error": _MODEL_CACHE["error"]}
        try:
            if args is None:
                result = {"executable": None, "models": [], "error": "OpenCode CLI was not found."}
            else:
                result = _run_models(args, moment)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
            result = {"executable": key, "models": [],
                      "error": "OpenCode could not list models."}
        result["at"] = clock()
        with _CACHE_LOCK:
            _MODEL_CACHE.update(result)
        return {"models": [dict(model) for model in result["models"]], "error": result["error"]}


def cached_models():
    with _CACHE_LOCK:
        if time.monotonic() - _MODEL_CACHE["at"] >= MODEL_CACHE_SECONDS:
            return []
        return [dict(model) for model in _MODEL_CACHE["models"]]


def model_is_listed(identifier):
    with _CACHE_LOCK:
        if time.monotonic() - _MODEL_CACHE["at"] >= MODEL_CACHE_SECONDS:
            return False
        return isinstance(identifier, str) and any(model.get("id") == identifier for model in _MODEL_CACHE["models"])


def info(home=None):
    # A fixture home is never permission to spawn a real CLI. Page loads read only this process cache.
    return {"default_model": DEFAULT_MODEL, "default_effort": None,
            "models": cached_models() if home is None else [], "efforts": []}


def reset_model_cache():
    with _CACHE_LOCK:
        _MODEL_CACHE.update(at=0.0, executable=None, models=[], error="")
