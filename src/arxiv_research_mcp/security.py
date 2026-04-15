"""Input validators for the MCP server boundary.

Every argument that crosses the MCP tool boundary — arXiv IDs, category
codes, integer windows, list sizes — passes through a validator here
before any I/O or business logic runs. Invalid inputs raise
``ValidationError`` and the tool returns a structured error to the client.

The goal is defense in depth against malformed or adversarial inputs:

- Regex-anchored matches for arXiv IDs and category codes (reject first,
  reject hard, never try to fix).
- Hard upper bounds on integer parameters and collection sizes (prevents
  resource exhaustion and runaway loops).
- A single ``Limits`` dataclass holds every cap so it's trivially
  auditable and overridable from config.

Design principle: the MCP server NEVER constructs a URL, a filesystem
path, or a subprocess argument from a raw tool argument. It validates
first, then uses the validated value. See SECURITY.md for the full
threat model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final


# ─────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────
class ValidationError(ValueError):
    """Raised when an MCP tool argument fails validation.

    Subclasses ``ValueError`` so clients and tests can catch either. The
    message is considered safe to surface to the MCP client — it does
    not include any stack traces, filesystem paths, or internal state.
    """


# ─────────────────────────────────────────────────────────────────────────
# Limits — hard caps enforced at the boundary
# ─────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Limits:
    """Hard upper bounds on every user-controlled parameter.

    These exist to prevent pathological inputs (deliberate or accidental)
    from causing runaway memory, CPU, or network usage. Every value can
    be overridden via the ``[limits]`` block in the user's config, but
    the defaults here are already generous for normal use.
    """

    max_window_days: int = 365
    max_results_per_page: int = 500
    max_pages_per_fetch: int = 10
    max_keywords: int = 500
    max_keyword_length: int = 128
    max_categories: int = 20
    max_arxiv_ids_per_call: int = 1000
    max_rubric_focus_chars: int = 10_000
    max_verdicts_per_call: int = 1000
    verdict_cache_ttl_days: int = 7


#: Default limits used when the user config doesn't override them.
DEFAULT_LIMITS: Final[Limits] = Limits()


# ─────────────────────────────────────────────────────────────────────────
# Regular expressions
# ─────────────────────────────────────────────────────────────────────────
# arXiv new-format identifier: YYMM.NNNNN (4- or 5-digit paper number)
# with optional version suffix vN. Examples: "2404.12345", "2404.12345v3".
_ARXIV_ID_NEW_RE: Final[re.Pattern[str]] = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")

# arXiv old-format identifier: category/YYMMNNN with optional version.
# Examples: "cs.CR/0601001", "math.GT/0312214v2".
_ARXIV_ID_OLD_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?$")

# arXiv category code: primary letters, optional dot-suffix of two capital
# letters. Examples: "cs.CR", "math.GT", "stat.ML", "q-bio.QM", "hep-th".
_CATEGORY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z\-]*(\.[A-Z]{2})?$")


# ─────────────────────────────────────────────────────────────────────────
# String validators
# ─────────────────────────────────────────────────────────────────────────
def validate_arxiv_id(value: str) -> str:
    """Validate a single arXiv identifier.

    Accepts both the new format (``2404.12345``, optionally with ``v3``)
    and the pre-2007 format (``cs.CR/0601001``). Strips surrounding
    whitespace before matching.

    Args:
        value: Candidate arXiv ID.

    Returns:
        The stripped, validated ID (unchanged otherwise).

    Raises:
        ValidationError: If ``value`` is empty, not a string, or does
            not match either format.
    """
    if not isinstance(value, str):
        raise ValidationError(f"arXiv ID must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValidationError("arXiv ID must not be empty")
    if _ARXIV_ID_NEW_RE.match(stripped) or _ARXIV_ID_OLD_RE.match(stripped):
        return stripped
    raise ValidationError(
        f"arXiv ID {stripped!r} does not match either the new-format "
        f"(YYMM.NNNNN) or old-format (category/YYMMNNN) pattern"
    )


def validate_category(value: str) -> str:
    """Validate a single arXiv category code.

    Accepts primary categories (``cs``, ``math``) and sub-categories
    (``cs.CR``, ``math.GT``, ``stat.ML``, ``q-bio.QM``).

    Args:
        value: Candidate category code.

    Returns:
        The stripped, validated category (unchanged otherwise).

    Raises:
        ValidationError: If ``value`` is empty, not a string, or does
            not match the category pattern.
    """
    if not isinstance(value, str):
        raise ValidationError(f"Category must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValidationError("Category must not be empty")
    if not _CATEGORY_RE.match(stripped):
        raise ValidationError(
            f"Category {stripped!r} does not match the arXiv category "
            f"pattern (e.g. 'cs.CR', 'math.GT', 'stat.ML')"
        )
    return stripped


def validate_keyword(value: str, limits: Limits = DEFAULT_LIMITS) -> str:
    """Validate a single prefilter keyword.

    Keywords are free-form substrings, but we still enforce a length cap
    to prevent abuse (e.g. a 10 MB "keyword" used as a memory exhaustion
    vector). Leading/trailing whitespace is stripped; internal whitespace
    is preserved.

    Args:
        value: Candidate keyword.
        limits: Effective limits (defaults to the frozen defaults).

    Returns:
        The stripped, validated keyword.

    Raises:
        ValidationError: If ``value`` is empty or exceeds ``max_keyword_length``.
    """
    if not isinstance(value, str):
        raise ValidationError(f"Keyword must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise ValidationError("Keyword must not be empty")
    if len(stripped) > limits.max_keyword_length:
        raise ValidationError(
            f"Keyword length {len(stripped)} exceeds max_keyword_length={limits.max_keyword_length}"
        )
    return stripped


# ─────────────────────────────────────────────────────────────────────────
# Numeric + collection validators
# ─────────────────────────────────────────────────────────────────────────
def validate_window_days(
    value: int,
    limits: Limits = DEFAULT_LIMITS,
) -> int:
    """Validate a lookback-window parameter in days.

    Args:
        value: Candidate window in days.
        limits: Effective limits.

    Returns:
        The validated integer.

    Raises:
        ValidationError: If not a positive int or above ``max_window_days``.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"window_days must be an int, got {type(value).__name__}")
    if value < 1:
        raise ValidationError(f"window_days must be >= 1, got {value}")
    if value > limits.max_window_days:
        raise ValidationError(
            f"window_days={value} exceeds max_window_days={limits.max_window_days}"
        )
    return value


def validate_positive_bounded_int(
    value: int,
    *,
    name: str,
    maximum: int,
    minimum: int = 1,
) -> int:
    """Generic positive-integer validator with explicit bounds.

    Used for per-call overrides of ``max_results_per_page``,
    ``max_pages_per_fetch``, etc. The parameter name is passed in so
    error messages are specific.

    Args:
        value: Candidate integer.
        name: Name of the parameter (for error messages).
        maximum: Upper bound (inclusive).
        minimum: Lower bound (inclusive). Defaults to 1.

    Returns:
        The validated integer.

    Raises:
        ValidationError: On any failure.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise ValidationError(f"{name} must be >= {minimum}, got {value}")
    if value > maximum:
        raise ValidationError(f"{name}={value} exceeds maximum={maximum}")
    return value


def validate_arxiv_id_list(
    values: list[str] | tuple[str, ...],
    limits: Limits = DEFAULT_LIMITS,
) -> list[str]:
    """Validate a list of arXiv IDs.

    Each element passes through ``validate_arxiv_id``; the list itself
    must not exceed ``max_arxiv_ids_per_call``. Duplicates are allowed
    (callers that care can de-dup downstream).

    Args:
        values: Candidate list of IDs.
        limits: Effective limits.

    Returns:
        The validated list (each element stripped).

    Raises:
        ValidationError: On any failure.
    """
    if not isinstance(values, (list, tuple)):
        raise ValidationError(f"arxiv_ids must be a list, got {type(values).__name__}")
    if len(values) > limits.max_arxiv_ids_per_call:
        raise ValidationError(
            f"arxiv_ids list length {len(values)} exceeds "
            f"max_arxiv_ids_per_call={limits.max_arxiv_ids_per_call}"
        )
    return [validate_arxiv_id(v) for v in values]


def validate_category_list(
    values: list[str] | tuple[str, ...],
    limits: Limits = DEFAULT_LIMITS,
) -> list[str]:
    """Validate a list of arXiv categories with size cap.

    Args:
        values: Candidate list of category codes.
        limits: Effective limits.

    Returns:
        The validated list.

    Raises:
        ValidationError: On any failure.
    """
    if not isinstance(values, (list, tuple)):
        raise ValidationError(f"categories must be a list, got {type(values).__name__}")
    if not values:
        raise ValidationError("categories list must not be empty")
    if len(values) > limits.max_categories:
        raise ValidationError(
            f"categories list length {len(values)} exceeds max_categories={limits.max_categories}"
        )
    return [validate_category(v) for v in values]


def validate_keyword_list(
    values: list[str] | tuple[str, ...],
    limits: Limits = DEFAULT_LIMITS,
) -> list[str]:
    """Validate a list of prefilter keywords with size cap.

    An empty list is allowed and means "no prefiltering" — the pipeline
    treats it the same as the config omitting the keywords field.

    Args:
        values: Candidate list of keywords.
        limits: Effective limits.

    Returns:
        The validated list.

    Raises:
        ValidationError: On any failure.
    """
    if not isinstance(values, (list, tuple)):
        raise ValidationError(f"keywords must be a list, got {type(values).__name__}")
    if len(values) > limits.max_keywords:
        raise ValidationError(
            f"keywords list length {len(values)} exceeds max_keywords={limits.max_keywords}"
        )
    return [validate_keyword(v, limits=limits) for v in values]


def validate_rubric_focus(
    value: str,
    limits: Limits = DEFAULT_LIMITS,
) -> str:
    """Validate the free-text rubric-focus string.

    Length-capped but otherwise unchecked — the content is injected into
    the judge prompt verbatim, so it's the user's responsibility to write
    something sensible. Empty string is allowed.

    Args:
        value: Candidate rubric focus.
        limits: Effective limits.

    Returns:
        The stripped, validated string.

    Raises:
        ValidationError: If not a string or exceeds the size cap.
    """
    if not isinstance(value, str):
        raise ValidationError(f"rubric_focus must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if len(stripped) > limits.max_rubric_focus_chars:
        raise ValidationError(
            f"rubric_focus length {len(stripped)} exceeds "
            f"max_rubric_focus_chars={limits.max_rubric_focus_chars}"
        )
    return stripped


# ─────────────────────────────────────────────────────────────────────────
# Verdict validators
# ─────────────────────────────────────────────────────────────────────────
#: Required keys in every verdict dict submitted by the client.
_VERDICT_REQUIRED_KEYS: frozenset[str] = frozenset({"arxiv_id", "relevance_score", "quality_score"})

#: Optional keys allowed in a verdict dict — anything else is stripped.
_VERDICT_OPTIONAL_KEYS: frozenset[str] = frozenset({"summary", "project_angle", "reasoning"})


def validate_verdict(value: object) -> dict[str, object]:
    """Validate a single verdict dict from the client's LLM.

    Ensures ``arxiv_id`` is a valid arXiv ID, both scores are ints in
    ``[0, 10]``, and optional text fields are truncated to safe lengths.
    Unknown keys are silently dropped (forward compatibility).

    Args:
        value: Candidate verdict.

    Returns:
        The validated verdict dict with normalized types.

    Raises:
        ValidationError: On missing/invalid required fields.
    """
    if not isinstance(value, dict):
        raise ValidationError(f"verdict must be a dict, got {type(value).__name__}")

    missing = _VERDICT_REQUIRED_KEYS - set(value.keys())
    if missing:
        raise ValidationError(f"verdict missing required keys: {sorted(missing)}")

    arxiv_id = validate_arxiv_id(str(value["arxiv_id"]))

    def _score(key: str) -> int:
        raw = value[key]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValidationError(f"verdict.{key} must be an int, got {type(raw).__name__}")
        if not 0 <= raw <= 10:
            raise ValidationError(f"verdict.{key} must be 0-10, got {raw}")
        score: int = raw
        return score

    result: dict[str, object] = {
        "arxiv_id": arxiv_id,
        "relevance_score": _score("relevance_score"),
        "quality_score": _score("quality_score"),
    }
    for key in _VERDICT_OPTIONAL_KEYS:
        if key in value and isinstance(value[key], str):
            result[key] = str(value[key])[:1000]
    return result


def validate_verdict_list(
    values: list[object] | tuple[object, ...],
    limits: Limits = DEFAULT_LIMITS,
) -> list[dict[str, object]]:
    """Validate a list of verdict dicts with size cap.

    Args:
        values: Candidate list of verdicts.
        limits: Effective limits.

    Returns:
        The validated list.

    Raises:
        ValidationError: On any failure.
    """
    if not isinstance(values, (list, tuple)):
        raise ValidationError(f"verdicts must be a list, got {type(values).__name__}")
    if len(values) > limits.max_verdicts_per_call:
        raise ValidationError(
            f"verdicts list length {len(values)} exceeds "
            f"max_verdicts_per_call={limits.max_verdicts_per_call}"
        )
    return [validate_verdict(v) for v in values]
