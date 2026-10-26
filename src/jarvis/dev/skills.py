"""Workflow skills: a saved, named, versioned Task (a list of tool calls with checks) that you can review and run again.

A skill is NOT code and NOT a permission. It is a list of ordinary tool calls; running it sends every step through the same executor as
anything else (validation, your category switches, your confirmation for each consequential step, audit, timeouts, the emergency stop).
Saving a skill does not approve anything it contains, and a skill can only call tools that exist; it cannot call the plugin / skill
management commands (those are not tools).

Lifecycle: create (a new numbered version, DRAFT) -> review (you read every step with its risk; the review is bound to the exact steps) ->
activate -> run. Old versions are never overwritten; `rollback` returns to the previous activated version.
What 'verified' means here: a skill's status is computed ONLY from runs recorded by `run`, and a run counts as verified only when the task
runner reported every step VERIFIED by its own checks. Registering or reviewing a skill proves nothing about whether it works."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

from jarvis.desktop.task import Task, TaskError

NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,40}$")
MANAGEMENT_WORDS = ("plugin", "skill", "permission", "setting", "policy")


class SkillError(ValueError):
    pass


def steps_hash(steps: list) -> str:
    return hashlib.sha256(json.dumps(steps, sort_keys=True, default=str).encode()).hexdigest()


class Skills:
    def __init__(self, profile_dir: Path):
        self.root = Path(profile_dir) / "skills"

    def _state(self) -> dict:
        try:
            s = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, s: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
        os.replace(tmp, self.root / "state.json")

    def _vfile(self, name: str, v: int) -> Path:
        if not NAME_RE.match(name):
            raise SkillError("a skill name is 2-41 characters: a lowercase letter, then lowercase letters, digits, - or _")
        return self.root / name / f"v{int(v)}.json"

    def versions(self, name: str) -> list[int]:
        d = self.root / name
        return sorted(int(m.group(1)) for f in d.glob("v*.json") if (m := re.fullmatch(r"v(\d+)\.json", f.name))) if d.is_dir() and NAME_RE.match(name) else []

    def load(self, name: str, v: int) -> dict:
        try:
            return json.loads(self._vfile(name, v).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SkillError(f"{name} v{v} cannot be read ({type(e).__name__})") from e

    def _write(self, rec: dict) -> None:
        f = self._vfile(rec["name"], rec["version"])
        f.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(f.parent), prefix=".v-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=1)
        os.replace(tmp, f)

    # ---------------------------------------------------------------- create
    def create(self, name: str, task: dict, description: str = "", source: str = "", from_run: dict | None = None) -> dict:
        """New DRAFT version from a task definition. `from_run` (a history record) is stored as the provenance if it really is a completed run."""
        if not NAME_RE.match(name):
            raise SkillError("a skill name is 2-41 characters: a lowercase letter, then lowercase letters, digits, - or _")
        try:
            t = Task.from_dict(task)
        except TaskError as e:
            raise SkillError(f"not a valid task: {e}") from e
        for s in t.steps:
            if any(w in s.tool.lower() for w in MANAGEMENT_WORDS):
                raise SkillError(f"step '{s.tool}' looks like a management action; skills cannot change plugins, skills, permissions or settings")
        prov = None
        if from_run is not None:
            if from_run.get("status") != "completed":
                raise SkillError(f"that run's status was '{from_run.get('status')}', not 'completed'; a skill is only assembled from a run that completed and verified")
            if from_run.get("goal", "") != t.goal:
                raise SkillError("that run was for a different goal than this task")
            prov = {"run_id": from_run.get("id"), "status": from_run.get("status"), "at": from_run.get("at")}
        steps = json.loads(json.dumps(task["steps"]))
        v = (self.versions(name) or [0])[-1] + 1
        rec = {"name": name, "version": v, "description": description[:300] or t.goal[:300], "goal": t.goal, "steps": steps, "steps_hash": steps_hash(steps),
               "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source[:300], "provenance_run": prov, "reviewed": None, "runs": []}
        self._write(rec)
        st = self._state()
        st.setdefault(name, {"active": None, "history": []})
        self._save_state(st)
        return rec

    # ---------------------------------------------------------------- review / activate
    def review(self, name: str, v: int, runner, approve: bool = False) -> dict:
        """Preview every step through the executor (nothing is done). `approve=True` records that YOU read it; it is bound to the steps' hash."""
        rec = self.load(name, v)
        if steps_hash(rec["steps"]) != rec["steps_hash"]:
            raise SkillError("the saved steps no longer match their recorded hash (the file was edited); review refused")
        t = Task.from_dict({"goal": rec["goal"], "steps": rec["steps"]})
        prev = runner.preview(t)
        problems = [f"step {p['step']}: {p.get('problem') or 'blocked'}" for p in prev if not p.get("ok")]
        info = {"steps": prev, "problems": problems, "max_risks": sorted({p.get("risk", "?") for p in prev})}
        if approve:
            if problems:
                raise SkillError("cannot approve: " + "; ".join(problems))
            rec["reviewed"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "steps_hash": rec["steps_hash"], "risks": info["max_risks"]}
            self._write(rec)
        return info

    def activate(self, name: str, v: int) -> None:
        rec = self.load(name, v)
        if not rec.get("reviewed") or rec["reviewed"]["steps_hash"] != steps_hash(rec["steps"]):
            raise SkillError("not activated: this version has not been reviewed (or its steps changed since). Run `jarvis skill review NAME --version N --approve`")
        st = self._state()
        s = st.setdefault(name, {"active": None, "history": []})
        if s["active"] and s["active"] != v:
            s["history"].append(s["active"])
        s["active"] = v
        self._save_state(st)

    def deactivate(self, name: str) -> None:
        st = self._state()
        s = st.get(name)
        if not s or not s["active"]:
            raise SkillError(f"{name} is not active")
        s["history"].append(s["active"])
        s["active"] = None
        self._save_state(st)

    def rollback(self, name: str) -> int:
        st = self._state()
        s = st.get(name)
        if not s:
            raise SkillError(f"unknown skill {name}")
        for v in reversed(s["history"]):
            if v == s["active"]:
                continue
            try:
                rec = self.load(name, v)
            except SkillError:
                continue
            if rec.get("reviewed") and rec["reviewed"]["steps_hash"] == steps_hash(rec["steps"]):
                if s["active"]:
                    s["history"].append(s["active"])
                s["active"] = v
                self._save_state(st)
                return v
        raise SkillError("no earlier reviewed version to go back to; nothing was changed")

    # ---------------------------------------------------------------- run / status
    def run(self, name: str, runner) -> tuple[object, dict]:
        st = self._state().get(name)
        if not st or not st["active"]:
            raise SkillError(f"{name} is not active; review and activate a version first")
        v = st["active"]
        rec = self.load(name, v)
        if steps_hash(rec["steps"]) != rec["steps_hash"] or not rec.get("reviewed") or rec["reviewed"]["steps_hash"] != rec["steps_hash"]:
            raise SkillError("this version's steps changed after review; it will not run")
        task = Task.from_dict({"goal": rec["goal"], "steps": rec["steps"]})
        rep = runner.run(task)
        run = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": rep.status, "all_steps_verified": rep.status == "completed", "task_id": task.id}
        rec = self.load(name, v)
        rec["runs"] = (rec.get("runs", []) + [run])[-50:]
        self._write(rec)
        return rep, run

    def status_text(self, name: str, v: int) -> str:
        rec = self.load(name, v)
        runs = rec.get("runs", [])
        good = sum(1 for r in runs if r.get("all_steps_verified"))
        rv = "reviewed by you" if rec.get("reviewed") and rec["reviewed"]["steps_hash"] == steps_hash(rec["steps"]) else "NOT reviewed"
        if not runs:
            return f"{rv}; never run, so NOT verified"
        last = runs[-1]
        return f"{rv}; {good} of {len(runs)} recorded run(s) had every step verified by its checks; last run: {last['status']}"

    def list(self) -> list[dict]:
        st = self._state()
        out = []
        for name in sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.is_dir() else []:
            vs = self.versions(name)
            if not vs:
                continue
            act = st.get(name, {}).get("active")
            out.append({"name": name, "versions": vs, "active": act, "status": self.status_text(name, act or vs[-1]), "description": self.load(name, vs[-1])["description"]})
        return out
