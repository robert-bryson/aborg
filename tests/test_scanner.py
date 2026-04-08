"""Tests for audiobook_organizer.scanner — file/directory scanning."""

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from audiobook_organizer.config import Config
from audiobook_organizer.scanner import scan_collection, scan_sources

from .conftest import make_cfg


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

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].kind == "audio_file"
        assert results[0].meta.author == "Author"
        assert results[0].meta.title == "Title"

    def test_finds_archive(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audiobook_zip(src / "Author - Book.zip")

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
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

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 0

    def test_finds_audio_directory(self, tmp_path):
        src = tmp_path / "downloads"
        book_dir = src / "Author - Book"
        _make_audio_file(book_dir / "track01.mp3")
        _make_audio_file(book_dir / "track02.mp3")

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].kind == "audio_dir"

    def test_skips_small_files(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "tiny.mp3", size=50)

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 0

    def test_skips_unknown_extensions(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "readme.txt", size=2_000_000)

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 0

    def test_missing_source_dir(self, tmp_path):
        cfg = Config(source_dirs=[tmp_path / "nonexistent"])
        results, missing_dirs = scan_sources(cfg)
        assert results == []
        assert missing_dirs == [tmp_path / "nonexistent"]

    def test_deduplicates(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")

        cfg = make_cfg(source_dirs=[src, src])
        results, _ = scan_sources(cfg)
        assert len(results) == 1

    def test_multiple_source_dirs(self, tmp_path):
        s1 = tmp_path / "dir1"
        s2 = tmp_path / "dir2"
        _make_audio_file(s1 / "Author1 - Book1.mp3")
        _make_audio_file(s2 / "Author2 - Book2.mp3")

        cfg = make_cfg(source_dirs=[s1, s2])
        results, _ = scan_sources(cfg)
        assert len(results) == 2

    def test_deduplicates_windows_copy_suffix(self, tmp_path):
        """Files like 'Author - Title(1).zip' should be deduped against 'Author - Title.zip'."""
        src = tmp_path / "downloads"
        _make_audiobook_zip(src / "Author - Title.zip")
        _make_audiobook_zip(src / "Author - Title(1).zip")

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].meta.title == "Title"

    def test_skips_unknown_author_dir(self, tmp_path):
        """Directories with no recognisable author should be skipped."""
        src = tmp_path / "downloads"
        book_dir = src / "French I"
        _make_audio_file(book_dir / "lesson01.mp3")

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 0


class TestScanCollection:
    def test_scans_author_title_structure(self, tmp_path):
        _make_audio_file(tmp_path / "Author A" / "Book One" / "audio.mp3")
        _make_audio_file(tmp_path / "Author B" / "Book Two" / "audio.m4b")

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg)
        assert len(collection.items) == 2
        authors = {r.meta.author for r in collection.items}
        assert "Author A" in authors
        assert "Author B" in authors

    def test_scans_series_structure(self, tmp_path):
        # Audio in subdirs of a series dir → _collect_dir_info walks
        # recursively, so the series dir itself reports audio_count > 0
        # and is treated as a single title dir.
        title1 = tmp_path / "Author" / "Series" / "Book 1"
        title2 = tmp_path / "Author" / "Series" / "Book 2"
        _make_audio_file(title1 / "audio.mp3")
        _make_audio_file(title2 / "audio.mp3")

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg)
        assert len(collection.items) == 1
        assert collection.items[0].meta.author == "Author"

    def test_empty_collection(self, tmp_path):
        cfg = Config()
        collection = scan_collection(tmp_path, cfg)
        assert collection.items == []

    def test_nonexistent_root(self, tmp_path):
        cfg = Config()
        collection = scan_collection(tmp_path / "nope", cfg)
        assert collection.items == []

    def test_detects_empty_dirs(self, tmp_path):
        (tmp_path / "Author" / "EmptyBook").mkdir(parents=True)
        _make_audio_file(tmp_path / "Author" / "RealBook" / "audio.mp3")

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg)
        assert len(collection.empty_dirs) >= 1
        assert any(d.name == "EmptyBook" for d in collection.empty_dirs)

    def test_detects_flat_audio_files(self, tmp_path):
        _make_audio_file(tmp_path / "loose.mp3")
        _make_audio_file(tmp_path / "Author" / "Book" / "audio.mp3")

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg)
        assert len(collection.flat_audio_files) >= 1
        assert any(f.name == "loose.mp3" for f in collection.flat_audio_files)

    def test_detects_cover_art(self, tmp_path):
        book_dir = tmp_path / "Author" / "Book"
        _make_audio_file(book_dir / "audio.mp3")
        (book_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff")

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg)
        assert len(collection.items) == 1
        assert collection.items[0].has_cover is True

    def test_missing_cover_detected(self, tmp_path):
        _make_audio_file(tmp_path / "Author" / "Book" / "audio.mp3")

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg)
        assert len(collection.items) == 1
        assert collection.items[0].has_cover is False


# ── Real-world messy data: Asimov collection ─────────────────────────────


class TestMessyAsimovCollection:
    """End-to-end tests simulating the real messy Asimov audiobook data.

    Book 1: Asimov, Issac/I, Robot - Isaac Asimov - 1950/
      - Note the typo ("Issac" vs "Isaac") and author embedded in folder name.
      - Audio files: "02 02 - I, Robot.mp3"

    Book 2: Asimov, Issac/Isaac Asimov - 1951/
      - Folder name has only author + year, no title at all.
      - Audio files: "1-01 1a.mp3" (generic numbered filenames)
      - Tags: artist="Top 100 Sci-Fi Books", album="03 - Book 1 - Foundation - Isaac Asimov"
    """

    def _build_collection(self, root: Path) -> None:
        """Create the messy Asimov folder structure with dummy audio files."""
        # Book 1: I, Robot
        book1 = root / "Asimov, Issac" / "I, Robot - Isaac Asimov - 1950"
        _make_audio_file(book1 / "01 01 - I, Robot.mp3")
        _make_audio_file(book1 / "02 02 - I, Robot.mp3")
        _make_audio_file(book1 / "03 03 - I, Robot.mp3")

        # Book 2: Foundation (folder name is just "Isaac Asimov - 1951")
        book2 = root / "Asimov, Issac" / "Isaac Asimov - 1951"
        _make_audio_file(book2 / "1-01 1a.mp3")
        _make_audio_file(book2 / "1-02 1b.mp3")
        _make_audio_file(book2 / "1-03 1c.mp3")

    # -- Without tags (read_tags=False) --

    def test_irobot_parsed_from_folder_name(self, tmp_path):
        """'I, Robot - Isaac Asimov - 1950' under 'Asimov, Issac' → correct metadata."""
        self._build_collection(tmp_path)
        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=False)

        robot = next(r for r in collection.items if "Robot" in r.path.name)
        assert robot.meta.author == "Asimov, Issac"
        assert robot.meta.title == "I, Robot"
        assert robot.meta.year == "1950"

    def test_irobot_dest_path(self, tmp_path):
        """I, Robot should produce the correct Audiobookshelf destination."""
        self._build_collection(tmp_path)
        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=False)

        robot = next(r for r in collection.items if "Robot" in r.path.name)
        assert robot.meta.dest_folder_name() == "1950 - I, Robot"
        assert str(robot.meta.dest_relative()) == "Asimov, Issac/1950 - I, Robot"

    def test_foundation_folder_only_author_and_year(self, tmp_path):
        """'Isaac Asimov - 1951' under 'Asimov, Issac' → author + year, title unknown."""
        self._build_collection(tmp_path)
        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=False)

        found = next(r for r in collection.items if "1951" in r.path.name)
        assert found.meta.author == "Asimov, Issac"
        assert found.meta.year == "1951"
        # Without tags, there's no way to know the title from the folder name alone.
        assert found.meta.title == "Unknown Title"

    def test_foundation_should_not_suggest_bad_rename(self, tmp_path):
        """The folder 'Isaac Asimov - 1951' must NOT parse Isaac Asimov as the title."""
        self._build_collection(tmp_path)
        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=False)

        found = next(r for r in collection.items if "1951" in r.path.name)
        dest = found.meta.dest_folder_name()
        # dest_folder_name() is just the title folder — no author component.
        # It must not contain the author's name as a title.
        assert "Isaac Asimov" not in dest
        assert "Asimov" not in dest

    def test_both_books_found(self, tmp_path):
        """Both books should be discovered in a single collection scan."""
        self._build_collection(tmp_path)
        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=False)

        assert len(collection.items) == 2
        titles = {r.meta.title for r in collection.items}
        assert "I, Robot" in titles
        authors = {r.meta.author for r in collection.items}
        assert authors == {"Asimov, Issac"}

    def test_all_file_counts(self, tmp_path):
        """Each book dir should report the correct number of audio files."""
        self._build_collection(tmp_path)
        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=False)

        for item in collection.items:
            assert item.file_count == 3
            assert item.kind == "audio_dir"

    # -- With mocked Mutagen (raw tag strings exercise the full pipeline) --
    #
    # We mock audiobook_organizer.parser.MutagenFile (not parse_audio_tags)
    # so that looks_like_author(), _clean_tag_title(), and merge_meta() all
    # run against the raw tag values — just like real audio files would.

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_foundation_title_from_raw_tags(self, mock_mutagen, tmp_path):
        """Raw tags: artist='Top 100 Sci-Fi Books' (rejected by looks_like_author),
        album='03 - Book 1 - Foundation - Isaac Asimov' (_clean_tag_title strips '03 - '),
        strip_author_from_title removes 'Isaac Asimov' → final title 'Book 1 - Foundation'."""
        self._build_collection(tmp_path)

        def _fake_mutagen(path, easy=False):
            if "1951" in str(path):
                m = MagicMock()
                m.tags = {
                    "artist": ["Top 100 Sci-Fi Books"],
                    "album": ["03 - Book 1 - Foundation - Isaac Asimov"],
                    "date": ["1951"],
                }
                return m
            return None  # no tags for other files

        mock_mutagen.side_effect = _fake_mutagen

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=True)

        found = next(r for r in collection.items if "1951" in r.path.name)
        assert found.meta.author == "Asimov, Issac"  # from dir, not tags
        assert found.meta.year == "1951"
        # _clean_tag_title strips "03 - ", strip_author_from_title strips "- Isaac Asimov"
        assert found.meta.title == "Book 1 - Foundation"
        assert "Isaac Asimov" not in found.meta.title

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_irobot_unaffected_by_empty_tags(self, mock_mutagen, tmp_path):
        """I, Robot folder has good metadata; empty tags shouldn't override it."""
        self._build_collection(tmp_path)
        mock_mutagen.return_value = None  # Mutagen returns None for unreadable files

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=True)

        robot = next(r for r in collection.items if "Robot" in r.path.name)
        assert robot.meta.author == "Asimov, Issac"
        assert robot.meta.title == "I, Robot"
        assert robot.meta.year == "1950"

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_irobot_with_good_tags_merges_narrator(self, mock_mutagen, tmp_path):
        """Raw tags with composer='Scott Brick' → narrator merged into metadata."""
        self._build_collection(tmp_path)

        def _fake_mutagen(path, easy=False):
            if "Robot" in str(path):
                m = MagicMock()
                m.tags = {
                    "artist": ["Isaac Asimov"],
                    "album": ["I, Robot"],
                    "date": ["1950"],
                    "composer": ["Scott Brick"],
                }
                return m
            return None

        mock_mutagen.side_effect = _fake_mutagen

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=True)

        robot = next(r for r in collection.items if "Robot" in r.path.name)
        assert robot.meta.author == "Asimov, Issac"  # dir author wins
        assert robot.meta.title == "I, Robot"
        assert robot.meta.narrator == "Scott Brick"

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_foundation_dest_path_with_raw_tags(self, mock_mutagen, tmp_path):
        """Full pipeline: raw tags → _clean_tag_title → merge → strip_author → dest path."""
        self._build_collection(tmp_path)

        def _fake_mutagen(path, easy=False):
            if "1951" in str(path):
                m = MagicMock()
                m.tags = {
                    "artist": ["Top 100 Sci-Fi Books"],
                    "album": ["03 - Book 1 - Foundation - Isaac Asimov"],
                    "date": ["1951"],
                }
                return m
            return None

        mock_mutagen.side_effect = _fake_mutagen

        cfg = make_cfg()
        collection = scan_collection(tmp_path, cfg, read_tags=True)

        found = next(r for r in collection.items if "1951" in r.path.name)
        assert found.meta.dest_folder_name() == "1951 - Book 1 - Foundation"
        assert str(found.meta.dest_relative()) == "Asimov, Issac/1951 - Book 1 - Foundation"
