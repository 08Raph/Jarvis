"""Records allowlisted events locally and, only with consent, sends them to the endpoint compiled into this build.
Never raises into the app: telemetry problems must not affect chatting."""
from __future__ import annotations

import json
import platform
from pathlib import Path

from jarvis import __version__
from jarvis.config import Config
from jarvis.net.policy import NetworkDisabled, NetworkPolicy
from jarvis.telemetry.consent import Consent
from jarvis.telemetry.schema import EVENTS, SchemaError, build_event, validate_event

OUTBOX_CAP = 500
BATCH_MAX = 100


def get_endpoint() -> str:
    """The collection server baked into this build (telemetry/endpoint.txt, written by the build script). Empty = none."""
    import sys
    roots = [Path(__file__).parent]
    if getattr(sys, "_MEIPASS", None):   # frozen (PyInstaller) build: data files live under the bundle folder
        roots.append(Path(sys._MEIPASS) / "jarvis" / "telemetry")
    for r in roots:
        try:
            return (r / "endpoint.txt").read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return ""


class TelemetryError(RuntimeError):
    pass


def _bucket(value: float | None, steps: tuple[int, ...]) -> int:
    v = value or 0
    pick = steps[0]
    for s in steps:
        if v >= s * 0.9:      # 7.9 GB counts as the 8 GB bucket
            pick = s
    return pick


class Telemetry:
    def __init__(self, cfg: Config, policy: NetworkPolicy | None = None, endpoint: str | None = None):
        self.cfg = cfg
        self.consent = Consent(cfg.data_dir / "telemetry.json")
        self.outbox = cfg.data_dir / "telemetry_outbox.jsonl"
        self.policy = policy or NetworkPolicy.load(cfg.settings_path)
        self.endpoint = get_endpoint() if endpoint is None else endpoint

    # ---- local queue ----
    def _read_outbox(self) -> list[dict]:
        out = []
        try:
            for line in self.outbox.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(validate_event(json.loads(line)))
                except (ValueError, SchemaError):
                    continue          # a damaged line is dropped, never sent
        except OSError:
            pass
        return out

    def _write_outbox(self, events: list[dict]) -> None:
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.outbox.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events[-OUTBOX_CAP:]), encoding="utf-8")
        tmp.replace(self.outbox)

    def clear_queue(self) -> None:
        self.outbox.unlink(missing_ok=True)

    def record(self, event: str, **data) -> bool:
        """Queue one event if the user consented to its category. Silent no-op otherwise. Never raises."""
        try:
            if event not in EVENTS or not self.consent.allows(EVENTS[event][0]):
                return False
            iid = self.consent.install_id()
            ev = build_event(event, iid, data)
            events = self._read_outbox()
            if event == "feature_used":      # merge counts within the same hour
                for e in events:
                    if e["event"] == event and e["t"] == ev["t"] and e["data"]["feature"] == data["feature"] and e["install_id"] == iid:
                        e["data"]["count"] += int(data.get("count", 1))
                        self._write_outbox(events)
                        return True
            events.append(ev)
            self._write_outbox(events)
            return True
        except Exception:
            return False

    def app_start_data(self) -> dict:
        from jarvis.hardware import detect_hardware
        import sys
        hw = detect_hardware(self.cfg.data_dir if self.cfg.data_dir.exists() else ".")
        if platform.system() == "Windows":
            parts = platform.version().split(".")
            build = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            osname = f"Windows {'11' if build >= 22000 else '10'} {platform.version()}" if len(parts) > 2 else "Windows 10"
        else:
            osname = {"Linux": "Linux", "Darwin": "macOS"}.get(platform.system(), "Linux")
        return {"app_version": __version__, "os": osname, "ram_gb_bucket": _bucket(hw.ram_gb, (4, 8, 16, 32, 64)),
                "cpu_threads_bucket": _bucket(hw.cpu_threads, (2, 4, 8, 16, 32)), "gpu_present": hw.gpu_name is not None,
                "packaged": bool(getattr(sys, "frozen", False))}

    # ---- transparency ----
    def preview(self) -> list[dict]:
        return self._read_outbox()

    # ---- sending ----
    def send(self, timeout: float = 10.0) -> dict:
        st = self.consent.state()
        if not st["enabled"]:
            raise TelemetryError("telemetry is off (or needs your agreement again); nothing was sent")
        if not self.endpoint:
            raise TelemetryError("this build has no telemetry server configured; nothing was sent")
        events = [e for e in self._read_outbox() if self.consent.allows(EVENTS[e["event"]][0])][:BATCH_MAX]
        if not events:
            return {"sent": 0}
        body = json.dumps({"events": events}, separators=(",", ":")).encode("utf-8")
        resp = self._post("/v1/telemetry", body, timeout)
        if not isinstance(resp, dict) or "accepted" not in resp:
            raise TelemetryError("the server gave an unexpected answer; events were kept")
        sent_keys = {json.dumps(e, sort_keys=True) for e in events}
        remaining = [e for e in self._read_outbox() if json.dumps(e, sort_keys=True) not in sent_keys]
        self._write_outbox(remaining)
        return {"sent": len(events), "accepted": resp.get("accepted"), "rejected": resp.get("rejected", 0)}

    def delete_remote(self, timeout: float = 15.0) -> dict:
        iid = self.consent.install_id()
        if not iid:
            raise TelemetryError("no install ID exists on this PC")
        if not self.endpoint:
            raise TelemetryError("this build has no telemetry server configured")
        return self._post("/v1/telemetry/delete", json.dumps({"install_id": iid}).encode("utf-8"), timeout, need_flag=False)

    def _post(self, path: str, body: bytes, timeout: float, need_flag: bool = True):
        try:
            if need_flag:
                resp = self.policy.open("telemetry", self.endpoint.rstrip("/") + path, timeout=timeout, data=body,
                                        headers={"Content-Type": "application/json", "User-Agent": f"ProjectJarvis/{__version__}"})
            else:      # deleting your data must work even after you switched sending off
                with self.policy.grant_once("telemetry"):
                    resp = self.policy.open("telemetry", self.endpoint.rstrip("/") + path, timeout=timeout, data=body,
                                            headers={"Content-Type": "application/json", "User-Agent": f"ProjectJarvis/{__version__}"})
            return json.loads(resp.read(100_000).decode("utf-8"))
        except NetworkDisabled as e:
            raise TelemetryError(str(e))
        except Exception as e:
            raise TelemetryError(f"could not reach the telemetry server ({type(e).__name__}); events were kept")


def record_quietly(cfg: Config, event: str, **data) -> None:
    try:
        Telemetry(cfg).record(event, **data)
    except Exception:
        pass


def flush_quietly(cfg: Config) -> None:
    """Best effort at the end of a command; only when the user consented and a server is configured."""
    try:
        t = Telemetry(cfg)
        if t.consent.state()["enabled"] and t.endpoint and t.policy.is_enabled("telemetry"):
            t.send(timeout=4.0)
    except Exception:
        pass
