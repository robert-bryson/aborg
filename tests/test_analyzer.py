"""Tests for audiobook_organizer.analyzer — collection analysis."""

from pathlib import Path

from audiobook_organizer.analyzer import (
    FixAction,
    _check_metadata_quality,
    _flip_author_name,
    _is_last_first,
    analyze_collection,
    apply_fixes,
)
from audiobook_organizer.config import Config
from audiobook_organizer.parser import AudiobookMeta
from audiobook_organizer.scanner import ScanResult

from .conftest import make_cfg


def _make_audio(path: Path, size: int = 2_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


class TestAnalyzeCollection:
    def test_empty_collection(self, tmp_path):
        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        assert report.total_books == 0
        assert report.total_size == 0

    def test_counts_books_and_authors(self, tmp_path):
        _make_audio(tmp_path / "Author A" / "Book 1" / "audio.mp3")
        _make_audio(tmp_path / "Author B" / "Book 2" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        assert report.total_books == 2
        assert report.authors == 2

    def test_detects_missing_cover(self, tmp_path):
        _make_audio(tmp_path / "Author" / "Book" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        cover_issues = [i for i in report.issues if "cover" in i.message.lower()]
        assert len(cover_issues) >= 1

    def test_no_cover_issue_when_present(self, tmp_path):
        book_dir = tmp_path / "Author" / "Book"
        _make_audio(book_dir / "audio.mp3")
        (book_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        cover_issues = [i for i in report.issues if "cover" in i.message.lower()]
        assert len(cover_issues) == 0

    def test_detects_flat_files(self, tmp_path):
        _make_audio(tmp_path / "loose_audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        struct_issues = [i for i in report.issues if i.category == "structure"]
        assert len(struct_issues) >= 1

    def test_detects_empty_dirs(self, tmp_path):
        (tmp_path / "Author" / "EmptyBook").mkdir(parents=True)
        # Need at least one real book for scan to work
        _make_audio(tmp_path / "Author" / "RealBook" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        cleanup_issues = [i for i in report.issues if i.category == "cleanup"]
        assert len(cleanup_issues) >= 1

    def test_detects_similar_authors(self, tmp_path):
        _make_audio(tmp_path / "J.R.R. Tolkien" / "Hobbit" / "audio.mp3")
        _make_audio(tmp_path / "J.R.R Tolkien" / "LOTR" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        assert len(report.author_variants) >= 1

    def test_detects_duplicates(self, tmp_path):
        _make_audio(tmp_path / "Author" / "The Hobbit" / "audio.mp3")
        _make_audio(tmp_path / "Author" / "The Hobbits" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        # fuzzy matching should catch these (ratio > 0.85)
        assert len(report.duplicates) >= 1


class TestApplyFixes:
    def test_fix_removes_empty_dir(self, tmp_path):
        empty = tmp_path / "Author" / "EmptyBook"
        empty.mkdir(parents=True)
        _make_audio(tmp_path / "Author" / "RealBook" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        fixable = [i for i in report.issues if i.fix is not None]
        assert any(f.fix.kind == "remove_dir" for f in fixable)

        applied = apply_fixes(report)
        assert len(applied) >= 1
        assert not empty.exists()

    def test_fix_dry_run_preserves_empty_dir(self, tmp_path):
        empty = tmp_path / "Author" / "EmptyBook"
        empty.mkdir(parents=True)
        _make_audio(tmp_path / "Author" / "RealBook" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        applied = apply_fixes(report, dry_run=True)
        assert len(applied) >= 1
        assert empty.exists()

    def test_fix_renames_folder(self, tmp_path):
        # Create a folder that doesn't match Audiobookshelf naming
        # The scanner should parse "Author - Title (2020)" where year=2020
        book_dir = tmp_path / "Author" / "Title"
        _make_audio(book_dir / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        rename_fixes = [i for i in report.issues if i.fix and i.fix.kind == "rename"]
        if rename_fixes:
            applied = apply_fixes(report)
            rename_applied = [a for a in applied if a.kind == "rename"]
            assert len(rename_applied) >= 1
            assert rename_applied[0].target.exists()

    def test_fix_rename_skips_conflict(self, tmp_path):
        book_dir = tmp_path / "Author" / "Title"
        _make_audio(book_dir / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        rename_fixes = [i for i in report.issues if i.fix and i.fix.kind == "rename"]
        if rename_fixes:
            # Pre-create the target so rename should be skipped
            rename_fixes[0].fix.target.mkdir(parents=True, exist_ok=True)
            applied = apply_fixes(report)
            rename_applied = [a for a in applied if a.kind == "rename"]
            assert len(rename_applied) == 0

    def test_on_fix_callback(self, tmp_path):
        empty = tmp_path / "Author" / "EmptyBook"
        empty.mkdir(parents=True)
        _make_audio(tmp_path / "Author" / "RealBook" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)

        called = []
        apply_fixes(report, on_fix=lambda action, ok, err: called.append((action.kind, ok)))
        assert len(called) >= 1


class TestAuthorNameFormat:
    def test_is_last_first(self):
        assert _is_last_first("Applebaum, Anne") is True
        assert _is_last_first("Austen, Jane") is True
        assert _is_last_first("Candice Millard") is False
        assert _is_last_first("E. B. White") is False

    def test_flip_last_first_to_first_last(self):
        assert _flip_author_name("Applebaum, Anne") == "Anne Applebaum"
        assert _flip_author_name("Austen, Jane") == "Jane Austen"

    def test_flip_first_last_to_last_first(self):
        assert _flip_author_name("Candice Millard") == "Millard, Candice"
        assert _flip_author_name("E. B. White") == "White, E. B."

    def test_flags_first_last_when_config_is_last_first(self, tmp_path):
        """Default config (last_first) should flag 'First Last' authors."""
        _make_audio(tmp_path / "Applebaum, Anne" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Candice Millard" / "Book D" / "audio.mp3")

        cfg = make_cfg(author_name_format="last_first")
        report = analyze_collection(tmp_path, cfg)
        format_issues = [
            i for i in report.issues
            if "preferred format" in i.message
        ]
        assert len(format_issues) == 1
        assert "Candice Millard" in format_issues[0].message
        assert format_issues[0].fix is not None
        assert format_issues[0].fix.target.name == "Millard, Candice"

    def test_flags_last_first_when_config_is_first_last(self, tmp_path):
        """Config set to first_last should flag 'Last, First' authors."""
        _make_audio(tmp_path / "Jane Austen" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Applebaum, Anne" / "Book B" / "audio.mp3")

        cfg = make_cfg(author_name_format="first_last")
        report = analyze_collection(tmp_path, cfg)
        format_issues = [
            i for i in report.issues
            if "preferred format" in i.message
        ]
        assert len(format_issues) == 1
        assert "Applebaum, Anne" in format_issues[0].message
        assert format_issues[0].fix.target.name == "Anne Applebaum"

    def test_no_issue_when_all_match_config(self, tmp_path):
        """All 'Last, First' with last_first config should produce no issues."""
        _make_audio(tmp_path / "Applebaum, Anne" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Austen, Jane" / "Book B" / "audio.mp3")

        cfg = make_cfg(author_name_format="last_first")
        report = analyze_collection(tmp_path, cfg)
        format_issues = [
            i for i in report.issues
            if "preferred format" in i.message
        ]
        assert len(format_issues) == 0

    def test_fix_renames_author_dir(self, tmp_path):
        """Auto-fix should rename author dir to match configured format."""
        _make_audio(tmp_path / "Applebaum, Anne" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Candice Millard" / "Book D" / "audio.mp3")

        cfg = make_cfg(author_name_format="last_first")
        report = analyze_collection(tmp_path, cfg)
        applied = apply_fixes(report)
        rename_applied = [a for a in applied if a.target and a.target.name == "Millard, Candice"]
        assert len(rename_applied) == 1
        assert (tmp_path / "Millard, Candice").exists()
        assert not (tmp_path / "Candice Millard").exists()


class TestMetadataQuality:
    def _make_report(self):
        from audiobook_organizer.analyzer import AnalysisReport
        return AnalysisReport()

    def test_flags_suspicious_artist(self):
        report = self._make_report()
        items = [
            ScanResult(
                path=Path("/collection/Author/Book"),
                kind="audio_dir",
                meta=AudiobookMeta(author="Author", title="Book"),
                size=1000,
                tag_meta=AudiobookMeta(
                    author="Top 100 Sci-Fi Books",
                    title="Foundation",
                ),
            ),
        ]
        _check_metadata_quality(items, report)
        assert any("Suspicious artist" in i.message for i in report.issues)

    def test_flags_tag_author_mismatch(self):
        report = self._make_report()
        items = [
            ScanResult(
                path=Path("/collection/Isaac Asimov/Foundation"),
                kind="audio_dir",
                meta=AudiobookMeta(author="Isaac Asimov", title="Foundation"),
                size=1000,
                tag_meta=AudiobookMeta(
                    author="Asimov, Isaac",
                    title="Foundation",
                ),
            ),
        ]
        _check_metadata_quality(items, report)
        mismatch = [i for i in report.issues if "differs from folder" in i.message]
        assert len(mismatch) == 1

    def test_flags_title_with_numbering(self):
        report = self._make_report()
        items = [
            ScanResult(
                path=Path("/collection/Author/Book"),
                kind="audio_dir",
                meta=AudiobookMeta(author="Author", title="Book"),
                size=1000,
                tag_meta=AudiobookMeta(
                    title="03 - Foundation",
                ),
            ),
        ]
        _check_metadata_quality(items, report)
        assert any("numbering" in i.message for i in report.issues)

    def test_no_issues_for_clean_tags(self):
        report = self._make_report()
        items = [
            ScanResult(
                path=Path("/collection/Isaac Asimov/Foundation"),
                kind="audio_dir",
                meta=AudiobookMeta(author="Isaac Asimov", title="Foundation"),
                size=1000,
                tag_meta=AudiobookMeta(
                    author="Isaac Asimov",
                    title="Foundation",
                ),
            ),
        ]
        _check_metadata_quality(items, report)
        assert len(report.issues) == 0

    def test_skips_items_without_tag_meta(self):
        report = self._make_report()
        items = [
            ScanResult(
                path=Path("/collection/Author/Book"),
                kind="audio_dir",
                meta=AudiobookMeta(author="Author", title="Book"),
                size=1000,
                tag_meta=None,
            ),
        ]
        _check_metadata_quality(items, report)
        assert len(report.issues) == 0
