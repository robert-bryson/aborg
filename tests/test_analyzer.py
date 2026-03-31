"""Tests for audiobook_organizer.analyzer — collection analysis."""

from pathlib import Path

from audiobook_organizer.analyzer import analyze_collection
from audiobook_organizer.config import Config

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
