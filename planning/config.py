"""Runtime configuration. All settings are optional with safe defaults."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "planning-mcp"
SERVER_VERSION = "1.14.2"

# The state dir is resolved from this file, NOT from the working directory.
# AnythingLLM spawns the server with its own CWD, which is why plans "disappear"
# after a restart if you resolve relative to os.getcwd().
_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parent


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    """One of `allowed`, or the default. A typo must not silently disarm the gate."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw not in allowed:
        # stderr, because logging is not configured yet when the config is built.
        print(
            f"[WARN] {name}={raw!r} is not one of {', '.join(allowed)}; using {default!r}",
            file=sys.stderr,
        )
        return default
    return raw


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# How long a client will let one tools/call run.
#
# The MCP TypeScript SDK's DEFAULT_REQUEST_TIMEOUT_MSEC is 60000, and that 60s is the
# de facto industry default: AnythingLLM inherits it, Claude Desktop hardcodes it with
# no way to configure it (anthropics/claude-code#22542, #43791), Cursor matches it.
# Exceeding it does not merely fail the call - the client drops the result and the
# conversation breaks mid-approval.
SDK_REQUEST_TIMEOUT_SEC = 60

# This code used to bet the whole gate on progress heartbeats: send
# notifications/progress every 20s and the client's timer resets, so a wait could run
# for the full approval_timeout. That bet was wrong twice over.
#
#   1. `resetTimeoutOnProgress` originally defaulted to FALSE in the TypeScript SDK and
#      was only later flipped to true (modelcontextprotocol/typescript-sdk#849). Any
#      client on an older bundled SDK ignores the heartbeat completely.
#   2. It is a per-request option the CLIENT passes. A server cannot set it, cannot read
#      it, and cannot detect which way it went - the only symptom is the call dying at
#      60s with a heartbeat thread still cheerfully ticking.
#
# So the heartbeat is now a bonus for clients where it happens to work, never the thing
# safety rests on. Every wait is instead cut into chunks that finish comfortably inside
# the tightest plausible client limit, and the model is told to call straight back. That
# is also where the protocol is heading - splitting one long call into several
# request/response pairs (SEP-1391 Long-Running Operations, SEP-1539 Timeout
# Coordination).
CALL_BUDGET_SEC = 45

# Retained for `trust_heartbeat` mode, which keeps the old single-call behaviour.
NO_PROGRESS_WAIT_CEILING_SEC = 55

# How the HITL wait is spent.
#   chunked         - default. Cut the wait into CALL_BUDGET_SEC slices; each slice ends
#                     in a normal response telling the model to call again immediately.
#                     Never trips a client timeout, and the conversation resumes on its
#                     own within one slice of the human clicking.
#   return          - publish the request and return at once. No tool calls are spent
#                     waiting, but the human must send a chat message after deciding
#                     before anything continues.
#   trust_heartbeat - the pre-1.14 behaviour: one long blocking call relying on progress
#                     notifications. Only for clients measured to honour them.
APPROVAL_MODE_CHUNKED = "chunked"
APPROVAL_MODE_RETURN = "return"
APPROVAL_MODE_TRUST_HEARTBEAT = "trust_heartbeat"
APPROVAL_MODES = (APPROVAL_MODE_CHUNKED, APPROVAL_MODE_RETURN, APPROVAL_MODE_TRUST_HEARTBEAT)


@dataclass
class Config:
    state_dir: Path
    log_level: str = "INFO"
    max_plans: int = 20
    max_tasks: int = 12
    autoapprove: bool = False
    blocking_approval: bool = True
    approval_mode: str = APPROVAL_MODE_CHUNKED
    # Ceiling on a SINGLE tools/call. The total wait is approval_timeout, spent across
    # as many calls as it takes.
    call_budget: int = CALL_BUDGET_SEC
    approval_port: int = 8765
    approval_timeout: int = 900
    approval_open_browser: bool = True
    approval_ttl: int = 1800
    max_active_plans: int = 5
    completion_approval: bool = True
    min_result_log: int = 8
    # Put the next task straight into IN_PROGRESS when one is reported DONE. Halves the
    # execution round trips without weakening the DONE guard - see _auto_advance.
    auto_advance: bool = True
    # SSE transport. CLI flags still win; these exist because AnythingLLM's MCP config
    # sets `env` more naturally than it sets `args`.
    sse_host: str = "127.0.0.1"
    sse_port: int = 8931

    @classmethod
    def from_env(cls, state_dir_override: str | None = None) -> "Config":
        state_dir = state_dir_override or os.environ.get("PLANNING_MCP_STATE_DIR")
        return cls(
            state_dir=Path(state_dir).expanduser().resolve()
            if state_dir
            else _PROJECT_DIR / "state",
            log_level=os.environ.get("PLANNING_MCP_LOG_LEVEL", "INFO").upper(),
            max_plans=_env_int("PLANNING_MCP_MAX_PLANS", 20),
            max_tasks=_env_int("PLANNING_MCP_MAX_TASKS", 12),
            autoapprove=_env_bool("PLANNING_MCP_AUTOAPPROVE", False),
            blocking_approval=_env_bool("PLANNING_MCP_BLOCKING_APPROVAL", True),
            approval_mode=_env_choice(
                "PLANNING_MCP_APPROVAL_MODE", APPROVAL_MODES, APPROVAL_MODE_CHUNKED
            ),
            call_budget=_env_int("PLANNING_MCP_CALL_BUDGET", CALL_BUDGET_SEC),
            approval_port=_env_int("PLANNING_MCP_APPROVAL_PORT", 8765),
            approval_timeout=_env_int("PLANNING_MCP_APPROVAL_TIMEOUT", 900),
            approval_open_browser=_env_bool("PLANNING_MCP_APPROVAL_OPEN_BROWSER", True),
            approval_ttl=_env_int("PLANNING_MCP_APPROVAL_TTL", 1800),
            max_active_plans=_env_int("PLANNING_MCP_MAX_ACTIVE_PLANS", 5),
            completion_approval=_env_bool("PLANNING_MCP_COMPLETION_APPROVAL", True),
            min_result_log=_env_int("PLANNING_MCP_MIN_RESULT_LOG", 8),
            auto_advance=_env_bool("PLANNING_MCP_AUTO_ADVANCE", True),
            sse_host=os.environ.get("PLANNING_MCP_SSE_HOST", "127.0.0.1"),
            sse_port=_env_int("PLANNING_MCP_SSE_PORT", 8931),
        )


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Log to stderr only. Under stdio transport, stdout belongs to the JSON-RPC stream."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return logging.getLogger(SERVER_NAME)
