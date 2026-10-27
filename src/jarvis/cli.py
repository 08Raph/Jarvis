"""Command-line entry point (also the first packaged executable; the desktop UI comes later).

Nothing here connects to the network unless the user has enabled that feature and runs a command that needs it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path

from jarvis import __version__
from jarvis.config import Config
from jarvis.firstrun import first_run
from jarvis.hardware import detect_hardware, recommend
from jarvis.inference.policy import ABSTAIN_MESSAGE, AnswerGate
from jarvis.memory.store import MemoryStore
from jarvis.models.manager import IntegrityError, ModelStore
from jarvis.net.policy import FEATURES, NETWORK_ERRORS, NetworkPolicy
from jarvis.storage import backup_sqlite

IMPLEMENTED_FEATURES = ("model_download", "update_check", "online_assistant")


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ProjectJarvis"
    return Path.home() / ".local" / "share" / "ProjectJarvis"


def _cfg(args) -> Config:
    from jarvis.config import DEFAULT_PROFILE, valid_profile
    # An explicit --profile (even an empty one, e.g. from an unset batch variable) must never silently fall back to the
    # default profile, which would show another person's memory.
    chosen = args.profile if args.profile is not None else (os.environ.get("JARVIS_PROFILE") or DEFAULT_PROFILE)
    profile = valid_profile(chosen)
    return Config(data_dir=Path(args.data_dir) if args.data_dir else default_data_dir(), profile=profile)


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="jarvis", description="PROJECT JARVIS (local assistant)")
    ap.add_argument("--data-dir", help="where models, memory and settings live")
    ap.add_argument("--profile", help="application profile: keeps one person's memory and preferences apart from another's "
                                      "(default: 'default'; or set JARVIS_PROFILE)")
    ap.add_argument("--guest", action="store_true",
                    help="temporary session: reads and saves no personal memory; everything is forgotten when Jarvis closes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    gu = sub.add_parser("gui", help="open the desktop window (needs PySide6; the command-line tools work without it)")
    gu.add_argument("--simulate", action="store_true", help="use the FAKE simulated desktop for missions (nothing on this PC is touched)")
    st = sub.add_parser("startup", help="optional: start the Jarvis window when you sign in to Windows (off by default; never changed silently)")
    st.add_argument("action", choices=["show", "enable", "disable"])
    st.add_argument("--confirm", action="store_true", help="required for enable: confirms you want Jarvis to start when you sign in")
    sub.add_parser("diagnostics", help="print a local, redacted diagnostics report as JSON (no conversations or memories; nothing is sent)")
    sub.add_parser("first-run", help="create folders and safe defaults; inspect hardware")
    sub.add_parser("setup", help="guided first-run setup (what the Start-menu shortcut runs)")
    sa = sub.add_parser("setup-auto", help="non-interactive version of the setup wizard (used by automated builds): check PC, download, verify, test the model")
    sa.add_argument("--model", help="catalog model id (default: the recommended one)")
    sa.add_argument("--accept-download", action="store_true", help="consent to download the model (its size and licence are printed first); without it nothing is downloaded")
    sub.add_parser("hardware", help="detect hardware and show which model sizes fit")
    pr = sub.add_parser("privacy", help="show or change optional online features")
    pr.add_argument("action", nargs="?", choices=["show", "enable", "disable"], default="show")
    pr.add_argument("feature", nargs="?", choices=FEATURES)
    m = sub.add_parser("models", help="manage local model files")
    ms = m.add_subparsers(dest="mcmd", required=True)
    ms.add_parser("list")
    mi = ms.add_parser("install")
    mi.add_argument("package")
    mi.add_argument("--id", required=True)
    g = mi.add_mutually_exclusive_group(required=True)
    g.add_argument("--sha256", help="trusted SHA-256 of the package")
    g.add_argument("--accept-unverified", action="store_true", help="install without a trusted hash")
    for name in ("remove", "select", "rollback"):
        ms.add_parser(name).add_argument("id")
    mv = ms.add_parser("verify", help="re-hash an installed model's files")
    mv.add_argument("id")
    ms.add_parser("catalog", help="free open-weight chat models this app can fetch, and whether they fit this PC")
    mp = ms.add_parser("plan", help="which catalog model fits this PC, and whether GPU acceleration is detected / available / verified")
    mp.add_argument("--prefer", choices=["balanced", "quality", "speed"], default="balanced")
    mp.add_argument("--model", help="check one specific model; it is never replaced automatically if it does not fit")
    mf = ms.add_parser("fetch", help="download a catalog model from its publisher (asks first; needs internet once)")
    mf.add_argument("id")
    mf.add_argument("--yes", action="store_true", help="skip the confirmation question")
    mf.add_argument("--trust-unpinned", action="store_true", help="allow a file whose host published no checksum")
    mg = ms.add_parser("add-gguf", help="install a .gguf model file you already have (license information required)")
    mg.add_argument("path")
    mg.add_argument("--id", required=True)
    mg.add_argument("--license", required=True, help="SPDX id, e.g. Apache-2.0 or MIT")
    mg.add_argument("--license-file", help="file with the license text/terms that came with the model")
    mg.add_argument("--license-url", help="where the license text can be read")
    mg.add_argument("--sha256", help="trusted SHA-256 of the file")
    mg.add_argument("--accept-unverified", action="store_true")
    mg.add_argument("--context-tokens", type=int, help="context window to use for this model (default: the engine default)")
    mg.add_argument("--no-think", action="store_true", help="Qwen3-style model: switch the hidden thinking phase off with /no_think")
    mg.add_argument("--no-select", action="store_true", help="install only; keep the currently selected model (used for side-by-side benchmarks)")
    sub.add_parser("selftest", help="REAL test: load the active model on this PC, generate text, measure speed")
    pf = sub.add_parser("perf", help="REAL multi-token speed measurement of the active model (load time, first token, sustained tokens/s, peak memory); writes perf.json")
    pf.add_argument("--out", help="folder for perf.json and perf_summary.txt")
    pf.add_argument("--runs", type=int, default=3, help="measured runs (max 10)")
    pf.add_argument("--warmup", type=int, default=1, help="warm-up runs, reported separately (max 3)")
    pf.add_argument("--max-tokens", type=int, default=128, help="tokens per run (max 512)")
    bi = sub.add_parser("bench-intel", help="REAL intelligence benchmark (intel-v1: 48 fixed cases, 12 categories) of an installed model; writes JSON + summary")
    bi.add_argument("--model", help="installed model id (default: the active model)")
    bi.add_argument("--out", help="folder for intel_<model>.json and .txt")
    bi.add_argument("--sampled-runs", type=int, default=2, help="extra runs per case at temperature 0.7 (0-4)")
    bi.add_argument("--only", help="comma-separated case ids (debugging)")
    bi.add_argument("--max-minutes", type=float, default=None, help="stop (marking the result incomplete) after this many minutes")
    bi.add_argument("--label", default="", help="name used in the output file names")
    bi.add_argument("--prompt-variant", choices=("v1", "v2"), help="system-prompt variant to measure (default: the app's current default)")
    bi.add_argument("--sampling-profile", choices=("default", "publisher"), help="sampling settings to measure")
    bi.add_argument("--thinking", action="store_true", help="let a Qwen3 model think before answering (much slower on CPU)")
    bi.add_argument("--max-tokens", type=int, default=None, help="reply token limit per turn (default 512)")
    bs = sub.add_parser("bench-security", help="REAL-MODEL security benchmark (sec-v1: injection, prompt leak, memory exfiltration, summary, tool authorization); raw model vs app results kept apart")
    bs.add_argument("--model", help="installed model id (default: the active model)")
    bs.add_argument("--out", help="folder for sec_<label>.json and .txt")
    bs.add_argument("--sampled-runs", type=int, default=2, help="extra runs per case at temperature 0.7 (0-4)")
    bs.add_argument("--only", help="comma-separated case ids (debugging)")
    bs.add_argument("--max-minutes", type=float, default=None, help="stop (marking the result incomplete) after this many minutes")
    bs.add_argument("--label", default="", help="name used in the output file names")
    bs.add_argument("--prompt-variant", choices=("v1", "v2"), help="system-prompt variant to measure (default: the app's current default)")
    bs.add_argument("--max-tokens", type=int, default=None, help="reply token limit per turn (default 512)")
    rt = sub.add_parser("runtime", help="is the local AI runtime present in this installation?")
    rt.add_argument("--require", action="store_true", help="exit with an error if it is missing")
    ch = sub.add_parser("chat", help="chat with the active local model (one prompt, or interactive if none given)")
    ch.add_argument("prompt", nargs="?")
    ch.add_argument("--max-new-tokens", type=int, default=128)
    ch.add_argument("--temperature", type=float, default=0.8)
    from jarvis.memory.cli_memory import add_parsers as _add_memory_parsers
    _add_memory_parsers(sub)
    from jarvis.desktop.cli_desktop import add_parsers as _add_desktop_parsers
    _add_desktop_parsers(sub)
    from jarvis.dev.cli_dev import add_parsers as _add_dev_parsers
    _add_dev_parsers(sub)
    from jarvis.cli_p8 import add_parsers as _add_p8_parsers
    _add_p8_parsers(sub)
    from jarvis.cli_missions import add_parsers as _add_mission_parsers
    _add_mission_parsers(sub)
    uc = sub.add_parser("update-check", help="opt-in: check a signed manifest for a newer version")
    uc.add_argument("--url", required=True)
    uc.add_argument("--public-key-hex", required=True)
    sub.add_parser("backup", help="back up the local memory database")
    rs = sub.add_parser("restore", help="restore the memory database from a backup file (keeps a safety copy)")
    rs.add_argument("backup_file")
    vo = sub.add_parser("voice", help="voice settings and (untested on real hardware) live voice session")
    vs = vo.add_subparsers(dest="vcmd", required=True)
    vs.add_parser("show")
    vset = vs.add_parser("set", help="change one voice setting, e.g. `voice set input_mode wake_word`")
    vset.add_argument("key")
    vset.add_argument("value")
    vs.add_parser("devices")
    vs.add_parser("voices")
    vsay = vs.add_parser("say", help="speak text with the Windows voice (tests output only)")
    vsay.add_argument("text")
    vrun = vs.add_parser("run", help="start the live voice session (needs sounddevice, vosk, a Vosk model, Windows)")
    vrun.add_argument("--vosk-model", default=None, help="folder of a Vosk model YOU installed (or save it once: jarvis voice set vosk_model FOLDER)")
    vs.add_parser("pron-list")
    pa = vs.add_parser("pron-add")
    pa.add_argument("word")
    pa.add_argument("say_as")
    pr2 = vs.add_parser("pron-remove")
    pr2.add_argument("word")
    tl = sub.add_parser("telemetry", help="optional anonymous diagnostics/usage reports (OFF by default; docs/TELEMETRY.md)")
    tl.add_argument("--dev-insecure-localhost", action="store_true", help="development only")
    tl.add_argument("--endpoint", help="development only: override the server address")
    tsub = tl.add_subparsers(dest="tcmd", required=True)
    for n in ("status", "schema", "preview", "send", "disable", "delete-my-data", "reset-id"):
        tsub.add_parser(n)
    te = tsub.add_parser("enable")
    te.add_argument("--diagnostics-only", action="store_true")
    te.add_argument("--usage-only", action="store_true")
    on = sub.add_parser("online", help="optional Jarvis Online service: sign in, chat, learning setting (docs/ONLINE.md)")
    on.add_argument("--dev-insecure-localhost", action="store_true", help="development only: allow http to localhost")
    osub = on.add_subparsers(dest="ocmd", required=True)
    oc = osub.add_parser("connect")
    oc.add_argument("url")
    for n in ("status", "terms", "register", "login", "logout", "accept-terms", "export", "delete-chats", "delete-account", "recover", "disconnect"):
        o = osub.add_parser(n)
        if n == "export":
            o.add_argument("--out", default="jarvis-online-export.json")
    och = osub.add_parser("chat")
    och.add_argument("prompt", nargs="?")
    ol = osub.add_parser("learning")
    ol.add_argument("setting", nargs="?", choices=["show", "on", "off"], default="show")
    ol.add_argument("--keep-past", action="store_true", help="when turning off, do not withdraw past conversations")
    pe = sub.add_parser("personality", help="personality modes, traits and approval of proposed changes")
    ps = pe.add_subparsers(dest="pcmd", required=True)
    ps.add_parser("show")
    psm = ps.add_parser("set-mode")
    psm.add_argument("mode")
    pst = ps.add_parser("set-trait")
    pst.add_argument("trait")
    pst.add_argument("value", type=float)
    ps.add_parser("pending")
    for n in ("approve", "reject"):
        ps.add_parser(n).add_argument("id", type=int)
    ps.add_parser("instructions")
    pp = sub.add_parser("persona", help="conversation style: Normal (default), Savage, Coach, Tutor, ... (wording only; never changes permissions)")
    pps = pp.add_subparsers(dest="ppcmd", required=True)
    for n in ("show", "list", "normal", "reset"):
        pps.add_parser(n)
    pps.add_parser("set").add_argument("style")
    pps.add_parser("preview").add_argument("style")
    ppsc = pps.add_parser("selfcheck", help="REAL-MODEL check of every style (needs a working model; read the saved outputs yourself)")
    ppsc.add_argument("--styles", nargs="*", help="only these style ids")
    ppl = pps.add_parser("level", help="roast|profanity|flirt|argument|motivation")
    ppl.add_argument("name")
    ppl.add_argument("value", type=int)
    ppa = pps.add_parser("adult", help="explicit opt-in for adult-only styles (Savage, Flirty, Romantic)")
    ppa.add_argument("setting", choices=["on", "off"])
    ppa.add_argument("--confirm-adult", action="store_true", help="required with 'on': you confirm you are 18+ and want adult-only styles")
    ppo = pps.add_parser("option", help="language | level | minutes | hands_busy")
    ppo.add_argument("name")
    ppo.add_argument("value")
    return ap


def _voice_cmd(args, cfg: Config) -> int:
    from dataclasses import asdict as _asdict
    from jarvis.voice.settings import VoiceSettings
    path = cfg.profile_dir / "voice.json"
    vs = VoiceSettings.load(path)
    c = args.vcmd
    if c == "show":
        _dump({**_asdict(vs), "note": "Microphone is used only when input_mode is wake_word or push_to_talk. "
               "Speech recognition and synthesis run locally; no audio is sent anywhere."})
        return 0
    if c == "set":
        from dataclasses import fields as _fields
        ftypes = {f.name: f.type for f in _fields(VoiceSettings)}
        if args.key not in ftypes or args.key in ("pronunciations", "wake_alternatives"):
            print(f"unknown or non-scalar setting {args.key!r}", file=sys.stderr)
            return 2
        cur = getattr(vs, args.key)
        try:
            if isinstance(cur, bool):
                if args.value.lower() not in ("true", "false"):
                    raise ValueError("use true or false")
                val = args.value.lower() == "true"
            elif isinstance(cur, float):
                val = float(args.value)
            elif args.value.lower() in ("none", "default"):
                val = None
            else:
                val = args.value
            setattr(vs, args.key, val)
            vs.save(path)
        except ValueError as exc:
            print(f"invalid: {exc}", file=sys.stderr)
            return 2
        print(f"{args.key} = {val}")
        return 0
    if c in ("pron-list", "pron-add", "pron-remove"):
        if c == "pron-add":
            vs.pronunciations[args.word] = args.say_as
            vs.save(path)
        elif c == "pron-remove":
            vs.pronunciations.pop(args.word, None)
            vs.save(path)
        _dump(vs.pronunciations)
        return 0
    from jarvis.voice.backends import BackendUnavailable, SapiSpeaker, SoundDeviceMic
    try:
        if c == "devices":
            _dump(SoundDeviceMic().devices())
        elif c == "voices":
            _dump(SapiSpeaker().list_voices())
        elif c == "say":
            from jarvis.voice.speakable import apply_pronunciations, speakable
            SapiSpeaker().speak(apply_pronunciations(speakable(args.text), vs.pronunciations), vs.rate, vs.volume, vs.tts_voice)
        elif c == "run":
            return _voice_run(args, cfg, vs)
    except (BackendUnavailable, OSError, FileNotFoundError) as exc:
        print(f"voice backend unavailable: {exc}", file=sys.stderr)
        return 2
    return 0


def _startup_cmd(args) -> int:
    from jarvis.app.startup import Startup
    s = Startup()
    try:
        if args.action == "enable":
            _dump(s.enable(confirm=bool(args.confirm)))
        elif args.action == "disable":
            _dump(s.disable())
        else:
            _dump(s.status())
    except (PermissionError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def _voice_run(args, cfg: Config, vs) -> int:
    import time
    from jarvis.personality import PersonalityStore, render
    from jarvis.voice.backends import SapiSpeaker, SoundDeviceMic, VoskRecognizer
    from jarvis.voice.session import VoiceSession
    if vs.input_mode == "off":
        print("Voice input is off. Turn it on explicitly: `jarvis voice set input_mode wake_word` "
              "(or push_to_talk).", file=sys.stderr)
        return 2
    from jarvis.app.lifecycle import InstanceLock
    mic_lock = InstanceLock(cfg.profile_dir / "mic.lock")          # the same lock the window's hands-free voice uses: one microphone owner at a time
    if not mic_lock.acquire():
        print("Another Jarvis process (probably the window's hands-free voice) is already using the microphone. Close it or turn hands-free off first.", file=sys.stderr)
        return 2
    try:
        return _voice_run_locked(args, cfg, vs)
    finally:
        mic_lock.release()


def _voice_run_locked(args, cfg: Config, vs) -> int:
    import time
    from jarvis.personality import PersonalityStore, render
    from jarvis.voice.backends import SapiSpeaker, SoundDeviceMic, VoskRecognizer
    from jarvis.voice.session import VoiceSession
    pers = PersonalityStore(cfg.profile_dir / "personality.json")

    assistant = None
    try:
        from jarvis.assistant import LocalAssistant
        assistant = LocalAssistant(cfg, guest=bool(args.guest))
    except Exception as exc:
        print(f"(No local model available for voice replies: {exc})", file=sys.stderr)

    def responder(text: str) -> str:
        if assistant is None:
            return render("I heard you, but no language model is installed yet, so I cannot answer.", pers.profile, "error")
        rep = assistant.reply(text, max_tokens=200)      # the assistant applies the persona (style, safety overrides, label)
        if assistant.last_command:             # /normal etc.: say the confirmation as is
            return rep
        if assistant.last_outcome.handled:      # a memory confirmation/answer: say the short version, unstyled
            return assistant.spoken_version(rep)
        if assistant.last_turn is not None and assistant.last_turn.style != "normal":
            return rep                          # a chosen style: the model's own wording, no template decoration
        return render(rep, pers.profile, "answer")

    from jarvis.voice.console import HELP, Controls, format_event, make_key_reader

    def show(kind, payload):
        line = format_event(kind, payload)
        if line:
            print(line, flush=True)
    print("Starting voice session. Audio is processed on this PC only and is never saved or sent anywhere.")
    print(HELP)
    sess = VoiceSession(vs, SoundDeviceMic(), VoskRecognizer(args.vosk_model or vs.vosk_model or "", vs.all_wake_phrases()), SapiSpeaker(),
                        responder, on_event=show,
                        rate_factor=lambda: assistant.last_turn.speech_rate if (assistant is not None and assistant.last_turn) else 1.0)
    def to_normal() -> str:
        p = assistant.persona if assistant is not None else pers_ctl
        return p.command("/normal") or "Back to Normal mode."
    from jarvis.personality.persona import Persona as _Persona
    pers_ctl = _Persona(cfg.profile_dir, persist=not args.guest)
    controls, read_key = Controls(sess, on_normal=to_normal), make_key_reader()
    try:
        sess.start()
        while not controls.quit:
            k = read_key()
            if k:
                controls.key(k)
            sess.tick()
            if sess.state in ("RECOVERING", "SLEEPING", "MUTED", "OFF", "ERROR"):
                time.sleep(0.1)
            if sess.state == "ERROR":
                return 2
    except KeyboardInterrupt:
        pass
    finally:
        sess.stop()
        print("Microphone closed.")
    return 0


def _tel(cfg: Config, event: str, **data) -> None:
    """Queue an allowlisted telemetry event if (and only if) the user agreed. Never raises."""
    from jarvis.telemetry.client import record_quietly
    record_quietly(cfg, event, **data)


def _catalog_or_custom(store: ModelStore, model_id: str) -> str:
    try:
        meta = json.loads((store.path(model_id) / "meta.json").read_text(encoding="utf-8"))
        return meta["catalog_id"] if meta.get("catalog_id") else "custom"    # user-chosen ids are never sent
    except (OSError, ValueError, KeyError):
        return "custom"


def _selftest_state(cfg: Config, store: ModelStore, model_id: str) -> str:
    f = cfg.data_dir / "selftest.json"
    try:
        r = json.loads(f.read_text(encoding="utf-8"))
        meta = json.loads((store.path(model_id) / "meta.json").read_text(encoding="utf-8"))
        if r.get("model_id") == model_id and r.get("model_sha256") == meta.get("package_sha256") and r.get("passed"):
            return "passed"
    except (OSError, ValueError):
        pass
    return "not_run"


def _chat_gguf(args, cfg: Config, store: ModelStore, active: str) -> int:
    from jarvis.assistant import LocalAssistant, NoActiveModel
    from jarvis.inference.gguf_engine import RuntimeUnavailable
    try:
        a = LocalAssistant(cfg, guest=bool(args.guest))
    except (NoActiveModel, RuntimeUnavailable) as exc:
        _LAST["err"] = "runtime_unavailable" if isinstance(exc, RuntimeUnavailable) else "unknown"
        print(str(exc), file=sys.stderr)
        return 2
    except IntegrityError as exc:
        _LAST["err"] = "integrity_failed"
        print(str(exc), file=sys.stderr)
        return 3
    if _selftest_state(cfg, store, active) != "passed":
        print("(This model has not been self-tested on this PC yet. Run `jarvis selftest` to check it and measure speed.)",
              file=sys.stderr)
    print(f"[local model: {active}; everything runs on this PC]", file=sys.stderr)
    if args.guest:
        print("[GUEST session: no personal memory is read or saved; everything is forgotten when you quit]", file=sys.stderr)
    else:
        n = len(a.memory.list())
        print(f"[memory: profile '{cfg.profile}', {n} saved item(s) on this PC; review with `jarvis memory list`]", file=sys.stderr)

    def one(text: str) -> None:
        gen = a.reply_stream(text, max_tokens=args.max_new_tokens, temperature=args.temperature)
        first = next(gen, None)           # runs the memory step, so the notice is known before the answer is printed
        if a.last_notice:
            print(f"[{a.last_notice}]", file=sys.stderr)
        if first is not None:
            print(first, end="", flush=True)
        for piece in gen:
            print(piece, end="", flush=True)
        print()

    def _end() -> int:
        try:
            from jarvis.assistant import summaries_enabled
            if not args.guest and summaries_enabled(cfg):
                row = a.summarize_session()
                if row:
                    print(f"[saved a short summary of this chat as memory #{row['id']}; review: `jarvis memory show {row['id']}`]",
                          file=sys.stderr)
        except Exception as exc:
            print(f"(could not save a summary: {type(exc).__name__})", file=sys.stderr)
        a.close()
        return 0
    try:
        if args.prompt:
            one(args.prompt)
            return _end()
        print("Type your message. /reset clears the conversation, /exit quits.", file=sys.stderr)
        while True:
            text = input("you> ").strip()
            if text in ("/exit", "/quit"):
                return _end()
            if text == "/reset":
                a.reset()
                continue
            if text:
                one(text)
    except (EOFError, KeyboardInterrupt):
        print()
        return _end()
    except Exception as exc:
        print(f"The model failed while answering: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


def _selftest(cfg: Config, store: ModelStore) -> int:
    import time as _t
    from datetime import datetime, timezone
    from jarvis.inference.gguf_engine import GGUFEngine, RuntimeUnavailable, runtime_info
    active = store.active()
    if active is None or store.kind(active) != "gguf":
        print("No chat model selected. Run `jarvis setup` first.", file=sys.stderr)
        return 2
    ok, problems = store.verify(active)
    if not ok:
        print(f"Model failed its integrity check: {problems}", file=sys.stderr)
        return 3
    print(f"Loading {active} (this can take a while the first time)...", flush=True)
    result = {"model_id": active, "app_version": __version__, "time": datetime.now(timezone.utc).isoformat(),
              "runtime": runtime_info(), "passed": False}
    try:
        meta = json.loads((store.path(active) / "meta.json").read_text(encoding="utf-8"))
        result["model_sha256"] = meta.get("package_sha256")
        eng = GGUFEngine(store.path(active))
        result["load_seconds"] = round(eng.load_seconds, 2)
        t0 = _t.perf_counter()
        text = eng.chat([{"role": "system", "content": "You are a helpful assistant."},
                         {"role": "user", "content": "What is 2+2? Answer with just the number."}], max_tokens=48, temperature=0.0)
        dt = _t.perf_counter() - t0
        n = eng._count(text)
        result.update(gpu_layers_requested=eng.gpu_layers_requested, answer=text, generation_seconds=round(dt, 2), output_tokens=n,
                      tokens_per_second=round(n / dt, 2) if dt > 0 and n else None,
                      answer_mentions_4=("4" in text), passed=bool(text.strip()))
    except RuntimeUnavailable as exc:
        result["error"] = str(exc)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    (cfg.data_dir / "selftest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if "error" in result:
        _LAST["err"] = "runtime_unavailable" if "not available" in result["error"] else "load_failed"
    rv = str(result["runtime"].get("version", ""))
    extra = {"runtime_version": rv} if re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", rv) else {}
    if result.get("load_seconds") is not None:
        extra["load_seconds"] = result["load_seconds"]
    if result.get("tokens_per_second") is not None and 0 <= result["tokens_per_second"] <= 10000:
        extra["tokens_per_second"] = result["tokens_per_second"]
    _tel(cfg, "selftest_result", model=_catalog_or_custom(store, active), passed=bool(result["passed"]), **extra)
    _dump(result)
    print("SELFTEST PASSED: the model loaded and produced text on this PC." if result["passed"]
          else "SELFTEST FAILED: see the error above.", file=sys.stderr)
    return 0 if result["passed"] else 3


def _models_gguf_cmd(args, cfg: Config, store: ModelStore, policy: NetworkPolicy) -> int:
    from jarvis.hardware import assess_gguf, detect_hardware
    from jarvis.models import catalog
    from jarvis.models.gguf import GGUFError
    hw = detect_hardware(cfg.data_dir if cfg.data_dir.exists() else ".")
    try:
        if args.mcmd == "catalog":
            installed = {m["id"] for m in store.installed()}
            _dump([{"id": m.id, "title": m.title, "license": m.license_spdx, "approx_download_gb": round(m.approx_size_bytes / 1e9, 2),
                    "fits_this_pc": assess_gguf(hw, m.approx_size_bytes, m.min_ram_gb), "installed": m.id in installed,
                    "note": m.note} for m in catalog.BUILTIN])
            return 0
        if args.mcmd == "plan":
            from jarvis.inference.gguf_engine import runtime_info
            from jarvis.models import selector
            rt = runtime_info()
            ch = selector.select(hw, rt, prefer=args.prefer, pinned=args.model)
            _dump({"hardware": hw.__dict__, "runtime": rt, "tier": ch.tier, "acceleration": selector.acceleration_state(hw, rt),
                   "fits": ch.fits, "model": ch.model.id if ch.model else None, "mode": ch.mode, "reasons": ch.reasons,
                   "alternatives": ch.alternatives, "records": [selector.compatibility_record(m) for m in catalog.BUILTIN],
                   "note": "Memory figures are estimates; no model has been verified on this machine unless verified_hardware says so."})
            return 0 if ch.fits else 2
        if args.mcmd == "fetch":
            m = catalog.get(args.id)
            fit = assess_gguf(hw, m.approx_size_bytes, m.min_ram_gb)
            print(f"{m.title}\n  License: {m.license_spdx} ({m.license_url})\n  Download: about {m.approx_size_bytes / 1e9:.2f} GB from huggingface.co"
                  f"\n  Fit for this PC: {fit['verdict']} - {'; '.join(fit['reasons'])}")
            if fit["verdict"] == "too_large":
                print("This PC does not meet the estimated needs. Not downloading.", file=sys.stderr)
                return 2
            if not args.yes and input("Download it now? Only the model file is fetched; your chats are never sent. Type  yes : ").strip().lower() != "yes":
                print("Cancelled.")
                return 1
            last = [0.0]

            def prog(done, total):
                import time as _t
                if _t.monotonic() - last[0] > 1:
                    last[0] = _t.monotonic()
                    print(f"\r  {done / 1e6:,.0f} / {total / 1e6:,.0f} MB", end="", flush=True)
            with policy.grant_once("model_download"):
                meta = store.download_gguf(m, prog, trust_unpinned=args.trust_unpinned)
            print()
            store.set_active(meta["id"])
            _tel(cfg, "model_installed", model=m.id, source="catalog")
            print(f"Installed and selected '{meta['id']}'. Now run: jarvis selftest")
            return 0
        if args.mcmd == "add-gguf":
            if not (args.license_file or args.license_url):
                print("Give --license-file or --license-url so the terms are recorded with the model.", file=sys.stderr)
                return 2
            if not (args.sha256 or args.accept_unverified):
                print("Give --sha256 (from the publisher) or --accept-unverified.", file=sys.stderr)
                return 2
            text = Path(args.license_file).read_text(encoding="utf-8", errors="replace") if args.license_file else f"License text: {args.license_url}"
            if args.context_tokens is not None and not 512 <= args.context_tokens <= 131072:
                print("--context-tokens must be between 512 and 131072.", file=sys.stderr)
                return 2
            extra = {}
            if args.context_tokens:
                extra["context_tokens"] = args.context_tokens
            if args.no_think:
                extra["no_think_suffix"] = True
            meta = store.install_gguf(args.path, args.id, args.license, text, args.license_url or "", args.sha256, extra_meta=extra or None)
            _tel(cfg, "model_installed", model="custom", source="file")
            if args.no_select:
                print(f"Installed '{meta['id']}'. The selected model was NOT changed. Benchmark it with: jarvis bench-intel --model {meta['id']} --out <folder>")
                return 0
            store.set_active(meta["id"])
            print(f"Installed and selected '{meta['id']}'. Now run: jarvis selftest")
            return 0
    except KeyError:
        print(f"Unknown catalog model. See: jarvis models catalog", file=sys.stderr)
    except NETWORK_ERRORS as exc:  # URLError subclasses OSError, so it must be caught first
        print(f"Download failed: {type(exc).__name__}: {exc}\nIf the problem persists, download the .gguf file in a browser and use "
              "`jarvis models add-gguf` (see docs/LOCAL_MODEL.md).", file=sys.stderr)
    except (GGUFError, IntegrityError, ValueError, OSError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
    except Exception as exc:  # other failures surface here (urllib errors are not imported outside the policy module)
        print(f"Download failed: {type(exc).__name__}: {exc}\nIf the problem persists, download the .gguf file in a browser and use "
              "`jarvis models add-gguf` (see docs/LOCAL_MODEL.md).", file=sys.stderr)
    return 3


def _telemetry_cmd(args, cfg: Config) -> int:
    from jarvis.telemetry.client import Telemetry, TelemetryError, get_endpoint
    from jarvis.telemetry.consent import notice_text
    from jarvis.telemetry.schema import categories
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    policy = NetworkPolicy.load(cfg.settings_path)
    policy.allow_insecure_localhost = bool(args.dev_insecure_localhost)
    t = Telemetry(cfg, policy, endpoint=args.endpoint if args.endpoint else None)
    c = args.tcmd
    try:
        if c == "status":
            _dump({**t.consent.state(), "server_configured_in_this_build": bool(t.endpoint),
                   "network_flag_on": policy.is_enabled("telemetry"), "queued_events": len(t.preview())})
        elif c == "schema":
            _dump(categories())
        elif c == "preview":
            _dump(t.preview())
        elif c == "enable":
            if not t.endpoint:
                print("This build has no telemetry server configured, so there is nothing to enable.", file=sys.stderr)
                return 2
            print(notice_text())
            if input("Type  I agree  to turn this on (anything else leaves it OFF): ").strip().lower() != "i agree":
                print("Telemetry stays OFF.")
                return 1
            both = not (args.diagnostics_only or args.usage_only)
            t.consent.enable(diagnostics=both or args.diagnostics_only, usage=both or args.usage_only)
            policy.enable("telemetry")
            policy.save(cfg.settings_path)
            print("Telemetry is ON. Review what is queued any time with: jarvis telemetry preview")
        elif c == "disable":
            t.consent.disable()
            t.clear_queue()
            policy.disable("telemetry")
            policy.save(cfg.settings_path)
            print("Telemetry is OFF and unsent data was deleted. Data already sent stays on the server until you run: jarvis telemetry delete-my-data")
        elif c == "send":
            _dump(t.send())
        elif c == "delete-my-data":
            r = t.delete_remote()
            t.consent.reset_id()
            t.clear_queue()
            _dump(r)
            print("Server data for your old install ID was deleted; a new random ID was created.")
        elif c == "reset-id":
            t.consent.reset_id()
            t.clear_queue()
            print("New random install ID created; the old ID is no longer used.")
    except TelemetryError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (EOFError, KeyboardInterrupt):
        print("\nTelemetry stays OFF.")
        return 1
    return 0


def _online_cmd(args, cfg: Config) -> int:
    import getpass
    from jarvis.net.policy import NetworkDisabled
    from jarvis.online.client import OnlineClient, ServiceError
    from jarvis.online.flows import login_flow, register_flow
    from jarvis.online.tokenstore import TokenStore
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    conf_path = cfg.data_dir / "online.json"
    policy = NetworkPolicy.load(cfg.settings_path)
    policy.allow_insecure_localhost = bool(args.dev_insecure_localhost)
    tokens = TokenStore(cfg.data_dir / "online.token")
    try:
        conf = json.loads(conf_path.read_text(encoding="utf-8")) if conf_path.exists() else {}
    except (ValueError, OSError):
        conf = {}
    c = args.ocmd

    if c == "connect":
        if not args.url.lower().startswith(("https://", "http://localhost", "http://127.0.0.1")):
            print("The service address must start with https://", file=sys.stderr)
            return 2
        print(f"You are about to allow Project JARVIS to contact: {args.url}")
        print("Your sign-in and, when you chat there, your messages will be sent to that server. Nothing is sent until you sign in.")
        print("Only connect to a service you trust. Read its terms first (jarvis online terms).")
        if input("Type  yes  to allow this connection: ").strip().lower() != "yes":
            print("Not connected.")
            return 1
        conf = {"url": args.url.rstrip("/")}
        conf_path.write_text(json.dumps(conf), encoding="utf-8")
        policy.enable("online_assistant")
        policy.save(cfg.settings_path)
        print("Connected (not signed in yet). Next: jarvis online terms, then jarvis online register")
        return 0
    if c == "disconnect":
        policy.disable("online_assistant")
        policy.save(cfg.settings_path)
        tokens.clear()
        conf_path.unlink(missing_ok=True)
        print("Disconnected. The saved sign-in was removed from this computer. Your server-side account still exists "
              "(delete it first with `jarvis online delete-account` if you want it gone).")
        return 0
    if c == "status":
        _dump({"connected_to": conf.get("url"), "network_feature_enabled": policy.is_enabled("online_assistant"),
               "signed_in_token_present": tokens.load() is not None,
               "note": "No server is built in. Local chat (`jarvis chat`) never uses the network."})
        return 0
    if not conf.get("url") or not policy.is_enabled("online_assistant"):
        print("Not connected to any service. Run: jarvis online connect https://your-service", file=sys.stderr)
        return 2
    client = OnlineClient(policy, conf["url"], tokens)

    def ask_secret(p): return getpass.getpass(p)
    try:
        if c == "terms":
            t = client.terms()
            print(t["text"], f"\n(fingerprint {t['sha256'][:16]})")
        elif c == "register":
            return 0 if register_flow(client, input, ask_secret, print) else 1
        elif c == "login":
            return 0 if login_flow(client, input, ask_secret, print) else 1
        elif c == "logout":
            client.logout()
            print("Signed out.")
        elif c == "accept-terms":
            t = client.terms()
            print(t["text"])
            if input("Type  I accept  to agree to this version: ").strip().lower() != "i accept":
                print("Not accepted.")
                return 1
            client.accept_terms(t["sha256"])
            print("Accepted.")
        elif c == "learning":
            if args.setting == "show":
                m = client.me()
                print(f"Learning from your conversations is {'ON' if m['learning_opt_in'] else 'OFF'}.")
            else:
                consent = None
                if args.setting == "on":
                    t = client.terms()
                    print(t.get("learning_notice", ""))
                    if not t.get("learning_notice_sha256") or input("Type exactly  I consent  to allow training use of your FUTURE conversations: ").strip() != "I consent":
                        print("Not changed. Learning stays as it was; nothing is used without your consent.")
                        return 1
                    consent = t["learning_notice_sha256"]
                r = client.set_learning(args.setting == "on", withdraw_past=not args.keep_past, consent_sha=consent)
                print(f"Learning is now {'ON' if r['learning_opt_in'] else 'OFF'}."
                      + (f" {r['past_messages_withdrawn']} earlier messages were withdrawn from future training." if r["past_messages_withdrawn"] else ""))
        elif c == "export":
            Path(args.out).write_text(json.dumps(client.export(), indent=2), encoding="utf-8")
            print(f"Saved your data to {args.out}")
        elif c == "delete-chats":
            if input("This permanently deletes your stored chat history on the service. Type  delete  to continue: ").strip().lower() != "delete":
                print("Cancelled.")
                return 1
            print(f"Deleted {client.delete_chats()['deleted_messages']} stored messages.")
        elif c == "delete-account":
            if input("This permanently deletes your account and conversations. Type  delete  to continue: ").strip().lower() != "delete":
                print("Cancelled.")
                return 1
            client.delete_account(ask_secret("Password: "))
            print("Account deleted.")
        elif c == "recover":
            r = client.recover(input("Username: ").strip(), input("Recovery code: ").strip(), ask_secret("New password: "))
            print(f"Password changed. Your NEW recovery code (the old one no longer works): {r['recovery_code']}")
        elif c == "chat":
            history: list[dict] = []
            cid = None
            prompts = [args.prompt] if args.prompt else None
            while True:
                text = prompts.pop(0) if prompts else (input("you> ") if args.prompt is None else None)
                if text is None or not text.strip():
                    break
                history.append({"role": "user", "content": text})
                r = client.chat(history, cid)
                cid = r["conversation_id"]
                history.append({"role": "assistant", "content": r["reply"]})
                print(r["reply"])
    except ServiceError as e:
        print(f"{e.message}", file=sys.stderr)
        return 2 if e.code in ("no_model", "auth_required", "terms_changed", "unreachable") else 3
    except NetworkDisabled as e:
        print(f"{e}", file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    return 0


def _personality_cmd(args, cfg: Config) -> int:
    from jarvis.personality import MODES, PersonalityStore
    st = PersonalityStore(cfg.profile_dir / "personality.json")
    c = args.pcmd
    try:
        if c == "set-mode":
            st.set_mode(args.mode)
        elif c == "set-trait":
            st.set_trait(args.trait, args.value)
        elif c == "approve":
            st.approve(args.id)
        elif c == "reject":
            st.reject(args.id)
        elif c == "instructions":
            print(st.profile.behavior_instructions())
            return 0
    except (ValueError, KeyError) as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2
    if c == "pending":
        _dump(st.pending)
    else:
        _dump({"mode": st.profile.mode, "traits": st.profile.traits, "available_modes": list(MODES),
               "pending_proposals": len(st.pending),
               "note": "Style only. Changes apply when you set them or approve a proposal; model weights are never altered."})
    return 0


def _persona_cmd(args, cfg: Config) -> int:
    from jarvis.personality.persona import Persona
    pe = Persona(cfg.profile_dir)
    c = args.ppcmd
    try:
        if c == "set":
            pe.set_style(args.style)
        elif c == "normal":
            pe.return_to_normal()
        elif c == "reset":
            pe.reset()
        elif c == "level":
            pe.set_level(args.name, args.value)
        elif c == "adult":
            if args.setting == "on" and not args.confirm_adult:
                print("error: adult-only styles need an explicit opt-in: add --confirm-adult to confirm you are 18 or older.", file=sys.stderr)
                return 2
            pe.set_adult_optin(args.setting == "on")
        elif c == "option":
            v: object = args.value
            if args.name == "minutes":
                v = int(v)
            elif args.name == "hands_busy":
                v = str(v).lower() in ("1", "true", "yes", "on")
            pe.set_option(**{args.name: v})
        elif c == "preview":
            _dump(pe.preview(args.style))
            return 0
        elif c == "selfcheck":
            return _persona_selfcheck(args, cfg)
        elif c == "list":
            _dump(pe.listing())
            return 0
    except (ValueError, TypeError) as exc:
        print(f"error: {exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 2
    from dataclasses import asdict
    _dump({"indicator": pe.indicator(), "settings": asdict(pe.settings), "note": pe.load_note or "Style only: wording and tone. It never grants a permission or approves an action."})
    return 0


def _persona_selfcheck(args, cfg: Config) -> int:
    from jarvis.assistant import LocalAssistant, NoActiveModel
    from jarvis.inference.gguf_engine import RuntimeUnavailable
    from jarvis.personality import selfcheck
    try:
        a = LocalAssistant(cfg, guest=True)           # guest: no saved memory or settings are read or changed
    except (NoActiveModel, RuntimeUnavailable, IntegrityError) as exc:
        print(f"Cannot run the self-check: {exc}", file=sys.stderr)
        return 2
    try:
        rep = selfcheck.run(a, styles=args.styles or None, say=lambda s: print(" ", s, flush=True))
    finally:
        a.close()
    path = selfcheck.save(rep, cfg.data_dir)
    print(f"{rep['automatic_checks_passed']}/{rep['automatic_checks_total']} automatic checks passed. Saved: {path}\n"
          "These checks do not judge tone: open the file and read each reply to decide whether the style sounds right.")
    return 0 if rep["automatic_checks_passed"] == rep["automatic_checks_total"] else 1


_LAST: dict = {}


def main(argv: list[str] | None = None) -> int:
    _LAST.clear()
    from jarvis.memory.store import MemoryDatabaseError
    try:
        rc = _main(argv)
    except MemoryDatabaseError as exc:          # a damaged memory file: say so and leave it untouched (found by the Prompt 11 audit)
        print(f"error: {exc}", file=sys.stderr)
        rc = 2
    # Optional telemetry (a silent no-op unless the user agreed): queue usage, then try to send. Never affects the result.
    try:
        cfg, cmd = _LAST.get("cfg"), _LAST.get("cmd")
        if cfg is not None and cmd in ("chat", "selftest", "setup", "voice") and not _LAST.get("guest"):
            from jarvis.telemetry.client import Telemetry, flush_quietly
            tel = Telemetry(cfg)
            if tel.consent.state()["enabled"]:
                feature = {"voice": "voice"}.get(cmd, cmd)
                tel.record("feature_used", feature=feature, count=1)
                if rc != 0 and cmd in ("chat", "selftest"):
                    tel.record("error", component="selftest" if cmd == "selftest" else "chat", code=_LAST.get("err", "unknown"))
                flush_quietly(cfg)
    except Exception:
        pass
    return rc


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = _cfg(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _LAST.update(cfg=cfg, cmd=args.cmd, guest=bool(args.guest))
    if args.cmd == "memory":
        from jarvis.memory.cli_memory import run as _memory_run
        return _memory_run(args, cfg)
    if args.cmd in ("plugin", "skill", "research"):
        from jarvis.dev.cli_dev import run as _dev_run
        return _dev_run(args, cfg)
    if args.cmd in ("mission", "schedule"):
        from jarvis.cli_missions import run as _mission_run
        return _mission_run(args, cfg)
    if args.cmd in ("mode", "monitor", "gaming"):
        from jarvis.cli_p8 import run as _p8_run
        return _p8_run(args, cfg)
    if args.cmd in ("desktop", "agent", "_desktop-worker"):
        from jarvis.desktop.cli_desktop import run as _desktop_run
        return _desktop_run(args, cfg)

    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "diagnostics":
        from jarvis.app import diagnostics as _dg
        rep = _dg.collect(cfg)
        print(_dg.render_text(rep))
        return 0
    if args.cmd == "gui":
        from jarvis.ui.app import run_gui
        return run_gui(cfg, guest=bool(args.guest), simulate=bool(getattr(args, "simulate", False)))
    if args.cmd == "first-run":
        _dump(first_run(cfg))
        return 0
    if args.cmd == "setup":
        from jarvis.setup_wizard import run_wizard
        rc = run_wizard(cfg)
        try:
            from jarvis.telemetry.client import Telemetry
            tel = Telemetry(cfg)
            if tel.consent.state()["enabled"]:
                tel.record("app_start", **tel.app_start_data())
        except Exception:
            pass
        return rc
    if args.cmd == "hardware":
        _dump(recommend(detect_hardware(cfg.data_dir if cfg.data_dir.exists() else ".")))
        return 0

    if args.cmd == "telemetry":
        return _telemetry_cmd(args, cfg)
    if args.cmd == "voice":
        return _voice_cmd(args, cfg)
    if args.cmd == "personality":
        return _personality_cmd(args, cfg)
    if args.cmd == "persona":
        return _persona_cmd(args, cfg)
    if args.cmd == "startup":
        return _startup_cmd(args)
    if args.cmd == "online":
        if args.guest:
            print("Guest mode is a private local session: the online service (accounts, sign-in, chat) is not available "
                  "in it. Run without --guest to use it.", file=sys.stderr)
            return 2
        return _online_cmd(args, cfg)

    policy = NetworkPolicy.load(cfg.settings_path)
    if args.cmd == "privacy":
        if args.action in ("enable", "disable"):
            if not args.feature:
                print("name a feature", file=sys.stderr)
                return 2
            if args.action == "enable":
                if args.feature == "telemetry":
                    print("Telemetry needs your informed agreement. Run: jarvis telemetry enable", file=sys.stderr)
                    return 2
                if args.feature not in IMPLEMENTED_FEATURES:
                    print(f"'{args.feature}' has no implementation in this build, so it cannot be enabled.",
                          file=sys.stderr)
                    return 2
                policy.enable(args.feature)
            else:
                policy.disable(args.feature)
            cfg.data_dir.mkdir(parents=True, exist_ok=True)
            policy.save(cfg.settings_path)
        _dump({"online_features": {f: policy.is_enabled(f) for f in FEATURES},
               "implemented": list(IMPLEMENTED_FEATURES),
               "note": "Local inference never uses the network. Features not listed as implemented do not exist yet."})
        return 0

    store = ModelStore(cfg.models_dir, policy)
    if args.cmd == "setup-auto":
        from jarvis.app.setup_engine import SetupEngine
        eng = SetupEngine(cfg, store, policy)
        shown = {}

        def consent(info: dict) -> bool:
            shown.update(info)
            print(f"Model: {info['model']}  download: about {info['download_gb']} GB  licence: {info['license']} ({info['license_url']})", flush=True)
            if not args.accept_download:
                print("Not downloading: pass --accept-download to consent.", file=sys.stderr)
            return bool(args.accept_download)
        rep = eng.run(args.model, confirm_download=consent, progress=lambda st, fr, msg: print(f"[{st}] {msg}", flush=True) if fr is None or int(fr * 100) % 10 == 0 else None)
        _dump({k: rep.get(k) for k in ("completed", "real", "model_id", "failed", "summary")})
        return 0 if rep.get("completed") else 3
    if args.cmd == "runtime":
        from jarvis.inference.gguf_engine import runtime_info
        info = runtime_info()
        _dump(info)
        return 0 if info["available"] or not args.require else 3
    if args.cmd == "selftest":
        return _selftest(cfg, store)
    if args.cmd == "perf":
        from jarvis.eval import perf
        return perf.run_cli(cfg.data_dir, Path(args.out) if args.out else None, args.runs, args.warmup, args.max_tokens)
    if args.cmd == "bench-security":
        from jarvis.eval import security_bench
        if not 0 <= args.sampled_runs <= 4:
            print("--sampled-runs must be between 0 and 4", file=sys.stderr)
            return 2
        return security_bench.run_cli(cfg.data_dir, args.model, Path(args.out) if args.out else None, args.sampled_runs,
                                      [x for x in (args.only or "").split(",") if x] or None, args.max_minutes, args.label,
                                      args.prompt_variant, args.max_tokens)
    if args.cmd == "bench-intel":
        from jarvis.eval import intel_bench
        if not 0 <= args.sampled_runs <= 4:
            print("--sampled-runs must be between 0 and 4", file=sys.stderr)
            return 2
        return intel_bench.run_cli(cfg.data_dir, args.model, Path(args.out) if args.out else None, args.sampled_runs,
                                   [x for x in (args.only or "").split(",") if x] or None, args.max_minutes, args.label,
                                   args.prompt_variant, args.sampling_profile, args.thinking, args.max_tokens)
    if args.cmd == "models" and args.mcmd in ("catalog", "fetch", "add-gguf", "plan"):
        return _models_gguf_cmd(args, cfg, store, policy)
    if args.cmd == "models":
        try:
            if args.mcmd == "list":
                _dump({"installed": store.installed(), "active": store.active(),
                       "disk_usage_bytes": store.disk_usage_bytes()})
            elif args.mcmd == "install":
                _dump(store.install_package(args.package, args.id, expected_sha256=args.sha256,
                                            confirm_unlisted=args.accept_unverified))
            elif args.mcmd == "verify":
                ok, problems = store.verify(args.id)
                _dump({"ok": ok, "problems": problems})
                return 0 if ok else 3
            elif args.mcmd == "select":
                store.set_active(args.id)
                print(f"active model: {args.id}")
            elif args.mcmd == "remove":
                print("removed" if store.remove(args.id) else "not installed")
            elif args.mcmd == "rollback":
                print("rolled back" if store.rollback(args.id) else "no previous version to roll back to")
        except (IntegrityError, ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        return 0

    if args.cmd == "chat":
        active = store.active()
        if active is None:
            print("No model is installed or selected. Run `jarvis setup` (or `jarvis models fetch qwen3-1.7b-q4_k_m`). "
                  "The assistant cannot answer without a model.", file=sys.stderr)
            return 2
        if store.kind(active) == "gguf":
            return _chat_gguf(args, cfg, store, active)
        if not args.prompt:
            print("This model type needs a prompt: jarvis chat \"text\"", file=sys.stderr)
            return 2
        ok, problems = store.verify(active)
        if not ok:
            print(f"Model {active} failed its integrity check: {problems}", file=sys.stderr)
            return 3
        from jarvis.inference.engine import InferenceEngine, ModelNotAvailable
        try:
            engine = InferenceEngine(store.path(active))
        except ModelNotAvailable as exc:
            print(f"Cannot run the model: {exc}", file=sys.stderr)
            return 2
        text, logp = engine.generate_scored(args.prompt, args.max_new_tokens, args.temperature)
        d = AnswerGate().decide(text, logp)
        print(d.text if d.answered else ABSTAIN_MESSAGE)
        return 0

    if args.cmd == "update-check":
        from jarvis.net.policy import NetworkDisabled
        from jarvis.updater.check import UpdateError, check_for_update
        try:
            info = check_for_update(policy, __version__, args.url, bytes.fromhex(args.public_key_hex))
        except NetworkDisabled as exc:
            print(f"{exc}. Enable it with `jarvis privacy enable update_check`.", file=sys.stderr)
            return 2
        except (UpdateError, ValueError, OSError) as exc:
            print(f"update check failed: {exc}", file=sys.stderr)
            return 3
        _dump(asdict(info) if info else {"update_available": False})
        return 0

    if args.cmd in ("backup", "restore") and args.guest:
        print("Guest mode keeps nothing on disk and must not read or replace saved memory, so backup and restore are not available in it.",
              file=sys.stderr)
        return 2
    if args.cmd == "backup":
        db = cfg.memory_db
        if not db.exists():
            MemoryStore(db).close()
        print(backup_sqlite(db, cfg.backups_dir))
        return 0

    if args.cmd == "restore":
        from jarvis.storage import restore_sqlite
        try:
            if cfg.memory_db.exists():
                print(f"safety copy of current database: {backup_sqlite(cfg.memory_db, cfg.backups_dir)}")
            restore_sqlite(args.backup_file, cfg.memory_db)
        except Exception as exc:
            print(f"restore failed: {exc}", file=sys.stderr)
            return 3
        print("restored")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
