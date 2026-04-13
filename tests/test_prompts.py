"""Tests for src/arxiv_research_mcp/prompts.py render helpers."""

from __future__ import annotations

import json

from arxiv_research_mcp.prompts import (
    render_research_judge_rubric,
    render_weekly_digest_workflow,
)


class TestResearchJudgeRubric:
    def test_includes_rubric_focus_verbatim(self) -> None:
        focus = "I care about zero-knowledge proofs specifically."
        rendered = render_research_judge_rubric(focus)
        assert focus in rendered

    def test_mentions_both_axes(self) -> None:
        rendered = render_research_judge_rubric("x")
        assert "RELEVANCE" in rendered
        assert "QUALITY" in rendered

    def test_instructs_skeptical_stance(self) -> None:
        rendered = render_research_judge_rubric("x")
        assert "SKEPTICAL" in rendered.upper()

    def test_demands_strict_json(self) -> None:
        rendered = render_research_judge_rubric("x")
        assert "STRICT JSON" in rendered.upper()

    def test_honest_theoretical_fallback(self) -> None:
        """Prompt must teach the model to say 'theoretical' when a
        paper doesn't support a hands-on project angle.
        """
        rendered = render_research_judge_rubric("x")
        assert "Theoretical" in rendered

    def test_embeds_papers_when_provided(self) -> None:
        papers = [{"arxiv_id": "2404.00001", "title": "Test"}]
        rendered = render_research_judge_rubric("focus", papers=papers)
        assert "2404.00001" in rendered
        assert "=== PAPERS TO SCORE ===" in rendered
        # And the JSON should round-trip:
        idx = rendered.index("=== PAPERS TO SCORE ===")
        json_part = rendered[idx:].split("\n", 1)[1]
        assert json.loads(json_part) == papers

    def test_no_embedded_papers_when_none(self) -> None:
        rendered = render_research_judge_rubric("focus")
        assert "=== PAPERS TO SCORE ===" not in rendered

    def test_no_gemma_tokens(self) -> None:
        """Package is model-agnostic — no Gemma-specific thinking tokens."""
        rendered = render_research_judge_rubric("focus")
        assert "<|think|>" not in rendered
        assert "<|channel>" not in rendered


class TestWeeklyDigestWorkflow:
    def test_default_window(self) -> None:
        rendered = render_weekly_digest_workflow()
        assert "window_days=7" in rendered

    def test_custom_window(self) -> None:
        rendered = render_weekly_digest_workflow(window_days=14)
        assert "window_days=14" in rendered

    def test_max_surfaced_baked_in(self) -> None:
        rendered = render_weekly_digest_workflow(max_surfaced=3)
        assert "3 papers" in rendered

    def test_references_tools(self) -> None:
        rendered = render_weekly_digest_workflow()
        assert "fetch_candidate_papers" in rendered
        assert "mark_papers_surfaced" in rendered
        assert "research_judge_rubric" in rendered

    def test_mentions_threshold(self) -> None:
        rendered = render_weekly_digest_workflow()
        assert ">= 7" in rendered

    def test_honest_about_weak_weeks(self) -> None:
        """The workflow prompt must explicitly instruct the LLM not to
        lower the bar on low-volume weeks.
        """
        rendered = render_weekly_digest_workflow()
        assert "honest" in rendered.lower() or "honestly" in rendered.lower()
