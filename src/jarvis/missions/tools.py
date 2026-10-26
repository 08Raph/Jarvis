"""Read-only mission tool for the agent. The model can look at missions; it cannot create, approve, start, schedule, change or delete them (those are
command-line actions for you, so no model reply can call them)."""
from __future__ import annotations

from jarvis.desktop import schema as S
from jarvis.desktop.schema import Arg, Outcome, ToolSpec
from jarvis.missions import model as M
from jarvis.missions.store import MissionStore
from jarvis.missions.schedule import Scheduler


def h_status(a, env) -> Outcome:
    st = MissionStore(env.profile_dir)
    if a["id"]:
        try:
            m = st.load(a["id"])
        except (KeyError, M.StateError) as e:
            return Outcome(False, str(e), verified=True, evidence="looked in the missions folder")
        return Outcome(True, f"mission {m['id']} is {m['state']}", verified=True, evidence="read the saved record (proposals are not executions)",
                       data={"state": m["state"], "executions": len(m["executions"])}, untrusted_text=M.summarize(m))
    rows = st.list()
    sch = Scheduler(env.profile_dir, st).list()
    lines = [f"{r['id']} [{r['state']}] {r['goal']}" for r in rows] + [f"schedule {s['id']} ({s['kind']}, {s['state']}) for {s['mission']}" for s in sch]
    return Outcome(True, f"{len(rows)} mission(s), {len(sch)} schedule(s)", verified=True, evidence="read the missions folder", data={"missions": len(rows), "schedules": len(sch)},
                   untrusted_text="\n".join(lines) or "(none)")


def specs() -> list[ToolSpec]:
    return [ToolSpec("mission_status", "list missions and schedules, or show one mission's recorded progress (read-only; cannot create, approve, run or change anything)", "system", S.OBSERVE,
                     (Arg("id", "str", "mission id (empty = list all)", required=False, default="", max_len=40, no_newlines=True),), h_status, timeout=15, untrusted_output=True)]
