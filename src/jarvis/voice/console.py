"""Keyboard controls and a plain-language microphone indicator for the console voice session (`jarvis voice run`).

The session object already supports mute, sleep, wake, cancel and push-to-talk; this module makes them reachable by the user
while a session is running and shows the microphone state in words. No network code, no audio handling.
Keys (single key, no Enter needed on Windows):  m mute/unmute   p push-to-talk (press to start, press again to send)
                                                s sleep         w wake          c cancel speech   n return to Normal style   h help   q quit
"""
from __future__ import annotations

import sys
from typing import Callable

HELP = ("Keys:  m = mute / unmute the microphone   p = push-to-talk (press to start talking, press again to send)\n"
        "       s = sleep (mic closed)  w = wake  c = cancel / stop speaking  n = back to Normal style (and stop speaking)  h = this help  q = quit")

_STATE_TEXT = {
    "OFF": "off",
    "LISTENING": "listening for the wake phrase",
    "CAPTURING": "hearing you speak",
    "THINKING": "working on a reply (keys are applied when it finishes)",
    "SPEAKING": "speaking (say 'stop' or press c to interrupt)",
    "FOLLOW_UP": "listening for your follow-up (no wake phrase needed)",
    "SLEEPING": "asleep (press w to wake, or p to talk)",
    "MUTED": "muted (press m to unmute)",
    "RECOVERING": "microphone problem, retrying",
    "ERROR": "stopped: microphone unavailable",
}


def banner(state: str, mic_open: bool, note: str = "") -> str:
    """One line a person can read at a glance. 'MIC OPEN' is printed ONLY when the stream is actually open."""
    mic = "MIC OPEN  " if mic_open else "MIC CLOSED"
    text = _STATE_TEXT.get(state, state.lower())
    return f"[{mic}] {text}" + (f"  ({note})" if note else "")


def format_event(kind: str, payload) -> str | None:
    """Console text for a session event; None = print nothing."""
    if kind == "state":
        return banner(payload["state"], bool(payload["mic_open"]), payload.get("note", ""))
    if kind == "wake":
        return f"  wake phrase heard: {payload}"
    if kind == "heard":
        return f"  you said: {payload}"
    if kind == "reply_text":
        return f"  reply: {payload}"
    if kind == "error":
        return f"  ! {payload}"
    return None            # 'spoken' text is not echoed again


class Controls:
    """Maps single keys to session actions. `quit` becomes True when the user asks to stop."""

    def __init__(self, session, say: Callable[[str], None] = print, on_normal: Callable[[], str] | None = None):
        self.sess, self.say, self.quit, self.ptt_on = session, say, False, False
        self.on_normal = on_normal            # returns the confirmation text; None = no style control available here

    def key(self, k: str) -> None:
        k = (k or "").lower()[:1]
        s = self.sess
        if k == "m":
            if s.s.muted:
                self.say("Unmuting the microphone.")
                s.unmute()
            else:
                self.ptt_on = False
                self.say("Muting: the microphone stream is being closed.")
                s.mute()
        elif k == "p":
            if s.s.muted:
                self.say("The microphone is muted; press m to unmute first.")
            elif not self.ptt_on:
                s.ptt_press()
                self.ptt_on = s.state == "CAPTURING"
                if self.ptt_on:
                    self.say("Push-to-talk: speak now, press p again to send.")
            else:
                self.ptt_on = False
                s.ptt_release()
        elif k == "s":
            self.ptt_on = False
            s.sleep()
        elif k == "w":
            s.wake()
        elif k == "c":
            self.ptt_on = False
            s.cancel()
        elif k == "n":
            self.ptt_on = False
            s.cancel()                         # silence first, whatever the style callback does
            if self.on_normal is not None:
                try:
                    self.say(self.on_normal())
                except Exception as e:  # noqa: BLE001
                    self.say(f"Could not change the style: {type(e).__name__}")
            else:
                self.say("Style control is not available in this session.")
        elif k in ("h", "?"):
            self.say(HELP)
        elif k == "q":
            self.quit = True


def make_key_reader() -> Callable[[], str | None]:
    """Non-blocking single-key reader. Windows: msvcrt. Elsewhere (development only): a line from an interactive terminal.
    Returns a callable that gives a key or None. If stdin is not interactive it always gives None (no hidden input)."""
    if sys.platform == "win32":
        import msvcrt

        def read_win() -> str | None:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):       # special key prefix: swallow the second code
                    msvcrt.getwch()
                    return None
                return ch
            return None
        return read_win
    try:
        interactive = sys.stdin.isatty()
    except Exception:
        interactive = False
    if not interactive:
        return lambda: None
    import select

    def read_posix() -> str | None:
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            line = sys.stdin.readline()
            return line.strip()[:1] or None
        return None
    return read_posix
