"""Run the service:  python -m jarvis_server --db data/jarvis.db [--host 127.0.0.1 --port 8080]
Public exposure requires TLS: either --tls-cert/--tls-key, or --behind-tls-proxy when a reverse proxy
(Caddy, Cloudflare Tunnel, nginx) terminates HTTPS in front of this process."""
from __future__ import annotations

import argparse
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jarvis_server.app import MAX_BODY, Service
from jarvis_server.backends import load_backend
from jarvis_server.db import DB

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def make_handler(service: Service, tls: bool):
    class Handler(BaseHTTPRequestHandler):
        timeout = 30
        server_version = "JarvisOnline"
        sys_version = ""

        def _ip(self) -> str:
            if service.trust_proxy:
                xff = self.headers.get("X-Forwarded-For", "")
                if xff:
                    return xff.split(",")[-1].strip()[:64]
            return self.client_address[0]

        def _send(self, status: int, payload: dict) -> None:
            import json
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if tls:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            self.end_headers()
            self.wfile.write(data)

        def _run(self, method: str) -> None:
            body = b""
            if method == "POST":
                try:
                    n = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    n = -1
                if n < 0:
                    return self._send(411, {"error": "length_required", "message": "Content-Length required."})
                if n > MAX_BODY:
                    return self._send(413, {"error": "too_large", "message": "Request too large."})
                body = self.rfile.read(n)
            status, payload = service.handle(method, self.path, dict(self.headers), body, self._ip())
            self._send(status, payload)

        def do_GET(self): self._run("GET")
        def do_POST(self): self._run("POST")

        def log_message(self, fmt, *args):  # method/path/status only: no IPs, no bodies
            sys.stderr.write(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jarvis_server")
    ap.add_argument("--db", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--model-dir", help="installed Jarvis model folder; without it the service answers 'no model connected'")
    ap.add_argument("--terms-file", help="your edited terms (default: bundled DRAFT template)")
    ap.add_argument("--tls-cert")
    ap.add_argument("--tls-key")
    ap.add_argument("--behind-tls-proxy", action="store_true", help="a reverse proxy provides HTTPS; trust its X-Forwarded-For")
    ap.add_argument("--no-telemetry", action="store_true", help="do not accept anonymous app telemetry")
    ap.add_argument("--telemetry-retention-days", type=int, default=180)
    ap.add_argument("--allow-draft-terms", action="store_true", help="development only")
    a = ap.parse_args(argv)

    public = a.host not in LOOPBACK
    tls = bool(a.tls_cert and a.tls_key)
    terms = Path(a.terms_file).read_text(encoding="utf-8") if a.terms_file else None
    service_terms = terms
    if public and not (tls or a.behind_tls_proxy):
        print("Refusing to listen on a non-local address without HTTPS. Use --tls-cert/--tls-key, or --behind-tls-proxy.", file=sys.stderr)
        return 2
    if (a.behind_tls_proxy or public) and "[OPERATOR:" in (terms if terms is not None else (Path(__file__).with_name("terms_en.md").read_text(encoding="utf-8"))) \
            and not a.allow_draft_terms:
        print("The terms still contain [OPERATOR: ...] placeholders. Edit them (with legal review) and pass --terms-file.", file=sys.stderr)
        return 2
    try:
        backend = load_backend(a.model_dir)
    except Exception as exc:
        print(f"Could not load the model from {a.model_dir}: {exc}", file=sys.stderr)
        return 2
    svc = Service(DB(a.db), backend, service_terms, trust_proxy=a.behind_tls_proxy,
                  telemetry_enabled=not a.no_telemetry, telemetry_retention_days=max(1, a.telemetry_retention_days))
    httpd = ThreadingHTTPServer((a.host, a.port), make_handler(svc, tls or a.behind_tls_proxy))
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(a.tls_cert, a.tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    print(f"Jarvis Online listening on {a.host}:{a.port}  model_connected={backend.available()}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
