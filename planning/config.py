"""Runtime configuration. All settings are optional with safe defaults."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "planning-mcp"
SERVER_VERSION = "1.2.0"

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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    state_dir: Path
    log_level: str = "INFO"
    max_plans: int = 20
    max_tasks: int = 12
    autoapprove: bool = False

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
        )


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Log to stderr only. Under stdio transport, stdout belongs to the JSON-RPC stream."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return logging.getLogger(SERVER_NAME)
