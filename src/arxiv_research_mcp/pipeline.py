"""The fetch → filter → dedup pipeline.

This module is the stateful layer between the pure arXiv parser
(``arxiv.py``) and the MCP server wiring (``server.py``). It handles:

- Keyword pre-filtering (case-insensitive substring match).
- Date-window filtering (timezone-aware).
- Persistent dedup via a JSON cursor file (atomic writes).
- Paginated candidate collection with early-stop when a full page is
  older than the window.

All LLM work is delegated to the connecting MCP client — the pipeline
only produces candidate lists. The client calls the judge prompt, makes
the scoring decisions, and then calls ``mark_papers_surfaced`` back
through the server to update the cursor.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypedDict, cast

from arxiv_research_mcp.arxiv import (
    FetchedPaper,
    fetch_arxiv_feed,
    parse_arxiv_feed,
    parse_iso8601,
)
from arxiv_research_mcp.security import DEFAULT_LIMITS, Limits

#: arXiv API rate limit — requests to the Atom endpoint should be at
#: least 3 seconds apart per their published API terms.
ARXIV_RATE_LIMIT_S: Final[int] = 3


# ─────────────────────────────────────────────────────────────────────────
# Filters (pure)
# ─────────────────────────────────────────────────────────────────────────
def keyword_prefilter(
    entries: Iterable[FetchedPaper],
    keywords: Iterable[str],
) -> list[FetchedPaper]:
    """Return entries whose title or summary contains any keyword.

    Case-insensitive substring match on ``title + " " + summary``. An
    empty keyword iterable means "no prefiltering" — the input is
    returned unchanged (materialized as a list).

    Args:
        entries: Papers to filter.
        keywords: Keyword vocabulary.

    Returns:
        Filtered list in input order.
    """
    kw_list = [k.lower() for k in keywords if k]
    if not kw_list:
        return list(entries)
    kept: list[FetchedPaper] = []
    for entry in entries:
        haystack = f"{entry.title} {entry.summary}".lower()
        if any(k in haystack for k in kw_list):
            kept.append(entry)
    return kept


def filter_by_date_window(
    entries: Iterable[FetchedPaper],
    since: datetime,
) -> list[FetchedPaper]:
    """Keep entries whose ``published`` timestamp is ``>= since``.

    Entries with unparseable timestamps are **kept** — better to surface
    a potentially-stale paper than to silently drop one because of a
    date quirk. ``since`` must be timezone-aware.

    Args:
        entries: Papers to filter.
        since: Cutoff timestamp (timezone-aware).

    Returns:
        Filtered list in input order.
    """
    kept: list[FetchedPaper] = []
    for entry in entries:
        pub = parse_iso8601(entry.published)
        if pub is None or pub >= since:
            kept.append(entry)
    return kept


# ─────────────────────────────────────────────────────────────────────────
# Cursor — persistent dedup
# ─────────────────────────────────────────────────────────────────────────
class CursorEntry(TypedDict):
    """One entry in the dedup cursor."""

    first_surfaced: str  # ISO 8601 timestamp
    version: str  # arXiv version suffix (e.g. "v2")
    title: str  # Truncated title for human inspection


class Cursor:
    """Persistent dedup cursor backed by an atomic JSON file.

    The cursor is a mapping of ``arxiv_id → CursorEntry``. It survives
    across server restarts and is the only persistent state the package
    writes. The path is resolved once at construction from the platform
    data directory (or an explicit override) — it is **never** derived
    from tool arguments.

    Writes are atomic: we serialize to ``<path>.tmp`` and then
    ``os.replace()`` onto the final path. A crash mid-write leaves
    either the old cursor or the new cursor intact — never a partial
    file.
    """

    def __init__(self, path: Path) -> None:
        """Bind the cursor to a filesystem path.

        The path's parent directory is created on first write if it
        doesn't exist. The file itself is created on first ``save()``.
        """
        self._path = path
        self._data: dict[str, CursorEntry] = {}
        self._load()

    @property
    def path(self) -> Path:
        """Filesystem path of the underlying cursor file."""
        return self._path

    def _load(self) -> None:
        """Populate ``self._data`` from disk. Tolerant of corrupt files."""
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            self._data = {}
            return
        except (OSError, json.JSONDecodeError):
            # Corrupt or unreadable cursor is treated as empty. The
            # next save() overwrites it cleanly. This prevents a bad
            # file from wedging the server across restarts.
            self._data = {}
            return
        # We can't strictly validate the CursorEntry shape from untrusted
        # JSON without constructing every field — instead we trust the
        # file-on-disk's shape (the server is the only writer) and cast.
        # mypy needs the explicit cast here; the runtime check above
        # guarantees each value is a dict.
        if isinstance(raw, dict):
            self._data = {
                str(k): cast("CursorEntry", v) for k, v in raw.items() if isinstance(v, dict)
            }
        else:
            self._data = {}

    def save(self) -> None:
        """Atomically write the cursor to disk.

        Creates the parent directory if needed. On write failure, the
        error is printed to stderr (via ``print()``) but not raised —
        the in-memory cursor stays consistent and the next tool call
        can retry.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as e:  # pragma: no cover — os dependent
            # Best-effort cleanup of the temp file.
            with contextlib.suppress(OSError):
                tmp.unlink()
            print(f"[arxiv-research-mcp] cursor save failed: {e}")

    def contains(self, arxiv_id: str) -> bool:
        """Return True if ``arxiv_id`` has been marked as surfaced."""
        return arxiv_id in self._data

    def mark(
        self,
        papers: Iterable[FetchedPaper],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Add papers to the cursor.

        Args:
            papers: Papers to record.
            now: Timestamp to record (defaults to ``datetime.now(UTC)``).

        Returns:
            List of arxiv_ids actually added (excludes any that were
            already in the cursor).
        """
        timestamp = (now or datetime.now(UTC)).isoformat()
        added: list[str] = []
        for paper in papers:
            if not paper.arxiv_id:
                continue
            if paper.arxiv_id in self._data:
                continue
            self._data[paper.arxiv_id] = CursorEntry(
                first_surfaced=timestamp,
                version=paper.version,
                title=paper.title[:200],
            )
            added.append(paper.arxiv_id)
        return added

    def mark_ids(
        self,
        arxiv_ids: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Add a list of IDs to the cursor without paper metadata.

        Used when the client already has the IDs in hand but not the
        full paper records — e.g. after the client's judge has scored
        the batch and wants to commit the surfaced set back.

        Args:
            arxiv_ids: IDs to record.
            now: Timestamp to record (defaults to ``datetime.now(UTC)``).

        Returns:
            List of IDs actually added.
        """
        timestamp = (now or datetime.now(UTC)).isoformat()
        added: list[str] = []
        for aid in arxiv_ids:
            if not aid or aid in self._data:
                continue
            self._data[aid] = CursorEntry(
                first_surfaced=timestamp,
                version="",
                title="",
            )
            added.append(aid)
        return added

    def unmark(self, arxiv_ids: Iterable[str]) -> list[str]:
        """Remove IDs from the cursor.

        Args:
            arxiv_ids: IDs to remove.

        Returns:
            List of IDs actually removed (excludes any that weren't
            present).
        """
        removed: list[str] = []
        for aid in arxiv_ids:
            if aid in self._data:
                del self._data[aid]
                removed.append(aid)
        return removed

    def clear(self) -> int:
        """Wipe the cursor. Returns the number of entries removed."""
        n = len(self._data)
        self._data = {}
        return n

    def filter_unseen(
        self,
        entries: Iterable[FetchedPaper],
    ) -> list[FetchedPaper]:
        """Return only entries whose arxiv_id is not in the cursor."""
        return [e for e in entries if e.arxiv_id not in self._data]

    def snapshot(self) -> dict[str, CursorEntry]:
        """Return a shallow copy of the cursor data.

        Callers can safely read this without risking mutation of the
        cursor's internal state.
        """
        return dict(self._data)

    def __len__(self) -> int:
        """Number of cursored entries."""
        return len(self._data)


# ─────────────────────────────────────────────────────────────────────────
# Verdict cache — LLM judge result memoization
# ─────────────────────────────────────────────────────────────────────────
class VerdictEntry(TypedDict):
    """One cached verdict from the client's LLM judge."""

    judged_at: str  # ISO 8601 timestamp
    arxiv_id: str
    relevance_score: int
    quality_score: int
    summary: str
    project_angle: str
    reasoning: str


def compute_rubric_hash(rubric_focus: str) -> str:
    """Stable hash of the operator's rubric focus string.

    The cache auto-invalidates when ``rubric_focus`` changes in config.
    Uses SHA-256 truncated to 16 hex chars (64 bits of collision
    resistance — more than enough for a single-user cache).

    Args:
        rubric_focus: The operator's free-text focus description.

    Returns:
        16-character hex digest.
    """
    return hashlib.sha256(rubric_focus.encode("utf-8")).hexdigest()[:16]


class VerdictCache:
    """Persistent verdict cache backed by an atomic JSON file.

    Stores LLM judge verdicts (both surfaced and rejected) so repeat
    digest runs within the TTL window skip re-judging known papers.
    Separate from the dedup ``Cursor``: the cursor tracks "have I
    surfaced this paper?", the cache tracks "what did the judge say?".

    The cache auto-invalidates when the operator's ``rubric_focus``
    changes (detected via ``rubric_hash``). Entries older than
    ``ttl_days`` are pruned on load.

    File shape::

        {
          "rubric_hash": "abc123...",
          "verdicts": {
            "<arxiv_id>": VerdictEntry
          }
        }
    """

    def __init__(
        self,
        path: Path,
        rubric_hash: str,
        *,
        ttl_days: int = 7,
    ) -> None:
        """Bind the cache to a filesystem path.

        Loads existing data, checks the rubric hash (invalidates on
        mismatch), and prunes expired entries.

        Args:
            path: Filesystem path for the JSON file.
            rubric_hash: Hash of the current rubric focus.
            ttl_days: Entries older than this are pruned.
        """
        self._path = path
        self._rubric_hash = rubric_hash
        self._ttl_days = ttl_days
        self._data: dict[str, VerdictEntry] = {}
        self._load()

    @property
    def path(self) -> Path:
        """Filesystem path of the underlying cache file."""
        return self._path

    @property
    def rubric_hash(self) -> str:
        """Currently active rubric hash."""
        return self._rubric_hash

    def _load(self) -> None:
        """Populate ``self._data`` from disk.

        Tolerant of corrupt files. Invalidates if the stored rubric
        hash differs from the current one. Prunes expired entries.
        """
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            self._data = {}
            return
        except (OSError, json.JSONDecodeError):
            self._data = {}
            return

        if not isinstance(raw, dict):
            self._data = {}
            return

        # Rubric changed → invalidate entire cache
        if raw.get("rubric_hash") != self._rubric_hash:
            self._data = {}
            return

        verdicts = raw.get("verdicts", {})
        if isinstance(verdicts, dict):
            self._data = {
                str(k): cast("VerdictEntry", v) for k, v in verdicts.items() if isinstance(v, dict)
            }
        else:
            self._data = {}

        self._prune()

    def _prune(self) -> None:
        """Remove entries older than ``_ttl_days``."""
        now = datetime.now(UTC)
        expired: list[str] = []
        for arxiv_id, entry in self._data.items():
            judged_at = parse_iso8601(entry.get("judged_at", ""))
            if judged_at is not None:
                age_days = (now - judged_at).total_seconds() / 86400
                if age_days > self._ttl_days:
                    expired.append(arxiv_id)
        for aid in expired:
            del self._data[aid]

    def save(self) -> None:
        """Atomically write the cache to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload: dict[str, Any] = {
            "rubric_hash": self._rubric_hash,
            "verdicts": self._data,
        }
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as e:  # pragma: no cover — os dependent
            with contextlib.suppress(OSError):
                tmp.unlink()
            print(f"[arxiv-research-mcp] verdict cache save failed: {e}")

    def lookup(self, arxiv_id: str) -> VerdictEntry | None:
        """Return the cached verdict for ``arxiv_id``, or ``None``."""
        return self._data.get(arxiv_id)

    def split_cached_vs_new(
        self,
        papers: Iterable[FetchedPaper],
    ) -> tuple[list[VerdictEntry], list[FetchedPaper]]:
        """Split papers into cache hits and net-new.

        Args:
            papers: Candidate papers to check.

        Returns:
            ``(cached_verdicts, net_new_papers)``.
        """
        cached: list[VerdictEntry] = []
        net_new: list[FetchedPaper] = []
        for paper in papers:
            entry = self._data.get(paper.arxiv_id)
            if entry is not None:
                cached.append(entry)
            else:
                net_new.append(paper)
        return cached, net_new

    def store(
        self,
        verdicts: Iterable[dict[str, object]],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Add verdicts to the cache.

        Overwrites existing entries for the same ``arxiv_id`` — this is
        intentional so ``!research --fresh`` can refresh stale scores.

        Args:
            verdicts: Verdict dicts (must contain ``arxiv_id``).
            now: Timestamp to record (defaults to ``datetime.now(UTC)``).

        Returns:
            List of arxiv_ids actually stored.
        """
        timestamp = (now or datetime.now(UTC)).isoformat()
        stored: list[str] = []
        for v in verdicts:
            aid = v.get("arxiv_id")
            if not isinstance(aid, str) or not aid:
                continue
            rel_raw = v.get("relevance_score", 0)
            qual_raw = v.get("quality_score", 0)
            self._data[aid] = VerdictEntry(
                judged_at=timestamp,
                arxiv_id=aid,
                relevance_score=int(rel_raw) if isinstance(rel_raw, (int, float)) else 0,
                quality_score=int(qual_raw) if isinstance(qual_raw, (int, float)) else 0,
                summary=str(v.get("summary", ""))[:1000],
                project_angle=str(v.get("project_angle", ""))[:1000],
                reasoning=str(v.get("reasoning", ""))[:500],
            )
            stored.append(aid)
        return stored

    def clear(self) -> int:
        """Wipe the cache. Returns the number of entries removed."""
        n = len(self._data)
        self._data = {}
        return n

    def snapshot(self) -> dict[str, VerdictEntry]:
        """Return a shallow copy of the cache data."""
        return dict(self._data)

    def __len__(self) -> int:
        """Number of cached verdicts."""
        return len(self._data)

    def contains(self, arxiv_id: str) -> bool:
        """Return True if ``arxiv_id`` has a cached verdict."""
        return arxiv_id in self._data


# ─────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────
def collect_candidate_papers(
    categories: list[str],
    keywords: list[str],
    since: datetime,
    *,
    page_size: int = 200,
    max_pages: int = 5,
    limits: Limits = DEFAULT_LIMITS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[FetchedPaper]:
    """Paginated fetch → parse → date-window → keyword prefilter.

    Walks arXiv pages newest-first and stops early once a full page is
    older than ``since`` — avoids pulling the full category history
    when the lookback window is small.

    Applies the 3-second rate limit between pages via ``sleep_fn``
    (injected so tests can pass a no-op).

    Network failures log to stderr and return whatever was collected
    before the failure (possibly ``[]``). This is a deliberate choice:
    a partial result is more useful than a hard error to the MCP client,
    and the client can see the shorter list and retry.

    Args:
        categories: arXiv categories to query.
        keywords: Prefilter vocabulary (empty list = no prefilter).
        since: Date-window cutoff (timezone-aware).
        page_size: arXiv API page size.
        max_pages: Maximum pages to fetch per call.
        limits: Effective limits.
        sleep_fn: Injected sleep function for rate limiting.

    Returns:
        Candidate papers (post-prefilter), newest-first.
    """
    collected: list[FetchedPaper] = []

    for page_num in range(max_pages):
        start_offset = page_num * page_size
        try:
            xml_text = fetch_arxiv_feed(
                categories,
                start=start_offset,
                max_results=page_size,
                limits=limits,
            )
        except Exception as e:
            print(f"[arxiv-research-mcp] arXiv fetch page {page_num} failed: {e}")
            break

        page_entries = parse_arxiv_feed(xml_text)
        if not page_entries:
            # Empty page — either end of results or a parse anomaly.
            break

        # Keep only entries inside the window.
        in_window = filter_by_date_window(page_entries, since=since)
        collected.extend(in_window)

        # Early-stop: if the oldest entry on this page is older than
        # `since`, subsequent pages will also be out of window.
        oldest_ts = parse_iso8601(page_entries[-1].published)
        if oldest_ts is not None and oldest_ts < since:
            break

        # Also stop if the page is not full — we've reached the end.
        if len(page_entries) < page_size:
            break

        if page_num < max_pages - 1:
            sleep_fn(ARXIV_RATE_LIMIT_S)

    return keyword_prefilter(collected, keywords)
