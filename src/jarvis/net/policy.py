"""The ONLY module allowed to open network connections.

Every optional online feature is OFF by default and must be enabled explicitly.
Local inference never goes through here, so it cannot send prompts anywhere.
`tests/test_offline_guarantees.py` enforces this by scanning the source tree for
network imports outside this file and by running core paths with sockets blocked.

Features:
  model_download   fetch a model package the user chose (integrity-checked afterwards)
  update_check     fetch a signed version manifest (never auto-installs)
  online_assistant  talk to the Jarvis Online service the USER named (sign-in required; see docs/ONLINE.md). Off until the user connects
  web_research     Prompt 7: the research tools (web_fetch, web_search) fetch PUBLIC web pages / Wikipedia / a SearXNG instance the user named.
                   Off until enabled; only public https hosts (private/loopback/link-local addresses are refused, also after redirects).
  web_search, telemetry, crash_reporting, account_sync   RESERVED: no implementation exists
  cloud_ai         RESERVED: no provider integration exists; enabling needs cost acknowledgement
"""
from __future__ import annotations

import ipaddress
import json
import socket
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FEATURES = ("model_download", "update_check", "online_assistant", "web_research", "web_search", "telemetry",
            "crash_reporting", "account_sync", "cloud_ai")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class NetworkDisabled(PermissionError):
    """Raised when a feature is not enabled; no connection was attempted."""


_NAT64 = ipaddress.ip_network("64:ff9b::/96")             # well-known NAT64 prefix: the last 32 bits are an IPv4 address
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")      # local-use NAT64 (RFC 8215): never a public destination
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


def _address_problem(ip) -> str:
    """'' if the address is a public destination; otherwise why not. IPv6 forms that EMBED an IPv4 address are judged by the embedded address,
    so ::ffff:127.0.0.1, 64:ff9b::7f00:1 (NAT64) and 2002:7f00:1:: (6to4) cannot be used to reach loopback or a private network."""
    if ip.version == 6:
        embedded = None
        if ip.ipv4_mapped is not None:
            embedded = ip.ipv4_mapped
        elif ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _NAT64_LOCAL:
            return "a local-use NAT64 address"
        elif ip in _SIX_TO_FOUR:
            embedded = ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
        if embedded is not None and not embedded.is_global:
            return f"an IPv6 address that embeds the non-public IPv4 address {embedded}"
    if not ip.is_global:
        return "a non-public address"
    return ""


def assert_public_host(host: str) -> None:
    """Refuse hosts that resolve to private, loopback, link-local, multicast or reserved addresses (server-side request forgery guard).
    Limit: the name is resolved here and again when connecting, so a hostile DNS server could answer differently the second
    time (DNS rebinding). `NetworkPolicy.open(require_public=True)` therefore also checks the address the connection really reached,
    and refuses to hand back the response if it was not public (the request itself has then already been sent)."""
    if not host:
        raise NetworkDisabled("no host in the URL")
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as e:
        raise NetworkDisabled(f"cannot resolve {host!r}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        why = _address_problem(ip)
        if why:
            raise NetworkDisabled(f"{host!r} resolves to {why} ({ip}); refused")


def _peer_address(resp) -> str | None:
    """The IP address the connection behind an HTTP response is really connected to (None if it cannot be determined)."""
    for chain in (("fp", "raw", "_sock"), ("raw", "_sock"), ("fp", "_sock"), ("_sock",)):
        o = resp
        for attr in chain:
            o = getattr(o, attr, None)
            if o is None:
                break
        else:
            try:
                return str(o.getpeername()[0]).split("%")[0]
            except (OSError, AttributeError, IndexError, TypeError):
                continue
    return None


def assert_peer_public(resp) -> None:
    """DNS-rebinding guard: the socket that produced `resp` must be connected to a public address. Fails closed when it cannot be read."""
    peer = _peer_address(resp)
    if peer is None:
        raise NetworkDisabled("could not verify which address the connection reached, so the response was discarded")
    why = _address_problem(ipaddress.ip_address(peer))
    if why:
        raise NetworkDisabled(f"the connection reached {why} ({peer}) although the name looked public (DNS rebinding?); response discarded")


@dataclass
class NetworkPolicy:
    enabled: dict[str, bool] = field(default_factory=lambda: {f: False for f in FEATURES})
    cloud_ai_cost_acknowledged: bool = False
    allow_insecure_localhost: bool = False  # tests / development only
    audit: list[dict] = field(default_factory=list)  # who/where/when only, never request content

    def is_enabled(self, feature: str) -> bool:
        self._check_name(feature)
        return bool(self.enabled.get(feature, False))

    @staticmethod
    def _check_name(feature: str) -> None:
        if feature not in FEATURES:
            raise ValueError(f"unknown network feature {feature!r}")

    def enable(self, feature: str, cost_acknowledged: bool = False) -> None:
        self._check_name(feature)
        if feature == "cloud_ai":
            if not cost_acknowledged:
                raise NetworkDisabled("cloud_ai requires explicit acknowledgement that the provider may bill you")
            self.cloud_ai_cost_acknowledged = True
        self.enabled[feature] = True

    def disable(self, feature: str) -> None:
        self._check_name(feature)
        self.enabled[feature] = False
        if feature == "cloud_ai":
            self.cloud_ai_cost_acknowledged = False

    @contextmanager
    def grant_once(self, feature: str, cost_acknowledged: bool = False):
        """Enable a feature for one user-approved action, then restore the previous state."""
        was = self.is_enabled(feature)
        self.enable(feature, cost_acknowledged)
        try:
            yield
        finally:
            if not was:
                self.disable(feature)

    def open(self, feature: str, url: str, timeout: float = 30.0, data: bytes | None = None,
             headers: dict[str, str] | None = None, allowed_host_suffixes: tuple[str, ...] | None = None,
             require_public: bool = False):
        """Open a URL for an enabled feature. Raises NetworkDisabled before any socket is touched.
        allowed_host_suffixes (e.g. ("huggingface.co", "hf.co")) restricts the first URL AND every redirect target."""
        if not self.is_enabled(feature):
            raise NetworkDisabled(f"network feature {feature!r} is disabled")
        if feature == "cloud_ai" and not self.cloud_ai_cost_acknowledged:
            raise NetworkDisabled("cloud_ai cost not acknowledged")
        host = self._validate_url(url, allowed_host_suffixes)
        if require_public:
            assert_public_host(host)
        self._record(feature, host, "POST" if data is not None else "GET", len(data) if data else 0)
        policy = self

        class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
            # Redirect targets get the same checks and audit entry as the original URL.
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                h = policy._validate_url(newurl, allowed_host_suffixes)
                if require_public:
                    assert_public_host(h)
                policy._record(feature, h, "GET", 0)
                return super().redirect_request(req, fp, code, msg, hdrs, newurl)

        opener = urllib.request.build_opener(_GuardedRedirect)
        req = urllib.request.Request(url, data=data, headers=headers or {})
        resp = opener.open(req, timeout=timeout)
        if require_public and not req.has_proxy():       # through a proxy the peer is the proxy; the proxy resolves the name itself
            try:
                assert_peer_public(resp)
            except BaseException:
                resp.close()
                raise
        return resp

    def _validate_url(self, url: str, allowed_host_suffixes: tuple[str, ...] | None = None) -> str:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if allowed_host_suffixes and not any(host == s or host.endswith("." + s) for s in allowed_host_suffixes):
            raise NetworkDisabled(f"host {host!r} is not on this feature's allowed list {list(allowed_host_suffixes)}")
        is_local = host in _LOCAL_HOSTS
        if parts.scheme != "https" and not (parts.scheme == "http" and is_local and self.allow_insecure_localhost):
            raise NetworkDisabled("only https URLs are allowed")
        return host

    def _record(self, feature: str, host: str, method: str, bytes_sent: int) -> None:
        self.audit.append({"time": datetime.now(timezone.utc).isoformat(), "feature": feature, "host": host,
                           "method": method, "bytes_sent": bytes_sent})
        del self.audit[:-1000]

    # persistence: flags only (audit is in-memory) -------------------------
    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({"enabled": self.enabled,
                                          "cloud_ai_cost_acknowledged": self.cloud_ai_cost_acknowledged},
                                         indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "NetworkPolicy":
        p = cls()
        f = Path(path)
        if f.exists():
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or not isinstance(raw.get("enabled", {}), dict):
                    raise ValueError("bad settings shape")
            except (ValueError, OSError):
                return p  # unreadable settings fail SAFE: every online feature stays off
            for k, v in raw.get("enabled", {}).items():
                if k in FEATURES:
                    p.enabled[k] = bool(v)
            p.cloud_ai_cost_acknowledged = bool(raw.get("cloud_ai_cost_acknowledged", False))
            if p.enabled["cloud_ai"] and not p.cloud_ai_cost_acknowledged:
                p.enabled["cloud_ai"] = False  # a hand-edited file cannot skip the acknowledgement
        return p
