"""HTTP entry point for SiliconRail."""

from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .rtl import RTLParseError
from .service import Service


def env_address() -> tuple[str, int]:
    raw = os.environ.get("SILICONRAIL_ADDR", "127.0.0.1:8080")
    host, _, port = raw.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"invalid SILICONRAIL_ADDR: {raw!r}")
    return host, int(port)


_INVALID_REQUEST_MESSAGE = "body must be a JSON object with a string 'source'"


class Handler(BaseHTTPRequestHandler):
    service = Service()

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, self.service.health())
            return
        self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})

    def do_POST(self) -> None:
        if self.path not in ("/v1/rtl/parse", "/v1/rtl/widths"):
            self.send_json(404, {"error": {"code": "not_found", "message": f"no route for {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.invalid_request()
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("source"), str):
            self.invalid_request()
            return
        if self.path == "/v1/rtl/widths":
            try:
                ir = self.service.analyze_widths(payload["source"])
            except RTLParseError as exc:
                self.parse_error(exc)
                return
            self.send_json(200, ir)
            return
        try:
            ir = self.service.parse_rtl(payload["source"])
        except RTLParseError as exc:
            self.parse_error(exc)
            return
        self.send_json(200, ir)

    def parse_error(self, exc: RTLParseError) -> None:
        self.send_json(
            422,
            {
                "error": {
                    "code": exc.code,
                    "line": exc.line,
                    "column": exc.column,
                    "message": exc.message,
                }
            },
        )

    def invalid_request(self) -> None:
        self.send_json(
            400,
            {"error": {"code": "invalid_request", "message": _INVALID_REQUEST_MESSAGE}},
        )

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence per-request logging so recorded output stays stable."""


def main() -> int:
    parser = argparse.ArgumentParser(prog="siliconrail.server", description="芯片前端设计与 RTL 流程工具链")
    host, port = env_address()
    parser.add_argument("--host", default=host)
    parser.add_argument("--port", type=int, default=port)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SiliconRail listening on http://{args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
