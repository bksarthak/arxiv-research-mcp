"""Prompt templates exposed as MCP ``prompts``.

MCP clients fetch these templates and then invoke them with their own
LLM — this package does no inference itself. The templates are
intentionally generic: no hardcoded model tokens (no ``<|think|>``, no
``<|channel>`` markers), no provider-specific syntax. They work with
Claude, GPT, local models, anything that can follow natural-language
instructions and emit JSON.

Two prompts are defined:

1. ``research_judge_rubric`` — the skeptical two-axis reviewer. Scores
   each paper on relevance and quality (both 0-10), writes a plain-
   language summary, and a one-sentence project angle. Outputs strict
   JSON.

2. ``weekly_digest_workflow`` — higher-level orchestration template
   that walks the client through fetch → judge → rank → surface → mark.
   Uses the research judge rubric internally.
"""

from __future__ import annotations

import json
from typing import Any

_RESEARCH_JUDGE_TEMPLATE = """You are a skeptical peer reviewer for an operator's weekly research \
digest. You will receive a JSON array of arXiv papers (title, authors, \
primary category, and abstract). For EACH paper, score it on two \
INDEPENDENT axes, write a plain-language summary, and suggest a mini \
project angle.

=== OPERATOR'S FOCUS (applied verbatim) ===
{rubric_focus}

=== RELEVANCE (0-10) ===
How well does this paper match the operator's focus above? Score HIGHER \
for papers that directly intersect the stated interests. Score LOWER for \
papers that are tangentially related, off-topic, or address a different \
audience (e.g. pure theoretical work when the operator cares about \
applied security).

=== QUALITY (0-10) — BE SKEPTICAL ===
You only see the abstract — you cannot verify experiments. But abstracts \
expose reliable quality signals. REWARD:
- Concrete method descriptions (not just "we propose a framework")
- Explicit evaluation signals: datasets, baselines, ablations mentioned
- Scope limits and honest claims ("we show X on benchmark Y")
- Clear novel contribution made in the opening sentences

PENALIZE:
- Marketing superlatives ("first-ever", "revolutionary", "solves the \
problem") without concrete backing
- Vague methods ("we leverage deep learning", "a novel framework")
- Missing evaluation signals in the abstract
- Survey / position papers that don't advance methodology
- Abstracts that hide what was actually done

=== SUMMARY ===
One or two plain-language sentences explaining what the paper actually \
does. No marketing adjectives. Assume a skilled engineer who is NOT an \
ML researcher.

=== PROJECT ANGLE ===
ONE sentence suggesting a small experiment the operator could run based \
on this paper. If the paper is purely theoretical or requires \
infrastructure the operator doesn't have (large training clusters, \
proprietary datasets, bespoke hardware), say so HONESTLY:

    "Theoretical — no direct homelab build angle."

Do NOT fabricate a project for a paper that doesn't support one. \
Honesty here is worth more than enthusiasm.

=== REASONING ===
One sentence explaining WHY you scored this paper the way you did — \
what specifically in the abstract drove the relevance and quality \
scores. This is the operator's audit trail.

=== OUTPUT FORMAT ===
Return a STRICT JSON array, no prose, no markdown fences, in the same \
order as the input:

[
  {{
    "arxiv_id": "...",
    "relevance_score": 0-10,
    "quality_score": 0-10,
    "summary": "...",
    "project_angle": "...",
    "reasoning": "..."
  }}
]

Length caps: summary ≤ 280 chars, project_angle ≤ 220 chars, \
reasoning ≤ 180 chars. If a paper is clearly out of scope you can still \
score it low and move on — don't skip entries."""


_WEEKLY_DIGEST_TEMPLATE = """You are assembling a weekly research digest from arXiv. Execute these \
steps in order:

1. Call ``fetch_candidate_papers(window_days={window_days})`` to fetch \
recent candidates from the configured topic (already keyword-filtered \
and deduped against the cursor). The response includes two lists:
   - ``candidates``: net-new papers that need judging.
   - ``cached_verdicts``: papers already judged in a previous run \
this {cadence} (scores, summaries, and reasoning are included). \
These do NOT need re-judging — use them as-is.

2. If BOTH lists are empty, respond: "No new candidates this \
{cadence} — check again next {cadence}." and stop.

3. If ``candidates`` is non-empty, apply the ``research_judge_rubric`` \
prompt ONLY to the ``candidates`` list (not the cached ones). Parse \
the JSON verdicts.

4. After judging, call ``submit_verdicts`` with ALL new verdicts \
(both those that passed and those that didn't) so they are cached \
for future runs this {cadence}.

5. Merge the fresh verdicts with ``cached_verdicts`` into one \
combined list. Filter: keep only papers where BOTH \
``relevance_score >= 7`` AND ``quality_score >= 7``. If nothing clears \
both thresholds, say so honestly: "No papers cleared both thresholds \
this {cadence}. The bar stays high for a reason." and stop.

6. Sort the passing verdicts by ``(relevance_score + quality_score)`` \
descending, break ties on relevance then quality then arxiv_id. Cap \
the surfaced set at {max_surfaced} papers.

7. Format each surfaced paper for the operator. Include:
   - Title and authors
   - arXiv URL
   - Both scores, prominently
   - Summary (plain language)
   - Project angle
   - Judge reasoning (so the operator can sanity-check the score)
   - Whether this was a cache hit or freshly judged

8. Call ``mark_papers_surfaced`` with the list of surfaced arxiv_ids \
so they don't re-appear next {cadence}.

9. Output a brief summary line: "Surfaced N papers (K from cache, \
J freshly judged). Dropped M below threshold. Cursor now tracks T ids."

Be honest about weak weeks. If the rubric rejects everything, that is \
the RIGHT outcome — do not lower the bar to manufacture a digest. \
To bypass the cache and re-judge everything, call \
``fetch_candidate_papers(use_cache=False)``."""


def render_research_judge_rubric(
    rubric_focus: str,
    papers: list[dict[str, Any]] | None = None,
) -> str:
    """Render the research-judge prompt with the operator's rubric focus.

    If ``papers`` is provided, the returned string includes the input
    JSON inline so the client's LLM has everything in one message. If
    omitted, the prompt ends with the instructions and expects the
    client to append the paper array in the same conversation.

    Args:
        rubric_focus: The operator's free-text focus description,
            injected verbatim.
        papers: Optional list of paper dicts to embed in the prompt.

    Returns:
        The fully-rendered prompt string.
    """
    body = _RESEARCH_JUDGE_TEMPLATE.format(rubric_focus=rubric_focus.strip())
    if papers is not None:
        body += "\n\n=== PAPERS TO SCORE ===\n" + json.dumps(papers, indent=2, ensure_ascii=False)
    return body


def render_weekly_digest_workflow(
    window_days: int = 7,
    max_surfaced: int = 7,
    cadence: str = "week",
) -> str:
    """Render the weekly-digest orchestration prompt.

    Args:
        window_days: Lookback window passed to ``fetch_candidate_papers``.
        max_surfaced: Hard cap on the surfaced set.
        cadence: Human-readable cadence word (``"week"``, ``"day"``,
            etc.) used in the rendered output.

    Returns:
        The fully-rendered prompt string.
    """
    return _WEEKLY_DIGEST_TEMPLATE.format(
        window_days=int(window_days),
        max_surfaced=int(max_surfaced),
        cadence=cadence,
    )
