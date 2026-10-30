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


def _install_bundled_model(app, cfg) -> None:
    """First start after installing: copy the model that shipped inside the installer into this profile (local file copy, no network, about a minute).
    Does nothing when there is no bundled model, when this profile already has one, or when it was done before. Failure is not fatal: the setup wizard
    then offers the usual ways to get a model."""
    try:
        from jarvis.models import bundled
        if not bundled.pending(cfg):
            return
    except Exception:  # noqa: BLE001 - never block the window on this check
        return
    import threading
    import time
    from PySide6.QtWidgets import QProgressDialog
    result: dict = {}
    t = threading.Thread(target=lambda: result.update(_run_seed(cfg)), daemon=True)
    dlg = QProgressDialog("Setting up the AI model that came with Jarvis. This stays on this PC and takes about a minute, only once.", "", 0, 0)
    dlg.setWindowTitle(V.APP_NAME)
    dlg.setCancelButton(None)
    dlg.setMinimumDuration(0)
    dlg.show()
    t.start()
    while t.is_alive():
        app.processEvents()
        time.sleep(0.05)
    dlg.close()


def _run_seed(cfg) -> dict:
    try:
        from jarvis.models import bundled
        return bundled.seed(cfg)
    except Exception as e:  # noqa: BLE001
        return {"state": "refused", "detail": f"{type(e).__name__}"}


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
    if not (guest or simulate):
        _install_bundled_model(app, cfg)
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
        ctl.voice_autostart()          # starts wake-word listening if the user enabled it, or on a fresh profile where they never chose (an explicit OFF is remembered); the microphone still opens only with a sound device AND a local Vosk model
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


SELFTEST_TIMEOUT_S = 180.0


def _qt_checks(rep, cfg) -> None:
    """The window part of the self-test. Real Qt widgets are built on Qt's OFFSCREEN platform (no screen, no real theme/DPI behaviour), every page is
    refreshed WITHOUT the window's usual error swallowing, and 'Run Safe Preflight' (offline part) is started through the controller's worker exactly
    as the button does and waited for while Qt events are processed. Failures are recorded as checks; they are not raised."""
    import sys
    from PySide6.QtWidgets import QApplication
    from jarvis.app import selftest as S
    from jarvis.app.controller import AppController
    from jarvis.ui import window as W
    from jarvis.ui.setup_dialog import SetupDialog
    app = QApplication.instance() or QApplication(sys.argv)
    bridge = W._Bridge()
    ctl = AppController(cfg, simulate=True, dispatch=lambda fn: bridge.call.emit(fn))
    win = W.MainWindow(ctl, bridge)
    win.show()
    dlg = SetupDialog(ctl, win)       # the wizard window must build and show too; it is closed straight away
    dlg.show()
    app.processEvents()
    dlg.close()
    names = [win.nav.item(i).text() for i in range(win.nav.count())]
    for i in range(win.nav.count()):
        win.nav.setCurrentRow(i)
        app.processEvents()
    rep.add("qt_window", "Window and pages build", S.PASS if names == list(W.PAGES) else S.FAIL, S.GENUINE,
            f"{len(names)} pages built and visited on Qt platform '{app.platformName()}' (offscreen = no visible window): {', '.join(names)}"
            if names == list(W.PAGES) else f"navigation {names} differs from the expected pages {list(W.PAGES)}")
    bad = []
    for n in W.PAGES:              # MainWindow.refresh() turns a page error into a status-bar message; call the page's own refresh so that an error is seen
        try:
            getattr(win, "_refresh_" + W._slug(n))()
        except Exception as e:  # noqa: BLE001
            bad.append(f"{n}: {type(e).__name__}: {str(e)[:120]}")
    rep.add("qt_refresh", "Every page refreshes without error", S.FAIL if bad else S.PASS, S.GENUINE, "; ".join(bad) if bad else f"{len(W.PAGES)} page refresh methods ran directly.")
    has = all(hasattr(win, a) for a in ("pf_run", "pf_cancel", "pf_table", "pf_verdict"))
    if not has:
        rep.add("preflight_offline", "Run Safe Preflight (offline part)", S.FAIL, S.GENUINE, "the System & Model Readiness page has no Run/Cancel button, result table or verdict label")
    else:
        S.run_preflight_via_controller(rep, ctl, cfg, app.processEvents, shown=lambda: (win.pf_table.rowCount(), win.pf_verdict.text()), timeout=SELFTEST_TIMEOUT_S)
    S.add_limits(rep)
    rep.add("controller_mode", "Controller mode", S.INFO, S.SIMULATED, "AppController(simulate=True): chat replies are stand-ins; nothing here exercises a model.")
    win.close()
    ctl.shutdown(timeout=2.0)
    app.processEvents()


def selftest(out_dir=None) -> int:
    """`jarvis-gui.exe --selftest`: see jarvis.app.selftest for the checks, the kinds and the exit codes (0 pass, 1 a check failed, 3 Qt could not load).
    Runs in a throw-away data folder and touches nothing of the user's; writes jarvis-gui-selftest.txt/.json to the temp folder."""
    import os
    import tempfile
    from pathlib import Path
    from jarvis.app import selftest as S
    out = Path(out_dir) if out_dir else Path(tempfile.gettempdir())
    rep = S.Report()
    S.mark_running(out)
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        qt_ok = True
        try:
            import PySide6.QtWidgets  # noqa: F401
        except ImportError as e:      # PySide6 missing, or a DLL / Visual C++ runtime problem on Windows
            qt_ok = False
            rep.add("qt_load", "Qt (PySide6) loads", S.FAIL, S.GENUINE, f"{type(e).__name__}: {str(e)[:200]}")
            rep.exit_override = S.EXIT_NO_QT
        S.check_files(rep)
        if qt_ok:
            rep.add("qt_load", "Qt (PySide6) loads", S.PASS, S.GENUINE, "PySide6.QtWidgets imported.")
            S.check_modules(rep)
            from jarvis.config import Config
            with tempfile.TemporaryDirectory() as d:
                _qt_checks(rep, Config(data_dir=Path(d) / "data"))
    except BaseException as e:  # noqa: BLE001 - the report must say what happened, never just disappear
        rep.add("selftest_crash", "Self-test ran to the end", S.FAIL, S.GENUINE, f"{type(e).__name__}: {e}")
    code = rep.exit_code()
    try:
        S.write(rep, out)
    except OSError:
        pass
    return code
