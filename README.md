# aborg

A CLI tool to scan, organize, and manage audiobook file collections. Outputs an [Audiobookshelf](https://www.audiobookshelf.org/)-compatible directory structure.

## Features

- **Scan** — discover audiobook files (zip archives, `.m4b`, `.mp3`, loose audio folders) across multiple source directories
- **Organize** — move or copy files into a clean `Author / [Series] / Title` hierarchy
- **Fetch** — download audiobook loans from [Libby/OverDrive](https://www.overdrive.com/apps/libby) and optionally auto-organize them
- **Analyze** — audit an existing collection for issues: duplicates, missing metadata, inconsistent naming, missing cover art, flat files
- **Parse** — test how a filename will be parsed before running
- **Rename** — batch-rename existing folders to match Audiobookshelf naming conventions
- **Undo** — revert the last organize operation via a move log
- **Dry-run** — every destructive command supports `--dry-run`
- **Auto-extract** — unzip archives at the destination (with zip-slip protection)
- **Metadata** — reads ID3/audio tags via Mutagen and merges with filename parsing
- **Cache** — speed up repeated scans with fingerprint-based caching (`--cache`)
- **Configurable** — YAML config for source dirs, destination, patterns, and more

## Directory structure produced

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

## Install

```bash
cd aborg
pip install -e .
```

## Quick start

```bash
# Show what's in your Downloads folder
aborg scan

# Show results in a table view
aborg scan --table

# Preview what would happen
aborg org --dry-run

# Organize for real
aborg org

# Copy instead of move
aborg org --copy

# Organize from a specific dir to a specific dest
aborg org -d /path/to/downloads --dest /mnt/nas/audiobooks

# Analyze your existing collection
aborg analyze --path /mnt/nas/audiobooks

# Analyze and apply automatic fixes
aborg analyze --path /mnt/nas/audiobooks --fix

# Test how a filename parses
aborg parse "Brandon Sanderson - Mistborn Book 1 - The Final Empire (2006) [Michael Kramer]"

# Rename existing folders to match conventions
aborg rename --path /mnt/nas/audiobooks --dry-run

# Undo last organize
aborg undo

# Link your Libby account (get code at https://help.libbyapp.com/en-us/6070.htm)
aborg fetch --setup 12345678

# List current Libby loans
aborg fetch --list

# Download and auto-organize the latest loan
aborg fetch --latest 1 --organize
```

## Configuration

Run the interactive setup wizard to create a config file:

```bash
aborg config
```

This writes `~/.aborg/config.yaml`. See [`config.example.yaml`](config.example.yaml) for all options.

If a config already exists, `aborg config` displays the current settings. Use `aborg config --show` to print the config explicitly.

Key settings:

| Key | Default | Description |
|-----|---------|-------------|
| `source_dirs` | `~/Downloads` | Directories to scan for new audiobooks |
| `destination` | `~/audiobooks` | Root of the organized collection |
| `auto_extract` | `true` | Extract zip/rar/7z archives at destination |
| `delete_after_extract` | `false` | Remove archive after successful extraction |
| `min_file_size` | `1 MB` | Ignore files smaller than this |
| `filename_patterns` | 4 built-in | Regex patterns for parsing filenames (tried in order) |
| `author_name_format` | `last_first` | Author folder format: `last_first` ("Austen, Jane") or `first_last` ("Jane Austen") |
| `archive_extensions` | `.zip .rar .7z` | File extensions treated as archives |
| `audio_extensions` | `.m4b .mp3 .m4a .ogg .opus .flac .wma .aac` | File extensions treated as audio |
| `companion_extensions` | `.jpg .jpeg .png .pdf .epub .nfo .cue .txt .opf` | Companion files moved alongside audio |
| `move_log` | `~/.aborg/moves.log` | Log file used by `undo` |

### Libby / OverDrive settings

These settings live under the `libby:` key in the config file and control `aborg fetch` behavior.

| Key | Default | Description |
|-----|---------|-------------|
| `libby.settings_folder` | `~/.aborg/libby` | Where Libby authentication tokens are stored |
| `libby.merge` | `false` | Merge downloaded MP3 parts into a single file |
| `libby.merge_format` | `m4b` | Merged file format (`mp3` or `m4b`; `m4b` requires ffmpeg) |
| `libby.chapters` | `true` | Embed chapter markers in downloaded files |
| `libby.keep_cover` | `true` | Download cover art (`cover.jpg`) |
| `libby.book_folder_format` | `%(Author)s - %(Title)s` | odmpy folder name template |

## Filename parsing

The tool tries multiple regex patterns against filenames (configurable). Built-in patterns handle:

| Pattern | Example |
|---------|---------|
| `Author - Series Book N - Title (Year) [Narrator]` | `Brandon Sanderson - Mistborn Book 1 - The Final Empire (2006) [Michael Kramer]` |
| `Author - Title (Year) [Narrator]` | `Frank Herbert - Dune (1965) [Scott Brick]` |
| `Title - Author (Year)` | `Dune - Frank Herbert (1965)` |
| `Author_Title` | `Frank Herbert_Dune` |

Metadata is collected from three sources (highest priority first):

1. **Audio tags** — ID3/Mutagen tags (artist, album, composer, series, narrator, etc.)
2. **Filename** — parsed against the configured regex patterns
3. **Parent directory** — used as a fallback author name

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

After organizing, aborg offers to clean up any empty source directories left behind (or the copied originals when using `--copy`).

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

**Typical workflow:**

```bash
# 1. Link your account (one-time)
aborg fetch --setup 12345678

# 2. See what's available
aborg fetch --list

# 3. Download by loan ID or by recency
aborg fetch --select abc123
aborg fetch --latest 3 --organize
aborg fetch --all
```

Get a Libby setup code at <https://help.libbyapp.com/en-us/6070.htm>.

---

### `analyze`

Scan an existing organized collection and report issues such as duplicates, missing metadata, inconsistent author name formatting, empty directories, missing cover art, and flat files.

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

Parse a filename (or file path) and display the metadata that would be extracted. Useful for testing your `filename_patterns` before organizing.

```
aborg parse FILENAME
```

When given an actual audio file path, `parse` also reads the file's ID3 tags and shows the merged result — the same logic used by `aborg scan`.

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
| `--cache` | Use fingerprint-based cache from previous scans |

---

### `undo`

Revert the most recent `org` operation by reading the move log and moving files back to their original locations.

```
aborg undo [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be undone without making changes |

---

### `config`

Show current configuration or launch the interactive setup wizard.

```
aborg config [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--show` | Print the current configuration and exit |

When no config file exists, `aborg config` automatically starts an interactive wizard that prompts for source directories, destination, and other key settings, then writes `~/.aborg/config.yaml`.

---

### `about`

Show version, build, and project information.

```
aborg about
```

Displays the installed version, last git commit (when running from source), Python version, install path, config path, repository URL, website, and license.

---

### `tldr`

Show common commands and quick-start examples grouped by task.

```
aborg tldr
```

## License

MIT
