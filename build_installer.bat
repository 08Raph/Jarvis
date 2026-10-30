@echo off
REM One-click build on Windows 10/11. Needs: Python 3.12 64-bit (py launcher) and Inno Setup 6 (iscc on PATH).
REM Builds the "everything inside" installer: the app, voice libraries, the local sign-in server and the Qwen3-1.7B model (ONE download, about 1.3 GB, checked
REM against the size and SHA-256 pinned in the program). For a small installer without the model, run scripts\build_windows.ps1 -SkipTorch -WithVoice yourself.
REM Output: dist\installer\ProjectJarvis-Setup-<version>.exe and SHA256SUMS.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_windows.ps1" -SkipTorch -WithVoice -BundleModel %*
if errorlevel 1 (echo BUILD FAILED & exit /b 1)
echo BUILD OK - see dist\installer
