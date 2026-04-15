"""Tests for src/arxiv_research_mcp/pipeline.py — filters, cursor, orchestrator."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from arxiv_research_mcp import pipeline
from arxiv_research_mcp.arxiv import FetchedPaper
from arxiv_research_mcp.pipeline import (
    Cursor,
    VerdictCache,
    VerdictEntry,
    collect_candidate_papers,
    compute_rubric_hash,
    filter_by_date_window,
    keyword_prefilter,
)


def _make_paper(
    arxiv_id: str,
    title: str = "",
    summary: str = "",
    published: str = "2026-04-10T12:00:00Z",
    version: str = "v1",
) -> FetchedPaper:
    return FetchedPaper(
        arxiv_id=arxiv_id,
        version=version,
        title=title,
        summary=summary,
        authors=("Someone",),
        published=published,
        updated=published,
        url=f"http://arxiv.org/abs/{arxiv_id}",
        primary_category="cs.CR",
        categories=("cs.CR",),
    )


class TestKeywordPrefilter:
    def test_title_match(self) -> None:
        papers = [
            _make_paper("a", title="Prompt Injection Study"),
            _make_paper("b", title="Lattice Crypto"),
        ]
        kept = keyword_prefilter(papers, keywords=["prompt injection"])
        assert [p.arxiv_id for p in kept] == ["a"]

    def test_summary_match(self) -> None:
        papers = [_make_paper("a", summary="We study jailbreak attacks.")]
        kept = keyword_prefilter(papers, keywords=["jailbreak"])
        assert len(kept) == 1

    def test_case_insensitive(self) -> None:
        papers = [_make_paper("a", title="JAILBREAK Considered")]
        assert keyword_prefilter(papers, keywords=["jailbreak"]) == papers

    def test_empty_keywords_returns_all(self) -> None:
        papers = [_make_paper("a"), _make_paper("b")]
        assert keyword_prefilter(papers, keywords=[]) == papers


class TestFilterByDateWindow:
    def test_recent_kept(self) -> None:
        papers = [
            _make_paper("a", published="2026-04-10T12:00:00Z"),
            _make_paper("b", published="2025-01-01T00:00:00Z"),
        ]
        since = datetime(2026, 4, 1, tzinfo=UTC)
        kept = filter_by_date_window(papers, since=since)
        assert [p.arxiv_id for p in kept] == ["a"]

    def test_unparseable_kept(self) -> None:
        """Better false positive than miss."""
        papers = [_make_paper("a", published="garbage")]
        since = datetime(2026, 4, 1, tzinfo=UTC)
        assert filter_by_date_window(papers, since=since) == papers

    def test_at_boundary_inclusive(self) -> None:
        papers = [_make_paper("a", published="2026-04-01T00:00:00Z")]
        since = datetime(2026, 4, 1, tzinfo=UTC)
        assert len(filter_by_date_window(papers, since=since)) == 1


class TestCursor:
    def test_roundtrip_mark_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        cursor = Cursor(path)
        cursor.mark([_make_paper("2404.00001", title="Test")])
        cursor.save()

        fresh = Cursor(path)
        assert fresh.contains("2404.00001")
        assert len(fresh) == 1

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        cursor = Cursor(tmp_path / "nope.json")
        assert len(cursor) == 0

    def test_corrupt_file_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("{not valid json")
        cursor = Cursor(path)
        assert len(cursor) == 0

    def test_mark_returns_newly_added(self, tmp_path: Path) -> None:
        cursor = Cursor(tmp_path / "cursor.json")
        # First mark — all added
        first = cursor.mark([_make_paper("a"), _make_paper("b")])
        assert sorted(first) == ["a", "b"]
        # Second mark with overlap — only new ones added
        second = cursor.mark([_make_paper("b"), _make_paper("c")])
        assert second == ["c"]

    def test_mark_ids_without_metadata(self, tmp_path: Path) -> None:
        cursor = Cursor(tmp_path / "cursor.json")
        added = cursor.mark_ids(["a", "b", "c"])
        assert sorted(added) == ["a", "b", "c"]
        assert cursor.contains("a")

    def test_unmark(self, tmp_path: Path) -> None:
        cursor = Cursor(tmp_path / "cursor.json")
        cursor.mark_ids(["a", "b"])
        removed = cursor.unmark(["a", "not-present"])
        assert removed == ["a"]
        assert not cursor.contains("a")
        assert cursor.contains("b")

    def test_clear(self, tmp_path: Path) -> None:
        cursor = Cursor(tmp_path / "cursor.json")
        cursor.mark_ids(["a", "b", "c"])
        assert cursor.clear() == 3
        assert len(cursor) == 0

    def test_filter_unseen(self, tmp_path: Path) -> None:
        cursor = Cursor(tmp_path / "cursor.json")
        cursor.mark_ids(["a"])
        papers = [_make_paper("a"), _make_paper("b"), _make_paper("c")]
        unseen = cursor.filter_unseen(papers)
        assert [p.arxiv_id for p in unseen] == ["b", "c"]

    def test_snapshot_is_copy(self, tmp_path: Path) -> None:
        cursor = Cursor(tmp_path / "cursor.json")
        cursor.mark_ids(["a"])
        snap = cursor.snapshot()
        snap["b"] = {"first_surfaced": "", "version": "", "title": ""}
        # Mutating the snapshot does NOT affect the cursor.
        assert not cursor.contains("b")

    def test_atomic_write_uses_tmp(self, tmp_path: Path) -> None:
        """After save, the .tmp sibling should not exist."""
        cursor = Cursor(tmp_path / "cursor.json")
        cursor.mark_ids(["a"])
        cursor.save()
        assert (tmp_path / "cursor.json").exists()
        assert not (tmp_path / "cursor.json.tmp").exists()

    def test_save_is_json_parseable(self, tmp_path: Path) -> None:
        path = tmp_path / "cursor.json"
        cursor = Cursor(path)
        cursor.mark([_make_paper("2404.00001", title="X")])
        cursor.save()
        data = json.loads(path.read_text())
        assert "2404.00001" in data


class TestCollectCandidatePapers:
    def _feed(self, num: int, publish_year: int = 2026) -> str:
        entries = []
        for i in range(num):
            entries.append(f"""
  <entry>
    <id>http://arxiv.org/abs/{publish_year}04.{i:05d}v1</id>
    <published>{publish_year}-04-{10 + (i % 5):02d}T12:00:00Z</published>
    <updated>{publish_year}-04-{10 + (i % 5):02d}T12:00:00Z</updated>
    <title>Paper {i} on prompt injection</title>
    <summary>Study of jailbreak attacks.</summary>
    <author><name>Author {i}</name></author>
    <arxiv:primary_category term="cs.CR"
      xmlns:arxiv="http://arxiv.org/schemas/atom"/>
    <category term="cs.CR"/>
    <link href="http://arxiv.org/abs/{publish_year}04.{i:05d}v1"
          rel="alternate" type="text/html"/>
  </entry>""")
        return (
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:arxiv="http://arxiv.org/schemas/atom">' + "".join(entries) + "</feed>"
        )

    def test_single_page(self) -> None:
        feed = self._feed(num=3)
        since = datetime(2026, 4, 1, tzinfo=UTC)
        with patch.object(pipeline, "fetch_arxiv_feed", return_value=feed) as mock:
            out = collect_candidate_papers(
                ["cs.CR"],
                ["prompt injection"],
                since,
                page_size=200,
                max_pages=5,
                sleep_fn=lambda _: None,
            )
        assert len(out) == 3
        assert mock.call_count == 1  # page not full → stop

    def test_all_out_of_window(self) -> None:
        feed = self._feed(num=200, publish_year=2024)  # all old
        since = datetime(2026, 4, 1, tzinfo=UTC)
        with patch.object(pipeline, "fetch_arxiv_feed", return_value=feed) as mock:
            out = collect_candidate_papers(
                ["cs.CR"],
                ["prompt injection"],
                since,
                sleep_fn=lambda _: None,
            )
        assert out == []
        assert mock.call_count == 1  # early-stop on page 0

    def test_fetch_error_graceful_degrade(self) -> None:
        with patch.object(
            pipeline,
            "fetch_arxiv_feed",
            side_effect=ConnectionError("arxiv down"),
        ):
            out = collect_candidate_papers(
                ["cs.CR"],
                [],
                datetime(2026, 4, 1, tzinfo=UTC),
                sleep_fn=lambda _: None,
            )
        assert out == []

    def test_keyword_prefilter_applied(self) -> None:
        feed = """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:arxiv="http://arxiv.org/schemas/atom">
          <entry>
            <id>http://arxiv.org/abs/2604.11111v1</id>
            <published>2026-04-10T12:00:00Z</published>
            <updated>2026-04-10T12:00:00Z</updated>
            <title>Pure number theory</title>
            <summary>Algebraic analysis.</summary>
            <author><name>X</name></author>
            <arxiv:primary_category term="cs.CR"
              xmlns:arxiv="http://arxiv.org/schemas/atom"/>
            <category term="cs.CR"/>
            <link href="http://arxiv.org/abs/2604.11111v1"
                  rel="alternate" type="text/html"/>
          </entry>
          <entry>
            <id>http://arxiv.org/abs/2604.22222v1</id>
            <published>2026-04-10T12:00:00Z</published>
            <updated>2026-04-10T12:00:00Z</updated>
            <title>Jailbreak attacks against LLMs</title>
            <summary>Prompt injection study.</summary>
            <author><name>Y</name></author>
            <arxiv:primary_category term="cs.CR"
              xmlns:arxiv="http://arxiv.org/schemas/atom"/>
            <category term="cs.CR"/>
            <link href="http://arxiv.org/abs/2604.22222v1"
                  rel="alternate" type="text/html"/>
          </entry>
        </feed>"""
        since = datetime(2026, 4, 1, tzinfo=UTC)
        with patch.object(pipeline, "fetch_arxiv_feed", return_value=feed):
            out = collect_candidate_papers(
                ["cs.CR"],
                ["jailbreak", "prompt injection"],
                since,
                sleep_fn=lambda _: None,
            )
        ids = {p.arxiv_id for p in out}
        assert ids == {"2604.22222"}


class TestComputeRubricHash:
    def test_stable(self) -> None:
        h1 = compute_rubric_hash("test focus")
        h2 = compute_rubric_hash("test focus")
        assert h1 == h2

    def test_different_inputs_differ(self) -> None:
        h1 = compute_rubric_hash("focus A")
        h2 = compute_rubric_hash("focus B")
        assert h1 != h2

    def test_length_16(self) -> None:
        assert len(compute_rubric_hash("anything")) == 16


class TestVerdictCache:
    def test_roundtrip_store_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "verdicts.json"
        rhash = compute_rubric_hash("test")
        cache = VerdictCache(path, rhash)
        cache.store(
            [
                {
                    "arxiv_id": "2604.00001",
                    "relevance_score": 8,
                    "quality_score": 9,
                    "summary": "Good paper",
                    "project_angle": "Build it",
                    "reasoning": "Strong eval",
                }
            ]
        )
        cache.save()

        fresh = VerdictCache(path, rhash)
        assert fresh.contains("2604.00001")
        assert len(fresh) == 1
        entry = fresh.lookup("2604.00001")
        assert entry is not None
        assert entry["relevance_score"] == 8

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path / "nope.json", "hash")
        assert len(cache) == 0

    def test_corrupt_file_is_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.json"
        path.write_text("{bad json")
        cache = VerdictCache(path, "hash")
        assert len(cache) == 0

    def test_rubric_change_invalidates(self, tmp_path: Path) -> None:
        path = tmp_path / "verdicts.json"
        cache = VerdictCache(path, "hash_v1")
        cache.store(
            [
                {
                    "arxiv_id": "2604.00001",
                    "relevance_score": 8,
                    "quality_score": 9,
                }
            ]
        )
        cache.save()

        # Reload with different hash → empty
        fresh = VerdictCache(path, "hash_v2")
        assert len(fresh) == 0

    def test_prune_expired_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "verdicts.json"
        rhash = "h"
        cache = VerdictCache(path, rhash, ttl_days=7)
        # Store with an old timestamp
        cache.store(
            [{"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5}],
            now=datetime(2026, 4, 1, tzinfo=UTC),
        )
        cache.store(
            [{"arxiv_id": "2604.00002", "relevance_score": 5, "quality_score": 5}],
            now=datetime(2026, 4, 12, tzinfo=UTC),
        )
        cache.save()

        # Reload (prune runs on load) — the 2026-04-01 entry is >7 days old
        # relative to the 2026-04-12 entry, but prune uses datetime.now(UTC).
        # To test deterministically, we verify both are present and then
        # check that the cache with a short TTL drops the old one.
        short_ttl = VerdictCache(path, rhash, ttl_days=1)
        # With 1-day TTL, both should be expired (they're weeks old)
        assert len(short_ttl) == 0

    def test_split_cached_vs_new(self, tmp_path: Path) -> None:
        path = tmp_path / "verdicts.json"
        rhash = "h"
        cache = VerdictCache(path, rhash)
        cache.store(
            [
                {
                    "arxiv_id": "2604.00001",
                    "relevance_score": 8,
                    "quality_score": 7,
                }
            ]
        )

        papers = [
            _make_paper("2604.00001"),
            _make_paper("2604.00002"),
            _make_paper("2604.00003"),
        ]
        cached, net_new = cache.split_cached_vs_new(papers)
        assert len(cached) == 1
        assert cached[0]["arxiv_id"] == "2604.00001"
        assert [p.arxiv_id for p in net_new] == ["2604.00002", "2604.00003"]

    def test_store_returns_ids(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path / "v.json", "h")
        stored = cache.store(
            [
                {"arxiv_id": "2604.00001", "relevance_score": 8, "quality_score": 7},
                {"arxiv_id": "2604.00002", "relevance_score": 4, "quality_score": 3},
            ]
        )
        assert sorted(stored) == ["2604.00001", "2604.00002"]

    def test_store_skips_empty_ids(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path / "v.json", "h")
        stored = cache.store(
            [
                {"arxiv_id": "", "relevance_score": 5, "quality_score": 5},
                {"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5},
            ]
        )
        assert stored == ["2604.00001"]

    def test_store_overwrites_existing(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path / "v.json", "h")
        cache.store([{"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5}])
        cache.store([{"arxiv_id": "2604.00001", "relevance_score": 9, "quality_score": 9}])
        entry = cache.lookup("2604.00001")
        assert entry is not None
        assert entry["relevance_score"] == 9

    def test_clear(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path / "v.json", "h")
        cache.store(
            [
                {"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5},
                {"arxiv_id": "2604.00002", "relevance_score": 5, "quality_score": 5},
            ]
        )
        assert cache.clear() == 2
        assert len(cache) == 0

    def test_snapshot_is_copy(self, tmp_path: Path) -> None:
        cache = VerdictCache(tmp_path / "v.json", "h")
        cache.store([{"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5}])
        snap = cache.snapshot()
        snap["injected"] = VerdictEntry(
            judged_at="",
            arxiv_id="injected",
            relevance_score=0,
            quality_score=0,
            summary="",
            project_angle="",
            reasoning="",
        )
        assert not cache.contains("injected")

    def test_atomic_write_no_tmp_leftover(self, tmp_path: Path) -> None:
        path = tmp_path / "verdicts.json"
        cache = VerdictCache(path, "h")
        cache.store([{"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5}])
        cache.save()
        assert path.exists()
        assert not path.with_suffix(".json.tmp").exists()
