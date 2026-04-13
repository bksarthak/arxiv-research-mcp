"""Full loop: fetch → judge with Claude → rank → display.

Where ``quickstart.py`` stops at "here's the prompt we'd send to an LLM,"
this script goes all the way: it uses the official Anthropic SDK to call
Claude with the skeptical-reviewer rubric and displays the resulting
digest.

This is what an MCP client does under the hood, reproduced here as a
standalone script so you can see how to build your own automation on
top of ``arxiv-research-mcp`` without going through MCP transport (cron
jobs, Slack bots, CI-driven digests, etc.).

Requires::

    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

Usage::

    python examples/with_claude.py

The script uses a temporary cursor so it will not touch your real state.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arxiv_research_mcp.arxiv import FetchedPaper
from arxiv_research_mcp.config import Topic
from arxiv_research_mcp.pipeline import Cursor, collect_candidate_papers
from arxiv_research_mcp.prompts import render_research_judge_rubric

# ─── Tunables ───────────────────────────────────────────────────────────
RELEVANCE_THRESHOLD = 7
QUALITY_THRESHOLD = 7
MAX_SURFACED = 5
WINDOW_DAYS = 7
JUDGE_BATCH = 15  # candidates sent to Claude in one prompt
ANTHROPIC_MODEL = "claude-sonnet-4-6"


def _require_anthropic() -> Any:
    """Import the Anthropic SDK with a friendly error on missing install."""
    try:
        import anthropic  # noqa: PLC0415 — lazy import keeps optional dep
    except ImportError:
        print("This example needs the Anthropic SDK:\n    pip install anthropic\n")
        sys.exit(1)
    return anthropic


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    """Pull the first JSON array out of an LLM response.

    LLMs sometimes wrap JSON in markdown fences or prose. The rubric
    explicitly asks for a bare JSON array, but we're forgiving.
    """
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0 or end < start:
        msg = "could not find a JSON array in the response"
        raise ValueError(msg)
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, list):
        msg = "response was JSON but not an array"
        raise ValueError(msg)
    return [v for v in parsed if isinstance(v, dict)]


def _print_paper(paper: FetchedPaper, verdict: dict[str, Any]) -> None:
    """Render one scored paper in human-readable form."""
    first_author = paper.authors[0] if paper.authors else "unknown"
    more = f" +{len(paper.authors) - 1}" if len(paper.authors) > 1 else ""
    relevance = int(verdict.get("relevance_score", 0))
    quality = int(verdict.get("quality_score", 0))

    print("─" * 70)
    print(f"📄 {paper.title}")
    print(f"   {first_author}{more} • {paper.primary_category} • arxiv:{paper.arxiv_id}")
    print(f"   {paper.url}")
    print(f"   Relevance: {relevance}/10  •  Quality: {quality}/10")
    print()
    summary = str(verdict.get("summary", "")).strip()
    if summary:
        print(f"   📝 {summary}")
        print()
    angle = str(verdict.get("project_angle", "")).strip()
    if angle:
        print(f"   🔧 Project angle: {angle}")
        print()
    reasoning = str(verdict.get("reasoning", "")).strip()
    if reasoning:
        print(f"   🧠 Judge reasoning: {reasoning}")
        print()


def main() -> None:
    """Run the full Claude-judged digest loop."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "Set ANTHROPIC_API_KEY before running this example.\n"
            "    export ANTHROPIC_API_KEY=sk-ant-..."
        )
        sys.exit(1)

    anthropic = _require_anthropic()

    # Define the research interest. Swap these out for your own.
    topic = Topic(
        name="ai-security",
        description="AI × security",
        categories=("cs.CR",),
        keywords=(
            "llm",
            "prompt injection",
            "jailbreak",
            "adversarial",
            "backdoor",
            "data poisoning",
            "red team",
            "agentic",
            "autonomous agent",
            "watermark",
        ),
        rubric_focus=(
            "I care about LLM and agentic AI security: prompt injection, "
            "jailbreaks, tool-use attacks, adversarial ML, model extraction, "
            "and applied attacks with novel methodology. I'm less interested "
            "in pure crypto theory or survey papers."
        ),
    )

    with tempfile.TemporaryDirectory(prefix="arxiv-mcp-claude-") as tmp:
        cursor = Cursor(Path(tmp) / "cursor.json")

        # ── Step 1: Fetch candidates from arXiv ────────────────────────
        since = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
        print(f"[1/3] Fetching cs.CR papers since {since.date()}...")
        candidates = collect_candidate_papers(
            categories=list(topic.categories),
            keywords=list(topic.keywords),
            since=since,
        )
        print(f"      → {len(candidates)} candidates after prefilter.\n")

        if not candidates:
            print("No candidates this week. Exiting.")
            return

        batch = candidates[:JUDGE_BATCH]
        papers_for_judge = [
            {
                "arxiv_id": p.arxiv_id,
                "title": p.title,
                "authors": list(p.authors[:4]),
                "primary_category": p.primary_category,
                "abstract": p.summary,
            }
            for p in batch
        ]

        # ── Step 2: Ask Claude to score the batch ──────────────────────
        rubric = render_research_judge_rubric(
            rubric_focus=topic.rubric_focus,
            papers=papers_for_judge,
        )

        print(f"[2/3] Asking Claude to score {len(batch)} papers...")
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": rubric}],
        )
        raw_text = response.content[0].text
        try:
            verdicts = _extract_json_array(raw_text)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"      Failed to parse Claude response: {e}")
            print("      First 500 chars of response:")
            print(raw_text[:500])
            return

        # ── Step 3: Filter + rank + display ────────────────────────────
        passing = [
            v
            for v in verdicts
            if int(v.get("relevance_score", 0)) >= RELEVANCE_THRESHOLD
            and int(v.get("quality_score", 0)) >= QUALITY_THRESHOLD
        ]
        passing.sort(
            key=lambda v: (
                -(int(v["relevance_score"]) + int(v["quality_score"])),
                -int(v["relevance_score"]),
                str(v.get("arxiv_id", "")),
            )
        )
        surfaced = passing[:MAX_SURFACED]

        print(
            f"      {len(verdicts)} judged, {len(passing)} cleared both "
            f"thresholds, {len(surfaced)} surfaced.\n"
        )

        print("[3/3] Research digest:\n")
        if not surfaced:
            print(
                "No papers cleared both thresholds this week. The bar stays "
                "high for a reason — check back next week."
            )
            return

        by_id = {p.arxiv_id: p for p in batch}
        for verdict in surfaced:
            arxiv_id = str(verdict.get("arxiv_id", ""))
            paper = by_id.get(arxiv_id)
            if paper is None:
                continue
            _print_paper(paper, verdict)

        # Mark surfaced papers in the cursor so the next run skips them.
        cursor.mark_ids([str(v["arxiv_id"]) for v in surfaced])
        cursor.save()
        print("─" * 70)
        print(f"Marked {len(surfaced)} papers in the (temporary) cursor. Next run would skip them.")


if __name__ == "__main__":
    main()
