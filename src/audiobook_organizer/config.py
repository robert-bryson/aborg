"""Configuration loading and defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("~/.aborg/config.yaml").expanduser()
DEFAULT_DESTINATION = "/mnt/n/media/audiobooks"

ARCHIVE_EXTS = frozenset({".zip", ".rar", ".7z"})
AUDIO_EXTS = frozenset({".m4b", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wma", ".aac"})
COMPANION_EXTS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".pdf",
        ".epub",
        ".nfo",
        ".cue",
        ".txt",
        ".opf",
    }
)

# Default filename parsing patterns (tried in order)
DEFAULT_PATTERNS: list[str] = [
    # Author - Series Book 1 - Title (Year) [Narrator]
    # Author - Series Book 1 - Title (Year) [Narrator]
    r"(?P<author>.+?) - (?P<series>.+?)\s*(?:Book|Vol\.?|Volume)\s*(?P<sequence>\d+)"
    r"\s*-\s*(?P<title>.+?)(?:\s*\((?P<year>\d{4})\))?(?:\s*\[(?P<narrator>.+?)\])?$",
    # Author - Title (Year) [Narrator]
    r"(?P<author>.+?) - (?P<title>.+?)(?:\s*\((?P<year>\d{4})\))?(?:\s*\[(?P<narrator>.+?)\])?$",
    # Title - Author (Year)
    r"(?P<title>.+?) - (?P<author>.+?)(?:\s*\((?P<year>\d{4})\))?$",
    # Author_Title
    r"(?P<author>[^_]+)_(?P<title>.+)$",
]

MIN_FILE_SIZE = 1_048_576  # 1 MB


@dataclass
class Config:
    source_dirs: list[Path] = field(default_factory=lambda: [Path("/mnt/c/Users/rsmbr/Downloads")])
    destination: Path = field(default_factory=lambda: Path(DEFAULT_DESTINATION))
    archive_extensions: frozenset[str] = ARCHIVE_EXTS
    audio_extensions: frozenset[str] = AUDIO_EXTS
    companion_extensions: frozenset[str] = COMPANION_EXTS
    auto_extract: bool = True
    delete_after_extract: bool = False
    filename_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    min_file_size: int = MIN_FILE_SIZE
    move_log: Path = field(default_factory=lambda: Path("~/.aborg/moves.log").expanduser())

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config from YAML file, falling back to defaults for missing keys."""
        cfg_path = path or DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            return cls()

        with cfg_path.open() as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        kwargs: dict[str, Any] = {}

        if "source_dirs" in raw:
            kwargs["source_dirs"] = [Path(d).expanduser() for d in raw["source_dirs"]]
        if "destination" in raw:
            kwargs["destination"] = Path(raw["destination"]).expanduser()
        if "archive_extensions" in raw:
            kwargs["archive_extensions"] = frozenset(raw["archive_extensions"])
        if "audio_extensions" in raw:
            kwargs["audio_extensions"] = frozenset(raw["audio_extensions"])
        if "companion_extensions" in raw:
            kwargs["companion_extensions"] = frozenset(raw["companion_extensions"])
        if "auto_extract" in raw:
            kwargs["auto_extract"] = bool(raw["auto_extract"])
        if "delete_after_extract" in raw:
            kwargs["delete_after_extract"] = bool(raw["delete_after_extract"])
        if "filename_patterns" in raw:
            kwargs["filename_patterns"] = raw["filename_patterns"]
        if "min_file_size" in raw:
            kwargs["min_file_size"] = int(raw["min_file_size"])
        if "move_log" in raw:
            kwargs["move_log"] = Path(raw["move_log"]).expanduser()

        return cls(**kwargs)

    def save(self, path: Path | None = None) -> None:
        """Persist current config to YAML."""
        cfg_path = path or DEFAULT_CONFIG_PATH
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "source_dirs": [str(d) for d in self.source_dirs],
            "destination": str(self.destination),
            "archive_extensions": sorted(self.archive_extensions),
            "audio_extensions": sorted(self.audio_extensions),
            "auto_extract": self.auto_extract,
            "delete_after_extract": self.delete_after_extract,
            "filename_patterns": self.filename_patterns,
            "min_file_size": self.min_file_size,
            "move_log": str(self.move_log),
        }
        with cfg_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
