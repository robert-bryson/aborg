"""CLI entry point — ``aborg`` command."""

from __future__ import annotations

import platform
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import click
import yaml
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from . import __version__
from .analyzer import (
    AnalysisReport,
    FixAction,
    analyze_collection,
    apply_fixes,
    are_probable_duplicates,
)
from .cache import ScanCache
from .cleanup import CleanupResult as _CleanupResult
from .cleanup import delete_sources
from .cleanup import unique_paths as _unique_paths
from .config import DEFAULT_CONFIG_PATH, Config
from .fetcher import (
    FetchResult,
    check_odmpy,
    download_latest,
    download_loan,
    is_authenticated,
    libby_setup,
    list_loans,
)
from .organizer import organize, undo_last
from .parser import (
    KNOWN_SINGLE_NAME_AUTHORS,
    AudiobookMeta,
    extract_series_from_title,
    flip_author_name,
    is_last_first,
    looks_like_author,
    merge_meta,
    normalize_path_name,
    parse_audio_tags,
    parse_filename,
    parse_title_folder,
    path_parent_name,
    resolve_single_name_author,
    split_path_parts,
    strip_author_from_title,
)
from .scanner import ScanResult, fold_accents, scan_collection, scan_sources

console = Console()
_FetchValue = TypeVar("_FetchValue")


def _fetch_call(function: Callable[..., _FetchValue], *args, **kwargs) -> _FetchValue:
    try:
        return function(*args, **kwargs)
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
        raise click.ClickException(f"Libby operation failed: {exc}") from exc


def _quantity(count: int, noun: str) -> str:
    plural = f"{noun[:-1]}ies" if noun.endswith("y") else f"{noun}s"
    return f"{count} {noun if count == 1 else plural}"


def _print_fields(rows: list[tuple[str, str]]) -> None:
    """Render literal values without borders or terminal-wide padding."""
    if console.width < 60:
        for label, value in rows:
            console.print(Text(label, "dim"))
            console.print(Padding(Text(value), (0, 0, 0, 2)))
        return
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(overflow="fold")
    for label, value in rows:
        table.add_row(Text(label), Text(value))
    console.print(table)


def _print_book(
    index: int,
    item: ScanResult,
    label: str,
    style: str,
    *,
    verbose: bool = False,
    cfg: Config | None = None,
) -> None:
    """Keep status and identity readable at both wide and narrow terminal widths."""
    table = Table.grid(padding=(0, 1), expand=True)
    table.add_column(width=3, justify="right", style="dim")
    table.add_column(width=9, style=style)
    table.add_column(ratio=1, overflow="fold")
    identity = Text(item.meta.title, style="bold")
    if console.width >= 90:
        identity.append(f" — {item.meta.author}")
        table.add_column(justify="right", no_wrap=True, style="dim")
        table.add_row(str(index), label, identity, _human_size(item.size))
    else:
        identity.append(f"\n{item.meta.author} · {_human_size(item.size)}", style="dim")
        table.add_row(str(index), label, identity)
    console.print(table)
    if item.meta.series:
        console.print(Text(f"    Series: {item.meta.series} #{item.meta.sequence or '?'}", "dim"))
    if verbose and cfg is not None:
        console.print(
            Text(f"    To: {item.meta.dest_relative(author_format=cfg.author_name_format)}", "blue")
        )


def _print_found(items: list[ScanResult], counters: _ScanCounters) -> None:
    console.print()
    console.print(
        Text.assemble(
            (f"Found {_quantity(len(items), 'audiobook')}", "bold"),
            f" · {counters.new_count} new · {counters.exist_count} already in collection"
            f" · {_human_size(sum(item.size for item in items))}",
        )
    )


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


@dataclass
class _ScanCounters:
    """Mutable counters shared between the hit callback and calling code."""

    count: int = 0
    new_count: int = 0
    exist_count: int = 0
    current_source_dir: Path | None = None


def _make_hit_callback(
    cfg: Config,
    counters: _ScanCounters,
    *,
    display: bool = True,
    verbose: bool = False,
) -> Callable[[ScanResult], None]:
    """Build a scan-hit callback that prints each discovered book."""
    author_fmt = cfg.author_name_format
    # Track authors for single-name matching and books per author for duplicate warnings.
    known_authors: dict[str, str] = {}  # normalized surname → full name
    books_by_author: dict[str, list[AudiobookMeta]] = {}

    def _on_hit(result: ScanResult) -> None:
        counters.count += 1
        if result.source_dir != counters.current_source_dir:
            counters.current_source_dir = result.source_dir
            if display:
                console.print()
                console.print(Text.assemble(("Source: ", "dim"), str(result.source_dir)))
        dest_rel = result.meta.dest_relative(author_format=author_fmt)
        dest_full = cfg.destination / dest_rel
        exists = dest_full.exists()
        if exists:
            counters.exist_count += 1
            label, style = "EXISTS", "yellow"
        else:
            counters.new_count += 1
            label, style = "NEW", "green"
        author = result.meta.author
        warnings: list[str] = []
        if author != "Unknown Author" and " " not in author and "," not in author:
            # Suppress warning for known canonical mononyms (Xenophon, Molière, Homer…)
            # Use accent-folded key so resolved forms like "Molière" still match "moliere"
            _resolved = KNOWN_SINGLE_NAME_AUTHORS.get(fold_accents(author.lower()), "")
            _is_known_mononym = bool(_resolved) and " " not in _resolved and "-" not in _resolved
            if not _is_known_mononym:
                # Try to match this single-name author against known full-name authors
                author_low = author.lower()
                match = known_authors.get(author_low)
                if match:
                    warnings.append(f"single-name author (possible match: {match})")
                else:
                    warnings.append("single-name author")

        # Track full-name authors by surname for single-name matching.
        if " " in author or "," in author:
            # Normalise to "Last, First" then take the surname.
            normalised = author if is_last_first(author) else flip_author_name(author)
            surname = normalised.split(",", 1)[0].strip().lower()
            known_authors[surname] = author

        # Near-duplicate title detection (fuzzy match within same author).
        author_key = fold_accents(author.lower())
        near_dupes = [
            previous.title
            for previous in books_by_author.get(author_key, [])
            if are_probable_duplicates(result.meta, previous)
        ]
        if near_dupes:
            warnings.append(f"possible duplicate of: {near_dupes[0]}")
        books_by_author.setdefault(author_key, []).append(result.meta)

        if display:
            _print_book(counters.count, result, label, style, verbose=verbose, cfg=cfg)
        for warning in warnings:
            console.print(Text(f"    Warning ({counters.count}): {warning}", "yellow"))

    return _on_hit


def _print_missing_dirs(missing_dirs: list[Path]) -> None:
    """Print warnings for source directories that don't exist."""
    if not missing_dirs:
        return
    console.print()
    for d in missing_dirs:
        console.print(Text.assemble(("Warning: Source directory not found: ", "red bold"), str(d)))
        hint = _check_wsl_mount(d)
        if hint:
            console.print(f"[yellow]  {hint}[/yellow]")


def _print_books_table(report: AnalysisReport) -> None:
    """Print a table of all books in the collection."""
    if not report.items:
        return
    tbl = Table(title="Library")
    tbl.add_column("#", style="dim", width=3)
    tbl.add_column("Author", style="green", no_wrap=True)
    tbl.add_column("Title", style="bold")
    tbl.add_column("Series", no_wrap=True)
    tbl.add_column("Year", no_wrap=True)
    tbl.add_column("Size", justify="right", no_wrap=True)
    tbl.add_column("Files", justify="right", width=5)

    sorted_items = sorted(report.items, key=lambda x: (x.meta.author.lower(), x.meta.title.lower()))
    for i, item in enumerate(sorted_items, 1):
        tbl.add_row(
            str(i),
            item.meta.author,
            item.meta.title,
            f"{item.meta.series} #{item.meta.sequence}" if item.meta.series else "",
            item.meta.year or "",
            _human_size(item.size),
            str(item.file_count) if item.file_count else "",
        )

    console.print(tbl)
    console.print()


def _is_wsl() -> bool:
    """Return True if running inside Windows Subsystem for Linux."""
    try:
        return "microsoft" in platform.uname().release.lower()
    except Exception:
        return False


def _win_drive_unc(drive_letter: str) -> str | None:
    """Query Windows for the UNC path of a mapped drive letter, or *None*."""
    try:
        out = subprocess.run(
            ["cmd.exe", "/c", f"net use {drive_letter.upper()}:"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in out.stdout.splitlines():
            if line.strip().startswith("Remote name"):
                return line.split(None, 2)[-1].strip()
    except Exception:  # noqa: S110
        pass
    return None


def _check_wsl_mount(path: Path) -> str | None:
    """If *path* looks like an unmounted WSL drive mount, return a hint message."""
    if not _is_wsl():
        return None
    m = re.match(r"^/mnt/([a-z])(?:/|$)", str(path))
    if not m:
        return None
    drive_letter = m.group(1)
    mount_point = Path(f"/mnt/{drive_letter}")
    # Mount point exists but is empty → drive not mounted
    if not (mount_point.is_dir() and not any(mount_point.iterdir())):
        return None

    drive = drive_letter.upper()
    unc = _win_drive_unc(drive_letter)
    if unc:
        # Network-mapped drive → needs CIFS, not drvfs
        smb_path = unc.replace("\\", "/")
        return (
            f"The WSL mount point /mnt/{drive_letter} exists but appears empty — "
            f"the {drive}: drive ({unc}) is likely not mounted.\n"
            f"  Mount it with:  [bold]sudo mount -t cifs {smb_path} /mnt/{drive_letter} "
            f"-o uid=1000,gid=1000[/bold]\n"
            f"  To automount, add to /etc/fstab:  "
            f"[bold]{smb_path} /mnt/{drive_letter} cifs uid=1000,gid=1000,soft 0 0[/bold]"
        )
    # Local drive → use drvfs
    return (
        f"The WSL mount point /mnt/{drive_letter} exists but appears empty — "
        f"the {drive}: drive is likely not mounted.\n"
        f"  Mount it with:  [bold]sudo mount -t drvfs {drive}: /mnt/{drive_letter}[/bold]\n"
        f"  To automount, add to /etc/fstab:  "
        f"[bold]{drive}: /mnt/{drive_letter} drvfs defaults 0 0[/bold]"
    )


def _require_dir(path: Path, label: str = "Directory") -> bool:
    """Print an informative error if *path* doesn't exist. Returns True if OK."""
    if path.is_dir():
        return True
    hint = _check_wsl_mount(path)
    if hint:
        console.print(f"[red]{label} not found: {path}[/red]")
        console.print(f"[yellow]{hint}[/yellow]")
    else:
        console.print(f"[red]{label} not found: {path}[/red]")
    return False


@click.group()
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(exists=False),
    default=None,
    help="Path to config YAML (default: ~/.aborg/config.yaml)",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """aborg — scan, organize, and manage your collection."""
    ctx.ensure_object(dict)
    cfg_path = Path(config_path).expanduser() if config_path else None
    ctx.obj["cfg_path"] = cfg_path or DEFAULT_CONFIG_PATH
    try:
        ctx.obj["cfg"] = Config.load(cfg_path)
    except FileNotFoundError:
        ctx.obj["cfg"] = None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Cannot load config {ctx.obj['cfg_path']}: {exc}") from exc


def _require_cfg(ctx: click.Context) -> Config:
    """Return the loaded Config or exit with a helpful message."""
    cfg = ctx.obj["cfg"]
    if cfg is not None:
        return cfg
    cfg_path = ctx.obj["cfg_path"]
    console.print(f"[red]Config file not found:[/red] {cfg_path}")
    console.print("[yellow]Run [bold]aborg config[/bold] to create one.[/yellow]")
    raise SystemExit(1)


def _run_scan(
    cfg: Config,
    *,
    use_cache: bool,
    show_hits: bool = True,
    verbose: bool = False,
    heading: str = "Scan",
) -> tuple[list[ScanResult], list[Path], _ScanCounters] | None:
    """Run the common scan-and-report logic shared by `scan` and `org`.

    Prints progress, saves cache, and warns about missing dirs.
    Returns ``None`` (after printing a message) when no items are found.
    """
    scan_cache = ScanCache() if use_cache else None

    console.print(heading, style="bold", markup=False)
    _print_fields([("Destination", str(cfg.destination))])

    counters = _ScanCounters()

    with console.status("[bold green]Scanning…[/bold green]", spinner="dots") as status:

        def _on_progress(msg: str) -> None:
            status.update(Text.assemble(("Scanning: ", "bold green"), msg))

        items, missing_dirs = scan_sources(
            cfg,
            on_progress=_on_progress,
            on_hit=_make_hit_callback(cfg, counters, display=show_hits, verbose=verbose),
            cache=scan_cache,
        )

    if scan_cache:
        scan_cache.save()

    _print_missing_dirs(missing_dirs)

    if not items:
        if not missing_dirs:
            console.print("[yellow]No audiobook files found.[/yellow]")
        else:
            console.print(
                "[yellow]No audiobook files found (check source directories above).[/yellow]"
            )
        return None

    return items, missing_dirs, counters


# ── scan ─────────────────────────────────────────────────────────────────


@cli.command()
@click.option("-d", "--dir", "extra_dirs", multiple=True, help="Additional directories to scan.")
@click.option("--table", is_flag=True, help="Show results in a table instead of streaming.")
@click.option("-v", "--verbose", is_flag=True, help="Show destination paths for each audiobook.")
@click.option("--cache", is_flag=True, help="Use cached results from previous scans.")
@click.pass_context
def scan(
    ctx: click.Context, extra_dirs: tuple[str, ...], table: bool, verbose: bool, cache: bool
) -> None:
    """Scan source directories and show discovered audiobook files."""
    cfg = _require_cfg(ctx)
    for d in extra_dirs:
        cfg.source_dirs.append(Path(d).expanduser())

    result = _run_scan(cfg, use_cache=cache, show_hits=not table, verbose=verbose)
    if result is None:
        return
    items, missing_dirs, counters = result

    if table:
        tbl = Table(box=None, padding=(0, 1))
        tbl.add_column("#", style="dim", width=3)
        tbl.add_column("Status")
        tbl.add_column("Author", style="green")
        tbl.add_column("Title", style="bold")
        tbl.add_column("Size", justify="right", no_wrap=True)
        if verbose:
            tbl.add_column("Dest path", style="blue")

        for i, item in enumerate(items, 1):
            dest_rel = item.meta.dest_relative(author_format=cfg.author_name_format)
            exists = (cfg.destination / dest_rel).exists()
            title = item.meta.title
            if item.meta.series:
                title += f" ({item.meta.series} #{item.meta.sequence or '?'})"
            row = [
                Text(str(i)),
                Text("EXISTS" if exists else "NEW", "yellow" if exists else "green"),
                Text(item.meta.author),
                Text(title),
                Text(_human_size(item.size)),
            ]
            if verbose:
                row.append(Text(str(dest_rel)))
            tbl.add_row(*row)

        console.print(tbl)

    _print_found(items, counters)
    if missing_dirs:
        console.print(
            f"Skipped {_quantity(len(missing_dirs), 'missing source directory')}.", style="yellow"
        )


# ── organize ─────────────────────────────────────────────────────────────


@cli.command()
@click.option("-d", "--dir", "extra_dirs", multiple=True, help="Additional directories to scan.")
@click.option("--dest", type=click.Path(), default=None, help="Override destination directory.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@click.option("--copy", is_flag=True, help="Copy instead of move.")
@click.option(
    "-y", "--yes", is_flag=True, help="Confirm planned actions; leave optional cleanup alone."
)
@click.option("-v", "--verbose", is_flag=True, help="Show destination paths for each audiobook.")
@click.option("--cache", is_flag=True, help="Use cached results from previous scans.")
@click.option(
    "--clean-exists",
    is_flag=True,
    help="Delete source files/dirs that are already in the collection.",
)
@click.pass_context
def org(
    ctx: click.Context,
    extra_dirs: tuple[str, ...],
    dest: str | None,
    dry_run: bool,
    copy: bool,
    yes: bool,
    cache: bool,
    clean_exists: bool,
    verbose: bool,
) -> None:
    """Scan source directories and organize audiobooks into the destination."""
    cfg = _require_cfg(ctx)
    for d in extra_dirs:
        cfg.source_dirs.append(Path(d).expanduser())
    if dest:
        cfg.destination = Path(dest)

    if not cfg.destination or cfg.destination == Path():
        console.print(
            "[red]Error:[/red] No destination configured.\n"
            "  Set 'destination' in your config file or pass --dest."
        )
        raise SystemExit(1)

    result = _run_scan(
        cfg,
        use_cache=cache,
        verbose=verbose or dry_run,
        heading="Organize preview (dry run)" if dry_run else "Organize",
    )
    if result is None:
        return
    items, missing_dirs, counters = result
    _print_found(items, counters)

    # Freeze which destinations already existed before this batch. A failed or
    # partial operation must never make another source eligible for deletion.
    existing = {
        i
        for i, item in enumerate(items, 1)
        if (
            cfg.destination
            / item.meta.dest_relative(
                author_format=cfg.author_name_format,
            )
        ).exists()
    }
    new_count = len(items) - len(existing)
    exist_sources = [
        source
        for i, item in enumerate(items, 1)
        if i in existing
        for source in (item.source_files or [item.path])
    ]
    stored_destinations = {
        source: cfg.destination / item.meta.dest_relative(author_format=cfg.author_name_format)
        for i, item in enumerate(items, 1)
        if i in existing
        for source in (item.source_files or [item.path])
    }
    deletion_paths = _unique_paths(exist_sources) if clean_exists else []
    actions = []
    if new_count:
        actions.append(f"{'Copy' if copy else 'Organize'} {_quantity(new_count, 'new audiobook')}")
    if deletion_paths:
        actions.append(
            f"delete {_quantity(len(deletion_paths), 'source path')} already in collection"
        )
        console.print("Source paths to delete:", style="bold")
        for path in deletion_paths:
            console.print(Text(f"  {_source_label(path, cfg)}"))
        console.print(
            "Deletion is permanent. Each source must match its stored data before deletion.",
            style="yellow",
        )
    if not actions:
        console.print("Nothing new to organize. Source files kept.", style="dim")
        return
    prompt = " and ".join(actions)
    prompt = prompt[0].upper() + prompt[1:]
    if dry_run or yes:
        console.print(Text(f"Plan: {prompt}."))

    # Validate before asking the user to approve an operation we cannot perform.
    if not dry_run and new_count:
        dest_root = cfg.destination
        if not dest_root.is_dir():
            console.print(Text(f"Error: Destination does not exist: {dest_root}", "red"))
            console.print("Create it or check your config.")
            ctx.exit(1)
        try:
            with tempfile.NamedTemporaryFile(dir=dest_root):
                pass
        except OSError:
            console.print(Text(f"Error: Destination is not writable: {dest_root}", "red"))
            console.print("Check directory permissions and, for a network drive, its connection.")
            ctx.exit(1)

    if not dry_run and not yes and not click.confirm(f"{prompt}?"):
        console.print("Cancelled. No files changed.", style="dim")
        return

    done = 0
    skipped = len(existing)
    failed = 0
    moved_sources: list[Path] = []
    retained_archives = 0
    batch_ts = datetime.now(timezone.utc).isoformat()
    verb = "Previewing" if dry_run else ("Copying" if copy else "Organizing")
    if new_count:
        console.print()
        with console.status(f"[bold green]{verb}…[/bold green]", spinner="dots") as status:
            for i, item in enumerate(items, 1):
                if i in existing:
                    continue
                dest_full = cfg.destination / item.meta.dest_relative(
                    author_format=cfg.author_name_format,
                )
                if dest_full.exists():
                    skipped += 1
                    _print_book(i, item, "SKIPPED", "yellow")
                    console.print(
                        "    Destination appeared since scanning; source kept.", style="yellow"
                    )
                    continue
                status.update(Text.assemble((f"{verb}: ", "bold green"), item.meta.title))
                try:
                    completed = organize([item], cfg, dry_run=dry_run, copy=copy, batch_ts=batch_ts)
                except (OSError, RuntimeError) as exc:
                    raise click.ClickException(
                        f"Cannot complete or roll back {item.path}: {exc}"
                    ) from exc
                if not completed:
                    failed += 1
                    _print_book(i, item, "FAILED", "red")
                    console.print(
                        "    Could not organize this audiobook. Review the error above.",
                        style="red",
                    )
                    continue
                completed_sources = [source for source, _destination in completed]
                moved_sources.extend(completed_sources)
                stored_destinations.update(completed)
                if item.source_files and len(completed_sources) != len(item.source_files):
                    failed += 1
                    _print_book(i, item, "PARTIAL", "red")
                    console.print(
                        f"    {len(completed_sources)}/{len(item.source_files)} files completed.",
                        style="red",
                    )
                else:
                    done += 1
                    if not dry_run:
                        _print_book(i, item, "COPIED" if copy else "ORGANIZED", "green")
                        if (
                            item.kind == "archive"
                            and item.path.suffix.lower() == ".zip"
                            and cfg.auto_extract
                            and item.path.exists()
                            and not copy
                        ):
                            retained_archives += 1

    cleanup = _CleanupResult()
    if dry_run and deletion_paths:
        cleanup = _delete_sources(
            deletion_paths, cfg, destinations=stored_destinations, dry_run=True
        )
    elif not dry_run:
        cleanup = _offer_source_cleanup(
            moved_sources,
            cfg,
            copy=copy,
            exist_sources=exist_sources,
            auto_clean_exists=clean_exists,
            prompt=not yes,
            destinations=stored_destinations,
        )

    # The final result includes cleanup, so it is the last thing the user sees.
    console.print()
    console.print("Preview complete — no files changed." if dry_run else "Result", style="bold")
    parts = []
    if new_count:
        verb_past = (
            ("Would copy" if copy else "Would organize")
            if dry_run
            else ("Copied" if copy else "Organized")
        )
        parts.append(f"{verb_past} {_quantity(done, 'audiobook')}")
    if skipped:
        parts.append(f"{skipped} already in collection")
    if dry_run and deletion_paths:
        parts.append(f"Would delete {_quantity(cleanup.removed, 'source path')}")
    elif cleanup.removed:
        parts.append(f"Deleted {_quantity(cleanup.removed, 'source path')}")
    if failed:
        parts.append(f"{failed} failed or partial")
    if cleanup.failed:
        parts.append(f"{cleanup.failed} cleanup failed")
    if cleanup.skipped:
        parts.append(f"{cleanup.skipped} cleanup skipped")
    console.print(
        " · ".join(parts) + ".",
        style="red" if failed or cleanup.failed or cleanup.skipped else "green",
        markup=False,
    )
    if retained_archives:
        console.print(
            f"Kept {_quantity(retained_archives, 'source archive')}.",
            style="dim",
        )
        if not cfg.delete_after_extract:
            console.print(
                "Set delete_after_extract: true in config to remove archives after extraction.",
                style="dim",
            )
    if missing_dirs:
        console.print(
            f"Skipped {_quantity(len(missing_dirs), 'missing source directory')}.", style="yellow"
        )
    if done and not dry_run:
        console.print("Undo organizing: aborg undo. Source cleanup cannot be undone.", style="dim")
    if failed or cleanup.failed or cleanup.skipped:
        ctx.exit(1)


def _source_label(path: Path, cfg: Config) -> str:
    """Avoid repeating the source root when a single source gives clear context."""
    if len(cfg.source_dirs) == 1:
        try:
            return str(path.relative_to(cfg.source_dirs[0]))
        except ValueError:
            pass
    return str(path)


def _delete_sources(
    paths: list[Path],
    cfg: Config,
    *,
    destinations: dict[Path, Path] | None = None,
    empty_dirs: set[Path] | None = None,
    dry_run: bool = False,
) -> _CleanupResult:
    def report(path: Path, status: str, reason: str) -> None:
        if status == "removed":
            verb = "Would delete" if dry_run else "Deleted"
            console.print(Text(f"  {verb}: {_source_label(path, cfg)}", "green"))
        else:
            label = "Failed to delete" if status == "failed" else "Skipped protected path"
            console.print(
                Text(f"  {label}: {path}: {reason}", "red" if status == "failed" else "yellow")
            )

    with console.status("Verifying source copies…", spinner="dots"):
        return delete_sources(
            paths,
            cfg,
            destinations=destinations,
            empty_dirs=empty_dirs,
            dry_run=dry_run,
            on_result=report,
        )


def _offer_source_cleanup(
    moved_sources: list[Path],
    cfg: Config,
    *,
    copy: bool,
    exist_sources: list[Path] | None = None,
    auto_clean_exists: bool = False,
    prompt: bool = True,
    destinations: dict[Path, Path] | None = None,
) -> _CleanupResult:
    """Clean explicitly requested sources, then offer optional source cleanup."""
    result = _CleanupResult()
    cleanup_paths: list[Path] = []
    source_dir_resolved = {sd.resolve() for sd in cfg.source_dirs}
    empty_dirs: set[Path] = set()
    exists_cleanup = _unique_paths([src for src in exist_sources or [] if src.exists()])
    if auto_clean_exists and exists_cleanup:
        console.print()
        console.print("Removing sources already in collection", style="bold")
        result = _delete_sources(exists_cleanup, cfg, destinations=destinations)
    else:
        cleanup_paths.extend(exists_cleanup)

    if not prompt:
        return result
    if copy:
        cleanup_paths.extend(src for src in moved_sources if src.exists())
    else:
        seen: set[Path] = set()
        planned: set[Path] = set()
        for src in moved_sources:
            parent = src.parent
            while (
                parent.exists()
                and parent.resolve() not in source_dir_resolved
                and any(parent.resolve().is_relative_to(root) for root in source_dir_resolved)
            ):
                if parent in seen:
                    break
                seen.add(parent)
                try:
                    children = set(parent.iterdir()) - planned
                except OSError as exc:
                    result.failed += 1
                    console.print(
                        Text(f"Could not inspect cleanup directory {parent}: {exc}", "red")
                    )
                    break
                if parent.is_dir() and not children:
                    cleanup_paths.append(parent)
                    planned.add(parent)
                    empty_dirs.add(parent)
                    parent = parent.parent
                else:
                    break

    if not cleanup_paths:
        return result
    cleanup_paths = _unique_paths(cleanup_paths)
    console.print()
    console.print("Optional source cleanup", style="bold")
    for path in cleanup_paths:
        console.print(Text(f"  {_source_label(path, cfg)}"))
    console.print(
        "Deletion is permanent. Organize undo does not restore these paths.", style="yellow"
    )
    if exists_cleanup and not auto_clean_exists:
        console.print("Each source must match its stored data before deletion.", style="yellow")
    if not click.confirm(f"Delete {_quantity(len(cleanup_paths), 'source path')}?"):
        console.print(f"Kept {_quantity(len(cleanup_paths), 'source path')}.", style="dim")
        return result
    result.merge(
        _delete_sources(cleanup_paths, cfg, destinations=destinations, empty_dirs=empty_dirs)
    )
    return result


# ── fetch (Libby / OverDrive) ───────────────────────────────────────────


@cli.command()
@click.option("--setup", "setup_code", default=None, help="8-digit Libby setup code.")
@click.option("--list", "list_only", is_flag=True, help="List current audiobook loans.")
@click.option("--latest", type=click.IntRange(min=1), default=None, help="Download latest N loans.")
@click.option("--select", "select_ids", multiple=True, help="Download specific loan(s) by ID.")
@click.option("--all", "fetch_all", is_flag=True, help="Download all current loans.")
@click.option(
    "-d",
    "--download-dir",
    type=click.Path(),
    default=None,
    help="Override download directory (defaults to first source_dir).",
)
@click.option(
    "--organize",
    "auto_organize",
    is_flag=True,
    help="Automatically organize after downloading.",
)
@click.option("--merge", is_flag=True, default=None, help="Merge MP3 parts into one file.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without downloading.")
@click.pass_context
def fetch(
    ctx: click.Context,
    setup_code: str | None,
    list_only: bool,
    latest: int | None,
    select_ids: tuple[str, ...],
    fetch_all: bool,
    download_dir: str | None,
    auto_organize: bool,
    merge: bool | None,
    dry_run: bool,
) -> None:
    """Download audiobook loans from Libby/OverDrive.

    First-time setup:  aborg fetch --setup 12345678

    Then list your loans:  aborg fetch --list

    Download the latest loan:  aborg fetch --latest 1

    Download and auto-organize:  aborg fetch --latest 1 --organize
    """
    cfg = _require_cfg(ctx)

    if sum((bool(setup_code), list_only, latest is not None, bool(select_ids), fetch_all)) > 1:
        raise click.UsageError(
            "Select one fetch action: --setup, --list, --latest, --select, or --all."
        )
    if not any((setup_code, list_only, latest is not None, select_ids, fetch_all)):
        click.echo(ctx.get_help())
        return

    # Check odmpy is installed
    if not check_odmpy():
        console.print(
            '[red]odmpy is not installed.[/red]\nInstall it with: [bold]uv pip install ".[libby]"[/bold]'
        )
        ctx.exit(1)
        return

    settings = cfg.libby_settings

    # ── Setup mode ──
    if setup_code:
        console.print("[dim]Linking Libby account…[/dim]")
        ok, msg = libby_setup(settings, setup_code)
        if ok:
            console.print(f"[green]{msg}[/green]")
        else:
            console.print(f"[red]{msg}[/red]")
            ctx.exit(1)
        return

    # Everything below requires authentication
    if not is_authenticated(settings):
        console.print(
            "[yellow]No Libby account linked.[/yellow]\n"
            "Run: [bold]aborg fetch --setup <8-digit-code>[/bold]\n"
            "Get a code at: https://help.libbyapp.com/en-us/6070.htm"
        )
        ctx.exit(1)
        return

    # ── List loans ──
    if list_only:
        with console.status("[bold green]Fetching loans…[/bold green]", spinner="dots"):
            loans = _fetch_call(list_loans, settings)

        if not loans:
            console.print("[yellow]No downloadable audiobook loans found.[/yellow]")
            return

        table = Table(title="Audiobook Loans")
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Author", style="green", no_wrap=True)
        table.add_column("Title", style="bold")

        for loan in loans:
            table.add_row(
                *(Text(value) for value in (str(loan.index), loan.id, loan.author, loan.title))
            )

        console.print(table)
        console.print(
            "\n[dim]Use [bold]aborg fetch --select <ID>[/bold] or "
            "[bold]aborg fetch --latest N[/bold] to download.[/dim]"
        )
        return

    # ── Determine download target dir ──
    if not download_dir and not cfg.source_dirs:
        raise click.ClickException(
            "No download directory is configured. Use --download-dir or set source_dirs."
        )
    dl_dir = Path(download_dir) if download_dir else cfg.source_dirs[0]
    dl_dir = dl_dir.expanduser()

    use_merge = merge if merge is not None else cfg.libby_merge

    # ── Download latest N ──
    if latest is not None:
        if dry_run:
            console.print(
                f"[dim]DRY RUN — would download latest {latest} loan(s) to {dl_dir}[/dim]"
            )
            return

        console.print(f"[dim]Downloading latest {latest} loan(s) to {dl_dir}…[/dim]")
        with console.status("[bold green]Downloading…[/bold green]", spinner="dots"):
            ok, output = _fetch_call(
                download_latest,
                settings,
                dl_dir,
                count=latest,
                merge=use_merge,
                merge_format=cfg.libby_merge_format,
                chapters=cfg.libby_chapters,
                keep_cover=cfg.libby_keep_cover,
                book_folder_format=cfg.libby_book_folder_format,
            )

        if ok:
            console.print("[green]Download complete.[/green]")
        else:
            console.print(f"[red]Download failed:[/red] {output}")
            ctx.exit(1)
            return

        if auto_organize:
            console.print()
            ctx.invoke(
                org, extra_dirs=(str(dl_dir),), dest=None, dry_run=False, copy=False, yes=True
            )
        return

    # ── Download by ID(s) ──
    if select_ids or fetch_all:
        with console.status("[bold green]Fetching loans…[/bold green]", spinner="dots"):
            loans = _fetch_call(list_loans, settings)

        if not loans:
            console.print("[yellow]No downloadable audiobook loans found.[/yellow]")
            return

        if fetch_all:
            to_download = loans
        else:
            id_set = set(select_ids)
            missing_ids = id_set - {loan.id for loan in loans}
            if missing_ids:
                raise click.ClickException(
                    f"No loans matched IDs: {', '.join(sorted(missing_ids))}"
                )
            to_download = [ln for ln in loans if ln.id in id_set]

        if dry_run:
            console.print(f"[dim]DRY RUN — would download {len(to_download)} loan(s):[/dim]")
            for loan in to_download:
                console.print(f"  [dim]{loan.index}.[/dim] {loan.author} — {loan.title}")
            return

        console.print(f"Downloading [bold]{len(to_download)}[/bold] loan(s) to {dl_dir}…\n")
        results: list[FetchResult] = []
        for loan in to_download:
            console.print(f"  [dim]↓[/dim] {loan.author} — [bold]{loan.title}[/bold]")
            with console.status(
                f"[bold green]Downloading:[/bold green] {loan.title}",
                spinner="dots",
            ):
                result = _fetch_call(
                    download_loan,
                    settings,
                    dl_dir,
                    loan,
                    merge=use_merge,
                    merge_format=cfg.libby_merge_format,
                    chapters=cfg.libby_chapters,
                    keep_cover=cfg.libby_keep_cover,
                    book_folder_format=cfg.libby_book_folder_format,
                )
            results.append(result)
            if result.success:
                console.print(f"  [green]✓[/green] {loan.title}")
            else:
                console.print(f"  [red]✗[/red] {loan.title}: {result.message}")

        ok_count = sum(1 for r in results if r.success)
        fail_count = len(results) - ok_count
        parts = [f"[green]{ok_count} downloaded[/green]"]
        if fail_count:
            parts.append(f"[red]{fail_count} failed[/red]")
        console.print(f"\n{', '.join(parts)}.")

        if auto_organize and ok_count:
            console.print()
            ctx.invoke(
                org, extra_dirs=(str(dl_dir),), dest=None, dry_run=False, copy=False, yes=True
            )
        if fail_count:
            ctx.exit(1)
        return


# ── analyze ──────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--path",
    type=click.Path(exists=True),
    default=None,
    help="Collection root to analyze (defaults to configured destination).",
)
@click.option("--fix", is_flag=True, help="Apply automatic fixes for detected issues.")
@click.option("--dry-run", is_flag=True, help="Show what --fix would do without making changes.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt when using --fix.")
@click.option("--cache", is_flag=True, help="Use cached results from previous scans.")
@click.option(
    "--check-tags/--no-check-tags",
    default=True,
    help="Read audio tags and check metadata quality (--no-check-tags for speed).",
)
@click.pass_context
def analyze(
    ctx: click.Context,
    path: str | None,
    fix: bool,
    dry_run: bool,
    yes: bool,
    cache: bool,
    check_tags: bool,
) -> None:
    """Analyze an existing audiobook collection and suggest improvements."""
    cfg = _require_cfg(ctx)
    root = Path(path) if path else cfg.destination

    if not _require_dir(root, "Collection directory"):
        ctx.exit(1)

    scan_cache = ScanCache() if cache else None

    with console.status(
        f"[bold green]Analyzing {root} …[/bold green]",
        spinner="dots",
    ) as status:

        def _on_progress(msg: str) -> None:
            status.update(f"[bold green]Analyzing:[/bold green] {msg}")

        report = analyze_collection(
            root,
            cfg,
            on_progress=_on_progress,
            cache=scan_cache,
            read_tags=check_tags,
        )

    if scan_cache:
        scan_cache.save()

    # Build summary table (printed at the end)
    summary = Table(title="Collection Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Total books", str(report.total_books))
    summary.add_row("Total size", _human_size(report.total_size))
    summary.add_row("Authors", str(report.authors))
    summary.add_row("Series", str(report.series))
    summary.add_row("Issues", str(len(report.issues)))

    if not report.issues:
        console.print()
        _print_books_table(report)
        console.print(summary)
        console.print("\n[green]No issues found — collection looks great![/green]")
        return

    # Issues table
    console.print()
    _print_books_table(report)
    issues_table = Table(title="Issues")
    issues_table.add_column("Sev", width=7)
    issues_table.add_column("Category", width=12)
    issues_table.add_column("Message")
    issues_table.add_column("Suggestion", style="dim")
    issues_table.add_column("Auto-fix", justify="center", width=8)

    severity_style = {"error": "red bold", "warning": "yellow", "info": "blue"}
    for issue in report.issues:
        auto_fix = "[green]✓[/green]" if issue.fix else "[dim]—[/dim]"
        issues_table.add_row(
            f"[{severity_style.get(issue.severity, '')}]{issue.severity}[/]",
            issue.category,
            issue.message,
            issue.suggestion or "",
            auto_fix,
        )

    console.print(issues_table)

    # Duplicate details
    if report.duplicates:
        console.print(f"\n[yellow]Found {len(report.duplicates)} possible duplicate(s).[/yellow]")

    if report.author_variants:
        n = len(report.author_variants)
        msg = f"Found {n} similar author name(s) that may need standardizing."
        console.print(f"\n[blue]{msg}[/blue]")

    # ── Apply fixes ──────────────────────────────────────────────────
    fixable = [i for i in report.issues if i.fix is not None]
    if not fixable:
        if fix or dry_run:
            console.print("\n[dim]No automatically fixable issues found.[/dim]")
        console.print()
        console.print(summary)
        return

    if not fix and not dry_run:
        console.print(
            f"\n[dim]{len(fixable)} issue(s) can be fixed automatically. "
            f"Re-run with [bold]--fix[/bold] to apply.[/dim]"
        )
        console.print()
        console.print(summary)
        return

    if fix and not dry_run and not yes:
        console.print()
        if not click.confirm(f"Apply {len(fixable)} automatic fix(es)?"):
            console.print("[yellow]Aborted.[/yellow]")
            console.print()
            console.print(summary)
            return

    verb = "Would apply" if dry_run else "Applying"
    console.print(f"\n[bold]{verb} {len(fixable)} fix(es):[/bold]\n")

    def _on_fix(action: FixAction, ok: bool, err: str) -> None:
        icon = "[green]\u2713[/green]" if ok else "[red]\u2717[/red]"
        if dry_run:
            icon = "[dim]\u2022[/dim]"
        reason = f" [red dim]({err})[/red dim]" if err and not ok else ""
        if action.kind == "remove_dir":
            console.print(f"  {icon} Remove empty directory: [dim]{action.source}[/dim]{reason}")
        elif action.kind == "rename":
            target_label = action.target.name if action.target else "?"
            console.print(
                f"  {icon} Rename: [dim]{action.source.name}[/dim]"
                f" [blue]\u2192[/blue] [green]{target_label}[/green]{reason}"
            )

    applied = apply_fixes(report, dry_run=dry_run, on_fix=_on_fix)

    if dry_run:
        console.print(f"\n[dim]{len(applied)} fix(es) would be applied.[/dim]")
    else:
        console.print(f"\n[green]Applied {len(applied)} fix(es).[/green]")

    console.print()
    console.print(summary)
    if not dry_run and len(applied) != len(fixable):
        console.print(
            f"Failed {_quantity(len(fixable) - len(applied), 'correction')}.", style="red"
        )
        ctx.exit(1)


# ── parse (utility) ─────────────────────────────────────────────────────


@cli.command()
@click.argument("filename")
@click.pass_context
def parse(ctx: click.Context, filename: str) -> None:
    """Parse a filename or path and show what metadata would be extracted.

    Accepts a plain filename, a full file path, or a directory path.
    When given an actual audio file, also reads tags and shows the merged
    result — the same logic used by ``aborg scan``.
    """
    cfg = _require_cfg(ctx)

    # ── Normalise the input ──────────────────────────────────────────
    name = normalize_path_name(filename)
    console.print(f"[dim]Parsed name:[/dim]  {name}")

    # ── Identify the parent folder (potential author) ────────────────
    parent = path_parent_name(filename)
    known_author: str | None = None
    parent_meta = AudiobookMeta()
    if parent:
        parent_parsed = parse_filename(parent, cfg.filename_patterns)
        if parent_parsed.author != "Unknown Author" and looks_like_author(parent_parsed.author):
            known_author = parent_parsed.author
            parent_meta = parent_parsed
        elif looks_like_author(parent):
            known_author = parent
            parent_meta.author = parent

    # ── Parse the folder/file name (author-aware when possible) ──────
    if known_author:
        name_meta = parse_title_folder(name, known_author, cfg.filename_patterns)
    else:
        name_meta = parse_filename(name, cfg.filename_patterns)

    # ── If it's an actual audio file, read tags (same as scanner) ────
    path = Path(filename)
    tag_meta = AudiobookMeta()
    has_tags = False
    if path.is_file() and path.suffix.lower() in cfg.audio_extensions:
        tag_meta = parse_audio_tags(path)
        has_tags = True

    # ── Merge sources (same priority as scanner: tags > name > parent)
    merged = merge_meta(tag_meta, name_meta, parent_meta)
    merged.author = resolve_single_name_author(merged.author, cfg.known_authors)

    # Strip author from title if it leaked through from tags.
    if merged.author != "Unknown Author" and merged.title != "Unknown Title":
        merged.title = strip_author_from_title(merged.title, merged.author)
    if merged.title != "Unknown Title":
        extract_series_from_title(merged)

    # If merged author is obviously wrong, search path ancestors for a
    # clean author name (skip "Author - Title" style components).
    if not looks_like_author(merged.author) or merged.author == "Unknown Author":
        for comp in reversed(split_path_parts(filename)[:-1]):
            if " - " in comp:
                continue
            if looks_like_author(comp):
                merged.author = comp
                break

    # ── Display individual sources and merged result ─────────────────
    def _meta_row(label: str, m: AudiobookMeta) -> None:
        parts = []
        if m.author != "Unknown Author":
            parts.append(f"author=[bold]{m.author}[/bold]")
        if m.title != "Unknown Title":
            parts.append(f"title={m.title}")
        if m.series:
            parts.append(f"series={m.series}")
        if m.sequence:
            parts.append(f"seq={m.sequence}")
        if m.year:
            parts.append(f"year={m.year}")
        if m.narrator:
            parts.append(f"narrator={m.narrator}")
        console.print(f"  [dim]{label}:[/dim] {', '.join(parts) if parts else '[dim]—[/dim]'}")

    console.print()
    console.print("[bold]Sources:[/bold]")
    _meta_row("Name   ", name_meta)
    if parent:
        _meta_row("Parent ", parent_meta)
    if has_tags:
        _meta_row("Tags   ", tag_meta)

    console.print()
    table = Table(title="Merged result", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Author", merged.author)
    table.add_row("Title", merged.title)
    table.add_row("Series", merged.series or "—")
    table.add_row("Sequence", merged.sequence or "—")
    table.add_row("Year", merged.year or "—")
    table.add_row("Narrator", merged.narrator or "—")
    table.add_row("Dest folder", merged.dest_folder_name())
    table.add_row("Dest path", str(merged.dest_relative(author_format=cfg.author_name_format)))
    console.print(table)


# ── undo ─────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be undone.")
@click.pass_context
def undo(ctx: click.Context, dry_run: bool) -> None:
    """Undo the most recent organize operation."""
    cfg = _require_cfg(ctx)
    errors: list[str] = []
    try:
        actions = undo_last(cfg, dry_run=dry_run, on_error=errors.append)
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Cannot process the undo log: {exc}") from exc

    if not actions:
        if errors:
            console.print(f"Failed to undo {_quantity(len(errors), 'operation')}.", style="red")
            ctx.exit(1)
        console.print("[yellow]Nothing to undo.[/yellow]")
        return

    verb = "Would undo" if dry_run else "Undid"
    console.print(f"\n[bold]{verb} {len(actions)} item(s):[/bold]\n")
    for i, (affected, original) in enumerate(actions, 1):
        console.print(
            f"  [green]↩[/green] [dim]{i:>3}.[/dim]"
            f" [dim]{affected.name}[/dim]"
            f"  [blue](original source: {original})[/blue]"
        )

    console.print(f"\n[green]{verb} {len(actions)} item(s).[/green]")
    if errors:
        console.print(f"Failed to undo {_quantity(len(errors), 'operation')}.", style="red")
        ctx.exit(1)


# ── config ───────────────────────────────────────────────────────────────


def _show_config(cfg: Config) -> None:
    """Print config as a Rich table."""
    table = Table(title="Current Configuration", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("Source dirs", ", ".join(str(d) for d in cfg.source_dirs) or "(none)")
    table.add_row("Destination", str(cfg.destination) or "(none)")
    table.add_row("Auto extract", str(cfg.auto_extract))
    table.add_row("Delete after extract", str(cfg.delete_after_extract))
    table.add_row("Min file size", _human_size(cfg.min_file_size))
    table.add_row("Move log", str(cfg.move_log))
    table.add_row("Archive exts", ", ".join(sorted(cfg.archive_extensions)))
    table.add_row("Audio exts", ", ".join(sorted(cfg.audio_extensions)))
    table.add_row("Patterns", f"{len(cfg.filename_patterns)} pattern(s)")
    console.print(table)


def _config_wizard(cfg_path: Path) -> None:
    """Interactive setup wizard — walk the user through creating a config."""
    console.print("\n[bold]aborg configuration setup[/bold]\n")

    cfg = Config.default()

    # ── Source directories ────────────────────────────────────────────
    console.print(
        "[dim]Source directories are where aborg looks for new audiobook files\n"
        "(e.g. your Downloads folder).  Enter one path per line, blank to finish.[/dim]"
    )
    dirs: list[Path] = []
    while True:
        idx = len(dirs) + 1
        prompt = f"  Source dir [{idx}]" if not dirs else f"  Source dir [{idx}] (blank to finish)"
        raw = click.prompt(prompt, default="", show_default=False).strip()
        if not raw:
            if not dirs:
                console.print("  [yellow]At least one source directory is required.[/yellow]")
                continue
            break
        p = Path(raw).expanduser()
        if not p.is_absolute():
            console.print(f"  [yellow]Please enter an absolute path (got: {raw})[/yellow]")
            continue
        dirs.append(p)
    cfg.source_dirs = dirs

    # ── Destination ───────────────────────────────────────────────────
    console.print(
        "\n[dim]Destination is the root of your organized audiobook collection\n"
        "(the library that Audiobookshelf points to).[/dim]"
    )
    while True:
        raw = click.prompt("  Destination", type=str).strip()
        p = Path(raw).expanduser()
        if not p.is_absolute():
            console.print(f"  [yellow]Please enter an absolute path (got: {raw})[/yellow]")
            continue
        cfg.destination = p
        break

    # ── Auto-extract archives ─────────────────────────────────────────
    cfg.auto_extract = click.confirm(
        "\n  Auto-extract zip/rar/7z archives at destination?",
        default=cfg.auto_extract,
    )

    # ── Delete after extract ──────────────────────────────────────────
    if cfg.auto_extract:
        cfg.delete_after_extract = click.confirm(
            "  Delete archive after successful extraction?",
            default=cfg.delete_after_extract,
        )

    # ── Review & confirm ──────────────────────────────────────────────
    console.print()
    _show_config(cfg)
    console.print(f"\n  [dim]Config will be saved to: {cfg_path}[/dim]")

    if not click.confirm("\n  Save this configuration?", default=True):
        console.print("[yellow]Aborted — nothing was written.[/yellow]")
        return

    cfg.save(cfg_path)
    console.print(f"\n[green]Config saved to {cfg_path}[/green]")
    console.print("You can edit it later or re-run [bold]aborg config[/bold] at any time.")


@cli.command("config")
@click.option("--show", is_flag=True, help="Print current configuration.")
@click.pass_context
def config_cmd(ctx: click.Context, show: bool) -> None:
    """Show current configuration, or create a new one interactively."""
    cfg = ctx.obj["cfg"]
    cfg_path = ctx.obj.get("cfg_path") or DEFAULT_CONFIG_PATH

    if cfg is not None and show:
        _show_config(cfg)
        return

    if cfg is not None and not show:
        _show_config(cfg)
        console.print(f"\n  [dim]Loaded from: {cfg_path}[/dim]")
        return

    # No config exists — offer to create one
    console.print(f"[yellow]No config file found at {cfg_path}[/yellow]")
    if not click.confirm("Would you like to create one now?", default=True):
        return

    _config_wizard(cfg_path)


# ── rename (batch rename existing collection) ────────────────────────────


@cli.command()
@click.option(
    "--path",
    type=click.Path(exists=True),
    default=None,
    help="Collection root (defaults to configured destination).",
)
@click.option("--dry-run", is_flag=True, help="Show what would be renamed.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
@click.option("--cache", is_flag=True, help="Use cached results from previous scans.")
@click.pass_context
def rename(ctx: click.Context, path: str | None, dry_run: bool, yes: bool, cache: bool) -> None:
    """Rename folders in an existing collection to match Audiobookshelf conventions."""
    cfg = _require_cfg(ctx)
    root = Path(path) if path else cfg.destination
    if not _require_dir(root, "Collection directory"):
        ctx.exit(1)

    scan_cache = ScanCache() if cache else None

    with console.status(
        f"[bold green]Scanning collection at {root} …[/bold green]",
        spinner="dots",
    ):
        collection = scan_collection(root, cfg, read_tags=False, cache=scan_cache)
        items = collection.items

    if scan_cache:
        scan_cache.save()

    renames: list[tuple[Path, Path]] = []
    skipped: int = 0

    for item in items:
        if not item.path or item.meta.title == "Unknown Title":
            continue
        expected_name = item.meta.dest_folder_name()
        if item.path.name != expected_name:
            new_path = item.path.parent / expected_name
            if new_path.exists():
                console.print(
                    f"  [yellow]⚠ skip conflict:[/yellow] [dim]{item.path.name}[/dim]"
                    f"  [blue]→[/blue] [dim]{new_path.name}[/dim] (target exists)"
                )
                skipped += 1
                continue
            renames.append((item.path, new_path))

    if not renames:
        if skipped:
            console.print(
                f"Skipped {_quantity(skipped, 'conflict')}. No folders were renamed.",
                style="yellow",
            )
            ctx.exit(1)
        console.print("[green]All folders already match conventions.[/green]")
        return

    console.print(
        f"\n[bold]{'Would rename' if dry_run else 'Renaming'} {len(renames)} folder(s):[/bold]\n"
    )
    for i, (old, new) in enumerate(renames, 1):
        console.print(
            f"  [dim]{i:>3}.[/dim] [dim]{old.name}[/dim]  [blue]→[/blue] [green]{new.name}[/green]"
        )

    if skipped:
        console.print(f"\n[yellow]Skipped {skipped} conflict(s).[/yellow]")

    if not dry_run:
        if not yes and not click.confirm(f"\nRename {len(renames)} folder(s)?"):
            console.print("[yellow]Aborted.[/yellow]")
            return
        completed = 0
        failed = skipped
        for old, new in renames:
            try:
                if new.exists():
                    raise FileExistsError(f"Target already exists: {new}")
                old.rename(new)
                completed += 1
            except OSError as exc:
                failed += 1
                console.print(Text(f"Failed to rename {old}: {exc}", "red"))
        console.print(f"Renamed {_quantity(completed, 'folder')}.", style="green")
        if failed:
            ctx.exit(1)


# ── about ────────────────────────────────────────────────────────────────


def _get_git_commit() -> str | None:
    """Return the short commit hash of the installed package, or *None*."""
    pkg_dir = Path(__file__).resolve().parent
    # Walk up to find the repo root (contains .git)
    for ancestor in pkg_dir.parents:
        if (ancestor / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "-C", str(ancestor), "log", "-1", "--format=%h (%ci)"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except (OSError, subprocess.SubprocessError):  # git not found / failed
                pass
            break
    return None


@cli.command()
@click.option("-v", "--verbose", is_flag=True, help="Include Python build and executable details.")
@click.pass_context
def about(ctx: click.Context, verbose: bool) -> None:
    """Show version, environment, and project information."""
    console.print("aborg", style="bold")
    rows = [("Version", __version__)]
    commit = _get_git_commit()
    if commit:
        rows.append(("Last commit", commit))
    rows.append(("Python", platform.python_version()))
    if verbose:
        rows.extend([("Python build", sys.version), ("Executable", sys.executable)])
    rows.extend(
        [
            ("Install path", str(Path(__file__).resolve().parent)),
            ("Config path", str(ctx.obj["cfg_path"])),
            ("Repository", "https://github.com/robert-bryson/aborg"),
            ("Website", "https://rsmb.tv"),
            ("License", "MIT"),
        ]
    )
    _print_fields(rows)


# ── tldr ─────────────────────────────────────────────────────────────────


@cli.command()
def tldr() -> None:
    """Show common commands and quick-start examples."""
    text = """\
[bold underline]Quick Start[/bold underline]

  [bold]aborg config[/bold]              Set up aborg interactively (creates ~/.aborg/config.yaml)
  [bold]aborg config --show[/bold]       Show current configuration

[bold underline]Scanning & Organizing[/bold underline]

  [bold]aborg scan[/bold]                Discover audiobooks in your source directories
  [bold]aborg scan -d ~/Downloads[/bold] Scan an extra directory alongside configured sources
  [bold]aborg org[/bold]                 Scan and move new audiobooks into your collection
  [bold]aborg org --dry-run[/bold]       Preview what would be organized without making changes
  [bold]aborg org --copy[/bold]          Copy instead of move (keeps originals)
  [bold]aborg undo[/bold]                Undo the last organize operation

[bold underline]Collection Management[/bold underline]

  [bold]aborg analyze[/bold]             Check your collection for issues (duplicates, naming, etc.)
  [bold]aborg analyze --fix[/bold]       Auto-fix detected issues
  [bold]aborg analyze --fix --dry-run[/bold]  Preview fixes without applying
  [bold]aborg rename --dry-run[/bold]    Preview folder renames to match Audiobookshelf conventions
  [bold]aborg rename[/bold]              Apply folder renames

[bold underline]Libby / OverDrive[/bold underline]

  [bold]aborg fetch --setup CODE[/bold]  Link your Libby account with an 8-digit code
  [bold]aborg fetch --list[/bold]        List current audiobook loans
  [bold]aborg fetch --latest 1[/bold]    Download the most recent loan
  [bold]aborg fetch --latest 1 --organize[/bold]  Download and auto-organize into your collection

[bold underline]Utilities[/bold underline]

  [bold]aborg parse "Author - Title"[/bold]  Test how a filename would be parsed
  [bold]aborg about[/bold]               Show version and project info
"""
    console.print(text)
