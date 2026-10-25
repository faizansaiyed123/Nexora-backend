"""Loopback-only fixture server for isolated browser E2E tests.

Only a fixed allowlist of HTML fixture files is served; no directory listing,
path traversal, proxying, or arbitrary filesystem reads are possible.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
ALLOWED_FILES = frozenset(
    {
        "retail-price-high.html",
        "retail-price-low.html",
        "retail-price-js.html",
        "retail-price-missing.html",
        "retail-discovery-product.html",
    }
)


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "NexoraFixtureServer/1.0"

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        prefix = "/fixtures/"
        filename = parsed.path.removeprefix(prefix) if parsed.path.startswith(prefix) else ""
        if (
            parsed.query
            or parsed.fragment
            or filename not in ALLOWED_FILES
            or "/" in filename
            or "\\" in filename
        ):
            self.send_error(404)
            return

        file_path = FIXTURE_DIR / filename
        try:
            body = file_path.read_bytes()
        except OSError:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        parsed = urlsplit(self.path)
        prefix = "/fixtures/"
        filename = parsed.path.removeprefix(prefix) if parsed.path.startswith(prefix) else ""
        if parsed.query or filename not in ALLOWED_FILES or "/" in filename or "\\" in filename:
            self.send_error(404)
            return
        try:
            body = (FIXTURE_DIR / filename).read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        print("NEXORA_FIXTURE_HTTP " + format % args, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    if args.host != "127.0.0.1" or args.port != 8877:
        raise SystemExit("The fixture server must bind to 127.0.0.1:8877.")
    server = ThreadingHTTPServer((args.host, args.port), FixtureHandler)
    print(f"Nexora E2E fixtures on http://{args.host}:{args.port}/fixtures/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
