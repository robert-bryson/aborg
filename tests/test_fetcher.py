"""Tests for audiobook_organizer.fetcher — Libby download integration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from audiobook_organizer.fetcher import (
    LibbyLoan,
    check_odmpy,
    download_latest,
    download_loan,
    is_authenticated,
    libby_setup,
    list_loans,
)


class TestCheckOdmpy:
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_available(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert check_odmpy() is True

    @patch("audiobook_organizer.fetcher.subprocess.run", side_effect=FileNotFoundError)
    def test_not_available(self, mock_run):
        assert check_odmpy() is False


class TestIsAuthenticated:
    def test_not_authenticated(self, tmp_path):
        assert is_authenticated(tmp_path) is False

    def test_authenticated(self, tmp_path):
        (tmp_path / "libby.json").write_text("{}")
        assert is_authenticated(tmp_path) is True


class TestLibbySetup:
    def test_invalid_code_too_short(self, tmp_path):
        ok, msg = libby_setup(tmp_path, "1234")
        assert ok is False
        assert "8 digits" in msg

    def test_invalid_code_non_numeric(self, tmp_path):
        ok, msg = libby_setup(tmp_path, "abcdefgh")
        assert ok is False
        assert "8 digits" in msg

    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    def test_setup_success(self, mock_cmd, tmp_path):
        mock_client = MagicMock()
        mock_client.get_chip.return_value = {}
        mock_client.clone_by_code.return_value = {}

        with patch.dict(
            "sys.modules",
            {
                "odmpy": MagicMock(),
                "odmpy.libby": MagicMock(LibbyClient=MagicMock(return_value=mock_client)),
            },
        ):
            ok, msg = libby_setup(tmp_path, "12345678")
            assert ok is True
            assert "successfully" in msg.lower()
            mock_client.get_chip.assert_called_once()
            mock_client.clone_by_code.assert_called_once_with("12345678")

    def test_setup_catches_runtime_error(self, tmp_path):
        """RuntimeError from LibbyClient should be caught and reported."""
        mock_client = MagicMock()
        mock_client.get_chip.side_effect = RuntimeError("connection failed")

        with patch.dict(
            "sys.modules",
            {
                "odmpy": MagicMock(),
                "odmpy.libby": MagicMock(LibbyClient=MagicMock(return_value=mock_client)),
            },
        ):
            ok, msg = libby_setup(tmp_path, "12345678")
            assert ok is False
            assert "connection failed" in msg

    def test_setup_catches_os_error(self, tmp_path):
        """OSError from LibbyClient should be caught and reported."""
        mock_client = MagicMock()
        mock_client.get_chip.side_effect = OSError("disk full")

        with patch.dict(
            "sys.modules",
            {
                "odmpy": MagicMock(),
                "odmpy.libby": MagicMock(LibbyClient=MagicMock(return_value=mock_client)),
            },
        ):
            ok, msg = libby_setup(tmp_path, "12345678")
            assert ok is False
            assert "disk full" in msg


class TestListLoans:
    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_list_loans_parses_json(self, mock_run, mock_cmd, tmp_path):
        loans_data = [
            {
                "id": 12345,
                "title": "Test Book",
                "firstCreatorName": "Test Author",
                "formats": [{"id": "audiobook-mp3"}],
            },
            {
                "id": 99999,
                "title": "An Ebook",
                "firstCreatorName": "Ebook Author",
                "formats": [{"id": "ebook-epub-adobe"}],
            },
        ]

        export_paths = []

        def side_effect(cmd, **kwargs):
            loans_file = None
            for i, arg in enumerate(cmd):
                if arg == "--exportloans" and i + 1 < len(cmd):
                    loans_file = Path(cmd[i + 1])
                    break
            if loans_file:
                export_paths.append(loans_file)
                loans_file.write_text(json.dumps(loans_data))
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect

        loans = list_loans(tmp_path)

        assert len(loans) == 1
        assert loans[0].title == "Test Book"
        assert loans[0].author == "Test Author"
        assert loans[0].id == "12345"
        assert loans[0].index == 1
        assert export_paths
        assert not export_paths[0].parent.exists()


class TestDownloadLoan:
    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_download_success(self, mock_run, mock_cmd, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        loan = LibbyLoan(id="123", title="My Book", author="Author", index=1)

        result = download_loan(tmp_path / "settings", tmp_path / "dl", loan)

        assert result.success is True
        assert result.loan is loan
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "--selectid" in cmd
        assert "123" in cmd

    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_download_failure(self, mock_run, mock_cmd, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="network error")
        loan = LibbyLoan(id="123", title="My Book", author="Author", index=1)

        result = download_loan(tmp_path / "settings", tmp_path / "dl", loan)

        assert result.success is False
        assert "network error" in result.message

    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_download_with_merge(self, mock_run, mock_cmd, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        loan = LibbyLoan(id="123", title="My Book", author="Author", index=1)

        download_loan(tmp_path / "settings", tmp_path / "dl", loan, merge=True)

        cmd = mock_run.call_args[0][0]
        assert "-m" in cmd
        assert "--mergeformat" in cmd


class TestDownloadLatest:
    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_latest_success(self, mock_run, mock_cmd, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="done", stderr="")

        ok, _output = download_latest(tmp_path / "settings", tmp_path / "dl", count=2)

        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert "--latest" in cmd
        assert "2" in cmd

    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_latest_failure(self, mock_run, mock_cmd, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="auth error")

        ok, output = download_latest(tmp_path / "settings", tmp_path / "dl")

        assert ok is False
        assert "auth error" in output


class TestCLIFetch:
    """Smoke tests for the fetch CLI command."""

    def test_fetch_help(self):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        result = CliRunner().invoke(cli, ["fetch", "--help"])
        assert result.exit_code == 0
        assert "Libby" in result.output
        assert "--setup" in result.output
        assert "--list" in result.output
        assert "--latest" in result.output

    @patch("audiobook_organizer.cli.check_odmpy", return_value=False)
    def test_fetch_no_odmpy(self, mock_check, tmp_cfg):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        result = CliRunner().invoke(cli, ["-c", tmp_cfg, "fetch", "--list"])
        assert "odmpy is not installed" in result.output

    @patch("audiobook_organizer.cli.check_odmpy", return_value=True)
    def test_fetch_no_auth(self, mock_check, tmp_cfg):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        result = CliRunner().invoke(cli, ["-c", tmp_cfg, "fetch", "--list"])
        assert "No Libby account linked" in result.output

    @patch("audiobook_organizer.cli.check_odmpy", return_value=True)
    def test_fetch_no_action(self, mock_check, tmp_path):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        # Create a fake auth file so we pass the auth check
        settings = tmp_path / "libby"
        settings.mkdir(parents=True)
        (settings / "libby.json").write_text("{}")

        result = CliRunner().invoke(cli, ["-c", str(tmp_path / "nonexistent.yaml"), "fetch"])
        # Without auth it shows the no-account message (since config defaults to ~/.aborg/libby)
        assert (
            result.exit_code != 0
            or "No action specified" in result.output
            or "No Libby" in result.output
        )


class TestListLoansEdgeCases:
    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_non_list_json_returns_empty(self, mock_run, mock_cmd, tmp_path):
        """Malformed loans JSON (not a list) should return an empty list."""

        def side_effect(cmd, **kwargs):
            for i, arg in enumerate(cmd):
                if arg == "--exportloans" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text('{"error": "unexpected"}')
                    break
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        loans = list_loans(tmp_path)
        assert loans == []

    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_non_dict_entries_skipped(self, mock_run, mock_cmd, tmp_path):
        """Non-dict entries in the loans JSON list are skipped without error."""

        def side_effect(cmd, **kwargs):
            for i, arg in enumerate(cmd):
                if arg == "--exportloans" and i + 1 < len(cmd):
                    Path(cmd[i + 1]).write_text(
                        '[null, 42, "string", {"id": 1, "title": "Real Book", '
                        '"firstCreatorName": "Author", "formats": [{"id": "audiobook-mp3"}]}]'
                    )
                    break
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        loans = list_loans(tmp_path)
        assert len(loans) == 1
        assert loans[0].title == "Real Book"

    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_subprocess_failure_raises(self, mock_run, mock_cmd, tmp_path):
        """odmpy export failure should raise RuntimeError."""
        mock_run.return_value = MagicMock(returncode=1, stderr="connection refused", stdout="")
        import pytest

        with pytest.raises(RuntimeError, match="odmpy export failed"):
            list_loans(tmp_path)

    @patch("audiobook_organizer.fetcher._odmpy_cmd", return_value=["odmpy"])
    @patch("audiobook_organizer.fetcher.subprocess.run")
    def test_missing_export_file_returns_empty(self, mock_run, mock_cmd, tmp_path):
        """If odmpy succeeds but writes no file, return an empty list."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        loans = list_loans(tmp_path)
        assert loans == []


@pytest.fixture()
def fetch_config(tmp_path):
    from audiobook_organizer.config import Config

    config = tmp_path / "config.yaml"
    cfg = Config.default()
    cfg.source_dirs = []
    cfg.destination = tmp_path / "library"
    cfg.destination.mkdir()
    cfg.save(config)
    return config


class TestFetchFailures:
    @pytest.mark.parametrize(
        "flags",
        [
            ["--latest", "0"],
            ["--latest", "-1"],
            ["--all", "--latest", "1"],
            ["--list", "--select", "1"],
        ],
    )
    def test_invalid_fetch_actions_are_rejected(self, fetch_config, flags):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        result = CliRunner().invoke(cli, ["-c", str(fetch_config), "fetch", *flags])
        assert result.exit_code == 2

    def test_no_action_shows_help_without_odmpy(self, fetch_config):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        with patch("audiobook_organizer.cli.check_odmpy") as check:
            result = CliRunner().invoke(cli, ["-c", str(fetch_config), "fetch"])
        assert result.exit_code == 0
        assert "--latest" in result.output
        check.assert_not_called()

    def test_download_requires_a_directory(self, fetch_config):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        with (
            patch("audiobook_organizer.cli.check_odmpy", return_value=True),
            patch("audiobook_organizer.cli.is_authenticated", return_value=True),
        ):
            result = CliRunner().invoke(cli, ["-c", str(fetch_config), "fetch", "--latest", "1"])
        assert result.exit_code == 1
        assert "No download directory" in result.output

    def test_subprocess_error_becomes_cli_error(self, fetch_config):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        with (
            patch("audiobook_organizer.cli.check_odmpy", return_value=True),
            patch("audiobook_organizer.cli.is_authenticated", return_value=True),
            patch(
                "audiobook_organizer.cli.list_loans", side_effect=RuntimeError("connection failed")
            ),
        ):
            result = CliRunner().invoke(cli, ["-c", str(fetch_config), "fetch", "--list"])
        assert result.exit_code == 1
        assert "Libby operation failed: connection failed" in result.output
        assert "Traceback" not in result.output

    def test_partly_invalid_id_selection_does_not_download_subset(self, fetch_config, tmp_path):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        loans = [LibbyLoan("valid", "Book", "Author", 1)]
        with (
            patch("audiobook_organizer.cli.check_odmpy", return_value=True),
            patch("audiobook_organizer.cli.is_authenticated", return_value=True),
            patch("audiobook_organizer.cli.list_loans", return_value=loans),
            patch("audiobook_organizer.cli.download_loan") as download,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "-c",
                    str(fetch_config),
                    "fetch",
                    "--select",
                    "valid",
                    "--select",
                    "invalid",
                    "--download-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 1
        assert "invalid" in result.output
        download.assert_not_called()

    def test_failed_selected_download_returns_nonzero(self, fetch_config, tmp_path):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli
        from audiobook_organizer.fetcher import FetchResult

        loan = LibbyLoan("valid", "Book", "Author", 1)
        with (
            patch("audiobook_organizer.cli.check_odmpy", return_value=True),
            patch("audiobook_organizer.cli.is_authenticated", return_value=True),
            patch("audiobook_organizer.cli.list_loans", return_value=[loan]),
            patch(
                "audiobook_organizer.cli.download_loan",
                return_value=FetchResult(loan, tmp_path, False, "failed"),
            ),
        ):
            result = CliRunner().invoke(
                cli, ["-c", str(fetch_config), "fetch", "--all", "--download-dir", str(tmp_path)]
            )
        assert result.exit_code == 1
        assert "1 failed" in result.output

    def test_auto_organize_includes_explicit_download_directory(self, fetch_config, tmp_path):
        from click.testing import CliRunner

        from audiobook_organizer.cli import cli

        with (
            patch("audiobook_organizer.cli.check_odmpy", return_value=True),
            patch("audiobook_organizer.cli.is_authenticated", return_value=True),
            patch("audiobook_organizer.cli.download_latest", return_value=(True, "done")),
            patch("audiobook_organizer.cli._run_scan", return_value=None) as scan,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "-c",
                    str(fetch_config),
                    "fetch",
                    "--latest",
                    "1",
                    "--organize",
                    "--download-dir",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        scan.assert_called_once()
        assert tmp_path in scan.call_args.args[0].source_dirs
