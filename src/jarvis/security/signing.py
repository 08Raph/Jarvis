"""Ed25519 detached signatures over canonical JSON (manifests and update notices).

The PUBLISHER keeps the private key offline; the app embeds only the public key.
This repo ships no real keys. Until a public key is configured, signed manifests
cannot be verified and the app must not trust remote manifests.
"""
from __future__ import annotations

import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def generate_keypair() -> tuple[bytes, bytes]:
    """Returns (private_raw, public_raw). For publisher tooling and tests."""
    sk = Ed25519PrivateKey.generate()
    return (sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption()),
            sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def sign(obj: dict, private_raw: bytes) -> str:
    return base64.b64encode(Ed25519PrivateKey.from_private_bytes(private_raw).sign(canonical(obj))).decode()


def verify(obj: dict, signature_b64: str, public_raw: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_raw).verify(base64.b64decode(signature_b64), canonical(obj))
        return True
    except (InvalidSignature, ValueError):
        return False
