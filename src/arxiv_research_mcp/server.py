"""FastMCP server wiring: tools, prompts, resources.

This module constructs the ``FastMCP`` instance and decorates every
tool / prompt / resource handler. It is imported both by the CLI entry
point (``__main__.py``) and by tests that want to poke the tool
functions directly.

Module-level state:

- ``_APP_CONFIG`` and ``_APP_CURSOR`` hold the active config and cursor
  singleton. They are initialized by ``_ensure_initialized()`` on first
  tool call, which reads the config file from disk. This indirection
  lets tests stub the config via ``set_runtime_state()`` before
  exercising the tools, without monkeypatching the module.

Security posture:

- Every tool handler validates its arguments via ``security.py``
  before touching the cursor or hitting the network.
- Errors are caught at the tool boundary and returned as structured
  ``{"ok": False, "error": str}`` payloads instead of raising Python
  exceptions up through the MCP transport.
- The cursor path is never derived from tool arguments.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from arxiv_research_mcp.arxiv import FetchedPaper
from arxiv_research_mcp.config import Config, load_config
from arxiv_research_mcp.pipeline import (
    Cursor,
    VerdictCache,
    collect_candidate_papers,
    compute_rubric_hash,
)
from arxiv_research_mcp.prompts import (
    render_research_judge_rubric,
    render_weekly_digest_workflow,
)
from arxiv_research_mcp.security import (
    ValidationError,
    validate_arxiv_id_list,
    validate_category_list,
    validate_keyword_list,
    validate_positive_bounded_int,
    validate_verdict_list,
    validate_window_days,
)

#: FastMCP server instance. The decorators below bind to this.
mcp: FastMCP = FastMCP("arxiv-research-mcp")


# ─────────────────────────────────────────────────────────────────────────
# Runtime state (lazy-initialized; overridable for tests)
# ─────────────────────────────────────────────────────────────────────────
_APP_CONFIG: Config | None = None
_APP_CURSOR: Cursor | None = None
_APP_VERDICT_CACHE: VerdictCache | None = None


def _ensure_initialized() -> tuple[Config, Cursor]:
    """Lazy-init the module-level config + cursor + verdict cache singletons.

    Called at the top of every tool / resource handler. Tests can
    pre-seed via ``set_runtime_state()`` to avoid touching the real
    filesystem.

    The ``global`` statement is intentional: FastMCP decorators bind at
    import time, so tool handlers have no instance to carry state on.
    The singleton pattern is the standard solution for this and the
    PLW0603 warning is suppressed here and in the other state
    accessors.

    Returns:
        ``(config, cursor)`` — both non-None after this call.
    """
    global _APP_CONFIG, _APP_CURSOR, _APP_VERDICT_CACHE  # noqa: PLW0603 — singleton pattern
    if _APP_CONFIG is None or _APP_CURSOR is None:
        cfg = load_config()
        cursor = Cursor(cfg.cursor_path)
        rubric_hash = compute_rubric_hash(cfg.topic.rubric_focus)
        vcache = VerdictCache(
            cfg.verdict_cache_path,
            rubric_hash,
            ttl_days=cfg.limits.verdict_cache_ttl_days,
        )
        _APP_CONFIG = cfg
        _APP_CURSOR = cursor
        _APP_VERDICT_CACHE = vcache
    return _APP_CONFIG, _APP_CURSOR


def _ensure_verdict_cache() -> VerdictCache:
    """Return the verdict cache singleton, initializing if needed."""
    _ensure_initialized()
    assert _APP_VERDICT_CACHE is not None  # noqa: S101 — post-init invariant
    return _APP_VERDICT_CACHE


def set_runtime_state(
    config: Config,
    cursor: Cursor,
    verdict_cache: VerdictCache | None = None,
) -> None:
    """Override the module-level config + cursor + verdict cache singletons.

    Used by tests (and potentially by library users who want to embed
    the server with a custom config). Passing test fixtures here lets
    the tool handlers be exercised without any disk I/O.

    Args:
        config: Test config.
        cursor: Test cursor.
        verdict_cache: Test verdict cache. If ``None``, a default cache
            is created using the config's verdict_cache_path.
    """
    global _APP_CONFIG, _APP_CURSOR, _APP_VERDICT_CACHE  # noqa: PLW0603 — singleton pattern
    _APP_CONFIG = config
    _APP_CURSOR = cursor
    if verdict_cache is not None:
        _APP_VERDICT_CACHE = verdict_cache
    else:
        rubric_hash = compute_rubric_hash(config.topic.rubric_focus)
        _APP_VERDICT_CACHE = VerdictCache(
            config.verdict_cache_path,
            rubric_hash,
            ttl_days=config.limits.verdict_cache_ttl_days,
        )


def reset_runtime_state() -> None:
    """Clear the module-level singletons.

    Next call to ``_ensure_initialized()`` will reload from disk.
    Used in test teardown so state doesn't leak between cases.
    """
    global _APP_CONFIG, _APP_CURSOR, _APP_VERDICT_CACHE  # noqa: PLW0603 — singleton pattern
    _APP_CONFIG = None
    _APP_CURSOR = None
    _APP_VERDICT_CACHE = None


# ─────────────────────────────────────────────────────────────────────────
# Error shaping
# ─────────────────────────────────────────────────────────────────────────
def _error(msg: str) -> dict[str, Any]:
    """Build a standard error response for tool handlers."""
    return {"ok": False, "error": msg}


def _ok(**kwargs: Any) -> dict[str, Any]:
    """Build a standard success response for tool handlers."""
    payload: dict[str, Any] = {"ok": True}
    payload.update(kwargs)
    return payload


# ─────────────────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────────────────
@mcp.tool()
def fetch_candidate_papers(
    window_days: int = 7,
    categories: list[str] | None = None,
    keywords: list[str] | None = None,
    dedup: bool = True,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Fetch arXiv candidate papers for the given window.

    Runs the full paginated fetch → parse → date-window → keyword
    prefilter pipeline. When ``dedup=True`` (default) also drops any
    papers already in the cursor.

    When ``use_cache=True`` (default), candidates with existing verdict
    cache entries are split out and returned as ``cached_verdicts`` —
    the client's LLM only needs to judge the remaining ``candidates``.

    Args:
        window_days: Lookback window in days. Must be ``1 <= N <= max_window_days``.
        categories: arXiv category codes to query. If ``None``, uses
            the configured topic's categories.
        keywords: Keyword prefilter vocabulary. If ``None``, uses the
            configured topic's keywords. Empty list disables prefiltering.
        dedup: If ``True``, filter out papers already in the cursor.
        use_cache: If ``True``, return cached verdicts for previously
            judged papers instead of including them in ``candidates``.
            Set to ``False`` for a fresh re-judge of everything.

    Returns:
        ``{"ok": True, "candidates": [paper_dict, ...], "total": N,
           "after_dedup": M, "cache_hits": K,
           "cached_verdicts": [verdict_dict, ...],
           "window_days": X, "categories": [...]}``
        on success, or ``{"ok": False, "error": str}`` on failure.
    """
    try:
        config, cursor = _ensure_initialized()
        vcache = _ensure_verdict_cache()
        limits = config.limits

        validated_days = validate_window_days(window_days, limits=limits)

        if categories is None:
            validated_cats = list(config.topic.categories)
        else:
            validated_cats = validate_category_list(categories, limits=limits)

        if keywords is None:
            validated_kws = list(config.topic.keywords)
        else:
            validated_kws = validate_keyword_list(keywords, limits=limits)

        since = datetime.now(UTC) - timedelta(days=validated_days)

        all_candidates: list[FetchedPaper] = collect_candidate_papers(
            validated_cats,
            validated_kws,
            since,
            page_size=limits.max_results_per_page,
            max_pages=limits.max_pages_per_fetch,
            limits=limits,
        )

        total = len(all_candidates)
        if dedup:
            all_candidates = cursor.filter_unseen(all_candidates)

        after_dedup = len(all_candidates)

        # Split into cached verdicts vs. net-new candidates
        cached_verdicts: list[dict[str, Any]] = []
        candidates_out: list[FetchedPaper] = all_candidates
        if use_cache and all_candidates:
            cached_entries, net_new = vcache.split_cached_vs_new(all_candidates)
            cached_verdicts = [dict(e) for e in cached_entries]
            candidates_out = net_new

        return _ok(
            candidates=[p.asdict() for p in candidates_out],
            total=total,
            after_dedup=after_dedup,
            cache_hits=len(cached_verdicts),
            cached_verdicts=cached_verdicts,
            window_days=validated_days,
            categories=validated_cats,
            keywords_applied=len(validated_kws),
        )
    except ValidationError as e:
        return _error(f"invalid argument: {e}")
    except Exception as e:
        return _error(f"fetch failed: {e}")


@mcp.tool()
def mark_papers_surfaced(arxiv_ids: list[str]) -> dict[str, Any]:
    """Add arXiv IDs to the dedup cursor.

    Call this after the client's LLM has judged a batch and decided
    which papers to surface to the user. Subsequent
    ``fetch_candidate_papers(dedup=True)`` calls will exclude these IDs.

    Args:
        arxiv_ids: IDs to mark.

    Returns:
        ``{"ok": True, "added": [id, ...], "cursor_size": N}`` on success.
    """
    try:
        _, cursor = _ensure_initialized()
        _, limits = _config_and_limits()
        validated = validate_arxiv_id_list(arxiv_ids, limits=limits)
        added = cursor.mark_ids(validated)
        cursor.save()
        return _ok(added=added, cursor_size=len(cursor))
    except ValidationError as e:
        return _error(f"invalid argument: {e}")
    except Exception as e:
        return _error(f"mark failed: {e}")


@mcp.tool()
def unmark_papers(arxiv_ids: list[str]) -> dict[str, Any]:
    """Remove arXiv IDs from the dedup cursor.

    Useful when the operator wants to re-evaluate a paper after tuning
    the rubric, or to undo an accidental ``mark_papers_surfaced`` call.

    Args:
        arxiv_ids: IDs to remove.

    Returns:
        ``{"ok": True, "removed": [id, ...], "cursor_size": N}``.
    """
    try:
        _, cursor = _ensure_initialized()
        _, limits = _config_and_limits()
        validated = validate_arxiv_id_list(arxiv_ids, limits=limits)
        removed = cursor.unmark(validated)
        cursor.save()
        return _ok(removed=removed, cursor_size=len(cursor))
    except ValidationError as e:
        return _error(f"invalid argument: {e}")
    except Exception as e:
        return _error(f"unmark failed: {e}")


@mcp.tool()
def get_cursor_state(limit: int = 100) -> dict[str, Any]:
    """Return the current dedup cursor contents.

    Args:
        limit: Maximum number of entries to include in the response.
            Must be ``1 <= limit <= 10000``. Defaults to 100.

    Returns:
        ``{"ok": True, "total": N, "entries": {id: entry, ...}}``.
        The entries dict is capped at ``limit`` — use repeated calls
        or a higher limit for exhaustive enumeration.
    """
    try:
        _, cursor = _ensure_initialized()
        validated_limit = validate_positive_bounded_int(limit, name="limit", maximum=10_000)
        snapshot = cursor.snapshot()
        total = len(snapshot)
        # Sort by arxiv_id for deterministic output, then slice.
        keys_sorted = sorted(snapshot.keys())[:validated_limit]
        entries = {k: snapshot[k] for k in keys_sorted}
        return _ok(total=total, entries=entries, returned=len(entries))
    except ValidationError as e:
        return _error(f"invalid argument: {e}")
    except Exception as e:
        return _error(f"get_cursor failed: {e}")


@mcp.tool()
def clear_cursor(confirm: bool = False) -> dict[str, Any]:
    """Wipe the dedup cursor (destructive operation).

    This is a footgun, so the ``confirm=True`` flag is required — a bare
    call without ``confirm`` is a no-op that returns an error. After
    wiping, previously-surfaced papers will re-appear in the next
    ``fetch_candidate_papers`` call.

    Args:
        confirm: Must be ``True`` to actually wipe the cursor.

    Returns:
        ``{"ok": True, "removed": N}`` on success, or an error
        response if ``confirm`` was not set.
    """
    try:
        _, cursor = _ensure_initialized()
        if not confirm:
            return _error(
                "clear_cursor requires confirm=True; "
                "refusing to wipe cursor without explicit confirmation"
            )
        removed = cursor.clear()
        cursor.save()
        return _ok(removed=removed)
    except Exception as e:
        return _error(f"clear_cursor failed: {e}")


# ─────────────────────────────────────────────────────────────────────────
# Verdict cache tools
# ─────────────────────────────────────────────────────────────────────────
@mcp.tool()
def submit_verdicts(verdicts_json: str) -> dict[str, Any]:
    """Submit LLM judge verdicts for caching.

    Call this after the client's LLM has judged a batch of papers.
    Both surfaced AND rejected papers should be submitted — this
    prevents re-judging rejected papers on the next run within the
    cache TTL window.

    The cache auto-invalidates when the operator's ``rubric_focus``
    changes in config. Entries expire after the configured TTL
    (default 7 days).

    Args:
        verdicts_json: JSON array of verdict objects. Each must have
            ``arxiv_id`` (valid arXiv ID), ``relevance_score`` (int 0-10),
            ``quality_score`` (int 0-10). Optional: ``summary``,
            ``project_angle``, ``reasoning`` (strings, truncated to
            safe lengths).

    Returns:
        ``{"ok": True, "stored": [id, ...], "cache_size": N}`` on
        success.
    """
    try:
        _, _ = _ensure_initialized()
        vcache = _ensure_verdict_cache()
        _, limits = _config_and_limits()

        try:
            parsed = json.loads(verdicts_json)
        except (json.JSONDecodeError, TypeError) as e:
            return _error(f"invalid JSON: {e}")

        if not isinstance(parsed, list):
            return _error("verdicts_json must be a JSON array")

        validated = validate_verdict_list(parsed, limits=limits)
        stored = vcache.store(validated)
        vcache.save()
        return _ok(stored=stored, cache_size=len(vcache))
    except ValidationError as e:
        return _error(f"invalid argument: {e}")
    except Exception as e:
        return _error(f"submit_verdicts failed: {e}")


@mcp.tool()
def get_cached_verdicts(limit: int = 100) -> dict[str, Any]:
    """Return the current verdict cache contents.

    Args:
        limit: Maximum number of entries to include. Must be
            ``1 <= limit <= 10000``. Defaults to 100.

    Returns:
        ``{"ok": True, "total": N, "entries": {id: entry, ...},
          "rubric_hash": str}``.
    """
    try:
        _, _ = _ensure_initialized()
        vcache = _ensure_verdict_cache()
        validated_limit = validate_positive_bounded_int(
            limit, name="limit", maximum=10_000,
        )
        snapshot = vcache.snapshot()
        total = len(snapshot)
        keys_sorted = sorted(snapshot.keys())[:validated_limit]
        entries = {k: dict(snapshot[k]) for k in keys_sorted}
        return _ok(
            total=total,
            entries=entries,
            returned=len(entries),
            rubric_hash=vcache.rubric_hash,
        )
    except ValidationError as e:
        return _error(f"invalid argument: {e}")
    except Exception as e:
        return _error(f"get_cached_verdicts failed: {e}")


@mcp.tool()
def clear_verdict_cache(confirm: bool = False) -> dict[str, Any]:
    """Wipe the verdict cache (destructive operation).

    Requires ``confirm=True`` — a bare call is a no-op that returns
    an error. After wiping, all papers will be re-judged on the next
    ``fetch_candidate_papers`` call.

    Args:
        confirm: Must be ``True`` to actually wipe the cache.

    Returns:
        ``{"ok": True, "removed": N}`` on success.
    """
    try:
        _, _ = _ensure_initialized()
        vcache = _ensure_verdict_cache()
        if not confirm:
            return _error(
                "clear_verdict_cache requires confirm=True; "
                "refusing to wipe cache without explicit confirmation"
            )
        removed = vcache.clear()
        vcache.save()
        return _ok(removed=removed)
    except Exception as e:
        return _error(f"clear_verdict_cache failed: {e}")


# ─────────────────────────────────────────────────────────────────────────
# Prompts
# ─────────────────────────────────────────────────────────────────────────
@mcp.prompt()
def research_judge_rubric(papers_json: str = "") -> str:
    """Skeptical two-axis reviewer template (relevance + quality).

    The rubric includes the operator's configured ``rubric_focus``
    verbatim so the connected LLM knows what the operator cares about.

    Args:
        papers_json: Optional JSON array of paper dicts to embed in the
            prompt. If empty, the prompt ends with the rubric and
            expects the client to append papers in the same
            conversation.

    Returns:
        The fully-rendered prompt text.
    """
    config, _ = _ensure_initialized()
    papers_list: list[dict[str, Any]] | None = None
    if papers_json.strip():
        try:
            parsed = json.loads(papers_json)
            if isinstance(parsed, list):
                papers_list = [p for p in parsed if isinstance(p, dict)]
        except json.JSONDecodeError:
            papers_list = None
    return render_research_judge_rubric(
        rubric_focus=config.topic.rubric_focus,
        papers=papers_list,
    )


@mcp.prompt()
def weekly_digest_workflow(
    window_days: int = 7,
    max_surfaced: int = 7,
    cadence: str = "week",
) -> str:
    """Orchestration template for a full weekly research digest run.

    Walks the client's LLM through fetch → judge → rank → surface →
    mark. Uses the research judge rubric internally. Honest about weak
    weeks — the template instructs the LLM to report "no papers cleared
    the bar" rather than lowering the threshold.

    Args:
        window_days: Lookback window.
        max_surfaced: Hard cap on the surfaced set.
        cadence: Human-readable cadence word (for the summary line).
    """
    return render_weekly_digest_workflow(
        window_days=window_days,
        max_surfaced=max_surfaced,
        cadence=cadence,
    )


# ─────────────────────────────────────────────────────────────────────────
# Resources
# ─────────────────────────────────────────────────────────────────────────
@mcp.resource("cursor://state")
def cursor_state_resource() -> str:
    """Expose the dedup cursor as a readable JSON resource.

    Returns the full cursor contents serialized as pretty-printed JSON.
    Useful for clients that want to inspect or audit what's been
    surfaced without going through a tool call.
    """
    _, cursor = _ensure_initialized()
    return json.dumps(
        {
            "path": str(cursor.path),
            "total": len(cursor),
            "entries": cursor.snapshot(),
        },
        indent=2,
        sort_keys=True,
    )


@mcp.resource("verdict-cache://state")
def verdict_cache_state_resource() -> str:
    """Expose the verdict cache as a readable JSON resource.

    Returns the cache contents including rubric hash, total entries,
    and all cached verdicts. Useful for auditing what the judge has
    scored without going through a tool call.
    """
    _, _ = _ensure_initialized()
    vcache = _ensure_verdict_cache()
    return json.dumps(
        {
            "path": str(vcache.path),
            "total": len(vcache),
            "rubric_hash": vcache.rubric_hash,
            "entries": {k: dict(v) for k, v in vcache.snapshot().items()},
        },
        indent=2,
        sort_keys=True,
    )


@mcp.resource("config://active")
def active_config_resource() -> str:
    """Expose the currently-active configuration as a JSON resource.

    Shows the resolved topic, limits, and the config path the server
    loaded from (or ``null`` if using defaults). No secrets — there
    are none to redact; the server has no auth tokens, no API keys.
    """
    config, _ = _ensure_initialized()
    payload = {
        "config_path": (str(config.config_path) if config.config_path else None),
        "data_dir": str(config.data_dir),
        "topic": {
            "name": config.topic.name,
            "description": config.topic.description,
            "categories": list(config.topic.categories),
            "keywords": list(config.topic.keywords),
            "rubric_focus": config.topic.rubric_focus,
        },
        "limits": {
            "max_window_days": config.limits.max_window_days,
            "max_results_per_page": config.limits.max_results_per_page,
            "max_pages_per_fetch": config.limits.max_pages_per_fetch,
            "max_keywords": config.limits.max_keywords,
            "max_keyword_length": config.limits.max_keyword_length,
            "max_categories": config.limits.max_categories,
            "max_arxiv_ids_per_call": config.limits.max_arxiv_ids_per_call,
            "max_rubric_focus_chars": config.limits.max_rubric_focus_chars,
            "max_verdicts_per_call": config.limits.max_verdicts_per_call,
            "verdict_cache_ttl_days": config.limits.verdict_cache_ttl_days,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


# ─────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────
def _config_and_limits() -> tuple[Config, Any]:
    """Return ``(config, config.limits)`` — convenience for tool handlers.

    Provided as a helper so multiple tool handlers can pull limits in
    one expression without re-declaring the unpacking each time.
    """
    config, _ = _ensure_initialized()
    return config, config.limits


# ─────────────────────────────────────────────────────────────────────────
# Run helper
# ─────────────────────────────────────────────────────────────────────────
def run_server() -> None:
    """Run the FastMCP server on stdio transport.

    This is the programmatic equivalent of the ``arxiv-research-mcp``
    CLI. ``FastMCP.run()`` defaults to stdio and handles the event loop
    internally — no asyncio wrapping required at this layer.
    """
    mcp.run()
