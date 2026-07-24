"""Minimal MCP (JSON-RPC 2.0) protocol layer - standard library only.

Implemented directly rather than via the `mcp` SDK because the deployment target is a
locked-down corporate PC where `pip install` may not be available. The surface MCP
clients actually use is small: initialize, tools/list, tools/call, ping.

Note on errors: a failed tool call is returned as a NORMAL result whose payload has
`ok: false` plus a corrective `next_action`. A JSON-RPC-level error would surface to the
model as a raw client exception string, which reliably makes a weak model abandon the
protocol and answer from memory.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("planning-mcp.protocol")

PROTOCOL_VERSION = "2024-11-05"

METHOD_NOT_FOUND = -32601
INVALID_REQUEST = -32600
PARSE_ERROR = -32700


class McpProtocol:
    def __init__(
        self,
        handlers: Any,
        tools: list[dict[str, Any]],
        name: str,
        version: str,
        notifier: Any = None,
    ):
        self.handlers = handlers
        self.tools = tools
        self.name = name
        self.version = version
        self.tool_names = {t["name"] for t in tools}
        # Set by the transport once it owns the output stream. Lets a blocking handler
        # emit progress heartbeats that keep the client's request timer alive.
        self.notifier = notifier

    # ---- JSON-RPC plumbing ---------------------------------------------
    @staticmethod
    def _result(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    def handle_message(self, msg: Any) -> dict[str, Any] | list[dict[str, Any]] | None:
        if isinstance(msg, list):  # JSON-RPC batch
            out = [r for r in (self.handle_message(m) for m in msg) if r is not None]
            return out or None
        if not isinstance(msg, dict):
            return self._error(None, INVALID_REQUEST, "Request must be a JSON object.")

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        try:
            result = self._route(method, params)
        except Exception as exc:  # noqa: BLE001
            log.exception("Protocol error handling %s", method)
            if is_notification:
                return None
            return self._error(msg_id, INVALID_REQUEST, f"{type(exc).__name__}: {exc}")

        if result is _UNKNOWN_METHOD:
            if is_notification:
                return None  # unknown notifications are ignored by design
            return self._error(msg_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

        if is_notification:
            return None
        return self._result(msg_id, result)

    # ---- method routing -------------------------------------------------
    def _route(self, method: str | None, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
                "instructions": (
                    "Planning harness. Call plan_and_think before answering anything, get the "
                    "user's approval with request_user_approval, then track execution with "
                    "update_task_progress. Always obey the next_action field in every response."
                ),
            }
        if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}
        if method == "logging/setLevel":
            return {}
        return _UNKNOWN_METHOD

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments")
        if isinstance(arguments, str):
            # Some clients forward the raw string the model emitted.
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}

        if name not in self.tool_names:
            payload = {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "plan_status": "NONE",
                "next_action": "CALL_GET_CURRENT_PLAN",
                "next_action_hint": (
                    f"There is no tool named '{name}'. The available tools are: "
                    + ", ".join(sorted(self.tool_names))
                    + ". Call get_current_plan with plan_id='current' to resync."
                ),
            }
        else:
            # MCP only permits progress notifications for a request that supplied a
            # token. Its presence decides whether a blocking handler may wait past the
            # client's 60s request timeout.
            meta = params.get("_meta") or {}
            payload = self.handlers.dispatch(
                name,
                arguments,
                progress_token=meta.get("progressToken"),
                notifier=self.notifier,
            )

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": False}


class _Unknown:
    pass


_UNKNOWN_METHOD = _Unknown()
