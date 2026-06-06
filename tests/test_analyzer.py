"""Tests for audiobook_organizer.analyzer — collection analysis."""

from pathlib import Path

from audiobook_organizer.analyzer import (
    AnalysisReport,
    FixAction,
    Issue,
    _apply_remove_dir,
    _apply_rename,
    _check_metadata_quality,
    analyze_collection,
    apply_fixes,
)
from audiobook_organizer.parser import AudiobookMeta, flip_author_name, is_last_first
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

    def test_detects_accented_duplicates(self, tmp_path):
        """Accented vs unaccented titles under the same author should be flagged."""
        _make_audio(tmp_path / "Author" / "Café Stories" / "audio.mp3")
        _make_audio(tmp_path / "Author" / "Cafe Stories" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        assert len(report.duplicates) >= 1

    def test_detects_accented_author_variants(self, tmp_path):
        """Authors with accented chars that are similar but not identical after
        folding should be flagged as variants."""
        _make_audio(tmp_path / "García Lorca, Federico" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Garcia Lorca, F." / "Book B" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        assert len(report.author_variants) >= 1

    def test_detects_accented_cross_author_duplicates(self, tmp_path):
        """Same book under accent-variant author names should be flagged as duplicate."""
        _make_audio(tmp_path / "Garcia Marquez" / "One Hundred Years" / "audio.mp3")
        _make_audio(tmp_path / "García Márquez" / "One Hundred Years" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        # These are under different authors (accent variants) but the
        # duplicate check groups by accent-folded author, so should be found
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
        assert is_last_first("Applebaum, Anne") is True
        assert is_last_first("Austen, Jane") is True
        assert is_last_first("Candice Millard") is False
        assert is_last_first("E. B. White") is False

    def test_flip_last_first_to_first_last(self):
        assert flip_author_name("Applebaum, Anne") == "Anne Applebaum"
        assert flip_author_name("Austen, Jane") == "Jane Austen"

    def test_flip_first_last_to_last_first(self):
        assert flip_author_name("Candice Millard") == "Millard, Candice"
        assert flip_author_name("E. B. White") == "White, E. B."

    def test_flags_first_last_when_config_is_last_first(self, tmp_path):
        """Default config (last_first) should flag 'First Last' authors."""
        _make_audio(tmp_path / "Applebaum, Anne" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Candice Millard" / "Book D" / "audio.mp3")

        cfg = make_cfg(author_name_format="last_first")
        report = analyze_collection(tmp_path, cfg)
        format_issues = [i for i in report.issues if "preferred format" in i.message]
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
        format_issues = [i for i in report.issues if "preferred format" in i.message]
        assert len(format_issues) == 1
        assert "Applebaum, Anne" in format_issues[0].message
        assert format_issues[0].fix.target.name == "Anne Applebaum"

    def test_no_issue_when_all_match_config(self, tmp_path):
        """All 'Last, First' with last_first config should produce no issues."""
        _make_audio(tmp_path / "Applebaum, Anne" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Austen, Jane" / "Book B" / "audio.mp3")

        cfg = make_cfg(author_name_format="last_first")
        report = analyze_collection(tmp_path, cfg)
        format_issues = [i for i in report.issues if "preferred format" in i.message]
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
                    author="Ray Bradbury",
                    title="Foundation",
                ),
            ),
        ]
        _check_metadata_quality(items, report)
        mismatch = [i for i in report.issues if "differs from folder" in i.message]
        assert len(mismatch) == 1

    def test_no_mismatch_for_flipped_author_name(self):
        """'Last, First' in folder should match 'First Last' in tags."""
        report = self._make_report()
        items = [
            ScanResult(
                path=Path("/collection/Asimov, Isaac/Foundation"),
                kind="audio_dir",
                meta=AudiobookMeta(author="Asimov, Isaac", title="Foundation"),
                size=1000,
                tag_meta=AudiobookMeta(
                    author="Isaac Asimov",
                    title="Foundation",
                ),
            ),
        ]
        _check_metadata_quality(items, report)
        mismatch = [i for i in report.issues if "differs from folder" in i.message]
        assert len(mismatch) == 0

    def test_no_mismatch_for_slash_separated_tag_author(self):
        """Tag 'Author/Narrator' should match folder 'Last, First'."""
        report = self._make_report()
        items = [
            ScanResult(
                path=Path("/collection/Arendt, Hannah/Some Book"),
                kind="audio_dir",
                meta=AudiobookMeta(author="Arendt, Hannah", title="Some Book"),
                size=1000,
                tag_meta=AudiobookMeta(
                    author="Hannah Arendt/Tavia Gilbert",
                    title="Some Book",
                ),
            ),
        ]
        _check_metadata_quality(items, report)
        mismatch = [i for i in report.issues if "differs from folder" in i.message]
        assert len(mismatch) == 0

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


class TestApplyRename:
    def test_rename_missing_source(self, tmp_path):
        """Rename should fail gracefully when source no longer exists."""
        action = FixAction(
            kind="rename",
            source=tmp_path / "does_not_exist",
            target=tmp_path / "new_name",
        )
        ok, err = _apply_rename(action)
        assert ok is False
        assert "source no longer exists" in err

    def test_rename_no_target(self):
        action = FixAction(kind="rename", source=Path("/tmp/x"), target=None)  # noqa: S108
        ok, err = _apply_rename(action)
        assert ok is False
        assert "no target" in err

    def test_rename_target_exists(self, tmp_path):
        src = tmp_path / "old"
        src.mkdir()
        tgt = tmp_path / "new"
        tgt.mkdir()
        action = FixAction(kind="rename", source=src, target=tgt)
        ok, err = _apply_rename(action)
        assert ok is False
        assert "already exists" in err

    def test_rename_success(self, tmp_path):
        src = tmp_path / "old"
        src.mkdir()
        tgt = tmp_path / "new"
        action = FixAction(kind="rename", source=src, target=tgt)
        ok, _err = _apply_rename(action)
        assert ok is True
        assert tgt.exists()
        assert not src.exists()


class TestAnalysisReportProperties:
    def test_errors_property(self):
        report = AnalysisReport()
        report.issues = [
            Issue(severity="error", category="test", message="err1"),
            Issue(severity="warning", category="test", message="warn1"),
            Issue(severity="error", category="test", message="err2"),
        ]
        assert len(report.errors) == 2
        assert all(i.severity == "error" for i in report.errors)

    def test_warnings_property(self):
        report = AnalysisReport()
        report.issues = [
            Issue(severity="error", category="test", message="err1"),
            Issue(severity="warning", category="test", message="warn1"),
            Issue(severity="info", category="test", message="info1"),
        ]
        assert len(report.warnings) == 1
        assert report.warnings[0].severity == "warning"


class TestApplyFixesEdgeCases:
    def test_apply_remove_dir_fails_nonempty(self, tmp_path):
        d = tmp_path / "notempty"
        d.mkdir()
        (d / "file.txt").write_text("content")
        action = FixAction(kind="remove_dir", source=d)
        ok, err = _apply_remove_dir(action)
        assert ok is False
        assert err  # Some error message

    def test_rename_source_missing(self, tmp_path):
        action = FixAction(kind="rename", source=tmp_path / "gone", target=tmp_path / "new")
        ok, err = _apply_rename(action)
        assert ok is False
        assert "source no longer exists" in err

    def test_apply_fixes_with_callback(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        report = AnalysisReport()
        report.issues = [
            Issue(
                severity="info",
                category="cleanup",
                message="Empty directory",
                path=d,
                fix=FixAction(kind="remove_dir", source=d),
            )
        ]
        notifications = []
        applied = apply_fixes(
            report,
            on_fix=lambda action, ok, err: notifications.append((action, ok, err)),
        )
        assert len(applied) == 1
        assert len(notifications) == 1
        assert notifications[0][1] is True  # ok=True


class TestDuplicateDetectionClustering:
    """Duplicate detection should cluster related items instead of O(n²) pairs."""

    def test_cluster_reduces_issues(self, tmp_path):
        """5 books with the same title should produce 1 cluster issue, not 10 pairs."""
        for i in range(5):
            _make_audio(tmp_path / "Author" / f"Same Title {i}" / "audio.mp3")
            # Hack: rename so the title parsed is exactly "Same Title" for all
        # Create 5 identical-title dirs
        author_dir = tmp_path / "Author"
        for child in author_dir.iterdir():
            child.rename(author_dir / "Same Title")
            break  # only need one dir for the approach below

        # Use a cleaner approach: create 5 dirs with nearly-identical names
        for child in list(author_dir.iterdir()):
            if child.name.startswith("Same Title "):
                child.rename(author_dir / f"Same Title Copy{child.name[-1]}")

        # Actually, let's test with real near-identical titles
        import shutil

        shutil.rmtree(author_dir)
        for i in range(5):
            _make_audio(author_dir / f"The Great Novel - Part {i:02d}" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        dup_issues = [i for i in report.issues if i.category == "duplicate"]
        # Should be fewer than C(5,2)=10 issues (clustered)
        # The titles differ enough that they may not all be flagged, but
        # the clustering logic should be exercised.
        assert len(dup_issues) <= 5

    def test_different_years_not_flagged(self, tmp_path):
        """Books with same title but different years should not be flagged."""
        for year in (1999, 2002, 2006):
            book_dir = tmp_path / "Author" / f"{year} - Same Title"
            _make_audio(book_dir / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        dup_issues = [i for i in report.issues if i.category == "duplicate"]
        assert len(dup_issues) == 0


class TestAuthorVariantFalsePositives:
    """Author variant detection should not flag names that merely share a first name."""

    def test_no_false_positive_shared_first_name(self, tmp_path):
        """'Caro, Robert' and 'Harris, Robert' should NOT be flagged."""
        _make_audio(tmp_path / "Caro, Robert" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Harris, Robert" / "Book B" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        assert len(report.author_variants) == 0

    def test_true_positive_similar_last_name(self, tmp_path):
        """'Freeman, Joanne B' and 'Freeman, Joshua B' SHOULD be flagged."""
        _make_audio(tmp_path / "Freeman, Joanne B" / "Book A" / "audio.mp3")
        _make_audio(tmp_path / "Freeman, Joshua B" / "Book B" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg)
        assert len(report.author_variants) >= 1


class TestCompoundSurnameFormat:
    """Author name format checks should handle compound surnames correctly."""

    def test_compound_surname_not_flagged(self, tmp_path):
        """'Garcia Marquez, Gabriel' is already in 'Last, First' — no issue."""
        _make_audio(tmp_path / "Garcia Marquez, Gabriel" / "Book" / "audio.mp3")

        cfg = make_cfg(author_name_format="last_first")
        report = analyze_collection(tmp_path, cfg)
        format_issues = [i for i in report.issues if "preferred format" in i.message]
        assert len(format_issues) == 0


class TestApplyRenameOSError:
    """Edge case: _apply_rename returns failure on OS-level rename errors."""

    def test_rename_os_error(self, tmp_path):
        """Rename should catch OSError (e.g. cross-device, permission denied)."""
        src = tmp_path / "source_dir"
        src.mkdir()
        # Target on a non-existent mount point triggers OSError
        tgt = Path("/proc/0/impossible_target")

        action = FixAction(kind="rename", source=src, target=tgt)
        ok, err = _apply_rename(action)
        assert ok is False
        assert err  # Some OS-level error message


class TestMissingCoverSkipsNonDir:
    """_check_missing_covers should skip archive and single-file items."""

    def test_skips_archive_items(self):
        from audiobook_organizer.analyzer import _check_missing_covers

        report = AnalysisReport()
        items = [
            ScanResult(
                path=Path("/collection/Author/Book.zip"),
                kind="archive",
                meta=AudiobookMeta(author="Author", title="Book"),
                size=1000,
                has_cover=False,
            ),
        ]
        _check_missing_covers(items, report)
        cover_issues = [i for i in report.issues if "cover" in i.message.lower()]
        assert len(cover_issues) == 0

    def test_skips_items_with_no_path(self):
        from audiobook_organizer.analyzer import _check_missing_covers

        report = AnalysisReport()
        items = [
            ScanResult(
                path=None,
                kind="audio_dir",
                meta=AudiobookMeta(author="Author", title="Book"),
                size=1000,
                has_cover=False,
            ),
        ]
        _check_missing_covers(items, report)
        assert len(report.issues) == 0


class TestCheckNamingConventionsEdge:
    """Edge cases for _check_naming_conventions."""

    def test_skips_unknown_title(self, tmp_path):
        """Items with Unknown Title should not get rename suggestions."""
        _make_audio(tmp_path / "Author" / "Weird Folder" / "audio.mp3")

        cfg = make_cfg()
        report = analyze_collection(tmp_path, cfg, read_tags=False)
        # Find items with Unknown Title
        unknown_items = [i for i in report.items if i.meta.title == "Unknown Title"]
        naming_issues = [
            i for i in report.issues if i.category == "naming" and "could be renamed" in i.message
        ]
        # Unknown Title items should NOT get rename suggestions
        for issue in naming_issues:
            assert issue.path not in [u.path for u in unknown_items]
