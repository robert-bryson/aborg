"""Parse audiobook metadata from filenames and audio file tags."""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from difflib import SequenceMatcher

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
        title = self.title
        if self.year:
            parts.append(self.year)
            # Avoid duplicating the year when it's already embedded in the title
            title = _strip_embedded_year(title, self.year)
        parts.append(title)
        folder = " - ".join(parts)
        if self.narrator:
            folder += f" {{{self.narrator}}}"
        return _sanitize(folder)

    def dest_relative(self, *, author_format: str = "") -> Path:
        """Return the relative destination path: ``Author[/Series]/TitleFolder``."""
        author = self.author
        if author_format:
            author = normalize_author_format(author, author_format)
        author_dir = _sanitize(author)
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


def _strip_embedded_year(title: str, year: str) -> str:
    """Remove *year* from *title* when it appears as a leading/trailing component."""
    # Trailing " - YYYY" (with negative lookbehind to preserve year ranges like 1944-1956)
    m = re.search(rf"(?<!\d)\s*[-\u2013\u2014]\s*{re.escape(year)}\s*$", title)
    if m:
        return title[: m.start()].strip() or title
    # Trailing " (YYYY)"
    m = re.search(rf"\s*\({re.escape(year)}\)\s*$", title)
    if m:
        return title[: m.start()].strip() or title
    # Leading "YYYY - "
    m = re.match(rf"^{re.escape(year)}\s*[-\u2013\u2014]\s+", title)
    if m:
        return title[m.end() :].strip() or title
    # Leading "(YYYY) - "
    m = re.match(rf"^\({re.escape(year)}\)\s*[-\u2013\u2014]\s*", title)
    if m:
        return title[m.end() :].strip() or title
    return title


def is_last_first(name: str) -> bool:
    """Return True if *name* looks like 'Last, First' format."""
    return bool(re.match(r'^[^,]+,\s*.+', name))


def flip_author_name(name: str) -> str:
    """Convert 'Last, First' to 'First Last' or vice versa."""
    if is_last_first(name):
        last, first = name.split(',', 1)
        return f"{first.strip()} {last.strip()}"
    parts = name.rsplit(None, 1)
    if len(parts) == 2:
        return f"{parts[1]}, {parts[0]}"
    return name


def normalize_author_format(name: str, fmt: str) -> str:
    """Normalize author name to the given format ('last_first' or 'first_last')."""
    if not name or name == "Unknown Author":
        return name
    if fmt == "last_first" and not is_last_first(name):
        return flip_author_name(name)
    if fmt == "first_last" and is_last_first(name):
        return flip_author_name(name)
    return name


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
        title = _NOISE_PAREN_RE.sub("", name).strip() or "Unknown Title"
        return AudiobookMeta(title=title)
    for pattern in patterns:
        m = re.match(pattern, name, re.IGNORECASE)
        if m:
            g = m.groupdict()
            raw_title = g.get("title", "").strip() or "Unknown Title"
            return AudiobookMeta(
                author=g.get("author", "").strip() or "Unknown Author",
                title=_NOISE_PAREN_RE.sub("", raw_title).strip() or raw_title,
                series=(g.get("series") or "").strip() or None,
                sequence=(g.get("sequence") or "").strip() or None,
                year=(g.get("year") or "").strip() or None,
                narrator=(g.get("narrator") or "").strip() or None,
            )
    # Fallback: use the whole name as the title
    title = _NOISE_PAREN_RE.sub("", name).strip() or "Unknown Title"
    return AudiobookMeta(title=title)


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


def _extract_narrator(text: str) -> tuple[str | None, str]:
    """Extract narrator from trailing ``{braces}`` or ``[brackets]``.

    Returns ``(narrator, cleaned_text)``.
    """
    m = re.search(r"\{(.+?)\}\s*$", text)
    if not m:
        m = re.search(r"\[([^\]]+)\]\s*$", text)
    if m:
        narrator = m.group(1).strip()
        cleaned = text[: m.start()].strip(" -\u2013\u2014")
        return narrator, cleaned
    return None, text


def _parse_title_remainder(text: str) -> AudiobookMeta:
    """Parse a title/year/narrator/sequence string after the author is stripped.

    Handles Audiobookshelf-style title folder naming conventions:
      - Narrator in ``{curly braces}`` or ``[square brackets]``
      - Volume/Book sequence: ``Vol 1 - …``, ``Book 2 - …``, ``1 - …``, ``1. …``
      - Year: ``YYYY - …``, ``(YYYY) - …``, ``… - YYYY``, ``… (YYYY)``
      - Combinations: ``Vol 1 - 1994 - Wizards First Rule {Sam Tsoutsouvas}``
    """
    meta = AudiobookMeta()
    text = text.strip()
    if not text:
        return meta

    # ── Step 1: extract narrator from {curly braces} or [square brackets] ──
    narrator, text = _extract_narrator(text)
    if narrator:
        meta.narrator = narrator

    if not text:
        return meta

    # ── Step 2: extract sequence (Vol/Book/bare number at start) ──
    # "Vol 1 - …", "Vol. 2 - …", "Volume 3 - …", "Book 1 - …"
    m_seq = re.match(
        r"^(?:Vol\.?|Volume|Book)\s*(\d+(?:\.\d+)?)\s*[-.\u2013\u2014]\s*",
        text,
        re.IGNORECASE,
    )
    if m_seq:
        meta.sequence = m_seq.group(1)
        text = text[m_seq.end() :].strip()
    else:
        # Bare leading number: "1 - …" or "1. …" (but not a 4-digit year)
        m_bare = re.match(
            r"^(\d{1,3}(?:\.\d+)?)\s*[-.\u2013\u2014]\s+", text
        )
        if m_bare:
            meta.sequence = m_bare.group(1)
            text = text[m_bare.end() :].strip()

    if not text:
        return meta

    # ── Step 3: extract year ──
    # Leading "(YYYY) - …"
    m_paren_year = re.match(r"^\((\d{4})\)\s*[-\u2013\u2014]\s*", text)
    if m_paren_year:
        meta.year = m_paren_year.group(1)
        text = text[m_paren_year.end() :].strip()
    else:
        # Leading "YYYY - …"
        m_lead_year = re.match(r"^(\d{4})\s*[-\u2013\u2014]\s+", text)
        if m_lead_year:
            meta.year = m_lead_year.group(1)
            text = text[m_lead_year.end() :].strip()
        else:
            # Trailing "… - YYYY" or "… (YYYY)"
            # Negative lookbehind for a digit prevents splitting year
            # ranges like "1944-1956" into title + year.
            m_trail = re.search(r"(?<!\d)\s*[-\u2013\u2014]\s*(\d{4})$", text)
            if not m_trail:
                m_trail = re.search(r"\s*\((\d{4})\)\s*$", text)
            if m_trail:
                meta.year = m_trail.group(1)
                text = text[: m_trail.start()].strip()

    # ── Step 3b: if year was extracted and no sequence yet, try sequence again ──
    # Handles "1994 - Book 1 - Title" where year consumed first.
    if meta.year and not meta.sequence and text:
        m_seq2 = re.match(
            r"^(?:Vol\.?|Volume|Book)\s*(\d+(?:\.\d+)?)\s*[-.\u2013\u2014]\s*",
            text,
            re.IGNORECASE,
        )
        if m_seq2:
            meta.sequence = m_seq2.group(1)
            text = text[m_seq2.end() :].strip()

    # ── Step 3c: trailing sequence "… - Volume 1" / "… - Book 2" ──
    if not meta.sequence and text:
        m_trail_seq = re.search(
            r"\s*[-\u2013\u2014]\s*(?:Vol\.?|Volume|Book)\s*(\d+(?:\.\d+)?)\s*$",
            text,
            re.IGNORECASE,
        )
        if m_trail_seq:
            meta.sequence = m_trail_seq.group(1)
            text = text[: m_trail_seq.start()].strip()

    if not text:
        # Only a year (and possibly sequence) — no title.
        if meta.year:
            return meta
        # Nothing left
        return meta

    # Bare 4-digit year with nothing else
    if re.match(r"^\d{4}$", text) and not meta.year:
        meta.year = text
        return meta

    # Strip noise parentheticals from title
    text = _NOISE_PAREN_RE.sub("", text).strip()
    meta.title = text or "Unknown Title"
    return meta


def _strip_by_author(text: str, known_author: str) -> str:
    """Remove a trailing ``by Author Name`` from *text* using fuzzy matching."""
    m = re.search(r"\s+by\s+(.+)$", text, re.IGNORECASE)
    if not m:
        return text
    candidate = m.group(1).strip()
    # Also strip noise parens from the candidate so "by Bob Woodward (Audiobook)" works
    candidate = _NOISE_PAREN_RE.sub("", candidate).strip()
    variants = _author_variants(known_author)
    cand_low = candidate.lower()
    for variant in variants:
        if SequenceMatcher(None, cand_low, variant).ratio() > 0.8:
            return text[: m.start()].strip()
    return text


def parse_title_folder(
    name: str, known_author: str, patterns: list[str] | None = None,
) -> AudiobookMeta:
    """Parse a title folder name when the author is already known.

    Tries to strip the known author from the folder name before extracting
    title, year, and other metadata.  Falls back to ``parse_filename`` when
    the author isn't found in the name.
    """
    # Pre-extract narrator from {braces} or [brackets] so it doesn't
    # interfere with author-segment fuzzy matching.
    narrator, clean_name = _extract_narrator(name)

    # Strip noise parentheticals early so they don't interfere.
    clean_name = _NOISE_PAREN_RE.sub("", clean_name).strip()

    # Strip trailing "by Author Name".
    clean_name = _strip_by_author(clean_name, known_author)

    # Step 1: try stripping the known author from the folder name.
    stripped = _strip_author_from_name(clean_name, known_author)
    if stripped is not None:
        meta = _parse_title_remainder(stripped)
        meta.author = known_author
        if narrator and not meta.narrator:
            meta.narrator = narrator
        return meta

    # Step 2: try interpreting as a direct title/year (no author expected).
    remainder = _parse_title_remainder(clean_name)
    if remainder.title != "Unknown Title" or remainder.year:
        remainder.author = known_author
        if narrator and not remainder.narrator:
            remainder.narrator = narrator
        return remainder

    # Step 3: fall back to standard pattern-based parsing.
    meta = parse_filename(clean_name, patterns)
    meta.author = known_author
    return meta


def strip_author_from_title(title: str, known_author: str) -> str:
    """Remove the known author's name from a title string."""
    cleaned = _strip_author_from_name(title, known_author)
    if cleaned:
        return cleaned
    # Try "by Author" pattern
    stripped = _strip_by_author(title, known_author)
    return stripped if stripped != title else title


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


# Trailing parenthetical noise to strip from titles (awards, format labels, etc.).
_NOISE_PAREN_RE = re.compile(
    r"\s*\((?:"
    r"[^)]*(?:prize|award|winner|bestseller|best seller|medal|finalist"
    r"|nominee|honor|notable)[^)]*"
    r"|audiobook|unabridged|abridged|audio edition|audio cd"
    r")\)",
    re.IGNORECASE,
)


def _clean_tag_title(title: str) -> str:
    """Strip common album-tag noise such as leading disc/track numbers."""
    if not title or title == "Unknown Title":
        return title
    # "03 - Book 1 - ..." → "Book 1 - ..."
    cleaned = re.sub(r"^\d+\s*[-.:]+\s*", "", title)
    # "G-Man (Pulitzer Prize Winner)" → "G-Man"
    cleaned = _NOISE_PAREN_RE.sub("", cleaned)
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


# Conventional path for sidecar metadata JSON.
METADATA_JSON_PATH = Path("metadata") / "metadata.json"
_METADATA_JSON_ZIP_NAME = "metadata/metadata.json"


def _parse_metadata_dict(data: dict, source_path: Path | None = None) -> AudiobookMeta:
    """Build an ``AudiobookMeta`` from a decoded metadata JSON dict."""
    meta = AudiobookMeta(source_path=source_path)

    title = data.get("title")
    if isinstance(title, str) and title.strip():
        meta.title = title.strip()

    creators = data.get("creator")
    if isinstance(creators, list):
        for entry in creators:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            role = entry.get("role", "")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            if role == "aut" and meta.author == "Unknown Author":
                meta.author = name
            elif role == "nrt" and meta.narrator is None:
                meta.narrator = name

    return meta


def parse_metadata_json(dir_path: Path) -> AudiobookMeta | None:
    """Read ``metadata/metadata.json`` inside *dir_path* and return metadata.

    Returns ``None`` if the file doesn't exist or can't be parsed.
    The JSON format uses ``creator`` entries with roles:
    ``"aut"`` for author, ``"nrt"`` for narrator.
    """
    json_path = dir_path / METADATA_JSON_PATH
    if not json_path.is_file():
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _parse_metadata_dict(data, source_path=dir_path)


def parse_metadata_json_from_zip(zip_path: Path) -> AudiobookMeta | None:
    """Read ``metadata/metadata.json`` from inside a zip archive.

    Returns ``None`` if the zip doesn't contain the file or can't be parsed.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # Try both with and without a top-level directory wrapper.
            for name in zf.namelist():
                if name == _METADATA_JSON_ZIP_NAME or name.endswith("/" + _METADATA_JSON_ZIP_NAME):
                    raw = zf.read(name)
                    data = json.loads(raw.decode("utf-8"))
                    if isinstance(data, dict):
                        return _parse_metadata_dict(data, source_path=zip_path)
    except (zipfile.BadZipFile, OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError):
        return None
    return None
