# aborg

Scan, organize, and manage audiobook file collections. Produces an [Audiobookshelf](https://www.audiobookshelf.org/)-compatible directory structure.

## Features

- **Scan** — Find audiobook files in multiple source directories. Supports zip archives, `.m4b`, `.mp3`, loose audio folders, nested download wrappers, and flat multi-album dumps. Applies accent-aware author deduplication and near-duplicate title warnings.
- **Organize** — Move or copy files into a clean `Author / [Series] / Title` hierarchy.
- **Fetch** — Download audiobook loans from [Libby/OverDrive](https://www.overdrive.com/apps/libby). Optionally organize after download.
- **Analyze** — Check an existing collection for issues: duplicates, missing metadata, inconsistent naming, missing cover art, and flat files.
- **Parse** — Test how a filename parses before you run a scan.
- **Rename** — Rename existing folders to match Audiobookshelf naming conventions.
- **Undo** — Revert the last organize operation. Supports moves, copies, and zip extractions.
- **Dry-run** — All destructive commands support `--dry-run`.
- **Auto-extract** — Extract zip archives at the destination. Zip-slip protection is built in. Non-zip archives (`.rar`, `.7z`) move as-is.
- **Metadata** — Read ID3/audio tags with Mutagen and merge with filename parsing.
- **Cache** — Speed up repeated scans with fingerprint-based caching (`--cache`).
- **Configurable** — YAML config file for source directories, destination, patterns, and more.

## Output structure

```
/mnt/audiobooks/
├── Goodkind, Terry/
│   └── Sword of Truth/
│       ├── Vol 1 - 1994 - Wizards First Rule {Sam Tsoutsouvas}/
│       │   ├── Track01.mp3
│       │   └── cover.jpg
│       └── Vol 2 - 1995 - Stone of Tears/
│           └── audiobook.m4b
├── Levy, Steven/
│   └── Hackers - Heroes of the Computer Revolution {Mike Chamberlain}/
│       └── audiobook.m4a
└── Orwell, George/
    └── 1945 - Animal Farm/
        └── audiobook.mp3
```

This follows the [Audiobookshelf directory conventions](https://www.audiobookshelf.org/docs/#book-directory-structure).

## Requirements

Python 3.10 or later.

## Install

```bash
cd aborg
uv pip install -e .
```

Or install with Libby support:

```bash
uv pip install -e ".[libby]"
```

## Quick start

```bash
# Show what is in your Downloads folder
aborg scan

# Show results in a table
aborg scan --table

# Preview what org would do
aborg org --dry-run

# Organize files
aborg org

# Copy instead of move
aborg org --copy

# Organize from a specific directory to a specific destination
aborg org -d /path/to/downloads --dest /mnt/nas/audiobooks

# Analyze your existing collection
aborg analyze --path /mnt/nas/audiobooks

# Apply automatic fixes
aborg analyze --path /mnt/nas/audiobooks --fix

# Test how a filename parses
aborg parse "Brandon Sanderson - Mistborn Book 1 - The Final Empire (2006) [Michael Kramer]"

# Rename existing folders to match conventions
aborg rename --path /mnt/nas/audiobooks --dry-run

# Undo the last organize operation
aborg undo

# Link your Libby account
aborg fetch --setup 12345678

# List current Libby loans
aborg fetch --list

# Download and organize the latest loan
aborg fetch --latest 1 --organize
```

## Configuration

Run the interactive setup wizard to create a config file:

```bash
aborg config
```

This writes `~/.aborg/config.yaml`. See [`config.example.yaml`](config.example.yaml) for all options.

When a config already exists, `aborg config` shows the current settings. Use `aborg config --show` to print the config explicitly.

Key settings:

| Key | Default | Description |
|-----|---------|-------------|
| `source_dirs` | *(none)* | Directories to scan for new audiobooks |
| `destination` | *(none)* | Root of the organized collection |
| `auto_extract` | `true` | Extract zip archives at destination. Non-zip archives move as-is. |
| `delete_after_extract` | `false` | Delete archive after successful extraction |
| `min_file_size` | `1 MB` | Skip files smaller than this value |
| `filename_patterns` | 7 built-in | Regex patterns for parsing filenames. Patterns are tried in order. |
| `author_name_format` | `last_first` | Author folder format: `last_first` (Austen, Jane) or `first_last` (Jane Austen) |
| `known_authors` | `{}` | Case-insensitive aliases for expanding single-name authors. Example: `Proust: Marcel Proust` |
| `archive_extensions` | `.zip .rar .7z` | File extensions treated as archives |
| `audio_extensions` | `.m4b .mp3 .m4a .ogg .opus .flac .wma .aac` | File extensions treated as audio |
| `companion_extensions` | `.jpg .jpeg .png .pdf .epub .nfo .cue .txt .opf` | Companion files moved with audio files |
| `move_log` | `~/.aborg/moves.log` | Log file used by `undo` |

### Libby/OverDrive settings

These settings are under the `libby:` key in the config file. They control `aborg fetch` behavior.

| Key | Default | Description |
|-----|---------|-------------|
| `libby.settings_folder` | `~/.aborg/libby` | Where Libby authentication tokens are stored |
| `libby.merge` | `false` | Merge downloaded MP3 parts into a single file |
| `libby.merge_format` | `m4b` | Merged file format. Use `mp3` or `m4b`. The `m4b` format requires ffmpeg. |
| `libby.chapters` | `true` | Embed chapter markers in downloaded files |
| `libby.keep_cover` | `true` | Download cover art (`cover.jpg`) |
| `libby.book_folder_format` | `%(Author)s - %(Title)s` | odmpy folder name template |

## Filename parsing

The tool tries multiple regex patterns against filenames. You can configure patterns in the config file. Built-in patterns handle these formats:

| Pattern | Example |
|---------|---------|
| `N - Title - Author - Year` | `2 - Dune - Frank Herbert - 1965` |
| `Author - Series Book N - Title (Year) [Narrator]` | `Brandon Sanderson - Mistborn Book 1 - The Final Empire (2006) [Michael Kramer]` |
| `Author - Title - Series, Book N` | `Arkady Martine - A Desolation Called Peace - Teixcalaan, Book 2` |
| `Author - Title (Year) [Narrator]` | `Frank Herbert - Dune (1965) [Scott Brick]` |
| `Series Name N Title` | `The Expanse 02.5 Gods of Risk` |
| `Author_Title` | `Frank Herbert_Dune` |

The two-part form `X - Y` is ambiguous. With default ordering, the tool interprets it as `Author - Title`. For collections that use `Title - Author`, reorder or replace `filename_patterns` in the config.

Single-word names do not expand to arbitrary authors by default. The tool normalizes established mononyms such as `Molière` automatically. For other single-name expansion, use `known_authors` in the config.

Metadata sources (highest priority first):

1. **Sidecar JSON** — `metadata/metadata.json` inside an audiobook directory or zip archive. Creator roles: `aut` (author), `nrt` (narrator), `trl` (translator).
2. **Audio tags** — ID3/Mutagen tags (artist, album, composer, series, narrator). The tool cleans copyright notices, placeholders, malformed Windows-1252 punctuation, HTML entities, and noise qualifiers such as "(audio)". When a tag date conflicts with a year that appears explicitly at the end of the title, the title year wins.
3. **Filename** — Parsed against the configured regex patterns.

## Commands

| Command | Description |
|---------|-------------|
| `scan` | List discovered audiobooks in source dirs |
| `org` | Organize (move/copy) audiobooks to destination |
| `fetch` | Download audiobook loans from Libby/OverDrive |
| `analyze` | Audit existing collection, list issues |
| `parse` | Test filename parsing |
| `rename` | Batch-rename folders to conventions |
| `undo` | Revert last organize batch |
| `config` | Show or initialize configuration |
| `about` | Show version, build, and project info |
| `tldr` | Show common commands and quick-start examples |

All destructive commands support `--dry-run`. Use `-c / --config` before any command to load a custom config file.

---

### `scan`

Scan source directories and display discovered audiobooks.

```
aborg scan [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-d, --dir PATH` | Additional directory to scan (repeatable) |
| `--table` | Show results in a table instead of streaming output |
| `--cache` | Use fingerprint-based cache from previous scans |

---

### `org`

Scan source directories and move (or copy) each audiobook into the destination hierarchy.

```
aborg org [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `-d, --dir PATH` | Additional directory to scan (repeatable) |
| `--dest PATH` | Override configured destination directory |
| `--dry-run` | Preview what would happen without making changes |
| `--copy` | Copy files instead of moving them |
| `-y, --yes` | Skip the confirmation prompt |
| `--cache` | Use fingerprint-based cache from previous scans |

After organizing, the tool offers to clean up empty source directories left behind, or the copied originals when you use `--copy`.

---

### `fetch`

Download audiobook loans from your Libby/OverDrive library account. Requires [odmpy](https://github.com/ping/odmpy).

```
aborg fetch [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--setup CODE` | Link your Libby account using an 8-digit setup code |
| `--list` | List current audiobook loans and exit |
| `--latest N` | Download the latest *N* loans non-interactively |
| `--select ID` | Download a specific loan by ID (repeatable) |
| `--all` | Download all current audiobook loans |
| `-d, --download-dir PATH` | Override download directory (defaults to first `source_dir`) |
| `--organize` | Automatically run `aborg org` after downloading |
| `--merge` | Merge MP3 parts into one file (overrides config) |
| `--dry-run` | Show what would be downloaded without downloading |

**Step-by-step workflow:**

```bash
# Step 1: Link your account (one time only)
aborg fetch --setup 12345678

# Step 2: See available loans
aborg fetch --list

# Step 3: Download by loan ID or by recency
aborg fetch --select abc123
aborg fetch --latest 3 --organize
aborg fetch --all
```

Get a Libby setup code at <https://help.libbyapp.com/en-us/6070.htm>.

---

### `analyze`

Check an existing organized collection and report issues such as duplicates, missing metadata, inconsistent author name formatting, empty directories, missing cover art, and flat files.

```
aborg analyze [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--path PATH` | Collection root to analyze (defaults to configured destination) |
| `--fix` | Apply automatic fixes for detected issues |
| `--dry-run` | Show what `--fix` would do without making changes |
| `-y, --yes` | Skip the confirmation prompt when using `--fix` |
| `--cache` | Use fingerprint-based cache from previous scans |
| `--check-tags / --no-check-tags` | Read audio tags to check metadata quality (disable with `--no-check-tags` for speed) |

---

### `parse`

Parse a filename or file path and show the extracted metadata. Use this command to test your `filename_patterns` before running a scan.

```
aborg parse FILENAME
```

When you supply an actual audio file path, `parse` also reads the ID3 tags and shows the merged result. This is the same logic that `aborg scan` uses.

---

### `rename`

Rename folders in an existing collection so that their names match the configured Audiobookshelf conventions.

```
aborg rename [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--path PATH` | Collection root (defaults to configured destination) |
| `--dry-run` | Show what would be renamed without making changes |
| `-y, --yes` | Skip confirmation prompt |
| `--cache` | Use fingerprint-based cache from previous scans |

---

### `undo`

Revert the most recent `org` operation. Moves restore to their source path. Copies are removed from the destination. Extracted zip directories are removed. If `delete_after_extract` deleted the original zip, `undo` rebuilds it from the extracted files before removing the destination.

```
aborg undo [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be undone without making changes |

---

### `config`

Show the current configuration or start the interactive setup wizard.

```
aborg config [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--show` | Print the current configuration and exit |

When no config file exists, `aborg config` starts an interactive wizard. The wizard prompts for source directories, destination, and key settings, then writes `~/.aborg/config.yaml`.

---

### `about`

Show version, build, and project information.

```
aborg about
```

Displays: installed version, last git commit (when running from source), Python version, install path, config path, repository URL, website, and license.

---

### `tldr`

Show common commands and quick-start examples grouped by task.

```
aborg tldr
```

## Development

```bash
# Install dev dependencies
uv sync

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=audiobook_organizer --cov-report=term-missing

# Lint and format
uv run ruff check src tests
uv run ruff format src tests
```

### Pre-commit hooks

The repo uses [pre-commit](https://pre-commit.com/) to run Ruff lint and format checks before each commit.

```bash
uv run pre-commit install
```

## License

MIT
