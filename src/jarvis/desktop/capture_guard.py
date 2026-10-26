"""When Jarvis must NOT look at a window: sign-in and password screens, security programs, password managers, and (competitive) games with anti-cheat.

This is a best-effort filter, not a guarantee. It looks at the program that owns the window, the window title and, where Windows accessibility is
available, whether a password box is visible. A page whose title does not say it is a sign-in page can still show sensitive content. The reasons
returned never repeat the window title (a title can itself contain private text)."""
from __future__ import annotations

import re

from jarvis.desktop.policy import ANTI_CHEAT_PROCESSES, PROTECTED_PROCESSES
from jarvis.memory.secrets import find_secret

SENSITIVE_TITLE = re.compile(
    r"(?i)\b(sign[ -]?in|log[ -]?in|log[ -]?on|password|passcode|passphrase|2fa|mfa|two[- ]factor|multi[- ]factor|verification code|security code|"
    r"authenticator|one[- ]time|otp|credentials?|recovery (?:code|key)|private key|seed phrase|wallet|unlock|vault|keychain|bank(?:ing)?)\b")
PASSWORD_MANAGER_WORDS = ("keepass", "1password", "bitwarden", "lastpass", "dashlane", "nordpass", "enpass", "roboform", "proton pass")


def window_problem(w: dict, running: set[str] | frozenset = frozenset(), game_protected=None) -> str | None:
    proc = (w.get("process") or "").lower()
    if proc in PROTECTED_PROCESSES or any(k in proc for k in PASSWORD_MANAGER_WORDS):
        return f"{proc or 'this program'} is a security, sign-in or password program; Jarvis never looks at it"
    if SENSITIVE_TITLE.search(w.get("title") or ""):
        return "the window title says it is a sign-in, password, verification-code or banking screen; Jarvis does not look at those"
    if proc in ANTI_CHEAT_PROCESSES:
        return "this is anti-cheat software; Jarvis never looks at it"
    if game_protected is not None:
        why = game_protected(proc, running)
        if why:
            return why
    return None


def password_box_visible(backend, hwnd: int) -> bool:
    """True if Windows accessibility reports a visible password field in the window. If accessibility is unavailable the answer is False
    (unknown), which is why the title/program checks above also exist."""
    try:
        return any(e.get("is_password") and not e.get("offscreen") for e in backend.ui_find(hwnd, {}))
    except Exception:  # noqa: BLE001 - unsupported or failing UIA: unknown
        return False


def guard(backend, hwnd: int | None, game_protected=None) -> str | None:
    """Reason not to capture this window (or the whole screen when hwnd is None), else None."""
    try:
        wins = backend.windows()
    except Exception:  # noqa: BLE001
        wins = []
    try:
        running = set(backend.process_names())
    except Exception:  # noqa: BLE001
        running = set()
    if hwnd is None:
        for w in wins:
            if w.get("minimized") or w.get("visible") is False:
                continue
            why = window_problem(w, running, game_protected)
            if why:
                return "the whole screen was not captured because a window that must not be looked at is open (" + why + "). Capture one window instead."
        return None
    for w in wins:
        if w["hwnd"] == hwnd:
            why = window_problem(w, running, game_protected)
            if why:
                return why
            if password_box_visible(backend, hwnd):
                return "a password box is visible in this window; Jarvis does not look at it"
            return None
    return None


def withhold_secrets(lines: list[dict]) -> tuple[list[dict], int]:
    """Replace lines that look like secrets (keys, tokens, card numbers, 'password: ...', or a lone 6-8 digit code) with a marker. Returns (lines, count)."""
    out, n = [], 0
    for ln in lines:
        t = str(ln.get("text", ""))
        if find_secret(t) or re.fullmatch(r"\s*\d{3}[ -]?\d{3,5}\s*", t) or re.search(r"(?i)\b(code|otp|pin)\b\D{0,12}\d{4,8}\b", t):
            out.append({**ln, "text": "[withheld: looks like a secret]", "withheld": True})
            n += 1
        else:
            out.append(ln)
    return out, n
