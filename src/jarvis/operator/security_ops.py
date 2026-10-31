"""Security and privacy assistance: read-only status, hashes, signatures, risky files, secrets in text, credential files. Never changes protections."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from jarvis.operator.core import LOW, MEDIUM, Outcome, OperatorError, Unavailable, cap
from jarvis.operator.files_ops import walk_files

SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*(is|:|=)\s*\S{4,}"), "a password"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "a GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"), "an API key"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "a private key"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|bearer)\s*[:=]\s*[A-Za-z0-9_\-\.]{16,}"), "an API key or token"),
    (re.compile(r"(?i)\brecovery code\b.{0,20}[A-Z0-9]{4}(-[A-Z0-9]{4}){2,}"), "a recovery code"),
]
CREDENTIAL_NAMES = re.compile(r"(?i)(^\.env(\..*)?$|^id_(rsa|dsa|ecdsa|ed25519)$|\.pem$|\.ppk$|\.kdbx?$|\.pfx$|\.p12$|passwords?\.|credentials?\.(json|txt|csv)$|secrets?\.(json|txt|ya?ml)$|^\.npmrc$|^\.netrc$)")


def find_secrets(text: str) -> list[str]:
    """What kinds of secret the text seems to contain (never the secret itself)."""
    return sorted({label for rx, label in SECRET_PATTERNS if rx.search(text or "")})


def mask_secrets(text: str) -> str:
    """The text with anything that looks like a password, key or token replaced by [hidden], so it never reaches chat history, memory or logs."""
    out = text or ""
    for rx, _label in SECRET_PATTERNS:
        out = rx.sub("[hidden]", out)
    return out


def warn_text(kinds: list[str]) -> str:
    return ("That message looks like it contains " + ", ".join(kinds) + ". I don't keep passwords or keys in memory or logs, but anything you type here can end up in the chat history on this PC. "
            "If it is real, treat it as exposed and change it if you can; a password manager is the safer place for it.")


@cap("security.defender", "security_read", "Check Microsoft Defender status", LOW, (), True, "22")
def defender(op) -> Outcome:
    raw = op.ops.powershell("Get-MpComputerStatus | Select-Object AMServiceEnabled,AntivirusEnabled,RealTimeProtectionEnabled,AntivirusSignatureLastUpdated | ConvertTo-Json -Compress", 30).strip()
    d = json.loads(raw) if raw else {}
    if not d:
        return Outcome("info", "Windows didn't report Defender status (another antivirus may be in charge).")
    rt = d.get("RealTimeProtectionEnabled")
    msg = f"Defender: antivirus {'on' if d.get('AntivirusEnabled') else 'OFF'}, real-time protection {'on' if rt else 'OFF'}"
    sig = d.get("AntivirusSignatureLastUpdated")
    if sig:
        msg += f", signatures updated {str(sig)[:10]}"
    return Outcome("info", msg + ("." if rt and d.get("AntivirusEnabled") else ". Something is off: open Windows Security to turn it back on; I never change protections myself."), d)


@cap("security.firewall", "security_read", "Check Windows Firewall status", LOW, (), True, "22")
def firewall(op) -> Outcome:
    code, out, _e = op.ops.run(["netsh.exe", "advfirewall", "show", "allprofiles", "state"], 15)
    profiles = re.findall(r"(?m)^(\w+) Profile Settings:.*?\n-+\nState\s+(\w+)", out, re.S)
    if not profiles:
        return Outcome("info", out.strip()[:200] or "I couldn't read the firewall state.")
    off = [p for p, s in profiles if s.upper() != "ON"]
    return Outcome("info", "Firewall: " + ", ".join(f"{p} {s.lower()}" for p, s in profiles) + (". A profile is off; turn it on in Windows Security." if off else "."), profiles)


@cap("security.signature", "security_read", "Check a program's digital signature", LOW, ("path",), True, "22")
def signature(op, path: str = "") -> Outcome:
    p = op.check_path(path)
    if not p.is_file() or not re.fullmatch(r"[^'\"`$;|&<>\r\n]+", str(p)):
        raise OperatorError("I can only check an ordinary file with a plain name.")
    raw = op.ops.powershell(f"Get-AuthenticodeSignature -LiteralPath '{p}' | Select-Object Status,@{{n='Signer';e={{$_.SignerCertificate.Subject}}}} | ConvertTo-Json -Compress", 30).strip()
    d = json.loads(raw) if raw else {}
    status = {0: "Valid", 1: "UnknownError", 2: "NotSigned", 3: "HashMismatch", 4: "NotTrusted", 5: "NotSupportedFileFormat"}.get(d.get("Status"), str(d.get("Status")))
    return Outcome("info", f"{p.name}: signature {status}" + (f", signed by {str(d.get('Signer'))[:80]}" if d.get("Signer") else "") + (". Be careful with this file." if status not in ("Valid",) else "."), d)


@cap("security.risky_files", "files_read", "Look for unexpected programs or scripts in a folder", LOW, ("folder",), False, "22")
def risky_files(op, folder: str | None = None) -> Outcome:
    from jarvis.operator.files_ops import unusual
    return unusual(op, folder)


@cap("security.credential_files", "files_read", "Find files that look like they hold passwords or keys (names only)", LOW, ("folder",), False, "24")
def credential_files(op, folder: str | None = None) -> Outcome:
    roots = [op.check_path(folder)] if folder else op.roots()
    if not roots:
        raise OperatorError("No allowed folders exist yet.")
    root = roots[0] if len(roots) == 1 else type("R", (), {"name": "your folders"})()
    hits = [p for r in roots for p, _s in walk_files(r) if CREDENTIAL_NAMES.search(p.name)]
    return Outcome("info", f"{len(hits)} file(s) in {root.name} look like they hold credentials: " + ", ".join(p.name for p in hits[:10]) + ". I listed names only and did not open them." if hits else "No credential-looking files found.", [str(p) for p in hits])


@cap("security.app_access", "security_read", "Show which apps recently used the microphone or camera", LOW, ("device",), True, "23")
def app_access(op, device: str = "microphone") -> Outcome:
    if device not in ("microphone", "webcam"):
        raise OperatorError("I can check the microphone or the camera.")
    try:
        import winreg
    except ImportError:
        raise Unavailable("Windows only.") from None
    rows = []
    base = rf"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{device}"
    for sub in ("", r"\NonPackaged"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + sub) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    n = winreg.EnumKey(k, i)
                    if n == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(k, n) as s:
                            start = winreg.QueryValueEx(s, "LastUsedTimeStart")[0]
                            stop = winreg.QueryValueEx(s, "LastUsedTimeStop")[0]
                        rows.append({"app": n.replace("#", "\\").rsplit("\\", 1)[-1], "in_use": stop == 0 and start != 0, "last": start})
                    except OSError:
                        pass
        except OSError:
            pass
    now = [r["app"] for r in rows if r["in_use"]]
    recent = sorted(rows, key=lambda r: r["last"], reverse=True)[:5]
    return Outcome("info", (f"Using the {device} right now: " + ", ".join(now) + ". " if now else f"Nothing is using the {device} right now. ") + "Most recent: " + ", ".join(r["app"] for r in recent), rows)


@cap("security.open_credentials", "security_read", "Open Windows Credential Manager", LOW, (), True, "24")
def open_credentials(op) -> Outcome:
    op.ops.run(["rundll32.exe", "keymgr.dll,KRShowKeyMgr"], 10)
    return Outcome("unverified", "Opened Credential Manager. I don't read or copy anything stored in it.")


@cap("security.report", "security_read", "Write a security status report", LOW, (), True, "22")
def report(op) -> Outcome:
    parts = []
    for fn in (defender, firewall):
        try:
            parts.append(fn(op).message)
        except OperatorError as e:
            parts.append(str(e))
    f = op.home / "Documents" / "Jarvis reports"
    f.mkdir(parents=True, exist_ok=True)
    out = f / time.strftime("security-%Y%m%d-%H%M%S.txt")
    out.write_text("\n".join(parts), encoding="utf-8")
    return Outcome("verified" if out.exists() else "failed", " ".join(parts) + f" Saved as {out.name}.", str(out))


@cap("security.explain", "security_read", "Explain a common security warning", LOW, ("warning",), False, "22")
def explain(op, warning: str = "") -> Outcome:
    w = warning.lower()
    table = [("smartscreen", "SmartScreen warns when a program isn't well known. If you didn't download it on purpose from the official site, don't run it."),
             ("uac", "UAC asks whether a program may change your PC. Say yes only if you started it and know what it is."),
             ("certificate", "A certificate warning means the site couldn't prove who it is (expired, wrong name, or someone in between). Don't enter passwords."),
             ("not secure", "\"Not secure\" means the page isn't encrypted (http). Don't type passwords or card numbers there."),
             ("virus", "A virus warning from Defender means a file matched known malware. Let Windows quarantine it, then run a full scan."),
             ("phishing", "A phishing warning means the page imitates a real site to steal logins. Close it and go to the real site by typing its address.")]
    for k, v in table:
        if k in w:
            return Outcome("info", v)
    return Outcome("info", "I don't have a specific note for that warning. Don't click through it; tell me its exact wording and which program showed it.")
