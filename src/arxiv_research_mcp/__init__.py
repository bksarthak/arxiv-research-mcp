"""arxiv-research-mcp — an MCP server for building arXiv research digests.

Public API surface:

- `__version__`: the package version string
- `Config`, `Topic`, `Limits`: config dataclasses
- `FetchedPaper`, `Cursor`: core data types
- `run_server()`: programmatic entry point (equivalent to the CLI)

Most users don't import this directly — they run the `arxiv-research-mcp`
CLI and connect from an MCP client.
"""

from arxiv_research_mcp.arxiv import FetchedPaper
from arxiv_research_mcp.config import Config, Topic
from arxiv_research_mcp.pipeline import Cursor, VerdictCache
from arxiv_research_mcp.security import Limits
from arxiv_research_mcp.server import run_server

__version__ = "0.1.0"

__all__ = [
    "Config",
    "Cursor",
    "FetchedPaper",
    "Limits",
    "Topic",
    "VerdictCache",
    "__version__",
    "run_server",
]
