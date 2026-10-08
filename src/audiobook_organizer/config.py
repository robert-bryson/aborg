"""Configuration loading."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from .persistence import atomic_write_text

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

    # Author name format: "last_first" (Austen, Jane) or "first_last" (Jane Austen)
    author_name_format: str = "last_first"

    # Libby / odmpy integration
    libby_settings: Path = field(default_factory=lambda: Path())
    libby_merge: bool = False
    libby_merge_format: str = "m4b"
    libby_chapters: bool = False
    libby_keep_cover: bool = False
    libby_book_folder_format: str = "%(Author)s - %(Title)s"

    # User-defined single-name → full-name overrides (merged with built-in table)
    known_authors: dict[str, str] = field(default_factory=dict)

    DEFAULT_PATTERNS: ClassVar[list[str]] = [
        # N - Title - Author - Year (e.g. "2 - Dune - Frank Herbert - 1965")
        # The year is required because the no-year form is inherently ambiguous.
        r"(?P<sequence>\d+)\s*-\s*(?P<title>.+)\s*-\s*(?P<author>[^\d]+?)"
        r"\s*-\s*(?P<year>\d{4})\s*$",
        # Author - Series Book N - Title (Year) [{Narrator}]  (float sequences like 0.2 supported)
        r"(?P<author>.+?) - (?P<series>.+?)\s*(?:Book|Vol\.?|Volume)\s*(?P<sequence>\d+(?:\.\d+)?)"
        r"\s*-\s*(?P<title>.+?)(?:\s*\((?P<year>\d{4})\))?(?:\s*[\[{](?P<narrator>[^\]}]+)[\]}])?$",
        # Author - Title - Series, Book N [{Narrator}]  (e.g. "Title - Teixcalaan Series, Book 2")
        r"(?P<author>.+?) - (?P<title>.+?)\s+-\s+(?P<series>.+?),\s*"
        r"(?:Book|Vol\.?|Volume)\s*(?P<sequence>\d+(?:\.\d+)?)"
        r"(?:\s*[\[{](?P<narrator>[^\]}]+)[\]}])?$",
        # Author - Title (Year) [{Narrator}]
        r"(?P<author>.+?) - (?P<title>.+?)"
        r"(?:\s*\((?P<year>\d{4})\))?(?:\s*[\[{](?P<narrator>[^\]}]+)[\]}])?$",
        # Title - Author (Year)
        r"(?P<title>.+?) - (?P<author>.+?)(?:\s*\((?P<year>\d{4})\))?$",
        # Series N Title  (e.g. "The Expanse 01 Leviathan Wakes", "The Expanse 02.5 Gods of Risk")
        # Requires 2+ words for the series name to avoid false matches like 'The 7 Habits'.
        r"(?P<series>(?:[^\W\d_]+\s+){2,}?)(?P<sequence>\d+(?:\.\d+)?)\s+(?P<title>.+?)"
        r"(?:\s*\((?P<year>\d{4})\))?(?:\s*[\[{](?P<narrator>[^\]}]+)[\]}])?$",
        # Author_Title
        r"(?P<author>[^_]+)_(?P<title>.+)$",
    ]

    @classmethod
    def default(cls) -> Config:
        """Return a Config with sensible defaults (paths left empty for user to fill in)."""
        return cls(
            archive_extensions=frozenset((".zip", ".rar", ".7z")),
            audio_extensions=frozenset(
                (".m4b", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wma", ".aac")
            ),
            companion_extensions=frozenset(
                (".jpg", ".jpeg", ".png", ".pdf", ".epub", ".nfo", ".cue", ".txt", ".opf")
            ),
            auto_extract=True,
            delete_after_extract=False,
            filename_patterns=list(cls.DEFAULT_PATTERNS),
            min_file_size=1_048_576,
            move_log=DEFAULT_CONFIG_PATH.parent / "moves.log",
            libby_settings=DEFAULT_CONFIG_PATH.parent / "libby",
            libby_merge=False,
            libby_merge_format="m4b",
            libby_chapters=True,
            libby_keep_cover=True,
            libby_book_folder_format="%(Author)s - %(Title)s",
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Load config from YAML file."""
        cfg_path = (path or DEFAULT_CONFIG_PATH).expanduser()
        if not cfg_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {cfg_path}\n"
                f"  Create one by copying config.example.yaml to {DEFAULT_CONFIG_PATH}"
            )

        with cfg_path.open(encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
        raw = _mapping({} if loaded is None else loaded, "config")
        cfg = cls.default()

        if "source_dirs" in raw:
            cfg.source_dirs = list(
                dict.fromkeys(
                    Path(value).expanduser()
                    for value in _string_list(raw["source_dirs"], "source_dirs")
                )
            )
        for key in ("destination", "move_log"):
            if key in raw:
                setattr(cfg, key, Path(_string(raw[key], key)).expanduser())
        for key in ("archive_extensions", "audio_extensions", "companion_extensions"):
            if key in raw:
                extensions = _string_list(raw[key], key)
                if any(not ext.startswith(".") or "/" in ext or "\\" in ext for ext in extensions):
                    raise ValueError(f"{key} must contain file extensions that start with a dot")
                setattr(cfg, key, frozenset(ext.lower() for ext in extensions))
        for key in ("auto_extract", "delete_after_extract"):
            if key in raw:
                setattr(cfg, key, _boolean(raw[key], key))
        if "filename_patterns" in raw:
            cfg.filename_patterns = _string_list(raw["filename_patterns"], "filename_patterns")
            for index, pattern in enumerate(cfg.filename_patterns, 1):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"filename_patterns entry {index} is invalid: {exc}") from exc
        if "min_file_size" in raw:
            value = raw["min_file_size"]
            if type(value) is not int or value < 0:
                raise ValueError("min_file_size must be a nonnegative integer in bytes")
            cfg.min_file_size = value
        if "author_name_format" in raw:
            value = _string(raw["author_name_format"], "author_name_format").strip().lower()
            if value not in {"last_first", "first_last"}:
                raise ValueError("author_name_format must be last_first or first_last")
            cfg.author_name_format = value
        if "known_authors" in raw:
            aliases = _mapping(raw["known_authors"], "known_authors")
            if any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in aliases.items()
            ):
                raise ValueError("known_authors must map author names to author names")
            cfg.known_authors = {
                key.strip().casefold(): value.strip()
                for key, value in aliases.items()
                if key.strip() and value.strip()
            }

        if "libby" in raw:
            libby = _mapping(raw["libby"], "libby")
            if "settings_folder" in libby:
                cfg.libby_settings = Path(
                    _string(libby["settings_folder"], "libby.settings_folder")
                ).expanduser()
            for key in ("merge", "chapters", "keep_cover"):
                if key in libby:
                    setattr(cfg, f"libby_{key}", _boolean(libby[key], f"libby.{key}"))
            if "merge_format" in libby:
                value = _string(libby["merge_format"], "libby.merge_format")
                if value not in {"mp3", "m4b"}:
                    raise ValueError("libby.merge_format must be mp3 or m4b")
                cfg.libby_merge_format = value
            if "book_folder_format" in libby:
                cfg.libby_book_folder_format = _string(
                    libby["book_folder_format"], "libby.book_folder_format"
                )

        return cfg

    def save(self, path: Path | None = None) -> None:
        """Persist current config to YAML."""
        cfg_path = (path or DEFAULT_CONFIG_PATH).expanduser()
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "source_dirs": [str(d) for d in self.source_dirs],
            "destination": str(self.destination),
            "archive_extensions": sorted(self.archive_extensions),
            "audio_extensions": sorted(self.audio_extensions),
            "companion_extensions": sorted(self.companion_extensions),
            "auto_extract": self.auto_extract,
            "delete_after_extract": self.delete_after_extract,
            "filename_patterns": self.filename_patterns,
            "min_file_size": self.min_file_size,
            "move_log": str(self.move_log),
            "author_name_format": self.author_name_format,
            # only write known_authors when non-empty to keep default configs clean
            **({"known_authors": dict(self.known_authors)} if self.known_authors else {}),
            "libby": {
                "settings_folder": str(self.libby_settings),
                "merge": self.libby_merge,
                "merge_format": self.libby_merge_format,
                "chapters": self.libby_chapters,
                "keep_cover": self.libby_keep_cover,
                "book_folder_format": self.libby_book_folder_format,
            },
        }
        atomic_write_text(
            cfg_path,
            yaml.safe_dump(
                data,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            ),
        )


def _mapping(value: Any, key: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a YAML mapping")
    return value


def _string(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a nonempty string")
    return value


def _string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list of strings")
    return [_string(item, key) for item in value]


def _boolean(value: Any, key: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{key} must be true or false without quotation marks")
    return value
