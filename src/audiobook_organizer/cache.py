"""Cache scan results to avoid re-scanning unchanged files and directories."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from .parser import AudiobookMeta
from .scanner import ScanResult

CACHE_VERSION = 1
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
            raw = json.loads(self.path.read_text())
            if raw.get("version") == CACHE_VERSION:
                self._entries = raw.get("entries", {})
        except (json.JSONDecodeError, OSError):
            self._entries = {}

    def save(self) -> None:
        """Write cache to disk (only if changed)."""
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "entries": self._entries}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        tmp.replace(self.path)
        self._dirty = False

    # ── lookup / store ───────────────────────────────────────────────

    def get(self, path: Path) -> ScanResult | None:
        """Return a cached ScanResult if *path* hasn't changed, else None."""
        key = str(path)
        entry = self._entries.get(key)
        if entry is None:
            return None

        fp = _fingerprint(path)
        if fp is None or fp != entry.get("fp"):
            return None

        return _deserialize(entry["result"])

    def put(self, path: Path, result: ScanResult) -> None:
        """Store *result* for *path* with the current filesystem fingerprint."""
        fp = _fingerprint(path)
        if fp is None:
            return
        self._entries[str(path)] = {
            "fp": fp,
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
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            try:
                fst = fpath.stat()
                h.update(
                    f"{fpath}:{fst.st_mtime_ns}:{fst.st_size}\n".encode("utf-8", "surrogateescape")
                )
            except OSError:
                pass
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
    }
    if result.tag_meta is not None:
        tm = asdict(result.tag_meta)
        tm["source_path"] = str(tm["source_path"]) if tm["source_path"] else None
        d["tag_meta"] = tm
    return d


def _deserialize(data: dict) -> ScanResult:
    meta_d = {**data["meta"]}
    sp = meta_d.pop("source_path", None)
    meta = AudiobookMeta(**meta_d)
    meta.source_path = Path(sp) if sp else None

    tag_meta = None
    if "tag_meta" in data:
        tm_d = {**data["tag_meta"]}
        tm_sp = tm_d.pop("source_path", None)
        tag_meta = AudiobookMeta(**tm_d)
        tag_meta.source_path = Path(tm_sp) if tm_sp else None

    return ScanResult(
        path=Path(data["path"]),
        kind=data["kind"],
        meta=meta,
        size=data["size"],
        has_cover=data.get("has_cover", False),
        file_count=data.get("file_count", 0),
        tag_meta=tag_meta,
    )
