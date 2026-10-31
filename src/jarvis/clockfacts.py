"""The real clock, for questions about the time and date. A small language model cannot know the time, so it guessed (and was wrong). Two fixes live here:
the exact local time and date are put in the system prompt of every turn, and a plain "what time is it" is answered directly from the computer's clock without calling the model."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Callable

_TIME = re.compile(r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis[, ]*)?(?:please\s+)?(?:"
                   r"what(?:'s|\s+is)\s+the\s+(?:current\s+)?time(?:\s+(?:now|right\s+now|please))?|what\s+time\s+is\s+it(?:\s+(?:now|right\s+now|please))?|"
                   r"tell\s+me\s+the\s+time|(?:the\s+)?current\s+time|do\s+you\s+know\s+the\s+time|got\s+the\s+time|what\s+time\s+do\s+you\s+have)\W*$", re.I)
_DATE = re.compile(r"^(?:(?:hey|ok|okay)\s+)?(?:jarvis[, ]*)?(?:please\s+)?(?:"
                   r"what(?:'s|\s+is)\s+(?:the\s+date|today'?s\s+date|the\s+date\s+today)|what\s+day\s+is\s+it(?:\s+today)?|what\s+is\s+today|what(?:'s|\s+is)\s+today|"
                   r"tell\s+me\s+the\s+date|(?:the\s+)?current\s+date|what\s+is\s+the\s+day\s+today)\W*$", re.I)


def local_now() -> datetime:
    return datetime.now().astimezone()


def _clock(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {'AM' if dt.hour < 12 else 'PM'}"


def _day(dt: datetime) -> str:
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {dt.day}, {dt.year}"


def prompt_line(dt: datetime) -> str:
    off = dt.strftime("%z")
    off = f"UTC{off[:3]}:{off[3:]}" if off else "local time"
    return (f"Current local date and time on the user's PC: {_day(dt)}, {_clock(dt)} ({off}). This comes from the computer's clock. "
            "Use it for any question about the time, date, weekday or how long until or since something; never guess it.")


def answer(text: str, now: Callable[[], datetime] | None = None) -> str | None:
    """A direct answer when the message is just a time or date question, else None."""
    t = re.sub(r"\s+", " ", str(text or "").strip())
    if not t or len(t) > 80:
        return None
    wants_time, wants_date = bool(_TIME.match(t)), bool(_DATE.match(t))
    if not (wants_time or wants_date):
        return None
    dt = (now or local_now)()
    return f"It's {_clock(dt)}." if wants_time else f"Today is {_day(dt)}."
