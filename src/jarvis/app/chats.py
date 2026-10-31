"""Saved chats. Every conversation is stored on this PC by itself (one small JSON file per chat in <profile>/chats), so starting a new chat never loses the old one and any
earlier chat can be opened again. Nothing leaves the PC. A guest session saves nothing. Bounded: at most MAX_CHATS chats (the oldest are removed), MAX_ENTRIES messages per chat.

What is kept is only what the window shows: the typed or spoken words and Jarvis's replies (plus short notices). No audio, no files, no secrets are added to it."""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

MAX_CHATS = 200
MAX_ENTRIES = 500
MAX_TEXT = 20_000
KEEP_KINDS = ("user", "assistant", "system", "error")
_ID = re.compile(r"^[0-9a-f]{12}$")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def title_for(entries: list[dict]) -> str:
    for e in entries:
        if e.get("kind") == "user":
            t = re.sub(r"^\(voice\)\s*", "", str(e.get("text", ""))).strip()
            t = re.sub(r"\s+", " ", t)
            if t:
                return t[:48] + ("..." if len(t) > 48 else "")
    return "New chat"


class ChatStore:
    def __init__(self, folder: Path, guest: bool = False):
        self.folder, self.guest = Path(folder), guest

    def _path(self, cid: str) -> Path:
        if not _ID.match(str(cid)):
            raise ValueError("not a chat id")
        return self.folder / f"{cid}.json"

    def store(self, cid: str, entries: list, title: str | None = None, created: float | None = None) -> bool:
        """Write the chat (atomically). entries: objects with kind/text/at (the transcript's entries) or dicts. Nothing is written for a guest or an empty chat."""
        if self.guest:
            return False
        rows = []
        for e in entries:
            d = e if isinstance(e, dict) else {"kind": e.kind, "text": e.text, "at": e.at}
            if d.get("kind") in KEEP_KINDS and str(d.get("text", "")).strip():
                rows.append({"kind": d["kind"], "text": str(d["text"])[:MAX_TEXT], "at": float(d.get("at") or time.time())})
        rows = rows[-MAX_ENTRIES:]
        if not any(r["kind"] in ("user", "assistant") for r in rows):
            return False
        p = self._path(cid)
        old = self._read(p)
        doc = {"version": 1, "id": cid, "title": (title or (old or {}).get("title_custom") or title_for(rows)), "title_custom": (title or (old or {}).get("title_custom") or ""),
               "created": float(created or (old or {}).get("created") or rows[0]["at"]), "updated": time.time(), "entries": rows}
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except OSError:
            return False
        self._trim()
        return True

    @staticmethod
    def _read(p: Path) -> dict | None:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and isinstance(d.get("entries"), list) else None

    def load(self, cid: str) -> dict | None:
        return self._read(self._path(cid))

    def list(self) -> list[dict]:
        """Newest first: id, title, updated, message count."""
        out = []
        try:
            files = list(self.folder.glob("*.json"))
        except OSError:
            return []
        for p in files:
            if not _ID.match(p.stem):
                continue
            d = self._read(p)
            if d:
                out.append({"id": p.stem, "title": str(d.get("title") or "New chat")[:80], "updated": float(d.get("updated") or 0), "created": float(d.get("created") or 0),
                            "messages": sum(1 for e in d["entries"] if e.get("kind") in ("user", "assistant"))})
        return sorted(out, key=lambda r: -r["updated"])

    def delete(self, cid: str) -> bool:
        try:
            self._path(cid).unlink()
            return True
        except (OSError, ValueError):
            return False

    def rename(self, cid: str, title: str) -> bool:
        d = self.load(cid)
        title = re.sub(r"\s+", " ", str(title)).strip()[:80]
        if not d or not title or self.guest:
            return False
        d["title"], d["title_custom"] = title, title
        try:
            tmp = self._path(cid).with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path(cid))
            return True
        except OSError:
            return False

    def _trim(self) -> None:
        rows = self.list()
        for r in rows[MAX_CHATS:]:
            self.delete(r["id"])

    @staticmethod
    def history_from(entries: list[dict], limit: int = 40) -> list[dict]:
        """The language model's conversation memory rebuilt from a saved chat, so a re-opened chat can be continued."""
        out = []
        for e in entries:
            if e.get("kind") == "user":
                out.append({"role": "user", "content": re.sub(r"^\(voice\)\s*", "", str(e.get("text", "")))})
            elif e.get("kind") == "assistant" and str(e.get("text", "")).strip():
                out.append({"role": "assistant", "content": str(e["text"])})
        return out[-limit:]
