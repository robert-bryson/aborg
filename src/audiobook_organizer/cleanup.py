"""Verify stored data before you delete a source path."""

from __future__ import annotations

import os
import shutil
import stat
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from .config import Config

_BLOCK_SIZE = 1024 * 1024


@dataclass
class CleanupResult:
    removed: int = 0
    failed: int = 0
    skipped: int = 0

    def merge(self, other: CleanupResult) -> None:
        self.removed += other.removed
        self.failed += other.failed
        self.skipped += other.skipped


def is_link(path: Path) -> bool:
    """Detect symbolic links and Windows reparse points."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def unique_paths(paths: list[Path]) -> list[Path]:
    """Put children before parents and use a stable order."""
    return sorted(set(paths), key=lambda path: (-len(path.parts), str(path).casefold()))


def _check_boundary(path: Path, root: Path) -> None:
    absolute = path.absolute()
    boundary = root.absolute()
    if not absolute.is_relative_to(boundary) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("path is outside the configured directory")
    for current in (absolute, *absolute.parents):
        if is_link(current):
            raise ValueError("path contains a symbolic link or a reparse point")
        if current == boundary:
            return
    raise ValueError("path has no configured parent directory")


def _signature(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _files(path: Path) -> dict[Path, tuple[int, int, int, int, int]]:
    """Collect regular files and reject links and special files."""
    files = {}
    pending = [path]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                child = Path(entry.path)
                if is_link(child):
                    raise ValueError("source directory contains a link")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(child)
                elif entry.is_file(follow_symlinks=False):
                    files[child] = _signature(child)
                else:
                    raise ValueError("source directory contains a special file")
    return files


def _same_streams(left: BinaryIO, right: BinaryIO) -> bool:
    while True:
        block = left.read(_BLOCK_SIZE)
        if block != right.read(_BLOCK_SIZE):
            return False
        if not block:
            return True


def _check_file(source: Path, destination: Path, root: Path) -> tuple[int, int, int, int, int]:
    _check_boundary(destination, root)
    if not destination.is_file() or source.stat().st_size != destination.stat().st_size:
        raise ValueError("stored file is missing or has a different size")
    before = _signature(destination)
    with source.open("rb") as left, destination.open("rb") as right:
        if not _same_streams(left, right):
            raise ValueError("source and stored file have different contents")
    if before != _signature(destination):
        raise ValueError("stored file changed during verification")
    return before


def _check_archive(
    source: Path, destination: Path, root: Path
) -> dict[Path, tuple[int, int, int, int, int]]:
    count = 0
    signatures = {}
    with zipfile.ZipFile(source) as archive:
        for member in archive.infolist():
            name = member.filename.replace("\\", "/")
            parts = PurePosixPath(name)
            if parts.is_absolute() or ".." in parts.parts or ":" in name:
                raise ValueError("archive contains an unsafe member path")
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("archive contains a symbolic link")
            if member.is_dir():
                continue
            stored = destination.joinpath(*parts.parts)
            _check_boundary(stored, root)
            if not stored.is_file() or stored.stat().st_size != member.file_size:
                raise ValueError("extracted file is missing or has a different size")
            before = _signature(stored)
            with archive.open(member) as left, stored.open("rb") as right:
                if not _same_streams(left, right):
                    raise ValueError("archive and extracted file have different contents")
            if before != _signature(stored):
                raise ValueError("extracted file changed during verification")
            signatures[stored] = before
            count += 1
    if not count:
        raise ValueError("archive has no files to verify")
    return signatures


def verify_source(source: Path, destination: Path, root: Path) -> None:
    """Require a complete stored copy and stable source data."""
    _check_boundary(destination, root)
    before = _signature(source)
    stored_signatures = {}
    if source.is_dir():
        files = _files(source)
        if not files:
            raise ValueError("source directory has no files to verify")
        for child in files:
            stored = destination / child.relative_to(source)
            stored_signatures[stored] = _check_file(child, stored, root)
        if files != _files(source):
            raise ValueError("source directory changed during verification")
    elif source.is_file():
        if destination.is_dir():
            stored = destination / source.name
            if stored.is_file():
                stored_signatures[stored] = _check_file(source, stored, root)
            elif source.suffix.lower() == ".zip":
                stored_signatures = _check_archive(source, destination, root)
            else:
                raise ValueError("stored source file is missing")
        else:
            stored_signatures[destination] = _check_file(source, destination, root)
    else:
        raise ValueError("source is not a regular file or directory")
    if before != _signature(source):
        raise ValueError("source changed during verification")
    if any(signature != _signature(stored) for stored, signature in stored_signatures.items()):
        raise ValueError("stored data changed during verification")


def delete_sources(
    paths: list[Path],
    cfg: Config,
    *,
    destinations: dict[Path, Path] | None = None,
    empty_dirs: set[Path] | None = None,
    dry_run: bool = False,
    on_result: Callable[[Path, str, str], None] | None = None,
) -> CleanupResult:
    """Delete verified sources or empty directories within configured sources."""
    result = CleanupResult()
    notify = on_result or (lambda _path, _status, _reason: None)
    roots = [root.resolve() for root in cfg.source_dirs]
    library = cfg.destination.resolve()
    for source in unique_paths(paths):
        try:
            resolved = source.resolve()
            source_root = next((root for root in roots if resolved.is_relative_to(root)), None)
            if (
                source_root is None
                or resolved in roots
                or resolved == library
                or library.is_relative_to(resolved)
                or resolved.is_relative_to(library)
            ):
                result.skipped += 1
                notify(source, "skipped", "protected path or path outside source directories")
                continue
            _check_boundary(source, source_root)
            if source in (empty_dirs or set()):
                # Recheck emptiness at deletion time. Never recurse here.
                if dry_run:
                    if any(source.iterdir()):
                        raise ValueError("source directory is no longer empty")
                else:
                    source.rmdir()
            else:
                destination = (destinations or {}).get(source)
                if destination is None:
                    raise ValueError("no stored destination is available for verification")
                verify_source(source, destination, library)
                if not dry_run:
                    if source.is_dir():
                        shutil.rmtree(source)
                    else:
                        source.unlink()
            result.removed += 1
            notify(source, "removed", "")
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            result.failed += 1
            notify(source, "failed", str(exc))
    return result
