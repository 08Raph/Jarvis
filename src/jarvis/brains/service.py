"""BrainManager: the logic behind the Import Brain tab. Qt-free, so it is testable without a window.

What it does:  inspect / import (register in place or copy) local GGUF files; download catalog models WITHOUT activating them; judge whether this
PC can run each one (compat.py); TEST a model with a real load + generation; and ACTIVATE a model transactionally:

    validate -> compatibility -> (warn) -> load candidate -> real generation -> only then switch -> on any failure release the candidate and keep
    (or restore) the previous brain.

Rules kept here: nothing is executed from a model file (only its header is read); no network except the explicit download call; every
long operation takes a cancel flag and is meant to run on a worker thread; one brain operation at a time; an engine this class did not load is
never closed by it unless it is the host's own engine being replaced."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from jarvis.brains import compat
from jarvis.models import gguf as gguf_mod
from jarvis.models.errors import IntegrityError

TEST_PROMPT = "In one short sentence, tell me what two plus two equals."
STATE_FILE = "brains.json"
CONTEXT_MIN, CONTEXT_MAX = 512, 131072
STATUSES = ("available", "downloading", "checking", "ready", "incompatible", "failed", "unverified")
NO_BRAIN_MESSAGE = ("No brain is active. Open Import Brain and activate an installed model, or install one. Jarvis will not use a cloud service or "
                    "paid API instead.")


class Cancelled(Exception):
    pass


class EngineHost(Protocol):
    """Who holds the running engine (the chat assistant). The manager never reaches into the assistant; it asks the host."""

    def loaded_engine(self) -> Any | None: ...
    def release_engine(self) -> None: ...                       # close the host's engine and keep conversation/memory/persona intact
    def install_engine(self, engine: Any, model_id: str) -> None: ...   # take ownership of a loaded, tested engine


class NullHost:
    """No chat session yet: nothing is loaded. A tested engine is closed again (the chat will load the active brain when first used)."""
    engine = None

    def loaded_engine(self):
        return None

    def release_engine(self) -> None:
        pass

    def install_engine(self, engine, model_id: str) -> None:
        close = getattr(engine, "close", None)
        if callable(close):
            close()


def _safe(e: BaseException | str, limit: int = 240) -> str:
    """Exception text that is safe to show or save: copied prompt text dropped, the Windows user name / home folder and secret-looking text redacted."""
    text = e if isinstance(e, str) else f"{type(e).__name__}: {e}"
    try:
        from jarvis.disclosure import scrub_error_text
        text = scrub_error_text(text, limit)
    except Exception:      # noqa: BLE001
        text = " ".join(text.split())[:limit]
    try:
        from jarvis.app.diagnostics import scrub_paths
        text = scrub_paths(text)
    except Exception:      # noqa: BLE001
        pass
    return text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")
    return (s or "model")[:40]


def _flag(cancel) -> bool:
    if cancel is None:
        return False
    return bool(cancel.is_set() if hasattr(cancel, "is_set") else getattr(cancel, "cancelled", False))


def hash_file(path: Path, cancel=None, progress: Callable[[int, int], None] | None = None) -> str:
    h, done, total = hashlib.sha256(), 0, Path(path).stat().st_size
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            if _flag(cancel):
                raise Cancelled()
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return h.hexdigest()


class BrainManager:
    def __init__(self, cfg, store, policy, *, host: EngineHost | None = None, engine_factory: Callable | None = None,
                 hardware=None, runtime: dict | None = None, setup_engine=None, llama_factory=None, clock: Callable[[], float] = time.monotonic):
        self.cfg, self.store, self.policy = cfg, store, policy
        self.host = host or NullHost()
        self._factory = engine_factory
        self._llama_factory = llama_factory
        self._hw, self._rt = hardware, runtime            # tests only: simulated hardware / runtime facts (they prove the logic, not a real PC)
        self._setup = setup_engine
        self._clock = clock
        self._op = threading.Lock()                        # one brain operation at a time
        self._busy: dict[str, str] = {}                    # model id -> "downloading" | "checking"
        self._busy_lock = threading.Lock()
        self.current_operation = ""

    # ------------------------------------------------------------------ plumbing
    @property
    def state_path(self) -> Path:
        return Path(self.cfg.data_dir) / STATE_FILE

    def load_state(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, d: dict) -> None:
        Path(self.cfg.data_dir).mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    def _record_test(self, model_id: str, rec: dict) -> None:
        st = self.load_state()
        # "real" = the engine came from the real runtime; False when a test engine factory was injected (then the record proves logic only)
        st.setdefault("last_tests", {})[model_id] = {**rec, "real": self._factory is None}
        self._save_state(st)

    def hardware(self):
        if self._hw is not None:
            return self._hw
        from jarvis.hardware import detect_hardware
        return detect_hardware(self.store.dir)

    def runtime(self) -> dict:
        if self._rt is not None:
            return self._rt
        if self._factory is not None or self._llama_factory is not None:
            return {"available": True, "version": "injected test factory", "simulated": True}
        from jarvis.inference.gguf_engine import runtime_info
        return runtime_info()

    def _make_engine(self, model_id: str):
        d = self.store.path(model_id)
        if self._factory is not None:
            return self._factory(d)
        from jarvis.inference.gguf_engine import GGUFEngine
        return GGUFEngine(d, llama_factory=self._llama_factory)

    def _try_begin(self, name: str) -> bool:
        if not self._op.acquire(blocking=False):
            return False
        self.current_operation = name
        return True

    def _end(self) -> None:
        self.current_operation = ""
        self._op.release()

    def _set_busy(self, model_id: str, what: str | None) -> None:
        with self._busy_lock:
            if what:
                self._busy[model_id] = what
            else:
                self._busy.pop(model_id, None)

    @staticmethod
    def _fail(kind: str, message: str, **extra) -> dict:
        return {"ok": False, "kind": kind, "message": message, **extra}

    def _busy_result(self) -> dict:
        return self._fail("busy", f"Another brain operation is running ({self.current_operation or 'in progress'}). Wait for it to finish or cancel it.")

    # ------------------------------------------------------------------ inspect an arbitrary local file (read-only; nothing is registered)
    def inspect_file(self, path: str | os.PathLike) -> dict:
        """Header-only look at a local file. Never loads the model and never runs anything from it. Returns facts + compatibility, or a clear rejection."""
        p = Path(str(path)).expanduser()
        try:
            if not p.exists():
                return self._fail("not_found", "That file does not exist.")
            if not p.is_file():
                return self._fail("not_a_file", "That path is not a file. Choose a single .gguf model file.")
            size = p.stat().st_size
        except OSError:
            return self._fail("unreadable", "That file could not be read.")
        if size == 0:
            return self._fail("empty", "That file is empty.")
        try:
            header = gguf_mod.read_header(p)
        except gguf_mod.GGUFError as e:
            return self._fail("not_gguf", f"This is not a usable GGUF model file: {e}", size_bytes=size)
        except OSError:
            return self._fail("unreadable", "That file could not be read.")
        summary = gguf_mod.summarize(header)
        verdict = compat.assess(summary, size, self.hardware(), self.runtime())
        notes = []
        if p.suffix.lower() != ".gguf":
            notes.append("The file name does not end in .gguf, but its contents are a GGUF file.")
        return {"ok": True, "path": str(p.resolve()), "file_name": p.name, "size_bytes": size, "format": f"GGUF v{summary.get('gguf_version')}",
                "gguf": summary, "compat": verdict, "notes": notes,
                "verification": "Not verified: this is a local file of unknown origin. Its checksum can be computed, but there is no trusted reference to compare it with."}

    # ------------------------------------------------------------------ import
    def import_file(self, path, *, name: str | None = None, copy: bool = False, compute_hash: bool = True, cancel=None,
                    progress: Callable[[int, int], None] | None = None, license_note: str = "") -> dict:
        """Add a local model to the library. Default: REGISTER IN PLACE (no copy; the file stays where it is). copy=True copies it into the model
        folder (with the usual free-space checks). A file that is not GGUF, not a chat architecture, or cannot fit this PC is refused with the reason."""
        if not self._try_begin("importing a model"):
            return self._busy_result()
        mid = None
        try:
            info = self.inspect_file(path)
            if not info["ok"]:
                return info
            if info["compat"]["level"] == compat.UNSUPPORTED:
                reasons = " ".join(r["text"] for r in info["compat"]["reasons"] if r["severity"] == "block")
                return self._fail("unsupported", "This model was not imported. " + reasons, compat=info["compat"], gguf=info["gguf"])
            if _flag(cancel):
                return self._fail("cancelled", "Import cancelled. Nothing was added.")
            digest = None
            before = self._stat_key(info["path"])
            if compute_hash:
                try:
                    digest = hash_file(Path(info["path"]), cancel, progress)
                except Cancelled:
                    return self._fail("cancelled", "Import cancelled. Nothing was added.")
                except OSError:
                    return self._fail("unreadable", "The file could not be read while computing its checksum. Nothing was added.")
            if before is None or self._stat_key(info["path"]) != before:
                return self._fail("changed", "The file changed while it was being checked, so nothing was added. Wait until it has finished copying or downloading and try again.")
            base = _slug(name or info["gguf"].get("name") or Path(info["path"]).stem)
            mid = "imp-" + base
            n = 2
            while (self.store.dir / mid).exists():
                mid, n = f"imp-{base}-{n}", n + 1
            mctx = info["gguf"].get("context_length")
            ctx = int(min(mctx or 4096, 4096))
            extra = {"title": (name or info["gguf"].get("name") or Path(info["path"]).stem)[:80], "context_tokens": ctx,
                     "provenance": {"origin": "local-file", "original_path": info["path"], "checksum_computed_locally": bool(digest),
                                    "checksum_reference": None, "note": "Unknown origin. No trusted checksum exists to compare with; the SHA-256 only lets Jarvis "
                                    "notice if this file changes later."},
                     "license_note": license_note[:500]}
            if copy:
                lic_text = license_note.strip() or "Imported by the user from a local file; the license is not known to Project JARVIS. Check the model publisher's terms."
                meta = self.store.install_gguf(info["path"], mid, "unknown", lic_text, source_url="local file", extra_meta={**extra, "origin": "imported", "copied": True})
            else:
                meta = self.store.register_external(info["path"], mid, sha256=digest, license_spdx="unknown", source="local file", extra_meta=extra)
            return {"ok": True, "model_id": mid, "linked": not copy, "copied": copy, "message": ("Registered in place; the file was not copied." if not copy
                    else "Copied into the model folder."), "compat": info["compat"], "gguf": info["gguf"], "sha256": digest,
                    "meta": {k: meta.get(k) for k in ("id", "title", "origin", "size_bytes")}}
        except IntegrityError as e:
            return self._fail("rejected", _safe(str(e)))
        except OSError as e:      # full disk, permissions, antivirus lock...: the store removes its partial files; say what happened
            return self._fail("io_error", "The model could not be added (" + _safe(e) + "). Nothing was added.")
        except Exception as e:      # noqa: BLE001 - a UI worker must get a result, never a traceback
            return self._fail("unexpected", _safe(e))
        finally:
            self._end()

    # ------------------------------------------------------------------ catalog / download
    @staticmethod
    def _stat_key(path) -> tuple[int, int] | None:
        try:
            st = os.stat(path)
            return st.st_size, st.st_mtime_ns
        except OSError:
            return None

    def _catalog_row(self, m, hw, installed_ids: set[str]) -> dict:
        from jarvis.models import selector
        pseudo = {"architecture": m.architecture or None, "quantization": m.quant, "context_length": m.context_tokens}
        verdict = compat.assess(pseudo, m.approx_size_bytes, hw, self.runtime(), m.context_tokens, from_catalog=True)
        verdict["reasons"].insert(0, {"severity": "info", "text": "Estimated from the catalog's published size; the real file header is read and checked after download."})
        meta = self.store.meta(m.id) if m.id in installed_ids else {}
        confirmed = not m.metadata_status.startswith("from-publisher-knowledge")
        installed = m.id in installed_ids
        return {
            "id": m.id, "name": m.title, "kind": "jarvis-catalog (third-party open-weight model)",
            "source": {"repo": m.repo, "host": "huggingface.co", "revision": (meta.get("source_revision") or "not recorded until downloaded")},
            "architecture": {"value": m.architecture or "unknown", "basis": "expected by the catalog; checked against the file's own header at install"},
            "format": "GGUF", "quantization": m.quant,
            "size": {"bytes": m.approx_size_bytes, "basis": ("verified: size of the installed file" if installed and meta.get("size_bytes") else
                                                            "publisher's page" if confirmed else "estimate: not confirmed against the publisher")},
            "license": {"spdx": m.license_spdx, "url": m.license_url, "basis": "from the publisher's page" if confirmed else "NOT confirmed here; check the publisher's page before use"},
            "memory_need_gb": {"value": selector.memory_need_gb(m), "basis": "estimate"},
            "compatibility": verdict, "installed": installed,
            "install_status": "installed" if installed else "not installed",
            "verification": ("integrity checked at install against the host's checksum (same host: detects a damaged download, does not prove the host trustworthy)"
                             if meta.get("checksum_reference") else "not installed, nothing verified yet"),
            "metadata_confirmed": confirmed, "auto_recommend": m.auto_recommend}

    def catalog(self) -> list[dict]:
        from jarvis.models import catalog as cat
        hw = self.hardware()
        ids = {x.get("id") for x in self.store.installed()}
        return [self._catalog_row(m, hw, ids) for m in cat.BUILTIN]

    def download_preflight(self, model_id: str) -> dict:
        """Everything the person must see BEFORE a download is started: size, licence, disk and memory warnings. Downloads nothing."""
        from jarvis.models import catalog as cat
        from jarvis.models.diskspace import free_bytes
        try:
            m = cat.get(model_id)
        except KeyError:
            return self._fail("unknown_model", f"'{model_id}' is not in the model list.")
        hw = self.hardware()
        need = int(m.approx_size_bytes * 1.2) + 500_000_000
        try:
            free = free_bytes(self.store.dir)
        except Exception:      # noqa: BLE001
            free = None
        row = self._catalog_row(m, hw, {x.get("id") for x in self.store.installed()})
        warnings = []
        if free is not None and free < need:
            warnings.append(f"Not enough free disk space: {free / 1e9:.1f} GB free, about {need / 1e9:.1f} GB needed.")
        if m.approx_size_bytes >= 2_000_000_000:
            warnings.append(f"This is a large download (about {m.approx_size_bytes / 1e9:.1f} GB).")
        if row["compatibility"]["level"] != compat.COMPATIBLE:
            warnings += [r["text"] for r in row["compatibility"]["reasons"] if r["severity"] in ("warn", "block")]
        if not row["metadata_confirmed"]:
            warnings.append("The size and license shown were not confirmed against the publisher's page.")
        return {"ok": free is None or free >= need, "model_id": m.id, "name": m.title, "download_gb": round(m.approx_size_bytes / 1e9, 2),
                "needs_free_gb": round(need / 1e9, 1), "free_gb": None if free is None else round(free / 1e9, 1), "license": m.license_spdx,
                "license_url": m.license_url, "source": f"https://huggingface.co/{m.repo}", "warnings": warnings, "compat": row["compatibility"],
                "verification": ("After download Jarvis compares the file's SHA-256 with the value the host (huggingface.co) publishes. That detects a damaged or "
                                 "incomplete download; because the checksum comes from the same host, it does not prove the host itself is trustworthy."),
                "nothing_downloaded": True, "will_activate": False}

    def download(self, model_id: str, *, consent: bool = False, cancel=None, progress: Callable[[int, int], None] | None = None) -> dict:
        """Download a catalog model. Needs consent=True (the person pressed Download after reading download_preflight). Never activates it."""
        if not consent:
            return self._fail("consent_required", "The download was not started: it needs your explicit confirmation. Nothing was downloaded.")
        if not self._try_begin("downloading a model"):
            return self._busy_result()
        self._set_busy(model_id, "downloading")
        try:
            eng = self._setup or self._default_setup()
            res = eng.download(model_id, progress, cancel, activate=False)
            if res.get("ok"):
                res["message"] = "Downloaded and checked. It is installed but NOT active; test it or activate it from Installed Brains."
            return res
        except Exception as e:      # noqa: BLE001
            return self._fail("unexpected", f"{type(e).__name__}: {e}")
        finally:
            self._set_busy(model_id, None)
            self._end()

    def _default_setup(self):
        from jarvis.app.setup_engine import SetupEngine
        return SetupEngine(self.cfg, self.store, self.policy)

    # ------------------------------------------------------------------ installed brains
    def _model_facts(self, meta: dict) -> dict:
        g = meta.get("gguf") if isinstance(meta.get("gguf"), dict) else {}
        return g

    def _status(self, mid: str, intact: bool, compat_level: str, last: dict | None) -> str:
        with self._busy_lock:
            if mid in self._busy:
                return self._busy[mid]
        if not intact:
            return "failed"
        if compat_level == compat.UNSUPPORTED:
            return "incompatible"
        if last is None:
            return "unverified"
        return "ready" if last.get("ok") else "failed"

    def installed_brains(self) -> list[dict]:
        """One record per installed model for the Installed Brains list. Quick checks only (sizes, GGUF signature); no hashing, no loading."""
        hw, rt = self.hardware(), self.runtime()
        st = self.load_state()
        active, prev = self.store.active(), st.get("previous_id")
        rep = self.store.installed_report()
        out = []
        for meta in rep["models"]:
            mid = meta.get("id", "")
            try:
                self.store._check_id(mid)
            except ValueError:
                continue
            g = self._model_facts(meta)
            size = meta.get("size_bytes") or 0
            ok, problems = self.store.verify(mid, quick=True)
            verdict = compat.assess(g, size, hw, rt, int(meta.get("context_tokens") or 0) or None) if g and size else None
            level = verdict["level"] if verdict else compat.WARNINGS
            last = (st.get("last_tests") or {}).get(mid)
            linked = isinstance(meta.get("linked"), dict)
            support = (verdict or {}).get("support") or {"tier": "uncertain", "verified_on_this_pc": False, "text": "Not assessed. NOT verified: it has not been loaded here."}
            if last and last.get("ok") and last.get("real") and ok and verdict and verdict["level"] != compat.UNSUPPORTED:
                support = {"tier": "verified", "verified_on_this_pc": True,
                           "text": f"Verified on this PC: it loaded and answered a real test question ({str(last.get('at', ''))[:10]})."}
            out.append({
                "id": mid, "name": meta.get("title") or meta.get("catalog_id") or mid, "origin": meta.get("origin", "catalog" if meta.get("catalog_id") else "unknown"),
                "linked": linked, "path": (meta.get("linked") or {}).get("path") if linked else str(self.store.path(mid) / "model.gguf"),
                "size_bytes": size, "architecture": g.get("architecture"), "quantization": g.get("quantization"), "format": "GGUF",
                "license": meta.get("license"), "source": meta.get("source_url"), "source_revision": meta.get("source_revision"),
                "verified_origin": bool(meta.get("verified_origin")), "provenance": meta.get("provenance"),
                "verification": ("checked against the host's checksum when downloaded (same host)" if meta.get("verified_origin") else
                                 "not verified against any trusted reference" if meta.get("origin") == "imported" or linked else "unverified"),
                "compat": verdict, "support": support, "intact": ok, "integrity_problems": problems,
                "status": self._status(mid, ok, level, last), "last_test": last, "active": mid == active, "previous": mid == prev,
                "context_tokens": meta.get("context_tokens")})
        for d in rep["damaged"]:
            out.append({"id": d["id"], "name": d["id"], "status": "failed", "damaged": True, "reason": d["reason"], "active": False})
        return out

    def summary(self) -> dict:
        """The Active Brain card."""
        st = self.load_state()
        active = self.store.active()
        brains = {b["id"]: b for b in self.installed_brains()}
        a = brains.get(active) if active else None
        eng = self.host.loaded_engine()
        return {"active_id": active, "active": a, "engine_loaded": eng is not None, "previous_id": st.get("previous_id"),
                "message": None if a else NO_BRAIN_MESSAGE, "last_test": a.get("last_test") if a else None,
                "operation": self.current_operation or None}

    # ------------------------------------------------------------------ test + activate
    def _generate(self, engine, cancel=None) -> dict:
        """One REAL generation through the engine. Success needs non-empty text. Content quality is not judged."""
        t0 = self._clock()
        before = dict(getattr(engine, "diag", {}) or {})
        text = engine.chat([{"role": "user", "content": TEST_PROMPT}], max_tokens=48, temperature=0.0)
        secs = max(self._clock() - t0, 1e-6)
        toks = int(((getattr(engine, "diag", {}) or {}).get("completion_tokens", 0)) - before.get("completion_tokens", 0))
        text = (text or "").strip()
        printable = any(ch.isprintable() and not ch.isspace() for ch in text)
        if not text or not printable:
            raise RuntimeError("the model produced no readable text")
        return {"text": text[:300], "seconds": round(secs, 2), "tokens": toks or None, "tokens_per_second": round(toks / secs, 2) if toks else None}

    def _gate(self, model_id: str, accept_warnings: bool) -> tuple[dict | None, dict | None]:
        """Checks before anything is loaded. Returns (refusal, brain record)."""
        rec = next((b for b in self.installed_brains() if b["id"] == model_id), None)
        if rec is None:
            return self._fail("not_installed", f"'{model_id}' is not in your brain library."), None
        if rec.get("damaged"):
            return self._fail("damaged", "This entry's record is damaged and cannot be used. Remove it from the library."), rec
        if not rec["intact"]:
            return self._fail("integrity", "The model file failed its check: " + "; ".join(rec["integrity_problems"]) + ". It was not loaded.", problems=rec["integrity_problems"]), rec
        c = rec["compat"]
        if c is None:
            return self._fail("no_header", "No file details were recorded for this model, so compatibility cannot be judged. Remove and add it again."), rec
        if c["level"] == compat.UNSUPPORTED:
            return self._fail("unsupported", "This model cannot be used on this PC: " + " ".join(r["text"] for r in c["reasons"] if r["severity"] == "block"), compat=c), rec
        if c["level"] == compat.WARNINGS and not accept_warnings:
            return self._fail("needs_confirmation", "This model may be too heavy or uncertain on this PC. Review the warnings, then confirm to continue.",
                              warnings=[r["text"] for r in c["reasons"] if r["severity"] == "warn"], compat=c), rec
        return None, rec

    def test_brain(self, model_id: str, *, accept_warnings: bool = False, cancel=None) -> dict:
        """Load the model and make it generate for real, then record the result. The ACTIVE model is tested through the engine already running
        (no second copy in memory). Any other model is loaded alone; if a different brain is running and memory is short, it is refused rather than
        loading two large models at once."""
        if not self._try_begin("testing a brain"):
            return self._busy_result()
        self._set_busy(model_id, "checking")
        eng = None
        try:
            refusal, rec = self._gate(model_id, accept_warnings)
            if refusal:
                return refusal
            running = self.host.loaded_engine()
            if model_id == self.store.active() and running is not None:
                try:
                    g = self._generate(running, cancel)
                except Exception as e:      # noqa: BLE001
                    res = self._fail("generation_failed", f"The running model failed to answer: {_safe(type(e).__name__)}.")
                    self._record_test(model_id, {"ok": False, "at": _now(), "reason": res["message"]})
                    return res
                rec_t = {"ok": True, "at": _now(), "mode": "active engine", **g}
                self._record_test(model_id, rec_t)
                return {"ok": True, "model_id": model_id, "test": rec_t, "message": "The brain answered a real question."}
            if running is not None and not self._fits_alongside(rec):
                return self._fail("memory", "A different brain is running and there is not enough free memory to test this one beside it. Deactivate the running "
                                  "brain (or make this one active) first; Jarvis will not hold two large models in memory at once.")
            if _flag(cancel):
                return self._fail("cancelled", "Test cancelled before loading.")
            try:
                eng = self._make_engine(model_id)
                if _flag(cancel):
                    return self._fail("cancelled", "Test cancelled after loading; the model was released.")
                g = self._generate(eng, cancel)
            except MemoryError:
                res = self._fail("out_of_memory", "Not enough memory to load this model. Close other programs or choose a smaller model.")
                self._record_test(model_id, {"ok": False, "at": _now(), "reason": res["message"]})
                return res
            except Exception as e:      # noqa: BLE001
                res = self._fail("load_failed", f"The model could not be loaded or did not answer ({_safe(e, 200)}).")
                self._record_test(model_id, {"ok": False, "at": _now(), "reason": res["message"]})
                return res
            rec_t = {"ok": True, "at": _now(), "mode": "temporary load", "load_seconds": round(getattr(eng, "load_seconds", 0) or 0, 2), **g}
            self._record_test(model_id, rec_t)
            return {"ok": True, "model_id": model_id, "test": rec_t, "message": "The brain loaded and answered a real question. It was released again."}
        finally:
            self._release(eng)
            self._set_busy(model_id, None)
            self._end()

    @staticmethod
    def _release(eng) -> None:
        close = getattr(eng, "close", None)
        if eng is not None and callable(close):
            try:
                close()
            except Exception:      # noqa: BLE001
                pass

    def _fits_alongside(self, rec: dict) -> bool:
        avail = getattr(self.hardware(), "ram_available_gb", None)
        need = (rec.get("compat") or {}).get("estimate", {}).get("total_gb")
        return avail is not None and need is not None and avail >= 1.25 * need

    def activate(self, model_id: str, *, accept_warnings: bool = False, cancel=None) -> dict:
        """Transactional switch. The active brain changes ONLY after the candidate loaded and generated real text. On any failure the candidate is
        released and the previous brain keeps (or gets back) its place. Conversation, notes, memory, persona and permissions are not touched."""
        if not self._try_begin("switching brains"):
            return self._busy_result()
        self._set_busy(model_id, "checking")
        cand = None
        prev_id = self.store.active()
        prev_engine = self.host.loaded_engine()
        released_prev = False
        try:
            refusal, rec = self._gate(model_id, accept_warnings)
            if refusal:
                return refusal
            if model_id == prev_id and prev_engine is not None:
                return {"ok": True, "model_id": model_id, "message": "This brain is already active and loaded.", "unchanged": True}
            if _flag(cancel):
                return self._fail("cancelled", "Switch cancelled. Nothing changed.")
            # memory plan: keep the running brain while loading the candidate only if the free memory clearly covers the candidate; else release first
            if prev_engine is not None and not self._fits_alongside(rec):
                self.host.release_engine()
                released_prev = True
            try:
                cand = self._make_engine(model_id)
                g = self._generate(cand, cancel)
                if _flag(cancel):
                    raise Cancelled()
            except Cancelled:
                return self._recover(prev_id, released_prev, "cancelled", "Switch cancelled. The new model was released and your previous brain was kept.", cand)
            except MemoryError:
                return self._recover(prev_id, released_prev, "out_of_memory", "Not enough memory to load this model, so nothing was switched. Your previous brain was kept.", cand,
                                     test_for=model_id)
            except Exception as e:      # noqa: BLE001
                return self._recover(prev_id, released_prev, "load_failed", f"The new model could not be loaded or did not answer ({_safe(e, 200)}). "
                                     "Your previous brain was kept.", cand, test_for=model_id)
            # success. Order matters: move the POINTER first (it re-verifies the file and can fail); only then swap the engine in the host. A failure
            # before the swap leaves the previous brain exactly as it was; a failure of the swap puts the pointer back.
            try:
                self.store.set_active(model_id)
            except Exception as e:      # noqa: BLE001
                return self._recover(prev_id, released_prev, "load_failed", f"The new model loaded but could not be selected ({_safe(e, 200)}). Your previous brain was kept.",
                                     cand, test_for=model_id)
            try:
                if prev_engine is not None and not released_prev:
                    self.host.release_engine()
                    released_prev = True
                self.host.install_engine(cand, model_id)
            except Exception as e:      # noqa: BLE001
                self._restore_pointer(prev_id)
                return self._recover(prev_id, released_prev, "load_failed", f"The new model could not be handed to the assistant ({_safe(e, 200)}). Your previous brain was kept.",
                                     cand, test_for=model_id)
            cand = None                                  # ownership moved to the host: it must not be closed here
            st = self.load_state()
            if prev_id and prev_id != model_id:
                st["previous_id"] = prev_id
            self._save_state(st)
            self._record_test(model_id, {"ok": True, "at": _now(), "mode": "activation", **g})
            return {"ok": True, "model_id": model_id, "previous_id": prev_id, "test": g,
                    "message": "Switched. The new brain loaded and answered a real question. Your conversations, notes and settings were not changed."}
        except IntegrityError as e:
            return self._recover(prev_id, released_prev, "integrity", _safe(str(e)), cand)
        finally:
            self._release(cand)
            self._set_busy(model_id, None)
            self._end()

    def _restore_pointer(self, prev_id: str | None) -> None:
        try:
            if prev_id:
                self.store.set_active(prev_id)
            else:
                (self.store.dir / "active.json").unlink(missing_ok=True)
        except Exception:      # noqa: BLE001 - best effort; the result still reports active_id
            pass

    def _recover(self, prev_id: str | None, released_prev: bool, kind: str, message: str, cand, test_for: str | None = None) -> dict:
        """Failure path of activate(): release the candidate, bring the previous brain back if it had been released, record the failure."""
        self._release(cand)
        if test_for:
            self._record_test(test_for, {"ok": False, "at": _now(), "reason": message})
        restored = None
        if released_prev and prev_id:
            try:
                self.host.install_engine(self._make_engine(prev_id), prev_id)
                restored = True
            except Exception as e:      # noqa: BLE001
                restored = False
                message += (f" The previous brain could not be reloaded either ({_safe(type(e).__name__)}); it is still selected and its file was not touched. "
                            "Use Activate on it to try again.")
        return {"ok": False, "kind": kind, "message": message, "previous_id": prev_id, "previous_restored": restored,
                "active_id": self.store.active()}

    def deactivate(self) -> dict:
        """Release the running brain and clear the selection. Nothing is deleted. Chat then says clearly that no brain is active."""
        if not self._try_begin("deactivating"):
            return self._busy_result()
        try:
            cur = self.store.active()
            self.host.release_engine()
            (self.store.dir / "active.json").unlink(missing_ok=True)
            st = self.load_state()
            if cur:
                st["previous_id"] = cur
            self._save_state(st)
            return {"ok": True, "previous_id": cur, "message": "The brain was released from memory. " + NO_BRAIN_MESSAGE}
        finally:
            self._end()

    def return_to_previous(self, **kw) -> dict:
        prev = self.load_state().get("previous_id")
        if not prev:
            return self._fail("no_previous", "There is no previous brain to go back to.")
        return self.activate(prev, **kw)

    # ------------------------------------------------------------------ settings, removal
    def set_context(self, model_id: str, n_ctx: int) -> dict:
        """Per-model context window (memory use grows with it). Applied the next time the model is loaded."""
        if type(n_ctx) is not int or not CONTEXT_MIN <= n_ctx <= CONTEXT_MAX:
            return self._fail("bad_value", f"The context size must be a whole number between {CONTEXT_MIN} and {CONTEXT_MAX}.")
        meta = self.store.meta(model_id)
        if not meta:
            return self._fail("not_installed", "That model is not in the library.")
        mctx = (meta.get("gguf") or {}).get("context_length")
        if mctx and n_ctx > mctx:
            return self._fail("bad_value", f"This model supports at most {mctx} tokens of context.")
        meta["context_tokens"] = n_ctx
        p = self.store.path(model_id) / "meta.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        tmp.replace(p)
        return {"ok": True, "message": f"Context set to {n_ctx} tokens. It applies the next time this brain is loaded."}

    def remove_preview(self, model_id: str) -> dict:
        """What removal would do, for the confirmation dialog. Changes nothing."""
        try:
            self.store._check_id(model_id)
        except ValueError:
            return self._fail("bad_id", "That is not a valid model id.")
        meta = self.store.meta(model_id)
        if not meta:
            return self._fail("not_installed", "That model is not in the library.")
        linked = isinstance(meta.get("linked"), dict)
        return {"ok": True, "id": model_id, "linked": linked, "is_active": model_id == self.store.active(),
                "file": (meta.get("linked") or {}).get("path") if linked else str(self.store.path(model_id) / "model.gguf"),
                "library_removal": "Removes the model from Jarvis's list" + (". Your original file stays where it is." if linked else " and deletes the copy Jarvis stored."),
                "file_deletion": ("Also deletes YOUR file at the path shown. This cannot be undone." if linked else None)}

    def remove(self, model_id: str, *, confirmed: bool = False, delete_file: bool = False) -> dict:
        if not confirmed:
            return self._fail("confirmation_required", "Removal needs your explicit confirmation. Nothing was removed.")
        if not self._try_begin("removing a model"):
            return self._busy_result()
        try:
            try:
                self.store._check_id(model_id)
            except ValueError:
                return self._fail("bad_id", "That is not a valid model id.")
            if model_id == self.store.active():
                return self._fail("is_active", "This brain is active. Deactivate it (or switch to another) before removing it.")
            if not self.store.meta(model_id) and not (self.store.path(model_id)).exists():
                return self._fail("not_installed", "That model is not in the library.")
            try:
                self.store.remove(model_id, delete_linked_file=delete_file)
            except IntegrityError as e:
                return self._fail("refused", _safe(str(e)))
            except OSError as e:
                return self._fail("io_error", "The model could not be removed (" + _safe(e) + ").")
            if self.store.path(model_id).exists():     # the store ignores rmtree errors (locked file, antivirus); never claim success then
                return self._fail("not_removed", "Some of this model's files could not be deleted (they may be in use). It is still in the library; close other programs "
                                  "that may be using it and try again.")
            st = self.load_state()
            st.get("last_tests", {}).pop(model_id, None)
            if st.get("previous_id") == model_id:
                st.pop("previous_id", None)
            self._save_state(st)
            return {"ok": True, "message": "Removed from the library." + (" The file was deleted." if delete_file else " Your files were not deleted unless they were Jarvis's own copy.")}
        finally:
            self._end()

    # ------------------------------------------------------------------ diagnostics
    def diagnostics(self) -> dict:
        hw, rt = self.hardware(), self.runtime()
        rep = self.store.installed_report()
        return {"hardware": {"os": hw.os, "cpu_threads": hw.cpu_threads, "ram_total_gb": hw.ram_gb, "ram_available_gb": getattr(hw, "ram_available_gb", None),
                             "free_disk_gb": hw.free_disk_gb, "gpu": hw.gpu_name, "vram_gb": hw.vram_gb, "avx2": hw.avx2, "notes": list(hw.notes)},
                "runtime": rt, "models_folder": str(self.store.dir), "models_folder_bytes": self.store.disk_usage_bytes(),
                "installed": len(rep["models"]), "damaged_entries": rep["damaged"], "rollback_copies": self.store.rollback_copies(),
                "staging_leftovers": self.store.staging_leftovers(), "engine_loaded": self.host.loaded_engine() is not None,
                "operation": self.current_operation or None,
                "cloud": "Project JARVIS never falls back to a cloud model or paid API. If no brain works, chat says so.",
                "estimates": "Memory figures are estimates from file size, quantization, context and a fixed overhead, not measurements."}
