"""Stage 5: read-only check of the Hugging Face METADATA for the five candidate GGUF files. It never downloads a model.

What it does: for each candidate it asks the official API (https://huggingface.co/api/models/<repo>?blobs=true, the same call `models fetch` uses) for the
repository record and compares it with what the project recorded: owner, exact file name (exact match, never a pattern), file size in bytes, the published
LFS SHA-256, the repository revision, licence tag, licence file and model card. It never requests a `resolve/` URL (that would download weights).

What a result means - and does not:
  * METADATA_CONSISTENT = the host's own metadata is complete and agrees with the recorded values. The SHA-256 here is the host's PUBLISHED value; it is NOT
    computed from file bytes and it is NOT independent of Hugging Face. Only a download plus scripts/verify_downloaded_model.py computes a hash from bytes.
  * Anything unexpected is a PROBLEM and the run exits 4: other owner, repository moved, file missing or duplicated, size missing/invalid/conflicting,
    hash missing/malformed/different from the recorded one, no revision, licence not Apache-2.0, gated/private/disabled repository, planned downloads above
    the 20 GB budget, too little free disk. Nothing is silently accepted. API unreachable = exit 3 (nothing is claimed).

Usage:  python scripts/verify_model_metadata.py --out C:\\jarvis-eval\\meta [--check-disk C:\\] [--include-control] [--from-raw DIR]
Exit codes: 0 all planned files consistent and within budget; 3 API unavailable for at least one repository; 4 problems found.
Standard library only. The checks themselves live in src/jarvis/preflight/metadata.py - the SAME code the app's "System & Model Readiness" page runs, so the
script and the app cannot disagree; this file only adds the plain-https fetcher and the command line. Run it on a networked machine.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.models import diskspace  # noqa: E402
from jarvis.preflight import metadata as _core  # noqa: E402
from jarvis.preflight.metadata import (API_HOST, CANDIDATES, DOWNLOAD_BUDGET_BYTES, HASH_KIND, HEX64, MAX_API_BYTES, MIN_FREE_BYTES, REPO,  # noqa: E402,F401
                                       ApiUnavailable, Cancelled, api_url, disk_check, evaluate, plan_budget, read_json_capped, report_md)
from jarvis.preflight.metadata import _licenses_of  # noqa: E402,F401


class _SameHostOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlparse(newurl)
        if u.scheme != "https" or u.hostname != API_HOST or not u.path.startswith("/api/"):
            raise urllib.error.URLError("redirect to another host or path refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_api(repo: str, timeout: float = 30.0) -> dict:
    """GET the repository record. Only https://huggingface.co/api/... is ever requested; the body is capped; anything else is ApiUnavailable."""
    url = api_url(repo)
    opener = urllib.request.build_opener(_SameHostOnly)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "jarvis-stage5-metadata-check"}), timeout=timeout) as r:
            return read_json_capped(r.read)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApiUnavailable(f"{type(exc).__name__}: {exc}") from None


def run(out, *args, fetch=None, **kwargs):
    """The shared implementation, with this script's plain-https fetcher as the default."""
    return _core.run(out, *args, fetch=fetch or fetch_api, **kwargs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="folder for model_metadata.json/.md and raw API responses")
    ap.add_argument("--check-disk", metavar="PATH", help="also check free space on this drive/folder (>= 25 GB and room for the largest file's two copies)")
    ap.add_argument("--include-control", action="store_true", help="count the optional official 4B control in the plan (it breaks the 20 GB budget by design)")
    ap.add_argument("--from-raw", type=Path, help="re-evaluate saved raw API responses instead of calling the network (offline replay)")
    a = ap.parse_args(argv)
    doc, code = run(Path(a.out), a.include_control, a.check_disk, a.from_raw)
    print(doc["overall"])
    print(f"written: model_metadata.json, model_metadata.md, raw/ in {a.out}")
    return code


if __name__ == "__main__":
    sys.exit(main())
