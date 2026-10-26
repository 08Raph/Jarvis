@echo off
REM One-click build on Windows 10/11. Needs: Python 3.12 64-bit (py launcher) and Inno Setup 6 (iscc on PATH).
REM Output: dist\installer\ProjectJarvis-Setup-<version>.exe and SHA256SUMS.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_windows.ps1" -SkipTorch %*
if errorlevel 1 (echo BUILD FAILED & exit /b 1)
echo BUILD OK - see dist\installer
