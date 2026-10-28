"""Staged validation of a REAL local model (Prompt 19). Each stage is its own state; later stages never run (and are never
reported as passed) when an earlier one failed. 'real_inference_passed' is True ONLY when every stage ran with the real runtime:
a run that used an injected factory (tests) is flagged `simulated` and can never be reported as real validation."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

STAGES = ("model_downloaded", "file_integrity_checked", "runtime_imported", "model_loaded", "inference_completed",
          "tool_calling_completed", "end_to_end_task_completed")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate(model_dir: str | Path, *, llama_factory=None, n_gpu_layers: int = 0, answer_check=None) -> dict:
    """Run the stages in order. `llama_factory` is for tests only (marks the run simulated)."""
    from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable, runtime_info
    d = Path(model_dir)
    simulated = llama_factory is not None
    res: dict = {"model_dir": str(d), "simulated": simulated, "stages": {s: {"state": "not_run", "detail": ""} for s in STAGES}}

    def mark(stage, ok, detail=""):
        res["stages"][stage] = {"state": "passed" if ok else "failed", "detail": detail}
        return ok

    def finish():
        done = [res["stages"][s]["state"] == "passed" for s in STAGES]
        res["all_stages_passed"] = all(done)
        res["real_inference_passed"] = (res["stages"]["inference_completed"]["state"] == "passed") and not simulated
        res["real_validation_passed"] = all(done) and not simulated
        return res

    from jarvis.models.gguf import model_file
    f = model_file(d)
    if not mark("model_downloaded", f.is_file(), "" if f.is_file() else f"no model.gguf in {d}"):
        return finish()
    try:
        with f.open("rb") as fh:
            magic = fh.read(4)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8")) if (d / "meta.json").is_file() else {}
        want = meta.get("sha256") if isinstance(meta, dict) else None
        got = _sha256(f)
        ok = magic == b"GGUF" and (want is None or want.lower() == got)
        detail = f"sha256={got}" + ("" if want else " (no recorded checksum to compare against)")
        if magic != b"GGUF":
            detail = "file does not start with the GGUF magic bytes"
        elif want and want.lower() != got:
            detail = f"sha256 mismatch: recorded {want}, file {got}"
        if not mark("file_integrity_checked", ok, detail):
            return finish()
    except (OSError, ValueError) as e:
        mark("file_integrity_checked", False, f"{type(e).__name__}: {e}")
        return finish()
    if llama_factory is None:
        ri = runtime_info()
        res["runtime"] = ri
        if not mark("runtime_imported", bool(ri.get("available")), ri.get("error", "") or f"llama-cpp-python {ri.get('version')}"):
            return finish()
    else:
        mark("runtime_imported", True, "injected test factory (simulated)")
    try:
        eng = GGUFEngine(d, llama_factory=llama_factory, n_gpu_layers=n_gpu_layers)
    except RuntimeUnavailable as e:
        mark("model_loaded", False, str(e))
        return finish()
    mark("model_loaded", True, f"loaded in {eng.load_seconds:.1f}s, gpu_layers_requested={eng.gpu_layers_requested}")
    try:
        t0 = time.perf_counter()
        out = eng.chat([{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=16, temperature=0.0)
        dt = time.perf_counter() - t0
        res["first_reply_seconds"] = round(dt, 2)
        if not mark("inference_completed", bool(out.strip()), f"{len(out)} chars in {dt:.1f}s" if out.strip() else "empty reply"):
            return finish()
    except Exception as e:     # noqa: BLE001
        mark("inference_completed", False, f"{type(e).__name__}: {e}")
        return finish()
    try:
        from jarvis.eval.benchmarks import parse_tool
        tool_out = eng.chat([{"role": "system", "content": "Answer ONLY with TOOL: <name>. Tools: get_time, read_file."},
                             {"role": "user", "content": "What time is it?"}], max_tokens=24, temperature=0.0)
        if not mark("tool_calling_completed", parse_tool(tool_out) == "get_time", f"model said: {tool_out.strip()[:80]!r}"):
            return finish()
    except Exception as e:     # noqa: BLE001
        mark("tool_calling_completed", False, f"{type(e).__name__}: {e}")
        return finish()
    if answer_check is None:
        res["stages"]["end_to_end_task_completed"] = {"state": "not_run", "detail": "no end-to-end task supplied (run the assistant mission suite)"}
    else:
        try:
            ok, detail = answer_check(eng)
            mark("end_to_end_task_completed", bool(ok), detail)
        except Exception as e:     # noqa: BLE001
            mark("end_to_end_task_completed", False, f"{type(e).__name__}: {e}")
    return finish()


def format_report(res: dict) -> str:
    lines = [f"Real-model validation for {res['model_dir']}" + ("  [SIMULATED RUN - not real validation]" if res["simulated"] else "")]
    for s in STAGES:
        st = res["stages"][s]
        lines.append(f"  {s:28} {st['state'].upper():8} {st['detail']}")
    lines.append(f"  REAL VALIDATION PASSED: {res['real_validation_passed']}")
    return "\n".join(lines)
