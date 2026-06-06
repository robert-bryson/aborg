"""Scan source directories for audiobook files."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
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

from mutagen import File as MutagenFile
from mutagen import MutagenError

from .config import Config
from .parser import (
    AudiobookMeta,
    extract_series_from_title,
    looks_like_author,
    merge_meta,
    parse_audio_tags,
    parse_filename,
    parse_metadata_json,
    parse_metadata_json_from_zip,
    parse_title_folder,
    resolve_single_name_author,
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

# Subdirectory names that indicate disc/part splits of a single multi-disc audiobook
# (not a series container directory where each subdir is a separate book).
_DISC_DIR_RE = re.compile(r"^(?:disc|disk|cd|part|track|side)\s*\d+$", re.IGNORECASE)


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
    source_files: tuple[Path, ...] = ()  # populated for grouped files from a flat directory


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
    cache_context = _cache_context(cfg, mode="sources", read_tags=True)

    missing_dirs: list[Path] = []

    def _accept(result: ScanResult, entry: Path, source_dir: Path) -> None:
        """Normalize, deduplicate, and record a valid scan result."""
        result.source_dir = source_dir

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

        dedup_key = _normalize_dedup(
            "::".join(
                (
                    result.meta.author,
                    result.meta.title,
                    result.meta.series or "",
                    result.meta.sequence or "",
                    result.meta.year or "",
                    result.meta.narrator or "",
                )
            )
        )
        if dedup_key in seen_titles:
            _log(f"  [yellow]skip duplicate[/yellow] {entry.name}")
            return
        seen_titles.add(dedup_key)
        _log(f"  [green]✓[/green] {result.meta.author} — {result.meta.title}")
        results.append(result)
        _hit(result)

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
            result: ScanResult | None = cache.get(entry, context=cache_context) if cache else None

            if result is None:
                if entry.is_file():
                    result = _check_file(entry, cfg)
                elif entry.is_dir():
                    result = _check_dir(entry, cfg)
                if result and cache:
                    cache.put(entry, result, context=cache_context)

            if result:
                _accept(result, entry, src_dir)
            elif entry.is_dir():
                # _check_dir returned None — may be a container directory
                # (no direct audio, multiple audiobook subdirs) OR a multi-album
                # flat dump (direct audio files with different album tags).
                # Check for direct audio first (multi-album flat case).
                direct_audio = [
                    f
                    for f in entry.iterdir()
                    if f.is_file() and f.suffix.lower() in cfg.audio_extensions
                ]
                if direct_audio:
                    _log(f"  splitting multi-album flat dir: {entry.name}")
                    for res in _split_flat_album_dir(entry, cfg):
                        _accept(res, entry, src_dir)
                else:
                    # Container dir — recurse one level to find individual audiobooks.
                    # Also process archive files (e.g. per-book zip containers).
                    _log(f"  descending into container dir: {entry.name}")
                    for sub in sorted(entry.iterdir()):
                        sub_resolved = sub.resolve()
                        if sub_resolved in seen:
                            continue
                        seen.add(sub_resolved)
                        sub_result: ScanResult | None = None
                        if sub.is_dir():
                            _log(f"    checking {sub.name}")
                            sub_result = cache.get(sub, context=cache_context) if cache else None
                            if sub_result is None:
                                sub_result = _check_dir(sub, cfg)
                                if sub_result and cache:
                                    cache.put(sub, sub_result, context=cache_context)
                            if sub_result:
                                _accept(sub_result, sub, src_dir)
                            else:
                                # Sub-subdir may itself be a multi-album flat dump
                                sub_direct = [
                                    f
                                    for f in sub.iterdir()
                                    if f.is_file() and f.suffix.lower() in cfg.audio_extensions
                                ]
                                if sub_direct:
                                    _log(f"    splitting multi-album flat dir: {sub.name}")
                                    for res in _split_flat_album_dir(sub, cfg):
                                        _accept(res, sub, src_dir)
                        elif sub.is_file():
                            _log(f"    checking {sub.name}")
                            sub_result = cache.get(sub, context=cache_context) if cache else None
                            if sub_result is None:
                                sub_result = _check_file(sub, cfg)
                                if sub_result and cache:
                                    cache.put(sub, sub_result, context=cache_context)
                            if sub_result:
                                _accept(sub_result, sub, src_dir)

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


def _read_tags_from_zip(path: Path, audio_exts: frozenset[str]) -> AudiobookMeta | None:
    """Read audio tags from the first audio file inside a zip archive.

    Extracts the first audio entry into a BytesIO buffer and passes it to
    Mutagen.  Returns None if the zip has no audio, is unreadable, or the
    first audio file exceeds 30 MB (avoids decompressing huge files just for tags).
    """
    max_bytes = 30 * 1024 * 1024
    try:
        with zipfile.ZipFile(path) as zf:
            audio_entries = [
                e
                for e in zf.infolist()
                if Path(e.filename).suffix.lower() in audio_exts and not e.filename.endswith("/")
            ]
            if not audio_entries:
                return None
            entry = sorted(audio_entries, key=lambda e: e.filename)[0]
            if entry.file_size > max_bytes:
                return None
            with zf.open(entry) as f:
                buf = io.BytesIO(f.read())
        buf.seek(0)
        obj = MutagenFile(buf, easy=True)
        if obj is None or not obj.tags:
            return None
        tags = obj.tags

        def _get(*keys: str) -> str | None:
            for k in keys:
                vals = tags.get(k)
                if vals:
                    raw = str(vals[0]).strip()
                    return raw if raw else None
            return None

        meta = AudiobookMeta()
        for raw in (_get("albumartist", "album_artist"), _get("artist")):
            if not raw:
                continue
            # Take just the first slash-separated contributor
            candidate = raw.split("/")[0].strip()
            if looks_like_author(candidate):
                meta.author = candidate
                break
        title = _get("album", "title")
        if title:
            meta.title = title
        meta.year = _get("date", "year")
        meta.narrator = _get("composer")
        meta.series = _get("series", "mvnm", "grouping")
        meta.sequence = _get("series-part", "mvin")
        return meta
    except (
        EOFError,
        MutagenError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        return None


def _check_file(path: Path, cfg: Config) -> ScanResult | None:
    """Check if a single file is a recognizable audiobook."""
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        return None

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
        meta.author = resolve_single_name_author(meta.author, cfg.known_authors)
        if meta.author == "Unknown Author" or not looks_like_author(meta.author):
            # Last resort: read tags from the first audio file inside the zip.
            if ext == ".zip":
                tag_meta = _read_tags_from_zip(path, cfg.audio_extensions)
                if tag_meta:
                    meta = merge_meta(tag_meta, meta)
                    meta.author = resolve_single_name_author(meta.author, cfg.known_authors)
            if meta.author == "Unknown Author" or not looks_like_author(meta.author):
                return None
        if meta.title != "Unknown Title":
            extract_series_from_title(meta)
        meta.source_path = path
        return ScanResult(path=path, kind="archive", meta=meta, size=size)

    if ext in cfg.audio_extensions:
        file_meta = parse_filename(path.stem, cfg.filename_patterns)
        tag_meta = parse_audio_tags(path)
        meta = merge_meta(tag_meta, file_meta)
        meta.author = resolve_single_name_author(meta.author, cfg.known_authors)
        if meta.author == "Unknown Author" or not looks_like_author(meta.author):
            return None
        if meta.title != "Unknown Title":
            meta.title = strip_author_from_title(meta.title, meta.author)
            extract_series_from_title(meta)
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

    # Detect container directories: no audio files directly in this dir, but
    # one or more immediate subdirectories contain audio. Only keep the parent
    # as one book when every audio subdir is a disc split ("Disc 1", "CD 2").
    has_direct_audio = any(f.parent == path for f in audio_files)
    if not has_direct_audio:
        audio_subdirs: set[Path] = set()
        for f in audio_files:
            try:
                direct_child = path / f.relative_to(path).parts[0]
                if direct_child.is_dir():
                    audio_subdirs.add(direct_child)
            except (ValueError, IndexError):
                pass
        if audio_subdirs and not all(_DISC_DIR_RE.match(d.name) for d in audio_subdirs):
            return None  # Container dir — scan_sources will recurse into subdirs
    else:
        # Flat dir with direct audio — check whether audio files span multiple albums
        # (e.g. "The Dark Tower All 7 Books" dump or "01 - Dune Saga" with 8 m4b files).
        # Sample up to 30 direct audio files to detect distinct album tags.
        direct_audio = [f for f in audio_files if f.parent == path]
        if len(direct_audio) > 1:
            album_set: set[str] = set()
            for f in sorted(direct_audio)[:30]:
                album = _read_album_name(f)
                if album:
                    album_set.add(album.casefold())
            if len(album_set) >= 2:
                return None  # Multi-album flat dump — caller uses _split_flat_album_dir

    # Try to get metadata from the directory name first, then first audio file
    dir_meta = parse_filename(path.name, cfg.filename_patterns)
    first_audio_meta = parse_audio_tags(audio_files[0]) if audio_files else AudiobookMeta()
    json_meta = parse_metadata_json(path)
    if json_meta:
        meta = merge_meta(json_meta, first_audio_meta, dir_meta)
    else:
        meta = merge_meta(first_audio_meta, dir_meta)
    meta.author = resolve_single_name_author(meta.author, cfg.known_authors)
    # Skip directories where we can't identify an author (likely not an audiobook)
    if meta.author == "Unknown Author" or not looks_like_author(meta.author):
        return None
    # Strip author name from title if it leaked through from tags or name.
    if meta.title != "Unknown Title":
        meta.title = strip_author_from_title(meta.title, meta.author)
        extract_series_from_title(meta)
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


def _split_flat_album_dir(path: Path, cfg: Config) -> list[ScanResult]:
    """Split a flat directory containing mp3/m4b files from multiple distinct albums.

    Called when *path* has direct audio children whose album tags span 2+ distinct
    values (e.g. all Dark Tower books dumped in one folder).  Groups files by album
    tag and returns one ScanResult per group.  Returns an empty list when the dir
    doesn't actually contain 2+ distinct albums.
    """
    direct_audio = sorted(
        f for f in path.iterdir() if f.is_file() and f.suffix.lower() in cfg.audio_extensions
    )
    if not direct_audio:
        return []

    # Group audio files by album tag.
    groups: dict[str, list[Path]] = {}
    for f in direct_audio:
        album = _read_album_name(f)
        if album:
            groups.setdefault(album.casefold(), []).append(f)

    if len(groups) < 2:
        return []  # Not actually multi-album; normal processing applies

    results: list[ScanResult] = []
    for _album_key, files in sorted(groups.items()):
        tag_meta = parse_audio_tags(files[0])
        total_size = 0
        for source_file in files:
            try:
                total_size += source_file.stat().st_size
            except OSError:
                continue
        meta = merge_meta(tag_meta, AudiobookMeta())
        meta.author = resolve_single_name_author(meta.author, cfg.known_authors)
        if meta.author == "Unknown Author" or not looks_like_author(meta.author):
            continue
        if meta.title != "Unknown Title":
            meta.title = strip_author_from_title(meta.title, meta.author)
            extract_series_from_title(meta)
        meta.source_path = path
        results.append(
            ScanResult(
                path=path,
                kind="audio_group",
                meta=meta,
                size=total_size,
                file_count=len(files),
                tag_meta=tag_meta,
                source_files=tuple(files),
            )
        )
    return results


def _read_album_name(path: Path) -> str | None:
    """Read and normalize the album tag used to group a flat audio directory."""
    try:
        obj = MutagenFile(path, easy=True)
    except (MutagenError, OSError):
        return None
    if obj is None or not obj.tags:
        return None
    values = obj.tags.get("album", [])
    if not values:
        return None
    album = str(values[0]).strip()
    return album or None


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
    cache_context = _cache_context(cfg, mode="collection", read_tags=read_tags)

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
                scan_result = cache.get(sub_path, context=cache_context) if cache else None
                if scan_result is None:
                    scan_result = _build_scan_result(
                        sub_path,
                        sub_info,
                        cfg,
                        author=author_name,
                        read_tags=read_tags,
                    )
                    if scan_result and cache:
                        cache.put(sub_path, scan_result, context=cache_context)
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
                        scan_result = (
                            cache.get(title_path, context=cache_context) if cache else None
                        )
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
                                cache.put(title_path, scan_result, context=cache_context)
                        if scan_result:
                            result.items.append(scan_result)
                            _hit(scan_result)
                    elif title_info.total_children == 0:
                        result.empty_dirs.append(Path(title_entry.path))

    return result


def _cache_context(cfg: Config, *, mode: str, read_tags: bool) -> str:
    """Hash scanner inputs that can change a cached result's metadata."""
    payload = {
        "mode": mode,
        "read_tags": read_tags,
        "archive_extensions": sorted(cfg.archive_extensions),
        "audio_extensions": sorted(cfg.audio_extensions),
        "companion_extensions": sorted(cfg.companion_extensions),
        "filename_patterns": cfg.filename_patterns,
        "known_authors": cfg.known_authors,
        "min_file_size": cfg.min_file_size,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded, usedforsecurity=False).hexdigest()


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
        meta.author = resolve_single_name_author(author, cfg.known_authors)
    # Strip author name from title if it leaked through from tags or name.
    if author and meta.title != "Unknown Title":
        meta.title = strip_author_from_title(meta.title, meta.author)
    extract_series_from_title(meta)
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
