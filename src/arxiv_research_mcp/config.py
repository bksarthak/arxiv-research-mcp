r"""Configuration: TOML loading, platform-appropriate paths, validation.

The server loads its config from the first of these that exists:

1. ``$ARXIV_RESEARCH_MCP_CONFIG`` (explicit override)
2. ``$XDG_CONFIG_HOME/arxiv-research-mcp/config.toml`` (Linux)
3. ``~/Library/Application Support/arxiv-research-mcp/config.toml`` (macOS)
4. ``%APPDATA%\arxiv-research-mcp\config.toml`` (Windows)

If none exist, the server falls back to documented defaults — the AI ×
security keyword set from ``examples/config.toml``. This means the
package works out of the box for users who just want the default topic,
while letting power users override every knob.

TOML is parsed with stdlib ``tomllib`` (Python 3.11+). No third-party
TOML library is needed.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

from arxiv_research_mcp.security import (
    DEFAULT_LIMITS,
    Limits,
    ValidationError,
    validate_category_list,
    validate_keyword_list,
    validate_positive_bounded_int,
    validate_rubric_focus,
)

#: Environment variable for an explicit config path override.
CONFIG_ENV_VAR: Final[str] = "ARXIV_RESEARCH_MCP_CONFIG"

#: Package name used in platform-default path resolution.
_PKG_DIR_NAME: Final[str] = "arxiv-research-mcp"

#: Default topic keywords if the user config is missing entirely. The
#: shipped ``examples/config.toml`` contains a larger list with comments;
#: this in-code fallback is the minimum viable set so a default install
#: gives meaningful results.
_DEFAULT_KEYWORDS: Final[tuple[str, ...]] = (
    "llm",
    "language model",
    "prompt injection",
    "jailbreak",
    "adversarial",
    "data poisoning",
    "backdoor",
    "red team",
    "red-teaming",
    "autonomous agent",
    "agentic",
    "watermark",
    "model extraction",
    "membership inference",
)

_DEFAULT_RUBRIC_FOCUS: Final[str] = (
    "Operator cares about the intersection of AI and cybersecurity: "
    "LLM and agentic-AI security (prompt injection, jailbreaks, tool-use "
    "attacks, autonomous agent compromise), adversarial ML (model "
    "extraction, data poisoning, backdoors, membership inference, "
    "watermarking), and applied security with a novel or hands-on angle "
    "(new attack classes, red-team tooling, ML-based defenses). "
    "Operator cares LESS about pure cryptographic theory, formal "
    "verification, and survey/position papers without new results."
)


# ─────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Topic:
    """User-configurable research topic definition.

    Every field has a sensible default so the server boots even with
    no config file present. Users override what they care about.
    """

    name: str = "ai-security"
    description: str = (
        "AI × cybersecurity intersection: LLM jailbreaks, adversarial ML, agentic-AI attacks."
    )
    categories: tuple[str, ...] = ("cs.CR",)
    keywords: tuple[str, ...] = _DEFAULT_KEYWORDS
    rubric_focus: str = _DEFAULT_RUBRIC_FOCUS


@dataclass(frozen=True, slots=True)
class Config:
    """Fully-resolved server configuration.

    ``data_dir`` is the root directory for persistent state (currently
    only the dedup cursor). ``topic`` is the active research profile.
    ``limits`` is the effective set of hard caps — either the defaults
    or user overrides.
    ``config_path`` is the path the config was loaded from, or ``None``
    if defaults were used. Shown in the ``config://active`` resource
    for transparency.
    """

    data_dir: Path
    topic: Topic = field(default_factory=Topic)
    limits: Limits = DEFAULT_LIMITS
    config_path: Path | None = None

    @property
    def cursor_path(self) -> Path:
        """Resolved path to the dedup cursor file."""
        return self.data_dir / "cursor.json"

    @property
    def verdict_cache_path(self) -> Path:
        """Resolved path to the verdict cache file."""
        return self.data_dir / "verdict_cache.json"


# ─────────────────────────────────────────────────────────────────────────
# Path resolution
# ─────────────────────────────────────────────────────────────────────────
def _platform_config_dir() -> Path:
    """Return the platform-appropriate user config directory."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / _PKG_DIR_NAME
        return Path.home() / "AppData" / "Roaming" / _PKG_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _PKG_DIR_NAME
    # Linux / other POSIX
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _PKG_DIR_NAME
    return Path.home() / ".config" / _PKG_DIR_NAME


def _platform_data_dir() -> Path:
    """Return the platform-appropriate user data directory."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / _PKG_DIR_NAME
        return Path.home() / "AppData" / "Local" / _PKG_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _PKG_DIR_NAME
    # Linux / other POSIX
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / _PKG_DIR_NAME
    return Path.home() / ".local" / "share" / _PKG_DIR_NAME


def resolve_config_path() -> Path | None:
    """Return the path of the first existing config file, or ``None``.

    Resolution order:

    1. ``$ARXIV_RESEARCH_MCP_CONFIG`` (if set and file exists)
    2. Platform default (``$XDG_CONFIG_HOME/arxiv-research-mcp/config.toml``
       or equivalent)

    Returns:
        A ``Path`` to an existing config file, or ``None`` if neither
        exists. Callers use defaults when ``None`` is returned.
    """
    env_override = os.environ.get(CONFIG_ENV_VAR)
    if env_override:
        p = Path(env_override).expanduser()
        if p.is_file():
            return p

    platform_default = _platform_config_dir() / "config.toml"
    if platform_default.is_file():
        return platform_default

    return None


# ─────────────────────────────────────────────────────────────────────────
# TOML parsing + validation
# ─────────────────────────────────────────────────────────────────────────
def _parse_topic_block(
    block: dict[str, object],
    limits: Limits,
) -> Topic:
    """Validate and project a ``[topic]`` block into a ``Topic`` dataclass.

    Raises:
        ValidationError: On any type or value violation. Unknown keys
            are ignored (forward compatibility).
    """
    defaults = Topic()

    name = block.get("name", defaults.name)
    if not isinstance(name, str):
        raise ValidationError(f"[topic] name must be a string, got {type(name).__name__}")

    description = block.get("description", defaults.description)
    if not isinstance(description, str):
        raise ValidationError(
            f"[topic] description must be a string, got {type(description).__name__}"
        )

    categories_raw = block.get("categories", list(defaults.categories))
    if not isinstance(categories_raw, list):
        raise ValidationError(
            f"[topic] categories must be a list, got {type(categories_raw).__name__}"
        )
    categories = tuple(validate_category_list(categories_raw, limits=limits))

    keywords_raw = block.get("keywords", list(defaults.keywords))
    if not isinstance(keywords_raw, list):
        raise ValidationError(f"[topic] keywords must be a list, got {type(keywords_raw).__name__}")
    keywords = tuple(validate_keyword_list(keywords_raw, limits=limits))

    rubric_raw = block.get("rubric_focus", defaults.rubric_focus)
    if not isinstance(rubric_raw, str):
        raise ValidationError(
            f"[topic] rubric_focus must be a string, got {type(rubric_raw).__name__}"
        )
    rubric_focus = validate_rubric_focus(rubric_raw, limits=limits)

    return Topic(
        name=name.strip() or defaults.name,
        description=description.strip() or defaults.description,
        categories=categories,
        keywords=keywords,
        rubric_focus=rubric_focus,
    )


def _parse_limits_block(block: dict[str, object]) -> Limits:
    """Validate and project a ``[limits]`` block into a ``Limits`` dataclass.

    Any field omitted from the block keeps its default. Unknown keys
    are rejected — limits bugs are sharp-edged enough that a typo
    should fail loudly rather than silently ignore a user override.
    """
    defaults = DEFAULT_LIMITS
    known = {
        "max_window_days",
        "max_results_per_page",
        "max_pages_per_fetch",
        "max_keywords",
        "max_keyword_length",
        "max_categories",
        "max_arxiv_ids_per_call",
        "max_rubric_focus_chars",
        "max_verdicts_per_call",
        "verdict_cache_ttl_days",
    }
    unknown = set(block.keys()) - known
    if unknown:
        raise ValidationError(
            f"[limits] unknown keys: {sorted(unknown)}. Known keys: {sorted(known)}"
        )

    def _get(name: str, current: int, cap: int) -> int:
        if name not in block:
            return current
        value = block[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"[limits] {name} must be an int, got {type(value).__name__}")
        return validate_positive_bounded_int(value, name=f"[limits].{name}", maximum=cap, minimum=1)

    return replace(
        defaults,
        max_window_days=_get("max_window_days", defaults.max_window_days, 3650),
        max_results_per_page=_get("max_results_per_page", defaults.max_results_per_page, 2000),
        max_pages_per_fetch=_get("max_pages_per_fetch", defaults.max_pages_per_fetch, 100),
        max_keywords=_get("max_keywords", defaults.max_keywords, 10_000),
        max_keyword_length=_get("max_keyword_length", defaults.max_keyword_length, 4096),
        max_categories=_get("max_categories", defaults.max_categories, 200),
        max_arxiv_ids_per_call=_get(
            "max_arxiv_ids_per_call",
            defaults.max_arxiv_ids_per_call,
            100_000,
        ),
        max_rubric_focus_chars=_get(
            "max_rubric_focus_chars",
            defaults.max_rubric_focus_chars,
            1_000_000,
        ),
        max_verdicts_per_call=_get(
            "max_verdicts_per_call",
            defaults.max_verdicts_per_call,
            100_000,
        ),
        verdict_cache_ttl_days=_get(
            "verdict_cache_ttl_days",
            defaults.verdict_cache_ttl_days,
            3650,
        ),
    )


def _parse_server_block(
    block: dict[str, object],
    default_data_dir: Path,
) -> Path:
    """Extract the data directory from a ``[server]`` block.

    Returns the default if ``data_dir`` is absent. If present, the
    value must be a string; it is expanded (``~`` and env vars) and
    converted to an absolute ``Path``.
    """
    value = block.get("data_dir")
    if value is None:
        return default_data_dir
    if not isinstance(value, str):
        raise ValidationError(f"[server] data_dir must be a string, got {type(value).__name__}")
    expanded = os.path.expanduser(os.path.expandvars(value))
    return Path(expanded).resolve()


def load_config(path: Path | None = None) -> Config:
    """Load and validate the server configuration.

    If ``path`` is ``None``, uses ``resolve_config_path()`` to find a
    config file. If still no file is found, returns a fully-defaulted
    ``Config`` with the platform data dir.

    Args:
        path: Explicit config path override.

    Returns:
        Validated ``Config``.

    Raises:
        ValidationError: If the config file exists but contains invalid
            data. Missing files are not errors — the defaults take over.
    """
    cfg_path = path if path is not None else resolve_config_path()
    default_data_dir = _platform_data_dir()

    if cfg_path is None:
        return Config(
            data_dir=default_data_dir,
            topic=Topic(),
            limits=DEFAULT_LIMITS,
            config_path=None,
        )

    try:
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as e:
        raise ValidationError(f"Could not read config file {cfg_path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ValidationError(f"Invalid TOML in {cfg_path}: {e}") from e

    if not isinstance(raw, dict):
        raise ValidationError(f"Config file {cfg_path} must contain a TOML table at the top level")

    # Limits must be parsed before topic because topic validators consult
    # the limits for their size caps.
    limits_block = raw.get("limits", {})
    if not isinstance(limits_block, dict):
        raise ValidationError(f"[limits] must be a table, got {type(limits_block).__name__}")
    limits = _parse_limits_block(limits_block)

    topic_block = raw.get("topic", {})
    if not isinstance(topic_block, dict):
        raise ValidationError(f"[topic] must be a table, got {type(topic_block).__name__}")
    topic = _parse_topic_block(topic_block, limits=limits)

    server_block = raw.get("server", {})
    if not isinstance(server_block, dict):
        raise ValidationError(f"[server] must be a table, got {type(server_block).__name__}")
    data_dir = _parse_server_block(server_block, default_data_dir)

    return Config(
        data_dir=data_dir,
        topic=topic,
        limits=limits,
        config_path=cfg_path,
    )
