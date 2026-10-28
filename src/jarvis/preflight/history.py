"""Where preflight results are kept: <data folder>/preflight/<run id>/ with preflight_record.json, preflight.log and, for an online check,
model_metadata.json/.md and raw/ (the host's API responses). Readable, small (the newest KEEP runs are kept, older ones removed), and scrubbed: the
home folder, the Windows user name and secret-looking text are removed before anything is written. Nothing is sent anywhere."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

KEEP = 20
MAX_RECORD_BYTES = 2_000_000
RUN_ID = re.compile(r"^\d{8}T\d{6}Z(-\d{1,3})?$")
SECRET_KEY = re.compile(r"(token|secret|passw|authorization|api[_-]?key|credential|cookie)", re.I)
PUBLIC_HASH = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")     # a published SHA-256 / commit id is public data; the general secret filter would blank it
SECRET_TEXT = re.compile(r"(?i)\b(bearer|token|password|passwd|secret|api[_-]?key|authorization)\b(\s*[=:]\s*|\s+)((?:bearer|basic|token)\s+)?([^\s,;\"']{4,})")   # "Authorization: Bearer abc" has a scheme word AND the value


def scrub_text(text: str) -> str:
    from jarvis.app.diagnostics import scrub_paths
    from jarvis.eval.redact import redact_paths
    t = SECRET_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}<removed>", str(text))
    return redact_paths(scrub_paths(t))


def scrub(obj):
    """A copy of a JSON-like structure that is safe to store: strings scrubbed, values under secret-looking keys dropped."""
    if isinstance(obj, str):
        return obj if PUBLIC_HASH.match(obj) else scrub_text(obj)
    if isinstance(obj, dict):
        return {k: ("<removed>" if SECRET_KEY.search(str(k)) else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj


def root(cfg) -> Path:
    return Path(cfg.data_dir) / "preflight"


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def new_run_dir(cfg, now: datetime | None = None) -> tuple[str, Path]:
    base = root(cfg)
    base.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(now)
    for n in range(0, 100):
        rid = stamp if n == 0 else f"{stamp}-{n}"
        try:
            (base / rid).mkdir()
            return rid, base / rid
        except FileExistsError:
            continue
    raise OSError("could not create a result folder")


def temp_run_dir() -> tuple[str, Path]:
    """Guest sessions never save: the same files go to a throw-away folder that the caller deletes."""
    return _stamp(), Path(tempfile.mkdtemp(prefix="jarvis-preflight-"))


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save(run_dir: Path, record: dict, log_lines: list[str]) -> None:
    clean = scrub(record)
    _write(run_dir / "preflight_record.json", json.dumps(clean, indent=2))
    _write(run_dir / "preflight.log", "\n".join(scrub_text(l) for l in log_lines) + "\n")


def prune(cfg, keep: int = KEEP) -> list[str]:
    """Remove the oldest run folders beyond `keep`. Only folders whose name is a run id, directly inside the preflight folder, and not links."""
    base = root(cfg)
    if not base.is_dir():
        return []
    runs = sorted(d for d in base.iterdir() if RUN_ID.match(d.name) and d.is_dir() and not d.is_symlink())
    removed = []
    for d in runs[:max(0, len(runs) - keep)]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


def list_runs(cfg, limit: int = KEEP) -> list[dict]:
    """Newest first. A damaged or oversized record is listed as unreadable, never trusted."""
    base = root(cfg)
    if not base.is_dir():
        return []
    out = []
    for d in sorted((d for d in base.iterdir() if RUN_ID.match(d.name) and d.is_dir() and not d.is_symlink()), reverse=True)[:limit]:
        f = d / "preflight_record.json"
        row = {"run_id": d.name, "readable": False}
        try:
            if f.is_symlink() or f.stat().st_size > MAX_RECORD_BYTES:
                raise ValueError
            rec = json.loads(f.read_text(encoding="utf-8"))
            v = rec.get("verdict") or {}
            row.update(readable=True, started_at_utc=rec.get("started_at_utc"), app_version=(rec.get("app") or {}).get("version"), include_online=rec.get("include_online"),
                       verdict=v.get("code"), text=v.get("text"), checks=len(rec.get("checks") or []))
        except (OSError, ValueError, AttributeError, TypeError, RecursionError):
            pass
        out.append(row)
    return out
