"""Test source verification and deletion boundaries."""

import shutil
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from audiobook_organizer.cleanup import delete_sources, verify_source
from audiobook_organizer.config import Config


@pytest.fixture()
def collection(tmp_path):
    source = tmp_path / "sources"
    stored = tmp_path / "library"
    source.mkdir()
    stored.mkdir()
    return Config(source_dirs=[source], destination=stored)


def write(path, data=b"complete audiobook"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_link(path, target):
    try:
        path.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("The system does not permit symbolic links.")


def test_verified_file_is_deleted_once(collection):
    source = write(collection.source_dirs[0] / "book.mp3")
    destination = write(collection.destination / source.name)
    result = delete_sources([source, source], collection, destinations={source: destination})
    assert result.removed == 1
    assert result.failed == 0
    assert not source.exists()
    assert destination.read_bytes() == b"complete audiobook"


@pytest.mark.parametrize("data", [b"different content!", b"short"])
def test_different_stored_data_preserves_source(collection, data):
    source = write(collection.source_dirs[0] / "book.mp3")
    destination = write(collection.destination / source.name, data)
    result = delete_sources([source], collection, destinations={source: destination})
    assert result.failed == 1
    assert source.read_bytes() == b"complete audiobook"


def test_missing_destination_information_preserves_source(collection):
    source = write(collection.source_dirs[0] / "book.mp3")
    assert delete_sources([source], collection).failed == 1
    assert source.exists()


def test_incomplete_directory_preserves_all_source_files(collection):
    source = collection.source_dirs[0] / "Book"
    write(source / "one.mp3")
    write(source / "nested" / "two.mp3")
    destination = collection.destination / "Book"
    write(destination / "one.mp3")
    result = delete_sources([source], collection, destinations={source: destination})
    assert result.failed == 1
    assert (source / "one.mp3").exists()
    assert (source / "nested" / "two.mp3").exists()


def test_complete_directory_can_have_extra_stored_files(collection):
    source = collection.source_dirs[0] / "Book"
    write(source / "nested" / "one.mp3")
    destination = collection.destination / "Book"
    shutil.copytree(source, destination)
    write(destination / "cover.jpg")
    result = delete_sources([source], collection, destinations={source: destination})
    assert result.removed == 1
    assert not source.exists()
    assert (destination / "nested" / "one.mp3").exists()


@pytest.mark.parametrize("complete", [True, False])
def test_zip_cleanup_checks_all_extracted_data(collection, complete):
    source = collection.source_dirs[0] / "Book.zip"
    destination = collection.destination / "Book"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("audio/one.mp3", b"chapter one")
        archive.writestr("audio/two.mp3", b"chapter two")
    write(destination / "audio" / "one.mp3", b"chapter one")
    if complete:
        write(destination / "audio" / "two.mp3", b"chapter two")
    result = delete_sources([source], collection, destinations={source: destination})
    assert result.removed == int(complete)
    assert result.failed == int(not complete)
    assert source.exists() is not complete


@pytest.mark.parametrize("member", ["../book.mp3", "/book.mp3", "C:/book.mp3", "..\\book.mp3"])
def test_unsafe_archive_is_kept(collection, member):
    source = collection.source_dirs[0] / "Book.zip"
    destination = collection.destination / "Book"
    destination.mkdir()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(member, b"audio")
    result = delete_sources([source], collection, destinations={source: destination})
    assert result.failed == 1
    assert source.exists()


def test_empty_zip_is_kept(collection):
    source = collection.source_dirs[0] / "Book.zip"
    with zipfile.ZipFile(source, "w"):
        pass
    destination = collection.destination / "Book"
    destination.mkdir()
    assert delete_sources([source], collection, destinations={source: destination}).failed == 1
    assert source.exists()


def test_zip_contents_must_match_even_when_sizes_match(collection):
    source = collection.source_dirs[0] / "Book.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("book.mp3", b"original")
    destination = collection.destination / "Book"
    write(destination / "book.mp3", b"modified")
    assert delete_sources([source], collection, destinations={source: destination}).failed == 1
    assert source.exists()


def test_source_outside_configured_roots_is_kept(collection, tmp_path):
    source = write(tmp_path / "unrelated" / "book.mp3")
    destination = write(collection.destination / source.name)
    assert delete_sources([source], collection, destinations={source: destination}).skipped == 1
    assert source.exists()


def test_protected_roots_and_library_are_kept(collection):
    roots = [collection.source_dirs[0], collection.source_dirs[0].parent, collection.destination]
    result = delete_sources(roots, collection)
    assert result.skipped == 3
    assert all(path.is_dir() for path in roots)


def test_empty_directory_cleanup_rechecks_emptiness(collection):
    source = collection.source_dirs[0] / "was-empty"
    write(source / "new-download.mp3")
    result = delete_sources([source], collection, empty_dirs={source})
    assert result.failed == 1
    assert (source / "new-download.mp3").exists()


def test_empty_parent_directories_are_removed_deepest_first(collection):
    parent = collection.source_dirs[0] / "empty"
    child = parent / "nested"
    child.mkdir(parents=True)
    result = delete_sources([parent, child], collection, empty_dirs={parent, child})
    assert result.removed == 2
    assert not parent.exists()
    assert collection.source_dirs[0].exists()


def test_destination_outside_library_is_not_verification_evidence(collection, tmp_path):
    source = write(collection.source_dirs[0] / "book.mp3")
    destination = write(tmp_path / "elsewhere" / source.name)
    assert delete_sources([source], collection, destinations={source: destination}).failed == 1
    assert source.exists()


def test_destination_link_is_not_verification_evidence(collection, tmp_path):
    source = write(collection.source_dirs[0] / "book.mp3")
    target = write(tmp_path / "outside.mp3")
    destination = collection.destination / source.name
    make_link(destination, target)
    assert delete_sources([source], collection, destinations={source: destination}).failed == 1
    assert source.exists() and target.exists()


def test_source_link_is_kept(collection):
    original = write(collection.source_dirs[0] / "original.mp3")
    source = collection.source_dirs[0] / "link.mp3"
    make_link(source, original)
    destination = write(collection.destination / source.name)
    assert delete_sources([source], collection, destinations={source: destination}).failed == 1
    assert source.is_symlink() and original.exists()


def test_directory_with_a_link_is_kept(collection, tmp_path):
    source = collection.source_dirs[0] / "Book"
    source.mkdir()
    target = write(tmp_path / "outside.mp3")
    make_link(source / "linked.mp3", target)
    destination = collection.destination / "Book"
    destination.mkdir()
    assert delete_sources([source], collection, destinations={source: destination}).failed == 1
    assert source.exists() and target.exists()


def test_source_change_during_verification_is_rejected(collection):
    source = write(collection.source_dirs[0] / "book.mp3")
    destination = write(collection.destination / source.name)

    def change_source(_left, _right):
        source.write_bytes(b"new source data")
        return True

    with (
        patch("audiobook_organizer.cleanup._same_streams", side_effect=change_source),
        pytest.raises(ValueError, match="source changed"),
    ):
        verify_source(source, destination, collection.destination)
    assert source.read_bytes() == b"new source data"


def test_large_file_verification_checks_final_block(collection):
    source = write(collection.source_dirs[0] / "book.mp3", b"0" * 1_048_576 + b"a")
    destination = write(collection.destination / source.name, b"0" * 1_048_576 + b"b")
    assert delete_sources([source], collection, destinations={source: destination}).failed == 1
    assert source.exists()


def test_dry_run_verifies_without_deletion(collection):
    source = write(collection.source_dirs[0] / "book.mp3")
    destination = write(collection.destination / source.name)
    result = delete_sources([source], collection, destinations={source: destination}, dry_run=True)
    assert result.removed == 1
    assert source.exists() and destination.exists()


def test_destination_change_after_first_file_is_still_rejected(collection):
    source = collection.source_dirs[0] / "Book"
    write(source / "one.mp3")
    write(source / "two.mp3")
    destination = collection.destination / "Book"
    shutil.copytree(source, destination)
    from audiobook_organizer.cleanup import _same_streams

    seen = []

    def change_previous(left, right):
        same = _same_streams(left, right)
        seen.append(Path(right.name))
        if len(seen) == 2:
            seen[0].write_bytes(b"changed stored data")
        return same

    with patch("audiobook_organizer.cleanup._same_streams", side_effect=change_previous):
        result = delete_sources([source], collection, destinations={source: destination})
    assert result.failed == 1
    assert (source / "one.mp3").exists() and (source / "two.mp3").exists()
