"""Runs a GGUF open-weight model fully on this PC through llama.cpp (llama-cpp-python, MIT license), CPU by default.
No network access. UNTESTED against a real model in the authoring sandbox (no runtime or weights could be installed there);
`jarvis selftest` performs the real check on the user's PC. Tests here use a fake runtime to verify our own logic only."""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

SAMPLING_PROFILES = ("default", "publisher")
DEFAULT_SAMPLING_PROFILE = "default"
_THINK = re.compile(r"<think>.*?</think>\s*", re.S)
CLOSE_WAIT_SECONDS = 5.0          # how long close() waits for a reply that is still being generated before it leaves the native free to the garbage collector
_LOCK_INIT = threading.Lock()


class InferenceError(RuntimeError):
    """The model runtime failed while generating.  Carries only the error type: the library's own message may contain the
    prompt (system instructions, saved notes, conversation), so it is not forwarded."""


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
    if "<think>" in text and "</think>" not in text:
        return ""                      # the reply was cut off while still thinking: partial reasoning is never shown as an answer
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


def _guard_stream(chunks):
    try:
        yield from chunks
    except GeneratorExit:
        raise
    except Exception as e:  # noqa: BLE001
        raise InferenceError(f"The local model failed while generating ({type(e).__name__}).") from None


class GGUFEngine:
    def __init__(self, model_dir: str | Path, n_ctx: int | None = None, n_threads: int | None = None,
                 llama_factory: Callable | None = None, no_think_suffix: bool | None = None,
                 n_gpu_layers: int | None = None, sampling_profile: str | None = None):
        d = Path(model_dir)
        from jarvis.models.gguf import model_file
        self.path = model_file(d)          # <folder>/model.gguf, or the user's own file for a registered (linked) model
        if not self.path.is_file():
            raise RuntimeUnavailable(f"no model.gguf in {d}" if self.path.parent == d else
                                     f"The registered model file is missing or was moved: {self.path.name}. Register it again or remove it from the list.")
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
        self.thinking = False          # True = let a Qwen3 model think before answering (slow on CPU); set by callers who asked for it
        self.model_meta = meta
        self.diag = self.diag_reset()
        self.sampling_profile = "default"
        self.sampling: dict = {}
        self.configure(sampling_profile=sampling_profile or os.environ.get("JARVIS_SAMPLING_PROFILE") or DEFAULT_SAMPLING_PROFILE)
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

    def configure(self, sampling_profile: str | None = None, thinking: bool | None = None) -> None:
        """sampling_profile: "default" = temperature only (Stage 3/4 behaviour); "publisher" = the model publisher's recommended top_p/top_k/min_p
        (and presence penalty) for its non-thinking mode, taken from the model catalog. thinking: allow the hidden reasoning phase."""
        if thinking is not None:
            self.thinking = bool(thinking)
        if sampling_profile is not None:
            if sampling_profile not in SAMPLING_PROFILES:
                raise ValueError(f"unknown sampling profile {sampling_profile!r}; choose from {SAMPLING_PROFILES}")
            self.sampling_profile, self.sampling = sampling_profile, {}
            if sampling_profile == "publisher":
                try:
                    from jarvis.models import catalog
                    cm = catalog.get(str(self.model_meta.get("catalog_id") or self.model_meta.get("id") or self.path.parent.name))
                    self.sampling = dict(cm.sampling)
                except Exception:      # noqa: BLE001 - an unknown model simply has no recommendation: fall back to the default
                    self.sampling = {}

    @staticmethod
    def diag_reset() -> dict:
        """Counters the benchmark reads (diagnostics only; they change no behaviour): replies, generated tokens, replies cut off by the
        token limit, replies blanked because they ended inside an unfinished <think> block."""
        return {"calls": 0, "completion_tokens": 0, "truncated": 0, "blank_unfinished_think": 0}

    def _note(self, r: dict, cleaned: str) -> None:
        try:
            d = self.diag
            d["calls"] += 1
            d["completion_tokens"] += int((r.get("usage") or {}).get("completion_tokens") or 0)
            raw = r["choices"][0]
            d["truncated"] += 1 if raw.get("finish_reason") == "length" else 0
            d["blank_unfinished_think"] += 1 if (not cleaned and "<think>" in (raw["message"]["content"] or "")) else 0
        except Exception:      # noqa: BLE001 - diagnostics must never break a reply
            pass

    def _gen_kwargs(self, temperature: float) -> dict:
        return dict(self.sampling) if temperature and temperature > 0 else {}

    def _count(self, text: str) -> int:
        try:
            return len(self.llm.tokenize(text.encode("utf-8"), add_bos=False))
        except Exception:
            return max(1, len(text) // 3)   # conservative fallback

    def _prepare(self, messages: list[dict], max_tokens: int) -> list[dict]:
        msgs = [dict(m) for m in messages]
        if self.no_think and not self.thinking and msgs and msgs[-1]["role"] == "user" and not msgs[-1]["content"].rstrip().endswith("/no_think"):
            msgs[-1]["content"] = msgs[-1]["content"] + " /no_think"
        return fit_messages(msgs, max(256, self.n_ctx - max_tokens - 64), self._count)

    def _lock_object(self) -> threading.Lock:
        """One lock per engine. llama-cpp-python's Llama object is NOT thread-safe (its own server serialises every request with a lock), so
        two replies, a summary and a reply, or a reply and a shutdown must never reach the same model object at the same time."""
        lk = self.__dict__.get("_gen_lock_obj")
        if lk is None:
            with _LOCK_INIT:
                lk = self.__dict__.setdefault("_gen_lock_obj", threading.Lock())
        return lk

    @contextlib.contextmanager
    def _generating(self):
        """Serialise generation on this engine. A second caller on ANOTHER thread waits its turn; a nested call on the SAME thread (it would wait
        for itself forever) is refused with a clear error instead of hanging."""
        me = threading.get_ident()
        if self.__dict__.get("_gen_owner") == me:
            raise InferenceError("The local model is already producing a reply on this thread. Finish or stop that reply first.")
        lk = self._lock_object()
        lk.acquire()
        try:
            self.__dict__["_gen_owner"] = me
            yield
        finally:
            self.__dict__["_gen_owner"] = None
            lk.release()

    def close(self) -> None:
        """Free the model's memory NOW (llama-cpp-python's own Llama.close()) instead of waiting for garbage collection: an 8B model is over 5 GB, and two
        copies alive at once (a model switch, a restart, a verification run) can exhaust a 16 GB PC. Safe to call twice; the engine cannot generate afterwards.
        New requests are refused immediately. If a reply is still being generated, the native object is not freed underneath it: close() waits up to
        CLOSE_WAIT_SECONDS for it to end and otherwise only drops its reference, so the memory is returned as soon as that reply finishes."""
        with _LOCK_INIT:                  # the swap is one step, so two threads closing at once cannot both get the model object
            llm, self.llm = getattr(self, "llm", None), None
        closer = getattr(llm, "close", None)
        if not callable(closer):
            return
        lk = self._lock_object()
        if self.__dict__.get("_gen_owner") == threading.get_ident() or not lk.acquire(timeout=CLOSE_WAIT_SECONDS):
            return
        try:
            closer()
        except Exception:      # noqa: BLE001 - shutting down must never raise; the reference is dropped either way
            pass
        finally:
            lk.release()

    def _require_loaded(self) -> None:
        if getattr(self, "llm", None) is None:
            raise InferenceError("The local model has been shut down. Start it again to continue.")

    def chat(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.7) -> str:
        with self._generating():
            self._require_loaded()
            llm = self.llm
            try:
                r = llm.create_chat_completion(messages=self._prepare(messages, max_tokens), max_tokens=max_tokens,
                                               temperature=temperature, **self._gen_kwargs(temperature))
            except Exception as e:  # noqa: BLE001
                raise InferenceError(f"The local model failed while generating ({type(e).__name__}).") from None
            out = strip_thinking(r["choices"][0]["message"]["content"] or "")
            self._note(r, out)
            return out

    def stream(self, messages: list[dict], max_tokens: int = 512, temperature: float = 0.7) -> Iterator[str]:
        with self._generating():          # held until the reply ends, is stopped (the generator is closed) or fails
            self._require_loaded()
            llm = self.llm
            buf, in_think = "", False
            try:
                chunks = llm.create_chat_completion(messages=self._prepare(messages, max_tokens), max_tokens=max_tokens,
                                                    temperature=temperature, stream=True, **self._gen_kwargs(temperature))
            except Exception as e:  # noqa: BLE001
                raise InferenceError(f"The local model failed while generating ({type(e).__name__}).") from None
            for chunk in _guard_stream(chunks):
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
