"""More offline helpers for the toolbox: sound control (mute / unmute / volume), habits with streaks, a small calendar and repeating reminders, clipboard history and snippets,
flashcard quizzes, focus and meeting mode, scenes (named bundles of steps, e.g. "movie mode"), playtime, and PC alerts. Mixed into ``Toolbox``; each ``_x_rules`` method returns a
reply or None, exactly like the toolbox's own rules."""
from __future__ import annotations

import difflib
import re
import threading
from datetime import date, datetime, timedelta

from jarvis.app.hostctl import HostUnavailable

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
BUILTIN_SCENES = {"movie": ["unmute", "set volume to 60"], "work": ["start focus mode for 50 minutes"], "study": ["start focus mode for 25 minutes"],
                  "bedtime": ["set volume to 10", "what's on tomorrow"], "game": ["start gaming mode"], "morning": ["good morning"]}
HABIT_VERBS = {"drank water": "water", "had water": "water", "worked out": "workout", "did my workout": "workout", "exercised": "exercise", "meditated": "meditation", "stretched": "stretching",
               "took my vitamins": "vitamins", "went for a walk": "walk", "studied": "study", "read": "reading", "practiced": "practice", "journaled": "journal", "slept well": "sleep"}


def _T():
    from jarvis.app import toolbox
    return toolbox


def parse_moment(text: str, now: datetime) -> tuple[float, bool] | None:
    """'tomorrow at 3 pm', 'friday 9:30', 'next monday', 'on december 5 at noon', 'in 2 hours' -> (epoch, a clock time was given)."""
    t = re.sub(r"\s+", " ", text.lower()).strip(" .,!?")
    m = re.match(r"in (.+)$", t)
    if m:
        d = _T().parse_duration(m.group(1))
        return (now.timestamp() + d, True) if d else None
    hh = mm = None
    has_time = False
    if re.search(r"\bnoon\b", t):
        hh, mm, has_time = 12, 0, True
        t = re.sub(r"\bnoon\b", " ", t)
    elif re.search(r"\bmidnight\b", t):
        hh, mm, has_time = 0, 0, True
        t = re.sub(r"\bmidnight\b", " ", t)
    else:
        m = re.search(r"(?:\bat\s+)?\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", t) or re.search(r"\b(?:at\s+)?(\d{1,2}):(\d{2})()\b", t) or re.search(r"\bat\s+(\d{1,2})()()\b", t)
        if m:
            hh, mm = int(m.group(1)), int(m.group(2) or 0)
            ap = (m.group(3) or "").replace(".", "")
            if ap == "pm" and hh < 12:
                hh += 12
            if ap == "am" and hh == 12:
                hh = 0
            if hh > 23 or mm > 59:
                return None
            has_time = True
            t = (t[:m.start()] + " " + t[m.end():])
    today = now.date()
    day = None
    if re.search(r"\btomorrow\b", t):
        day = today + timedelta(days=1)
    elif re.search(r"\b(today|tonight|this evening)\b", t):
        day = today
        if hh is None and "tonight" in t or "evening" in t:
            hh, mm, has_time = (hh, mm, has_time) if hh is not None else (19, 0, False)
    else:
        m = re.search(r"\b(next |this |on )?(" + "|".join(DAYS) + r")\b", t)
        if m:
            target = DAYS.index(m.group(2))
            delta = (target - today.weekday()) % 7
            if delta == 0 and (m.group(1) or "").strip() == "next":
                delta = 7
            day = today + timedelta(days=delta)
        else:
            m = re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", t) or re.search(rf"\b{_MON}\.? \d{{1,2}}(?:st|nd|rd|th)?(?:,? \d{{4}})?\b", t) or re.search(rf"\b\d{{1,2}}(?:st|nd|rd|th)? (?:of )?{_MON}(?:,? \d{{4}})?\b", t)
            if m:
                day = _T().parse_date(m.group(0), today)
                if day is None:
                    return None
    if day is None and not has_time:
        return None
    if hh is None:
        hh, mm = 9, 0
    if day is None:
        day = today
        if datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm) <= now:
            day = today + timedelta(days=1)
    return datetime(day.year, day.month, day.day, hh, mm).timestamp(), has_time


def _streak(dates: list[str], today: date) -> int:
    ds = {date.fromisoformat(x) for x in dates}
    d = today if today in ds else today - timedelta(days=1)
    n = 0
    while d in ds:
        n += 1
        d -= timedelta(days=1)
    return n


def next_repeat(rep: dict, after: float) -> float:
    """The next time a repeating reminder is due, strictly after ``after``."""
    a = datetime.fromtimestamp(after)
    if rep["kind"] == "interval":
        return after + rep["every"]
    for add in range(0, 9):
        d = (a + timedelta(days=add)).replace(hour=rep["h"], minute=rep["m"], second=0, microsecond=0)
        if d.timestamp() > after and (rep["kind"] == "daily" or d.weekday() in rep["days"]):
            return d.timestamp()
    return after + 86400


def say_repeat(rep: dict) -> str:
    T = _T()
    if rep["kind"] == "interval":
        return "every " + T.say_duration(rep["every"])
    at = datetime(2000, 1, 1, rep["h"], rep["m"]).strftime("%I:%M %p").lstrip("0")
    if rep["kind"] == "daily":
        return f"every day at {at}"
    ds = rep["days"]
    if ds == [0, 1, 2, 3, 4]:
        return f"every weekday at {at}"
    return "every " + " and ".join(DAYS[d].title() for d in ds) + f" at {at}"


class LifeMixin:
    # ------------------------------------------------------------------ plumbing
    def _life_init(self, host=None):
        self.host = host
        self._lock = threading.RLock()
        self._clip_hist: list[str] = []
        self._quiz: dict | None = None
        self._in_scene = False
        self.step_runner = None                 # set by the controller: runs one sentence the way typing it would
        self.sensors = self._real_sensors
        self._alert_at = 0.0
        self._alert_flags: dict = {}

    def _h(self):
        if self.host is None:
            from jarvis.app.hostctl import Host
            self.host = Host()
        return self.host

    def _bg(self, fn) -> None:
        threading.Thread(target=lambda: self._safe(fn), daemon=True).start()

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ sound
    def _audio_rules(self, low: str, t: str) -> str | None:
        thing = r"(?:\s+(?:the|my))?(?:\s+(?:sound|audio|volume|speakers?|computer|pc|laptop|everything|music))?"
        mute = re.fullmatch(rf"(?:mute|silence){thing}|(?:turn|switch)(?: the| my)? (?:sound|audio|volume|speakers?) off|(?:sound|audio) off|turn off the (?:sound|audio|volume)|cut the (?:sound|audio)", low)
        unmute = re.fullmatch(rf"un-?mute{thing}|(?:turn|switch)(?: the| my)? (?:sound|audio|volume|speakers?) (?:back )?on|turn (?:on|back on) the (?:sound|audio|volume)|(?:sound|audio) (?:back )?on|"
                              r"(?:bring|put) the (?:sound|audio|volume) back(?: on)?|(?:restore|resume) (?:the )?(?:sound|audio)|bring back the (?:sound|audio)|let me hear (?:it|you)", low)
        tog = re.fullmatch(r"toggle(?: the)? (?:mute|sound|audio)|(?:mute|sound) toggle", low)
        status = re.fullmatch(r"(?:am i|is it|is the (?:sound|audio|computer|pc)|are the speakers) (?:muted|mute|on mute)|(?:is|are) (?:the )?(?:sound|audio|speakers) (?:muted|on|off)|what(?:'s| is)(?: the| my)? (?:current )?volume(?: level)?|how loud is (?:it|the (?:sound|volume))|volume(?: level)?", low)
        setv = re.fullmatch(r"(?:set|turn|put|make|change)(?: the| my)? (?:sound |audio |master )?volume (?:to|at) (\d{1,3})(?: ?(?:%|percent))?|volume (\d{1,3})(?: ?(?:%|percent))?|(?:set|put) (?:the )?(?:sound|audio) (?:to|at) (\d{1,3})(?: ?(?:%|percent))?", low)
        rel = re.fullmatch(r"(?:turn|make|put)(?: the| my| it)? (?:sound |audio )?(?:volume )?(up|down)(?: (?:by )?(\d{1,2}))?(?: (?:a bit|a little|a lot|some))?|(?:volume|sound) (up|down)(?: (?:by )?(\d{1,2}))?|(louder|quieter|softer)|(?:raise|lower|increase|decrease|reduce)(?: the| my)? volume(?: by (\d{1,2}))?", low)
        if not (mute or unmute or tog or status or setv or rel):
            return None
        h = self._h()
        try:
            if mute:
                h.set_mute(True)
                return "Muted."
            if unmute:
                h.set_mute(False)
                v = h.get_volume()
                if v == 0:
                    h.set_volume(30)
                return "Sound is back on."
            if tog:
                on = not h.get_mute()
                h.set_mute(on)
                return "Muted." if on else "Sound is back on."
            if status:
                if "volume" in low or "loud" in low or low == "volume":
                    return ("The sound is muted. " if h.get_mute() else "") + f"The volume is {h.get_volume()} percent."
                return "Yes, the sound is muted." if h.get_mute() else "No, the sound is on."
            if setv:
                n = int(next(g for g in setv.groups() if g))
                if n > 100:
                    return "The volume goes from 0 to 100."
                h.set_mute(False)
                return f"Volume set to {h.set_volume(n)} percent."
            g = rel.groups()
            word = next((x for x in g if x and not x.isdigit()), None) or ("down" if re.match(r"(?:lower|decrease|reduce)", low) else "up")
            amt = next((int(x) for x in g if x and x.isdigit()), 10)
            down = word in ("down", "quieter", "softer")
            v = max(0, min(100, h.get_volume() + (-amt if down else amt)))
            h.set_mute(False)
            return f"Volume {'down' if down else 'up'} to {h.set_volume(v)} percent."
        except HostUnavailable as e:
            return f"I couldn't change the sound: {e}"
        except (ValueError, OSError):
            return "Windows gave me an answer I couldn't read, so I left the sound alone."

    # ------------------------------------------------------------------ habits
    def _habits_rules(self, low: str, t: str) -> str | None:
        hs = self._state()["habits"]
        today = datetime.fromtimestamp(self.now()).date()
        iso = today.isoformat()
        m = re.fullmatch(r"i (?:just |already )?(" + "|".join(re.escape(v) for v in HABIT_VERBS) + r")(?: today)?", low)
        name = HABIT_VERBS[m.group(1)] if m else None
        if not name:
            m = re.fullmatch(r"(?:log|record|check off|tick off|mark)(?: my)?(?: habit)? (.+?)(?: (?:as )?(?:done|complete|completed|finished))?|habit done[: ]+(.+)|i did my (.+?) habit", low)
            if m and (re.match(r"(?:log|record) habit|check off|tick off|habit done|i did my", low) or "habit" in low or (m.group(1) and m.group(1).strip() in hs)):
                name = next(g for g in m.groups() if g).strip()
                name = re.sub(r"^(?:my|the)\s+|\s+habit$", "", name)
        if name:
            days = hs.setdefault(name, [])
            if iso in days:
                return f"You already logged {name} today. Streak: {_streak(days, today)} day{'' if _streak(days, today) == 1 else 's'}."
            days.append(iso)
            del days[:-400]
            self._save()
            n = _streak(days, today)
            return f"Logged {name}. Streak: {n} day{'' if n == 1 else 's'}." + (" Nice, a whole week!" if n == 7 else " Keep it going!" if n > 1 else "")
        m = re.fullmatch(r"(?:start|begin) tracking(?: the| my)?(?: habit)? (.+)|(?:add|new|create) habit[: ]+(.+)", low)
        if m:
            nm = re.sub(r"^(?:my|the)\s+", "", next(g for g in m.groups() if g).strip())
            if len(nm) > 40:
                return None
            hs.setdefault(nm, [])
            self._save()
            return f"Now tracking {nm}. Say “log habit {nm}” each day you do it."
        m = re.fullmatch(r"(?:stop tracking|delete habit|remove habit|forget habit)(?: the| my)? (.+)", low)
        if m:
            nm = m.group(1).strip()
            if nm in hs:
                del hs[nm]
                self._save()
                return f"Stopped tracking {nm}."
            return f"I'm not tracking {nm}."
        m = re.fullmatch(r"(?:what(?:'s| is)|how long is|tell me) (?:my )?(.+?) streak|streak for (.+)|how many days in a row (?:have i|did i) (.+)", low)
        if m:
            nm = re.sub(r"^(?:my|the)\s+", "", next(g for g in m.groups() if g).strip())
            if nm in hs:
                n = _streak(hs[nm], today)
                return f"Your {nm} streak is {n} day{'' if n == 1 else 's'} ({len(hs[nm])} in total)."
            return None
        if re.fullmatch(r"(?:my )?habits?|habit (?:summary|streaks?|report|status)|how are my habits(?: going)?|show (?:my )?habits|list (?:my )?habits", low):
            if not hs:
                return "You aren't tracking any habits yet. Say “start tracking water” to begin."
            parts = [f"{k}: {_streak(v, today)} day streak, {'done' if iso in v else 'not done'} today" for k, v in hs.items()]
            return "Your habits: " + "; ".join(parts) + "."
        return None

    # ------------------------------------------------------------------ calendar and repeating reminders
    def _split_title_when(self, rest: str) -> tuple[str, str]:
        m = re.search(r"\s(?:today|tonight|tomorrow|on\b|next\b|this\b|at\b|in\s+\d|in\s+an?\s|\d{4}-\d{1,2}-\d{1,2}|" + "|".join(DAYS) + rf"|{_MON}\b)", " " + rest)
        if not m:
            return rest.strip(), ""
        i = m.start() - 1 if m.start() > 0 else 0
        return rest[:max(i, 0)].strip(" ,-"), rest[max(i, 0):].strip()

    def _events_rules(self, low: str, t: str) -> str | None:
        st = self._state()
        now = self.now()
        nowdt = datetime.fromtimestamp(now)
        T = _T()
        m = re.fullmatch(r"(?:add|create|schedule|put|set up|book|make)(?: an?)?(?: new)?(?: calendar)? (?:event|appointment|meeting)(?: called| named| titled| for| with)? (.+)", t, re.I)
        m2 = re.fullmatch(r"(?:put|add) (.+?) (?:on|to|in|onto) (?:my )?(?:calendar|schedule)(?: (.+))?", t, re.I)
        if m or m2:
            if m:
                title, when = self._split_title_when(m.group(1))
            else:
                title, when = m2.group(1).strip(), (m2.group(2) or "")
                if not when:
                    title, when = self._split_title_when(title)
            title = re.sub(r"^(?:an?|the|my)\s+", "", re.sub(r"\s+(?:on|at|for)$", "", title)).strip()
            pm = parse_moment(when, nowdt) if when else None
            if not title or pm is None:
                return "Tell me what and when, for example: “add event dentist tomorrow at 3 pm”."
            ts, has_time = pm
            if ts < now - 60:
                return "That time has already passed."
            st["events"] = (st["events"] + [{"id": T.uuid.uuid4().hex[:6], "ts": ts, "title": title[:80], "time": has_time}])[-300:]
            if ts - 900 > now and has_time:
                self.add_timer(ts - 900, f"{title} starts in 15 minutes", "reminder")
            self._save()
            return f"Added {title} {T.say_when(ts, now) if has_time else 'on ' + datetime.fromtimestamp(ts).strftime('%A %B %d').replace(' 0', ' ')}." + (" I'll remind you 15 minutes before." if has_time and ts - 900 > now else "")
        m = re.fullmatch(r"(?:delete|remove|cancel)(?: the| my)? (?:event|appointment|meeting) (.+)", low)
        if m:
            key = m.group(1).strip()
            keep = [e for e in st["events"] if key not in e["title"].lower()]
            n = len(st["events"]) - len(keep)
            st["events"] = keep
            self._save()
            return f"Removed {n} event{'' if n == 1 else 's'}." if n else f"I couldn't find an event called {key}."
        m = re.fullmatch(r"what(?:'s| is| do i have| have i got)(?: on| happening| planned| scheduled| coming up)?(?: (?:on )?(?:my )?(?:calendar|schedule|agenda))?(?: for)? ?(today|tomorrow|tonight|this week|next week|" + "|".join(DAYS) + r"|the week|this weekend)?|"
                         r"(?:show|read|check)(?: me)?(?: my)? (?:calendar|schedule|agenda)(?: for)? ?(today|tomorrow|this week|next week|" + "|".join(DAYS) + r")?|what(?:'s| is) my next (?:event|appointment|meeting)", low)
        if m and (re.search(r"calendar|schedule|agenda|next|planned|scheduled|coming up|happening", low) or (m.group(1) and re.fullmatch(r"what(?:'s| is| do i have)(?: on)? ?(?:today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)", low))):
            if "next" in low and "week" not in low:
                up = sorted([e for e in st["events"] if e["ts"] >= now - 60], key=lambda e: e["ts"])
                return f"Your next event is {up[0]['title']} {T.say_when(up[0]['ts'], now)}." if up else "You have nothing coming up."
            which = next((g for g in m.groups() if g), None)
            lo = datetime.combine(nowdt.date(), datetime.min.time())
            if which in ("today", "tonight", None):
                rng = (lo, lo + timedelta(days=1)) if which else (lo, lo + timedelta(days=7))
            elif which == "tomorrow":
                rng = (lo + timedelta(days=1), lo + timedelta(days=2))
            elif which in DAYS:
                d = (DAYS.index(which) - nowdt.weekday()) % 7
                rng = (lo + timedelta(days=d), lo + timedelta(days=d + 1))
            elif which == "next week":
                rng = (lo + timedelta(days=7), lo + timedelta(days=14))
            elif which == "this weekend":
                d = (5 - nowdt.weekday()) % 7
                rng = (lo + timedelta(days=d), lo + timedelta(days=d + 2))
            else:
                rng = (lo, lo + timedelta(days=7))
            evs = sorted([e for e in st["events"] if rng[0].timestamp() <= e["ts"] < rng[1].timestamp()], key=lambda e: e["ts"])
            label = which or "the next 7 days"
            if not evs:
                return f"You have nothing on the calendar for {label}."
            return f"For {label} you have {len(evs)}: " + "; ".join(f"{e['title']} {T.say_when(e['ts'], now) if e.get('time', True) else 'on ' + datetime.fromtimestamp(e['ts']).strftime('%A')}" for e in evs[:10]) + "."
        # repeating reminders
        m = re.match(r"remind me (?:to |about )?(.+)$|(?:set|create|make|add)(?: a)? (?:repeating|recurring) reminder (?:to |for )?(.+)$", low)
        if m and re.search(r"\bevery\b|\bdaily\b|\bweekly\b|\beach (?:day|morning|evening|night|week)\b", low):
            body = next(g for g in m.groups() if g)
            return self._add_repeat(body, nowdt)
        if re.fullmatch(r"(?:list|show|what are)(?: all)?(?: my)? (?:repeating|recurring) reminders|(?:my )?(?:repeating|recurring) reminders", low):
            reps = st["repeats"]
            if not reps:
                return "You have no repeating reminders."
            return f"You have {len(reps)}: " + "; ".join(f"{r['label']} {say_repeat(r)}" for r in reps) + "."
        m = re.fullmatch(r"(?:cancel|stop|delete|remove|clear)(?: all)?(?: my| the)? (?:repeating|recurring) reminders?(?: (?:about|to|for) (.+))?", low)
        if m:
            key = (m.group(1) or "").strip()
            keep = [r for r in st["repeats"] if key and key not in r["label"].lower()]
            n = len(st["repeats"]) - len(keep)
            st["repeats"] = keep
            self._save()
            return f"Stopped {n} repeating reminder{'' if n == 1 else 's'}." if n else "There was no repeating reminder to stop."
        return None

    def _add_repeat(self, body: str, nowdt: datetime, scene: str | None = None) -> str | None:
        T = _T()
        mt = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b|\bat\s+(\d{1,2}):(\d{2})\b|\bat (noon|midnight)\b", body)
        h = mi = None
        rest = body
        if mt:
            if mt.group(6):
                h, mi = (12 if mt.group(6) == "noon" else 0), 0
            elif mt.group(1):
                h, mi = int(mt.group(1)), int(mt.group(2) or 0)
                ap = mt.group(3).replace(".", "")
                h = (h % 12) + (12 if ap == "pm" else 0)
            else:
                h, mi = int(mt.group(4)), int(mt.group(5))
            if h > 23 or mi > 59:
                return None
            rest = rest[:mt.start()] + " " + rest[mt.end():]
        rep: dict | None = None
        fm = re.search(r"\bevery (\d+(?:\.\d+)?|an?|other)?\s*(second|minute|min|hour|hr)s?\b", rest)
        if fm:
            n = 1.0 if not fm.group(1) or fm.group(1) in ("a", "an") else 2.0 if fm.group(1) == "other" else float(fm.group(1))
            secs = n * {"second": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}[fm.group(2)]
            if secs < 60:
                return "The shortest repeat is every minute."
            rep = {"kind": "interval", "every": secs}
        else:
            fm = re.search(r"\bevery (weekday|week day|day|morning|evening|night|afternoon|" + "|".join(DAYS) + r")s?\b|\b(daily|weekly)\b|\beach (day|morning|evening|night)\b", rest)
            if not fm:
                return None
            w = next(g for g in fm.groups() if g)
            default = {"morning": (8, 0), "evening": (18, 0), "night": (21, 0), "afternoon": (14, 0)}.get(w, (9, 0))
            if h is None:
                h, mi = default
            if w in ("weekday", "week day"):
                rep = {"kind": "weekly", "days": [0, 1, 2, 3, 4], "h": h, "m": mi}
            elif w in DAYS:
                rep = {"kind": "weekly", "days": [DAYS.index(w)], "h": h, "m": mi}
            elif w == "weekly":
                rep = {"kind": "weekly", "days": [nowdt.weekday()], "h": h, "m": mi}
            else:
                rep = {"kind": "daily", "h": h, "m": mi}
        rest = rest[:fm.start()] + " " + rest[fm.end():]
        label = re.sub(r"\s+", " ", re.sub(r"^\s*(?:to|that|about)\s+|\s+(?:to|at|on)\s*$", "", rest.strip())).strip(" ,.")
        label = re.sub(r"^(?:to|about)\s+", "", label)
        if not label:
            return "What should I remind you about? For example: “remind me every day at 8 pm to take my vitamins”."
        rep.update(id=T.uuid.uuid4().hex[:6], label=label[:80])
        if scene:
            rep["scene"] = scene
        rep["next"] = next_repeat(rep, self.now())
        st = self._state()
        st["repeats"] = (st["repeats"] + [rep])[-40:]
        self._save()
        if scene:
            return f"Okay, I'll run {scene} mode {say_repeat(rep)}. (Jarvis has to be running at that time.)"
        return f"Okay, I'll remind you to {label} {say_repeat(rep)}."

    # ------------------------------------------------------------------ clipboard and snippets
    def note_clipboard(self, text: str) -> None:
        """Called by the window whenever the clipboard changes (only when history is on). History is kept in memory only, never written to disk, and secrets are skipped."""
        with self._lock:
            if self.guest or not self._state().get("clip_on") or not text or len(text) > 2000:
                return
            try:
                from jarvis.operator import security_ops as SEC
                if SEC.find_secrets(text):
                    return
            except Exception:  # noqa: BLE001
                pass
            if self._clip_hist and self._clip_hist[-1] == text:
                return
            self._clip_hist = (self._clip_hist + [text])[-20:]

    def _clip_rules(self, low: str, t: str) -> str | None:
        st = self._state()
        if re.fullmatch(r"(?:start|turn on|enable|begin) (?:the )?clipboard history", low):
            st["clip_on"] = True
            self._save()
            return "Clipboard history is on. I keep your last 20 copies in memory only, never on disk, and I skip anything that looks like a password or key."
        if re.fullmatch(r"(?:stop|turn off|disable|end) (?:the )?clipboard history", low):
            st["clip_on"] = False
            self._clip_hist = []
            self._save()
            return "Clipboard history is off and cleared."
        if re.fullmatch(r"(?:clear|empty|wipe|delete) (?:the |my )?clipboard(?: history)?", low):
            self._clip_hist = []
            return "Cleared the clipboard history."
        if re.fullmatch(r"(?:show|read|list|what(?:'s| is) in)(?: me)?(?: my| the)? clipboard history|what did i (?:copy|copy recently|copy before)|(?:my )?clipboard history", low):
            if not st.get("clip_on"):
                return "Clipboard history is off. Say “start clipboard history” and I'll remember what you copy from now on."
            if not self._clip_hist:
                return "Nothing has been copied since history was turned on."
            items = list(reversed(self._clip_hist))[:5]
            return "Your last copies: " + " ".join(f"{i}. {x[:60].replace(chr(10), ' ')}{'...' if len(x) > 60 else ''}." for i, x in enumerate(items, 1))
        m = re.fullmatch(r"(?:copy|restore|put back|bring back|paste|get)(?: me)?(?: my)? (?:(last|previous|second|third|\d)(?:st|nd|rd|th)?)(?: one| copy| clip| item)?(?: from (?:the )?(?:clipboard )?history)?(?: (?:to|on|onto) (?:the |my )?clipboard)?", low)
        if m and (st.get("clip_on") or "history" in low or "previous" in low or "last" in low):
            idx = {"last": 1, "previous": 2, "second": 2, "third": 3}.get(m.group(1)) or int(m.group(1))
            hist = list(reversed(self._clip_hist))
            if idx > len(hist):
                return "I don't have that many copies yet."
            try:
                self._h().clip_set(hist[idx - 1])
            except HostUnavailable as e:
                return f"I couldn't use the clipboard: {e}"
            return "It's on your clipboard again. Press Control V to paste it."
        m = re.fullmatch(r"(?:save|store|remember|keep)(?: what(?:'s| is) on)?(?: this| the clipboard| what i copied| my clipboard| the copied text)(?: text)? as(?: my)? (.+?)(?: snippet)?", low)
        if m:
            name = m.group(1).strip()
            if len(name) > 40:
                return None
            try:
                txt = self._h().clip_get()
            except HostUnavailable as e:
                return f"I couldn't read the clipboard: {e}"
            if not txt.strip():
                return "The clipboard is empty. Copy something first."
            try:
                from jarvis.operator import security_ops as SEC
                if SEC.find_secrets(txt):
                    return "That looks like a password or key, so I won't save it."
            except Exception:  # noqa: BLE001
                pass
            if len(txt) > 2000:
                return "That's too long for a snippet (the limit is 2,000 characters)."
            st["snippets"][name] = txt
            self._save()
            return f"Saved your {name} snippet."
        m = re.fullmatch(r"(?:copy|put|load|get|paste|use)(?: my)? (.+?) snippet(?: (?:to|on|onto) (?:the |my )?clipboard)?|(?:copy|put) (?:my )?(.+?) (?:to|on|onto) (?:the |my )?clipboard", low)
        if m:
            name = next(g for g in m.groups() if g).strip()
            if name not in st["snippets"]:
                return None if m.group(2) else f"I don't have a snippet called {name}."
            try:
                self._h().clip_set(st["snippets"][name])
            except HostUnavailable as e:
                return f"I couldn't use the clipboard: {e}"
            return f"Your {name} snippet is on the clipboard. Press Control V to paste it."
        if re.fullmatch(r"(?:list|show|what are)(?: all)?(?: my)? snippets|my snippets", low):
            names = list(st["snippets"])
            return ("Your snippets: " + ", ".join(names) + ".") if names else "You have no snippets. Copy some text, then say “save this as my address”."
        m = re.fullmatch(r"(?:delete|remove|forget) (?:my )?(.+?) snippet|(?:delete|remove) snippet (.+)", low)
        if m:
            name = next(g for g in m.groups() if g).strip()
            if st["snippets"].pop(name, None) is None:
                return f"I don't have a snippet called {name}."
            self._save()
            return f"Deleted the {name} snippet."
        return None

    # ------------------------------------------------------------------ flashcards
    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", re.sub(r"^(?:the|a|an)\s+", "", s.lower().strip())).strip()

    def _cards_rules(self, low: str, t: str) -> str | None:
        st = self._state()
        cards = st["cards"]
        m = re.fullmatch(r"(?:add|create|make|new)(?: a| another)? (?:flash ?card|card)\s*[:,-]?\s*(.+?)\s*(?:=|\||->|\banswer is\b|\banswer:|\banswer\b)\s*(.+)", t, re.I)
        if m:
            q, a = m.group(1).strip(" ?:"), m.group(2).strip()
            if not q or not a:
                return None
            cards.append({"q": q[:200], "a": a[:200], "right": 0, "wrong": 0})
            del cards[:-500]
            self._save()
            return f"Added a card. You have {len(cards)}."
        if re.fullmatch(r"(?:how many|list|show)(?: my)? (?:flash ?)?cards(?: do i have)?|(?:my )?flash ?cards", low):
            return f"You have {len(cards)} flashcard{'' if len(cards) == 1 else 's'}." if cards else "You have no flashcards yet. Say “add flashcard capital of France = Paris”."
        if re.fullmatch(r"(?:delete|clear|remove) (?:all )?(?:my )?(?:flash ?)?cards", low):
            n = len(cards)
            st["cards"] = []
            self._quiz = None
            self._save()
            return f"Deleted {n} flashcard{'' if n == 1 else 's'}."
        if re.fullmatch(r"(?:stop|end|quit|exit|finish) (?:the )?(?:quiz|flash ?cards?)", low):
            q, self._quiz = self._quiz, None
            return f"Quiz over. You got {q['right']} of {q['asked']}." if q and q["asked"] else "Okay, no more questions."
        if re.fullmatch(r"(?:quiz me|test me|start (?:a |the )?quiz|flash ?cards?|ask me (?:a )?question|next (?:card|question)|another (?:card|question|one)|quiz me again)", low):
            if not cards:
                return "You have no flashcards yet. Say “add flashcard capital of France = Paris”."
            if self._quiz is None or low.startswith(("quiz", "test", "start")):
                self._quiz = {"cur": None, "right": 0, "asked": 0}
            return self._ask_card()
        if self._quiz and self._quiz.get("cur") is not None:
            cur = cards[self._quiz["cur"]] if self._quiz["cur"] < len(cards) else None
            if cur is None:
                self._quiz = None
                return None
            if re.fullmatch(r"(?:show|tell me|what(?:'s| is)) (?:the )?answer|i (?:don'?t|do not) know|give up|skip(?: it| this one)?|no idea|pass", low):
                cur["wrong"] += 1
                self._quiz["asked"] += 1
                self._save()
                a = cur["a"]
                self._quiz["cur"] = None
                return f"The answer is {a}. Say “next card” to continue."
            m = re.fullmatch(r"(?:the )?(?:answer|it'?s|its|is it|i think (?:it'?s|its|it is)|my answer is|maybe|probably)[: ]+(.+)", t, re.I)
            if m:
                guess = self._norm(m.group(1))
                want = self._norm(cur["a"])
                ok = bool(guess) and (guess == want or difflib.SequenceMatcher(None, guess, want).ratio() >= 0.85 or (len(want) >= 4 and want in guess.split(" ") ))
                self._quiz["asked"] += 1
                self._quiz["cur"] = None
                if ok:
                    cur["right"] += 1
                    self._quiz["right"] += 1
                    self._save()
                    return f"Correct! {self._quiz['right']} of {self._quiz['asked']} so far. Say “next card” for another."
                cur["wrong"] += 1
                self._save()
                return f"Not quite. The answer is {cur['a']}. Say “next card” to continue."
        return None

    def _ask_card(self) -> str:
        cards = self._state()["cards"]
        order = sorted(range(len(cards)), key=lambda i: (cards[i]["right"] - cards[i]["wrong"], self.rng.random()))
        pick = next((i for i in order if i != self._quiz.get("last")), order[0])
        self._quiz["cur"] = self._quiz["last"] = pick
        return f"Question: {cards[pick]['q']}?" if not cards[pick]["q"].endswith("?") else f"Question: {cards[pick]['q']}"

    # ------------------------------------------------------------------ focus and meeting mode
    def _focus_log_add(self, minutes: float) -> None:
        d = datetime.fromtimestamp(self.now()).date().isoformat()
        log = self._state()["focus_log"]
        log[d] = round(log.get(d, 0) + max(minutes, 0), 1)
        for k in sorted(log)[:-120]:
            del log[k]

    def _focus_start(self, secs: float | None, kind: str = "focus") -> str:
        T = _T()
        st = self._state()
        f = st["focus"]
        if f.get("on"):
            return f"{f['kind'].title()} mode is already on."
        f.clear()
        f.update(on=True, kind=kind, start=self.now(), muted=False)
        out = []
        if kind == "meeting":
            try:
                self._h().set_mute(True)
                f["muted"] = True
                out.append("Sound muted.")
            except HostUnavailable:
                out.append("I couldn't mute the sound.")
        if secs:
            tm = self.add_timer(self.now() + secs, "", kind)
            f["timer"] = tm["id"]
        self._save()
        head = {"focus": "Focus mode on", "meeting": "Meeting mode on"}[kind]
        tail = " Say “stop focus mode” when you're done." if kind == "focus" else " Say “end meeting mode” to bring the sound back."
        return " ".join([head + (f" for {T.say_duration(secs)}" if secs else "") + "."] + out) + tail

    def _focus_end(self, announce: bool = True) -> str:
        T = _T()
        st = self._state()
        f = st["focus"]
        if not f.get("on"):
            return "Focus mode isn't on."
        mins = (self.now() - f["start"]) / 60
        if f["kind"] == "focus":
            self._focus_log_add(mins)
        st["timers"] = [x for x in st["timers"] if x.get("id") != f.get("timer")]
        muted = f.get("muted")
        kind = f["kind"]
        st["focus"] = {}
        self._save()
        msg = f"{kind.title()} mode ended after {T.say_duration(mins * 60)}."
        if muted:
            try:
                self._h().set_mute(False)
                msg += " Sound is back on."
            except HostUnavailable:
                msg += " I couldn't turn the sound back on."
        return msg

    def _focus_rules(self, low: str, t: str) -> str | None:
        T = _T()
        m = re.fullmatch(r"(?:start|begin|turn on|enable|enter)(?: a| my)? (?:focus|study|work|deep work)(?: mode| session| time)?(?: for (.+))?|focus(?: mode)?(?: for (.+))?|(?:i need to focus|help me focus)(?: for (.+))?", low)
        if m:
            d = next((g for g in m.groups() if g), None)
            secs = T.parse_duration(d) if d else 25 * 60
            if d and not secs:
                return None
            return self._focus_start(secs, "focus")
        m = re.fullmatch(r"(?:start|begin|turn on|enable|enter)(?: a| my)? meeting(?: mode)?(?: for (.+))?|(?:i'?m|i am) (?:in|starting) a meeting", low)
        if m:
            d = m.group(1) if m.lastindex else None
            secs = T.parse_duration(d) if d else None
            return self._focus_start(secs, "meeting")
        if re.fullmatch(r"(?:stop|end|finish|turn off|exit|leave|disable)(?: my| the)? (?:focus|study|work|deep work|meeting)(?: mode| session)?|(?:i'?m|i am) (?:done|finished) (?:focusing|with (?:my )?(?:meeting|focus session))|meeting(?: is)? over", low):
            if not self._state()["focus"].get("on"):
                return "Focus mode isn't on."
            return self._focus_end()
        if re.fullmatch(r"(?:how (?:long|much)(?: time)? (?:did|have) i (?:focus|focused|worked?|studied|study)(?: for)?(?: today| this week)?)|focus (?:time|stats|statistics|report|log)|(?:my )?focus (?:today|this week)", low):
            log = self._state()["focus_log"]
            today = datetime.fromtimestamp(self.now()).date()
            td = log.get(today.isoformat(), 0)
            wk = sum(v for k, v in log.items() if (today - date.fromisoformat(k)).days < 7)
            cur = self._state()["focus"]
            extra = f" (a session is running now: {int((self.now() - cur['start']) / 60)} minutes so far)" if cur.get("on") and cur.get("kind") == "focus" else ""
            return f"You focused {T.say_duration(td * 60) if td else 'for 0 minutes'} today and {T.say_duration(wk * 60) if wk else '0 minutes'} in the last 7 days{extra}."
        return None

    def _finish_due(self, t: dict) -> str | None:
        """A focus or meeting timer came due (called by tick)."""
        if t["kind"] not in ("focus", "meeting"):
            return None
        f = self._state()["focus"]
        if not f.get("on") or f.get("timer") != t["id"]:
            return None
        f["timer"] = None
        if t["kind"] == "focus":
            mins = (self.now() - f["start"]) / 60
            self._focus_log_add(mins)
            self._state()["focus"] = {}
            self._save()
            return f"Focus time is over: {int(round(mins))} minutes logged. Take a break!"
        muted = f.get("muted")
        self._state()["focus"] = {}
        self._save()
        if muted:
            self._bg(lambda: self._h().set_mute(False))
        return "Meeting mode ended." + (" Sound is back on." if muted else "")

    # ------------------------------------------------------------------ scenes
    def _scene_steps(self, name: str) -> list[str] | None:
        user = self._state()["scenes"]
        n = re.sub(r"^(?:the|my)\s+", "", name.lower().strip())
        n = re.sub(r"\s+(?:mode|scene|routine)$", "", n)
        if n in user:
            return user[n]
        n = {"movies": "movie", "film": "movie", "cinema": "movie", "gaming": "game", "sleep": "bedtime", "night": "bedtime", "good night": "bedtime", "studying": "study", "working": "work"}.get(n, n)
        return BUILTIN_SCENES.get(n)

    def _run_scene(self, name: str, steps: list[str]) -> str:
        if self._in_scene:
            return "A scene can't start another scene."
        if self.step_runner is None:
            return "Scenes need the full Jarvis app to run their steps."
        self._in_scene = True
        try:
            out = []
            for s in steps[:12]:
                try:
                    r = self.step_runner(s)
                except Exception:  # noqa: BLE001
                    r = None
                out.append(r if r else f"(I didn't understand “{s}”.)")
        finally:
            self._in_scene = False
        return f"{name.title()} mode: " + " ".join(out)

    def _scenes_rules(self, low: str, t: str) -> str | None:
        if self._in_scene:
            return None                      # steps of a scene are ordinary sentences, never scenes
        st = self._state()
        m = re.fullmatch(r"game mode for (.+?)|(?:play|launch|start) (.+?) in game mode|(?:start|enter|activate|turn on)? ?game mode (?:and|then) (?:open|play|launch) (.+)", t, re.I)
        if m:
            g = next(x for x in m.groups() if x).strip()
            return self._run_scene("game", ["start gaming mode", f"open {g}", f"i'm playing {g}"])
        m = re.fullmatch(r"(?:create|make|save|add|define|set up|new)(?: a| my)?(?: new)? (?:scene|mode|routine)(?: called| named)? ([\w' -]{1,30}?)\s*(?:[:=]|\bthat\b|\bwhich\b|\bto\b|\bwith\b|\bdoes\b|\bruns\b)\s*(.+)", t, re.I)
        if m:
            name = re.sub(r"\s+(?:mode|scene|routine)$", "", m.group(1).strip().lower())
            steps = [s.strip(" .") for s in re.split(r",|;|\s+and then\s+|\s+then\s+", m.group(2)) if s.strip(" .")]
            if not name or not steps or len(steps) > 12:
                return "Tell me a name and up to 12 steps, for example: “create scene gaming: open discord, open steam, set volume to 50”."
            st["scenes"][name] = steps[:12]
            self._save()
            return f"Saved the {name} scene with {len(steps)} step{'' if len(steps) == 1 else 's'}. Say “{name} mode” to run it."
        if re.fullmatch(r"(?:list|show|what are)(?: all)?(?: my)? (?:scenes|modes|routines)|what (?:scenes|modes) (?:do you have|can you do)", low):
            names = sorted(set(BUILTIN_SCENES) | set(st["scenes"]))
            return "Scenes: " + ", ".join(f"{n} mode" for n in names) + ". Say “create scene NAME: step, step” to make your own."
        m = re.fullmatch(r"(?:delete|remove|forget)(?: the| my)? (?:scene|mode|routine) (.+?)|(?:delete|remove)(?: the| my)? (.+?) (?:scene|mode|routine)", low)
        if m:
            n = next(g for g in m.groups() if g).strip()
            if st["scenes"].pop(n, None) is None:
                return f"I don't have a custom scene called {n}."
            self._save()
            return f"Deleted the {n} scene."
        m = re.fullmatch(r"(?:run|start|activate|enter|turn on|switch to|begin|do)?\s*(?:the |my )?(?:scene |routine )?([\w' -]{2,30}?)(?: mode| scene| routine| time)?", low)
        if m and (re.search(r"\b(?:mode|scene|routine)\b", low) or low in ("movie time", "bedtime", "good night") or low in st["scenes"]):
            steps = self._scene_steps("movie" if low == "movie time" else "bedtime" if low == "good night" else m.group(1))
            if steps:
                return self._run_scene(re.sub(r"^(?:run|start|activate|enter|turn on|switch to|begin|do)\s+", "", m.group(1)).strip() if m.group(1) else low, steps)
        return None

    # ------------------------------------------------------------------ playtime
    def _play_rules(self, low: str, t: str) -> str | None:
        st = self._state()
        pt = st["playtime"]
        now = self.now()
        T = _T()

        def close_open():
            for s in pt:
                if s.get("end") is None:
                    s["end"] = now
                    return s
            return None
        m = re.fullmatch(r"(?:i'?m|i am|we'?re) (?:now |currently )?playing (.+)|(?:start|begin|log) playtime(?: for| on)? (.+)|(?:started|start) playing (.+)", t, re.I)
        if m:
            g = next(x for x in m.groups() if x).strip(" .!")
            if len(g) > 60 or re.search(r"\b(?:later|tomorrow|tonight|soon|today|with|at|in|on|after|before|when|if|because)\b", g, re.I):
                return None                       # "I'm playing guitar later" is a remark, not a game session
            prev = close_open()
            pt.append({"game": g, "start": now, "end": None})
            del pt[:-500]
            self._save()
            return f"Tracking your playtime for {g}. Say “I'm done playing” when you stop." + (f" (I ended your earlier session of {prev['game']}.)" if prev else "")
        if re.fullmatch(r"(?:i'?m|i am) (?:done|finished|through) playing|(?:i )?(?:stopped|quit|finished) playing|stop (?:the )?playtime(?: tracking)?|stop tracking (?:my )?playtime|end (?:my )?(?:game|gaming) session|gg", low):
            s = close_open()
            if not s:
                return "I wasn't tracking a game."
            self._save()
            return f"Logged {T.say_duration(s['end'] - s['start'])} of {s['game']}."
        m = re.fullmatch(r"how (?:long|much)(?: time)? (?:did|have) i (?:play|played|been playing|spent playing) (.+?)(?: (today|yesterday|this week|this month|in total|so far|all time|ever))?|how much (.+?) have i played(?: (today|this week|this month|in total))?", low)
        if m:
            g = next((x for x in (m.group(1), m.group(3)) if x), "").strip()
            per = (m.group(2) or m.group(4) or "in total")
            if g in ("games", "game", "anything", "today", "this week"):
                return None
            lo = self._period_start(per)
            tot = sum(((s["end"] or now) - max(s["start"], lo)) for s in pt if g.lower() in s["game"].lower() and (s["end"] or now) > lo)
            if tot <= 0:
                return f"I have no playtime for {g} {'in total' if per in ('in total', 'so far', 'all time', 'ever') else per}."
            return f"You played {g} for {T.say_duration(tot)} {'in total' if per in ('in total', 'so far', 'all time', 'ever') else per}."
        m = re.fullmatch(r"what (?:did|have) i (?:play|played)(?: (today|yesterday|this week|this month))?|playtime(?: report| stats| summary)?(?: (today|this week|this month))?|(?:my )?game ?time(?: report)?", low)
        if m:
            per = next((g for g in m.groups() if g), "this week")
            lo = self._period_start(per)
            by: dict[str, float] = {}
            for s in pt:
                end = s["end"] or now
                if end > lo:
                    by[s["game"]] = by.get(s["game"], 0) + end - max(s["start"], lo)
            if not by:
                return f"I have no playtime logged {per}. Say “I'm playing CS2” when you start a game."
            return f"Playtime {per}: " + "; ".join(f"{g} {T.say_duration(v)}" for g, v in sorted(by.items(), key=lambda kv: -kv[1])[:8]) + "."
        return None

    def _period_start(self, per: str) -> float:
        n = datetime.fromtimestamp(self.now())
        d0 = datetime.combine(n.date(), datetime.min.time())
        if per == "today":
            return d0.timestamp()
        if per == "yesterday":
            return (d0 - timedelta(days=1)).timestamp()
        if per == "this week":
            return (d0 - timedelta(days=n.weekday())).timestamp()
        if per == "this month":
            return d0.replace(day=1).timestamp()
        return 0.0

    # ------------------------------------------------------------------ PC alerts
    def _real_sensors(self) -> dict | None:
        try:
            import psutil
        except ImportError:
            return None
        out: dict = {}
        try:
            b = psutil.sensors_battery()
            if b is not None:
                out["battery"], out["plugged"] = b.percent, bool(b.power_plugged)
        except Exception:  # noqa: BLE001
            pass
        try:
            import shutil
            du = shutil.disk_usage(str(self.dir.anchor or self.dir))
            out["disk_free"] = du.free * 100.0 / du.total
            out["ram"] = psutil.virtual_memory().percent
        except Exception:  # noqa: BLE001
            pass
        return out or None

    def alerts_check(self) -> list[str]:
        """Messages for anything newly worth saying. Each alert is said once and re-armed only after the number recovers."""
        s = self.sensors()
        if not s:
            return []
        fl, out = self._alert_flags, []
        if "battery" in s:
            if not s["plugged"] and s["battery"] <= 10 and not fl.get("bat10"):
                fl["bat10"] = fl["bat20"] = True
                out.append(f"Battery is critically low: {int(s['battery'])} percent. Plug in now.")
            elif not s["plugged"] and s["battery"] <= 20 and not fl.get("bat20"):
                fl["bat20"] = True
                out.append(f"Battery is at {int(s['battery'])} percent. Plug in soon.")
            if s["plugged"] or s["battery"] > 30:
                fl["bat10"] = fl["bat20"] = False
        if "disk_free" in s:
            if s["disk_free"] < 10 and not fl.get("disk"):
                fl["disk"] = True
                out.append(f"Your main drive has only {s['disk_free']:.0f} percent free space left.")
            elif s["disk_free"] > 15:
                fl["disk"] = False
        if "ram" in s:
            if s["ram"] > 92 and not fl.get("ram"):
                fl["ram"] = True
                out.append("Memory is almost full. Closing a few programs would help.")
            elif s["ram"] < 85:
                fl["ram"] = False
        return out

    def _alerts_rules(self, low: str, t: str) -> str | None:
        st = self._state()
        if re.fullmatch(r"(?:turn on|enable|start|activate)(?: the)?(?: my)? (?:pc |computer |battery |system |laptop )?(?:alerts?|warnings?|monitoring)|alert me about (?:my )?(?:battery|pc|computer)(?: problems)?", low):
            st["alerts"] = True
            self._alert_flags = {}
            self._save()
            return "PC alerts are on. I'll speak up if the battery gets low (20 and 10 percent), the drive is nearly full, or memory is almost used up."
        if re.fullmatch(r"(?:turn off|disable|stop|deactivate)(?: the)?(?: my)? (?:pc |computer |battery |system |laptop )?(?:alerts?|warnings?|monitoring)", low):
            st["alerts"] = False
            self._save()
            return "PC alerts are off."
        if re.fullmatch(r"(?:are )?(?:pc |computer )?alerts? (?:on|enabled|status)|(?:are|is) (?:the )?(?:pc )?alerts? (?:on|enabled)", low):
            return "PC alerts are on." if st.get("alerts") else "PC alerts are off. Say “turn on PC alerts” to enable them."
        return None
