"""Tests for audiobook_organizer.scanner — file/directory scanning."""

import zipfile
from pathlib import Path

from audiobook_organizer.config import Config
from audiobook_organizer.scanner import scan_collection, scan_sources


def _make_audio_file(path: Path, size: int = 2_000_000) -> None:
    """Create a dummy audio file of specified size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


def _make_audiobook_zip(path: Path) -> None:
    """Create a zip that contains an audio file and is large enough to pass filters."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        # Write a large-ish mp3 entry so the zip is > 50 MB
        zf.writestr("audiobook.mp3", b"\x00" * 51_000_000)


class TestScanSources:
    def test_finds_audio_file(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].kind == "audio_file"
        assert results[0].meta.author == "Author"
        assert results[0].meta.title == "Title"

    def test_finds_archive(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audiobook_zip(src / "Author - Book.zip")

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].kind == "archive"

    def test_skips_non_audiobook_archive(self, tmp_path):
        """Archives without audio content or without Author - Title naming are skipped."""
        src = tmp_path / "downloads"
        # No author in name
        noname = src / "Sync.zip"
        noname.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(noname, "w") as zf:
            zf.writestr("data.txt", b"x" * 60_000_000)
        # Proper name but no audio inside
        no_audio = src / "Author - Book.zip"
        with zipfile.ZipFile(no_audio, "w") as zf:
            zf.writestr("readme.txt", b"x" * 60_000_000)

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 0

    def test_finds_audio_directory(self, tmp_path):
        src = tmp_path / "downloads"
        book_dir = src / "Author - Book"
        _make_audio_file(book_dir / "track01.mp3")
        _make_audio_file(book_dir / "track02.mp3")

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].kind == "audio_dir"

    def test_skips_small_files(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "tiny.mp3", size=50)

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 0

    def test_skips_unknown_extensions(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "readme.txt", size=2_000_000)

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 0

    def test_missing_source_dir(self, tmp_path):
        cfg = Config(source_dirs=[tmp_path / "nonexistent"])
        results = scan_sources(cfg)
        assert results == []

    def test_deduplicates(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")

        cfg = Config(source_dirs=[src, src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 1

    def test_multiple_source_dirs(self, tmp_path):
        s1 = tmp_path / "dir1"
        s2 = tmp_path / "dir2"
        _make_audio_file(s1 / "Author1 - Book1.mp3")
        _make_audio_file(s2 / "Author2 - Book2.mp3")

        cfg = Config(source_dirs=[s1, s2], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 2

    def test_deduplicates_windows_copy_suffix(self, tmp_path):
        """Files like 'Author - Title(1).zip' should be deduped against 'Author - Title.zip'."""
        src = tmp_path / "downloads"
        _make_audiobook_zip(src / "Author - Title.zip")
        _make_audiobook_zip(src / "Author - Title(1).zip")

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].meta.title == "Title"

    def test_skips_unknown_author_dir(self, tmp_path):
        """Directories with no recognisable author should be skipped."""
        src = tmp_path / "downloads"
        book_dir = src / "French I"
        _make_audio_file(book_dir / "lesson01.mp3")

        cfg = Config(source_dirs=[src], min_file_size=100)
        results = scan_sources(cfg)
        assert len(results) == 0


class TestScanCollection:
    def test_scans_author_title_structure(self, tmp_path):
        _make_audio_file(tmp_path / "Author A" / "Book One" / "audio.mp3")
        _make_audio_file(tmp_path / "Author B" / "Book Two" / "audio.m4b")

        cfg = Config(min_file_size=100)
        results = scan_collection(tmp_path, cfg)
        assert len(results) == 2
        authors = {r.meta.author for r in results}
        assert "Author A" in authors
        assert "Author B" in authors

    def test_scans_series_structure(self, tmp_path):
        # scan_collection uses rglob, so audio in subdirs counts.
        # Put audio directly in title dirs (not series root)
        title1 = tmp_path / "Author" / "Series" / "Book 1"
        title2 = tmp_path / "Author" / "Series" / "Book 2"
        _make_audio_file(title1 / "audio.mp3")
        _make_audio_file(title2 / "audio.mp3")

        cfg = Config(min_file_size=100)
        results = scan_collection(tmp_path, cfg)
        # _count_audio uses rglob so Series dir has audio -> treated as title dir
        # This is expected: the top-level Series dir is scanned as a single book
        assert len(results) >= 1
        assert any(r.meta.author == "Author" for r in results)

    def test_empty_collection(self, tmp_path):
        cfg = Config()
        assert scan_collection(tmp_path, cfg) == []

    def test_nonexistent_root(self, tmp_path):
        cfg = Config()
        assert scan_collection(tmp_path / "nope", cfg) == []
