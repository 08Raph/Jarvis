# Hardware and model compatibility (Prompt 19)

**Status: design and logic are tested; NO real model has been run on ANY hardware yet.** Every result column below is "not measured".
Numbers in this file are estimates (file size + KV cache + 1 GB overhead), not measurements.

## What the program decides, and from what
`jarvis.hardware.detect_hardware()` reads OS, CPU threads, RAM, free disk and (NVIDIA only, via `nvidia-smi`) GPU name and VRAM.
`jarvis.inference.gguf_engine.runtime_info()` asks the installed llama-cpp-python whether it was built with a GPU backend
(`gpu_offload_supported`; `None` means the runtime is too old to say). `jarvis.models.selector` combines them.

Three states are never merged:

| State | Meaning | How it is established |
|---|---|---|
| GPU detected | the driver tool reports a GPU | `nvidia-smi` |
| GPU backend available | the installed runtime can offload layers | `llama_supports_gpu_offload()` |
| Acceleration verified | a real model ran faster on GPU than CPU, both numbers recorded | measured tokens/s, GPU > 1.2 x CPU |

The default `llama-cpp-python` wheel from PyPI is a CPU build. A detected RTX card therefore does **nothing** until a GPU-enabled build is
installed (build with CUDA or Vulkan; see the llama-cpp-python documentation) and `n_gpu_layers` is set (`GGUFEngine(..., n_gpu_layers=-1)`).
The selector only counts VRAM toward the hardware tier when the backend is available.

## Tiers (rules in `selector.hardware_tier`)
entry (<12 GB RAM, no usable GPU) / mainstream (12-24 GB RAM or >=4 GB usable VRAM) / performance (>=24 GB RAM or >=8 GB usable VRAM) / high-performance (>=16 GB usable VRAM).

## Selection behaviour
* `prefer="balanced"`: largest model that fits comfortably; stays fully on the GPU when something fits there.
* `prefer="quality"`: largest that fits at all. `prefer="speed"`: smallest.
* A **pinned** model that does not fit is **never replaced silently**: the result says why, lists models that would fit, and leaves the decision to the user.
* No hardware purchase is ever required; the entry tier uses the 1.7B model on CPU.

## Model records (publisher metadata, not run results)
| id | params | quant | est. download | est. memory | licence | metadata status |
|---|---|---|---|---|---|---|
| qwen3-1.7b-q4_k_m | 1.7 B | Q4_K_M | ~1.3 GB | ~3 GB | Apache-2.0 | publisher page read during earlier development |
| qwen3-4b-q4_k_m | 4 B | Q4_K_M | ~2.5 GB | ~5 GB | Apache-2.0 (to confirm) | **not fetched**: publisher unreachable from the build sandbox |
| qwen3-8b-q4_k_m | 8 B | Q4_K_M | ~5.03 GB (rounded page value) | ~12 GB | Apache-2.0 (publisher page tag) | **not fetched** (repository corrected to `Qwen/Qwen3-8B-GGUF`; install on request, never auto-selected) |

The exact file, size and SHA-256 are resolved from the host at download time; confirm the licence on the model page before first use.
`verified_hardware` is empty for every model: nothing has been loaded and answered on any recorded machine.

## Compatibility matrix
`jarvis models plan` prints the live plan; `jarvis.models.selector.matrix_rows()` returns the matrix skeleton. Every row is `NOT RUN`.
Fill a row only from a real run on that hardware. Virtual machines and simulated GPUs are not valid benchmarks.

| Hardware tier | Inference mode | Model | Quality | Performance | Status |
|---|---|---|---|---|---|
| entry | CPU | Qwen3 1.7B Q4 | not measured | not measured | NOT RUN |
| mainstream | CPU | Qwen3 1.7B / 4B Q4 | not measured | not measured | NOT RUN |
| performance | CPU / GPU | Qwen3 4B / 8B Q4 | not measured | not measured | NOT RUN |
| high-performance | GPU | Qwen3 8B Q4 | not measured | not measured | NOT RUN |

## Reproducible path on the target PC (RTX 4070 SUPER, 12 GB; exact CPU generation unconfirmed)
1. Install a GPU-enabled llama-cpp-python (CUDA build) in the project's venv.
2. `jarvis models fetch qwen3-1.7b-q4_k_m` (and optionally the 4B / 8B), accept the licence prompt.
3. Run the staged validation: `python -c "from jarvis.eval import realmodel as r; print(r.format_report(r.validate(r'<model dir>', n_gpu_layers=0)))"`, then again with `n_gpu_layers=-1`.
   Seven stages: downloaded, integrity, runtime imported, loaded, inference, tool call, end-to-end. Real validation is only reported when none was simulated.
4. Record load time, first-reply seconds, tokens/s for both runs. If GPU > 1.2 x CPU, `selector.acceleration_state(hw, runtime, cpu_tps, gpu_tps)` reports "verified".
5. Run `jarvis.eval.suite19.run_all(answer)` with the real model's answer function; keep the JSON.
