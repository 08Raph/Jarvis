"""Resumable language-model pretraining from random initialization.

STATUS: written but NOT executed in the authoring sandbox (no PyTorch there).
Verify with `python scripts/smoke_train.py` on a machine with torch.

Evaluation integrity: the tokenizer is trained on train.txt only; val.txt drives
checkpoint selection / early stopping; test.txt is evaluated exactly once, at the
end, using the best-validation weights.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from jarvis.tokenizer.bpe import BPETokenizer


@dataclass
class TrainConfig:
    data_dir: str
    out_dir: str
    vocab_size: int = 1024
    block_size: int = 128
    n_layer: int = 4
    n_head: int = 4
    n_embd: int = 128
    dropout: float = 0.1
    batch_size: int = 16
    grad_accum: int = 1
    lr: float = 3e-4
    min_lr_frac: float = 0.1
    warmup_steps: int = 50
    max_steps: int = 1000
    eval_interval: int = 50
    eval_iters: int = 20
    ckpt_interval: int = 100
    patience: int = 5            # evals without val improvement before early stop
    overfit_ratio: float = 1.3   # flag when val_loss / train_loss exceeds this
    seed: int = 1337
    device: str | None = None
    resume: bool = False


def _atomic_save(torch, obj, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(obj, tmp)
    os.replace(tmp, path)


def _tokens(tok: BPETokenizer, path: Path) -> np.ndarray:
    text = path.read_text(encoding="utf-8")
    return np.asarray(tok.encode(text), dtype=np.int32) if text else np.zeros(0, dtype=np.int32)


def _lr(cfg: TrainConfig, step: int) -> float:
    if step < cfg.warmup_steps:
        return cfg.lr * (step + 1) / cfg.warmup_steps
    prog = (step - cfg.warmup_steps) / max(1, cfg.max_steps - cfg.warmup_steps)
    floor = cfg.lr * cfg.min_lr_frac
    return floor + 0.5 * (cfg.lr - floor) * (1 + math.cos(math.pi * min(1.0, prog)))


def run(cfg: TrainConfig) -> dict:
    import torch
    from jarvis.model.gpt import GPT, GPTConfig

    data, out = Path(cfg.data_dir), Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    device = cfg.device or ("cuda" if torch.cuda.is_available() else "cpu")

    tok_path = out / "tokenizer.json"
    # Guard against a stale tokenizer/checkpoint trained on a different split (would leak test data).
    meta_path = out / "data_hashes.json"
    hashes = {s: hashlib.sha256((data / f"{s}.txt").read_bytes()).hexdigest() for s in ("train", "val", "test")}
    if meta_path.exists():
        if json.loads(meta_path.read_text(encoding="utf-8")) != hashes:
            raise RuntimeError("the prepared dataset differs from the one this output folder was started with; "
                               "use a fresh out_dir so old tokenizers/checkpoints cannot leak held-out data")
    elif tok_path.exists():
        raise RuntimeError("out_dir already has a tokenizer but no dataset record; use a fresh out_dir")
    meta_path.write_text(json.dumps(hashes), encoding="utf-8")
    if tok_path.exists():
        tok = BPETokenizer.load(tok_path)
    else:
        tok = BPETokenizer.train((data / "train.txt").read_text(encoding="utf-8"), cfg.vocab_size)
        tok.save(tok_path)
    train_ids, val_ids, test_ids = (_tokens(tok, data / f"{s}.txt") for s in ("train", "val", "test"))
    need = cfg.block_size + 2
    if len(train_ids) < need or len(val_ids) < need:
        raise RuntimeError("train/val split too small for this block_size; add approved data or reduce block_size")

    mcfg = GPTConfig(vocab_size=tok.vocab_size, block_size=cfg.block_size, n_layer=cfg.n_layer,
                     n_head=cfg.n_head, n_embd=cfg.n_embd, dropout=cfg.dropout)

    random.seed(cfg.seed)
    np_rng = np.random.default_rng(cfg.seed)
    torch.manual_seed(cfg.seed)
    model = GPT(mcfg).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=0.1)

    step, best_val, bad_evals, history, overfit_flags = 0, float("inf"), 0, [], []
    latest = out / "latest.pt"
    if cfg.resume and latest.exists():
        ck = torch.load(latest, map_location=device, weights_only=False)  # our own trusted file
        if ck["tokenizer_fingerprint"] != tok.fingerprint or ck["model_config"] != asdict(mcfg):
            raise RuntimeError("checkpoint does not match current tokenizer/model config; refusing to resume")
        model.load_state_dict(ck["model"])
        opt.load_state_dict(ck["opt"])
        step, best_val, bad_evals = ck["step"], ck["best_val"], ck["bad_evals"]
        history, overfit_flags = ck["history"], ck["overfit_flags"]
        np_rng.bit_generator.state = ck["np_rng"]
        torch.set_rng_state(ck["torch_rng"].cpu())
        random.setstate(ck["py_rng"])
    elif cfg.resume:
        print("resume requested but no checkpoint found; starting fresh", file=sys.stderr)

    (out / "run_config.json").write_text(json.dumps({
        "config": asdict(cfg), "model_config": asdict(mcfg), "seed": cfg.seed,
        "tokenizer_fingerprint": tok.fingerprint, "device": device, "python": sys.version,
        "torch": torch.__version__, "numpy": np.__version__, "platform": platform.platform(),
        "started_at": datetime.now(timezone.utc).isoformat(), "resumed_from_step": step,
    }, indent=2), encoding="utf-8")

    def batch(arr: np.ndarray):
        ix = np_rng.integers(0, len(arr) - cfg.block_size - 1, size=cfg.batch_size)
        x = np.stack([arr[i:i + cfg.block_size] for i in ix])
        y = np.stack([arr[i + 1:i + 1 + cfg.block_size] for i in ix])
        return torch.from_numpy(x).long().to(device), torch.from_numpy(y).long().to(device)

    @torch.no_grad()
    def loss_on(arr: np.ndarray) -> float:
        model.eval()
        v = float(np.mean([model(*batch(arr))[1].item() for _ in range(cfg.eval_iters)]))
        model.train()
        return v

    def save_latest():
        _atomic_save(torch, {
            "model": model.state_dict(), "opt": opt.state_dict(), "step": step, "best_val": best_val,
            "bad_evals": bad_evals, "history": history, "overfit_flags": overfit_flags,
            "np_rng": np_rng.bit_generator.state, "torch_rng": torch.get_rng_state(),
            "py_rng": random.getstate(), "tokenizer_fingerprint": tok.fingerprint,
            "model_config": asdict(mcfg)}, latest)

    metrics_log = out / "metrics.jsonl"
    stopped_early = False
    t0 = time.time()
    model.train()
    while step < cfg.max_steps:
        for g in opt.param_groups:
            g["lr"] = _lr(cfg, step)
        opt.zero_grad(set_to_none=True)
        for _ in range(cfg.grad_accum):
            _, loss = model(*batch(train_ids))
            (loss / cfg.grad_accum).backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        step += 1

        if step % cfg.eval_interval == 0 or step == cfg.max_steps:
            tr, va = loss_on(train_ids), loss_on(val_ids)
            rec = {"step": step, "train_loss": tr, "val_loss": va, "lr": _lr(cfg, step),
                   "elapsed_s": round(time.time() - t0, 1)}
            if va / max(tr, 1e-9) > cfg.overfit_ratio and (not history or va >= history[-1]["val_loss"]):
                rec["overfit_warning"] = True
                overfit_flags.append(step)
            history.append(rec)
            with metrics_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
            if va < best_val:
                best_val, bad_evals = va, 0
                _atomic_save(torch, {"model": model.state_dict(), "config": asdict(mcfg),
                                     "tokenizer_fingerprint": tok.fingerprint, "step": step}, out / "best.pt")
            else:
                bad_evals += 1
                if bad_evals >= cfg.patience:
                    stopped_early = True
                    save_latest()
                    break
        if step % cfg.ckpt_interval == 0:
            save_latest()
    save_latest()

    # Final: load best-val weights, touch the test split exactly once.
    best = torch.load(out / "best.pt", map_location=device, weights_only=True)
    model.load_state_dict(best["model"])
    test_loss = loss_on(test_ids) if len(test_ids) >= need else None
    _atomic_save(torch, {"model": best["model"], "config": asdict(mcfg),
                         "tokenizer_fingerprint": tok.fingerprint}, out / "model.pt")
    metrics = {
        "finished_at": datetime.now(timezone.utc).isoformat(), "params": model.num_params(),
        "steps_completed": step, "stopped_early": stopped_early, "best_val_loss": best_val,
        "best_step": best["step"], "test_loss": test_loss,
        "test_perplexity": math.exp(test_loss) if test_loss is not None else None,
        "overfit_warning_steps": overfit_flags, "device": device, "seed": cfg.seed,
        "tokens": {"train": int(len(train_ids)), "val": int(len(val_ids)), "test": int(len(test_ids))},
        "history": history, "final": history[-1] if history else None,
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics
