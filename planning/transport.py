"""Transports: stdio (primary) and SSE (optional).

stdio needs no port, no firewall exception and no CORS, which matters on a locked-down
corporate laptop. SSE exists only for the case where the server must outlive AnythingLLM
restarts or be shared by several workspaces; it binds to loopback only.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .protocol import McpProtocol

log = logging.getLogger("planning-mcp.transport")


# ---------------------------------------------------------------------------
# stdio
# ---------------------------------------------------------------------------


class StdioNotifier:
    """Thread-safe sender for server-initiated notifications.

    Needed because a blocking approval must emit `notifications/progress` heartbeats
    from a side thread while the main stdio loop sits inside a tool handler. Writes are
    serialized so a heartbeat can never interleave with a response frame.
    """

    def __init__(self, out) -> None:
        self._out = out
        self._lock = threading.Lock()

    def send(self, method: str, params: dict[str, Any]) -> None:
        frame = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params}, ensure_ascii=False
        )
        try:
            with self._lock:
                self._out.write(frame + "\n")
                self._out.flush()
        except (BrokenPipeError, ValueError):
            pass  # client went away; the handler will unblock on its own timeout

    def progress(self, token: Any, progress: float, message: str | None = None) -> None:
        params: dict[str, Any] = {"progressToken": token, "progress": progress}
        if message:
            params["message"] = message
        self.send("notifications/progress", params)


def serve_stdio(protocol: McpProtocol) -> None:
    """Newline-delimited JSON-RPC over stdin/stdout."""
    out = sys.stdout
    # Hard guarantee: stdout belongs to the protocol. Any stray print() anywhere in the
    # process goes to stderr instead of corrupting the JSON-RPC stream.
    sys.stdout = sys.stderr
    protocol.notifier = StdioNotifier(out)

    log.info("planning-mcp listening on stdio")
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("Discarded unparseable line from client")
                continue
            response = protocol.handle_message(msg)
            if response is None:
                continue
            out.write(json.dumps(response, ensure_ascii=False) + "\n")
            out.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    log.info("planning-mcp stdio stream closed")


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------


class _SseSessions:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.queues: dict[str, queue.Queue] = {}

    def create(self) -> tuple[str, queue.Queue]:
        session_id = uuid.uuid4().hex
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.queues[session_id] = q
        return session_id, q

    def get(self, session_id: str) -> queue.Queue | None:
        with self.lock:
            return self.queues.get(session_id)

    def drop(self, session_id: str) -> None:
        with self.lock:
            self.queues.pop(session_id, None)


def _make_handler(protocol: McpProtocol, sessions: _SseSessions):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/sse", "/"):
                self.send_error(404)
                return

            session_id, q = sessions.create()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            log.info("SSE session %s opened", session_id)

            try:
                self._send_event("endpoint", f"/messages?session_id={session_id}")
                while True:
                    try:
                        payload = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if payload is None:
                        break
                    self._send_event("message", json.dumps(payload, ensure_ascii=False))
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                sessions.drop(session_id)
                log.info("SSE session %s closed", session_id)

        def _send_event(self, event: str, data: str) -> None:
            frame = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
            self.wfile.write(frame)
            self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in ("/messages", "/message"):
                self.send_error(404)
                return
            session_id = (parse_qs(parsed.query).get("session_id") or [""])[0]
            q = sessions.get(session_id)
            if q is None:
                self.send_error(404, "Unknown session_id")
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                msg = json.loads(body) if body else None
            except json.JSONDecodeError:
                self.send_error(400, "Malformed JSON")
                return

            response = protocol.handle_message(msg)
            self.send_response(202)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            if response is not None:
                q.put(response)

    return Handler


def serve_sse(protocol: McpProtocol, host: str = "127.0.0.1", port: int = 8931) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        # Loopback only: this server has no authentication and exposes plan contents.
        log.warning("Refusing to bind %s; falling back to 127.0.0.1", host)
        host = "127.0.0.1"
    sessions = _SseSessions()
    httpd = ThreadingHTTPServer((host, port), _make_handler(protocol, sessions))
    log.info("planning-mcp listening on http://%s:%d/sse", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
