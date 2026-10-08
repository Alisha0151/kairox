"""Zero-dependency HTTP server exposing the exact same routes as the FastAPI app.

Used by the test-suite / e2e script and as a fallback:  python -m app.stdlib_server
(no WebSocket here -- the dashboard falls back to polling).
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from .api import Context, dispatch


def make_handler(ctx: Context):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("access-control-allow-origin", "*")
            self.send_header("access-control-allow-headers", "content-type")
            self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")

        def _handle(self, method: str):
            u = urlparse(self.path)
            n = int(self.headers.get("content-length") or 0)
            try:
                body = json.loads(self.rfile.read(n)) if n else {}
            except json.JSONDecodeError:
                body = {}
            try:
                status, obj = dispatch(ctx, method, u.path, dict(parse_qsl(u.query)), body)
            except Exception as e:  # surface as 500 rather than dropping the connection
                status, obj = 500, {"error": f"{type(e).__name__}: {e}"}
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("content-length", "0")
            self.end_headers()

    return H


def serve(ctx: Context, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(ctx))


if __name__ == "__main__":
    c = Context()
    c.start_background()
    port = int(os.environ.get("PORT", "8000"))
    print(f"[kairox] stdlib server on :{port}", flush=True)
    serve(c, port).serve_forever()
