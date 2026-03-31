"""Configuration loading."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path("~/.aborg/config.yaml").expanduser()


@dataclass
class Config:
    source_dirs: list[Path] = field(default_factory=list)
    destination: Path = field(default_factory=lambda: Path())
    archive_extensions: frozenset[str] = field(default_factory=frozenset)
    audio_extensions: frozenset[str] = field(default_factory=frozenset)
    companion_extensions: frozenset[str] = field(default_factory=frozenset)
    auto_extract: bool = False
    delete_after_extract: bool = False
    filename_patterns: list[str] = field(default_factory=list)
    min_file_size: int = 0
    move_log: Path = field(default_factory=lambda: Path())

    # Libby / odmpy integration
    libby_settings: Path = field(default_factory=lambda: Path())
    libby_merge: bool = False
    libby_merge_format: str = ""
    libby_chapters: bool = False
    libby_keep_cover: bool = False
    libby_book_folder_format: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config from YAML file."""
        cfg_path = path or DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {cfg_path}\n"
                f"  Create one by copying config.example.yaml to {DEFAULT_CONFIG_PATH}"
            )

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

        # Libby settings
        libby = raw.get("libby", {})
        if isinstance(libby, dict):
            if "settings_folder" in libby:
                kwargs["libby_settings"] = Path(libby["settings_folder"]).expanduser()
            if "merge" in libby:
                kwargs["libby_merge"] = bool(libby["merge"])
            if "merge_format" in libby:
                kwargs["libby_merge_format"] = str(libby["merge_format"])
            if "chapters" in libby:
                kwargs["libby_chapters"] = bool(libby["chapters"])
            if "keep_cover" in libby:
                kwargs["libby_keep_cover"] = bool(libby["keep_cover"])
            if "book_folder_format" in libby:
                kwargs["libby_book_folder_format"] = str(libby["book_folder_format"])

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
            "libby": {
                "settings_folder": str(self.libby_settings),
                "merge": self.libby_merge,
                "merge_format": self.libby_merge_format,
                "chapters": self.libby_chapters,
                "keep_cover": self.libby_keep_cover,
                "book_folder_format": self.libby_book_folder_format,
            },
        }
        with cfg_path.open("w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
