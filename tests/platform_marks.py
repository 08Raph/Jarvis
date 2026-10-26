"""Markers for tests that assert facts about the LINUX authoring sandbox ("no llama-cpp here", "no speech engine here", "no GPU listing").
They are correct there and wrong on a Windows PC, which really has those things. On Windows they are skipped and the real Windows facts are
established by the CI real-model run (scripts/ci_real_model_test.ps1), not by pretending the sandbox assertion holds."""
import sys
import unittest

WINDOWS = sys.platform == "win32"


def linux_sandbox_only(why):
    return unittest.skipIf(WINDOWS, f"asserts a Linux-sandbox fact ({why}); not applicable on Windows")


def not_when_elevated():
    """Jarvis deliberately observes only when it is run as Administrator (least privilege); tests of acting need a normal token."""
    elevated = False
    if WINDOWS:
        try:
            import ctypes
            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            elevated = False
    return unittest.skipIf(elevated, "this process is elevated (Administrator): Jarvis refuses to act when elevated, by design")
