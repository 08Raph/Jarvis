"""Network status and troubleshooting. Checks are plain connection tests; nothing is changed."""
from __future__ import annotations

import re
import time

from jarvis.operator.core import LOW, Outcome, OperatorError, Unavailable, cap, human_size

HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-\.]{0,251}[A-Za-z0-9])?$")


def _ps():
    from jarvis.operator.system_ops import _psutil
    return _psutil()


@cap("network.status", "network", "Show network adapters, addresses and whether they are up", LOW, (), False, "19")
def status(op) -> Outcome:
    ps = _ps()
    stats, addrs = ps.net_if_stats(), ps.net_if_addrs()
    rows = []
    for name, st in stats.items():
        ips = [a.address for a in addrs.get(name, []) if int(a.family) == 2]      # 2 = IPv4 (AF_INET); this module deliberately imports no network library
        rows.append({"name": name, "up": st.isup, "speed_mbps": st.speed, "ipv4": ips})
    up = [r for r in rows if r["up"] and r["ipv4"] and not r["ipv4"][0].startswith("127.")]
    msg = (f"{len(up)} connection(s) up: " + "; ".join(f"{r['name']} {', '.join(r['ipv4'])}" + (f" ({r['speed_mbps']} Mbps)" if r["speed_mbps"] else "") for r in up) if up else "No network adapter has an address: you appear to be offline.")
    down = [r["name"] for r in rows if not r["up"] and not r["name"].lower().startswith(("lo", "loopback"))]
    if down:
        msg += " Disconnected adapters: " + ", ".join(down[:6]) + "."
    return Outcome("info", msg, rows)


@cap("network.ip_config", "network", "Show IP, gateway and DNS settings", LOW, (), True, "19")
def ip_config(op) -> Outcome:
    code, out, _e = op.ops.run(["ipconfig.exe", "/all"], 15)
    wanted = ("IPv4 Address", "Default Gateway", "DNS Servers", "Subnet Mask", "Description", "DHCP Enabled")
    lines = [ln.strip() for ln in out.splitlines() if any(ln.strip().startswith(w) for w in wanted)]
    return Outcome("info", " | ".join(re.sub(r"\s*\.+\s*:", ":", ln) for ln in lines[:18]) or "Windows returned no address details.", lines)


@cap("network.throughput", "network", "Measure current upload and download speed", LOW, ("seconds",), False, "19")
def throughput(op, seconds: float = 2.0) -> Outcome:
    ps = _ps()
    a = ps.net_io_counters()
    t0 = time.time()
    time.sleep(max(0.5, min(float(seconds), 10)))
    b = ps.net_io_counters()
    dt = max(0.1, time.time() - t0)
    down, up = (b.bytes_recv - a.bytes_recv) / dt, (b.bytes_sent - a.bytes_sent) / dt
    return Outcome("info", f"Right now: download {human_size(down)}/s, upload {human_size(up)}/s (all programs together; Windows doesn't let me split it per program).", {"down": down, "up": up})


def _ping(op, host: str, tries: int = 4) -> dict:
    """Windows' own ping.exe from a fixed argument list. Nothing here opens a socket."""
    code, out, _e = op.ops.run(["ping.exe", "-n", str(tries), "-w", "2000", host], 25)
    rec = re.search(r"Received = (\d+)", out)
    ok = int(rec.group(1)) if rec else 0
    avg = re.search(r"Average = (\d+)ms", out)
    mx = re.search(r"Maximum = (\d+)ms", out)
    return {"host": host, "ok": ok, "lost": max(0, tries - ok), "avg_ms": float(avg.group(1)) if avg else None, "max_ms": float(mx.group(1)) if mx else None}


def _resolve(op, host: str) -> list[str]:
    code, out, _e = op.ops.run(["nslookup.exe", host], 15)
    tail = out.split("Name:", 1)[1] if "Name:" in out else ""
    return sorted(set(re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", tail)))


@cap("network.dns", "network", "Check that a website name resolves", LOW, ("host",), True, "19,20")
def dns(op, host: str = "example.com") -> Outcome:
    if not HOST_RE.match(host or ""):
        raise OperatorError("That doesn't look like a website name.")
    t = time.time()
    ips = _resolve(op, host)
    if not ips:
        return Outcome("failed", f"{host} did not resolve.")
    return Outcome("info", f"{host} resolves to {', '.join(ips[:4])} in {int((time.time() - t) * 1000)} ms.", ips)


@cap("network.latency", "network", "Measure round-trip time and packet loss to a host (ping)", LOW, ("host", "port"), True, "19,20")
def latency(op, host: str = "1.1.1.1", port: int = 443) -> Outcome:
    if not HOST_RE.match(host or "") or not (1 <= int(port) <= 65535):
        raise OperatorError("That host or port isn't valid.")
    r = _ping(op, host)
    if not r["ok"]:
        return Outcome("failed", f"{host} did not answer ({r['lost']} of {r['lost']} pings failed; some servers ignore ping).", r)
    return Outcome("info", f"{host}: average {r['avg_ms']} ms, slowest {r['max_ms']} ms, {r['lost']} of {r['lost'] + r['ok']} pings lost.", r)


@cap("network.diagnose", "network", "Diagnose a 'no internet' problem step by step", LOW, (), True, "19,20,47")
def diagnose(op) -> Outcome:
    steps, verdict = [], None
    st = status(op)
    up = [r for r in st.data if r["up"] and r["ipv4"] and not r["ipv4"][0].startswith(("127.", "169.254."))]
    steps.append(("A network adapter has an address", bool(up)))
    if not up:
        verdict = "No adapter has a usable address. Evidence: " + st.message + " Likely causes: Wi-Fi/Ethernet is off or unplugged, or the router isn't handing out addresses. Try reconnecting; I can open the network settings."
    ip = _ping(op, "1.1.1.1", 3)
    steps.append(("The internet answers by address (1.1.1.1)", bool(ip["ok"])))
    if verdict is None:
        resolved = bool(_resolve(op, "example.com"))
        steps.append(("Names resolve (DNS)", resolved))
        if not ip["ok"] and not resolved:
            verdict = "Your PC has an address but nothing beyond it answers: the router or the line is likely down (evidence: no reply by address and no DNS). Restart the router; if it persists, contact your provider."
        elif ip["ok"] and not resolved:
            verdict = "The internet is reachable by address but names don't resolve: this looks like a DNS problem (evidence: 1.1.1.1 answers, name lookup fails). Try another DNS server or restart the router. I won't change DNS settings without you asking."
        elif not ip["ok"]:
            verdict = "Names resolve but pings fail: pings may be blocked, or a firewall, VPN or the router is in the way."
        else:
            verdict = f"Everything I tested works (ping {ip['avg_ms']} ms, DNS fine). If a specific site fails, the problem is with that site."
    lines = "; ".join(f"{'OK' if ok else 'FAIL'}: {n}" for n, ok in steps)
    return Outcome("info", lines + ". " + verdict + " (This test pings 1.1.1.1 and looks up example.com using Windows' own tools.)", dict(steps))


@cap("network.open_settings", "network", "Open Windows network settings", LOW, (), True, "20")
def open_settings(op) -> Outcome:
    op.ops.open_target("ms-settings:network")
    return Outcome("unverified", "Opened the network settings.")
