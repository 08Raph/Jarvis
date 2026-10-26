# Build and release pipeline (Windows 11)

**Status: written but never executed.** The authoring sandbox is Linux without PowerShell, PyInstaller, or Inno Setup.
Treat every step below as unverified until you have run it once and fixed what breaks.

## Versioning
Single source: `jarvis.__version__` (`src/jarvis/__init__.py`), checked against `pyproject.toml` by a test. The installer receives it as `/DAppVersion`.

## One-command build
`.\scripts\build_windows.ps1` (needs 64-bit Python 3.11/3.12 and Inno Setup 6.3+ on PATH). It:
1. creates `.venv`, installs CPU-only PyTorch (no bundled NVIDIA libraries), then the project with `dev` and `build` extras;
2. writes `requirements-lock.txt` (`pip freeze`) so the release is reproducible;
3. runs the tests; packaging stops if they fail;
4. regenerates `THIRD_PARTY_NOTICES.md` and `docs/MODEL_REQUIREMENTS.md`;
5. freezes the app with PyInstaller (one-folder, `packaging/jarvis.spec`);
6. smoke-runs `jarvis.exe version` and `privacy` on the frozen build;
7. compiles `packaging/installer.iss`;
8. writes `SHA256SUMS.txt`; optionally signs an update manifest with an offline key.

## Integrity chain
- Installer: publish `SHA256SUMS.txt`; users compare with `Get-FileHash`.
- Update manifest and model catalog: Ed25519 signatures. Generate keys with `python scripts/release_tools.py gen-keys --out-dir <offline folder>`, embed only the public key, never commit or upload the private key. Losing the key means users cannot verify new releases; leaking it lets others sign fake ones.
- Model packages: `release_tools.py pack-model` (refuses without LICENSE.txt); the catalog or release page carries each package's SHA-256; per-file hashes are verified again on every `jarvis models verify`.

## Licenses
Do not publish weights or datasets unless every training-data license and the weights' license permit redistribution (see `docs/DATA.md`).
`THIRD_PARTY_NOTICES.md` is generated from the build environment; PySide6 (LGPL) and PyInstaller bootloader obligations are flagged for legal review.

## Clean-install test (not yet run)
Copy the installer and `scripts\clean_install_test.ps1` to `C:\jarvis-share`, enable Windows Sandbox, open `packaging\clean_install_test.wsb`.
It runs with networking disabled and checks: silent install, version, first run (all online features off), honest "no model" behavior, no weights bundled, uninstall keeps user data. Results are written to `%TEMP%\jarvis-clean-install-results.json` inside the sandbox.

## Release checklist
Tests pass on Windows, build succeeds, clean-install test passes, notices reviewed, installer hash published, manifest signed, previous installer archived for rollback.
