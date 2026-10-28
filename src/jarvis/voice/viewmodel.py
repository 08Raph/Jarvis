"""Text and lists for the Voice page, with no Qt. The window only copies these into widgets, so the wording and the grouping are testable on any machine.
Every sentence here avoids claiming more than is known: a preview is "finished" (the engine said so), never "heard"; voices exist only if an engine listed them."""
from __future__ import annotations

from jarvis.voice.engines import CATEGORY_LABELS, FEMININE, MASCULINE, UNSPECIFIED


def _of(vs: list, engine: str | None) -> list:
    return [v for v in vs if engine is None or v.get("engine") == engine]


def category_options(cat: dict, engine: str | None = None) -> list[tuple[str, str]]:
    """[(key, 'Feminine (3)')...]. The two main groups are always offered (so the choice is visible even when empty); 'not specified' only if some voice is in it.
    With `engine`, only that engine's voices are counted."""
    groups = cat.get("categories") or {}
    out = []
    for key in (FEMININE, MASCULINE, UNSPECIFIED):
        n = len(_of(groups.get(key) or [], engine))
        if key != UNSPECIFIED or n:
            out.append((key, f"{CATEGORY_LABELS[key]} ({n})"))
    return out


def voice_items(cat: dict, category: str, engine: str | None = None) -> list[tuple[str, str]]:
    """[(voice id, 'Name - English (United Kingdom)')...] for one category, in the order the engine listed them (optionally only one engine's)."""
    items = []
    for v in _of((cat.get("categories") or {}).get(category) or [], engine):
        lang = v.get("language_label") or v.get("language") or "language not stated"
        items.append((v["id"], f"{v['name']} - {lang}"))
    return items


def engine_options(cat: dict) -> list[tuple[str, str]]:
    """[(engine id, 'Kokoro (local neural voice) - ready')...] for the engine selector. Every registered engine is offered, with its state, so the reason is visible."""
    out = []
    for e in cat.get("engines") or []:
        out.append((e["id"], f"{e['title']} - " + ("ready" if e["available"] else "not ready")))
    return out


def find_voice(cat: dict, voice_id: str | None) -> dict | None:
    for vs in (cat.get("categories") or {}).values():
        for v in vs:
            if v["id"] == voice_id:
                return v
    return None


def category_of_voice(cat: dict, voice_id: str | None) -> str | None:
    v = find_voice(cat, voice_id)
    return v["category"] if v else None


def engine_title(cat: dict, engine_id: str) -> str:
    return next((e["title"] for e in cat.get("engines", []) if e["id"] == engine_id), engine_id)


def voice_details(cat: dict, voice_id: str | None) -> str:
    v = find_voice(cat, voice_id)
    if v is None:
        return "No voice selected."
    where = "runs on this PC" + ("" if not v.get("needs_internet") else "; NEEDS the internet")
    gender = v.get("gender_reported") or "not stated by the engine"
    if v.get("engine") == "kokoro":
        return "\n".join([f"Voice ID: {v['name']}", f"Engine: {engine_title(cat, v['engine'])}  -  {where}",
                           "Kokoro's voices file gives only the voice ID, so no accent, gender or quality is shown: none is reported and Jarvis does not guess it from the name."])
    lines = [f"Name: {v['name']}",
             f"Language / accent: {v.get('language_label') or 'not stated'}" + (f"  ({v['language']})" if v.get("language") else ""),
             f"Type: {CATEGORY_LABELS.get(v['category'], v['category'])}  (the engine reports: {gender})" + (f"; age group: {v['age']}" if v.get("age") else ""),
             f"Engine: {engine_title(cat, v['engine'])}  -  {where}"]
    return "\n".join(lines)


def engines_text(cat: dict) -> str:
    if not cat.get("engines"):
        return "No speech engine is registered."
    lines = []
    for e in cat["engines"]:
        state = f"{e['voice_count']} voice(s) found" if e["available"] and cat.get("loaded") else ("available" if e["available"] else "unavailable")
        why = e["error"] or ("" if e["available"] else e["note"])
        lines.append(f"{e['title']}: {state}" + (f" - {why}" if why else "") + ("" if e["local"] else " (online)") + (" - works offline" if e["local"] and not e["needs_internet"] else ""))
    return "\n".join(lines)


def empty_message(cat: dict) -> str:
    if cat.get("refreshing"):
        return "Looking for the voices installed on this PC..."
    if not cat.get("loaded"):
        return "The voice list has not been read yet."
    if cat.get("count", 0) == 0:
        if not cat.get("any_engine_available"):
            first = next((e for e in cat.get("engines", []) if not e["available"]), None)
            return "No voices are available: " + (first["note"] if first and first["note"] else "no speech engine works on this PC.") + " Jarvis will answer as text only."
        return "The speech engine reported no installed voices. Add a voice in Windows Settings > Time & language > Speech, then press Refresh voices."
    return ""


def preview_text(p: dict) -> str:
    st = p.get("state", "idle")
    msg = p.get("message") or ""
    return {"idle": "Choose a voice, then press Preview Voice.", "loading": msg or "Preparing the voice...", "generating": msg or "Generating speech...",
            "playing": msg or "Playing the sample...",
            "finished": "Finished. " + msg, "stopped": msg or "Stopped.", "error": "Could not play the sample. " + msg}.get(st, msg)


def resolution_text(cat: dict) -> str:
    r = (cat.get("resolution") or {}).get("resolution") or {}
    if not r:
        return ""
    if r.get("source") == "unavailable":
        return "Your chosen voice cannot be used right now, so replies are NOT being spoken. " + (r.get("note") or "")
    if r.get("engine") is None:
        return "No voice can speak right now. " + (r.get("note") or "")
    if r.get("voice_name"):
        base = f"Jarvis speaks with: {r['voice_name']}" + (" (this personality mode's own voice)" if r.get("source") == "persona" else " (your default voice)")
    else:
        base = "Jarvis speaks with the speech engine's default voice."
    return base + ((" " + r["note"]) if r.get("note") else "")


def persona_text(cat: dict) -> str:
    p = cat.get("persona") or {}
    if not p:
        return ""
    own = cat.get("by_persona", {}).get(p.get("id"))
    s = f"Current personality mode: {p.get('label')}. "
    s += (f"It has its own voice ({own.split(':', 1)[-1]})." if own else "It uses your default voice.")
    return s + " Switching personality never changes your voice unless you give a mode its own voice."


def apply_scope_label(cat: dict) -> str:
    p = cat.get("persona") or {}
    return f"Use this voice only for the current personality mode ({p.get('label', '?')}), not as my default"


def input_report_text(rep: dict | None, running: bool = False, error: str = "") -> str:
    if running:
        return "Checking the microphone list and the speech recogniser (nothing is recorded)..."
    if error:
        return error
    if not rep:
        return "Not checked yet. The check lists microphones and looks for the speech recogniser. It does not open the microphone."
    m, r = rep["microphone"], rep["recognizer"]
    lines = [("Ready: everything Jarvis needs appears to be present." if rep["ready"] else "Not ready:")]
    lines += [f" - {p}" for p in rep["problems"]]
    if m["library_ok"]:
        lines.append("Microphones found: " + (", ".join(d["name"] for d in m["devices"]) if m["devices"] else "none"))
    lines.append(f"Chosen microphone: {m['using']}")
    lines.append(f"Speech recogniser (Vosk): {'installed' if r['installed'] else 'not installed'}; model folder: {'looks valid' if r['model_ok'] else 'not set or not valid'}")
    lines.append(f"Wake phrase: \"{rep['wake_phrase']}\"; listening is {'ON (' + rep['mode'].replace('_', ' ') + ')' if rep['listening_enabled'] else 'OFF'}")
    lines.append(rep["note"])
    return "\n".join(lines)


def mic_choices(rep: dict | None, current: str | None) -> list[tuple[str | None, str]]:
    """[(value, label)]: the system default first, then the microphones from the last check, then the saved choice if it is not connected."""
    out: list[tuple[str | None, str]] = [(None, "System default microphone")]
    names = [d["name"] for d in ((rep or {}).get("microphone") or {}).get("devices", [])]
    out += [(n, n) for n in names]
    if current and current not in names:
        out.append((current, f"{current} (not connected)"))
    return out


MODE_LABELS = (("wake_word", "Wake phrase"), ("push_to_talk", "Push-to-talk (hold a button)"))


# ---------------- Kokoro (Stage 6.5) ----------------
def kokoro_entry(cat: dict) -> dict | None:
    return next((e for e in cat.get("engines") or [] if e["id"] == "kokoro"), None)


def kokoro_ready(cat: dict) -> bool:
    e = kokoro_entry(cat)
    return bool(e and e["available"])


def kokoro_text(cat: dict) -> str:
    e = kokoro_entry(cat)
    if e is None:
        return "Kokoro is not part of this build."
    st = e.get("status") or {}
    m = st.get("model") or {}
    lines = ["Kokoro (local neural voice): " + ("READY" if e["available"] else "NOT READY")]
    if not e["available"]:
        lines.append(" - " + e["note"])
    miss = st.get("packages_missing") or []
    lines.append("Python packages: " + ("all present" if not miss else "missing " + ", ".join(miss) + f" (install with: {st.get('install_hint', '')})"))
    state = m.get("state", "missing")
    lines.append("Model files: " + {"ready": "installed", "missing": "not installed", "incomplete": "incomplete", "damaged": "damaged"}.get(state, state)
                 + (f"  -  stored in {m['folder']}" if m.get("folder") else ""))
    for n, why in (m.get("problems") or {}).items():
        lines.append(f" - {n}: {why}")
    if state != "ready":
        lines.append(f"Download: about {m.get('approx_total', '340 MB')} from github.com (needs the internet once, only after you confirm). Afterwards Kokoro works offline.")
    if state == "ready":
        pres, absent = st.get("presets_present") or [], st.get("presets_absent") or []
        lines.append(f"Voices in the installed file: {st.get('voices_in_file', 0)}. Offered by Jarvis: " + (", ".join(pres) if pres else "none of the four presets"))
        if absent:
            lines.append("Not in the installed voices file (so not selectable): " + ", ".join(absent))
    prov = st.get("providers")
    lines.append("Acceleration: " + ("not checked yet. " if prov is None else ("ONNX Runtime offers " + (", ".join(prov) if prov else "no providers") + ". ")) + (st.get("acceleration") or ""))
    lines.append("Memory: " + ("model loaded" if st.get("loaded") else "model not loaded (it loads on first use and then stays loaded)"))
    lines.append("Licences: " + e.get("licence", ""))
    lines.append("Kokoro only speaks. It does not listen: the microphone, wake phrase and Vosk are unchanged. To go back, choose \"Windows built-in speech\" and press Use Windows speech.")
    return "\n".join(lines)


def kokoro_job_text(k: dict | None) -> str:
    k = k or {}
    if k.get("running"):
        return ("Checking the model files..." if k.get("op") == "check" else "Downloading the Kokoro model: " + (k.get("current") or "starting...")) + "  (Cancel removes the partial files.)"
    r = k.get("result") or {}
    if r:
        return ("Done. " if r.get("ok") else ("Cancelled. " if r.get("cancelled") else "Failed. ")) + (r.get("message") or "")
    return k.get("message") or ""


def kokoro_confirm_text(cat: dict) -> str:
    e = kokoro_entry(cat) or {}
    m = (e.get("status") or {}).get("model") or {}
    return ("Download the Kokoro voice model?\n\n"
            f"Size: about {m.get('approx_total', '340 MB')} (two files). Source: the kokoro-onnx project's release on github.com. "
            f"Stored in: {m.get('folder', 'your Jarvis data folder')}\n"
            "This uses the internet once, only for this download. Speech afterwards is generated on this PC, with no account, key or fee.\n\n"
            f"Licences: {m.get('model_licence', '')}. {m.get('runtime_licence', '')}\n\n"
            "No publisher checksum is available to compare, so Jarvis checks the size and file structure and records its own checksum. You can cancel at any time.")


def recovery_text(cat: dict) -> str:
    """Shown when the voice you chose cannot be used. Never says a different voice was used."""
    r = (cat.get("resolution") or {}).get("resolution") or {}
    if r.get("source") != "unavailable":
        return ""
    return ("Choose how to continue: press Install / repair to get the Kokoro model working, or press Use Windows speech to switch your default voice to Windows. "
            "Until you do, Jarvis stays silent instead of speaking with a different voice.")
