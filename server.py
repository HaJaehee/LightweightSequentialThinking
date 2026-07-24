#!/usr/bin/env python
"""planning-mcp entry point.

    python server.py                      # stdio (what AnythingLLM spawns)
    python server.py --transport sse      # http://127.0.0.1:8931/sse

Standard library only - no pip install required.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `planning` importable no matter how the interpreter was invoked. The embeddable
# Python distribution runs in isolated mode, where the script's own directory is NOT added
# to sys.path and PYTHONPATH is ignored - without this, `import planning` fails.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from planning.config import SERVER_NAME, SERVER_VERSION, Config, setup_logging  # noqa: E402
from planning.handlers import PlanningHandlers  # noqa: E402
from planning.protocol import McpProtocol  # noqa: E402
from planning.schemas import TOOL_DEFINITIONS  # noqa: E402
from planning.store import Store  # noqa: E402
from planning.transport import serve_sse, serve_stdio  # noqa: E402


def build_protocol(config: Config, log=None) -> McpProtocol:
    store = Store(config.state_dir, max_plans=config.max_plans)
    handlers = PlanningHandlers(store, config)
    if handlers.approval_ui is not None:
        # Bind now, not at the first approval: a failure has to be visible at startup
        # rather than silently disarming the gate mid-workflow.
        if handlers.approval_ui.start():
            if log:
                log.warning(
                    "APPROVE PLANS AT -> %s   (leave this tab open; it alerts on new requests)",
                    handlers.approval_ui.url,
                )
        elif log:
            log.error(
                "Approval UI FAILED TO START - blocking approval is disarmed. "
                "Free the port range or set PLANNING_MCP_APPROVAL_PORT."
            )
    return McpProtocol(handlers, TOOL_DEFINITIONS, SERVER_NAME, SERVER_VERSION)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVER_NAME, description=__doc__)
    parser.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="SSE bind address (loopback only)")
    parser.add_argument("--port", type=int, default=8931, help="SSE port")
    parser.add_argument("--state-dir", default=None, help="Override PLANNING_MCP_STATE_DIR")
    parser.add_argument("--log-level", default=None, help="DEBUG / INFO / WARNING / ERROR")
    args = parser.parse_args(argv)

    config = Config.from_env(state_dir_override=args.state_dir)
    if args.log_level:
        config.log_level = args.log_level.upper()
    log = setup_logging(config.log_level)

    log.info("%s %s starting (%s)", SERVER_NAME, SERVER_VERSION, args.transport)
    log.info("State directory: %s", config.state_dir)
    if config.autoapprove:
        log.warning(
            "PLANNING_MCP_AUTOAPPROVE=true - the human approval gate is BYPASSED. "
            "This is a test-only setting; turn it off before real use."
        )
    if config.blocking_approval:
        log.info(
            "Blocking approval ON - request_user_approval(ASK_USER) holds the tool call "
            "open (agent loop paused) until you decide at http://127.0.0.1:%d/",
            config.approval_port,
        )
    else:
        log.warning(
            "PLANNING_MCP_BLOCKING_APPROVAL=false - the agent is only *asked* to stop and "
            "wait. A weak model may ignore that and keep executing."
        )

    protocol = build_protocol(config, log=log)

    if args.transport == "sse":
        serve_sse(protocol, host=args.host, port=args.port)
    else:
        serve_stdio(protocol)
    return 0


if __name__ == "__main__":
    sys.exit(main())
