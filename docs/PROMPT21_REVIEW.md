# Prompt 21 review: first real Windows CI evidence (GitHub Actions)

Everything below is taken from GitHub Actions runs on a **GitHub-hosted `windows-2022` runner** (Windows Server 2022, 2 vCPU, ~8.6 GB RAM, no GPU,
runs as Administrator). That is not a customer PC and not Windows 11. A green run is evidence, not production readiness.

Repository: https://github.com/08Raph/Jarvis (private). Branch `main`. Baseline commit `b9244d0` = the unmodified Stage 3 archive
(SHA-256 `be36f4c3b323345c5ba25657cc5c635f74314c3ac8835eafef2e7b5382d9cb67`, 288 files; the "322" in the archive listing counts directory entries).
The log blob and artifact downloads are not reachable from the authoring session's proxy, so results were read from job step conclusions and from
workflow annotations that the workflow itself prints (`::notice` / `::error`). The uploaded artifacts (`ProjectJarvis-ci-logs`, `ProjectJarvis-installer`)
exist in the run and can be downloaded from the Actions page by the repository owner; this session could not open them (including the screenshot).

## Runs (bounded: 10 runs, 8 failures fixed in sequence, then 2 passes)

| # | Run ID | Commit | Result | First meaningful failure -> fix |
|---|--------|--------|--------|---------------------------------|
| 1 | 40412585951 | fedfdb8 | failure | build step; log not readable -> tee step output, print log tails as annotations |
| 2 | 40413317444 | f3f1c26 | failure | build step died after `pip` fetched the llama wheel: Windows PowerShell 5.1 treats any stderr line of a native program as a terminating error -> `Run` helper uses the exit code |
| 3 | 40413682770 | f91eae4 | failure | first Windows execution of the unit tests (12 failures, 18 errors) blocked packaging -> tests run as their own reported step, final gate keeps the run red |
| 4 | 40414386816 | 9b1957b | failure | PyInstaller built both exes; `check_release_assets` rejected the build (PyInstaller 6 puts data in `_internal`; `*private*` matched Windows/Qt DLLs) -> checker fixed |
| 5 | 40415812600 | 5a8914d | failure | Inno Setup rejected `InfoBeforeInstallFile` (correct: `InfoBeforeFile`) -> fixed + static directive test |
| 6 | 40416699554 | c1055e0 | failure | first real installer produced (159.5 MB); silent-install exit code read as empty (PS 5.1) -> handle cached, log tail surfaced |
| 7 | 40418098912 | 99b9299 | failure | all install checks passed; step marked failed by a stale `$LASTEXITCODE` -> explicit exit codes. Also found: plugin review crashed on a dangling symlink |
| 8 | 40419354879 | ac3dcaa | failure (gate) | install + real-model steps passed; 1 unit-test error (database closed too late in a test) -> fixed |
| 9 | 40420778232 | 4480635 | **success** | all steps green |
| 10 | 40422256861 | 82ffb6e | **success** | all steps green, plus real-prompt answers and peak memory |

## Product defects the Windows run found (fixed, with regression tests)
- Named time zones (e.g. America/Toronto) failed on Windows: `tzdata` was not declared or bundled. Now a win32 dependency and collected by the PyInstaller spec.
- The build-machine path scanner missed Windows paths inside JSON (backslashes are doubled). Now checks the escaped form.
- Plugin review followed symlinks and crashed on a dangling link instead of blocking it. Now never follows links.
- CLI reported network failures as a generic `error:`; now `Download failed` (so the offline proof can tell a genuine network failure from other errors).
- Installer script used an invalid Inno directive; release-asset checker used a Linux-shaped layout and an over-broad key pattern.
- Optional `pypdf` and `psutil` were missing from the Windows build, so PDF reading and real monitoring were silently absent. The build now installs and bundles them (licences recorded in THIRD_PARTY_NOTICES.md).

## Test-only Windows differences (not product bugs; documented, not deleted)
87 tests are skipped on Windows (79 existed before: POSIX-only assertions; 8 added in this prompt via `tests/platform_marks.py`):
Linux-sandbox facts ("no llama-cpp / no speech engine / no GPU listing here"), and two desktop-control tests that cannot run in an elevated (Administrator)
process because Jarvis deliberately refuses to act when elevated. Two plugin tests need the symlink privilege. Tests that left SQLite files open were fixed.
The unit-test step sets `PYTHONUTF8=1` because many tests read source files without an encoding (Windows default is cp1252); product code was checked and passes encodings explicitly.

## What the passing Windows runs prove (run 10, commit 82ffb6e)
- PyInstaller one-folder build: `jarvis.exe` (9,175,397 B) and `jarvis-gui.exe` (9,171,789 B), VC++ runtime bundled, frozen-exe smoke tests, headless GUI self-test.
- Inno Setup installer `ProjectJarvis-Setup-0.6.0.exe`, 159,551,611 bytes, SHA-256 `066CC7BF6872C55FAEE61EF43303859B4AD065B85AB13DEDEEEA5A0075E5CD8A`
  (run 9 produced 159,540,605 bytes / `1E62488E…D725`: **the build is not bit-reproducible**).
- Silent install, `version`/`runtime --require` with a stripped PATH (no Python needed), first-run defaults all-offline, honest refusal to chat without a model, no weights bundled, silent uninstall keeps user data.
- `jarvis-gui.exe` launched from the installed folder on the runner desktop, stayed alive for 25 s, and `EnumWindows` listed visible windows
  "Project JARVIS setup" and "Project JARVIS 0.6.0 (preview)". A screenshot PNG was saved (64,063 bytes); **its content was not viewed by this session**.
- `jarvis setup-auto --accept-download` (the setup engine without the clicks): model Qwen3 1.7B Q4_K_M, 1.28 GB, Apache-2.0, consent printed before download,
  downloaded from huggingface.co, integrity step passed, real llama.cpp CPU load and a real inference; `setup_state`: completed=true, real=true, mode=cpu, gpu_verified=false.
- Self-test with the real model: load 1.33 s, one-token answer "4" (generation 1.28 s). **Tokens/second from this self-test (0.78) is not meaningful: one token.**
- Real prompt "capital of France": reply "The capital of France is Paris." Wall time 12.73 s including model load, peak working set 2,318 MB (sampled).
- Offline: Windows Firewall outbound block rules on `jarvis.exe` and `jarvis-gui.exe`; the model download then failed with `URLError ... WinError 10013`
  (socket access forbidden) — a genuine OS-level block, not the app's own switch. With the block active the self-test and the real prompt both answered
  (peak 2,318 MB, 12.44 s). Rules were removed afterwards.
- Diagnostics text did not contain the Windows user name.

## Not proven
- Clicking through the wizard: the model download/consent path was exercised through `setup-auto`, not by GUI interaction. Wizard appearance is shown by window title only.
- Windows 11 and any customer hardware; installer on a clean machine (the runner image has developer tools); code signing / SmartScreen; upgrade-over-existing install.
- GPU: NOT TESTED (runner has none; the packaged runtime is the CPU build, `gpu_offload_supported=false`).
- Voice: NOT TESTED / not accepted. No audio was generated or evaluated. No distinct masculine/feminine voice identities are established.
- Windows computer control: unit tests pass on Windows only for the non-elevated parts; real desktop actions were not exercised on a normal user session.
- Time to first token, and sustained tokens/second on a multi-token answer, were not measured separately.
- Model host availability and the checksum published by the host are read at run time; a host change could break the download.
