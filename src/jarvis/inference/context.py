"""Fit a conversation into the model's fixed context window.

A model can only read `block_size` tokens at once; nothing here makes that larger. Long sessions
stay usable by (1) retrieving only the stored notes relevant to the current message (BM25) and
(2) dropping the OLDEST turns first. Dropped turns remain in the SQLite store and can be retrieved later.
The chat format is only meaningful for an instruction-tuned model (stage 3, not built yet).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN = re.compile(r"\w+", re.UNICODE)


def bm25_rank(query: str, docs: list[str], k: int = 3, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
    """Return up to k (doc_index, score) pairs with positive score, best first."""
    q = _TOKEN.findall(query.lower())
    toks = [_TOKEN.findall(d.lower()) for d in docs]
    n = len(docs)
    if not q or not n:
        return []
    avg = (sum(len(t) for t in toks) / n) or 1.0
    df = Counter(w for t in toks for w in set(t))
    scores = []
    for i, t in enumerate(toks):
        tf = Counter(t)
        s = 0.0
        for w in set(q):
            if tf[w]:
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        if s > 0:
            scores.append((i, s))
    return sorted(scores, key=lambda x: -x[1])[:k]


@dataclass
class ContextResult:
    prompt: str
    n_tokens: int
    turns_included: int
    turns_dropped: int
    notes_used: list[str] = field(default_factory=list)
    user_text_truncated: bool = False


class ContextBuilder:
    def __init__(self, tokenizer, block_size: int, reserve_new_tokens: int = 128, header: str = "") -> None:
        if reserve_new_tokens >= block_size:
            raise ValueError("reserve_new_tokens must be smaller than block_size")
        self.tok = tokenizer
        self.budget = block_size - reserve_new_tokens
        self.header = header

    def _n(self, text: str) -> int:
        return len(self.tok.encode(text))

    def build(self, user_text: str, history: list[dict], notes: list[str] | None = None,
              max_notes: int = 3) -> ContextResult:
        final = f"User: {user_text}\nJarvis:"
        truncated = False
        if self._n(final) > self.budget:  # keep the END of an oversized message
            truncated = True
            lo, hi = 0, len(user_text)
            while lo < hi:
                mid = (lo + hi) // 2
                if self._n(f"User: {user_text[mid:]}\nJarvis:") <= self.budget:
                    hi = mid
                else:
                    lo = mid + 1
            final = f"User: {user_text[lo:]}\nJarvis:"
        used = self._n(final) + (self._n(self.header + "\n") if self.header else 0)

        chosen_notes: list[str] = []
        for idx, _ in bm25_rank(user_text, notes or [], k=max_notes):
            line = f"Note: {notes[idx]}\n"  # type: ignore[index]
            c = self._n(line)
            if used + c <= self.budget:
                chosen_notes.append(line)
                used += c

        turns: list[str] = []
        for m in reversed(history):  # newest first; stop at the first turn that does not fit
            label = "User" if m["role"] == "user" else "Jarvis"
            line = f"{label}: {m['content']}\n"
            c = self._n(line)
            if used + c > self.budget:
                break
            turns.append(line)
            used += c
        turns.reverse()

        parts = ([self.header + "\n"] if self.header else []) + chosen_notes + turns + [final]
        prompt = "".join(parts)
        return ContextResult(prompt, self._n(prompt), len(turns), len(history) - len(turns),
                             [n.removeprefix("Note: ").rstrip("\n") for n in chosen_notes], truncated)
