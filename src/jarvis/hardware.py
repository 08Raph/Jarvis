"""Hardware detection and model-size compatibility.

Requirement numbers are ESTIMATES derived from parameter counts (fp32 weights x 1.25
plus a fixed runtime allowance). They have not been measured on real machines.
Only NVIDIA GPUs are detected; AMD/Intel GPUs report as unknown and the CPU path is used.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from jarvis.planning.estimate import gpt_params

RUNTIME_OVERHEAD_GB = 0.5  # assumed allowance for Python + PyTorch runtime


@dataclass
class Hardware:
    os: str
    cpu_threads: int
    ram_gb: float | None
    free_disk_gb: float | None
    gpu_name: str | None = None
    vram_gb: float | None = None
    notes: list[str] = field(default_factory=list)
    avx2: bool | None = None   # None = unknown (not Windows / could not be read)


def _ram_gb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):  # type: ignore[attr-defined]
                return ms.ullTotalPhys / 1e9
            return None
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024 / 1e9
    except Exception:
        return None
    return None


def _nvidia_exe() -> str | None:
    """Absolute path only: never search the current directory or PATH for an executable to run."""
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        for cand in (Path(root) / "System32" / "nvidia-smi.exe",
                     Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"):
            if cand.is_file():
                return str(cand)
        return None
    return shutil.which("nvidia-smi")


def _nvidia() -> tuple[str | None, float | None]:
    exe = _nvidia_exe()
    if exe is None:
        return None, None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash if launched from a GUI later
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, check=True,
                             creationflags=flags).stdout.strip().splitlines()
        name, mem = [x.strip() for x in out[0].split(",")]
        return name, float(mem) / 1024
    except Exception:
        return None, None


def cpu_has_avx2() -> bool | None:
    """Windows: ask the OS whether the CPU supports AVX2 (PF_AVX2_INSTRUCTIONS_AVAILABLE = 40). None = unknown."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))  # type: ignore[attr-defined]
    except Exception:
        return None


AVX2_WARNING = ("This CPU does not report AVX2 support. Whether the prebuilt local AI runtime needs AVX2 has NOT been verified, "
                "so loading a model may or may not work here; if the program closes without a message when a model loads, "
                "that is the likely reason. Nothing is blocked, and the rest of Project JARVIS works normally.")


def detect_hardware(path: str | Path = ".") -> Hardware:
    notes: list[str] = []
    ram = _ram_gb()
    if ram is None:
        notes.append("RAM could not be detected")
    try:
        free = shutil.disk_usage(str(path)).free / 1e9
    except OSError:
        free = None
        notes.append("free disk space could not be detected")
    gpu, vram = _nvidia()
    avx2 = cpu_has_avx2()
    if avx2 is False:
        notes.append(AVX2_WARNING)
    if gpu is None:
        notes.append("no NVIDIA GPU detected (AMD/Intel GPUs are not detected); CPU inference would be used")
    return Hardware(os=f"{platform.system()} {platform.release()}", cpu_threads=os.cpu_count() or 1,
                    ram_gb=ram, free_disk_gb=free, gpu_name=gpu, vram_gb=vram, notes=notes, avx2=avx2)


@dataclass(frozen=True)
class Tier:
    name: str
    vocab: int
    block: int
    layers: int
    embd: int
    heads: int

    @property
    def params(self) -> int:
        return gpt_params(self.vocab, self.block, self.layers, self.embd)

    @property
    def model_file_gb(self) -> float:
        return self.params * 4 / 1e9

    @property
    def min_ram_gb(self) -> float:
        return self.model_file_gb * 1.25 + RUNTIME_OVERHEAD_GB

    @property
    def recommended_ram_gb(self) -> float:
        return max(4.0, self.min_ram_gb * 2)

    @property
    def min_vram_gb(self) -> float:
        return self.model_file_gb * 1.25 + 0.3


TIERS = (Tier("jarvis-lm-tiny", 1024, 128, 4, 128, 4),
         Tier("jarvis-lm-small", 8192, 512, 8, 512, 8),
         Tier("jarvis-lm-base", 32000, 1024, 12, 768, 12))


def assess(hw: Hardware, tier: Tier) -> dict:
    reasons: list[str] = []
    if hw.free_disk_gb is not None and hw.free_disk_gb < tier.model_file_gb * 2 + 0.1:
        return {"tier": tier.name, "verdict": "too_large", "reasons": ["not enough free disk space to install"]}
    if hw.ram_gb is None:
        return {"tier": tier.name, "verdict": "unknown", "reasons": ["RAM unknown"]}
    if hw.ram_gb < tier.min_ram_gb:
        return {"tier": tier.name, "verdict": "too_large",
                "reasons": [f"needs about {tier.min_ram_gb:.1f} GB RAM, found {hw.ram_gb:.1f} GB"]}
    if hw.ram_gb < tier.recommended_ram_gb:
        reasons.append(f"below the {tier.recommended_ram_gb:.1f} GB recommended; expect slowdowns")
        return {"tier": tier.name, "verdict": "tight", "reasons": reasons}
    return {"tier": tier.name, "verdict": "ok", "reasons": ["meets estimated RAM and disk needs; speed not measured"]}


def recommend(hw: Hardware, tiers=TIERS) -> dict:
    results = [assess(hw, t) for t in tiers]
    ok = [r["tier"] for r in results if r["verdict"] == "ok"]
    return {"hardware": asdict(hw), "assessments": results,
            "recommended": ok[-1] if ok else None,
            "disclaimer": "Estimates only. Real speed depends on your CPU/GPU and has not been measured."}


def assess_gguf(hw: Hardware, size_bytes: int, min_ram_gb: float | None = None) -> dict:
    """Will a GGUF model of this file size fit? ESTIMATE: file size + ~1.5 GB (context cache and runtime) must fit in RAM,
    and the rest of Windows needs room too. Speed is NOT predicted here; `jarvis selftest` measures it on the real PC."""
    size_gb = size_bytes / 1e9
    need = max(size_gb + 1.5, min_ram_gb or 0)
    rec = need + 3.0
    reasons: list[str] = []
    if hw.free_disk_gb is not None and hw.free_disk_gb < size_gb + 0.5:
        return {"verdict": "too_large", "reasons": [f"needs about {size_gb + 0.5:.1f} GB free disk, found {hw.free_disk_gb:.1f} GB"],
                "min_ram_gb": round(need, 1), "recommended_ram_gb": round(rec, 1)}
    if hw.ram_gb is None:
        return {"verdict": "unknown", "reasons": ["RAM unknown"], "min_ram_gb": round(need, 1), "recommended_ram_gb": round(rec, 1)}
    if hw.ram_gb < need:
        verdict, reasons = "too_large", [f"needs about {need:.1f} GB RAM, found {hw.ram_gb:.1f} GB"]
    elif hw.ram_gb < rec:
        verdict, reasons = "tight", [f"below the {rec:.1f} GB recommended; close other programs and expect slowdowns"]
    else:
        verdict, reasons = "ok", ["meets the estimated RAM and disk needs; speed is not predicted"]
    if hw.gpu_name is None:
        reasons.append("no NVIDIA GPU detected: the CPU is used (this is the supported default; a GPU is not required)")
    return {"verdict": verdict, "reasons": reasons, "min_ram_gb": round(need, 1), "recommended_ram_gb": round(rec, 1)}
