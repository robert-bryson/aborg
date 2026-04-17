"""Shared test fixtures."""

import pytest

from audiobook_organizer.config import Config

# Standard extensions used across tests — derive from Config.default() to stay DRY
_DEFAULTS = Config.default()
AUDIO_EXTS = _DEFAULTS.audio_extensions
ARCHIVE_EXTS = _DEFAULTS.archive_extensions
COMPANION_EXTS = _DEFAULTS.companion_extensions
PATTERNS = list(_DEFAULTS.filename_patterns)


def make_cfg(**overrides) -> Config:
    """Build a Config with standard test extensions pre-filled."""
    defaults = dict(
        audio_extensions=AUDIO_EXTS,
        archive_extensions=ARCHIVE_EXTS,
        companion_extensions=COMPANION_EXTS,
        filename_patterns=list(PATTERNS),
        min_file_size=100,
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture()
def tmp_cfg(tmp_path):
    """Write a minimal config.yaml and return its path as a string."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "source_dirs: []\ndestination: /tmp/aborg-dest\nmove_log: /tmp/aborg-moves.log\n"
    )
    return str(cfg_file)
