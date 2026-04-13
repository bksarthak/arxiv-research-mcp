"""Tests for src/arxiv_research_mcp/arxiv.py (parser + URL builder).

The ``fetch_arxiv_feed`` function is the single network-touching call;
tests mock it. All other functions are pure and tested against fixture
strings.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arxiv_research_mcp.arxiv import (
    FetchedPaper,
    _parse_arxiv_id,
    build_arxiv_query_url,
    parse_arxiv_feed,
    parse_iso8601,
)
from arxiv_research_mcp.security import Limits, ValidationError

SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <title>arXiv Query Results</title>
  <entry>
    <id>http://arxiv.org/abs/2604.00001v2</id>
    <updated>2026-04-10T12:00:00Z</updated>
    <published>2026-04-10T12:00:00Z</published>
    <title>
      Indirect Prompt Injection Attacks on Autonomous LLM Agents
    </title>
    <summary>
      We present a novel attack vector where adversarial instructions
      embedded in tool outputs compromise LLM-driven agents.
    </summary>
    <author><name>Jane Researcher</name></author>
    <author><name>John Coauthor</name></author>
    <arxiv:primary_category term="cs.CR"
      xmlns:arxiv="http://arxiv.org/schemas/atom"/>
    <category term="cs.CR"/>
    <category term="cs.AI"/>
    <link href="http://arxiv.org/abs/2604.00001v2" rel="alternate"
          type="text/html"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2604.00002v1</id>
    <updated>2026-04-11T08:00:00Z</updated>
    <published>2026-04-11T08:00:00Z</published>
    <title>A Lattice Signature Scheme</title>
    <summary>Lattice-based signatures.</summary>
    <author><name>Cryptographer A</name></author>
    <arxiv:primary_category term="cs.CR"
      xmlns:arxiv="http://arxiv.org/schemas/atom"/>
    <category term="cs.CR"/>
    <link href="http://arxiv.org/abs/2604.00002v1" rel="alternate"
          type="text/html"/>
  </entry>
</feed>
"""


class TestParseArxivId:
    def test_new_format_with_version(self) -> None:
        assert _parse_arxiv_id("http://arxiv.org/abs/2404.12345v3") == ("2404.12345", "v3")

    def test_new_format_without_version_defaults_v1(self) -> None:
        assert _parse_arxiv_id("http://arxiv.org/abs/2404.12345") == ("2404.12345", "v1")

    def test_old_category_format(self) -> None:
        arxiv_id, version = _parse_arxiv_id("http://arxiv.org/abs/cs.CR/0601001v2")
        assert arxiv_id == "0601001"
        assert version == "v2"

    def test_empty_returns_empty(self) -> None:
        assert _parse_arxiv_id("") == ("", "v1")


class TestParseArxivFeed:
    def test_parses_all_entries(self) -> None:
        entries = parse_arxiv_feed(SAMPLE_FEED)
        assert len(entries) == 2

    def test_shape_is_fetched_paper(self) -> None:
        entries = parse_arxiv_feed(SAMPLE_FEED)
        assert all(isinstance(e, FetchedPaper) for e in entries)

    def test_extracts_fields(self) -> None:
        entries = parse_arxiv_feed(SAMPLE_FEED)
        first = entries[0]
        assert first.arxiv_id == "2604.00001"
        assert first.version == "v2"
        assert "Indirect Prompt Injection" in first.title
        assert "adversarial instructions" in first.summary
        assert first.authors == ("Jane Researcher", "John Coauthor")
        assert first.primary_category == "cs.CR"
        assert "cs.CR" in first.categories
        assert "cs.AI" in first.categories
        assert first.url.startswith("http://arxiv.org/abs/2604.00001")
        assert first.published == "2026-04-10T12:00:00Z"

    def test_collapses_whitespace_in_title(self) -> None:
        entries = parse_arxiv_feed(SAMPLE_FEED)
        assert "\n" not in entries[0].title
        assert "  " not in entries[0].title

    def test_parse_error_returns_empty(self) -> None:
        assert parse_arxiv_feed("<not valid xml") == []
        assert parse_arxiv_feed("") == []

    def test_skips_entries_without_id(self) -> None:
        feed = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <title>Orphan entry</title>
            <summary>No id</summary>
          </entry>
          <entry>
            <id>http://arxiv.org/abs/2604.99999v1</id>
            <title>Valid</title>
            <summary>Has id</summary>
          </entry>
        </feed>"""
        entries = parse_arxiv_feed(feed)
        assert len(entries) == 1
        assert entries[0].arxiv_id == "2604.99999"

    def test_fetched_paper_asdict_roundtrip(self) -> None:
        entries = parse_arxiv_feed(SAMPLE_FEED)
        d = entries[0].asdict()
        assert isinstance(d["authors"], list)
        assert d["arxiv_id"] == "2604.00001"


class TestParseIso8601:
    def test_z_suffix(self) -> None:
        result = parse_iso8601("2024-04-17T18:00:00Z")
        assert result is not None
        assert result == datetime(2024, 4, 17, 18, 0, 0, tzinfo=UTC)

    def test_explicit_offset(self) -> None:
        result = parse_iso8601("2024-04-17T18:00:00+00:00")
        assert result is not None
        assert result.tzinfo is not None

    def test_empty_returns_none(self) -> None:
        assert parse_iso8601("") is None

    def test_garbage_returns_none(self) -> None:
        assert parse_iso8601("not-a-timestamp") is None


class TestBuildArxivQueryUrl:
    def test_single_category(self) -> None:
        url = build_arxiv_query_url(["cs.CR"])
        assert url.startswith("http://export.arxiv.org/api/query")
        assert "cat:cs.CR" in url
        assert "sortBy=submittedDate" in url
        assert "sortOrder=descending" in url

    def test_multiple_categories_or_joined(self) -> None:
        url = build_arxiv_query_url(["cs.CR", "cs.AI"])
        assert "cat:cs.CR+OR+cat:cs.AI" in url

    def test_pagination_params(self) -> None:
        url = build_arxiv_query_url(["cs.CR"], start=200, max_results=100)
        assert "start=200" in url
        assert "max_results=100" in url

    def test_invalid_category_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_arxiv_query_url(["../../etc"])

    def test_negative_start_rejected(self) -> None:
        with pytest.raises(ValidationError):
            build_arxiv_query_url(["cs.CR"], start=-1)

    def test_max_results_cap(self) -> None:
        limits = Limits(max_results_per_page=100)
        with pytest.raises(ValidationError):
            build_arxiv_query_url(["cs.CR"], max_results=101, limits=limits)
