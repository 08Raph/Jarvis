"""Thin ctypes wrappers over documented Win32 APIs: windows, processes, focus, SendInput, screen capture.
Only importable functions run on Windows; nothing here has been executed on real Windows by the author (see docs)."""
from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes

if sys.platform != "win32":  # pragma: no cover - keeps imports harmless elsewhere
    raise ImportError("jarvis.desktop.win32 is Windows-only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE, SW_SHOW = 9, 5


def set_dpi_aware() -> None:
    """Make window rectangles and mouse coordinates use real pixels (per-monitor DPI aware v2)."""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def is_elevated() -> bool:
    """True when running with an administrator token. If it cannot be determined the answer is True (fail closed): Jarvis then
    only observes."""
    try:
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return True


# ------------------------------------------------------------------ processes
def process_path(pid: int) -> str | None:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def processes() -> dict[int, str]:
    """pid -> exe name (lower case), for every process this user can see."""
    TH32CS_SNAPPROCESS = 0x2
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, wintypes.HANDLE(-1).value):
        return {}
    out = {}
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = e.szExeFile.lower()
            ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        kernel32.CloseHandle(snap)
    return out


def pid_alive(pid: int) -> bool:
    SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x102
    h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        return kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(h)


# ------------------------------------------------------------------ windows
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def window_info(hwnd: int) -> dict:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    path = process_path(pid.value) or ""
    return {"hwnd": int(hwnd), "title": buf.value, "class": cls.value, "pid": int(pid.value),
            "process": path.rsplit("\\", 1)[-1].lower() if path else None, "process_path": path or None,
            "visible": bool(user32.IsWindowVisible(hwnd)), "minimized": bool(user32.IsIconic(hwnd)),
            "rect": [r.left, r.top, r.right, r.bottom]}


def _cloaked(hwnd: int) -> bool:
    try:
        val = ctypes.c_int(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val))
        return bool(val.value)
    except (AttributeError, OSError):
        return False


def top_windows() -> list[dict]:
    """Visible, titled, un-cloaked top-level windows (roughly what Alt+Tab shows)."""
    hwnds = []

    def cb(h, _):
        if user32.IsWindowVisible(h) and user32.GetWindowTextLengthW(h) > 0 and not user32.GetWindow(h, 4):  # GW_OWNER
            hwnds.append(int(h))
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return [window_info(h) for h in hwnds if not _cloaked(h)]


def foreground() -> int:
    return int(user32.GetForegroundWindow() or 0)


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def focus(hwnd: int, wait: float = 1.0) -> bool:
    """Ask Windows to bring a window to the front, then CHECK that it really is in front. Windows can refuse focus changes
    (foreground lock); in that case this returns False and the caller must report it - it does not force anything and it
    never sends input events."""
    if not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if foreground() == hwnd:
            return True
        time.sleep(0.05)
    # Deliberately NO fallback that sends keystrokes to win the foreground lock: focus_window is not an input tool, so it must
    # never inject input. The caller reports that Windows refused, and nothing is typed or clicked.
    return False


# ------------------------------------------------------------------ SendInput
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x2, 0x4
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x1, 0x8000, 0x4000
MOUSE_BTN = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _IU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _IU)]


def _send(inputs: list[INPUT]) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError(f"Windows accepted {sent} of {len(inputs)} input events (blocked by a higher-privilege window or UIPI?)")
    return sent


def send_vk_sequence(seq: list[tuple[int, bool]]) -> None:
    ins = []
    for vk, up in seq:
        i = INPUT(type=INPUT_KEYBOARD)
        i.u.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=0)
        ins.append(i)
    _send(ins)


def send_unicode(text: str, delay: float = 0.004) -> None:
    for ch in text:
        if ch == "\n":
            send_vk_sequence([(0x0D, False), (0x0D, True)])
            continue
        units = ch.encode("utf-16-le")
        ins = []
        for k in range(0, len(units), 2):
            code = struct.unpack("<H", units[k:k + 2])[0]
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                i = INPUT(type=INPUT_KEYBOARD)
                i.u.ki = KEYBDINPUT(wVk=0, wScan=code, dwFlags=flags, time=0, dwExtraInfo=0)
                ins.append(i)
        _send(ins)
        if delay:
            time.sleep(delay)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    vl, vt = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)          # virtual screen origin
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    ax = int((x - vl) * 65535 / max(1, vw - 1))
    ay = int((y - vt) * 65535 / max(1, vh - 1))
    down, up = MOUSE_BTN[button]
    pos = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    ins = []
    for flags in [MOUSEEVENTF_MOVE | pos] + [down | pos, up | pos] * (2 if double else 1):
        i = INPUT(type=INPUT_MOUSE)
        i.u.mi = MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
        ins.append(i)
    _send(ins)


# ------------------------------------------------------------------ screen capture -> PNG bytes
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def capture(hwnd: int | None = None) -> tuple[int, int, bytes]:
    """Returns (width, height, PNG bytes) of a window (via PrintWindow) or of the whole virtual screen."""
    if hwnd:
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        left, top, w, h = r.left, r.top, r.right - r.left, r.bottom - r.top
    else:
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        raise OSError("window has no visible area (minimized?)")
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    user32.GetDC.restype = wintypes.HDC
    screen = user32.GetDC(None)
    if not screen:
        raise OSError("could not get the screen device context")
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h) if mem else None
    if not mem or not bmp:
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)
        raise OSError("could not create a bitmap for the capture (window or screen too large?)")
    old = gdi32.SelectObject(mem, bmp)
    selected = True
    try:
        done = False
        if hwnd:
            done = bool(user32.PrintWindow(hwnd, mem, 2))          # PW_RENDERFULLCONTENT
        if not done:
            if not gdi32.BitBlt(mem, 0, 0, w, h, screen, left, top, 0x00CC0020 | 0x40000000):   # SRCCOPY | CAPTUREBLT
                raise OSError("BitBlt failed")
        bi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h, biPlanes=1, biBitCount=32,
                              biCompression=0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.SelectObject(mem, old)                      # GetDIBits requires that the bitmap is NOT selected into a DC
        selected = False
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0) != h:
            raise OSError("GetDIBits failed")
        return w, h, bgra_to_png(w, h, buf.raw)
    finally:
        if selected:
            gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def bgra_to_png(w: int, h: int, bgra: bytes) -> bytes:
    from jarvis.desktop.png import encode_rgb
    rgb = bytearray(w * h * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return encode_rgb(w, h, bytes(rgb))


# ------------------------------------------------------------------ monitors and moving a window (documented APIs; NOT run on a real PC by the author)
MONITORINFOF_PRIMARY = 1
SW_MAXIMIZE, SW_SHOWNORMAL = 3, 1
SWP_NOZORDER, SWP_NOACTIVATE = 0x4, 0x10


class MONITORINFO(ctypes.Structure):          # MONITORINFOEXW: the extra szDevice is "\\.\DISPLAY2", the number Windows shows in Settings > Display
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD), ("szDevice", ctypes.c_wchar * 32)]


def monitors() -> list[dict]:
    """The connected screens: `number` is the one Windows shows in Settings > Display (the DISPLAYn device name), `index` is the position from the left (1 = leftmost), plus whether it is
    the main one and its usable area (work area) in pixels."""
    found: list[dict] = []
    MonitorEnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def cb(hmon, _hdc, _rect, _lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            w = mi.rcWork
            digits = "".join(ch for ch in mi.szDevice if ch.isdigit())
            found.append({"handle": int(hmon) if hmon else 0, "primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY), "number": int(digits) if digits else 0,
                          "left": w.left, "top": w.top, "right": w.right, "bottom": w.bottom})
        return True
    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    found.sort(key=lambda m: (m["left"], m["top"]))
    for i, m in enumerate(found, 1):
        m["index"] = i
        if not m["number"]:
            m["number"] = i
    return found


def move_window_to_monitor(hwnd: int, index: int) -> dict:
    """Move a window to the screen with this `number` (see monitors()), keeping its size where it fits and its maximized state. Raises LookupError for an unknown screen."""
    mons = monitors()
    target = next((m for m in mons if m["number"] == index), None)
    if target is None:
        raise LookupError(f"there is no screen {index} (I see {len(mons)})")
    if not user32.IsWindow(hwnd):
        raise LookupError("that window is gone")
    was_max = bool(user32.IsZoomed(hwnd))
    if user32.IsIconic(hwnd) or was_max:
        user32.ShowWindow(hwnd, SW_RESTORE)
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    tw, th = target["right"] - target["left"], target["bottom"] - target["top"]
    w, h = min(w, tw), min(h, th)
    x = target["left"] + max(0, (tw - w) // 2)
    y = target["top"] + max(0, (th - h) // 2)
    if not user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE):
        raise OSError(f"Windows refused to move the window (error {ctypes.get_last_error()}); it may belong to a program running as administrator")
    if was_max:
        user32.ShowWindow(hwnd, SW_MAXIMIZE)
    return {"screen": index, "maximized": was_max, "rect": [x, y, x + w, y + h]}



# ------------------------------------------------------------------ clipboard text and "what is highlighted"
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x2
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]


def _open_clipboard(tries: int = 10) -> bool:
    for _ in range(tries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str | None) -> bool:
    """Put text on the clipboard; None or "" empties it."""
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        if not text:
            return True
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, ctypes.addressof(data), size)
        kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


def selected_text(wait: float = 0.6) -> str | None:
    """The text currently highlighted in the front window: press Ctrl+C, read what arrives on the clipboard, then put the old clipboard text back. None when nothing was highlighted
    (or the program does not support copying). Only the clipboard text is touched; other clipboard formats (images, files) are not restored, so an image on the clipboard is replaced."""
    before_seq = user32.GetClipboardSequenceNumber()
    old = get_clipboard_text()
    send_vk_sequence([(0x11, False), (0x43, False), (0x43, True), (0x11, True)])        # (key, is_release): Ctrl down, C down, C up, Ctrl up
    end = time.monotonic() + wait
    got = None
    while time.monotonic() < end:
        if user32.GetClipboardSequenceNumber() != before_seq:
            got = get_clipboard_text()
            break
        time.sleep(0.03)
    if got is not None:
        set_clipboard_text(old)
    return got or None
