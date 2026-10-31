"""Windows settings: open any supported Settings page, wallpaper, brightness. Settings that need administrator rights are opened, never forced."""
from __future__ import annotations

import re

from jarvis.operator.core import HIGH, LOW, MEDIUM, Outcome, OperatorError, cap

PAGES = {"display": "ms-settings:display", "sound": "ms-settings:sound", "bluetooth": "ms-settings:bluetooth", "wifi": "ms-settings:network-wifi", "wi-fi": "ms-settings:network-wifi",
         "network": "ms-settings:network", "power": "ms-settings:powersleep", "battery": "ms-settings:batterysaver", "apps": "ms-settings:appsfeatures", "startup": "ms-settings:startupapps",
         "notifications": "ms-settings:notifications", "accessibility": "ms-settings:easeofaccess", "mouse": "ms-settings:mousetouchpad", "keyboard": "ms-settings:typing",
         "language": "ms-settings:regionlanguage", "region": "ms-settings:regionformat", "date": "ms-settings:dateandtime", "time": "ms-settings:dateandtime",
         "default apps": "ms-settings:defaultapps", "printers": "ms-settings:printers", "privacy": "ms-settings:privacy", "microphone": "ms-settings:privacy-microphone",
         "camera": "ms-settings:privacy-webcam", "personalization": "ms-settings:personalization", "background": "ms-settings:personalization-background", "themes": "ms-settings:themes",
         "windows update": "ms-settings:windowsupdate", "storage": "ms-settings:storagesense", "about": "ms-settings:about", "night light": "ms-settings:nightlight",
         "magnifier": "ms-settings:easeofaccess-magnifier", "narrator": "ms-settings:easeofaccess-narrator", "high contrast": "ms-settings:easeofaccess-highcontrast",
         "captions": "ms-settings:easeofaccess-closedcaptioning", "voice access": "ms-settings:easeofaccess-speechrecognition", "text size": "ms-settings:easeofaccess-display"}
EXPLAIN = {"display": "Resolution, scaling and refresh rate are changed here; I open the page rather than change them blindly so you don't end up with a black screen.",
           "bluetooth": "Windows only lets programs switch Bluetooth through its own settings page, so I open it.", "wifi": "Changing Wi-Fi or joining a network needs your approval and password, so I open the page."}


@cap("settings.open", "settings", "Open a Windows Settings page", LOW, ("page",), True, "15,53")
def open_page(op, page: str = "") -> Outcome:
    k = page.lower().strip().removeprefix("the ").removesuffix(" settings")
    url = PAGES.get(k)
    if not url:
        raise OperatorError("I can open: " + ", ".join(sorted(set(PAGES))[:30]) + ".")
    op.ops.open_target(url)
    return Outcome("unverified", f"Opened the {k} settings." + (" " + EXPLAIN[k] if k in EXPLAIN else ""))


@cap("settings.wallpaper", "settings", "Change the desktop wallpaper to a picture", MEDIUM, ("path",), True, "15")
def wallpaper(op, path: str = "") -> Outcome:
    p = op.check_path(path)
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".bmp"):
        raise OperatorError("Use a .jpg, .png or .bmp picture.")
    ok = op.ops.set_wallpaper(str(p))
    return Outcome("unverified" if ok else "failed", f"Set {p.name} as the wallpaper (Windows doesn't tell me how it looks)." if ok else "Windows refused the change.")


@cap("settings.brightness", "settings", "Set screen brightness (laptops and supported monitors)", MEDIUM, ("percent",), True, "15")
def brightness(op, percent: int = 70) -> Outcome:
    p = int(percent)
    if not 0 <= p <= 100:
        raise OperatorError("Brightness is 0 to 100.")
    op.ops.powershell(f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{p})", 20)
    raw = op.ops.powershell("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness", 15).strip()
    ok = bool(re.fullmatch(r"\d+", raw)) and abs(int(raw) - p) <= 5
    return Outcome("verified" if ok else "unverified", f"Brightness is now {raw}%." if ok else "I sent the change but couldn't confirm it (desktop monitors usually don't support this; use the monitor's own buttons).")
