"""Network status and troubleshooting. Checks are plain connection tests; nothing is changed."""
from __future__ import annotations

import re
import socket
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
        ips = [a.address for a in addrs.get(name, []) if a.family == socket.AF_INET]
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


@cap("network.dns", "network", "Check that a website name resolves", LOW, ("host",), False, "19,20")
def dns(op, host: str = "example.com") -> Outcome:
    if not HOST_RE.match(host or ""):
        raise OperatorError("That doesn't look like a website name.")
    t = time.time()
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return Outcome("failed", f"{host} did not resolve ({e.strerror or 'DNS lookup failed'}).")
    ips = sorted({i[4][0] for i in infos})
    return Outcome("info", f"{host} resolves to {', '.join(ips[:4])} in {int((time.time() - t) * 1000)} ms.", ips)


def tcp_latency(host: str, port: int = 443, tries: int = 4, timeout: float = 2.0) -> dict:
    times, lost = [], 0
    for _ in range(tries):
        t = time.time()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                times.append((time.time() - t) * 1000)
        except OSError:
            lost += 1
    return {"host": host, "port": port, "ok": len(times), "lost": lost, "avg_ms": round(sum(times) / len(times), 1) if times else None, "max_ms": round(max(times), 1) if times else None}


@cap("network.latency", "network", "Measure connection time and packet loss to a host", LOW, ("host", "port"), False, "19,20")
def latency(op, host: str = "1.1.1.1", port: int = 443) -> Outcome:
    if not HOST_RE.match(host or "") or not (1 <= int(port) <= 65535):
        raise OperatorError("That host or port isn't valid.")
    r = tcp_latency(host, int(port))
    if not r["ok"]:
        return Outcome("failed", f"I couldn't connect to {host}:{port} at all ({r['lost']} of {r['lost']} attempts failed).", r)
    return Outcome("info", f"{host}: average {r['avg_ms']} ms, slowest {r['max_ms']} ms, {r['lost']} of {r['lost'] + r['ok']} connection attempts failed. (This is connection time, not a ping.)", r)


@cap("network.diagnose", "network", "Diagnose a 'no internet' problem step by step", LOW, (), False, "19,20,47")
def diagnose(op) -> Outcome:
    steps, verdict = [], None
    st = status(op)
    up = [r for r in st.data if r["up"] and r["ipv4"] and not r["ipv4"][0].startswith(("127.", "169.254."))]
    steps.append(("A network adapter has an address", bool(up)))
    if not up:
        verdict = "No adapter has a usable address. Evidence: " + st.message + " Likely causes: Wi-Fi/Ethernet is off or unplugged, or the router isn't handing out addresses. Try reconnecting; I can open the network settings."
    ip = tcp_latency("1.1.1.1", 443, 3, 2.0)
    steps.append(("The internet answers by address (1.1.1.1)", bool(ip["ok"])))
    resolved = None
    if verdict is None:
        try:
            socket.getaddrinfo("example.com", None)
            resolved = True
        except socket.gaierror:
            resolved = False
        steps.append(("Names resolve (DNS)", resolved))
        if not ip["ok"] and resolved is False:
            verdict = "Your PC has an address but nothing beyond it answers: the router or the line is likely down (evidence: no reply by address and no DNS). Restart the router; if it persists, contact your provider."
        elif ip["ok"] and resolved is False:
            verdict = "The internet is reachable by address but names don't resolve: this looks like a DNS problem (evidence: connection to 1.1.1.1 works, name lookup fails). Try another DNS server or restart the router. I won't change DNS settings without you asking."
        elif not ip["ok"]:
            verdict = "Names resolve but connections fail: something between you and the internet is blocking (a firewall, VPN or the router)."
        else:
            verdict = f"Everything I tested works (connection {ip['avg_ms']} ms, DNS fine). If a specific site fails, the problem is with that site."
    lines = "; ".join(f"{'OK' if ok else 'FAIL'}: {n}" for n, ok in steps)
    return Outcome("info", lines + ". " + verdict + " (This test makes a connection to 1.1.1.1 and looks up example.com.)", dict(steps))


@cap("network.open_settings", "network", "Open Windows network settings", LOW, (), True, "20")
def open_settings(op) -> Outcome:
    op.ops.open_target("ms-settings:network")
    return Outcome("unverified", "Opened the network settings.")
