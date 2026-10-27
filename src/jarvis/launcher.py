"""Start-up behaviour for the packaged jarvis.exe on a normal person's PC.

Problems this solves (all of them make an app look "broken" on someone else's computer):
  * Double-clicking jarvis.exe opens a console that closes instantly. With no arguments we now run the guided
    setup, and when the console belongs only to this program we wait for Enter before it closes.
  * A crash in a frozen app shows a traceback for a split second. We print a short plain message, keep the
    details in a local crash.log (never sent anywhere) and wait for Enter when the window would vanish.
Standard library only; nothing here touches the network.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path
from typing import Callable, Sequence


def console_is_ours() -> bool:
    """True on Windows when this process is the only one attached to its console (typically a double-click).

    When started from cmd/PowerShell/Terminal, the shell is attached too (count >= 2), so we never pause there.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        buf = (ctypes.c_uint * 4)()
        n = ctypes.windll.kernel32.GetConsoleProcessList(buf, 4)  # type: ignore[attr-defined]
        return n == 1
    except Exception:
        return False


def _wait(input_fn: Callable[[str], str]) -> None:
    try:
        input_fn("\nPress Enter to close this window...")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def crash_log_path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from jarvis.cli import default_data_dir
        data_dir = default_data_dir()
    return Path(data_dir) / "crash.log"


def _scrub(text: str) -> str:
    """Best effort: remove anything that looks like a password, key, token or card number before text is shown or written.
    It cannot be guaranteed to catch every secret."""
    try:
        from jarvis.memory.secrets import redact
        return redact(text)
    except Exception:
        return "(details hidden)"


def write_crash_log(exc: BaseException, data_dir: Path | None = None) -> Path | None:
    """Keep technical details on this PC only. Returns the file, or None if it could not be written."""
    try:
        p = crash_log_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.exists() and p.stat().st_size > 200_000:
                p.write_text("", encoding="utf-8")   # simple size cap
        except OSError:
            pass
        from jarvis import __version__
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} Project JARVIS {__version__} ===\n")
            from jarvis.disclosure import safe_exception_text
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            f.write(_scrub(safe_exception_text(exc, tb)))
        return p
    except Exception:
        return None


def run(main: Callable[[Sequence[str] | None], int], argv: Sequence[str] | None = None,
        input_fn: Callable[[str], str] = input, ours: Callable[[], bool] = console_is_ours,
        data_dir: Path | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    double_clicked = not args and ours()
    if not args:
        args = ["setup"]          # a bare jarvis.exe starts the guided setup instead of an argparse error
    try:
        rc = main(args)
    except SystemExit as e:        # argparse errors / explicit exits
        rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except KeyboardInterrupt:
        print("\nStopped.")
        rc = 130
    except BaseException as e:     # noqa: BLE001 - last line of defence in a packaged app
        from jarvis.disclosure import scrub_error_text
        log = write_crash_log(e, data_dir)
        print(f"\nProject JARVIS hit an unexpected problem: {type(e).__name__}: {_scrub(scrub_error_text(str(e)))}")
        if log:
            print(f"Technical details were saved on this PC only: {log}")
        else:
            print("Technical details could not be saved (the data folder is not writable).")
        print("Nothing was sent anywhere. If you ask for help you may attach that file; it can contain file paths with your Windows user name, so look before sharing.")
        rc = 1
        double_clicked = double_clicked or ours()
    if double_clicked:
        _wait(input_fn)
    return rc
