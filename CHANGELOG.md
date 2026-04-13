# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Verdict caching** — LLM judge scores are cached server-side in
  `verdict_cache.json`. Repeat runs within the TTL window skip re-judging
  known papers and only send net-new candidates to the LLM.
- `VerdictCache` class in `pipeline.py` — mirrors the `Cursor` pattern:
  atomic JSON writes, typed entries, TTL-based expiry, rubric hash
  auto-invalidation.
- `compute_rubric_hash()` — stable SHA-256 of the operator's `rubric_focus`
  string. Cache auto-invalidates when the config changes.
- `submit_verdicts(verdicts_json)` MCP tool — client reports LLM judge
  verdicts (both surfaced and rejected) for caching.
- `get_cached_verdicts(limit)` MCP tool — inspect cached verdicts.
- `clear_verdict_cache(confirm)` MCP tool — wipe the cache (requires
  `confirm=True`).
- `verdict-cache://state` MCP resource — current cache as JSON.
- `use_cache` parameter on `fetch_candidate_papers` — returns
  `cached_verdicts` alongside `candidates`. Set to `False` to bypass the
  cache for a fresh re-judge.
- `validate_verdict()` and `validate_verdict_list()` input validators in
  `security.py` — arXiv ID validation, score range [0, 10], text truncation.
- `max_verdicts_per_call` and `verdict_cache_ttl_days` fields in `Limits`
  dataclass, configurable via `[limits]` in TOML config.
- `Config.verdict_cache_path` property.
- Updated `weekly_digest_workflow` prompt with cache-aware orchestration
  steps (use cached verdicts, judge only net-new, submit all verdicts).
- 40 new tests covering verdict cache, validators, and MCP tool handlers.

## [0.1.0] — 2026-04-12

### Added
- Initial release.
- `fetch_candidate_papers` MCP tool — paginated arXiv Atom API client with
  keyword pre-filtering, date-window filtering, and cursor dedup.
- `mark_papers_surfaced` / `unmark_papers` / `get_cursor_state` /
  `clear_cursor` MCP tools for dedup cursor management.
- `research_judge_rubric` MCP prompt — skeptical two-axis reviewer
  template (relevance + quality) with configurable rubric focus injected
  from user config.
- `weekly_digest_workflow` MCP prompt — higher-level orchestration
  template that walks the client through fetch → judge → rank → surface.
- `cursor://state` and `config://active` MCP resources.
- TOML configuration at `${XDG_CONFIG_HOME}/arxiv-research-mcp/config.toml`
  (or platform-appropriate default) with configurable arXiv categories,
  keyword prefilter vocabulary, and rubric focus.
- Strict input validation on every tool argument: arXiv ID regex,
  category name regex, integer bounds, list size limits.
- SSRF protection on any URL construction (only the fixed arXiv endpoint).
- XXE protection via `defusedxml` for Atom feed parsing.
- 26 unit tests covering parse/filter/cursor/security pipelines.
- GitHub Actions CI (ruff, mypy --strict, pytest on Python 3.11/3.12/3.13).
