"""Local inference engine. Never fakes output: with no trained model it raises."""
from __future__ import annotations

import json
from pathlib import Path

from jarvis.tokenizer.bpe import BPETokenizer

PROMPT_TEMPLATE = "User: {user}\nJarvis:"


class ModelNotAvailable(RuntimeError):
    """Raised when no trained checkpoint (or torch) is present."""


def model_status(model_dir: str | Path) -> dict:
    d = Path(model_dir)
    have = all((d / n).exists() for n in ("model.pt", "tokenizer.json"))
    info: dict = {"trained_model_present": have, "model_dir": str(d)}
    mpath = d / "metrics.json"
    if have and mpath.exists():
        m = json.loads(mpath.read_text(encoding="utf-8"))
        info.update(params=m.get("params"), test_loss=m.get("test_loss"), best_val_loss=m.get("best_val_loss"))
    return info


class InferenceEngine:
    def __init__(self, model_dir: str | Path) -> None:
        d = Path(model_dir)
        if not (d / "model.pt").exists() or not (d / "tokenizer.json").exists():
            raise ModelNotAvailable(f"no trained model found in {d}. Train one first (see README).")
        try:
            import torch
        except ImportError as exc:
            raise ModelNotAvailable("PyTorch is not installed; cannot run the model") from exc
        from jarvis.model.gpt import GPT, GPTConfig

        self._torch = torch
        self.tokenizer = BPETokenizer.load(d / "tokenizer.json")
        ckpt = torch.load(d / "model.pt", map_location="cpu", weights_only=True)
        if ckpt.get("tokenizer_fingerprint") != self.tokenizer.fingerprint:
            raise ModelNotAvailable("tokenizer does not match checkpoint (fingerprint mismatch)")
        self.model = GPT(GPTConfig(**ckpt["config"]))
        self.model.load_state_dict(ckpt["model"])
        self.model.eval()
        # A pretrained base model never saw the chat template or <bos>; only use them if the
        # checkpoint was explicitly produced by instruction tuning (stage 3, not built yet).
        self.chat_format = bool(ckpt.get("chat_format", False))

    def generate_scored(self, user_text: str, max_new_tokens: int = 128, temperature: float = 0.8,
                        top_k: int = 40) -> tuple[str, float]:
        """Return (text, mean log-probability of the sampled tokens under the raw model)."""
        torch = self._torch
        F = torch.nn.functional
        prompt = PROMPT_TEMPLATE.format(user=user_text) if self.chat_format else user_text
        ids = self.tokenizer.encode(prompt, bos=self.chat_format)
        if not ids:
            raise ValueError("empty prompt")
        idx = torch.tensor([ids], dtype=torch.long)
        logps: list[float] = []
        with torch.no_grad():
            for _ in range(max_new_tokens):
                logits, _ = self.model(idx[:, -self.model.config.block_size:])
                logits = logits[:, -1, :]
                raw_logp = F.log_softmax(logits, dim=-1)
                z = logits / max(temperature, 1e-5)
                if top_k > 0:
                    kth = torch.topk(z, min(top_k, z.size(-1))).values[:, -1:]
                    z = z.masked_fill(z < kth, float("-inf"))
                nxt = torch.multinomial(F.softmax(z, dim=-1), num_samples=1)
                tok_id = int(nxt[0, 0])
                if tok_id == self.tokenizer.eos_id:
                    break
                logps.append(float(raw_logp[0, tok_id]))
                idx = torch.cat([idx, nxt], dim=1)
        text = self.tokenizer.decode(idx[0].tolist()[len(ids):]).strip()
        return text, (sum(logps) / len(logps) if logps else float("-inf"))

    def generate(self, user_text: str, max_new_tokens: int = 128, temperature: float = 0.8,
                 top_k: int = 40) -> str:
        return self.generate_scored(user_text, max_new_tokens, temperature, top_k)[0]
