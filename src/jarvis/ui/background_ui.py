"""Tray icon, 'keep running when closed', and the wallpaper display. PySide6 is NOT available in the authoring sandbox, so this file has never been run
there; the choices it follows are stored and tested in jarvis.app.background. Windows-only parts are guarded and fall back quietly."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget, QVBoxLayout

from jarvis import version as V
from jarvis.ui.hud import HudWidget


def app_icon() -> QIcon:
    """The Jarvis logo mark (shipped next to this file); a drawn circle if the file is missing."""
    from pathlib import Path
    f = Path(__file__).parent / "assets" / "jarvis.png"
    try:
        if f.is_file():
            ic = QIcon(str(f))
            if not ic.isNull():
                return ic
    except Exception:  # noqa: BLE001
        pass
    return _icon()


def _icon() -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#2fd3ff"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(3, 3, 26, 26)
    p.setBrush(QColor("#06121a"))
    p.drawEllipse(10, 10, 12, 12)
    p.end()
    return QIcon(pm)


def _attach_behind_icons(widget: QWidget) -> bool:
    """Windows: put the widget inside the desktop's WorkerW window so it sits behind the icons like a wallpaper. Returns False when that did not work."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        progman = u.FindWindowW("Progman", None)
        if not progman:
            return False
        res = wintypes.DWORD()
        u.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(res))
        found: list[int] = []
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd, _l):
            if u.FindWindowExW(hwnd, None, "SHELLDLL_DefView", None):
                w = u.FindWindowExW(None, hwnd, "WorkerW", None)
                if w:
                    found.append(w)
            return True
        u.EnumWindows(proto(each), 0)
        if not found:
            return False
        return bool(u.SetParent(int(widget.winId()), found[0]))
    except Exception:  # noqa: BLE001
        return False


class WallpaperWindow(QWidget):
    """The animated Jarvis display filling the main screen, behind everything. It shows the same state as the window; it cannot be clicked (it is wallpaper)."""

    def __init__(self, provider):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background:#05090d;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.hud = HudWidget(provider=provider)
        lay.addWidget(self.hud)
        self.attached = False

    def show_on_desktop(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is not None:
            self.setGeometry(scr.geometry())
        self.show()
        self.attached = _attach_behind_icons(self)
        if self.attached and scr is not None:
            self.setGeometry(0, 0, scr.geometry().width(), scr.geometry().height())
        self.hud.timer.start(66)
        self.hud._reschedule()

    def hide_wallpaper(self) -> None:
        self.hud.timer.stop()
        self.hide()


class BackgroundManager:
    """Owns the tray icon and the wallpaper window for one MainWindow."""

    def __init__(self, win):
        self.win = win
        self.ctl = win.ctl
        self.force_quit = False
        self.tray = None
        self.wall = None
        self._told = False

    def _ensure_tray(self):
        """The tray icon is created only when it is really needed (the first time the window is hidden to it), never while building the window: a tray icon that exists
        in tests or in a self-test has nothing to do there and was the prime suspect of a Windows CI crash."""
        if self.tray is not None:
            return self.tray
        try:
            ok = bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            return None
        self.tray = QSystemTrayIcon(app_icon(), self.win)
        self.tray.setToolTip(f"{V.APP_NAME} (running)")
        menu = QMenu(self.win)                       # parented, so it is destroyed with the window and never outlives the tray icon that points at it
        a_open = QAction("Open Jarvis", menu)
        a_open.triggered.connect(self.show_window)
        a_quit = QAction("Quit Jarvis completely", menu)
        a_quit.triggered.connect(self.quit_now)
        menu.addAction(a_open)
        menu.addAction(a_quit)
        self._menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.show_window() if r in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        return self.tray

    # ---- window
    def wants_background(self) -> bool:
        # tray mode exists only in the real application (run_gui turns "quit on last window closed" off); a window opened by a test or the self-test closes normally
        if self.force_quit or QApplication.quitOnLastWindowClosed():
            return False
        return self.ctl.background_options()["on_close"] == "background" and self._ensure_tray() is not None

    def hide_to_tray(self) -> None:
        self._ensure_tray().show()
        self.win.hide()
        if not self._told:
            self._told = True
            self.tray.showMessage(V.APP_NAME, "Still running in the background. Use the tray icon to open it or to quit completely.",
                                  QSystemTrayIcon.MessageIcon.Information, 4000)

    def show_window(self) -> None:
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def quit_now(self) -> None:
        self.force_quit = True
        self.win.close()
        QApplication.quit()

    def finish(self) -> None:
        if self.tray is not None:
            self.tray.hide()
        if self.wall is not None:
            self.wall.hide_wallpaper()

    # ---- wallpaper
    def apply_wallpaper(self) -> None:
        on = bool(self.ctl.background_options()["wallpaper"])
        if on and self.wall is None:
            self.wall = WallpaperWindow(self.win._hud_provider)
        if on:
            self.wall.show_on_desktop()
        elif self.wall is not None:
            self.wall.hide_wallpaper()
