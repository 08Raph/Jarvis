"""Uncertainty gate: answer only when the model's own confidence clears a threshold.

The score is the mean log-probability of the generated tokens. This is a weak,
imperfect proxy (a model can be confidently wrong), so the threshold MUST be
calibrated on held-out evaluation data with calibrate_threshold() and the
resulting error rates reported. The default is an uncalibrated placeholder.
"""
from __future__ import annotations

from dataclasses import dataclass

ABSTAIN_MESSAGE = ("I'm not confident enough to answer that reliably. Could you give me more detail, "
                   "or would you like me to use one of my tools instead?")


@dataclass(frozen=True)
class GateDecision:
    answered: bool
    text: str
    reason: str


class AnswerGate:
    def __init__(self, min_mean_logprob: float = -3.0) -> None:
        self.min_mean_logprob = min_mean_logprob

    def decide(self, text: str, mean_logprob: float) -> GateDecision:
        if not text.strip():
            return GateDecision(False, ABSTAIN_MESSAGE, "empty_output")
        if mean_logprob < self.min_mean_logprob:
            return GateDecision(False, ABSTAIN_MESSAGE, "low_confidence")
        return GateDecision(True, text, "confident")


def calibrate_threshold(correct_scores: list[float], incorrect_scores: list[float]) -> float:
    """Pick the threshold t (answer iff score >= t) maximizing accuracy of the
    answer/abstain decision on labelled examples. Ties prefer the higher (more cautious) t."""
    if not correct_scores or not incorrect_scores:
        raise ValueError("need both correct and incorrect examples to calibrate")
    best_t, best_acc = None, -1
    for t in sorted(set(correct_scores) | set(incorrect_scores)):
        acc = sum(s >= t for s in correct_scores) + sum(s < t for s in incorrect_scores)
        if acc >= best_acc:
            best_t, best_acc = t, acc
    return float(best_t)
