"""Tests for audiobook_organizer.scanner — file/directory scanning."""

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from audiobook_organizer.config import Config
from audiobook_organizer.scanner import (
    _normalize_dedup,
    fold_accents,
    scan_collection,
    scan_sources,
)

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

    def test_skips_article_word_author(self, tmp_path):
        """Folders like 'The - Hobbit' where 'The' is parsed as author should be skipped."""
        src = tmp_path / "downloads"
        book_dir = src / "The - Hobbit"
        _make_audio_file(book_dir / "track01.mp3")

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 0

    def test_skips_article_word_author_file(self, tmp_path):
        """Files like 'The - Title.mp3' where 'The' is parsed as author should be skipped."""
        src = tmp_path / "downloads"
        _make_audio_file(src / "A - Something.mp3")

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 0

    def test_deduplicates_source_dirs_at_list_level(self, tmp_path):
        """Duplicate source_dirs should only be scanned once (not just deduped by entry)."""
        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")

        hit_count = []
        cfg = make_cfg(source_dirs=[src, src, src])
        results, _ = scan_sources(cfg, on_hit=lambda r: hit_count.append(1))
        assert len(results) == 1
        assert len(hit_count) == 1

    def test_check_dir_survives_deleted_file(self, tmp_path):
        """_check_dir should handle files vanishing between iteration and stat."""
        src = tmp_path / "downloads"
        book_dir = src / "Author - Book"
        _make_audio_file(book_dir / "track01.mp3")
        _make_audio_file(book_dir / "track02.mp3")
        # Create and immediately delete a file to simulate race condition
        ghost = book_dir / "track03.mp3"
        ghost.write_bytes(b"\x00" * 2_000_000)

        cfg = make_cfg(source_dirs=[src])
        # Delete after cfg creation but before scan — the rglob may or may not see it
        # but the code should not crash either way
        ghost.unlink()
        results, _ = scan_sources(cfg)
        # Should still find the book with remaining files
        assert len(results) == 1

    def test_deduplicates_accented_authors(self, tmp_path):
        """Authors with accented vs unaccented names should be deduped."""
        s1 = tmp_path / "dir1"
        s2 = tmp_path / "dir2"
        _make_audio_file(s1 / "Gabriel Garcia Marquez - One Hundred Years.mp3")
        _make_audio_file(s2 / "Gabriel García Márquez - One Hundred Years.mp3")

        cfg = make_cfg(source_dirs=[s1, s2])
        results, _ = scan_sources(cfg)
        assert len(results) == 1

    def test_deduplicates_accented_titles(self, tmp_path):
        """Titles with accented vs unaccented characters should be deduped."""
        s1 = tmp_path / "dir1"
        s2 = tmp_path / "dir2"
        _make_audio_file(s1 / "Author - Café Stories.mp3")
        _make_audio_file(s2 / "Author - Cafe Stories.mp3")

        cfg = make_cfg(source_dirs=[s1, s2])
        results, _ = scan_sources(cfg)
        assert len(results) == 1

    def test_normalizes_accented_author_across_books(self, tmp_path):
        """Different books by accented/unaccented author variants get a single author name."""
        s1 = tmp_path / "dir1"
        s2 = tmp_path / "dir2"
        _make_audio_file(s1 / "Gabriel Garcia Marquez - One Hundred Years.mp3")
        _make_audio_file(s2 / "Gabriel García Márquez - Love in the Time.mp3")

        cfg = make_cfg(source_dirs=[s1, s2])
        results, _ = scan_sources(cfg)
        assert len(results) == 2
        # Both should use the same canonical author name
        authors = {r.meta.author for r in results}
        assert len(authors) == 1

    def test_prefers_accented_author_form(self, tmp_path):
        """When accented variant appears second, it should become canonical."""
        s1 = tmp_path / "dir1"
        s2 = tmp_path / "dir2"
        _make_audio_file(s1 / "Gabriel Garcia Marquez - One Hundred Years.mp3")
        _make_audio_file(s2 / "Gabriel García Márquez - Love in the Time.mp3")

        cfg = make_cfg(source_dirs=[s1, s2])
        results, _ = scan_sources(cfg)
        # The accented form should win
        for r in results:
            assert r.meta.author == "Gabriel García Márquez"


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


# ── _normalize_dedup ─────────────────────────────────────────────────────


class TestNormalizeDedup:
    def test_case_insensitive(self):
        assert _normalize_dedup("Author::Title") == _normalize_dedup("author::title")

    def test_accent_folding(self):
        assert _normalize_dedup("García Márquez") == _normalize_dedup("Garcia Marquez")

    def test_combined(self):
        a = _normalize_dedup("Gabriel García Márquez::Cien años de soledad")
        b = _normalize_dedup("gabriel garcia marquez::cien anos de soledad")
        assert a == b

    def test_preserves_distinctness(self):
        assert _normalize_dedup("Author A") != _normalize_dedup("Author B")


class TestFoldAccents:
    """Tests for the shared fold_accents utility."""

    def test_folds_accented_chars(self):
        assert fold_accents("García Márquez") == "Garcia Marquez"

    def test_preserves_ascii(self):
        assert fold_accents("plain text") == "plain text"

    def test_preserves_case(self):
        """fold_accents should not lowercase — that's _normalize_dedup's job."""
        assert fold_accents("García") == "Garcia"

    def test_handles_empty_string(self):
        assert fold_accents("") == ""


class TestCheckDir:
    """Tests for _check_dir: scanning a directory for audiobook content."""

    def test_sets_has_cover_and_file_count(self, tmp_path):
        """Bug fix: _check_dir should populate has_cover and file_count."""
        src = tmp_path / "downloads"
        book = src / "Author - Title"
        _make_audio_file(book / "track01.mp3")
        _make_audio_file(book / "track02.mp3")
        (book / "cover.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 100)

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].has_cover is True
        assert results[0].file_count == 2

    def test_no_cover_sets_false(self, tmp_path):
        src = tmp_path / "downloads"
        book = src / "Author - Title"
        _make_audio_file(book / "track01.mp3")

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 1
        assert results[0].has_cover is False
        assert results[0].file_count == 1


class TestScanSourcesCallbacks:
    """Tests for scan_sources callback and edge case behavior."""

    def test_on_progress_called(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")

        cfg = make_cfg(source_dirs=[src])
        messages = []
        scan_sources(cfg, on_progress=lambda msg: messages.append(msg))
        assert any("Scanning" in m for m in messages)

    def test_on_hit_called(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")

        cfg = make_cfg(source_dirs=[src])
        hits = []
        scan_sources(cfg, on_hit=lambda r: hits.append(r))
        assert len(hits) == 1

    def test_deduplicates_resolved_paths(self, tmp_path):
        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")
        # Create a symlink to the same dir
        link = tmp_path / "link"
        link.symlink_to(src)

        cfg = make_cfg(source_dirs=[src, link])
        results, _ = scan_sources(cfg)
        assert len(results) == 1

    def test_skips_files_below_min_size(self, tmp_path):
        src = tmp_path / "downloads"
        tiny = src / "Author - Title.mp3"
        tiny.parent.mkdir(parents=True, exist_ok=True)
        tiny.write_bytes(b"\x00" * 10)  # Below 100-byte min_file_size

        cfg = make_cfg(source_dirs=[src])
        results, _ = scan_sources(cfg)
        assert len(results) == 0

    def test_scan_with_cache(self, tmp_path):
        from audiobook_organizer.cache import ScanCache

        src = tmp_path / "downloads"
        _make_audio_file(src / "Author - Title.mp3")

        cache = ScanCache(tmp_path / "cache.json")
        cfg = make_cfg(source_dirs=[src])

        # First scan populates cache
        results1, _ = scan_sources(cfg, cache=cache)
        assert len(results1) == 1

        # Second scan uses cache
        results2, _ = scan_sources(cfg, cache=cache)
        assert len(results2) == 1


class TestScanCollectionEdgeCases:
    """Tests for scan_collection edge cases."""

    def test_nonexistent_root_returns_empty(self, tmp_path):
        cfg = make_cfg()
        result = scan_collection(tmp_path / "nonexistent", cfg)
        assert result.items == []
        assert result.empty_dirs == []
        assert result.flat_audio_files == []

    def test_hidden_dirs_skipped(self, tmp_path):
        root = tmp_path / "collection"
        hidden = root / ".hidden" / "Book"
        _make_audio_file(hidden / "track.mp3")

        cfg = make_cfg()
        result = scan_collection(root, cfg)
        assert len(result.items) == 0

    def test_ignored_author_dirs_skipped(self, tmp_path):
        root = tmp_path / "collection"
        for name in ("_new", "_raw_inputs", "_downloads"):
            _make_audio_file(root / name / "Book" / "track.mp3")

        cfg = make_cfg()
        result = scan_collection(root, cfg)
        assert len(result.items) == 0

    def test_flat_audio_files_detected(self, tmp_path):
        root = tmp_path / "collection"
        _make_audio_file(root / "loose_track.mp3")

        cfg = make_cfg()
        result = scan_collection(root, cfg)
        assert len(result.flat_audio_files) == 1

    def test_series_nested_items_found(self, tmp_path):
        root = tmp_path / "collection"
        author_dir = root / "Terry Goodkind"
        series_dir = author_dir / "Sword of Truth"
        _make_audio_file(series_dir / "Vol 1 - Wizards First Rule" / "track.mp3")

        cfg = make_cfg()
        from audiobook_organizer.parser import AudiobookMeta

        with patch("audiobook_organizer.scanner.parse_audio_tags") as mock_tags:
            mock_tags.return_value = AudiobookMeta(
                author="Terry Goodkind",
                title="Wizards First Rule",
                series="Sword of Truth",
                sequence="1",
            )
            result = scan_collection(root, cfg)
        assert len(result.items) == 1

    def test_empty_series_subdir_detected(self, tmp_path):
        root = tmp_path / "collection"
        author_dir = root / "Terry Goodkind"
        series_dir = author_dir / "Sword of Truth"
        empty_title = series_dir / "Empty Book"
        empty_title.mkdir(parents=True)

        cfg = make_cfg()
        result = scan_collection(root, cfg)
        assert empty_title in result.empty_dirs

    def test_scan_collection_with_cache(self, tmp_path):
        from audiobook_organizer.cache import ScanCache
        from audiobook_organizer.parser import AudiobookMeta

        root = tmp_path / "collection"
        _make_audio_file(root / "Author" / "Book Title" / "track.mp3")

        cache = ScanCache(tmp_path / "cache.json")
        cfg = make_cfg()

        with patch("audiobook_organizer.scanner.parse_audio_tags") as mock_tags:
            mock_tags.return_value = AudiobookMeta(
                author="Author",
                title="Book Title",
            )
            result1 = scan_collection(root, cfg, cache=cache)
            # Second scan should hit cache
            result2 = scan_collection(root, cfg, cache=cache)

        assert len(result1.items) == len(result2.items)
