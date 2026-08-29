"""Deterministic failure attribution from Cambium's durable session evidence.

Only the production event stores and bounded checkpoint projections are read;
provider or transcript text is never emitted.

Run::

    python scripts/failure_attribution.py SESSION_DIR [SESSION_DIR ...]
    python scripts/failure_attribution.py --json SESSION_DIR
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cambium.ipc import MAX_LINE_BYTES  # noqa: E402
from cambium.store import (  # noqa: E402
    MAX_EVENT_ROWS_PER_READ,
    StoreError,
    count_events_file,
    read_events_file,
)

_TURN_RE = re.compile(r"^turn-(\d+)$")
_SUCCESS_STATUSES = frozenset({"succeeded"})
_TOOL_KINDS = frozenset({"tool_event"})
_TERMINAL_KINDS = frozenset({"result"})
_FAILURE_KINDS = frozenset({"worker_failed", "task_failed", "timeout"})
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_DETECTOR_ORDER = (
    "retry-loop",
    "finish-without-verification",
    "objective_met-override",
    "read-churn",
    "compaction-stall",
)

# These are the same practical bounds used by the production readers/writers.
_MAX_CHECKPOINT_BYTES = MAX_LINE_BYTES * 4
_MAX_CHECKPOINT_FILES = 10_000
_MIN_READ_EVENTS = 4
_MIN_DISTINCT_READ_UNITS = 4


class SessionPathError(ValueError):
    """The command-line target is not a selectable session path."""


class EventReadError(OSError):
    """A selected event store could not be read safely."""


def _session_root(session: Path | str) -> tuple[Path, Path | None]:
    """Return the resolved session root and an explicitly selected DB, if any."""
    requested = Path(session).expanduser()
    try:
        exists = requested.exists()
    except OSError as exc:
        raise SessionPathError(f"cannot inspect session path {requested}: {exc}") from exc
    if not exists:
        raise SessionPathError(f"session path does not exist: {requested}")

    if requested.is_symlink() and requested.is_file():
        raise SessionPathError(f"event store path must not be a symlink: {requested}")
    if requested.is_file():
        if requested.name != "events.db":
            raise SessionPathError(f"session file must be named events.db: {requested}")
        root = (
            requested.parent.parent.resolve()
            if requested.parent.name == ".cambium"
            else requested.parent.resolve()
        )
        return root, requested.resolve()
    if not requested.is_dir():
        raise SessionPathError(f"session path is not a directory or events.db: {requested}")
    return requested.resolve(), None


def _path_has_symlink(path: Path, root: Path) -> bool:
    """Check path components without following a symlink out of ``root``."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
        except OSError:
            return True
    return False


def _source_label(root: Path, database: Path) -> str:
    relative = database.relative_to(root)
    if relative.parts and _TURN_RE.fullmatch(relative.parts[0]):
        return relative.parts[0]
    return "root"


def _store_root(root: Path, database: Path) -> Path:
    relative = database.relative_to(root)
    if relative.parts and _TURN_RE.fullmatch(relative.parts[0]):
        return root / relative.parts[0]
    return root


def _outer_turn(root: Path, database: Path) -> int | None:
    relative = database.relative_to(root)
    if relative.parts:
        match = _TURN_RE.fullmatch(relative.parts[0])
        if match:
            return int(match.group(1))
    return None


def _discover_databases(  # noqa: C901
    root: Path, selected: Path | None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Enumerate only the four documented event-store layouts."""
    issues: list[str] = []
    candidates: list[Path] = []

    def add(path: Path) -> None:
        if _path_has_symlink(path, root):
            issues.append(f"event store path uses a symlink: {_source_label(root, path)}")
            return
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        except OSError as exc:
            issues.append(f"event store path cannot be inspected: {exc}")
            return
        if stat.S_ISLNK(mode):
            issues.append(f"event store path uses a symlink: {_source_label(root, path)}")
        elif stat.S_ISREG(mode):
            candidates.append(path)
        else:
            issues.append(f"event store path is not a regular file: {path.name}")

    if selected is not None:
        add(selected)
    else:
        # The canonical state DB comes first; the direct archived spelling is
        # retained because it is a documented old-session layout.
        add(root / ".cambium" / "events.db")
        add(root / "events.db")
        try:
            children = sorted(root.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            raise EventReadError(f"cannot enumerate session directory {root}: {exc}") from exc
        turns: list[tuple[int, Path]] = []
        for child in children:
            match = _TURN_RE.fullmatch(child.name)
            if match is None:
                continue
            try:
                mode = child.lstat().st_mode
            except OSError as exc:
                issues.append(f"cannot inspect {child.name}: {exc}")
                continue
            if stat.S_ISLNK(mode):
                issues.append(f"turn store directory uses a symlink: {child.name}")
                continue
            if not stat.S_ISDIR(mode):
                issues.append(f"turn store is not a directory: {child.name}")
                continue
            turns.append((int(match.group(1)), child))
        for _turn, turn_dir in sorted(turns, key=lambda item: item[0]):
            add(turn_dir / ".cambium" / "events.db")
            add(turn_dir / "events.db")

    # The exact candidates cannot overlap in a valid layout, but deduping here
    # keeps a selected path and its canonical spelling from being read twice.
    descriptors: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for index, database in enumerate(candidates):
        resolved = database.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        descriptors.append(
            {
                "path": database,
                "label": _source_label(root, database),
                "root": _store_root(root, database),
                "index": index,
                "outer_turn": _outer_turn(root, database),
            }
        )
    label_counts: dict[str, int] = {}
    for index, descriptor in enumerate(descriptors):
        descriptor["index"] = index
        label = descriptor["label"]
        occurrence = label_counts.get(label, 0) + 1
        label_counts[label] = occurrence
        if occurrence > 1:
            descriptor["label"] = f"{label}-{occurrence}"
    return descriptors, issues


def _db_paths(session: Path | str) -> list[Path]:
    """Return only documented event DB paths beneath the selected session."""
    root, selected = _session_root(session)
    databases, _issues = _discover_databases(root, selected)
    return [descriptor["path"] for descriptor in databases]


def _event_identity(event: Mapping[str, Any]) -> tuple[str | None, int | None]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    task_id = event.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        task_id = None
    generation = event.get("generation")
    if type(generation) is not int:
        generation = payload.get("generation")
    if type(generation) is not int or generation < 0:
        generation = None
    return task_id, generation


def _event_turn(event: Mapping[str, Any]) -> int | None:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        value = payload.get("turn")
        if type(value) is int and value >= 0:
            return value
    value = event.get("turn")
    return value if type(value) is int and value >= 0 else None


def _read_event_stores(
    root: Path, descriptors: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    issues: list[str] = []
    for descriptor in descriptors:
        database = descriptor["path"]
        try:
            rows = read_events_file(database, max_rows=MAX_EVENT_ROWS_PER_READ)
            row_count = count_events_file(database)
        except (OSError, StoreError, ValueError) as exc:
            raise EventReadError(f"cannot read event store {database}: {exc}") from exc
        if row_count and row_count != len(rows):
            issues.append(f"event store has an incomplete trailing row: {descriptor['label']}")
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("payload"), Mapping):
                issues.append(f"event store contains an incomplete row: {descriptor['label']}")
                continue
            event = dict(row)
            event["payload"] = dict(row["payload"])
            sequence = event.get("seq")
            if type(sequence) is not int or sequence <= 0:
                issues.append(f"event store contains an invalid sequence: {descriptor['label']}")
                continue
            event["_source_store"] = descriptor["label"]
            event["_source_path"] = str(database.resolve())
            event["_source_root"] = str(descriptor["root"])
            event["_source_turn"] = descriptor["outer_turn"]
            event["_source_index"] = descriptor["index"]
            event["_order"] = (descriptor["index"], sequence)
            event["_event_ref"] = f"{descriptor['label']}:event-{sequence}"
            event["_turn"] = _event_turn(event)
            events.append(event)

    events.sort(key=lambda event: event["_order"])
    if not events:
        issues.append("selected event stores are empty")
    return events, issues


def _read_events(session: Path | str) -> list[dict[str, Any]]:
    """Read bounded, source-tagged events from the selected session tree."""
    root, selected = _session_root(session)
    descriptors, issues = _discover_databases(root, selected)
    if not descriptors:
        detail = "; ".join(issues) if issues else "no documented events.db found"
        raise EventReadError(f"cannot analyze session {root}: {detail}")
    events, _read_issues = _read_event_stores(root, descriptors)
    return events


def _mapping_sources(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return production nested metadata sources used by result/checkpoint rows."""
    sources = [value]
    for key in ("meta", "terminal_action"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    return sources


def _first_value(sources: list[Mapping[str, Any]], key: str) -> Any:
    for source in sources:
        if key in source:
            return source[key]
    return None


def _checkpoint_ref_path(source_root: Path, raw_ref: object) -> Path | None:
    if not isinstance(raw_ref, str) or not raw_ref:
        return None
    relative = Path(raw_ref)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix != ".json"
    ):
        return None
    return source_root / ".cambium" / "checkpoints" / relative


def _state_ref_path(source_root: Path, session_root: Path, raw_ref: object) -> Path | None:
    if not isinstance(raw_ref, str) or not raw_ref:
        return None
    candidate = Path(raw_ref)
    if not candidate.is_absolute():
        candidate = source_root / ".cambium" / "checkpoints" / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        return None
    if not resolved.is_relative_to(session_root):
        return None
    if _path_has_symlink(candidate, session_root):
        return None
    return resolved


def _bounded_json(path: Path) -> dict[str, Any]:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"checkpoint cannot be inspected: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise ValueError("checkpoint path is a symlink")
    if not stat.S_ISREG(mode):
        raise ValueError("checkpoint is not a regular file")
    try:
        if path.stat().st_size > _MAX_CHECKPOINT_BYTES:
            raise ValueError(f"checkpoint exceeds {_MAX_CHECKPOINT_BYTES}-byte cap")
        with path.open("rb") as handle:
            raw = handle.read(_MAX_CHECKPOINT_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"checkpoint cannot be read: {exc}") from exc
    if len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ValueError(f"checkpoint exceeds {_MAX_CHECKPOINT_BYTES}-byte cap")
    if not raw.strip():
        raise ValueError("checkpoint is empty")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("checkpoint contains invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("checkpoint is not an object")
    return value


def _checkpoint_task(
    path: Path, checkpoint_root: Path, sources: list[Mapping[str, Any]]
) -> str | None:
    value = _first_value(sources, "task_id")
    if isinstance(value, str) and value:
        return value
    try:
        relative = path.relative_to(checkpoint_root)
    except ValueError:
        return None
    return relative.parts[0] if len(relative.parts) >= 2 else None


def _checkpoint_turn(path: Path, sources: list[Mapping[str, Any]]) -> int | None:
    value = _first_value(sources, "turn")
    if type(value) is int and value >= 0:
        return value
    for part in reversed(path.parts):
        match = re.match(r"(?:turn|epoch)-?(\d+)", part)
        if match:
            return int(match.group(1))
    return None


def _checkpoint_record(
    path: Path,
    source: Mapping[str, Any],
    reference_event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    raw = _bounded_json(path)
    if "meta" in raw and not isinstance(raw.get("meta"), Mapping):
        raise ValueError("checkpoint metadata is not an object")
    sources = _mapping_sources(raw)
    checkpoint_root = path.parent.parent
    task_id = _checkpoint_task(path, checkpoint_root, sources)
    generation = _first_value(sources, "generation")
    if type(generation) is not int or generation < 0:
        generation = None
    turn = _checkpoint_turn(path, sources)
    code_changed = _first_value(sources, "code_changed")
    if type(code_changed) is not bool:
        code_changed = None
    # verified_after_change is durable only in immutable epoch metadata.
    meta = raw.get("meta")
    verified = meta.get("verified_after_change") if isinstance(meta, Mapping) else None
    if type(verified) is not bool:
        verified = None
    if code_changed is None and verified is None and not (
        raw.get("schema") == 1 and isinstance(raw.get("transcript"), list)
    ):
        raise ValueError("checkpoint has no durable state")
    record: dict[str, Any] = {
        "_path": str(path),
        "_source_store": source["label"],
        "_source_path": str(source["path"].resolve()),
        "_source_root": str(source["root"]),
        "_source_index": source["index"],
        "_source_turn": source["outer_turn"],
        "_task_id": task_id,
        "_generation": generation,
        "_turn": turn,
        "_code_changed": code_changed,
        "_verified": verified,
        "_event_order": reference_event.get("_order") if reference_event else None,
        "_reference_event": reference_event,
    }
    if record["_event_order"] is None:
        record["_fallback_order"] = (
            source["index"],
            turn if type(turn) is int else -1,
            path.name,
        )
    return record


def _read_checkpoints(  # noqa: C901
    session: Path | str,
    events: list[dict[str, Any]],
    descriptors: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Read bounded checkpoints from documented roots and referenced state_ref files."""
    session_root, selected = _session_root(session)
    if descriptors is None:
        descriptors, discover_issues = _discover_databases(session_root, selected)
    else:
        discover_issues = []
    issues: list[str] = list(discover_issues)
    candidates: dict[tuple[Path, Path], tuple[dict[str, Any], Mapping[str, Any] | None]] = {}
    seen_roots: set[tuple[Path, Path]] = set()

    def add_candidate(
        descriptor: dict[str, Any], path: Path, reference_event: Mapping[str, Any] | None
    ) -> None:
        if _path_has_symlink(path, session_root):
            issues.append(f"checkpoint path escapes or uses a symlink: {descriptor['label']}")
            return
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            issues.append(f"referenced checkpoint is missing: {descriptor['label']}")
            return
        except OSError as exc:
            issues.append(f"checkpoint cannot be inspected: {exc}")
            return
        if stat.S_ISLNK(mode):
            issues.append(f"checkpoint path is a symlink: {descriptor['label']}")
            return
        if not stat.S_ISREG(mode):
            issues.append(f"checkpoint is not a regular file: {descriptor['label']}")
            return
        key = (descriptor["path"].resolve(), path.resolve())
        previous = candidates.get(key)
        if previous is None or (previous[1] is None and reference_event is not None):
            candidates[key] = (descriptor, reference_event)

    for descriptor in descriptors:
        checkpoint_root = descriptor["root"] / ".cambium" / "checkpoints"
        checkpoint_root = Path(checkpoint_root)
        root_key = (descriptor["path"].resolve(), checkpoint_root)
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        if _path_has_symlink(checkpoint_root, session_root):
            issues.append(f"checkpoint root uses a symlink: {descriptor['label']}")
            continue
        try:
            root_mode = checkpoint_root.lstat().st_mode
        except FileNotFoundError:
            root_mode = None
        except OSError as exc:
            issues.append(f"checkpoint root cannot be inspected: {exc}")
            continue
        if root_mode is None:
            continue
        if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
            issues.append(f"checkpoint root is not a directory: {descriptor['label']}")
            continue
        try:
            task_dirs = sorted(checkpoint_root.iterdir(), key=lambda child: child.name)
        except OSError as exc:
            issues.append(f"checkpoint root cannot be enumerated: {exc}")
            continue
        files_seen = 0
        for task_dir in task_dirs:
            try:
                task_mode = task_dir.lstat().st_mode
            except OSError as exc:
                issues.append(f"checkpoint task directory cannot be inspected: {exc}")
                continue
            if stat.S_ISLNK(task_mode):
                issues.append(f"checkpoint task directory is a symlink: {descriptor['label']}")
                continue
            if not stat.S_ISDIR(task_mode):
                continue
            try:
                children = sorted(task_dir.iterdir(), key=lambda child: child.name)
            except OSError as exc:
                issues.append(f"checkpoint task directory cannot be enumerated: {exc}")
                continue
            for child in children:
                if child.suffix != ".json":
                    continue
                files_seen += 1
                if files_seen > _MAX_CHECKPOINT_FILES:
                    issues.append(f"checkpoint file count exceeds {_MAX_CHECKPOINT_FILES}")
                    break
                add_candidate(descriptor, child, None)
            if files_seen > _MAX_CHECKPOINT_FILES:
                break

    for event in events:
        payload = event["payload"]
        if event.get("kind") == "checkpoint" and "state_ref" in payload:
            source_root = Path(event["_source_root"])
            path = _state_ref_path(source_root, session_root, payload.get("state_ref"))
            if path is None:
                issues.append("checkpoint state_ref is outside the selected session tree")
                continue
            descriptor = next(
                (
                    item
                    for item in descriptors
                    if item["label"] == event["_source_store"]
                    and str(item["path"].resolve()) == event["_source_path"]
                ),
                None,
            )
            if descriptor is not None:
                add_candidate(descriptor, path, event)
        if event.get("kind") in {"context_checkpoint", "context_epoch_advanced"}:
            path = _checkpoint_ref_path(Path(event["_source_root"]), payload.get("checkpoint_ref"))
            if path is None:
                issues.append("context checkpoint_ref is invalid")
                continue
            descriptor = next(
                (
                    item
                    for item in descriptors
                    if item["label"] == event["_source_store"]
                    and str(item["path"].resolve()) == event["_source_path"]
                ),
                None,
            )
            if descriptor is None:
                continue
            add_candidate(descriptor, path, event)

    checkpoints: list[dict[str, Any]] = []
    for (_source_path, path), (descriptor, reference_event) in sorted(
        candidates.items(), key=lambda item: (item[1][0]["index"], str(item[0][1]))
    ):
        try:
            checkpoint = _checkpoint_record(path, descriptor, reference_event)
        except (OSError, ValueError) as exc:
            issues.append(f"checkpoint is incomplete in {descriptor['label']}: {exc}")
            continue
        if checkpoint["_task_id"] is None or checkpoint["_turn"] is None:
            issues.append(f"checkpoint is missing durable identity in {descriptor['label']}")
            continue
        checkpoints.append(checkpoint)
    return checkpoints, issues


def _owner(event: Mapping[str, Any]) -> tuple[Any, ...]:
    """Identity includes the source store and outer interactive turn."""
    task_id, generation = _event_identity(event)
    source = event.get("_source_store")
    outer_turn = event.get("_source_turn")
    if source is None:
        source = "unknown-store"
    source_path = event.get("_source_path", source)
    if task_id is None and generation is None:
        return (None, None, source, outer_turn, source_path, event.get("_event_ref"))
    return (task_id, generation, source, outer_turn, source_path)


def _semantic_owner(event: Mapping[str, Any]) -> tuple[Any, ...]:
    task_id, generation = _event_identity(event)
    if task_id is None and generation is None:
        return (None, None, event.get("_source_store"), event.get("_source_turn"))
    return task_id, generation


def _checkpoint_matches(event: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> bool:
    task_id, generation = _event_identity(event)
    if checkpoint.get("_source_store") != event.get("_source_store") or checkpoint.get(
        "_source_path"
    ) != event.get("_source_path"):
        return False
    checkpoint_task = checkpoint.get("_task_id")
    if task_id is not None and checkpoint_task != task_id:
        return False
    checkpoint_generation = checkpoint.get("_generation")
    if generation is not None and checkpoint_generation is not None:
        return generation == checkpoint_generation
    return True


def _worker_turn(event: Mapping[str, Any]) -> int | None:
    return _event_turn(event)


def _latest_checkpoint(
    event: Mapping[str, Any], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    event_order = event.get("_order")
    event_turn = _worker_turn(event)
    for checkpoint in checkpoints:
        if not _checkpoint_matches(event, checkpoint):
            continue
        checkpoint_order = checkpoint.get("_event_order")
        if isinstance(checkpoint_order, tuple) and isinstance(event_order, tuple):
            if checkpoint_order >= event_order:
                continue
        elif (
            type(event_turn) is int
            and type(checkpoint.get("_turn")) is int
            and checkpoint["_turn"] > event_turn
        ):
            continue
        candidates.append(checkpoint)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda checkpoint: (
            checkpoint.get("_event_order")
            if isinstance(checkpoint.get("_event_order"), tuple)
            else checkpoint.get("_fallback_order", (-1, -1, "")),
        ),
    )


def _canonical(value: object) -> str:
    if isinstance(value, Mapping):
        value = {str(key): _canonical(item) for key, item in value.items()}
    elif isinstance(value, list):
        value = [_canonical(item) for item in value]
    elif isinstance(value, str):
        value = " ".join(value.split())
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        return repr(value)


def _command_details(event: Mapping[str, Any]) -> tuple[str | None, object]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    name = payload.get("tool")
    command = payload.get("cmd")
    if not isinstance(command, str):
        command = ""
    command = command.strip()
    if not isinstance(name, str) or not name:
        name, _, command = command.partition(" ")
    if not isinstance(name, str) or not name:
        return None, {}
    if command.startswith(name):
        command = command[len(name) :].strip()
    if command.startswith("{") or command.startswith("["):
        try:
            return name, json.loads(command)
        except json.JSONDecodeError:
            pass
    return name, command


def _tool_signature(event: Mapping[str, Any]) -> str | None:
    name, arguments = _command_details(event)
    return f"{name}:{_canonical(arguments)}" if name else None


def _read_units(event: Mapping[str, Any]) -> set[str]:
    name, arguments = _command_details(event)
    if name != "read_batch" or not isinstance(arguments, Mapping):
        return set()
    paths = arguments.get("paths")
    if not isinstance(paths, list):
        return set()
    offset = arguments.get("offset") if type(arguments.get("offset")) is int else None
    limit = arguments.get("limit") if type(arguments.get("limit")) is int else None
    units: set[str] = set()
    for path in paths:
        if isinstance(path, str) and path:
            units.add(_canonical((path, offset, limit)))
    return units


def _write_paths(event: Mapping[str, Any]) -> set[str]:
    name, arguments = _command_details(event)
    if name not in _WRITE_TOOLS or not isinstance(arguments, Mapping):
        return set()
    path = arguments.get("path")
    return {path} if isinstance(path, str) and path else set()


def _tool_ok(event: Mapping[str, Any]) -> bool:
    payload = event.get("payload")
    value = payload.get("ok") if isinstance(payload, Mapping) else None
    return value is True


def _group_events(
    events: list[dict[str, Any]], *, semantic: bool = False
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for event in events:
        key = _semantic_owner(event) if semantic else _owner(event)
        groups.setdefault(key, []).append(event)
    for group in groups.values():
        group.sort(key=lambda event: event["_order"])
    return groups


def _event_ref(item: Mapping[str, Any]) -> str | None:
    value = item.get("_event_ref")
    return value if isinstance(value, str) else None


def _finding(
    detector: str, evidence: list[Mapping[str, Any]], fire_at: Mapping[str, Any]
) -> dict[str, Any]:
    ordered: dict[str, Mapping[str, Any]] = {}
    for item in evidence:
        reference = _event_ref(item)
        if reference is not None:
            ordered[reference] = item
    source_events = sorted(ordered.values(), key=lambda item: item.get("_order", (0, 0)))
    # Keep evidence order independent of detector traversal.
    refs = [_event_ref(item) for item in source_events if _event_ref(item) is not None]
    first_event = source_events[0] if source_events else None
    first_turn: int | str | None
    if first_event is None:
        first_turn = None
    else:
        turn = _worker_turn(first_event)
        first_turn = turn if type(turn) is int else _event_ref(first_event)
    fire_order = fire_at.get("_order", (0, 0))
    return {
        "detector": detector,
        "evidence": refs,
        "first_turn": first_turn,
        "_fire_order": fire_order,
    }


def _retry_finding(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    runs: list[list[dict[str, Any]]] = []
    for group in _group_events(events).values():
        current: list[dict[str, Any]] = []
        signature: str | None = None
        for event in group:
            if event.get("kind") not in _TOOL_KINDS:
                continue
            candidate = _tool_signature(event)
            if candidate is not None and candidate == signature:
                current.append(event)
            else:
                if len(current) >= 3:
                    runs.append(current)
                current = [event] if candidate is not None else []
                signature = candidate
        if len(current) >= 3:
            runs.append(current)
    if not runs:
        return None
    run = min(runs, key=lambda item: item[2]["_order"])
    return _finding("retry-loop", run, run[2])


def _result_status(event: Mapping[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("status")
    return value.casefold() if isinstance(value, str) and value else None


def _event_before(event: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    return candidate.get("_order", (0, 0)) <= event.get("_order", (0, 0))


def _state_for_result(
    event: dict[str, Any], events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> tuple[bool, bool]:
    checkpoint = _latest_checkpoint(event, checkpoints)
    owner = _owner(event)
    writes = [
        candidate
        for candidate in events
        if candidate.get("kind") in _TOOL_KINDS
        and _owner(candidate) == owner
        and _event_before(event, candidate)
        and _write_paths(candidate)
        and _tool_ok(candidate)
    ]
    changed = checkpoint.get("_code_changed") if checkpoint is not None else None
    if type(changed) is not bool:
        changed = bool(writes)
    elif writes:
        changed = bool(changed) or bool(writes)

    verifications = [
        candidate
        for candidate in events
        if candidate.get("kind") in _TOOL_KINDS
        and _owner(candidate) == owner
        and _event_before(event, candidate)
        and _command_details(candidate)[0] == "run_shell"
    ]
    verified = checkpoint.get("_verified") if checkpoint is not None else None
    if verifications and (
        checkpoint is None
        or not isinstance(checkpoint.get("_event_order"), tuple)
        or verifications[-1]["_order"] > checkpoint["_event_order"]
    ):
        verified = _tool_ok(verifications[-1])
    if type(verified) is not bool:
        verified = False
    return bool(changed), bool(verified)


def _verification_finding(
    events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    failures: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") not in _TERMINAL_KINDS:
            continue
        if _result_status(event) not in _SUCCESS_STATUSES:
            continue
        changed, verified = _state_for_result(event, events, checkpoints)
        if changed and not verified:
            failures.append(event)
    return _finding("finish-without-verification", failures, failures[0]) if failures else None


def _objective_finding(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    failures: list[dict[str, Any]] = []
    for event in events:
        if (
            event.get("kind") not in _TERMINAL_KINDS
            or _result_status(event) not in _SUCCESS_STATUSES
        ):
            continue
        payload = event.get("payload")
        action = payload.get("terminal_action") if isinstance(payload, Mapping) else None
        if (
            isinstance(action, Mapping)
            and action.get("type") == "finish"
            and action.get("objective_met") is False
        ):
            failures.append(event)
    return _finding("objective_met-override", failures, failures[0]) if failures else None


def _task_states(events: list[dict[str, Any]], issues: list[str]) -> dict[tuple[Any, ...], str]:
    """Classify each semantic task for read-churn gating and completeness."""
    states: dict[tuple[Any, ...], str] = {}
    terminal: set[tuple[Any, ...]] = set()
    for event in events:
        owner = _semantic_owner(event)
        kind = event.get("kind")
        if kind in _FAILURE_KINDS or kind == "compaction_failed":
            states[owner] = "failed"
            terminal.add(owner)
        elif kind in _TERMINAL_KINDS:
            status = _result_status(event)
            if status is None:
                states[owner] = "incomplete"
            elif status not in _SUCCESS_STATUSES:
                states[owner] = "failed"
            else:
                payload = event.get("payload")
                action = payload.get("terminal_action") if isinstance(payload, Mapping) else None
                if (
                    isinstance(action, Mapping)
                    and action.get("type") == "finish"
                    and action.get("objective_met") is False
                ):
                    states[owner] = "forced"
                elif states.get(owner) not in {"failed", "incomplete", "forced"}:
                    states[owner] = "success"
            terminal.add(owner)
    active = {_semantic_owner(event) for event in events if event.get("kind") in _TOOL_KINDS}
    for owner in active - terminal:
        if states.get(owner) != "failed":
            states[owner] = "incomplete"
            issues.append("task has no durable terminal result")
    task_ids = {
        task_id
        for event in events
        if event.get("kind") in {"task_assigned", "run_task", "init", "ready"}
        for task_id, _generation in [_event_identity(event)]
        if task_id is not None
    }
    terminal_task_ids = {
        task_id
        for event in events
        if event.get("kind") in (_TERMINAL_KINDS | _FAILURE_KINDS | {"compaction_failed"})
        for task_id, _generation in [_event_identity(event)]
        if task_id is not None
    }
    if task_ids and not terminal_task_ids:
        issues.append("session has no durable terminal outcome")
    elif task_ids - terminal_task_ids:
        issues.append("task has no durable terminal result")
    if events and not any(
        event.get("kind") in (_TERMINAL_KINDS | _FAILURE_KINDS | {"compaction_failed"})
        for event in events
    ):
        issues.append("session has no durable terminal outcome")
    return states


def _read_churn_finding(
    events: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    task_states: Mapping[tuple[Any, ...], str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("kind") in _TOOL_KINDS and _read_units(event):
            grouped.setdefault(_semantic_owner(event), []).append(event)
    for owner, reads in grouped.items():
        reads.sort(key=lambda event: event["_order"])
        units = set().union(*(_read_units(event) for event in reads))
        touched: set[str] = set()
        changed_hint = False
        for event in events:
            if event.get("kind") not in _TOOL_KINDS or _semantic_owner(event) != owner:
                continue
            if _tool_ok(event):
                touched.update(_write_paths(event))
        for checkpoint in checkpoints:
            checkpoint_owner = (
                checkpoint.get("_task_id"),
                checkpoint.get("_generation"),
            )
            if checkpoint_owner == owner and checkpoint.get("_code_changed") is True:
                changed_hint = True
        touched_count = max(len(touched), int(changed_hint))
        if (
            len(reads) < _MIN_READ_EVENTS
            or len(units) < _MIN_DISTINCT_READ_UNITS
            or len(units) <= 3 * max(1, touched_count)
        ):
            continue
        finding = _finding("read-churn", reads, reads[-1])
        if task_states.get(owner) in {"failed", "incomplete", "forced"}:
            findings.append(finding)
        else:
            warnings.append(finding)
    if not findings:
        return None, warnings
    return min(findings, key=lambda item: item["_fire_order"]), warnings


def _compaction_finding(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    evidence = [event for event in events if event.get("kind") == "compaction_failed"]
    return _finding("compaction-stall", evidence, evidence[0]) if evidence else None


def _event_shape_issues(events: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for event in events:
        kind = event.get("kind")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            issues.append("event payload is not an object")
            continue
        if kind == "tool_event":
            if not isinstance(payload.get("tool"), str) or not payload.get("tool"):
                issues.append("tool event is missing its durable tool name")
            if not isinstance(payload.get("cmd"), str):
                issues.append("tool event is missing its durable command")
            if type(payload.get("ok")) is not bool:
                issues.append("tool event is missing its durable outcome")
        elif kind == "result":
            if _result_status(event) is None:
                issues.append("result event is missing its durable status")
            action = payload.get("terminal_action")
            if action is not None and (
                not isinstance(action, Mapping)
                or action.get("type") != "finish"
                or type(action.get("objective_met")) is not bool
            ):
                issues.append("result event has an invalid terminal action")
        elif kind == "compaction_failed":
            if type(payload.get("epoch")) is not int or payload.get("epoch") < 0:
                issues.append("compaction_failed event has an invalid epoch")
            if not isinstance(payload.get("reason"), str) or not payload.get("reason"):
                issues.append("compaction_failed event is missing its durable reason")
        elif kind == "checkpoint" and "state_ref" not in payload:
            issues.append("checkpoint event is missing its durable state_ref")
        elif kind in {"context_checkpoint", "context_epoch_advanced"} and not payload.get(
            "checkpoint_ref"
        ):
            issues.append("context checkpoint event is missing its durable checkpoint_ref")
    return issues


def _session_failed(events: list[dict[str, Any]]) -> bool:
    for event in events:
        if event.get("kind") in _FAILURE_KINDS:
            return True
        if event.get("kind") in _TERMINAL_KINDS and _result_status(event) not in _SUCCESS_STATUSES:
            return True
    return False


def _base_record(session: Path, verdict: str, confidence: float) -> dict[str, Any]:
    return {
        "session": str(session),
        "verdict": verdict,
        "detectors_fired": [],
        "confidence": confidence,
    }


def analyze_session(session: Path | str) -> dict[str, Any]:
    """Return one stable attribution record for a selected session tree."""
    session_path = Path(session).expanduser()
    root, selected = _session_root(session_path)
    descriptors, discovery_issues = _discover_databases(root, selected)
    if not descriptors:
        record = _base_record(session_path, "incomplete", 0.0)
        record["incomplete_reasons"] = discovery_issues or ["no documented events.db found"]
        return record
    try:
        events, event_issues = _read_event_stores(root, descriptors)
    except EventReadError as exc:
        record = _base_record(session_path, "error", 0.0)
        record["error"] = str(exc)
        return record

    checkpoints, checkpoint_issues = _read_checkpoints(session_path, events, descriptors)
    issues = [
        *discovery_issues,
        *event_issues,
        *checkpoint_issues,
        *_event_shape_issues(events),
    ]
    task_states = _task_states(events, issues)
    findings: list[dict[str, Any]] = []
    for finding in (
        _retry_finding(events),
        _verification_finding(events, checkpoints),
        _objective_finding(events),
    ):
        if finding is not None:
            findings.append(finding)
    read_finding, warnings = _read_churn_finding(events, checkpoints, task_states)
    if read_finding is not None:
        findings.append(read_finding)
    if (compaction := _compaction_finding(events)) is not None:
        findings.append(compaction)

    detector_order = {name: index for index, name in enumerate(_DETECTOR_ORDER)}
    findings.sort(key=lambda finding: (finding["_fire_order"], detector_order[finding["detector"]]))
    record = _base_record(
        session_path,
        "incomplete" if issues else ("failed" if findings or _session_failed(events) else "clean"),
        0.5 if issues else 1.0,
    )
    record["detectors_fired"] = [
        {
            "detector": finding["detector"],
            "evidence": finding["evidence"],
            "first_turn": finding["first_turn"],
        }
        for finding in findings
    ]
    if warnings:
        warnings.sort(key=lambda finding: finding["_fire_order"])
        record["warnings"] = [
            {
                "detector": finding["detector"],
                "evidence": finding["evidence"],
                "first_turn": finding["first_turn"],
            }
            for finding in warnings
        ]
    if issues:
        record["incomplete_reasons"] = sorted(set(issues))
    return record


def _unique_sessions(raw_sessions: list[str]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for raw in raw_sessions:
        path = Path(raw).expanduser()
        resolved = path.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def _print_table(records: list[dict[str, Any]]) -> None:
    print("session | verdict | detectors")
    for record in records:
        detectors = ",".join(item["detector"] for item in record["detectors_fired"]) or "-"
        print(f"{record['session']} | {record['verdict']} | {detectors}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic failure attribution for sessions.")
    parser.add_argument("sessions", nargs="+", help="session directory or directories")
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    args = parser.parse_args(argv)
    try:
        records = [analyze_session(path) for path in _unique_sessions(args.sessions)]
    except SessionPathError as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(records[0] if len(records) == 1 else records, indent=2, sort_keys=True))
    else:
        _print_table(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
