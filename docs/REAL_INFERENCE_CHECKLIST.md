# Running the real-inference test (REAL_INFERENCE_CHECKLIST)

**Status (corrected 2026-10-29, see `docs/VERIFICATION_LEDGER.md`):** a genuine GGUF model (Qwen3-1.7B Q4_K_M) WAS loaded and answered through the real llama.cpp runtime on a hosted Windows CI runner on 2026-10-26/27 (runs 40422256861, 40431278616, older commit bee5873). It has NOT been done with the current code, on a person's PC, or by THIS test (`RealRuntime`), and the authoring sandbox has neither a runtime nor weights. Every other model test in the unit suite uses a scripted stand-in runtime. This page is the exact sequence for running the one test that does not (`test_p73_inference_behaviour.RealRuntime`) as soon as both
prerequisites exist. A skipped test is not a pass, and a run that stops at a prerequisite check proves nothing about inference.

## What the test does (and does not) prove
`RealRuntime.test_load_reply_stream_stop_and_reuse` loads a real `.gguf` through `GGUFEngine` and the real `llama_cpp.Llama`, then checks: the file has weight tensors; `llama_cpp` is
backed by a loaded shared library and `engine.llm` is a genuine `Llama`; a non-streaming reply is non-empty text; a stream yields text and can be stopped (generator closed) and the model
answers again afterwards; a stream run to the end yields text. It proves load, generation, streaming and stop/reuse **on this machine with this model**. It says nothing about answer
quality, speed on other PCs, the GUI, the microphone, or Windows 11 unless you run it there.

## Prerequisites (both are needed; nothing else substitutes)
1. **A working `llama-cpp-python` runtime**, pinned `0.3.36` (`pip install -e ".[local]"` in a virtual environment, or any environment where `import llama_cpp` works). CPU with AVX2 for the default wheel.
2. **A genuine chat-capable `.gguf` with weights** (size is in the hundreds of MB or more). Vocabulary-only files (such as llama.cpp's `models/ggml-vocab-*.gguf`) and Jarvis's synthetic test fixtures have **0 tensors** and cannot generate text; the test refuses them. Get a model the supported way, on a PC where you agree to the download: `jarvis models fetch qwen3-1.7b-q4_k_m --yes` (about 1.28 GB, Apache-2.0, checked by SHA-256), then use `<data dir>\models\qwen3-1.7b-q4_k_m\model.gguf`; or point at a `.gguf` you already have.

## Step 0: prerequisite check (save as `check_prereqs.py`, run from the repository root)
```python
import importlib.util, os, sys
sys.path.insert(0, "src")
ok = True
print("python:", sys.version.split()[0])
if importlib.util.find_spec("llama_cpp") is None:
    print("MISSING 1/2: llama-cpp-python is not installed (pip install llama-cpp-python==0.3.36, or use the installed Jarvis build's Python)"); ok = False
else:
    try:
        import llama_cpp
        print("runtime: llama_cpp", llama_cpp.__version__, "| shared library loaded:", getattr(llama_cpp.llama_cpp, "_lib", None) is not None)
    except Exception as e:
        print("MISSING 1/2: llama_cpp is installed but does not load:", type(e).__name__, e); ok = False
p = os.environ.get("JARVIS_REAL_MODEL_PATH", "")
if not p or not os.path.isfile(p):
    print("MISSING 2/2: JARVIS_REAL_MODEL_PATH is not set to an existing .gguf file (got %r)" % p); ok = False
else:
    from jarvis.models import gguf as G
    try:
        i = G.summarize(G.read_header(p))
    except Exception as e:
        print("MISSING 2/2: not a readable GGUF:", e); ok = False
    else:
        print("model:", os.path.basename(p), os.path.getsize(p), "bytes |", i["name"], i["architecture"], i["quantization"], "| tensors:", i["tensor_count"])
        if not i["tensor_count"]:
            print("MISSING 2/2: this file has no weight tensors (vocabulary-only or synthetic); it cannot generate text"); ok = False
print("READY: both prerequisites present" if ok else "NOT READY: stop here; nothing below can be a real test")
sys.exit(0 if ok else 1)
```
Expected on a machine that is NOT ready (this is the actual output on the authoring sandbox): `MISSING 1/2 ...`, `MISSING 2/2 ...`, `NOT READY: stop here`, exit code 1. Do not continue past a NOT READY.

## Step 1: run the test
Windows PowerShell:
```powershell
$env:JARVIS_REAL_MODEL_TEST = "1"
$env:JARVIS_REAL_MODEL_PATH = "C:\path\to\model.gguf"
$env:JARVIS_REAL_MODEL_REPORT = "$PWD\real_inference_report.json"
python -m unittest -v tests.test_p73_inference_behaviour.RealRuntime
```
Linux/macOS shell (from the repository root; the test modules import each other, so run it from `tests`):
```bash
cd tests
JARVIS_REAL_MODEL_TEST=1 JARVIS_REAL_MODEL_PATH=/path/to/model.gguf JARVIS_REAL_MODEL_REPORT=$PWD/real_inference_report.json \
  python3 -m unittest -v test_p73_inference_behaviour.RealRuntime
```
A 1.7B Q4 model on a modern CPU should finish in well under a minute after loading; loading itself takes seconds to tens of seconds.

## Step 2: what a real pass looks like
`OK` with **0 skipped** for `test_load_reply_stream_stop_and_reuse`, and a report file with this shape (values are examples of the fields, not results):
```json
{
 "model_file": "model.gguf", "model_bytes": <hundreds of millions or more>, "model_sha256": "<64 hex>",
 "gguf": {"name": "...", "architecture": "qwen3", "quantization": "Q4_K_M", "context_length": <n>, "tensor_count": <> 0>, "block_count": <n>},
 "llama_cpp_version": "0.3.36", "gpu_offload_supported": false, "n_ctx": 4096, "n_gpu_layers_requested": 0,
 "load_seconds": <s>, "chat_seconds": <s>, "first_stream_piece_seconds": <s>,
 "reply_chars": <> 0>, "first_stream_piece": "...", "reply_after_stop_chars": <> 0>, "full_stream_chars": <> 0>
}
```
Anything else is not a pass: `skipped` means a prerequisite was missing; a failure naming "no weight tensors" or "not backed by a loaded llama.cpp shared library" means the inputs were not genuine.

## Alternative: the existing CI route (costs a 1.28 GB download on the runner)
Run the **windows-ci** workflow by hand and type exactly `yes-download-qwen3-1.7b-q4_k_m` in the consent box. That downloads the model and runs the *packaged* `jarvis.exe selftest` on it (a different check from the unit test above: it covers the frozen app's worker process, not `RealRuntime`). Its result is in the run summary under "Real inference through the packaged worker" as PASS / FAIL / BLOCKED. Only a person should type the consent value.

## After a pass
Record the report file and the run's date in `docs/ACCEPTANCE_TEST_PLAN.md` (part 4) and change the "never run" statements only to what the report supports (model, runtime version, this machine).
