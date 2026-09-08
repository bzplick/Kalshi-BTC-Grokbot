"""Rebuild paper dashboard data, then serve dashboard/ on localhost.

Stdlib only. Does not call Kalshi or post orders.

    python serve_dashboard.py
    python serve_dashboard.py --port 8765
"""

from __future__ import annotations

import argparse
import functools
import http.server
import socketserver
import sys

from build_dashboard import DASHBOARD, main as build_main

DEFAULT_PORT = 8765


class ReuseTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the dashboard directory; discourage stale data.json."""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild data.json and serve the paper dashboard.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Listen port (default 8765)")
    parser.add_argument("--no-build", action="store_true", help="Serve existing dashboard/data.json without rebuilding")
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    args = parser.parse_args(argv)

    if not args.no_build:
        rc = build_main([])
        if rc != 0:
            return rc

    DASHBOARD.mkdir(parents=True, exist_ok=True)
    handler = functools.partial(NoCacheHandler, directory=str(DASHBOARD))
    try:
        with ReuseTCPServer((args.bind, args.port), handler) as httpd:
            url = f"http://{args.bind}:{args.port}/"
            print(f"Paper dashboard (dry-run) at {url}", flush=True)
            print("Ctrl+C to stop. Live trading is off.", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nStopped.", flush=True)
                return 0
    except OSError as exc:
        print(f"Could not bind {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
