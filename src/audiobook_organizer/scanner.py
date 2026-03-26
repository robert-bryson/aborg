"""Scan source directories for audiobook files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .parser import AudiobookMeta, merge_meta, parse_audio_tags, parse_filename


@dataclass
class ScanResult:
    """A discovered audiobook file or directory with parsed metadata."""

    path: Path
    kind: str  # "archive", "audio_file", "audio_dir"
    meta: AudiobookMeta
    size: int  # total bytes


def scan_sources(cfg: Config) -> list[ScanResult]:
    """Walk all configured source directories and return discovered audiobooks."""
    results: list[ScanResult] = []
    seen: set[Path] = set()

    for src_dir in cfg.source_dirs:
        if not src_dir.exists():
            continue
        for entry in sorted(src_dir.iterdir()):
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)

            if entry.is_file():
                result = _check_file(entry, cfg)
                if result:
                    results.append(result)
            elif entry.is_dir():
                result = _check_dir(entry, cfg)
                if result:
                    results.append(result)

    return results


def _check_file(path: Path, cfg: Config) -> ScanResult | None:
    """Check if a single file is a recognizable audiobook."""
    ext = path.suffix.lower()
    size = path.stat().st_size

    if size < cfg.min_file_size:
        return None

    if ext in cfg.archive_extensions:
        meta = parse_filename(path.stem, cfg.filename_patterns)
        meta.source_path = path
        return ScanResult(path=path, kind="archive", meta=meta, size=size)

    if ext in cfg.audio_extensions:
        file_meta = parse_filename(path.stem, cfg.filename_patterns)
        tag_meta = parse_audio_tags(path)
        meta = merge_meta(tag_meta, file_meta)
        meta.source_path = path
        return ScanResult(path=path, kind="audio_file", meta=meta, size=size)

    return None


def _check_dir(path: Path, cfg: Config) -> ScanResult | None:
    """Check if a directory contains audiobook audio files."""
    all_exts = cfg.audio_extensions | cfg.companion_extensions
    audio_files: list[Path] = []
    total_size = 0

    for child in path.rglob("*"):
        if child.is_file() and child.suffix.lower() in all_exts:
            if child.suffix.lower() in cfg.audio_extensions:
                audio_files.append(child)
            total_size += child.stat().st_size

    if not audio_files:
        return None

    # Try to get metadata from the directory name first, then first audio file
    dir_meta = parse_filename(path.name, cfg.filename_patterns)
    first_audio_meta = parse_audio_tags(audio_files[0]) if audio_files else AudiobookMeta()
    meta = merge_meta(first_audio_meta, dir_meta)
    meta.source_path = path

    return ScanResult(path=path, kind="audio_dir", meta=meta, size=total_size)


def scan_collection(root: Path, cfg: Config) -> list[ScanResult]:
    """Scan an existing organized collection at *root* and return all audiobooks found."""
    results: list[ScanResult] = []
    if not root.exists():
        return results

    # Walk up to 3 levels deep: Author / [Series] / Title
    for author_dir in sorted(root.iterdir()):
        if not author_dir.is_dir() or author_dir.name.startswith("."):
            continue
        for sub in sorted(author_dir.iterdir()):
            if not sub.is_dir():
                continue
            # Could be a series dir or a title dir
            audio_in_sub = _count_audio(sub, cfg)
            if audio_in_sub > 0:
                # This is a title dir directly under author
                result = _scan_title_dir(sub, cfg, author=author_dir.name)
                if result:
                    results.append(result)
            else:
                # Could be a series dir — check children
                for title_dir in sorted(sub.iterdir()):
                    if title_dir.is_dir():
                        result = _scan_title_dir(
                            title_dir, cfg, author=author_dir.name, series=sub.name
                        )
                        if result:
                            results.append(result)
    return results


def _count_audio(directory: Path, cfg: Config) -> int:
    return sum(
        1 for f in directory.rglob("*") if f.is_file() and f.suffix.lower() in cfg.audio_extensions
    )


def _scan_title_dir(
    path: Path, cfg: Config, author: str = "", series: str | None = None
) -> ScanResult | None:
    audio_files = [
        f for f in path.rglob("*") if f.is_file() and f.suffix.lower() in cfg.audio_extensions
    ]
    if not audio_files:
        return None

    total_size = sum(f.stat().st_size for f in audio_files)
    tag_meta = parse_audio_tags(audio_files[0])
    dir_meta = parse_filename(path.name, cfg.filename_patterns)

    meta = merge_meta(tag_meta, dir_meta)
    if author:
        meta.author = author
    if series:
        meta.series = series
    meta.source_path = path

    return ScanResult(path=path, kind="audio_dir", meta=meta, size=total_size)
