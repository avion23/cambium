"""Durable, bounded dead-letter records for supervisor analysis.

The queue is stored below ``<session_dir>/.cambium/dlq``.  A record is written
to a temporary file, fsync'd, atomically renamed to its final name, and the
directory is fsync'd before :meth:`DeadLetterQueue.put` returns.  Queue
capacity is a keep-newest policy: when the count exceeds ``max_entries``, the
oldest files by modification time are removed.

The redaction module is optional while the v2.1 modules are being integrated.
When ``cambium.redact.Redactor`` is available, the complete record is passed
through its recursive ``redact_mapping`` method before it is serialized.  If
the module is absent, this compatibility path logs a warning and writes the
record unchanged.  No second copy of the canonical redaction patterns lives
here; production wiring must provide ``cambium.redact``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from .redact import Redactor
except ModuleNotFoundError as exc:
    if exc.name != "cambium.redact":
        raise
    Redactor = None  # type: ignore[assignment,misc]


__all__ = ["DeadLetterQueue"]

logger = logging.getLogger(__name__)

_JSON_SUFFIX = ".json"
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_COMPONENT_LENGTH = 96
_MISSING = "unknown"


class DeadLetterQueue:
    """A thread-safe, durable queue of failed supervisor records.

    ``directory`` is the session directory.  The queue owns only
    ``directory/.cambium/dlq`` and never retries records; consumers decide
    what supervisor action to take after inspection.
    """

    def __init__(
        self,
        dir: Path,
        *,
        max_entries: int = 1000,
        redactor: Redactor | None = None,
    ) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise TypeError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")

        self._directory = Path(dir) / ".cambium" / "dlq"
        self._directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._directory, 0o700)
        except OSError:
            pass
        for entry in self._directory.iterdir():
            if entry.is_file() and entry.name.endswith(_JSON_SUFFIX):
                try:
                    os.chmod(entry, 0o600)
                except OSError:
                    pass
        self._max_entries = max_entries
        self._lock = threading.RLock()
        if redactor is not None:
            self._redactor = redactor
        else:
            self._redactor = Redactor() if Redactor is not None else None
        if self._redactor is None:
            logger.warning(
                "cambium.redact is unavailable; dead-letter records are written without redaction"
            )

    @property
    def directory(self) -> Path:
        """Return the directory containing the queue files."""

        return self._directory

    def put(self, record: dict) -> Path:
        """Persist *record* and return its final path.

        The returned filename contains the task and generation identifiers,
        followed by a UUID4 hex component to make concurrent puts distinct.
        The input mapping is never modified.
        """

        if not isinstance(record, dict):
            raise TypeError("record must be a dict")

        with self._lock:
            persisted = self._redact(record)
            filename = self._filename(record)
            target = self._directory / filename
            self._write_atomically(target, persisted)
            self._prune_locked()
            return target

    def entries(self) -> list[dict]:
        """Return records ordered oldest-to-newest, each with its filename."""

        with self._lock:
            result: list[dict] = []
            for path in self._ordered_paths_locked():
                try:
                    record = self._read(path)
                except FileNotFoundError:
                    continue
                record["file"] = path.name
                result.append(record)
            return result

    def get(self, name: str | Path) -> dict:
        """Read one queue record by its filename."""

        with self._lock:
            return self._read(self._path_for_name(name))

    def remove(self, name: str | Path) -> None:
        """Remove one record by filename; removing an absent record is harmless."""

        with self._lock:
            path = self._path_for_name(name)
            try:
                path.unlink()
            except FileNotFoundError:
                return
            self._fsync_directory()

    def summarize(self) -> dict[str, Any]:
        """Count records by terminal status and supervisor failure reason.

        Missing ``status`` or ``reason`` values are counted as ``"unknown"``.
        If ``reason`` is absent, ``failure_kind`` is used when present.  The
        summary is a snapshot; it does not change queue contents.
        """

        status_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        for record in self.entries():
            status_counts[_label(record.get("status"))] += 1
            reason = record.get("reason")
            if reason is None:
                reason = record.get("failure_kind")
            reason_counts[_label(reason)] += 1
        return {
            "total": sum(status_counts.values()),
            "by_status": dict(status_counts),
            "by_reason": dict(reason_counts),
        }

    def _redact(self, record: dict) -> dict:
        if self._redactor is None:
            return dict(record)
        return self._redactor.redact_mapping(record)

    def _filename(self, record: dict) -> str:
        task_id = _safe_component(record.get("task_id", _MISSING))
        generation = _safe_component(record.get("generation", _MISSING))
        return f"{task_id}-{generation}-{uuid.uuid4().hex}{_JSON_SUFFIX}"

    def _write_atomically(self, target: Path, record: dict) -> None:
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        temporary = self._directory / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            stream = None
            try:
                stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            finally:
                if stream is not None:
                    stream.close()
                else:
                    os.close(fd)
            os.replace(temporary, target)
            self._fsync_directory()
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _fsync_directory(self) -> None:
        fd = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _prune_locked(self) -> None:
        paths = self._ordered_paths_locked()
        excess = len(paths) - self._max_entries
        if excess <= 0:
            return
        for path in paths[:excess]:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
        self._fsync_directory()

    def _ordered_paths_locked(self) -> list[Path]:
        ordered: list[tuple[int, str, Path]] = []
        for path in self._directory.iterdir():
            if not path.is_file() or path.suffix != _JSON_SUFFIX:
                continue
            try:
                modified_at = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            ordered.append((modified_at, path.name, path))
        ordered.sort(key=lambda entry: (entry[0], entry[1]))
        return [entry[2] for entry in ordered]

    def _path_for_name(self, name: str | Path) -> Path:
        filename = Path(name).name
        if str(name) != filename or not filename.endswith(_JSON_SUFFIX):
            raise ValueError("name must be a .json filename from the dead-letter queue")
        return self._directory / filename

    @staticmethod
    def _read(path: Path) -> dict:
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if not isinstance(record, dict):
            raise ValueError(f"dead-letter record is not an object: {path.name}")
        return record


def _safe_component(value: object) -> str:
    component = _SAFE_COMPONENT.sub("_", str(value))
    component = component.strip(".-_")[:_MAX_COMPONENT_LENGTH]
    return component or _MISSING


def _label(value: object) -> str:
    if value is None:
        return _MISSING
    if isinstance(value, str):
        return value or _MISSING
    return str(value)
