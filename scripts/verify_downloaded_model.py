"""Stage 5: verify a model file that a person has ALREADY downloaded (scratch folder) against the metadata record, before it is installed.

It never downloads anything and never installs anything. It reads the file once and checks, failing closed on anything missing or contradictory:
  1. the metadata record for the candidate exists and is METADATA_CONSISTENT (scripts/verify_model_metadata.py);
  2. the path is a regular file (not a link, folder or device);
  3. the exact byte size equals the metadata size and is within 115 % of it;
  4. the SHA-256 COMPUTED FROM THE ACTUAL BYTES equals the published metadata hash (and, if given, the value a person read on the model page);
  5. the file starts like a GGUF file (header parsed by jarvis.models.gguf);
  6. (optional) the model folder's drive has room for the add-gguf copy (diskspace.need_for_copy + margin).
On PASS it prints the exact `models add-gguf` command (with --sha256 and --expected-size-bytes) for a throw-away --data-dir. The default model and the existing
model store are not touched. A PASS means: these bytes equal what Hugging Face publishes. It does NOT mean the model works, is safe, or runs on Windows.

Usage: python scripts/verify_downloaded_model.py --file C:\\jarvis-eval\\dl\\Qwen3-8B-Q4_K_M.gguf --metadata C:\\jarvis-eval\\meta\\model_metadata.json
           --candidate qwen3-8b-official [--human-sha256 <value read on the model page>] [--data-dir C:\\jarvis-eval\\data] --out C:\\jarvis-eval\\meta
Exit codes: 0 PASS; 4 FAIL (any check).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.models import diskspace, gguf  # noqa: E402

HEX64 = re.compile(r"[0-9a-f]{64}")


def sha256_of(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def ps_quote(text: str) -> str:
    """A PowerShell SINGLE-quoted literal: nothing inside is expanded ($, backtick, $(...) stay literal); only ' needs doubling."""
    return "'" + str(text).replace("'", "''") + "'"


PATH_PLACEHOLDER = "<PATH-TO-DOWNLOADED-FILE>"
DATA_PLACEHOLDER = "<THROWAWAY-DATA-DIR>"


def add_gguf_command(candidate: str, computed: str, size: int, file_path: str = PATH_PLACEHOLDER, data_dir: str | None = None) -> str:
    """The exact PowerShell command for `models add-gguf`; values are single-quoted so paths with spaces, $ or ' are safe to paste.
    The model id is the candidate id (lowercase letters, digits, - and _ only, as `ModelStore` requires), so the command has no placeholder left except, when not supplied, the data folder."""
    cid = re.sub(r"[^a-z0-9_-]", "-", candidate.lower())
    return (f"python -m jarvis --data-dir {ps_quote(data_dir or DATA_PLACEHOLDER)} models add-gguf {ps_quote(file_path)} --id {cid} --license Apache-2.0 "
            f"--license-url https://www.apache.org/licenses/LICENSE-2.0 --sha256 {computed} --expected-size-bytes {size} --no-select")


def verify(file: Path, metadata: dict, candidate: str, human_sha256: str | None = None, data_dir: Path | None = None,
           free_fn=diskspace.free_bytes, hash_fn=sha256_of) -> dict:
    res = {"kind": "downloaded-file-verification", "candidate": candidate, "file_name": Path(file).name, "checks": [], "hash_computed_from_bytes": False,
           "verified_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def check(name: str, ok: bool, detail: str = "") -> bool:
        res["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def done(status: str) -> dict:
        res["status"] = status
        return res

    rec = next((r for r in metadata.get("results", []) if isinstance(r, dict) and r.get("id") == candidate), None)
    if not check("candidate_in_metadata", rec is not None, "" if rec else "unknown candidate id"):
        return done("FAIL")
    if not check("metadata_consistent", rec.get("status") == "METADATA_CONSISTENT", f"status={rec.get('status')} problems={rec.get('problems')}"):
        return done("FAIL")
    size, want = rec.get("size_bytes"), rec.get("published_sha256")
    if not check("metadata_has_size_and_hash", type(size) is int and size > 0 and isinstance(want, str) and bool(HEX64.fullmatch(want)), "size/hash missing or malformed"):
        return done("FAIL")
    if not check("file_name_matches_metadata", file.name == rec.get("file"), f"expected {rec.get('file')}"):
        return done("FAIL")
    try:
        st = file.lstat()
    except OSError:
        check("file_readable", False, "cannot stat the file")
        return done("FAIL")
    if not check("regular_file_not_a_link", stat.S_ISREG(st.st_mode), "must be a regular file, not a link, folder or device"):
        return done("FAIL")
    limit = diskspace.size_limit(size)
    if not check("size_within_115_percent", st.st_size <= limit, f"{st.st_size} bytes; limit {limit}"):
        return done("FAIL")
    if not check("size_equals_published_size", st.st_size == size, f"file {st.st_size} bytes, published {size}"):
        return done("FAIL")
    human = None
    if human_sha256 is not None:
        human = human_sha256.strip().lower()
        if not check("human_hash_well_formed", bool(HEX64.fullmatch(human)), "64 hexadecimal characters required"):
            return done("FAIL")
    computed = hash_fn(file)
    res["hash_computed_from_bytes"] = True
    res["computed_sha256"] = computed
    ok_pub = check("computed_hash_equals_published_hash", computed == want, f"published {want}")
    ok_hum = True
    if human is not None:
        ok_hum = check("computed_hash_equals_hash_read_on_model_page", computed == human, "value typed by a person")
    if not (ok_pub and ok_hum):
        return done("FAIL")
    try:
        gguf.read_header(file)
        check("gguf_header_valid", True)
    except Exception as exc:        # noqa: BLE001 - any parse problem is a failure
        check("gguf_header_valid", False, f"{type(exc).__name__}")
        return done("FAIL")
    if data_dir is not None:
        try:
            free = free_fn(data_dir)
            req = diskspace.required_free(diskspace.need_for_copy(st.st_size))
            if not check("room_for_add_gguf_copy", free >= req, f"needed {req} bytes including margin, available {free}"):
                return done("FAIL")
        except diskspace.DiskSpaceError as exc:
            check("room_for_add_gguf_copy", False, str(exc))
            return done("FAIL")
    res["human_page_hash_compared"] = human is not None
    # traceability: which metadata record these bytes were checked against
    res["metadata_record"] = {k: rec.get(k) for k in ("repo", "file", "revision", "retrieved_at_utc", "size_bytes", "published_sha256", "license_tag")}
    res["metadata_record"]["metadata_mode"] = metadata.get("mode")
    res["add_gguf_command"] = add_gguf_command(candidate, computed, size)      # saved with placeholders: no local path is written into the record
    res["meaning"] = ("PASS: these bytes equal what the host publishes (single source). " +
                      ("A person also compared the hash with the model page. " if human else "NO human comparison with the model page was recorded. ") +
                      "This does not show that the model works, is safe, or runs on Windows.")
    return done("PASS")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--metadata", required=True, type=Path)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--human-sha256", help="the SHA-256 a person read on the model page")
    ap.add_argument("--data-dir", type=Path, help="throw-away Jarvis data folder: checks room for the add-gguf copy on its drive")
    ap.add_argument("--out", type=Path, help="folder for download_verification_<candidate>.json")
    a = ap.parse_args(argv)
    try:
        meta = json.loads(a.metadata.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ValueError
    except (OSError, ValueError, RecursionError):
        print("FAIL: the metadata file cannot be read", file=sys.stderr)
        return 4
    res = verify(a.file, meta, a.candidate, a.human_sha256, a.data_dir)
    for c in res["checks"]:
        print(f"[{'ok' if c['ok'] else 'FAIL'}] {c['check']} {c['detail']}")
    print(res["status"], res.get("meaning", ""))
    if res.get("add_gguf_command"):
        print(add_gguf_command(a.candidate, res["computed_sha256"], res["metadata_record"]["size_bytes"], str(a.file), str(a.data_dir) if a.data_dir else None))
        # real paths on the console only; the saved JSON keeps the placeholders
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / f"download_verification_{re.sub(r'[^A-Za-z0-9._-]', '_', a.candidate)}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    return 0 if res["status"] == "PASS" else 4


if __name__ == "__main__":
    sys.exit(main())
