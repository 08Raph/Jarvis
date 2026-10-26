"""Setup wizard engine (Prompt 20): everything the first-run wizard does, with no user-interface code in it so it can be tested without a window.
Steps: system -> model -> download -> runtime/verify -> finish. Every claim in the result is a measurement or is labelled as not measured.

Honesty rules enforced here (and by tests):
  * A setup that ran with an injected (fake) model factory can never mark itself complete: `real` is False and `completed` stays False.
  * GPU mode is chosen only when a real GPU run was measurably faster than the CPU run AND GPU activity was observed; otherwise CPU is kept
    and the reason is recorded. A GPU that fails to load falls back to CPU with the error text, never silently to anything online.
  * Downloads need explicit consent (callable), check disk space first, support cancel and bounded retry, and are checksum-verified by the store.
"""
from __future__ import annotations

import json
import platform
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from jarvis import version as V
from jarvis.config import Config
from jarvis.models import catalog, selector

STEPS = ("system", "model", "download", "verify", "finish")
PROMPT = "Write two short sentences about the sea."
STATE_FILE = "setup_state.json"
REPORT_FILE = "setup_report.json"


class Cancelled(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _windows_ok() -> tuple[bool | None, str]:
    if platform.system() != "Windows":
        return None, f"not Windows ({platform.system()}): this run is a development/test environment, Windows support is not checked here"
    have = tuple(int(x) for x in (platform.version().split(".") + ["0", "0", "0"])[:3] if x.isdigit())
    need = tuple(int(x) for x in V.MIN_WINDOWS.split("."))
    ok = have >= need
    return ok, f"Windows {platform.version()} ({platform.machine()})" + ("" if ok else f" is older than the supported minimum {V.MIN_WINDOWS}")


class SetupEngine:
    def __init__(self, cfg: Config, store, policy, *, llama_factory=None, sleep: Callable[[float], None] = time.sleep,
                 hardware=None, runtime: dict | None = None):
        self.cfg, self.store, self.policy = cfg, store, policy
        self.factory = llama_factory           # tests only; None = the real llama-cpp-python runtime
        self._hw, self._rt = hardware, runtime  # tests only: simulated hardware / runtime facts
        self._sleep = sleep
        self.real = llama_factory is None and hardware is None and runtime is None

    def _hardware(self):
        from jarvis.hardware import detect_hardware
        return self._hw or detect_hardware(self.cfg.data_dir if Path(self.cfg.data_dir).exists() else ".")

    def _runtime(self) -> dict:
        from jarvis.inference.gguf_engine import runtime_info
        if self._rt is not None:
            return self._rt
        return runtime_info() if self.factory is None else {"available": True, "version": "injected test factory", "simulated": True}

    # ------------------------------------------------------------------ state
    @property
    def state_path(self) -> Path:
        return Path(self.cfg.data_dir) / STATE_FILE

    def load_state(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def needs_setup(self) -> bool:
        """True until a REAL verified setup finished for a model that is still installed and intact."""
        st = self.load_state()
        if not st.get("completed") or st.get("version_major") != V.__version__.split(".")[0]:
            return True
        mid = st.get("model_id")
        if not mid or self.store.active() != mid:
            return True
        ok, _ = self.store.verify(mid, quick=True)
        return not ok

    def _save_state(self, d: dict) -> None:
        Path(self.cfg.data_dir).mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.state_path)

    # ------------------------------------------------------------------ step 1
    def check_system(self) -> dict:
        hw, rt = self._hardware(), self._runtime()
        acc = selector.acceleration_state(hw, rt)
        win_ok, win_text = _windows_ok()
        blockers, warnings = [], []
        if win_ok is False:
            blockers.append(win_text)
        if not rt.get("available"):
            blockers.append("The local AI runtime (llama-cpp-python) is not available in this installation: " + str(rt.get("error", "unknown reason")))
        if hw.ram_gb is not None and hw.ram_gb < 4:
            warnings.append(f"Only {hw.ram_gb:.1f} GB of RAM: only the smallest model is likely to run, and slowly.")
        if hw.free_disk_gb is not None and hw.free_disk_gb < 3:
            blockers.append(f"Only {hw.free_disk_gb:.1f} GB of free disk space; a model needs about 2 GB or more.")
        if hw.avx2 is False:
            warnings.append(hw.notes[0] if hw.notes else "This CPU does not report AVX2.")
        if acc["gpu_detected"] and not acc["backend_available"]:
            warnings.append("A GPU was detected, but this installation has no GPU-enabled AI runtime, so models will run on the CPU.")
        return {"windows": win_text, "windows_ok": win_ok, "hardware": hw.__dict__, "runtime": rt, "acceleration": acc,
                "tier": selector.hardware_tier(hw.ram_gb, hw.vram_gb, acc["backend_available"]), "blockers": blockers, "warnings": warnings,
                "can_continue": not blockers, "gpu_note": acc["message"]}

    # ------------------------------------------------------------------ step 2
    def recommend(self, prefer: str = "balanced") -> dict:
        hw, rt = self._hardware(), self._runtime()
        ch = selector.select(hw, rt, prefer=prefer)
        acc = selector.acceleration_state(hw, rt)
        rows = []
        for m in catalog.BUILTIN:
            ev = selector.evaluate(m, hw, acc)
            rows.append({"id": m.id, "title": m.title, "license": m.license_spdx, "license_url": m.license_url, "download_gb": round(m.approx_size_bytes / 1e9, 2),
                         "memory_need_gb_estimate": ev["need_gb"], "fits": bool(ev["cpu_fits"] or ev["gpu_fits"]) and ev["disk_ok"], "disk_ok": ev["disk_ok"],
                         "metadata_confirmed": not m.metadata_status.startswith("from-publisher-knowledge"), "note": m.note,
                         "installed": m.id in {x["id"] for x in self.store.installed()}})
        return {"recommended": ch.model.id if ch.model else None, "mode_estimate": ch.mode, "reasons": ch.reasons, "alternatives": ch.alternatives,
                "models": rows, "tier": ch.tier, "note": "Memory figures are estimates. Only models whose size and licence were read from the publisher are recommended automatically."}

    # ------------------------------------------------------------------ step 3
    def download(self, model_id: str, progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None,
                 attempts: int = 3) -> dict:
        try:
            m = catalog.get(model_id)
        except KeyError:
            return {"ok": False, "kind": "unknown_model", "message": f"'{model_id}' is not in the model list."}
        try:
            if self.store.active() == m.id and self.store.verify(m.id)[0]:
                return {"ok": True, "already_installed": True, "model_id": m.id}
        except Exception:     # noqa: BLE001 - a damaged install is simply downloaded again
            pass
        free = shutil.disk_usage(self.store.dir).free
        need = int(m.approx_size_bytes * 1.2) + 500_000_000
        if free < need:
            return {"ok": False, "kind": "disk_space", "message": f"Not enough free disk space: {free / 1e9:.1f} GB free, about {need / 1e9:.1f} GB needed."}
        from jarvis.models.manager import IntegrityError
        from jarvis.net.policy import NetworkDisabled

        def prog(done: int, total: int) -> None:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if progress:
                progress(done, total)
        last = ""
        for n in range(1, attempts + 1):
            try:
                with self.policy.grant_once("model_download"):
                    meta = self.store.download_gguf(m, prog)
                self.store.set_active(meta["id"])
                return {"ok": True, "model_id": meta["id"], "attempts": n, "sha256_verified_against_publisher": True}
            except Cancelled:
                return {"ok": False, "kind": "cancelled", "message": "Download cancelled. The partial file was removed."}
            except IntegrityError as e:
                return {"ok": False, "kind": "integrity", "message": f"The downloaded file was rejected and not installed: {e}"}
            except NetworkDisabled as e:
                return {"ok": False, "kind": "network_blocked", "message": f"Network access for model download is switched off: {e}"}
            except Exception as e:     # noqa: BLE001 - network failures arrive as many exception types
                last = f"{type(e).__name__}: {e}"
                if cancel is not None and cancel.is_set():
                    return {"ok": False, "kind": "cancelled", "message": "Download cancelled."}
                if n < attempts:
                    self._sleep(2.0 * n)
        return {"ok": False, "kind": "network", "message": f"The download failed after {attempts} tries ({last}). Check the internet connection and press Retry. Nothing was installed."}

    # ------------------------------------------------------------------ step 4
    def verify(self, model_id: str, cancel: threading.Event | None = None) -> dict:
        """Load the actual model, send a real prompt, measure CPU (and GPU when a GPU backend exists), choose and SAVE the mode, then re-test the way the app will run."""
        from jarvis.eval import winvalidate
        from jarvis.inference import accel
        from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable
        res: dict = {"model_id": model_id, "real": self.real, "checks": [], "ok": False}

        def check(name: str, ok: bool, detail: str = "") -> bool:
            res["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
            return bool(ok)
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        ok, problems = self.store.verify(model_id)
        if not check("model file integrity (full SHA-256)", ok, "; ".join(problems) or "matches the recorded checksum"):
            return res
        rt = self._runtime()
        if not check("AI runtime present", rt.get("available"), str(rt.get("error", rt.get("version", "")))):
            res["recovery"] = "Reinstall Project JARVIS; the AI runtime is part of the installer."
            return res
        mdir = self.store.path(model_id)

        def run_once(layers: int):
            eng = GGUFEngine(mdir, llama_factory=self.factory, n_gpu_layers=layers)
            with winvalidate.Sampler() as s:
                g = winvalidate._gen(eng, [{"role": "user", "content": PROMPT}], 64)
            return eng, g, s.summary()
        try:
            eng, g, rs = run_once(0)
        except RuntimeUnavailable as e:
            check("model loads on CPU", False, str(e))
            res["recovery"] = "The model could not be loaded. If memory is short, close other programs or choose a smaller model."
            return res
        except Exception as e:     # noqa: BLE001
            check("CPU generation", False, f"{type(e).__name__}: {e}")
            return res
        check("model loads on CPU", True, f"{eng.load_seconds:.1f} s")
        check("CPU generated a reply", bool(g["text"].strip()), f"{g['tokens']} tokens, {g['tokens_per_second']} tokens/s")
        res["cpu"] = {k: g[k] for k in ("seconds", "first_token_seconds", "tokens", "tokens_per_second")} | {"load_seconds": round(eng.load_seconds, 2), "resources": rs}
        res["sample_reply"] = g["text"][:300]
        layers, gpu_note, verified = 0, "CPU mode.", False
        acc = selector.acceleration_state(self._hardware(), rt)
        res["gpu"] = {"detected": acc["gpu_detected"], "backend_available": acc["backend_available"], "tried": False, "verified": False}
        if acc["gpu_detected"] and acc["backend_available"]:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            res["gpu"]["tried"] = True
            try:
                geng, gg, grs = run_once(-1)
                res["gpu"].update(load_seconds=round(geng.load_seconds, 2), tokens_per_second=gg["tokens_per_second"], resources=grs)
                check("model loads with GPU offload", True, f"{geng.load_seconds:.1f} s")
                util = grs.get("gpu_util_max_percent") or 0
                faster = bool(gg["tokens_per_second"] and g["tokens_per_second"] and gg["tokens_per_second"] > 1.2 * g["tokens_per_second"])
                verified = bool(gg["text"].strip()) and faster and util > 0
                res["gpu"]["verified"] = verified
                if verified:
                    layers, gpu_note = -1, f"GPU acceleration verified: {gg['tokens_per_second']} vs {g['tokens_per_second']} tokens/s (CPU), GPU activity {util:.0f}%."
                else:
                    gpu_note = (f"GPU loaded but was not measurably faster with visible GPU activity (GPU {gg['tokens_per_second']} vs CPU {g['tokens_per_second']} "
                                f"tokens/s, GPU activity {util:.0f}%), so CPU mode is kept.")
            except Exception as e:     # noqa: BLE001 - any GPU failure falls back to the CPU, with the reason
                res["gpu"]["error"] = f"{type(e).__name__}: {e}"
                check("model loads with GPU offload", False, res["gpu"]["error"])
                gpu_note = f"GPU start-up failed ({type(e).__name__}); CPU mode is used instead. Nothing was sent anywhere."
        elif acc["gpu_detected"]:
            gpu_note = acc["message"]
        else:
            gpu_note = "No supported GPU detected; CPU mode."
        accel.save(self.store.dir, layers, verified=verified, evidence={"cpu_tps": res["cpu"]["tokens_per_second"], "gpu": res["gpu"]})
        # final: exactly the way the app will load it (no explicit layers), and confirm the saved mode is what it used
        try:
            app_eng = GGUFEngine(mdir, llama_factory=self.factory)
            out = app_eng.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=16, temperature=0.0)
            check("the app's own engine answers", bool(out.strip()), out.strip()[:60])
            check("configured mode matches what was verified", app_eng.gpu_layers_requested == layers,
                  f"engine used {app_eng.gpu_layers_requested} GPU layers, setup chose {layers}")
        except Exception as e:     # noqa: BLE001
            check("the app's own engine answers", False, f"{type(e).__name__}: {e}")
        res.update(mode="gpu" if layers else "cpu", gpu_layers=layers, gpu_verified=verified, gpu_note=gpu_note, cpu_fallback_available=True)
        res["ok"] = all(c["ok"] for c in res["checks"] if not c["name"].startswith("model loads with GPU"))
        return res

    # ------------------------------------------------------------------ orchestration
    def run(self, model_id: str | None = None, *, confirm_download: Callable[[dict], bool], progress: Callable[[str, float | None, str], None] | None = None,
            cancel: threading.Event | None = None) -> dict:
        """The whole wizard. `confirm_download(info)` must return True after showing the user the size and licence. Returns a report; never raises for expected failures."""
        say = progress or (lambda step, frac, msg: None)
        rep: dict = {"started": _now(), "version": V.__version__, "steps": {}, "real": self.real, "completed": False}
        try:
            say("system", None, "Checking this PC...")
            rep["steps"]["system"] = sysr = self.check_system()
            if not sysr["can_continue"]:
                rep["failed"] = {"step": "system", "message": "; ".join(sysr["blockers"])}
                return self._finish(rep)
            say("model", None, "Choosing a model...")
            rep["steps"]["model"] = rec = self.recommend()
            mid = model_id or rec["recommended"]
            if not mid:
                rep["failed"] = {"step": "model", "message": "No model in the list fits this PC's memory and disk space: " + "; ".join(rec["reasons"])}
                return self._finish(rep)
            m = catalog.get(mid)
            row = next(r for r in rec["models"] if r["id"] == mid)
            rep["model_id"] = mid
            if not row["installed"]:
                info = {"model": m.title, "id": m.id, "download_gb": row["download_gb"], "license": m.license_spdx, "license_url": m.license_url,
                        "fits": row["fits"], "metadata_confirmed": row["metadata_confirmed"]}
                if not row["fits"]:
                    rep["failed"] = {"step": "model", "message": f"{m.title} will not fit this PC (about {row['memory_need_gb_estimate']} GB needed, estimate). It was not downloaded and not replaced."}
                    return self._finish(rep)
                if not confirm_download(info):
                    rep["failed"] = {"step": "download", "message": "Download not approved. Nothing was downloaded."}
                    return self._finish(rep)
            say("download", 0.0, f"Downloading {m.title}...")
            rep["steps"]["download"] = dl = self.download(mid, lambda d, t: say("download", d / t if t else None, f"{d / 1e6:,.0f} / {t / 1e6:,.0f} MB"), cancel)
            if not dl["ok"]:
                rep["failed"] = {"step": "download", "message": dl["message"], "kind": dl["kind"]}
                return self._finish(rep)
            say("verify", None, "Loading the model and asking it a real question...")
            rep["steps"]["verify"] = v = self.verify(mid, cancel)
            if not v["ok"]:
                rep["failed"] = {"step": "verify", "message": "; ".join(c["name"] + ": " + c["detail"] for c in v["checks"] if not c["ok"]) or "verification failed",
                                 "recovery": v.get("recovery", "")}
                return self._finish(rep)
            rep["completed"] = bool(v["real"] and self.real)          # a simulated run can never complete setup
            rep["summary"] = {"model": m.title, "model_id": mid, "license": m.license_spdx, "mode": v["mode"], "gpu_verified": v["gpu_verified"],
                              "cpu_fallback_available": True, "gpu_note": v["gpu_note"], "tokens_per_second_cpu": v["cpu"]["tokens_per_second"],
                              "pretrained_third_party_model": True,
                              "limitations": ["Voice, desktop control and the window itself are verified separately.", *sysr["warnings"]]}
            return self._finish(rep)
        except Cancelled:
            rep["failed"] = {"step": "cancelled", "message": "Setup was cancelled. Nothing was changed except files already downloaded and checked."}
            return self._finish(rep)
        except Exception as e:     # noqa: BLE001 - the wizard must end with a readable message, never a traceback
            rep["failed"] = {"step": "unexpected", "message": f"{type(e).__name__}: {e}"}
            return self._finish(rep)

    def _finish(self, rep: dict) -> dict:
        from jarvis.app.diagnostics import scrub_paths
        rep["finished"] = _now()
        st = {"completed": bool(rep.get("completed")), "model_id": rep.get("model_id"), "version_major": V.__version__.split(".")[0],
              "mode": rep.get("summary", {}).get("mode"), "gpu_verified": rep.get("summary", {}).get("gpu_verified", False),
              "real": rep["real"], "finished": rep["finished"], "failed": rep.get("failed")}
        try:
            self._save_state(st)
            (Path(self.cfg.data_dir) / REPORT_FILE).write_text(scrub_paths(json.dumps(rep, indent=2, default=str)), encoding="utf-8")
        except OSError as e:
            rep["save_error"] = f"{type(e).__name__}: {e}"
        return rep
