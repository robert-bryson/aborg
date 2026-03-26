"""Analyze an existing audiobook collection and suggest improvements."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .config import Config
from .scanner import ScanResult, scan_collection


@dataclass
class Issue:
    """A single detected issue in the collection."""

    severity: str  # "error", "warning", "info"
    category: str
    message: str
    path: Path | None = None
    suggestion: str | None = None


@dataclass
class AnalysisReport:
    """Full analysis report for a collection."""

    total_books: int = 0
    total_size: int = 0
    authors: int = 0
    series: int = 0
    issues: list[Issue] = field(default_factory=list)
    duplicates: list[tuple[ScanResult, ScanResult]] = field(default_factory=list)
    author_variants: list[tuple[str, str]] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]


def analyze_collection(root: Path, cfg: Config) -> AnalysisReport:
    """Analyze the collection at *root* and return a detailed report."""
    items = scan_collection(root, cfg)
    report = AnalysisReport()
    report.total_books = len(items)
    report.total_size = sum(i.size for i in items)

    authors: set[str] = set()
    series_set: set[str] = set()
    for item in items:
        authors.add(item.meta.author)
        if item.meta.series:
            series_set.add(item.meta.series)
    report.authors = len(authors)
    report.series = len(series_set)

    # Run checks
    _check_unknown_metadata(items, report)
    _check_duplicates(items, report)
    _check_author_variants(authors, report)
    _check_empty_dirs(root, report)
    _check_flat_files(root, cfg, report)
    _check_missing_covers(items, report)
    _check_naming_conventions(items, report)

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
        for i, a in enumerate(author_items):
            for b in author_items[i + 1 :]:
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


def _check_empty_dirs(root: Path, report: AnalysisReport) -> None:
    """Flag empty directories in the collection."""
    if not root.exists():
        return
    for d in root.rglob("*"):
        if d.is_dir() and not any(d.iterdir()):
            report.issues.append(
                Issue(
                    severity="info",
                    category="cleanup",
                    message="Empty directory",
                    path=d,
                    suggestion="Remove empty directory",
                )
            )


def _check_flat_files(root: Path, cfg: Config, report: AnalysisReport) -> None:
    """Flag audio files sitting directly in the root (not in Author/Title structure)."""
    if not root.exists():
        return
    for f in root.iterdir():
        if f.is_file() and f.suffix.lower() in cfg.audio_extensions:
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
    """Flag audiobooks without cover art."""
    cover_names = {"cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png"}
    for item in items:
        if item.kind != "audio_dir" or item.path is None:
            continue
        has_cover = any(f.name.lower() in cover_names for f in item.path.iterdir() if f.is_file())
        if not has_cover:
            report.issues.append(
                Issue(
                    severity="info",
                    category="metadata",
                    message=f"No cover art found for '{item.meta.title}'",
                    path=item.path,
                    suggestion="Add a cover.jpg file",
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
            report.issues.append(
                Issue(
                    severity="info",
                    category="naming",
                    message=f"Folder '{folder_name}' could be renamed",
                    path=item.path,
                    suggestion=f"Rename to '{expected}' for Audiobookshelf compatibility",
                )
            )
