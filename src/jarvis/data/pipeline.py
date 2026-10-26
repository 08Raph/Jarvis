"""Dataset preparation: ingest -> clean -> filter -> dedupe -> split -> decontaminate.

Design rules that protect evaluation integrity:
  * Splits are assigned per document by a seeded content hash, so a document can
    never land in two splits.
  * Exact and near-duplicate removal runs BEFORE splitting.
  * Any train/val document sharing a long word n-gram with val/test is dropped
    from train (test is never trained on, tuned on, or used by the tokenizer).
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

import numpy as np

from jarvis.data.provenance import ProvenanceLedger

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD = re.compile(r"\w+", re.UNICODE)


# ----------------------------------------------------------------- cleaning
def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def iter_documents(text: str) -> list[str]:
    """A document is a blank-line-separated block."""
    return [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]


def quality_check(doc: str, min_chars: int = 40, max_chars: int = 20000,
                  min_alpha_frac: float = 0.6, max_dup_line_frac: float = 0.5) -> tuple[bool, str]:
    if len(doc) < min_chars:
        return False, "too_short"
    if len(doc) > max_chars:
        return False, "too_long"
    alpha = sum(c.isalpha() or c.isspace() for c in doc) / len(doc)
    if alpha < min_alpha_frac:
        return False, "low_alpha"
    lines = [l for l in doc.split("\n") if l.strip()]
    if len(lines) >= 4 and 1 - len(set(lines)) / len(lines) > max_dup_line_frac:
        return False, "repetitive"
    return True, "ok"


# -------------------------------------------------------------- deduplication
def _norm(doc: str) -> str:
    return " ".join(doc.lower().split())


def dedupe_exact(docs: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for d in docs:
        h = hashlib.sha256(_norm(d).encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            out.append(d)
    return out


_MASK = np.uint64(0xFFFFFFFFFFFFFFFF)


def _minhash_sig(doc: str, a: np.ndarray, b: np.ndarray, k: int = 5) -> np.ndarray | None:
    words = _norm(doc).split()
    if len(words) < k:
        shingles = {" ".join(words)} if words else set()
    else:
        shingles = {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}
    if not shingles:
        return None
    hs = np.array([int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "little")
                   for s in shingles], dtype=np.uint64)
    with np.errstate(over="ignore"):
        return (a[:, None] * hs[None, :] + b[:, None]).min(axis=1)


def dedupe_near(docs: list[str], threshold: float = 0.8, num_perm: int = 64, bands: int = 16,
                seed: int = 0) -> list[str]:
    """MinHash + LSH near-duplicate removal (keeps the first occurrence)."""
    if num_perm % bands:
        raise ValueError("num_perm must be divisible by bands")
    rng = np.random.default_rng(seed)
    a = rng.integers(1, 2**63, size=num_perm, dtype=np.uint64) | np.uint64(1)
    b = rng.integers(0, 2**63, size=num_perm, dtype=np.uint64)
    rows = num_perm // bands
    buckets: dict[tuple[int, bytes], list[int]] = {}
    sigs: dict[int, np.ndarray] = {}
    kept: list[int] = []
    for i, d in enumerate(docs):
        sig = _minhash_sig(d, a, b)
        if sig is None:
            kept.append(i)
            continue
        dup = False
        keys = [(j, sig[j * rows:(j + 1) * rows].tobytes()) for j in range(bands)]
        cands = {c for k in keys for c in buckets.get(k, [])}
        for c in cands:
            if float(np.mean(sigs[c] == sig)) >= threshold:
                dup = True
                break
        if not dup:
            kept.append(i)
            sigs[i] = sig
            for k in keys:
                buckets.setdefault(k, []).append(i)
    return [docs[i] for i in kept]


# ------------------------------------------------------------ split + leakage
def assign_split(doc: str, seed: int, val_frac: float, test_frac: float) -> str:
    h = hashlib.sha256(f"{seed}:{_norm(doc)}".encode()).digest()
    u = int.from_bytes(h[:8], "big") / 2**64
    if u < test_frac:
        return "test"
    if u < test_frac + val_frac:
        return "val"
    return "train"


def ngram_set(text: str, n: int = 13) -> set[tuple[str, ...]]:
    w = [x.lower() for x in _WORD.findall(text)]
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def decontaminate(train_docs: list[str], held_out_docs: list[str], n: int = 13) -> tuple[list[str], int]:
    bad: set[tuple[str, ...]] = set()
    for d in held_out_docs:
        bad |= ngram_set(d, n)
    if not bad:
        return train_docs, 0
    kept = [d for d in train_docs if not (ngram_set(d, n) & bad)]
    return kept, len(train_docs) - len(kept)


# ----------------------------------------------------------------- orchestration
def prepare_dataset(ledger_db: str | Path, out_dir: str | Path, seed: int = 1337,
                    val_frac: float = 0.05, test_frac: float = 0.05, ngram: int = 13,
                    near_threshold: float = 0.8) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ledger = ProvenanceLedger(ledger_db)
    rows = ledger.approved()
    ledger.close()
    if not rows:
        raise RuntimeError("no approved sources in the provenance ledger")

    report: dict = {"seed": seed, "stages": {}, "warnings": [],
                    "sources": [{"id": r["id"], "name": r["name"], "license": r["license"],
                                 "sha256": r["sha256"]} for r in rows]}
    docs: list[str] = []
    for r in rows:
        raw = Path(r["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != r["sha256"]:
            raise RuntimeError(f"source {r['id']} ({r['name']}) changed after it was approved; "
                               "re-register it (and re-review its license/PII) before training")
        docs.extend(iter_documents(clean_text(raw.decode("utf-8"))))
    report["stages"]["ingested"] = len(docs)

    reasons: dict[str, int] = {}
    kept = []
    for d in docs:
        ok, why = quality_check(d)
        if ok:
            kept.append(d)
        else:
            reasons[why] = reasons.get(why, 0) + 1
    report["stages"]["after_filter"] = len(kept)
    report["filtered_reasons"] = reasons

    kept = dedupe_exact(kept)
    report["stages"]["after_exact_dedupe"] = len(kept)
    kept = dedupe_near(kept, threshold=near_threshold, seed=seed)
    report["stages"]["after_near_dedupe"] = len(kept)

    splits: dict[str, list[str]] = {"train": [], "val": [], "test": []}
    for d in kept:
        splits[assign_split(d, seed, val_frac, test_frac)].append(d)
    held = splits["val"] + splits["test"]
    splits["train"], dropped = decontaminate(splits["train"], held, ngram)
    report["decontamination_dropped_from_train"] = dropped

    files = {}
    for name, ds in splits.items():
        p = out / f"{name}.txt"
        p.write_text("\n\n".join(ds), encoding="utf-8")
        files[name] = {"documents": len(ds), "chars": sum(len(x) for x in ds),
                       "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        if not ds:
            report["warnings"].append(f"{name} split is empty; add more approved data")
    report["splits"] = files
    (out / "dataset_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
