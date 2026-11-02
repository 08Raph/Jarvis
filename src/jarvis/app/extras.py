"""Even more offline helpers for the toolbox: launch nicknames that Jarvis learns from its mistakes, spoken-word corrections, follow-up questions, a setup check, scheduled scenes,
loan / savings / date / currency calculators (rates you type in yourself), clipboard text tasks, find-a-file and what-is-using-my-disk, automatic game playtime, a PC performance
snapshot, and spoken changes to the wake word and speaking speed. Mixed into ``Toolbox``; every ``_x_rules`` method returns a reply or None like the others."""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

GAME_EXES = {"cs2.exe": "CS2", "csgo.exe": "CS:GO", "robloxplayerbeta.exe": "Roblox", "robloxstudiobeta.exe": "Roblox Studio", "valorant-win64-shipping.exe": "Valorant",
             "fortniteclient-win64-shipping.exe": "Fortnite", "eldenring.exe": "Elden Ring", "gta5.exe": "GTA V", "r5apex.exe": "Apex Legends", "dota2.exe": "Dota 2",
             "overwatch.exe": "Overwatch", "rocketleague.exe": "Rocket League", "destiny2.exe": "Destiny 2", "hogwartslegacy.exe": "Hogwarts Legacy", "cyberpunk2077.exe": "Cyberpunk 2077",
             "helldivers2.exe": "Helldivers 2", "bg3.exe": "Baldur's Gate 3", "palworld-win64-shipping.exe": "Palworld", "leagueoflegends.exe": "League of Legends",
             "minecraft.windows.exe": "Minecraft", "terraria.exe": "Terraria", "stardewvalley.exe": "Stardew Valley", "witcher3.exe": "The Witcher 3", "rainbowsix.exe": "Rainbow Six Siege"}
KINDS = {"pdf": (".pdf",), "image": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"), "picture": (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"), "photo": (".jpg", ".jpeg", ".png", ".heic"),
         "video": (".mp4", ".mkv", ".mov", ".avi", ".webm"), "music": (".mp3", ".wav", ".flac", ".m4a"), "song": (".mp3", ".wav", ".flac", ".m4a"), "document": (".docx", ".doc", ".txt", ".odt", ".pdf"),
         "word": (".docx", ".doc"), "excel": (".xlsx", ".xls", ".csv"), "spreadsheet": (".xlsx", ".xls", ".csv", ".ods"), "powerpoint": (".pptx", ".ppt"), "presentation": (".pptx", ".ppt"),
         "zip": (".zip", ".7z", ".rar"), "text": (".txt", ".md"), "installer": (".exe", ".msi")}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "appdata", "$recycle.bin", "windows"}


def _money(x: float) -> str:
    return f"{x:,.2f}"


class ExtraMixin:
    def _extras_init(self):
        self.probes: dict = {}                 # name -> callable returning (ok, detail); the controller adds the ones that need the app (microphone, AI model, app list)
        self.actions: dict = {}                # name -> callable: "wake" (phrase), "rate_get", "rate_set" (float)
        self.roots: list[Path] | None = None   # folders searched for files (default: the usual folders under the home folder)
        self.procs = self._real_procs          # -> set of lower-case program names that are running
        self.last_fail: tuple[str, float] | None = None
        self._due_scenes: list[str] = []
        self._ctx: tuple[str, float] | None = None
        self._auto_at = 0.0

    # ------------------------------------------------------------------ nicknames (learning from failed launches) and word corrections
    def note_launch_failure(self, target: str) -> None:
        self.last_fail = (target, self.now())

    @staticmethod
    def _sub_words(text: str, mapping: dict[str, str]) -> str:
        for k in sorted(mapping, key=len, reverse=True):
            text = re.sub(rf"(?<![\w']){re.escape(k)}(?![\w'])", mapping[k], text, flags=re.I)
        return text

    def apply_aliases(self, text: str) -> str:
        """'open cs 2' -> 'open Counter-Strike 2' when you taught Jarvis that nickname. Used for launch commands only."""
        al = self._state()["aliases"]
        return self._sub_words(text, al) if al and re.match(r"\s*(?:please\s+)?(?:open|launch|start|run|play|go to)\b", text, re.I) else text

    def apply_corrections(self, text: str) -> str:
        """Words the microphone keeps mishearing, replaced with what you taught Jarvis. Used for spoken input only."""
        c = self._state()["corrections"]
        return self._sub_words(text, c) if c else text

    def _alias_rules(self, low: str, t: str) -> str | None:
        st = self._state()
        al, co = st["aliases"], st["corrections"]
        if self.last_fail and self.now() - self.last_fail[1] < 300:
            m = re.fullmatch(r"(?:no,? )?(?:it'?s|it is|i meant|i mean|try|the name is|it'?s called|it is called|its called) (?:the )?(.{2,60})", t, re.I)
            if m and self.step_runner is not None:
                bad, new = self.last_fail[0], m.group(1).strip(" .")
                self.last_fail = None
                r = self.step_runner(f"open {new}")
                if r and not r.startswith("I couldn't find"):
                    al[bad.lower()] = new
                    self._save()
                    return f"Got it, “{bad}” now means {new}. {r}"
                self.last_fail = (bad, self.now())
                return r or f"I still couldn't find {new}."
        m = (re.fullmatch(r"(?:when i say|if i say) (.{2,40}?),? (?:then )?(?:open|launch|start) (.{2,60})", t, re.I)
             or re.fullmatch(r"(?:remember (?:that )?)(.{2,40}?) (?:means|should open|opens) (?:open |launch )?(.{2,60})", t, re.I))
        if m and len(m.group(1).split()) <= 3:
            k, v = m.group(1).strip(" ,"), m.group(2).strip(" .")
            al[k.lower()] = v
            self._save()
            return f"Okay, “{k}” now means {v}. Say “open {k}” to try it."
        m = re.fullmatch(r"(?:when you hear|if you hear|when i say|if i say) (.{2,40}?),? (?:it means|i mean|means|treat it as|replace it with|write) (.{1,60})", t, re.I)
        if m:
            co[m.group(1).strip(" ,").lower()] = m.group(2).strip(" .")
            self._save()
            return f"Okay, when I hear “{m.group(1).strip()}” I'll treat it as “{m.group(2).strip()}”."
        if re.fullmatch(r"(?:list|show|what are)(?: all)?(?: my)? (?:nicknames|aliases|corrections|word corrections)|(?:my )?(?:nicknames|aliases|corrections)", low):
            parts = [f"{k} means {v}" for k, v in {**al, **co}.items()]
            return ("You taught me: " + "; ".join(parts[:15]) + ".") if parts else "You haven't taught me any nicknames or corrections yet."
        m = re.fullmatch(r"(?:forget|delete|remove) (?:the )?(?:nickname|alias|correction) (?:for )?(.+)", low)
        if m:
            k = m.group(1).strip()
            if al.pop(k, None) is None and co.pop(k, None) is None:
                return f"I don't have a nickname or correction for {k}."
            self._save()
            return f"Forgot {k}."
        return None

    # ------------------------------------------------------------------ follow-up questions ("and tomorrow?")
    def _followup(self, low: str) -> str | None:
        m = re.fullmatch(r"(?:and|what about|how about|and what about)\s+(?:for |on |in |until |till |the )?(.+)", low)
        if not m or not self._ctx or self.now() - self._ctx[1] > 600:
            return None
        new, last = m.group(1).strip(), self._ctx[0]
        cands = []
        for pat in (r"(.*\btime in )(.+)", r"(.*\bdays? (?:until|till|to) )(.+)", r"(.*\bdid i play )(.+?)((?: this week| today| this month| yesterday| in total)?)$", r"(.*\bmy )(.+?)( streak)$",
                    r"(.*\bwhat(?:'s| is| do i have)(?: on)? (?:for )?)(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|this week|next week)()$",
                    r"(.*\bwhat day (?:is|was) )(.+)", r"(.*\bis )(\d+)( prime)$", r"(.*\bsquare root of )(\d+)()$"):
            mm = re.fullmatch(pat, last)
            if mm:
                cands.append(mm.group(1) + new + (mm.group(3) if mm.lastindex and mm.lastindex >= 3 else ""))
        for c in cands:
            r = self.handle(c, _follow=False)
            if r:
                return r
        return None

    # ------------------------------------------------------------------ setup check
    def _default_probes(self) -> dict:
        def audio():
            h = self._h()
            return True, f"volume {h.get_volume()} percent, {'muted' if h.get_mute() else 'sound on'}"

        def clip():
            self._h().clip_get()
            return True, "can read the clipboard"

        def timers():
            t = self.add_timer(self.now() + 3600, "selfcheck", "timer")
            self._state()["timers"] = [x for x in self._state()["timers"] if x["id"] != t["id"]]
            self._save()
            return True, "timers work and are saved"

        def calc():
            ok = self.handle("what is 6 times 7") == "42."
            return ok, "maths works" if ok else "maths gave a wrong answer"

        def psu():
            import psutil  # noqa: F401
            return True, "system readings available"
        return {"Sound control": audio, "Clipboard": clip, "Timers": timers, "Maths": calc, "System readings (psutil)": psu, **self.probes}

    def _setup_rules(self, low: str, t: str) -> str | None:
        if not re.fullmatch(r"(?:test|check|diagnose|self[- ]?test|self[- ]?check) (?:jarvis|yourself|my setup|the setup|everything|your setup|your abilities|all)|(?:check|test) (?:that )?(?:everything|it all)(?: works)?|run (?:a |the )?(?:setup|self|system)[- ]?(?:check|test)|setup check|is everything (?:working|ok|okay)", low):
            return None
        bad, ok_n, total = [], 0, 0
        for name, fn in self._default_probes().items():
            total += 1
            try:
                ok, detail = fn()
            except ImportError:
                ok, detail = False, "not installed on this PC"
            except Exception as e:  # noqa: BLE001 - a probe must never break the check
                ok, detail = False, str(e)[:100] or type(e).__name__
            if ok:
                ok_n += 1
            else:
                bad.append(f"{name}: {detail}")
        if not bad:
            return f"Setup check: all {total} checks passed."
        return f"Setup check: {ok_n} of {total} passed. Problems: " + "; ".join(bad) + "."

    # ------------------------------------------------------------------ calculators
    def _calc_rules(self, low: str, t: str) -> str | None:
        num = r"\$?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million|m)?"

        def val(g, suf):
            v = float(g.replace(",", ""))
            return v * (1000 if suf in ("k", "thousand") else 1e6 if suf in ("million", "m") else 1)
        m = re.fullmatch(rf"(?:what(?:'s| is) )?(?:the )?(?:monthly )?(?:loan |mortgage )?payment (?:on|for) (?:a |an )?{num}(?: loan| mortgage)?,? (?:at|with) (\d+(?:\.\d+)?)\s*(?:%|percent)(?: interest)?,? (?:for|over) (\d+(?:\.\d+)?) (years?|months?)", low)
        if m:
            P, r, n = val(m.group(1), m.group(2)), float(m.group(3)) / 100 / 12, int(round(float(m.group(4)) * (12 if m.group(5).startswith("year") else 1)))
            if n < 1 or n > 1200 or P <= 0:
                return None
            pay = P / n if r == 0 else P * r / (1 - (1 + r) ** -n)
            return f"The payment is {_money(pay)} a month. Over {n} months you pay {_money(pay * n)} in total, which is {_money(pay * n - P)} of interest."
        m = re.fullmatch(rf"(?:how much (?:will|would) i have )?(?:if i|when i) (?:save|invest|put away|deposit) {num} (?:a|per|every|each) month,? (?:at|with) (\d+(?:\.\d+)?)\s*(?:%|percent)(?: interest| return)?,? (?:for|over) (\d+(?:\.\d+)?) years?", low)
        if m:
            pm, r, n = val(m.group(1), m.group(2)), float(m.group(3)) / 100 / 12, int(round(float(m.group(4)) * 12))
            if n < 1 or n > 1200:
                return None
            fv = pm * n if r == 0 else pm * (((1 + r) ** n - 1) / r)
            return f"After {m.group(4)} years you'd have about {_money(fv)}, of which {_money(pm * n)} is what you put in."
        m = re.fullmatch(rf"(?:what(?:'s| is) )?(?:the )?(?:compound interest|growth|future value) (?:on|of) {num},? (?:at|with) (\d+(?:\.\d+)?)\s*(?:%|percent)(?: interest)?,? (?:for|over) (\d+(?:\.\d+)?) years?|how much (?:will|would) {num} (?:be|become|grow to) (?:at|with) (\d+(?:\.\d+)?)\s*(?:%|percent),? (?:after|in|for) (\d+(?:\.\d+)?) years?", low)
        if m:
            g = [x for x in m.groups()]
            a, suf, rate, yrs = (g[0], g[1], g[2], g[3]) if g[0] else (g[4], g[5], g[6], g[7])
            P, y = val(a, suf), float(yrs)
            if y > 100:
                return None
            fv = P * (1 + float(rate) / 100) ** y
            return f"{_money(P)} at {rate} percent for {yrs} years grows to about {_money(fv)} (compounded yearly)."
        m = re.fullmatch(r"(?:how many )?days? (?:are there )?(?:between|from) (.+?) (?:and|to) (.+)", low)
        if m:
            today = datetime.fromtimestamp(self.now()).date()
            from jarvis.app import toolbox as TB
            a, b = TB.parse_date(m.group(1), today), TB.parse_date(m.group(2), today)
            if a and b:
                n = abs((b - a).days)
                return f"{n:,} day{'' if n == 1 else 's'} ({n // 7} weeks and {n % 7} days)."
            return None
        rates = self._state()["rates"]
        m = re.fullmatch(r"(?:set|save|store|update)(?: the)? (?:exchange )?rate ([a-z]{3}) (?:to|in|for|per) ([a-z]{3})(?: to| at| is| =)? (\d+(?:\.\d+)?)|1 ([a-z]{3}) (?:is|=|equals) (\d+(?:\.\d+)?) ([a-z]{3})", low)
        if m:
            g = m.groups()
            a, b, r = (g[0], g[1], float(g[2])) if g[0] else (g[3], g[5], float(g[4]))
            if r <= 0:
                return None
            rates[f"{a}>{b}"] = r
            self._save()
            return f"Saved: 1 {a.upper()} = {r:g} {b.upper()}. Rates are only what you tell me, so update them when they change."
        m = re.fullmatch(r"(?:convert |change |what(?:'s| is) )?(\d+(?:\.\d+)?)\s*([a-z]{3}) (?:to|in|into|as) ([a-z]{3})", low)
        if m:
            amt, a, b = float(m.group(1)), m.group(2), m.group(3)
            if a == b:
                return None
            r = rates.get(f"{a}>{b}") or (1 / rates[f"{b}>{a}"] if rates.get(f"{b}>{a}") else None)
            if r is None:
                if a in {x.split(">")[0] for x in rates} | {x.split(">")[1] for x in rates} or a in ("usd", "eur", "gbp", "cad", "aud", "jpy", "inr", "mxn", "chf", "cny"):
                    return f"I don't have a rate for {a.upper()} to {b.upper()}. Tell me one, for example “set rate {a} to {b} 1.35”; I don't look rates up because I work offline."
                return None
            return f"{amt:,.2f} {a.upper()} is about {amt * r:,.2f} {b.upper()} (at your saved rate of {r:g})."
        return None

    # ------------------------------------------------------------------ clipboard text tasks (the model does the writing)
    def text_task(self, text: str) -> str | None:
        """For 'summarize my clipboard', 'fix the grammar of what I copied', 'translate my clipboard to French': the instruction for the language model, or None."""
        t = re.sub(r"\s+", " ", text.strip().strip("?.! ")).lower()
        t = re.sub(r"^(?:please |can you |could you |hey jarvis,? |jarvis,? )+", "", t)
        src = r"(?:my clipboard|the clipboard|what i copied|what i just copied|the copied text|the text i copied|this text|my copied text)"
        m = re.fullmatch(rf"(?:summari[sz]e|tldr|sum up|give me the gist of|shorten) {src}", t)
        if m:
            return "Summarize the text below in a few clear sentences."
        if re.fullmatch(rf"(?:fix|correct|proofread|check) (?:the )?(?:grammar|spelling|writing|grammar and spelling|typos)(?: in| of| for)? {src}|(?:proofread|fix) {src}", t):
            return "Fix the grammar and spelling of the text below. Return the corrected text, then a one-line list of what you changed."
        m = re.fullmatch(rf"translate {src}(?: (?:in)?to ([a-z]+))?", t)
        if m:
            return f"Translate the text below into {m.group(1).title() if m.group(1) else 'English'}."
        if re.fullmatch(rf"(?:rewrite|reword|rephrase|make) {src}(?: (?:more )?(?:clearly|simpler|professional|friendly|polite|shorter))?", t):
            tone = re.search(r"(clearly|simpler|professional|friendly|polite|shorter)$", t)
            return f"Rewrite the text below{(' to be ' + tone.group(1)) if tone else ' more clearly'}."
        if re.fullmatch(rf"explain {src}", t):
            return "Explain the text below in simple words."
        return None

    # ------------------------------------------------------------------ files and disk
    def _roots(self) -> list[Path]:
        if self.roots is not None:
            return self.roots
        h = Path.home()
        return [h / n for n in ("Downloads", "Desktop", "Documents", "Pictures", "Videos", "Music") if (h / n).is_dir()]

    def _walk(self, budget: float = 2.0, depth: int = 4):
        end = time.monotonic() + budget
        n = 0
        for root in self._roots():
            stack = [(root, 0)]
            while stack:
                d, dep = stack.pop()
                try:
                    it = list(os.scandir(d))
                except OSError:
                    continue
                for e in it:
                    n += 1
                    if n > 40000 or time.monotonic() > end:
                        return
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if dep < depth and e.name.lower() not in SKIP_DIRS and not e.name.startswith("."):
                                stack.append((e.path, dep + 1))
                        elif not e.name.startswith("."):
                            yield e
                    except OSError:
                        continue

    def _files_rules(self, low: str, t: str) -> str | None:
        if re.fullmatch(r"(?:what(?:'s| is)|what are) (?:taking up|using|eating|hogging|filling)(?: up)?(?: all)?(?: of)?(?: my)?(?: disk| hard drive| drive| pc)? (?:space|storage)|where(?:'s| is) my (?:disk |storage |drive )?space (?:going|gone)|what(?:'s| is) using (?:up )?my (?:disk|storage)|biggest (?:folders|files)", low):
            sizes = []
            end = time.monotonic() + 4
            for root in self._roots():
                tot = 0
                for dp, dn, fn in os.walk(root):
                    dn[:] = [d for d in dn if d.lower() not in SKIP_DIRS]
                    for f in fn:
                        try:
                            tot += os.path.getsize(os.path.join(dp, f))
                        except OSError:
                            pass
                    if time.monotonic() > end:
                        break
                sizes.append((root.name, tot))
            sizes = [s for s in sorted(sizes, key=lambda x: -x[1]) if s[1] > 0][:5]
            if not sizes:
                return "I couldn't find anything large in your usual folders."
            from jarvis.operator.core import human_size
            return "Biggest folders: " + "; ".join(f"{n} {human_size(s)}" for n, s in sizes) + ". (I looked at Downloads, Desktop, Documents, Pictures, Videos and Music.)"
        m = re.match(r"(?:find|show me|show|search for|look for|where(?:'s| is)|locate)\s+(.+)$", low)
        if not m:
            return None
        body = m.group(1)
        kind = next((k for k in KINDS if re.search(rf"\b{k}s?\b", body)), None)
        when = next((w for w in ("today", "yesterday", "this week", "last week", "this month", "recently", "last month") if w in body), None)
        nm = re.search(r"(?:called|named|containing|with|that has|including) (?:the word |the name )?[\"“']?(.+?)[\"”']?(?: in (?:the|its) name)?(?:$| (?:from|that|i|in) )", body)
        name = nm.group(1).strip() if nm else None
        if not (kind or when) or (not kind and "file" not in body and "download" not in body):
            return None
        lo = {"today": 0, "yesterday": 1, "this week": 7, "last week": 14, "this month": 31, "last month": 62, "recently": 7}.get(when)
        floor = (datetime.combine(datetime.fromtimestamp(self.now()).date(), datetime.min.time()) - timedelta(days=lo)).timestamp() if lo is not None else 0
        hits = []
        for e in self._walk():
            low_n = e.name.lower()
            if kind and not low_n.endswith(KINDS[kind]):
                continue
            if name and name not in low_n:
                continue
            try:
                mt = e.stat().st_mtime
            except OSError:
                continue
            if mt >= floor and (when != "yesterday" or mt < floor + 86400 * 1):
                hits.append((mt, e))
        if not hits:
            return "I didn't find a matching file in Downloads, Desktop, Documents, Pictures, Videos or Music."
        hits.sort(key=lambda x: -x[0])
        parts = [f"{e.name} (in {Path(e.path).parent.name}, {datetime.fromtimestamp(mt).strftime('%b %d').replace(' 0', ' ')})" for mt, e in hits[:5]]
        return f"I found {len(hits)}: " + "; ".join(parts) + ("." if len(hits) <= 5 else f", and {len(hits) - 5} more.")

    # ------------------------------------------------------------------ automatic game playtime and PC performance
    @staticmethod
    def _real_procs() -> set[str]:
        try:
            import psutil
            return {(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
        except Exception:  # noqa: BLE001
            return set()

    def autoplay_check(self) -> None:
        st = self._state()
        if not st.get("autoplay") or self.guest:
            return
        running = self.procs()
        exes = {**GAME_EXES, **{k.lower(): v for k, v in st["game_exes"].items()}}
        live = {exes[p] for p in running if p in exes}
        pt, now = st["playtime"], self.now()
        openers = [s for s in pt if s.get("end") is None]
        changed = False
        for s in openers:
            if s.get("auto") and s["game"] not in live:
                s["end"] = now
                changed = True
        for g in live:
            if not any(s.get("end") is None and s["game"] == g for s in pt) and not any(s.get("end") is None and not s.get("auto") for s in pt):
                pt.append({"game": g, "start": now, "end": None, "auto": True})
                changed = True
        if changed:
            del pt[:-500]
            self._save()

    def _auto_rules(self, low: str, t: str) -> str | None:
        st = self._state()
        if re.fullmatch(r"(?:turn on|enable|start|activate) (?:automatic|auto) (?:game )?(?:play ?time|game tracking|tracking)|track my games automatically", low):
            st["autoplay"] = True
            self._save()
            return "Automatic playtime is on. I check every 30 seconds for well-known games (CS2, Roblox, Valorant, Fortnite, Minecraft and more). Add another with “track program mygame.exe as My Game”."
        if re.fullmatch(r"(?:turn off|disable|stop) (?:automatic|auto) (?:game )?(?:play ?time|game tracking|tracking)", low):
            st["autoplay"] = False
            self._save()
            return "Automatic playtime is off."
        m = re.fullmatch(r"track (?:the )?(?:program|exe|process|app) ([\w\-. ]{3,40}?\.exe) as (.{2,40})", t, re.I)
        if m:
            st["game_exes"][m.group(1).strip().lower()] = m.group(2).strip()
            self._save()
            return f"Okay, {m.group(1).strip()} counts as {m.group(2).strip()} for automatic playtime."
        if re.fullmatch(r"how(?:'s| is) my (?:pc|computer|laptop|system|machine) (?:doing|running|performing)|(?:pc|computer|system) (?:performance|stats|status|snapshot)|performance snapshot|how(?:'s| is) my performance|check my performance", low):
            try:
                import psutil
            except ImportError:
                return "I can't read system numbers because the monitoring library isn't installed."
            cpu = psutil.cpu_percent(interval=0.4)
            vm = psutil.virtual_memory()
            try:
                import shutil
                du = shutil.disk_usage(str(Path.home().anchor or "/"))
                disk = f" The main drive is {du.used * 100 // du.total} percent full."
            except OSError:
                disk = ""
            top = []
            try:
                procs = sorted(psutil.process_iter(["name", "memory_info"]), key=lambda p: -(p.info["memory_info"].rss if p.info.get("memory_info") else 0))[:3]
                top = [f"{p.info['name']} {p.info['memory_info'].rss // 1048576} megabytes" for p in procs if p.info.get("memory_info")]
            except Exception:  # noqa: BLE001
                pass
            return (f"CPU is at {cpu:.0f} percent and memory is {vm.percent:.0f} percent used ({vm.used // 2**30} of {vm.total // 2**30} gigabytes).{disk}"
                    + (" Biggest memory users: " + ", ".join(top) + "." if top else "") + " I can't read graphics card numbers from here.")
        return None

    # ------------------------------------------------------------------ scheduled scenes
    def _sched_rules(self, low: str, t: str) -> str | None:
        m = re.fullmatch(r"(?:run|start|activate|turn on) (?:the )?(.+?) (?:mode|scene)\s+(every .+|daily.*|each .+)|(every .+?|each .+?)\s+(?:run|start|activate|turn on) (?:the )?(.+?) (?:mode|scene)", low)
        if not m:
            return None
        name, freq = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if not self._scene_steps(name):
            return f"I don't have a scene called {name}. Say “list scenes” to hear them."
        r = self._add_repeat(f"{freq} to run {name} mode", datetime.fromtimestamp(self.now()), scene=name.strip())
        return r

    def take_due_scenes(self) -> list[str]:
        with self._lock:
            out, self._due_scenes = self._due_scenes, []
            return out

    # ------------------------------------------------------------------ wake word and speaking speed
    def _voice_rules(self, low: str, t: str) -> str | None:
        m = re.fullmatch(r"(?:change|set|make|switch)(?: my| the)? wake (?:word|phrase)(?: to| as)? [\"“']?(.{2,30}?)[\"”']?", t, re.I)
        if m:
            phrase = m.group(1).strip().lower()
            if phrase in ("to", "as") or not re.fullmatch(r"[a-z][a-z ']{1,28}", phrase):
                return "A wake word should be one to three plain words, for example “hey computer”."
            fn = self.actions.get("wake")
            if fn is None:
                return "I can't change the wake word from here. Open the Voice page."
            try:
                fn(phrase)
            except Exception as e:  # noqa: BLE001
                return f"I couldn't change the wake word: {e}"
            return f"Wake word changed to “{phrase}”. I'm restarting the listener so it takes effect."
        m = re.fullmatch(r"(?:speak|talk|read|go)(?: a bit| a little| much| way)? (slower|faster|normally|normal speed)|(?:slow down|speed up)(?: your (?:voice|speech|talking))?|(?:set )?(?:your )?(?:speaking |speech |voice )?speed (?:to )?(slower|faster|normal|\d(?:\.\d+)?)|(?:talk|speak) (?:more )?(slowly|quickly|quick)", low)
        if m:
            word = next((g for g in m.groups() if g), None) or ("slower" if low.startswith("slow") else "faster")
            get, setr = self.actions.get("rate_get"), self.actions.get("rate_set")
            if get is None or setr is None:
                return "I can't change my speaking speed from here. Open the Voice page."
            cur = float(get())
            if word in ("slower", "slowly"):
                new = cur - 0.2
            elif word in ("faster", "quickly", "quick"):
                new = cur + 0.2
            elif word in ("normally", "normal speed", "normal"):
                new = 1.0
            else:
                new = float(word)
            new = max(0.5, min(2.0, round(new, 2)))
            try:
                setr(new)
            except Exception as e:  # noqa: BLE001
                return f"I couldn't change my speed: {e}"
            return f"Okay, my speaking speed is {new:g}x" + (" (the slowest)." if new == 0.5 else " (the fastest)." if new == 2.0 else ".")
        return None
