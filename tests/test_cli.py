"""Tests for audiobook_organizer.cli — CLI smoke tests."""

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from audiobook_organizer.cli import _offer_source_cleanup, cli
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
