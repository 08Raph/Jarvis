"""One measurement, with everything needed to judge it: unit, time, where it came from and whether it is a measurement at all.

kind:
  measured     read from the operating system / a device driver just now
  derived      computed from measured values by a stated formula (for example a percentile of frame times)
  imported     taken from a file made by another tool (for example a PresentMon log); Jarvis did not measure it and says so
  estimated    a guess; must say how it was made
  unavailable  no reliable source, or the source failed or returned nonsense; `detail` says why. `value` is None.
Nothing is ever filled in with a plausible number."""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

KINDS = ("measured", "derived", "imported", "estimated", "unavailable")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


@dataclass(frozen=True)
class Reading:
    name: str
    value: float | int | str | None
    unit: str
    kind: str
    source: str
    at: str
    detail: str = ""

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown reading kind {self.kind!r}")
        if self.kind == "unavailable" and self.value is not None:
            raise ValueError("an unavailable reading has no value")
        if self.kind != "unavailable" and self.value is None:
            raise ValueError("a reading without a value must be 'unavailable'")
        if self.kind in ("estimated", "derived") and not self.detail:
            raise ValueError(f"a {self.kind} reading must say how it was made")

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        if self.kind == "unavailable":
            return f"{self.name}: UNAVAILABLE - {self.detail} (source: {self.source}, {self.at})"
        tag = "" if self.kind == "measured" else f" [{self.kind}]"
        extra = f" ({self.detail})" if self.detail else ""
        return f"{self.name}: {self.value} {self.unit}{tag}{extra} (source: {self.source}, {self.at})"


def ok(name, value, unit, source, kind="measured", detail="", at=None) -> Reading:
    return Reading(name, value, unit, kind, source, at or now_iso(), detail)


def na(name, unit, source, why, at=None) -> Reading:
    return Reading(name, None, unit, "unavailable", source, at or now_iso(), str(why)[:300])


def checked(name, value, unit, source, lo=0.0, hi=None, kind="measured", detail="", at=None) -> Reading:
    """A reading only if the number is a real, finite, in-range number; otherwise 'unavailable: invalid reading' (never clamped or corrected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return na(name, unit, source, f"invalid reading from the source ({value!r} is not a finite number)", at)
    if value < lo or (hi is not None and value > hi):
        return na(name, unit, source, f"invalid reading from the source ({value} {unit} is outside the possible range {lo}..{hi})", at)
    return ok(name, round(value, 3) if isinstance(value, float) else value, unit, source, kind, detail, at)
