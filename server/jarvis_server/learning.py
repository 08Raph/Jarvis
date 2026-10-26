"""Opt-in learning data export. This does NOT change any model. It writes a JSONL file of conversations from users who
had learning ON when the message was sent and have not withdrawn it, with obvious personal data scrubbed. The file feeds
the project's OFFLINE training pipeline (provenance ledger, dedupe, decontamination) at a time the operator chooses."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from jarvis_server.db import DB

_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"https?://\S+"), "[link]"),
    (re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)"), "[number]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9_\-]{32,}\b"), "[secret]"),
    (re.compile(r"[A-Za-z]:\\[^\s]+"), "[path]"),
]


def scrub(text: str) -> str:
    for rx, rep in _PATTERNS:
        text = rx.sub(rep, text)
    return text


def export_training_data(db: DB, out_path: str | Path, mark_used: bool = True) -> int:
    rows = db.all(
        "SELECT m.id, m.conversation_id, m.role, m.content FROM messages m JOIN users u ON u.id = m.user_id "
        "WHERE m.learn_ok = 1 AND u.learning_opt_in = 1 AND u.learning_basis = 'explicit' "
        "AND m.created_at >= u.learning_consent_at AND m.used_in_export = 0 ORDER BY m.user_id, m.conversation_id, m.id")
    n, ids = 0, []
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            cid = hashlib.sha256(f"{r['conversation_id']}".encode()).hexdigest()[:16]  # no user id is written
            f.write(json.dumps({"conversation": cid, "role": r["role"], "text": scrub(r["content"])}, ensure_ascii=False) + "\n")
            ids.append(r["id"])
            n += 1
    if mark_used and ids:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            db.q(f"UPDATE messages SET used_in_export = 1 WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    return n
