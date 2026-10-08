"""Cache scan results to avoid re-scanning unchanged files and directories."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from .parser import AudiobookMeta
from .persistence import atomic_write_text
from .scanner import ScanResult

CACHE_VERSION = 2
DEFAULT_CACHE_PATH = Path("~/.aborg/cache.json").expanduser()


class ScanCache:
    """Persistent cache of scan results keyed by path + filesystem fingerprint."""

    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_CACHE_PATH
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # ── persistence ──────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("version") == CACHE_VERSION:
                entries = raw.get("entries", {})
                if isinstance(entries, dict):
                    self._entries = {
                        key: value for key, value in entries.items() if isinstance(value, dict)
                    }
        except (UnicodeError, json.JSONDecodeError, OSError, TypeError):
            self._entries = {}

    def save(self) -> None:
        """Write cache to disk (only if changed)."""
        if not self._dirty:
            return
        payload = {"version": CACHE_VERSION, "entries": self._entries}
        atomic_write_text(self.path, json.dumps(payload, separators=(",", ":")))
        self._dirty = False

    # ── lookup / store ───────────────────────────────────────────────

    def get(self, path: Path, *, context: str = "") -> ScanResult | None:
        """Return a cached ScanResult if *path* hasn't changed, else None."""
        key = str(path)
        entry = self._entries.get(key)
        if entry is None:
            return None

        fp = _fingerprint(path)
        if fp is None or fp != entry.get("fp") or context != entry.get("context", ""):
            return None

        try:
            result = _deserialize(entry["result"])
            if result.path.resolve() != path.resolve():
                raise ValueError("cached result has a different source path")
            if any(
                not source.resolve().is_relative_to(path.resolve())
                for source in result.source_files
            ):
                raise ValueError("cached source file is outside its source directory")
            return result
        except (KeyError, TypeError, ValueError):
            # Corrupt cache entry — discard it silently
            del self._entries[key]
            self._dirty = True
            return None

    def put(self, path: Path, result: ScanResult, *, context: str = "") -> None:
        """Store *result* for *path* with the current filesystem fingerprint."""
        fp = _fingerprint(path)
        if fp is None:
            return
        self._entries[str(path)] = {
            "fp": fp,
            "context": context,
            "result": _serialize(result),
        }
        self._dirty = True

    def prune(self) -> int:
        """Remove entries whose paths no longer exist. Returns count removed."""
        stale = [k for k in self._entries if not Path(k).exists()]
        for k in stale:
            del self._entries[k]
        if stale:
            self._dirty = True
        return len(stale)

    def clear(self) -> None:
        """Drop all entries."""
        if self._entries:
            self._entries.clear()
            self._dirty = True

    @property
    def size(self) -> int:
        return len(self._entries)


# ── fingerprinting ───────────────────────────────────────────────────────


def _fingerprint(path: Path) -> str | None:
    """Compute a quick fingerprint for *path* based on filesystem metadata.

    Files: ``f:<mtime>:<size>``
    Directories: SHA-1 of sorted ``(name, mtime, size)`` for all children.
    """
    try:
        st = path.stat()
    except OSError:
        return None

    if path.is_file():
        return f"f:{st.st_mtime_ns}:{st.st_size}"

    # Directory — build a content fingerprint from the recursive listing.
    # This catches added/removed/renamed/modified files anywhere inside.
    h = hashlib.sha1(usedforsecurity=False)

    def walk_error(error: OSError) -> None:
        raise error

    try:
        for dirpath, dirnames, filenames in os.walk(path, onerror=walk_error):
            dirnames.sort()
            h.update(f"{dirpath}:{dirnames}\n".encode("utf-8", "surrogateescape"))
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                fst = fpath.stat()
                h.update(
                    f"{fpath}:{fst.st_mtime_ns}:{fst.st_size}\n".encode("utf-8", "surrogateescape")
                )
    except OSError:
        return None

    return f"d:{h.hexdigest()}"


# ── serialization ────────────────────────────────────────────────────────


def _serialize(result: ScanResult) -> dict:
    meta = asdict(result.meta)
    meta["source_path"] = str(meta["source_path"]) if meta["source_path"] else None
    d = {
        "path": str(result.path),
        "kind": result.kind,
        "meta": meta,
        "size": result.size,
        "has_cover": result.has_cover,
        "file_count": result.file_count,
        "source_files": [str(path) for path in result.source_files],
    }
    if result.tag_meta is not None:
        tm = asdict(result.tag_meta)
        tm["source_path"] = str(tm["source_path"]) if tm["source_path"] else None
        d["tag_meta"] = tm
    return d


def _metadata(data: dict) -> AudiobookMeta:
    if not isinstance(data, dict):
        raise ValueError("metadata must be an object")
    fields = dict(data)
    source_path = fields.pop("source_path", None)
    if source_path is not None and not isinstance(source_path, str):
        raise ValueError("metadata source path must be a string")
    for key, value in fields.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f"metadata {key} must be a string")
    for key in ("author", "title"):
        if not isinstance(fields.get(key), str) or not fields[key]:
            raise ValueError(f"metadata {key} must be a nonempty string")
    return AudiobookMeta(**fields, source_path=Path(source_path) if source_path else None)


def _deserialize(data: dict) -> ScanResult:
    if not isinstance(data, dict):
        raise ValueError("cached result must be an object")
    if not isinstance(data["path"], str) or not data["path"]:
        raise ValueError("cached path must be a nonempty string")
    if data["kind"] not in {"audio_file", "audio_dir", "audio_group", "archive"}:
        raise ValueError("unknown cached item kind")
    for key in ("size", "file_count"):
        value = data.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError(f"cached {key} must be a nonnegative integer")
    if type(data.get("has_cover", False)) is not bool:
        raise ValueError("cached has_cover must be a boolean")
    sources = data.get("source_files", [])
    if not isinstance(sources, list) or any(
        not isinstance(path, str) or not path for path in sources
    ):
        raise ValueError("cached source_files must be a list of paths")
    return ScanResult(
        path=Path(data["path"]),
        kind=data["kind"],
        meta=_metadata(data["meta"]),
        size=data["size"],
        has_cover=data.get("has_cover", False),
        file_count=data.get("file_count", 0),
        tag_meta=_metadata(data["tag_meta"]) if "tag_meta" in data else None,
        source_files=tuple(Path(path) for path in sources),
    )
