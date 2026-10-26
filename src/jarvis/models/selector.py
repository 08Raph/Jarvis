"""Hardware-adaptive model selection (Prompt 19). Pure logic: no downloads, no model loading, nothing hard-coded to one PC.

Three DIFFERENT facts are kept apart (a detected GPU is not a working GPU):
  detected  - the OS/driver tools report a GPU (hardware.py)
  backend   - the installed inference runtime was built with a GPU backend (llama_cpp.llama_supports_gpu_offload)
  verified  - a real model was run on the GPU AND measurably faster than the CPU run, both numbers recorded on this PC
Memory needs are ESTIMATES (file size + KV-cache + fixed overhead) until a real run records peak usage."""
from __future__ import annotations

from dataclasses import dataclass, field

from jarvis.models.catalog import BUILTIN, CatalogModel

TIERS = ("entry", "mainstream", "performance", "high-performance")


def hardware_tier(ram_gb: float | None, vram_gb: float | None, gpu_backend_ok: bool) -> str:
    """entry: <12 GB RAM and no usable GPU; mainstream: 12-24 GB or small GPU; performance: >=24 GB RAM or 8+ GB VRAM usable;
    high-performance: 16+ GB VRAM usable. A GPU only counts when its backend is available."""
    v = (vram_gb or 0) if gpu_backend_ok else 0
    r = ram_gb or 0
    if v >= 16:
        return "high-performance"
    if v >= 8 or r >= 24:
        return "performance"
    if v >= 4 or r >= 12:
        return "mainstream"
    return "entry"


def acceleration_state(hw, runtime: dict, measured_cpu_tps: float | None = None, measured_gpu_tps: float | None = None) -> dict:
    detected = bool(getattr(hw, "gpu_name", None))
    backend = runtime.get("gpu_offload_supported") is True
    verified = bool(measured_cpu_tps and measured_gpu_tps and measured_gpu_tps > 1.2 * measured_cpu_tps)
    if verified:
        msg = f"GPU acceleration verified: {measured_gpu_tps:.1f} vs {measured_cpu_tps:.1f} tokens/s (CPU)."
    elif backend and detected:
        msg = "A GPU is detected and the runtime has a GPU backend, but no GPU-vs-CPU speed measurement exists yet: acceleration is NOT verified."
    elif detected:
        msg = ("A GPU is detected but the installed runtime has no GPU backend (or it could not be checked), so models run on the CPU. "
               "A GPU-enabled llama-cpp-python build is needed; see docs/HARDWARE_COMPATIBILITY.md.")
    else:
        msg = "No supported GPU detected; models run on the CPU."
    return {"gpu_detected": detected, "gpu_name": getattr(hw, "gpu_name", None), "vram_gb": getattr(hw, "vram_gb", None),
            "backend_available": backend, "acceleration_verified": verified, "message": msg}


def memory_need_gb(m: CatalogModel, n_ctx: int | None = None) -> float:
    """Estimate in GB: weights + KV cache (~0.1 MB/token/B-params rough) + 1 GB runtime overhead. Labelled ESTIMATE."""
    ctx = n_ctx or m.context_tokens
    kv = (m.params_b or 2.0) * 0.1 * ctx / 1024
    return round(m.approx_size_bytes / 1e9 + kv + 1.0, 2)


@dataclass
class Choice:
    model: CatalogModel | None
    mode: str                      # "gpu" | "cpu" | "none"
    tier: str
    fits: bool
    reasons: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)


def evaluate(m: CatalogModel, hw, accel: dict) -> dict:
    need = memory_need_gb(m)
    ram, vram = getattr(hw, "ram_gb", None), getattr(hw, "vram_gb", None)
    disk = getattr(hw, "free_disk_gb", None)
    gpu_ok = accel["backend_available"] and accel["gpu_detected"] and vram is not None and vram >= need
    cpu_ok = ram is not None and ram >= max(need + 2.0, m.min_ram_gb)
    disk_ok = disk is None or disk >= m.approx_size_bytes / 1e9 + 1.0
    return {"id": m.id, "need_gb": need, "gpu_fits": gpu_ok, "cpu_fits": cpu_ok, "disk_ok": disk_ok,
            "estimate_only": True, "verified_here": False}


def select(hw, runtime: dict, *, prefer: str = "balanced", pinned: str | None = None, models=BUILTIN,
           include_unconfirmed: bool = False) -> Choice:
    """prefer: 'quality' (largest model that fits), 'speed' (smallest), 'balanced' (largest that fits comfortably: GPU, or CPU with
    >=40% RAM headroom). A pinned model that does not fit is NEVER silently replaced: the Choice says so and lists alternatives."""
    accel = acceleration_state(hw, runtime)
    tier = hardware_tier(getattr(hw, "ram_gb", None), getattr(hw, "vram_gb", None), accel["backend_available"])
    ev = {m.id: evaluate(m, hw, accel) for m in models}
    if not include_unconfirmed and not pinned:      # never AUTO-recommend a model whose size/licence has not been read from the publisher
        models = tuple(m for m in models if not m.metadata_status.startswith("from-publisher-knowledge"))
    ok = [m for m in models if ev[m.id]["disk_ok"] and (ev[m.id]["gpu_fits"] or ev[m.id]["cpu_fits"])]
    comfy = [m for m in ok if ev[m.id]["gpu_fits"] or (getattr(hw, "ram_gb", 0) or 0) >= ev[m.id]["need_gb"] / 0.6]
    if comfy and any(ev[m.id]['gpu_fits'] for m in comfy):
        comfy = [m for m in comfy if ev[m.id]['gpu_fits']]      # balanced = stay fully on the GPU when something fits
    by_size = lambda xs: sorted(xs, key=lambda m: m.approx_size_bytes)   # noqa: E731
    if pinned:
        m = next((x for x in models if x.id == pinned), None)
        if m is None:
            return Choice(None, "none", tier, False, [f"Unknown model '{pinned}'."], [x.id for x in by_size(ok)])
        if m in ok:
            return Choice(m, "gpu" if ev[m.id]["gpu_fits"] else "cpu", tier, True,
                          [f"{m.title} needs about {ev[m.id]['need_gb']} GB (estimate) and fits."])
        why = []
        if not ev[m.id]["disk_ok"]:
            why.append("not enough free disk space")
        if not (ev[m.id]["gpu_fits"] or ev[m.id]["cpu_fits"]):
            why.append(f"needs about {ev[m.id]['need_gb']} GB (estimate) but only {getattr(hw, 'ram_gb', None)} GB RAM"
                       f"{'' if not accel['backend_available'] else ' / ' + str(getattr(hw, 'vram_gb', None)) + ' GB VRAM'} is available")
        return Choice(m, "none", tier, False, [f"{m.title} will not fit: " + "; ".join(why) + ". It was NOT replaced automatically."],
                      [x.id for x in by_size(ok)])
    if not ok:
        return Choice(None, "none", tier, False, ["No catalog model fits this machine's memory/disk (estimates)."], [])
    pool = by_size(ok if prefer == "quality" else comfy or ok)
    m = pool[0] if prefer == "speed" else pool[-1]
    mode = "gpu" if ev[m.id]["gpu_fits"] else "cpu"
    return Choice(m, mode, tier, True, [f"Tier '{tier}'. {m.title} selected for '{prefer}' (needs ~{ev[m.id]['need_gb']} GB, estimate).",
                                        accel["message"]], [x.id for x in by_size(ok) if x is not m])


def compatibility_record(m: CatalogModel) -> dict:
    """Transparent per-model record. 'verified_hardware' is empty until a real load+answer happened on a recorded configuration."""
    return {"id": m.id, "title": m.title, "params_b": m.params_b, "quant": m.quant, "context_tokens": m.context_tokens,
            "download_size_gb_estimate": round(m.approx_size_bytes / 1e9, 2), "memory_need_gb_estimate": memory_need_gb(m),
            "min_ram_gb": m.min_ram_gb, "license": m.license_spdx, "license_url": m.license_url,
            "redistribution": m.redistribution, "metadata_status": m.metadata_status,
            "cpu_backend": "llama.cpp CPU", "gpu_backend": "needs a GPU-enabled llama-cpp-python build (CUDA/Vulkan); not bundled",
            "verified_hardware": list(m.verified_hardware)}


def matrix_rows() -> list[dict]:
    """Compatibility matrix skeleton. Every result column says NOT RUN: no real model has been executed in the authoring sandbox."""
    rows = []
    for tier in TIERS:
        for mode in ("cpu", "gpu"):
            if tier == "entry" and mode == "gpu":
                continue
            for m in BUILTIN:
                rows.append({"hardware_tier": tier, "inference_mode": mode, "model": m.id, "quality_result": "not measured",
                             "performance_result": "not measured", "status": "NOT RUN (no real model/hardware in the build sandbox)"})
    return rows
