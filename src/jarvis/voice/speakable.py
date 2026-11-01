"""Decide what may be spoken aloud. Internal logs, tracebacks, code, paths and secrets are never read out."""
from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_LOGLINE = re.compile(r"^\s*(\[?(DEBUG|INFO|WARN(ING)?|ERROR|TRACE|CRITICAL)\]?|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|Traceback \(most recent|File \".*\", line \d+|at [\w.$]+\(.*\))", re.I)
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s]+|(?:/[\w.\-]+){2,})")
_URL = re.compile(r"https?://\S+")
_SECRET = re.compile(r"\b(?:[A-Fa-f0-9]{32,}|[A-Za-z0-9_\-]{32,})\b")
_MD = re.compile(r"[*_`#>]+")
# the program adds labels such as "[Debate mode]" or "[Flirty roleplay]" to the TEXT on screen; they are never read aloud (spoken, they sounded like "roleplay, hello")
_LABEL = re.compile(r"\[[^\]\n]{2,90}\]")


_NUM_ITEM = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+(\S.*)$")
_BULLET_ITEM = re.compile(r"^\s*(?:[-*\u2022\u25cf]|\u2013)\s+(\S.*)$")


def _list_line(line: str) -> str:
    """A numbered line becomes "Step N: text." so it is spoken as a step. Without this "1." looked like the end of a sentence and the reply was cut after it (so Jarvis said
    step one and stopped); a bullet becomes a short sentence of its own, and every item ends with a full stop so the pause between items is natural."""
    m = _NUM_ITEM.match(line)
    if m:
        body = m.group(2).strip()
        return f"Step {m.group(1)}: {body}" + ("" if body[-1:] in ".!?" else ".")
    m = _BULLET_ITEM.match(line)
    if m:
        body = m.group(1).strip()
        return body + ("" if body[-1:] in ".!?:" else ".")
    return line.strip()


def speakable(text: str) -> str:
    """Strip everything that should not be read aloud; the on-screen text is unchanged by this."""
    t = _LABEL.sub(" ", text)
    t = _CODE_BLOCK.sub(" (code omitted) ", t)
    lines = [_list_line(ln) for ln in t.splitlines() if not _LOGLINE.match(ln)]
    t = " ".join(ln for ln in lines if ln)
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
    if length == "normal" and len(re.findall(r"\bStep \d{1,2}:", text)) >= 2:
        return " ".join(sents[:40])           # a list of steps is read to the end, never cut after the first few sentences
    n = 1 if length == "brief" else 3
    out = " ".join(sents[:n])
    if len(sents) > n:
        out += " The full answer is on screen." if length == "brief" else ""
    return out
