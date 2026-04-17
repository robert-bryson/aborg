"""Scan source directories for audiobook files."""

from __future__ import annotations

import os
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from .cache import ScanCache

    ProgressCallback = Callable[[str], None]
    HitCallback = Callable[["ScanResult"], None]

import re

from .config import Config
from .parser import (
    AudiobookMeta,
    looks_like_author,
    merge_meta,
    parse_audio_tags,
    parse_filename,
    parse_metadata_json,
    parse_metadata_json_from_zip,
    parse_title_folder,
    strip_author_from_title,
)

# Archives below this size are almost certainly not audiobooks (50 MB).
MIN_ARCHIVE_SIZE = 50_000_000

# Filename stems that are clearly not audiobooks.
_JUNK_PREFIXES = (
    "sync",
    "takeout",
    "export",
    "photos",
    "backup",
    "driver",
    "asset-pack",
    "omnivore",
    "gpx",
    "routes",
)

# Regex to strip Windows download-duplicate suffixes like "(1)", " (2)" etc.
_DUP_SUFFIX_RE = re.compile(r"\s*\(\d+\)$")


def fold_accents(s: str) -> str:
    """Fold accented characters to their ASCII equivalents."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _normalize_dedup(s: str) -> str:
    """Normalize a string for deduplication (case + accent folding)."""
    return fold_accents(s.lower())


# Cover-art filenames recognised by Audiobookshelf.
COVER_NAMES = frozenset({"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png"})

# Top-level directories inside a collection that should never be treated as authors.
_IGNORED_AUTHOR_DIRS = frozenset({"_new", "_raw_inputs", "_downloads"})


@dataclass
class ScanResult:
    """A discovered audiobook file or directory with parsed metadata."""

    path: Path
    kind: str  # "archive", "audio_file", "audio_dir"
    meta: AudiobookMeta
    size: int  # total bytes
    has_cover: bool = False
    file_count: int = 0
    source_dir: Path | None = None
    tag_meta: AudiobookMeta | None = None  # raw tag-derived metadata (before merge)


@dataclass
class CollectionScan:
    """Result of a single-pass collection scan, including filesystem metadata."""

    items: list[ScanResult] = field(default_factory=list)
    empty_dirs: list[Path] = field(default_factory=list)
    flat_audio_files: list[Path] = field(default_factory=list)


def scan_sources(
    cfg: Config,
    *,
    on_progress: ProgressCallback | None = None,
    on_hit: HitCallback | None = None,
    cache: ScanCache | None = None,
) -> tuple[list[ScanResult], list[Path]]:
    """Walk all configured source directories and return discovered audiobooks.

    Returns a tuple of (results, missing_dirs).
    """
    results: list[ScanResult] = []
    seen: set[Path] = set()
    seen_titles: set[str] = set()  # deduplicate Windows "(1)" copies
    seen_authors: dict[str, str] = {}  # normalized → canonical author name
    _log = on_progress or (lambda _msg: None)
    _hit = on_hit or (lambda _r: None)

    # Deduplicate source directories (config may list the same path twice).
    source_dirs = list(dict.fromkeys(cfg.source_dirs))

    missing_dirs: list[Path] = []
    for src_dir in source_dirs:
        if not src_dir.exists():
            missing_dirs.append(src_dir)
            _log(f"[dim]Skipping missing dir: {src_dir}[/dim]")
            continue
        _log(f"Scanning [cyan]{src_dir}[/cyan] …")
        for entry in sorted(src_dir.iterdir()):
            resolved = entry.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)

            _log(f"  checking {entry.name}")

            # Try cache first
            result: ScanResult | None = cache.get(entry) if cache else None

            if result is None:
                if entry.is_file():
                    result = _check_file(entry, cfg)
                elif entry.is_dir():
                    result = _check_dir(entry, cfg)
                if result and cache:
                    cache.put(entry, result)

            if result:
                result.source_dir = src_dir

                # ── Author accent normalization ──
                # Prefer the name variant with more Unicode characters
                # so "Gabriel García Márquez" wins over "Gabriel Garcia Marquez".
                author_key = _normalize_dedup(result.meta.author)
                canonical = seen_authors.get(author_key)
                if canonical is not None:
                    new_unicode = sum(1 for c in result.meta.author if ord(c) > 127)
                    old_unicode = sum(1 for c in canonical if ord(c) > 127)
                    if new_unicode > old_unicode:
                        # Upgrade: new form is more accented — update mapping
                        # and retroactively fix already-collected results.
                        seen_authors[author_key] = result.meta.author
                        for prev in results:
                            if _normalize_dedup(prev.meta.author) == author_key:
                                prev.meta.author = result.meta.author
                    else:
                        result.meta.author = canonical
                else:
                    seen_authors[author_key] = result.meta.author

                dedup_key = _normalize_dedup(f"{result.meta.author}::{result.meta.title}")
                if dedup_key in seen_titles:
                    _log(f"  [yellow]skip duplicate[/yellow] {entry.name}")
                    continue
                seen_titles.add(dedup_key)
                _log(f"  [green]✓[/green] {result.meta.author} — {result.meta.title}")
                results.append(result)
                _hit(result)

    return results, missing_dirs


def _looks_like_junk(stem: str) -> bool:
    """Return True if the filename clearly isn't an audiobook."""
    low = stem.lower()
    return any(low.startswith(p) for p in _JUNK_PREFIXES)


def _zip_contains_audio(path: Path, audio_exts: frozenset[str]) -> bool:
    """Peek inside a zip and return True if it contains audio files."""
    try:
        with zipfile.ZipFile(path) as zf:
            return any(Path(name).suffix.lower() in audio_exts for name in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def _check_file(path: Path, cfg: Config) -> ScanResult | None:
    """Check if a single file is a recognizable audiobook."""
    ext = path.suffix.lower()
    size = path.stat().st_size

    if size < cfg.min_file_size:
        return None

    if ext in cfg.archive_extensions:
        # Quick rejection: too small or junk filename
        if size < MIN_ARCHIVE_SIZE or _looks_like_junk(path.stem):
            return None
        # For zips, peek inside for audio files
        if ext == ".zip" and not _zip_contains_audio(path, cfg.audio_extensions):
            return None
        # Try filename-based parsing first.
        clean_stem = _DUP_SUFFIX_RE.sub("", path.stem)
        file_meta = parse_filename(clean_stem, cfg.filename_patterns)
        # For zips, try metadata.json inside the archive as a fallback.
        zip_meta = parse_metadata_json_from_zip(path) if ext == ".zip" else None
        meta = merge_meta(zip_meta, file_meta) if zip_meta else file_meta
        if meta.author == "Unknown Author" or not looks_like_author(meta.author):
            return None
        meta.source_path = path
        return ScanResult(path=path, kind="archive", meta=meta, size=size)

    if ext in cfg.audio_extensions:
        file_meta = parse_filename(path.stem, cfg.filename_patterns)
        tag_meta = parse_audio_tags(path)
        meta = merge_meta(tag_meta, file_meta)
        if meta.author == "Unknown Author" or not looks_like_author(meta.author):
            return None
        if meta.title != "Unknown Title":
            meta.title = strip_author_from_title(meta.title, meta.author)
        meta.source_path = path
        return ScanResult(path=path, kind="audio_file", meta=meta, size=size, tag_meta=tag_meta)

    return None


def _check_dir(path: Path, cfg: Config) -> ScanResult | None:
    """Check if a directory contains audiobook audio files."""
    all_exts = cfg.audio_extensions | cfg.companion_extensions
    audio_files: list[Path] = []
    total_size = 0
    has_cover = False

    for child in path.rglob("*"):
        if child.is_file() and child.suffix.lower() in all_exts:
            try:
                size = child.stat().st_size
            except OSError:
                continue
            if child.suffix.lower() in cfg.audio_extensions:
                audio_files.append(child)
            total_size += size
        if child.is_file() and child.name.lower() in COVER_NAMES:
            has_cover = True

    if not audio_files:
        return None

    # Try to get metadata from the directory name first, then first audio file
    dir_meta = parse_filename(path.name, cfg.filename_patterns)
    first_audio_meta = parse_audio_tags(audio_files[0]) if audio_files else AudiobookMeta()
    json_meta = parse_metadata_json(path)
    if json_meta:
        meta = merge_meta(json_meta, first_audio_meta, dir_meta)
    else:
        meta = merge_meta(first_audio_meta, dir_meta)
    # Skip directories where we can't identify an author (likely not an audiobook)
    if meta.author == "Unknown Author" or not looks_like_author(meta.author):
        return None
    # Strip author name from title if it leaked through from tags or name.
    if meta.title != "Unknown Title":
        meta.title = strip_author_from_title(meta.title, meta.author)
    meta.source_path = path

    return ScanResult(
        path=path,
        kind="audio_dir",
        meta=meta,
        size=total_size,
        has_cover=has_cover,
        file_count=len(audio_files),
        tag_meta=first_audio_meta if audio_files else None,
    )


def scan_collection(
    root: Path,
    cfg: Config,
    *,
    on_progress: ProgressCallback | None = None,
    on_hit: HitCallback | None = None,
    read_tags: bool = True,
    cache: ScanCache | None = None,
) -> CollectionScan:
    """Scan an existing organized collection at *root* via a single-pass walk.

    Returns a ``CollectionScan`` containing all discovered audiobooks plus
    filesystem metadata (empty dirs, flat files, cover-art presence) gathered
    during the same walk — no extra I/O needed by the analyzer.

    Set *read_tags* to ``False`` to skip reading audio-file tags (faster
    when author/title are already known from the directory structure).
    """
    result = CollectionScan()
    if not root.exists():
        return result

    _log = on_progress or (lambda _msg: None)
    _hit = on_hit or (lambda _r: None)

    audio_exts = cfg.audio_extensions

    # ------------------------------------------------------------------
    # Single pass: os.scandir the tree up to 3 levels deep
    #   Level 0: root          → detect flat audio files
    #   Level 1: author dirs
    #   Level 2: title or series dirs
    #   Level 3: title dirs inside a series
    # ------------------------------------------------------------------

    # Level 0 — root entries
    root_str = str(root)
    try:
        root_entries = sorted(os.scandir(root_str), key=lambda e: e.name)
    except OSError:
        return result

    for root_entry in root_entries:
        if not root_entry.is_dir(follow_symlinks=False):
            # Flat file in root
            if root_entry.is_file(follow_symlinks=False) and (
                Path(root_entry.name).suffix.lower() in audio_exts
            ):
                result.flat_audio_files.append(Path(root_entry.path))
            continue

        if root_entry.name.startswith("."):
            continue

        if root_entry.name in _IGNORED_AUTHOR_DIRS:
            continue

        author_name = root_entry.name
        _log(f"Scanning author: {author_name}")

        # Level 1 — entries under author dir
        try:
            author_entries = sorted(os.scandir(root_entry.path), key=lambda e: e.name)
        except OSError:
            continue

        for sub_entry in author_entries:
            if not sub_entry.is_dir(follow_symlinks=False):
                continue

            # Check whether this is a title dir (has audio) or a series dir
            sub_info = _collect_dir_info(sub_entry.path, audio_exts)

            if sub_info.audio_count > 0:
                # This is a title dir directly under author
                sub_path = Path(sub_entry.path)
                scan_result = cache.get(sub_path) if cache else None
                if scan_result is None:
                    scan_result = _build_scan_result(
                        sub_path,
                        sub_info,
                        cfg,
                        author=author_name,
                        read_tags=read_tags,
                    )
                    if scan_result and cache:
                        cache.put(sub_path, scan_result)
                if scan_result:
                    result.items.append(scan_result)
                    _hit(scan_result)
            else:
                # Empty or series dir — check children
                if sub_info.total_children == 0:
                    result.empty_dirs.append(Path(sub_entry.path))
                    continue

                series_name = sub_entry.name
                try:
                    series_entries = sorted(os.scandir(sub_entry.path), key=lambda e: e.name)
                except OSError:
                    continue

                for title_entry in series_entries:
                    if not title_entry.is_dir(follow_symlinks=False):
                        continue
                    title_info = _collect_dir_info(title_entry.path, audio_exts)
                    if title_info.audio_count > 0:
                        title_path = Path(title_entry.path)
                        scan_result = cache.get(title_path) if cache else None
                        if scan_result is None:
                            scan_result = _build_scan_result(
                                title_path,
                                title_info,
                                cfg,
                                author=author_name,
                                series=series_name,
                                read_tags=read_tags,
                            )
                            if scan_result and cache:
                                cache.put(title_path, scan_result)
                        if scan_result:
                            result.items.append(scan_result)
                            _hit(scan_result)
                    elif title_info.total_children == 0:
                        result.empty_dirs.append(Path(title_entry.path))

    return result


# ------------------------------------------------------------------
# Internal helpers for the single-pass collection scanner
# ------------------------------------------------------------------


@dataclass
class _DirInfo:
    """Lightweight summary of a directory gathered in one scandir pass."""

    audio_files: list[tuple[str, int]] = field(default_factory=list)  # (path, size)
    audio_count: int = 0
    total_size: int = 0
    total_children: int = 0
    has_cover: bool = False


def _collect_dir_info(dir_path: str, audio_exts: frozenset[str]) -> _DirInfo:
    """Walk *dir_path* recursively once, collecting audio file info and cover presence."""
    info = _DirInfo()
    stack = [dir_path]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        for entry in entries:
            info.total_children += 1
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
            elif entry.is_file(follow_symlinks=False):
                name_lower = entry.name.lower()
                ext = Path(name_lower).suffix
                if ext in audio_exts:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    info.audio_files.append((entry.path, size))
                    info.audio_count += 1
                    info.total_size += size
                if name_lower in COVER_NAMES:
                    info.has_cover = True
    return info


def _build_scan_result(
    path: Path,
    info: _DirInfo,
    cfg: Config,
    *,
    author: str = "",
    series: str | None = None,
    read_tags: bool = True,
) -> ScanResult | None:
    """Build a ScanResult from a pre-collected _DirInfo."""
    if info.audio_count == 0:
        return None

    if author:
        dir_meta = parse_title_folder(path.name, author, cfg.filename_patterns)
    else:
        dir_meta = parse_filename(path.name, cfg.filename_patterns)

    json_meta = parse_metadata_json(path)

    if read_tags and info.audio_files:
        tag_meta = parse_audio_tags(Path(info.audio_files[0][0]))
    else:
        tag_meta = None

    sources = [s for s in (json_meta, tag_meta, dir_meta) if s is not None]
    meta = merge_meta(*sources) if sources else dir_meta

    if author:
        meta.author = author
    # Strip author name from title if it leaked through from tags or name.
    if author and meta.title != "Unknown Title":
        meta.title = strip_author_from_title(meta.title, author)
    if series:
        meta.series = series
    meta.source_path = path

    return ScanResult(
        path=path,
        kind="audio_dir",
        meta=meta,
        size=info.total_size,
        has_cover=info.has_cover,
        file_count=info.audio_count,
        tag_meta=tag_meta,
    )
