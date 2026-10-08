"""Tests for audiobook_organizer.cli — CLI smoke tests."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from audiobook_organizer.cli import (
    _delete_sources,
    _get_git_commit,
    _human_size,
    _offer_source_cleanup,
    _print_book,
    cli,
)
from audiobook_organizer.config import Config
from audiobook_organizer.parser import AudiobookMeta
from audiobook_organizer.scanner import ScanResult


class TestCLI:
    def test_help(self):
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "aborg" in result.output

    def test_scan_help(self):
        result = CliRunner().invoke(cli, ["scan", "--help"])
        assert result.exit_code == 0

    def test_org_help(self):
        result = CliRunner().invoke(cli, ["org", "--help"])
        assert result.exit_code == 0

    def test_parse_command(self, tmp_cfg):
        result = CliRunner().invoke(
            cli, ["-c", tmp_cfg, "parse", "Brandon Sanderson - Mistborn Book 1 - The Final Empire"]
        )
        assert result.exit_code == 0
        assert "Brandon Sanderson" in result.output
        assert "The Final Empire" in result.output

    def test_parse_handles_full_path(self, tmp_cfg):
        """Parse should extract the folder name from a full path."""
        result = CliRunner().invoke(
            cli,
            [
                "-c",
                tmp_cfg,
                "parse",
                r"\\nas\drive\media\audiobooks\Asimov, Isaac\I, Robot - Isaac Asimov - 1950",
            ],
        )
        assert result.exit_code == 0
        # Should show the extracted name, not the full path as author
        assert "I, Robot - Isaac Asimov - 1950" in result.output
        # Parent folder "Asimov, Isaac" should be shown as a source
        assert "Asimov, Isaac" in result.output

    def test_parse_shows_sources(self, tmp_cfg):
        """Parse should display the Sources section."""
        result = CliRunner().invoke(cli, ["-c", tmp_cfg, "parse", "Author - Title"])
        assert result.exit_code == 0
        assert "Sources" in result.output
        assert "Merged result" in result.output

    def test_config_show(self, tmp_cfg):
        result = CliRunner().invoke(cli, ["-c", tmp_cfg, "config", "--show"])
        assert result.exit_code == 0
        assert "Source dirs" in result.output or "Destination" in result.output

    def test_analyze_nonexistent(self, tmp_path):
        # Use a real temp dir but nonexistent subpath — Click validates exists=True
        bad = tmp_path / "nope"
        result = CliRunner().invoke(cli, ["analyze", "--path", str(bad)])
        # Click rejects nonexistent path with exit code 2
        assert result.exit_code == 2

    def test_undo_empty(self, tmp_cfg):
        result = CliRunner().invoke(cli, ["-c", tmp_cfg, "undo"])
        assert result.exit_code == 0
        assert "Nothing to undo" in result.output


class TestOfferSourceCleanup:
    """Tests for _offer_source_cleanup after org moves/copies."""

    def _write(self, path: Path, data: bytes = b"\x00" * 64) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_move_cleans_empty_parent(self, tmp_path):
        """After moving a dir out, its empty parent is offered for cleanup."""
        source_dir = tmp_path / "downloads"
        source_dir.mkdir()
        # Simulate: item was at downloads/batch/Author - Title/ and was moved
        batch = source_dir / "batch"
        batch.mkdir()
        # batch is now empty (audiobook was moved out)

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )
        moved = [batch / "Author - Title"]  # this path no longer exists

        with patch("audiobook_organizer.cli.click.confirm", return_value=True):
            _offer_source_cleanup(moved, cfg, copy=False)

        assert not batch.exists()

    def test_move_no_cleanup_when_parent_not_empty(self, tmp_path):
        """Non-empty parent directories are not offered for cleanup."""
        source_dir = tmp_path / "downloads"
        batch = source_dir / "batch"
        self._write(batch / "other_file.txt")
        # Simulate moved item
        moved = [batch / "Author - Title"]

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )

        with patch("audiobook_organizer.cli.click.confirm", return_value=True):
            _offer_source_cleanup(moved, cfg, copy=False)

        # batch still exists because it has other_file.txt
        assert batch.exists()

    def test_move_does_not_delete_source_dir(self, tmp_path):
        """Source dirs themselves are never deleted, even if empty."""
        source_dir = tmp_path / "downloads"
        source_dir.mkdir()

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )
        # Item was directly inside source_dir
        moved = [source_dir / "book.m4b"]

        with patch("audiobook_organizer.cli.click.confirm", return_value=True):
            _offer_source_cleanup(moved, cfg, copy=False)

        assert source_dir.exists()

    def test_copy_deletes_originals(self, tmp_path):
        """After copying, originals are offered for deletion."""
        source_dir = tmp_path / "downloads"
        src_file = self._write(source_dir / "book.m4b")

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )

        with patch("audiobook_organizer.cli.click.confirm", return_value=True):
            stored = self._write(cfg.destination / src_file.name)
            _offer_source_cleanup([src_file], cfg, copy=True, destinations={src_file: stored})

        assert not src_file.exists()

    def test_copy_cleanup_declined(self, tmp_path):
        """Declining cleanup leaves source files intact."""
        source_dir = tmp_path / "downloads"
        src_file = self._write(source_dir / "book.m4b")

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )

        with patch("audiobook_organizer.cli.click.confirm", return_value=False):
            _offer_source_cleanup([src_file], cfg, copy=True)

        assert src_file.exists()

    def test_move_walks_up_nested_empty_dirs(self, tmp_path):
        """Cleanup walks up through multiple levels of empty dirs."""
        source_dir = tmp_path / "downloads"
        deep = source_dir / "a" / "b" / "c"
        deep.mkdir(parents=True)
        # a/b/c is empty, a/b is empty, a is empty

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )
        moved = [deep / "Some Book"]

        with patch("audiobook_organizer.cli.click.confirm", return_value=True):
            _offer_source_cleanup(moved, cfg, copy=False)

        assert not (source_dir / "a").exists()

    def test_no_cleanup_for_empty_moved_list(self, tmp_path):
        """No prompt when nothing was moved."""
        source_dir = tmp_path / "downloads"
        source_dir.mkdir()
        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )

        with patch("audiobook_organizer.cli.click.confirm") as mock_confirm:
            _offer_source_cleanup([], cfg, copy=False)
            mock_confirm.assert_not_called()

    def test_exist_sources_cleaned_up(self, tmp_path):
        """Source files for books already in collection are offered for cleanup."""
        source_dir = tmp_path / "downloads"
        exist_file = self._write(source_dir / "Author - Title" / "book.m4b")
        exist_dir = exist_file.parent

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )

        with patch("audiobook_organizer.cli.click.confirm", return_value=True):
            stored = self._write(cfg.destination / "Book" / "book.m4b").parent
            _offer_source_cleanup(
                [], cfg, copy=False, exist_sources=[exist_dir], destinations={exist_dir: stored}
            )

        assert not exist_dir.exists()

    def test_exist_sources_and_moved_combined(self, tmp_path):
        """Both exist sources and empty parent dirs from moves are cleaned up."""
        source_dir = tmp_path / "downloads"
        # An EXISTS book still in source
        exist_file = self._write(source_dir / "Old Book" / "book.m4b")
        exist_dir = exist_file.parent
        # A moved book left an empty parent
        empty_parent = source_dir / "batch"
        empty_parent.mkdir()

        cfg = Config(
            source_dirs=[source_dir], destination=tmp_path / "dest", move_log=tmp_path / "log"
        )
        moved = [empty_parent / "New Book"]

        with patch("audiobook_organizer.cli.click.confirm", return_value=True):
            stored = self._write(cfg.destination / "Book" / "book.m4b").parent
            _offer_source_cleanup(
                moved, cfg, copy=False, exist_sources=[exist_dir], destinations={exist_dir: stored}
            )

        assert not exist_dir.exists()
        assert not empty_parent.exists()


class TestAboutCommand:
    def test_about_shows_version(self):
        result = CliRunner().invoke(cli, ["about"])
        assert result.exit_code == 0
        assert "Version" in result.output
        assert "0.1.0" in result.output

    def test_about_shows_project_info(self):
        result = CliRunner().invoke(cli, ["about"])
        assert result.exit_code == 0
        assert "rsmb.tv" in result.output
        assert "robert-bryson/aborg" in result.output
        assert "MIT" in result.output

    def test_about_shows_python_info(self):
        result = CliRunner().invoke(cli, ["about"])
        assert result.exit_code == 0
        assert "Python" in result.output
        assert "Install path" in result.output
        assert "Config path" in result.output


class TestTldrCommand:
    def test_tldr_shows_commands(self):
        result = CliRunner().invoke(cli, ["tldr"])
        assert result.exit_code == 0
        assert "aborg scan" in result.output
        assert "aborg org" in result.output
        assert "aborg config" in result.output

    def test_tldr_shows_sections(self):
        result = CliRunner().invoke(cli, ["tldr"])
        assert result.exit_code == 0
        assert "Quick Start" in result.output
        assert "Scanning" in result.output
        assert "Collection Management" in result.output
        assert "Libby" in result.output

    def test_tldr_shows_about_and_parse(self):
        result = CliRunner().invoke(cli, ["tldr"])
        assert result.exit_code == 0
        assert "aborg about" in result.output
        assert "aborg parse" in result.output


class TestGetGitCommit:
    def test_returns_string_in_git_repo(self):
        """When running from this repo, should return a commit string."""
        result = _get_git_commit()
        # We're in a git repo during tests, so this should return something
        assert result is None or isinstance(result, str)

    def test_returns_none_when_git_fails(self):
        """When git command fails, should return None gracefully."""
        with patch("audiobook_organizer.cli.subprocess.run", side_effect=OSError("no git")):
            assert _get_git_commit() is None

    def test_returns_none_when_not_in_repo(self, tmp_path):
        """When package is not in a git repo, should return None."""
        fake_file = tmp_path / "pkg" / "cli.py"
        fake_file.parent.mkdir(parents=True)
        fake_file.touch()
        with patch("audiobook_organizer.cli.__file__", str(fake_file)):
            result = _get_git_commit()
        assert result is None

    def test_about_no_commit_row_when_git_unavailable(self):
        """About command should skip commit row when git info unavailable."""
        with patch("audiobook_organizer.cli._get_git_commit", return_value=None):
            result = CliRunner().invoke(cli, ["about"])
        assert result.exit_code == 0
        assert "Version" in result.output
        assert "Last commit" not in result.output

    def test_about_shows_commit_when_available(self):
        """About command should show commit when available."""
        with patch("audiobook_organizer.cli._get_git_commit", return_value="abc1234 (2025-01-01)"):
            result = CliRunner().invoke(cli, ["about"])
        assert result.exit_code == 0
        assert "abc1234" in result.output


class TestOrgDestinationValidation:
    """Tests for the org command destination validation."""

    def test_org_rejects_empty_destination(self, tmp_path):
        """org should fail with a clear error when no destination is configured."""
        cfg_file = tmp_path / "config.yaml"
        src = tmp_path / "downloads"
        src.mkdir()
        # Config with no destination
        cfg_file.write_text(f"source_dirs:\n  - {src}\nmove_log: {tmp_path / 'moves.log'}\n")
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "org", "--dry-run"])
        assert result.exit_code != 0
        assert "No destination configured" in result.output


class TestHumanSize:
    """Tests for the _human_size utility function."""

    def test_bytes(self):
        assert _human_size(500) == "500.0 B"

    def test_kilobytes(self):
        assert _human_size(1024) == "1.0 KB"

    def test_megabytes(self):
        assert _human_size(1_048_576) == "1.0 MB"

    def test_gigabytes(self):
        assert _human_size(1_073_741_824) == "1.0 GB"

    def test_terabytes(self):
        assert _human_size(1_099_511_627_776) == "1.0 TB"

    def test_zero(self):
        assert _human_size(0) == "0.0 B"

    def test_fractional(self):
        result = _human_size(1_500_000)
        assert "MB" in result

    def test_petabytes(self):
        result = _human_size(2 * 1024**5)
        assert "PB" in result

    def test_large_exact_boundary(self):
        """Exactly 1 KB should not overflow to MB."""
        assert _human_size(1024) == "1.0 KB"
        assert _human_size(1023) == "1023.0 B"


# ── scan command ─────────────────────────────────────────────────────────


class TestScanCommand:
    def _make_cfg_file(self, tmp_path: Path, src: Path) -> Path:
        cfg_file = tmp_path / "config.yaml"
        dest = tmp_path / "dest"
        dest.mkdir()
        cfg_file.write_text(
            f"source_dirs:\n  - {src}\n"
            f"destination: {dest}\n"
            f"move_log: {tmp_path / 'moves.log'}\n"
            "min_file_size: 100\n"
        )
        return cfg_file

    def test_scan_no_books(self, tmp_path):
        src = tmp_path / "empty_src"
        src.mkdir()
        cfg_file = self._make_cfg_file(tmp_path, src)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "scan"])
        assert result.exit_code == 0
        assert "No audiobook files found" in result.output

    def test_scan_finds_books(self, tmp_path):
        src = tmp_path / "src"
        (src / "Author - Title.mp3").parent.mkdir(parents=True)
        (src / "Author - Title.mp3").write_bytes(b"\x00" * 200)
        cfg_file = self._make_cfg_file(tmp_path, src)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "scan"])
        assert result.exit_code == 0
        assert "Author" in result.output

    def test_scan_table_mode(self, tmp_path):
        src = tmp_path / "src"
        (src / "Author - Title.mp3").parent.mkdir(parents=True)
        (src / "Author - Title.mp3").write_bytes(b"\x00" * 200)
        cfg_file = self._make_cfg_file(tmp_path, src)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "scan", "--table"])
        assert result.exit_code == 0
        assert "Author" in result.output or "Title" in result.output

    def test_scan_with_extra_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        extra = tmp_path / "extra"
        (extra / "Author2 - Book2.mp3").parent.mkdir(parents=True)
        (extra / "Author2 - Book2.mp3").write_bytes(b"\x00" * 200)
        cfg_file = self._make_cfg_file(tmp_path, src)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "scan", "-d", str(extra)])
        assert result.exit_code == 0
        assert "Author2" in result.output

    def test_scan_missing_source_dir(self, tmp_path):
        src = tmp_path / "nonexistent_source"
        cfg_file = self._make_cfg_file(tmp_path, src)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "scan"])
        assert result.exit_code == 0


# ── org command ──────────────────────────────────────────────────────────


class TestOrgCommand:
    def _setup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        src = tmp_path / "src"
        dest = tmp_path / "dest"
        dest.mkdir()
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            f"source_dirs:\n  - {src}\n"
            f"destination: {dest}\n"
            f"move_log: {tmp_path / 'moves.log'}\n"
            "min_file_size: 100\n"
        )
        return src, dest, cfg_file

    def test_org_dry_run_nothing_moved(self, tmp_path):
        src, _dest, cfg_file = self._setup(tmp_path)
        (src / "Author - Book.mp3").parent.mkdir(parents=True)
        (src / "Author - Book.mp3").write_bytes(b"\x00" * 200)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "org", "--dry-run"])
        assert result.exit_code == 0
        assert (src / "Author - Book.mp3").exists()

    def test_org_no_destination_configured(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            f"source_dirs:\n  - {src}\nmove_log: /tmp/moves.log\nmin_file_size: 100\n"
        )
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "org"])
        assert result.exit_code != 0 or "No destination" in result.output

    def test_org_nothing_new(self, tmp_path):
        src, _dest, cfg_file = self._setup(tmp_path)
        src.mkdir()
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "org"])
        assert result.exit_code == 0
        assert "Nothing new" in result.output or "No audiobook" in result.output

    def test_org_yes_flag_skips_prompt(self, tmp_path):
        src, _dest, cfg_file = self._setup(tmp_path)
        (src / "Author - Book.mp3").parent.mkdir(parents=True)
        (src / "Author - Book.mp3").write_bytes(b"\x00" * 200)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "org", "--yes"])
        assert result.exit_code == 0


# ── rename command ───────────────────────────────────────────────────────


class TestRenameCommand:
    def _make_collection(self, root: Path, author: str, book_name: str) -> Path:
        book_dir = root / author / book_name
        book_dir.mkdir(parents=True)
        (book_dir / "audio.mp3").write_bytes(b"\x00" * 200)
        return book_dir

    def _make_cfg_file(self, tmp_path: Path, dest: Path) -> Path:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            f"source_dirs: []\ndestination: {dest}\nmove_log: {tmp_path / 'moves.log'}\n"
            "min_file_size: 100\n"
        )
        return cfg_file

    def test_rename_help(self):
        result = CliRunner().invoke(cli, ["rename", "--help"])
        assert result.exit_code == 0

    def test_rename_dry_run_all_match(self, tmp_path):
        """When all folders already match conventions, report no renames needed."""
        dest = tmp_path / "collection"
        self._make_collection(dest, "Author", "Book Title")
        cfg_file = self._make_cfg_file(tmp_path, dest)
        with patch("audiobook_organizer.scanner.parse_audio_tags") as mock_tags:
            from audiobook_organizer.parser import AudiobookMeta

            mock_tags.return_value = AudiobookMeta(author="Author", title="Book Title")
            result = CliRunner().invoke(cli, ["-c", str(cfg_file), "rename", "--dry-run"])
        assert result.exit_code == 0

    def test_rename_dry_run_shows_renames(self, tmp_path):
        """Folders that need renaming should be shown in dry-run output."""
        dest = tmp_path / "collection"
        # Create a book whose folder name includes a year
        book_dir = dest / "Author" / "2001 - Book Title"
        book_dir.mkdir(parents=True)
        (book_dir / "audio.mp3").write_bytes(b"\x00" * 200)
        cfg_file = self._make_cfg_file(tmp_path, dest)
        with patch("audiobook_organizer.scanner.parse_audio_tags") as mock_tags:
            from audiobook_organizer.parser import AudiobookMeta

            mock_tags.return_value = AudiobookMeta(author="Author", title="Book Title", year="2001")
            result = CliRunner().invoke(cli, ["-c", str(cfg_file), "rename", "--dry-run"])
        assert result.exit_code == 0


# ── analyze command ───────────────────────────────────────────────────────


class TestAnalyzeCommand:
    def _make_collection(self, root: Path) -> None:
        book_dir = root / "Author" / "Book Title"
        book_dir.mkdir(parents=True)
        (book_dir / "audio.mp3").write_bytes(b"\x00" * 200)

    def _make_cfg_file(self, tmp_path: Path, dest: Path) -> Path:
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            f"source_dirs: []\ndestination: {dest}\nmove_log: {tmp_path / 'moves.log'}\n"
            "min_file_size: 100\n"
        )
        return cfg_file

    def test_analyze_help(self):
        result = CliRunner().invoke(cli, ["analyze", "--help"])
        assert result.exit_code == 0

    def test_analyze_empty_collection(self, tmp_path):
        dest = tmp_path / "collection"
        dest.mkdir()
        cfg_file = self._make_cfg_file(tmp_path, dest)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "analyze"])
        assert result.exit_code == 0

    def test_analyze_with_path_override(self, tmp_path):
        dest = tmp_path / "collection"
        dest.mkdir()
        cfg_file = self._make_cfg_file(tmp_path, dest)
        result = CliRunner().invoke(cli, ["-c", str(cfg_file), "analyze", "--path", str(dest)])
        assert result.exit_code == 0

    def test_analyze_with_no_check_tags(self, tmp_path):
        dest = tmp_path / "collection"
        self._make_collection(dest)
        cfg_file = self._make_cfg_file(tmp_path, dest)
        with patch("audiobook_organizer.scanner.parse_audio_tags") as mock_tags:
            from audiobook_organizer.parser import AudiobookMeta

            mock_tags.return_value = AudiobookMeta(author="Author", title="Book Title")
            result = CliRunner().invoke(cli, ["-c", str(cfg_file), "analyze", "--no-check-tags"])
        assert result.exit_code == 0


# ── undo command ──────────────────────────────────────────────────────────


class TestUndoCommand:
    def test_undo_dry_run(self, tmp_cfg):
        result = CliRunner().invoke(cli, ["-c", tmp_cfg, "undo", "--dry-run"])
        assert result.exit_code == 0
        assert "Nothing to undo" in result.output


@pytest.fixture()
def ux_library(tmp_path):
    src, dest, config = TestOrgCommand()._setup(tmp_path)
    src.mkdir()
    existing = src / "Jane Austen - Emma.mp3"
    new = src / "George Orwell - Animal Farm.mp3"
    existing.write_bytes(b"\0" * 200)
    new.write_bytes(b"\0" * 200)
    book_dir = dest / "Austen, Jane" / "Emma"
    book_dir.mkdir(parents=True)
    (book_dir / "audio.mp3").write_bytes(b"original library data")
    (book_dir / existing.name).write_bytes(existing.read_bytes())
    return config, existing, new, book_dir


class TestOrganizeUX:
    def test_mixed_plan_names_deletion_and_result_follows_cleanup(self, ux_library):
        config, existing, new, book_dir = ux_library
        result = CliRunner().invoke(cli, ["-c", str(config), "org", "--clean-exists"], input="y\n")
        assert result.exit_code == 0, result.output
        assert (
            "Organize 1 new audiobook and delete 1 source path already in collection?"
            in result.output
        )
        assert result.output.index("Source paths to delete:") < result.output.index("[y/N]")
        assert result.output.index("Deleted:") < result.output.index("Result\n")
        assert "Deleted 1 source path" in result.output
        assert "Organized 1 audiobook" in result.output
        assert "✗" not in result.output
        assert not existing.exists()
        assert not new.exists()
        assert (book_dir / "audio.mp3").read_bytes() == b"original library data"

    def test_cleanup_only_prompts_for_deletion(self, ux_library):
        config, existing, new, _ = ux_library
        new.unlink()
        result = CliRunner().invoke(cli, ["-c", str(config), "org", "--clean-exists"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "Delete 1 source path already in collection?" in result.output
        assert "Organize 0" not in result.output
        assert "Deleted 1 source path" in result.output
        assert not existing.exists()

    def test_declined_plan_preserves_all_sources(self, ux_library):
        config, existing, new, _ = ux_library
        result = CliRunner().invoke(cli, ["-c", str(config), "org", "--clean-exists"], input="n\n")
        assert result.exit_code == 0, result.output
        assert "Cancelled. No files changed." in result.output
        assert existing.exists() and new.exists()

    @pytest.mark.parametrize("flags", [[], ["--yes"], ["--dry-run"]])
    def test_noop_does_not_prompt_even_with_yes_or_dry_run(self, ux_library, flags):
        config, existing, new, _ = ux_library
        new.unlink()
        with patch("audiobook_organizer.cli.click.confirm") as confirm:
            result = CliRunner().invoke(cli, ["-c", str(config), "org", *flags])
        assert result.exit_code == 0, result.output
        confirm.assert_not_called()
        assert "Nothing new to organize" in result.output
        assert existing.exists()

    @pytest.mark.parametrize("copy_flags, verb", [([], "organize"), (["--copy"], "copy")])
    def test_dry_run_previews_both_actions_without_changes(self, ux_library, copy_flags, verb):
        config, existing, new, _ = ux_library
        with patch("audiobook_organizer.cli.click.confirm") as confirm:
            result = CliRunner().invoke(
                cli, ["-c", str(config), "org", "--clean-exists", "--dry-run", *copy_flags]
            )
        assert result.exit_code == 0, result.output
        confirm.assert_not_called()
        assert f"Would {verb} 1 audiobook" in result.output
        assert "Would delete 1 source path" in " ".join(result.output.split())
        assert "no files changed" in result.output
        assert "To:" in result.output
        assert existing.exists() and new.exists()
        assert not (new.parent.parent / "moves.log").exists()

    def test_yes_copy_keeps_originals_without_optional_prompt(self, ux_library):
        config, existing, new, _ = ux_library
        with patch("audiobook_organizer.cli.click.confirm") as confirm:
            result = CliRunner().invoke(cli, ["-c", str(config), "org", "--copy", "--yes"])
        assert result.exit_code == 0, result.output
        confirm.assert_not_called()
        assert "Copied 1 audiobook" in result.output
        assert existing.exists() and new.exists()

    def test_yes_clean_exists_removes_only_requested_sources(self, ux_library):
        config, existing, new, _ = ux_library
        with patch("audiobook_organizer.cli.click.confirm") as confirm:
            result = CliRunner().invoke(
                cli, ["-c", str(config), "org", "--copy", "--yes", "--clean-exists"]
            )
        assert result.exit_code == 0, result.output
        confirm.assert_not_called()
        assert not existing.exists()
        assert new.exists()

    def test_organize_failure_is_identified_and_returns_nonzero(self, ux_library):
        config, existing, new, _ = ux_library
        with patch("audiobook_organizer.cli.organize", return_value=[]):
            result = CliRunner().invoke(cli, ["-c", str(config), "org", "--yes"])
        assert result.exit_code == 1, result.output
        assert "FAILED" in result.output
        assert "Animal Farm" in result.output
        assert "1 failed or partial" in result.output
        assert existing.exists() and new.exists()

    def test_cleanup_failure_is_in_final_result_and_returns_nonzero(self, ux_library):
        config, existing, new, _ = ux_library
        new.unlink()
        with patch.object(Path, "unlink", side_effect=PermissionError("file is in use")):
            result = CliRunner().invoke(cli, ["-c", str(config), "org", "--yes", "--clean-exists"])
        assert result.exit_code == 1, result.output
        assert "Failed to delete" in result.output
        assert "1 cleanup failed" in result.output
        assert "Deleted 1 source path" not in result.output
        assert existing.exists()

    def test_new_collision_is_kept_during_clean_exists(self, ux_library):
        config, existing, new, _ = ux_library
        extra = new.parent.parent / "another-download"
        extra.mkdir()
        duplicate = extra / new.name
        duplicate.write_bytes(new.read_bytes())
        # Exercise a collision even if scanner deduplication normally filters
        # it (e.g. distinct long titles truncated to the same folder name).
        hits = [
            ScanResult(
                existing,
                "audio_file",
                AudiobookMeta(author="Jane Austen", title="Emma"),
                200,
                source_dir=existing.parent,
            ),
            ScanResult(
                new,
                "audio_file",
                AudiobookMeta(author="George Orwell", title="Animal Farm"),
                200,
                source_dir=new.parent,
            ),
            ScanResult(
                duplicate,
                "audio_file",
                AudiobookMeta(author="George Orwell", title="Animal Farm"),
                200,
                source_dir=extra,
            ),
        ]

        def scan_hits(_cfg, *, on_hit, **_kwargs):
            for hit in hits:
                on_hit(hit)
            return hits, []

        with patch("audiobook_organizer.cli.scan_sources", side_effect=scan_hits):
            result = CliRunner().invoke(
                cli, ["-c", str(config), "org", "--yes", "--clean-exists", "--dir", str(extra)]
            )
        assert result.exit_code == 0, result.output
        assert "SKIPPED" in result.output
        assert not existing.exists()
        assert sum(path.exists() for path in (new, duplicate)) == 1

    def test_invalid_destination_fails_before_confirmation(self, ux_library):
        config, existing, new, _ = ux_library
        missing = new.parent / "missing-destination"
        with patch("audiobook_organizer.cli.click.confirm") as confirm:
            result = CliRunner().invoke(cli, ["-c", str(config), "org", "--dest", str(missing)])
        assert result.exit_code == 1
        confirm.assert_not_called()
        assert "Destination does not exist" in result.output
        assert existing.exists() and new.exists()

    def test_cleanup_protects_source_root_and_library(self, ux_library):
        config, existing, _, book_dir = ux_library
        cfg = Config.load(config)
        result = _delete_sources(
            [existing.parent, book_dir, book_dir / "audio.mp3", cfg.destination], cfg
        )
        assert result.removed == 0
        assert result.skipped == 4
        assert existing.exists() and (book_dir / "audio.mp3").exists()


class TestTerminalUX:
    def test_table_mode_prints_books_once(self, ux_library):
        config, _, _, _ = ux_library
        result = CliRunner().invoke(cli, ["-c", str(config), "scan", "--table"])
        assert result.exit_code == 0, result.output
        assert result.output.count("Animal Farm") == 1
        assert result.output.count("Emma") == 1
        assert "EXISTS" in result.output and "NEW" in result.output
        assert "To:" not in result.output

    def test_verbose_scan_includes_destinations(self, ux_library):
        config, _, _, _ = ux_library
        result = CliRunner().invoke(cli, ["-c", str(config), "scan", "--verbose"])
        assert result.exit_code == 0, result.output
        assert "To:" in result.output
        assert "Orwell, George" in result.output

    @pytest.mark.parametrize("width", [40, 80, 140])
    def test_book_rows_preserve_literal_metadata_and_fit_width(self, width):
        from io import StringIO

        output = StringIO()
        terminal = Console(file=output, width=width, color_system=None, highlight=False)
        item = ScanResult(
            Path("book.mp3"),
            "audio_file",
            AudiobookMeta(
                author="Jane [red] Austen",
                title="A very long [bold] title with [/bold] brackets and extra words",
                series="Series [blue]",
                sequence="1",
            ),
            200,
        )
        with patch("audiobook_organizer.cli.console", terminal):
            _print_book(1, item, "NEW", "green")
        rendered = output.getvalue()
        assert "[bold]" in rendered and "[/bold]" in rendered
        assert "[red]" in rendered and "[blue]" in rendered
        assert all(len(line) <= width for line in rendered.splitlines())
        assert "NEW" in rendered
        assert "\x1b" not in rendered

    @pytest.mark.parametrize("width", [40, 80, 140])
    def test_about_is_compact_and_reports_selected_config(self, ux_library, width):
        config, _, _, _ = ux_library
        terminal = Console(width=width, color_system=None, highlight=False)
        with patch("audiobook_organizer.cli.console", terminal):
            result = CliRunner().invoke(cli, ["-c", str(config), "about"])
        assert result.exit_code == 0, result.output
        assert "Python build" not in result.output
        assert "Executable" not in result.output
        assert "┌" not in result.output
        assert all(len(line) <= width for line in result.output.splitlines())
        compact = "".join(result.output.split())
        assert "".join(str(config).split()) in compact

    def test_verbose_about_includes_build_details(self):
        result = CliRunner().invoke(cli, ["about", "--verbose"])
        assert result.exit_code == 0, result.output
        assert "Python build" in result.output
        assert "Executable" in result.output


class TestReviewRegressions:
    def test_bad_config_is_a_cli_error(self, tmp_path):
        config = tmp_path / "config.yaml"
        config.write_text('delete_after_extract: "false"\n')
        result = CliRunner().invoke(cli, ["-c", str(config), "org", "--yes"])
        assert result.exit_code == 1
        assert "delete_after_extract must be true or false" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("flags", [[], ["--dry-run"]])
    def test_empty_existing_destination_cannot_destroy_source(self, ux_library, flags):
        config, existing, new, book_dir = ux_library
        new.unlink()
        for child in book_dir.iterdir():
            child.unlink()
        result = CliRunner().invoke(
            cli, ["-c", str(config), "org", "--clean-exists", "--yes", *flags]
        )
        assert result.exit_code == 1, result.output
        assert "1 cleanup failed" in result.output
        assert existing.exists()
        assert book_dir.exists()

    def test_partial_group_failure_preserves_remaining_files(self, ux_library):
        config, _, new, _ = ux_library
        other = new.with_name("other.mp3")
        other.write_bytes(b"audio")
        item = ScanResult(
            new.parent,
            "audio_group",
            AudiobookMeta(author="Author One", title="Book One"),
            205,
            source_dir=new.parent,
            source_files=(new, other),
        )

        def scan_hits(_cfg, *, on_hit, **_kwargs):
            on_hit(item)
            return [item], []

        with (
            patch("audiobook_organizer.cli.scan_sources", side_effect=scan_hits),
            patch(
                "audiobook_organizer.cli.organize",
                return_value=[(new, new.parent.parent / "stored.mp3")],
            ),
        ):
            result = CliRunner().invoke(cli, ["-c", str(config), "org", "--yes"])
        assert result.exit_code == 1
        assert "PARTIAL" in result.output
        assert "1/2 files completed" in result.output
        assert other.exists()


class TestCorrectionExitCodes:
    def test_analyze_failed_correction_returns_nonzero(self, ux_library):
        from audiobook_organizer.analyzer import AnalysisReport, FixAction, Issue

        config, _, _, book_dir = ux_library
        report = AnalysisReport(
            issues=[
                Issue(
                    "warning",
                    "naming",
                    "Rename directory",
                    fix=FixAction("rename", book_dir, book_dir),
                )
            ]
        )
        with patch("audiobook_organizer.cli.analyze_collection", return_value=report):
            result = CliRunner().invoke(cli, ["-c", str(config), "analyze", "--fix", "--yes"])
        assert result.exit_code == 1, result.output
        assert "Failed 1 correction" in result.output
        assert book_dir.exists()

    def test_rename_permission_failure_returns_nonzero(self, ux_library):
        from audiobook_organizer.scanner import CollectionScan

        config, _, _, book_dir = ux_library
        item = ScanResult(
            book_dir, "audio_dir", AudiobookMeta(author="Jane Austen", title="New Title"), 200
        )
        with (
            patch(
                "audiobook_organizer.cli.scan_collection", return_value=CollectionScan(items=[item])
            ),
            patch.object(Path, "rename", side_effect=PermissionError("locked")),
        ):
            result = CliRunner().invoke(cli, ["-c", str(config), "rename", "--yes"])
        assert result.exit_code == 1, result.output
        assert "Failed to rename" in result.output
        assert "Renamed 0 folders" in result.output
        assert book_dir.exists()

    def test_undo_conflict_returns_nonzero_and_keeps_log(self, ux_library):
        config, _, new, _ = ux_library
        organized = CliRunner().invoke(cli, ["-c", str(config), "org", "--yes"])
        assert organized.exit_code == 0, organized.output
        new.write_bytes(b"new download at original source")
        result = CliRunner().invoke(cli, ["-c", str(config), "undo"])
        assert result.exit_code == 1, result.output
        assert "Failed to undo 1 operation" in result.output
        assert new.read_bytes() == b"new download at original source"
        assert Config.load(config).move_log.read_text(encoding="utf-8")

    def test_git_worktree_file_is_supported(self, tmp_path):
        from unittest.mock import MagicMock

        (tmp_path / ".git").write_text("gitdir: /some/worktree")
        package = tmp_path / "src" / "package" / "cli.py"
        package.parent.mkdir(parents=True)
        package.touch()
        with (
            patch("audiobook_organizer.cli.__file__", str(package)),
            patch(
                "audiobook_organizer.cli.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="abc123 (date)"),
            ),
        ):
            assert _get_git_commit() == "abc123 (date)"
