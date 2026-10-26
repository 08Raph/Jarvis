"""Screen understanding, honestly.

WHAT THIS IS: a permission-gated way to look at one window (or the screen) ONCE per approved request and report what can actually be read:
  * text found by OCR (Windows' built-in OCR, or Tesseract), with the engine, a confidence where the engine gives one, a bounding box where it gives one;
  * interface elements (buttons, boxes, menus, dialogs) reported by Windows accessibility (UI Automation) - an exact source, not a guess.
Every observation carries its source, the time it was captured and the window it came from.

WHAT THIS IS NOT: there is NO image-understanding model here (the local Qwen model is text-only). Nothing describes pictures, icons, colours, charts or
layout from pixels. OCR and accessibility are not a trained vision model and are never presented as one. Anything outside what they can read is simply
not observed, and the tools say so.

PRIVACY: nothing is captured unless you approve that request; there is no background or continuous capture; the image is held in a private temporary
folder only while OCR runs and is then deleted (it is NOT saved in the screenshots folder); nothing is sent anywhere; sign-in/password/security windows
and anti-cheat windows are refused; lines that look like secrets are withheld; the text of what was seen lives only in memory (a few captures, ten
minutes) and in the answer to the model - never in the audit log or task history."""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from jarvis.desktop import capture_guard
from jarvis.desktop import schema as S
from jarvis.desktop.backend import DesktopBackend, Unsupported
from jarvis.desktop.ocr import NO_WINDOW, OcrUnavailable
from jarvis.desktop.paths import PathRejected
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.desktop.tools import resolve_window
from jarvis.monitor.readings import now_iso

CACHE_MAX, CACHE_TTL = 5, 600.0
MAX_LINES, MAX_ELEMENTS = 400, 200
LOW_CONFIDENCE = 60.0
DIALOG_WORDS = re.compile(r"(?i)\b(error|warning|alert|confirm|are you sure|save as|open|properties|options|settings|preferences|install|update|permission|allow|failed)\b")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


# ------------------------------------------------------------------ OCR with confidence
def _uses_default_ocr(backend: DesktopBackend) -> bool:
    return type(backend).ocr is DesktopBackend.ocr and not getattr(backend, "simulated", False)


def tesseract_tsv(path: Path, timeout: float = 40) -> dict:
    tess = shutil.which("tesseract")
    if not tess:
        raise OcrUnavailable("Tesseract is not installed")
    r = subprocess.run([tess, str(path), "stdout", "tsv"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise OcrUnavailable("tesseract: " + r.stderr.strip()[:200])
    rows: dict[tuple, list] = {}
    for ln in r.stdout.splitlines()[1:]:
        f = ln.split("\t")
        if len(f) < 12 or not f[11].strip():
            continue
        try:
            conf = float(f[10])
            box = (int(f[6]), int(f[7]), int(f[6]) + int(f[8]), int(f[7]) + int(f[9]))
        except ValueError:
            continue
        rows.setdefault((f[2], f[3], f[4]), []).append((f[11].strip(), conf, box))
    lines = []
    for words in rows.values():
        confs = [c for _, c, _ in words if c >= 0]
        lines.append({"text": " ".join(w for w, _, _ in words), "rect": [min(b[0] for *_, b in words), min(b[1] for *_, b in words),
                                                                          max(b[2] for *_, b in words), max(b[3] for *_, b in words)],
                      "confidence": round(sum(confs) / len(confs), 1) if confs else None})
    return {"engine": "tesseract", "lines": lines}


def run_ocr(backend: DesktopBackend, png: Path) -> dict:
    """{'engine','lines':[{text, rect|None, confidence|None}]}. Raises OcrUnavailable when no engine can run."""
    if sys.platform != "win32" and _uses_default_ocr(backend):
        return tesseract_tsv(png)
    return backend.ocr(png)


def capabilities(backend: DesktopBackend) -> dict:
    ocr = None
    if getattr(backend, "simulated", False):
        ocr = "simulated OCR (fake)"
    elif sys.platform == "win32":
        ocr = "Windows built-in OCR (via Windows PowerShell 5.1) - not yet verified on Windows 11 by this project"
    elif shutil.which("tesseract"):
        ocr = "tesseract"
    uia = type(backend).ui_find is not DesktopBackend.ui_find
    return {"screenshot": type(backend).screenshot is not DesktopBackend.screenshot, "ocr_engine": ocr, "accessibility_elements": uia,
            "image_understanding_model": None,
            "note": "No image-understanding model is installed. Jarvis can only report text that OCR reads and elements that Windows accessibility reports; "
                    "it cannot describe pictures, icons, colours or layout from pixels."}


# ------------------------------------------------------------------ observation memory (RAM only)
def _cache(env) -> OrderedDict:
    return env.ext.setdefault("vision", OrderedDict())


def _remember(env, rec: dict) -> None:
    c = _cache(env)
    c[rec["id"]] = rec
    while len(c) > CACHE_MAX:
        c.popitem(last=False)


def _recall(env, cid: str) -> dict | None:
    rec = _cache(env).get(cid)
    if rec and time.monotonic() - rec["mono"] > CACHE_TTL:
        _cache(env).pop(cid, None)
        return None
    return rec


def recent_screen_text(env) -> list[str]:
    now = time.monotonic()
    return [t for r in _cache(env).values() if now - r["mono"] <= CACHE_TTL for t in r["texts"]]


# ------------------------------------------------------------------ capture + analysis
def _game_guard(env):
    try:
        from jarvis.monitor import gaming
        return gaming.capture_protection(env)
    except ImportError:
        return None


def _target(env, a) -> tuple[int | None, dict]:
    if a.get("scope") == "screen":
        return None, {"title": "whole screen", "process": None, "hwnd": 0}
    w = resolve_window(env, a.get("window"), a.get("hwnd") or None)
    return w["hwnd"], w


def _refuse_if_unsafe(env, hwnd):
    why = capture_guard.guard(env.backend, hwnd, _game_guard(env))
    if why:
        raise PathRejected("not captured: " + why)


def capture_and_read(env, hwnd: int | None, w: dict) -> dict:
    """One approved capture. Returns a record; raises Unsupported / OcrUnavailable-free (engine problems are reported inside the record)."""
    _refuse_if_unsafe(env, hwnd)
    at = now_iso()
    width, height, png = env.backend.screenshot(hwnd)
    limits, engine, lines, withheld = [], None, [], 0
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-look-"))
    try:
        p = tmp / "capture.png"
        p.write_bytes(png)
        try:
            res = run_ocr(env.backend, p)
            engine = res.get("engine")
            lines = [{"text": str(l.get("text", "")).strip(), "rect": l.get("rect"), "confidence": l.get("confidence")} for l in res.get("lines", []) if str(l.get("text", "")).strip()]
        except OcrUnavailable as e:
            limits.append(f"no OCR engine could run ({str(e)[:160]}); text on screen was NOT read")
        except (subprocess.SubprocessError, OSError) as e:
            limits.append(f"OCR failed ({type(e).__name__}); text on screen was NOT read")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    del png
    lines, withheld = capture_guard.withhold_secrets(lines[:MAX_LINES])
    elements, uia = [], None
    if hwnd:
        try:
            raw = env.backend.ui_find(hwnd, {})
            uia = True
            for e in raw[:MAX_ELEMENTS]:
                if e.get("is_password"):
                    continue
                elements.append({"name": str(e.get("name", ""))[:120], "control_type": e.get("control_type"), "enabled": e.get("enabled"),
                                 "offscreen": bool(e.get("offscreen")), "rect": e.get("rect")})
            els2, w2 = capture_guard.withhold_secrets([{"text": e["name"]} for e in elements])
            for e, t in zip(elements, els2):
                if t.get("withheld"):
                    e["name"] = t["text"]
        except Exception as e:  # noqa: BLE001 - Unsupported or a failing automation call
            uia = False
            limits.append("Windows accessibility (UI Automation) is not available for this window here; interface elements were NOT listed")
    else:
        limits.append("a whole-screen capture has no accessibility information; only OCR text is reported")
    if engine is None and not elements:
        limits.append("nothing could be observed: neither OCR nor accessibility produced anything")
    if any((l.get("confidence") is None) for l in lines) and engine:
        limits.append(f"{engine} does not report a confidence per line; every OCR line can be wrong")
    cid = "c" + format(int(time.time() * 1000) % 10**8, "x")
    win = {"hwnd": int(w.get("hwnd") or 0), "title": w.get("title", ""), "process": w.get("process")}
    obs = []
    for i, l in enumerate(lines, 1):
        conf = l.get("confidence")
        obs.append({"id": f"t{i}", "kind": "text", "text": l["text"], "bbox": l.get("rect"), "confidence": conf, "source": f"ocr:{engine}",
                    "certainty": "uncertain" if (conf is not None and conf < LOW_CONFIDENCE) else "observed", "withheld": bool(l.get("withheld"))})
    for i, e in enumerate(elements, 1):
        obs.append({"id": f"e{i}", "kind": "element", "text": e["name"], "control_type": e["control_type"], "enabled": e["enabled"], "offscreen": e["offscreen"],
                    "bbox": e["rect"], "confidence": None, "source": "uia", "certainty": "observed",
                    "withheld": e["name"].startswith("[withheld")})
    rec = {"id": cid, "at": at, "mono": time.monotonic(), "window": win, "width": width, "height": height, "engine": engine, "uia": uia, "limits": limits,
           "observations": obs, "withheld": withheld, "texts": [o["text"] for o in obs if not o["withheld"]],
           "norm_lines": sorted({norm(o["text"]) for o in obs if o["kind"] == "text" and not o["withheld"] and norm(o["text"])}),
           "norm_elements": sorted({norm(o["text"]) for o in obs if o["kind"] == "element" and norm(o["text"])})}
    rec["inferences"] = infer(rec)
    _remember(env, rec)
    return rec


def infer(rec: dict) -> list[dict]:
    """A few clearly labelled guesses built from observations. They are NOT observations."""
    out = []
    title = rec["window"].get("title", "")
    buttons = [o for o in rec["observations"] if o["kind"] == "element" and (o["control_type"] or "") == "button" and not o["offscreen"]]
    text_blob = " ".join(o["text"] for o in rec["observations"] if o["kind"] == "text")
    if buttons and DIALOG_WORDS.search(title + " " + text_blob[:400]):
        out.append({"statement": "this may be a dialog or message box asking for a decision", "basis": [o["id"] for o in buttons[:4]], "strength": "low"})
    if re.search(r"(?i)\b(error|failed|exception|cannot|could not|unable to)\b", text_blob):
        ids = [o["id"] for o in rec["observations"] if o["kind"] == "text" and re.search(r"(?i)\b(error|failed|exception|cannot|could not|unable to)\b", o["text"])][:4]
        out.append({"statement": "an error message may be showing", "basis": ids, "strength": "low"})
    return out


def render(rec: dict, limit: int = 120) -> str:
    w = rec["window"]
    head = (f"capture {rec['id']} at {rec['at']} of window [{w['hwnd']}] {w['title']!r} ({w['process']}); OCR engine: {rec['engine'] or 'NONE'}; "
            f"accessibility: {'yes' if rec['uia'] else 'no/unknown'}")
    lines = [head]
    for o in rec["observations"][:limit]:
        conf = "" if o["confidence"] is None else f" conf={o['confidence']}"
        extra = f" {o['control_type']}{' (offscreen)' if o['offscreen'] else ''}" if o["kind"] == "element" else ""
        flag = " UNCERTAIN" if o["certainty"] == "uncertain" else ""
        lines.append(f"{o['id']} [{o['source']}{conf}]{extra}{flag}: {o['text']}")
    if len(rec["observations"]) > limit:
        lines.append(f"... {len(rec['observations']) - limit} more observation(s) not shown")
    if rec["withheld"]:
        lines.append(f"({rec['withheld']} text line(s) withheld because they look like secrets)")
    for l in rec["limits"]:
        lines.append(f"LIMIT: {l}")
    for i in rec["inferences"]:
        lines.append(f"INFERENCE (not observed; {i['strength']} confidence; based on {', '.join(i['basis']) or 'n/a'}): {i['statement']}")
    return "\n".join(lines)


def _meta(rec: dict) -> dict:
    """What goes into the structured result and the audit trail: counts and identifiers, never the text that was on the screen."""
    return {"capture": rec["id"], "captured_at": rec["at"], "window": {"hwnd": rec["window"]["hwnd"], "process": rec["window"]["process"]},
            "ocr_engine": rec["engine"], "accessibility": rec["uia"], "text_lines": sum(o["kind"] == "text" for o in rec["observations"]),
            "elements": sum(o["kind"] == "element" for o in rec["observations"]), "uncertain": sum(o["certainty"] == "uncertain" for o in rec["observations"]),
            "withheld": rec["withheld"], "limits": rec["limits"], "image_saved": False, "image_understanding_model": None}


# ------------------------------------------------------------------ tools
TARGET = (Arg("window", "str", "part of the window title or process name (default: the window in front)", required=False, default="", max_len=200, no_newlines=True),
          Arg("hwnd", "int", "exact window handle from list_windows", required=False, default=0, min=0),
          Arg("scope", "str", "'window' (default) or 'screen' for everything on screen", required=False, default="window", choices=("window", "screen")))
PURPOSE = Arg("purpose", "str", "why you need to look (shown to the user)", max_len=200, no_newlines=True)


def _bind(a, env):
    hwnd, w = _target(env, a)
    _refuse_if_unsafe(env, hwnd)
    what = "the whole screen" if hwnd is None else f"the window [{w['hwnd']}] '{w['title']}' of {w.get('process') or 'unknown program'}"
    eff = [f"takes ONE screenshot of {what}, for: {a['purpose']}",
           "the picture is analysed on this PC (OCR and accessibility) and is NOT saved; nothing is sent anywhere",
           "lines that look like passwords, codes or card numbers are withheld; sign-in and password windows are refused"]
    return dict(a, hwnd=hwnd or 0, window="", scope="screen" if hwnd is None else "window"), eff, [str(hwnd or 0), str(w.get("process") or "")]


def _no_capture(e: Exception) -> Outcome:
    return Outcome(False, f"nothing was captured: {e}", verified=True, evidence="the backend cannot take screenshots here", data={"image_saved": False})


def h_observe(a, env) -> Outcome:
    hwnd, w = _target(env, a)
    try:
        rec = capture_and_read(env, hwnd, w)
    except Unsupported as e:
        return _no_capture(e)
    got = rec["engine"] is not None or rec["uia"]
    return Outcome(got, f"observed {rec['window']['process'] or 'the screen'}: {sum(o['kind'] == 'text' for o in rec['observations'])} text line(s), "
                        f"{sum(o['kind'] == 'element' for o in rec['observations'])} element(s)" + ("" if got else " - nothing could be read"),
                   verified=True if got else None,
                   evidence=("fresh capture analysed now" + (f" by {rec['engine']}" if rec["engine"] else "") + ("; OCR can misread" if rec["engine"] else "")) if got else "",
                   data=_meta(rec), untrusted_text=render(rec))


def _fuzzy(rec: dict, q: str):
    nq = norm(q)
    best, best_o = 0.0, None
    for o in rec["observations"]:
        if o["withheld"]:
            continue
        no = norm(o["text"])
        if not no:
            continue
        r = 1.0 if nq == no else (0.95 if nq in no and len(nq) >= 3 else difflib.SequenceMatcher(None, nq, no).ratio())
        if r > best:
            best, best_o = r, o
    return best, best_o


def h_find(a, env) -> Outcome:
    hwnd, w = _target(env, a)
    try:
        rec = capture_and_read(env, hwnd, w)
    except Unsupported as e:
        return _no_capture(e)
    if rec["engine"] is None and not rec["uia"]:
        return Outcome(False, "cannot tell whether it is on screen: neither OCR nor accessibility is available here", verified=None, evidence="", data=_meta(rec) | {"found": None})
    score, o = _fuzzy(rec, a["text"])
    data = _meta(rec) | {"query_length": len(a["text"])}
    if o and o["source"] == "uia" and score >= 0.95:
        return Outcome(True, "FOUND as an interface element (exact source: accessibility)", verified=True, evidence=f"UI Automation reports a {o['control_type']} with that name",
                       data=data | {"found": True, "certainty": "observed", "observation": o["id"], "source": "uia"}, untrusted_text=render(rec, 40))
    if o and score >= 0.95:
        sure = o["certainty"] == "observed"
        return Outcome(True, "FOUND in OCR text" + ("" if sure else " (low OCR confidence - uncertain)"), verified=True if sure else None,
                       evidence=f"{rec['engine']} read it; OCR can misread", data=data | {"found": True, "certainty": o["certainty"], "observation": o["id"], "source": o["source"], "bbox": o["bbox"]},
                       untrusted_text=render(rec, 40))
    if o and score >= 0.8:
        return Outcome(True, f"PROBABLE match only (similarity {score:.2f}) - uncertain", verified=None, evidence="a near match; OCR may have misread it, or it is different text",
                       data=data | {"found": None, "certainty": "uncertain", "similarity": round(score, 2), "observation": o["id"]}, untrusted_text=render(rec, 40))
    why = "OCR can miss text (small, stylised or low-contrast) and pictures are not read at all"
    return Outcome(True, "NOT SEEN - that is not proof it is absent: " + why, verified=None, evidence="searched the OCR text and accessibility elements of one capture",
                   data=data | {"found": False, "certainty": "not_seen"}, untrusted_text=render(rec, 40))


def h_recheck(a, env) -> Outcome:
    before = _recall(env, a["before"])
    if before is None:
        return Outcome(False, f"there is no remembered capture '{a['before']}' (captures are kept in memory for {int(CACHE_TTL // 60)} minutes and the last {CACHE_MAX} only); "
                              "take a new one with observe_screen first", verified=True, evidence="looked in the in-memory capture list")
    if not a["expect_appears"] and not a["expect_disappears"]:
        raise S.ArgError("say what you expect: expect_appears and/or expect_disappears")
    hwnd = before["window"]["hwnd"] or None
    try:
        w = {"hwnd": hwnd or 0, "title": before["window"]["title"], "process": before["window"]["process"]}
        if hwnd:
            w = resolve_window(env, None, hwnd)
    except LookupError:
        return Outcome(True, "the window that was observed before is no longer open", verified=True, evidence="the window handle is not in the list of open windows",
                       data={"window_closed": True, "capture_before": before["id"]})
    try:
        after = capture_and_read(env, hwnd, w)
    except Unsupported as e:
        return _no_capture(e)
    added = sorted(set(after["norm_lines"]) - set(before["norm_lines"]))
    removed = sorted(set(before["norm_lines"]) - set(after["norm_lines"]))
    results, verdicts = [], []
    for kind, text in (("appears", a["expect_appears"]), ("disappears", a["expect_disappears"])):
        if not text:
            continue
        sc, o = _fuzzy(after, text)
        exact_uia = bool(o and o["source"] == "uia" and sc >= 0.95)
        present_ocr = bool(o and sc >= 0.95 and o["source"] != "uia")
        if kind == "appears":
            if exact_uia or present_ocr:
                res, ver = "met: seen now", (True if (exact_uia or o["certainty"] == "observed") else None)
            elif o and sc >= 0.8:
                res, ver = "uncertain: only a near match was seen", None
            else:
                res, ver = "NOT CONFIRMED: not seen now (it may not have happened, or OCR missed it)", None
        else:
            if exact_uia or present_ocr:
                res, ver = "NOT met: it is still seen", False
            elif o and sc >= 0.8:
                res, ver = "uncertain: a near match is still seen", None
            else:
                res, ver = ("met: no longer reported by accessibility" if after["uia"] else "not seen now (OCR only: this does not prove it is gone)"), (True if after["uia"] and before["norm_elements"] and norm(text) in " ".join(before["norm_elements"]) else None)
        results.append({"expectation": kind, "result": res, "verified": ver})
        verdicts.append(ver)
    overall = False if any(v is False for v in verdicts) else True if all(v is True for v in verdicts) else None
    text = "\n".join([f"{r['expectation']}: {r['result']}" for r in results] + [f"lines added since {before['id']}: {len(added)}; removed: {len(removed)}"] +
                     [f"+ {x}" for x in added[:25]] + [f"- {x}" for x in removed[:25]] + ["", render(after, 30)])
    return Outcome(overall is not False, "; ".join(f"{r['expectation']}: {r['result']}" for r in results), verified=overall,
                   evidence=f"second capture {after['id']} compared with {before['id']} (OCR can misread; only accessibility is exact)",
                   data=_meta(after) | {"before": before["id"], "added": len(added), "removed": len(removed), "results": results}, untrusted_text=text)


def h_status(a, env) -> Outcome:
    cap = capabilities(env.backend)
    blocked = bool(env.settings.enabled.get("screen"))
    lines = [f"screenshots possible on this backend: {cap['screenshot']}", f"OCR engine: {cap['ocr_engine'] or 'NONE'}", f"accessibility elements: {cap['accessibility_elements']}",
             "image-understanding model: NONE", cap["note"], f"screen permission: {'ON' if blocked else 'off (jarvis desktop enable screen)'}"]
    return Outcome(True, "vision capabilities listed (nothing was captured)", verified=True, evidence="inspected the installed engines; no capture", data=cap, untrusted_text="\n".join(lines))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec("vision_status", "what Jarvis can and cannot see on this PC (OCR, accessibility, no image model); captures nothing", "system", S.OBSERVE, (), h_status, timeout=10),
        ToolSpec("observe_screen", "capture ONE window (or the screen) after you approve, and list the text and interface elements that can be read, each with source, time and window",
                 "screen", S.OBSERVE, TARGET + (PURPOSE,), h_observe, timeout=90, untrusted_output=True, bind=_bind),
        ToolSpec("find_on_screen", "check whether some text or a control is visible now: FOUND (exact), PROBABLE, or NOT SEEN (never a proof of absence)", "screen", S.OBSERVE,
                 TARGET + (PURPOSE, Arg("text", "str", max_len=200, no_newlines=True)), h_find, timeout=90, untrusted_output=True, bind=_bind),
        ToolSpec("recheck_screen", "after an action, capture the same window again and compare with an earlier capture: did the expected text appear / disappear?", "screen", S.OBSERVE,
                 (PURPOSE, Arg("before", "str", "capture id from observe_screen", max_len=20, no_newlines=True),
                  Arg("expect_appears", "str", required=False, default="", max_len=200, no_newlines=True),
                  Arg("expect_disappears", "str", required=False, default="", max_len=200, no_newlines=True)), h_recheck, timeout=90, untrusted_output=True,
                 bind=lambda a, env: (a, ["takes ONE new screenshot of the window observed earlier (" + a["before"] + ") to check: " + a["purpose"],
                                         "the picture is analysed on this PC and is NOT saved; nothing is sent anywhere"], [a["before"]])),
    ]
