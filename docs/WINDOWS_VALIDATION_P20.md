# Windows validation (Prompt 20)

Nothing here has been executed on Windows by the author. For a normal customer none of this is needed: install, follow the wizard, launch.

* **Customer path (target):** installer -> Launch Project JARVIS -> the setup wizard checks the PC, downloads and verifies the model, tests it, saves CPU/GPU mode.
  Evidence is saved by the app itself: `%LOCALAPPDATA%\ProjectJarvis\setup_report.json` and Status page -> Export diagnostic report.
* **Getting the installer without a Windows build machine:** push the project to a public GitHub repository and run the `build-windows` workflow
  (`.github/workflows/build-windows.yml`); download the installer artifact. Never run before.
* **Developer helper (optional):** `python scripts\win_validate.py --out <dir> --data-dir <dir> env|infer|suite|compare`. `jarvis --data-dir D <subcommand>`: the option goes BEFORE the subcommand.
* `JARVIS_GPU_LAYERS` (0 = CPU, -1 = all layers on GPU) overrides the wizard's saved choice; the saved choice lives in `<models folder>\acceleration.json`.
