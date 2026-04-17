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

    def test_non_zip_archive_moved_not_extracted(self, tmp_path):
        """Non-zip archives (.rar, .7z) should be moved, not extracted."""
        rar_path = _write(tmp_path / "src" / "book.rar")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(rar_path, kind="archive")

        actions = organize([item], cfg)
        assert len(actions) == 1
        # File should have been moved (not extracted)
        actual_dest = actions[0][1]
        assert actual_dest.exists()
        assert actual_dest.name.startswith("book")
        assert actual_dest.suffix == ".rar"

    def test_zip_slip_protection(self, tmp_path):
        """Ensure zip-slip attacks are blocked — archive is refused entirely."""
        zip_path = tmp_path / "src" / "malicious.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../../etc/evil.txt", b"pwned")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        # Should not crash — refuses the archive, no action taken
        actions = organize([item], cfg)
        assert len(actions) == 0
        # The evil file must NOT exist outside dest
        assert not (tmp_path / "etc" / "evil.txt").exists()
        # The original zip is left untouched
        assert zip_path.exists()

    def test_zip_slip_sibling_prefix_attack(self, tmp_path):
        """A zip member escaping to a sibling dir with a matching name prefix is blocked."""
        zip_path = tmp_path / "src" / "tricky.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            # "../Test Book_evil/payload.txt" resolves to a sibling whose name
            # starts with the dest dir name — the old startswith() check missed this.
            zf.writestr("../Test Book_evil/payload.txt", b"pwned")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        # Unsafe zip is refused entirely
        actions = organize([item], cfg)
        assert len(actions) == 0
        # The payload must NOT exist anywhere outside dest
        evil_dir = dest / "Test Author" / "Test Book_evil"
        assert not evil_dir.exists()
        # Original zip is left untouched
        assert zip_path.exists()

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

    def test_undo_when_dest_already_deleted(self, tmp_path):
        """Undo should skip entries where dest no longer exists (already cleaned up)."""
        src = _write(tmp_path / "src" / "book.mp3")
        log = tmp_path / "moves.log"
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=log)
        item = _scan_result(src)

        organize([item], cfg)
        assert not src.exists()

        # Simulate destination being manually deleted before undo
        actual_dest = next(iter(dest.rglob("*.mp3")))
        actual_dest.unlink()

        # Undo should not crash, but the file can't be restored
        undone = undo_last(cfg)
        assert len(undone) == 1
        # src won't exist because dest was already deleted
        assert not src.exists()

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


class TestUndoMalformedLog:
    def test_undo_skips_malformed_lines(self, tmp_path):
        """Malformed log lines should be skipped, not crash."""
        log = tmp_path / "moves.log"
        # Write a mix of valid and malformed lines
        log.write_text(
            "2025-01-01T00:00:00+00:00\t/src/book.mp3\t/dest/book.mp3\n"
            "malformed line without tabs\n"
            "2025-01-01T00:00:00+00:00\tonlytwofields\n"
        )
        cfg = Config(move_log=log)
        # Should not raise — malformed lines are silently skipped
        result = undo_last(cfg, dry_run=True)
        # Only the valid line forms a batch (all share same timestamp)
        assert isinstance(result, list)

    def test_undo_handles_empty_lines(self, tmp_path):
        """Empty log lines should not crash undo."""
        log = tmp_path / "moves.log"
        log.write_text("\n\n")
        cfg = Config(move_log=log)
        assert undo_last(cfg) == []

    def test_undo_batch_uses_exact_timestamp_match(self, tmp_path):
        """Batch detection should match exact timestamps, not prefixes."""
        log = tmp_path / "moves.log"
        src1 = _write(tmp_path / "src" / "a.mp3")
        src2 = _write(tmp_path / "src" / "b.mp3")
        dest1 = _write(tmp_path / "dest" / "a.mp3")
        dest2 = _write(tmp_path / "dest" / "b.mp3")
        # Timestamps where one is a prefix of another
        log.write_text(
            f"2025-01-01T00:00:00\t{src1}\t{dest1}\n2025-01-01T00:00:00.123\t{src2}\t{dest2}\n"
        )
        cfg = Config(move_log=log)
        undone = undo_last(cfg, dry_run=True)
        # Only the last batch (the .123 timestamp) should be undone
        assert len(undone) == 1
        assert undone[0] == (dest2, src2)


class TestExtractWithDelete:
    def test_delete_after_extract_only_when_content_exists(self, tmp_path):
        """Archive should only be deleted if extraction produced content."""
        zip_path = tmp_path / "src" / "book.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("audio.mp3", b"\x00" * 100)

        dest = tmp_path / "dest"
        cfg = Config(
            destination=dest,
            auto_extract=True,
            delete_after_extract=True,
            move_log=tmp_path / "log",
        )
        item = _scan_result(zip_path, kind="archive")

        actions = organize([item], cfg)
        assert len(actions) == 1
        # Archive should be deleted (extraction succeeded)
        assert not zip_path.exists()
        # Extracted content should exist
        assert (actions[0][1] / "audio.mp3").exists()


class TestCorruptZipFallback:
    def test_bad_zip_falls_back_to_move(self, tmp_path):
        """A corrupt zip (not a valid archive) should be moved as-is."""
        zip_path = tmp_path / "src" / "corrupt.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"this is not a zip file at all")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        actions = organize([item], cfg)
        assert len(actions) == 1
        # The corrupt file should have been moved to the destination
        moved_file = actions[0][1]
        assert moved_file.exists()
        assert moved_file.name == "corrupt.zip"
        # Original should be gone (moved, not copied)
        assert not zip_path.exists()


class TestDirectoryMerge:
    def test_merge_into_existing_dir(self, tmp_path):
        """Moving a directory into an existing one should merge contents."""
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")

        # First organize creates the destination dir
        src1 = tmp_path / "src1" / "Author - Book"
        _write(src1 / "existing.mp3")
        item1 = _scan_result(src1, kind="audio_dir")
        actions1 = organize([item1], cfg)
        assert len(actions1) == 1
        dest_dir = actions1[0][1]
        assert (dest_dir / "existing.mp3").exists()

        # Second organize with same metadata merges into existing
        src2 = tmp_path / "src2" / "Author - Book"
        _write(src2 / "new_track.mp3")
        item2 = _scan_result(src2, kind="audio_dir")
        actions2 = organize([item2], cfg)
        assert len(actions2) == 1

        # Both files should exist in the destination
        assert (dest_dir / "existing.mp3").exists()
        assert (dest_dir / "new_track.mp3").exists()
        # Source should be removed
        assert not src2.exists()


class TestSymlinkProtection:
    def test_symlink_source_dir_refused(self, tmp_path):
        """A symlink source directory should be refused to prevent data loss."""
        real_dir = tmp_path / "real_audiobook"
        _write(real_dir / "track.mp3")
        link_dir = tmp_path / "link_audiobook"
        link_dir.symlink_to(real_dir)

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")
        item = _scan_result(link_dir, kind="audio_dir")

        actions = organize([item], cfg)
        # Symlink source is refused — no action taken
        assert len(actions) == 0
        # Original data is untouched
        assert real_dir.exists()
        assert (real_dir / "track.mp3").exists()

    def test_copy_dir_preserves_internal_symlinks(self, tmp_path):
        """Internal symlinks should be copied as symlinks (symlinks=True)."""
        book_dir = tmp_path / "src" / "Author - Book"
        _write(book_dir / "track.mp3")
        # Create a symlink inside the audiobook dir
        (book_dir / "link.mp3").symlink_to(book_dir / "track.mp3")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")
        item = _scan_result(book_dir, kind="audio_dir")

        actions = organize([item], cfg, copy=True)
        assert len(actions) == 1
        dest_dir = actions[0][1]
        # The internal symlink should be preserved as a symlink, not dereferenced
        assert (dest_dir / "link.mp3").is_symlink()


class TestArchiveDryRun:
    def test_archive_extract_dry_run(self, tmp_path):
        """Dry run on archive extraction should return dest but not extract."""
        zip_path = tmp_path / "src" / "book.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("audio.mp3", b"\x00" * 100)

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        actions = organize([item], cfg, dry_run=True)
        assert len(actions) == 1
        # Nothing should actually be extracted
        assert not dest.exists()


class TestNonZipArchiveCopy:
    def test_rar_copy_preserves_source(self, tmp_path):
        """Non-zip archives should respect --copy and preserve the source file."""
        rar_path = _write(tmp_path / "src" / "book.rar")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(rar_path, kind="archive")

        actions = organize([item], cfg, copy=True)
        assert len(actions) == 1
        # Source should still exist (copied, not moved)
        assert rar_path.exists()
        # Destination should also exist
        assert actions[0][1].exists()

    def test_7z_copy_preserves_source(self, tmp_path):
        """7z archives should also respect --copy."""
        sz_path = _write(tmp_path / "src" / "book.7z")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(sz_path, kind="archive")

        actions = organize([item], cfg, copy=True)
        assert len(actions) == 1
        assert sz_path.exists()  # source preserved


class TestBatchTimestamp:
    def test_batch_ts_groups_undo(self, tmp_path):
        """Multiple organize calls with the same batch_ts should undo as one batch."""
        dest = tmp_path / "dest"
        log = tmp_path / "moves.log"
        cfg = Config(destination=dest, move_log=log)

        src1 = _write(tmp_path / "src1" / "book1.mp3")
        src2 = _write(tmp_path / "src2" / "book2.mp3")
        item1 = _scan_result(src1, title="Book One")
        item2 = _scan_result(src2, title="Book Two")

        batch_ts = "2025-01-01T00:00:00+00:00"
        organize([item1], cfg, batch_ts=batch_ts)
        organize([item2], cfg, batch_ts=batch_ts)

        # Both items should be in the same undo batch
        undone = undo_last(cfg)
        assert len(undone) == 2
        # After one undo call, both should be restored
        assert src1.exists()
        assert src2.exists()

    def test_without_batch_ts_separate_undo(self, tmp_path):
        """Without batch_ts, each organize call gets its own timestamp."""
        dest = tmp_path / "dest"
        log = tmp_path / "moves.log"
        cfg = Config(destination=dest, move_log=log)

        src1 = _write(tmp_path / "src1" / "book1.mp3")
        src2 = _write(tmp_path / "src2" / "book2.mp3")
        item1 = _scan_result(src1, title="Book One")
        item2 = _scan_result(src2, title="Book Two")

        # Organize separately without batch_ts — timestamps will differ
        organize([item1], cfg)
        import time

        time.sleep(0.01)  # ensure different timestamps
        organize([item2], cfg)

        # First undo should only restore the last item
        undone = undo_last(cfg)
        assert len(undone) == 1


class TestDirectoryDryRun:
    def test_directory_dry_run(self, tmp_path):
        """Dry run on directory organize should return dest but not move."""
        book_dir = tmp_path / "src" / "Author - Book"
        _write(book_dir / "track01.mp3")
        dest = tmp_path / "dest"
        cfg = Config(destination=dest, move_log=tmp_path / "log")
        item = _scan_result(book_dir, kind="audio_dir")

        actions = organize([item], cfg, dry_run=True)
        assert len(actions) == 1
        # Source should still exist (dry run)
        assert book_dir.exists()
        # Destination should NOT exist
        assert not dest.exists()


class TestZipSlipSymlink:
    def test_zip_with_symlink_entry_refused(self, tmp_path):
        """Zip files containing symlink entries should be refused entirely."""
        zip_path = tmp_path / "src" / "symlink.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            # Create a normal file
            zf.writestr("audio.mp3", b"\x00" * 100)
            # Create a symlink entry — external_attr >> 28 == 0xA
            info = zipfile.ZipInfo("evil_link")
            info.external_attr = 0xA0000000  # symlink
            zf.writestr(info, "/etc/passwd")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        actions = organize([item], cfg)
        assert len(actions) == 0
        assert zip_path.exists()  # original untouched

    def test_zip_with_absolute_path_refused(self, tmp_path):
        """Zip files with absolute member paths should be refused."""
        zip_path = tmp_path / "src" / "abs.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("/etc/evil.txt", b"pwned")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        actions = organize([item], cfg)
        assert len(actions) == 0
        assert not (Path("/etc/evil.txt")).exists()

    def test_zip_with_dotdot_segments_refused(self, tmp_path):
        """Zip members with '..' path segments should be refused."""
        zip_path = tmp_path / "src" / "dotdot.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("subdir/../../escape.txt", b"pwned")

        dest = tmp_path / "dest"
        cfg = Config(destination=dest, auto_extract=True, move_log=tmp_path / "log")
        item = _scan_result(zip_path, kind="archive")

        actions = organize([item], cfg)
        assert len(actions) == 0


class TestUndoLogTabsInPath:
    def test_undo_handles_tab_in_dest_path(self, tmp_path):
        """Log entries with tabs in destination path should parse correctly with maxsplit."""
        log = tmp_path / "moves.log"
        src = _write(tmp_path / "src" / "book.mp3")
        # Create dest with tab in name
        dest_dir = tmp_path / "dest" / "Author\tName"
        dest_file = _write(dest_dir / "book.mp3")

        # Write log entry where dest contains a tab
        log.write_text(f"2025-01-01T00:00:00+00:00\t{src}\t{dest_file}\n")
        cfg = Config(move_log=log)

        # undo_last should parse correctly with maxsplit=2
        undone = undo_last(cfg, dry_run=True)
        assert len(undone) == 1
        assert undone[0][0] == dest_file
        assert undone[0][1] == src
