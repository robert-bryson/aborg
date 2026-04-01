"""CLI entry point — ``aborg`` command."""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .analyzer import FixAction, analyze_collection, apply_fixes
from .cache import ScanCache
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
from .parser import AudiobookMeta, looks_like_author, merge_meta, normalize_path_name, parse_audio_tags, parse_filename, path_parent_name
from .scanner import scan_collection, scan_sources

console = Console()


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore[assignment]
    return f"{size:.1f} PB"


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
            capture_output=True, text=True, timeout=5,
        )
        for line in out.stdout.splitlines():
            if line.strip().startswith("Remote name"):
                return line.split(None, 2)[-1].strip()
    except Exception:
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
    if path.exists():
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
    cfg_path = Path(config_path) if config_path else None
    try:
        ctx.obj["cfg"] = Config.load(cfg_path)
    except FileNotFoundError:
        ctx.obj["cfg"] = None
        ctx.obj["cfg_path"] = cfg_path or DEFAULT_CONFIG_PATH


def _require_cfg(ctx: click.Context) -> Config:
    """Return the loaded Config or exit with a helpful message."""
    cfg = ctx.obj["cfg"]
    if cfg is not None:
        return cfg
    cfg_path = ctx.obj["cfg_path"]
    console.print(f"[red]Config file not found:[/red] {cfg_path}")
    console.print("[yellow]Run [bold]aborg config[/bold] to create one.[/yellow]")
    raise SystemExit(1)


# ── scan ─────────────────────────────────────────────────────────────────


@cli.command()
@click.option("-d", "--dir", "extra_dirs", multiple=True, help="Additional directories to scan.")
@click.option("--table", is_flag=True, help="Show results in a table instead of streaming.")
@click.option("--no-cache", is_flag=True, help="Ignore cached results and rescan everything.")
@click.pass_context
def scan(ctx: click.Context, extra_dirs: tuple[str, ...], table: bool, no_cache: bool) -> None:
    """Scan source directories and show discovered audiobook files."""
    cfg = _require_cfg(ctx)
    for d in extra_dirs:
        cfg.source_dirs.append(Path(d).expanduser())

    cache = None if no_cache else ScanCache()

    console.print(f"[dim]Scanning: {', '.join(str(d) for d in cfg.source_dirs)}[/dim]")
    console.print(f"[dim]Destination: {cfg.destination}[/dim]\n")

    count = 0
    new_count = 0
    exist_count = 0
    current_source_dir: Path | None = None

    with console.status("[bold green]Scanning…[/bold green]", spinner="dots") as status:

        def _on_progress(msg: str) -> None:
            status.update(f"[bold green]Scanning:[/bold green] {msg}")

        def _on_hit(result: object) -> None:
            nonlocal count, new_count, exist_count, current_source_dir
            count += 1
            if result.source_dir != current_source_dir:
                current_source_dir = result.source_dir
                console.print(f"\n[bold cyan]── {current_source_dir} ──[/bold cyan]")
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

        items, missing_dirs = scan_sources(
            cfg,
            on_progress=_on_progress,
            on_hit=_on_hit,
            cache=cache,
        )

    if cache:
        cache.save()

    if missing_dirs:
        console.print()
        for d in missing_dirs:
            console.print(f"[red bold]⚠ Source directory not found:[/red bold] {d}")
            hint = _check_wsl_mount(d)
            if hint:
                console.print(f"[yellow]  {hint}[/yellow]")

    if not items:
        if not missing_dirs:
            console.print("[yellow]No audiobook files found.[/yellow]")
        else:
            console.print("[yellow]No audiobook files found (check source directories above).[/yellow]")
        return

    total_size = sum(i.size for i in items)
    authors = len({i.meta.author for i in items})

    console.print()
    summary = Table(title="Scan Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Found", f"{len(items)} audiobook(s)")
    summary.add_row("New", f"[green]{new_count}[/green]")
    summary.add_row("Already in collection", f"[yellow]{exist_count}[/yellow]")
    summary.add_row("Authors", str(authors))
    summary.add_row("Total size", _human_size(total_size))
    if missing_dirs:
        summary.add_row("Missing source dirs", f"[red]{len(missing_dirs)}[/red]")
    console.print(summary)

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
@click.option("--no-cache", is_flag=True, help="Ignore cached results and rescan everything.")
@click.pass_context
def org(
    ctx: click.Context,
    extra_dirs: tuple[str, ...],
    dest: str | None,
    dry_run: bool,
    copy: bool,
    yes: bool,
    no_cache: bool,
) -> None:
    """Scan source directories and organize audiobooks into the destination."""
    cfg = _require_cfg(ctx)
    for d in extra_dirs:
        cfg.source_dirs.append(Path(d).expanduser())
    if dest:
        cfg.destination = Path(dest)

    cache = None if no_cache else ScanCache()

    console.print(f"[dim]Scanning: {', '.join(str(d) for d in cfg.source_dirs)}[/dim]")
    console.print(f"[dim]Destination: {cfg.destination}[/dim]\n")

    count = 0
    new_count = 0
    exist_count = 0
    current_source_dir: Path | None = None

    with console.status("[bold green]Scanning…[/bold green]", spinner="dots") as status:

        def _org_progress(msg: str) -> None:
            status.update(f"[bold green]Scanning:[/bold green] {msg}")

        def _org_hit(result: object) -> None:
            nonlocal count, new_count, exist_count, current_source_dir
            count += 1
            if result.source_dir != current_source_dir:
                current_source_dir = result.source_dir
                console.print(f"\n[bold cyan]── {current_source_dir} ──[/bold cyan]")
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

        items, missing_dirs = scan_sources(
            cfg,
            on_progress=_org_progress,
            on_hit=_org_hit,
            cache=cache,
        )

    if cache:
        cache.save()

    if missing_dirs:
        console.print()
        for d in missing_dirs:
            console.print(f"[red bold]⚠ Source directory not found:[/red bold] {d}")
            hint = _check_wsl_mount(d)
            if hint:
                console.print(f"[yellow]  {hint}[/yellow]")

    if not items:
        if not missing_dirs:
            console.print("[yellow]No audiobook files found.[/yellow]")
        else:
            console.print("[yellow]No audiobook files found (check source directories above).[/yellow]")
        return

    total_size = sum(i.size for i in items)
    authors = len({i.meta.author for i in items})

    prefix = "DRY RUN — " if dry_run else ""
    prompt = f"\n{prefix}Organize {new_count} new item(s)?"
    if not dry_run and not yes and not click.confirm(prompt):
        console.print("[yellow]Aborted.[/yellow]")
        return

    if not dry_run:
        dest = cfg.destination
        if not dest.exists():
            console.print(
                f"[red]Error:[/red] Destination does not exist: [bold]{dest}[/bold]\n"
                f"  Create it or check your config."
            )
            raise SystemExit(1)
        import tempfile
        try:
            with tempfile.NamedTemporaryFile(dir=dest):
                pass
        except OSError:
            console.print(
                f"[red]Error:[/red] Destination is not writable: [bold]{dest}[/bold]\n"
                f"  If this is a network mount, check that it is mounted with read-write permissions.\n"
                f"  Hint: [dim]mount | grep {dest.name}[/dim]"
            )
            raise SystemExit(1)

    console.print()
    verb = "Would move" if dry_run else ("Copying" if copy else "Moving")
    done = 0
    skipped = 0
    failed = 0
    with console.status(f"[bold green]{verb}…[/bold green]", spinner="dots") as status:
        for i, item in enumerate(items, 1):
            dest_full = cfg.destination / item.meta.dest_relative()
            if dest_full.exists():
                skipped += 1
                continue
            status.update(f"[bold green]{verb}:[/bold green] {item.meta.title}")
            result = organize([item], cfg, dry_run=dry_run, copy=copy)
            if result:
                done += len(result)
                console.print(
                    f"  [green]✓[/green] [dim]{i}.[/dim] {item.meta.author} — {item.meta.title}"
                )
            else:
                failed += 1

    verb_past = "Would organize" if dry_run else ("Copied" if copy else "Moved")

    console.print()
    summary = Table(title="Organize Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value", justify="right")
    summary.add_row("Found", f"{len(items)} audiobook(s)")
    summary.add_row(verb_past, f"[green]{done}[/green]")
    summary.add_row("Already in collection", f"[yellow]{skipped}[/yellow]")
    if failed:
        summary.add_row("Failed", f"[red]{failed}[/red]")
    summary.add_row("Authors", str(authors))
    summary.add_row("Total size", _human_size(total_size))
    if missing_dirs:
        summary.add_row("Missing source dirs", f"[red]{len(missing_dirs)}[/red]")
    console.print(summary)


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
    cfg = _require_cfg(ctx)

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
@click.option("--fix", is_flag=True, help="Apply automatic fixes for detected issues.")
@click.option("--dry-run", is_flag=True, help="Show what --fix would do without making changes.")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompt when using --fix.")
@click.option("--no-cache", is_flag=True, help="Ignore cached results and rescan everything.")
@click.option("--check-tags", is_flag=True, help="Read audio tags and check metadata quality (slower).")
@click.pass_context
def analyze(
    ctx: click.Context,
    path: str | None,
    fix: bool,
    dry_run: bool,
    yes: bool,
    no_cache: bool,
    check_tags: bool,
) -> None:
    """Analyze an existing audiobook collection and suggest improvements."""
    cfg = _require_cfg(ctx)
    root = Path(path) if path else cfg.destination

    if not _require_dir(root, "Collection directory"):
        return

    cache = None if no_cache else ScanCache()

    with console.status(
        f"[bold green]Analyzing {root} …[/bold green]",
        spinner="dots",
    ) as status:

        def _on_progress(msg: str) -> None:
            status.update(f"[bold green]Analyzing:[/bold green] {msg}")

        report = analyze_collection(
            root, cfg, on_progress=_on_progress, cache=cache, read_tags=check_tags,
        )

    if cache:
        cache.save()

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
        console.print(summary)
        console.print("\n[green]No issues found — collection looks great![/green]")
        return

    # Issues table
    console.print()
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
            console.print(
                f"  {icon} Rename: [dim]{action.source.name}[/dim]"
                f" [blue]\u2192[/blue] [green]{action.target.name}[/green]{reason}"
            )

    applied = apply_fixes(report, dry_run=dry_run, on_fix=_on_fix)

    if dry_run:
        console.print(f"\n[dim]{len(applied)} fix(es) would be applied.[/dim]")
    else:
        console.print(f"\n[green]Applied {len(applied)} fix(es).[/green]")

    console.print()
    console.print(summary)


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

    # ── Parse the folder/file name (same as scanner does) ────────────
    name_meta = parse_filename(name, cfg.filename_patterns)

    # ── Try ancestors for supplementary author context ────────────────
    normalized = filename.replace("\\", "/").rstrip("/")
    parts = [p for p in normalized.split("/") if p]

    parent = parts[-2] if len(parts) >= 2 else None
    parent_meta = AudiobookMeta()
    if parent:
        parent_parsed = parse_filename(parent, cfg.filename_patterns)
        if parent_parsed.author != "Unknown Author":
            parent_meta = parent_parsed
        elif looks_like_author(parent):
            parent_meta.author = parent

    # ── If it's an actual audio file, read tags (same as scanner) ────
    path = Path(filename)
    tag_meta = AudiobookMeta()
    has_tags = False
    if path.is_file() and path.suffix.lower() in cfg.audio_extensions:
        tag_meta = parse_audio_tags(path)
        has_tags = True

    # ── Merge sources (same priority as scanner: tags > name > parent)
    merged = merge_meta(tag_meta, name_meta, parent_meta)

    # If merged author is obviously wrong, search path ancestors for a
    # clean author name (skip "Author - Title" style components).
    if not looks_like_author(merged.author) or merged.author == "Unknown Author":
        for comp in reversed(parts[:-1]):
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
    table.add_row("Dest path", str(merged.dest_relative()))
    console.print(table)


# ── undo ─────────────────────────────────────────────────────────────────


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be undone.")
@click.pass_context
def undo(ctx: click.Context, dry_run: bool) -> None:
    """Undo the most recent organize operation."""
    cfg = _require_cfg(ctx)
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
        prompt = f"  Source dir [{len(dirs) + 1}]" if not dirs else f"  Source dir [{len(dirs) + 1}] (blank to finish)"
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
        "\n  Auto-extract zip/rar/7z archives at destination?", default=cfg.auto_extract,
    )

    # ── Delete after extract ──────────────────────────────────────────
    if cfg.auto_extract:
        cfg.delete_after_extract = click.confirm(
            "  Delete archive after successful extraction?", default=cfg.delete_after_extract,
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
@click.option("--no-cache", is_flag=True, help="Ignore cached results and rescan everything.")
@click.pass_context
def rename(ctx: click.Context, path: str | None, dry_run: bool, no_cache: bool) -> None:
    """Rename folders in an existing collection to match Audiobookshelf conventions."""
    cfg = _require_cfg(ctx)
    root = Path(path) if path else cfg.destination

    cache = None if no_cache else ScanCache()

    with console.status(
        f"[bold green]Scanning collection at {root} …[/bold green]",
        spinner="dots",
    ):
        collection = scan_collection(root, cfg, cache=cache)
        items = collection.items

    if cache:
        cache.save()

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
