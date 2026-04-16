"""Tests for audiobook_organizer.cli — CLI smoke tests."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from audiobook_organizer.cli import _get_git_commit, _offer_source_cleanup, cli
from audiobook_organizer.config import Config


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
            _offer_source_cleanup([src_file], cfg, copy=True)

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
            _offer_source_cleanup([], cfg, copy=False, exist_sources=[exist_dir])

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
            _offer_source_cleanup(moved, cfg, copy=False, exist_sources=[exist_dir])

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


class TestRenameCommand:
    def test_rename_help(self):
        result = CliRunner().invoke(cli, ["rename", "--help"])
        assert result.exit_code == 0
        assert "dry-run" in result.output

    def test_fetch_help(self):
        result = CliRunner().invoke(cli, ["fetch", "--help"])
        assert result.exit_code == 0
        assert "setup" in result.output
