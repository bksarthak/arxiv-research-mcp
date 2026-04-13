"""Quickstart: exercise the full pipeline without an MCP client.

This is the simplest way to see what ``arxiv-research-mcp`` actually does.
It imports the package directly, runs one real HTTPS request against the
arXiv API, then walks through the whole pipeline in-process:

    1. Build an in-memory ``Topic`` (in real use this comes from config.toml)
    2. Fetch candidate papers for the last 7 days (one network call)
    3. Show the first handful
    4. Render the skeptical-reviewer judge prompt with those papers embedded
       — this is exactly what an MCP client hands to its LLM
    5. Simulate marking the top two as "surfaced" in a temporary cursor
    6. Re-apply the cursor to show dedup kicks in on the next run

Usage::

    python examples/quickstart.py

The temporary cursor lives in a tmpdir — it will NOT touch your real state
at ``~/.local/share/arxiv-research-mcp/cursor.json``. Running this script
repeatedly is safe and idempotent.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arxiv_research_mcp.config import Topic
from arxiv_research_mcp.pipeline import Cursor, collect_candidate_papers
from arxiv_research_mcp.prompts import render_research_judge_rubric


def _hr(title: str = "") -> None:
    """Print a horizontal separator, optionally with an inline title."""
    if title:
        print(f"\n─── {title} " + "─" * max(0, 60 - len(title)))
    else:
        print("─" * 64)


def main() -> None:
    """Run the quickstart pipeline."""
    # 1. Topic definition. In real use this is loaded from a TOML config
    #    at ``${XDG_CONFIG_HOME}/arxiv-research-mcp/config.toml``.
    topic = Topic(
        name="ai-security-demo",
        description="AI × security quickstart",
        categories=("cs.CR",),
        keywords=(
            "prompt injection",
            "jailbreak",
            "adversarial",
            "backdoor",
            "agentic",
            "red team",
        ),
        rubric_focus=(
            "I care about LLM security — prompt injection, jailbreaks, "
            "agentic attacks, adversarial ML, model extraction. Less "
            "interested in pure crypto theory or survey papers."
        ),
    )
    print(f"▶ Topic: {topic.name}")
    print(f"  Categories: {list(topic.categories)}")
    print(f"  Keywords:   {len(topic.keywords)} terms")

    # 2. Put the cursor in a temp dir so the demo is hermetic.
    with tempfile.TemporaryDirectory(prefix="arxiv-mcp-demo-") as tmp:
        cursor_path = Path(tmp) / "cursor.json"
        cursor = Cursor(cursor_path)

        # 3. Fetch candidates. This is the ONE network call in the script.
        since = datetime.now(UTC) - timedelta(days=7)
        _hr("Fetching")
        print(f"Querying arXiv for cs.CR papers submitted since {since.date()}...")
        candidates = collect_candidate_papers(
            categories=list(topic.categories),
            keywords=list(topic.keywords),
            since=since,
        )
        print(f"→ {len(candidates)} candidates after keyword prefilter.")

        if not candidates:
            print(
                "\nNo candidates this week. Try widening the window, or relaxing "
                "the keywords in the Topic above."
            )
            return

        # 4. Show the first few. In production your MCP client gets the
        #    same list as a JSON array via the `fetch_candidate_papers` tool.
        _hr("Top candidates (pre-judging)")
        for paper in candidates[:5]:
            first_author = paper.authors[0] if paper.authors else "unknown"
            more = f" +{len(paper.authors) - 1}" if len(paper.authors) > 1 else ""
            title = paper.title if len(paper.title) <= 72 else paper.title[:69] + "..."
            print(f"• [{paper.arxiv_id}] {title}")
            print(f"    {first_author}{more} • {paper.primary_category}")

        # 5. Render the skeptical-reviewer prompt with the candidates
        #    embedded. This is exactly what an MCP client sends to its LLM
        #    when it invokes the `research_judge_rubric` prompt.
        judge_batch = candidates[: min(5, len(candidates))]
        papers_for_judge = [
            {
                "arxiv_id": p.arxiv_id,
                "title": p.title,
                "authors": list(p.authors[:4]),
                "primary_category": p.primary_category,
                "abstract": p.summary,
            }
            for p in judge_batch
        ]
        rubric_text = render_research_judge_rubric(
            rubric_focus=topic.rubric_focus,
            papers=papers_for_judge,
        )
        _hr("Judge prompt preview")
        preview_len = 700
        print(rubric_text[:preview_len])
        print(f"\n[... {len(rubric_text) - preview_len} more chars ...]\n")
        print(f"Full prompt size: {len(rubric_text):,} chars")
        print(f"Papers in batch:  {len(papers_for_judge)}")
        print(
            "\nIn a real run your MCP client's LLM (Claude, GPT, whatever) "
            "receives\nthis prompt and returns a JSON array of verdicts with "
            "relevance,\nquality, summary, project_angle, and reasoning."
        )

        # 6. Simulate marking the top two papers as surfaced.
        top_two = candidates[:2]
        added = cursor.mark(top_two)
        cursor.save()
        _hr("Cursor state")
        print(f"Marked {len(added)} papers as surfaced: {added}")
        print(f"Cursor now tracks {len(cursor)} ID(s) at {cursor_path}")

        # 7. Verify dedup works: the papers we just marked should be filtered
        #    out when we re-apply the cursor to the candidate list.
        remaining = cursor.filter_unseen(candidates)
        _hr("After dedup")
        print(f"{len(remaining)}/{len(candidates)} candidates would be shown on the next run.")
        print("(The two we just marked are filtered out automatically.)")

    _hr()
    print("✓ Quickstart complete.\n")
    print("Next steps:")
    print("  • Read README.md for install + Claude Desktop / Claude Code setup")
    print("  • Customize examples/config.toml for your own research interests")
    print("  • Run examples/with_claude.py for the full fetch→judge→rank loop")


if __name__ == "__main__":
    main()
