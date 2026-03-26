# aborg

A CLI tool to scan, organize, and manage audiobook file collections. Outputs an [Audiobookshelf](https://www.audiobookshelf.org/)-compatible directory structure.

## Features

- **Scan** — discover audiobook files (zip archives, `.m4b`, `.mp3`, loose audio folders) across multiple source directories
- **Organize** — move or copy files into a clean `Author / [Series] / Title` hierarchy
- **Analyze** — audit an existing collection for issues: duplicates, missing metadata, inconsistent naming, missing cover art, flat files
- **Parse** — test how a filename will be parsed before running
- **Rename** — batch-rename existing folders to match Audiobookshelf naming conventions
- **Undo** — revert the last organize operation via a move log
- **Dry-run** — every destructive command supports `--dry-run`
- **Auto-extract** — unzip archives at the destination (with zip-slip protection)
- **Metadata** — reads ID3/audio tags via Mutagen and merges with filename parsing
- **Configurable** — YAML config for source dirs, destination, patterns, and more

## Directory structure produced

```
N:\media\audiobooks\
├── Terry Goodkind/
│   └── Sword of Truth/
│       ├── Vol 1 - 1994 - Wizards First Rule {Sam Tsoutsouvas}/
│       │   ├── Track01.mp3
│       │   └── cover.jpg
│       └── Vol 2 - 1995 - Stone of Tears/
│           └── audiobook.m4b
├── Steven Levy/
│   └── Hackers - Heroes of the Computer Revolution {Mike Chamberlain}/
│       └── audiobook.m4a
└── George Orwell/
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

# Preview what would happen
aborg org --dry-run

# Organize for real
aborg org

# Organize from a specific dir to a specific dest
aborg org -d /path/to/downloads --dest /mnt/nas/audiobooks

# Analyze your existing collection
aborg analyze --path /mnt/nas/audiobooks

# Test how a filename parses
aborg parse "Brandon Sanderson - Mistborn Book 1 - The Final Empire (2006) [Michael Kramer]"

# Rename existing folders to match conventions
aborg rename --path /mnt/nas/audiobooks --dry-run

# Undo last organize
aborg undo
```

## Configuration

Create a config file:

```bash
aborg config --init
```

This writes `~/.aborg/config.yaml`. See [`config.example.yaml`](config.example.yaml) for all options.

Key settings:

| Key | Default | Description |
|-----|---------|-------------|
| `source_dirs` | `~/Downloads` | Directories to scan for new audiobooks |
| `destination` | `N:\media\audiobooks` | Where organized files go |
| `auto_extract` | `true` | Extract zip files at destination |
| `delete_after_extract` | `false` | Remove zip after extraction |
| `min_file_size` | `1 MB` | Ignore files smaller than this |
| `filename_patterns` | 4 built-in | Regex patterns for parsing filenames |

## Filename parsing

The tool tries multiple regex patterns against filenames (configurable). Built-in patterns handle:

| Pattern | Example |
|---------|---------|
| `Author - Series Book N - Title (Year) [Narrator]` | `Brandon Sanderson - Mistborn Book 1 - The Final Empire (2006) [Michael Kramer]` |
| `Author - Title (Year) [Narrator]` | `Frank Herbert - Dune (1965) [Scott Brick]` |
| `Title - Author (Year)` | `Dune - Frank Herbert (1965)` |
| `Author_Title` | `Frank Herbert_Dune` |

Audio file ID3 tags (artist, album, composer, series, etc.) are also read and merged.

## Commands

| Command | Description |
|---------|-------------|
| `scan` | List discovered audiobooks in source dirs |
| `org` | Organize (move/copy) audiobooks to destination |
| `analyze` | Audit existing collection, list issues |
| `parse` | Test filename parsing |
| `rename` | Batch-rename folders to conventions |
| `undo` | Revert last organize batch |
| `config` | Show or initialize configuration |

All destructive commands support `--dry-run`.

## License

MIT
