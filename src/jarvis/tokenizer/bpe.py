"""Original byte-level BPE tokenizer (pure Python, no third-party dependencies).

Every byte (0-255) is a base token, so any text round-trips losslessly.
Merges are learned from the training corpus; special tokens are appended after
the merge vocabulary and are never produced by encoding plain text.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

SPECIAL_TOKENS = ("<pad>", "<bos>", "<eos>", "<unk>")

_WORD_CHARS = r"A-Za-z0-9_À-￿"
_PRETOKEN = re.compile(rf" ?[{_WORD_CHARS}]+| ?[^\s{_WORD_CHARS}]+|\s+")


def _merge(seq: tuple[int, ...], pair: tuple[int, int], new_id: int) -> tuple[int, ...]:
    out: list[int] = []
    i = 0
    n = len(seq)
    while i < n:
        if i < n - 1 and seq[i] == pair[0] and seq[i + 1] == pair[1]:
            out.append(new_id)
            i += 2
        else:
            out.append(seq[i])
            i += 1
    return tuple(out)


class BPETokenizer:
    def __init__(self, merges: list[tuple[int, int]] | tuple = ()) -> None:
        self.merges: list[tuple[int, int]] = [tuple(m) for m in merges]  # type: ignore[misc]
        self._ranks = {m: i for i, m in enumerate(self.merges)}
        self._vocab: dict[int, bytes] = {i: bytes([i]) for i in range(256)}
        for i, (a, b) in enumerate(self.merges):
            self._vocab[256 + i] = self._vocab[a] + self._vocab[b]
        base = 256 + len(self.merges)
        self.special: dict[str, int] = {t: base + i for i, t in enumerate(SPECIAL_TOKENS)}
        self._special_inv = {v: k for k, v in self.special.items()}
        self._cache: dict[bytes, tuple[int, ...]] = {}

    # ------------------------------------------------------------------ props
    @property
    def vocab_size(self) -> int:
        return 256 + len(self.merges) + len(SPECIAL_TOKENS)

    @property
    def pad_id(self) -> int:
        return self.special["<pad>"]

    @property
    def bos_id(self) -> int:
        return self.special["<bos>"]

    @property
    def eos_id(self) -> int:
        return self.special["<eos>"]

    @property
    def fingerprint(self) -> str:
        """Stable hash of the merge table; stored in checkpoints to detect mismatches."""
        blob = json.dumps(self.merges, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    # --------------------------------------------------------------- training
    @classmethod
    def train(cls, text: str, vocab_size: int) -> "BPETokenizer":
        floor = 256 + len(SPECIAL_TOKENS)
        if vocab_size < floor:
            raise ValueError(f"vocab_size must be >= {floor}")
        n_merges = vocab_size - floor
        words: Counter[tuple[int, ...]] = Counter(
            tuple(w.encode("utf-8")) for w in _PRETOKEN.findall(text)
        )
        merges: list[tuple[int, int]] = []
        for _ in range(n_merges):
            pairs: Counter[tuple[int, int]] = Counter()
            for w, c in words.items():
                for p in zip(w, w[1:]):
                    pairs[p] += c
            if not pairs:
                break
            best, count = max(pairs.items(), key=lambda kv: (kv[1], (-kv[0][0], -kv[0][1])))
            if count < 2:
                break
            new_id = 256 + len(merges)
            merges.append(best)
            merged: Counter[tuple[int, ...]] = Counter()
            for w, c in words.items():
                merged[_merge(w, best, new_id)] += c
            words = merged
        return cls(merges)

    # --------------------------------------------------------------- encoding
    def _encode_word(self, word: bytes) -> tuple[int, ...]:
        hit = self._cache.get(word)
        if hit is not None:
            return hit
        ids = tuple(word)
        while len(ids) > 1:
            best_pair = None
            best_rank = None
            for p in zip(ids, ids[1:]):
                r = self._ranks.get(p)
                if r is not None and (best_rank is None or r < best_rank):
                    best_pair, best_rank = p, r
            if best_pair is None:
                break
            ids = _merge(ids, best_pair, 256 + best_rank)  # type: ignore[operator]
        self._cache[word] = ids
        return ids

    def encode(self, text: str, bos: bool = False, eos: bool = False) -> list[int]:
        out: list[int] = [self.bos_id] if bos else []
        for w in _PRETOKEN.findall(text):
            out.extend(self._encode_word(w.encode("utf-8")))
        if eos:
            out.append(self.eos_id)
        return out

    def decode(self, ids: list[int], skip_special: bool = True) -> str:
        chunks: list[bytes] = []
        for i in ids:
            if i in self._special_inv:
                if not skip_special:
                    chunks.append(self._special_inv[i].encode())
                continue
            tok = self._vocab.get(i)
            if tok is None:
                raise ValueError(f"unknown token id {i}")
            chunks.append(tok)
        return b"".join(chunks).decode("utf-8", errors="replace")

    # ------------------------------------------------------------ persistence
    def save(self, path: str | Path) -> None:
        payload = {
            "type": "byte-bpe",
            "version": 1,
            "special": list(SPECIAL_TOKENS),
            "merges": self.merges,
        }
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "BPETokenizer":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("type") != "byte-bpe" or data.get("version") != 1:
            raise ValueError("unsupported tokenizer file")
        return cls([tuple(m) for m in data["merges"]])
