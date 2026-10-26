"""Wake-phrase matching over recognizer text. A recognizer constrained to a small grammar produces the text;
this module decides whether it counts as the wake phrase. Fuzzy matching tolerates small ASR errors, and
sensitivity controls how much."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_WS = re.compile(r"[^a-z0-9' ]+")


def normalize(text: str) -> list[str]:
    return _WS.sub(" ", text.lower()).split()


def _tolerance(sensitivity: float) -> float:
    # similarity needed per word: sensitivity 0 -> 1.0 (exact), 0.5 -> 0.8, 1 -> 0.6
    return 1.0 - 0.4 * max(0.0, min(1.0, sensitivity))


def match_wake(text: str, phrases: list[str], sensitivity: float = 0.5) -> tuple[str | None, str]:
    """Return (matched_phrase, remainder_after_phrase) or (None, text). The phrase must be at the START of the
    utterance (a name mentioned mid-sentence is not a wake)."""
    words = normalize(text)
    need = _tolerance(sensitivity)
    best = None
    for ph in phrases:
        pw = normalize(ph)
        if not pw or len(words) < len(pw):
            continue
        sims = [SequenceMatcher(None, a, b).ratio() for a, b in zip(words[:len(pw)], pw)]
        if all(s >= need for s in sims):
            score = sum(sims) / len(sims)
            if best is None or score > best[0]:
                best = (score, ph, " ".join(words[len(pw):]))
    return (best[1], best[2]) if best else (None, text)
