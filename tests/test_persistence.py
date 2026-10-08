"""Test atomic text replacement."""

from pathlib import Path
from unittest.mock import patch

import pytest

from audiobook_organizer.persistence import atomic_write_text


def test_text_replacement_preserves_unicode(tmp_path):
    path = tmp_path / "nested" / "config.yaml"
    atomic_write_text(path, "author: 日本語\n")
    assert path.read_text(encoding="utf-8") == "author: 日本語\n"
    assert list(path.parent.iterdir()) == [path]


def test_failed_replace_preserves_original_and_removes_temporary_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("original", encoding="utf-8")
    with (
        patch.object(Path, "replace", side_effect=PermissionError("locked")),
        pytest.raises(PermissionError, match="locked"),
    ):
        atomic_write_text(path, "replacement")
    assert path.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [path]
