"""Where the Kokoro model files live, whether they are usable, and the consented, cancellable download of them.

What is and is NOT known (read before relying on this):
* The two files are the ones the kokoro-onnx project lists for its "v1.0" model: `kokoro-v1.0.onnx` (about 310 MB) and `voices-v1.0.bin` (a few tens of MB).
  The sizes below are APPROXIMATE figures from that project's documentation, used only to warn the person and to cap a runaway download; they are not checksums.
* No publisher SHA-256 for these files was found, so none is compared. Integrity is: the host's announced length must match what arrived, the size must be
  plausible, the voices file must be a readable archive, and a SHA-256 of the bytes received is recorded locally for later "has this file changed" checks.
  That detects truncation and later damage; it does not prove the publisher's file is the one received.
* The download URLs were NOT opened in the authoring sandbox (no route to the host). The code is tested with a fake network policy only.
* Nothing downloads by itself: `download()` needs `consent=True`, uses the Stage 5 NetworkPolicy (feature `model_download`, granted for this one action only),
  talks only to the hosts listed in `HOSTS`, and removes its partial files on any failure or cancel.
* The files go to the user's data folder (`<data_dir>/voice_models/kokoro`), never into SQLite or the settings. No model weights are shipped with Jarvis."""
from __future__ import annotations

import hashlib
import json
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis.models import diskspace
from jarvis.models.errors import IntegrityError
from jarvis.net.policy import NetworkDisabled

MB = 1024 * 1024
RELEASE_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
HOSTS = ("github.com", "githubusercontent.com")      # the release page and the content hosts it redirects to; every redirect target is checked too
MODEL_LICENCE = "Apache-2.0 (Kokoro-82M weights, as stated by their publisher)"
RUNTIME_LICENCE = "kokoro-onnx: MIT. Its text-to-phoneme step uses espeak-ng, which is GPL-3.0, so Jarvis does not bundle it: you install it yourself with pip."
MANIFEST = "kokoro-install.json"


@dataclass(frozen=True)
class ModelFile:
    name: str
    approx_bytes: int
    role: str

    @property
    def url(self) -> str:
        return RELEASE_BASE + self.name

    @property
    def min_bytes(self) -> int:
        return self.approx_bytes // 2

    @property
    def max_bytes(self) -> int:
        return diskspace.size_limit(self.approx_bytes)


FILES = (ModelFile("kokoro-v1.0.onnx", 310 * MB, "model"), ModelFile("voices-v1.0.bin", 28 * MB, "voices"))
MODEL_FILE, VOICES_FILE = FILES


class KokoroModelError(RuntimeError):
    """Plain-text, path-free message safe to show."""


class Cancelled(KokoroModelError):
    pass


def approx_total_bytes() -> int:
    return sum(f.approx_bytes for f in FILES)


def human_size(n: int) -> str:
    return f"{n / MB:,.0f} MB"


def model_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "voice_models" / "kokoro"


_IDS_CACHE: dict[str, tuple[tuple, list[str]]] = {}


def read_voice_ids(voices_path: Path, limit: int = 2000) -> list[str]:
    """Voice names stored in the voices file (an .npz-style zip of one array per voice). Reads only the archive's index, never array data, never pickles."""
    try:
        s = Path(voices_path).stat()
        key = (s.st_size, s.st_mtime_ns)
        hit = _IDS_CACHE.get(str(voices_path))
        if hit and hit[0] == key:
            return list(hit[1])
        with zipfile.ZipFile(voices_path) as z:
            names = [i.filename for i in z.infolist()[:limit]]
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise KokoroModelError("the voices file is not a readable archive; it may be incomplete or damaged") from e
    ids = sorted({n[:-4] for n in names if n.endswith(".npy") and "/" not in n and n[:-4]})
    if not ids:
        raise KokoroModelError("the voices file lists no voices; it is not a compatible Kokoro voices file")
    _IDS_CACHE[str(voices_path)] = (key, ids)
    return list(ids)


class KokoroModelStore:
    def __init__(self, data_dir: str | Path, files: tuple[ModelFile, ...] = FILES):
        self.dir = model_dir(data_dir)
        self.files = tuple(files)            # always the two real files in the app; tests pass tiny stand-ins
        self.model_file = next(f for f in self.files if f.role == "model")
        self.voices_file = next(f for f in self.files if f.role == "voices")

    def path(self, f: ModelFile) -> Path:
        return self.dir / f.name

    # ---------------- state ----------------
    def _problem(self, f: ModelFile) -> str:
        """'' = file looks usable; otherwise the plain reason. Cheap: sizes and the archive index only."""
        p = self.path(f)
        try:
            if not p.is_file():
                return "missing"
            size = p.stat().st_size
        except OSError:
            return "unreadable"
        if size < f.min_bytes or size > f.max_bytes:
            return f"has an unexpected size ({human_size(size)}); it is probably incomplete or not the right file"
        if f.role == "model":
            try:
                with open(p, "rb") as fh:
                    head = fh.read(16).lstrip().lower()
            except OSError:
                return "unreadable"
            if head.startswith((b"<!doctype", b"<html", b"<?xml", b"{")):
                return "is a web page or text file, not a model"
        else:
            try:
                read_voice_ids(p)
            except KokoroModelError as e:
                return str(e)
        rec = self._manifest().get("files", {}).get(f.name)
        if rec and rec.get("size") != size:
            return "has changed size since it was installed; it may be damaged"
        return ""

    def _manifest(self) -> dict:
        try:
            m = json.loads((self.dir / MANIFEST).read_text(encoding="utf-8"))
            return m if isinstance(m, dict) else {}
        except (OSError, ValueError):
            return {}

    def partials(self) -> list[Path]:
        try:
            return sorted(self.dir.glob(".part-*"))
        except OSError:
            return []

    def status(self) -> dict:
        """state: missing | incomplete | damaged | ready. Never touches the network."""
        probs = {f.name: self._problem(f) for f in self.files}
        missing = [n for n, p in probs.items() if p == "missing"]
        bad = {n: p for n, p in probs.items() if p and p != "missing"}
        if not missing and not bad:
            state = "ready"
        elif len(missing) == len(self.files) and not bad:
            state = "incomplete" if self.partials() else "missing"
        elif bad:
            state = "damaged"
        else:
            state = "incomplete"
        return {"state": state, "folder": str(self.dir), "missing": missing, "problems": bad, "partial_files": len(self.partials()),
                "files": [{"name": f.name, "approx_bytes": f.approx_bytes, "url": f.url} for f in self.files],
                "approx_total": human_size(sum(f.approx_bytes for f in self.files)), "model_licence": MODEL_LICENCE, "runtime_licence": RUNTIME_LICENCE}

    def voice_ids(self) -> list[str]:
        return read_voice_ids(self.path(self.voices_file))

    def verify_hashes(self, cancel: Callable[[], bool] | None = None) -> dict:
        """Deep check (reads both files; run it on a worker): compares each file's SHA-256 with the value recorded when Jarvis installed it.
        Files placed by hand have no record, so for those only the cheap checks apply."""
        rec = self._manifest().get("files", {})
        out = {}
        for f in self.files:
            if self._problem(f):
                out[f.name] = "problem"
                continue
            want = (rec.get(f.name) or {}).get("sha256")
            if not want:
                out[f.name] = "no-record"
                continue
            h = hashlib.sha256()
            with open(self.path(f), "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    if cancel and cancel():
                        raise Cancelled("The check was cancelled.")
                    h.update(chunk)
            out[f.name] = "match" if h.hexdigest() == want else "changed"
        return out

    def clean_partials(self) -> int:
        n = 0
        for p in self.partials():
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    # ---------------- download ----------------
    def download(self, policy, *, consent: bool, cancel: Callable[[], bool] | None = None, progress: Callable[[int, int], None] | None = None) -> dict:
        """Fetch the files that are not already usable. `policy` is a NetworkPolicy (or anything with grant_once/open). Returns {"installed": [...], "kept": [...]}.
        Raises KokoroModelError (plain message) on refusal, failure or cancel; partial files are always removed."""
        if not consent:
            raise KokoroModelError("Downloading needs your confirmation first. Nothing was downloaded.")
        cancel = cancel or (lambda: False)
        todo = [f for f in self.files if self._problem(f)]
        kept = [f.name for f in self.files if f not in todo]
        if not todo:
            return {"installed": [], "kept": kept}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.clean_partials()
            diskspace.ensure_free(self.dir, sum(diskspace.need_for_download(f.max_bytes) for f in todo), "download the Kokoro model files")
        except IntegrityError as e:
            raise KokoroModelError(str(e)) from e
        except OSError as e:
            raise KokoroModelError("The Kokoro folder could not be created or written. Check the disk and the folder permissions.") from e
        total_expected, done_before = sum(f.approx_bytes for f in todo), 0
        parts: list[tuple[ModelFile, Path, str, int]] = []
        try:
            for f in todo:
                part, digest, size = self._fetch_one(policy, f, cancel, progress, done_before, total_expected)
                parts.append((f, part, digest, size))
                done_before += size
            rec = dict(self._manifest().get("files", {}))
            for f, part, digest, size in parts:
                part.replace(self.path(f))
                rec[f.name] = {"size": size, "sha256": digest, "source": f.url}
            (self.dir / MANIFEST).write_text(json.dumps(
                {"files": rec, "note": "sha256 was computed locally from the bytes received; no publisher checksum was available to compare it with"}, indent=2), encoding="utf-8")
        except KokoroModelError:
            raise
        except NetworkDisabled as e:
            raise KokoroModelError("Downloads are switched off by the network policy, so nothing was downloaded.") from e
        except IntegrityError as e:
            raise KokoroModelError(str(e)) from e
        except OSError as e:
            full = diskspace.translate_oserror(e, "download the Kokoro model files")
            raise KokoroModelError(str(full) if full else "The model files could not be written. Check the disk and the folder permissions.") from e
        except Exception as e:      # noqa: BLE001 - network failures and the like become a plain message
            raise KokoroModelError(f"The download failed ({type(e).__name__}). Check the internet connection and try again; nothing was installed.") from e
        finally:
            for p in self.partials():
                p.unlink(missing_ok=True)
        bad = [f.name for f in todo if self._problem(f)]
        if bad:
            raise KokoroModelError("The downloaded files did not pass the checks (" + ", ".join(bad) + "). Press Install / repair to try again.")
        return {"installed": [f.name for f in todo], "kept": kept}

    def _fetch_one(self, policy, f: ModelFile, cancel, progress, done_before: int, total_expected: int) -> tuple[Path, str, int]:
        part = self.dir / f".part-{uuid.uuid4().hex}"
        h, total = hashlib.sha256(), 0
        limit = f.max_bytes
        net_msg = "The download failed ({}). Check the internet connection and try again; nothing was installed."
        with policy.grant_once("model_download"):
            try:
                resp_cm = policy.open("model_download", f.url, timeout=60, allowed_host_suffixes=HOSTS)
            except (NetworkDisabled, KokoroModelError):
                raise
            except Exception as e:      # noqa: BLE001 - DNS, refused, blocked, HTTP error status ...
                raise KokoroModelError(net_msg.format(type(e).__name__)) from e
            with resp_cm as resp, open(part, "wb") as out:
                declared = None
                try:
                    declared = diskspace.parse_content_length(resp.headers.get("Content-Length"))
                except Exception:      # noqa: BLE001 - no usable header: the streaming limit alone applies
                    declared = None
                if declared is not None and (declared > limit or declared < f.min_bytes):
                    raise KokoroModelError(f"The server announced an unexpected size for {f.name} ({human_size(declared)}); aborted before downloading.")
                while True:
                    try:
                        chunk = resp.read(1 << 20)          # only the READ is a network step: a failure here is a connection problem, a failed write below is a disk problem
                    except Exception as e:      # noqa: BLE001
                        raise KokoroModelError(net_msg.format(type(e).__name__)) from e
                    if not chunk:
                        break
                    if cancel():
                        raise Cancelled("The download was cancelled. The partial files were removed.")
                    total += len(chunk)
                    if total > limit:
                        raise KokoroModelError(f"{f.name} was larger than expected; the download was stopped and removed.")
                    out.write(chunk)
                    h.update(chunk)
                    if progress:
                        progress(done_before + total, max(total_expected, done_before + total))
        if declared is not None and total != declared:
            raise KokoroModelError(f"{f.name} arrived incomplete ({human_size(total)} of {human_size(declared)}); nothing was installed.")
        return part, h.hexdigest(), total
