"""Energy-based voice activity detector with adaptive noise floor. Simple and dependency-free (numpy only).
Accuracy on real rooms/microphones is UNMEASURED; thresholds are configurable."""
from __future__ import annotations

import numpy as np


def rms(frame: np.ndarray) -> float:
    x = frame.astype(np.float32)
    if x.dtype != np.float32 or (frame.dtype == np.int16):
        x = x / 32768.0
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


class EnergyVAD:
    def __init__(self, ratio: float = 3.0, min_rms: float = 0.01, floor_alpha: float = 0.05):
        self.ratio, self.min_rms, self.alpha = ratio, min_rms, floor_alpha
        self.floor = min_rms / 2

    def is_speech(self, frame: np.ndarray) -> bool:
        e = rms(frame)
        speech = e > max(self.min_rms, self.floor * self.ratio)
        if not speech:  # only learn the noise floor from non-speech frames
            self.floor = (1 - self.alpha) * self.floor + self.alpha * e
        return speech
