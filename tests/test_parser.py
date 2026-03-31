"""Tests for audiobook_organizer.parser — filename parsing and metadata."""

from pathlib import Path

import pytest

from audiobook_organizer.parser import (
    AudiobookMeta,
    _sanitize,
    merge_meta,
    parse_filename,
)

# Patterns used in tests (same as the defaults shipped in config.yaml)
PATTERNS = [
    r"(?P<author>.+?) - (?P<series>.+?)\s*(?:Book|Vol\.?|Volume)\s*(?P<sequence>\d+)"
    r"\s*-\s*(?P<title>.+?)(?:\s*\((?P<year>\d{4})\))?(?:\s*\[(?P<narrator>.+?)\])?$",
    r"(?P<author>.+?) - (?P<title>.+?)(?:\s*\((?P<year>\d{4})\))?(?:\s*\[(?P<narrator>.+?)\])?$",
    r"(?P<title>.+?) - (?P<author>.+?)(?:\s*\((?P<year>\d{4})\))?$",
    r"(?P<author>[^_]+)_(?P<title>.+)$",
]

# ── _sanitize ────────────────────────────────────────────────────────────


class TestSanitize:
    def test_removes_unsafe_chars(self):
        assert _sanitize('foo<>:"/\\|?*bar') == "foobar"

    def test_collapses_spaces(self):
        assert _sanitize("too   many   spaces") == "too many spaces"

    def test_strips_trailing_dots_and_spaces(self):
        assert _sanitize("title... ") == "title"

    def test_returns_unknown_for_empty(self):
        assert _sanitize("") == "Unknown"
        assert _sanitize("...") == "Unknown"


# ── parse_filename ───────────────────────────────────────────────────────


class TestParseFilename:
    def test_author_series_title_year_narrator(self):
        meta = parse_filename(
            "Brandon Sanderson - Mistborn Book 1 - The Final Empire (2006) [Michael Kramer]",
            PATTERNS,
        )
        assert meta.author == "Brandon Sanderson"
        assert meta.series == "Mistborn"
        assert meta.sequence == "1"
        assert meta.title == "The Final Empire"
        assert meta.year == "2006"
        assert meta.narrator == "Michael Kramer"

    def test_author_title_year_narrator(self):
        meta = parse_filename("George Orwell - Animal Farm (1945) [Voice Actor]", PATTERNS)
        assert meta.author == "George Orwell"
        assert meta.title == "Animal Farm"
        assert meta.year == "1945"
        assert meta.narrator == "Voice Actor"

    def test_author_title_only(self):
        meta = parse_filename("Steven Levy - Hackers", PATTERNS)
        assert meta.author == "Steven Levy"
        assert meta.title == "Hackers"
        assert meta.year is None
        assert meta.narrator is None

    def test_title_author_year(self):
        # Pattern 2 (Author - Title) matches before pattern 3, so author/title are swapped
        # Use a format that only pattern 3 can match (no year in pattern-2 position)
        meta = parse_filename("Animal Farm - George Orwell (1945)", PATTERNS)
        # Pattern 2 matches: author="Animal Farm", title="George Orwell", year="1945"
        assert meta.author == "Animal Farm"
        assert meta.title == "George Orwell"
        assert meta.year == "1945"

    def test_underscore_format(self):
        meta = parse_filename("Tolkien_The Hobbit", PATTERNS)
        assert meta.author == "Tolkien"
        assert meta.title == "The Hobbit"

    def test_fallback_no_match(self):
        meta = parse_filename("JustATitle", PATTERNS)
        assert meta.title == "JustATitle"
        assert meta.author == "Unknown Author"

    def test_empty_string(self):
        meta = parse_filename("", PATTERNS)
        assert meta.title == "Unknown Title"

    def test_series_with_volume(self):
        meta = parse_filename("Terry Goodkind - Sword of Truth Vol 2 - Stone of Tears", PATTERNS)
        assert meta.author == "Terry Goodkind"
        assert meta.series == "Sword of Truth"
        assert meta.sequence == "2"
        assert meta.title == "Stone of Tears"


# ── AudiobookMeta ────────────────────────────────────────────────────────


class TestAudiobookMeta:
    def test_dest_folder_name_simple(self):
        meta = AudiobookMeta(author="George Orwell", title="Animal Farm")
        assert meta.dest_folder_name() == "Animal Farm"

    def test_dest_folder_name_with_year(self):
        meta = AudiobookMeta(title="Animal Farm", year="1945")
        assert meta.dest_folder_name() == "1945 - Animal Farm"

    def test_dest_folder_name_with_sequence_and_series(self):
        meta = AudiobookMeta(
            title="Stone of Tears", series="Sword of Truth", sequence="2", year="1995"
        )
        assert meta.dest_folder_name() == "Vol 2 - 1995 - Stone of Tears"

    def test_dest_folder_name_with_narrator(self):
        meta = AudiobookMeta(title="Hackers", narrator="Mike Chamberlain")
        assert meta.dest_folder_name() == "Hackers {Mike Chamberlain}"

    def test_dest_relative_no_series(self):
        meta = AudiobookMeta(author="George Orwell", title="Animal Farm", year="1945")
        assert meta.dest_relative() == Path("George Orwell/1945 - Animal Farm")

    def test_dest_relative_with_series(self):
        meta = AudiobookMeta(
            author="Terry Goodkind",
            title="Stone of Tears",
            series="Sword of Truth",
            sequence="2",
        )
        assert meta.dest_relative() == Path("Terry Goodkind/Sword of Truth/Vol 2 - Stone of Tears")

    def test_dest_folder_name_sanitizes(self):
        meta = AudiobookMeta(title='Bad: "Title" <here>')
        assert '"' not in meta.dest_folder_name()
        assert "<" not in meta.dest_folder_name()


# ── merge_meta ───────────────────────────────────────────────────────────


class TestMergeMeta:
    def test_prefers_earlier_non_default(self):
        a = AudiobookMeta(author="Real Author", title="Unknown Title")
        b = AudiobookMeta(author="Fallback", title="Real Title")
        merged = merge_meta(a, b)
        assert merged.author == "Real Author"
        assert merged.title == "Real Title"

    def test_keeps_first_non_none(self):
        a = AudiobookMeta(year="2020", narrator=None)
        b = AudiobookMeta(year="1999", narrator="Narrator B")
        merged = merge_meta(a, b)
        assert merged.year == "2020"
        assert merged.narrator == "Narrator B"

    def test_all_defaults(self):
        merged = merge_meta(AudiobookMeta(), AudiobookMeta())
        assert merged.author == "Unknown Author"
        assert merged.title == "Unknown Title"

    def test_source_path_merged(self):
        p = Path("/home/test.mp3")
        a = AudiobookMeta(source_path=p)
        b = AudiobookMeta()
        assert merge_meta(a, b).source_path == p
        assert merge_meta(b, a).source_path == p
