"""Persistent mission records in <profile>/missions/. Local files only, written atomically.

What is stored: the plan (which you wrote or approved), state changes, and for each step the tool's status, a SHORT redacted message and evidence, the
verification outcome and timestamps. NOT stored: file contents, screen text, process lists or anything a tool returned as 'untrusted_text', or any
secret-looking text (redacted before writing). You can read every file; `jarvis mission delete` and `jarvis mission history --delete` remove them, and
`jarvis memory forget-all` removes the whole folder."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

import re
from jarvis.memory.secrets import redact
from jarvis.missions import model as M

DIR = "missions"


_IDENT = re.compile(r"^(m-\d{8}-\d{6}-[0-9a-f]{6}(#\d+)?|sch-[0-9a-f]{8}|[0-9a-f]{64})(@[0-9TZ:\-+]+)?$")   # our own ids/hashes: random hex can look like a token to the redactor


def redact_obj(o):
    if isinstance(o, str):
        return o if _IDENT.match(o) else redact(o)
    if isinstance(o, dict):
        return {k: redact_obj(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [redact_obj(v) for v in o]
    return o


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":                                   # UNTESTED on Windows
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# Mission locks held by THIS process. The lock file records a process id, and a lock owned by our own pid used to be treated as stale (a leftover of an
# earlier life of the same pid), so a second thread of the same program could take a running mission's lock and run it a second time. The in-process
# registry makes the thread case exact; the file still protects against other processes.
_HELD: set[str] = set()
_HELD_GUARD = threading.Lock()


class MissionStore:
    def __init__(self, profile_dir):
        self.root = Path(profile_dir) / DIR
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- files
    def _path(self, mid: str) -> Path:
        if not isinstance(mid, str) or not M.MISSION_ID_RE.match(mid):
            raise M.StateError("not a mission id (they look like m-20260101-120000-ab12cd)")
        return self.root / f"{mid}.json"

    def save(self, m: dict) -> None:
        d = redact_obj(m)
        d["id"] = m["id"]                                      # random hex in an id can look like a token to the redactor; ids are not secret
        d["steps"] = m["steps"]                                # the plan is stored exactly as approved (normalize already refused secrets in it)
        d["plan_hash"] = m["plan_hash"]                        # a SHA-256 looks like a token to the redactor; it is not secret
        d["approval"] = m["approval"]
        d["executions"] = [dict(e, plan_hash=x["plan_hash"]) for e, x in zip(d["executions"], m["executions"])]
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".m-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1, default=str)
            os.replace(tmp, self._path(m["id"]))

    def load(self, mid: str) -> dict:
        try:
            m = json.loads(self._path(mid).read_text(encoding="utf-8"))
        except OSError:
            raise KeyError(f"no mission {mid}") from None
        except ValueError:
            raise M.StateError(f"mission {mid} is unreadable (damaged file)") from None
        if not isinstance(m, dict) or m.get("id") != mid or m.get("state") not in M.STATES:
            raise M.StateError(f"mission {mid} is unreadable (damaged file)")
        if M.plan_hash({"goal": m["goal"], "limits": m["limits"], "steps": m["steps"]}) != m.get("plan_hash"):
            m["tampered"] = True                               # the steps no longer match the hash that was shown/approved
        return m

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.root.glob("m-*.json")) if self.root.is_dir() else []:
            try:
                m = self.load(f.stem)
                out.append({"id": m["id"], "state": m["state"], "goal": m["goal"][:100], "updated": m["updated"], "executions": len(m["executions"]), "tampered": bool(m.get("tampered"))})
            except (KeyError, M.StateError):
                out.append({"id": f.stem, "state": "damaged", "goal": "", "updated": "", "executions": 0, "tampered": True})
        return out

    def delete(self, mid: str) -> bool:
        p = self._path(mid)
        existed = p.exists()
        for f in (p, self.root / f"{mid}.cancel", self.root / f"{mid}.pause", self.root / f"{mid}.lock"):
            f.unlink(missing_ok=True)
        return existed

    def delete_all(self) -> int:
        n = len(list(self.root.glob("m-*.json"))) if self.root.is_dir() else 0
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        return n

    # ---------------------------------------------------------------- run lock + flags (cross-process cancel/pause)
    def lock(self, mid: str) -> bool:
        p = self.root / f"{mid}.lock"
        self.root.mkdir(parents=True, exist_ok=True)
        key = os.path.normcase(os.path.abspath(str(p)))
        with _HELD_GUARD:
            if key in _HELD:
                return False                                    # another thread of this program is running this mission right now
            if self._lock_file(p):
                _HELD.add(key)
                return True
            return False

    def _lock_file(self, p: Path) -> bool:
        for _ in range(2):
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    json.dump({"pid": os.getpid(), "at": M.now()}, f)
                return True
            except FileExistsError:
                try:
                    pid = int(json.loads(p.read_text()).get("pid", 0))
                except (OSError, ValueError, TypeError):
                    pid = 0
                if pid_alive(pid) and pid != os.getpid():
                    return False
                p.unlink(missing_ok=True)                       # left behind by a process that no longer exists
        return False

    def unlock(self, mid: str) -> None:
        p = self.root / f"{mid}.lock"
        with _HELD_GUARD:
            _HELD.discard(os.path.normcase(os.path.abspath(str(p))))
            p.unlink(missing_ok=True)

    def is_running_elsewhere(self, mid: str) -> bool:
        p = self.root / f"{mid}.lock"
        try:
            pid = int(json.loads(p.read_text()).get("pid", 0))
        except (OSError, ValueError, TypeError):
            return False
        return pid_alive(pid)

    def flag(self, mid: str, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{mid}.{name}").write_text(M.now(), encoding="utf-8")

    def flagged(self, mid: str, name: str) -> bool:
        return (self.root / f"{mid}.{name}").exists()

    def clear_flag(self, mid: str, name: str) -> None:
        (self.root / f"{mid}.{name}").unlink(missing_ok=True)

    # ---------------------------------------------------------------- crash recovery
    def recover(self, mid: str) -> bool:
        """A mission recorded as 'running' whose process is gone was interrupted. It is NOT restarted and its in-flight step is marked UNKNOWN."""
        m = self.load(mid)
        if m["state"] != "running" or self.is_running_elsewhere(mid):
            return False
        ex = M.current_execution(m)
        if ex is not None:
            for sid, r in ex["steps"].items():
                if r["status"] == "running":
                    inflight = r.pop("in_flight", None) or {}
                    r["status"] = "unknown"
                    r["finished"] = M.now()
                    r["attempts"].append({"n": len(r["attempts"]) + 1, "kind": inflight.get("kind", "initial"), "at": M.now(), "action_status": "interrupted", "message": "Jarvis stopped while this step was running; it may or may not have happened",
                                          "evidence": "", "verification": "unavailable", "checks": []})
            ex["state"] = "paused"
            ex["notes"].append("interrupted: Jarvis closed or crashed while the mission was running; nothing was repeated")
        M.set_state(m, "paused", "interrupted by an application stop or crash; resume with your decision about the step that was running")
        self.save(m)
        return True

    def recover_all(self) -> list[str]:
        out = []
        for row in self.list():
            if row["state"] == "running":
                try:
                    if self.recover(row["id"]):
                        out.append(row["id"])
                except (KeyError, M.StateError):
                    pass
        return out

    # ---------------------------------------------------------------- execution history (append-only, redacted)
    @property
    def history_file(self) -> Path:
        return self.root / "history.jsonl"

    def log(self, rec: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        rec = redact_obj({"at": M.now(), **rec})
        with self._lock, open(self.history_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def history(self, mission_id: str | None = None, n: int = 50) -> list[dict]:
        out = []
        try:
            lines = self.history_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if mission_id is None or r.get("mission") == mission_id:
                out.append(r)
        return out[-n:]

    def delete_history(self, mission_id: str | None = None) -> int:
        """Remove history lines (all, or one mission's). Mission files themselves are removed with `delete`."""
        try:
            lines = self.history_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return 0
        keep, gone = [], 0
        for ln in lines:
            try:
                r = json.loads(ln)
            except ValueError:
                gone += 1
                continue
            if mission_id is None or r.get("mission") == mission_id:
                gone += 1
            else:
                keep.append(ln)
        if keep:
            self.history_file.write_text("\n".join(keep) + "\n", encoding="utf-8")
        else:
            self.history_file.unlink(missing_ok=True)
        if mission_id is None:
            for m in self.list():
                try:
                    mm = self.load(m["id"])
                    mm["executions"] = []
                    self.save(mm)
                except (KeyError, M.StateError):
                    pass
        else:
            try:
                mm = self.load(mission_id)
                mm["executions"] = []
                self.save(mm)
            except (KeyError, M.StateError):
                pass
        return gone

    def export(self, mid: str) -> dict:
        return redact_obj(self.load(mid))
