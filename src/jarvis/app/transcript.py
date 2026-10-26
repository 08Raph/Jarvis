"""What the conversation view shows. Every entry has a KIND so the window can style user text, assistant text, tool actions, errors and
confirmations differently. Entries are created only from real events (a typed message, a model reply, a tool result, an error, a confirmation
and its answer); nothing is invented to make the screen look busy."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

KINDS = ("user", "assistant", "tool", "error", "confirm", "system")
LABEL = {"user": "You", "assistant": "Jarvis", "tool": "Action", "error": "Problem", "confirm": "Needs your approval", "system": "Info"}


@dataclass
class Entry:
    kind: str
    text: str
    at: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)
    id: int = 0

    def label(self) -> str:
        return LABEL[self.kind]

    def stamp(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))


class Transcript:
    """Bounded in-memory list. It is the VIEW's record only; conversation memory lives in the assistant and personal memory as before."""

    def __init__(self, limit: int = 500):
        self.limit = limit
        self.entries: list[Entry] = []
        self._next = 1

    def add(self, kind: str, text: str, **meta) -> Entry:
        if kind not in KINDS:
            raise ValueError(f"unknown entry kind {kind!r}")
        e = Entry(kind, str(text), meta=meta, id=self._next)
        self._next += 1
        self.entries.append(e)
        if len(self.entries) > self.limit:
            del self.entries[: len(self.entries) - self.limit]
        return e

    def append_to(self, entry_id: int, more: str) -> Entry | None:
        """Streaming: a reply grows as the model produces text."""
        for e in reversed(self.entries):
            if e.id == entry_id:
                e.text += more
                return e
        return None

    def clear(self) -> None:
        self.entries.clear()

    def to_plain(self) -> str:
        return "\n".join(f"[{e.stamp()}] {e.label()}: {e.text}" for e in self.entries)
