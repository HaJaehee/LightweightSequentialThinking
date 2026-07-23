"""planning-mcp: a lightweight Planning & Task Management MCP server.

Combines Sequential Thinking and Task Tracking into one server with four tools, plus a
Human-In-The-Loop approval gate, for use with AnythingLLM Agent Mode against a weak,
air-gapped corporate LLM.
"""

from .config import SERVER_NAME, SERVER_VERSION, Config

__all__ = ["Config", "SERVER_NAME", "SERVER_VERSION"]
__version__ = SERVER_VERSION
