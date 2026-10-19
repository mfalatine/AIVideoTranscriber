"""Per-user discovery for optional Python runtimes used by the application."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Callable


AGY_PYTHON_ENV = "AIVIDEOTRANSCRIBER_AGY_PYTHON"
YTDLP_PYTHON_ENV = "AIVIDEOTRANSCRIBER_YTDLP_PYTHON"
WHISPER_PYTHON_ENV = "AIVIDEOTRANSCRIBER_WHISPER_PYTHON"


def resolve_interpreter(environment_variable: str, discover: Callable[[], Path | None]) -> Path | None:
    """Prefer an explicit path, then an existing discovered executable; never run it to discover it."""
    configured = os.environ.get(environment_variable, "").strip()
    if configured:
        return Path(configured).expanduser()
    candidate = discover()
    return Path(candidate) if candidate is not None else None


def discover_download_python() -> Path | None:
    """Find the per-user Python installation used for the bundled yt-dlp integrations."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    root = Path(local_app_data) / "Programs" / "Python"
    preferred = root / "Python312" / "python.exe"
    if preferred.is_file():
        return preferred
    candidates = []
    for candidate in root.glob("Python*/python.exe"):
        match = re.fullmatch(r"Python(\d+)", candidate.parent.name)
        if match and candidate.is_file():
            candidates.append((int(match.group(1)), candidate))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def discover_whisper_python() -> Path | None:
    """Find the optional Whisper venv in its conventional per-user location."""
    profile = os.environ.get("USERPROFILE")
    root = Path(profile) if profile else Path.home()
    candidate = root / "AIVideoTranscriberWhisper" / "Scripts" / "python.exe"
    return candidate if candidate.is_file() else None


def missing_interpreter_message(environment_variable: str, purpose: str, path: Path | None = None) -> str:
    """Return an actionable, account-neutral setup message for an unavailable runtime."""
    detail = f" The configured path does not exist: {path}." if path is not None else ""
    return (f"{purpose} needs a Python interpreter that was not found.{detail} Set {environment_variable} "
            "to the full path of the Python executable for this feature, then restart the app.")


def missing_module_message(environment_variable: str, module: str, path: Path) -> str:
    return (f"{module} is not installed for this Python. Run \"{path} -m pip install {module}\", "
            f"or set {environment_variable} to a Python installation that has {module} installed.")
