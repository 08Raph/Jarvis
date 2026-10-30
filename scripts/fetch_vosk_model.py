"""Fetch the small English Vosk speech model for the installer build, or PROBE it once to learn its checksums.

  python scripts/fetch_vosk_model.py --probe            ONE download; prints size, SHA-256, folder digest, file list and the licence line from the publisher's page.
                                                         Enforces nothing. Used once to obtain the pins in src/jarvis/voice/bundled_vosk.py.
  python scripts/fetch_vosk_model.py --out DIR          ONE download; verifies the zip (size + SHA-256) and the extracted folder (digest + file count) against the pins,
                                                         then writes DIR/vosk-model-small-en-us-0.15/. Refuses while the pins are empty.
  --zip FILE                                             use a local zip instead of downloading (tests, offline rebuilds).

The download is from the model's publisher over HTTPS and is made only when this script is run by a person's build; the app itself never downloads a speech model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from jarvis.voice import bundled_vosk as V  # noqa: E402

MAX_ZIP = 150 * 1024 * 1024
MAX_UNPACKED = 400 * 1024 * 1024


def download(url: str, dst: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "ProjectJarvis-build/1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:      # noqa: S310 - fixed https URL
        total = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ZIP:
                raise SystemExit(f"the download is larger than {MAX_ZIP} bytes; stopped")
            f.write(chunk)


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_extract(zpath: Path, dest: Path) -> Path:
    """Extract with path checks (no absolute paths, no '..', no links); returns the single top-level model folder."""
    with zipfile.ZipFile(zpath) as z:
        total = 0
        for i in z.infolist():
            n = i.filename
            if n.startswith(("/", "\\")) or ".." in Path(n).parts or re.match(r"^[A-Za-z]:", n) or (i.external_attr >> 28) == 0xA:
                raise SystemExit(f"unsafe path in the zip: {n!r}")
            total += i.file_size
            if total > MAX_UNPACKED:
                raise SystemExit("the zip unpacks to more than the allowed size; stopped")
        z.extractall(dest)
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1 or tops[0].name != V.MODEL_NAME or any(p.is_file() for p in dest.iterdir()):
        raise SystemExit(f"expected exactly one folder named {V.MODEL_NAME} in the zip, found {[p.name for p in dest.iterdir()]}")
    return tops[0]


def page_licence_lines() -> list[str]:
    try:
        req = urllib.request.Request(V.MODELS_PAGE, headers={"User-Agent": "ProjectJarvis-build/1"})
        with urllib.request.urlopen(req, timeout=60) as r:       # noqa: S310
            html = r.read(3_000_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return [f"(could not read {V.MODELS_PAGE}: {type(e).__name__})"]
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    out = []
    for m in re.finditer(re.escape(V.MODEL_NAME), text):
        out.append("…" + text[max(0, m.start() - 120): m.end() + 260] + "…")
        if len(out) >= 2:
            break
    out += ["page mentions: " + ", ".join(sorted(set(x.strip() for x in re.findall(r"(Apache[- ]?2\.0|MIT|GPL[- ]?\d?|CC[- ]BY[\w\- .]*|Unlicense)", text))))]
    return out or ["(model name not found on the page)"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--zip")
    a = ap.parse_args(argv)
    if not a.probe and not a.out:
        ap.error("give --probe or --out DIR")
    work = Path(tempfile.mkdtemp(prefix="jarvis-vosk-"))
    try:
        if a.zip:
            zpath = Path(a.zip)
        else:
            zpath = work / "model.zip"
            print(f"downloading {V.SOURCE_URL}")
            download(V.SOURCE_URL, zpath)
        zbytes, zsha = zpath.stat().st_size, sha256_file(zpath)
        folder = safe_extract(zpath, work / "x")
        digest, count = V.tree_digest(folder)
        if a.probe:
            files = sorted((p.relative_to(folder).as_posix(), p.stat().st_size) for p in folder.rglob("*") if p.is_file())
            lic = [f for f, _ in files if re.search(r"licen[cs]e|readme|copying|notice", f, re.I)]
            report = {"url": V.SOURCE_URL, "zip_bytes": zbytes, "zip_sha256": zsha, "file_count": count, "tree_sha256": digest,
                      "licence_or_readme_files_in_zip": lic, "files": files, "page_licence_lines": page_licence_lines()}
            for f in lic[:3]:
                try:
                    report["text_of_" + f] = (folder / f).read_text(encoding="utf-8", errors="replace")[:1500]
                except OSError:
                    pass
            txt = json.dumps(report, indent=2)
            print(txt)
            if os.environ.get("GITHUB_ACTIONS"):      # annotations are readable through the checks API even when the raw job log is not
                esc = lambda s: s.replace("%", "%25").replace("\r", "").replace("\n", "%0A")      # noqa: E731
                head = {k: report[k] for k in ("url", "zip_bytes", "zip_sha256", "file_count", "tree_sha256", "licence_or_readme_files_in_zip")}
                print("::notice title=Vosk probe values::" + esc(json.dumps(head, indent=1)))
                print("::notice title=Vosk probe licence lines::" + esc(json.dumps(report["page_licence_lines"], indent=1))[:3000])
                print("::notice title=Vosk probe files::" + esc("\n".join(f"{n} {s}" for n, s in files))[:6000])
                for k, v in report.items():
                    if k.startswith("text_of_"):
                        print(f"::notice title=Vosk probe {k[:60]}::" + esc(v)[:1500])
            s = os.environ.get("GITHUB_STEP_SUMMARY")
            if s:
                with open(s, "a", encoding="utf-8") as fh:
                    fh.write("### Vosk small English model - probe (one download, nothing enforced)\n```json\n" + txt + "\n```\n")
            return 0
        if not V.pinned() or not V.ZIP_PIN_SHA256 or not V.ZIP_PIN_BYTES:
            print("ERROR: the speech model has no pinned checksum in this version of the program; refusing to bundle an unverified folder.", file=sys.stderr)
            return 2
        if zbytes != V.ZIP_PIN_BYTES or zsha != V.ZIP_PIN_SHA256:
            print(f"ERROR: the downloaded zip is {zbytes} bytes / {zsha}, not the pinned {V.ZIP_PIN_BYTES} / {V.ZIP_PIN_SHA256}", file=sys.stderr)
            return 3
        if digest != V.PIN_TREE_SHA256 or count != V.PIN_FILE_COUNT:
            print(f"ERROR: the unpacked folder ({count} files, {digest}) does not match the pin ({V.PIN_FILE_COUNT} files, {V.PIN_TREE_SHA256})", file=sys.stderr)
            return 3
        out = Path(a.out) / V.MODEL_NAME
        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, out)
        print(f"bundled speech model written: {out} ({count} files)")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
