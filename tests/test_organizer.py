"""Tests for audiobook_organizer.organizer — file moving/copying/extraction."""

import zipfile
from pathlib import Path

from audiobook_organizer.config import Config
from audiobook_organizer.organizer import organize, undo_last
from audiobook_organizer.parser import AudiobookMeta
from audiobook_organizer.scanner import ScanResult


def _scan_result(path: Path, kind: str = "audio_file", **meta_kw) -> ScanResult:
    """Helper to build a ScanResult."""
    defaults = {"author": "Test Author", "title": "Test Book"}
    defaults.update(meta_kw)
    meta = AudiobookMeta(source_path=path, **defaults)
    size = path.stat().st_size if path.exists() else 0
    return ScanResult(path=path, kind=kind, meta=meta, size=size)


def _write(path: Path, data: bytes = b"\x00" * 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestOrganize:
    def test_dry_run_no_changes(self, tmp_path):
        src = _write(tmp_path / "src" / "book.mp3")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")
        item = _scan_result(src)

        actions = organize([item], cfg, dry_run=True)
        assert len(actions) == 1
        assert not dest.exists()  # nothing actually moved

    def test_move_single_file(self, tmp_path):
        src = _write(tmp_path / "src" / "book.mp3")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")
        item = _scan_result(src)

        actions = organize([item], cfg)
        assert len(actions) == 1
        assert not src.exists()  # moved away
        actual_dest = actions[0][1]
        assert actual_dest.exists()

    def test_copy_single_file(self, tmp_path):
        src = _write(tmp_path / "src" / "book.mp3")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")
        item = _scan_result(src)

        actions = organize([item], cfg, copy=True)
        assert len(actions) == 1
        assert src.exists()  # still there
        assert actions[0][1].exists()

    def test_move_directory(self, tmp_path):
        book_dir = tmp_path / "src" / "Author - Book"
        _write(book_dir / "track01.mp3")
        _write(book_dir / "track02.mp3")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")
        item = _scan_result(book_dir, kind="audio_dir")

        actions = organize([item], cfg)
        assert len(actions) == 1
        assert not book_dir.exists()

    def test_extract_zip(self, tmp_path):
        # Create a real zip file
        zip_path = tmp_path / "src" / "book.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("audio.mp3", b"\x00" * 100)

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        actions = organize([item], cfg)
        assert len(actions) == 1
        # The extracted contents should exist
        extracted_dir = actions[0][1]
        assert extracted_dir.exists()

    def test_zip_slip_protection(self, tmp_path):
        """Ensure zip-slip attacks are blocked."""
        zip_path = tmp_path / "src" / "malicious.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../../etc/evil.txt", b"pwned")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        # Should not crash — falls back to moving the file
        actions = organize([item], cfg)
        assert len(actions) == 1
        # The evil file must NOT exist outside dest
        assert not (tmp_path / "etc" / "evil.txt").exists()

    def test_log_created(self, tmp_path):
        src = _write(tmp_path / "src" / "book.mp3")
        log = tmp_path / "moves.log"
        cfg = Config(destination=tmp_path / "dest", move_log=log)
        item = _scan_result(src)

        organize([item], cfg)
        assert log.exists()
        content = log.read_text()
        assert "book.mp3" in content or "Test Book" in content


class TestUndo:
    def test_undo_moves_back(self, tmp_path):
        src = _write(tmp_path / "src" / "book.mp3")
        log = tmp_path / "moves.log"
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=log)
        item = _scan_result(src)

        organize([item], cfg)
        assert not src.exists()

        undone = undo_last(cfg)
        assert len(undone) == 1
        assert src.exists()

    def test_undo_empty_log(self, tmp_path):
        cfg = Config(move_log=tmp_path / "nonexistent.log")
        assert undo_last(cfg) == []

    def test_undo_dry_run(self, tmp_path):
        src = _write(tmp_path / "src" / "book.mp3")
        log = tmp_path / "moves.log"
        cfg = Config(destination=tmp_path / "dest", move_log=log)
        item = _scan_result(src)

        organize([item], cfg)
        undone = undo_last(cfg, dry_run=True)
        assert len(undone) == 1
        # File should NOT have been moved back
        assert not src.exists()


class TestCollisionHandling:
    def test_collision_adds_unique_suffix(self, tmp_path):
        """When destination file already exists, a unique timestamp suffix is added."""
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")

        # Create first file and organize it
        src1 = _write(tmp_path / "src1" / "book.mp3")
        item1 = _scan_result(src1)
        actions1 = organize([item1], cfg)
        assert len(actions1) == 1
        first_dest = actions1[0][1]

        # Create second file with same metadata (will collide)
        src2 = _write(tmp_path / "src2" / "book.mp3")
        item2 = _scan_result(src2)
        actions2 = organize([item2], cfg)
        assert len(actions2) == 1
        second_dest = actions2[0][1]

        # Both should exist at different paths
        assert first_dest.exists()
        assert second_dest.exists()
        assert first_dest != second_dest
        # Suffix should contain microseconds (20 chars: YYYYMMDDHHMMSS + 6 digits)
        suffix_part = second_dest.stem.split("_")[-1]
        assert len(suffix_part) == 20
