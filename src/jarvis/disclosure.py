"""Application-level limits on disclosing Jarvis's own instructions and saved memory (Stage 5 security review).

What this is: two cheap, deterministic, model-independent checks.
  * is_extraction_request(text): the user's message asks to print/reveal the system prompt, instructions, configuration or saved memory.
    For such a turn the saved-memory block is not put in front of the model at all, so even a model that obeys cannot repeat it.
  * leaks_instructions(reply, protected): the reply repeats a long verbatim run of the instruction text it was given.
    Such a reply is withheld and replaced by a short refusal.
What this is NOT: a proof of injection resistance. Both checks are heuristics. A paraphrase, a translation, a spelled-out or encoded copy,
or a leak of a saved note through an ordinary-looking answer is not caught. Whether a real model leaks or obeys injected text can only be
measured with a real model (intel-v1 cases inj1-inj4); passing the unit tests for this module says nothing about that.
Nothing here grants or removes any tool permission: tool authorisation, schema validation and confirmation prompts are decided by the executor
from the parsed call and the user's own answers, never from what the model says about itself.
"""
from __future__ import annotations

import re

LEAK_WINDOW_WORDS = 12          # a reply containing 12 consecutive words of the protected text is treated as a copy
REFUSAL = ("I can't share my internal instructions or configuration. I'm happy to help with what you actually need - "
           "ask me anything, or check Settings to see and manage what I have saved.")

_WORD = re.compile(r"[a-z0-9']+")
_REVEAL = r"(?:print|show|reveal|repeat|output|display|dump|leak|recite|copy|paste|quote|list|read\s+out|tell\s+me|give\s+me|what(?:'s|\s+is|\s+are)|disclose|expose|write\s+out)"
_THING = (r"(?:system\s*(?:prompt|message|instructions?)|(?:initial|hidden|secret|internal|original|developer|base)\s+(?:prompt|instructions?|message|rules)|"
          r"your\s+(?:instructions|rules|prompt|guidelines|configuration|config|settings)|(?:everything|all\s+text|the\s+text)\s+(?:above|before)|"
          r"saved\s+memor(?:y|ies)|memory\s+(?:notes|block|contents?)|(?:all\s+)?(?:the\s+)?(?:notes|things)\s+you\s+(?:have\s+)?(?:saved|remember)|"
          r"api\s*keys?|passwords?|credentials|environment\s+variables|config(?:uration)?\s+files?)")
_EXTRACT = re.compile(rf"(?is)\b{_REVEAL}\b[^.\n]{{0,60}}?\b{_THING}")
_IGNORE_RULES = re.compile(r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|above|prior|earlier|all|your|these)\b[^.\n]{0,30}\b(?:instructions?|rules|prompt|guidelines)\b")


def is_extraction_request(text: str) -> bool:
    t = text or ""
    return bool(_EXTRACT.search(t) or _IGNORE_RULES.search(t))


QUERY_HEAD_CHARS, QUERY_TAIL_CHARS = 250, 150
LONG_MESSAGE_CHARS = 600         # a message this long almost certainly contains pasted material (document, email, web page)
_NOTE_INJECTION = re.compile(r"(?i)<\s*/?\s*(?:tool_call|tool_response|untrusted_data|think|system)\b|\byou are now\b|\bnew instructions?\b|\bsystem prompt\b")


def retrieval_query(text: str) -> str:
    """The part of a user message that may be used to look things up in saved memory: the first paragraph (up to 250 characters) and, when the
    message has several paragraphs, the last one (up to 150). Pasted material sits in between, so a document cannot widen the lookup by stuffing
    words into its body. Heuristic: a document whose payload sits in its first or last paragraph still influences the lookup."""
    t = (text or "").strip()
    if len(t) <= QUERY_HEAD_CHARS + QUERY_TAIL_CHARS:
        return t
    paras = [p.strip() for p in re.split(r"\n\s*\n", t) if p.strip()]
    if len(paras) >= 2:
        return paras[0][:QUERY_HEAD_CHARS] + " " + paras[-1][-QUERY_TAIL_CHARS:]
    return t[:QUERY_HEAD_CHARS] + " " + t[-QUERY_TAIL_CHARS:]


def is_long_message(text: str) -> bool:
    return len(text or "") > LONG_MESSAGE_CHARS


_DOC_FENCE = re.compile(r"```|<<<|^-{3,}\s*$", re.M)
_DOC_QUOTE = re.compile(r"^\s*>", re.M)
_DOC_HEADER = re.compile(r"^(?:from|to|subject|date|cc|sent):\s*\S", re.I | re.M)


def is_document_like(text: str) -> bool:
    """Is this turn probably carrying pasted material (a document, email, web page, log) rather than a personal request?
    The chat path has no attachment channel - a document arrives as ordinary user text - so this is a structural guess: a long
    message, a fenced/quoted block, or email-style headers. It is deliberately biased towards 'yes': a wrong 'yes' only means
    saved NOTES are left out of that one turn (standing preferences still apply); a wrong 'no' is the old behaviour."""
    t = text or ""
    return (len(t) > LONG_MESSAGE_CHARS or bool(_DOC_FENCE.search(t)) or len(_DOC_QUOTE.findall(t)) >= 3
            or len(_DOC_HEADER.findall(t)) >= 2)


def looks_like_instruction(note: str) -> bool:
    """A saved note that reads as an attempt to give the model commands (override rules, forge tool/system tags). Such a note is not shown to the
    model; it is NOT deleted or changed."""
    return bool(_IGNORE_RULES.search(note or "") or _NOTE_INJECTION.search(note or ""))


def neutralise_note(note: str) -> str:
    """Saved text may not forge a tag the prompt or the tool protocol uses."""
    return (note or "").replace("<", "\u2039").replace(">", "\u203a")


def _words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def leaks_instructions(reply: str, protected: list[str], window: int = LEAK_WINDOW_WORDS) -> bool:
    rw = _words(reply)
    if len(rw) < window:
        return False
    grams: set[tuple[str, ...]] = set()
    for p in protected:
        pw = _words(p)
        for i in range(len(pw) - window + 1):
            grams.add(tuple(pw[i:i + window]))
    return any(tuple(rw[i:i + window]) in grams for i in range(len(rw) - window + 1))


class StreamGuard:
    """Holds back the last (window-1) words of a streamed reply so a copied run of the protected text is detected before its
    final words are shown; on a detected copy everything not yet shown is replaced by the refusal."""

    def __init__(self, protected: list[str], window: int = LEAK_WINDOW_WORDS):
        self.protected, self.window = protected, window
        self.full, self.shown, self.tripped = "", 0, False

    def feed(self, piece: str) -> str:
        if self.tripped:
            return ""
        self.full += piece
        if leaks_instructions(self.full, self.protected, self.window):
            self.tripped = True
            return "\n" + REFUSAL
        starts = [m.start() for m in _WORD.finditer(self.full.lower())]
        cut = starts[-(self.window - 1)] if len(starts) >= self.window else 0     # start of the held-back tail
        out = self.full[self.shown:cut] if cut > self.shown else ""
        self.shown = max(self.shown, cut)
        return out

    def flush(self) -> str:
        if self.tripped:
            return ""
        if leaks_instructions(self.full, self.protected, self.window):
            self.tripped = True
            return "\n" + REFUSAL
        out = self.full[self.shown:]
        self.shown = len(self.full)
        return out


# --- error text -------------------------------------------------------------------------------------------------
_PAYLOAD_START = re.compile(r"""[=:]\s*["']""")
ERROR_LIMIT = 300


def scrub_error_text(text: str, limit: int = ERROR_LIMIT) -> str:
    """Make an exception message safe to print, log or put in a shareable report.

    Some libraries (llama-cpp-python: ``Failed to tokenize: text="<whole prompt>"``) copy the text they were given into
    the exception message, and that text is the system prompt, saved-memory notes and the conversation.  Everything after
    an opening quote that follows ``=`` or ``:`` is dropped when more than 40 characters follow it, and the message is
    length-capped.  Short ordinary messages are returned unchanged.  Best effort: a library that embeds text some other
    way is only bounded by the length cap."""
    t = " ".join(str(text or "").split())
    m = _PAYLOAD_START.search(t)
    if m and len(t) - m.end() > 40:
        t = t[:m.end()] + "<text withheld>"
    return t if len(t) <= limit else t[:limit] + "..."


def safe_exception_text(exc: BaseException, formatted: str) -> str:
    """Replace every long message in an exception chain inside an already formatted traceback with its scrubbed form."""
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        msg = str(e)
        if len(msg) > 40 and msg in formatted:
            formatted = formatted.replace(msg, scrub_error_text(msg))
        stack += [e.__cause__, e.__context__]
    return formatted
