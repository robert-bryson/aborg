# aborg

aborg scans source directories and puts audiobook files in an organized collection.
The destination directory uses the [Audiobookshelf directory structure](https://www.audiobookshelf.org/docs/#book-directory-structure).

## Requirements

- Python 3.10 or later.
- [uv](https://docs.astral.sh/uv/) for the installation commands below.
- odmpy for Libby downloads.
- ffmpeg to merge downloads into M4B files.

## Installation

Run these commands from the repository directory:

```sh
# Create the environment and install the development tools.
uv sync

# Install the optional Libby tools.
uv sync --extra libby

# Show the available commands.
uv run aborg --help
```

Use `uv run aborg` in this environment.
If you use another active Python environment, install the package with `uv pip install -e .`.
To include Libby support, use `uv pip install -e ".[libby]"`.
The examples below use `aborg` from an active environment.

## First use

1. Run `aborg config` to create the configuration file.
2. Set the source directories and the destination directory.
3. Create the destination directory if it does not exist.
4. Run `aborg scan` to check the detected metadata.
5. Run `aborg org --dry-run` to examine the planned operations.
6. Run `aborg org` to organize the books.

The configuration file is `~/.aborg/config.yaml` by default.
Use `aborg -c PATH COMMAND` to select another configuration file.

## Commands

| Command | Function |
| --- | --- |
| `scan` | Find audiobooks in the source directories. |
| `org` | Move, copy, or extract audiobooks into the collection. |
| `fetch` | Download audiobook loans from Libby. |
| `analyze` | Find metadata, naming, and directory problems. |
| `rename` | Change title directory names to the required format. |
| `undo` | Reverse the most recent organize batch. |
| `parse` | Show the metadata from a name or path. |
| `config` | Show the configuration or start the setup procedure. |
| `about` | Show the version and environment information. |
| `tldr` | Show common command examples. |

Use `aborg COMMAND --help` for the full option list.
Use `--dry-run` before an operation that changes collection data.
Do not run simultaneous change commands against the same collection or undo log.

### Scan

```sh
aborg scan
aborg scan --table
aborg scan --verbose
aborg scan -d /path/to/other/downloads --cache
```

| Option | Function |
| --- | --- |
| `-d, --dir PATH` | Add a source directory. Repeat this option to add more directories. |
| `--table` | Print each book once in a table. |
| `-v, --verbose` | Show the destination path for each book. |
| `--cache` | Use stored scan results for unchanged paths. |

The `NEW` label means that the destination path does not exist.
The `EXISTS` label means that the destination path exists.
This label does not confirm that the destination contains a complete copy.
Cleanup makes a separate content check before deletion.

Rows use text labels and adjust to the terminal width.
Set the `NO_COLOR` environment variable to disable color.
Rich removes terminal control codes from redirected output unless an environment setting forces terminal mode.

The scanner accepts audio files, audio directories, and configured archive formats.
It can separate tagged albums in a shared directory.
It skips files below `min_file_size`.
It also skips archives below 50,000,000 bytes.

### Organize

```sh
aborg org --dry-run
aborg org
aborg org --copy
aborg org --clean-exists --dry-run
aborg org --clean-exists
aborg org --yes --clean-exists
```

| Option | Function |
| --- | --- |
| `-d, --dir PATH` | Add a source directory. |
| `--dest PATH` | Use this destination directory. |
| `--dry-run` | Check the plan without changing source or destination data. |
| `--copy` | Keep the original data when you organize a book. |
| `-y, --yes` | Accept the plan without a prompt. Keep optional cleanup paths. |
| `-v, --verbose` | Show the destination path for each book. |
| `--cache` | Use stored scan results. |
| `--clean-exists` | Delete verified sources for books that already have a destination. |

The confirmation prompt names the planned operations, including source deletion.
A cleanup-only operation asks to delete sources.
An operation with no work ends without a prompt.
The final result includes organizing and cleanup counts.

Cleanup compares the source data with the stored data.
It checks each source file or ZIP member against its stored file.
The relative file names must match.
Extra destination files, such as cover images, do not prevent cleanup.

Cleanup keeps a source if stored files are missing, incomplete, or different.
It also keeps a source if verification detects a data change.
Cleanup rejects links, Windows reparse points, protected directories, and paths outside the configured source directories.
It removes an empty directory only if that directory is still empty.

Content checks read the source and stored data.
Large books can need more time.
`--dry-run --clean-exists` makes the same checks without deletion.
An incomplete cleanup returns a nonzero exit code.

Sources with a destination created during the current batch are kept.
`--clean-exists` does not delete these sources.
After organizing, an optional prompt offers to remove copied originals or empty source directories.
`--yes` does not accept this optional cleanup.

ZIP extraction keeps the source archive by default.
Set `delete_after_extract: true` to remove an archive after successful extraction.
`--copy` keeps the source archive.
Source cleanup is permanent; `aborg undo` cannot restore cleanup deletions.

### Fetch

```sh
aborg fetch --setup 12345678
aborg fetch --list
aborg fetch --latest 1 --organize
aborg fetch --select LOAN_ID --download-dir /path/to/downloads
aborg fetch --all --dry-run
```

Select one action: `--setup`, `--list`, `--latest`, `--select`, or `--all`.
Repeat `--select` to download more than one loan.
`--latest` requires a positive integer.
An invalid loan ID stops the selection before downloads start.

| Option | Function |
| --- | --- |
| `--setup CODE` | Link a Libby account with an eight-digit setup code. |
| `--list` | Show the available audiobook loans. |
| `--latest N` | Download the latest N loans. |
| `--select ID` | Download the selected loan. |
| `--all` | Download all available audiobook loans. |
| `-d, --download-dir PATH` | Set the download directory. |
| `--organize` | Organize the downloaded books from the selected download directory. |
| `--merge` | Merge the audio parts into one file. |
| `--dry-run` | Show the download plan without downloading books. |

Without `--download-dir`, downloads use the first configured source directory.
A download requires one of these directory settings.
Failed downloads return a nonzero exit code.
Account checks can access Libby during a download preview.
Get a setup code from the [Libby instructions](https://help.libbyapp.com/en-us/6070.htm).

### Analyze and rename

```sh
aborg analyze --path /path/to/library
aborg analyze --no-check-tags
aborg analyze --fix --dry-run
aborg analyze --fix
aborg rename --dry-run
aborg rename --yes
```

`analyze` checks duplicate books, unknown metadata, author names, empty directories, cover images, and directory names.
`--no-check-tags` skips audio tag reads.
`--fix` applies available corrections after confirmation.
`--yes` accepts these corrections without a prompt.

`rename` changes title directory names.
Both commands accept `--path` to select a collection and `--cache` to use stored scan results.
They report failed corrections or name conflicts with a nonzero exit code.
The organize undo log does not record these corrections.

### Undo

```sh
aborg undo --dry-run
aborg undo
```

Undo processes the most recent recorded organize batch.
It restores moved data and removes copied data from the destination.
For extraction, it removes the extracted directory.
If the source ZIP is missing, undo first rebuilds it from the extracted files.
The rebuilt ZIP can have different compression and archive metadata.

A conflicting source path prevents restoration of that move.
The undo log keeps failed entries for another attempt.
New records use UTF-8 JSON Lines and absolute paths.
The reader also accepts the two earlier tab-separated log formats.
Keep a backup of the collection; the undo log is not a backup.

### Other commands

```sh
aborg parse "Frank Herbert - Dune (1965) [Scott Brick]"
aborg config --show
aborg about
aborg about --verbose
aborg tldr
```

`parse` shows filename metadata and available audio tags.
`about` shows the selected configuration path and a short Python version.
`about --verbose` also shows the Python build and executable path.

## Configuration

Use [config.example.yaml](config.example.yaml) as the configuration reference.
YAML lists must contain strings.
Boolean values must be YAML booleans, such as `true` or `false`, without quotation marks.
Invalid types, invalid name formats, and invalid regular expressions stop the command before changes start.

| Setting | Default | Function |
| --- | --- | --- |
| `source_dirs` | Empty list | Source directories to scan. |
| `destination` | Unset | Destination directory for the collection. |
| `auto_extract` | `true` | Extract ZIP files. Move or copy other archives without extraction. |
| `delete_after_extract` | `false` | Delete a ZIP after successful extraction, except with `--copy`. |
| `min_file_size` | `1048576` | Minimum file size in bytes. Use a nonnegative integer. |
| `author_name_format` | `last_first` | Use `last_first` or `first_last` author directory names. |
| `known_authors` | Empty mapping | Map an author alias to a full author name. |
| `filename_patterns` | Seven patterns | Apply these regular expressions in order. |
| `archive_extensions` | `.zip .rar .7z` | Archive file extensions. |
| `audio_extensions` | See the example file | Audio file extensions. |
| `companion_extensions` | See the example file | File extensions for related files, such as cover images. |
| `move_log` | `~/.aborg/moves.log` | Undo log path. |

The `libby` mapping contains these settings:

| Setting | Default | Function |
| --- | --- | --- |
| `settings_folder` | `~/.aborg/libby` | Store Libby account data here. |
| `merge` | `false` | Merge downloaded audio parts. |
| `merge_format` | `m4b` | Use `mp3` or `m4b` for merged data. |
| `chapters` | `true` | Add chapter markers. |
| `keep_cover` | `true` | Download the cover image. |
| `book_folder_format` | `%(Author)s - %(Title)s` | Set the odmpy directory name template. |

Configuration and cache files use UTF-8 and atomic file replacement.
Malformed cache records cause a new scan; the cache is not proof of stored content.

## Metadata and directory names

For source scans, metadata has this priority:

1. Sidecar JSON at `metadata/metadata.json`, in the directory or ZIP.
2. Audio tags from Mutagen.
3. The configured filename patterns.

Sidecar creator roles include `aut` for authors, `nrt` for narrators, and `trl` for translators.
The parser removes common placeholders and damaged punctuation from audio tags.
It can also detect a series name and sequence number.

The default two-part pattern treats `X - Y` as `Author - Title`.
Change the pattern order for collections that use `Title - Author`.
Use `known_authors` to expand a single-name alias.
The parser also recognizes established names such as `Molière`.

Destination paths have this form:

```text
Author / [Series /] [Vol N - ] [Year - ] Title [ {Narrator} ]
```

For example:

```text
Audiobooks/
  Orwell, George/
    1945 - Animal Farm/
      audiobook.mp3
```

The parser removes unsupported path characters and truncates long titles after 180 characters.
Different long titles can produce the same destination name.
The organizer keeps a later source when an earlier operation creates that destination.

## Data protection

ZIP checks reject absolute paths, parent-directory traversal, drive prefixes, and symbolic link entries.
A ZIP with an unsafe member path remains unchanged.
A failed extraction removes only a destination directory that this operation created.
The organizer rejects destinations outside the configured collection or inside the source directory.
It checks undo log access before data changes and reverses an item if its log write fails.

Keep the account settings directory private.
Do not edit an undo log while a command is active.
A process failure or power loss can interrupt an operation; keep a separate backup.

## Development

```sh
uv sync
uv run pytest --cov=audiobook_organizer --cov-report=term-missing
uv run ruff check src tests
uv run ruff format --check src tests
uv run pre-commit install
```

CI checks Python 3.10 through 3.14.
Lint errors and format errors fail CI.
Tests must meet the 80 percent combined statement and branch coverage requirement.
The pre-commit Ruff version matches the lockfile.
See [TODO.md](TODO.md) for open engineering work.

Write documentation with [ASD-STE100](https://www.asd-ste100.org/about_STE.html) principles.
Use short sentences, active instructions, and one name for each technical concept.
Use software identifiers as technical names.

## License

MIT. See [LICENSE](LICENSE).
