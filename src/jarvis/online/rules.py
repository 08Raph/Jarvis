"""Sign-up input rules, shown to the person and checked in the window BEFORE anything is sent. The SERVER repeats every check and is the authority
(server/jarvis_server/security.py); a test compares the two on a shared list of examples so they cannot drift apart silently."""
from __future__ import annotations

import re
import unicodedata

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,31}$")
COMMON_PASSWORDS = {"password", "password1", "1234567890", "qwertyuiop", "iloveyou12", "letmein123", "administrator", "welcome123"}
_DISPLAY_BAD = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")

PASSWORD_REQUIREMENTS = ("At least 10 characters (at most 200); not a common password; not your username; at least 5 different characters. "
                         "A long phrase of several words is the easiest way to meet this.")
USERNAME_REQUIREMENTS = "3-32 characters: lower-case letters, digits, dot, underscore or hyphen; it must start with a letter or digit."
DISPLAY_REQUIREMENTS = "1-40 characters; shown only to you. Optional (defaults to your username)."


def normalize_username(u: str) -> str:
    return (u or "").strip().lower()


def username_problem(u: str) -> str | None:
    return None if USERNAME_RE.match(normalize_username(u)) else "Username: " + USERNAME_REQUIREMENTS


def password_problem(pw: str, username: str) -> str | None:
    if not isinstance(pw, str) or len(pw) < 10:
        return "The password must be at least 10 characters."
    if len(pw) > 200:
        return "The password is too long (at most 200 characters)."
    if pw.lower() in COMMON_PASSWORDS or pw.lower() == normalize_username(username):
        return "That password is too easy to guess."
    if len(set(pw)) < 5:
        return "The password needs more variety (at least 5 different characters)."
    return None


def display_name_problem(name: str) -> str | None:
    n = unicodedata.normalize("NFC", name or "").strip()
    if not 1 <= len(n) <= 40:
        return "The display name must be 1-40 characters."
    if _DISPLAY_BAD.search(n):
        return "The display name contains characters that are not allowed."
    return None
