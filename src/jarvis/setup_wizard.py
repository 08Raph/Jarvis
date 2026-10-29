"""Guided first-run setup for people who double-click the installed app. Console based, local only.
It never touches the network and never enables voice or any online feature: it explains and points to the commands."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from jarvis import __version__
from jarvis.config import Config
from jarvis.firstrun import first_run
from jarvis.models.manager import IntegrityError, ModelStore
from jarvis.net.policy import NetworkPolicy


def _model_id(path: str) -> str:
    stem = re.sub(r"[^a-z0-9._-]+", "-", Path(path).stem.lower()).strip("-.")
    return stem[:40] or "model"


def _hw_from(rep: dict):
    from jarvis.hardware import Hardware
    h = rep["hardware"]["hardware"]
    return Hardware(os=h.get("os", ""), cpu_threads=h.get("cpu_threads", 1), ram_gb=h.get("ram_gb"),
                    free_disk_gb=h.get("free_disk_gb"), gpu_name=h.get("gpu_name"), vram_gb=h.get("vram_gb"))


def _download_flow(cfg, store, policy, model, fit, ask, say) -> None:
    if fit["verdict"] == "too_large":
        say("   This PC does not meet the estimated needs for this model, so it was not downloaded.")
        return
    try:
        ok = ask(f"   Download {model.title} (~{model.approx_size_bytes / 1e9:.1f} GB) from huggingface.co? Type yes: ").strip().lower() == "yes"
    except EOFError:
        ok = False
    if not ok:
        say("   Skipped. Later: jarvis models fetch " + model.id)
        return
    try:
        with policy.grant_once("model_download"):
            say("   Downloading... (this can take a while; keep the window open)")
            marks = [0]

            def progress(done, total):
                pct = int(100 * done / total) if total else 0
                if pct >= marks[0] + 10:       # one line per 10 percent, so a slow link does not look frozen
                    marks[0] = pct - pct % 10
                    say(f"   ... {marks[0]}%  ({done / 1e6:,.0f} MB)")
            meta = store.download_gguf(model, progress)
        store.set_active(meta["id"])
        say(f"   Downloaded, checked against the publisher's checksum, installed and selected: '{meta['id']}'.")
        say("   It has NOT been loaded yet. Run  jarvis selftest  to check that it loads and answers on this PC.")
    except Exception as exc:
        say(f"   Download did not complete: {type(exc).__name__}: {exc}")
        say("   Nothing was installed. See docs\\LOCAL_MODEL.md for the manual download steps.")


def _gguf_file_flow(store, path, ask, say) -> None:
    try:
        spdx = ask("   Which license is this model published under (e.g. Apache-2.0, MIT)? ").strip()
        where = ask("   Where can its license be read (web address or file path)? ").strip()
        sha = ask("   Its SHA-256 from the publisher (Enter if none): ").strip().lower()
        unverified = False
        if not sha:
            unverified = ask("   Install without a checksum? Type yes: ").strip().lower() == "yes"
        if not spdx or not where or not (sha or unverified):
            say("   Skipped (license and either a checksum or your confirmation are required).")
            return
        meta = store.install_gguf(path, _model_id(path), spdx, f"License reference: {where}", where, sha or None)
        store.set_active(meta["id"])
        say(f"   Installed and selected '{meta['id']}'. Run  jarvis selftest  to check it works on this PC.")
    except (IntegrityError, ValueError, OSError, EOFError) as exc:
        say(f"   Could not install the model: {exc}")


def run_wizard(cfg: Config, ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> int:
    raw_ask = ask

    def ask(prompt: str) -> str:
        # Windows PowerShell can put a UTF-8 byte-order mark in front of text piped into a program (`'' | jarvis.exe setup`). U+FEFF is not whitespace,
        # so an otherwise empty answer was taken as a file name ("File not found: <BOM>"). Drop a leading mark; nothing else is changed.
        return raw_ask(prompt).lstrip("\ufeff")

    say(f"PROJECT JARVIS {__version__} - setup guide")
    say("Everything runs on this computer. No account, no subscription, no paid service.\n")
    rep = first_run(cfg)
    hw = rep["hardware"]["hardware"]
    say(f"1. Your data folder: {rep['data_dir']}")
    say(f"   (models, memory, settings and backups live here; uninstalling the app does not delete it silently)")
    say(f"2. Your computer: RAM {hw.get('ram_gb')} GB, free disk {hw.get('free_disk_gb')} GB.")
    if hw.get("gpu_name"):
        say(f"   Graphics card: {hw['gpu_name']} (a GPU is optional; the free model runs on the CPU).")
    else:
        say("   No NVIDIA graphics card detected. That is fine: the free model runs on the CPU.")
    say("3. Privacy: every online feature is OFF. Nothing is sent anywhere. Check any time with: jarvis privacy")

    policy = NetworkPolicy.load(cfg.settings_path)
    store = ModelStore(cfg.models_dir, policy)
    say("")
    if store.active():
        say(f"4. Model: '{store.active()}' is installed and selected.")
    else:
        from jarvis.hardware import assess_gguf
        from jarvis.models import catalog
        rec_model = catalog.BUILTIN[0]
        fit = assess_gguf(_hw_from(rep), rec_model.approx_size_bytes, rec_model.min_ram_gb)
        say("4. Model: none installed yet, and the assistant cannot answer without one. Nothing is bundled in this installer.")
        say(f"   Free option: {rec_model.title}, license {rec_model.license_spdx}, about {rec_model.approx_size_bytes / 1e9:.1f} GB download.")
        say(f"   Fit for this PC: {fit['verdict']} - {'; '.join(fit['reasons'])}")
        say("   Type  download  to fetch it from huggingface.co now (needs internet once; your chats are never sent),")
        say("   or give the path to a .gguf / .jarvismodel file you already have.")
        try:
            path = ask("   download / path (press Enter to skip): ").strip().strip('"')
        except EOFError:
            path = ""
        if path.lower() == "download":
            _download_flow(cfg, store, policy, rec_model, fit, ask, say)
        elif path:
            if not Path(path).is_file():
                say(f"   File not found: {path}. Skipped. You can run this guide again with: jarvis setup")
            elif path.lower().endswith(".gguf"):
                _gguf_file_flow(store, path, ask, say)
            else:
                try:
                    sha = ask("   Its SHA-256 from whoever gave it to you (Enter if you don't have one): ").strip().lower()
                    unverified = False
                    if not sha:
                        unverified = ask("   Without a hash the file's origin cannot be checked. Install anyway? Type yes: ").strip().lower() == "yes"
                        if not unverified:
                            say("   Skipped.")
                    if sha or unverified:
                        mid = _model_id(path)
                        info = store.install_package(path, mid, expected_sha256=sha or None, confirm_unlisted=unverified)
                        store.set_active(mid)
                        say(f"   Installed and selected '{mid}' (licence: {info.get('license', 'see LICENSE.txt in the package')}).")
                except (IntegrityError, ValueError, OSError, EOFError) as exc:
                    say(f"   Could not install the model: {exc}")
    say("")
    say("5. Voice is OFF by default and the microphone is not used. To enable it later, read docs\\VOICE.md (installed under the app folder, in _internal\\docs).")
    from jarvis.telemetry.client import Telemetry, get_endpoint
    if get_endpoint() and not Telemetry(cfg).consent.state()["enabled"]:
        say("\n6. Optional: anonymous diagnostics. Off unless you choose. It never includes your chats or files.")
        try:
            if ask("   Type  review  to read exactly what would be sent, or press Enter to keep it OFF: ").strip().lower() == "review":
                say("   Run  jarvis telemetry enable  in this window to read the notice and decide. Nothing is sent until you agree.")
        except EOFError:
            pass
    say("\nUseful commands (type them in this window):")
    say("   jarvis chat        jarvis selftest        jarvis hardware        jarvis models list        jarvis privacy        jarvis setup")
    return 0
