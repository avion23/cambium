"""Compatibility wrapper for Cambium's OpenCode trajectory extractor.

The installed ``cambium optimize extract`` command uses
:mod:`cambium.opencode`.  This script remains available for existing offline
workflows and keeps its historical review-queue default.
"""

from __future__ import annotations

from collections.abc import Sequence

from cambium import opencode as _opencode
from cambium.opencode import (
    Candidate,
    DatabaseSummary,
    ExtractionResult,
    RawCandidate,
    dataset_stats,
    extract_candidates,
    extract_main,
    resolve_database_paths,
    write_dataset,
    write_records,
)

__all__ = [
    "Candidate",
    "DatabaseSummary",
    "ExtractionResult",
    "RawCandidate",
    "dataset_stats",
    "extract_candidates",
    "extract_main",
    "resolve_database_paths",
    "write_dataset",
    "write_records",
]


def __getattr__(name: str):
    return getattr(_opencode, name)


def main(argv: Sequence[str] | None = None) -> int:
    return _opencode.legacy_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
