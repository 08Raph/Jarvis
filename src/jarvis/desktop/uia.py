"""Windows UI Automation (the accessibility API screen readers use), through comtypes (free, MIT licence).
Elements are found by meaning - name, control type, automation id - not by screen coordinates. Each call searches again
from the window, so nothing depends on stale element handles. Untested on real Windows by the author."""
from __future__ import annotations

import sys
import threading

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("jarvis.desktop.uia is Windows-only")

CONTROL_TYPES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox", 50004: "edit", 50005: "hyperlink",
    50006: "image", 50007: "listitem", 50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar", 50015: "slider", 50016: "spinner", 50017: "statusbar",
    50018: "tab", 50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip", 50023: "tree", 50024: "treeitem",
    50025: "custom", 50026: "group", 50027: "thumb", 50028: "datagrid", 50029: "dataitem", 50030: "document",
    50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header", 50035: "headeritem", 50036: "table",
    50037: "titlebar", 50038: "separator",
}
P_INVOKE, P_VALUE, P_EXPANDCOLLAPSE, P_SELECTIONITEM, P_TOGGLE = 10000, 10002, 10005, 10010, 10015
TREE_DESCENDANTS = 4

_tls = threading.local()


def _client():
    """The UI Automation client for THIS thread. COM objects belong to the apartment of the thread that created them, so the
    client is never shared between threads (the inline executor runs every tool on a fresh thread)."""
    c = getattr(_tls, "client", None)
    if c is None:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as mod
        _tls.client = c = (comtypes.client.CreateObject(mod.CUIAutomation, interface=mod.IUIAutomation), mod)
    return c


def _describe(el, index: int) -> dict:
    r = el.CurrentBoundingRectangle
    try:
        is_pw = bool(el.CurrentIsPassword)
    except Exception:  # noqa: BLE001
        is_pw = False
    return {"index": index, "name": el.CurrentName or "", "control_type": CONTROL_TYPES.get(el.CurrentControlType, str(el.CurrentControlType)),
            "automation_id": el.CurrentAutomationId or "", "enabled": bool(el.CurrentIsEnabled),
            "offscreen": bool(el.CurrentIsOffscreen), "is_password": is_pw,
            "rect": [r.left, r.top, r.right, r.bottom]}


def _matches(d: dict, name: str | None, control_type: str | None, automation_id: str | None, exact: bool) -> bool:
    if control_type and d["control_type"] != control_type.lower():
        return False
    if automation_id and d["automation_id"] != automation_id:
        return False
    if name:
        n, q = d["name"].lower(), name.lower()
        if (n != q) if exact else (q not in n):
            return False
    return True


def find(hwnd: int, name=None, control_type=None, automation_id=None, exact=False, limit=200) -> list[tuple[object, dict]]:
    uia, mod = _client()
    root = uia.ElementFromHandle(hwnd)
    arr = root.FindAll(TREE_DESCENDANTS, uia.CreateTrueCondition())
    out = []
    for i in range(min(arr.Length, 5000)):
        el = arr.GetElement(i)
        try:
            d = _describe(el, len(out))
        except Exception:  # noqa: BLE001 - element vanished while enumerating
            continue
        if _matches(d, name, control_type, automation_id, exact):
            out.append((el, d))
            if len(out) >= limit:
                break
    return out


def _pattern(el, pid, iface_name):
    _, mod = _client()
    unk = el.GetCurrentPattern(pid)
    if not unk:
        return None
    return unk.QueryInterface(getattr(mod, iface_name))


def read(el) -> dict:
    d = {"name": el.CurrentName or ""}
    if el.CurrentIsPassword:
        d["value"] = None
        d["note"] = "password field: value not read"
        return d
    v = _pattern(el, P_VALUE, "IUIAutomationValuePattern")
    if v is not None:
        d["value"] = v.CurrentValue
    t = _pattern(el, P_TOGGLE, "IUIAutomationTogglePattern")
    if t is not None:
        d["toggle_state"] = {0: "off", 1: "on", 2: "indeterminate"}.get(t.CurrentToggleState)
    s = _pattern(el, P_SELECTIONITEM, "IUIAutomationSelectionItemPattern")
    if s is not None:
        d["selected"] = bool(s.CurrentIsSelected)
    e = _pattern(el, P_EXPANDCOLLAPSE, "IUIAutomationExpandCollapsePattern")
    if e is not None:
        d["expanded"] = {0: False, 1: True, 2: True, 3: None}.get(e.CurrentExpandCollapseState)
    return d


def act(el, action: str, value: str | None = None) -> str:
    """Perform a semantic action. Returns which pattern was used. Raises if the element does not support it."""
    if action == "invoke":
        p = _pattern(el, P_INVOKE, "IUIAutomationInvokePattern")
        if p is None:
            raise ValueError("this element cannot be invoked (no Invoke pattern)")
        p.Invoke()
        return "Invoke"
    if action == "toggle":
        p = _pattern(el, P_TOGGLE, "IUIAutomationTogglePattern")
        if p is None:
            raise ValueError("this element has no on/off state (no Toggle pattern)")
        p.Toggle()
        return "Toggle"
    if action == "select":
        p = _pattern(el, P_SELECTIONITEM, "IUIAutomationSelectionItemPattern")
        if p is None:
            raise ValueError("this element cannot be selected")
        p.Select()
        return "SelectionItem"
    if action in ("expand", "collapse"):
        p = _pattern(el, P_EXPANDCOLLAPSE, "IUIAutomationExpandCollapsePattern")
        if p is None:
            raise ValueError("this element cannot expand/collapse")
        (p.Expand if action == "expand" else p.Collapse)()
        return "ExpandCollapse"
    if action == "set_value":
        if el.CurrentIsPassword:
            raise PermissionError("Jarvis does not type into password fields")
        p = _pattern(el, P_VALUE, "IUIAutomationValuePattern")
        if p is None or p.CurrentIsReadOnly:
            raise ValueError("this element does not accept a value (no writable Value pattern)")
        p.SetValue(value or "")
        return "Value"
    if action == "focus":
        el.SetFocus()
        return "SetFocus"
    raise ValueError(f"unknown action {action}")
