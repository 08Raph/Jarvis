"""Model-metadata check for the five GGUF candidates: the ONE implementation used by the in-app "System & Model Readiness" page and by
scripts/verify_model_metadata.py. It never downloads a model and never opens a network connection itself: the caller passes a `fetch(repo) -> dict`
function (the app passes one that goes through jarvis.net.policy.NetworkPolicy; the script passes a plain https fetcher for a developer machine).

What a result means - and does not:
  * METADATA_CONSISTENT = the host's own metadata is complete and agrees with the values recorded in this file. The SHA-256 is the host's PUBLISHED value;
    it is NOT computed from file bytes and it is NOT independent of Hugging Face (same source as the model page).
  * Anything unexpected is a PROBLEM (exit-style code 4): other owner, repository moved, file missing or duplicated, size missing/invalid/conflicting, hash
    missing/malformed/different from the recorded one, no or malformed revision, licence missing/conflicting/not Apache-2.0, gated/private/disabled
    repository, planned downloads above the 20 GB budget, too little free disk. API unreachable = code 3 (nothing is claimed).
  * The values in CANDIDATES are expectations recorded from file pages (docs/STAGE5_MODEL_SOURCE_VERIFICATION.md section 17.2), not verified facts.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jarvis.models import diskspace

API_HOST = "huggingface.co"
MAX_API_BYTES = 5_000_000
DOWNLOAD_BUDGET_BYTES = 20_000_000_000       # experiment-wide budget (docs/STAGE5_VALIDATION_RUNBOOK.md section 5)
MIN_FREE_BYTES = 25_000_000_000              # minimum free disk before the experiment
HEX64 = re.compile(r"[0-9a-f]{64}")
REPO = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")   # no leading dot: rules out "." and ".." segments
HASH_KIND = "published LFS sha256 from the host's API metadata (NOT computed from file bytes; same source as the model page)"

# Values RECORDED earlier from the Hugging Face file pages (docs/STAGE5_MODEL_SOURCE_VERIFICATION.md section 17.2). They are expectations to compare the
# API against, not verified facts.
CANDIDATES = (
    {"id": "qwen3-1.7b-ggml-org", "repo": "ggml-org/Qwen3-1.7B-GGUF", "file": "Qwen3-1.7B-Q4_K_M.gguf", "in_plan": True,
     "recorded_sha256": "d2387ca2dbfee2ffabce7120d3770dadca0b293052bc2f0e138fdc940d9bc7b5", "role": "third-party conversion"},
    {"id": "qwen3-4b-ggml-org", "repo": "ggml-org/Qwen3-4B-GGUF", "file": "Qwen3-4B-Q4_K_M.gguf", "in_plan": True,
     "recorded_sha256": "ab27b9bfa375a178d6cba48f3ad892b94b7739659dcc7aae8058ce0ffed6b328", "role": "third-party conversion"},
    {"id": "qwen3-4b-official", "repo": "Qwen/Qwen3-4B-GGUF", "file": "Qwen3-4B-Q4_K_M.gguf", "in_plan": False,
     "recorded_sha256": "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5", "role": "official control (excluded from the plan: 20 GB cap)"},
    {"id": "qwen3-8b-official", "repo": "Qwen/Qwen3-8B-GGUF", "file": "Qwen3-8B-Q4_K_M.gguf", "in_plan": True,
     "recorded_sha256": "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785", "role": "official"},
    {"id": "qwen3-14b-official", "repo": "Qwen/Qwen3-14B-GGUF", "file": "Qwen3-14B-Q4_K_M.gguf", "in_plan": True,
     "recorded_sha256": "500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0", "role": "official"},
)


class ApiUnavailable(Exception):
    pass


class Cancelled(Exception):
    """The person pressed Cancel; no further request is made."""


def read_json_capped(read, what: str = "response") -> dict:
    """`read(n)` returns at most n bytes of a response body. Anything oversized, not JSON, or not a JSON object is ApiUnavailable (fail closed)."""
    body = read(MAX_API_BYTES + 1)
    if len(body) > MAX_API_BYTES:
        raise ApiUnavailable(f"{what} larger than the allowed size")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ApiUnavailable(f"{what} is not valid JSON") from None
    if not isinstance(data, dict):
        raise ApiUnavailable(f"{what} is not a JSON object")
    return data


def api_url(repo: str) -> str:
    if not REPO.fullmatch(repo):
        raise ValueError("invalid repository name")
    return f"https://{API_HOST}/api/models/{repo}?blobs=true"




def _licenses_of(data: dict) -> list[str]:
    """Every licence value the record states (card data and tags), de-duplicated. More than one distinct value is a conflict, never a silent pick."""
    found = []
    card = data.get("cardData")
    if isinstance(card, dict) and isinstance(card.get("license"), str):
        found.append(card["license"].lower())
    for t in data.get("tags") or []:
        if isinstance(t, str) and t.lower().startswith("license:"):
            found.append(t.split(":", 1)[1].lower())
    return sorted(set(found))


def evaluate(cand: dict, data: dict | None, error: str | None, retrieved_at: str) -> dict:
    """Compare one API record with the recorded expectations. Never raises on odd input: odd input is a recorded problem."""
    rec = {"id": cand["id"], "repo": cand["repo"], "file": cand["file"], "role": cand["role"], "in_plan": cand["in_plan"],
           "source": api_url(cand["repo"]), "retrieval_method": "HTTPS GET of the official API (metadata only; no model bytes)",
           "retrieved_at_utc": retrieved_at, "hash_kind": HASH_KIND, "recorded_sha256": cand["recorded_sha256"],
           "problems": [], "notes": []}
    if data is None:
        rec["problems"].append("API_UNAVAILABLE")
        rec["error"] = error
        rec["status"] = "API_UNAVAILABLE"
        return rec
    p = rec["problems"]
    repo_id = data.get("id")
    rec["repo_id_reported"] = repo_id
    if not isinstance(repo_id, str) or repo_id.lower() != cand["repo"].lower():
        p.append("REPO_ID_MISMATCH")
    owner_expected = cand["repo"].split("/")[0]
    author = data.get("author") if isinstance(data.get("author"), str) else (repo_id.split("/")[0] if isinstance(repo_id, str) and "/" in repo_id else None)
    rec["owner_reported"] = author
    if author is None or author.lower() != owner_expected.lower():
        p.append("OWNER_MISMATCH")
    for flag in ("gated", "private", "disabled"):
        if data.get(flag):
            p.append(f"REPO_{flag.upper()}")
    sha = data.get("sha")
    rec["revision"] = sha if isinstance(sha, str) and sha else None
    rec["last_modified"] = data.get("lastModified")
    if not rec["revision"]:
        p.append("REVISION_MISSING")
    elif not re.fullmatch(r"[0-9a-f]{40}", rec["revision"]):
        p.append("REVISION_MALFORMED")      # a commit id is 40 hex characters; anything else cannot be pinned later
    lics = _licenses_of(data)
    lic = lics[0] if len(lics) == 1 else None
    rec["license_tag"] = lic if lic else (",".join(lics) or None)
    if not lics:
        p.append("LICENSE_MISSING")
    elif len(lics) > 1:
        p.append("LICENSE_CONFLICT")
    elif lic != "apache-2.0":
        p.append("LICENSE_NOT_APACHE_2_0")
    sib = [s for s in (data.get("siblings") or []) if isinstance(s, dict)]
    names = [s.get("rfilename") for s in sib]
    rec["license_file_in_repo"] = "LICENSE" in names
    rec["model_card_file_in_repo"] = "README.md" in names
    rec["model_card_url"] = f"https://{API_HOST}/{cand['repo']}"
    rec["license_url"] = f"https://{API_HOST}/{cand['repo']}/blob/main/LICENSE" if rec["license_file_in_repo"] else None
    if not rec["license_file_in_repo"]:
        rec["notes"].append("no LICENSE file in this repository: the terms rest on the licence tag and the base model's licence")
    rec["other_q4_k_m_files"] = sorted(n for n in names if isinstance(n, str) and "q4_k_m" in n.lower() and n != cand["file"])
    match = [s for s in sib if s.get("rfilename") == cand["file"]]
    if not match:
        p.append("FILE_MISSING")
    elif len(match) > 1:
        p.append("FILE_DUPLICATE")
    if len(match) == 1:
        s = match[0]
        lfs = s.get("lfs") if isinstance(s.get("lfs"), dict) else {}
        sizes = [v for v in (s.get("size"), lfs.get("size")) if v is not None]
        if any(type(v) is not int or v <= 0 for v in sizes):
            p.append("SIZE_INVALID")
        elif not sizes:
            p.append("SIZE_MISSING")
        elif len(set(sizes)) > 1:
            p.append("SIZE_CONFLICT")
        else:
            rec["size_bytes"] = sizes[0]
            rec["size_limit_115_percent_bytes"] = diskspace.size_limit(sizes[0])
        h = lfs.get("sha256") or lfs.get("oid")
        if not h:
            p.append("HASH_MISSING")
        elif not isinstance(h, str) or not HEX64.fullmatch(h.lower()):
            p.append("HASH_MALFORMED")
        else:
            rec["published_sha256"] = h.lower()
            if h.lower() != cand["recorded_sha256"]:
                p.append("HASH_DIFFERS_FROM_RECORDED")
    rec["status"] = "METADATA_CONSISTENT" if not p else "PROBLEMS"
    return rec


def plan_budget(results: list[dict]) -> dict:
    planned = [r for r in results if r["in_plan"]]
    if not planned or any("size_bytes" not in r for r in planned):
        return {"planned_files": [r["id"] for r in planned], "status": "UNKNOWN (a planned file has no usable size)", "total_bytes": None,
                "budget_bytes": DOWNLOAD_BUDGET_BYTES}
    total = sum(r["size_bytes"] for r in planned)
    return {"planned_files": [r["id"] for r in planned], "total_bytes": total, "budget_bytes": DOWNLOAD_BUDGET_BYTES,
            "status": "WITHIN_BUDGET" if total <= DOWNLOAD_BUDGET_BYTES else "OVER_BUDGET",
            "note": "plan-level arithmetic on the host-reported sizes; nothing counts real bytes transferred - that stays a manual tally"}


def disk_check(path: str, results: list[dict], free_fn=diskspace.free_bytes) -> dict:
    """Pre-flight: free space at the start must be >= 25 GB, and the largest planned file needs its scratch copy plus the add-gguf copy (2 x size) and the margin."""
    try:
        free = free_fn(path)
    except diskspace.DiskSpaceError as exc:
        return {"status": "UNKNOWN", "error": str(exc)}
    sizes = [r["size_bytes"] for r in results if r["in_plan"] and "size_bytes" in r]
    largest = max(sizes) if sizes else None
    peak = diskspace.required_free(2 * largest + 2 * diskspace.META_OVERHEAD) if largest else None
    ok = free >= MIN_FREE_BYTES and (peak is None or free >= peak)
    return {"status": "OK" if ok else "INSUFFICIENT", "free_bytes": free, "minimum_free_bytes": MIN_FREE_BYTES, "largest_planned_file_bytes": largest,
            "peak_need_for_largest_with_margin_bytes": peak}


def report_md(doc: dict) -> str:
    L = [f"# Model metadata check ({doc['retrieved_at_utc']})", "",
         "Metadata only. The hash column is the host's PUBLISHED value, not a hash of file bytes. Nothing was downloaded.", "",
         "| id | repo | file | size (bytes) | published sha256 | revision | licence | status | problems |", "|---|---|---|---|---|---|---|---|---|"]
    for r in doc["results"]:
        L.append(f"| {r['id']} | {r['repo']} | {r['file']} | {r.get('size_bytes', '?')} | {r.get('published_sha256', '?')} | {r.get('revision', '?')} | "
                 f"{r.get('license_tag', '?')} | {r['status']} | {', '.join(r['problems']) or '-'} |")
    b = doc["budget"]
    L += ["", f"Budget: {b['status']} (planned {b.get('total_bytes')} of {b['budget_bytes']} bytes)"]
    if doc.get("disk"):
        L.append(f"Disk: {doc['disk']['status']} {json.dumps({k: v for k, v in doc['disk'].items() if k != 'status'})}")
    L += ["", f"Overall: {doc['overall']}", "Missing evidence this tool cannot supply: a hash computed from downloaded bytes (scripts/verify_downloaded_model.py), "
          "a source independent of Hugging Face, the organisation's verified status."]
    return "\n".join(L) + "\n"


def run(out: Path, include_control: bool = False, check_disk: str | None = None, from_raw: Path | None = None, fetch=None,
        now=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), free_fn=diskspace.free_bytes, progress=None, should_stop=None) -> tuple[dict, int]:
    """Returns (record, code): 0 consistent and within budget, 3 API unavailable, 4 problems. `fetch(repo)` is required unless `from_raw` replays saved responses.
    `progress(done, total, text)` is called before each repository; `should_stop()` returning True raises Cancelled before the next request."""
    when = now()
    cands = [dict(c) for c in CANDIDATES]
    if include_control:
        for c in cands:
            c["in_plan"] = True
    results, unavailable = [], False
    raw_dir = out / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    replay = from_raw is not None
    if replay:      # the time of the ORIGINAL retrieval, if that run saved it; otherwise unknown - never the time of the replay
        try:
            when = (from_raw / "_retrieved_at_utc.txt").read_text(encoding="utf-8").strip() or "UNKNOWN"
        except OSError:
            when = "UNKNOWN (offline replay; the original retrieval time was not saved)"
    else:
        (raw_dir / "_retrieved_at_utc.txt").write_text(when, encoding="utf-8")
    for n, c in enumerate(cands):
        if should_stop is not None and should_stop():
            raise Cancelled()
        if progress is not None:
            progress(n, len(cands), f"Reading the published record of {c['repo']}")
        tag = c["repo"].replace("/", "__") + ".api.json"
        data = err = None
        try:
            if from_raw is not None:
                data = json.loads((from_raw / tag).read_text(encoding="utf-8"))
            elif fetch is None:
                raise ApiUnavailable("no network fetcher was supplied")
            else:
                data = fetch(c["repo"])
        except (ApiUnavailable, OSError, ValueError) as exc:
            err, unavailable = str(exc), True
        if replay:
            (raw_dir / "_retrieved_at_utc.txt").write_text(when, encoding="utf-8")
        if data is not None:
            (raw_dir / tag).write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        results.append(evaluate(c, data, err, when))
    if replay:
        for r in results:
            r["retrieval_method"] = "OFFLINE REPLAY of a saved API response (no network access in this run)"
    doc = {"kind": "model-metadata-check", "mode": "offline-replay" if replay else "live-api", "retrieved_at_utc": when, "results": results, "budget": plan_budget(results),
           "disk": disk_check(check_disk, results, free_fn) if check_disk else None, "hash_kind": HASH_KIND}
    bad = [r for r in results if r["in_plan"] and r["status"] != "METADATA_CONSISTENT"]
    if unavailable:
        doc["overall"] = "API_UNAVAILABLE for at least one repository: nothing is claimed (exit 3)"
        code = 3
    elif bad or doc["budget"]["status"] != "WITHIN_BUDGET" or (doc["disk"] and doc["disk"]["status"] != "OK"):
        doc["overall"] = "PROBLEMS found (exit 4): " + "; ".join(f"{r['id']}: {','.join(r['problems'])}" for r in bad) + \
                         f"; budget {doc['budget']['status']}" + (f"; disk {doc['disk']['status']}" if doc["disk"] else "")
        code = 4
    else:
        doc["overall"] = ("METADATA CONSISTENT for the planned files, still single-source (the host), no hash computed from bytes, "
                          "organisation status and licence texts still to be read by a person (exit 0)")
        code = 0
    out.mkdir(parents=True, exist_ok=True)
    (out / "model_metadata.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    (out / "model_metadata.md").write_text(report_md(doc), encoding="utf-8")
    return doc, code


