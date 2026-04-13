"""Tests for src/arxiv_research_mcp/config.py TOML loading + validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from arxiv_research_mcp.config import (
    CONFIG_ENV_VAR,
    Config,
    Topic,
    load_config,
    resolve_config_path,
)
from arxiv_research_mcp.security import ValidationError


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadConfigDefaults:
    def test_no_config_uses_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no config file and no env override, we should get a
        fully-defaulted Config — no crashes, sensible values.
        """
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        # Point HOME at a fresh temp dir to avoid hitting the real
        # user's ~/.config/arxiv-research-mcp.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        cfg = load_config()
        assert isinstance(cfg, Config)
        assert cfg.config_path is None
        assert cfg.topic.categories == ("cs.CR",)
        assert "llm" in cfg.topic.keywords

    def test_cursor_path_derived_from_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        cfg = load_config()
        assert cfg.cursor_path == cfg.data_dir / "cursor.json"


class TestLoadConfigFromFile:
    def test_valid_config_parses(self, tmp_path: Path) -> None:
        config_file = _write(
            tmp_path / "config.toml",
            """
            [topic]
            name = "ml-security"
            description = "ML security focus"
            categories = ["cs.CR", "cs.LG"]
            keywords = ["adversarial", "backdoor"]
            rubric_focus = "Test focus"
            """.strip(),
        )
        cfg = load_config(config_file)
        assert cfg.topic.name == "ml-security"
        assert cfg.topic.categories == ("cs.CR", "cs.LG")
        assert cfg.topic.keywords == ("adversarial", "backdoor")
        assert cfg.topic.rubric_focus == "Test focus"
        assert cfg.config_path == config_file

    def test_invalid_category_rejected(self, tmp_path: Path) -> None:
        config_file = _write(
            tmp_path / "config.toml",
            """
            [topic]
            categories = ["../../etc"]
            """.strip(),
        )
        with pytest.raises(ValidationError):
            load_config(config_file)

    def test_wrong_type_rejected(self, tmp_path: Path) -> None:
        config_file = _write(
            tmp_path / "config.toml",
            """
            [topic]
            categories = "cs.CR"
            """.strip(),
        )
        with pytest.raises(ValidationError, match="must be a list"):
            load_config(config_file)

    def test_invalid_toml_rejected(self, tmp_path: Path) -> None:
        config_file = _write(tmp_path / "config.toml", "not = valid ::: toml")
        with pytest.raises(ValidationError, match="Invalid TOML"):
            load_config(config_file)

    def test_unknown_limits_key_rejected(self, tmp_path: Path) -> None:
        """Typos in [limits] keys are dangerous — fail loud."""
        config_file = _write(
            tmp_path / "config.toml",
            """
            [limits]
            max_windwo_days = 30
            """.strip(),
        )
        with pytest.raises(ValidationError, match="unknown keys"):
            load_config(config_file)

    def test_limits_override(self, tmp_path: Path) -> None:
        config_file = _write(
            tmp_path / "config.toml",
            """
            [limits]
            max_window_days = 30
            max_keywords = 100
            """.strip(),
        )
        cfg = load_config(config_file)
        assert cfg.limits.max_window_days == 30
        assert cfg.limits.max_keywords == 100

    def test_partial_config_keeps_defaults(self, tmp_path: Path) -> None:
        """A config that only overrides [topic.name] should still have
        default categories, keywords, etc.
        """
        config_file = _write(
            tmp_path / "config.toml",
            """
            [topic]
            name = "custom"
            """.strip(),
        )
        cfg = load_config(config_file)
        assert cfg.topic.name == "custom"
        # Defaults preserved
        assert cfg.topic.categories == Topic().categories
        assert cfg.topic.keywords == Topic().keywords

    def test_data_dir_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        config_file = _write(
            tmp_path / "config.toml",
            """
            [server]
            data_dir = "~/my-state"
            """.strip(),
        )
        cfg = load_config(config_file)
        assert cfg.data_dir == (tmp_path / "my-state").resolve()


class TestResolveConfigPath:
    def test_env_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        explicit = _write(tmp_path / "custom.toml", "[topic]\nname = 'x'")
        monkeypatch.setenv(CONFIG_ENV_VAR, str(explicit))
        assert resolve_config_path() == explicit

    def test_env_override_missing_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the env-specified file doesn't exist, we ignore the env
        and fall back to the platform default (which also doesn't
        exist in this test, so we get None).
        """
        monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "nope.toml"))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
        assert resolve_config_path() is None

    def test_xdg_config_home_respected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)
        xdg = tmp_path / "xdg"
        pkg_dir = xdg / "arxiv-research-mcp"
        pkg_dir.mkdir(parents=True)
        cfg_file = _write(pkg_dir / "config.toml", "[topic]\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        monkeypatch.setenv("HOME", str(tmp_path))
        if sys.platform in ("linux", "linux2"):
            assert resolve_config_path() == cfg_file
