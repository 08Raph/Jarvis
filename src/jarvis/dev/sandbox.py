"""A real sandbox for running code that Jarvis (or a model, or a plugin author) wrote - or NOT running it.

Linux only. It uses unprivileged user + network + mount + pid namespaces (`unshare`) and `pivot_root`, so inside it:
  * the host filesystem does not exist (only a read-only /usr, a private writable /work copy, and a small /tmp);
  * there is no network (a new empty network namespace: connect() fails);
  * all capabilities are dropped and no new privileges can be gained (`setpriv`);
  * the process tree lives in its own pid namespace and is killed as a whole on timeout / stop / limit;
  * CPU time, memory, file size, open files and (where the OS enforces it) process count are limited, output and /work growth are capped.
It is NOT a hardened container: no seccomp filter, no cgroup limits, the kernel is shared (a kernel bug could escape), and the
process-count limit is only enforced by Linux for non-root users. Treat it as a strong guard against accidents and ordinary
malicious code, not as a guarantee against a determined attacker with a kernel exploit.

It never pretends: `availability()` actually starts the jail and checks that a canary host file is invisible and that a network
connection fails. If that does not hold (Windows, macOS, a container that forbids user namespaces, a missing tool) it reports
UNAVAILABLE with the reason, `run()` raises SandboxUnavailable, and callers must refuse to run untrusted code.
On Windows there is no equivalent in pure Python (Job Objects can limit memory/time but not file or network access), so nothing here
runs on Windows; WSL2 could provide the Linux sandbox but that is untested."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    wall_seconds: float = 30.0
    cpu_seconds: int = 20
    memory_mb: int = 1024
    file_mb: int = 50           # largest single file the code may write
    work_mb: int = 200          # total size of /work (checked while running)
    tmp_mb: int = 64            # size of /tmp (a tmpfs)
    open_files: int = 256
    output_bytes: int = 200_000  # per stream
    extra_processes: int = 128   # allowed on top of the user's current processes (only enforced for non-root)


@dataclass
class SandboxResult:
    returncode: int | None
    stdout: str
    stderr: str
    killed_for: str = ""         # "", "timeout", "stop", "output", "disk", "limit" (CPU time or memory limit)
    seconds: float = 0.0
    truncated: bool = False
    backend: str = "linux-namespaces"
    files_copied: int = 0
    skipped: list = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.killed_for == "" and self.returncode is not None


_JAIL = r'''
set -eu
FD="$1"; BASE="$2"; TMPMB="$3"; shift 3
NEW="$(mktemp -d)"
mount --make-rprivate / 2>/dev/null || true
mount -t tmpfs -o "size=${TMPMB}m,mode=755" tmpfs "$NEW"
mkdir "$NEW/usr"; mount --bind /usr "$NEW/usr"; mount -o remount,bind,ro "$NEW/usr"
for l in bin lib lib64 sbin; do if [ -L "/$l" ]; then ln -s "$(readlink "/$l")" "$NEW/$l"; elif [ -d "/$l" ]; then mkdir "$NEW/$l"; mount --bind "/$l" "$NEW/$l"; mount -o remount,bind,ro "$NEW/$l"; fi; done
while [ "$1" != "--" ]; do mkdir -p "$NEW$1"; mount --bind "$1" "$NEW$1"; mount -o remount,bind,ro "$NEW$1"; shift; done
shift
mkdir "$NEW/work" "$NEW/tmp" "$NEW/proc" "$NEW/dev" "$NEW/etc" "$NEW/old"
chmod 1777 "$NEW/tmp"
mount --bind "$BASE/work" "$NEW/work"
mount -t proc proc "$NEW/proc"
for n in null zero urandom; do : > "$NEW/dev/$n"; mount --bind "/dev/$n" "$NEW/dev/$n"; done
if [ -f /etc/ld.so.cache ]; then : > "$NEW/etc/ld.so.cache"; mount --bind /etc/ld.so.cache "$NEW/etc/ld.so.cache"; fi
cd "$NEW"
pivot_root . old
umount -l /old
rmdir /old 2>/dev/null || true
cd /work
echo ready >&"$FD"
exec setpriv --no-new-privs --bounding-set=-all --inh-caps=-all -- "$@"
'''

_SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".idea"}
MAX_COPY_FILES = 5000
MAX_COPY_BYTES = 100 * 1024 * 1024

_cached: tuple[bool, str] | None = None
_cache_lock = threading.Lock()


def _tools_missing() -> str:
    for t in ("unshare", "setpriv", "sh", "mount", "umount", "pivot_root"):
        if shutil.which(t) is None:
            return f"the '{t}' program is not installed"
    return ""


def interpreter() -> str:
    """The Python that runs INSIDE the sandbox (the one running Jarvis, resolved). It must be visible there (see _ro_binds)."""
    return os.path.realpath(sys.executable)


def _ro_binds() -> list[str]:
    """Directories that hold the interpreter and its standard library but are not under /usr (a venv or ~/python install)."""
    out = []
    for p in {os.path.realpath(sys.base_prefix), os.path.dirname(interpreter())}:
        if p and os.path.isdir(p) and not (p == "/usr" or p.startswith("/usr/")) and p not in ("/", "/home", "/root", "/tmp"):
            out.append(p)
    return sorted(out)


def _count_my_processes() -> int:
    uid = os.getuid()
    n = 0
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                if os.stat(f"/proc/{name}").st_uid == uid:
                    n += 1
            except OSError:
                pass
    return n


def _limits_preexec(lim: Limits):
    import resource

    def apply():
        os.setsid()
        mb = 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (lim.cpu_seconds, lim.cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (lim.memory_mb * mb, lim.memory_mb * mb))
        resource.setrlimit(resource.RLIMIT_FSIZE, (lim.file_mb * mb, lim.file_mb * mb))
        resource.setrlimit(resource.RLIMIT_NOFILE, (lim.open_files, lim.open_files))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:
            n = _count_my_processes() + lim.extra_processes
            resource.setrlimit(resource.RLIMIT_NPROC, (n, n))
        except (ValueError, OSError):
            pass
    return apply


def _dir_size(p: Path, cap: int) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
            if total > cap:
                return total
    return total


def _stage(base: Path, files: dict | None, copy_from: Path | None) -> tuple[int, list]:
    work = base / "work"
    work.mkdir(parents=True)
    skipped: list = []
    n = size = 0
    if copy_from is not None:
        src = Path(copy_from)
        for root, dirs, fs in os.walk(src):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not os.path.islink(os.path.join(root, d))]
            for f in fs:
                sp = Path(root) / f
                rel = sp.relative_to(src)
                if sp.is_symlink() or not sp.is_file():
                    skipped.append(f"{rel} (link or special file, not copied)")
                    continue
                sz = sp.stat().st_size
                n += 1
                size += sz
                if n > MAX_COPY_FILES or size > MAX_COPY_BYTES:
                    raise SandboxUnavailable(f"the project is too large to copy into the sandbox (more than {MAX_COPY_FILES} files or {MAX_COPY_BYTES // 2**20} MB)")
                dp = work / rel
                dp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sp, dp)
    for name, data in (files or {}).items():
        parts = Path(name).parts
        if os.path.isabs(name) or not parts or ".." in parts or any(c in name for c in "\x00:"):
            raise ValueError(f"bad file name for the sandbox: {name!r}")
        dp = work / name
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))
        n += 1
    return n, skipped


def _clean_env() -> dict:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"}


def run(argv: list[str], *, files: dict | None = None, copy_from: Path | None = None, limits: Limits | None = None,
        stop=None, check_available: bool = True, env: dict | None = None) -> SandboxResult:
    """Run argv (no shell) inside the sandbox. `argv[0]` must be an absolute path visible inside (the interpreter, or /usr/bin/...).
    `files` and `copy_from` are COPIED into /work; nothing the code does reaches the host, and nothing is copied back."""
    lim = limits or Limits()
    if check_available:
        ok, why = availability()
        if not ok:
            raise SandboxUnavailable(why)
    elif not sys.platform.startswith("linux") or _tools_missing():
        raise SandboxUnavailable(_tools_missing() or "no sandbox on this OS")
    if not argv or not os.path.isabs(argv[0]):
        raise ValueError("argv[0] must be an absolute path")
    base = Path(tempfile.mkdtemp(prefix="jarvis-sb-"))
    t0 = time.monotonic()
    try:
        copied, skipped = _stage(base, files, copy_from)
        rfd, wfd = os.pipe()
        cmd = ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "--kill-child", "--",
               "sh", "-c", _JAIL, "jail", str(wfd), str(base), str(lim.tmp_mb), *_ro_binds(), "--", *argv]
        e = _clean_env()
        e.update(env or {})
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e,
                                    pass_fds=(wfd,), preexec_fn=_limits_preexec(lim), close_fds=True)
        finally:
            os.close(wfd)
        out_buf, err_buf = bytearray(), bytearray()
        flags = {"over": False}

        def pump(stream, buf):
            while True:
                chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
                if not chunk:
                    return
                room = lim.output_bytes - len(buf)
                if room > 0:
                    buf.extend(chunk[:room])
                if len(chunk) > room:
                    flags["over"] = True
        threads = [threading.Thread(target=pump, args=(proc.stdout, out_buf), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, err_buf), daemon=True)]
        for t in threads:
            t.start()

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.kill()
            except OSError:
                pass
        killed, last_disk = "", 0.0
        deadline = t0 + lim.wall_seconds
        while proc.poll() is None:
            now = time.monotonic()
            if stop is not None and stop():
                killed = "stop"
            elif now > deadline:
                killed = "timeout"
            elif flags["over"]:
                killed = "output"
            elif now - last_disk > 0.25:
                last_disk = now
                if _dir_size(base / "work", lim.work_mb * 2**20) > lim.work_mb * 2**20:
                    killed = "disk"
            if killed:
                kill()
                break
            time.sleep(0.02)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            kill()
            proc.wait()
        for t in threads:
            t.join(2)
        for st in (proc.stdout, proc.stderr):
            try:
                st.close()
            except OSError:
                pass
        ready = b""
        try:
            ready = os.read(rfd, 16)
        except OSError:
            pass
        finally:
            os.close(rfd)
        out, err = out_buf.decode("utf-8", "replace"), err_buf.decode("utf-8", "replace")
        if ready.strip() != b"ready" and not killed:
            raise SandboxUnavailable("the sandbox could not be set up (nothing of yours was run): " + (err.strip()[-400:] or f"exit code {proc.returncode}"))
        if not killed and proc.returncode == 1 and "unshare: sigprocmask unblock failed" in err:
            # the jail's first process was killed by the kernel (CPU-time limit, or out of memory): say so instead of passing on
            # the wrapper's confusing message
            killed = "limit"
            err = err.replace("unshare: sigprocmask unblock failed: Invalid argument\n", "")
        return SandboxResult(None if killed else proc.returncode, out, err, killed, time.monotonic() - t0, flags["over"], "linux-namespaces", copied, skipped)
    finally:
        shutil.rmtree(base, ignore_errors=True)


_SELFTEST = r'''
import os, socket, sys
canary = sys.argv[1]
problems = []
if os.path.exists(canary): problems.append("host canary file is visible")
for p in ("/root", "/home", "/etc/passwd", "/var"):
    if os.path.exists(p): problems.append(p + " is visible")
try:
    s = socket.socket(); s.settimeout(2); s.connect(("93.184.216.34", 80)); problems.append("network connect succeeded")
except OSError:
    pass
try:
    open("/usr/jarvis-write-test", "w").close(); problems.append("/usr is writable")
except OSError:
    pass
open("/work/ok", "w").write("x")
print("PROBLEMS:" + "; ".join(problems) if problems else "ISOLATED")
'''


def availability(refresh: bool = False) -> tuple[bool, str]:
    """(usable, reason). Starts the jail for real and checks the isolation; a negative answer says why."""
    global _cached
    with _cache_lock:
        if _cached is not None and not refresh:
            return _cached
        if not sys.platform.startswith("linux"):
            _cached = (False, f"no genuine sandbox is available on {sys.platform}: Jarvis only has a Linux (namespace) sandbox. "
                              "Untrusted code will not be run here.")
            return _cached
        miss = _tools_missing()
        if miss:
            _cached = (False, f"no genuine sandbox: {miss}")
            return _cached
        canary = Path(tempfile.mkdtemp(prefix="jarvis-canary-")) / "canary.txt"
        try:
            canary.write_text("host secret")
            r = run([interpreter(), "-I", "-c", _SELFTEST, str(canary)], limits=Limits(wall_seconds=20, cpu_seconds=10, memory_mb=512),
                    check_available=False)
            last = (r.stdout.strip().splitlines() or [""])[-1]
            if r.completed and r.returncode == 0 and last == "ISOLATED":
                _cached = (True, "linux namespaces: no network, no host filesystem, read-only /usr, capabilities dropped (self-test passed)")
            else:
                _cached = (False, "the sandbox self-test did not confirm isolation, so it is not used: " + (last or r.stderr.strip()[-300:] or "no output"))
        except SandboxUnavailable as e:
            _cached = (False, str(e))
        except Exception as e:  # noqa: BLE001
            _cached = (False, f"sandbox self-test failed ({type(e).__name__}: {e})")
        finally:
            shutil.rmtree(canary.parent, ignore_errors=True)
        return _cached
