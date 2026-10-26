"""Local retrieval over personal memories. DEFAULT = lexical (word overlap with IDF weighting, light stemming): fast, no model,
no download, but it only matches shared words, so it is NOT semantic. OPTIONAL semantic retrieval works only if an `embedder`
(text -> list of floats) is supplied; none is bundled and none has been tested with a real embedding model."""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Sequence

STOP = frozenset("""a an the and or but if then of to in on at for with about as is are was were be been it its this that these those
i me my we our you your do does did done can could should would will shall have has had not no yes please just so than too very
what which who whom when where why how from by into over under up down out off again more most some any each other
am like much many get got make tell give know""".split())
Embedder = Callable[[str], Sequence[float]]


_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ied", "ers", "er", "ed", "es", "s", "ly")


def stem(w: str) -> str:
    """Light suffix stripper (not Porter). 'saving'->'sav', 'designs'/'designer'->'design', 'studies'->'study'."""
    w = w.replace("'s", "").strip("'")
    for suf in _SUFFIXES:
        if len(w) > len(suf) + 2 and w.endswith(suf):
            base = w[: -len(suf)]
            if suf in ("ies", "ied"):
                return base + "y"
            if len(base) > 3 and base[-1] == base[-2] and base[-1] not in "ls":      # running -> run
                base = base[:-1]
            return base.rstrip("e") if len(base) > 3 else base
    return w.rstrip("e") if len(w) > 4 else w


# Small hand-written table of everyday near-synonyms / question-to-answer word pairs. It is NOT a language model: it only widens
# matching for common household/work words. Each group is stemmed at import time; words in one group count as the same term.
_GROUPS = [
    "boss manager supervisor lead", "wife spouse partner husband", "mom mother mum", "dad father", "car vehicle drive honda",
    "laptop computer notebook pc", "memory ram gigabyte", "wake up rise alarm", "avoid allergic allergy eat food diet",
    "study learn course lesson", "language spanish french german japanese", "database postgresql mysql sqlite storage db",
    "live reside home city address", "job work office employer", "phone mobile cell", "run jog running kilometer mile exercise",
    "weekend saturday sunday", "meeting standup retro retrospective sync", "format style concise bullet", "unit metric imperial",
    "editor ide", "notes note obsidian notebook", "money save saving fund budget", "train bus commute transit", "cat dog pet",
    "router wifi network internet", "drive disk backup storage", "sister brother sibling", "monitor screen display",
    "deadline due release", "coffee tea drink latte", "dark light theme mode appearance", "birthday born anniversary",
]
_STEM_SYN: dict[str, str] = {}


def _build_groups() -> None:
    for i, g in enumerate(_GROUPS):
        for w in g.split():
            _STEM_SYN.setdefault(stem(w), f"~{i}")


_build_groups()


def tokens(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in STOP and len(w) > 1]


def _close(a: str, b: str) -> bool:
    """Typo tolerance: same first letter and (edit distance <= 1 for 5-8 letters, <= 2 for longer)."""
    if a == b:
        return True
    if len(a) < 5 or len(b) < 5 or a[0] != b[0] or abs(len(a) - len(b)) > 2:
        return False
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() >= (0.86 if min(len(a), len(b)) < 8 else 0.82)


def lexical_scores(query: str, docs: Sequence[str]) -> list[float]:
    """Score each document against the query, 0..1 (1 = every query term found, rare terms count more).
    A query term matches a document term if stems are equal, the typo check passes, or both are in the same near-synonym group
    (synonym matches count 0.6 of an exact match)."""
    q = list(dict.fromkeys(tokens(query)))
    if not q or not docs:
        return [0.0] * len(docs)
    dtoks = [set(tokens(d)) for d in docs]
    n = len(docs)
    vocab = set().union(*dtoks)
    df = Counter(t for d in dtoks for t in d)
    idf = {t: math.log(1 + (n + 1) / (1 + df.get(t, 0))) for t in q}
    total = sum(idf.values()) or 1.0
    out = []
    for d in dtoks:
        syn = {_STEM_SYN[t] for t in d if t in _STEM_SYN}
        got = 0.0
        for t in q:
            if t in d:
                got += idf[t]
            elif any(_close(t, x) for x in d):
                got += 0.9 * idf[t]
            elif _STEM_SYN.get(t) in syn and t not in vocab:
                got += 0.6 * idf[t]
            elif _STEM_SYN.get(t) in syn:
                got += 0.4 * idf[t]
        out.append(got / total)
    return out


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    import numpy as np
    x, y = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    nx, ny = float(np.linalg.norm(x)), float(np.linalg.norm(y))
    if nx == 0 or ny == 0 or x.shape != y.shape:
        return 0.0
    return float(x @ y / (nx * ny))
