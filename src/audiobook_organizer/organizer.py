"""Move / copy / extract audiobook files into an organized hierarchy."""

from __future__ import annotations

import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .scanner import ScanResult

logger = logging.getLogger(__name__)


def organize(
    items: list[ScanResult],
    cfg: Config,
    *,
    dry_run: bool = False,
    copy: bool = False,
) -> list[tuple[Path, Path]]:
    """Organize a list of scan results into the destination directory.

    Returns a list of ``(source, destination)`` tuples for each action taken.
    """
    actions: list[tuple[Path, Path]] = []

    for item in items:
        dest_rel = item.meta.dest_relative(author_format=cfg.author_name_format)
        dest_dir = cfg.destination / dest_rel

        if item.kind == "archive" and cfg.auto_extract:
            dest = _handle_archive(item, dest_dir, cfg, dry_run=dry_run)
        elif item.kind == "audio_dir":
            dest = _handle_directory(item, dest_dir, dry_run=dry_run, copy=copy)
        else:
            dest = _handle_single_file(item, dest_dir, dry_run=dry_run, copy=copy)

        if dest:
            actions.append((item.path, dest))

    if not dry_run and actions:
        _log_actions(actions, cfg.move_log)

    return actions


def _handle_archive(item: ScanResult, dest_dir: Path, cfg: Config, *, dry_run: bool) -> Path | None:
    """Extract a zip archive to the destination, or just move if extraction is off."""
    if item.path.suffix.lower() != ".zip":
        # Only .zip extraction is supported; .rar/.7z require external tools
        logger.info(
            "Cannot extract %s — only .zip extraction is supported; moving as-is",
            item.path.suffix,
        )
        return _handle_single_file(item, dest_dir, dry_run=dry_run, copy=False)

    if dry_run:
        return dest_dir

    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(item.path) as zf:
            # Security: validate all member paths to prevent zip-slip
            resolved_dest = dest_dir.resolve()
            for member in zf.namelist():
                member_path = (dest_dir / member).resolve()
                if not member_path.is_relative_to(resolved_dest):
                    raise ValueError(f"Unsafe zip member path: {member}")
            zf.extractall(dest_dir)
    except (zipfile.BadZipFile, ValueError):
        # Fall back to just moving the archive
        return _move_or_copy(item.path, dest_dir / item.path.name, copy=False, dry_run=False)

    if cfg.delete_after_extract and any(dest_dir.iterdir()):
        item.path.unlink()

    return dest_dir


def _handle_directory(
    item: ScanResult, dest_dir: Path, *, dry_run: bool, copy: bool
) -> Path | None:
    """Move or copy an audiobook directory to *dest_dir*.

    When the destination already exists the contents are merged.  For moves
    this is implemented as copy-then-delete because there is no atomic
    "move with merge" operation.
    """
    if dry_run:
        return dest_dir
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copytree(item.path, dest_dir, dirs_exist_ok=True)
    elif dest_dir.exists():
        shutil.copytree(item.path, dest_dir, dirs_exist_ok=True)
        shutil.rmtree(item.path)
    else:
        shutil.move(str(item.path), str(dest_dir))
    return dest_dir


def _handle_single_file(
    item: ScanResult, dest_dir: Path, *, dry_run: bool, copy: bool
) -> Path | None:
    dest_file = dest_dir / item.path.name
    return _move_or_copy(item.path, dest_file, copy=copy, dry_run=dry_run)


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
        shutil.copy2(str(src), str(dest))
    else:
        shutil.move(str(src), str(dest))
    return dest


def _log_actions(actions: list[tuple[Path, Path]], log_path: Path) -> None:
    """Append move/copy actions to the log for undo support."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with log_path.open("a") as f:
        for src, dest in actions:
            f.write(f"{ts}\t{src}\t{dest}\n")


def undo_last(cfg: Config, *, dry_run: bool = False) -> list[tuple[Path, Path]]:
    """Undo the most recent batch of moves from the log.

    Returns list of ``(current_location, restored_location)`` tuples.
    """
    if not cfg.move_log.exists():
        return []

    lines = cfg.move_log.read_text().strip().splitlines()
    if not lines:
        return []

    # Find the last batch (all lines sharing the same timestamp)
    last_ts = lines[-1].split("\t")[0]
    batch = [line for line in lines if line.split("\t", 1)[0] == last_ts]
    remaining = [line for line in lines if line.split("\t", 1)[0] != last_ts]

    undone: list[tuple[Path, Path]] = []
    for line in batch:
        parts = line.split("\t")
        if len(parts) < 3:
            continue  # skip malformed log entries
        _, src_str, dest_str = parts[0], parts[1], parts[2]
        src, dest = Path(src_str), Path(dest_str)
        if not dry_run and dest.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(src))
        undone.append((dest, src))

    if not dry_run:
        cfg.move_log.write_text("\n".join(remaining) + ("\n" if remaining else ""))

    return undone
