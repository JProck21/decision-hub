"""Tests for dhub.cli.config -- CLI configuration management."""

import json

import click
import pytest

from dhub.cli.config import CliConfig, load_config, save_config


class TestLoadConfig:
    """load_config should handle missing, valid, and corrupted config files."""

    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        """Missing config file returns default CliConfig."""
        monkeypatch.setattr("dhub.cli.config.CONFIG_DIR", tmp_path)
        monkeypatch.setenv("DHUB_ENV", "dev")

        config = load_config()

        assert config.token is None
        assert "dev" in config.api_url

    def test_loads_valid_config(self, tmp_path, monkeypatch):
        """Valid JSON config file is loaded correctly."""
        monkeypatch.setattr("dhub.cli.config.CONFIG_DIR", tmp_path)
        monkeypatch.setenv("DHUB_ENV", "dev")

        config_path = tmp_path / "config.dev.json"
        config_path.write_text(
            json.dumps({"api_url": "https://example.com", "token": "tok123"})
        )

        config = load_config()

        assert config.api_url == "https://example.com"
        assert config.token == "tok123"

    def test_corrupted_json_exits_gracefully(self, tmp_path, monkeypatch):
        """Corrupted JSON should exit with code 1, not crash with a traceback."""
        monkeypatch.setattr("dhub.cli.config.CONFIG_DIR", tmp_path)
        monkeypatch.setenv("DHUB_ENV", "dev")

        config_path = tmp_path / "config.dev.json"
        config_path.write_text("{invalid json!!")

        with pytest.raises(click.exceptions.Exit):
            load_config()

    def test_empty_file_exits_gracefully(self, tmp_path, monkeypatch):
        """Empty config file should exit with code 1."""
        monkeypatch.setattr("dhub.cli.config.CONFIG_DIR", tmp_path)
        monkeypatch.setenv("DHUB_ENV", "dev")

        config_path = tmp_path / "config.dev.json"
        config_path.write_text("")

        with pytest.raises(click.exceptions.Exit):
            load_config()


class TestSaveConfig:
    """save_config should persist config to the correct env-specific file."""

    def test_round_trip(self, tmp_path, monkeypatch):
        """Saved config should be loadable."""
        monkeypatch.setattr("dhub.cli.config.CONFIG_DIR", tmp_path)
        monkeypatch.setenv("DHUB_ENV", "dev")

        original = CliConfig(api_url="https://test.example.com", token="secret")
        save_config(original)

        loaded = load_config()

        assert loaded.api_url == original.api_url
        assert loaded.token == original.token
