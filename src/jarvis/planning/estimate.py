"""Back-of-envelope compute planning for GPT training runs.

These are ESTIMATES from standard rules of thumb (FLOPs ~ 6*N*D, AdamW fp32
state = 16 bytes/param). Real throughput varies; measure on your hardware.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

# Peak fp32 TFLOPs, rough public spec-sheet numbers used only as planning inputs.
HARDWARE_PEAK_TFLOPS = {"cpu_laptop": 0.3, "rtx_3060": 12.7, "rtx_4090": 82.6, "a100_40gb": 19.5}


def gpt_params(vocab_size: int, block_size: int, n_layer: int, n_embd: int) -> int:
    """Parameter count of jarvis.model.gpt.GPT (tied output head)."""
    d = n_embd
    per_layer = 12 * d * d + 9 * d  # qkv+proj (4d^2, no bias) + mlp (8d^2+5d) + 2 layernorms (4d)
    return vocab_size * d + block_size * d + n_layer * per_layer + 2 * d


@dataclass
class RunEstimate:
    params: int
    tokens: int
    flops: float
    hours: float
    train_state_gb: float
    activation_gb: float
    checkpoint_mb: float
    min_tokens_chinchilla: int
    tokens_per_param: float
    notes: list[str]


def estimate_run(params: int, tokens: int, n_layer: int, n_embd: int, block_size: int,
                 batch_size: int, hardware: str = "cpu_laptop", mfu: float = 0.3) -> RunEstimate:
    peak = HARDWARE_PEAK_TFLOPS[hardware]
    flops = 6.0 * params * tokens
    hours = flops / (peak * mfu * 1e12) / 3600.0
    state_gb = params * 16 / 1e9
    act_gb = batch_size * block_size * n_embd * n_layer * 34 * 4 / 1e9  # coarse, fp32, no recompute
    notes = []
    tpp = tokens / params
    if tpp < 20:
        notes.append(f"{tpp:.1f} tokens/param is below the ~20 tokens/param compute-optimal rule of thumb: "
                     "expect an under-trained model for this size.")
    notes.append(f"Assumes {hardware} peak {peak} TFLOPs at {mfu:.0%} utilization; both are assumptions.")
    return RunEstimate(params, tokens, flops, hours, state_gb, act_gb, params * 4 / 1e6, params * 20, tpp, notes)


def format_estimate(name: str, e: RunEstimate) -> str:
    return (f"{name}: {e.params / 1e6:.2f}M params, {e.tokens / 1e6:.1f}M tokens, "
            f"{e.flops:.2e} FLOPs, ~{e.hours:.2f} h; weights+grads+Adam {e.train_state_gb:.2f} GB, "
            f"activations ~{e.activation_gb:.2f} GB, checkpoint ~{e.checkpoint_mb:.0f} MB; "
            f"compute-optimal data ~{e.min_tokens_chinchilla / 1e6:.0f}M tokens")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vocab", type=int, default=1024)
    ap.add_argument("--block", type=int, default=128)
    ap.add_argument("--layers", type=int, default=4)
    ap.add_argument("--embd", type=int, default=128)
    ap.add_argument("--tokens", type=int, default=2_000_000)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--hardware", default="cpu_laptop", choices=sorted(HARDWARE_PEAK_TFLOPS))
    a = ap.parse_args()
    p = gpt_params(a.vocab, a.block, a.layers, a.embd)
    e = estimate_run(p, a.tokens, a.layers, a.embd, a.block, a.batch, a.hardware)
    print(format_estimate("run", e))
    for n in e.notes:
        print(" -", n)


if __name__ == "__main__":
    main()
