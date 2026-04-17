"""Tests for audiobook_organizer.config — configuration loading."""

from pathlib import Path

import pytest
import yaml

from audiobook_organizer.config import Config


class TestConfigDefaults:
    """Bare Config() should have neutral zero-values (no hardcoded user data)."""

    def test_default_source_dirs_empty(self):
        cfg = Config()
        assert cfg.source_dirs == []

    def test_default_auto_extract(self):
        assert Config().auto_extract is False

    def test_default_delete_after_extract(self):
        assert Config().delete_after_extract is False

    def test_default_patterns_empty(self):
        cfg = Config()
        assert cfg.filename_patterns == []


class TestConfigLoad:
    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            Config.load(tmp_path / "nonexistent.yaml")

    def test_load_from_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            yaml.dump(
                {
                    "source_dirs": [str(tmp_path / "audiobooks")],
                    "destination": "/mnt/nas/audiobooks",
                    "auto_extract": False,
                    "min_file_size": 500,
                }
            )
        )
        cfg = Config.load(cfg_file)
        assert cfg.source_dirs == [tmp_path / "audiobooks"]
        assert cfg.destination == Path("/mnt/nas/audiobooks")
        assert cfg.auto_extract is False
        assert cfg.min_file_size == 500

    def test_load_empty_yaml(self, tmp_path):
        """An empty config file should still produce sensible defaults."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("")
        cfg = Config.load(cfg_file)
        assert cfg.auto_extract is True  # sensible default
        assert cfg.audio_extensions  # should have default extensions
        assert cfg.filename_patterns  # should have default patterns

    def test_load_invalid_yaml_raises(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("invalid: yaml: [[[")
        with pytest.raises(yaml.YAMLError):
            Config.load(cfg_file)

    def test_partial_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"auto_extract": False}))
        cfg = Config.load(cfg_file)
        assert cfg.auto_extract is False
        assert cfg.delete_after_extract is False  # untouched

    def test_load_author_name_format(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"author_name_format": "first_last"}))
        cfg = Config.load(cfg_file)
        assert cfg.author_name_format == "first_last"

    def test_default_author_name_format_is_last_first(self):
        assert Config().author_name_format == "last_first"

    def test_invalid_author_name_format_ignored(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"author_name_format": "bogus"}))
        cfg = Config.load(cfg_file)
        assert cfg.author_name_format == "last_first"  # default preserved

    def test_source_dirs_deduplicated_at_load(self, tmp_path):
        """Duplicate source_dirs should be removed when loading config."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"source_dirs": ["/a/b", "/c/d", "/a/b", "/c/d", "/a/b"]}))
        cfg = Config.load(cfg_file)
        assert len(cfg.source_dirs) == 2
        assert cfg.source_dirs == [Path("/a/b"), Path("/c/d")]


class TestConfigSave:
    def test_save_creates_file(self, tmp_path):
        cfg = Config(destination=Path("/test/dest"))
        out = tmp_path / "sub" / "config.yaml"
        cfg.save(out)
        assert out.exists()

        loaded = yaml.safe_load(out.read_text())
        assert loaded["destination"] == "/test/dest"

    def test_roundtrip(self, tmp_path):
        original = Config(
            source_dirs=[Path("/a"), Path("/b")],
            auto_extract=False,
            min_file_size=42,
        )
        out = tmp_path / "config.yaml"
        original.save(out)
        loaded = Config.load(out)
        assert loaded.auto_extract is False
        assert loaded.min_file_size == 42
        assert len(loaded.source_dirs) == 2

    def test_roundtrip_all_fields(self, tmp_path):
        """Save and load should preserve companion_extensions and author_name_format."""
        original = Config(
            source_dirs=[Path("/src")],
            destination=Path("/dest"),
            companion_extensions=frozenset({".pdf", ".epub"}),
            author_name_format="first_last",
        )
        out = tmp_path / "config.yaml"
        original.save(out)
        loaded = Config.load(out)
        assert loaded.companion_extensions == frozenset({".pdf", ".epub"})
        assert loaded.author_name_format == "first_last"

    def test_minimal_config_gets_defaults(self, tmp_path):
        """A config with just source_dirs and destination should still be functional."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"source_dirs": ["/src"], "destination": "/dest"}))
        cfg = Config.load(cfg_file)
        assert cfg.audio_extensions  # has default audio extensions
        assert cfg.archive_extensions  # has default archive extensions
        assert cfg.filename_patterns  # has default patterns
        assert cfg.min_file_size > 0  # has a sane minimum
