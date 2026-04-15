"""Tests for src/arxiv_research_mcp/server.py tool handlers.

We invoke the tool handler functions directly — FastMCP's ``@mcp.tool()``
decorator exposes the underlying function via the wrapped object, and
we also use ``set_runtime_state()`` to inject test fixtures so no real
filesystem or network I/O happens.

These tests cover the validation and error-shaping behavior at the MCP
boundary. End-to-end MCP transport tests would require spinning up a
client and are out of scope for the unit suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from arxiv_research_mcp import server
from arxiv_research_mcp.arxiv import FetchedPaper
from arxiv_research_mcp.config import Config, Topic
from arxiv_research_mcp.pipeline import Cursor, VerdictCache, compute_rubric_hash
from arxiv_research_mcp.security import DEFAULT_LIMITS


# ─────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────
def _call(tool_name: str, **kwargs: Any) -> Any:
    """Invoke a FastMCP-decorated tool / prompt / resource by name.

    FastMCP attaches the original function to the wrapper via ``.fn``
    in recent SDK versions. If that attribute isn't present (older
    SDKs), we fall back to calling the wrapper directly — it behaves
    like the original function in most cases. Return type is ``Any``
    because tools return dicts, prompts and resources return strings.
    """
    handler = getattr(server, tool_name)
    underlying = getattr(handler, "fn", None)
    if callable(underlying):
        return underlying(**kwargs)
    return handler(**kwargs)


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path,
        topic=Topic(
            name="test",
            description="test topic",
            categories=("cs.CR",),
            keywords=("jailbreak", "prompt injection"),
            rubric_focus="Test focus",
        ),
        limits=DEFAULT_LIMITS,
        config_path=None,
    )


@pytest.fixture
def test_cursor(tmp_path: Path) -> Cursor:
    return Cursor(tmp_path / "cursor.json")


@pytest.fixture
def test_verdict_cache(tmp_path: Path, test_config: Config) -> VerdictCache:
    rhash = compute_rubric_hash(test_config.topic.rubric_focus)
    return VerdictCache(tmp_path / "verdict_cache.json", rhash)


@pytest.fixture(autouse=True)
def reset_server_state(
    test_config: Config,
    test_cursor: Cursor,
    test_verdict_cache: VerdictCache,
) -> Any:
    """Ensure every test starts with a clean, injected runtime state."""
    server.set_runtime_state(test_config, test_cursor, test_verdict_cache)
    yield
    server.reset_runtime_state()


def _make_paper(arxiv_id: str, title: str = "T", summary: str = "S") -> FetchedPaper:
    return FetchedPaper(
        arxiv_id=arxiv_id,
        version="v1",
        title=title,
        summary=summary,
        authors=("Someone",),
        published="2026-04-10T12:00:00Z",
        updated="2026-04-10T12:00:00Z",
        url=f"http://arxiv.org/abs/{arxiv_id}",
        primary_category="cs.CR",
        categories=("cs.CR",),
    )


# ─────────────────────────────────────────────────────────────────────────
# fetch_candidate_papers
# ─────────────────────────────────────────────────────────────────────────
class TestFetchCandidatePapers:
    def test_returns_structured_success(self) -> None:
        with patch.object(
            server,
            "collect_candidate_papers",
            return_value=[_make_paper("2604.00001", title="Jailbreak study")],
        ):
            result = _call("fetch_candidate_papers", window_days=7)
        assert result["ok"] is True
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["arxiv_id"] == "2604.00001"
        assert result["window_days"] == 7
        assert result["categories"] == ["cs.CR"]

    def test_dedup_filters_seen(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids(["2604.00001"])
        with patch.object(
            server,
            "collect_candidate_papers",
            return_value=[
                _make_paper("2604.00001"),
                _make_paper("2604.00002"),
            ],
        ):
            result = _call("fetch_candidate_papers", window_days=7)
        assert result["ok"] is True
        assert result["total"] == 2
        assert result["after_dedup"] == 1
        assert [c["arxiv_id"] for c in result["candidates"]] == ["2604.00002"]

    def test_dedup_disabled(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids(["2604.00001"])
        with patch.object(
            server,
            "collect_candidate_papers",
            return_value=[_make_paper("2604.00001")],
        ):
            result = _call("fetch_candidate_papers", window_days=7, dedup=False)
        assert result["after_dedup"] == 1

    def test_invalid_window_returns_error(self) -> None:
        result = _call("fetch_candidate_papers", window_days=0)
        assert result["ok"] is False
        assert "invalid argument" in result["error"]

    def test_custom_categories_and_keywords(self) -> None:
        with patch.object(server, "collect_candidate_papers", return_value=[]) as mock_fn:
            _call(
                "fetch_candidate_papers",
                window_days=3,
                categories=["cs.LG"],
                keywords=["adversarial"],
            )
        # Verify the mock was called with the validated values
        call_args = mock_fn.call_args
        assert call_args.args[0] == ["cs.LG"]
        assert call_args.args[1] == ["adversarial"]

    def test_garbage_category_returns_error(self) -> None:
        result = _call(
            "fetch_candidate_papers",
            window_days=7,
            categories=["../../etc"],
        )
        assert result["ok"] is False

    def test_network_failure_returns_error(self) -> None:
        with patch.object(
            server,
            "collect_candidate_papers",
            side_effect=RuntimeError("arxiv down"),
        ):
            result = _call("fetch_candidate_papers", window_days=7)
        assert result["ok"] is False
        assert "arxiv down" in result["error"]


# ─────────────────────────────────────────────────────────────────────────
# mark_papers_surfaced / unmark_papers / clear_cursor
# ─────────────────────────────────────────────────────────────────────────
class TestMarkPapersSurfaced:
    def test_adds_ids(self, test_cursor: Cursor) -> None:
        result = _call(
            "mark_papers_surfaced",
            arxiv_ids=["2604.00001", "2604.00002"],
        )
        assert result["ok"] is True
        assert sorted(result["added"]) == ["2604.00001", "2604.00002"]
        assert test_cursor.contains("2604.00001")

    def test_rejects_invalid_id(self) -> None:
        result = _call("mark_papers_surfaced", arxiv_ids=["garbage"])
        assert result["ok"] is False

    def test_persists_to_disk(self, test_cursor: Cursor, tmp_path: Path) -> None:
        _call("mark_papers_surfaced", arxiv_ids=["2604.00001"])
        # Reload from the same path
        fresh = Cursor(tmp_path / "cursor.json")
        assert fresh.contains("2604.00001")


class TestUnmarkPapers:
    def test_removes_ids(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids(["2604.00001", "2604.00002"])
        result = _call("unmark_papers", arxiv_ids=["2604.00001"])
        assert result["ok"] is True
        assert result["removed"] == ["2604.00001"]
        assert not test_cursor.contains("2604.00001")
        assert test_cursor.contains("2604.00002")

    def test_rejects_invalid_id(self) -> None:
        result = _call("unmark_papers", arxiv_ids=["garbage"])
        assert result["ok"] is False


class TestClearCursor:
    def test_requires_confirm(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids(["2604.00001"])
        result = _call("clear_cursor")
        assert result["ok"] is False
        assert "confirm" in result["error"].lower()
        # Cursor not cleared
        assert test_cursor.contains("2604.00001")

    def test_confirm_wipes(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids(["2604.00001", "2604.00002"])
        result = _call("clear_cursor", confirm=True)
        assert result["ok"] is True
        assert result["removed"] == 2
        assert len(test_cursor) == 0


class TestGetCursorState:
    def test_returns_entries(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids(["2604.00001", "2604.00002"])
        result = _call("get_cursor_state")
        assert result["ok"] is True
        assert result["total"] == 2
        assert "2604.00001" in result["entries"]

    def test_respects_limit(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids([f"2604.{i:05d}" for i in range(10)])
        result = _call("get_cursor_state", limit=3)
        assert result["total"] == 10
        assert result["returned"] == 3

    def test_invalid_limit_rejected(self) -> None:
        result = _call("get_cursor_state", limit=0)
        assert result["ok"] is False


# ─────────────────────────────────────────────────────────────────────────
# Prompts (via the FastMCP-decorated functions)
# ─────────────────────────────────────────────────────────────────────────
class TestResearchJudgeRubricPrompt:
    def test_includes_rubric_focus(self) -> None:
        result = _call("research_judge_rubric", papers_json="")
        assert "Test focus" in result

    def test_embeds_valid_papers_json(self) -> None:
        papers_json = '[{"arxiv_id": "2604.00001", "title": "Test"}]'
        result = _call("research_judge_rubric", papers_json=papers_json)
        assert "2604.00001" in result

    def test_ignores_invalid_json(self) -> None:
        """Invalid JSON is silently dropped — the prompt still renders."""
        result = _call("research_judge_rubric", papers_json="{not valid}")
        assert "Test focus" in result
        assert "not valid" not in result  # not embedded


class TestWeeklyDigestWorkflowPrompt:
    def test_default(self) -> None:
        result = _call("weekly_digest_workflow")
        assert "window_days=7" in result

    def test_custom_window(self) -> None:
        result = _call("weekly_digest_workflow", window_days=14)
        assert "window_days=14" in result


# ─────────────────────────────────────────────────────────────────────────
# Resources
# ─────────────────────────────────────────────────────────────────────────
class TestCursorStateResource:
    def test_returns_json(self, test_cursor: Cursor) -> None:
        test_cursor.mark_ids(["2604.00001"])
        result = _call("cursor_state_resource")
        data = json.loads(result)
        assert data["total"] == 1
        assert "2604.00001" in data["entries"]


class TestFetchCandidatePapersCache:
    """Tests for verdict cache integration in fetch_candidate_papers."""

    def test_cache_splits_candidates(
        self,
        test_verdict_cache: VerdictCache,
    ) -> None:
        # Pre-populate the cache with a verdict for paper 1
        test_verdict_cache.store(
            [
                {
                    "arxiv_id": "2604.00001",
                    "relevance_score": 8,
                    "quality_score": 9,
                    "summary": "Cached paper",
                }
            ]
        )
        with patch.object(
            server,
            "collect_candidate_papers",
            return_value=[
                _make_paper("2604.00001", title="Jailbreak"),
                _make_paper("2604.00002", title="Prompt injection"),
            ],
        ):
            result = _call("fetch_candidate_papers", window_days=7, use_cache=True)
        assert result["ok"] is True
        assert result["cache_hits"] == 1
        assert len(result["cached_verdicts"]) == 1
        assert result["cached_verdicts"][0]["arxiv_id"] == "2604.00001"
        # Only net-new papers in candidates
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["arxiv_id"] == "2604.00002"

    def test_cache_disabled(
        self,
        test_verdict_cache: VerdictCache,
    ) -> None:
        test_verdict_cache.store(
            [
                {
                    "arxiv_id": "2604.00001",
                    "relevance_score": 8,
                    "quality_score": 9,
                }
            ]
        )
        with patch.object(
            server,
            "collect_candidate_papers",
            return_value=[_make_paper("2604.00001", title="Jailbreak")],
        ):
            result = _call(
                "fetch_candidate_papers",
                window_days=7,
                use_cache=False,
            )
        assert result["ok"] is True
        assert result["cache_hits"] == 0
        assert len(result["candidates"]) == 1


# ─────────────────────────────────────────────────────────────────────────
# submit_verdicts
# ─────────────────────────────────────────────────────────────────────────
class TestSubmitVerdicts:
    def test_stores_verdicts(self, test_verdict_cache: VerdictCache) -> None:
        verdicts_json = json.dumps(
            [
                {
                    "arxiv_id": "2604.00001",
                    "relevance_score": 8,
                    "quality_score": 7,
                    "summary": "Paper A",
                    "reasoning": "Good eval",
                },
                {
                    "arxiv_id": "2604.00002",
                    "relevance_score": 4,
                    "quality_score": 3,
                    "summary": "Paper B",
                },
            ]
        )
        result = _call("submit_verdicts", verdicts_json=verdicts_json)
        assert result["ok"] is True
        assert sorted(result["stored"]) == ["2604.00001", "2604.00002"]
        assert result["cache_size"] == 2
        assert test_verdict_cache.contains("2604.00001")
        assert test_verdict_cache.contains("2604.00002")

    def test_invalid_json_returns_error(self) -> None:
        result = _call("submit_verdicts", verdicts_json="{bad json}")
        assert result["ok"] is False
        assert "invalid JSON" in result["error"]

    def test_non_array_returns_error(self) -> None:
        result = _call("submit_verdicts", verdicts_json='{"not": "array"}')
        assert result["ok"] is False
        assert "must be a JSON array" in result["error"]

    def test_invalid_verdict_returns_error(self) -> None:
        verdicts_json = json.dumps(
            [
                {"arxiv_id": "garbage", "relevance_score": 5, "quality_score": 5},
            ]
        )
        result = _call("submit_verdicts", verdicts_json=verdicts_json)
        assert result["ok"] is False

    def test_persists_to_disk(
        self,
        test_verdict_cache: VerdictCache,
        tmp_path: Path,
        test_config: Config,
    ) -> None:
        verdicts_json = json.dumps(
            [
                {"arxiv_id": "2604.00001", "relevance_score": 8, "quality_score": 7},
            ]
        )
        _call("submit_verdicts", verdicts_json=verdicts_json)
        # Reload from disk
        rhash = compute_rubric_hash(test_config.topic.rubric_focus)
        fresh = VerdictCache(tmp_path / "verdict_cache.json", rhash)
        assert fresh.contains("2604.00001")


# ─────────────────────────────────────────────────────────────────────────
# get_cached_verdicts / clear_verdict_cache
# ─────────────────────────────────────────────────────────────────────────
class TestGetCachedVerdicts:
    def test_returns_entries(self, test_verdict_cache: VerdictCache) -> None:
        test_verdict_cache.store(
            [
                {"arxiv_id": "2604.00001", "relevance_score": 8, "quality_score": 7},
                {"arxiv_id": "2604.00002", "relevance_score": 4, "quality_score": 3},
            ]
        )
        result = _call("get_cached_verdicts")
        assert result["ok"] is True
        assert result["total"] == 2
        assert "2604.00001" in result["entries"]

    def test_respects_limit(self, test_verdict_cache: VerdictCache) -> None:
        test_verdict_cache.store(
            [
                {"arxiv_id": f"2604.{i:05d}", "relevance_score": 5, "quality_score": 5}
                for i in range(10)
            ]
        )
        result = _call("get_cached_verdicts", limit=3)
        assert result["total"] == 10
        assert result["returned"] == 3

    def test_invalid_limit_rejected(self) -> None:
        result = _call("get_cached_verdicts", limit=0)
        assert result["ok"] is False


class TestClearVerdictCache:
    def test_requires_confirm(self, test_verdict_cache: VerdictCache) -> None:
        test_verdict_cache.store(
            [
                {"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5},
            ]
        )
        result = _call("clear_verdict_cache")
        assert result["ok"] is False
        assert "confirm" in result["error"].lower()
        assert test_verdict_cache.contains("2604.00001")

    def test_confirm_wipes(self, test_verdict_cache: VerdictCache) -> None:
        test_verdict_cache.store(
            [
                {"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5},
                {"arxiv_id": "2604.00002", "relevance_score": 5, "quality_score": 5},
            ]
        )
        result = _call("clear_verdict_cache", confirm=True)
        assert result["ok"] is True
        assert result["removed"] == 2
        assert len(test_verdict_cache) == 0


# ─────────────────────────────────────────────────────────────────────────
# verdict-cache://state resource
# ─────────────────────────────────────────────────────────────────────────
class TestVerdictCacheStateResource:
    def test_returns_json(self, test_verdict_cache: VerdictCache) -> None:
        test_verdict_cache.store(
            [
                {"arxiv_id": "2604.00001", "relevance_score": 8, "quality_score": 7},
            ]
        )
        result = _call("verdict_cache_state_resource")
        data = json.loads(result)
        assert data["total"] == 1
        assert "2604.00001" in data["entries"]
        assert "rubric_hash" in data


class TestActiveConfigResource:
    def test_returns_json(self) -> None:
        result = _call("active_config_resource")
        data = json.loads(result)
        assert data["topic"]["name"] == "test"
        assert data["topic"]["categories"] == ["cs.CR"]
        assert "limits" in data
        assert "max_verdicts_per_call" in data["limits"]
        assert "verdict_cache_ttl_days" in data["limits"]
