"""Turns what the user says into transparent memory actions. Rule-based on purpose: it is predictable, runs without a model,
and every decision is shown to the user ("Saved as memory #12 ... delete it with ...").

What it recognises: "remember ...", "forget ...", "don't do that again", corrections ("no, I meant ..."), style feedback
("keep answers short"), "use my usual style", "do it like last time", "what did we discuss / resume my X project", and
"that worked / that didn't work" for verified procedures. A temporary instruction ("for now", "this time") is kept for 24 hours
as a temporary_task; a durable one ("from now on", "always", "remember") becomes a standing memory. The same temporary request made
twice is only PROPOSED as a standing preference (pending until the user agrees). Nothing here touches the model's weights."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from jarvis.memory import secrets
from jarvis.memory.personal import PersonalMemory, MemoryError_

DURABLE = re.compile(r"\b(from now on|always|in (?:the )?future|going forward|by default|every time|i prefer|i(?:'d| would) (?:like|prefer)|"
                     r"as a rule|permanently|standing|my usual|each time|whenever)\b", re.I)
TEMPORARY = re.compile(r"\b(for now|for today|for tonight|for this (?:chat|conversation|session|task|one)|just this once|this time|"
                       r"temporarily|until i say otherwise|until tomorrow|only now)\b", re.I)
ARTIFACT = re.compile(r"\b(summary|story|poem|email|message|paragraph|sentence|version|list|description|overview|explanation|example|"
                      r"note|letter|essay|article|post|caption|title|name|report|draft|plan)\b", re.I)

# topic -> (memory key, memory text, trait, value)
TOPICS: dict[str, tuple[str, str, str | None, float | None]] = {
    "brief": ("style.length", "Prefers short, concise answers.", "verbosity", 0.2),
    "detailed": ("style.length", "Prefers detailed, thorough answers.", "verbosity", 0.8),
    "formal": ("style.formality", "Prefers a formal tone.", "formality", 0.85),
    "casual": ("style.formality", "Prefers a casual, relaxed tone.", "formality", 0.25),
    "more_humor": ("style.humor", "Enjoys light humour.", "humor", 0.7),
    "less_humor": ("style.humor", "Prefers no jokes; keep replies serious.", "humor", 0.05),
    "plain": ("style.language", "Prefers plain language without jargon.", "technical_depth", 0.2),
    "emoji": ("style.emoji", "Does not want emojis in replies.", None, None),
    "aloud": ("accessibility.voice_replies", "Prefers replies to be spoken aloud (voice settings are separate).", None, None),
    "bigtext": ("accessibility.text_size", "Prefers larger text (the app has no text-size setting yet).", None, None),
}
_TOPIC_RX: list[tuple[str, re.Pattern]] = [
    ("brief", re.compile(r"\b(short(?:er)?|brief(?:er)?|concise|succinct|to the point|less (?:wordy|verbose)|too (?:long|wordy|verbose))\b", re.I)),
    ("detailed", re.compile(r"\b(longer|more detail(?:ed|s)?|in detail|detailed|thorough|elaborate|too short|explain more)\b", re.I)),
    ("formal", re.compile(r"\b(?:be|more|so|very|too|bit more) formal\b|\bformal(?:ly| tone| style| language)\b|\bprofessional(?:ly| tone)\b", re.I)),
    ("casual", re.compile(r"\b(casual(?:ly)?|informal|relaxed|less formal|friendlier)\b", re.I)),
    ("less_humor", re.compile(r"\b(no jokes|fewer jokes|less (?:humou?r|funny)|more serious|stop joking|be serious)\b", re.I)),
    ("more_humor", re.compile(r"\b(more (?:humou?r|jokes|funny)|funnier|be funny)\b", re.I)),
    ("plain", re.compile(r"\b(plain|simple|simpler) (?:language|english|words|terms)|\bno jargon\b|\bsimpler\b", re.I)),
    ("emoji", re.compile(r"\b(no|without|stop using) emojis?\b", re.I)),
    ("aloud", re.compile(r"\bread (?:it |things |replies |answers )?(?:aloud|out loud)\b|\bspeak (?:the )?(?:answers|replies)\b", re.I)),
    ("bigtext", re.compile(r"\b(larger|bigger|large|big) (?:text|print|font)\b", re.I)),
]
_STYLE_LEAD = re.compile(r"^(?:please\s+)?(?:(?:from now on|always|in (?:the )?future|going forward|for now|this time)[, ]+)?"
                         r"(?:keep|make|be|give me|answer|reply|respond|talk|speak|use|read|no|stop)\b", re.I)
_BARE = re.compile(r"^(?:please\s+)?(?:shorter|briefer|longer|simpler|more concise|more detail|more detailed|more formal|less formal|"
                   r"more casual|funnier|no jokes|less wordy)[.!]?$", re.I)
_PREFER = re.compile(r"^(?:i prefer|i(?:'d| would) (?:like|prefer)|i like)\b", re.I)

REMEMBER = re.compile(r"^(?:please\s+)?(?:(?:hey\s+)?jarvis[,:]?\s+)?(?:remember|make a note|note that|keep in mind)"
                      r"(?:\s+(?:that|this|the following))?\s*[:,\-]?\s*(.*)$", re.I | re.S)
FORGET = re.compile(r"^(?:please\s+)?(?:forget|stop remembering|delete (?:the )?memory (?:about|of))\s+(?:that\s+|about\s+)?(.+)$", re.I)
AVOID_BARE = re.compile(r"^(?:please\s+)?(?:don'?t|do not|never)\s+(?:do|say|use|repeat|write)\s+(?:that|this|it)(?:\s+again)?[.!]?$", re.I)
_BEHAVIOUR = (r"(?:use|say|call|write|mention|add|include|repeat|show|suggest|ask|explain|answer|reply|respond|give|offer|apologi[sz]e|"
              r"interrupt|speak|bring|start|end|put|make|talk|be)")
AVOID_RULE = re.compile(r"^(?:please\s+)?(?:(?:from now on|going forward|in future)[, ]+)?(?:(?:don'?t|do not|never)\s+" + _BEHAVIOUR +
                        r"\b.+|stop\s+\w+ing\b.+)$", re.I)
CORRECTION = re.compile(r"^(?:no[,.!]\s+|nope[,.!]?\s+|actually[,]?\s+|that(?:'s| is) (?:wrong|not right|incorrect)[,.!]?\s*|"
                        r"you (?:got|have) (?:that|it) wrong[,.!]?\s*|i (?:said|meant)\s+)(.{8,})$", re.I | re.S)
USUAL = re.compile(r"\b(my usual (?:style|format|tone|way)|in my usual style|like i usually (?:do|like|want)|as usual)\b", re.I)
LAST_TIME = re.compile(r"\b(like last time|same as (?:last time|before)|the way we did (?:it )?(?:last time|before)|as we did before|do it again)\b", re.I)
RECALL = re.compile(r"^(?:what did we (?:discuss|talk about|decide|do)|where did we (?:leave off|stop)|do you remember|"
                    r"what do you (?:remember|know) about|what have (?:i|you) told you|what(?:'s| is) in (?:your|my) memory)(.*)$", re.I)
PAST_REF = re.compile(r"\b(?:as (?:i|we) (?:told|said|discussed|mentioned|agreed)(?: you)?|remember when|(?:earlier|yesterday|last (?:week|month|night|time)),? "
                      r"(?:you|we|i)\b|you (?:told|promised|suggested|said)(?: to)? (?:me )?(?:earlier|yesterday|before|last)|"
                      r"(?:our|that|the) (?:previous|earlier|last) (?:chat|conversation|session))", re.I)
RESUME = re.compile(r"^(?:please\s+)?(?:resume|pick up|continue)\b.*\bproject\b", re.I)
WORKED = re.compile(r"^(?:yes[,! ]+)?(?:that|it) (?:worked|did it|fixed it|is fixed|solved it)|^(?:perfect|great)[,! ]+that worked", re.I)
FAILED = re.compile(r"^(?:no[,! ]+)?(?:that|it) (?:didn'?t work|did not work|failed|is still broken|doesn'?t work)|^still (?:broken|failing|not working)", re.I)
YES_KEEP = re.compile(r"^(?:yes[,! ]+)?(?:please\s+)?(?:remember (?:that|it|this)|keep (?:that|it)|make (?:that|it) (?:permanent|standing)|save (?:that|it))[.!]?$", re.I)
CALL_ME = re.compile(r"^(?:please\s+)?(?:call me|my name is)\s+([A-Za-z][A-Za-z'\- ]{0,30})[.!]?$", re.I)
PROJECT_IN = re.compile(r"\b(?:for|on|in|about)\s+(?:my |the |our )?([A-Za-z0-9][A-Za-z0-9 _\-]{1,38}?)\s+project\b", re.I)


@dataclass
class Outcome:
    handled: bool = False                 # True: answer locally with `reply`, do not call the model
    reply: str = ""
    notice: str = ""                      # one short line shown to the user (what was saved / used)
    extra_context: str = ""               # additional saved context for the model on this turn only
    saved: list[int] = field(default_factory=list)
    kind: str = "pass"

    @property
    def spoken(self) -> str:
        """Short wording for voice: no command lines or ids read out beyond the memory number."""
        if self.handled and self.saved and self.kind in ("remember", "preference_saved", "correction_saved", "promoted", "temp_saved",
                                                         "procedure_saved"):
            return f"Saved to memory number {self.saved[0]}. You can review or delete it at any time."
        return self.reply


class MemoryAgent:
    def __init__(self, memory: PersonalMemory, personality=None, guest: bool = False):
        self.mem, self.personality, self.guest = memory, personality, guest
        self.awaiting: str | None = None
        self.last_candidate: int | None = None     # a temporary note or proposal the user can still promote
        self.pending_run: dict | None = None       # {"name", "steps"} offered workflow awaiting "that worked"

    # --------------------------------------------------------------- helpers
    def _where(self) -> str:
        return ("Guest session: this is kept for this session only and disappears when Jarvis closes." if self.guest else
                "It stays on this PC and is used in future chats.")

    def _saved_notice(self, row: dict, what: str, extra: str = "") -> str:
        tail = "" if self.guest else f" See or remove it: `jarvis memory list` / `jarvis memory delete {row['id']}`."
        return (f"Saved to memory #{row['id']} as {what}: \"{row['text']}\". {self._where()}{tail} "
                f"(A note the model is shown; the AI model itself is not retrained.){extra}")

    def _propose_trait(self, topic: str, reason: str) -> str:
        _, _, trait, value = TOPICS[topic]
        if not self.personality or trait is None:
            return ""
        if any(p["trait"] == trait and abs(p["value"] - value) < 1e-9 for p in self.personality.pending):
            return ""
        if abs(self.personality.profile.traits.get(trait, -1) - value) < 1e-9:
            return ""
        pid = self.personality.propose(trait, value, secrets.redact(reason))
        self.mem.log_learning("profile_adjustment", f"proposed personality change #{pid} ({trait}={value})")
        return f" I also queued a personality-setting suggestion (#{pid}); it changes nothing until you run `jarvis personality approve {pid}`."

    def _topic(self, text: str) -> str | None:
        for name, rx in _TOPIC_RX:
            if rx.search(text):
                return name
        return None

    def _is_style_feedback(self, text: str) -> str | None:
        t = text.strip()
        if len(t.split()) > 14 or "?" in t or re.search(r"\b(how|what|why|when|where|which|who)\b", t, re.I):
            return None
        topic = self._topic(t)
        if topic is None:
            return None
        if _BARE.match(t):
            return topic
        if _PREFER.match(t):
            return topic
        if _STYLE_LEAD.match(t) and not ARTIFACT.search(t):
            return topic
        return None

    def _safe_add(self, category, text, **kw):
        try:
            return self.mem.add(category, text, **kw), None
        except secrets.SecretRejected as e:
            return None, Outcome(handled=True, reply=str(e), kind="secret_refused")
        except MemoryError_ as e:
            return None, Outcome(handled=True, reply=f"I could not save that: {e}", kind="not_saved")

    # ----------------------------------------------------------------- main
    def process(self, text: str, last_assistant: str | None = None) -> Outcome:
        t = " ".join(text.split())
        if not t:
            return Outcome()

        if self.awaiting == "avoid_rule":
            self.awaiting = None
            if re.match(r"^(never mind|nevermind|cancel|forget it)\b", t, re.I):
                return Outcome(True, "Okay, I won't save anything.", kind="cancelled")
            row, err = self._safe_add("correction", f"Avoid: {t}", source="user_said", confidence=0.9, key=None)
            if err:
                return err
            return Outcome(True, self._saved_notice(row, "a standing correction"), saved=[row["id"]], kind="correction_saved")

        if YES_KEEP.match(t) and self.last_candidate:
            cand = self.mem.get(self.last_candidate)
            self.last_candidate = None
            if cand:
                if cand["status"] == "pending":
                    row = self.mem.approve(cand["id"])
                else:
                    pm_ = cand["meta"].get("promote_to", "approved_context")
                    clean = cand["meta"].get("clean_text") or cand["text"]
                    self.mem.db.execute("UPDATE memories SET expires_at=NULL, category=?, text=?, source='user_confirmed', updated_at=? WHERE id=?",
                                        (pm_, clean, self.mem._now().isoformat(), cand["id"]))
                    self.mem.db.commit()
                    self.mem.log_learning("memory_update", f"promoted #{cand['id']} to a standing memory", cand["id"])
                    row = self.mem.get(cand["id"])
                return Outcome(True, self._saved_notice(row, "a standing memory"), saved=[row["id"]], kind="promoted")

        if WORKED.match(t) or FAILED.match(t):
            if self.pending_run:
                return self._finish_run(bool(WORKED.match(t)))

        m = FORGET.match(t)
        if m:
            return self._forget(m.group(1))

        m = REMEMBER.match(t)
        if m:
            return self._remember(m.group(1).strip(), t)

        m = CALL_ME.match(t)
        if m:
            name = " ".join(m.group(1).split())[:30]
            row, err = self._safe_add("explicit_preference", f"Address the user as {name}.", key="address.name", source="user_said")
            return err or Outcome(True, self._saved_notice(row, "a standing preference"), saved=[row["id"]], kind="preference_saved")

        if AVOID_BARE.match(t):
            self.awaiting = "avoid_rule"
            return Outcome(True, "Understood. What exactly should I avoid? Tell me in one sentence (for example "
                                 "\"don't use bullet lists\") and I'll save it as a standing correction. Say \"never mind\" to skip.",
                           kind="ask_avoid")

        topic = self._is_style_feedback(t)
        if topic:
            return self._style(topic, t)

        if AVOID_RULE.match(t) and len(t.split()) <= 18 and "?" not in t:
            durable = bool(DURABLE.search(t) or re.search(r"\b(never|again|anymore|ever)\b", t, re.I)) and not TEMPORARY.search(t)
            if durable:
                row, err = self._safe_add("correction", f"Avoid: {t}", source="user_said", confidence=0.9)
                if err:
                    return err
                return Outcome(True, self._saved_notice(row, "a standing correction"), saved=[row["id"]], kind="correction_saved")

        m = CORRECTION.match(t)
        if m:
            durable = bool(DURABLE.search(t)) and not TEMPORARY.search(t)
            if durable:
                row, err = self._safe_add("correction", f"User correction: {m.group(1).strip()}", source="user_said", confidence=0.8)
                if err:
                    return err
                return Outcome(False, notice=self._saved_notice(row, "a standing correction"), saved=[row["id"]], kind="correction_saved")
            row, err = self._safe_add("temporary_task", f"User correction (for now): {m.group(1).strip()}", source="user_said",
                                      confidence=0.7, meta={"promote_to": "correction", "clean_text": f"User correction: {m.group(1).strip()}"})
            if err:
                return Outcome(False, kind="pass")        # a correction containing a secret is simply not stored; the chat goes on
            self.last_candidate = row["id"]
            return Outcome(False, notice=f"Noted as a correction for the next 24 hours (memory #{row['id']}). "
                                         "Say \"remember that\" to keep it permanently.", saved=[row["id"]], kind="correction_temp")

        if USUAL.search(t):
            prefs = self.mem.list("explicit_preference")
            if not prefs:
                return Outcome(True, "I don't have a saved style for you yet. Tell me what you like (for example "
                                     "\"from now on keep answers short and casual\") and I'll remember it.", kind="no_style")
            return Outcome(False, notice="Using your saved preferences: " + "; ".join(p["text"] for p in prefs[-6:]), kind="usual_style")

        if LAST_TIME.search(t):
            q = LAST_TIME.sub(" ", t)
            hits = self.mem.search(q, limit=3, categories=("procedure", "conversation_summary", "project", "approved_context", "skill"))
            if not hits:
                return Outcome(True, "I don't have a record of a previous time: nothing saved matches that. If you tell me how you'd "
                                     "like it done, I can remember it.", kind="no_evidence")
            ctx = "The user refers to how this was done before. Saved records (use only these, do not invent others):\n" + \
                "\n".join(f"- {h['text']}" for h in hits)
            return Outcome(False, notice="Using saved memory " + ", ".join(f"#{h['id']}" for h in hits) + ".", extra_context=ctx, kind="like_last_time")

        m = RECALL.match(t)
        if m:
            return self._recall(m.group(1).strip(" ?.!"), t)
        if RESUME.match(t):
            return self._recall("", t)

        if PAST_REF.search(t):
            hits = self.mem.search(t, limit=3)
            if hits:
                ctx = ("The user refers to something said earlier. These saved notes are the ONLY record; use them if they fit, and do "
                       "not invent anything beyond them:\n" + "\n".join(f"- {h['text']}" for h in hits))
            else:
                ctx = ("The user refers to something said or done in an earlier conversation, but NO saved memory matches. You have no "
                       "record of it. Say so plainly and ask them to restate it; do not pretend to remember or invent details.")
            return Outcome(False, extra_context=ctx, kind="past_reference")

        return Outcome()

    # ---------------------------------------------------------------- pieces
    def _remember(self, body: str, whole: str) -> Outcome:
        if not body or body.lower() in ("this", "that", "it"):
            if body and self.last_candidate and self.mem.get(self.last_candidate):
                return self.process("remember that")
            return Outcome(True, "What should I remember? Say it in a sentence, for example \"remember that I prefer metric units\".", kind="ask_what")
        temp = bool(TEMPORARY.search(body)) and not DURABLE.search(body)
        topic = self._is_style_feedback(body) or self._topic(body) if re.search(r"\b(answers?|repl(?:y|ies)|responses?|tone|style|jokes|emojis?)\b", body, re.I) else None
        pm = PROJECT_IN.search(body)
        if temp:
            clean = re.sub(r"\s+", " ", TEMPORARY.sub(" ", body)).strip(" ,.")
            clean = re.sub(r"^that\s+", "", clean, flags=re.I) or body
            row, err = self._safe_add("temporary_task", body, source="user_said",
                                      meta={"promote_to": "approved_context", "clean_text": clean})
            if err:
                return err
            self.last_candidate = row["id"]
            return Outcome(True, f"Okay, I'll keep that in mind for the next 24 hours (memory #{row['id']}). Say \"remember that\" to make it permanent.",
                           saved=[row["id"]], kind="temp_saved")
        if pm:
            row, err = self._safe_add("project", body, source="user_said", project=pm.group(1).strip())
            what = f"a note for your \"{pm.group(1).strip()}\" project"
        elif topic and topic in TOPICS:
            key, _, _, _ = TOPICS[topic]
            row, err = self._safe_add("explicit_preference", body, source="user_said", key=key)
            what = "a standing preference"
        elif DURABLE.search(body) or re.match(r"^i (?:prefer|like|want|need)\b", body, re.I):
            row, err = self._safe_add("explicit_preference", body, source="user_said")
            what = "a standing preference"
        else:
            row, err = self._safe_add("approved_context", body, source="user_said")
            what = "personal context you asked me to remember"
        if err:
            return err
        extra = self._propose_trait(topic, f"you said: {body[:80]}") if topic in TOPICS else ""
        return Outcome(True, self._saved_notice(row, what, extra), saved=[row["id"]], kind="remember")

    def _style(self, topic: str, text: str) -> Outcome:
        key, mtext, _, _ = TOPICS[topic]
        durable = bool(DURABLE.search(text)) and not TEMPORARY.search(text)
        pure = len(text.split()) <= 9
        if durable:
            row, err = self._safe_add("explicit_preference", mtext, source="user_said", key=key)
            if err:
                return err
            extra = self._propose_trait(topic, f"you said: {text[:80]}")
            note = self._saved_notice(row, "a standing preference", extra)
            return Outcome(True, note, saved=[row["id"]], kind="preference_saved") if pure else Outcome(False, notice=note, saved=[row["id"]], kind="preference_saved")
        # temporary: apply now, and only PROPOSE a standing preference after it has been asked for repeatedly
        row, err = self._safe_add("temporary_task", f"For now: {mtext}", source="user_said", confidence=0.8,
                                  meta={"promote_to": "explicit_preference", "clean_text": mtext})
        if err:
            return err
        n = self.mem.bump_signal(f"style:{topic}")
        notice = f"Applying that for now only (memory #{row['id']}, expires in 24 hours). Say \"from now on\" to make it a standing preference."
        if n >= 2:
            prop = self.mem.add("explicit_preference", mtext, source="assistant_inferred", confidence=0.6, key=key)
            self.last_candidate = prop["id"]
            self.mem.clear_signal(f"style:{topic}")
            notice = (f"You've asked for this more than once. Proposed standing preference #{prop['id']}: \"{mtext}\" — "
                      "it is NOT active until you say \"remember that\" or run `jarvis memory approve " + str(prop["id"]) + "`.")
        else:
            self.last_candidate = row["id"]
        return Outcome(False, notice=notice, saved=[row["id"]], kind="style_temp")

    def _forget(self, what: str) -> Outcome:
        hits = self.mem.search(what, limit=5, touch=False, min_score=0.34)
        if not hits:
            return Outcome(True, f"I couldn't find anything saved about \"{what}\". See everything with `jarvis memory list`.", kind="forget_none")
        if len(hits) == 1 or hits[0]["score"] >= 0.9 and hits[1]["score"] < 0.5:
            h = hits[0]
            self.mem.delete(h["id"])
            return Outcome(True, f"Deleted memory #{h['id']}: \"{h['text']}\".", kind="forgot")
        return Outcome(True, "Several memories match; tell me which to delete, or use `jarvis memory delete ID`:\n" +
                       "\n".join(f"  #{h['id']} {h['text']}" for h in hits), kind="forget_choose")

    def _recall(self, rest: str, whole: str) -> Outcome:
        pm = (re.search(r"([A-Za-z0-9][A-Za-z0-9 _\-]{1,38}?)\s+project\b", rest, re.I) or PROJECT_IN.search(whole)
              or re.search(r"(?:resume|pick up|continue)(?:\s+(?:with|on))?\s+(?:my |the |our )?([A-Za-z0-9][A-Za-z0-9 _\-]{1,38}?)\s+project\b", whole, re.I))
        if pm:
            name = re.sub(r"^(?:my|the|our|a)\s+", "", pm.group(1).strip(), flags=re.I)
            r = self.mem.recall_project(name)
            if not r["found"]:
                return Outcome(True, f"I have nothing saved about a \"{name}\" project, so I can't say where we left off. "
                                     "I only know what is in my saved memory.", kind="no_evidence")
            lines = [f"Here is what I have saved about \"{name}\":"]
            if r["latest_summary"]:
                lines.append(f"Latest summary (#{r['latest_summary']['id']}, {r['latest_summary']['created_at'][:10]}): {r['latest_summary']['text']}")
            lines += [f"- #{i['id']} ({i['category']}) {i['text']}" for i in r["items"] if i is not r["latest_summary"]][:8]
            return Outcome(True, "\n".join(lines), kind="project_recall")
        if re.search(r"\b(about me|you know|told you|your memory|my memory)\b", whole, re.I) or not rest:
            rows = [r for r in self.mem.list() if r["category"] != "conversation_summary"]
            if not rows:
                return Outcome(True, "I have nothing saved about you yet.", kind="no_evidence")
            return Outcome(True, "Here is what I have saved:\n" + "\n".join(f"- #{r['id']} ({r['category']}) {r['text']}" for r in rows[-12:]), kind="memory_list")
        hits = self.mem.search(rest, limit=5)
        if not hits:
            return Outcome(True, f"I don't have anything saved about \"{rest}\". I can't recall a past conversation I have no record of.", kind="no_evidence")
        return Outcome(True, "Here is what I have saved:\n" + "\n".join(f"- #{h['id']} ({h['category']}, {h['created_at'][:10]}) {h['text']}" for h in hits), kind="recall")

    # --------------------------------------------------------- workflows
    def offer_workflow(self, name: str, steps: list[str]) -> int:
        """Register a procedure that was just tried. It is saved for reuse ONLY after the user says it worked."""
        rid = self.mem.record_run(name, "unverified", steps=steps)
        self.pending_run = {"id": rid, "name": name, "steps": steps}
        return rid

    def _finish_run(self, worked: bool) -> Outcome:
        run = self.pending_run
        self.pending_run = None
        if not worked:
            self.mem.resolve_run(run["id"], "failure", "user_confirmed")
            return Outcome(True, f"Thanks; I recorded that \"{run['name']}\" did not work and will not save it as a procedure.", kind="run_failed")
        rid = self.mem.resolve_run(run["id"], "success", "user_confirmed")
        row = self.mem.save_procedure(run["name"], run["steps"], rid)
        return Outcome(True, f"Glad that worked. Since you confirmed it, I saved it as a reusable procedure (memory #{row['id']}). "
                             f"{self._where()}", saved=[row["id"]], kind="procedure_saved")
