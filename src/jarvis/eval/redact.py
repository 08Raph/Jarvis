"""Path redaction for benchmark output (JSON and printed summaries).

Benchmark files are meant to be shared. Exception messages and runtime/DLL errors can carry a local path such as
``C:\\Users\\Jane Doe\\AppData\\Local\\Temp\\...``; that exposes the Windows account name. ``redact_paths`` replaces the
account-name component (and this machine's own home directory) and leaves everything else - the exception type, the file
names, the message - so errors can still be classified. Best effort: a path written some unusual way is not recognised, so
read a result file before sharing it."""
from __future__ import annotations

import re
from pathlib import Path

USER_TOKEN = "<user>"
_PATTERNS = (
    # C:\Users\Jane Doe\...  C:/Users/jane/...  C:\Documents and Settings\jane\...   (a name may contain spaces; it ends at the next separator)
    re.compile(r"(?i)\b([A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+)[^\\/\r\n\"'<>|:*?]+"),
    # \\?\C:\Users\jane\...  (extended-length form)
    re.compile(r"(?i)(\\\\\?\\[A-Z]:\\(?:Users|Documents and Settings)\\)[^\\/\r\n\"'<>|:*?]+"),
    # POSIX: /home/jane/... /Users/jane/...
    re.compile(r"((?<![A-Za-z0-9_.-])/(?:home|Users)/)[^/\s\"'<>|:*?]+"),
    # %USERPROFILE% already expanded into a roaming profile share, e.g. \\server\profiles\jane is NOT recognised (documented limit)
)


def _home_variants() -> list[str]:
    try:
        h = str(Path.home())
    except Exception:  # noqa: BLE001 - no home directory is not an error here
        return []
    out = {h, h.replace("\\", "/"), h.replace("/", "\\")}
    return [v for v in out if len(v) > 3]


def redact_paths(text: str) -> str:
    t = str(text)
    for p in _PATTERNS:
        t = p.sub(lambda m: m.group(1) + USER_TOKEN, t)
    for h in _home_variants():                   # anything under this machine's own home directory, however it is spelled
        if h in t:
            t = t.replace(h, "<home>")
    return t


def redact_obj(obj):
    """Return a copy of a JSON-like structure with every string passed through redact_paths."""
    if isinstance(obj, str):
        return redact_paths(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def safe_error(exc: BaseException) -> str:
    """``ExceptionType: message`` with prompt/conversation text withheld (disclosure.scrub_error_text) and the user's name removed from paths.
    The exception type is always kept, so failures can still be classified."""
    from jarvis import disclosure
    return redact_paths(f"{type(exc).__name__}: {disclosure.scrub_error_text(str(exc))}")
