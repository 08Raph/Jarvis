"""Release helpers: checksums, signing keys, signed update manifests, model packages, notices, requirements doc.

Usage: python scripts/release_tools.py <command> ...
The signing PRIVATE key must stay offline; this repo contains no keys.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_release_assets as CRA  # noqa: E402
from jarvis import __version__  # noqa: E402
from jarvis.version import RELEASE_CHANNEL  # noqa: E402
from jarvis.hardware import TIERS  # noqa: E402
from jarvis.models.package import pack_model  # noqa: E402
from jarvis.security import signing  # noqa: E402

# (distribution name, what it is used for, obligation note)
RUNTIME_DEPENDENCIES = [
    ("numpy", "numerical arrays", "BSD-style; keep copyright notice"),
    ("cryptography", "signature verification", "Apache-2.0 OR BSD-3-Clause; keep notices"),
    ("torch", "model training/inference (optional extra)", "BSD-3-Clause; a CUDA build also redistributes NVIDIA "
     "libraries under NVIDIA's own license terms: prefer the CPU-only build for distribution and verify"),
    ("PySide6", "desktop window (jarvis-gui.exe; optional extra [ui], bundled in the Windows build)", "LGPL-3.0 / GPL / commercial (Qt): LGPL use needs the "
     "license text, notices, and the ability for users to replace Qt libraries (one-folder build helps). Verify with counsel"),
    ("llama-cpp-python", "local AI runtime (llama.cpp bindings)", "MIT; bundles llama.cpp (MIT) and ggml. Keep notices"),
    ("sounddevice", "microphone access (optional voice extra)", "MIT; bundles/uses PortAudio (MIT-style). Verify before bundling"),
    ("vosk", "offline speech recognition (optional voice extra)", "Apache-2.0 for the library; each Vosk MODEL has its own "
     "licence: check it before redistributing any model"),
    ("kokoro-onnx", "natural local voices (Kokoro; bundled when built with -WithKokoro)", "MIT; the Kokoro-82M voice model it uses is Apache-2.0 (downloaded by the person on the Voice page, not shipped)"),
    ("onnxruntime", "runs the Kokoro voice model on the CPU (bundled with Kokoro)", "MIT"),
    ("phonemizer-fork", "text to phonemes for Kokoro (bundled with Kokoro)", "GPL-3.0 (see docs/GPL_NOTICE.md; verify with counsel)"),
    ("espeakng-loader", "ships the espeak-ng library and data for phonemizer (bundled with Kokoro)", "its own metadata, plus espeak-ng itself which is GPL-3.0 (see docs/GPL_NOTICE.md; verify with counsel)"),
    ("comtypes", "Windows UI Automation access for desktop control (Windows only)", "MIT; keep notice"),
    ("psutil", "optional network interface state for system_info (not bundled unless installed)", "BSD-3-Clause; keep notice"),
    ("pyinstaller", "build tool (not shipped, but its bootloader is embedded)", "GPL-2.0 with bootloader exception; verify"),
]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def write_sha256sums(directory: Path, out: Path) -> int:
    lines = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p != out:
            lines.append(f"{sha256_file(p)}  {p.relative_to(directory).as_posix()}")
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def make_update_manifest(payload: dict, private_raw: bytes) -> dict:
    return {"payload": payload, "signature": signing.sign(payload, private_raw)}


def _license_of(name: str) -> str:
    try:
        md = metadata.metadata(name)
    except metadata.PackageNotFoundError:
        return ""
    lic = md.get("License-Expression") or ""
    if not lic:
        raw = md.get("License") or ""
        lic = raw.splitlines()[0][:120] if raw and len(raw) < 400 else ""
    if not lic:
        lic = "; ".join(c.split("::")[-1].strip() for c in md.get_all("Classifier", []) if c.startswith("License ::"))
    return lic or "(license field not provided by package metadata; check the project)"


def generate_notices(out: Path) -> str:
    L = ["# Third-party notices", "",
         f"Project JARVIS {__version__} is released under the MIT License (see LICENSE).", "",
         "No training datasets are bundled. Installers built with -BundleModel include one model file, the free chat model Qwen3-1.7B "
         "Q4_K_M (Apache-2.0, by the Qwen team / Alibaba Cloud; GGUF conversion by the llama.cpp organisation), which keeps its own license "
         "(shown with the installed model); other builds include no model weights and the app can download that model from its publisher on the "
         "user's request. "
         "Any model package carries its own LICENSE.txt, which you must read before installing or redistributing it.", "",
         "Installers built with -WithKokoro also include the natural-voice engine, which contains GPL-3.0 licensed components (espeak-ng and the phonemizer package); "
         "their licence text, the source offer and your rights to replace them are in docs/GPL_NOTICE.md.", "",
         "Installers built with -BundleVoiceModel also include the speech-recognition model **vosk-model-small-en-us-0.15** (folder "
         "`bundled_models/vosk-model-small-en-us-0.15`): Copyright 2020 Alpha Cephei Inc; listed as Apache 2.0 on the publisher's page "
         "https://alphacephei.com/vosk/models ; licence text https://www.apache.org/licenses/LICENSE-2.0 . Its own README ships in the folder.", "",
         "## Runtime and build dependencies", "",
         "Versions and licenses below were read from the packages installed in the build environment when this "
         "file was generated. Regenerate it on the build machine (`python scripts/release_tools.py notices`).", ""]
    for name, use, note in RUNTIME_DEPENDENCIES:
        try:
            ver = metadata.version(name)
            L.append(f"- **{name} {ver}**: {use}. License metadata: {_license_of(name)}. Note: {note}.")
        except metadata.PackageNotFoundError:
            L.append(f"- **{name}**: {use}. NOT INSTALLED when this file was generated, so version and license "
                     f"were not read. Note: {note}.")
    L += ["", "Python itself is distributed under the PSF License. This summary is informational, not legal advice.", ""]
    text = "\n".join(L)
    out.write_text(text, encoding="utf-8")
    return text


def requirements_doc() -> str:
    L = ["# Model sizes and estimated requirements", "",
         "ESTIMATES from parameter counts (fp32 weights x 1.25 + 0.5 GB runtime allowance for RAM). "
         "Nothing here has been measured on real hardware, and no trained model exists yet; these are planned sizes.",
         "Speed (tokens per second) is NOT measured. A GPU is optional; only NVIDIA GPUs are auto-detected.", "",
         "| Tier | Config (layers / width / context / vocab) | Parameters | Model file | Min RAM | Recommended RAM | Min VRAM if GPU |",
         "|---|---|---|---|---|---|---|"]
    for t in TIERS:
        L.append(f"| {t.name} | {t.layers} / {t.embd} / {t.block} / {t.vocab} | {t.params / 1e6:.1f}M | "
                 f"{t.model_file_gb * 1000:.0f} MB | {t.min_ram_gb:.1f} GB | {t.recommended_ram_gb:.1f} GB | "
                 f"{t.min_vram_gb:.1f} GB |")
    L += ["", "Disk: model file x 2 free space is required during install (the previous version is kept for rollback). "
          "The application itself (EXE + bundled Python/PyTorch runtime) has not been built, so its size is unknown; "
          "a PyTorch-based build is expected to be hundreds of MB.", "",
          "CPU: any 64-bit Windows 11 CPU; larger tiers will be slow on CPU. First run: `jarvis first-run` creates folders, "
          "detects hardware, and installs nothing. Then install a model package with `jarvis models install`.", ""]
    return "\n".join(L)


def build_info(root: Path, env=None) -> dict:
    """Facts about THIS build, written to build_info.json and shown by `jarvis diagnostics`. Nothing is invented: a value that cannot be
    read is recorded as "unknown". SOURCE_DATE_EPOCH, when set, replaces the wall clock so two builds of one commit can be compared."""
    env = os.environ if env is None else env
    commit = "unknown"
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        pass

    def h(rel):
        f = root / rel
        return hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "unknown"
    epoch = env.get("SOURCE_DATE_EPOCH", "")
    when = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch.isdigit() else datetime.now(timezone.utc)
    lock = root / "requirements-lock.txt"
    return {"version": __version__, "channel": RELEASE_CHANNEL, "git_commit": commit,
            "built_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "source_date_epoch": epoch or None,
            "python": platform.python_version(), "platform": platform.platform(),
            "dependencies": installed_versions(),
            "spec_sha256": h("packaging/jarvis.spec"), "manifest_sha256": h("packaging/manifest.py"),
            "requirements_lock_sha256": h("requirements-lock.txt") if lock.is_file() else "unknown",
            "note": "Written by scripts/release_tools.py build-info. Not a signature and not a statement that the build was tested."}


KEY_PACKAGES = ["kokoro-onnx", "onnxruntime", "phonemizer-fork", "espeakng-loader", "numpy", "cryptography", "PySide6", "llama-cpp-python", "pyinstaller", "comtypes", "psutil", "pypdf", "sounddevice", "vosk", "torch"]


def installed_versions(names=KEY_PACKAGES, getter=metadata.version) -> dict:
    """{package: version or None}. None = not installed in the build environment (read from package metadata, not guessed)."""
    out = {}
    for n in names:
        try:
            out[n] = getter(n)
        except metadata.PackageNotFoundError:
            out[n] = None
    return out


def build_manifest(root: Path, dist: Path, installer_dir: Path | None = None, env=None, versions: dict | None = None) -> dict:
    """What THIS build produced: tool versions, a SHA-256 for every file of the application folder and for the installer. It is a record, not a
    signature and not a statement that the build was tested. Written next to (not inside) the application folder."""
    env = os.environ if env is None else env
    dist = Path(dist)
    files = CRA.hash_tree(dist)
    tree = hashlib.sha256("\n".join(f"{k} {files[k]}" for k in sorted(files)).encode()).hexdigest()
    vers = installed_versions() if versions is None else versions
    arts = {}
    if installer_dir and Path(installer_dir).is_dir():
        for p in sorted(Path(installer_dir).glob("*.exe")):
            arts[p.name] = {"sha256": CRA.sha256_of(p), "bytes": p.stat().st_size}
    exes = {n: {"sha256": files[n], "bytes": (dist / n).stat().st_size} for n in ("jarvis.exe", "jarvis-gui.exe") if n in files}
    bi = build_info(root, env)
    lock = root / "requirements-lock.txt"
    allowed = CRA.MF.BUNDLED_MODEL_RELPATH          # the one pinned model of a full installer; check_release_assets verifies its exact size and SHA-256
    weights = sorted(k for k in files if k.lower().endswith((".gguf", ".safetensors", ".pt", ".onnx", ".bin")) and (dist / k).stat().st_size > 50_000_000
                     and k.replace("\\", "/") != allowed
                     and not k.replace("\\", "/").startswith(CRA.MF.BUNDLED_VOSK_RELDIR + "/"))
    return {"schema": 1, "version": bi["version"], "channel": bi["channel"], "built_utc": bi["built_utc"], "git_commit": bi["git_commit"],
            "python": bi["python"], "build_platform": platform.system() + " " + platform.release(), "dependencies": vers,
            "requirements_lock_sha256": CRA.sha256_of(lock) if lock.is_file() else "unknown",
            "torch_bundled": bool(vers.get("torch")) and any(k.startswith(("torch/", "_internal/torch/")) for k in files),
            "model_weights_in_build": weights, "bundled_model": ({"path": allowed, "sha256": files[allowed], "bytes": (dist / allowed).stat().st_size} if allowed in {k.replace("\\", "/") for k in files} else None),
            "bundled_voice_model": _voice_model_record(dist), "executables": exes, "installer": arts, "file_count": len(files), "tree_sha256": tree, "files": files,
            "machine_paths_in_text_files": CRA.scan_machine_paths(dist, [str(root), str(Path.home())]),
            "note": "A record of what this build contained. Not a signature. It does not say the build was tested or that the installer works."}


def _voice_model_record(dist: Path):
    d = Path(dist) / CRA.MF.BUNDLED_VOSK_RELDIR
    if not d.is_dir():
        return None
    from jarvis.voice import bundled_vosk as VB
    digest, count = VB.tree_digest(d)
    return {"path": CRA.MF.BUNDLED_VOSK_RELDIR, "tree_sha256": digest, "file_count": count}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sha256sums")
    s.add_argument("directory")
    s.add_argument("out")
    k = sub.add_parser("gen-keys", help="create an Ed25519 signing keypair (keep the private key OFFLINE)")
    k.add_argument("--out-dir", required=True)
    m = sub.add_parser("sign-manifest")
    pg = m.add_mutually_exclusive_group(required=True)
    pg.add_argument("--payload-json", help="inline JSON (beware shell quoting on Windows PowerShell 5.1)")
    pg.add_argument("--payload-file", help="path to a JSON file (preferred; a UTF-8 BOM is accepted)")
    m.add_argument("--private-key-file", required=True)
    m.add_argument("--out", required=True)
    p = sub.add_parser("pack-model")
    p.add_argument("--model-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--license", required=True, help="SPDX id of the weights' license")
    n = sub.add_parser("notices")
    n.add_argument("--out", default="THIRD_PARTY_NOTICES.md")
    r = sub.add_parser("requirements-doc")
    r.add_argument("--out", default="docs/MODEL_REQUIREMENTS.md")
    b = sub.add_parser("build-info", help="write build_info.json (version, commit, time, tool versions)")
    b.add_argument("--out", default="build_info.json")
    bm = sub.add_parser("build-manifest", help="hash the built application folder and the installer into one JSON record")
    bm.add_argument("--dist", required=True)
    bm.add_argument("--installer-dir")
    bm.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "build-manifest":
        root = Path(__file__).resolve().parents[1]
        m = build_manifest(root, Path(a.dist), Path(a.installer_dir) if a.installer_dir else None)
        Path(a.out).write_text(json.dumps(m, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {a.out}: {m['file_count']} files, tree {m['tree_sha256'][:16]}..., torch bundled: {m['torch_bundled']}")
        if m["model_weights_in_build"]:
            print("WARNING: model weights are inside the build:", ", ".join(m["model_weights_in_build"]))
        return 1 if (m["model_weights_in_build"] or m["machine_paths_in_text_files"]) else 0
    if a.cmd == "build-info":
        root = Path(__file__).resolve().parents[1]
        Path(a.out).write_text(json.dumps(build_info(root), indent=2) + "\n", encoding="utf-8")
        print("wrote", a.out)
    elif a.cmd == "sha256sums":
        print(write_sha256sums(Path(a.directory), Path(a.out)), "files hashed")
    elif a.cmd == "gen-keys":
        d = Path(a.out_dir)
        d.mkdir(parents=True, exist_ok=True)
        sk, pk = signing.generate_keypair()
        (d / "jarvis_signing_private.hex").write_text(sk.hex())
        (d / "jarvis_signing_public.hex").write_text(pk.hex())
        print(f"public key (embed in the app): {pk.hex()}\nprivate key written to {d} - move it OFFLINE and never commit it")
    elif a.cmd == "sign-manifest":
        sk = bytes.fromhex(Path(a.private_key_file).read_text(encoding="utf-8-sig").strip())
        raw = a.payload_json if a.payload_json else Path(a.payload_file).read_text(encoding="utf-8-sig")
        Path(a.out).write_text(json.dumps(make_update_manifest(json.loads(raw), sk), indent=2))
    elif a.cmd == "pack-model":
        print(pack_model(a.model_dir, a.out, a.license))
    elif a.cmd == "notices":
        generate_notices(Path(a.out))
    elif a.cmd == "requirements-doc":
        Path(a.out).write_text(requirements_doc(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
