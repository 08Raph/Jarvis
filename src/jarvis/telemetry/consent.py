"""Consent record for telemetry. Off until the user types an agreement after reading the notice. Reversible any time."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jarvis.telemetry.schema import SCHEMA_VERSION, categories

NOTICE_VERSION = 1      # bump whenever schema.py gains an event/field: everyone must agree again


def notice_text() -> str:
    c = categories()
    diag = "\n".join("      - " + x for x in c["diagnostics"])
    usage = "\n".join("      - " + x for x in c["usage"])
    return (
        f"OPTIONAL: HELP IMPROVE PROJECT JARVIS (notice version {NOTICE_VERSION})\n\n"
        "Project JARVIS runs its AI on your PC. If - and only if - you agree, it can send small, anonymous reports to the\n"
        "developer's server. Nothing is sent unless you agree, and you can stop at any time.\n\n"
        "WHAT CAN BE SENT (exactly this list; `jarvis telemetry schema` shows it again, and `jarvis telemetry preview` shows the\n"
        "real queued data before anything is sent):\n"
        f"  Diagnostics (technical health):\n{diag}\n"
        f"  Usage (which features are used, as counts):\n{usage}\n"
        "  Every report also carries a random install ID (not linked to your name, account, PC name or hardware), the hour of the\n"
        "  event, and the schema version. Hardware values are rounded into buckets (for example 8 GB, not 7.9 GB).\n\n"
        "WHAT IS NEVER SENT: your chats, prompts, the AI's replies, files, file names or paths, voice or audio, your name, e-mail,\n"
        "PC name, IP address (the server does not store it), or error message text. The format has no field that can hold free text.\n\n"
        "YOUR CONTROLS: `jarvis telemetry disable` stops sending and deletes anything unsent. `jarvis telemetry delete-my-data` asks the\n"
        "server to delete everything stored under your install ID and makes a new ID. You can enable diagnostics only or usage only.\n"
        "Data is kept for [OPERATOR: retention period] by [OPERATOR: legal name and contact]. [OPERATOR: legal basis and regional rules].\n"
    )


class Consent:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> dict | None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
        except (OSError, ValueError):
            return None

    def state(self) -> dict:
        d = self._read() or {}
        valid = d.get("notice_version") == NOTICE_VERSION and d.get("schema") == SCHEMA_VERSION
        return {"enabled": bool(valid and (d.get("diagnostics") or d.get("usage"))),
                "diagnostics": bool(valid and d.get("diagnostics")), "usage": bool(valid and d.get("usage")),
                "needs_reconsent": bool(d) and not valid, "install_id": d.get("install_id") if valid else None}

    def allows(self, category: str) -> bool:
        return bool(self.state().get(category))

    def install_id(self) -> str | None:
        return self.state()["install_id"]

    def enable(self, diagnostics: bool, usage: bool) -> dict:
        if not (diagnostics or usage):
            raise ValueError("choose at least one category, or leave telemetry off")
        old = self._read() or {}
        iid = old.get("install_id") if old.get("notice_version") == NOTICE_VERSION else None
        self._write({"notice_version": NOTICE_VERSION, "schema": SCHEMA_VERSION, "diagnostics": diagnostics, "usage": usage,
                     "install_id": iid or uuid.uuid4().hex, "consented_at": datetime.now(timezone.utc).isoformat()})
        return self.state()

    def disable(self) -> None:
        d = self._read() or {}
        self._write({"notice_version": NOTICE_VERSION, "schema": SCHEMA_VERSION, "diagnostics": False, "usage": False,
                     "install_id": d.get("install_id") or uuid.uuid4().hex, "disabled_at": datetime.now(timezone.utc).isoformat()})

    def reset_id(self) -> str:
        d = self._read() or {}
        d["install_id"] = uuid.uuid4().hex
        d.setdefault("notice_version", NOTICE_VERSION)
        d.setdefault("schema", SCHEMA_VERSION)
        d.setdefault("diagnostics", False)
        d.setdefault("usage", False)
        self._write(d)
        return d["install_id"]

    def _write(self, d: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        tmp.replace(self.path)
