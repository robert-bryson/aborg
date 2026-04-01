"""Tests for audiobook_organizer.cache — scan result caching."""

from pathlib import Path

from audiobook_organizer.cache import ScanCache, _fingerprint
from audiobook_organizer.parser import AudiobookMeta
from audiobook_organizer.scanner import ScanResult


def _make_audio_file(path: Path, size: int = 2_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)


def _make_result(path: Path, **kwargs) -> ScanResult:
    defaults = dict(
        path=path,
        kind="audio_dir",
        meta=AudiobookMeta(author="Author", title="Title", source_path=path),
        size=2_000_000,
        has_cover=False,
        file_count=1,
    )
    defaults.update(kwargs)
    return ScanResult(**defaults)


class TestFingerprint:
    def test_file_fingerprint(self, tmp_path):
        f = tmp_path / "audio.mp3"
        f.write_bytes(b"\x00" * 100)
        fp = _fingerprint(f)
        assert fp is not None
        assert fp.startswith("f:")

    def test_file_fingerprint_changes_on_modify(self, tmp_path):
        f = tmp_path / "audio.mp3"
        f.write_bytes(b"\x00" * 100)
        fp1 = _fingerprint(f)
        f.write_bytes(b"\x00" * 200)
        fp2 = _fingerprint(f)
        assert fp1 != fp2

    def test_dir_fingerprint(self, tmp_path):
        d = tmp_path / "book"
        _make_audio_file(d / "track.mp3")
        fp = _fingerprint(d)
        assert fp is not None
        assert fp.startswith("d:")

    def test_dir_fingerprint_changes_on_add(self, tmp_path):
        d = tmp_path / "book"
        _make_audio_file(d / "track1.mp3")
        fp1 = _fingerprint(d)
        _make_audio_file(d / "track2.mp3")
        fp2 = _fingerprint(d)
        assert fp1 != fp2

    def test_missing_path_returns_none(self, tmp_path):
        assert _fingerprint(tmp_path / "nope") is None


class TestScanCache:
    def test_put_and_get(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        audio = tmp_path / "Author - Title.mp3"
        audio.write_bytes(b"\x00" * 100)

        cache = ScanCache(cache_file)
        result = _make_result(audio, kind="audio_file")
        cache.put(audio, result)

        got = cache.get(audio)
        assert got is not None
        assert got.meta.author == "Author"
        assert got.meta.title == "Title"
        assert got.kind == "audio_file"

    def test_cache_miss_on_modify(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        audio = tmp_path / "file.mp3"
        audio.write_bytes(b"\x00" * 100)

        cache = ScanCache(cache_file)
        cache.put(audio, _make_result(audio))

        # Modify the file
        audio.write_bytes(b"\x00" * 200)
        assert cache.get(audio) is None

    def test_cache_miss_for_unknown(self, tmp_path):
        cache = ScanCache(tmp_path / "cache.json")
        assert cache.get(tmp_path / "nonexistent.mp3") is None

    def test_persistence(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        audio = tmp_path / "file.mp3"
        audio.write_bytes(b"\x00" * 100)

        cache1 = ScanCache(cache_file)
        cache1.put(audio, _make_result(audio))
        cache1.save()

        cache2 = ScanCache(cache_file)
        got = cache2.get(audio)
        assert got is not None
        assert got.meta.author == "Author"

    def test_prune_removes_stale(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        audio = tmp_path / "file.mp3"
        audio.write_bytes(b"\x00" * 100)

        cache = ScanCache(cache_file)
        cache.put(audio, _make_result(audio))
        assert cache.size == 1

        audio.unlink()
        removed = cache.prune()
        assert removed == 1
        assert cache.size == 0

    def test_clear(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        audio = tmp_path / "file.mp3"
        audio.write_bytes(b"\x00" * 100)

        cache = ScanCache(cache_file)
        cache.put(audio, _make_result(audio))
        cache.clear()
        assert cache.size == 0

    def test_save_noop_when_clean(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache = ScanCache(cache_file)
        cache.save()
        assert not cache_file.exists()

    def test_dir_caching(self, tmp_path):
        cache_file = tmp_path / "cache" / "cache.json"
        book = tmp_path / "Author" / "Book"
        _make_audio_file(book / "track.mp3")

        cache = ScanCache(cache_file)
        result = _make_result(book, has_cover=True, file_count=1)
        cache.put(book, result)

        got = cache.get(book)
        assert got is not None
        assert got.has_cover is True
        assert got.file_count == 1

    def test_serializes_all_meta_fields(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        audio = tmp_path / "file.mp3"
        audio.write_bytes(b"\x00" * 100)

        meta = AudiobookMeta(
            author="Author",
            title="Title",
            series="Series",
            sequence="3",
            year="2024",
            narrator="Narrator",
            source_path=audio,
        )
        result = ScanResult(
            path=audio, kind="audio_file", meta=meta, size=100, has_cover=True, file_count=0,
        )

        cache = ScanCache(cache_file)
        cache.put(audio, result)
        cache.save()

        cache2 = ScanCache(cache_file)
        got = cache2.get(audio)
        assert got is not None
        assert got.meta.series == "Series"
        assert got.meta.sequence == "3"
        assert got.meta.year == "2024"
        assert got.meta.narrator == "Narrator"
        assert got.meta.source_path == audio
