"""Consent notices for the Prompt 8 permissions. Shown by the command line ONLY (never by a tool or a model); consent is recorded only when you type
'yes' (or pass --yes on purpose). Turning a permission off withdraws the recorded consent."""
from __future__ import annotations

import sys
import time

NOTICES = {
    "screen": ("SCREEN CAPTURE. When you approve a request, Jarvis takes ONE screenshot of ONE window (or the screen), reads its text with OCR and its buttons with Windows "
               "accessibility, and discards the picture; it is not saved. Nothing is captured in the background or without your approval of each request. Sign-in, password, "
               "verification-code, banking, password-manager and anti-cheat windows are refused, and text that looks like a secret is withheld. Nothing is sent anywhere. "
               "What was seen is held in memory for a few minutes and is not written to the audit log. Jarvis has no image-understanding model: OCR can misread and cannot "
               "describe pictures."),
    "monitoring": ("SYSTEM MONITORING. On request, Jarvis reads CPU, memory, disk, GPU/temperature (if a reliable source exists), the busiest process names and its own usage. "
                   "Read-only; nothing runs in the background; it never ends a process or changes a setting. Process names are read, never their command lines or files. "
                   "Readings stay on this PC."),
    "gaming": ("GAMING MODE. Jarvis can recognise known games by program name, show whole-PC CPU/memory/GPU readings, compute FPS statistics from log files YOU recorded with another "
               "tool, and keep your game profiles (notes only). It sends no input, reads no game memory, never touches anti-cheat and changes no game or system setting. "
               "It cannot measure FPS by itself. Nothing is sent anywhere."),
}
RECORDED = ("monitoring", "gaming")      # categories whose consent is stored in the permission file (policy.CONSENT_REQUIRED)


def ask(cat: str, assume_yes: bool = False, stream=None) -> bool:
    if cat not in NOTICES:
        return True
    out = stream or sys.stdout
    print("\n" + NOTICES[cat] + "\n", file=out)
    if assume_yes:
        print("(agreed with --yes)", file=out)
        return True
    try:
        return input("Type 'yes' to agree and turn it on: ").strip().lower() == "yes"
    except EOFError:
        return False


def record(settings, cat: str) -> None:
    if cat in RECORDED:
        settings.consents[cat] = time.strftime("%Y-%m-%dT%H:%M:%S")


def withdraw(settings, cat: str) -> None:
    settings.consents.pop(cat, None)
