"""The safe preflight: a fixed list of read-only checks run in order, producing ONE record that the window shows and the history keeps.

Rules this file keeps (each has a test):
  * Nothing is downloaded, installed, loaded or changed. The default model, the catalog and the runtime pin are not touched. Models on disk are only listed.
  * The one online step (the model host's metadata) runs only when the caller says the person agreed; it goes through NetworkPolicy for that one action.
  * A check that cannot run is reported as UNAVAILABLE or NOT_RUN, a check that crashes as ERROR; the verdict is PASSED only when every check passed.
  * No subprocess and no shell: the checks are plain function calls, so there are no command lines to build or inject into. The "exit code" stored with
    a check is the same 0 / 3 / 4 convention the scripts use (0 passed, 3 unavailable, 4 problem, 1 crashed), not a process exit status.
  * Limits are shown as what they are: 25 GB free is CHECKED here (once); the 20 GB budget is only compared with the host's published sizes; the 115 %
    per-file ceiling and the per-operation free-space check are enforced by the download code, not here; the 13 GB RAM stop is NOT enforced by the app.
"""
from __future__ import annotations

import platform
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from jarvis import version as V
from jarvis.models import diskspace
from jarvis.preflight import history, metadata

PASS, PROBLEM, UNAVAILABLE, NOT_RUN, ERROR, INFO = "PASS", "PROBLEM", "UNAVAILABLE", "NOT_RUN", "ERROR", "INFO"
LABEL = {PASS: "OK", PROBLEM: "PROBLEM", UNAVAILABLE: "UNAVAILABLE", NOT_RUN: "NOT RUN", ERROR: "ERROR (the check itself crashed)", INFO: "INFO"}
CODE = {PASS: 0, UNAVAILABLE: 3, PROBLEM: 4, ERROR: 1, NOT_RUN: None, INFO: None}
MIN_PYTHON = (3, 11)
MIN_RAM_GB = 16.0
STATEMENTS = {"model_downloaded": False, "model_loaded": False, "model_benchmarked": False, "runtime_installed_by_this_run": False,
              "default_model_changed": False, "catalog_changed": False, "windows_exe_behaviour_validated": False}


class Probe:
    """Everything read from the machine; tests replace it with a fake."""

    def system(self) -> dict:
        return {"system": platform.system(), "release": platform.release(), "version": platform.version(), "machine": platform.machine()}

    def python(self) -> dict:
        return {"frozen": bool(getattr(sys, "frozen", False)), "implementation": platform.python_implementation(), "version": platform.python_version(),
                "bits": struct.calcsize("P") * 8}

    def ram_gb(self):
        from jarvis import hardware
        return hardware._ram_gb()

    def app_files(self) -> dict:
        from jarvis.app import diagnostics
        return {"files": diagnostics.packaged_files(), "modules": diagnostics.required_modules(False)}

    def free_bytes(self, path) -> int:
        return diskspace.free_bytes(path)

    def runtime(self) -> dict:
        from jarvis.inference.gguf_engine import runtime_info
        return runtime_info()

    def models(self, cfg) -> dict:
        from jarvis.models.manager import ModelStore
        from jarvis.net.policy import NetworkPolicy
        if not Path(cfg.models_dir).is_dir():              # look before constructing the store, so that nothing is created
            return {"installed": [], "selected": None}
        store = ModelStore(cfg.models_dir, NetworkPolicy())
        return {"installed": [m.get("id") for m in store.installed()], "selected": store.active()}


def _res(id_, title, needs_network, status, detail, **extra) -> dict:
    return {"id": id_, "title": title, "needs_internet": needs_network, "status": status, "label": LABEL[status], "exit_code": CODE[status], "detail": detail, **extra}


# ------------------------------------------------------------------ the checks (each returns a result dict; a crash is turned into ERROR by the runner)
def check_os(ctx):
    s = ctx.probe.system()
    ctx.system = s
    if s["system"] != "Windows":
        return _res("os", "Operating system", False, INFO, f"{s['system']} {s['release']} ({s['machine']}). This is NOT Windows: nothing from this run is Windows evidence.")
    try:
        build = int(str(s["version"]).split(".")[2])
    except (IndexError, ValueError):
        return _res("os", "Operating system", False, UNAVAILABLE, "Windows was detected but its build number could not be read, so Windows 10 or 11 cannot be told apart.")
    name = "Windows 11" if build >= 22000 else "Windows 10 or older"
    if str(s["machine"]).upper() not in ("AMD64", "X86_64"):
        return _res("os", "Operating system", False, PROBLEM, f"{name}, {s['machine']}: the model runtime wheels this project targets are for 64-bit Intel/AMD (win_amd64) only.")
    if build < 22000:
        return _res("os", "Operating system", False, INFO, f"{name} (build {build}), 64-bit. The Stage 5 target is Windows 11; results from this PC would not be Windows 11 evidence.")
    return _res("os", "Operating system", False, PASS, f"Windows 11 (build {build}), 64-bit.")


def check_python(ctx):
    p = ctx.probe.python()
    where = "the interpreter bundled inside the app" if p["frozen"] else "the interpreter running Jarvis"
    ver = tuple(int(x) for x in p["version"].split(".")[:2])
    txt = f"{p['implementation']} {p['version']}, {p['bits']}-bit ({where})."
    if p["bits"] != 64 or ver < MIN_PYTHON:
        return _res("python", "Python runtime", False, PROBLEM, txt + f" Jarvis needs 64-bit Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.")
    return _res("python", "Python runtime", False, PASS, txt)


def check_app_files(ctx):
    """Does this copy of Jarvis contain the files and program modules it needs? The same function feeds the diagnostics report and the self-test."""
    r = ctx.probe.app_files()
    f, m = r["files"], r["modules"]
    where = "the installed app folder" if f["mode"] == "packaged" else "a source checkout (not the installed app)"
    missing = list(f["missing_required"]) + list(m["missing"])
    if missing:
        shown = ", ".join(missing[:8]) + (f" and {len(missing) - 8} more" if len(missing) > 8 else "")
        return _res("app_files", "App files and modules", False, PROBLEM,
                    f"{len(missing)} required item(s) missing from {where}: {shown}. This copy of Jarvis is incomplete or damaged: reinstall Project JARVIS (your data folder is not touched by a reinstall).",
                    missing=missing)
    return _res("app_files", "App files and modules", False, PASS,
                f"All {f['checked']} expected files and {len(m['modules'])} program modules are present in {where}. Files were looked up, modules were found on disk but not imported here.")


def check_data_folder(ctx):
    d = ctx.run_dir
    probe_file = d / ".write-test"
    try:
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
    except OSError as exc:
        return _res("data_folder", "Data folder writable", False, PROBLEM, f"Jarvis cannot write its results: {type(exc).__name__}. Check that the folder is not read-only or blocked by security software.")
    where = "a temporary folder (guest session: nothing is saved)" if ctx.guest else "its own results folder (inside your Jarvis data folder)"
    return _res("data_folder", "Data folder writable", False, PASS, f"Jarvis can write {where}. No administrator rights were needed or requested.")


def check_disk(ctx):
    try:
        free = ctx.probe.free_bytes(ctx.cfg.data_dir)
    except diskspace.DiskSpaceError as exc:
        return _res("disk", "Free disk space", False, UNAVAILABLE, str(exc))
    gb = free / 1e9
    need = metadata.MIN_FREE_BYTES
    if free < need:
        return _res("disk", "Free disk space", False, PROBLEM, f"{gb:.1f} GB free on the drive holding your Jarvis data; the model experiment needs at least {need / 1e9:.0f} GB free BEFORE it starts. Free some space first.")
    return _res("disk", "Free disk space", False, PASS, f"{gb:.1f} GB free (minimum {need / 1e9:.0f} GB before the experiment). A snapshot: nothing reserves this space.")


def check_memory(ctx):
    gb = ctx.probe.ram_gb()
    if gb is None:
        return _res("memory", "Memory (RAM)", False, UNAVAILABLE, "Total RAM could not be read.")
    if gb < MIN_RAM_GB:
        return _res("memory", "Memory (RAM)", False, PROBLEM, f"{gb:.1f} GB installed; at least {MIN_RAM_GB:.0f} GB is recommended for the 8B/14B comparison (peak model memory must stay under 13 GB). Smaller models can still be tried.")
    return _res("memory", "Memory (RAM)", False, PASS, f"{gb:.1f} GB installed. The 13 GB peak limit is NOT enforced by this app; watch Task Manager during any model test.")


def check_runtime(ctx):
    r = ctx.probe.runtime()
    ctx.runtime = r
    if r.get("available"):
        return _res("runtime", "Local AI runtime (import only)", False, PASS,
                    f"llama-cpp-python {r.get('version', '?')} imports. This does NOT show that a model loads or answers: that is a separate test, not run here.")
    return _res("runtime", "Local AI runtime (import only)", False, UNAVAILABLE,
                "The local AI runtime is not installed or failed to import: " + history.scrub_text(str(r.get("error", "unknown reason"))) +
                " This preflight never installs it. It must be provided by the build or by you outside this page (docs/STAGE5_WINDOWS_PLAYBOOK.md part C).")


def check_models(ctx):
    try:
        m = ctx.probe.models(ctx.cfg)
    except Exception as exc:      # noqa: BLE001 - a damaged model list is information here, not a reason to fail the preflight
        return _res("models", "Installed models (listed, not touched)", False, INFO, f"The list of installed models could not be read ({type(exc).__name__}). Nothing was changed; the Status page and `jarvis models verify` give details.")
    n = len(m["installed"])
    return _res("models", "Installed models (listed, not touched)", False, INFO,
                f"{n} installed" + (f"; selected: {m['selected']}" if m["selected"] else "; none selected") + ". Not verified, loaded or changed by this preflight.")


def check_network_policy(ctx):
    on = ctx.policy.is_enabled("model_download") if ctx.policy is not None else False
    return _res("network", "Network permission", False, INFO,
                f"Model-host access is currently {'ON' if on else 'OFF (the default)'}. The online check below asks first and switches it on for that one action only, then restores it.")


def check_metadata(ctx):
    title = "Model metadata (online: contacts huggingface.co)"
    if not ctx.include_online:
        return _res("metadata", title, True, NOT_RUN, "Not run: the online check was not selected. Without it, no model information has been checked.")
    from jarvis.preflight.hf_fetch import make_fetcher
    fetch = ctx.fetch or make_fetcher(ctx.policy)
    try:
        with ctx.policy.grant_once("model_download"):
            doc, code = metadata.run(ctx.run_dir, check_disk=str(ctx.cfg.data_dir), fetch=fetch, free_fn=ctx.probe.free_bytes,
                                     progress=lambda d, n, t: ctx.progress_step(t), should_stop=ctx.stop)
    except metadata.Cancelled:
        return _res("metadata", title, True, NOT_RUN, "Cancelled before it finished; nothing from the partial reading is used.")
    ctx.metadata_doc = doc
    extra = {"models": [{k: r.get(k) for k in ("id", "repo", "file", "role", "in_plan", "size_bytes", "published_sha256", "revision", "license_tag", "status", "problems")}
                        for r in doc["results"]], "budget": doc["budget"], "disk_plan": doc.get("disk")}
    caveat = " Values come from one source (huggingface.co) and are published values, not hashes of downloaded files; the saved model_metadata.json is a local, editable copy and is not proof of authenticity."
    if code == 0:
        return _res("metadata", title, True, PASS, "Metadata is CONSISTENT with what the project recorded for the planned files, and the plan fits the 20 GB budget." + caveat, **extra)
    if code == 3:
        n = sum(1 for r in doc["results"] if r["status"] == "API_UNAVAILABLE")
        why = next((r.get("error") for r in doc["results"] if r.get("error")), "")
        return _res("metadata", title, True, UNAVAILABLE, f"The model host could not be reached for {n} of {len(doc['results'])} repositories ({history.scrub_text(str(why))}). Nothing is claimed about the models. Check your internet connection and try again.", **extra)
    probs = "; ".join(f"{r['id']}: {', '.join(r['problems'])}" for r in doc["results"] if r["in_plan"] and r["problems"]) or doc["overall"]
    return _res("metadata", title, True, PROBLEM, f"Problems found, so nothing here may be downloaded yet: {history.scrub_text(probs)}; budget {doc['budget']['status']}." + caveat, **extra)


STEPS = (("os", check_os), ("python", check_python), ("app_files", check_app_files), ("data_folder", check_data_folder), ("disk", check_disk), ("memory", check_memory),
         ("runtime", check_runtime), ("models", check_models), ("network", check_network_policy), ("metadata", check_metadata))
TITLES = {"os": "Operating system", "python": "Python runtime", "app_files": "App files and modules", "data_folder": "Data folder writable", "disk": "Free disk space", "memory": "Memory (RAM)",
          "runtime": "Local AI runtime (import only)", "models": "Installed models", "network": "Network permission", "metadata": "Model metadata (online)"}


def blockers(ctx, checks) -> list[str]:
    rt = ctx.runtime if hasattr(ctx, "runtime") else None
    b = ["No independent second source exists for the model sizes and hashes: every value comes from huggingface.co and none was computed from a downloaded file.",
         "No model has been downloaded in this milestone, so no file hash, size or GGUF header has been checked against real bytes.",
         "The Qwen3-8B entry in the model list is known to be defective (its repository has no Q4_K_M file); a replacement is proposed but not approved, and the list is unchanged.",
         ("The local AI runtime imports on this PC, but loading a model with it has not been tested." if rt and rt.get("available") else
          "A Windows 11 / CPython 3.12 llama-cpp-python wheel has not been identified or verified, and this app does not install it."),
         "No model has been loaded or benchmarked, so speed, memory use, answer quality and tool safety are unmeasured.",
         "This page has not been validated inside the Windows EXE; until you have run it there, treat it as untested.",
         "Limits: 25 GB free is checked above (once). The 20 GB budget is only compared with the host's published sizes. The 115% per-file ceiling and the per-operation free-space check are enforced by the app's download code. The 13 GB RAM stop is NOT enforced by the app."]
    return b


def verdict(checks: list[dict], cancelled: bool) -> dict:
    st = [c["status"] for c in checks]
    if cancelled:
        return {"code": "CANCELLED", "text": "Cancelled. Some checks did not run, so nothing is concluded from this partial run."}
    if PROBLEM in st or ERROR in st:
        return {"code": "PROBLEMS", "text": "Problems were found or a check failed. See the lines marked PROBLEM or ERROR; nothing should be downloaded until they are resolved."}
    if UNAVAILABLE in st:
        return {"code": "INCOMPLETE", "text": "Incomplete: at least one check could not run (network or a missing component). This is NOT a pass."}
    if NOT_RUN in st:
        return {"code": "PARTIAL", "text": "Partial: the checks that ran passed, but the online model check was not run, so the models have not been checked at all."}
    return {"code": "PASSED", "text": "Every check passed. This covers this PC's basics and the model host's published metadata only. No model was downloaded, loaded or benchmarked, and it does not prove the Windows EXE works."}


def run_preflight(cfg, *, include_online: bool = False, online_consent: bool = False, policy=None, fetch=None, probe=None, should_stop=None, progress=None,
                  guest: bool = False, now=None, keep: int = history.KEEP) -> dict:
    """Run every check and return the record (also saved unless this is a guest session). Raises ValueError if the online check is requested without consent."""
    if include_online and not online_consent:
        raise ValueError("the online check needs the person's confirmation; it was not given")
    if policy is None:
        from jarvis.net.policy import NetworkPolicy
        policy = NetworkPolicy.load(cfg.settings_path)
    t0 = (now or (lambda: datetime.now(timezone.utc)))()
    rid, run_dir = history.temp_run_dir() if guest else history.new_run_dir(cfg, t0)
    log: list[str] = []
    stop = should_stop or (lambda: False)

    def note(text: str) -> None:
        log.append(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {text}")

    total = len(STEPS)
    state = {"i": 0}

    def progress_step(text: str) -> None:
        note(text)
        if progress:
            progress(state["i"], total, text)
    ctx = SimpleNamespace(cfg=cfg, probe=probe or Probe(), policy=policy, fetch=fetch, include_online=include_online, run_dir=run_dir, stop=stop,
                          progress_step=progress_step, system={}, runtime=None, metadata_doc=None, guest=guest)
    checks, cancelled = [], False
    note(f"preflight started (online check: {'yes, confirmed by the person' if include_online else 'no'})")
    for i, (sid, fn) in enumerate(STEPS):
        state["i"] = i
        if cancelled or stop():
            cancelled = True
            checks.append(_res(sid, TITLES[sid], sid == "metadata", NOT_RUN, "Not run: cancelled."))
            continue
        progress_step(f"Checking: {TITLES[sid]}")
        try:
            r = fn(ctx)
        except metadata.Cancelled:
            r, cancelled = _res(sid, TITLES[sid], sid == "metadata", NOT_RUN, "Cancelled."), True
        except Exception as exc:      # noqa: BLE001 - a crashing check is reported as ERROR, never as success and never as a crash of the app
            from jarvis.eval.redact import safe_error
            r = _res(sid, TITLES[sid], sid == "metadata", ERROR, "The check crashed: " + history.scrub_text(safe_error(exc)))
        note(f"{r['id']}: {r['label']} - {r['detail'][:300]}")
        checks.append(r)
    if not cancelled and stop():
        cancelled = True
    if progress:
        progress(total, total, "Finished")
    meta = next((c for c in checks if c["id"] == "metadata"), {})
    record = {"kind": "readiness-preflight", "schema": 1, "run_id": rid, "started_at_utc": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
              "finished_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "app": {"name": V.APP_NAME, "version": V.__version__, "channel": V.RELEASE_CHANNEL},
              "system": {**ctx.system, "python": ctx.probe.python()}, "include_online": include_online, "guest_session_not_saved": guest,
              "checks": checks, "verdict": verdict(checks, cancelled), "models": meta.get("models", []), "budget": meta.get("budget"),
              "blockers": blockers(ctx, checks), "statements": dict(STATEMENTS),
              "log_file": None if guest else "preflight.log", "files": {} if guest else {"record": "preflight_record.json", "log": "preflight.log"}}
    if not guest:
        record["files"]["metadata"] = "model_metadata.json" if (run_dir / "model_metadata.json").is_file() else None
    note(f"verdict: {record['verdict']['code']}")
    record = history.scrub(record)
    if guest:
        import shutil
        shutil.rmtree(run_dir, ignore_errors=True)
    else:
        try:
            history.save(run_dir, record, log)
            history.prune(cfg, keep)
        except OSError as exc:
            record["history_error"] = f"The result could not be saved ({type(exc).__name__}); it is shown here only."
    return record
