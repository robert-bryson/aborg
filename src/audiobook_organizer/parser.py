"""Parse audiobook metadata from filenames and audio file tags."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen import MutagenError

# Words that suggest an "artist" tag is a category/genre, not a person.
_NON_AUTHOR_WORDS = frozenset({
    "top", "best", "greatest", "100", "sci-fi", "scifi", "fantasy", "fiction",
    "books", "audiobooks", "collection", "series", "classics", "library",
    "various", "artists", "unknown", "anthology", "assorted", "compilation",
    "audio", "unabridged", "abridged",
})

# Audio extensions used for path normalisation.
_AUDIO_EXTS = frozenset({
    ".m4b", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wma", ".aac",
})


@dataclass
class AudiobookMeta:
    """Parsed metadata for a single audiobook."""

    author: str = "Unknown Author"
    title: str = "Unknown Title"
    series: str | None = None
    sequence: str | None = None
    year: str | None = None
    narrator: str | None = None
    source_path: Path | None = None

    def dest_folder_name(self) -> str:
        """Build the title-folder name in Audiobookshelf convention.

        Format: ``[Vol N - ][YYYY - ]Title[ {Narrator}]``
        """
        parts: list[str] = []
        if self.sequence and self.series:
            parts.append(f"Vol {self.sequence}")
        if self.year:
            parts.append(self.year)
        parts.append(self.title)
        folder = " - ".join(parts)
        if self.narrator:
            folder += f" {{{self.narrator}}}"
        return _sanitize(folder)

    def dest_relative(self) -> Path:
        """Return the relative destination path: ``Author[/Series]/TitleFolder``."""
        author_dir = _sanitize(self.author)
        title_dir = self.dest_folder_name()
        if self.series:
            series_dir = _sanitize(self.series)
            return Path(author_dir) / series_dir / title_dir
        return Path(author_dir) / title_dir


def _sanitize(name: str) -> str:
    """Remove or replace filesystem-unsafe characters."""
    # Replace characters illegal on Windows/Linux
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    # Collapse multiple spaces/dots, strip trailing dots/spaces
    name = re.sub(r"\s{2,}", " ", name).strip(". ")
    return name or "Unknown"


def looks_like_author(name: str) -> bool:
    """Heuristic: return *True* if *name* looks like a person, not a category."""
    if not name or name == "Unknown Author":
        return False
    words = name.lower().split()
    if not words:
        return False
    bad = sum(1 for w in words if w in _NON_AUTHOR_WORDS)
    if bad >= 2:
        return False
    if len(words) <= 3 and bad >= 1:
        return False
    # Leading digit → probably track/disc numbering, not a name
    if words[0].isdigit():
        return False
    return True


def normalize_path_name(input_str: str) -> str:
    """Extract the last meaningful name from a path string.

    Handles both Windows (backslash / UNC) and Unix paths.
    Strips recognised audio file extensions.
    """
    # Normalise to forward slashes
    normalized = input_str.replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized
    # Strip audio extension
    dot = name.rfind(".")
    if dot > 0 and name[dot:].lower() in _AUDIO_EXTS:
        name = name[:dot]
    return name.strip()


def path_parent_name(input_str: str) -> str | None:
    """Return the parent directory name from a path, or *None*."""
    normalized = input_str.replace("\\", "/").rstrip("/")
    parts = [p for p in normalized.split("/") if p]
    if len(parts) >= 2:
        return parts[-2]
    return None


def parse_filename(name: str, patterns: list[str] | None = None) -> AudiobookMeta:
    """Attempt to parse audiobook metadata from a filename (without extension).

    Tries each regex pattern in order; first match wins.
    """
    if not patterns:
        return AudiobookMeta(title=name.strip() or "Unknown Title")
    for pattern in patterns:
        m = re.match(pattern, name, re.IGNORECASE)
        if m:
            g = m.groupdict()
            return AudiobookMeta(
                author=g.get("author", "").strip() or "Unknown Author",
                title=g.get("title", "").strip() or "Unknown Title",
                series=(g.get("series") or "").strip() or None,
                sequence=(g.get("sequence") or "").strip() or None,
                year=(g.get("year") or "").strip() or None,
                narrator=(g.get("narrator") or "").strip() or None,
            )
    # Fallback: use the whole name as the title
    return AudiobookMeta(title=name.strip() or "Unknown Title")


# Regex matching common separators in folder names: " - ", " – ", " — "
_SEP_RE = re.compile(r"\s+[-\u2013\u2014]\s+")


def _author_variants(author: str) -> list[str]:
    """Return lowercase name variants (First Last / Last, First) for matching."""
    low = author.strip().lower()
    variants = [low]
    if "," in low:
        parts = low.split(",", 1)
        variants.append(f"{parts[1].strip()} {parts[0].strip()}")
    else:
        parts = low.rsplit(None, 1)
        if len(parts) == 2:
            variants.append(f"{parts[1]}, {parts[0]}")
    return variants


def _strip_author_from_name(name: str, known_author: str) -> str | None:
    """Remove the known author from *name* by matching dash-separated segments.

    Uses fuzzy matching (>0.8 similarity) to handle typos and name-format
    differences.  Returns the cleaned remainder, or *None* if no match.
    """
    from difflib import SequenceMatcher

    segments = _SEP_RE.split(name)
    if len(segments) < 2:
        return None

    variants = _author_variants(known_author)
    for i, segment in enumerate(segments):
        seg_low = segment.strip().lower()
        for variant in variants:
            if SequenceMatcher(None, seg_low, variant).ratio() > 0.8:
                remaining = [s for j, s in enumerate(segments) if j != i]
                result = " - ".join(remaining).strip()
                return result if result else None
    return None


def _parse_title_remainder(text: str) -> AudiobookMeta:
    """Parse a title/year string remaining after the author has been stripped."""
    meta = AudiobookMeta()
    text = text.strip()
    if not text:
        return meta

    # "Title - Year" or "Title (Year)"
    m = re.match(r"^(.+?)\s*[-\u2013\u2014]\s*(\d{4})$", text)
    if not m:
        m = re.match(r"^(.+?)\s*\((\d{4})\)\s*$", text)
    if m and m.group(1).strip():
        meta.title = m.group(1).strip()
        meta.year = m.group(2)
        return meta

    # "Year - Title"
    m = re.match(r"^(\d{4})\s*[-\u2013\u2014]\s*(.+)$", text)
    if m:
        meta.year = m.group(1)
        meta.title = m.group(2).strip()
        return meta

    # Bare year — record it but leave title unknown
    if re.match(r"^\d{4}$", text):
        meta.year = text
        return meta

    meta.title = text
    return meta


def parse_title_folder(
    name: str, known_author: str, patterns: list[str] | None = None,
) -> AudiobookMeta:
    """Parse a title folder name when the author is already known.

    Tries to strip the known author from the folder name before extracting
    title, year, and other metadata.  Falls back to ``parse_filename`` when
    the author isn't found in the name.
    """
    # Step 1: try stripping the known author from the folder name.
    stripped = _strip_author_from_name(name, known_author)
    if stripped is not None:
        meta = _parse_title_remainder(stripped)
        meta.author = known_author
        return meta

    # Step 2: try interpreting as a direct title/year (no author expected).
    remainder = _parse_title_remainder(name)
    if remainder.title != "Unknown Title" or remainder.year:
        remainder.author = known_author
        return remainder

    # Step 3: fall back to standard pattern-based parsing.
    meta = parse_filename(name, patterns)
    meta.author = known_author
    return meta


def strip_author_from_title(title: str, known_author: str) -> str:
    """Remove the known author's name from a title string."""
    cleaned = _strip_author_from_name(title, known_author)
    return cleaned if cleaned else title


def parse_audio_tags(path: Path) -> AudiobookMeta:
    """Read ID3/audio metadata from an audio file using Mutagen.

    Validates the artist tag to avoid using genre/category labels
    (e.g. "Top 100 Sci-Fi Books") as the author.
    """
    meta = AudiobookMeta(source_path=path)
    try:
        audio = MutagenFile(path, easy=True)
    except MutagenError:
        return meta
    if audio is None:
        return meta

    tags = audio.tags or {}

    def _get(*keys: str) -> str | None:
        for k in keys:
            vals = tags.get(k)
            if vals:
                return str(vals[0]).strip()
        return None

    # Author — try each candidate, keep the first that looks like a person.
    for candidate in (_get("albumartist", "album_artist"), _get("artist")):
        if candidate and looks_like_author(candidate):
            meta.author = candidate
            break

    raw_title = _get("album", "title") or meta.title
    meta.title = _clean_tag_title(raw_title)

    meta.year = _get("date", "year") or meta.year
    meta.narrator = _get("composer") or meta.narrator
    meta.series = _get("series", "mvnm", "grouping") or meta.series
    meta.sequence = _get("series-part", "mvin") or meta.sequence

    return meta


def _clean_tag_title(title: str) -> str:
    """Strip common album-tag noise such as leading disc/track numbers."""
    if not title or title == "Unknown Title":
        return title
    # "03 - Book 1 - ..." → "Book 1 - ..."
    cleaned = re.sub(r"^\d+\s*[-.:]+\s*", "", title)
    return cleaned.strip() or title


def merge_meta(*sources: AudiobookMeta) -> AudiobookMeta:
    """Merge multiple metadata sources, preferring earlier non-default values."""
    result = AudiobookMeta()
    for src in sources:
        if result.author == "Unknown Author" and src.author != "Unknown Author":
            result.author = src.author
        if result.title == "Unknown Title" and src.title != "Unknown Title":
            result.title = src.title
        if result.series is None and src.series:
            result.series = src.series
        if result.sequence is None and src.sequence:
            result.sequence = src.sequence
        if result.year is None and src.year:
            result.year = src.year
        if result.narrator is None and src.narrator:
            result.narrator = src.narrator
        if result.source_path is None and src.source_path:
            result.source_path = src.source_path
    return result
