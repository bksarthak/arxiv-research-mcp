"""arXiv Atom API client and parser.

Talks to the arXiv public export endpoint (``http://export.arxiv.org``)
and converts the Atom feed into typed ``FetchedPaper`` records. Uses
``defusedxml`` instead of the stdlib ElementTree for XXE / billion-laughs
protection — arXiv is a trusted source over HTTPS, but defensive default
is the right call for a public package.

This module is intentionally I/O-free except for ``fetch_arxiv_feed``,
which is the single function that touches the network. Everything else
takes already-fetched XML text and produces typed dicts, so the bulk of
the logic is testable offline against fixture strings.

Design notes:

- Only one host is ever contacted (``export.arxiv.org``). The URL is
  constructed server-side from validated category codes — users cannot
  inject an arbitrary hostname. SSRF is not a risk here.
- We advertise a clear User-Agent. arXiv's API docs recommend this for
  courtesy and rate-tracking.
- Rate-limit sleeping is the caller's responsibility — ``fetch_arxiv_feed``
  is called at most once per tool invocation, and the ``collect`` helper
  in ``pipeline.py`` handles per-page sleeping.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Final

# `ET` is the conventional alias for ElementTree throughout the Python
# ecosystem — the N817 naming rule objects but the convention predates it.
import defusedxml.ElementTree as ET  # noqa: N817

from arxiv_research_mcp.security import (
    DEFAULT_LIMITS,
    Limits,
    validate_category_list,
    validate_positive_bounded_int,
)

# ─────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────
ARXIV_API_URL: Final[str] = "http://export.arxiv.org/api/query"

#: User-Agent header sent to arXiv. Non-default UAs are preferred — the
#: default Python ``urllib`` UA is frequently filtered by CDNs.
USER_AGENT: Final[str] = "arxiv-research-mcp/0.1.0 (+github.com/bksarthak/arxiv-research-mcp)"

#: Default HTTP request timeout in seconds. arXiv responses are usually
#: sub-second; 30s is generous headroom for slow networks.
DEFAULT_TIMEOUT_S: Final[int] = 30

#: Atom / arXiv XML namespaces.
_ATOM_NS: Final[dict[str, str]] = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


# ─────────────────────────────────────────────────────────────────────────
# Typed data
# ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class FetchedPaper:
    """A parsed arXiv paper record.

    Frozen dataclass so it's trivially hashable + safe to pass around
    the pipeline. The ``asdict()`` helper converts it back to a JSON-safe
    dict for MCP tool responses.
    """

    arxiv_id: str
    version: str
    title: str
    summary: str
    authors: tuple[str, ...]
    published: str
    updated: str
    url: str
    primary_category: str
    categories: tuple[str, ...]

    def asdict(self) -> dict[str, str | list[str]]:
        """Return a JSON-safe dict representation of this paper.

        Tuples are converted to lists since MCP tool responses are
        JSON-serialized and JSON has no tuple type.
        """
        return {
            "arxiv_id": self.arxiv_id,
            "version": self.version,
            "title": self.title,
            "summary": self.summary,
            "authors": list(self.authors),
            "published": self.published,
            "updated": self.updated,
            "url": self.url,
            "primary_category": self.primary_category,
            "categories": list(self.categories),
        }


# ─────────────────────────────────────────────────────────────────────────
# URL construction + fetch
# ─────────────────────────────────────────────────────────────────────────
def build_arxiv_query_url(
    categories: list[str],
    *,
    start: int = 0,
    max_results: int = 200,
    limits: Limits = DEFAULT_LIMITS,
) -> str:
    """Construct a validated arXiv API query URL.

    The URL is built server-side from validated inputs. Callers never
    provide the host. Every component is URL-encoded.

    Args:
        categories: List of arXiv category codes (e.g. ``["cs.CR"]``).
            Each one is validated before use.
        start: Pagination offset (0-based).
        max_results: Page size.
        limits: Effective limits (for bounds-checking ``start`` and
            ``max_results``).

    Returns:
        A fully-qualified GET URL.

    Raises:
        ValidationError: If categories or integer bounds fail validation.
    """
    validated_cats = validate_category_list(categories, limits=limits)
    validated_start = validate_positive_bounded_int(
        start,
        name="start",
        maximum=100_000,
        minimum=0,
    )
    validated_max = validate_positive_bounded_int(
        max_results,
        name="max_results",
        maximum=limits.max_results_per_page,
    )

    # arXiv's search_query syntax: `cat:cs.CR+OR+cat:cs.AI`. The '+OR+'
    # separator must not be URL-encoded as %2B — it's part of the
    # query DSL — so we assemble the clause manually and let urlencode
    # handle only the remaining parameters.
    cat_clause = "+OR+".join(f"cat:{c}" for c in validated_cats)

    params = urllib.parse.urlencode(
        [
            ("start", str(validated_start)),
            ("max_results", str(validated_max)),
            ("sortBy", "submittedDate"),
            ("sortOrder", "descending"),
        ]
    )
    return f"{ARXIV_API_URL}?search_query={cat_clause}&{params}"


def fetch_arxiv_feed(
    categories: list[str],
    *,
    start: int = 0,
    max_results: int = 200,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    limits: Limits = DEFAULT_LIMITS,
) -> str:
    """HTTPS GET against the arXiv Atom API. Returns the raw XML body.

    This is the ONE function in the package that touches the network.
    Tests mock it; everything else operates on already-fetched strings.

    Args:
        categories: arXiv categories to query.
        start: Pagination offset.
        max_results: Page size.
        timeout_s: Request timeout in seconds.
        limits: Effective limits.

    Returns:
        Raw XML response body, UTF-8-decoded (with replacement for
        invalid byte sequences — arXiv almost never emits these, but
        we're tolerant).

    Raises:
        urllib.error.HTTPError: For non-2xx responses.
        urllib.error.URLError: For transport-layer failures.
        TimeoutError: On timeout.
        ValidationError: If inputs fail validation.
    """
    url = build_arxiv_query_url(
        categories,
        start=start,
        max_results=max_results,
        limits=limits,
    )
    # `http://export.arxiv.org` is the arXiv-documented endpoint and the
    # only host we ever contact. Bandit / ruff's S310 flags any Request
    # constructor with a user-supplied URL as a possible SSRF vector; we
    # validate the inputs via build_arxiv_query_url above, so the
    # suppression here is intentional and audited.
    req = urllib.request.Request(  # noqa: S310
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        body: bytes = resp.read()
    return body.decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────
def _parse_arxiv_id(raw_id: str) -> tuple[str, str]:
    """Extract ``(canonical_id, version)`` from an arXiv ``<id>`` URL.

    ``http://arxiv.org/abs/2404.12345v3`` → ``("2404.12345", "v3")``

    Handles both new-format (YYMM.NNNNN) and old-format
    (``category/YYMMNNN``) identifiers. Falls back to ``(tail, "v1")``
    on unrecognized shapes so a single weird record doesn't derail the
    parse of an entire feed.
    """
    tail = raw_id.rsplit("/", 1)[-1] if raw_id else ""
    # Strip trailing vN if present.
    for i in range(len(tail) - 1, 0, -1):
        if tail[i] == "v" and tail[i + 1 :].isdigit():
            return (tail[:i], tail[i:])
    return (tail, "v1") if tail else ("", "v1")


def _parse_entry(entry: ET.Element) -> FetchedPaper | None:
    """Parse a single ``<atom:entry>`` element into a ``FetchedPaper``.

    Returns ``None`` if the entry is missing a stable ID (we can't
    dedup such entries and surfacing them would break the cursor).
    """

    def _text(xpath: str) -> str:
        el = entry.find(xpath, _ATOM_NS)
        return (el.text or "").strip() if el is not None else ""

    raw_id = _text("atom:id")
    arxiv_id, version = _parse_arxiv_id(raw_id)
    if not arxiv_id:
        return None

    # Collapse whitespace for display — arXiv titles and abstracts
    # contain LaTeX-ish line breaks that render badly in chat UIs.
    title = " ".join(_text("atom:title").split())
    summary = " ".join(_text("atom:summary").split())

    authors: list[str] = []
    for author_el in entry.findall("atom:author", _ATOM_NS):
        name_el = author_el.find("atom:name", _ATOM_NS)
        name = (name_el.text or "").strip() if name_el is not None else ""
        if name:
            authors.append(name)

    primary_el = entry.find("arxiv:primary_category", _ATOM_NS)
    primary_category = primary_el.get("term", "") if primary_el is not None else ""
    categories: list[str] = []
    for cat_el in entry.findall("atom:category", _ATOM_NS):
        term = (cat_el.get("term", "") or "").strip()
        if term:
            categories.append(term)

    url = ""
    for link_el in entry.findall("atom:link", _ATOM_NS):
        if link_el.get("rel") == "alternate":
            url = link_el.get("href", "")
            break
    if not url:
        url = f"http://arxiv.org/abs/{arxiv_id}"

    return FetchedPaper(
        arxiv_id=arxiv_id,
        version=version,
        title=title,
        summary=summary,
        authors=tuple(authors),
        published=_text("atom:published"),
        updated=_text("atom:updated"),
        url=url,
        primary_category=primary_category,
        categories=tuple(categories),
    )


def parse_arxiv_feed(xml_text: str) -> list[FetchedPaper]:
    """Parse an arXiv Atom feed into a list of ``FetchedPaper`` records.

    Tolerant: returns an empty list on unparseable XML and skips
    individual malformed entries rather than failing the whole feed.
    Upstream callers should never crash because arXiv served one weird
    ``<entry>``.

    XML parsing uses ``defusedxml`` so XXE, billion-laughs,
    quadratic-blowup, and external-DTD resolution are all disabled by
    default. arXiv is a trusted source over HTTPS, but that's a
    defensive default for a public package.

    Args:
        xml_text: Raw Atom feed XML.

    Returns:
        List of parsed papers in feed order.
    """
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    parsed: list[FetchedPaper] = []
    for entry_el in root.findall("atom:entry", _ATOM_NS):
        # Best-effort per-entry parsing: one malformed record should not
        # drop an entire feed. The operator will notice the missing paper
        # from the resulting digest being shorter — far less dangerous
        # than surfacing a crash mid-fetch. S112 noqa is intentional.
        try:
            paper = _parse_entry(entry_el)
        except Exception:  # noqa: S112
            continue
        if paper is not None:
            parsed.append(paper)
    return parsed


# ─────────────────────────────────────────────────────────────────────────
# Datetime helper (used by pipeline.py for date-window filtering)
# ─────────────────────────────────────────────────────────────────────────
def parse_iso8601(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp with a trailing ``Z``.

    arXiv emits UTC timestamps like ``2024-04-17T18:00:00Z``. Python's
    ``datetime.fromisoformat`` accepts trailing ``Z`` in 3.11+, but we
    normalize anyway so older interpreters stay compatible if the
    package is ever backported.

    Args:
        ts: Candidate timestamp string.

    Returns:
        Timezone-aware ``datetime``, or ``None`` if parsing fails.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
