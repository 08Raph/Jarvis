"""Entry point of the desktop window (`jarvis gui`, `jarvis-gui.exe`). Everything that can go wrong before the window exists is reported in plain
language (a message box when a toolkit is present, the console otherwise) instead of a traceback. Imports PySide6 lazily so the rest of Jarvis
works without it."""
from __future__ import annotations

import os
import sys

from jarvis import version as V


def qt_available() -> bool:
    from jarvis.app.status import has_module
    return has_module("PySide6")


def _report(title: str, text: str) -> None:
    print(f"{title}: {text}", file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, f"{V.APP_NAME}: {title}", 0x10)     # MB_ICONERROR; works without Qt in a windowed exe
        except Exception:  # noqa: BLE001
            pass


def run_gui(cfg, guest: bool = False, simulate: bool = False) -> int:
    from jarvis.app.lifecycle import StartupError, prepare
    try:
        prepare(cfg)
    except StartupError as e:
        _report("Cannot start", e.text())
        return 2
    if not qt_available():
        _report("The window needs PySide6", "PySide6 is not installed in this copy of Jarvis, so no window can be shown. "
                "Use the installer build, or `pip install \"PySide6>=6.7,<7\"`. The command-line features (`jarvis chat`, `jarvis mission ...`) still work.")
        return 3
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        from jarvis.app.controller import AppController
        from jarvis.ui.window import MainWindow, _Bridge
    except ImportError as e:      # PySide6 is on disk but Qt could not load (missing DLL / Visual C++ runtime / damaged install)
        _report("The window could not load Qt", f"PySide6 is installed but failed to load ({type(e).__name__}: {str(e)[:200]}). "
                "On Windows this usually means the Visual C++ runtime is missing: install the latest 'Microsoft Visual C++ Redistributable (x64)' "
                "or reinstall Project JARVIS. The command-line features still work.")
        return 3
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(V.APP_NAME)
    app.setApplicationVersion(V.__version__)
    bridge = _Bridge()
    ctl = AppController(cfg, guest=guest, simulate=simulate, dispatch=lambda fn: bridge.call.emit(fn))
    if not ctl.start():
        QMessageBox.information(None, V.APP_NAME, "Jarvis is already open for this profile. Switch to that window instead of starting a second one.")
        return 4
    try:
        win = MainWindow(ctl, bridge)
        win.show()
        if ctl.needs_setup():          # first run (or the model is missing/damaged): the wizard opens by itself; "Set up later" is always available
            from jarvis.ui.setup_dialog import SetupDialog
            SetupDialog(ctl, win).exec()
            win.refresh_all()
        ctl.voice_autostart()          # only starts if the user turned hands-free voice ON earlier; a no-op otherwise (never opens the microphone by itself)
        return app.exec()
    finally:
        ctl.shutdown(timeout=2.0)


def gui_main() -> int:
    """Windowed entry (jarvis-gui.exe). A crash is written to crash.log (redacted) and shown once; never silently disappears."""
    from jarvis.cli import default_data_dir
    from jarvis.config import DEFAULT_PROFILE, Config, valid_profile
    from jarvis.launcher import write_crash_log
    try:
        # Same rule as the command line (cli._cfg): JARVIS_PROFILE picks the profile, so the window and `jarvis ...` never open different memories.
        profile = valid_profile(os.environ.get("JARVIS_PROFILE") or DEFAULT_PROFILE)
    except ValueError as e:
        _report("Cannot start", f"The JARVIS_PROFILE setting is not a valid profile name ({e}). Remove or correct it, then start Jarvis again.")
        return 2
    cfg = Config(data_dir=default_data_dir(), profile=profile)
    try:
        return run_gui(cfg)
    except BaseException as e:  # noqa: BLE001
        log = write_crash_log(e, cfg.data_dir)
        where = (f"Technical details were saved on this PC only: {log}" if log
                 else "Technical details could not be saved (the data folder is not writable).")
        _report("Unexpected problem", f"{type(e).__name__}. {where}")
        return 1


def selftest() -> int:
    """`jarvis-gui.exe --selftest`: build the real window with an offscreen Qt platform in a throw-away data folder, process events, close, and write
    PASS/FAIL to %TEMP%\\jarvis-gui-selftest.txt (a windowed exe has no console). Used by the build script; it touches nothing of the user's."""
    import os
    import tempfile
    from pathlib import Path
    out = Path(tempfile.gettempdir()) / "jarvis-gui-selftest.txt"
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from jarvis.app.controller import AppController
        from jarvis.config import Config
        from jarvis.ui.window import MainWindow, _Bridge
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(data_dir=Path(d) / "data")
            app = QApplication.instance() or QApplication(sys.argv)
            bridge = _Bridge()
            ctl = AppController(cfg, simulate=True, dispatch=lambda fn: bridge.call.emit(fn))
            win = MainWindow(ctl, bridge)
            win.show()
            from jarvis.ui.setup_dialog import SetupDialog
            dlg = SetupDialog(ctl, win)       # the wizard window must build and show too (offscreen); it is closed straight away
            dlg.show()
            app.processEvents()
            dlg.close()
            for i in range(win.nav.count()):
                win.nav.setCurrentRow(i)
                app.processEvents()
            win.close()
            app.processEvents()
        out.write_text("PASS\n", encoding="utf-8")
        return 0
    except BaseException as e:  # noqa: BLE001
        out.write_text(f"FAIL {type(e).__name__}: {e}\n", encoding="utf-8")
        return 1
