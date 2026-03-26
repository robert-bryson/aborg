"""CLI entry point — ``aborg`` command."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .analyzer import analyze_collection
from .config import Config
from .organizer import organize, undo_last
from .parser import parse_filename
from .scanner import scan_collection, scan_sources

console = Console()


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"


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
    cfg_path = Path(config_path) if config_path else None
    ctx.obj["cfg"] = Config.load(cfg_path)


# ── scan ─────────────────────────────────────────────────────────────────


@cli.command()
@click.option("-d", "--dir", "extra_dirs", multiple=True, help="Additional directories to scan.")
@click.pass_context
def scan(ctx: click.Context, extra_dirs: tuple[str, ...]) -> None:
    """Scan source directories and show discovered audiobook files."""
    cfg: Config = ctx.obj["cfg"]
    for d in extra_dirs:
        cfg.source_dirs.append(Path(d).expanduser())

    items = scan_sources(cfg)
    if not items:
        console.print("[yellow]No audiobook files found.[/yellow]")
        return

    table = Table(title=f"Found {len(items)} audiobook(s)")
    table.add_column("Type", style="cyan", width=10)
    table.add_column("Author", style="green")
    table.add_column("Title", style="bold")
    table.add_column("Series")
    table.add_column("Size", justify="right")
    table.add_column("Source")

    for item in items:
        table.add_row(
            item.kind,
            item.meta.author,
            item.meta.title,
            f"{item.meta.series} #{item.meta.sequence}" if item.meta.series else "",
            _human_size(item.size),
            str(item.path.name),
        )

    console.print(table)


# ── organize ─────────────────────────────────────────────────────────────


@cli.command()
@click.option("-d", "--dir", "extra_dirs", multiple=True, help="Additional directories to scan.")
@click.option("--dest", type=click.Path(), default=None, help="Override destination directory.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes.")
@click.option("--copy", is_flag=True, help="Copy instead of move.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def org(
    ctx: click.Context,
    extra_dirs: tuple[str, ...],
    dest: str | None,
    dry_run: bool,
    copy: bool,
    yes: bool,
) -> None:
    """Scan source directories and organize audiobooks into the destination."""
    cfg: Config = ctx.obj["cfg"]
    for d in extra_dirs:
        cfg.source_dirs.append(Path(d).expanduser())
    if dest:
        cfg.destination = Path(dest)

    items = scan_sources(cfg)
    if not items:
        console.print("[yellow]No audiobook files found.[/yellow]")
        return

    # Preview
    prefix = "DRY RUN — " if dry_run else ""
    console.print(f"\n[bold]{prefix}Organizing {len(items)} item(s) → {cfg.destination}[/bold]\n")

    table = Table()
    table.add_column("Source", style="dim")
    table.add_column("→")
    table.add_column("Destination", style="green")

    for item in items:
        rel_dest = item.meta.dest_relative()
        table.add_row(str(item.path.name), "→", str(cfg.destination / rel_dest))

    console.print(table)

    if not dry_run and not yes and not click.confirm("\nProceed?"):
        console.print("[yellow]Aborted.[/yellow]")
        return

    actions = organize(items, cfg, dry_run=dry_run, copy=copy)
    verb = "Would move" if dry_run else ("Copied" if copy else "Moved")
    console.print(f"\n[green]{verb} {len(actions)} item(s).[/green]")


# ── analyze ──────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--path",
    type=click.Path(exists=True),
    default=None,
    help="Collection root to analyze (defaults to configured destination).",
)
@click.pass_context
def analyze(ctx: click.Context, path: str | None) -> None:
    """Analyze an existing audiobook collection and suggest improvements."""
    cfg: Config = ctx.obj["cfg"]
    root = Path(path) if path else cfg.destination

    if not root.exists():
        console.print(f"[red]Directory not found: {root}[/red]")
        return

    console.print(f"[bold]Analyzing collection at {root} …[/bold]\n")
    report = analyze_collection(root, cfg)

    # Summary
    summary = Table(title="Collection Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Total books", str(report.total_books))
    summary.add_row("Total size", _human_size(report.total_size))
    summary.add_row("Authors", str(report.authors))
    summary.add_row("Series", str(report.series))
    summary.add_row("Issues", str(len(report.issues)))
    console.print(summary)

    if not report.issues:
        console.print("\n[green]No issues found — collection looks great![/green]")
        return

    # Issues table
    console.print()
    issues_table = Table(title="Issues")
    issues_table.add_column("Sev", width=7)
    issues_table.add_column("Category", width=12)
    issues_table.add_column("Message")
    issues_table.add_column("Suggestion", style="dim")

    severity_style = {"error": "red bold", "warning": "yellow", "info": "blue"}
    for issue in report.issues:
        issues_table.add_row(
            f"[{severity_style.get(issue.severity, '')}]{issue.severity}[/]",
            issue.category,
            issue.message,
            issue.suggestion or "",
        )

    console.print(issues_table)

    # Duplicate details
    if report.duplicates:
        console.print(f"\n[yellow]Found {len(report.duplicates)} possible duplicate(s).[/yellow]")

    if report.author_variants:
        n = len(report.author_variants)
        msg = f"Found {n} similar author name(s) that may need standardizing."
        console.print(f"\n[blue]{msg}[/blue]")


# ── parse (utility) ─────────────────────────────────────────────────────


@cli.command()
@click.argument("filename")
@click.pass_context
def parse(ctx: click.Context, filename: str) -> None:
    """Parse a filename and show what metadata would be extracted."""
    cfg: Config = ctx.obj["cfg"]
    meta = parse_filename(filename, cfg.filename_patterns)

    table = Table(title=f"Parsed: {filename}", show_header=False)
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Author", meta.author)
    table.add_row("Title", meta.title)
    table.add_row("Series", meta.series or "—")
    table.add_row("Sequence", meta.sequence or "—")
    table.add_row("Year", meta.year or "—")
    table.add_row("Narrator", meta.narrator or "—")
    table.add_row("Dest folder", meta.dest_folder_name())
    table.add_row("Dest path", str(meta.dest_relative()))
    console.print(table)


# ── undo ─────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be undone.")
@click.pass_context
def undo(ctx: click.Context, dry_run: bool) -> None:
    """Undo the most recent organize operation."""
    cfg: Config = ctx.obj["cfg"]
    actions = undo_last(cfg, dry_run=dry_run)

    if not actions:
        console.print("[yellow]Nothing to undo.[/yellow]")
        return

    table = Table(title=f"{'Would undo' if dry_run else 'Undone'}: {len(actions)} action(s)")
    table.add_column("From")
    table.add_column("→")
    table.add_column("Restored to", style="green")

    for src, dest in actions:
        table.add_row(str(src), "→", str(dest))

    console.print(table)


# ── config ───────────────────────────────────────────────────────────────


@cli.command("config")
@click.option("--init", is_flag=True, help="Create a default config file.")
@click.option("--show", is_flag=True, help="Print current configuration.")
@click.pass_context
def config_cmd(ctx: click.Context, init: bool, show: bool) -> None:
    """Manage configuration."""
    cfg: Config = ctx.obj["cfg"]

    if init:
        cfg.save()
        console.print(f"[green]Config written to {cfg.move_log.parent / 'config.yaml'}[/green]")
        return

    if show or not init:
        table = Table(title="Current Configuration", show_header=False)
        table.add_column("Key", style="bold")
        table.add_column("Value")
        table.add_row("Source dirs", ", ".join(str(d) for d in cfg.source_dirs))
        table.add_row("Destination", str(cfg.destination))
        table.add_row("Auto extract", str(cfg.auto_extract))
        table.add_row("Delete after extract", str(cfg.delete_after_extract))
        table.add_row("Min file size", _human_size(cfg.min_file_size))
        table.add_row("Move log", str(cfg.move_log))
        table.add_row("Archive exts", ", ".join(sorted(cfg.archive_extensions)))
        table.add_row("Audio exts", ", ".join(sorted(cfg.audio_extensions)))
        table.add_row("Patterns", f"{len(cfg.filename_patterns)} pattern(s)")
        console.print(table)


# ── rename (batch rename existing collection) ────────────────────────────


@cli.command()
@click.option(
    "--path",
    type=click.Path(exists=True),
    default=None,
    help="Collection root (defaults to configured destination).",
)
@click.option("--dry-run", is_flag=True, help="Show what would be renamed.")
@click.pass_context
def rename(ctx: click.Context, path: str | None, dry_run: bool) -> None:
    """Rename folders in an existing collection to match Audiobookshelf conventions."""
    cfg: Config = ctx.obj["cfg"]
    root = Path(path) if path else cfg.destination

    items = scan_collection(root, cfg)
    renames: list[tuple[Path, Path]] = []

    for item in items:
        if not item.path or item.meta.title == "Unknown Title":
            continue
        expected_name = item.meta.dest_folder_name()
        if item.path.name != expected_name:
            new_path = item.path.parent / expected_name
            renames.append((item.path, new_path))

    if not renames:
        console.print("[green]All folders already match conventions.[/green]")
        return

    table = Table(title=f"{'Would rename' if dry_run else 'Renaming'} {len(renames)} folder(s)")
    table.add_column("Current", style="dim")
    table.add_column("→")
    table.add_column("New", style="green")

    for old, new in renames:
        table.add_row(old.name, "→", new.name)

    console.print(table)

    if not dry_run:
        for old, new in renames:
            old.rename(new)
        console.print(f"\n[green]Renamed {len(renames)} folder(s).[/green]")
