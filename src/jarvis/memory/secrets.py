"""Refuse to put secrets into ordinary memory. Heuristic: it catches common shapes (passwords stated in words, well-known API
key and token formats, private keys, long random-looking strings, card numbers, ID numbers). It cannot catch everything, so the
user can always inspect and delete memories. The matched text is NEVER returned or logged: only a generic label."""
from __future__ import annotations

import re

_VALUE = r"(?:'[^'\n]*'|\"[^\"\n]*\"|\S+)"          # a quoted value may contain spaces

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an API key or token", re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
        r"AIza[0-9A-Za-z_-]{30,}|hf_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")),
    ("a password or secret", re.compile(
        r"(?i)\b(?:password|passwd|pwd|passphrase|pin(?: code)?|secret|api[ _-]?key|access[ _-]?token|auth(?:entication)?[ _-]?token|"
        r"bearer token|recovery code|security code|2fa code|one[- ]time code|otp)\b\s*(?:is|are|=|:|was)\s*" + _VALUE)),
    # .env / config style assignments whose NAME contains a secret word: DB_PASSWORD=..., client_secret: ..., GITHUB_TOKEN=..., SECRET_KEY='...'
    # (the word-boundary pattern above cannot see "secret" inside DB_SECRET because "_" is a word character)
    ("a password or secret", re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.\-])*(?:password|passwd|pwd|passphrase|secret|api[_\-]?key|access[_\-]?key|private[_\-]?key|"
        r"credentials?)(?:[_.\-][A-Za-z0-9_.\-]*)?\s*[=:]\s*" + _VALUE)),
    ("an API key or token", re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_.\-])*token\s*[=:]\s*(?:'[^'\n]{8,}'|\"[^\"\n]{8,}\"|[^\s'\";,]{8,})")),
    ("a password in a web address", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@")),
    ("an authorization header", re.compile(
        r"(?i)\b(?:proxy-)?authorization\s*[:=]\s*(?:bearer|basic|token|digest)?\s*\S+|\bbearer\s+(?=[A-Za-z0-9._~+/=\-]*\d)[A-Za-z0-9._~+/=\-]{12,}")),
    ("an ID number", re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{3}[ -]\d{3}[ -]\d{3}\b")),
]
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


class SecretRejected(ValueError):
    def __init__(self, label: str):
        self.label = label
        super().__init__(f"That looks like {label}, so I did not save it. I try not to store passwords, keys, tokens "
                         "or ID numbers, but this check is best effort and can miss some; don't put secrets in memory notes.")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def find_secret(text: str) -> str | None:
    """Generic label of the kind of secret found, or None."""
    for label, rx in _PATTERNS:
        if rx.search(text):
            return label
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "a payment card number"
    for m in _LONG_TOKEN.finditer(text):
        tok = m.group()
        if (re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok) and "/" not in tok[:1]
                and len(set(tok)) >= 12 and not re.fullmatch(r"[A-Za-z]+(?:[-_][A-Za-z]+)+", tok)):
            return "an API key or token"
    return None


def check(text: str) -> None:
    label = find_secret(text)
    if label:
        raise SecretRejected(label)


def redact(text: str) -> str:
    """For logs: replace anything that looks secret. Used before text is kept in summaries."""
    out = text
    for _, rx in _PATTERNS:
        out = rx.sub("[removed]", out)
    out = _LONG_TOKEN.sub(lambda m: "[removed]" if find_secret(m.group()) else m.group(), out)
    out = _CARD.sub(lambda m: "[removed]" if find_secret(m.group()) == "a payment card number" else m.group(), out)
    return out
