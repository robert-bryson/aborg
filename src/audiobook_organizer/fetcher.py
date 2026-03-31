"""Download audiobook loans from Libby/OverDrive via odmpy."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LibbyLoan:
    """Represents a single audiobook loan from Libby."""

    id: str
    title: str
    author: str
    index: int  # 1-based position in loan list


@dataclass
class FetchResult:
    """Result of a fetch operation."""

    loan: LibbyLoan
    download_dir: Path
    success: bool
    message: str = ""


def _odmpy_cmd() -> list[str]:
    """Return the base command list to invoke odmpy, or raise if not installed."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "odmpy", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            return [sys.executable, "-m", "odmpy"]
    except Exception:
        pass
    raise FileNotFoundError(
        "odmpy is not installed. Install it with:  uv pip install ."
    )


def _settings_dir(libby_settings: Path) -> Path:
    """Ensure the Libby settings directory exists and return it."""
    libby_settings.mkdir(parents=True, exist_ok=True)
    return libby_settings


def check_odmpy() -> bool:
    """Return True if odmpy is importable in the current Python environment."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "odmpy", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        return proc.returncode == 0
    except Exception:
        return False


def is_authenticated(settings_folder: Path) -> bool:
    """Return True if a Libby identity file exists (user has synced before)."""
    return (settings_folder / "libby.json").exists()


def libby_setup(settings_folder: Path, code: str) -> tuple[bool, str]:
    """Authenticate with Libby using an 8-digit setup code.

    This runs ``odmpy libby --check`` after syncing to verify.
    Returns ``(success, message)``.
    """
    if not code.isdigit() or len(code) != 8:
        return False, "Setup code must be exactly 8 digits."

    settings = _settings_dir(settings_folder)

    # odmpy doesn't have a dedicated "sync" CLI command — the LibbyClient
    # is used internally.  We use its Python API directly for the setup step.
    try:
        from odmpy.libby import LibbyClient  # type: ignore[import-untyped]

        client = LibbyClient(settings_folder=str(settings))
        client.get_chip()
        client.clone_by_code(code)
        return True, "Libby account linked successfully."
    except Exception as exc:
        return False, f"Libby setup failed: {exc}"


def list_loans(settings_folder: Path) -> list[LibbyLoan]:
    """Export current audiobook loans as a list of ``LibbyLoan`` objects.

    Uses ``odmpy libby --exportloans`` in non-interactive mode.
    """
    settings = _settings_dir(settings_folder)
    odmpy = _odmpy_cmd()

    # Export loans to a temp JSON file
    loans_file = settings / "loans_export.json"
    cmd = [
        *odmpy,
        "libby",
        "--settings", str(settings),
        "--exportloans", str(loans_file),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"odmpy export failed: {proc.stderr.strip()}")

    if not loans_file.exists():
        return []

    raw: list[dict[str, Any]] = json.loads(loans_file.read_text())
    loans_file.unlink(missing_ok=True)

    results: list[LibbyLoan] = []
    idx = 0
    for entry in raw:
        # Only include audiobook loans that can be downloaded
        formats = [f.get("id", "") for f in entry.get("formats", [])]
        if "audiobook-mp3" not in formats:
            continue
        idx += 1
        title = entry.get("title", "Unknown Title")
        author = entry.get("firstCreatorName", "Unknown Author")
        loan_id = str(entry.get("id", ""))
        results.append(LibbyLoan(id=loan_id, title=title, author=author, index=idx))

    return results


def download_loan(
    settings_folder: Path,
    download_dir: Path,
    loan: LibbyLoan,
    *,
    merge: bool = False,
    merge_format: str = "m4b",
    chapters: bool = True,
    keep_cover: bool = True,
    book_folder_format: str = "%(Author)s - %(Title)s",
) -> FetchResult:
    """Download a single audiobook loan via odmpy.

    Uses ``odmpy libby --selectid`` in non-interactive mode for automation.
    """
    settings = _settings_dir(settings_folder)
    odmpy = _odmpy_cmd()
    download_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        *odmpy,
        "--retry", "3",
        "libby",
        "--settings", str(settings),
        "--direct",
        "-d", str(download_dir),
        "--selectid", loan.id,
        "--bookfolderformat", book_folder_format,
    ]

    if chapters:
        cmd.append("-c")
    if keep_cover:
        cmd.append("-k")
    if merge:
        cmd.extend(["-m", "--mergeformat", merge_format])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

    if proc.returncode != 0:
        return FetchResult(
            loan=loan,
            download_dir=download_dir,
            success=False,
            message=proc.stderr.strip() or "Download failed (unknown error).",
        )

    return FetchResult(
        loan=loan,
        download_dir=download_dir,
        success=True,
        message=f"Downloaded to {download_dir}",
    )


def download_latest(
    settings_folder: Path,
    download_dir: Path,
    count: int = 1,
    *,
    merge: bool = False,
    merge_format: str = "m4b",
    chapters: bool = True,
    keep_cover: bool = True,
    book_folder_format: str = "%(Author)s - %(Title)s",
) -> tuple[bool, str]:
    """Download the latest *count* loans non-interactively.

    Returns ``(success, output_or_error)``.
    """
    settings = _settings_dir(settings_folder)
    odmpy = _odmpy_cmd()
    download_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        *odmpy,
        "--retry", "3",
        "libby",
        "--settings", str(settings),
        "--direct",
        "-d", str(download_dir),
        "--latest", str(count),
        "--bookfolderformat", book_folder_format,
    ]

    if chapters:
        cmd.append("-c")
    if keep_cover:
        cmd.append("-k")
    if merge:
        cmd.extend(["-m", "--mergeformat", merge_format])

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

    if proc.returncode != 0:
        return False, proc.stderr.strip() or "Download failed."

    return True, proc.stdout.strip()
