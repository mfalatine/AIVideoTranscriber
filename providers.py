"""Documented transcript services: Gemini listens to the video, two caption APIs copy captions. All times in the normalized form are seconds."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import app_version

PROVIDERS = ("gemini", "supadata", "fetchtranscript", "antigravity")
KEYED_PROVIDERS = ("gemini", "supadata", "fetchtranscript")
LABELS = {"gemini": "Gemini", "supadata": "Supadata", "fetchtranscript": "FetchTranscript",
          "antigravity": "Antigravity local audio", "whisper": "Whisper (this PC)", "import": "Manual import"}


class ProviderError(RuntimeError):
    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code, self.status = code, status


def video_id(value):
    value = str(value).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        raise ValueError("Enter a YouTube video URL or 11-character video ID.")
    host = (parsed.hostname or "").lower()
    parts = parsed.path.strip("/").split("/")
    result = ""
    if host in ("youtu.be", "www.youtu.be"):
        result = parts[0]
    elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
        if parsed.path.rstrip("/") == "/watch":
            result = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) == 2 and parts[0] in ("shorts", "live", "embed"):
            result = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", result):
        raise ValueError("Use individual YouTube video links. Playlist expansion is not included yet.")
    return result


def stamp(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def normalize(provider, payload, vid):
    if not isinstance(payload, dict):
        raise ProviderError("invalid_response", "The service returned an unexpected response.")
    if provider == "fetchtranscript" and payload.get("video_id", vid) != vid:
        raise ProviderError("wrong_video", "The service returned a transcript for a different video.")
    chunks = payload.get("content" if provider == "supadata" else "segments")
    if not isinstance(chunks, list) or not chunks:
        raise ProviderError("no_transcript", "No timestamped transcript was returned.")
    segments = []
    divisor = 1000 if provider == "supadata" else 1
    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str):
            raise ProviderError("invalid_response", "The service returned a malformed transcript segment.")
        try:
            start = float(chunk["offset" if provider == "supadata" else "start"]) / divisor
            duration = float(chunk.get("duration", 0)) / divisor
        except (KeyError, TypeError, ValueError):
            raise ProviderError("invalid_response", "A transcript segment has invalid timing.") from None
        if not math.isfinite(start) or not math.isfinite(duration) or min(start, duration) < 0:
            raise ProviderError("invalid_response", "A transcript segment has invalid timing.")
        segments.append({"text": chunk["text"], "start": start, "duration": duration})
    text = "\n".join(s["text"] for s in segments)
    if not text.strip():
        raise ProviderError("no_transcript", "The transcript contains no speech text.")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    language = str(payload.get("lang" if provider == "supadata" else "language", "unknown"))
    source = {
        "provider": provider, "video_id": vid, "language": language,
        "title": str(metadata.get("title") or vid),
        "segments": segments, "text": text,
        "timed_text": "\n".join(f"[{stamp(s['start'])}] {s['text']}" for s in segments),
        "generated_captions": payload.get("is_generated"),
        "raw_response": payload, "characters": len(text), "words": len(text.split()),
        "last_timestamp": max(s["start"] + s["duration"] for s in segments),
    }
    source["hash"] = hashlib.sha256(json.dumps(
        {"segments": segments, "language": language, "video_id": vid},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return source


def fetch(provider, vid, key, language="en", opener=None, **options):
    """Gemini and Antigravity consume their own options; the caption services ignore them."""
    if provider not in PROVIDERS:
        raise ValueError("Unknown transcript service.")
    if provider == "antigravity":
        import antigravity_source
        try:
            arguments = {"model": options.get("antigravity_model", ""),
                         "cancel": options.get("cancel"), "progress": options.get("progress"),
                         "work_root": options.get("work_root")}
            if options.get("step_update") is not None:
                arguments["step_update"] = options["step_update"]
            return antigravity_source.fetch(vid, language, **arguments)
        except antigravity_source.SourceError as error:
            raise ProviderError(error.code, str(error)) from None
    if not key:
        raise ProviderError("missing_key", f"Add a {LABELS[provider]} API key in Settings.")
    if provider == "gemini":
        import gemini  # imported here because gemini.py itself imports this module
        return gemini.fetch(vid, key, language, opener=opener, **options)
    if provider == "supadata":
        query = urllib.parse.urlencode({"url": f"https://www.youtube.com/watch?v={vid}",
                                       "lang": language, "text": "false", "mode": "native"})
        url, headers = "https://api.supadata.ai/v1/transcript?" + query, {"x-api-key": key}
    else:
        query = urllib.parse.urlencode({"lang": language, "format": "json"})
        url = f"https://api.fetchtranscript.com/v1/transcripts/{vid}?{query}"
        headers = {"Authorization": "Bearer " + key}
    headers.update({"Accept": "application/json", "User-Agent": f"AIVideoTranscriber/{app_version.VERSION}"})
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with (opener or urllib.request.urlopen)(request, timeout=40) as response:
            status = response.status
            if status == 206:
                raise ProviderError("no_transcript", "This service has no existing captions for this video.", status)
            if status == 202:
                raise ProviderError("not_ready", "The service deferred processing. Retry later or use the fallback.", status)
            if status != 200:
                raise ProviderError("http_error", f"The service returned HTTP {status}.", status)
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise ProviderError("too_large", "The transcript response exceeds the 8 MB limit.")
            payload = json.loads(body)
            result = normalize(provider, payload, vid)
            result["elapsed_seconds"] = round(time.monotonic() - started, 2)
            result["credits_remaining"] = response.headers.get("X-Credits-Remaining")
            result["billable_credits"] = response.headers.get("x-billable-requests")
            return result
    except urllib.error.HTTPError as error:
        messages = {
            401: ("authentication", "API key rejected. Check this provider's key in Settings."),
            403: ("restricted", "The provider refused access to this video or account."),
            402: ("quota", "This provider has no available credits. No credits were purchased."),
            404: ("no_transcript", "The video or its transcript is unavailable from this service."),
            429: ("rate_limit", "The service reported a request or quota limit. Try later."),
        }
        code, message = messages.get(error.code, ("service_error", f"Service returned HTTP {error.code}."))
        raise ProviderError(code, message, error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError("connection", "Could not reach the service within 40 seconds.") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderError("invalid_response", "The service did not return valid JSON.") from None
