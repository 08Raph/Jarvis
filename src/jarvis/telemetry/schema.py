"""The ONLY data Project JARVIS may ever send for telemetry. It is an allowlist: every event type and every field is listed
here, each with a strict validator. Anything else is rejected on the PC before queueing and again on the server.

There is deliberately no field that can hold free text, so prompts, replies, file names, paths, user names, exception messages,
or anything typed by the user cannot be carried even by accident. If you add a field you MUST bump NOTICE_VERSION in consent.py
(users are then asked to agree again)."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

SCHEMA_VERSION = 1

_VERSION = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_OS = re.compile(r"Windows (10|11) \d{1,2}\.\d{1,2}\.\d{4,6}|Windows (10|11)|Linux|macOS")
_ID = re.compile(r"[0-9a-f]{32}")
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:00:00Z")


class SchemaError(ValueError):
    pass


def _enum(*vals):
    return lambda v: isinstance(v, (str, int)) and not isinstance(v, bool) and v in vals


def _re(rx):
    return lambda v: isinstance(v, str) and len(v) <= 64 and rx.fullmatch(v) is not None


def _bool(v):
    return isinstance(v, bool)


def _float(lo, hi):
    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and lo <= v <= hi


def _int(lo, hi):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


# event -> (category, {field: validator}, required fields)
EVENTS: dict[str, tuple[str, dict, tuple]] = {
    "app_start": ("diagnostics", {
        "app_version": _re(_VERSION), "os": _re(_OS), "ram_gb_bucket": _enum(4, 8, 16, 32, 64),
        "cpu_threads_bucket": _enum(2, 4, 8, 16, 32), "gpu_present": _bool, "packaged": _bool},
        ("app_version", "os", "ram_gb_bucket", "cpu_threads_bucket", "gpu_present", "packaged")),
    "selftest_result": ("diagnostics", {
        "model": _re(_MODEL_ID), "passed": _bool, "load_seconds": _float(0, 3600),
        "tokens_per_second": _float(0, 10000), "runtime_version": _re(_VERSION)},
        ("model", "passed")),
    "model_installed": ("diagnostics", {"model": _re(_MODEL_ID), "source": _enum("catalog", "file")}, ("model", "source")),
    "error": ("diagnostics", {
        "component": _enum("model_load", "model_download", "chat", "voice", "selftest", "setup"),
        "code": _enum("runtime_unavailable", "load_failed", "integrity_failed", "download_failed", "unknown")},
        ("component", "code")),
    "feature_used": ("usage", {
        "feature": _enum("chat", "voice", "setup", "online", "models", "selftest", "personality"),
        "count": _int(1, 100000)}, ("feature", "count")),
}


def categories() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"diagnostics": [], "usage": []}
    for name, (cat, fields, _) in EVENTS.items():
        out[cat].append(f"{name}: {', '.join(fields)}")
    return out


def hour_bucket(now: datetime | None = None) -> str:
    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:00:00Z")      # hour precision only: less fingerprinting than exact times


from jarvis.memory.secrets import find_secret as _find_secret  # noqa: E402


def validate_event(obj) -> dict:
    """Return a clean copy of a valid event or raise SchemaError. Used on the PC before queueing and by the server."""
    if not isinstance(obj, dict) or set(obj) != {"schema", "event", "t", "install_id", "data"}:
        raise SchemaError("envelope must contain exactly: schema, event, t, install_id, data")
    if obj["schema"] != SCHEMA_VERSION:
        raise SchemaError("unsupported schema version")
    if obj["event"] not in EVENTS:
        raise SchemaError("unknown event type")
    if not (isinstance(obj["t"], str) and _TS.fullmatch(obj["t"])):
        raise SchemaError("bad timestamp")
    if not (isinstance(obj["install_id"], str) and _ID.fullmatch(obj["install_id"])):
        raise SchemaError("bad install id")
    _, fields, required = EVENTS[obj["event"]]
    data = obj["data"]
    if not isinstance(data, dict):
        raise SchemaError("data must be an object")
    for k, v in data.items():
        if k not in fields:
            raise SchemaError(f"field {k!r} is not allowed for event {obj['event']!r}")
        if not fields[k](v):
            raise SchemaError(f"value for {k!r} is not acceptable")
        if isinstance(v, str) and _find_secret(v):
            raise SchemaError(f"value for {k!r} looks like a secret and was refused")   # defence in depth; best effort
    for k in required:
        if k not in data:
            raise SchemaError(f"missing required field {k!r}")
    return {"schema": obj["schema"], "event": obj["event"], "t": obj["t"], "install_id": obj["install_id"], "data": dict(data)}


def build_event(event: str, install_id: str, data: dict, now: datetime | None = None) -> dict:
    return validate_event({"schema": SCHEMA_VERSION, "event": event, "t": hour_bucket(now), "install_id": install_id, "data": data})
