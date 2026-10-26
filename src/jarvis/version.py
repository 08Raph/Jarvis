"""The ONE authoritative version of PROJECT JARVIS. Everything else (package metadata, the installer, the build script, the About page, the
diagnostics report) reads it from here; tests fail if a packaging file hard-codes a different number.

RELEASE_CHANNEL says how much evidence there is for this build. "preview" means: source tests pass on Linux, the Windows build and installer have
NOT been verified by the maintainers. Changing the number or the channel does not make a build verified (docs/PROMPT10_REVIEW.md)."""
__version__ = "0.6.0"
RELEASE_CHANNEL = "preview"
MIN_WINDOWS = "10.0.17763"           # Windows 10 1809 / Windows 11 (the installer refuses older versions)
APP_NAME = "Project JARVIS"
