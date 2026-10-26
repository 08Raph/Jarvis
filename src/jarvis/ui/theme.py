"""Colours, type scale and the style sheet of the desktop window. Pure Python (no Qt import), so the contrast of every text/background pair is
checked by automated tests: body text must reach WCAG AA (4.5:1), large/secondary text 3:1 at least. Light and dark follow the system setting."""
from __future__ import annotations

LIGHT = {
    "bg": "#f6f7f9", "surface": "#ffffff", "surface_alt": "#eef0f4", "border": "#c9ced8",
    "text": "#1b1f27", "text_muted": "#4a5260", "accent": "#1f5fbf", "accent_text": "#ffffff", "accent_soft": "#e3ecfa",
    "ok": "#0f6b3a", "warn": "#8a5a00", "danger": "#a31d1d", "danger_text": "#ffffff", "focus": "#0b3d91",
    "user_bg": "#e3ecfa", "assistant_bg": "#ffffff", "tool_bg": "#eef0f4", "error_bg": "#fbe9e9", "confirm_bg": "#fff3d6",
}
DARK = {
    "bg": "#14171c", "surface": "#1c2027", "surface_alt": "#252a33", "border": "#3a414d",
    "text": "#e8ebf0", "text_muted": "#b0b8c6", "accent": "#7fb0ff", "accent_text": "#0c1522", "accent_soft": "#22344f",
    "ok": "#6fd39b", "warn": "#f0c05a", "danger": "#ff8d8d", "danger_text": "#2a0c0c", "focus": "#a9c9ff",
    "user_bg": "#22344f", "assistant_bg": "#1c2027", "tool_bg": "#252a33", "error_bg": "#3b2020", "confirm_bg": "#3b3220",
}
# (foreground token, background token, minimum contrast) - every pair the stylesheet and the message bubbles actually use
PAIRS = [
    ("text", "bg", 4.5), ("text", "surface", 4.5), ("text", "surface_alt", 4.5), ("text_muted", "bg", 4.5), ("text_muted", "surface", 4.5),
    ("text_muted", "surface_alt", 4.5), ("accent_text", "accent", 4.5), ("text", "accent_soft", 4.5), ("accent", "surface", 4.5),
    ("danger_text", "danger", 4.5), ("ok", "surface", 4.5), ("warn", "surface", 4.5), ("danger", "surface", 4.5), ("danger", "error_bg", 4.5),
    ("text", "user_bg", 4.5), ("text", "assistant_bg", 4.5), ("text", "tool_bg", 4.5), ("text", "error_bg", 4.5), ("text", "confirm_bg", 4.5),
    ("focus", "surface", 3.0), ("focus", "bg", 3.0), ("border", "surface", 1.5),
]
SCALES = {"100%": 1.0, "125%": 1.25, "150%": 1.5}
BASE_PT = 10


def _lin(c: int) -> float:
    v = c / 255
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def palette(dark: bool) -> dict:
    return dict(DARK if dark else LIGHT)


def kind_colors(p: dict) -> dict:
    """background + label colour for each transcript kind (the label text uses 'text' except errors, which use 'danger')."""
    return {"user": (p["user_bg"], p["text"]), "assistant": (p["assistant_bg"], p["text"]), "tool": (p["tool_bg"], p["text"]),
            "error": (p["error_bg"], p["danger"]), "confirm": (p["confirm_bg"], p["text"]), "system": (p["bg"], p["text_muted"])}


def stylesheet(p: dict, scale: float = 1.0) -> str:
    pt = round(BASE_PT * scale, 1)
    return f"""
* {{ font-family: "Segoe UI Variable", "Segoe UI", "Noto Sans", sans-serif; font-size: {pt}pt; }}
QWidget {{ background: {p['bg']}; color: {p['text']}; }}
QMainWindow, QDialog {{ background: {p['bg']}; }}
QLabel[muted="true"] {{ color: {p['text_muted']}; }}
QLabel[heading="true"] {{ font-size: {round(pt * 1.4, 1)}pt; font-weight: 600; }}
QListWidget, QTableWidget, QTextBrowser, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox {{
    background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 4px; }}
QListWidget#nav {{ background: {p['surface_alt']}; border: none; border-right: 1px solid {p['border']}; border-radius: 0; padding: 8px 0; }}
QListWidget#nav::item {{ padding: 10px 16px; border-left: 3px solid transparent; }}
QListWidget#nav::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; border-left: 3px solid {p['accent']}; }}
QListWidget::item:selected, QTableWidget::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}
QHeaderView::section {{ background: {p['surface_alt']}; color: {p['text']}; border: none; border-bottom: 1px solid {p['border']}; padding: 6px; }}
QPushButton {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; border-radius: 6px; padding: 6px 14px; min-height: 22px; }}
QPushButton:hover {{ background: {p['accent_soft']}; }}
QPushButton:disabled {{ color: {p['text_muted']}; background: {p['surface_alt']}; }}
QPushButton[primary="true"] {{ background: {p['accent']}; color: {p['accent_text']}; border: 1px solid {p['accent']}; }}
QPushButton[danger="true"] {{ background: {p['danger']}; color: {p['danger_text']}; border: 1px solid {p['danger']}; font-weight: 600; }}
QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QListWidget:focus, QTableWidget:focus, QTextBrowser:focus {{
    border: 2px solid {p['focus']}; }}
QCheckBox {{ spacing: 8px; }}
QStatusBar {{ background: {p['surface_alt']}; color: {p['text']}; }}
QToolTip {{ background: {p['surface']}; color: {p['text']}; border: 1px solid {p['border']}; }}
"""
