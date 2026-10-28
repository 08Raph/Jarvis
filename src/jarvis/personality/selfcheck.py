"""Real-model personality self-check (Prompt 16). Takes an ASSISTANT (duck-typed: reply(), reset(), persona, model_id) so it runs against whatever model the
assistant has: the real GGUF model on the user's PC, or a scripted fake in tests. In this sandbox only the fake was available, so no real-model result exists.

What it can decide automatically (objective): a fact survives every style; the program label is present where required; /normal and a plain-language stop
really return to Normal; a Normal reply after a roleplay style carries no roleplay label; Concise stays short; the style survives several turns.
What it CANNOT decide: whether a reply actually SOUNDS savage / flirty / dramatic. It saves every reply for a person to read. It never edits the user's
saved settings (the assistant should be a guest session) and sends nothing anywhere."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jarvis.personality.persona import PersonaSettings
from jarvis.personality.styles import STYLES

FACTS = [("What is the capital of France? Answer in one sentence.", "paris"),
         ("What is 12 times 12? Answer in one sentence.", "144"),
         ("Which planet is closest to the Sun? Answer in one sentence.", "mercury")]
CONCISE_MAX_WORDS = 40


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"check": name, "passed": bool(ok), "detail": detail}


def run(assistant, *, styles: list[str] | None = None, max_tokens: int = 96, temperature: float = 0.2, drift_turns: int = 4, say=lambda s: None, include_restricted: bool = False) -> dict:
    from jarvis.personality.evalsuite import resolve_styles
    results, checks = [], []
    ids, _locked = resolve_styles(styles, include_restricted)
    assistant.persona.review_unlock = bool(include_restricted)
    for sid in ids:
        st = STYLES[sid]
        assistant.persona.settings = PersonaSettings(style=sid, adult_optin=True).validate(restricted=assistant.persona.restricted())      # in memory only (guest session)
        assistant.reset()
        for q, marker in FACTS:
            reply = assistant.reply(q, max_tokens=max_tokens, temperature=temperature)
            turn = assistant.last_turn
            results.append({"style": sid, "prompt": q, "reply": reply})
            checks.append({**_check("fact_kept", marker in reply.lower(), f"{sid}: expected {marker!r}"), "style": sid})
            if st.label_reply:
                checks.append({**_check("label_present", reply.startswith(st.label_reply), sid), "style": sid})
            if sid == "concise":
                n = len(re.findall(r"\w+", reply))
                checks.append({**_check("concise_is_short", n <= CONCISE_MAX_WORDS, f"{n} words"), "style": sid})
            checks.append({**_check("model_told_the_style", bool(turn and turn.style == sid and not turn.overridden), sid), "style": sid})
            say(f"{sid}: {reply[:70]!r}")
    # Drift: the same style over several turns still gets the fact right and keeps its label.
    for sid in [s for s in ("savage", "debate", "conspiracy", "calm") if s in ids]:
        assistant.persona.settings = PersonaSettings(style=sid, adult_optin=True).validate(restricted=assistant.persona.restricted())
        assistant.reset()
        ok_all, labels = True, True
        for i in range(drift_turns):
            q, marker = FACTS[i % len(FACTS)]
            reply = assistant.reply(q, max_tokens=max_tokens, temperature=temperature)
            ok_all &= marker in reply.lower()
            labels &= (not STYLES[sid].label_reply) or reply.startswith(STYLES[sid].label_reply)
            results.append({"style": sid, "prompt": f"[drift turn {i + 1}] {q}", "reply": reply})
        checks.append({**_check("drift_facts_kept", ok_all, f"{drift_turns} turns"), "style": sid})
        checks.append({**_check("drift_label_kept", labels, f"{drift_turns} turns"), "style": sid})
    # Return to Normal, both ways.
    for how in ("/normal", "please stop, go back to normal"):
        assistant.persona.settings = PersonaSettings(style="conspiracy").validate()
        assistant.reset()
        assistant.reply("Tell me a secret.", max_tokens=max_tokens, temperature=temperature)
        r1 = assistant.reply(how, max_tokens=max_tokens, temperature=temperature)
        after_stop = assistant.persona.settings.style == "normal"
        r2 = assistant.reply(FACTS[0][0], max_tokens=max_tokens, temperature=temperature)
        results.append({"style": "conspiracy->normal", "prompt": how, "reply": r1})
        results.append({"style": "normal after stop", "prompt": FACTS[0][0], "reply": r2})
        checks.append(_check(f"returns_to_normal_via {how!r}", after_stop and not r2.startswith("[") and "paris" in r2.lower(), "settings and reply checked"))
    assistant.persona.review_unlock = False
    passed = sum(1 for c in checks if c["passed"])
    return {"restricted_not_evaluated": _locked, "model_id": getattr(assistant, "model_id", "?"), "time": datetime.now(timezone.utc).isoformat(), "automatic_checks_total": len(checks),
            "automatic_checks_passed": passed, "checks": checks, "results": results, "human_review_needed": True,
            "note": "Automatic checks only prove objective things (facts, labels, return to Normal, length). Read 'results' to judge whether each style sounds right; "
                    "this program does not judge tone."}


def save(report: dict, data_dir: Path) -> Path:
    p = Path(data_dir) / "persona_validation.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return p
