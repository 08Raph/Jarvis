"""The window's brain, with no GUI toolkit in it. Every operation calls existing Jarvis code (LocalAssistant, PersonalMemory, MissionStore /
MissionRunner / Scheduler, modes, Settings, EmergencyStop); this class only moves results between them and the view.

State is changed on the "interface thread" only (callbacks are delivered through BackgroundTasks.dispatch). Anything slow runs on a worker.
Confirmation requests reach the person through the view; if there is no view, or it does not answer in time, the answer is NO (fail closed)."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jarvis.app import diagnostics, status
from jarvis.app.lifecycle import InstanceLock
from jarvis.app.transcript import Transcript
from jarvis.app.worker import BackgroundTasks, CancelToken
from jarvis.config import Config

CONFIRM_TIMEOUT_S = 300.0


@dataclass
class Ask:
    """A confirmation the app itself needs (not a tool): same shape the view already renders for tool confirmations."""
    description: str
    tool: str = "app"
    risk: str = "destructive"
    category: str = "app"
    args: dict | None = None
    fingerprint: str = ""


def explain_error(exc) -> str:
    """Plain-language, actionable text for the failures a first-time user can actually hit. Accepts an exception or the 'Name: message' text a
    worker reports. Other details are not shown, to avoid leaking personal data."""
    if isinstance(exc, str):
        name, _, msg = exc.partition(": ")
        exc = MemoryError() if name == "MemoryError" else exc
    else:
        name, msg = type(exc).__name__, str(exc)
    if name == "NoActiveModel":
        return msg
    if name == "IntegrityError":
        return msg + "  Run `jarvis models verify`, or reinstall the model with `jarvis setup`."
    if name == "InferenceError":
        return msg + "  Try again; if it keeps happening, restart the app or pick a smaller model."
    if name == "RuntimeUnavailable":
        return msg + "  The local AI runtime or the model file could not be loaded. See docs/LOCAL_MODEL.md and run `jarvis runtime`."
    if isinstance(exc, MemoryError):
        return "Not enough memory to load the model. Close other programs, or install a smaller model."
    if name in ("OSError", "ValueError", "KeyError", "FileNotFoundError", "PermissionError", "MissionError", "ScheduleError", "StateError", "PlanError", "MemoryDatabaseError"):
        from jarvis.app.diagnostics import scrub_paths
        from jarvis.disclosure import scrub_error_text
        return scrub_paths(f"{name}: {scrub_error_text(msg)}")
    return f"Unexpected problem ({name}). Details were not shown to avoid leaking personal data; see the diagnostics page."


class AppController:
    def __init__(self, cfg: Config, *, guest: bool = False, engine_factory=None, dispatch: Callable | None = None,
                 on_update: Callable[[str], None] | None = None, ask_user: Callable | None = None, simulate: bool = False,
                 speaker_factory: Callable | None = None, mic_factory: Callable | None = None, recognizer_factory: Callable | None = None, startup_backend=None):
        self.cfg, self.guest, self.simulate = cfg, guest, simulate
        self.engine_factory = engine_factory
        self.tasks = BackgroundTasks(dispatch)
        self.transcript = Transcript()
        self.on_update = on_update or (lambda topic: None)
        self.ask_user = ask_user                    # view hook: ask_user(request, resolve) -> shows a dialog on the interface thread
        self.assistant = None
        from jarvis.app.speech import SpeechOut
        from jarvis.personality.persona import Persona
        self.speech = SpeechOut(cfg, speaker_factory=speaker_factory)     # spoken replies: off by default, never opens a microphone
        self.persona = Persona(cfg.profile_dir, persist=not guest)    # Normal unless the user chose otherwise; guest sessions never save
        self._mic_factory = mic_factory
        self._startup_backend = startup_backend
        self._rec_factory = recognizer_factory
        self.voice_thread_leaked = False
        self.voice = None                           # hands-free VoiceService, created on first use
        self.busy = False                           # a chat reply is being produced
        self.loading = False
        self.lock = InstanceLock.for_profile(cfg)
        self.closed = False
        self._gen_token: CancelToken | None = None
        self._pending: list[tuple[threading.Event, dict]] = []
        self._plock = threading.Lock()
        self.mission_progress: dict[str, list[str]] = {}
        self.running_missions: set[str] = set()

    # ------------------------------------------------------------------ plumbing
    def _ui(self, fn: Callable[[], None]) -> None:
        self.tasks._post(fn)

    def _notify(self, topic: str) -> None:
        self._ui(lambda: self.on_update(topic))

    def start(self) -> bool:
        """Take the one-window-per-profile lock. False if another window already uses this profile."""
        if self.guest:
            return True
        return self.lock.acquire()

    # ------------------------------------------------------------------ confirmation bridge (worker thread -> person -> worker thread)
    def confirm(self, req) -> bool:
        """Used as the executor's confirm callback. Blocks the WORKER (never the window) until answered. No view, timeout, or shutdown = NO."""
        if self.ask_user is None or self.closed:
            return False
        ev, box = threading.Event(), {"ok": False}

        def resolve(ok: bool) -> None:
            box["ok"] = bool(ok)
            ev.set()
        with self._plock:
            self._pending.append((ev, box))

        def show():
            self.transcript.add("confirm", getattr(req, "description", str(req)), tool=getattr(req, "tool", ""), risk=getattr(req, "risk", ""))
            self.on_update("transcript")
            try:
                self.ask_user(req, resolve)
            except Exception:  # noqa: BLE001 - a broken dialog must mean "no"
                resolve(False)
        self._ui(show)
        ev.wait(CONFIRM_TIMEOUT_S)
        with self._plock:
            self._pending = [p for p in self._pending if p[0] is not ev]
        ok = box["ok"] and ev.is_set() and not self.closed

        def record():
            self.transcript.add("system", "You allowed that action." if ok else "That action was NOT allowed (declined, closed or timed out).")
            self.on_update("transcript")
        self._ui(record)
        return ok

    # ------------------------------------------------------------------ status
    def readiness(self) -> dict:
        """Layered, measured readiness for the Status page and banner (never loads a model, opens a microphone or uses the network)."""
        from jarvis.app import readiness as R
        return {"headline": R.model_headline(self.cfg), "model": R.model_levels(self.cfg), "voice": R.voice_levels(self.cfg)}

    def model_status(self) -> status.ModelStatus:
        return status.model_status(self.cfg)

    def features(self) -> list[dict]:
        return status.feature_rows(self.cfg)

    def diagnostics_text(self) -> str:
        return diagnostics.render_text(diagnostics.collect(self.cfg))

    # ------------------------------------------------------------------ setup wizard and diagnostics (Prompt 20)
    def setup_engine(self):
        from jarvis.app.setup_engine import SetupEngine
        from jarvis.models.manager import ModelStore
        from jarvis.net.policy import NetworkPolicy
        policy = NetworkPolicy.load(self.cfg.settings_path)
        return SetupEngine(self.cfg, ModelStore(self.cfg.models_dir, policy), policy)

    def needs_setup(self) -> bool:
        if self.guest or self.simulate:
            return False
        try:
            return self.setup_engine().needs_setup()
        except Exception:  # noqa: BLE001 - if the check itself fails, offer setup rather than hide it
            return True

    def setup_preview(self) -> dict:
        """Steps 1-2 only (no network, no model load): what this PC can do and which model is recommended."""
        eng = self.setup_engine()
        return {"system": eng.check_system(), "model": eng.recommend()}

    def run_setup(self, model_id: str | None, progress: Callable[[str, float | None, str], None], on_done: Callable[[dict], None]) -> bool:
        """Runs the whole wizard on a worker thread. Calling this IS the person's consent to download (the page showed size and licence first)."""
        class _Cancel:                      # the engine only asks `is_set()`; adapt the task's cancel token
            def __init__(self, token):
                self.t = token

            def is_set(self):
                return self.t.cancelled

        def work(token):
            return self.setup_engine().run(model_id, confirm_download=lambda info: True, cancel=_Cancel(token),
                                           progress=lambda s, f, m: self._ui(lambda: progress(s, f, m)))

        def done(res, err):
            on_done(res if isinstance(res, dict) else {"failed": {"step": "unexpected", "message": err or "setup ended without a result"}, "completed": False})
        return self.tasks.submit("setup", work, done) is not None

    def cancel_setup(self) -> None:
        self.tasks.cancel_all()

    def export_diagnostic_report(self, dest) -> str:
        return str(diagnostics.export_report(self.cfg, dest))

    # ------------------------------------------------------------------ chat
    def _ensure_assistant(self):
        if self.assistant is None:
            from jarvis.assistant import LocalAssistant
            from jarvis.memory.personal import PersonalMemory
            mem = PersonalMemory.guest() if self.guest else None
            if not self.guest:
                self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
                mem = PersonalMemory.open(self.cfg.memory_db, any_thread=True)      # created on a worker, closed on the interface thread
            try:
                self.assistant = LocalAssistant(self.cfg, engine_factory=self.engine_factory, guest=self.guest, memory=mem)
            except BaseException:
                mem.close()                                  # model missing/corrupt: do not leave the database open
                raise
            self.assistant.persona = self.persona            # one shared persona: the window and the model path always agree
        return self.assistant

    def send(self, text: str) -> bool:
        """Start a reply. Returns False (with an explanation in the transcript) if it cannot start. Nothing is faked: with no working model,
        the transcript shows the actionable reason and no assistant text."""
        text = (text or "").strip()
        if not text:
            return False
        if self.busy or self.closed:
            self.transcript.add("system", "Jarvis is still answering the previous message. Press Stop to cancel it.")
            self.on_update("transcript")
            return False
        if len(text) > 8000:
            self.transcript.add("error", "That message is too long (8000 characters at most).")
            self.on_update("transcript")
            return False
        ms = self.model_status() if self.assistant is None else None
        self.transcript.add("user", text)
        if ms is not None and not ms.ok and self.engine_factory is None:
            self.transcript.add("error", ms.summary + ("  " + ms.action if ms.action else ""))
            self.on_update("transcript")
            return False
        self.busy, self.loading = True, self.assistant is None
        entry = self.transcript.add("assistant", "")
        self.on_update("transcript")
        self.on_update("busy")

        def work(token: CancelToken):
            asst = self._ensure_assistant()
            self._ui(lambda: setattr(self, "loading", False))
            parts = 0
            for piece in asst.reply_stream(text):
                if token.cancelled:
                    break
                parts += 1
                self._ui(lambda p=piece: (self.transcript.append_to(entry.id, p), self.on_update("transcript")))
            turn = asst.last_turn
            return {"stopped": token.cancelled, "parts": parts, "notice": turn.notice if turn else "", "rate": turn.speech_rate if turn else 1.0}

        def done(result, err):
            self.busy = self.loading = False
            if err:
                self.transcript.entries = [e for e in self.transcript.entries if e.id != entry.id or e.text]      # drop the empty reply placeholder
                self.transcript.add("error", explain_error(err))
            elif result and result["stopped"]:
                self.transcript.add("system", "Stopped. The reply above is only what was produced before you pressed Stop.")
            elif result and not result["parts"]:
                self.transcript.add("error", "The model returned no text.")
            if result and not err and not result["stopped"] and result["parts"] and not self.closed:
                final = next((e.text for e in self.transcript.entries if e.id == entry.id), "")
                if final and not self.speech.speak(final, result.get("rate", 1.0)) and self.speech.last_error:
                    self.transcript.add("error", self.speech.last_error)
                    self.on_update("transcript")
            if result and result.get("notice"):
                self.transcript.add("system", result["notice"])
                self.on_update("persona")
            self.on_update("transcript")
            self.on_update("busy")
        t = self.tasks.submit("chat", work, done)
        if t is None:
            self.busy = False
            return False
        self._gen_token = t.token
        return True

    def stop_generation(self) -> bool:
        self.stop_speaking()
        if self._gen_token is not None and self.busy:
            self._gen_token.cancel()
            return True
        return False

    def new_conversation(self) -> None:
        if self.busy:
            return
        if self.assistant is not None:
            self.assistant.reset()
        self.transcript.clear()
        self.on_update("transcript")

    # ------------------------------------------------------------------ missions (existing store/runner/service)
    def _store(self):
        from jarvis.missions.store import MissionStore
        return MissionStore(self.cfg.profile_dir)

    def _runner(self):
        from jarvis.desktop import cli_desktop
        from jarvis.missions.runner import MissionRunner
        env, ex = cli_desktop.build(self.cfg, self.guest, self.simulate, confirm=self.confirm)
        return MissionRunner(ex, self._store(), progress=self._progress_cb), ex

    def _progress_cb(self, line: str) -> None:
        def add():
            self.mission_progress.setdefault("_all", []).append(line)
            self.mission_progress["_all"] = self.mission_progress["_all"][-200:]
            self.on_update("missions")
        self._ui(add)

    def missions(self) -> list[dict]:
        if self.guest:
            return []
        st = self._store()
        st.recover_all()
        return st.list()

    def mission_detail(self, mid: str) -> dict:
        """What the person needs to decide: the plan rows (a PROPOSAL until it has run), the recorded summary, and what is allowed next."""
        from jarvis.missions import model as M
        st = self._store()
        m = st.load(mid)
        runner, ex = self._runner()
        conseq = runner.is_consequential(m)
        return {"id": m["id"], "state": m["state"], "goal": m["goal"], "plan_hash": m["plan_hash"], "tampered": bool(m.get("tampered")),
                "consequential": conseq, "rows": runner.review(m), "summary": M.summarize(m),
                "executions": len(m["executions"]), "running": mid in self.running_missions or st.is_running_elsewhere(mid),
                "can_approve": m["state"] in ("draft", "awaiting_approval") and not m.get("tampered"),
                "can_start": (m["state"] in ("draft", "approved", "paused", "failed") and not m.get("tampered")
                              and (not conseq or m["state"] in ("approved", "paused", "failed"))),
                "can_pause": st.is_running_elsewhere(mid), "can_cancel": m["state"] not in ("completed", "cancelled"),
                "hash_hint": m["plan_hash"][:12]}

    def mission_from_file(self, path) -> dict:
        """Save a plan file (JSON) as a DRAFT after validation. Nothing runs."""
        from jarvis.missions import service
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ValueError(f"cannot read the plan file: {e}") from None
        runner, ex = self._runner()
        m = service.save_draft(self._store(), ex, self.cfg.profile_dir, raw, "user_file")
        self._notify("missions")
        return {"id": m["id"], "state": m["state"]}

    def mission_plan(self, goal: str) -> bool:
        """Ask the LOCAL model for a draft plan (worker thread). The model only proposes; the draft is validated and stored, never run or approved."""
        if self.busy or self.closed:
            return False
        self.busy = True
        self.on_update("busy")

        def work(token):
            from jarvis.missions import service
            from jarvis.missions.planner import propose
            asst = self._ensure_assistant()
            runner, ex = self._runner()
            defn, info = propose(lambda msgs: asst.engine.chat(msgs, 1024, 0.2), goal, ex)
            m = service.save_draft(self._store(), ex, self.cfg.profile_dir, defn, "model")
            return m["id"]

        def done(mid, err):
            self.busy = False
            if err:
                self.transcript.add("error", "The model did not produce a valid plan. Nothing was saved or run. " + explain_error(err))
            else:
                self.transcript.add("system", f"Draft plan {mid} saved. It is a PROPOSAL: review it on the Missions page; nothing has run.")
            self.on_update("transcript")
            self.on_update("missions")
            self.on_update("busy")
        return self.tasks.submit("plan", work, done) is not None

    def mission_review(self, mid: str) -> dict:
        from jarvis.missions import service
        runner, ex = self._runner()
        service.mark_reviewed(self._store(), runner, self._store().load(mid))
        return self.mission_detail(mid)

    def mission_approve(self, mid: str, hash_prefix: str) -> str:
        from jarvis.missions import service
        runner, ex = self._runner()
        service.approve(self._store(), runner, mid, hash_prefix)
        self._notify("missions")
        return "Approved for ONE start. Every action still follows your permission switches and asks you when it runs."

    def mission_reject(self, mid: str) -> str:
        from jarvis.missions import service
        service.reject(self._store(), mid)
        self._notify("missions")
        return "Rejected. Nothing was run."

    def mission_pause(self, mid: str) -> str:
        from jarvis.missions import service
        return service.request_pause(self._store(), mid)

    def mission_cancel(self, mid: str) -> str:
        from jarvis.missions import service
        msg = service.request_cancel(self._store(), mid)
        self._notify("missions")
        return msg

    def mission_start(self, mid: str, decisions: dict | None = None) -> bool:
        """Runs on a worker so the window stays responsive. The runner enforces approval, permissions at every step, and confirmations."""
        if mid in self.running_missions or self.closed:
            return False
        self.running_missions.add(mid)

        def work(token):
            runner, ex = self._runner()
            return runner.run(mid, trigger="manual", decisions=decisions or {})

        def done(result, err):
            self.running_missions.discard(mid)
            from jarvis.missions import model as M
            if err:
                self.transcript.add("error", f"Mission {mid} did not start or stopped: {explain_error(err)}")
            else:
                self.transcript.add("tool", M.summarize(result), mission=mid, state=result["state"])
            self.on_update("transcript")
            self.on_update("missions")
        t = self.tasks.submit(f"mission-{mid}", work, done)
        self._notify("missions")
        return t is not None

    # ------------------------------------------------------------------ schedules
    def schedules(self) -> list[dict]:
        if self.guest:
            return []
        from jarvis.missions.schedule import Scheduler
        return Scheduler(self.cfg.profile_dir, self._store()).list()

    def schedule_set_state(self, sid: str, state: str) -> None:
        from jarvis.missions.schedule import Scheduler
        if state not in ("active", "paused", "cancelled"):
            raise ValueError("state must be active, paused or cancelled")
        Scheduler(self.cfg.profile_dir, self._store()).set_state(sid, state, "changed in the window")
        self._notify("schedules")

    def schedule_delete(self, sid: str) -> None:
        from jarvis.missions.schedule import Scheduler
        Scheduler(self.cfg.profile_dir, self._store()).delete_schedule(sid)
        self._notify("schedules")

    def schedule_add(self, mission_id: str, kind: str, *, tz: str, at: str = "", time_: str = "", weekdays: str = "", every_minutes: int = 0,
                     missed: str = "skip") -> dict:
        from jarvis.missions.schedule import Scheduler
        runner, ex = self._runner()
        s = Scheduler(self.cfg.profile_dir, self._store()).create(mission_id, kind, tz=tz or None, at=at or None, time_=time_ or None,
                                                                   weekdays=weekdays or None, every_minutes=every_minutes or None, missed=missed, specs=ex.specs)
        self._notify("schedules")
        return s

    def schedule_approve(self, sid: str, hash_prefix: str, max_runs: int | None, days: int | None) -> dict:
        from jarvis.missions.schedule import Scheduler
        ap = Scheduler(self.cfg.profile_dir, self._store()).approve(sid, hash_prefix, max_runs, days)
        self._notify("schedules")
        return ap

    def run_due_schedules(self) -> bool:
        """Check once and run what is due. Called by the window's timer ONLY while its 'run due schedules while this window is open' switch is on."""
        if self.guest or self.closed:
            return False

        def work(token):
            from jarvis.missions.schedule import Scheduler
            runner, ex = self._runner()
            sch = Scheduler(self.cfg.profile_dir, self._store())
            sch.recover_claims()
            return sch.tick(runner)

        def done(events, err):
            if err:
                self.transcript.add("error", "Schedule check failed: " + explain_error(err))
            for e in events or []:
                self.transcript.add("system", f"Schedule {e['schedule']}: {e['status']} - {e.get('detail', '')}")
            self.on_update("transcript")
            self.on_update("schedules")
        return self.tasks.submit("schedules", work, done) is not None

    # ------------------------------------------------------------------ memory
    def _memory(self):
        from jarvis.memory.personal import PersonalMemory
        if self.guest:
            raise ValueError("Guest sessions save nothing, so there is no memory to manage.")
        self.cfg.profile_dir.mkdir(parents=True, exist_ok=True)
        return PersonalMemory.open(self.cfg.memory_db)

    def memory_items(self, category: str | None = None) -> list[dict]:
        mem = self._memory()
        try:
            return mem.list(category=category)
        finally:
            mem.close()

    def memory_update(self, mid: int, text: str) -> dict:
        mem = self._memory()
        try:
            return mem.update(mid, text)
        finally:
            mem.close()

    def memory_delete(self, mid: int) -> bool:
        mem = self._memory()
        try:
            return mem.delete(mid)
        finally:
            mem.close()

    def memory_export(self, out: Path) -> Path:
        mem = self._memory()
        try:
            Path(out).write_text(json.dumps(mem.export(), indent=2, default=str), encoding="utf-8")
        finally:
            mem.close()
        return Path(out)

    def memory_forget_all(self) -> str | None:
        """Asks first (through the same confirmation bridge); returns the result text, or None if not confirmed. Blocks: call from a worker."""
        from jarvis.memory.cli_memory import forget_all_profile
        if not self.confirm(Ask("Delete EVERYTHING saved in this profile: memories, saved conversations, notes, action logs, missions, schedules and screenshots. "
                                "This cannot be undone.")):
            return None
        mem = self._memory()
        try:
            return forget_all_profile(self.cfg, mem)
        finally:
            mem.close()

    # ------------------------------------------------------------------ safety: permissions (read-only here), modes, emergency stop, action history
    def permissions(self) -> dict:
        from jarvis.desktop import cli_desktop
        from jarvis.desktop.policy import CATEGORIES, CONSENT_REQUIRED, Settings
        s = Settings.load(cli_desktop._settings_path(self.cfg)) if not self.guest else Settings()
        rows = [{"category": c, "description": d, "enabled": bool(s.enabled.get(c)), "needs_consent": c in CONSENT_REQUIRED,
                 "consent_recorded": bool(s.consents.get(c))} for c, d in CATEGORIES.items()]
        return {"rows": rows, "allowed_folders": list(s.allowed_folders), "project_folders": list(s.project_folders),
                "how_to_change": "Permissions are changed in a console with `jarvis desktop enable <name>` / `disable <name>` (some show a notice and ask you to type yes). "
                                 "This window shows them but deliberately cannot change them, so nothing here can grant itself access."}

    # ------------------------------------------------------------------ personality (style only; never touches permissions or the executor)
    # ------------------------------------------------------------------ spoken replies (speaker only; the window never uses the microphone)
    def stop_speaking(self) -> None:
        self.speech.stop()
        if self.voice is not None:
            self.voice.cancel()                       # also abandons a voice reply still being produced and returns to listening

    # ------------------------------------------------------------------ hands-free voice (Prompt 17; the service owns the microphone)
    def _voice_service(self):
        if self.voice is None:
            from jarvis.app.voice_service import VoiceService
            self.voice = VoiceService(self.cfg, responder=self._voice_respond, speech=self.speech, guest=self.guest,
                                      on_change=lambda: self._notify("voice"), on_user=self._voice_user, on_reply=self._voice_reply,
                                      mic_factory=self._mic_factory, recognizer_factory=self._rec_factory,
                                      begin_turn=self._voice_begin, end_turn=self._voice_end)
        return self.voice

    def _voice_begin(self) -> bool:
        with self._plock:
            if self.busy or self.closed:
                return False
            self.busy = True
            return True

    def _voice_end(self) -> None:
        self.busy = False
        self._notify("busy")

    def _voice_respond(self, text: str, cancel) -> tuple[str, float]:
        """Runs on a helper thread. The SAME assistant (persona, safety, memory rules) as typed chat; no tool or executor is reachable from here."""
        if self.assistant is None and self.engine_factory is None:
            ms = self.model_status()
            if not ms.ok:
                return ms.summary + ("  " + ms.action if ms.action else ""), 1.0
        asst = self._ensure_assistant()
        parts: list[str] = []
        for piece in asst.reply_stream(text):
            if cancel.is_set():
                break
            parts.append(piece)
        turn = asst.last_turn
        notice = turn.notice if turn else ""
        if notice:
            self._ui(lambda: (self.transcript.add("system", notice), self.on_update("transcript"), self.on_update("persona")))
        return "".join(parts), (turn.speech_rate if turn else 1.0)

    def _voice_user(self, text: str) -> None:
        self._ui(lambda: (self.transcript.add("user", "(voice) " + text), self.on_update("transcript")))

    def _voice_reply(self, text: str) -> None:
        if text:
            self._ui(lambda: (self.transcript.add("assistant", text), self.on_update("transcript")))

    def voice_autostart(self) -> dict | None:
        """Called once by the window after it is shown. Starts hands-free voice ONLY if the user enabled it earlier; otherwise the microphone stays untouched.
        Idempotent: calling it again while voice runs does nothing."""
        if self.guest or self.closed:
            return None
        svc = self._voice_service()
        if not svc.enabled():
            return svc.state()
        return svc.start()

    def voice_enable(self, mode: str = "wake_word") -> dict:
        return self._voice_service().enable(mode)

    def voice_disable(self) -> dict:
        return self._voice_service().disable()

    def voice_mute(self) -> dict:
        return self._voice_service().mic_mute()

    def voice_unmute(self) -> dict:
        return self._voice_service().mic_unmute()

    def voice_speaker_mute(self, on: bool) -> dict:
        return self._voice_service().speaker_mute(on)

    def voice_retry(self) -> dict:
        return self._voice_service().retry()

    def voice_state(self) -> dict:
        return self._voice_service().state()

    def voice_setting(self, **kw) -> dict:
        """Speaking speed / volume for hands-free speech (validated, saved with the voice settings)."""
        if self.guest:
            raise ValueError("Voice settings are not changed in a guest session.")
        bad = set(kw) - {"rate", "volume"}
        if bad:
            raise ValueError(f"unknown voice option(s): {sorted(bad)}")
        self.speech.update(**kw)
        svc = self._voice_service()
        if svc.alive() and svc._session is not None:
            vs = svc.settings()
            svc._session.s.rate, svc._session.s.volume = vs.rate, vs.volume      # takes effect on the next spoken reply
        self._notify("voice")
        return svc.state()

    # ------------------------------------------------------------------ optional start-with-Windows (separate from voice; explicit consent)
    def startup_state(self) -> dict:
        from jarvis.app.startup import Startup
        return Startup(backend=self._startup_backend).status()

    def startup_enable(self, confirmed: bool = False) -> str:
        from jarvis.app.startup import Startup
        if self.guest:
            raise ValueError("Start with Windows is not changed in a guest session.")
        Startup(backend=self._startup_backend).enable(confirm=confirmed)
        self._notify("voice")
        return "Jarvis will open when you sign in to Windows. The microphone stays off unless hands-free voice is ON."

    def startup_disable(self) -> str:
        from jarvis.app.startup import Startup
        Startup(backend=self._startup_backend).disable()
        self._notify("voice")
        return "Jarvis will no longer start when you sign in to Windows."

    def voice_ptt(self, down: bool) -> None:
        svc = self._voice_service()
        svc.ptt_press() if down else svc.ptt_release()

    def speech_state(self) -> dict:
        vs = self.speech.settings()
        ok, why = self.speech.available()
        return {"speak_in_window": vs.speak_in_window, "engine_available": ok, "engine_note": why, "speaking": self.speech.is_speaking(),
                "rate": vs.rate, "volume": vs.volume, "microphone": "only while hands-free voice is turned on (Voice page)"}

    def voice_set(self, **kw) -> str:
        """Change window speech settings (speak_in_window, rate, volume). Validated; the microphone setting is not reachable from here."""
        if self.guest:
            raise ValueError("Voice settings are not changed in a guest session.")
        vs = self.speech.update(**kw)
        if not vs.speak_in_window:
            self.speech.stop()
        self._notify("persona")
        return "Spoken replies are " + ("ON." if vs.speak_in_window else "off.") + (" " + self.speech.available()[1] if vs.speak_in_window and not self.speech.available()[0] else "")

    def persona_state(self) -> dict:
        from dataclasses import asdict
        return {"indicator": self.persona.indicator(), "styles": self.persona.listing(), "settings": asdict(self.persona.settings),
                "note": self.persona.load_note, "default": "normal"}

    def persona_set(self, style: str) -> str:
        msg = self.persona.set_style(style)
        self._notify("persona")
        return msg + ". A style changes wording only; it never grants a permission."

    def persona_normal(self) -> str:
        """The visible, reliable stop: back to Normal now, cancel any reply still being written, and silence speech."""
        self.stop_generation()
        msg = self.persona.return_to_normal()
        self._notify("persona")
        return msg

    def persona_reset(self) -> str:
        msg = self.persona.reset()
        self._notify("persona")
        return "Personality settings reset. " + msg

    def persona_level(self, name: str, value: int) -> None:
        self.persona.set_level(name, int(value))
        self._notify("persona")

    def persona_option(self, **kw) -> None:
        self.persona.set_option(**kw)
        self._notify("persona")

    def persona_adult(self, on: bool, confirmed: bool = False) -> str:
        if on and not confirmed:
            raise ValueError("confirm that you are 18 or older to turn adult-only styles on")
        self.persona.set_adult_optin(bool(on))
        self._notify("persona")
        return "Adult-only styles are " + ("available." if on else "off.")

    def persona_preview(self, style: str) -> dict:
        return self.persona.preview(style)

    def modes(self) -> dict:
        from jarvis import modes
        cur = modes.current(self.cfg.profile_dir)
        return {"current": cur.name if cur else "", "available": [modes.describe(m) for m in modes.MODES.values()]}

    def set_mode(self, name: str) -> str:
        from jarvis import modes
        if name:
            modes.set_mode(self.cfg.profile_dir, name)
            msg = f"Mode set to {name}. A mode only hides tools; it never grants a permission."
        else:
            modes.clear_mode(self.cfg.profile_dir)
            msg = "No mode selected (all tools your permissions allow)."
        self._notify("modes")
        return msg

    def emergency_stop(self) -> str:
        stop_file = self.cfg.profile_dir / "STOP"          # the same file `jarvis desktop stop` writes; every Jarvis process checks it
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop requested from the window", encoding="utf-8")
        self.stop_generation()
        self.stop_speaking()
        self.tasks.cancel_all()
        self._notify("safety")
        return ("Emergency stop is ON. Running missions and desktop tasks stop at their next check; an operation already in progress cannot be interrupted "
                "and ends by itself. Press 'Clear stop' to allow tasks to run again.")

    def clear_stop(self) -> None:
        from jarvis.desktop.stop import EmergencyStop
        EmergencyStop(self.cfg.profile_dir / "STOP").clear()
        self._notify("safety")

    def stop_active(self) -> bool:
        return (self.cfg.profile_dir / "STOP").exists()

    def action_history(self, n: int = 50) -> list[dict]:
        p = self.cfg.profile_dir / "desktop_audit.jsonl"
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------------ shutdown
    def shutdown(self, timeout: float = 5.0) -> dict:
        """Close cleanly. Pending confirmations are answered NO, running work is asked to stop, the lock is released. Returns what could not be
        stopped in time so the window can say so honestly (a mission step in progress cannot be interrupted)."""
        self.closed = True
        self.stop_speaking()
        leaked_voice = False
        if self.voice is not None:
            leaked_voice = bool(self.voice.shutdown(timeout).get("leaked_thread"))      # closes the microphone and the recognizer
        with self._plock:
            for ev, box in self._pending:
                box["ok"] = False
                ev.set()
        still = self.tasks.shutdown(timeout)
        try:
            if self.assistant is not None and not still:      # never close a database a stuck worker may still be using
                self.assistant.close()
        except Exception:  # noqa: BLE001
            pass
        self.lock.release()
        self.voice_thread_leaked = leaked_voice
        return {"still_running": still}
