"""CLI entry point: ``arxiv-research-mcp``.

Invoked via ``python -m arxiv_research_mcp`` or via the console script
declared in ``pyproject.toml`` (``arxiv-research-mcp``). Both paths land
in ``main()`` which defers to ``server.run_server()``.

Why this separation? The ``__main__.py`` module is tiny and import-free
beyond the one call, so tests can import ``server.py`` in isolation
without starting the MCP transport.
"""

from __future__ import annotations

from arxiv_research_mcp.server import run_server


def main() -> None:
    """Start the FastMCP server on stdio.

    This is the ``[project.scripts]`` entry point. Returns normally
    when the client disconnects; returns non-zero via ``SystemExit``
    on unrecoverable errors.
    """
    run_server()


if __name__ == "__main__":
    main()
