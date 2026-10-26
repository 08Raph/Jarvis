# Model sizes and estimated requirements

ESTIMATES from parameter counts (fp32 weights x 1.25 + 0.5 GB runtime allowance for RAM). Nothing here has been measured on real hardware, and no trained model exists yet; these are planned sizes.
Speed (tokens per second) is NOT measured. A GPU is optional; only NVIDIA GPUs are auto-detected.

| Tier | Config (layers / width / context / vocab) | Parameters | Model file | Min RAM | Recommended RAM | Min VRAM if GPU |
|---|---|---|---|---|---|---|
| jarvis-lm-tiny | 4 / 128 / 128 / 1024 | 0.9M | 4 MB | 0.5 GB | 4.0 GB | 0.3 GB |
| jarvis-lm-small | 8 / 512 / 512 / 8192 | 29.7M | 119 MB | 0.6 GB | 4.0 GB | 0.4 GB |
| jarvis-lm-base | 12 / 768 / 1024 / 32000 | 110.4M | 442 MB | 1.1 GB | 4.0 GB | 0.9 GB |

Disk: model file x 2 free space is required during install (the previous version is kept for rollback). The application itself (EXE + bundled Python/PyTorch runtime) has not been built, so its size is unknown; a PyTorch-based build is expected to be hundreds of MB.

CPU: any 64-bit Windows 11 CPU; larger tiers will be slow on CPU. First run: `jarvis first-run` creates folders, detects hardware, and installs nothing. Then install a model package with `jarvis models install`.
