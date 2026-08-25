"""Canonical result boundaries for worker, parent, and session results.

Worker messages are not session results.  This module keeps the two wire
boundaries explicit:

* :func:`wire_to_child_result` creates the strict nine-key message that can
  travel from a child to its parent.
* :class:`Result` is the seventeen-field, root/session-level record.  Only a
  :class:`Result` can cross the JSON file boundary.

The functions in this module are deterministic apart from the optional
timestamp defaults in :func:`root_result_from_wire`.  They do not mutate
their input mappings and do not serialize arbitrary dataclasses or wire
messages.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

CHILD_RESULT_KEYS: tuple[str, ...] = (
    "parent_task_id",
    "unified_diff",
    "diff_truncated",
    "summary",
    "metric_score",
    "metric_breakdown",
    "commits",
    "files_changed",
    "status",
)

ROOT_RESULT_KEYS: tuple[str, ...] = (
    "status",
    "exit_code",
    "commits",
    "files_changed",
    "unified_diff",
    "diff_truncated",
    "summary",
    "provider",
    "fell_back_from",
    "metric_score",
    "metric_breakdown",
    "parent_task_id",
    "event_log_ref",
    "session_id",
    "started_at",
    "ended_at",
    "failure_reason",
)

EXIT_CODES: dict[str, int] = {
    "done": 0,
    "failed": 1,
    "rejected": 2,
    "timeout": 3,
    "cancelled": 4,
}

_MISSING = object()
_CANCEL_TOKENS = frozenset(
    {
        "cancel",
        "cancelled",
        "canceled",
        "cancellation",
        "aborted",
        "shutdown",
    }
)
_TIMEOUT_TOKENS = frozenset({"timeout", "timed_out", "watchdog_timeout"})
_TIMEOUT_REASON_MARKERS = (
    "timeout",
    "timed_out",
    "watchdog_timeout",
    "ready_timeout",
    "ping_no_pong",
)
_REJECT_TOKENS = frozenset(
    {
        "reject",
        "rejected",
        "evaluator_reject",
        "evaluator_rejected",
        "evaluator_rejection",
        "review_reject",
        "review_rejected",
        "review_rejection",
    }
)
_HARD_FAILURE_TOKENS = frozenset(
    {
        "crash",
        "crashed",
        "failed",
        "failure",
        "fatal",
        "fatal_error",
        "protocol",
        "protocol_error",
        "protocol_failed",
        "restart_exhausted",
        "restart_exhaust",
        "restarts_exhausted",
        "gate_failed",
        "merge_failed",
    }
)
_FAILURE_MARKERS = (
    "crash",
    "fatal",
    "protocol",
    "restart_exhaust",
    "max_restart",
    "gate_fail",
    "merge_fail",
)


def _token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _text(value: Any) -> str:
    if value is _MISSING:
        return ""
    if value is None or not isinstance(value, str):
        raise TypeError("summary must be a string")
    return value


def _wire_value(wire: Mapping[str, Any], key: str) -> Any:
    """Read a field from a wire mapping or its event-style payload.

    The payload is an input convenience only.  It is never copied wholesale
    into a child result.
    """
    value = wire.get(key, _MISSING)
    if value is not _MISSING:
        return value
    payload = wire.get("payload")
    if isinstance(payload, Mapping):
        return payload.get(key, _MISSING)
    return _MISSING


def _first_wire_value(wire: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = _wire_value(wire, key)
        if value is not _MISSING and value is not None:
            return value
    return _MISSING


def _flag(value: Any) -> bool | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0
    token = _token(value)
    if token in {"true", "yes", "ok", "pass", "passed", "success", "succeeded"}:
        return True
    if token in {"false", "no", "fail", "failed", "error", "reject", "rejected"}:
        return False
    return None


def _has_marker(value: Any, markers: Sequence[str]) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return any(marker in normalized for marker in markers)


def _signal_failed(value: Any) -> bool:
    """Return whether a merge verdict failed or is not understood."""
    if value is _MISSING or value is None:
        return False
    if isinstance(value, Mapping):
        value = _first_wire_value(value, ("status", "verdict", "ok", "passed", "exit_code"))
        if value is _MISSING:
            return True
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0
    flag = _flag(value)
    if flag is not None:
        return not flag
    token = _token(value)
    if token is None:
        return True
    return True


def _signal_rejected(value: Any) -> bool:
    """Return whether an evaluator verdict rejects or is not understood."""
    if value is _MISSING or value is None:
        return False
    if isinstance(value, Mapping):
        verdict = _first_wire_value(value, ("status", "verdict"))
        if verdict is not _MISSING:
            value = verdict
        else:
            rejected = _first_wire_value(value, ("rejected",))
            if rejected is not _MISSING:
                flag = _flag(rejected)
                return True if flag is None else flag is True
            ok = _first_wire_value(value, ("ok", "passed"))
            if ok is _MISSING:
                return True
            flag = _flag(ok)
            return True if flag is None else flag is False
    flag = _flag(value)
    if flag is not None:
        return not flag
    token = _token(value)
    if token in _REJECT_TOKENS or _has_marker(token, ("evaluator_reject", "review_reject")):
        return True
    return True


def _status_mapping(value: Mapping[str, Any] | str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        return {"status": value}
    raise TypeError("status input must be a wire mapping or status string")


def status_from_wire(
    wire_or_status: Mapping[str, Any] | str,
    *,
    merge_ok: bool | None = None,
    merge_status: Any = None,
    evaluator_rejected: bool | None = None,
    evaluator_status: Any = None,
) -> str:
    """Convert worker/supervisor outcome signals to a canonical status.

    A successful worker is ``done`` only when no merge failure is present.
    Supervisor failure classes are deliberately fail-closed and collapse to
    ``failed``.  Explicit evaluator rejection has priority over the worker and
    merge outcome because it is the terminal orchestration verdict.
    """
    wire = _status_mapping(wire_or_status)

    raw_status = _first_wire_value(
        wire, ("status", "worker_status", "supervisor_status", "outcome", "result_status")
    )
    raw_token = _token(raw_status)
    wire_type = _wire_value(wire, "type")

    evaluator_value = evaluator_status
    evaluator_explicit = evaluator_value is not None
    if evaluator_value is None:
        evaluator_value = _first_wire_value(
            wire,
            (
                "evaluator_status",
                "evaluator_verdict",
                "evaluation_status",
                "review_status",
                "review_verdict",
                "evaluator",
                "review",
            ),
        )
        evaluator_explicit = evaluator_value is not _MISSING
    if evaluator_value is _MISSING:
        evaluator_value = _first_wire_value(wire, ("failure_reason", "reason"))
    evaluator_rejected_value = _first_wire_value(
        wire, ("evaluator_rejected", "evaluation_rejected")
    )
    rejected = evaluator_rejected
    if rejected is None:
        rejected = _flag(evaluator_rejected_value)
    fallback_rejection = not evaluator_explicit and (
        _token(evaluator_value) in _REJECT_TOKENS
        or _has_marker(evaluator_value, ("evaluator_reject", "review_reject"))
    )
    unknown_rejection_flag = (
        evaluator_rejected_value is not _MISSING and _flag(evaluator_rejected_value) is None
    )
    if (
        rejected is True
        or unknown_rejection_flag
        or evaluator_explicit
        and _signal_rejected(evaluator_value)
        or fallback_rejection
    ):
        return "rejected"
    if (
        raw_token in _REJECT_TOKENS
        or _token(wire_type) in _REJECT_TOKENS
        or _has_marker(raw_status, ("evaluator_reject", "evaluator_rejection", "review_reject"))
        or _has_marker(wire_type, ("evaluator_reject", "review_reject"))
    ):
        return "rejected"

    failure_value = _first_wire_value(
        wire,
        (
            "failure_kind",
            "failure_reason",
            "error_type",
            "exit_reason",
            "termination_reason",
        ),
    )
    cancellation_flag = _first_wire_value(wire, ("cancelled", "canceled", "cancellation"))
    cancellation_value = _first_wire_value(
        wire,
        (
            "cancel_reason",
            "termination_reason",
            "failure_reason",
            "reason",
        ),
    )
    if (
        raw_token in _CANCEL_TOKENS
        or _token(wire_type) in _CANCEL_TOKENS
        or _has_marker(raw_status, ("cancel", "shutdown"))
        or _has_marker(wire_type, ("cancel", "shutdown"))
        or _flag(cancellation_flag) is True
        or _token(cancellation_flag) in _CANCEL_TOKENS
        or _has_marker(cancellation_flag, ("cancel", "shutdown"))
        or _token(cancellation_value) in _CANCEL_TOKENS
        or _has_marker(cancellation_value, ("cancel", "shutdown"))
    ):
        return "cancelled"

    reason_value = _first_wire_value(wire, ("reason",))
    timeout_value = _first_wire_value(
        wire, ("timeout", "timed_out", "watchdog_timeout", "timeout_phase")
    )
    if (
        raw_token in _TIMEOUT_TOKENS
        or _token(wire_type) in _TIMEOUT_TOKENS
        or _has_marker(raw_status, _TIMEOUT_REASON_MARKERS)
        or _has_marker(wire_type, _TIMEOUT_REASON_MARKERS)
        or _flag(timeout_value) is True
        or timeout_value is not _MISSING
        and timeout_value is not None
        and _token(timeout_value) in _TIMEOUT_TOKENS
        or _has_marker(timeout_value, _TIMEOUT_REASON_MARKERS)
        or _has_marker(failure_value, _TIMEOUT_REASON_MARKERS)
        or _has_marker(reason_value, _TIMEOUT_REASON_MARKERS)
    ):
        return "timeout"

    hard_failure = (
        raw_token in _HARD_FAILURE_TOKENS
        or _token(wire_type) in _HARD_FAILURE_TOKENS
        or _has_marker(raw_status, _FAILURE_MARKERS)
        or _has_marker(wire_type, _FAILURE_MARKERS)
        or _has_marker(failure_value, _FAILURE_MARKERS)
        or _flag(_first_wire_value(wire, ("crashed", "crash", "protocol_error"))) is True
        or _flag(
            _first_wire_value(
                wire, ("restart_exhausted", "restart_exhaust", "max_restarts_exceeded")
            )
        )
        is True
    )
    if hard_failure:
        return "failed"

    merge_value = merge_status
    if merge_value is None:
        merge_value = _first_wire_value(
            wire,
            (
                "merge_status",
                "merge_verdict",
                "merge_result",
                "merge_ok",
                "merge_succeeded",
                "merge",
            ),
        )
    merge_failed = (merge_ok is not None and merge_ok is not True) or _signal_failed(merge_value)
    if merge_failed:
        return "failed"

    if raw_token in {"done", "succeeded", "success", "complete", "completed"}:
        return "done"
    if raw_token in {"failed", "failure", "error"}:
        return "failed"

    # A missing or unknown terminal status is not proof of success.
    return "failed"


def _copy_sequence(value: Any) -> list[Any]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise TypeError("commits and files_changed must be sequences")
    items = list(value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError("commits and files_changed must contain strings")
    return items


def _final_bool(value: Any) -> bool:
    if value is _MISSING:
        return False
    if not isinstance(value, bool):
        raise TypeError("diff_truncated must be a boolean")
    return value


def _final_exit_code(value: Any) -> int | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("exit_code must be an integer")
    return value


def _metrics_from_wire(wire: Mapping[str, Any]) -> tuple[Any, Any]:
    score = _wire_value(wire, "metric_score")
    breakdown = _wire_value(wire, "metric_breakdown")
    metrics = _wire_value(wire, "metrics")
    if isinstance(metrics, Mapping):
        if score is _MISSING:
            score = metrics.get("metric_score", _MISSING)
        if breakdown is _MISSING:
            breakdown = metrics.get("metric_breakdown", _MISSING)
    if score is _MISSING:
        score = None
    if breakdown is _MISSING or breakdown is None:
        breakdown = {}
    if not isinstance(breakdown, Mapping):
        raise TypeError("metric_breakdown must be a mapping")
    return score, dict(breakdown)


def wire_to_child_result(
    wire: Mapping[str, Any],
    *,
    parent_task_id: str | None | object = _MISSING,
    include_diff: bool | None = None,
) -> dict[str, Any]:
    """Map a worker/supervisor wire mapping to the strict child envelope.

    Only :data:`CHILD_RESULT_KEYS` are emitted.  ``diff`` is the worker wire
    spelling for ``unified_diff``; ``include_diff=False`` (or an omitted diff)
    produces the required empty string while preserving the key.  An explicit
    ``None`` diff is rejected even when ``include_diff=False``: the field is
    always a string.
    """
    if not isinstance(wire, Mapping):
        raise TypeError("worker result must be a mapping")

    if parent_task_id is _MISSING:
        parent_task_id = _wire_value(wire, "parent_task_id")
        if parent_task_id is _MISSING:
            parent_task_id = None

    if include_diff is None:
        include_diff_value = _wire_value(wire, "include_diff")
        include_diff = _flag(include_diff_value) is not False

    diff = _wire_value(wire, "unified_diff")
    if diff is _MISSING:
        diff = _wire_value(wire, "diff")
    if diff is _MISSING:
        unified_diff = ""
    elif not isinstance(diff, str):
        raise TypeError("unified_diff must be a string")
    elif not include_diff:
        unified_diff = ""
    else:
        unified_diff = diff

    metric_score, metric_breakdown = _metrics_from_wire(wire)
    summary = _wire_value(wire, "summary")
    status = status_from_wire(wire)
    exit_code = _final_exit_code(_wire_value(wire, "exit_code"))
    if exit_code is not None and status == "done" and exit_code != 0:
        raise ValueError(f"worker exit_code {exit_code} does not match status {status!r}")
    values = {
        "parent_task_id": parent_task_id,
        "unified_diff": unified_diff,
        "diff_truncated": _final_bool(_wire_value(wire, "diff_truncated")),
        "summary": _text(summary),
        "metric_score": _unit_metric_score(metric_score),
        "metric_breakdown": _final_metric_breakdown(metric_breakdown),
        "commits": _copy_sequence(_wire_value(wire, "commits")),
        "files_changed": _copy_sequence(_wire_value(wire, "files_changed")),
        "status": status,
    }
    return {key: values[key] for key in CHILD_RESULT_KEYS}


def _final_metric_score(value: Any) -> float:
    if value is None or value is _MISSING:
        return 0.0
    if isinstance(value, bool):
        raise TypeError("metric_score must be numeric")
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("metric_score must be numeric") from exc
    if not math.isfinite(score):
        raise ValueError("metric_score must be finite")
    return score


def _final_timestamp(value: Any) -> float:
    if value is _MISSING or value is None:
        raise TypeError("timestamps must be numbers")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("timestamps must be numbers")
    timestamp = float(value)
    if not math.isfinite(timestamp):
        raise ValueError("timestamps must be finite")
    return timestamp


def _unit_metric_score(value: Any) -> float:
    """Coerce a metric score and enforce the public [0.0, 1.0] contract."""
    score = _final_metric_score(value)
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"metric_score must be in [0.0, 1.0], got {score!r}")
    return score


def _final_metric_breakdown(value: Any) -> dict[str, float]:
    if value is _MISSING or value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("metric_breakdown must be a mapping")
    result: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("metric_breakdown keys must be strings")
        result[key] = _unit_metric_score(item)
    return result


def _final_sequence(value: Any) -> tuple[str, ...]:
    if value is _MISSING or value is None:
        return ()
    if isinstance(value, str | bytes | bytearray) or not isinstance(value, Sequence):
        raise TypeError("commits and files_changed must be sequences")
    items = tuple(value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError("commits and files_changed must contain strings")
    return items


@dataclass(frozen=True, slots=True)
class Result:
    """The canonical seventeen-field root/session result.

    This is intentionally separate from ``TaskResult``, worker outcomes, and
    event records.  Its constructor enforces the root invariants so a caller
    cannot write a child or a process-level wrapper as ``result.json``.
    """

    status: str
    exit_code: int
    commits: tuple[str, ...]
    files_changed: tuple[str, ...]
    unified_diff: str
    diff_truncated: bool
    summary: str
    metric_score: float
    metric_breakdown: Mapping[str, float]
    parent_task_id: str | None
    event_log_ref: str
    session_id: str
    started_at: float
    ended_at: float
    failure_reason: str | None
    provider: str | None = None
    fell_back_from: str | None = None

    def __post_init__(self) -> None:
        status = _token(self.status)
        if status not in EXIT_CODES:
            raise ValueError(f"invalid canonical result status: {self.status!r}")
        if isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int):
            raise TypeError("exit_code must be an integer")
        expected_exit_code = EXIT_CODES[status]
        if self.exit_code != expected_exit_code:
            raise ValueError(
                f"exit_code {self.exit_code} does not match status {status!r} "
                f"({expected_exit_code})"
            )
        if self.parent_task_id is not None:
            raise ValueError("the root result parent_task_id must be None")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be an explicit non-empty string")
        if not isinstance(self.event_log_ref, str) or not self.event_log_ref:
            raise ValueError("event_log_ref must be a non-empty string")
        if not isinstance(self.unified_diff, str):
            raise TypeError("unified_diff must be a string")
        if not isinstance(self.summary, str):
            raise TypeError("summary must be a string")
        if self.failure_reason is not None and not isinstance(self.failure_reason, str):
            raise TypeError("failure_reason must be a string or None")
        for field_name, value in (
            ("provider", self.provider),
            ("fell_back_from", self.fell_back_from),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")
        if status == "done" and self.failure_reason is not None:
            raise ValueError("done results must not have a failure_reason")
        if not isinstance(self.diff_truncated, bool):
            raise TypeError("diff_truncated must be a boolean")
        started_at = _final_timestamp(self.started_at)
        ended_at = _final_timestamp(self.ended_at)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "commits", _final_sequence(self.commits))
        object.__setattr__(self, "files_changed", _final_sequence(self.files_changed))
        object.__setattr__(self, "diff_truncated", self.diff_truncated)
        object.__setattr__(self, "metric_score", _unit_metric_score(self.metric_score))
        object.__setattr__(
            self,
            "metric_breakdown",
            MappingProxyType(_final_metric_breakdown(self.metric_breakdown)),
        )
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "ended_at", ended_at)

    @classmethod
    def from_wire(
        cls,
        wire: Mapping[str, Any],
        session_dir: Path | str,
        *,
        session_id: str,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> Result:
        """Build a root result from one worker/supervisor wire mapping."""
        result = root_result_from_wire(
            wire,
            session_dir,
            session_id=session_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        if not isinstance(result, cls):
            raise TypeError("root result factory returned the wrong result type")
        return result

    def to_dict(self) -> dict[str, Any]:
        """Return the exact JSON-ready root record."""
        return result_to_dict(self)


def _timestamp_or_now(value: Any) -> float:
    if value is _MISSING or value is None:
        value = time.time()
    return _final_timestamp(value)


def _failure_reason(wire: Mapping[str, Any], status: str) -> str | None:
    if status == "done":
        return None
    for key in ("failure_reason", "reason", "failure_kind", "error_type"):
        value = _wire_value(wire, key)
        if isinstance(value, str) and value:
            return value
    return status


def _root_from_child(
    child: Mapping[str, Any],
    *,
    session_dir: Path | str,
    session_id: str,
    started_at: float | None,
    ended_at: float | None,
    failure_reason: str | None,
    provider: str | None = None,
    fell_back_from: str | None = None,
) -> Result:
    if not isinstance(child, Mapping):
        raise TypeError("child result must be a mapping")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be an explicit non-empty string")
    session_root = Path(session_dir).resolve()
    event_log_ref = f"sqlite:{session_root / '.cambium' / 'events.db'}"
    status = status_from_wire(child)
    unified_diff = child.get("unified_diff", "")
    if not isinstance(unified_diff, str):
        raise TypeError("unified_diff must be a string")
    exit_code = _final_exit_code(child.get("exit_code", _MISSING))
    if exit_code is not None and status == "done" and exit_code != 0:
        raise ValueError(f"exit_code {exit_code} does not match status {status!r}")
    return Result(
        status=status,
        exit_code=EXIT_CODES[status],
        commits=_final_sequence(child.get("commits", _MISSING)),
        files_changed=_final_sequence(child.get("files_changed", _MISSING)),
        unified_diff=unified_diff,
        diff_truncated=_final_bool(child.get("diff_truncated", _MISSING)),
        summary=_text(child.get("summary", "")),
        metric_score=_unit_metric_score(child.get("metric_score", None)),
        metric_breakdown=_final_metric_breakdown(child.get("metric_breakdown", {})),
        parent_task_id=None,
        event_log_ref=event_log_ref,
        session_id=session_id,
        started_at=_timestamp_or_now(started_at),
        ended_at=_timestamp_or_now(ended_at),
        failure_reason=(
            failure_reason if failure_reason is not None else _failure_reason(child, status)
        ),
        provider=provider,
        fell_back_from=fell_back_from,
    )


def root_result_from_wire(
    wire: Mapping[str, Any],
    session_dir: Path | str,
    *,
    session_id: str,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> Result:
    """Finalize a canonical root result from a worker/supervisor wire dict."""
    if not isinstance(wire, Mapping):
        raise TypeError("worker result must be a mapping")
    child = wire_to_child_result(wire)
    started = started_at if started_at is not None else _wire_value(wire, "started_at")
    ended = ended_at if ended_at is not None else _wire_value(wire, "ended_at")
    status = child["status"]
    metadata = wire.get("provider_metadata")
    if not isinstance(metadata, Mapping):
        metadata = wire
    provider = metadata.get("provider")
    fell_back_from = metadata.get("fell_back_from")
    return _root_from_child(
        child,
        session_dir=session_dir,
        session_id=session_id,
        started_at=started,
        ended_at=ended,
        failure_reason=_failure_reason(wire, status),
        provider=provider if isinstance(provider, str) else None,
        fell_back_from=fell_back_from if isinstance(fell_back_from, str) else None,
    )


def root_result_from_child(
    child: Mapping[str, Any],
    session_dir: Path | str,
    *,
    session_id: str,
    started_at: float | None = None,
    ended_at: float | None = None,
    failure_reason: str | None = None,
) -> Result:
    """Finalize a root result from an already restricted child mapping."""
    if not isinstance(child, Mapping):
        raise TypeError("child result must be a mapping")
    return _root_from_child(
        child,
        session_dir=session_dir,
        session_id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        failure_reason=failure_reason,
    )


def result_to_dict(result: Result) -> dict[str, Any]:
    """Serialize only a canonical :class:`Result` to its exact root keys."""
    if not isinstance(result, Result):
        raise TypeError("only a cambium.results.Result can be serialized")
    record = {key: getattr(result, key) for key in ROOT_RESULT_KEYS}
    record["commits"] = list(result.commits)
    record["files_changed"] = list(result.files_changed)
    record["metric_score"] = _unit_metric_score(result.metric_score)
    record["metric_breakdown"] = dict(_final_metric_breakdown(result.metric_breakdown))
    return record


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reject_symlink(path: Path, description: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValueError(f"cannot inspect {description}") from exc
    if stat.S_ISLNK(mode):
        raise ValueError(f"{description} must not be a symlink")


def _validate_event_log_ref(event_log_ref: str, session_dir: Path | str) -> None:
    prefix = "sqlite:"
    if not event_log_ref.startswith(prefix):
        raise ValueError("event_log_ref must use a sqlite: path")
    referenced_path = event_log_ref.removeprefix(prefix)
    if not referenced_path:
        raise ValueError("event_log_ref must identify the session events.db")
    state_dir = Path(session_dir) / ".cambium"
    _reject_symlink(state_dir, "the session .cambium directory")
    _reject_symlink(Path(referenced_path), "the event log")
    expected_path = (state_dir / "events.db").absolute()
    if Path(referenced_path).absolute() != expected_path:
        raise ValueError("result event_log_ref does not match the session events.db")


def write_result(
    result: Result,
    session_dir: Path | str,
    *,
    session_id: str,
) -> Path:
    """Atomically persist one canonical result as ``.cambium/result.json``.

    ``session_id`` is deliberately required even though it also exists on the
    dataclass.  The caller must make the session binding explicit and the
    writer rejects a mismatched result rather than silently rewriting it.
    """
    if not isinstance(result, Result):
        raise TypeError("only a cambium.results.Result can be written")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be an explicit non-empty string")
    if result.session_id != session_id:
        raise ValueError("result session_id does not match the explicit session_id")
    _validate_event_log_ref(result.event_log_ref, session_dir)

    state_dir = Path(session_dir) / ".cambium"
    _reject_symlink(state_dir, "the session .cambium directory")
    state_dir.mkdir(parents=True, exist_ok=True)
    _reject_symlink(state_dir, "the session .cambium directory")
    os.chmod(state_dir, 0o700)
    try:
        mode = os.lstat(state_dir).st_mode
    except OSError:
        raise
    if not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
        raise PermissionError("the session .cambium directory could not be made private")
    target = state_dir / "result.json"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=state_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                result_to_dict(result),
                stream,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(state_dir)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


__all__ = [
    "CHILD_RESULT_KEYS",
    "EXIT_CODES",
    "ROOT_RESULT_KEYS",
    "Result",
    "result_to_dict",
    "root_result_from_child",
    "root_result_from_wire",
    "status_from_wire",
    "wire_to_child_result",
    "write_result",
]
