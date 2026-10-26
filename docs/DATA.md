# Data policy and status

**Current corpus: none.** No real training data has been acquired. Only synthetic text is used by
the smoke test. Any model trained from it is for pipeline checks only.

## Rules
1. Every file is registered in the provenance ledger with an explicit license from the allowlist
   (CC0-1.0, CC-BY-4.0, CC-BY-SA-4.0, MIT, Apache-2.0, BSD-3-Clause, public-domain, project-original).
   Registration is refused otherwise. Do not guess licenses; verify the source's own terms.
2. Files containing email, phone, card-like, or SSN-like patterns are quarantined until a human
   reviews and approves them. The regex scan is a safety net, not a privacy guarantee.
3. Removal requests: `ledger.mark_removed(id, reason)`, then retrain; removed sources are excluded.
4. Pipeline order: clean, quality-filter, exact dedupe, near-duplicate (MinHash) dedupe, split by
   seeded content hash, drop train documents sharing a 13-gram with val/test. The tokenizer is trained on
   the train split only. The test split is evaluated once per run.
5. `dataset_report.json` records counts at each stage, split sizes and hashes, and source licenses.

## Candidate source types (verify each before use)
Public-domain books, openly licensed encyclopedic text, permissively licensed source code, and
text you write yourself. Share-alike (CC-BY-SA) and attribution (CC-BY) licenses carry obligations that
may extend to distributed weights; get legal advice before shipping.

## Known limitations and biases
Unknown until a real corpus exists. Expect skew toward the language, era, region, and viewpoints of the
sources. No bias audit has been run, and the model card says so until one is.
