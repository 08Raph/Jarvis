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
                   "general.size_label", "general.basename")
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


def summarize(header: dict) -> dict:
    m = header["metadata"]
    ctx = next((v for k, v in m.items() if k.endswith(".context_length")), None)
    return {"name": m.get("general.name"), "architecture": m.get("general.architecture"),
            "license": m.get("general.license"), "context_length": ctx, "size_label": m.get("general.size_label")}
