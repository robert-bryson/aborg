"""Tests for audiobook_organizer.parser — filename parsing and metadata."""

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from audiobook_organizer.config import Config
from audiobook_organizer.parser import (
    AudiobookMeta,
    _clean_tag_title,
    _extract_narrator,
    _is_copyright_notice,
    _parse_title_remainder,
    _sanitize,
    _strip_author_from_name,
    _strip_author_noise,
    _strip_embedded_year,
    flip_author_name,
    is_last_first,
    looks_like_author,
    merge_meta,
    normalize_author_format,
    normalize_path_name,
    parse_audio_tags,
    parse_filename,
    parse_metadata_json,
    parse_metadata_json_from_zip,
    parse_title_folder,
    path_parent_name,
    split_path_parts,
    strip_author_from_title,
    strip_narrator_from_author,
)

# Patterns used in tests (same as the defaults shipped in config.yaml)
PATTERNS = list(Config.DEFAULT_PATTERNS)

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
        # Known ambiguity: "Title - Author (Year)" also matches pattern 2
        # (Author - Title) which is tried first, so author/title get swapped.
        # This test documents current behaviour — callers that know the author
        # should use parse_title_folder() instead.
        meta = parse_filename("Animal Farm - George Orwell (1945)", PATTERNS)
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

    def test_strips_audiobook_paren_from_pattern_match(self):
        meta = parse_filename("Beverly Gage - G-Man (Pulitzer Prize Winner)", PATTERNS)
        assert meta.author == "Beverly Gage"
        assert meta.title == "G-Man"

    def test_strips_audiobook_paren_from_fallback(self):
        meta = parse_filename("Fear (Audiobook)", PATTERNS)
        assert meta.title == "Fear"

    def test_strips_audiobook_paren_no_patterns(self):
        meta = parse_filename("Fear (Audiobook)")
        assert meta.title == "Fear"


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

    def test_strips_narrator_from_multi_author(self):
        """When narrator matches second name in 'Author, Narrator' author field."""
        tag = AudiobookMeta(author="Stephen King, Will Patton", narrator="Will Patton")
        name = AudiobookMeta(title="The Outsider")
        merged = merge_meta(tag, name)
        assert merged.author == "Stephen King"
        assert merged.narrator == "Will Patton"

    def test_keeps_genuine_coauthors(self):
        """When narrator is someone else, both authors stay."""
        tag = AudiobookMeta(author="Bob Woodward, Carl Bernstein", narrator="John Doe")
        merged = merge_meta(tag)
        assert merged.author == "Bob Woodward, Carl Bernstein"

    def test_no_narrator_keeps_multi_author(self):
        """When no narrator info, multi-author stays unchanged."""
        tag = AudiobookMeta(author="Bob Woodward, Carl Bernstein")
        merged = merge_meta(tag)
        assert merged.author == "Bob Woodward, Carl Bernstein"

    def test_strips_translator_from_multi_author(self):
        """When translator matches one name in multi-author field."""
        tag = AudiobookMeta(author="Deepa Bhasthi, Banu Mushtaq", translator="Deepa Bhasthi")
        merged = merge_meta(tag)
        assert merged.author == "Banu Mushtaq"
        assert merged.translator == "Deepa Bhasthi"

    def test_merges_translator_field(self):
        a = AudiobookMeta(translator="Deepa Bhasthi")
        b = AudiobookMeta(translator="Someone Else")
        merged = merge_meta(a, b)
        assert merged.translator == "Deepa Bhasthi"


# ── strip_narrator_from_author ───────────────────────────────────────────


class TestStripNarratorFromAuthor:
    def test_narrator_matches_second_author(self):
        meta = AudiobookMeta(author="Stephen King, Ray Porter", narrator="Ray Porter")
        strip_narrator_from_author(meta)
        assert meta.author == "Stephen King"

    def test_narrator_matches_first_author(self):
        meta = AudiobookMeta(author="Ray Porter, Stephen King", narrator="Ray Porter")
        strip_narrator_from_author(meta)
        assert meta.author == "Stephen King"

    def test_no_match_keeps_both(self):
        meta = AudiobookMeta(author="Bob Woodward, Carl Bernstein", narrator="Someone Else")
        strip_narrator_from_author(meta)
        assert meta.author == "Bob Woodward, Carl Bernstein"

    def test_no_narrator_noop(self):
        meta = AudiobookMeta(author="Bob Woodward, Carl Bernstein")
        strip_narrator_from_author(meta)
        assert meta.author == "Bob Woodward, Carl Bernstein"

    def test_single_author_noop(self):
        meta = AudiobookMeta(author="Stephen King", narrator="Ray Porter")
        strip_narrator_from_author(meta)
        assert meta.author == "Stephen King"

    def test_fuzzy_match_strips(self):
        meta = AudiobookMeta(author="Stephen King, Ray A. Porter", narrator="Ray A Porter")
        strip_narrator_from_author(meta)
        assert meta.author == "Stephen King"

    def test_ampersand_separated(self):
        meta = AudiobookMeta(author="Stephen King & Ray Porter", narrator="Ray Porter")
        strip_narrator_from_author(meta)
        assert meta.author == "Stephen King"

    def test_translator_stripped(self):
        meta = AudiobookMeta(author="Deepa Bhasthi, Banu Mushtaq", translator="Deepa Bhasthi")
        strip_narrator_from_author(meta)
        assert meta.author == "Banu Mushtaq"

    def test_both_narrator_and_translator_stripped(self):
        meta = AudiobookMeta(
            author="Translator Name, Real Author, Narrator Name",
            narrator="Narrator Name",
            translator="Translator Name",
        )
        strip_narrator_from_author(meta)
        assert meta.author == "Real Author"

    def test_no_translator_no_narrator_noop(self):
        meta = AudiobookMeta(author="Alice Smith, Bob Jones")
        strip_narrator_from_author(meta)
        assert meta.author == "Alice Smith, Bob Jones"


# ── looks_like_author ────────────────────────────────────────────────────


class TestLooksLikeAuthor:
    def test_real_authors(self):
        assert looks_like_author("Isaac Asimov") is True
        assert looks_like_author("Asimov, Isaac") is True
        assert looks_like_author("J.R.R. Tolkien") is True
        assert looks_like_author("Brandon Sanderson") is True

    def test_category_labels(self):
        assert looks_like_author("Top 100 Sci-Fi Books") is False
        assert looks_like_author("Various Artists") is False
        assert looks_like_author("Audiobooks Collection") is False
        assert looks_like_author("Best Fantasy Classics") is False

    def test_numbered_strings(self):
        assert looks_like_author("03 - Book 1 - Foundation") is False

    def test_empty_and_default(self):
        assert looks_like_author("") is False
        assert looks_like_author("Unknown Author") is False

    def test_single_word_author(self):
        # Single names like "Plato" should pass
        assert looks_like_author("Plato") is True
        assert looks_like_author("Homer") is True

    def test_single_bad_word(self):
        assert looks_like_author("Audiobooks") is False
        assert looks_like_author("Unknown") is False

    def test_tbd_placeholder(self):
        assert looks_like_author("TBD") is False
        assert looks_like_author("tbd") is False


# ── normalize_path_name ──────────────────────────────────────────────────


class TestNormalizePathName:
    def test_windows_unc_path(self):
        name = normalize_path_name(
            r"\\nas\drive\media\audiobooks-test\Asimov, Issac\I, Robot - Isaac Asimov - 1950"
        )
        assert name == "I, Robot - Isaac Asimov - 1950"

    def test_windows_path_with_audio_extension(self):
        name = normalize_path_name(r"\\nas\drive\media\audiobooks-test\Folder\02 02 - I, Robot.mp3")
        assert name == "02 02 - I, Robot"

    def test_unix_path(self):
        assert normalize_path_name("/home/user/downloads/Author - Title") == "Author - Title"

    def test_unix_path_with_extension(self):
        assert normalize_path_name("/home/user/Author - Title.m4b") == "Author - Title"

    def test_plain_string(self):
        assert normalize_path_name("Author - Title") == "Author - Title"

    def test_trailing_slash_stripped(self):
        assert normalize_path_name("/foo/bar/") == "bar"
        assert normalize_path_name("\\\\server\\share\\folder\\") == "folder"

    def test_non_audio_extension_kept(self):
        assert normalize_path_name("/foo/readme.txt") == "readme.txt"


# ── path_parent_name ─────────────────────────────────────────────────────


class TestPathParentName:
    def test_windows_path(self):
        parent = path_parent_name(
            r"\\nas\drive\media\audiobooks-test\Asimov, Issac\I, Robot - Isaac Asimov - 1950"
        )
        assert parent == "Asimov, Issac"

    def test_unix_path(self):
        assert path_parent_name("/home/user/Author/Title") == "Author"

    def test_single_component(self):
        assert path_parent_name("JustAName") is None

    def test_two_components(self):
        assert path_parent_name("parent/child") == "parent"


# ── split_path_parts ─────────────────────────────────────────────────────


class TestSplitPathParts:
    def test_unix_path(self):
        assert split_path_parts("/home/user/Author/Title") == ["home", "user", "Author", "Title"]

    def test_windows_path(self):
        parts = split_path_parts(r"\\nas\share\audiobooks\Author\Title")
        assert parts == ["nas", "share", "audiobooks", "Author", "Title"]

    def test_single_name(self):
        assert split_path_parts("JustAName") == ["JustAName"]

    def test_empty_string(self):
        assert split_path_parts("") == []

    def test_trailing_slashes_stripped(self):
        assert split_path_parts("/a/b/c/") == ["a", "b", "c"]

    def test_mixed_separators(self):
        assert split_path_parts(r"a\b/c\d") == ["a", "b", "c", "d"]


# ── _clean_tag_title ─────────────────────────────────────────────────────


class TestCleanTagTitle:
    def test_strips_leading_number(self):
        assert _clean_tag_title("03 - Book 1 - Foundation") == "Book 1 - Foundation"

    def test_strips_dotted_number(self):
        assert _clean_tag_title("01. Chapter One") == "Chapter One"

    def test_leaves_clean_title(self):
        assert _clean_tag_title("Foundation") == "Foundation"

    def test_unknown_title_passthrough(self):
        assert _clean_tag_title("Unknown Title") == "Unknown Title"

    def test_empty_passthrough(self):
        assert _clean_tag_title("") == ""

    def test_strips_pulitzer_prize_winner(self):
        assert _clean_tag_title("G-Man (Pulitzer Prize Winner)") == "G-Man"

    def test_strips_award_winner(self):
        assert _clean_tag_title("The Overstory (National Book Award Winner)") == "The Overstory"

    def test_strips_bestseller(self):
        assert _clean_tag_title("Atomic Habits (#1 New York Times Bestseller)") == "Atomic Habits"

    def test_leaves_non_award_parens(self):
        assert _clean_tag_title("All About Love (New Visions)") == "All About Love (New Visions)"

    def test_strips_audiobook_paren(self):
        assert _clean_tag_title("Fear (Audiobook)") == "Fear"

    def test_strips_unabridged_paren(self):
        assert _clean_tag_title("Dune (Unabridged)") == "Dune"


# ── parse_audio_tags (with mocked Mutagen) ───────────────────────────────


class TestParseAudioTags:
    def _mock_tags(self, tags: dict) -> MagicMock:
        """Create a mock Mutagen file with the given easy tags."""
        mock_audio = MagicMock()
        mock_audio.tags = {k: [v] for k, v in tags.items()}
        return mock_audio

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_good_metadata(self, mock_mutagen):
        mock_mutagen.return_value = self._mock_tags(
            {
                "artist": "Isaac Asimov",
                "album": "Foundation",
                "date": "1951",
                "composer": "Scott Brick",
            }
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Isaac Asimov"
        assert meta.title == "Foundation"
        assert meta.year == "1951"
        assert meta.narrator == "Scott Brick"

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_rejects_category_artist(self, mock_mutagen):
        mock_mutagen.return_value = self._mock_tags(
            {
                "artist": "Top 100 Sci-Fi Books",
                "album": "03 - Book 1 - Foundation - Isaac Asimov",
            }
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        # Should NOT use the garbage artist
        assert meta.author == "Unknown Author"
        # Title should be cleaned (leading number stripped)
        assert meta.title == "Book 1 - Foundation - Isaac Asimov"

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_prefers_albumartist_over_bad_artist(self, mock_mutagen):
        mock_mutagen.return_value = self._mock_tags(
            {
                "artist": "Various Artists",
                "albumartist": "Isaac Asimov",
                "album": "Foundation",
            }
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Isaac Asimov"

    @patch("audiobook_organizer.parser.MutagenFile")
    def test_no_tags(self, mock_mutagen):
        mock_mutagen.return_value = None
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Unknown Author"
        assert meta.title == "Unknown Title"

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_slash_separated_author_narrator(self, mock_mutagen, mock_translator):
        """Artist tag with Author/Narrator should split correctly."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {"artist": "Ron Chernow/Scott Brick", "album": "Alexander Hamilton"}
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Ron Chernow"
        assert meta.narrator == "Scott Brick"
        assert meta.title == "Alexander Hamilton"

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_slash_separated_with_copyright(self, mock_mutagen, mock_translator):
        """Copyright notice in artist tag should be filtered out."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {
                "artist": "George R.R. Martin/John Lee/(c) 2005 by George R.R. Martin",
                "album": "A Feast for Crows",
            }
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "George R.R. Martin"
        assert meta.narrator == "John Lee"

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_slash_separated_html_copyright(self, mock_mutagen, mock_translator):
        """HTML-encoded copyright entity in artist tag should be filtered."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {
                "artist": "John Steinbeck/Gary Sinise/&#169;1937 John Steinbeck",
                "album": "Of Mice and Men",
            }
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "John Steinbeck"
        assert meta.narrator == "Gary Sinise"

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_slash_duplicate_name(self, mock_mutagen, mock_translator):
        """Same person as author and narrator should not duplicate."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {"artist": "Donna Tartt/Donna Tartt", "album": "The Goldfinch"}
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Donna Tartt"
        # Duplicate name should NOT be set as narrator
        assert meta.narrator is None

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_composer_overrides_slash_narrator(self, mock_mutagen, mock_translator):
        """Composer tag should take precedence over /-extracted narrator."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {
                "artist": "Ron Chernow/Scott Brick",
                "album": "Alexander Hamilton",
                "composer": "A Different Narrator",
            }
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Ron Chernow"
        assert meta.narrator == "A Different Narrator"

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_html_entity_decoded_in_title(self, mock_mutagen, mock_translator):
        """HTML entities like &#8211; should be decoded in titles."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {
                "artist": "David M. Kennedy",
                "album": "Freedom from Fear &#8211; 1929&#8211;1945",
            }
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert "\u2013" in meta.title  # en-dash
        assert "&#" not in meta.title

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_author_audio_qualifier_stripped(self, mock_mutagen, mock_translator):
        """Parenthetical qualifiers like (audio) should be stripped from author."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {"artist": "Frank Herbert (audio)", "album": "Dune"}
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Frank Herbert"

    @patch("audiobook_organizer.parser._read_translator")
    @patch("audiobook_organizer.parser.MutagenFile")
    def test_tbd_narrator_filtered(self, mock_mutagen, mock_translator):
        """TBD placeholder should not be used as narrator."""
        mock_translator.return_value = None
        mock_mutagen.return_value = self._mock_tags(
            {"artist": "Ann Patchett/TBD", "album": "Commonwealth"}
        )
        meta = parse_audio_tags(Path("/fake/audio.mp3"))
        assert meta.author == "Ann Patchett"
        # "TBD" should not pass looks_like_author, so narrator stays None
        assert meta.narrator is None


# ── _is_copyright_notice ─────────────────────────────────────────────────


class TestIsCopyrightNotice:
    def test_c_paren(self):
        assert _is_copyright_notice("(c) 2005 by George R.R. Martin") is True

    def test_p_paren(self):
        assert _is_copyright_notice("(p) 2012 HighBridge Company") is True

    def test_copyright_symbol(self):
        assert _is_copyright_notice("\u00a91937 John Steinbeck") is True

    def test_html_copyright_entity(self):
        assert _is_copyright_notice("&#169;1937 John Steinbeck") is True

    def test_copyright_word(self):
        assert _is_copyright_notice("Copyright renewed John Steinbeck, 1965.") is True

    def test_normal_name(self):
        assert _is_copyright_notice("George R.R. Martin") is False

    def test_narrator_name(self):
        assert _is_copyright_notice("Scott Brick") is False


# ── _strip_author_noise ──────────────────────────────────────────────────


class TestStripAuthorNoise:
    def test_strips_audio_qualifier(self):
        assert _strip_author_noise("Frank Herbert (audio)") == "Frank Herbert"

    def test_strips_narrator_qualifier(self):
        assert _strip_author_noise("Jane Austen (narrator)") == "Jane Austen"

    def test_preserves_normal_name(self):
        assert _strip_author_noise("George Orwell") == "George Orwell"

    def test_case_insensitive(self):
        assert _strip_author_noise("Author (AUDIO)") == "Author"


# ── _strip_author_from_name ──────────────────────────────────────────────


class TestStripAuthorFromName:
    def test_strips_exact_match(self):
        assert (
            _strip_author_from_name("I, Robot - Isaac Asimov - 1950", "Isaac Asimov")
            == "I, Robot - 1950"
        )

    def test_strips_flipped_name(self):
        # Known author is "Last, First" but folder has "First Last"
        assert (
            _strip_author_from_name("I, Robot - Isaac Asimov - 1950", "Asimov, Isaac")
            == "I, Robot - 1950"
        )

    def test_strips_with_typo(self):
        # "Issac" vs "Isaac" — fuzzy match should handle it
        assert _strip_author_from_name("Isaac Asimov - 1951", "Asimov, Issac") == "1951"

    def test_returns_none_for_no_match(self):
        assert _strip_author_from_name("Foundation - 1951", "Brandon Sanderson") is None

    def test_returns_none_for_single_segment(self):
        assert _strip_author_from_name("Foundation", "Isaac Asimov") is None

    def test_strips_from_three_segments(self):
        result = _strip_author_from_name("Book 1 - Foundation - Isaac Asimov", "Asimov, Issac")
        assert result == "Book 1 - Foundation"


# ── _parse_title_remainder ───────────────────────────────────────────────


class TestParseTitleRemainder:
    """Comprehensive tests for ABS-style title folder name parsing.

    Covers all Audiobookshelf naming conventions plus messy real-world data.
    """

    # -- Basic patterns --

    def test_plain_title(self):
        meta = _parse_title_remainder("Foundation")
        assert meta.title == "Foundation"
        assert meta.year is None
        assert meta.sequence is None
        assert meta.narrator is None

    def test_empty_string(self):
        meta = _parse_title_remainder("")
        assert meta.title == "Unknown Title"

    def test_whitespace_only(self):
        meta = _parse_title_remainder("   ")
        assert meta.title == "Unknown Title"

    # -- Year extraction --

    def test_title_dash_year(self):
        meta = _parse_title_remainder("I, Robot - 1950")
        assert meta.title == "I, Robot"
        assert meta.year == "1950"

    def test_title_paren_year(self):
        meta = _parse_title_remainder("Foundation (1951)")
        assert meta.title == "Foundation"
        assert meta.year == "1951"

    def test_year_dash_title(self):
        meta = _parse_title_remainder("1951 - Foundation")
        assert meta.title == "Foundation"
        assert meta.year == "1951"

    def test_paren_year_prefix(self):
        """ABS format: (YYYY) - Title"""
        meta = _parse_title_remainder("(1994) - Wizards First Rule")
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"

    def test_bare_year(self):
        meta = _parse_title_remainder("1951")
        assert meta.title == "Unknown Title"
        assert meta.year == "1951"

    # -- Narrator extraction --

    def test_narrator_curly_braces(self):
        """ABS convention: {Narrator Name}"""
        meta = _parse_title_remainder("Wizards First Rule {Sam Tsoutsouvas}")
        assert meta.title == "Wizards First Rule"
        assert meta.narrator == "Sam Tsoutsouvas"

    def test_narrator_square_brackets(self):
        """Common alternative: [Narrator Name]"""
        meta = _parse_title_remainder("Foundation [Scott Brick]")
        assert meta.title == "Foundation"
        assert meta.narrator == "Scott Brick"

    def test_narrator_with_year(self):
        meta = _parse_title_remainder("1994 - Wizards First Rule {Sam Tsoutsouvas}")
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.narrator == "Sam Tsoutsouvas"

    # -- Sequence (Vol/Book/bare number) extraction --

    def test_vol_prefix(self):
        meta = _parse_title_remainder("Vol 1 - Wizards First Rule")
        assert meta.title == "Wizards First Rule"
        assert meta.sequence == "1"

    def test_vol_dot_prefix(self):
        meta = _parse_title_remainder("Vol. 2 - Stone of Tears")
        assert meta.title == "Stone of Tears"
        assert meta.sequence == "2"

    def test_volume_prefix(self):
        meta = _parse_title_remainder("Volume 3 - Blood of the Fold")
        assert meta.title == "Blood of the Fold"
        assert meta.sequence == "3"

    def test_book_prefix(self):
        meta = _parse_title_remainder("Book 1 - The Final Empire")
        assert meta.title == "The Final Empire"
        assert meta.sequence == "1"

    def test_bare_number_prefix(self):
        """ABS format: '1 - Title' or '1. Title'"""
        meta = _parse_title_remainder("1 - Wizards First Rule")
        assert meta.title == "Wizards First Rule"
        assert meta.sequence == "1"

    def test_bare_number_dot_prefix(self):
        meta = _parse_title_remainder("1. Wizards First Rule")
        assert meta.title == "Wizards First Rule"
        assert meta.sequence == "1"

    def test_decimal_sequence(self):
        """ABS supports decimal sequences."""
        meta = _parse_title_remainder("Vol 1.5 - A Novella")
        assert meta.title == "A Novella"
        assert meta.sequence == "1.5"

    def test_trailing_volume(self):
        """ABS format: Title - Volume 1"""
        meta = _parse_title_remainder("Wizards First Rule - Volume 1")
        assert meta.title == "Wizards First Rule"
        assert meta.sequence == "1"

    def test_trailing_book(self):
        meta = _parse_title_remainder("Wizards First Rule - Book 2")
        assert meta.title == "Wizards First Rule"
        assert meta.sequence == "2"

    # -- Combined ABS formats (from documentation) --

    def test_abs_vol_year_title(self):
        """ABS: Vol 1 - 1994 - Wizards First Rule"""
        meta = _parse_title_remainder("Vol 1 - 1994 - Wizards First Rule")
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"

    def test_abs_vol_year_title_narrator(self):
        """ABS: Vol. 1 - 1994 - Wizards First Rule {Sam Tsoutsouvas}"""
        meta = _parse_title_remainder("Vol. 1 - 1994 - Wizards First Rule {Sam Tsoutsouvas}")
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"
        assert meta.narrator == "Sam Tsoutsouvas"

    def test_abs_year_book_title(self):
        """ABS: 1994 - Book 1 - Wizards First Rule"""
        meta = _parse_title_remainder("1994 - Book 1 - Wizards First Rule")
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"

    def test_abs_year_title_volume_suffix(self):
        """ABS: 1994 - Wizards First Rule - Volume 1"""
        meta = _parse_title_remainder("1994 - Wizards First Rule - Volume 1")
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"

    def test_abs_paren_year_title_subtitle(self):
        """ABS: (1994) - Wizards First Rule - A Really Good Subtitle"""
        meta = _parse_title_remainder("(1994) - Wizards First Rule - A Really Good Subtitle")
        assert meta.title == "Wizards First Rule - A Really Good Subtitle"
        assert meta.year == "1994"

    def test_abs_full_complex(self):
        """ABS: Vol. 1 - 1994 - Title - Subtitle {Narrator}"""
        meta = _parse_title_remainder(
            "Vol. 1 - 1994 - Wizards First Rule - A Really Good Subtitle {Sam Tsoutsouvas}"
        )
        assert meta.title == "Wizards First Rule - A Really Good Subtitle"
        assert meta.year == "1994"
        assert meta.sequence == "1"
        assert meta.narrator == "Sam Tsoutsouvas"

    # -- Messy / edge-case real-world data --

    def test_extra_whitespace(self):
        meta = _parse_title_remainder("  Foundation   -   1951  ")
        assert meta.title == "Foundation"
        assert meta.year == "1951"

    def test_en_dash_separator(self):
        """Unicode en-dash (U+2013) used as separator."""
        meta = _parse_title_remainder("1951 \u2013 Foundation")
        assert meta.title == "Foundation"
        assert meta.year == "1951"

    def test_em_dash_separator(self):
        """Unicode em-dash (U+2014) used as separator."""
        meta = _parse_title_remainder("Foundation \u2014 1951")
        assert meta.title == "Foundation"
        assert meta.year == "1951"

    def test_title_with_colon(self):
        meta = _parse_title_remainder("The Hobbit: An Unexpected Journey")
        assert meta.title == "The Hobbit: An Unexpected Journey"

    def test_title_with_numbers_not_year(self):
        """Titles containing numbers shouldn't be mis-parsed as years."""
        meta = _parse_title_remainder("Fahrenheit 451")
        assert meta.title == "Fahrenheit 451"
        assert meta.year is None

    def test_title_with_numbers_and_year(self):
        meta = _parse_title_remainder("Fahrenheit 451 (1953)")
        assert meta.title == "Fahrenheit 451"
        assert meta.year == "1953"

    def test_title_starting_with_number(self):
        """4-digit number as part of title, not year, when there's more text."""
        meta = _parse_title_remainder("2001 A Space Odyssey")
        # No dash separator → treated as a plain title, not a year
        assert meta.title == "2001 A Space Odyssey"
        assert meta.year is None

    def test_title_2001_with_year(self):
        meta = _parse_title_remainder("1968 - 2001 A Space Odyssey")
        assert meta.title == "2001 A Space Odyssey"
        assert meta.year == "1968"

    def test_narrator_with_dots(self):
        meta = _parse_title_remainder("Foundation {J.R.R. Tolkien}")
        assert meta.narrator == "J.R.R. Tolkien"

    def test_three_digit_sequence_not_year(self):
        """3-digit leading number is a sequence, not a year."""
        meta = _parse_title_remainder("100 - The Hundredth Title")
        assert meta.sequence == "100"
        assert meta.title == "The Hundredth Title"
        assert meta.year is None

    def test_year_range_not_split(self):
        """Year ranges like 1944-1956 must stay intact in the title."""
        meta = _parse_title_remainder("The Crushing of Eastern Europe, 1944-1956")
        assert meta.title == "The Crushing of Eastern Europe, 1944-1956"
        assert meta.year is None


# ── parse_title_folder ───────────────────────────────────────────────────


class TestParseTitleFolder:
    """Tests for author-aware title folder parsing, including messy data."""

    # -- Existing Asimov bug-fix cases --

    def test_strips_author_extracts_title_and_year(self):
        meta = parse_title_folder(
            "I, Robot - Isaac Asimov - 1950",
            "Asimov, Issac",
            PATTERNS,
        )
        assert meta.author == "Asimov, Issac"
        assert meta.title == "I, Robot"
        assert meta.year == "1950"

    def test_author_only_remainder_is_year(self):
        meta = parse_title_folder("Isaac Asimov - 1951", "Asimov, Issac", PATTERNS)
        assert meta.author == "Asimov, Issac"
        assert meta.title == "Unknown Title"
        assert meta.year == "1951"

    def test_no_author_in_name_uses_remainder(self):
        meta = parse_title_folder("Foundation", "Asimov, Isaac", PATTERNS)
        assert meta.author == "Asimov, Isaac"
        assert meta.title == "Foundation"

    def test_year_title_format(self):
        meta = parse_title_folder("1951 - Foundation", "Asimov, Isaac", PATTERNS)
        assert meta.title == "Foundation"
        assert meta.year == "1951"
        assert meta.author == "Asimov, Isaac"

    def test_title_with_parenthesized_year(self):
        meta = parse_title_folder(
            "Mistborn Book 1 - The Final Empire (2006)",
            "Brandon Sanderson",
            PATTERNS,
        )
        assert meta.author == "Brandon Sanderson"
        assert meta.title == "Mistborn Book 1 - The Final Empire"
        assert meta.year == "2006"

    def test_dest_folder_name_after_fix(self):
        meta = parse_title_folder(
            "I, Robot - Isaac Asimov - 1950",
            "Asimov, Issac",
            PATTERNS,
        )
        assert meta.dest_folder_name() == "1950 - I, Robot"

    # -- ABS-style folder names with known author --

    def test_abs_narrator_curly_braces(self):
        meta = parse_title_folder(
            "Foundation {Scott Brick}",
            "Isaac Asimov",
            PATTERNS,
        )
        assert meta.title == "Foundation"
        assert meta.narrator == "Scott Brick"
        assert meta.author == "Isaac Asimov"

    def test_abs_narrator_square_brackets(self):
        meta = parse_title_folder(
            "Foundation [Scott Brick]",
            "Isaac Asimov",
            PATTERNS,
        )
        assert meta.title == "Foundation"
        assert meta.narrator == "Scott Brick"

    def test_abs_vol_year_title(self):
        meta = parse_title_folder(
            "Vol 1 - 1994 - Wizards First Rule",
            "Terry Goodkind",
            PATTERNS,
        )
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"
        assert meta.author == "Terry Goodkind"

    def test_abs_vol_year_title_narrator(self):
        meta = parse_title_folder(
            "Vol. 1 - 1994 - Wizards First Rule {Sam Tsoutsouvas}",
            "Terry Goodkind",
            PATTERNS,
        )
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"
        assert meta.narrator == "Sam Tsoutsouvas"

    def test_abs_year_book_title(self):
        meta = parse_title_folder(
            "1994 - Book 1 - Wizards First Rule",
            "Terry Goodkind",
            PATTERNS,
        )
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"

    def test_abs_paren_year(self):
        meta = parse_title_folder(
            "(2006) - The Final Empire",
            "Brandon Sanderson",
            PATTERNS,
        )
        assert meta.title == "The Final Empire"
        assert meta.year == "2006"

    def test_abs_bare_sequence(self):
        meta = parse_title_folder(
            "1 - Wizards First Rule",
            "Terry Goodkind",
            PATTERNS,
        )
        assert meta.title == "Wizards First Rule"
        assert meta.sequence == "1"

    def test_abs_trailing_volume(self):
        meta = parse_title_folder(
            "1994 - Wizards First Rule - Volume 1",
            "Terry Goodkind",
            PATTERNS,
        )
        assert meta.title == "Wizards First Rule"
        assert meta.year == "1994"
        assert meta.sequence == "1"

    # -- Messy real-world data --

    def test_author_with_typo_stripped(self):
        """Fuzzy matching handles 'Issac' vs 'Isaac' in folder name."""
        meta = parse_title_folder(
            "Issac Asimov - Foundation - 1951",
            "Asimov, Isaac",
            PATTERNS,
        )
        assert meta.author == "Asimov, Isaac"
        assert meta.title == "Foundation"
        assert meta.year == "1951"

    def test_author_stripped_narrator_preserved(self):
        """Author stripped but narrator in braces is preserved."""
        meta = parse_title_folder(
            "I, Robot - Isaac Asimov {Scott Brick}",
            "Asimov, Issac",
            PATTERNS,
        )
        assert meta.title == "I, Robot"
        assert meta.narrator == "Scott Brick"
        assert meta.author == "Asimov, Issac"

    def test_messy_title_only_year_and_author(self):
        """Folder has only author + year — title should be Unknown."""
        meta = parse_title_folder(
            "Isaac Asimov - 1951",
            "Asimov, Issac",
            PATTERNS,
        )
        assert meta.year == "1951"
        assert meta.title == "Unknown Title"
        # Must not suggest Asimov as the title
        assert "Asimov" not in meta.dest_folder_name()

    def test_title_with_comma_not_confused_with_author(self):
        """Titles containing commas shouldn't trip up parsing."""
        meta = parse_title_folder(
            "Caves of Steel, The",
            "Isaac Asimov",
            PATTERNS,
        )
        assert meta.title == "Caves of Steel, The"
        assert meta.author == "Isaac Asimov"

    def test_multiple_dashes_preserves_subtitle(self):
        """Multiple dashes: subtitle preserved as part of title."""
        meta = parse_title_folder(
            "Heart of Black Ice - Sister of Darkness",
            "Terry Goodkind",
            PATTERNS,
        )
        assert meta.title == "Heart of Black Ice - Sister of Darkness"

    def test_dest_relative_with_series(self):
        """dest_relative includes series folder when series is set."""
        meta = parse_title_folder(
            "Vol 1 - 1994 - Wizards First Rule",
            "Terry Goodkind",
            PATTERNS,
        )
        meta.series = "Sword of Truth"
        expected = "Terry Goodkind/Sword of Truth/Vol 1 - 1994 - Wizards First Rule"
        assert str(meta.dest_relative()) == expected

    def test_strips_by_author_and_audiobook_paren(self):
        """Messy download folder: 'Title by Author (Audiobook)' is cleaned."""
        meta = parse_title_folder(
            "Fear, Trump in the White House by Bob Woodward (Audiobook)",
            "Woodward, Bob",
            PATTERNS,
        )
        assert meta.author == "Woodward, Bob"
        assert meta.title == "Fear, Trump in the White House"

    def test_strips_by_author_no_parens(self):
        """Folder with 'by Author' but no parenthetical."""
        meta = parse_title_folder(
            "The Great Gatsby by F. Scott Fitzgerald",
            "F. Scott Fitzgerald",
            PATTERNS,
        )
        assert meta.title == "The Great Gatsby"
        assert meta.author == "F. Scott Fitzgerald"

    def test_strips_audiobook_paren_no_by_author(self):
        """Folder with (Audiobook) but no 'by Author'."""
        meta = parse_title_folder(
            "Fear (Audiobook)",
            "Bob Woodward",
            PATTERNS,
        )
        assert meta.title == "Fear"
        assert meta.author == "Bob Woodward"

    def test_year_range_in_title_preserved(self):
        """Year range 1944-1956 must not be split into title + year."""
        meta = parse_title_folder(
            "The Crushing of Eastern Europe, 1944-1956",
            "Applebaum, Anne",
            PATTERNS,
        )
        assert meta.author == "Applebaum, Anne"
        assert meta.title == "The Crushing of Eastern Europe, 1944-1956"
        assert meta.year is None

    def test_iron_curtain_full_title_by_author(self):
        """Full book title with 'by Author' and year range preserved."""
        meta = parse_title_folder(
            "Iron Curtain The Crushing of Eastern Europe 1944-1956 by Anne Applebaum",
            "Applebaum, Anne",
            PATTERNS,
        )
        assert meta.author == "Applebaum, Anne"
        assert meta.title == "Iron Curtain The Crushing of Eastern Europe 1944-1956"
        assert meta.year is None


# ── strip_author_from_title ──────────────────────────────────────────────


class TestStripAuthorFromTitle:
    def test_strips_author_from_tag_title(self):
        assert (
            strip_author_from_title("Book 1 - Foundation - Isaac Asimov", "Asimov, Issac")
            == "Book 1 - Foundation"
        )

    def test_leaves_clean_title(self):
        assert strip_author_from_title("Foundation", "Isaac Asimov") == "Foundation"

    def test_leaves_title_without_author(self):
        assert (
            strip_author_from_title("The Final Empire", "Brandon Sanderson") == "The Final Empire"
        )

    def test_strips_by_author(self):
        assert (
            strip_author_from_title(
                "Fear, Trump in the White House by Bob Woodward",
                "Woodward, Bob",
            )
            == "Fear, Trump in the White House"
        )


# ── parse_metadata_json ──────────────────────────────────────────────────


class TestParseMetadataJson:
    def test_full_metadata(self, tmp_path):
        """Parse a complete metadata.json with author and narrator."""
        meta_dir = tmp_path / "metadata"
        meta_dir.mkdir()
        (meta_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "title": "A Tale of Two Cities",
                    "creator": [
                        {"name": "Charles Dickens", "role": "aut"},
                        {"name": "Adam Henderson", "role": "nrt"},
                    ],
                }
            )
        )
        meta = parse_metadata_json(tmp_path)
        assert meta is not None
        assert meta.author == "Charles Dickens"
        assert meta.title == "A Tale of Two Cities"
        assert meta.narrator == "Adam Henderson"

    def test_no_metadata_dir(self, tmp_path):
        """Returns None when metadata/metadata.json doesn't exist."""
        assert parse_metadata_json(tmp_path) is None

    def test_empty_json(self, tmp_path):
        """Returns defaults for an empty JSON object."""
        meta_dir = tmp_path / "metadata"
        meta_dir.mkdir()
        (meta_dir / "metadata.json").write_text("{}")
        meta = parse_metadata_json(tmp_path)
        assert meta is not None
        assert meta.author == "Unknown Author"
        assert meta.title == "Unknown Title"

    def test_title_only(self, tmp_path):
        """Extracts title when no creator entries."""
        meta_dir = tmp_path / "metadata"
        meta_dir.mkdir()
        (meta_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "title": "Foundation",
                }
            )
        )
        meta = parse_metadata_json(tmp_path)
        assert meta is not None
        assert meta.title == "Foundation"
        assert meta.author == "Unknown Author"

    def test_multiple_authors_uses_first(self, tmp_path):
        """Uses the first author when multiple are listed."""
        meta_dir = tmp_path / "metadata"
        meta_dir.mkdir()
        (meta_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "title": "Collab Book",
                    "creator": [
                        {"name": "Author One", "role": "aut"},
                        {"name": "Author Two", "role": "aut"},
                    ],
                }
            )
        )
        meta = parse_metadata_json(tmp_path)
        assert meta is not None
        assert meta.author == "Author One"

    def test_invalid_json(self, tmp_path):
        """Returns None for unparsable JSON."""
        meta_dir = tmp_path / "metadata"
        meta_dir.mkdir()
        (meta_dir / "metadata.json").write_text("not json at all")
        assert parse_metadata_json(tmp_path) is None

    def test_creator_with_unknown_roles_ignored(self, tmp_path):
        """Only aut, nrt, and trl roles are used."""
        meta_dir = tmp_path / "metadata"
        meta_dir.mkdir()
        (meta_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "title": "Illustrated Book",
                    "creator": [
                        {"name": "Some Illustrator", "role": "ill"},
                        {"name": "Real Author", "role": "aut"},
                    ],
                }
            )
        )
        meta = parse_metadata_json(tmp_path)
        assert meta is not None
        assert meta.author == "Real Author"
        assert meta.narrator is None

    def test_translator_role_extracted(self, tmp_path):
        """Extracts translator from 'trl' role."""
        meta_dir = tmp_path / "metadata"
        meta_dir.mkdir()
        (meta_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "title": "Heart Lamp",
                    "creator": [
                        {"name": "Banu Mushtaq", "role": "aut"},
                        {"name": "Deepa Bhasthi", "role": "trl"},
                    ],
                }
            )
        )
        meta = parse_metadata_json(tmp_path)
        assert meta is not None
        assert meta.author == "Banu Mushtaq"
        assert meta.translator == "Deepa Bhasthi"


# ── parse_metadata_json_from_zip ─────────────────────────────────────────


def _make_metadata_zip(tmp_path, metadata_dict, *, prefix=""):
    """Helper: create a zip with metadata/metadata.json inside."""
    zip_path = tmp_path / "book.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(prefix + "metadata/metadata.json", json.dumps(metadata_dict))
        # Add a dummy audio file so the zip isn't empty.
        zf.writestr(prefix + "Part 001.mp3", b"fake audio")
    return zip_path


class TestParseMetadataJsonFromZip:
    def test_full_metadata(self, tmp_path):
        """Read author, title, narrator from zip."""
        zp = _make_metadata_zip(
            tmp_path,
            {
                "title": "A Tale of Two Cities",
                "creator": [
                    {"name": "Charles Dickens", "role": "aut"},
                    {"name": "Adam Henderson", "role": "nrt"},
                ],
            },
        )
        meta = parse_metadata_json_from_zip(zp)
        assert meta is not None
        assert meta.author == "Charles Dickens"
        assert meta.title == "A Tale of Two Cities"
        assert meta.narrator == "Adam Henderson"

    def test_nested_in_subdirectory(self, tmp_path):
        """metadata.json wrapped in a top-level folder inside the zip."""
        zp = _make_metadata_zip(
            tmp_path,
            {
                "title": "Foundation",
                "creator": [{"name": "Isaac Asimov", "role": "aut"}],
            },
            prefix="- Foundation/",
        )
        meta = parse_metadata_json_from_zip(zp)
        assert meta is not None
        assert meta.author == "Isaac Asimov"
        assert meta.title == "Foundation"

    def test_no_metadata_in_zip(self, tmp_path):
        """Returns None when zip has no metadata.json."""
        zip_path = tmp_path / "book.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Part 001.mp3", b"fake audio")
        assert parse_metadata_json_from_zip(zip_path) is None

    def test_invalid_zip(self, tmp_path):
        """Returns None for a non-zip file."""
        fake = tmp_path / "book.zip"
        fake.write_text("not a zip")
        assert parse_metadata_json_from_zip(fake) is None

    def test_corrupt_json_in_zip(self, tmp_path):
        """Returns None when metadata.json inside zip is not valid JSON."""
        zip_path = tmp_path / "book.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("metadata/metadata.json", "not json")
        assert parse_metadata_json_from_zip(zip_path) is None


# ── _strip_embedded_year ─────────────────────────────────────────────────


class TestStripEmbeddedYear:
    def test_trailing_dash_year(self):
        assert _strip_embedded_year("I, Robot - 1950", "1950") == "I, Robot"

    def test_trailing_paren_year(self):
        assert _strip_embedded_year("Foundation (1951)", "1951") == "Foundation"

    def test_leading_year(self):
        assert _strip_embedded_year("1951 - Foundation", "1951") == "Foundation"

    def test_leading_paren_year(self):
        assert _strip_embedded_year("(1951) - Foundation", "1951") == "Foundation"

    def test_no_match_different_year(self):
        assert _strip_embedded_year("I, Robot - 1950", "1951") == "I, Robot - 1950"

    def test_no_match_no_year(self):
        assert _strip_embedded_year("Foundation", "1951") == "Foundation"

    def test_year_range_preserved(self):
        """Year ranges like 1944-1956 must not be broken."""
        assert (
            _strip_embedded_year("The Crushing of Eastern Europe, 1944-1956", "1956")
            == "The Crushing of Eastern Europe, 1944-1956"
        )

    def test_title_is_only_year(self):
        """If stripping would leave an empty title, return original."""
        assert _strip_embedded_year("1951", "1951") == "1951"


class TestDestFolderNameYearDedup:
    """Ensure dest_folder_name doesn't duplicate the year."""

    def test_no_duplication_when_year_in_title(self):
        meta = AudiobookMeta(title="I, Robot - 1950", year="1950")
        assert meta.dest_folder_name() == "1950 - I, Robot"

    def test_no_duplication_with_paren_year_in_title(self):
        meta = AudiobookMeta(title="Foundation (1951)", year="1951")
        assert meta.dest_folder_name() == "1951 - Foundation"

    def test_no_duplication_book_number_and_year_in_title(self):
        meta = AudiobookMeta(title="Book 1 - Foundation - 1951", year="1951")
        assert meta.dest_folder_name() == "1951 - Book 1 - Foundation"

    def test_normal_case_still_works(self):
        meta = AudiobookMeta(title="Foundation", year="1951")
        assert meta.dest_folder_name() == "1951 - Foundation"


# ── _extract_narrator ────────────────────────────────────────────────────


class TestExtractNarrator:
    def test_curly_braces(self):
        narrator, text = _extract_narrator("Foundation {Scott Brick}")
        assert narrator == "Scott Brick"
        assert text == "Foundation"

    def test_square_brackets(self):
        narrator, text = _extract_narrator("Foundation [Scott Brick]")
        assert narrator == "Scott Brick"
        assert text == "Foundation"

    def test_no_narrator(self):
        narrator, text = _extract_narrator("Foundation")
        assert narrator is None
        assert text == "Foundation"


# ── is_last_first / flip_author_name / normalize_author_format ───────────


class TestAuthorFormatHelpers:
    def test_is_last_first(self):
        assert is_last_first("Applebaum, Anne") is True
        assert is_last_first("Austen, Jane") is True
        assert is_last_first("Candice Millard") is False
        assert is_last_first("E. B. White") is False

    def test_is_last_first_multi_author(self):
        # Comma-separated "First Last" names are NOT "Last, First"
        assert is_last_first("Bob Woodward, Carl Bernstein") is False
        assert is_last_first("Deepa Bhasthi, Banu Mushtaq") is False
        # Ampersand-separated also not "Last, First"
        assert is_last_first("Woodward, Bob & Bernstein, Carl") is False

    def test_flip_last_first(self):
        assert flip_author_name("Applebaum, Anne") == "Anne Applebaum"

    def test_flip_first_last(self):
        assert flip_author_name("Candice Millard") == "Millard, Candice"
        assert flip_author_name("E. B. White") == "White, E. B."

    def test_single_word_unchanged(self):
        assert flip_author_name("Plato") == "Plato"

    def test_normalize_last_first(self):
        assert normalize_author_format("Candice Millard", "last_first") == "Millard, Candice"
        assert normalize_author_format("Applebaum, Anne", "last_first") == "Applebaum, Anne"

    def test_normalize_first_last(self):
        assert normalize_author_format("Applebaum, Anne", "first_last") == "Anne Applebaum"
        assert normalize_author_format("Candice Millard", "first_last") == "Candice Millard"

    def test_normalize_unknown_author_unchanged(self):
        assert normalize_author_format("Unknown Author", "last_first") == "Unknown Author"

    def test_normalize_empty(self):
        assert normalize_author_format("", "last_first") == ""

    def test_normalize_multi_author_last_first(self):
        result = normalize_author_format("Bob Woodward, Carl Bernstein", "last_first")
        assert result == "Woodward, Bob & Bernstein, Carl"

    def test_normalize_multi_author_first_last(self):
        # Already in first_last → unchanged (comma-separated)
        result = normalize_author_format("Bob Woodward, Carl Bernstein", "first_last")
        assert result == "Bob Woodward, Carl Bernstein"

    def test_normalize_multi_author_ampersand_to_first_last(self):
        result = normalize_author_format("Woodward, Bob & Bernstein, Carl", "first_last")
        assert result == "Bob Woodward, Carl Bernstein"

    def test_normalize_multi_author_ampersand_unchanged_last_first(self):
        result = normalize_author_format("Woodward, Bob & Bernstein, Carl", "last_first")
        assert result == "Woodward, Bob & Bernstein, Carl"

    def test_normalize_three_authors_last_first(self):
        result = normalize_author_format("Alice Smith, Bob Jones, Carol White", "last_first")
        assert result == "Smith, Alice & Jones, Bob & White, Carol"


# ── dest_relative with author_format ─────────────────────────────────────


class TestDestRelativeAuthorFormat:
    def test_last_first_format(self):
        meta = AudiobookMeta(author="Candice Millard", title="River of the Gods")
        result = meta.dest_relative(author_format="last_first")
        assert result == Path("Millard, Candice/River of the Gods")

    def test_first_last_format(self):
        meta = AudiobookMeta(author="Applebaum, Anne", title="Red Famine")
        result = meta.dest_relative(author_format="first_last")
        assert result == Path("Anne Applebaum/Red Famine")

    def test_no_format_uses_raw(self):
        meta = AudiobookMeta(author="Candice Millard", title="River of the Gods")
        result = meta.dest_relative()
        assert result == Path("Candice Millard/River of the Gods")

    def test_multi_author_last_first(self):
        meta = AudiobookMeta(
            author="Bob Woodward, Carl Bernstein",
            title="All the President's Men",
        )
        result = meta.dest_relative(author_format="last_first")
        assert result == Path("Woodward, Bob & Bernstein, Carl/All the President's Men")
