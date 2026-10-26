"""Windowed entry point (jarvis-gui.exe): no console window. Problems are shown in a message box and written to crash.log."""
import sys

from jarvis.ui.app import gui_main  # noqa: E402

if "--selftest" in sys.argv[1:]:
    from jarvis.ui.app import selftest
    raise SystemExit(selftest())
raise SystemExit(gui_main())
