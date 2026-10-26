"""Runs a GGUF open-weight model fully on this PC through llama.cpp (llama-cpp-python, MIT license), CPU by default.
No network access. UNTESTED against a real model in the authoring sandbox (no runtime or weights could be installed there);
`jarvis selftest` performs the real check on the user's PC. Tests here use a fake runtime to verify our own logic only."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Iterator

_THINK = re.compile(r"<think>.*?</think>\s*", re.S)


class RuntimeUnavailable(RuntimeError):
    pass


def runtime_info() -> dict:
    try:
        import llama_cpp  # type: ignore
        from jarvis.hardware import cpu_has_avx2
        info = {"available": True, "version": getattr(llama_cpp, "__version__", "unknown"), "cpu_avx2": cpu_has_avx2()}
        try:       # does THIS build of the runtime contain a GPU backend (CUDA/Vulkan/Metal)? The default Windows CPU wheel does not.
            info["gpu_offload_supported"] = bool(llama_cpp.llama_supports_gpu_offload())
        except Exception:
            info["gpu_offload_supported"] = None   # unknown (older runtime)
        if info["cpu_avx2"] is False:
            from jarvis.hardware import AVX2_WARNING
            info["warning"] = AVX2_WARNING
        return info
    except Exception as e:  # ImportError, or a DLL load failure on Windows
        return {"available": False, "error": f"{type(e).__name__}: {e}"}


def default_threads() -> int:
    n = os.cpu_count() or 2
    return max(1, n // 2) if n > 4 else max(1, n - 1)   # leave headroom so Windows stays responsive


def strip_thinking(text: str) -> str:
    text = _THINK.sub("", text)
    return text.split("</think>")[-1].strip() if "</think>" in text else text.strip()


def fit_messages(messages: list[dict], budget_tokens: int, count: Callable[[str], int]) -> list[dict]:
    """Keep the system message(s) and as many of the most recent turns as fit. Never drops the last user message
    (if even that does not fit, the caller gets it anyway and the runtime will report the overflow)."""
    system = [m for m in messages if m["role"] == "system"]
    rest = [m for m in messages if m["role"] != "system"]
    used = sum(count(m["content"]) + 8 for m in system)
    kept: list[dict] = []
    for m in reversed(rest):
        c = count(m["content"]) + 8
        if kept and used + c > budget_tokens:
            break
        kept.append(m)
        used += c
    return system + list(reversed(kept))


class GGUFEngine:
    def __init__(self, model_dir: str | Path, n_ctx: int | None = None, n_threads: int | None = None,
                 llama_factory: Callable | None = None, no_think_suffix: bool | None = None,
                 n_gpu_layers: int | None = None):
        d = Path(model_dir)
        self.path = d / "model.gguf"
        if not self.path.is_file():
            raise RuntimeUnavailable(f"no model.gguf in {d}")
        meta = {}
        if (d / "meta.json").is_file():
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                raise RuntimeUnavailable(f"The model's meta.json in {d} is damaged ({type(e).__name__}); the model file was not touched. "
                                         "Reinstall the model: `jarvis models remove` then `jarvis models fetch ...`.") from e
        self.n_ctx = int(n_ctx or meta.get("context_tokens") or 4096)
        self.no_think = bool(meta.get("no_think_suffix")) if no_think_suffix is None else no_think_suffix
        if llama_factory is None:
            try:
                from llama_cpp import Llama  # type: ignore
            except Exception as e:
                raise RuntimeUnavailable(
                    "The local AI runtime (llama-cpp-python) is not available in this installation "
                    f"({type(e).__name__}: {e}). Reinstall Project JARVIS or see docs/LOCAL_MODEL.md.") from e
            llama_factory = Llama
        if n_gpu_layers is None:          # order: explicit argument > JARVIS_GPU_LAYERS > acceleration.json written by setup (measured) > CPU
            env = os.environ.get("JARVIS_GPU_LAYERS")
            if env is not None:
                try:
                    n_gpu_layers = int(env)
                except ValueError:
                    raise RuntimeUnavailable("JARVIS_GPU_LAYERS must be a whole number (0 = CPU only, -1 = offload everything)") from None
            else:
                from jarvis.inference import accel
                n_gpu_layers = accel.gpu_layers(d.parent)
        t0 = time.perf_counter()
        try:
            kw = {"n_gpu_layers": int(n_gpu_layers)} if n_gpu_layers else {}     # -1 = offload everything the backend can
            self.llm = llama_factory(model_path=str(self.path), n_ctx=self.n_ctx,
                                     n_threads=n_threads or default_threads(), verbose=False, **kw)
        except Exception as e:
            raise RuntimeUnavailable(f"The model file could not be loaded: {type(e).__name__}: {e}") from e
        self.load_seconds = time.perf_counter() - t0
        self.gpu_layers_requested = int(n_gpu_layers)

    def _count(self, text: str) -> int:
        try:
            return len(self.llm.tokenize(text.encode("utf-8"), add_bos=False))
        except Exception:
            return max(1, len(text) // 3)   # conservative fallback

    def _prepare(self, messages: list[dict], max_tokens: int) -> list[dict]:
        msgs = [dict(m) for m in messages]
        if self.no_think and msgs and msgs[-1]["role"] == "user" and not msgs[-1]["content"].rstrip().endswith("/no_think"):
            msgs[-1]["content"] = msgs[-1]["content"] + " /no_think"
        return fit_messages(msgs, max(256, self.n_ctx - max_tokens - 64), self._count)

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.7) -> str:
        r = self.llm.create_chat_completion(messages=self._prepare(messages, max_tokens), max_tokens=max_tokens,
                                            temperature=temperature)
        return strip_thinking(r["choices"][0]["message"]["content"] or "")

    def stream(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.7) -> Iterator[str]:
        buf, in_think = "", False
        for chunk in self.llm.create_chat_completion(messages=self._prepare(messages, max_tokens), max_tokens=max_tokens,
                                                     temperature=temperature, stream=True):
            piece = chunk["choices"][0].get("delta", {}).get("content") or ""
            buf += piece
            # hide any <think>...</think> block even if the model ignores /no_think
            while True:
                if in_think:
                    if "</think>" in buf:
                        buf = buf.split("</think>", 1)[1].lstrip()
                        in_think = False
                        continue
                    buf = buf[-8:] if len(buf) > 8 else buf
                    break
                if "<think>" in buf:
                    out, buf = buf.split("<think>", 1)
                    if out:
                        yield out
                    in_think = True
                    continue
                keep = 7                      # hold back a possible partial "<think>" tag
                if len(buf) > keep:
                    yield buf[:-keep]
                    buf = buf[-keep:]
                break
        if not in_think and buf:
            yield buf
