# Security Policy

## Supported Versions

Only the latest released version on PyPI receives security updates.

## Reporting a Vulnerability

If you believe you have found a security vulnerability in `arxiv-research-mcp`, please **do not open a public issue**. Instead, report it privately via GitHub's security advisory flow:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability**.
3. Fill in as much detail as you can: affected version, reproduction steps, impact.

You should expect an acknowledgement within 72 hours. Fixes will be coordinated with you before public disclosure.

## Scope

This project is in-scope for reports on:

- Any code path that leads to arbitrary file read/write outside the configured data directory.
- Any code path that leads to arbitrary command execution.
- Any code path that sends requests to hosts other than `export.arxiv.org` (SSRF).
- XXE, billion-laughs, or other XML parsing vulnerabilities (we use `defusedxml` as a defensive default — regressions here are security bugs).
- Injection flaws in the prompt templates returned to MCP clients.
- Denial-of-service via unbounded memory or CPU growth on tool inputs.

Out of scope:

- Issues that require the attacker to already have write access to the user's config file.
- Issues in the MCP client itself, or in the LLM the client is running.
- Issues in `arxiv.org`'s public API.

## Security Design Principles

This section documents the security posture of the package for reviewers and auditors.

### Input validation at every boundary

Every MCP tool argument is validated before use:

- **arXiv IDs** must match `^[a-z\-]+/\d{7}(v\d+)?$` (old format) or `^\d{4}\.\d{4,5}(v\d+)?$` (new format). Invalid IDs are rejected with `ValidationError` before any I/O.
- **Category names** must match `^[a-zA-Z][a-zA-Z\-]*(\.[A-Z]{2})?$`.
- **Integer parameters** (window_days, max_results, etc.) have hard upper bounds defined in `security.py::LIMITS`.
- **List parameters** (arxiv_ids, keywords, categories, verdicts) have max-length caps to prevent resource exhaustion.
- **Verdict dicts** submitted via `submit_verdicts` are validated: arXiv ID regex, scores must be ints in [0, 10] (booleans rejected), optional text fields truncated to safe lengths.

Validators live in `src/arxiv_research_mcp/security.py` and are unit-tested in `tests/test_security.py`.

### No shell execution

The package does not call `subprocess`, `os.system`, `os.popen`, or `shell=True` anywhere. There is no code path that executes an external binary.

### SSRF mitigation

The package only ever connects to `http://export.arxiv.org/api/query`. The URL is constructed internally from validated inputs — the user cannot inject an arbitrary host. No user-supplied URL is ever fetched.

### Safe XML parsing

We use `defusedxml.ElementTree` instead of stdlib `xml.etree.ElementTree` to parse arXiv's Atom feed. This protects against XXE, billion-laughs, quadratic-blowup, and external DTD resolution — all of which are disabled by default in `defusedxml`.

### File I/O sandboxing

The cursor file (`cursor.json`) and verdict cache file (`verdict_cache.json`) are the only persistent state the package writes. Both live under the platform-appropriate data directory (XDG_DATA_HOME on Linux, `~/Library/Application Support` on macOS, `%LOCALAPPDATA%` on Windows), resolved once at startup. Neither path is derived from tool arguments — users cannot redirect writes to arbitrary paths.

Writes are atomic via `os.replace()` against a sibling `.tmp` file.

### Rate limiting

The arXiv Atom API client sleeps 3 seconds between paginated requests as specified in the arXiv API terms. This is enforced in-process; it cannot be bypassed by tool arguments.

### Dependency hygiene

Runtime dependencies are kept minimal:

- `mcp` — the Model Context Protocol SDK
- `defusedxml` — safe XML parsing

Everything else (HTTP, filesystem, TOML parsing) uses the Python standard library.

### Type strictness

The codebase is type-checked with `mypy --strict`. This eliminates a class of runtime errors that can become security bugs under adversarial inputs.
