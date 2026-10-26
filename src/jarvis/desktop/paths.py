"""Restricted working directories. Every path a tool touches must lie inside a folder the user allowed, must not pass through
a symbolic link, junction or other reparse point, and must not be Jarvis' own data (permissions, audit log, checkpoints,
memory). System folders can never be allowed. Executable and script files can never be written, so the model cannot create a
program and get it run. When the answer is uncertain (a link check fails, a name cannot be resolved) the path is REFUSED."""
from __future__ import annotations

import os
import re
import stat
import unicodedata
from pathlib import Path

# Never writable and never allowed as a root, even if the user asks (they can still do it themselves in Explorer).
_SYSTEM_ENV = ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
EXECUTABLE_EXT = {".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
                  ".msi", ".msp", ".msc", ".scr", ".pif", ".cpl", ".dll", ".sys", ".lnk", ".url", ".reg", ".hta", ".jar",
                  ".py", ".pyw", ".appx", ".msix", ".appinstaller", ".application", ".inf", ".sh", ".scf", ".chm"}
INSTALLER_EXT = {".msi", ".msp", ".msix", ".appx", ".appinstaller"}
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
MAX_PATH_CHARS = 1000


class PathRejected(ValueError):
    pass


class PathMissing(PathRejected):
    """The path is fine but nothing is there. This is a failure to find it, not a refusal, and is reported as such."""


# Windows reparse-point tags that are NOT redirections (cloud placeholders such as OneDrive Files On-Demand, compression, dedup).
# Everything else flagged as a reparse point (junctions, mount points, symlinks, unknown tags) is refused. UNTESTED ON WINDOWS.
_REPARSE_FLAG = 0x400
_BENIGN_REPARSE_TAGS = {0x80000017, 0x80000013}          # WOF (compressed), data deduplication
_CLOUD_TAG_MASK, _CLOUD_TAG = 0xFFFF0FFF, 0x9000001A      # IO_REPARSE_TAG_CLOUD_* family


def _lstat(p):
    """One place that asks the OS about a path WITHOUT following links (tests replace it to simulate Windows)."""
    return os.lstat(p)


def default_protected(profile_dir) -> list[Path]:
    """Folders that hold Jarvis' own data for a profile: the profile folder itself and, for a named profile, the shared data
    folder above it (settings, models, provenance)."""
    pd = Path(profile_dir)
    out = [pd]
    if pd.parent.name == "profiles":
        out.append(pd.parent.parent)
    return out


def _link_problem(p: Path) -> str | None:
    """None when p is a plain file/folder or does not exist; otherwise why it is refused. Raises PathRejected when the
    operating system cannot tell us (fail closed)."""
    try:
        st = _lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError) as e:
        raise PathRejected(f"could not check whether {p} is a link ({type(e).__name__}); refused to be safe") from e
    if stat.S_ISLNK(st.st_mode):
        return "is a symbolic link"
    if getattr(st, "st_file_attributes", 0) & _REPARSE_FLAG:
        tag = getattr(st, "st_reparse_tag", 0)
        if tag in _BENIGN_REPARSE_TAGS or (tag & _CLOUD_TAG_MASK) == _CLOUD_TAG:
            return None
        return "is a junction, mount point or other redirecting reparse point"
    return None


def system_dirs() -> list[Path]:
    """Folders whose whole subtree is off limits."""
    out = [Path(p) for v in _SYSTEM_ENV if (p := os.environ.get(v))]
    if os.name != "nt":
        out += [Path(p) for p in ("/bin", "/sbin", "/usr", "/etc", "/boot", "/lib", "/proc", "/sys", "/dev")]
    return out


def _is_drive_root(p: Path) -> bool:
    return p == Path(p.anchor)


def _safe_resolve(p: Path) -> Path | None:
    try:
        return p.resolve()
    except OSError:
        return None


def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def _inside(child: Path, parent: Path) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _resolved_protected(protected) -> list[Path]:
    out = []
    for x in protected or ():
        r = _safe_resolve(Path(x))
        out.append(r if r is not None else Path(os.path.abspath(x)))
    return out


def check_root(root: str | Path, protected=()) -> Path:
    """Validate a folder the user wants to allow."""
    p = Path(root).expanduser()
    if not p.is_absolute():
        raise PathRejected("allowed folders must be absolute paths")
    r = p.resolve()
    if _is_drive_root(r):
        raise PathRejected(f"{r} is a whole drive; choose a folder")
    home = _safe_resolve(Path.home())
    if home is not None and _norm(r) == _norm(home):
        raise PathRejected("your whole user folder is too broad; choose a folder such as Documents\\JarvisWork")
    for s in system_dirs():
        sr = _safe_resolve(s)
        if sr is None:
            continue
        if _inside(r, sr):
            raise PathRejected(f"{r} is a system folder; Jarvis is never allowed to work there")
        if _inside(sr, r):
            raise PathRejected(f"{r} contains a system folder ({sr}); choose a narrower folder")
    for pr in _resolved_protected(protected):
        if _inside(r, pr) or _inside(pr, r):
            raise PathRejected(f"{r} is, contains or lies inside the folder where Jarvis keeps its own data ({pr}); "
                               "choose a different folder")
    return r


# Invisible or direction-changing characters that can make one path look like another. Zero-width JOINERS (U+200C/U+200D) are
# deliberately allowed because real file names in Persian, Hindi and emoji sequences use them.
_DISGUISING = {0x00AD, 0x061C, 0x200B, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2060, 0x2065), *range(0x2066, 0x206A),
               0xFEFF, 0xFFF9, 0xFFFA, 0xFFFB}


def _forbidden_char(raw: str) -> str | None:
    for ch in raw:
        if unicodedata.category(ch) in ("Cc", "Zl", "Zp") or ord(ch) in _DISGUISING:
            return f"U+{ord(ch):04X}"
    return None


# Source files a PROJECT folder may contain and the code tools may edit. Everything else in EXECUTABLE_EXT (.exe .bat .ps1 .vbs ...)
# stays forbidden everywhere. The ordinary allowed folders never get this exception.
SOURCE_EXT = {".py", ".js", ".sh"}
VCS_DIRS = {".git", ".hg", ".svn"}          # hooks and config in here can run programs: never written through any tool


class SafePaths:
    def __init__(self, roots: list[str | Path], protected=(), source_files: bool = False):
        self.roots = [Path(r).resolve() for r in roots]
        self.protected = _resolved_protected(protected)
        self.source_files = source_files

    def is_protected(self, p: Path) -> bool:
        """True when p is inside Jarvis' own data."""
        rp = _safe_resolve(Path(p))
        if rp is None:
            return True                                   # cannot tell: treat as protected
        return any(_inside(rp, pr) for pr in self.protected)

    def revalidate(self, p: Path, *, for_write: bool = True) -> None:
        """Re-run every check right before a change is committed; the path must still be exactly the one that was approved."""
        again = self.resolve(str(p), for_write=for_write)
        if _norm(again) != _norm(p):
            raise PathRejected(f"{p} changed while Jarvis was working on it (now {again}); nothing further was done")

    def resolve(self, raw: str, *, must_exist: bool = False, for_write: bool = False, allow_ancestor: bool = False) -> Path:
        """The one place a tool path is turned into the path that is really used. `allow_ancestor` lets a read-only listing
        name a folder that merely CONTAINS Jarvis' data (its contents are then hidden); changes never get that exception."""
        if not isinstance(raw, str) or not raw.strip():
            raise PathRejected("a path is required")
        if len(raw) > MAX_PATH_CHARS:
            raise PathRejected("path too long")
        bad = _forbidden_char(raw)
        if bad:
            raise PathRejected(f"the path contains an invisible or control character ({bad}), which could disguise where it points")
        if raw.startswith("\\\\") or raw.startswith("//"):
            raise PathRejected("network and device paths (\\\\server\\share, \\\\?\\) are not allowed")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            if not self.roots:
                raise PathRejected("no folders are allowed yet")
            p = self.roots[0] / p             # relative paths are relative to the first allowed folder
        # alternate data streams (file.txt:hidden) and reserved device names
        tail = str(p)[2:] if re.match(r"^[A-Za-z]:", str(p)) else str(p)
        if ":" in tail:
            raise PathRejected("':' is not allowed in file names (alternate data streams)")
        for part in p.parts[1:]:
            if part in (".", ".."):
                raise PathRejected("'.' and '..' are not allowed in paths; give the full path")
            if _RESERVED.match(part.strip()):
                raise PathRejected(f"'{part}' is a reserved Windows device name")
            if part != part.rstrip(" ."):
                raise PathRejected(f"'{part}' ends with a space or dot, which Windows silently drops")
        try:
            r = p.resolve()
        except (OSError, RuntimeError, ValueError) as e:     # e.g. a symlink loop
            raise PathRejected(f"{p} could not be resolved safely ({type(e).__name__}); refused") from e
        if not any(_inside(r, root) for root in self.roots):
            raise PathRejected(f"{r} is outside the folders Jarvis may use ({', '.join(map(str, self.roots)) or 'none'})")
        # No part of the path below the allowed folder may be a link / junction: the path the user approved must be the path used.
        root = next((rt for rt in self.roots if _inside(p, rt)), None)
        if root is None:
            raise PathRejected(f"{p} reaches an allowed folder through a link or alias; give the folder's real path")
        cur = root
        rel = os.path.relpath(str(p), str(root)) if _norm(p) != _norm(root) else "."
        for part in ([] if rel == "." else Path(rel).parts):
            cur = cur / part
            why = _link_problem(cur)
            if why:
                raise PathRejected(f"{cur} {why}; Jarvis does not follow links, junctions or reparse points")
        if _norm(r) != _norm(p) and not any(_inside(r, rt) and _inside(p, rt) for rt in self.roots):
            raise PathRejected(f"{p} resolves to a different location ({r}); refused")
        for pr in self.protected:
            if _inside(r, pr):
                raise PathRejected(f"{r} is part of Jarvis' own data (permissions, logs, backups, memory); file tools never touch it")
            if _inside(pr, r) and not allow_ancestor:
                raise PathRejected(f"{r} contains Jarvis' own data ({pr}); file tools never change, copy or move a folder that holds it")
        if any(sr is not None and _inside(r, sr) for sr in map(_safe_resolve, system_dirs())):
            raise PathRejected(f"{r} is inside a system folder")
        if for_write and r.suffix.lower() in (EXECUTABLE_EXT - SOURCE_EXT if self.source_files else EXECUTABLE_EXT):
            raise PathRejected(f"Jarvis does not create or change program/script files ({r.suffix})")
        if for_write and self.source_files and any(part.lower() in VCS_DIRS for part in r.parts):
            raise PathRejected(f"{r} is inside version-control internals (hooks and config there can run programs); never written")
        try:
            exists = r.exists()
        except OSError as e:
            raise PathRejected(f"could not check {r} ({type(e).__name__}); refused") from e
        if must_exist and not exists:
            raise PathMissing(f"{r} does not exist")
        if exists:
            st = _lstat(r)
            if stat.S_ISREG(st.st_mode) and getattr(st, "st_nlink", 1) > 1:
                raise PathRejected(f"{r} has more than one hard link, so it might also be a file Jarvis must not touch; refused")
        return r
