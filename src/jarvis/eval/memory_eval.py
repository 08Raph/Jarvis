"""Known-answer evaluation of personal-memory retrieval (Prompt 19). Needs NO language model, so it is a REAL measurement on this PC.

What it measures: given saved memories, does `PersonalMemory.search` return the right one near the top, does it stay quiet when
nothing relevant was saved, and does a newer fact (a correction) beat the old one it replaced?
What it does NOT measure: usefulness inside a conversation, or anything about a language model.

Honesty rules baked into the data:
  * The memories and queries were written BEFORE any retrieval change was made and are split into `dev` (used while tuning) and `test`
    (looked at only to report). Odd-numbered queries are dev, even-numbered are test, assigned by position, not by outcome.
  * Queries are tagged by difficulty: `lex` (shares content words), `morph` (inflection/derivation), `typo`, `semantic` (almost no shared
    words, needs meaning), `conflict` (an older value was superseded), `hardneg` (an unanswerable question that shares words with a memory)
    and `none` (nothing relevant was saved). A purely lexical retriever is EXPECTED to fail most `semantic` queries; they stay in the set.
  * Memories are always ADDED in the listed order so superseded rows behave the same on every run."""
from __future__ import annotations

from typing import Callable

# (label, category, text, key, project)
MEMORIES: list[tuple[str, str, str, str | None, str | None]] = [
    ("M1", "explicit_preference", "My name is Jordan Rivera.", "name", None),
    ("M2", "explicit_preference", "I prefer dark mode in every app.", "theme", None),
    ("M3", "explicit_preference", "My favorite programming language is Python.", "fav_language", None),
    ("M31", "approved_context", "I live in Ottawa.", "city", None),                        # older value; superseded by M4
    ("M4", "approved_context", "I live in Toronto, Ontario.", "city", None),
    ("M5", "approved_context", "My dentist appointment is on Friday at 2pm.", None, None),
    ("M6", "project", "Deadline is March 3rd.", None, "Atlas"),
    ("M7", "project", "Uses PostgreSQL for storage.", None, "Atlas"),
    ("M8", "project", "A mobile app for tracking hikes.", None, "Borealis"),
    ("M9", "project", "Release candidate is due in June.", None, "Borealis"),
    ("M10", "approved_context", "My wife's name is Elena.", None, None),
    ("M11", "approved_context", "My daughter plays violin on Tuesdays.", None, None),
    ("M12", "explicit_preference", "I don't eat shellfish.", None, None),
    ("M13", "explicit_preference", "I usually wake up at 6:30 in the morning.", None, None),
    ("M14", "approved_context", "My laptop is a Lenovo ThinkPad with 32 GB of RAM.", None, None),
    ("M15", "approved_context", "The wifi router is in the basement closet.", None, None),
    ("M16", "approved_context", "I drive a blue Honda Civic.", None, None),
    ("M17", "approved_context", "My car insurance renews in September.", None, None),
    ("M18", "explicit_preference", "I am learning Spanish using flashcards every evening.", None, None),
    ("M19", "approved_context", "My manager is Priya Natarajan.", None, None),
    ("M20", "approved_context", "Weekly team standup is Monday at 9:15.", None, None),
    ("M21", "explicit_preference", "Always answer in metric units.", "units", None),
    ("M22", "explicit_preference", "I like concise answers without bullet lists.", "style", None),
    ("M23", "approved_context", "My cat is called Biscuit.", None, None),
    ("M24", "approved_context", "The backup drive is the gray Seagate in the desk drawer.", None, None),
    ("M25", "explicit_preference", "I run 5 kilometers on Saturday mornings.", None, None),
    ("M32", "explicit_preference", "My preferred code editor is Sublime Text.", "editor", None),   # older value; superseded by M27
    ("M27", "explicit_preference", "My preferred code editor is VS Code.", "editor", None),
    ("M28", "approved_context", "Mom's birthday is on October 12.", None, None),
    ("M29", "approved_context", "I'm saving for a trip to Japan next year.", None, None),
    ("M30", "explicit_preference", "My favorite coffee is a flat white with oat milk.", None, None),
    ("M33", "project", "Retro meeting is every other Thursday.", None, "Atlas"),
    ("M34", "project", "The designer is Marcus.", None, "Borealis"),
    ("M36", "explicit_preference", "I keep my notes in Obsidian.", None, None),
    ("M37", "approved_context", "My sister Dana lives in Vancouver.", None, None),
    ("M38", "approved_context", "I take the 7:40 train to the office on weekdays.", None, None),
    ("M39", "approved_context", "The office is on the fourth floor, room 412.", None, None),
    ("M40", "approved_context", "My monitor is a 27 inch Dell with a USB-C dock.", None, None),
    ("M26", "approved_context", "Passport renewal is due next spring.", None, None),
]

# (query, gold labels (any counts as a hit), tag, project filter or None). Empty gold = nothing relevant was saved.
_Q: list[tuple[str, tuple[str, ...], str, str | None]] = [
    ("What is my name?", ("M1",), "lex", None),
    ("Which theme do I prefer?", ("M2",), "lex", None),
    ("What language do I like to program in?", ("M3",), "morph", None),
    ("Where do I live?", ("M4",), "conflict", None),
    ("When is my dentist appointment?", ("M5",), "lex", None),
    ("When is the deadline?", ("M6",), "lex", "Atlas"),
    ("What database does it use?", ("M7",), "semantic", "Atlas"),
    ("What is Borealis?", ("M8",), "lex", None),
    ("When is the Borealis release due?", ("M9",), "lex", None),
    ("What's my wife called?", ("M10",), "lex", None),
    ("What day does my daughter have violin?", ("M11",), "lex", None),
    ("Which foods do I avoid?", ("M12",), "semantic", None),
    ("What time do I get up?", ("M13",), "semantic", None),
    ("How much memory does my laptop have?", ("M14",), "semantic", None),
    ("Where is the router?", ("M15",), "lex", None),
    ("What car do I drive?", ("M16",), "lex", None),
    ("When does my car insurance renew?", ("M17",), "morph", None),
    ("Which language am I studying?", ("M18",), "semantic", None),
    ("Who is my boss?", ("M19",), "semantic", None),
    ("When is the standup?", ("M20",), "lex", None),
    ("Which units should you use?", ("M21",), "morph", None),
    ("How should answers be formatted?", ("M22",), "semantic", None),
    ("What is my cats name?", ("M23",), "typo", None),
    ("Where's my backup drive?", ("M24",), "lex", None),
    ("How far do I run on weekends?", ("M25",), "semantic", None),
    ("Which code editor do I like?", ("M27",), "conflict", None),
    ("When is mom's birthday?", ("M28",), "lex", None),
    ("What am I saving money for?", ("M29",), "morph", None),
    ("How do I take my coffee?", ("M30",), "lex", None),
    ("Who designs for Borealis?", ("M34",), "morph", None),
    ("What notes app do I use?", ("M36",), "morph", None),
    ("Where does my sister live?", ("M37",), "lex", None),
    ("Which train do I catch to work?", ("M38",), "lex", None),
    ("What floor is the office on?", ("M39",), "lex", None),
    ("What kind of monitor do I own?", ("M40",), "lex", None),
    ("How often is the Atlas retrospective?", ("M33",), "morph", None),
    ("When do I need to renew my pasport?", ("M26",), "typo", None),
    ("What is my favorite movie?", (), "hardneg", None),
    ("What is my phone number?", (), "none", None),
    ("What did I have for dinner last night?", (), "none", None),
    ("When is my anniversary?", (), "none", None),
    ("What is my blood type?", (), "none", None),
    ("Which airline do I prefer?", (), "none", None),
    ("What is the weather like?", (), "none", None),
    ("Who won the game last night?", (), "none", None),
    ("What is the Atlas budget?", (), "hardneg", None),
    ("What is the price of the Borealis app?", (), "hardneg", None),
    ("How tall am I?", (), "none", None),
]
QUERIES = [{"id": f"Q{i + 1}", "query": q, "gold": g, "tag": t, "project": p, "split": "dev" if (i + 1) % 2 == 1 else "test"}
           for i, (q, g, t, p) in enumerate(_Q)]
SUPERSEDED = {"M31", "M32"}          # must never be returned


def build(mem) -> dict[int, str]:
    """Fill `mem` (a PersonalMemory) and return {memory id: label}."""
    ids: dict[int, str] = {}
    for label, cat, text, key, project in MEMORIES:
        row = mem.add(cat, text, key=key, project=project, log=False)
        ids[row["id"]] = label
    return ids


def evaluate(search: Callable[[str, str | None], list[dict]], ids: dict[int, str], split: str | None = None) -> dict:
    """`search(query, project)` -> hits (dicts with 'id'), best first. Returns the metrics and every per-query outcome."""
    rows = []
    for q in QUERIES:
        if split and q["split"] != split:
            continue
        hits = search(q["query"], q["project"])
        labels = [ids.get(h["id"], "?") for h in hits]
        gold = set(q["gold"])
        r = {"id": q["id"], "query": q["query"], "tag": q["tag"], "split": q["split"], "gold": sorted(gold), "returned": labels[:5]}
        if gold:
            rank = next((i + 1 for i, lab in enumerate(labels) if lab in gold), None)
            r.update(rank=rank, hit1=rank == 1, hit3=bool(rank and rank <= 3), rr=(1.0 / rank) if rank else 0.0,
                     returned_superseded=bool(SUPERSEDED & set(labels)))
        else:
            r.update(false_positive=bool(labels))
        rows.append(r)
    ans = [r for r in rows if r["gold"]]
    neg = [r for r in rows if not r["gold"]]

    def rate(xs, k):
        return round(sum(1 for x in xs if x[k]) / len(xs), 3) if xs else None
    by_tag: dict[str, dict] = {}
    for tag in sorted({r["tag"] for r in rows}):
        sub = [r for r in rows if r["tag"] == tag]
        if sub[0]["gold"]:
            by_tag[tag] = {"n": len(sub), "hit@1": rate(sub, "hit1"), "hit@3": rate(sub, "hit3")}
        else:
            by_tag[tag] = {"n": len(sub), "false_positive_rate": rate(sub, "false_positive")}
    return {"n_answerable": len(ans), "n_unanswerable": len(neg), "hit@1": rate(ans, "hit1"), "hit@3": rate(ans, "hit3"),
            "mrr": round(sum(r["rr"] for r in ans) / len(ans), 3) if ans else None,
            "false_positive_rate": rate(neg, "false_positive"),
            "superseded_returned": sum(1 for r in ans if r["returned_superseded"]), "by_tag": by_tag, "rows": rows}


def run(split: str | None = None, **search_kw) -> dict:
    """Evaluate the real `PersonalMemory.search` on a fresh in-memory guest database."""
    from jarvis.memory.personal import PersonalMemory
    mem = PersonalMemory.guest()
    try:
        ids = build(mem)
        return evaluate(lambda q, p: mem.search(q, limit=5, project=p, touch=False, **search_kw), ids, split)
    finally:
        mem.close()
