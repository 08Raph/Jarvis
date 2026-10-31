"""Tasks, reminders, a local calendar, plans and email/message drafts. Everything is stored on this PC; nothing is synced and nothing is ever sent."""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta

from jarvis.operator.core import LOW, MEDIUM, Outcome, OperatorError, cap

PRIORITY = {"high": 3, "medium": 2, "normal": 2, "low": 1}


def _file(op):
    return op.folder / "tasks.json"


def load(op) -> dict:
    try:
        d = json.loads(_file(op).read_text(encoding="utf-8"))
        d.setdefault("tasks", [])
        d.setdefault("events", [])
        return d
    except (OSError, ValueError):
        return {"tasks": [], "events": []}


def save(op, d: dict) -> None:
    if op.guest:
        return
    op.folder.mkdir(parents=True, exist_ok=True)
    _file(op).write_text(json.dumps(d), encoding="utf-8")


def parse_when(text: str, now: datetime | None = None) -> float | None:
    """'in 10 minutes', 'tomorrow at 9am', 'at 5pm', 'on 2026-11-02 14:30' -> epoch seconds, else None."""
    now = now or datetime.now()
    t = text.lower().strip()
    m = re.search(r"in (\d+(?:\.\d+)?)\s*(second|sec|minute|min|hour|hr|day|week)s?", t)
    if m:
        n, u = float(m.group(1)), m.group(2)
        secs = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}[u]
        return (now + timedelta(seconds=n * secs)).timestamp()
    day = now
    if "tomorrow" in t:
        day = now + timedelta(days=1)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        day = day.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)))
    tm = re.search(r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t.split("on ")[-1] if " on " in t else t)
    if (("at " in t) or m or "tomorrow" in t) and tm and (tm.group(3) or tm.group(2) or "at " in t):
        h, mi = int(tm.group(1)), int(tm.group(2) or 0)
        if tm.group(3) == "pm" and h < 12:
            h += 12
        if tm.group(3) == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            return None
        when = day.replace(hour=h, minute=mi, second=0, microsecond=0)
        if when <= now and "tomorrow" not in t and not m:
            when += timedelta(days=1)
        return when.timestamp()
    return None


@cap("tasks.add", "tasks", "Add a task or reminder", LOW, ("title", "when", "priority", "project", "remind"), False, "41,42")
def add(op, title: str = "", when: str = "", priority: str = "normal", project: str = "", remind: bool = False) -> Outcome:
    title = title.strip()
    if not title:
        raise OperatorError("What is the task?")
    due = parse_when(when) if when else None
    if when and due is None:
        raise OperatorError("I couldn't understand that time. Try \"in 20 minutes\", \"tomorrow at 9am\" or \"at 5pm\".")
    d = load(op)
    t = {"id": uuid.uuid4().hex[:8], "title": title[:200], "due": due, "priority": PRIORITY.get(priority.lower(), 2), "project": project[:60], "done": False,
         "remind": bool(remind or due), "reminded": False, "created": time.time()}
    d["tasks"].append(t)
    save(op, d)
    return Outcome("verified" if any(x["id"] == t["id"] for x in load(op)["tasks"]) or op.guest else "failed",
                   f"Added: {title}" + (f", due {datetime.fromtimestamp(due).strftime('%a %b %d %H:%M')}" if due else "") + ".", t)


@cap("tasks.list", "tasks", "List open tasks", LOW, ("project", "all"), False, "42")
def list_tasks(op, project: str = "", all: bool = False) -> Outcome:
    ts = [t for t in load(op)["tasks"] if (all or not t["done"]) and (not project or project.lower() in t["project"].lower())]
    ts.sort(key=lambda t: (t["done"], -(t["priority"]), t["due"] or 9e12))
    now = time.time()
    if not ts:
        return Outcome("info", "You have no open tasks.")
    return Outcome("info", f"{len(ts)} task(s): " + "; ".join(f"{t['title']}" + (" (OVERDUE)" if t["due"] and t["due"] < now and not t["done"] else (f" (due {datetime.fromtimestamp(t['due']).strftime('%b %d %H:%M')})" if t["due"] else "")) for t in ts[:15]), ts)


@cap("tasks.complete", "tasks", "Mark a task done", LOW, ("title",), False, "42")
def complete(op, title: str = "") -> Outcome:
    d = load(op)
    q = title.lower().strip()
    hit = next((t for t in d["tasks"] if not t["done"] and q and q in t["title"].lower()), None)
    if hit is None:
        return Outcome("info", f"I don't have an open task matching \"{title}\".")
    hit["done"] = True
    save(op, d)
    return Outcome("verified", f"Marked \"{hit['title']}\" done.")


@cap("tasks.delete", "tasks", "Delete a task", MEDIUM, ("title",), False, "42")
def delete(op, title: str = "") -> Outcome:
    d = load(op)
    q = title.lower().strip()
    keep = [t for t in d["tasks"] if not (q and q in t["title"].lower())]
    n = len(d["tasks"]) - len(keep)
    d["tasks"] = keep
    save(op, d)
    return Outcome("verified" if n else "info", f"Deleted {n} task(s)." if n else "No task matched.")


def due_reminders(op, now: float | None = None) -> list[dict]:
    """Tasks whose time has come and that haven't been announced; marks them announced."""
    now = now or time.time()
    d = load(op)
    out = []
    for t in d["tasks"]:
        if not t["done"] and t.get("remind") and t["due"] and t["due"] <= now and not t.get("reminded"):
            t["reminded"] = True
            out.append(t)
    if out:
        save(op, d)
    return out


@cap("tasks.plan", "tasks", "Summarize today's or this week's plan, overdue items and priorities", LOW, ("span",), False, "41,42")
def plan(op, span: str = "today") -> Outcome:
    d = load(op)
    now = datetime.now()
    end = now.replace(hour=23, minute=59, second=59) + (timedelta(days=6) if span == "week" else timedelta())
    open_ = [t for t in d["tasks"] if not t["done"]]
    overdue = [t for t in open_ if t["due"] and t["due"] < time.time()]
    soon = [t for t in open_ if t["due"] and time.time() <= t["due"] <= end.timestamp()]
    evs = [e for e in d["events"] if time.time() - 3600 <= e["start"] <= end.timestamp()]
    top = sorted((t for t in open_ if t not in overdue and t not in soon), key=lambda t: -t["priority"])[:3]
    parts = []
    if overdue:
        parts.append("Overdue: " + ", ".join(t["title"] for t in overdue[:6]))
    if soon:
        parts.append(("Due " + ("this week" if span == "week" else "today") + ": ") + ", ".join(f"{t['title']} ({datetime.fromtimestamp(t['due']).strftime('%a %H:%M')})" for t in sorted(soon, key=lambda t: t["due"])[:8]))
    if evs:
        parts.append("Events: " + ", ".join(f"{e['title']} ({datetime.fromtimestamp(e['start']).strftime('%a %H:%M')})" for e in sorted(evs, key=lambda e: e["start"])[:8]))
    if top:
        parts.append("Worth doing next: " + ", ".join(t["title"] for t in top))
    return Outcome("info", " ".join(parts) + "." if parts else f"Nothing is scheduled for {'this week' if span == 'week' else 'today'} and you have no overdue tasks.", {"overdue": len(overdue)})


@cap("calendar.add", "tasks", "Add an event to the local calendar", LOW, ("title", "when", "minutes"), False, "41")
def cal_add(op, title: str = "", when: str = "", minutes: int = 60) -> Outcome:
    start = parse_when(when)
    if not title.strip() or start is None:
        raise OperatorError("Tell me the event and when, e.g. \"dentist tomorrow at 3pm\".")
    d = load(op)
    clash = [e for e in d["events"] if e["start"] < start + minutes * 60 and start < e["start"] + e["minutes"] * 60]
    d["events"].append({"id": uuid.uuid4().hex[:8], "title": title[:120], "start": start, "minutes": int(minutes)})
    save(op, d)
    return Outcome("verified", f"Added {title} on {datetime.fromtimestamp(start).strftime('%a %b %d %H:%M')} to your local calendar (not synced to Outlook or Google)." + (f" It overlaps with {clash[0]['title']}." if clash else ""))


@cap("calendar.list", "tasks", "Show upcoming local calendar events and conflicts", LOW, ("days",), False, "41")
def cal_list(op, days: int = 7) -> Outcome:
    now = time.time()
    evs = sorted((e for e in load(op)["events"] if now - 3600 <= e["start"] <= now + days * 86400), key=lambda e: e["start"])
    clashes = [(a["title"], b["title"]) for i, a in enumerate(evs) for b in evs[i + 1:] if b["start"] < a["start"] + a["minutes"] * 60]
    if not evs:
        return Outcome("info", f"Nothing on your local calendar in the next {days} days.")
    return Outcome("info", "; ".join(f"{e['title']} {datetime.fromtimestamp(e['start']).strftime('%a %H:%M')}" for e in evs[:12]) + (f". Conflict: {clashes[0][0]} and {clashes[0][1]}." if clashes else "."), evs)


@cap("draft.email", "tasks", "Open a new email draft in your mail app (never sent)", MEDIUM, ("to", "subject", "body"), True, "39,40")
def draft_email(op, to: str = "", subject: str = "", body: str = "") -> Outcome:
    if to and not re.fullmatch(r"[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+", to):
        raise OperatorError("That email address doesn't look right.")
    url = "mailto:" + urllib.parse.quote(to) + "?subject=" + urllib.parse.quote(subject[:200]) + "&body=" + urllib.parse.quote(body[:1500])
    op.ops.open_target(url)
    return Outcome("unverified", "Opened a draft in your mail app. I did not send it; you press Send.")


@cap("draft.spam_check", "tasks", "Look for spam or phishing signs in pasted email text", LOW, ("text",), False, "39")
def spam_check(op, text: str = "") -> Outcome:
    t = text.lower()
    signs = []
    for pat, why in ((r"urgent|immediately|within 24 hours|account (will be )?(suspended|closed|locked)", "pressure to act fast"), (r"verify your (account|password|identity)|confirm your (login|password)", "asks you to verify credentials"),
                     (r"https?://\d+\.\d+\.\d+\.\d+", "link to a bare IP address"), (r"bit\.ly|tinyurl|t\.co/", "shortened link hides the destination"), (r"gift card|wire transfer|crypto|bitcoin", "asks for hard-to-reverse payment"),
                     (r"dear (customer|user|sir|member)", "generic greeting"), (r"you('ve| have) won|lottery|prize", "prize bait"), (r"\.(exe|scr|zip|js|iso)\b", "risky attachment type")):
        if re.search(pat, t):
            signs.append(why)
    return Outcome("info", ("Warning signs: " + "; ".join(signs) + ". Don't click links; go to the real site by typing its address.") if signs else "I see no obvious spam signs in that text, but I can't verify the sender.", signs)
