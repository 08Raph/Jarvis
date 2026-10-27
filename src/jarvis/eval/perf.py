"""Multi-token inference performance measurement (Prompt 22, Stage 4B): `jarvis perf`.

Uses the ACTIVE installed model and the real llama.cpp runtime (or an injected factory in unit tests, which then marks the result `simulated`).
Method, so that results are comparable later:
  * model LOAD time is measured by GGUFEngine and reported on its own; it is never part of a generation interval.
  * each run first clears the context (`llm.reset()`), so the prompt is evaluated every time instead of being served from the previous run's cache.
  * the response is streamed straight from the runtime (not through GGUFEngine.stream, which holds back characters to hide <think> tags).
    One streamed chunk with text = one generated token in llama-cpp-python. The same text is also re-tokenised; both counts are recorded.
  * time to first token (TTFT) = request start -> first chunk with text (includes prompt evaluation).
  * sustained speed = (generated_tokens - 1) / (time of last token - time of first token): the decode rate, excluding prompt evaluation.
    `overall_tokens_per_second` (tokens / total generation time, including prompt evaluation) is recorded too. Neither includes model loading.
  * warm-up runs are reported separately and are excluded from the summary statistics.
  * peak memory = the process's peak working set (Windows) or peak RSS (Linux) after the run; trustworthy only as "this process, whole run so far".
A run is `valid` only if it completed normally and produced at least MIN_VALID_TOKENS tokens. Anything else is recorded with its reason, and the command
exits non-zero if any measured run is invalid or the model could not be loaded. Results are not claimed to be identical across machines."""
from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROMPT = "Explain how the water cycle works in about 150 words."
SYSTEM = "You are a helpful assistant."
MAX_TOKENS = 128
MIN_VALID_TOKENS = 32
RUNS = 3
WARMUP = 1
HARD_MAX_TOKENS = 512       # the command refuses larger values: the workflow must stay bounded
HARD_MAX_RUNS = 10


def peak_memory_mb() -> float | None:
    """Peak resident memory of THIS process in MB, or None if the platform gives no trustworthy figure."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return None
            return round(pmc.PeakWorkingSetSize / 1e6, 1)
        import resource
        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss          # kilobytes on Linux
        return round(kb / 1e3, 1) if sys.platform.startswith("linux") else None
    except Exception:  # noqa: BLE001
        return None


def environment(data_dir: Path) -> dict:
    from jarvis.hardware import detect_hardware
    from jarvis.inference.gguf_engine import default_threads, runtime_info
    hw = detect_hardware(data_dir if Path(data_dir).exists() else ".")
    return {"os": f"{platform.system()} {platform.release()} ({platform.version()})", "machine": platform.machine(),
            "cpu_name": platform.processor() or None, "cpu_logical_threads": hw.cpu_threads, "threads_used_by_the_app": default_threads(),
            "ram_gb": hw.ram_gb, "gpu_detected": hw.gpu_name, "avx2": hw.avx2, "runtime": runtime_info(),
            "python": sys.version.split()[0], "frozen_exe": bool(getattr(sys, "frozen", False))}


def one_run(eng, prompt: str, max_tokens: int, clock=time.perf_counter) -> dict:
    """One streamed generation. Never raises for runtime errors: they are recorded, with whatever was measured before them."""
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    r: dict = {"max_tokens": max_tokens, "completed": False, "error": None}
    stamps: list[float] = []
    parts: list[str] = []
    t0 = t1 = clock()
    try:
        reset = getattr(eng.llm, "reset", None)
        if callable(reset):
            reset()                                   # forget the previous run's context so the prompt is evaluated again
        prepared = eng._prepare(msgs, max_tokens)
        r["prompt_content_tokens"] = sum(eng._count(m["content"]) for m in prepared)
        t0 = clock()
        for chunk in eng.llm.create_chat_completion(messages=prepared, max_tokens=max_tokens, temperature=0.0, stream=True):
            piece = chunk["choices"][0].get("delta", {}).get("content") or ""
            if piece:
                stamps.append(clock())
                parts.append(piece)
        t1 = clock()
        r["completed"] = True
    except KeyboardInterrupt:
        r["error"] = "interrupted"
        t1 = clock()
    except Exception as e:  # noqa: BLE001
        r["error"] = f"{type(e).__name__}: {e}"
        t1 = clock()
    try:
        n_total = getattr(eng.llm, "n_tokens", None)
    except Exception:  # noqa: BLE001
        n_total = None
    text = "".join(parts)
    n = len(stamps)
    r.update(generated_tokens=n, generated_tokens_retokenized=eng._count(text) if text else 0, characters=len(text),
             total_seconds=round(t1 - t0, 3),
             peak_memory_mb_after_run=peak_memory_mb(), sample_start=text[:80].replace("\n", " "))
    if isinstance(n_total, int) and n:
        r["prompt_tokens_incl_chat_template"] = n_total - n         # best effort: context length after the run minus generated tokens
    if n:
        ttft = stamps[0] - t0
        r["time_to_first_token_seconds"] = round(ttft, 3)
        r["overall_tokens_per_second"] = round(n / (t1 - t0), 2) if t1 > t0 else None
        if n >= 2 and stamps[-1] > stamps[0]:
            r["decode_seconds"] = round(stamps[-1] - stamps[0], 3)
            r["sustained_tokens_per_second"] = round((n - 1) / (stamps[-1] - stamps[0]), 2)
    r["valid"] = bool(r["completed"] and n >= MIN_VALID_TOKENS and r.get("sustained_tokens_per_second"))
    if r["completed"] and n < MIN_VALID_TOKENS:
        r["error"] = f"only {n} tokens were generated; at least {MIN_VALID_TOKENS} are needed for a sustained-speed figure"
    return r


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {}
    return {"n": len(vals), "min": round(min(vals), 2), "median": round(statistics.median(vals), 2), "max": round(max(vals), 2),
            "mean": round(statistics.fmean(vals), 2), "stdev": round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0}


def measure(data_dir: Path, runs: int = RUNS, warmup: int = WARMUP, max_tokens: int = MAX_TOKENS, prompt: str = PROMPT, llama_factory=None) -> dict:
    """Full measurement. Returns the result dict; result['status'] is 'complete', 'incomplete' or 'failed'."""
    from jarvis.config import Config
    from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable
    from jarvis.models import catalog
    from jarvis.models.manager import ModelStore
    from jarvis.net.policy import NetworkPolicy
    res: dict = {"kind": "inference-performance", "schema": 1, "time": datetime.now(timezone.utc).isoformat(), "status": "failed",
                 "simulated": llama_factory is not None, "method": __doc__.split("Method, so that results are comparable later:")[1].split("A run is")[0].strip(),
                 "settings": {"prompt": prompt, "system": SYSTEM, "max_tokens": max_tokens, "temperature": 0.0, "warmup_runs": warmup, "measured_runs": runs,
                              "min_valid_tokens": MIN_VALID_TOKENS}}
    runs, warmup, max_tokens = max(1, min(runs, HARD_MAX_RUNS)), max(0, min(warmup, 3)), max(8, min(max_tokens, HARD_MAX_TOKENS))
    res["settings"].update(measured_runs=runs, warmup_runs=warmup, max_tokens=max_tokens)
    cfg = Config(data_dir=data_dir)
    res["environment"] = environment(data_dir)
    store = ModelStore(cfg.models_dir, NetworkPolicy.load(cfg.settings_path))
    active = store.active()
    if active is None or store.kind(active) != "gguf":
        res["error"] = "no chat model is installed/selected; run setup first. Nothing was downloaded."
        return res
    mdir = store.path(active)
    try:
        meta = json.loads((mdir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    cm = None
    try:
        cm = catalog.get(meta.get("catalog_id") or active)
    except Exception:  # noqa: BLE001
        pass
    res["model"] = {"id": active, "file": str(mdir / "model.gguf").replace(str(Path.home()), "~"), "file_size_bytes": (mdir / "model.gguf").stat().st_size,
                    "sha256": meta.get("package_sha256"), "quantization": getattr(cm, "quant", None), "title": getattr(cm, "title", None),
                    "license": getattr(cm, "license_spdx", None), "context_tokens": meta.get("context_tokens")}
    ok, problems = store.verify(active)
    res["model"]["integrity_ok"] = ok
    if not ok:
        res["error"] = f"model failed its integrity check, not measured: {problems}"
        return res
    try:
        eng = GGUFEngine(mdir, llama_factory=llama_factory)
    except RuntimeUnavailable as e:
        res["error"] = str(e)
        return res
    res["load"] = {"load_seconds": round(eng.load_seconds, 3), "peak_memory_mb_after_load": peak_memory_mb()}
    res["execution"] = {"mode": "gpu" if eng.gpu_layers_requested else "cpu", "gpu_layers_requested": eng.gpu_layers_requested, "n_ctx": eng.n_ctx,
                        "backend": "llama.cpp via llama-cpp-python " + str(res["environment"]["runtime"].get("version")),
                        "gpu_offload_supported_by_runtime": res["environment"]["runtime"].get("gpu_offload_supported")}
    res["warmup"] = [one_run(eng, prompt, max(16, max_tokens // 4)) for _ in range(warmup)]
    res["runs"] = [one_run(eng, prompt, max_tokens) for _ in range(runs)]
    good = [x for x in res["runs"] if x["valid"]]
    res["summary"] = {"valid_runs": len(good), "measured_runs": runs,
                      "sustained_tokens_per_second": _stats([x["sustained_tokens_per_second"] for x in good]),
                      "overall_tokens_per_second": _stats([x["overall_tokens_per_second"] for x in good if x.get("overall_tokens_per_second")]),
                      "time_to_first_token_seconds": _stats([x["time_to_first_token_seconds"] for x in good]),
                      "generated_tokens": _stats([float(x["generated_tokens"]) for x in good]),
                      "load_seconds": res["load"]["load_seconds"], "peak_memory_mb": peak_memory_mb()}
    res["status"] = "complete" if len(good) == runs else "incomplete"
    if res["status"] == "incomplete":
        res["error"] = "; ".join(f"run {i + 1}: {x['error'] or 'invalid'}" for i, x in enumerate(res["runs"]) if not x["valid"])
    return res


def summary_text(res: dict) -> str:
    L = [f"Inference performance - status: {res.get('status', '?').upper()}" + ("   (SIMULATED RUNTIME - not a real measurement)" if res.get("simulated") else "")]
    env, m = res.get("environment", {}), res.get("model", {})
    L.append(f"OS: {env.get('os')}  CPU threads: {env.get('cpu_logical_threads')} (app uses {env.get('threads_used_by_the_app')})  RAM: {env.get('ram_gb')} GB  GPU detected: {env.get('gpu_detected')}")
    L.append(f"Model: {m.get('id')} ({m.get('quantization')}), {m.get('file_size_bytes')} bytes, sha256 {str(m.get('sha256'))[:16]}...")
    ex = res.get("execution", {})
    L.append(f"Execution: {ex.get('mode')} ({ex.get('backend')}), GPU layers requested {ex.get('gpu_layers_requested')}, runtime GPU offload supported: {ex.get('gpu_offload_supported_by_runtime')}")
    if res.get("error"):
        L.append(f"Problem: {res['error']}")
    if "load" in res:
        L.append(f"Model load: {res['load']['load_seconds']} s (not included in any speed below)")
    s = res.get("summary")
    if s:
        t, o, f, g = s["sustained_tokens_per_second"], s["overall_tokens_per_second"], s["time_to_first_token_seconds"], s["generated_tokens"]
        L.append(f"Valid measured runs: {s['valid_runs']}/{s['measured_runs']} (warm-up runs reported separately, excluded)")
        if t:
            L.append(f"Sustained decode speed: median {t['median']} tokens/s (min {t['min']}, max {t['max']}, stdev {t['stdev']}) over about {g['median']:.0f} generated tokens")
            L.append(f"Overall speed incl. prompt evaluation: median {o['median']} tokens/s; time to first token: median {f['median']} s")
        L.append(f"Peak process memory so far: {s['peak_memory_mb']} MB")
    for i, w in enumerate(res.get("warmup", [])):
        L.append(f"Warm-up {i + 1}: {w.get('generated_tokens')} tokens, {w.get('total_seconds')} s (not counted)")
    for i, x in enumerate(res.get("runs", [])):
        L.append(f"Run {i + 1}: {'valid' if x['valid'] else 'INVALID'} - {x.get('generated_tokens')} tokens, ttft {x.get('time_to_first_token_seconds')} s, "
                 f"sustained {x.get('sustained_tokens_per_second')} tok/s, overall {x.get('overall_tokens_per_second')} tok/s, peak {x.get('peak_memory_mb_after_run')} MB"
                 + (f" [{x['error']}]" if x.get("error") else ""))
    L.append("Reproduce: jarvis perf --out <folder>   (same model file, same settings; results differ between machines and between runs)")
    return "\n".join(L)


def run_cli(data_dir: Path, out: Path | None, runs: int, warmup: int, max_tokens: int) -> int:
    if runs > HARD_MAX_RUNS or max_tokens > HARD_MAX_TOKENS:
        print(f"Refused: at most {HARD_MAX_RUNS} runs and {HARD_MAX_TOKENS} tokens per run (keeps the measurement bounded).", file=sys.stderr)
        return 2
    res = measure(Path(data_dir), runs, warmup, max_tokens)
    text = summary_text(res)
    print(text)
    if out is not None:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "perf.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        (out / "perf_summary.txt").write_text(text + "\n", encoding="utf-8")
    return {"complete": 0, "incomplete": 4}.get(res["status"], 3)
