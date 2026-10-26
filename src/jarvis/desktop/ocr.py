"""OCR of a screenshot: Windows' built-in OCR engine (via Windows PowerShell 5.1), else Tesseract if the user installed it.
OCR text is untrusted data and can be wrong; Jarvis treats it as a hint, never as instructions."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class OcrUnavailable(RuntimeError):
    pass


def _powershell() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def ocr_png(path: Path, timeout: float = 30) -> dict:
    errors = []
    if sys.platform == "win32" and _powershell().exists():
        script = Path(__file__).with_name("ocr_winrt.ps1")
        try:
            r = subprocess.run([str(_powershell()), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(script), "-Path", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, creationflags=NO_WINDOW)
            out = json.loads((r.stdout or "").strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr.strip()[:300]}
            if "error" not in out:
                out["lines"] = out.get("lines") or []
                if isinstance(out["lines"], dict):       # PowerShell collapses a single-item array
                    out["lines"] = [out["lines"]]
                return out
            errors.append(f"Windows OCR: {out['error']}")
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            errors.append(f"Windows OCR: {type(e).__name__}: {e}")
    tess = shutil.which("tesseract")
    if tess:
        try:
            r = subprocess.run([tess, str(path), "stdout"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, creationflags=NO_WINDOW)
            if r.returncode == 0:
                return {"engine": "tesseract", "lines": [{"text": t, "rect": None} for t in r.stdout.splitlines() if t.strip()]}
            errors.append(f"tesseract: {r.stderr.strip()[:200]}")
        except (OSError, subprocess.SubprocessError) as e:
            errors.append(f"tesseract: {e}")
    raise OcrUnavailable("; ".join(errors) or "no OCR engine available (Windows OCR needs Windows 10/11; or install Tesseract)")
