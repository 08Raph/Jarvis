"""Real-PC validation driver (Prompt 20). Run by scripts/win_validate.py; every result is written as JSON into one folder.
It never loads a model unless you ask it to (`infer`, `suite`), never touches the network, and records what actually happened,
including failures. Nothing here is a score: it collects evidence for a person to read."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _write(out: Path, name: str, obj: dict) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    p = out / name
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return p


def _pkg_versions() -> dict:
    import importlib.metadata as md
    res = {}
    for n in ("llama-cpp-python", "PySide6", "sounddevice", "vosk", "numpy", "cryptography", "psutil", "pypdf", "comtypes"):
        try:
            res[n] = md.version(n)
        except md.PackageNotFoundError:
            res[n] = None
    return res


def _nvidia_text() -> dict:
    from jarvis.hardware import _nvidia_exe
    exe = _nvidia_exe()
    if exe is None:
        return {"found": False}
    out = {"found": True, "exe": exe}
    for key, args in (("summary", []), ("query", ["--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu", "--format=csv"])):
        try:
            r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out[key] = r.stdout.strip() if r.returncode == 0 else f"exit {r.returncode}: {r.stderr.strip()}"
        except Exception as e:     # noqa: BLE001
            out[key] = f"{type(e).__name__}: {e}"
    return out


def environment(data_dir: Path) -> dict:
    from jarvis import __version__
    from jarvis.config import Config
    from jarvis.hardware import detect_hardware
    from jarvis.inference.gguf_engine import runtime_info
    from jarvis.models import selector
    hw = detect_hardware(data_dir if data_dir.exists() else ".")
    rt = runtime_info()
    ch = selector.select(hw, rt)
    return {"kind": "environment", "time": datetime.now(timezone.utc).isoformat(), "jarvis_version": __version__,
            "python": sys.version, "python_64bit": sys.maxsize > 2**32, "platform": platform.platform(), "machine": platform.machine(),
            "windows_version": platform.version(), "processor": os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor(),
            "hardware": hw.__dict__, "runtime": rt, "packages": _pkg_versions(), "nvidia_smi": _nvidia_text(),
            "acceleration_state": selector.acceleration_state(hw, rt), "plan": {"tier": ch.tier, "fits": ch.fits, "mode": ch.mode,
            "model": ch.model.id if ch.model else None, "reasons": ch.reasons, "alternatives": ch.alternatives},
            "env_JARVIS_GPU_LAYERS": os.environ.get("JARVIS_GPU_LAYERS"), "data_dir": str(data_dir)}


class Sampler:
    """Polls peak process memory and (if nvidia-smi works) GPU utilisation and memory while a model runs."""
    def __init__(self, interval: float = 0.5):
        self.interval, self.stop = interval, threading.Event()
        self.peak_rss_gb = None
        self.gpu = []
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            import psutil
            proc = psutil.Process()
        except Exception:     # noqa: BLE001
            proc = None
        from jarvis.hardware import _nvidia_exe
        exe = _nvidia_exe()
        while not self.stop.is_set():
            if proc is not None:
                rss = proc.memory_info().rss / 1e9
                self.peak_rss_gb = max(self.peak_rss_gb or 0.0, rss)
            if exe:
                try:
                    r = subprocess.run([exe, "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"], capture_output=True,
                                       text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    u, m = [float(x) for x in r.stdout.strip().splitlines()[0].split(",")]
                    self.gpu.append((u, m))
                except Exception:     # noqa: BLE001
                    pass
            self.stop.wait(self.interval)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()
        self._t.join(timeout=5)

    def summary(self) -> dict:
        g = self.gpu
        return {"peak_process_rss_gb": round(self.peak_rss_gb, 2) if self.peak_rss_gb else None,
                "gpu_samples": len(g), "gpu_util_max_percent": max((u for u, _ in g), default=None),
                "gpu_memory_used_mb_min": min((m for _, m in g), default=None), "gpu_memory_used_mb_max": max((m for _, m in g), default=None),
                "note": "RSS is this Python process; GPU numbers are whole-GPU, so close other GPU apps for a clean reading"}


def _gen(eng, messages, max_tokens, temperature=0.0) -> dict:
    t0 = time.perf_counter()
    first, parts = None, []
    for chunk in eng.stream(messages, max_tokens=max_tokens, temperature=temperature):
        if first is None and chunk:
            first = time.perf_counter() - t0
        parts.append(chunk)
    dt = time.perf_counter() - t0
    text = "".join(parts)
    n = eng._count(text) if text else 0
    return {"text": text, "seconds": round(dt, 2), "first_token_seconds": round(first, 2) if first is not None else None,
            "tokens": n, "tokens_per_second": round(n / dt, 2) if dt > 0 and n else None}


def _open_store(data_dir: Path):
    from jarvis.config import Config
    from jarvis.models.manager import ModelStore
    from jarvis.net.policy import NetworkPolicy
    cfg = Config(data_dir=data_dir)
    store = ModelStore(cfg.models_dir, NetworkPolicy.load(cfg.settings_path))
    active = store.active()
    return store, active


def infer(data_dir: Path, layers: int, llama_factory=None) -> dict:
    from jarvis.eval import realmodel
    from jarvis.inference.gguf_engine import runtime_info
    store, active = _open_store(data_dir)
    res: dict = {"kind": "infer", "time": datetime.now(timezone.utc).isoformat(), "gpu_layers_requested": layers, "runtime": runtime_info()}
    if active is None:
        res["error"] = "no model is installed/selected (run: jarvis models fetch qwen3-1.7b-q4_k_m)"
        return res
    mdir = store.path(active)
    ok, problems = store.verify(active)
    res.update(model_id=active, model_dir=str(mdir), store_integrity_ok=ok, store_integrity_problems=problems)
    metrics: dict = {}

    def e2e(eng):
        sys_m = {"role": "system", "content": "You are a helpful assistant."}
        checks = {}
        turns = [{"role": "user", "content": "Hi! My name is Dana and I work on bridges."}]
        a = _gen(eng, [sys_m, *turns], 96)
        metrics["greeting"] = {k: v for k, v in a.items() if k != "text"}
        checks["greeting_nonempty"] = bool(a["text"].strip())
        turns += [{"role": "assistant", "content": a["text"]}, {"role": "user", "content": "Give me one safety tip for bridge inspectors."}]
        b = _gen(eng, [sys_m, *turns], 128)
        turns += [{"role": "assistant", "content": b["text"]}, {"role": "user", "content": "What is my name and what do I work on?"}]
        c = _gen(eng, [sys_m, *turns], 64)
        metrics["multi_turn"] = {k: v for k, v in c.items() if k != "text"}
        checks["multi_turn_recalls_name"] = "dana" in c["text"].lower()
        checks["multi_turn_recalls_topic"] = "bridge" in c["text"].lower()
        d = _gen(eng, [sys_m, {"role": "user", "content": "Explain the difference between TCP and UDP in about 200 words."}], 400)
        metrics["long_response"] = {k: v for k, v in d.items() if k != "text"}
        checks["long_response_over_120_words"] = len(d["text"].split()) > 120
        e = _gen(eng, [sys_m, {"role": "user", "content": "Write a Python function that reverses a singly linked list."}], 300)
        metrics["technical_code"] = {k: v for k, v in e.items() if k != "text"}
        checks["technical_code_has_def"] = "def " in e["text"]
        f = _gen(eng, [sys_m, {"role": "user", "content": "What is the difference between a process and a thread?"}], 250)
        metrics["technical_concept"] = {k: v for k, v in f.items() if k != "text"}
        checks["technical_mentions_thread"] = "thread" in f["text"].lower()
        res["sample_replies"] = {"multi_turn_recall": c["text"][:300], "code": e["text"][:600], "concept": f["text"][:400]}
        res["content_checks"] = checks
        return all([checks["greeting_nonempty"], checks["multi_turn_recalls_name"], checks["long_response_over_120_words"]]), json.dumps(checks)

    with Sampler() as s:
        v = realmodel.validate(mdir, llama_factory=llama_factory, n_gpu_layers=layers, answer_check=e2e)
    res["validation"] = v
    res["report_text"] = realmodel.format_report(v)
    res["metrics"] = metrics
    res["resources"] = s.summary()
    res["load_seconds_from_stage"] = v["stages"]["model_loaded"]["detail"]
    tps = [m["tokens_per_second"] for m in metrics.values() if m.get("tokens_per_second")]
    res["median_tokens_per_second"] = sorted(tps)[len(tps) // 2] if tps else None
    return res


def compare(cpu_json: Path, gpu_json: Path) -> dict:
    """Decide 'acceleration verified' from two real runs. Needs both runs to be real validations AND GPU activity to be observed."""
    from jarvis.hardware import Hardware
    from jarvis.models import selector
    cpu, gpu = json.loads(cpu_json.read_text(encoding="utf-8")), json.loads(gpu_json.read_text(encoding="utf-8"))
    out = {"kind": "compare", "cpu_tps": cpu.get("median_tokens_per_second"), "gpu_tps": gpu.get("median_tokens_per_second")}
    real = all(r.get("validation", {}).get("real_inference_passed") for r in (cpu, gpu))
    util = (gpu.get("resources") or {}).get("gpu_util_max_percent")
    out.update(both_runs_real=real, gpu_layers_cpu_run=cpu.get("gpu_layers_requested"), gpu_layers_gpu_run=gpu.get("gpu_layers_requested"),
               gpu_util_max_percent=util, gpu_backend_in_runtime=gpu.get("runtime", {}).get("gpu_offload_supported"))
    fake_hw = Hardware(os="n/a", cpu_threads=0, ram_gb=None, free_disk_gb=None, gpu_name="seen in gpu run" if util is not None else None)
    st = selector.acceleration_state(fake_hw, {"gpu_offload_supported": out["gpu_backend_in_runtime"]},
                                     out["cpu_tps"] if real and cpu.get("gpu_layers_requested") == 0 else None,
                                     out["gpu_tps"] if real and gpu.get("gpu_layers_requested") not in (0, None) and (util or 0) > 0 else None)
    out["acceleration_verified"] = st["acceleration_verified"]
    out["message"] = st["message"]
    out["speedup"] = round(out["gpu_tps"] / out["cpu_tps"], 2) if out["gpu_tps"] and out["cpu_tps"] else None
    return out


def suite(data_dir: Path, layers: int) -> dict:
    from jarvis.eval import suite19
    from jarvis.inference.gguf_engine import GGUFEngine, runtime_info
    store, active = _open_store(data_dir)
    if active is None:
        return {"kind": "suite", "error": "no model is installed/selected"}
    eng = GGUFEngine(store.path(active), n_gpu_layers=layers)

    def answer(prompt: str) -> str:
        return eng.chat([{"role": "user", "content": prompt}], max_tokens=300, temperature=0.0)
    t0 = time.perf_counter()
    with Sampler() as s:
        res = suite19.run_all(answer, label="real_model")
    return {"kind": "suite", "time": datetime.now(timezone.utc).isoformat(), "model_id": active, "gpu_layers_requested": layers,
            "n_ctx": eng.n_ctx, "runtime": runtime_info(), "total_seconds": round(time.perf_counter() - t0, 1), "results": res,
            "resources": s.summary(), "note": "n_ctx limits the long_context tasks; the engine drops old turns that do not fit"}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="win_validate", description="Collect real-PC evidence for Project JARVIS (writes JSON files).")
    ap.add_argument("--out", required=True, help="folder for result files")
    ap.add_argument("--data-dir", default=None, help="Jarvis data folder (same value you pass to `jarvis --data-dir`)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("env")
    for n in ("infer", "suite"):
        p = sub.add_parser(n)
        p.add_argument("--layers", type=int, default=0, help="GPU layers: 0 = CPU only (default), -1 = offload everything")
    c = sub.add_parser("compare")
    c.add_argument("cpu_json")
    c.add_argument("gpu_json")
    a = ap.parse_args(argv)
    out = Path(a.out)
    dd = Path(a.data_dir) if a.data_dir else Path("jarvis_data")
    if a.cmd == "env":
        r = environment(dd)
        name = "env.json"
    elif a.cmd == "infer":
        r = infer(dd, a.layers)
        name = f"infer_layers{a.layers}.json"
    elif a.cmd == "suite":
        r = suite(dd, a.layers)
        name = f"suite_layers{a.layers}.json"
    else:
        r = compare(Path(a.cpu_json), Path(a.gpu_json))
        name = "compare.json"
    p = _write(out, name, r)
    print(r.get("report_text") or json.dumps({k: r[k] for k in r if k in ("kind", "error", "acceleration_verified", "message", "speedup", "plan")}, indent=2, default=str))
    print(f"\nResult file: {p}")
    ok = not r.get("error") and (r.get("validation", {}).get("real_validation_passed", True) if a.cmd == "infer" else True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
