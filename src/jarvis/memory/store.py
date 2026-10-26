"""Local SQLite memory: conversations, notes, and an honest action log."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2   # v2 adds the personal-memory tables (additive; v1 databases are upgraded in place)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryDatabaseError(ValueError):
    """The memory file exists but is not a usable database. The file is left exactly as it was so it can still be recovered."""


class MemoryStore:
    def __init__(self, db_path: str | Path = ":memory:", check_same_thread: bool = True) -> None:
        # check_same_thread=False is used only by the desktop window, which creates the assistant on a worker thread but closes it from the
        # interface thread; it never lets two threads use the connection at the same time (one reply at a time, closed after workers end).
        self._db = sqlite3.connect(str(db_path), check_same_thread=check_same_thread)
        self._db.row_factory = sqlite3.Row
        try:
            found = self._db.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as e:
            self._db.close()
            raise MemoryDatabaseError(f"The memory database {db_path} cannot be read ({e}). Jarvis did not change or delete it. "
                                      "If it is a damaged file, restore a backup with `jarvis restore` (backups are in the backups folder next to it), "
                                      "or move the file aside to start with an empty memory. Other Jarvis features are not affected.") from None
        if found > SCHEMA_VERSION:  # downgrade safety: never run an old build on a newer database
            self._db.close()
            raise RuntimeError(f"this database was written by a newer version (schema {found}, this build "
                               f"understands {SCHEMA_VERSION}). Upgrade the app or restore a backup.")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations(
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
                content TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notes(
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS action_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, args TEXT NOT NULL,
                status TEXT NOT NULL, output TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL, key TEXT, text TEXT NOT NULL, project TEXT,
                source TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT, last_used_at TEXT,
                use_count INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}');
            CREATE INDEX IF NOT EXISTS memories_cat ON memories(category, status);
            CREATE TABLE IF NOT EXISTS memory_vectors(
                memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE, dim INTEGER NOT NULL, vec BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS workflow_runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, outcome TEXT NOT NULL,
                verified_by TEXT, steps TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS learning_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, detail TEXT NOT NULL, ref_id INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS signals(
                kind TEXT PRIMARY KEY, count INTEGER NOT NULL, last_at TEXT NOT NULL);
            PRAGMA foreign_keys=ON;
            """
        )
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()

    @property
    def db(self) -> sqlite3.Connection:
        """The open connection (used by jarvis.memory.personal, which owns the personal-memory tables)."""
        return self._db

    def close(self) -> None:
        self._db.close()

    # conversations -----------------------------------------------------------
    def new_conversation(self, title: str = "New chat") -> int:
        cur = self._db.execute("INSERT INTO conversations(title,created_at) VALUES(?,?)", (title, _now()))
        self._db.commit()
        return int(cur.lastrowid)

    def add_message(self, conversation_id: int, role: str, content: str) -> int:
        cur = self._db.execute(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)",
            (conversation_id, role, content, _now()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def messages(self, conversation_id: int) -> list[dict]:
        rows = self._db.execute(
            "SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def conversations(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM conversations ORDER BY id DESC").fetchall()]

    def delete_conversation(self, conversation_id: int) -> None:
        self._db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        self._db.commit()

    # notes -------------------------------------------------------------------
    def add_note(self, text: str) -> int:
        cur = self._db.execute("INSERT INTO notes(text,created_at) VALUES(?,?)", (text, _now()))
        self._db.commit()
        return int(cur.lastrowid)

    def notes(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM notes ORDER BY id").fetchall()]

    def delete_note(self, note_id: int) -> bool:
        cur = self._db.execute("DELETE FROM notes WHERE id=?", (note_id,))
        self._db.commit()
        return cur.rowcount > 0

    # action log --------------------------------------------------------------
    def log_action(self, tool: str, args: dict, status: str, output: str) -> int:
        cur = self._db.execute(
            "INSERT INTO action_log(tool,args,status,output,created_at) VALUES(?,?,?,?,?)",
            (tool, json.dumps(args, default=str), status, output, _now()),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def actions(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM action_log ORDER BY id").fetchall()]

    # privacy -----------------------------------------------------------------
    def export_all(self) -> dict:
        return {
            "conversations": self.conversations(),
            "messages": [dict(r) for r in self._db.execute("SELECT * FROM messages ORDER BY id").fetchall()],
            "notes": self.notes(),
            "action_log": self.actions(),
        }

    def delete_all(self) -> None:
        for t in ("messages", "conversations", "notes", "action_log", "memory_vectors", "memories", "workflow_runs",
                  "learning_log", "signals"):
            self._db.execute(f"DELETE FROM {t}")
        self._db.commit()
