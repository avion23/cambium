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


def _canonical_json(value: dict) -> str:
    """Serialize a dict canonically so equivalent dicts hash identically."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class EvalCache:
    """A bounded, opt-in cache for deterministic evaluation-harness calls.

    Cache identity (F6-15) is the full request as seen by the provider:
    provider name/revision, provider model, the exact prompt dict, and the
    sampling/request params that reach the provider. ``EvalCache.key`` hashes
    that identity with SHA-256 over a canonical JSON serialization, so a
    change in any component — provider revision, temperature, tool schema,
    prompt content — yields a distinct key and never reuses a response from
    a materially different request. This module keeps a static stdlib-only
    import boundary; only the eval harness and its tests construct
    ``EvalCache``.

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
    def key(
        prompt: dict,
        *,
        provider: str,
        model: str,
        params: dict | None = None,
    ) -> str:
        """Return a deterministic SHA-256 key for the full request identity.

        The identity is the complete request as seen by the provider:
        provider name/revision, provider model, the exact prompt dict, and
        the sampling/request params. Every component is serialized
        canonically, so a change in any of them (provider revision,
        temperature, tool schema, prompt content) produces a distinct key.
        """
        digest = hashlib.sha256()
        digest.update(provider.encode("utf-8"))
        digest.update(b"\0")
        digest.update(model.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_json(prompt).encode("utf-8"))
        if params is not None:
            digest.update(b"\0")
            digest.update(_canonical_json(params).encode("utf-8"))
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
