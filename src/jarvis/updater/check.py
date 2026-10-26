"""Opt-in update CHECK. It only reads a signed manifest; it never downloads or runs an installer.

The user downloads the installer from the release page and compares its SHA-256 (shown by the app)
with the signed value, e.g. PowerShell: Get-FileHash .\\ProjectJarvis-Setup.exe
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from jarvis.net.policy import NetworkPolicy
from jarvis.security import signing

_VER = re.compile(r"^\d+(\.\d+){0,3}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    installer_url: str
    installer_sha256: str
    notes: str


def parse_version(s: str) -> tuple[int, ...]:
    if not _VER.match(s):
        raise ValueError(f"bad version {s!r}")
    return tuple(int(x) for x in s.split("."))


def check_for_update(policy: NetworkPolicy, current_version: str, manifest_url: str,
                     public_key: bytes) -> UpdateInfo | None:
    with policy.open("update_check", manifest_url, timeout=15) as resp:
        raw = resp.read(1_000_001)
    if len(raw) > 1_000_000:
        raise UpdateError("update manifest too large")
    try:
        doc = json.loads(raw)
        payload, sig = doc["payload"], doc["signature"]
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateError("malformed update manifest") from exc
    if not signing.verify(payload, sig, public_key):
        raise UpdateError("update manifest signature is invalid; ignoring it")
    try:
        latest = str(payload["latest"])
        parse_version(latest)
        url, sha = str(payload["installer_url"]), str(payload["installer_sha256"]).lower()
    except (KeyError, ValueError) as exc:
        raise UpdateError("update manifest is missing or has invalid fields") from exc
    if not url.startswith("https://") or not _HEX64.match(sha):
        raise UpdateError("update manifest has an unsafe installer URL or bad hash")
    if parse_version(latest) <= parse_version(current_version):
        return None
    return UpdateInfo(latest, url, sha, str(payload.get("notes", "")))
