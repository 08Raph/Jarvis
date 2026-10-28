"""Minimal GGUF header reader (the file format used by llama.cpp). Used to reject files that are not GGUF before they are
installed, and to show the model's name/architecture/context length. It reads only the metadata block and never runs code.
Written from the public GGUF specification; checked against files built by this project's own test writer, NOT yet against
real downloaded models."""
from __future__ import annotations

import struct
from pathlib import Path

MAGIC = b"GGUF"
_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
WANTED_SUFFIXES = ("general.name", "general.architecture", "general.license", "general.file_type", ".context_length",
                   "general.size_label", "general.basename",
                   # values the memory estimate needs (KV cache = 2 x layers x context x KV heads x head size x bytes per element)
                   ".block_count", ".embedding_length", ".attention.head_count", ".attention.head_count_kv", ".attention.key_length",
                   ".attention.value_length", ".expert_count", "tokenizer.chat_template",
                   # what decides whether the runtime can load it and whether it is a chat model at all
                   "general.type", "tokenizer.ggml.model", "tokenizer.ggml.pre", "split.count", "split.no", ".attention.kv_lora_rank")

# general.file_type -> quantisation name. Source: LlamaFileType in the llama.cpp release that llama-cpp-python 0.3.36 bundles (gguf-py/gguf/constants.py,
# commit 0c1e57098). The value is written by whoever made the file: it labels the file, it does not prove how the weights were made.
FILE_TYPES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
              14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K", 19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS",
              23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
              36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4_MOE", 39: "NVFP4", 40: "Q1_0", 41: "Q2_0"}
MAX_STRING = 1 << 20
MAX_KV = 100_000


class GGUFError(ValueError):
    pass


class _R:
    def __init__(self, f):
        self.f = f

    def read(self, n: int) -> bytes:
        b = self.f.read(n)
        if len(b) != n:
            raise GGUFError("file ends inside the GGUF header")
        return b

    def unpack(self, fmt: str):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def string(self) -> str:
        n = self.unpack("<Q")
        if n > MAX_STRING:
            raise GGUFError("implausible string length in GGUF header")
        return self.read(n).decode("utf-8", errors="replace")

    def value(self, t: int, keep: bool):
        if t in _SCALARS:
            return self.unpack(_SCALARS[t])
        if t == 8:
            return self.string()
        if t == 9:
            et, n = self.unpack("<I"), self.unpack("<Q")
            if n > 50_000_000:
                raise GGUFError("implausible array length in GGUF header")
            if et in _SCALARS and not keep:
                self.f.seek(struct.calcsize(_SCALARS[et]) * n, 1)
                return None
            return [self.value(et, False) for _ in range(n)] if keep or et == 8 else None
        raise GGUFError(f"unknown GGUF value type {t}")


def read_header(path: str | Path) -> dict:
    """Return {'version', 'tensor_count', 'metadata': {...selected keys...}}; raises GGUFError if not a sane GGUF file."""
    with open(path, "rb") as f:
        r = _R(f)
        if r.read(4) != MAGIC:
            raise GGUFError("not a GGUF file (missing 'GGUF' signature)")
        version = r.unpack("<I")
        if version not in (2, 3):
            raise GGUFError(f"unsupported GGUF version {version}")
        tensors, kvs = r.unpack("<Q"), r.unpack("<Q")
        if tensors > 1_000_000 or kvs > MAX_KV:
            raise GGUFError("implausible tensor/metadata counts; file looks corrupt")
        meta: dict = {}
        for _ in range(kvs):
            key = r.string()
            t = r.unpack("<I")
            wanted = any(key.endswith(s) or key == s for s in WANTED_SUFFIXES)
            v = r.value(t, wanted)
            if wanted and v is not None:
                meta[key] = v
    return {"version": version, "tensor_count": tensors, "metadata": meta}


def _suffix_value(m: dict, suffix: str):
    return next((v for k, v in m.items() if k.endswith(suffix)), None)


def _as_int(v):
    """A metadata number as a plain int, or None. Per-layer arrays (some architectures) give their largest entry; booleans and negatives are not numbers here."""
    if isinstance(v, list):
        v = max((x for x in v if isinstance(x, int) and not isinstance(x, bool)), default=None)
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def summarize(header: dict) -> dict:
    m = header["metadata"]
    ctx = _suffix_value(m, ".context_length")
    ft = m.get("general.file_type")
    tmpl = m.get("tokenizer.chat_template")
    return {"name": m.get("general.name"), "architecture": m.get("general.architecture"),
            "license": m.get("general.license"), "context_length": ctx, "size_label": m.get("general.size_label"),
            "gguf_version": header.get("version"), "tensor_count": header.get("tensor_count"),
            "file_type": ft if isinstance(ft, int) and not isinstance(ft, bool) else None,
            "quantization": FILE_TYPES.get(ft) if isinstance(ft, int) and not isinstance(ft, bool) else None,
            "block_count": _as_int(_suffix_value(m, ".block_count")), "embedding_length": _as_int(_suffix_value(m, ".embedding_length")),
            "head_count": _as_int(_suffix_value(m, ".attention.head_count")), "head_count_kv": _as_int(_suffix_value(m, ".attention.head_count_kv")),
            "key_length": _as_int(_suffix_value(m, ".attention.key_length")), "value_length": _as_int(_suffix_value(m, ".attention.value_length")),
            "expert_count": _as_int(_suffix_value(m, ".expert_count")),
            "general_type": m.get("general.type") if isinstance(m.get("general.type"), str) else None,
            "tokenizer_model": m.get("tokenizer.ggml.model") if isinstance(m.get("tokenizer.ggml.model"), str) else None,
            "tokenizer_pre": m.get("tokenizer.ggml.pre") if isinstance(m.get("tokenizer.ggml.pre"), str) else None,
            "split_count": _as_int(m.get("split.count")), "kv_lora_rank": _as_int(_suffix_value(m, ".attention.kv_lora_rank")),
            "has_chat_template": isinstance(tmpl, str) and bool(tmpl.strip()), "chat_template_chars": len(tmpl) if isinstance(tmpl, str) else 0}


def model_file(model_dir) -> "Path":
    """The GGUF file of an installed model folder: <folder>/model.gguf for a copied or downloaded model, or the user's own file for a model
    that was only REGISTERED (meta.json "linked": {"path": ...}; nothing was copied). A damaged or unusable record falls back to model.gguf."""
    import json
    from pathlib import Path as _P
    d = _P(model_dir)
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        link = meta.get("linked") if isinstance(meta, dict) else None
        if isinstance(link, dict) and isinstance(link.get("path"), str) and link["path"]:
            return _P(link["path"])
    except (OSError, ValueError):
        pass
    return d / "model.gguf"
