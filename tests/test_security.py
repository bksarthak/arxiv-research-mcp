"""Tests for src/arxiv_research_mcp/security.py input validators."""

from __future__ import annotations

import pytest

from arxiv_research_mcp.security import (
    DEFAULT_LIMITS,
    Limits,
    ValidationError,
    validate_arxiv_id,
    validate_arxiv_id_list,
    validate_category,
    validate_category_list,
    validate_keyword,
    validate_keyword_list,
    validate_positive_bounded_int,
    validate_rubric_focus,
    validate_verdict,
    validate_verdict_list,
    validate_window_days,
)


class TestValidateArxivId:
    def test_new_format_without_version(self) -> None:
        assert validate_arxiv_id("2404.12345") == "2404.12345"

    def test_new_format_with_version(self) -> None:
        assert validate_arxiv_id("2404.12345v3") == "2404.12345v3"

    def test_new_format_four_digit_paper_number(self) -> None:
        assert validate_arxiv_id("0704.1234") == "0704.1234"

    def test_old_format(self) -> None:
        assert validate_arxiv_id("cs.CR/0601001") == "cs.CR/0601001"

    def test_old_format_with_version(self) -> None:
        assert validate_arxiv_id("math.GT/0312214v2") == "math.GT/0312214v2"

    def test_whitespace_is_stripped(self) -> None:
        assert validate_arxiv_id("  2404.12345  ") == "2404.12345"

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            validate_arxiv_id("")

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not be empty"):
            validate_arxiv_id("   ")

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a string"):
            validate_arxiv_id(12345)  # type: ignore[arg-type]

    def test_garbage_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            validate_arxiv_id("not an arxiv id")

    def test_sql_injection_rejected(self) -> None:
        """Defensive: obvious injection attempts are rejected at the regex."""
        with pytest.raises(ValidationError):
            validate_arxiv_id("2404.12345'; DROP TABLE papers; --")

    def test_path_traversal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_arxiv_id("../../../etc/passwd")


class TestValidateCategory:
    def test_primary_category(self) -> None:
        assert validate_category("cs") == "cs"

    def test_subcategory(self) -> None:
        assert validate_category("cs.CR") == "cs.CR"

    def test_math_subcategory(self) -> None:
        assert validate_category("math.GT") == "math.GT"

    def test_stat_ml(self) -> None:
        assert validate_category("stat.ML") == "stat.ML"

    def test_hyphenated_primary(self) -> None:
        assert validate_category("q-bio.QM") == "q-bio.QM"

    def test_hep_th(self) -> None:
        assert validate_category("hep-th") == "hep-th"

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_category("")

    def test_uppercase_primary_rejected(self) -> None:
        """Primary category must be lowercase."""
        with pytest.raises(ValidationError):
            validate_category("CS.CR")

    def test_lowercase_subcategory_rejected(self) -> None:
        """Subcategory suffix must be uppercase two letters."""
        with pytest.raises(ValidationError):
            validate_category("cs.cr")

    def test_injection_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_category("cs.CR; DROP")


class TestValidateKeyword:
    def test_simple(self) -> None:
        assert validate_keyword("prompt injection") == "prompt injection"

    def test_whitespace_stripped(self) -> None:
        assert validate_keyword("  llm  ") == "llm"

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_keyword("")

    def test_too_long_rejected(self) -> None:
        limits = Limits(max_keyword_length=10)
        with pytest.raises(ValidationError, match="exceeds"):
            validate_keyword("this keyword is way too long", limits=limits)

    def test_at_limit_accepted(self) -> None:
        limits = Limits(max_keyword_length=10)
        assert validate_keyword("0123456789", limits=limits) == "0123456789"


class TestValidateWindowDays:
    def test_normal(self) -> None:
        assert validate_window_days(7) == 7

    def test_one_day_minimum(self) -> None:
        assert validate_window_days(1) == 1

    def test_at_maximum(self) -> None:
        assert validate_window_days(DEFAULT_LIMITS.max_window_days) == (
            DEFAULT_LIMITS.max_window_days
        )

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match=">= 1"):
            validate_window_days(0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_window_days(-1)

    def test_exceeding_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exceeds"):
            validate_window_days(DEFAULT_LIMITS.max_window_days + 1)

    def test_float_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be an int"):
            validate_window_days(7.5)  # type: ignore[arg-type]

    def test_bool_rejected(self) -> None:
        """Python bool is a subclass of int — we reject it explicitly
        because True/False as a day count is almost certainly a bug.
        """
        with pytest.raises(ValidationError, match="must be an int"):
            validate_window_days(True)


class TestValidatePositiveBoundedInt:
    def test_valid(self) -> None:
        assert validate_positive_bounded_int(5, name="x", maximum=10) == 5

    def test_at_minimum(self) -> None:
        assert validate_positive_bounded_int(1, name="x", maximum=10) == 1

    def test_at_maximum(self) -> None:
        assert validate_positive_bounded_int(10, name="x", maximum=10) == 10

    def test_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError, match=">= 1"):
            validate_positive_bounded_int(0, name="x", maximum=10)

    def test_custom_minimum(self) -> None:
        assert validate_positive_bounded_int(0, name="x", maximum=10, minimum=0) == 0

    def test_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exceeds"):
            validate_positive_bounded_int(11, name="x", maximum=10)

    def test_error_includes_name(self) -> None:
        with pytest.raises(ValidationError, match="my_param"):
            validate_positive_bounded_int(100, name="my_param", maximum=10)


class TestValidateArxivIdList:
    def test_valid_list(self) -> None:
        result = validate_arxiv_id_list(["2404.12345", "2404.99999"])
        assert result == ["2404.12345", "2404.99999"]

    def test_empty_list_accepted(self) -> None:
        assert validate_arxiv_id_list([]) == []

    def test_size_cap_enforced(self) -> None:
        limits = Limits(max_arxiv_ids_per_call=2)
        with pytest.raises(ValidationError, match="exceeds"):
            validate_arxiv_id_list(["2404.1", "2404.2", "2404.3"], limits=limits)

    def test_invalid_element_rejected(self) -> None:
        with pytest.raises(ValidationError):
            validate_arxiv_id_list(["2404.12345", "garbage"])

    def test_non_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a list"):
            validate_arxiv_id_list("2404.12345")  # type: ignore[arg-type]


class TestValidateCategoryList:
    def test_valid(self) -> None:
        assert validate_category_list(["cs.CR", "cs.AI"]) == ["cs.CR", "cs.AI"]

    def test_empty_rejected(self) -> None:
        """An empty category list means 'query nothing' — not useful."""
        with pytest.raises(ValidationError, match="must not be empty"):
            validate_category_list([])

    def test_size_cap(self) -> None:
        limits = Limits(max_categories=2)
        with pytest.raises(ValidationError):
            validate_category_list(["cs.CR", "cs.AI", "cs.LG"], limits=limits)


class TestValidateKeywordList:
    def test_valid(self) -> None:
        assert validate_keyword_list(["llm", "jailbreak"]) == [
            "llm",
            "jailbreak",
        ]

    def test_empty_list_accepted(self) -> None:
        """Empty keywords means 'no prefiltering' — valid."""
        assert validate_keyword_list([]) == []

    def test_size_cap(self) -> None:
        limits = Limits(max_keywords=2)
        with pytest.raises(ValidationError):
            validate_keyword_list(["a", "b", "c"], limits=limits)


class TestValidateRubricFocus:
    def test_valid(self) -> None:
        assert validate_rubric_focus("Some focus") == "Some focus"

    def test_empty_accepted(self) -> None:
        assert validate_rubric_focus("") == ""

    def test_whitespace_stripped(self) -> None:
        assert validate_rubric_focus("  text  ") == "text"

    def test_too_long_rejected(self) -> None:
        limits = Limits(max_rubric_focus_chars=10)
        with pytest.raises(ValidationError, match="exceeds"):
            validate_rubric_focus("x" * 100, limits=limits)

    def test_non_string_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a string"):
            validate_rubric_focus(123)  # type: ignore[arg-type]


class TestValidateVerdict:
    def test_valid_verdict(self) -> None:
        v = validate_verdict({
            "arxiv_id": "2604.00001",
            "relevance_score": 8,
            "quality_score": 7,
            "summary": "Good paper",
            "project_angle": "Build it",
            "reasoning": "Strong eval",
        })
        assert v["arxiv_id"] == "2604.00001"
        assert v["relevance_score"] == 8
        assert v["quality_score"] == 7
        assert v["summary"] == "Good paper"

    def test_minimal_verdict(self) -> None:
        v = validate_verdict({
            "arxiv_id": "2604.00001",
            "relevance_score": 5,
            "quality_score": 3,
        })
        assert v["arxiv_id"] == "2604.00001"
        assert "summary" not in v

    def test_missing_required_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="missing required"):
            validate_verdict({"arxiv_id": "2604.00001", "relevance_score": 5})

    def test_invalid_arxiv_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="does not match"):
            validate_verdict({
                "arxiv_id": "garbage",
                "relevance_score": 5,
                "quality_score": 5,
            })

    def test_score_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be 0-10"):
            validate_verdict({
                "arxiv_id": "2604.00001",
                "relevance_score": 11,
                "quality_score": 5,
            })

    def test_score_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be 0-10"):
            validate_verdict({
                "arxiv_id": "2604.00001",
                "relevance_score": -1,
                "quality_score": 5,
            })

    def test_non_dict_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a dict"):
            validate_verdict("not a dict")

    def test_bool_score_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be an int"):
            validate_verdict({
                "arxiv_id": "2604.00001",
                "relevance_score": True,
                "quality_score": 5,
            })

    def test_optional_fields_truncated(self) -> None:
        v = validate_verdict({
            "arxiv_id": "2604.00001",
            "relevance_score": 5,
            "quality_score": 5,
            "summary": "x" * 5000,
        })
        assert len(str(v.get("summary", ""))) <= 1000


class TestValidateVerdictList:
    def test_valid_list(self) -> None:
        verdicts = validate_verdict_list([
            {"arxiv_id": "2604.00001", "relevance_score": 8, "quality_score": 7},
            {"arxiv_id": "2604.00002", "relevance_score": 5, "quality_score": 3},
        ])
        assert len(verdicts) == 2

    def test_exceeds_limit_rejected(self) -> None:
        limits = Limits(max_verdicts_per_call=1)
        with pytest.raises(ValidationError, match="exceeds"):
            validate_verdict_list(
                [
                    {"arxiv_id": "2604.00001", "relevance_score": 5, "quality_score": 5},
                    {"arxiv_id": "2604.00002", "relevance_score": 5, "quality_score": 5},
                ],
                limits=limits,
            )

    def test_non_list_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must be a list"):
            validate_verdict_list("not a list")  # type: ignore[arg-type]
