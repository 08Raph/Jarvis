"""Append-only audit log of every desktop action attempt (JSON lines, this PC only). File contents and typed text are not
recorded - only their length and a short hash - and secret-looking values are redacted (best effort)."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from jarvis.memory.secrets import redact

MAX_BYTES = 2_000_000
_SENSITIVE_ARGS = {"text", "content", "new_text", "old_text", "keys", "value"}      # "value" = what ui_act sets in a control (it can be a password)


def _digest(v: str) -> str:
    return f"<{len(v)} chars, sha256 {hashlib.sha256(v.encode('utf-8', 'replace')).hexdigest()[:12]}>"


def scrub_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str):
            out[k] = _digest(v) if k in _SENSITIVE_ARGS else redact(v)[:300]
        elif isinstance(v, list):
            out[k] = [redact(str(x))[:200] for x in v[:20]]
        else:
            out[k] = v
    return out


class AuditLog:
    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.memory: list[dict] = []          # also kept in RAM (guest mode keeps ONLY this)

    def write(self, **rec) -> dict:
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **rec}
        if "args" in rec:
            rec["args"] = scrub_args(rec["args"])
        for k in ("summary", "evidence", "error", "reason"):
            if isinstance(rec.get(k), str):
                rec[k] = redact(rec[k])[:500]
        self.memory.append(rec)
        if self.path is None:
            return rec
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_suffix(".1.jsonl"))
            except OSError:
                pass
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return rec

    def tail(self, n: int = 50) -> list[dict]:
        if self.path is None or not self.path.exists():
            return self.memory[-n:]
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out
