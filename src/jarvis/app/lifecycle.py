"""Start-up and shut-down safety for the desktop app: safe data-folder resolution, understandable errors, and one window per profile."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from jarvis.config import Config


class StartupError(Exception):
    """A problem the user can act on. `message` says what happened; `hint` says what to do."""
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message, self.hint = message, hint

    def text(self) -> str:
        return self.message + (f"\n\nWhat to do: {self.hint}" if self.hint else "")


_BAD_DIR = re.compile(r'[\x00-\x1f<>"|?*]')


def check_data_dir(path: Path) -> Path:
    """Resolve and validate the folder for writable user data. It must not be a file, must not sit inside the program folder of a frozen
    app (writable data stays outside the installation), and must be writable."""
    p = Path(path)
    if _BAD_DIR.search(str(p)):
        raise StartupError(f"The data folder name contains characters Windows does not allow: {p}", "choose a different folder with --data-dir")
    try:
        p = p.expanduser().resolve()
    except (OSError, RuntimeError) as e:
        raise StartupError(f"The data folder could not be resolved: {e}", "choose a different folder with --data-dir") from None
    if p.exists() and not p.is_dir():
        raise StartupError(f"The data location is a file, not a folder: {p}", "move or rename that file, or use --data-dir")
    import sys
    if getattr(sys, "frozen", False):
        try:
            exe_dir = Path(sys.executable).resolve().parent
            if p == exe_dir or exe_dir in p.parents:
                raise StartupError(f"The data folder is inside the program folder ({exe_dir}); upgrades could overwrite it.",
                                   "use the default folder (%LOCALAPPDATA%\\ProjectJarvis) or another folder outside the installation")
        except OSError:
            pass
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / f".write_test_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise StartupError(f"Jarvis cannot write to its data folder {p} ({e.strerror or e}).",
                           "check the folder's permissions or free disk space, or pick another folder with --data-dir") from None
    return p


def prepare(cfg: Config) -> list[str]:
    """Create the folders the app needs (no administrator rights, nothing outside the data folder). Returns what was created."""
    root = check_data_dir(cfg.data_dir)
    made = []
    for d in (root, cfg.models_dir, cfg.backups_dir, root / "logs", cfg.profile_dir):
        if not d.exists():
            made.append(str(d))
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StartupError(f"Cannot create {d} ({e.strerror or e}).", "check permissions and free disk space") from None
    return made


def load_config_file(path: str | Path | None) -> Config:
    """Read an optional TOML configuration file; a bad file gives an understandable error instead of a traceback."""
    from jarvis.config import load_config
    try:
        return load_config(path)
    except ValueError as e:                                     # includes tomllib.TOMLDecodeError
        raise StartupError(f"The configuration file {path} is not valid ({e}).",
                           "fix or delete the file; defaults are used when it is missing. A sample is config.sample.toml") from None
    except OSError as e:
        raise StartupError(f"The configuration file {path} could not be read ({e.strerror or e}).", "check the path and permissions") from None


# ------------------------------------------------------------------------------ one window per profile
def _pid_alive(pid: int) -> bool:
    from jarvis.missions.store import pid_alive
    return pid_alive(pid)


@dataclass
class InstanceLock:
    """A lock FILE (not a network port) so that two windows cannot work on the same profile at once. Stale locks left by a crash are detected by
    checking whether the recorded process still exists. The lock never blocks the command-line tools; those have their own per-file locks."""
    path: Path
    held: bool = False

    @classmethod
    def for_profile(cls, cfg: Config) -> "InstanceLock":
        return cls(cfg.profile_dir / "app.lock")

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self.owner()
                if owner and owner != os.getpid() and _pid_alive(owner):
                    return False
                try:
                    self.path.unlink()                         # stale (the owner is gone) or unreadable: take it over
                except OSError:
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "since": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
            self.held = True
            return True
        return False

    def owner(self) -> int | None:
        try:
            return int(json.loads(self.path.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def release(self) -> None:
        if self.held:
            try:
                if self.owner() == os.getpid():
                    self.path.unlink()
            except OSError:
                pass
            self.held = False
