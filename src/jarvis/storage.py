"""SQLite backup/restore used before upgrades, so a rollback never loses user data."""
from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def backup_sqlite(db_path: str | Path, backups_dir: str | Path, keep: int = 5) -> Path:
    db_path, backups_dir = Path(db_path), Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    out = backups_dir / f"{db_path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.sqlite"
    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(out))
    try:
        src.backup(dst)  # consistent even if the DB is in use
    finally:
        dst.close()
        src.close()
    old = sorted(backups_dir.glob(f"{db_path.stem}-*.sqlite"))
    for p in old[:-keep] if keep > 0 else []:
        p.unlink(missing_ok=True)
    return out


def restore_sqlite(backup: str | Path, db_path: str | Path) -> None:
    con = sqlite3.connect(str(backup))
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if ok != "ok":
        raise RuntimeError(f"backup failed its integrity check: {ok}")
    con = sqlite3.connect(str(backup))
    try:
        ver = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    from jarvis.memory.store import SCHEMA_VERSION
    if ver > SCHEMA_VERSION:
        raise RuntimeError(f"backup was made by a newer version (schema {ver}); this build understands up to {SCHEMA_VERSION}")
    tmp = Path(str(db_path) + ".restoring")
    shutil.copyfile(backup, tmp)
    # A leftover journal/WAL from the old file must never be applied to the restored one.
    for suffix in ("-journal", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    os.replace(tmp, db_path)
