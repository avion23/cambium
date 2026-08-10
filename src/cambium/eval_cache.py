"""Opt-in disk cache for the frozen evaluation harness.

This cache belongs to the v2.1 eval harness (Ascensus/bench optimization loop)
only. Production worker and Diffundo paths do not import or construct
``EvalCache``; they remain cache-free by construction, as required by D1.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path


class EvalCache:
    """A bounded, opt-in cache for deterministic evaluation-harness calls.

    ``enabled`` defaults to ``False`` so merely constructing the helper does
    not activate disk caching. The eval harness must explicitly pass
    ``enabled=True``. Entries are JSON objects stored beneath a two-character
    key shard and are written with a temporary file followed by ``os.replace``.
    """

    def __init__(
        self,
        dir: Path,
        *,
        max_entries: int = 10_000,
        enabled: bool = False,
    ) -> None:
        if max_entries < 0:
            raise ValueError("max_entries must be non-negative")

        self.dir = Path(dir)
        self.max_entries = max_entries
        self.enabled = enabled
        self._lock = threading.RLock()

        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(prompt: dict, model: str) -> str:
        """Return a deterministic SHA-256 key for the complete prompt and model."""
        normalized_prompt = json.dumps(
            prompt,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = hashlib.sha256()
        digest.update(normalized_prompt.encode("utf-8"))
        digest.update(b"\0")
        digest.update(model.encode("utf-8"))
        return digest.hexdigest()

    def get(self, key: str) -> dict | None:
        """Read an entry, returning ``None`` for a miss or corrupt file."""
        if not self.enabled:
            return None

        path = self._entry_path(key)
        with self._lock:
            try:
                with path.open("r", encoding="utf-8") as handle:
                    result = json.load(handle)
            except (OSError, TypeError, UnicodeError, ValueError):
                return None

            if not isinstance(result, dict):
                return None

            try:
                os.utime(path, None)
            except OSError:
                pass
            return result

    def put(self, key: str, result: dict) -> None:
        """Atomically store an entry and prune older entries beyond the bound."""
        if not self.enabled:
            return

        payload = json.dumps(
            result,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        path = self._entry_path(key)

        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{key}.", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(payload)
                os.replace(temporary_name, path)
            finally:
                if fd != -1:
                    os.close(fd)
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

            self._prune()

    def _entry_path(self, key: str) -> Path:
        return self.dir / key[:2] / f"{key}.json"

    def _prune(self) -> None:
        try:
            paths = list(self.dir.rglob("*.json"))
        except OSError:
            return

        entries: list[tuple[int, str, Path]] = []
        for path in paths:
            try:
                entries.append((path.stat().st_mtime_ns, str(path), path))
            except OSError:
                continue

        entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        for _, _, path in entries[self.max_entries :]:
            try:
                path.unlink()
            except OSError:
                pass
