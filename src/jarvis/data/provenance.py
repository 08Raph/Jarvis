"""Training-data provenance ledger.

Every file that may enter training must be registered with an explicit license
from ALLOWED_LICENSES. The ledger records a content hash, a PII scan result,
and a status so that sources can be quarantined or removed on request.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_LICENSES = frozenset(
    {
        "CC0-1.0",
        "CC-BY-4.0",
        "CC-BY-SA-4.0",
        "MIT",
        "Apache-2.0",
        "BSD-3-Clause",
        "public-domain",
        "project-original",
    }
)

_PII_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ssn_like": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card_like": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "phone_like": re.compile(r"(?<!\d)\+?\d[\d\s().-]{8,}\d(?!\d)"),
}


class LicenseError(ValueError):
    pass


def scan_pii(text: str) -> dict[str, int]:
    return {k: len(p.findall(text)) for k, p in _PII_PATTERNS.items() if p.search(text)}


def redact_pii(text: str) -> str:
    for name, pat in _PII_PATTERNS.items():
        text = pat.sub(f"[{name.upper()}]", text)
    return text


class ProvenanceLedger:
    def __init__(self, db_path: str | Path) -> None:
        self._db = sqlite3.connect(str(db_path))
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS sources(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, origin TEXT NOT NULL, license TEXT NOT NULL,
                path TEXT NOT NULL, sha256 TEXT NOT NULL, n_bytes INTEGER NOT NULL,
                pii_hits TEXT NOT NULL, status TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL,
                removed_reason TEXT)"""
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def register(self, file_path: str | Path, name: str, origin: str, license: str, note: str = "") -> int:
        if license not in ALLOWED_LICENSES:
            raise LicenseError(f"license {license!r} is not in the allowed list")
        p = Path(file_path)
        raw = p.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if self._db.execute("SELECT 1 FROM sources WHERE sha256=? AND status='removed'", (digest,)).fetchone():
            raise ValueError("this exact content was removed from the dataset and cannot be re-registered")
        dup = self._db.execute(
            "SELECT id FROM sources WHERE sha256=? AND status!='removed'", (digest,)
        ).fetchone()
        if dup:
            return int(dup["id"])
        hits = scan_pii(raw.decode("utf-8", errors="replace"))
        status = "quarantined" if hits else "approved"
        cur = self._db.execute(
            "INSERT INTO sources(name,origin,license,path,sha256,n_bytes,pii_hits,status,note,added_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, origin, license, str(p.resolve()), digest, len(raw), json.dumps(hits), status, note,
             datetime.now(timezone.utc).isoformat()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def approve_quarantined(self, source_id: int) -> None:
        """Manual review step: only call after a human has checked the PII hits."""
        self._db.execute("UPDATE sources SET status='approved' WHERE id=? AND status='quarantined'", (source_id,))
        self._db.commit()

    def mark_removed(self, source_id: int, reason: str) -> None:
        self._db.execute("UPDATE sources SET status='removed', removed_reason=? WHERE id=?", (reason, source_id))
        self._db.commit()

    def approved(self) -> list[dict]:
        rows = self._db.execute("SELECT * FROM sources WHERE status='approved' ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def all(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM sources ORDER BY id").fetchall()]

    def export_manifest(self, out_path: str | Path) -> None:
        Path(out_path).write_text(json.dumps(self.all(), indent=2), encoding="utf-8")
