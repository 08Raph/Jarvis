"""The Home screen's HUD, without Qt: which state to show, what to say about it, and the animation maths. Everything here is plain Python so it is tested on any machine;
`jarvis.ui.hud` only turns these numbers into paint calls.

Honesty rules (each one has a test):
  * A state is derived ONLY from measured values: the voice service's own report, the controller's busy flag and the speaker's own is_speaking(). Nothing is
    invented to look busy, and "listening" is shown only while the microphone stream is really open and the recogniser is loaded.
  * The waveform follows the real microphone level ONLY while the microphone is open and the HUD is in a listening state. While thinking or speaking it is a
    pattern, and the caption under it says so ("animation only, not audio"). While the microphone is closed it is flat.
  * Speaking is animated from "the speaker says it is speaking": Windows speech (SAPI/PowerShell) and Kokoro give no amplitude to read, so none is claimed.
  * Motion is cheap: the frame interval depends on the state, stops when the window is hidden, and `reduce_motion` makes it nearly static."""
from __future__ import annotations

import math
import os
import random
import shutil
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from jarvis.ui import theme

OFF, STARTING, LISTENING, WAKE, CAPTURING, FOLLOW_UP, THINKING, SPEAKING, MUTED, MIC_UNAVAILABLE, ERROR = (
    "off", "starting", "listening", "wake", "capturing", "follow_up", "thinking", "speaking", "muted", "mic_unavailable", "error")
STATES = (OFF, STARTING, LISTENING, WAKE, CAPTURING, FOLLOW_UP, THINKING, SPEAKING, MUTED, MIC_UNAVAILABLE, ERROR)
WAKE_FLASH_SECONDS = 1.2

# The HUD's own dark palette (the page is always dark, like the approved reference, whatever the system theme is).
HUD = {"bg": "#050b14", "panel": "#0a1626", "panel_edge": "#1b4a78", "text": "#e1f0ff", "muted": "#a5bfd8", "accent": "#35d6ff", "accent_dim": "#1c6f8c",
       "wake": "#ffd166", "ok": "#3ddc97", "warn": "#ffb347", "danger": "#ff7b7b", "off": "#9fb2c4", "core": "#8fe9ff"}
# (foreground, background, minimum contrast) for every text pair the Home page uses; a test checks them.
HUD_PAIRS = [("text", "bg", 4.5), ("text", "panel", 4.5), ("muted", "bg", 4.5), ("muted", "panel", 4.5), ("accent", "bg", 4.5), ("accent", "panel", 4.5),
             ("ok", "panel", 4.5), ("warn", "panel", 4.5), ("danger", "panel", 4.5), ("off", "panel", 4.5), ("wake", "bg", 4.5)]


def contrast_report() -> list[tuple[str, str, float, float]]:
    return [(a, b, round(theme.contrast(HUD[a], HUD[b]), 2), need) for a, b, need in HUD_PAIRS]


@dataclass(frozen=True)
class Look:
    """How a state moves. ring_dps: degrees per second of the fastest ring; pulse_hz/pulse_depth: the core's breathing; wave: where the waveform comes from."""
    ring_dps: float
    pulse_hz: float
    pulse_depth: float
    wave: str          # "mic" (real level) | "thinking" | "speaking" (patterns) | "flat"
    accent: str        # key of HUD
    glow: float        # 0..1 base brightness of the core


LOOKS = {
    OFF: Look(2.0, 0.10, 0.25, "flat", "off", 0.30),
    STARTING: Look(10.0, 0.40, 0.40, "flat", "warn", 0.45),
    LISTENING: Look(5.0, 0.22, 0.35, "mic", "accent", 0.60),          # slow and subtle
    WAKE: Look(40.0, 1.60, 0.90, "mic", "wake", 1.00),                # brief and distinct
    CAPTURING: Look(14.0, 0.70, 0.60, "mic", "accent", 0.85),
    FOLLOW_UP: Look(9.0, 0.40, 0.45, "mic", "accent", 0.70),
    THINKING: Look(45.0, 0.90, 0.55, "thinking", "accent", 0.80),     # fast sweep: clearly "working"
    SPEAKING: Look(12.0, 1.10, 0.70, "speaking", "ok", 0.90),
    MUTED: Look(1.5, 0.08, 0.15, "flat", "off", 0.20),
    MIC_UNAVAILABLE: Look(1.0, 0.0, 0.0, "flat", "danger", 0.25),
    ERROR: Look(1.0, 0.0, 0.0, "flat", "danger", 0.25),
}
MIC_STATES = (LISTENING, WAKE, CAPTURING, FOLLOW_UP)


def _g(d, k, default=None):
    try:
        return d.get(k, default)
    except AttributeError:
        return default


def resolve_state(voice: dict | None, live: dict | None, busy: bool = False, speaking: bool = False) -> str:
    """One HUD state from measured facts.
    voice = VoiceService.state() (the labels the Voice page shows), live = VoiceService.live() (fast, in memory), busy = the controller is producing a reply,
    speaking = the shared speaker reports that it is speaking. Real activity (thinking, speaking) outranks the voice-listening states because it is what is
    happening now; the microphone chip next to the core still tells the truth about the microphone."""
    voice, live = voice or {}, live or {}
    phase = _g(live, "phase", "") or _g(voice, "phase", "")
    if busy or phase == "THINKING":
        return THINKING
    if phase == "SPEAKING" or speaking:
        return SPEAKING
    vstate = _g(voice, "state", "Off")
    if vstate == "Unavailable" or phase == "ERROR":
        return MIC_UNAVAILABLE if _g(voice, "failure_kind", "") == "microphone" or phase == "ERROR" else ERROR
    if vstate == "Muted" or phase == "MUTED":
        return MUTED
    if vstate == "Starting" or phase == "RECOVERING":
        return MIC_UNAVAILABLE if phase == "RECOVERING" else STARTING
    if vstate != "Listening" or not _g(live, "mic_open", _g(voice, "mic_open", False)):
        return OFF
    age = _g(live, "wake_age", None)
    if age is not None and age < WAKE_FLASH_SECONDS and phase in ("LISTENING", "CAPTURING", ""):
        return WAKE
    if phase == "CAPTURING":
        return CAPTURING
    if phase == "FOLLOW_UP":
        return FOLLOW_UP
    return LISTENING


@dataclass(frozen=True)
class HudView:
    state: str
    label: str          # the big caption under the core
    sub: str            # one line of detail
    mic_open: bool      # MEASURED: the microphone stream is open
    mic_text: str       # "MIC ON" / "MIC OFF - muted" ...
    mic_tone: str       # key of HUD: ok | off | danger | warn
    wave_caption: str   # where the waveform comes from, in words
    accent: str


def _short(text: str, n: int = 150) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def describe(state: str, voice: dict | None = None, live: dict | None = None, loading: bool = False) -> HudView:
    voice, live = voice or {}, live or {}
    wake_phrase = str(_g(voice, "wake_phrase", "hey jarvis") or "hey jarvis")
    mic_open = bool(_g(live, "mic_open", _g(voice, "mic_open", False)))
    detail = _short(_g(voice, "detail", ""))
    label, sub = {
        OFF: ("VOICE OFF", "Microphone not in use. Turn hands-free on to say “" + wake_phrase + "”."),
        STARTING: ("STARTING", "Checking the microphone and loading the local speech model…"),
        LISTENING: ("LISTENING…", "Say “" + wake_phrase + "”. Nothing is acted on before that."),
        WAKE: ("WAKE WORD HEARD", "“" + wake_phrase + "” detected. Go ahead."),
        CAPTURING: ("HEARING YOU", "Capturing your command. Audio is processed in memory and not saved."),
        FOLLOW_UP: ("LISTENING FOR A FOLLOW-UP", "No wake word needed for the next few seconds."),
        THINKING: (("LOADING MODEL" if loading else "THINKING"), ("Loading the local model into memory…" if loading else "The local model is preparing a reply.")),
        SPEAKING: ("SPEAKING", "Press Stop speaking to interrupt."),
        MUTED: ("MUTED", "Microphone closed. Nothing is being heard."),
        MIC_UNAVAILABLE: ("MICROPHONE UNAVAILABLE", detail or "Jarvis cannot use the microphone right now."),
        ERROR: ("VOICE ERROR", detail or "Voice could not start."),
    }[state]
    if mic_open:
        mic_text, tone = "MIC ON", "ok"
    elif state == MUTED:
        mic_text, tone = "MIC OFF · muted", "off"
    elif state in (MIC_UNAVAILABLE, ERROR):
        mic_text, tone = "MIC OFF · unavailable" if state == MIC_UNAVAILABLE else "MIC OFF · voice error", "danger"
    elif state == STARTING:
        mic_text, tone = "MIC OFF · starting", "warn"
    else:
        mic_text, tone = "MIC OFF", "off"
    look = LOOKS[state]
    if look.wave == "mic" and mic_open:
        cap = "live microphone level"
    elif look.wave in ("thinking", "speaking"):
        cap = "animation only · not audio"
    else:
        cap = "microphone closed" if not mic_open else "microphone open · waiting for the wake word"
    return HudView(state, label, sub, mic_open, mic_text, tone, cap, look.accent)


def stage_active(state: str) -> str | None:
    """Which of LISTENING / THINKING / SPEAKING is true now (None = none of them)."""
    if state in MIC_STATES:
        return "LISTENING"
    if state == THINKING:
        return "THINKING"
    if state == SPEAKING:
        return "SPEAKING"
    return None


STAGES = ("LISTENING", "THINKING", "SPEAKING")


def frame_interval_ms(state: str, visible: bool = True, reduce_motion: bool = False) -> int:
    """0 = do not animate at all (window hidden or minimised)."""
    if not visible:
        return 0
    if reduce_motion:
        return 500
    if state in (LISTENING, WAKE, CAPTURING, FOLLOW_UP, THINKING, SPEAKING):
        return 33          # ~30 fps: the waveform follows a live level
    return 66              # ~15 fps: slow, subtle motion is enough


def reduce_motion_requested(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return str(env.get("JARVIS_REDUCE_MOTION", "")).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- level / waveform
def display_level(rms: float) -> float:
    """Microphone RMS (0..1 of full scale) -> 0..1 for drawing. Quiet-room hiss stays at 0; ordinary speech (RMS ~0.02-0.2) fills most of the range."""
    try:
        r = float(rms)
    except (TypeError, ValueError):
        return 0.0
    if not (r == r) or r < 0.0008:
        return 0.0
    return max(0.0, min(1.0, (r / 0.12) ** 0.6))


class Smoother:
    """Fast attack, slower release, so a syllable shows at once and fades smoothly."""

    def __init__(self, attack_s: float = 0.04, release_s: float = 0.18):
        self.attack, self.release, self.value = attack_s, release_s, 0.0

    def update(self, target: float, dt: float) -> float:
        dt = max(0.0, min(0.5, dt))
        tau = self.attack if target > self.value else self.release
        self.value += (target - self.value) * (1.0 - math.exp(-dt / tau))
        if self.value < 1e-4:
            self.value = 0.0
        return self.value


class Wave:
    """A scrolling history of levels, drawn mirrored around the centre. Newest value sits at the centre and moves outwards."""
    SIZE = 48
    STEP_S = 0.035

    def __init__(self):
        self.hist: deque[float] = deque([0.0] * self.SIZE, maxlen=self.SIZE)
        self._acc = 0.0
        self._t = 0.0

    def _pattern(self, mode: str, t: float) -> float:
        if mode == "speaking":
            return max(0.0, min(1.0, 0.18 + 0.55 * abs(math.sin(t * 7.3) * math.sin(t * 2.1 + 1.0)) + 0.15 * abs(math.sin(t * 13.0))))
        if mode == "thinking":
            return 0.10 + 0.14 * (0.5 + 0.5 * math.sin(t * 5.0))
        return 0.0

    def advance(self, dt: float, mode: str, level: float) -> None:
        """mode: "mic" (uses `level`, already display_level), "thinking" / "speaking" (a pattern), "flat" (decays to nothing)."""
        dt = max(0.0, min(0.5, dt))
        self._t += dt
        self._acc += dt
        while self._acc >= self.STEP_S:
            self._acc -= self.STEP_S
            if mode == "mic":
                v = max(0.0, min(1.0, level))
            elif mode == "flat":
                v = 0.0
            else:
                v = self._pattern(mode, self._t)
            self.hist.appendleft(v)

    def heights(self) -> list[float]:
        """SIZE values, index 0 = at the centre. A short taper keeps the bars near the core taller than the far ones."""
        out = []
        for i, v in enumerate(self.hist):
            env = 1.0 - 0.65 * (i / (self.SIZE - 1))
            out.append(max(0.0, min(1.0, v * env)))
        return out


# ---------------------------------------------------------------- rings, core, particles
# (radius as a fraction of the HUD radius, line width px, speed multiplier (negative = counter-clockwise), arc segments, arc length in degrees)
RINGS = (
    (0.30, 2.0, 1.00, 3, 70), (0.37, 1.5, -0.70, 4, 40), (0.46, 3.0, 0.45, 2, 120), (0.54, 1.2, -1.20, 6, 24),
    (0.63, 2.5, 0.30, 3, 80), (0.72, 1.0, -0.55, 12, 12), (0.80, 2.0, 0.20, 2, 150), (0.90, 1.0, -0.15, 40, 4),
)


class Hud:
    """The animation clock. advance() takes the real elapsed seconds, the state and the measured microphone level and updates phases; nothing here reads a device."""

    def __init__(self, seed: int = 7):
        self.state = OFF
        self.ring_deg = 0.0
        self.pulse_phase = 0.0
        self.level = Smoother()
        self.wave = Wave()
        self.flash = 0.0
        self.particles = Particles(seed=seed)
        self.glow = LOOKS[OFF].glow

    def advance(self, dt: float, state: str, mic_rms: float = 0.0, wake_age: float | None = None) -> None:
        dt = max(0.0, min(0.5, dt))
        look = LOOKS[state]
        self.state = state
        self.ring_deg = (self.ring_deg + look.ring_dps * dt) % (360.0 * 120)
        self.pulse_phase = (self.pulse_phase + 2 * math.pi * look.pulse_hz * dt) % (2 * math.pi * 1000)
        mic_live = look.wave == "mic"
        target = display_level(mic_rms) if mic_live else 0.0
        lv = self.level.update(target, dt)
        self.wave.advance(dt, look.wave, lv if mic_live else 0.0)
        self.flash = max(0.0, 1.0 - wake_age / WAKE_FLASH_SECONDS) if (state == WAKE and wake_age is not None) else 0.0
        self.glow += (look.glow - self.glow) * (1.0 - math.exp(-dt / 0.25))
        self.particles.advance(dt, 0.4 + look.ring_dps / 30.0)

    def core_scale(self) -> float:
        """1.0 = base size. Breathes with the state's pulse; swells a little with the real microphone level while listening."""
        look = LOOKS[self.state]
        pulse = 0.5 + 0.5 * math.sin(self.pulse_phase)
        return 1.0 + look.pulse_depth * 0.05 * pulse + (0.10 * self.level.value if look.wave == "mic" else 0.0) + 0.12 * self.flash


class Particles:
    def __init__(self, n: int = 48, seed: int = 7):
        rnd = random.Random(seed)
        # angle (deg), radius fraction, angular speed (deg/s), size px, twinkle phase
        self.items = [[rnd.uniform(0, 360), rnd.uniform(0.25, 0.98), rnd.uniform(-6.0, 6.0), rnd.uniform(1.0, 2.6), rnd.uniform(0, 6.28)] for _ in range(n)]
        self._t = 0.0

    def advance(self, dt: float, speed: float = 1.0) -> None:
        self._t += dt
        for p in self.items:
            p[0] = (p[0] + p[2] * dt * speed) % 360.0

    def points(self) -> list[tuple[float, float, float, float]]:
        """(angle degrees, radius fraction, size, alpha 0..1)"""
        return [(p[0], p[1], p[3], 0.25 + 0.55 * (0.5 + 0.5 * math.sin(self._t * 1.3 + p[4]))) for p in self.items]


# ---------------------------------------------------------------- panels
def recent_from_entries(entries, limit: int = 4) -> list[tuple[str, str, str]]:
    """Transcript entries -> [(kind, text, stamp)] for the last few things said by the user or Jarvis (tool and system lines are not conversation)."""
    return [(e.kind, e.text, e.stamp()) for e in entries if e.kind in ("user", "assistant")][-limit:]


def recent_lines(recent, limit: int = 4, width: int = 90) -> list[dict]:
    """[(kind, text, stamp)] -> [{who, text, stamp}] for the Recent conversation panel (newest last)."""
    out = []
    for kind, text, stamp in list(recent or [])[-limit:]:
        t = " ".join(str(text or "").split())
        if t.startswith("(voice) "):
            t = t[len("(voice) "):]
        out.append({"who": "You" if kind == "user" else "Jarvis", "text": t if len(t) <= width else t[: width - 1].rstrip() + "…", "stamp": stamp})
    return out


def privacy_lines(online_on) -> tuple[str, str, str, str]:
    """(header text, header tone, bottom title, bottom detail) from the list of online features that are ON (None = unknown)."""
    if online_on is None:
        return "Local AI", "off", "Local AI", "Privacy state could not be read."
    if not online_on:
        return "Local AI · no online features on", "ok", "Local AI", "No internet required. Your audio and data stay on this PC."
    return "Online features ON: " + ", ".join(online_on), "warn", "Online features are ON", "Enabled by you: " + ", ".join(online_on) + ". Microphone audio is never sent."


def quick_info(facts: dict | None, view: HudView) -> list[tuple[str, str, str]]:
    f = facts or {}
    mem = f.get("memory_count")
    online = f.get("online_on")
    rows = [("Local model", str(f.get("model_text", "unknown")), "ok" if f.get("model_ok") else "warn"),
            ("Memory", ("not saved in a guest session" if f.get("guest") else "could not be read") if mem is None else f"{mem} saved item{'s' if mem != 1 else ''}", "off" if mem is None else "ok"),
            ("Microphone", view.mic_text.replace("·", "-").capitalize(), view.mic_tone)]
    rows.append(("Privacy", "unknown" if online is None else ("no online features on" if not online else "online ON: " + ", ".join(online)), "off" if online is None else ("ok" if not online else "warn")))
    return rows


class Resources:
    """CPU / RAM / disk percentages for the small panel. Display only: nothing is stored, logged or shown to the assistant. CPU and RAM need psutil (an optional
    package); without it they are None and the panel says "n/a" rather than guessing. Disk uses the standard library."""

    def __init__(self, psutil_mod=None, disk_path: str | None = None, load_psutil: bool = True):
        self._ps = psutil_mod
        if self._ps is None and load_psutil:
            try:
                import psutil  # type: ignore
                self._ps = psutil
            except Exception:  # noqa: BLE001
                self._ps = None
        self.disk_path = disk_path or (Path.home().anchor or "/")
        self._primed = False

    def sample(self) -> dict:
        out: dict = {"cpu": None, "ram": None, "disk": None}
        ps = self._ps
        if ps is not None:
            try:
                v = float(ps.cpu_percent(interval=None))
                if self._primed:           # the first reading since start is meaningless (psutil measures since the previous call)
                    out["cpu"] = max(0.0, min(100.0, v))
                self._primed = True
            except Exception:  # noqa: BLE001
                pass
            try:
                out["ram"] = max(0.0, min(100.0, float(ps.virtual_memory().percent)))
            except Exception:  # noqa: BLE001
                pass
        try:
            du = shutil.disk_usage(self.disk_path)
            out["disk"] = max(0.0, min(100.0, 100.0 * du.used / du.total)) if du.total else None
        except Exception:  # noqa: BLE001
            pass
        return out


def clock_text(ts: float | None = None) -> tuple[str, str]:
    lt = time.localtime(ts if ts is not None else time.time())
    return time.strftime("%I:%M %p", lt).lstrip("0"), time.strftime("%b %d, %Y", lt)


# ---------------------------------------------------------------- style sheets
NAV = {"bg": "#07111f", "text": "#d3e8fb", "sel_bg": "#12304f", "edge": HUD["accent"], "hover": "#0e2238"}
NAV_PAIRS = [("text", "bg", 4.5), ("text", "sel_bg", 4.5), ("text", "hover", 4.5)]


def nav_contrast() -> list[tuple[str, str, float, float]]:
    return [(a, b, round(theme.contrast(NAV[a], NAV[b]), 2), need) for a, b, need in NAV_PAIRS]


def nav_rail_css() -> str:
    """Appended after the window's theme: the section list is a dark rail beside the Home screen in both light and dark themes."""
    return (f"QListWidget#nav {{ background: {NAV['bg']}; color: {NAV['text']}; border: none; border-right: 1px solid {HUD['panel_edge']}; border-radius: 0; padding: 8px 0; }}\n"
            f"QListWidget#nav::item {{ padding: 10px 16px; border-left: 3px solid transparent; color: {NAV['text']}; background: transparent; }}\n"
            f"QListWidget#nav::item:hover {{ background: {NAV['hover']}; }}\n"
            f"QListWidget#nav::item:selected {{ background: {NAV['sel_bg']}; color: {NAV['text']}; border-left: 3px solid {NAV['edge']}; }}\n")


def home_stylesheet() -> str:
    h = HUD
    return f"""
#homePage, #homePage QWidget {{ background: {h['bg']}; color: {h['text']}; }}
#homePage QLabel {{ background: transparent; }}
#homePage QLabel[muted="true"] {{ color: {h['muted']}; }}
#homePage QFrame#panel {{ background: {h['panel']}; border: 1px solid {h['panel_edge']}; border-radius: 8px; }}
#homePage QFrame#panel QLabel {{ background: transparent; }}
#homePage QPushButton {{ background: {h['panel']}; color: {h['text']}; border: 1px solid {h['panel_edge']}; border-radius: 6px; padding: 6px 14px; min-height: 22px; }}
#homePage QPushButton:hover {{ background: #12304f; }}
#homePage QPushButton:focus {{ border: 2px solid {h['accent']}; }}
#homePage QPushButton:disabled {{ color: {h['off']}; background: {h['bg']}; }}
#homePage QPushButton[primary="true"] {{ background: {h['accent']}; color: {h['bg']}; border: 1px solid {h['accent']}; font-weight: 600; }}
#homePage QProgressBar {{ background: #102238; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; text-align: center; color: {h['text']}; }}
#homePage QProgressBar::chunk {{ background: {h['accent']}; border-radius: 3px; }}
"""
