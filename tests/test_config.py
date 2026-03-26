"""Tests for audiobook_organizer.config — configuration loading."""

from pathlib import Path

import yaml

from audiobook_organizer.config import DEFAULT_PATTERNS, Config


class TestConfigDefaults:
    def test_default_source_dirs(self):
        cfg = Config()
        assert len(cfg.source_dirs) == 1
        assert cfg.source_dirs[0] == Path("/mnt/c/Users/rsmbr/Downloads")

    def test_default_auto_extract(self):
        assert Config().auto_extract is True

    def test_default_delete_after_extract(self):
        assert Config().delete_after_extract is False

    def test_default_patterns(self):
        cfg = Config()
        assert cfg.filename_patterns == DEFAULT_PATTERNS


class TestConfigLoad:
    def test_load_missing_file_returns_defaults(self, tmp_path):
        cfg = Config.load(tmp_path / "nonexistent.yaml")
        assert cfg.auto_extract is True
        assert len(cfg.source_dirs) == 1

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
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("")
        cfg = Config.load(cfg_file)
        assert cfg.auto_extract is True  # defaults

    def test_partial_override(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(yaml.dump({"auto_extract": False}))
        cfg = Config.load(cfg_file)
        assert cfg.auto_extract is False
        assert cfg.delete_after_extract is False  # untouched


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
