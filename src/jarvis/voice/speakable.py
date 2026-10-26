"""Decide what may be spoken aloud. Internal logs, tracebacks, code, paths and secrets are never read out."""
from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LOGLINE = re.compile(r"^\s*(\[?(DEBUG|INFO|WARN(ING)?|ERROR|TRACE|CRITICAL)\]?|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|Traceback \(most recent|File \".*\", line \d+|at [\w.$]+\(.*\))", re.I)
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s]+|(?:/[\w.\-]+){2,})")
_URL = re.compile(r"https?://\S+")
_SECRET = re.compile(r"\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9_\-]{32,})\b")
_MD = re.compile(r"[*_`#>]+")


def speakable(text: str) -> str:
    """Strip everything that should not be read aloud; the on-screen text is unchanged by this."""
    t = _CODE_BLOCK.sub(" (code omitted) ", text)
    lines = [ln for ln in t.splitlines() if not _LOGLINE.match(ln)]
    t = " ".join(lines)
    t = _URL.sub(" a link ", t)
    t = _PATH.sub(" a file path ", t)
    t = _SECRET.sub(" a long identifier ", t)
    t = _MD.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def apply_pronunciations(text: str, mapping: dict[str, str]) -> str:
    for word, say in sorted(mapping.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{re.escape(word)}\b", say, text, flags=re.I)
    return text


def shorten(text: str, length: str) -> str:
    """Response length for speech: brief = first sentence, normal = first three, detailed = unchanged."""
    if length == "detailed":
        return text
    sents = re.split(r"(?<=[.!?])\s+", text)
    n = 1 if length == "brief" else 3
    out = " ".join(sents[:n])
    if len(sents) > n:
        out += " The full answer is on screen." if length == "brief" else ""
    return out
