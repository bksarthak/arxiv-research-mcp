"""Full loop with Gemini: fetch → judge with Gemini → rank → display.

Mirror of ``with_claude.py`` using Google's official ``google-genai``
SDK (the current-generation replacement for the legacy
``google-generativeai`` package). The three provider examples are kept
deliberately parallel so you can diff them to see exactly where SDKs
differ.

Requires::

    pip install google-genai
    export GOOGLE_API_KEY=AIza...   # or GEMINI_API_KEY

Usage::

    python examples/with_gemini.py

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
JUDGE_BATCH = 15  # candidates sent to Gemini in one prompt
# Swap this to whatever Gemini model your account has access to.
GEMINI_MODEL = "gemini-2.5-pro"


def _require_genai() -> Any:
    """Import the google-genai SDK with a friendly error on missing install."""
    try:
        from google import genai  # noqa: PLC0415 — lazy import keeps optional dep
    except ImportError:
        print(
            "This example needs Google's Gen AI SDK:\n    pip install google-genai\n",
        )
        sys.exit(1)
    return genai


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
    """Run the full Gemini-judged digest loop."""
    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "Set GOOGLE_API_KEY (or GEMINI_API_KEY) before running this example.\n"
            "    export GOOGLE_API_KEY=AIza...",
        )
        sys.exit(1)

    genai = _require_genai()

    # Define the research interest. Swap these out for your own — or
    # load from a TOML file via ``arxiv_research_mcp.config.load_config``.
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

    with tempfile.TemporaryDirectory(prefix="arxiv-mcp-gemini-") as tmp:
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

        # ── Step 2: Ask Gemini to score the batch ──────────────────────
        rubric = render_research_judge_rubric(
            rubric_focus=topic.rubric_focus,
            papers=papers_for_judge,
        )

        print(f"[2/3] Asking {GEMINI_MODEL} to score {len(batch)} papers...")
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=rubric,
        )
        raw_text = response.text or ""
        try:
            verdicts = _extract_json_array(raw_text)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"      Failed to parse Gemini response: {e}")
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
