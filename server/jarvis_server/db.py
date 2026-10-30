from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, recovery_hash TEXT NOT NULL,
  created_at REAL NOT NULL, terms_sha TEXT NOT NULL, terms_accepted_at REAL NOT NULL, age_confirmed INTEGER NOT NULL,
  learning_opt_in INTEGER NOT NULL DEFAULT 0, learning_changed_at REAL,
  learning_basis TEXT NOT NULL DEFAULT 'none', learning_consent_sha TEXT, learning_consent_at REAL,
  failed_logins INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0,
  display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', pw_changed_at REAL);
CREATE TABLE IF NOT EXISTS sessions(
  token_digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, origin_at REAL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
  learn_ok INTEGER NOT NULL, used_in_export INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_id);
CREATE TABLE IF NOT EXISTS telemetry(
  id INTEGER PRIMARY KEY, received_at REAL NOT NULL, install_id TEXT NOT NULL, event TEXT NOT NULL,
  event_hour TEXT NOT NULL, app_version TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tel_install ON telemetry(install_id);
CREATE INDEX IF NOT EXISTS ix_tel_received ON telemetry(received_at);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, user_id INTEGER);
"""


SCHEMA_VERSION = 2


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self._open_and_migrate()
        except BaseException:
            self.conn.close()                  # a refused or failed open must not leave the file locked (Windows cannot delete an open database)
            raise

    def _open_and_migrate(self) -> None:
        self.conn.execute("PRAGMA foreign_keys = ON")
        ver = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {ver} is newer than this server understands")
        if ver < SCHEMA_VERSION and self._has_users():
            self._backup(ver)                  # an older database is copied BEFORE any change, so a failed or unwanted upgrade can be undone by restoring the copy
        self._migrate_users()
        self._migrate_v2()
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _has_users(self) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone())

    def _backup(self, ver: int) -> None:
        if self.path == ":memory:" or not Path(self.path).exists():
            return
        self.conn.commit()
        dest = Path(self.path + f".v{ver}.bak")
        if dest.exists():
            return                             # never overwrite an earlier backup
        bk = sqlite3.connect(str(dest))
        try:
            self.conn.backup(bk)
        finally:
            bk.close()

    def _migrate_v2(self) -> None:
        """v1 -> v2: display name, account status (for future suspension; only 'active' is used today), password-change time, session origin time."""
        if not self._has_users():
            return
        ucols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        try:
            self.conn.execute("BEGIN")
            if "display_name" not in ucols:
                self.conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
                self.conn.execute("UPDATE users SET display_name = username WHERE display_name = ''")
            if "status" not in ucols:
                self.conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            if "pw_changed_at" not in ucols:
                self.conn.execute("ALTER TABLE users ADD COLUMN pw_changed_at REAL")
            scols = {r[1] for r in self.conn.execute("PRAGMA table_info(sessions)").fetchall()}
            if scols and "origin_at" not in scols:
                self.conn.execute("ALTER TABLE sessions ADD COLUMN origin_at REAL")
                self.conn.execute("UPDATE sessions SET origin_at = created_at WHERE origin_at IS NULL")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _migrate_users(self) -> None:
        """Databases made before the opt-in redesign had learning ON by default. Their stored value is NOT changed
        (we never silently flip a user's setting), but they are marked 'legacy_default': that is not consent, so no
        message of theirs is used for training until they explicitly opt in again."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if not cols or "learning_basis" in cols:
            return
        self.conn.execute("ALTER TABLE users ADD COLUMN learning_basis TEXT NOT NULL DEFAULT 'none'")
        self.conn.execute("ALTER TABLE users ADD COLUMN learning_consent_sha TEXT")
        self.conn.execute("ALTER TABLE users ADD COLUMN learning_consent_at REAL")
        self.conn.execute("UPDATE users SET learning_basis = 'legacy_default' WHERE learning_opt_in = 1")
        self.conn.commit()

    def q(self, sql: str, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def one(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql: str, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def event(self, kind: str, user_id: int | None = None) -> None:
        """Audit trail: what happened and to whom, NEVER content, passwords, or IP addresses."""
        self.q("INSERT INTO events(at, kind, user_id) VALUES(?,?,?)", (time.time(), kind, user_id))

    def purge_expired_sessions(self) -> None:
        self.q("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

    def close(self) -> None:
        self.conn.close()
