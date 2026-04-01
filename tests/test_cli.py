"""Tests for audiobook_organizer.cli — CLI smoke tests."""

from click.testing import CliRunner

from audiobook_organizer.cli import cli


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

    def test_parse_command(self):
        result = CliRunner().invoke(
            cli, ["parse", "Brandon Sanderson - Mistborn Book 1 - The Final Empire"]
        )
        assert result.exit_code == 0
        assert "Brandon Sanderson" in result.output
        assert "The Final Empire" in result.output

    def test_parse_handles_full_path(self):
        """Parse should extract the folder name from a full path."""
        result = CliRunner().invoke(
            cli,
            ["parse", r"\\nas\drive\media\audiobooks\Asimov, Isaac\I, Robot - Isaac Asimov - 1950"],
        )
        assert result.exit_code == 0
        # Should show the extracted name, not the full path as author
        assert "I, Robot - Isaac Asimov - 1950" in result.output
        # Parent folder "Asimov, Isaac" should be shown as a source
        assert "Asimov, Isaac" in result.output

    def test_parse_shows_sources(self):
        """Parse should display the Sources section."""
        result = CliRunner().invoke(
            cli, ["parse", "Author - Title"]
        )
        assert result.exit_code == 0
        assert "Sources" in result.output
        assert "Merged result" in result.output

    def test_config_show(self):
        result = CliRunner().invoke(cli, ["config", "--show"])
        assert result.exit_code == 0
        assert "Source dirs" in result.output or "Destination" in result.output

    def test_analyze_nonexistent(self, tmp_path):
        # Use a real temp dir but nonexistent subpath — Click validates exists=True
        bad = tmp_path / "nope"
        result = CliRunner().invoke(cli, ["analyze", "--path", str(bad)])
        # Click rejects nonexistent path with exit code 2
        assert result.exit_code == 2

    def test_undo_empty(self):
        result = CliRunner().invoke(cli, ["undo"])
        assert result.exit_code == 0
        assert "Nothing to undo" in result.output
