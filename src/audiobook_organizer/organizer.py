"""Move / copy / extract audiobook files into an organized hierarchy."""

from __future__ import annotations

import logging
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .scanner import ScanResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Action:
    """A filesystem change and the operation needed to undo it."""

    source: Path
    destination: Path
    operation: str  # "move", "copy", or "extract"


def organize(
    items: list[ScanResult],
    cfg: Config,
    *,
    dry_run: bool = False,
    copy: bool = False,
    batch_ts: str | None = None,
) -> list[tuple[Path, Path]]:
    """Organize a list of scan results into the destination directory.

    Returns a list of ``(source, destination)`` tuples for each action taken.
    When called multiple times for the same user operation, pass a shared
    *batch_ts* (ISO-format timestamp) so ``undo_last`` treats them as one batch.
    """
    actions: list[_Action] = []
    effective_batch_ts = batch_ts or datetime.now(timezone.utc).isoformat()

    for item in items:
        dest_rel = item.meta.dest_relative(author_format=cfg.author_name_format)
        dest_dir = cfg.destination / dest_rel

        try:
            if item.kind == "audio_group":
                item_actions = _handle_audio_group(item, dest_dir, dry_run=dry_run, copy=copy)
            elif item.kind == "archive" and cfg.auto_extract:
                item_actions = _handle_archive(item, dest_dir, cfg, dry_run=dry_run, copy=copy)
            elif item.kind == "audio_dir":
                item_actions = _handle_directory(item, dest_dir, dry_run=dry_run, copy=copy)
            else:
                item_actions = _handle_single_file(item, dest_dir, dry_run=dry_run, copy=copy)
        except OSError as exc:
            logger.error("Failed to organize %s: %s", item.path, exc)
            continue
        actions.extend(item_actions)
        if not dry_run and item_actions:
            # Log completed work immediately so a later item failure cannot
            # leave earlier filesystem changes without an undo record.
            _log_actions(item_actions, cfg.move_log, batch_ts=effective_batch_ts)

    return [(action.source, action.destination) for action in actions]


def _handle_archive(
    item: ScanResult, dest_dir: Path, cfg: Config, *, dry_run: bool, copy: bool
) -> list[_Action]:
    """Extract a zip archive to the destination, or just move if extraction is off."""
    if item.path.suffix.lower() != ".zip":
        # Only .zip extraction is supported; .rar/.7z require external tools
        logger.info(
            "Cannot extract %s — only .zip extraction is supported; %s as-is",
            item.path.suffix,
            "copying" if copy else "moving",
        )
        return _handle_single_file(item, dest_dir, dry_run=dry_run, copy=copy)

    if dry_run:
        return [_Action(item.path, dest_dir, "extract")]
    if dest_dir.exists():
        logger.warning("Refusing to extract into existing destination: %s", dest_dir)
        return []

    try:
        with zipfile.ZipFile(item.path) as zf:
            # Security: validate all member paths to prevent zip-slip.
            # Validation runs BEFORE creating dest_dir so a rejected zip
            # doesn't leave orphaned empty directories.
            resolved_dest = dest_dir.resolve()
            for info in zf.infolist():
                member = info.filename
                # Normalise backslashes so traversal via "foo\..\.." is caught
                member_normalized = member.replace("\\", "/")
                # Reject absolute paths and directory traversal
                if member_normalized.startswith("/") or ".." in member_normalized.split("/"):
                    raise ValueError(f"Unsafe zip member path: {member}")
                # Reject symlink entries (external_attr >> 28 == 0xA for symlinks)
                if (info.external_attr >> 28) == 0xA:
                    raise ValueError(f"Zip contains symlink entry: {member}")
                member_path = (dest_dir / member).resolve()
                if not member_path.is_relative_to(resolved_dest):
                    raise ValueError(f"Zip member escapes destination: {member}")
            dest_dir.mkdir(parents=True)
            zf.extractall(dest_dir)
    except zipfile.BadZipFile:
        # Corrupt archive: preserve --copy semantics and keep it as an archive.
        _remove_path(dest_dir)
        return _handle_single_file(item, dest_dir, dry_run=False, copy=copy)
    except (OSError, RuntimeError, zipfile.LargeZipFile) as exc:
        logger.error("Failed to extract %s: %s", item.path, exc)
        _remove_path(dest_dir)
        return []
    except ValueError as exc:
        # Unsafe zip member path — refuse to extract or move
        logger.error("Refusing to extract %s: %s", item.path, exc)
        return []

    try:
        extracted = any(dest_dir.iterdir())
    except OSError:
        extracted = False
    if not extracted:
        dest_dir.rmdir()
        return []
    if cfg.delete_after_extract and extracted and not copy:
        try:
            item.path.unlink()
        except OSError as exc:
            # Extraction succeeded and must remain undoable. Keeping the source
            # archive is safer than dropping the completed action.
            logger.warning("Could not delete extracted archive %s: %s", item.path, exc)

    return [_Action(item.path, dest_dir, "extract")]


def _handle_directory(
    item: ScanResult, dest_dir: Path, *, dry_run: bool, copy: bool
) -> list[_Action]:
    """Move or copy an audiobook directory to *dest_dir*.

    Existing destinations are refused to avoid overwriting or creating an
    undo record that cannot distinguish old files from new ones. Symlinks
    inside copied source trees are preserved rather than followed.
    """
    if dry_run:
        return [_Action(item.path, dest_dir, "copy" if copy else "move")]
    if item.path.is_symlink():
        logger.warning("Refusing to move/copy symlink source: %s", item.path)
        return []
    if dest_dir.exists():
        logger.warning("Refusing to merge into existing destination: %s", dest_dir)
        return []
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        if copy:
            shutil.copytree(item.path, dest_dir, symlinks=True)
        else:
            shutil.move(item.path, dest_dir)
    except OSError:
        if item.path.exists():
            _remove_path(dest_dir)
        raise
    return [_Action(item.path, dest_dir, "copy" if copy else "move")]


def _handle_single_file(
    item: ScanResult, dest_dir: Path, *, dry_run: bool, copy: bool
) -> list[_Action]:
    dest_file = dest_dir / item.path.name
    destination = _move_or_copy(item.path, dest_file, copy=copy, dry_run=dry_run)
    if destination is None:
        return []
    return [_Action(item.path, destination, "copy" if copy else "move")]


def _handle_audio_group(
    item: ScanResult, dest_dir: Path, *, dry_run: bool, copy: bool
) -> list[_Action]:
    """Move or copy only the files belonging to one album in a flat directory."""
    if not item.source_files:
        logger.warning("Grouped audio result has no source files: %s", item.path)
        return []
    if dest_dir.exists():
        logger.warning("Refusing to merge grouped audio into existing destination: %s", dest_dir)
        return []

    actions: list[_Action] = []
    operation = "copy" if copy else "move"
    for source in item.source_files:
        try:
            destination = _move_or_copy(
                source,
                dest_dir / source.name,
                copy=copy,
                dry_run=dry_run,
            )
        except OSError as exc:
            logger.error("Failed to organize grouped file %s: %s", source, exc)
            continue
        if destination is not None:
            actions.append(_Action(source, destination, operation))
    return actions


def _move_or_copy(src: Path, dest: Path, *, copy: bool, dry_run: bool) -> Path | None:
    if dry_run:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Avoid clobbering — add a timestamp suffix (with microseconds to prevent collisions)
        stem = dest.stem
        suffix = dest.suffix
        dest = dest.with_name(f"{stem}_{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}{suffix}")
    if copy:
        try:
            shutil.copy2(src, dest)
        except OSError:
            dest.unlink(missing_ok=True)
            raise
    else:
        shutil.move(src, dest)
    return dest


def _log_actions(actions: list[_Action], log_path: Path, *, batch_ts: str | None = None) -> None:
    """Append move/copy actions to the log for undo support."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = batch_ts or datetime.now(timezone.utc).isoformat()
    with log_path.open("a") as f:
        for action in actions:
            f.write(f"{ts}\t{action.operation}\t{action.source}\t{action.destination}\n")


def undo_last(cfg: Config, *, dry_run: bool = False) -> list[tuple[Path, Path]]:
    """Undo the most recent batch of moves from the log.

    Returns ``(affected_destination, original_source)`` tuples.
    """
    if not cfg.move_log.exists():
        return []

    lines = cfg.move_log.read_text().strip().splitlines()
    if not lines:
        return []

    parsed = [_parse_log_line(line) for line in lines]
    last_ts = next((entry[0] for entry in reversed(parsed) if entry is not None), None)
    if last_ts is None:
        return []
    batch_indices = [
        index for index, entry in enumerate(parsed) if entry is not None and entry[0] == last_ts
    ]

    undone: list[tuple[Path, Path]] = []
    completed_indices: set[int] = set()
    for index in reversed(batch_indices):
        entry = parsed[index]
        if entry is None:
            continue
        _, operation, src, dest = entry
        if not dest.exists():
            # Destination is already gone — stale log entry; purge it without
            # reporting a successful undo (nothing was actually restored).
            completed_indices.add(index)
            continue
        if dry_run:
            undone.append((dest, src))
            continue
        try:
            if operation == "move":
                if src.exists():
                    logger.error("Cannot undo move; source already exists: %s", src)
                    continue
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(dest, src)
            elif operation == "copy":
                _remove_path(dest)
            elif operation == "extract":
                if not src.exists():
                    _rebuild_zip(dest, src)
                _remove_path(dest)
            else:
                logger.error("Unknown move-log operation %r", operation)
                continue
        except (
            OSError,
            RuntimeError,
            ValueError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
        ) as exc:
            logger.error("Failed to undo %s from %s: %s", operation, dest, exc)
            continue
        completed_indices.add(index)
        undone.append((dest, src))

    if not dry_run:
        remaining = [line for index, line in enumerate(lines) if index not in completed_indices]
        cfg.move_log.write_text("\n".join(remaining) + ("\n" if remaining else ""))

    return undone


def _parse_log_line(line: str) -> tuple[str, str, Path, Path] | None:
    """Parse current four-field logs and legacy three-field move logs."""
    current_parts = line.split("\t", maxsplit=3)
    if len(current_parts) == 4 and current_parts[1] in {"move", "copy", "extract"}:
        timestamp, operation, source, destination = current_parts
        return timestamp, operation, Path(source), Path(destination)

    legacy_parts = line.split("\t", maxsplit=2)
    if len(legacy_parts) == 3:
        timestamp, source, destination = legacy_parts
        return timestamp, "move", Path(source), Path(destination)

    return None


def _remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory without following symlinks."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _rebuild_zip(extracted_dir: Path, archive_path: Path) -> None:
    """Recreate a deleted source zip before removing its extracted destination."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive_path.with_suffix(f"{archive_path.suffix}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for child in sorted(extracted_dir.rglob("*")):
                if child.is_file():
                    zf.write(child, child.relative_to(extracted_dir))
        temporary.replace(archive_path)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        temporary.unlink(missing_ok=True)
        raise
