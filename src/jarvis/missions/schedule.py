"""Schedules for approved missions. Persistent, time-zone aware, duplicate-safe, and honest about when they can run.

WHEN DO SCHEDULES RUN? Only while a Jarvis process is checking them: `jarvis schedule watch` (a console loop you leave open) or `jarvis schedule run-due`
(one check, e.g. from Windows Task Scheduler if you set that up yourself). Jarvis installs NO service and NO background task. While Jarvis is closed, the
PC is asleep or off, nothing runs. On the next check, an occurrence that is still within its grace window (default 15 min) runs late; an older one counts
as MISSED and is recorded, and is either skipped (default) or run once if you chose `run_once`. Missed occurrences are never replayed all at once.

AUTHORITY: scheduling a mission does not approve it. A mission with consequential steps needs its own schedule approval, bound to the exact plan hash,
limited to N runs and an expiry date; it is checked again at every run, together with the permission switches, consent and your confirmation of each
action (which needs you at the console: if nobody answers, the action is declined). Missions that run code, install software or use the internet are
never scheduled. Editing a schedule so that it fires more often removes its approval."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from jarvis.missions import model as M
from jarvis.missions.store import DIR, MissionStore, pid_alive

MIN_INTERVAL_MIN, MIN_INTERVAL_CONSEQ_MIN = 5, 60
DEFAULT_GRACE, MAX_GRACE = 15, 240
MAX_RUNS, DEFAULT_RECURRING_RUNS = 50, 5
MAX_APPROVAL_DAYS, DEFAULT_APPROVAL_DAYS = 90, 30
SID_RE = re.compile(r"^sch-[0-9a-f]{8}$")
KINDS = ("once", "daily", "weekly", "interval")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleError(ValueError):
    pass


def get_tz(name: str) -> tzinfo:
    if name.upper() == "UTC":
        return timezone.utc
    m = re.fullmatch(r"(?i)(?:UTC)?([+-])(\d{2}):?(\d{2})", name)
    if m:
        off = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        if off > timedelta(hours=14):
            raise ScheduleError("a UTC offset is at most 14 hours")
        return timezone(off if m.group(1) == "+" else -off, name)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception as e:  # noqa: BLE001 - not found, or no time-zone database on this PC (common on Windows without the 'tzdata' package)
        raise ScheduleError(f"time zone '{name}' is not available here ({type(e).__name__}). Use 'UTC', a fixed offset like UTC-05:00, "
                            "or install the 'tzdata' package for names like America/Toronto") from None


def local_default_tz_name() -> str:
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    mins = int(off.total_seconds() // 60)
    sign = "+" if mins >= 0 else "-"
    return f"UTC{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wall(date, hh: int, mm: int, tz: tzinfo) -> datetime:
    """The instant of this wall-clock time in tz. A time that does not exist (clocks jump forward) moves forward; a repeated time (clocks go back) uses the first."""
    naive = datetime(date.year, date.month, date.day, hh, mm)
    return _utc(naive.replace(tzinfo=tz, fold=0))


def occurrences(s: dict, after: datetime, until: datetime, cap: int = 500) -> list[datetime]:
    """Occurrences strictly after `after` and not after `until` (both aware)."""
    out: list[datetime] = []
    tz = get_tz(s["tz"])
    if s["kind"] == "once":
        t = _utc(datetime.strptime(s["at"], "%Y-%m-%dT%H:%M").replace(tzinfo=tz, fold=0))
        if after < t <= until:
            out.append(t)
    elif s["kind"] in ("daily", "weekly"):
        hh, mm = (int(x) for x in s["time"].split(":"))
        d = (after.astimezone(tz) - timedelta(days=1)).date()
        end = (until.astimezone(tz) + timedelta(days=1)).date()
        while d <= end and len(out) < cap:
            if s["kind"] == "daily" or d.weekday() in s["weekdays"]:
                t = _wall(d, hh, mm, tz)
                if after < t <= until and t not in out:
                    out.append(t)
            d += timedelta(days=1)
    else:
        step = timedelta(minutes=s["every_minutes"])
        anchor = _parse_utc(s["anchor"])
        if until >= anchor:
            k = max(0, int((after - anchor) / step))
            t = anchor + k * step
            while t <= until and len(out) < cap:
                if t > after:
                    out.append(t)
                t += step
    return sorted(out)


def parse_time(t: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScheduleError("time must look like 09:30 (24-hour)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_days(spec: str) -> list[int]:
    out = []
    for p in re.split(r"[,\s]+", spec.strip().lower()):
        if p not in DAYS:
            raise ScheduleError(f"unknown weekday '{p}'; use {', '.join(DAYS)}")
        out.append(DAYS.index(p))
    if not out:
        raise ScheduleError("name at least one weekday")
    return sorted(set(out))


class Scheduler:
    def __init__(self, profile_dir, store: MissionStore | None = None, clock=None):
        self.root = Path(profile_dir) / DIR
        self.file = self.root / "schedules.json"
        self.store = store or MissionStore(profile_dir)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ---------------------------------------------------------------- persistence (locked, atomic)
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / "schedules.lock"
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    json.dump({"pid": os.getpid()}, f)
                return p
            except FileExistsError:
                try:
                    pid = int(json.loads(p.read_text()).get("pid", 0))
                except (OSError, ValueError, TypeError):
                    pid = 0
                if (not pid_alive(pid) or time.time() - p.stat().st_mtime > 120) and pid != os.getpid():
                    p.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise ScheduleError("the schedule file is locked by another Jarvis process")
                time.sleep(0.05)

    def _read(self) -> dict:
        try:
            d = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("schedules"), list):
                d.setdefault("claims", {})
                return d
        except (OSError, ValueError):
            pass
        return {"version": 1, "schedules": [], "claims": {}}

    def _write(self, d: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".s-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, self.file)

    def _txn(self, fn):
        lk = self._lock()
        try:
            d = self._read()
            r = fn(d)
            self._write(d)
            return r
        finally:
            lk.unlink(missing_ok=True)

    def list(self) -> list[dict]:
        return self._read()["schedules"]

    def get(self, sid: str) -> dict:
        for s in self.list():
            if s["id"] == sid:
                return s
        raise ScheduleError(f"no schedule {sid}")

    # ---------------------------------------------------------------- create / edit
    def create(self, mission_id: str, kind: str, *, tz: str | None = None, at: str | None = None, time_: str | None = None, weekdays: str | None = None,
               every_minutes: int | None = None, missed: str = "skip", grace: int = DEFAULT_GRACE, specs: dict | None = None) -> dict:
        m = self.store.load(mission_id)
        if m.get("tampered"):
            raise ScheduleError("that mission's steps do not match its recorded hash; review it again first")
        if kind not in KINDS:
            raise ScheduleError(f"kind must be one of {KINDS}")
        if missed not in ("skip", "run_once"):
            raise ScheduleError("missed must be skip or run_once")
        if not 1 <= grace <= MAX_GRACE:
            raise ScheduleError(f"grace must be 1-{MAX_GRACE} minutes")
        tzname = tz or local_default_tz_name()
        get_tz(tzname)
        now = self.clock()
        s = {"id": "sch-" + uuid.uuid4().hex[:8], "mission": mission_id, "kind": kind, "tz": tzname, "state": "active", "missed": missed, "grace_minutes": grace,
             "created": _iso(now), "cursor": _iso(now), "runs": 0, "approval": None, "last": None, "reason": ""}
        if kind == "once":
            if not at:
                raise ScheduleError("a one-time schedule needs --at 'YYYY-MM-DD HH:MM' (in the schedule's time zone)")
            try:
                s["at"] = datetime.strptime(at.strip().replace("T", " "), "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                raise ScheduleError("the time must look like 2026-11-02 09:30") from None
            if occurrences(s, now - timedelta(days=3650), now) or not occurrences(s, now, now + timedelta(days=3650)):
                raise ScheduleError("that time is not in the future")
        elif kind in ("daily", "weekly"):
            s["time"] = parse_time(time_ or "")
            if kind == "weekly" and not weekdays:
                raise ScheduleError("a weekly schedule needs weekdays, e.g. mon,thu")
            s["weekdays"] = parse_days(weekdays) if kind == "weekly" else list(range(7))
        else:
            if not every_minutes or every_minutes < MIN_INTERVAL_MIN or every_minutes > 7 * 24 * 60:
                raise ScheduleError(f"an interval schedule needs --every-minutes {MIN_INTERVAL_MIN} to {7 * 24 * 60}")
            s["every_minutes"], s["anchor"] = int(every_minutes), _iso(now)
        if specs is not None:
            bad = M.unattended_problems(m, specs)
            if bad:
                raise ScheduleError("this mission cannot be scheduled: " + "; ".join(bad))
        if specs is not None and M.consequential(m, specs) and kind == "interval" and s["every_minutes"] < MIN_INTERVAL_CONSEQ_MIN:
            raise ScheduleError(f"a mission that changes things cannot repeat more often than every {MIN_INTERVAL_CONSEQ_MIN} minutes")
        self._txn(lambda d: d["schedules"].append(s))
        return s

    def approve(self, sid: str, plan_hash_prefix: str, max_runs: int | None = None, days: int | None = None) -> dict:
        """The schedule's own authorisation for a mission with consequential steps: this exact plan, at most N runs, until a date."""
        sch = self.get(sid)
        m = self.store.load(sch["mission"])
        if len(plan_hash_prefix) < 12 or not m["plan_hash"].startswith(plan_hash_prefix.lower()):
            raise ScheduleError("the hash does not match this mission's current plan (copy the first 12+ characters from `jarvis mission review`)")
        runs = max_runs if max_runs is not None else (1 if sch["kind"] == "once" else DEFAULT_RECURRING_RUNS)
        if not 1 <= runs <= MAX_RUNS:
            raise ScheduleError(f"max runs must be 1-{MAX_RUNS}")
        dd = days if days is not None else DEFAULT_APPROVAL_DAYS
        if not 1 <= dd <= MAX_APPROVAL_DAYS:
            raise ScheduleError(f"approval lasts 1-{MAX_APPROVAL_DAYS} days")
        now = self.clock()
        ap = {"plan_hash": m["plan_hash"], "at": _iso(now), "expires": _iso(now + timedelta(days=dd)), "max_runs": runs, "runs_used": 0}

        def f(d):
            for s in d["schedules"]:
                if s["id"] == sid:
                    s["approval"] = ap
                    if s["state"] == "paused" and "approval" in s.get("reason", ""):
                        s["state"], s["reason"] = "active", ""
        self._txn(f)
        return ap

    def _update(self, sid, fn):
        def f(d):
            for s in d["schedules"]:
                if s["id"] == sid:
                    return fn(s, d)
            raise ScheduleError(f"no schedule {sid}")
        return self._txn(f)

    def set_state(self, sid: str, state: str, reason: str = "") -> None:
        cur = self.get(sid)
        legal = {"active": {"paused", "cancelled"}, "paused": {"active", "cancelled"}, "done": {"cancelled"}, "cancelled": set()}
        if state != cur["state"] and state not in legal[cur["state"]]:
            raise ScheduleError(f"a {cur['state']} schedule cannot become {state}")

        def f(s, d):
            s["state"], s["reason"] = state, reason[:200]
            if state == "active":
                s["cursor"] = _iso(self.clock())                # resuming never replays what passed while it was paused
        self._update(sid, f)

    def edit(self, sid: str, *, tz=None, at=None, time_=None, weekdays=None, every_minutes=None, specs: dict | None = None) -> dict:
        cur = self.get(sid)
        m = self.store.load(cur["mission"])
        conseq = specs is not None and M.consequential(m, specs)
        if tz:
            get_tz(tz)
        broadens = False

        def f(s, d):
            nonlocal broadens
            if tz:
                s["tz"] = tz
            if at and s["kind"] == "once":
                s["at"] = datetime.strptime(at.strip().replace("T", " "), "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M")
                s["state"] = "active" if s["state"] == "done" else s["state"]
            if time_ and s["kind"] in ("daily", "weekly"):
                s["time"] = parse_time(time_)
            if weekdays and s["kind"] == "weekly":
                new = parse_days(weekdays)
                broadens = broadens or len(new) > len(s["weekdays"])
                s["weekdays"] = new
            if every_minutes and s["kind"] == "interval":
                if every_minutes < MIN_INTERVAL_MIN or (conseq and every_minutes < MIN_INTERVAL_CONSEQ_MIN):
                    raise ScheduleError("that interval is too short for this mission")
                broadens = broadens or every_minutes < s["every_minutes"]
                s["every_minutes"], s["anchor"] = int(every_minutes), _iso(self.clock())
            s["cursor"] = _iso(self.clock())
            if conseq and broadens and s["approval"]:
                s["approval"], s["state"], s["reason"] = None, "paused", "approval removed: the schedule now fires more often; approve it again"
            return dict(s)
        return self._update(sid, f)

    # ---------------------------------------------------------------- the check
    def tick(self, runner, now: datetime | None = None, interactive_note: str = "") -> list[dict]:
        """Look at every schedule once and run what is due. `runner` is a MissionRunner. Returns what happened (also written to the history)."""
        now = now or self.clock()
        events: list[dict] = []
        for s in [x for x in self.list() if x["state"] == "active"]:
            try:
                events += self._tick_one(s, runner, now)
            except (ScheduleError, KeyError, M.StateError) as e:
                self._log(events, s, None, "error", str(e))
        return events

    def _log(self, events, s, occ, status, detail="", extra=None):
        rec = {"schedule": s["id"], "mission": s["mission"], "due": _iso(occ) if occ else None, "status": status, "detail": detail[:300], **(extra or {})}
        events.append(rec)
        self.store.log({"event": "schedule", **rec})

    def _tick_one(self, s, runner, now):
        events: list[dict] = []
        after = _parse_utc(s["cursor"])
        occ = occurrences(s, after, now)
        # advance the cursor FIRST (under the lock): even a crash during the run cannot make the same occurrence due again
        self._update(s["id"], lambda x, d: x.update(cursor=_iso(max(now, _parse_utc(x["cursor"])))))   # never moves backwards (clock set back)
        if not occ:
            return events
        grace = timedelta(minutes=s["grace_minutes"])
        ontime = [o for o in occ if now - o <= grace]
        late = [o for o in occ if now - o > grace]
        for o in (late if ontime else late[:-1]):
            self._log(events, s, o, "missed", "Jarvis was not checking at that time")
        target = ontime[-1] if ontime else None
        for o in ontime[:-1]:
            self._log(events, s, o, "coalesced", "a later occurrence is also due; only the latest runs")
        if target is None and late:
            o = late[-1]
            if s["missed"] == "run_once":
                target = o
                self._log(events, s, o, "missed", "ran late once because this schedule says run_once")
            else:
                self._log(events, s, o, "missed", "skipped (this schedule says skip missed runs)")
                self._after_occurrence(s, ran=False)
        if target is None:
            return events
        events += self._fire(s, target, runner, now)
        return events

    def _after_occurrence(self, s, ran: bool):
        def f(x, d):
            if x["kind"] == "once":
                x["state"] = "done"
        self._update(s["id"], f)

    def _authorize(self, s, m, specs, now) -> str | None:
        if m.get("tampered"):
            return "the mission's steps changed after it was approved"
        if m["state"] in ("running", "paused", "cancelled"):
            return f"the mission is {m['state']}"
        if M.unattended_problems(m, specs):
            return "the mission contains steps that are never run unattended"
        if not M.consequential(m, specs):
            return None                                   # a pure observation mission needs no approval of its own
        ap = s.get("approval")
        if not ap:
            return "approval missing: this mission changes things, so the schedule needs its own approval (jarvis schedule approve)"
        if ap["plan_hash"] != m["plan_hash"]:
            return "approval does not match the mission's current plan"
        if _parse_utc(ap["expires"]) <= now:
            return "approval expired"
        if ap["runs_used"] >= ap["max_runs"]:
            return "approval used up (its run limit was reached)"
        return None

    def _fire(self, s, occ, runner, now):
        events: list[dict] = []
        key = f"{s['id']}@{_iso(occ)}"
        m = self.store.load(s["mission"])
        why = self._authorize(s, m, runner.ex.specs, now)
        if why:
            self._log(events, s, occ, "refused", why)
            self._update(s["id"], lambda x, d: x.update(state="paused", reason="approval: " + why) if "approv" in why else x.update(last={"due": _iso(occ), "status": "refused"}))
            if s["kind"] == "once":
                self._after_occurrence(s, ran=False)
            return events
        claimed = {"ok": False}

        def claim(x, d):                                   # write the claim BEFORE running: a restart can never run the same occurrence twice
            if key in d["claims"]:
                return
            d["claims"][key] = {"schedule": s["id"], "claimed": _iso(now), "status": "started", "pid": os.getpid()}
            claimed["ok"] = True
            if len(d["claims"]) > 2000:
                for k in sorted(d["claims"], key=lambda k: d["claims"][k]["claimed"])[:500]:
                    d["claims"].pop(k)
            ap = x.get("approval")
            if ap:
                ap["runs_used"] = ap.get("runs_used", 0) + 1
            x["runs"] = x.get("runs", 0) + 1
        self._update(s["id"], claim)
        if not claimed["ok"]:
            self._log(events, s, occ, "duplicate_prevented", "this occurrence was already started before (restart or second process)")
            return events
        status, detail, ex_id = "ran", "", None
        try:
            res = runner.run(s["mission"], trigger=f"schedule:{s['id']}", authority=f"schedule {s['id']}", fresh=True)
            ex = M.current_execution(res)
            ex_id = ex["id"] if ex else None
            status, detail = f"mission_{res['state']}", res["state_reason"]
        except Exception as e:  # noqa: BLE001 - recorded, never lets one schedule stop the others
            status, detail = "error", f"{type(e).__name__}: {e}"

        def done(x, d):
            d["claims"][key].update(status=status, finished=_iso(self.clock()), execution=ex_id)
            x["last"] = {"due": _iso(occ), "status": status, "execution": ex_id}
            if x["kind"] == "once":
                x["state"] = "done"
            if x.get("approval") and x["approval"]["runs_used"] >= x["approval"]["max_runs"] and x["kind"] != "once":
                x["state"], x["reason"] = "paused", "approval used up: approve again to continue"
        self._update(s["id"], done)
        self._log(events, s, occ, status, detail, {"execution": ex_id})
        return events

    def recover_claims(self) -> int:
        """Claims left 'started' by a process that no longer exists: marked interrupted. They are NOT re-run."""
        n = {"n": 0}

        def f(d):
            for k, c in d["claims"].items():
                if c["status"] == "started" and not pid_alive(int(c.get("pid", 0))):
                    c["status"], c["finished"] = "interrupted_unknown", _iso(self.clock())
                    n["n"] += 1
        self._txn(f)
        return n["n"]

    def delete_schedule(self, sid: str) -> None:
        self.get(sid)
        self._txn(lambda d: d.update(schedules=[s for s in d["schedules"] if s["id"] != sid],
                                    claims={k: v for k, v in d["claims"].items() if v.get("schedule") != sid}))

    def delete_all(self) -> None:
        self.file.unlink(missing_ok=True)
