"""Shared test fixtures."""

from audiobook_organizer.config import Config

# Standard extensions used across tests
AUDIO_EXTS = frozenset({".m4b", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wma", ".aac"})
ARCHIVE_EXTS = frozenset({".zip", ".rar", ".7z"})
COMPANION_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".pdf", ".epub", ".nfo", ".cue", ".txt", ".opf"}
)
PATTERNS = list(Config.DEFAULT_PATTERNS)


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
