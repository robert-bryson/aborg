"""Analyze an existing audiobook collection and suggest improvements."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cache import ScanCache

from .config import Config
from .parser import flip_author_name, is_last_first, looks_like_author
from .scanner import CollectionScan, ScanResult, scan_collection


@dataclass
class FixAction:
    """An automated fix that can be applied for an issue."""

    kind: str  # "remove_dir", "rename"
    source: Path
    target: Path | None = None  # for renames


@dataclass
class Issue:
    """A single detected issue in the collection."""

    severity: str  # "error", "warning", "info"
    category: str
    message: str
    path: Path | None = None
    suggestion: str | None = None
    fix: FixAction | None = None


@dataclass
class AnalysisReport:
    """Full analysis report for a collection."""

    total_books: int = 0
    total_size: int = 0
    authors: int = 0
    series: int = 0
    items: list[ScanResult] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    duplicates: list[tuple[ScanResult, ScanResult]] = field(default_factory=list)
    author_variants: list[tuple[str, str]] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]


# Re-export the callback type aliases so CLI can use a single import.
ProgressCallback = Callable[[str], None]


def analyze_collection(
    root: Path,
    cfg: Config,
    *,
    on_progress: ProgressCallback | None = None,
    cache: ScanCache | None = None,
    read_tags: bool = False,
) -> AnalysisReport:
    """Analyze the collection at *root* and return a detailed report."""
    _log = on_progress or (lambda _msg: None)

    _log("Scanning collection…")
    collection = scan_collection(root, cfg, on_progress=on_progress, read_tags=read_tags, cache=cache)
    items = collection.items
    report = AnalysisReport()
    report.total_books = len(items)
    report.total_size = sum(i.size for i in items)
    report.items = items

    authors: set[str] = set()
    series_set: set[str] = set()
    for item in items:
        authors.add(item.meta.author)
        if item.meta.series:
            series_set.add(item.meta.series)
    report.authors = len(authors)
    report.series = len(series_set)

    # Run checks — metadata, duplicates, and naming are pure in-memory.
    # Empty dirs, flat files, and cover art use data already collected
    # by the single-pass scan (no extra filesystem I/O).
    _log("Checking metadata…")
    _check_unknown_metadata(items, report)
    _log("Checking for duplicates…")
    _check_duplicates(items, report)
    _log("Checking author name variants…")
    _check_author_variants(authors, report)
    _log("Checking for empty directories…")
    _check_empty_dirs(collection, report)
    _log("Checking directory structure…")
    _check_flat_files(collection, cfg, report)
    _log("Checking cover art…")
    _check_missing_covers(items, report)
    _log("Checking naming conventions…")
    _check_naming_conventions(items, report)
    _log("Checking author name format consistency…")
    _check_author_name_format(items, root, cfg, report)
    if read_tags:
        _log("Checking metadata quality…")
        _check_metadata_quality(items, report)

    return report


def _check_unknown_metadata(items: list[ScanResult], report: AnalysisReport) -> None:
    for item in items:
        if item.meta.author == "Unknown Author":
            report.issues.append(
                Issue(
                    severity="warning",
                    category="metadata",
                    message=f"Unknown author for '{item.meta.title}'",
                    path=item.path,
                    suggestion="Add author metadata or rename using 'Author - Title' format",
                )
            )
        if item.meta.title == "Unknown Title":
            report.issues.append(
                Issue(
                    severity="warning",
                    category="metadata",
                    message="Unknown title",
                    path=item.path,
                    suggestion="Add title metadata or rename the file/folder",
                )
            )


def _check_duplicates(items: list[ScanResult], report: AnalysisReport) -> None:
    """Detect possible duplicate audiobooks by fuzzy title matching."""
    by_author: defaultdict[str, list[ScanResult]] = defaultdict(list)
    for item in items:
        by_author[item.meta.author.lower()].append(item)

    for author_items in by_author.values():
        n = len(author_items)
        for i in range(n):
            a = author_items[i]
            for j in range(i + 1, n):
                b = author_items[j]
                ratio = SequenceMatcher(None, a.meta.title.lower(), b.meta.title.lower()).ratio()
                if ratio > 0.85:
                    report.duplicates.append((a, b))
                    report.issues.append(
                        Issue(
                            severity="warning",
                            category="duplicate",
                            message=f"Possible duplicate: '{a.meta.title}' vs '{b.meta.title}'",
                            path=a.path,
                            suggestion=f"Compare with {b.path}",
                        )
                    )


def _check_author_variants(authors: set[str], report: AnalysisReport) -> None:
    """Detect similar author names that might be variants of the same person."""
    author_list = sorted(authors)
    for i, a in enumerate(author_list):
        for b in author_list[i + 1 :]:
            ratio = SequenceMatcher(None, a.lower(), b.lower()).ratio()
            if 0.75 < ratio < 1.0:
                report.author_variants.append((a, b))
                report.issues.append(
                    Issue(
                        severity="info",
                        category="naming",
                        message=f"Similar author names: '{a}' and '{b}'",
                        suggestion="Consider standardizing to one name",
                    )
                )


def _check_empty_dirs(collection: CollectionScan, report: AnalysisReport) -> None:
    """Flag empty directories discovered during the scan."""
    for d in collection.empty_dirs:
        report.issues.append(
            Issue(
                severity="info",
                category="cleanup",
                message="Empty directory",
                path=d,
                suggestion="Remove empty directory",
                fix=FixAction(kind="remove_dir", source=d),
            )
        )


def _check_flat_files(collection: CollectionScan, cfg: Config, report: AnalysisReport) -> None:
    """Flag audio files sitting directly in the root (not in Author/Title structure)."""
    for f in collection.flat_audio_files:
        report.issues.append(
            Issue(
                severity="error",
                category="structure",
                message=f"Audio file in root directory: {f.name}",
                path=f,
                suggestion="Move into Author/Title folder structure",
            )
        )


def _check_missing_covers(items: list[ScanResult], report: AnalysisReport) -> None:
    """Flag audiobooks without cover art (using cached has_cover from scan)."""
    for item in items:
        if item.kind != "audio_dir" or item.path is None:
            continue
        if not item.has_cover:
            report.issues.append(
                Issue(
                    severity="info",
                    category="metadata",
                    message=f"No cover art found for '{item.meta.title}'",
                    path=item.path,
                    suggestion="Add a cover.jpg file",
                )
            )


# _is_last_first and _flip_author_name are imported from parser.


def _check_author_name_format(
    items: list[ScanResult], root: Path, cfg: Config, report: AnalysisReport,
) -> None:
    """Flag author folders that don't match the configured name format."""
    prefer_last_first = cfg.author_name_format == "last_first"
    preferred_label = "Last, First" if prefer_last_first else "First Last"

    # Collect unique author dirs (author_name -> author_dir_path).
    author_dirs: dict[str, Path] = {}
    for item in items:
        author = item.meta.author
        if author == "Unknown Author" or not item.path:
            continue
        if author not in author_dirs:
            author_dirs[author] = root / item.path.relative_to(root).parts[0]

    for author in sorted(author_dirs):
        is_lf = is_last_first(author)
        if is_lf == prefer_last_first:
            continue  # Already in the preferred format
        suggested = flip_author_name(author)
        if suggested == author:
            continue  # Can't meaningfully flip (e.g. single-word name)
        author_dir = author_dirs[author]
        target_dir = author_dir.parent / suggested
        report.issues.append(
            Issue(
                severity="warning",
                category="naming",
                message=(
                    f"Author '{author}' doesn't match preferred format "
                    f"({preferred_label})"
                ),
                path=author_dir,
                suggestion=f"Rename to '{suggested}'",
                fix=FixAction(kind="rename", source=author_dir, target=target_dir),
            )
        )


def _check_naming_conventions(items: list[ScanResult], report: AnalysisReport) -> None:
    """Flag title folders that don't follow Audiobookshelf naming conventions."""
    for item in items:
        if not item.path:
            continue
        folder_name = item.path.name
        expected = item.meta.dest_folder_name()
        if folder_name != expected and item.meta.title != "Unknown Title":
            target = item.path.parent / expected
            report.issues.append(
                Issue(
                    severity="info",
                    category="naming",
                    message=f"Folder '{folder_name}' could be renamed",
                    path=item.path,
                    suggestion=f"Rename to '{expected}' for Audiobookshelf compatibility",
                    fix=FixAction(kind="rename", source=item.path, target=target),
                )
            )


def _check_metadata_quality(items: list[ScanResult], report: AnalysisReport) -> None:
    """Flag books whose audio-file tags look suspicious (requires read_tags)."""
    for item in items:
        if item.tag_meta is None:
            continue
        tag = item.tag_meta

        # Suspicious artist tag (genre/category instead of a person name)
        if tag.author != "Unknown Author" and not looks_like_author(tag.author):
            report.issues.append(
                Issue(
                    severity="warning",
                    category="metadata",
                    message=(
                        f"Suspicious artist tag '{tag.author}' for "
                        f"'{item.meta.title}'"
                    ),
                    path=item.path,
                    suggestion=(
                        "The artist tag doesn't look like an author name. "
                        "Consider updating the audio file tags"
                    ),
                )
            )

        # Tag author vs folder author mismatch
        if (
            tag.author != "Unknown Author"
            and item.meta.author != "Unknown Author"
            and tag.author.lower() != item.meta.author.lower()
            and looks_like_author(tag.author)
        ):
            report.issues.append(
                Issue(
                    severity="info",
                    category="metadata",
                    message=(
                        f"Tag author '{tag.author}' differs from folder "
                        f"author '{item.meta.author}'"
                    ),
                    path=item.path,
                    suggestion="Verify which author name is correct",
                )
            )

        # Title looks like it contains track numbering
        if tag.title and tag.title != "Unknown Title" and re.match(r"^\d+\s*[-.:]+\s*", tag.title):
            report.issues.append(
                Issue(
                    severity="info",
                    category="metadata",
                    message=(
                        f"Title tag '{tag.title}' starts with numbering "
                        f"for '{item.meta.title}'"
                    ),
                    path=item.path,
                    suggestion="The title tag may contain track/disc numbering",
                )
            )


def apply_fixes(
    report: AnalysisReport,
    *,
    dry_run: bool = False,
    on_fix: Callable[[FixAction, bool, str], None] | None = None,
) -> list[FixAction]:
    """Apply all automatic fixes from *report*. Returns the list of applied actions."""
    applied: list[FixAction] = []
    _notify = on_fix or (lambda _action, _ok, _err: None)

    for issue in report.issues:
        if issue.fix is None:
            continue
        action = issue.fix
        if dry_run:
            _notify(action, True, "")
            applied.append(action)
            continue

        ok, err = False, ""
        if action.kind == "remove_dir":
            ok, err = _apply_remove_dir(action)
        elif action.kind == "rename":
            ok, err = _apply_rename(action)
        _notify(action, ok, err)
        if ok:
            applied.append(action)

    return applied


def _apply_remove_dir(action: FixAction) -> tuple[bool, str]:
    try:
        action.source.rmdir()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _apply_rename(action: FixAction) -> tuple[bool, str]:
    if action.target is None:
        return False, "no target path"
    if action.target.exists():
        return False, "target already exists"
    try:
        action.source.rename(action.target)
        return True, ""
    except OSError as exc:
        return False, str(exc)
