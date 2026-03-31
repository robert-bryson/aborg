"""CLI entry point — ``aborg`` command."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .analyzer import analyze_collection
from .config import Config
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
@click.option("--table", is_flag=True, help="Show results in a table instead of streaming.")
@click.pass_context
def scan(ctx: click.Context, extra_dirs: tuple[str, ...], table: bool) -> None:
    """Scan source directories and show discovered audiobook files."""
    cfg: Config = ctx.obj["cfg"]
    for d in extra_dirs:
        cfg.source_dirs.append(Path(d).expanduser())

    console.print(f"[dim]Scanning: {', '.join(str(d) for d in cfg.source_dirs)}[/dim]")
    console.print(f"[dim]Destination: {cfg.destination}[/dim]\n")

    count = 0
    new_count = 0
    exist_count = 0

    with console.status("[bold green]Scanning…[/bold green]", spinner="dots") as status:

        def _on_progress(msg: str) -> None:
            status.update(f"[bold green]Scanning:[/bold green] {msg}")

        def _on_hit(result: object) -> None:
            nonlocal count, new_count, exist_count
            count += 1
            dest_full = cfg.destination / result.meta.dest_relative()
            exists = dest_full.exists()
            if exists:
                exist_count += 1
                tag = "[yellow] EXISTS [/yellow]"
            else:
                new_count += 1
                tag = "[green]    NEW [/green]"
            series = ""
            if result.meta.series:
                seq = result.meta.sequence or "?"
                series = f"  [dim]({result.meta.series} #{seq})[/dim]"
            console.print(
                f"{tag} [dim]{count:>3}.[/dim]"
                f" [bold]{result.meta.author}[/bold] —"
                f" {result.meta.title}{series}"
                f"  [dim]{_human_size(result.size)}[/dim]"
                f"  [blue]→ {result.meta.dest_relative()}[/blue]"
            )

        items = scan_sources(
            cfg,
            on_progress=_on_progress,
            on_hit=_on_hit,
        )

    if not items:
        console.print("[yellow]No audiobook files found.[/yellow]")
        return

    parts = [f"Found [bold]{len(items)}[/bold] audiobook(s)"]
    if new_count:
        parts.append(f"[green]{new_count} new[/green]")
    if exist_count:
        parts.append(f"[yellow]{exist_count} already in collection[/yellow]")
    console.print("\n" + ", ".join(parts) + ".")

    if table:
        tbl = Table(show_lines=True)
        tbl.add_column("#", style="dim", width=3)
        tbl.add_column("Author", style="green", no_wrap=True)
        tbl.add_column("Title", style="bold", no_wrap=True)
        tbl.add_column("Series", no_wrap=True)
        tbl.add_column("Size", justify="right", no_wrap=True)
        tbl.add_column("Dest path", style="blue")

        for i, item in enumerate(items, 1):
            tbl.add_row(
                str(i),
                item.meta.author,
                item.meta.title,
                f"{item.meta.series} #{item.meta.sequence}" if item.meta.series else "",
                _human_size(item.size),
                str(item.meta.dest_relative()),
            )

        console.print(tbl)


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

    console.print(f"[dim]Scanning: {', '.join(str(d) for d in cfg.source_dirs)}[/dim]")
    console.print(f"[dim]Destination: {cfg.destination}[/dim]\n")

    count = 0
    new_count = 0
    exist_count = 0

    with console.status("[bold green]Scanning…[/bold green]", spinner="dots") as status:

        def _org_progress(msg: str) -> None:
            status.update(f"[bold green]Scanning:[/bold green] {msg}")

        def _org_hit(result: object) -> None:
            nonlocal count, new_count, exist_count
            count += 1
            dest_full = cfg.destination / result.meta.dest_relative()
            exists = dest_full.exists()
            if exists:
                exist_count += 1
                tag = "[yellow] EXISTS [/yellow]"
            else:
                new_count += 1
                tag = "[green]    NEW [/green]"
            series = ""
            if result.meta.series:
                seq = result.meta.sequence or "?"
                series = f"  [dim]({result.meta.series} #{seq})[/dim]"
            console.print(
                f"{tag} [dim]{count:>3}.[/dim]"
                f" [bold]{result.meta.author}[/bold] —"
                f" {result.meta.title}{series}"
                f"  [dim]{_human_size(result.size)}[/dim]"
                f"  [blue]→ {result.meta.dest_relative()}[/blue]"
            )

        items = scan_sources(
            cfg,
            on_progress=_org_progress,
            on_hit=_org_hit,
        )

    if not items:
        console.print("[yellow]No audiobook files found.[/yellow]")
        return

    parts = [f"Found [bold]{len(items)}[/bold] audiobook(s)"]
    if new_count:
        parts.append(f"[green]{new_count} new[/green]")
    if exist_count:
        parts.append(f"[yellow]{exist_count} already in collection[/yellow]")
    console.print("\n" + ", ".join(parts) + ".")

    prefix = "DRY RUN — " if dry_run else ""
    prompt = f"\n{prefix}Organize {new_count} new item(s)?"
    if not dry_run and not yes and not click.confirm(prompt):
        console.print("[yellow]Aborted.[/yellow]")
        return

    console.print()
    verb = "Would move" if dry_run else ("Copying" if copy else "Moving")
    done = 0
    with console.status(f"[bold green]{verb}…[/bold green]", spinner="dots") as status:
        for i, item in enumerate(items, 1):
            dest_full = cfg.destination / item.meta.dest_relative()
            if dest_full.exists():
                continue
            status.update(f"[bold green]{verb}:[/bold green] {item.meta.title}")
            result = organize([item], cfg, dry_run=dry_run, copy=copy)
            if result:
                done += len(result)
                console.print(
                    f"  [green]✓[/green] [dim]{i}.[/dim] {item.meta.author} — {item.meta.title}"
                )

    verb_past = "Would move" if dry_run else ("Copied" if copy else "Moved")
    console.print(f"\n[green]{verb_past} {done} item(s).[/green]")


# ── fetch (Libby / OverDrive) ───────────────────────────────────────────


@cli.command()
@click.option("--setup", "setup_code", default=None, help="8-digit Libby setup code to link account.")
@click.option("--list", "list_only", is_flag=True, help="List current audiobook loans and exit.")
@click.option("--latest", type=int, default=None, help="Download the latest N loans non-interactively.")
@click.option("--select", "select_ids", multiple=True, help="Download specific loan(s) by ID.")
@click.option("--all", "fetch_all", is_flag=True, help="Download all current audiobook loans.")
@click.option("-d", "--download-dir", type=click.Path(), default=None, help="Override download directory (defaults to first source_dir).")
@click.option("--organize", "auto_organize", is_flag=True, help="Automatically organize after downloading.")
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
    cfg: Config = ctx.obj["cfg"]

    # Check odmpy is installed
    if not check_odmpy():
        console.print(
            "[red]odmpy is not installed.[/red]\n"
            "Install it with: [bold]uv pip install .[/bold]"
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
            loans = list_loans(settings)

        if not loans:
            console.print("[yellow]No downloadable audiobook loans found.[/yellow]")
            return

        table = Table(title="Audiobook Loans")
        table.add_column("#", style="dim", width=3)
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Author", style="green", no_wrap=True)
        table.add_column("Title", style="bold")

        for loan in loans:
            table.add_row(str(loan.index), loan.id, loan.author, loan.title)

        console.print(table)
        console.print(
            f"\n[dim]Use [bold]aborg fetch --select <ID>[/bold] or "
            f"[bold]aborg fetch --latest N[/bold] to download.[/dim]"
        )
        return

    # ── Determine download target dir ──
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
            ok, output = download_latest(
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
            console.print(f"[green]Download complete.[/green]")
        else:
            console.print(f"[red]Download failed:[/red] {output}")
            ctx.exit(1)
            return

        if auto_organize:
            console.print()
            ctx.invoke(org, extra_dirs=(), dest=None, dry_run=False, copy=False, yes=True)
        return

    # ── Download by ID(s) ──
    if select_ids or fetch_all:
        with console.status("[bold green]Fetching loans…[/bold green]", spinner="dots"):
            loans = list_loans(settings)

        if not loans:
            console.print("[yellow]No downloadable audiobook loans found.[/yellow]")
            return

        if fetch_all:
            to_download = loans
        else:
            id_set = set(select_ids)
            to_download = [l for l in loans if l.id in id_set]
            if not to_download:
                console.print(f"[red]No loans matched IDs: {', '.join(select_ids)}[/red]")
                console.print("[dim]Use [bold]aborg fetch --list[/bold] to see available IDs.[/dim]")
                ctx.exit(1)
                return

        if dry_run:
            console.print(f"[dim]DRY RUN — would download {len(to_download)} loan(s):[/dim]")
            for loan in to_download:
                console.print(f"  [dim]{loan.index}.[/dim] {loan.author} — {loan.title}")
            return

        console.print(f"Downloading [bold]{len(to_download)}[/bold] loan(s) to {dl_dir}…\n")
        results: list[FetchResult] = []
        for loan in to_download:
            console.print(f"  [dim]↓[/dim] {loan.author} — [bold]{loan.title}[/bold]")
            with console.status(f"[bold green]Downloading:[/bold green] {loan.title}", spinner="dots"):
                result = download_loan(
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
            ctx.invoke(org, extra_dirs=(), dest=None, dry_run=False, copy=False, yes=True)
        return

    # ── No action specified — show help ──
    console.print(
        "[yellow]No action specified.[/yellow] Use one of:\n"
        "  [bold]aborg fetch --setup CODE[/bold]    Link your Libby account\n"
        "  [bold]aborg fetch --list[/bold]           List current loans\n"
        "  [bold]aborg fetch --latest N[/bold]       Download latest N loans\n"
        "  [bold]aborg fetch --select ID[/bold]      Download specific loan by ID\n"
        "  [bold]aborg fetch --all[/bold]            Download all loans"
    )


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

    with console.status(
        f"[bold green]Analyzing {root} …[/bold green]",
        spinner="dots",
    ):
        report = analyze_collection(root, cfg)

    console.print()
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

    verb = "Would restore" if dry_run else "Restored"
    console.print(f"\n[bold]{verb} {len(actions)} item(s):[/bold]\n")
    for i, (src, dest) in enumerate(actions, 1):
        console.print(
            f"  [green]↩[/green] [dim]{i:>3}.[/dim]"
            f" [dim]{src.name}[/dim]"
            f"  [blue]→[/blue] [green]{dest}[/green]"
        )

    console.print(f"\n[green]{verb} {len(actions)} item(s).[/green]")


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

    with console.status(
        f"[bold green]Scanning collection at {root} …[/bold green]",
        spinner="dots",
    ):
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

    console.print(
        f"\n[bold]{'Would rename' if dry_run else 'Renaming'} {len(renames)} folder(s):[/bold]\n"
    )
    for i, (old, new) in enumerate(renames, 1):
        console.print(
            f"  [dim]{i:>3}.[/dim] [dim]{old.name}[/dim]  [blue]→[/blue] [green]{new.name}[/green]"
        )
        if not dry_run:
            old.rename(new)

    if not dry_run:
        console.print(f"\n[green]Renamed {len(renames)} folder(s).[/green]")
