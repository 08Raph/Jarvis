"""Judge whether the CURRENT application produced a reply from a GENUINE model (stdlib + the project's own GGUF header reader).

Usage: python scripts/ci_real_response_check.py --data-dir DIR --model-id ID [--cmd "path\\to\\jarvis.exe"] [--prompt ...] [--expect Paris] [--json OUT] [--summary FILE]

It complements `scripts/ci_check_selftest_report.py`, which judges the one-token `jarvis selftest`. This script adds two things that report does not record:
  1. WEIGHTS: the installed model.gguf must have a plausible number of weight tensors, a plausible size (and, for a catalogued model, a size within 10 % of the
     catalogue), a real architecture and a name that is not the project's synthetic fixture. The project's own fixture (1 tensor, 52 MB, "Synthetic 1B") fails all of these.
  2. THE APPLICATION: it runs `<cmd> --data-dir DIR chat "<prompt>"`, i.e. the same LocalAssistant -> GGUFEngine -> llama.cpp path a user's chat takes, and the
     reply on stdout must be non-empty, contain the expected word, and come with the "[local model: ID" banner on stderr. A mocked runtime cannot satisfy this inside the
     packaged exe, and a fake `--cmd` is rejected unless --allow-test-command is given (tests only).
Outcomes: PASS (exit 0), FAIL (exit 1), BLOCKED (exit 3: the model file or command is missing, so nothing was tested).
What a PASS means: ONE reply to ONE prompt from the model whose file was inspected, on this machine. It is not a quality, speed, GPU, voice or other-PC result."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROMPT = "What is the capital of France? Answer in one short sentence."
DEFAULT_EXPECT = "Paris"
MIN_TENSORS = 50                  # Qwen3-1.7B has hundreds; the synthetic fixture has 1
MIN_BYTES = 500_000_000           # the smallest catalogued model is about 1.28 GB; the fixture is 52 MB
SIZE_TOLERANCE = 0.10


def _gguf():
    sys.path.insert(0, str(ROOT / "src"))
    from jarvis.models import gguf as G           # noqa: PLC0415
    return G


def _catalog_size(model_id: str):
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from jarvis.models.catalog import BUILTIN  # noqa: PLC0415
    except Exception:                              # noqa: BLE001
        return None
    for m in BUILTIN:
        if m.id == model_id:
            return int(m.approx_size_bytes)
    return None


def check_weights(path: Path, model_id: str, min_tensors: int, min_bytes: int, expect_sha256: str = "") -> tuple[list[str], dict]:
    problems: list[str] = []
    facts: dict = {"model_file": str(path)}
    if not path.is_file():
        return ["model file is missing"], facts
    size = path.stat().st_size
    facts["model_bytes"] = size
    try:
        G = _gguf()
        info = G.summarize(G.read_header(path))
    except Exception as exc:                       # noqa: BLE001
        return [f"GGUF header unreadable: {type(exc).__name__}: {str(exc)[:120]}"], facts
    facts.update({k: info.get(k) for k in ("name", "architecture", "quantization", "context_length", "tensor_count", "block_count")})
    if not isinstance(info.get("tensor_count"), int) or info["tensor_count"] < min_tensors:
        problems.append(f"tensor_count {info.get('tensor_count')!r} is below {min_tensors}: not a genuine model's weights")
    if size < min_bytes:
        problems.append(f"file is {size} bytes, below {min_bytes}")
    exp = _catalog_size(model_id)
    if exp:
        facts["catalog_size_bytes"] = exp
        if abs(size - exp) > SIZE_TOLERANCE * exp:
            problems.append(f"size {size} differs from the catalogue's {exp} by more than {int(SIZE_TOLERANCE * 100)} %")
    if not info.get("architecture"):
        problems.append("no architecture in the header")
    if "synthetic" in str(info.get("name") or "").lower():
        problems.append(f"model name {info.get('name')!r} marks a synthetic test fixture")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    facts["model_sha256"] = h.hexdigest()
    if expect_sha256 and facts["model_sha256"] != expect_sha256.strip().lower():
        problems.append(f"sha256 {facts['model_sha256']} is not the expected {expect_sha256.strip().lower()}")
    return problems, facts


def run_chat(cmd: list[str], data_dir: Path, prompt: str, max_tokens: int, timeout: float) -> dict:
    argv = [*cmd, "--data-dir", str(data_dir), "chat", prompt, "--max-new-tokens", str(max_tokens)]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"started": False, "error": "command not found"}
    except subprocess.TimeoutExpired:
        return {"started": True, "timed_out": True, "seconds": round(time.perf_counter() - t0, 2)}
    return {"started": True, "timed_out": False, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr,
            "seconds": round(time.perf_counter() - t0, 2)}


def judge_chat(res: dict, model_id: str, expect: str) -> list[str]:
    if not res.get("started"):
        return ["the application command could not be started"]
    if res.get("timed_out"):
        return [f"the chat did not finish within the time limit ({res.get('seconds')} s)"]
    problems = []
    out = (res.get("stdout") or "").strip()
    err = res.get("stderr") or ""
    if res.get("returncode") != 0:
        problems.append(f"chat exit code {res.get('returncode')!r} is not 0")
    if not out:
        problems.append("the reply on stdout is empty")
    if "Traceback" in out or "Traceback" in err:
        problems.append("a Python traceback appeared")
    if "returned no text" in out or "returned no text" in err:
        problems.append("the application reported that the model returned no text")
    if expect and expect.casefold() not in out.casefold():
        problems.append(f"the reply does not contain {expect!r}")
    if len(out) > 4000:
        problems.append("the reply is implausibly long for a one-sentence prompt")
    if f"[local model: {model_id}" not in err:
        problems.append(f"the banner '[local model: {model_id}' is missing from stderr (the application did not report running this model locally)")
    return problems


def judge(args) -> tuple[str, str, dict]:
    d = Path(args.data_dir)
    model = d / "models" / args.model_id / "model.gguf"
    cmd = args.cmd
    if not model.is_file():
        return "BLOCKED", f"no model file at {model}: nothing was tested", {"model_file": str(model)}
    wp, facts = check_weights(model, args.model_id, args.min_tensors, args.min_bytes, args.expect_sha256)
    if wp:
        return "FAIL", "the model file is not a genuine model: " + "; ".join(wp), facts
    if cmd[0] == sys.executable and not args.allow_test_command:
        return "FAIL", "--cmd is this Python interpreter: a stand-in application is not accepted", facts
    res = run_chat(cmd, d, args.prompt, args.max_tokens, args.timeout)
    facts["chat_seconds"] = res.get("seconds")
    if not res.get("started"):
        return "BLOCKED", "the application command was not found: nothing was tested", facts
    facts["reply_chars"] = len((res.get("stdout") or "").strip())
    facts["reply_head"] = (res.get("stdout") or "").strip()[:160]
    cp = judge_chat(res, args.model_id, args.expect)
    if cp:
        return "FAIL", "the application did not produce a valid reply: " + "; ".join(cp), facts
    return "PASS", (f"the application answered from a genuine model file ({facts['tensor_count']} tensors, {facts['model_bytes']} bytes, "
                    f"{facts.get('architecture')} {facts.get('quantization')}, sha256 {facts['model_sha256'][:16]}...) in {res['seconds']} s; one reply to one prompt, "
                    "not a quality, speed, GPU, voice or other-PC result"), facts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--cmd", default="jarvis", help="the application command (path to jarvis.exe); split on spaces only if it exists as written")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT)
    ap.add_argument("--expect", default=DEFAULT_EXPECT)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--min-tensors", type=int, default=MIN_TENSORS)
    ap.add_argument("--min-bytes", type=int, default=MIN_BYTES)
    ap.add_argument("--expect-sha256", default="", help="optional: the exact SHA-256 the model file must have (pins the model identity)")
    ap.add_argument("--allow-test-command", action="store_true", help="TESTS ONLY: accept this Python interpreter as the application")
    ap.add_argument("--json")
    ap.add_argument("--summary")
    a = ap.parse_args(argv)
    a.cmd = [a.cmd] if Path(a.cmd).exists() or " " not in a.cmd else a.cmd.split()
    verdict, why, facts = judge(a)
    line = f"{verdict}: {why}"
    print(line)
    if a.json:
        Path(a.json).write_text(json.dumps({"verdict": verdict, "why": why, **facts}, indent=2), encoding="utf-8")
    if a.summary:
        with open(a.summary, "a", encoding="utf-8") as f:
            f.write(f"### Real response through the application (`jarvis chat`): **{verdict}**\n\n{why}\n\n")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 3}[verdict]


if __name__ == "__main__":
    sys.exit(main())
