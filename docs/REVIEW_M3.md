# Milestone 3 review

## Problems found and fixed (each confirmed by a probe before fixing; regression tests in `tests/test_review_fixes.py`)

| Problem | Fix |
|---|---|
| Calculator hung on nested powers such as `(((10**100)**100)**100)**100` | Result-size bound in `safe_eval` |
| A removed data source could be re-registered and re-approved | Ledger refuses content whose hash was removed |
| Pipeline read a source file that changed after approval | SHA-256 verified at ingest; mismatch aborts |
| Reusing an output folder with a different dataset kept an old tokenizer/checkpoints (possible held-out leakage) | `data_hashes.json` guard; mismatch aborts |
| Inference prepended `<bos>` and a chat template the pretrained model never saw | Raw prompt unless checkpoint declares `chat_format` |
| Coding benchmark executed model code with no opt-in | Off unless `allow_code_exec` / `--allow-code-execution` |
| `open_app` could resolve a bare name from the current directory on Windows | Bare names resolve to `System32` only; absolute paths must exist |
| Model card said "trained from scratch" even with no trained model | Wording depends on whether metrics exist |

## What has actually been executed
49 tests exist. 48 pass, 1 is skipped (parameter formula vs real PyTorch; needs torch), 0 fail.
Never executed: `model/gpt.py`, `train/pretrain.py`, `inference/engine.py`, `scripts/smoke_train.py`
(only syntax-compiled). Checkpoint resume, early stopping, overfit detection and generation are
therefore UNVERIFIED. The parameter formula was derived by hand from `gpt.py` and matches, but is not
checked against torch.

## Measured here (sandbox, 2 CPUs)
Near-duplicate removal: 3000 documents of 120 words in 0.5 s. BPE training to vocab 1024 on 0.79 MB: 9.4 s
(cost grows with merges x unique words; fine for MBs, not GBs). Encode speed was measured on a tiny repetitive
vocabulary and is unrepresentative. Nothing larger was measured.

## Remaining issues
1. All torch code unexecuted (above). No model trained; no benchmark scores exist; no real corpus.
2. The smoke test's "interrupt" is a controlled stop at step 20 and resume to 40, not a crash mid-step. The cosine
   schedule is recomputed from the new `max_steps` on resume.
3. Gate threshold is an uncalibrated placeholder; log-probability is a weak confidence signal.
4. Contamination check finds 13-word overlaps only; paraphrased or semantic leakage and documents under 13 words
   are not covered by it (exact and 5-word near-duplicate removal still apply).
5. Weight decay applies to all parameters including embeddings and norms; not tuned.
6. GPU peak-TFLOPs figures in the estimator are from memory of spec sheets, not verified here; estimates are rough.
7. Resume loads `latest.pt` with `weights_only=False` (pickle). Resume only checkpoints this tool wrote.
8. `MemoryStore` uses one SQLite connection bound to its creating thread; the future UI needs a worker-thread design.
9. Windows launch path was exercised only by simulating `platform="win32"` on Linux with `/bin/true`.
10. No dependency lock file (packages could not be resolved offline); create one with `pip freeze` after installing.
11. Test runs show harmless ResourceWarnings (unclosed SQLite connections in tests).
12. Not started: PySide6 app, voice, updater, PyInstaller/Inno Setup packaging.

## Test it on Windows 11
1. Install 64-bit Python 3.11 or 3.12 from python.org (tick "Add python.exe to PATH").
2. Unzip `project-jarvis-m3.zip`, open PowerShell in the `project-jarvis` folder.
3. `py -3.12 -m venv .venv` then `.\.venv\Scripts\Activate.ps1`
   (if blocked: `Set-ExecutionPolicy -Scope Process RemoteSigned`, then retry).
4. `python -m pip install --upgrade pip` then `pip install -e ".[dev]"`
5. `python -m pytest -q`   Expected without torch: 48 passed, 1 skipped.
6. `pip install torch --index-url https://download.pytorch.org/whl/cpu` (CPU build; ~200 MB), then `python -m pytest -q`.
   Expected with torch: 49 passed, 0 skipped. This is the first real check of the parameter formula.
7. `python scripts/smoke_train.py` (a few minutes on CPU). Success = last line `SMOKE TEST PASSED`.
   Copy the full output, especially any traceback, and send it back.
8. Windows app-launch check (opens Notepad after you type y):
   `python -c "import sys; sys.path.insert(0,'src'); from jarvis.memory.store import MemoryStore; from jarvis.tools.registry import build_default_registry as b; m=MemoryStore(); r=b(m,{'notepad':'notepad.exe'}).run('open_app',{'name':'notepad'},confirm=lambda t,a: input('launch? y/n ')=='y'); print(r)"`
9. Report any failure with the exact command and full error text.
