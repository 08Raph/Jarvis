"""Stage 5 probe: can the bundled llama.cpp runtime load this installed GGUF and answer? Prints facts only (no benchmark)."""
import json
import sys
import time
from pathlib import Path

from jarvis.inference.gguf_engine import GGUFEngine, runtime_info

data, mid = Path(sys.argv[1]), sys.argv[2]
out = {"model_id": mid, "runtime": runtime_info()}
d = data / "models" / mid
try:
    eng = GGUFEngine(d)
    out["load_seconds"] = round(eng.load_seconds, 2)
    md = getattr(eng.llm, "metadata", {}) or {}
    out["architecture"] = md.get("general.architecture")
    out["has_chat_template"] = "tokenizer.chat_template" in md
    tpl = md.get("tokenizer.chat_template", "") or ""
    out["chat_template_mentions_enable_thinking"] = "enable_thinking" in tpl
    out["chat_template_head"] = tpl[:160]
    out["n_ctx"] = eng.n_ctx
    for label, prompt in (("capital", "What is the capital of France? Answer in one short sentence."), ("math", "What is 17 * 23? Show the final number.")):
        t0 = time.perf_counter()
        raw = eng.llm.create_chat_completion(messages=[{"role": "user", "content": prompt}], max_tokens=200, temperature=0.0)
        dt = time.perf_counter() - t0
        txt = raw["choices"][0]["message"]["content"] or ""
        out[label] = {"seconds": round(dt, 1), "completion_tokens": raw.get("usage", {}).get("completion_tokens"), "raw_head": txt[:300],
                      "starts_with_think": txt.lstrip().startswith("<think>")}
    out["ok"] = True
except Exception as e:     # noqa: BLE001 - the whole point is to record why it failed
    out["ok"] = False
    out["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(out, indent=2))
