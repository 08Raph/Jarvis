"""Stops a reply that has fallen into a loop (the same sentence again and again), which small local models sometimes do."""
from __future__ import annotations

import re

MIN_UNIT = 12          # shortest repeated piece (characters) that counts as a loop
MAX_UNIT = 400
TIMES = 3


def repeating_unit(text: str) -> int:
    """Length of the piece that the text ENDS with, repeated TIMES times in a row; 0 when there is no loop."""
    n = len(text)
    for size in range(MIN_UNIT, min(MAX_UNIT, n // TIMES) + 1):
        unit = text[-size:]
        if unit.strip() and text.endswith(unit * TIMES) and len(unit.strip()) >= MIN_UNIT:
            return size
    return 0


def trim_loop(text: str) -> tuple[str, bool]:
    """(text with the repeated copies removed so one copy remains, whether a loop was found)."""
    size = repeating_unit(text)
    if not size:
        return text, False
    unit = text[-size:]
    base = text
    while base.endswith(unit):
        base = base[:-size]
    return (base + unit).rstrip(), True


def sentence_loop(text: str) -> bool:
    """The last three sentences are identical (ignoring case and punctuation)."""
    sents = [re.sub(r"\W+", " ", s).strip().lower() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    return len(sents) >= TIMES and len(sents[-1]) >= 8 and sents[-1] == sents[-2] == sents[-3]
