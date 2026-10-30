"""What goes into the Windows application folder - the SINGLE list used by the PyInstaller spec, by scripts/check_release_assets.py and by the tests.
Pure Python (no PyInstaller import) so it can be checked on any machine. Paths are relative to the project root."""
from __future__ import annotations

# (source relative to project root, destination folder inside the app folder)
DATAS = [
    ("src/jarvis/telemetry/endpoint.txt", "jarvis/telemetry"),     # empty unless the build was given a server address; users must still opt in
    ("src/jarvis/desktop/ocr_winrt.ps1", "jarvis/desktop"),        # Windows OCR helper script
    ("config.sample.toml", "."),                                   # configuration template; real settings live in %LOCALAPPDATA%\ProjectJarvis
    ("LICENSE", "."),
    ("THIRD_PARTY_NOTICES.md", "."),
    ("CHANGELOG.md", "."),
    ("build_info.json", "."),                                      # written by scripts/release_tools.py build-info just before the build
    ("server/jarvis_server/terms_en.md", "jarvis_server"),         # the local sign-in server (jarvis local-server) reads these next to its code
    ("server/jarvis_server/learning_notice_en.md", "jarvis_server"),
]
# The ONE model file that may ship in the folder/installer (see src/jarvis/models/bundled.py for its size and SHA-256 pins; check_release_assets verifies them).
BUNDLED_MODEL_RELPATH = "bundled_models/qwen3-1.7b-q4_k_m/model.gguf"
SERVER_PACKAGE = "jarvis_server"        # lives in server/, frozen into the app so `jarvis local-server` works; the spec adds server/ to the module search path
# user-facing documentation shipped with the app (maintainer review notes and test checklists stay in the source repository)
DOCS = ["RELEASE_NOTES.md", "TROUBLESHOOTING.md", "MODEL_DISTRIBUTION.md", "PRIVACY_AND_NETWORK.md", "LOCAL_MODEL.md", "DESKTOP_CONTROL.md",
        "MEMORY.md", "VOICE.md", "WINDOWS_DISTRIBUTION.md", "IN_APP_PREFLIGHT.md"]

# Files that MUST exist in the built folder (relative to dist/ProjectJarvis)
REQUIRED_IN_DIST = ["jarvis.exe", "jarvis-gui.exe", "LICENSE", "THIRD_PARTY_NOTICES.md", "build_info.json", "config.sample.toml",
                    "docs/RELEASE_NOTES.md", "docs/MODEL_DISTRIBUTION.md"]

# Names/patterns that must NEVER appear in the built folder or the installer (secrets, user data, development leftovers)
FORBIDDEN_PATTERNS = ["*.pem", "*.key", "*_private.*", "*private_key*", "*signing_private*", "*.sqlite", "*.sqlite-wal", ".env", ".env.*", "*.pt", "*.gguf", "*.safetensors", "jarvis_data",
                      "crash.log", "STOP", "desktop_audit*.jsonl", "*.pyc.bak", "requirements-lock.txt"]
# Public data that merely matches a forbidden pattern. certifi (a dependency of the voice library vosk, via requests) ships Mozilla's list of PUBLIC root
# certificates as cacert.pem. It is allowed ONLY at this exact path and ONLY while it contains no private key (check_release_assets verifies that).
ALLOWED_PUBLIC_PEM = ["_internal/certifi/cacert.pem", "certifi/cacert.pem"]
# Third-party binaries that must not be frozen into the 64-bit app (sounddevice ships libportaudio32bit.dll next to the 64-bit one; the app is 64-bit only,
# and scripts/check_vc_runtime.py rejects any non-AMD64 file).
EXCLUDED_BINARY_NAME_PARTS = ["32bit"]
FORBIDDEN_TOP_LEVEL = ["tests", "server", ".github", "scripts", "src", ".venv", "build"]

# Python modules the frozen app needs although PyInstaller cannot see them statically (dynamic imports); optional ones are included only if installed
HIDDEN_REQUIRED = ["jarvis.cli", "jarvis.ui.app", "jarvis.ui.window", "jarvis.app.controller", "jarvis.missions.runner", "jarvis.desktop.cli_desktop",
                   "jarvis.preflight.runner", "jarvis.preflight.metadata", "jarvis.preflight.hf_fetch", "jarvis.preflight.history",
                   "jarvis.brains.service", "jarvis.brains.viewmodel", "jarvis.brains.compat", "jarvis.brains.archs", "jarvis.models.gguf_vocab",
                   "jarvis.voice.engines", "jarvis.voice.manager", "jarvis.voice.preview", "jarvis.voice.inputcheck", "jarvis.voice.viewmodel", "jarvis.voice.kokoro_engine", "jarvis.models.kokoro_files",
                   "jarvis.selftest_core"]      # selftest supervisor + worker (the worker is this same program started again); System & Model Readiness page (imported lazily)
OPTIONAL_PACKAGES = ["llama_cpp", "sounddevice", "_sounddevice_data", "vosk", "comtypes", "psutil", "pypdf"]
MANDATORY_PACKAGES_FOR_RELEASE = ["PySide6", "numpy", "cryptography"]    # the build FAILS if these are missing

# Qt modules this application never uses; excluding them keeps the folder smaller and the LGPL surface smaller
QT_EXCLUDES = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtQml", "PySide6.QtQuick",
               "PySide6.QtQuickWidgets", "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
               "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth", "PySide6.QtNfc",
               "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtWebSockets", "PySide6.QtPdf", "PySide6.QtPdfWidgets"]
EXCLUDES = ["tkinter", "pytest"] + QT_EXCLUDES
