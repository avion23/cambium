"""Deterministic failure attribution from Cambium session event logs.

The miner deliberately uses only durable events and checkpoint state.  It is
small enough to run over old sessions and does not make provider/LLM calls.

Run::

    python scripts/failure_attribution.py SESSION_DIR [SESSION_DIR ...]
    python scripts/failure_attribution.py --json SESSION_DIR
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_TURN_RE = re.compile(r"^turn-(\d+)$")
_LOOP_STATE_RE = re.compile(
    r"<cambium-loop-state>\s*(\{.*?\})\s*</cambium-loop-state>", re.DOTALL
)
_ERROR_CLASS_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Failure|Timeout|Denied))\b"
)
_SUCCESS_STATUSES = frozenset({"succeeded", "success", "done", "completed"})
_TOOL_KINDS = frozenset({"tool_event", "tool", "tool_call"})
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "write", "edit"})
_TERMINAL_KINDS = frozenset({"result", "result_envelope"})
_INITIAL_KINDS = frozenset({"task_assigned", "spawned", "init", "ready", "run_task"})
_DETECTOR_ORDER = (
    "retry-loop",
    "finish-without-verification",
    "objective_met-override",
    "read-churn",
    "compaction-stall",
)


def _turn_from_path(path: Path) -> int | None:
    for part in reversed(path.parts):
        match = _TURN_RE.fullmatch(part)
        if match:
            return int(match.group(1))
    return None


def _db_paths(session: Path) -> list[Path]:
    root = session if session.is_dir() else session.parent
    if session.is_file():
        return [session] if session.name == "events.db" else []
    paths = {path for path in root.rglob("events.db") if path.is_file()}
    return sorted(
        paths,
        key=lambda path: (
            _turn_from_path(path) is None,
            _turn_from_path(path) if _turn_from_path(path) is not None else -1,
            str(path),
        ),
    )


def _read_events(session: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    databases = _db_paths(session)
    for database in databases:
        uri = f"{database.resolve().as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
                connection.execute("PRAGMA busy_timeout=5000")
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
                ).fetchone() is None:
                    continue
                rows = connection.execute(
                    "SELECT seq, kind, payload, task_id, generation FROM events ORDER BY seq"
                ).fetchall()
        except sqlite3.Error as exc:
            raise OSError(f"cannot read {database}: {exc}") from exc
        path_turn = _turn_from_path(database)
        for seq, kind, raw_payload, task_id, generation in rows:
            try:
                payload = json.loads(raw_payload)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            event = {
                "seq": seq,
                "kind": kind,
                "payload": payload,
                "task_id": task_id,
                "generation": generation,
                "_path_turn": path_turn,
                "_database": str(database),
            }
            event["_turn"] = _event_turn(event)
            events.append(event)

    if len(databases) == 1:
        events.sort(key=lambda event: event["seq"])
    else:
        max_turn = max(
            (event["_turn"] for event in events if isinstance(event["_turn"], int)),
            default=0,
        )
        events.sort(key=lambda event: _event_sort_key(event, max_turn))
    for index, event in enumerate(events):
        event["_index"] = index
    return events


def _event_turn(event: Mapping[str, Any]) -> int | None:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        value = payload.get("turn")
        if type(value) is int and value >= 0:
            return value
    value = event.get("turn")
    if type(value) is int and value >= 0:
        return value
    value = event.get("_path_turn")
    return value if type(value) is int and value >= 0 else None


def _worker_turn(event: Mapping[str, Any]) -> int | None:
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        value = payload.get("turn")
        if type(value) is int and value >= 0:
            return value
    value = event.get("turn")
    return value if type(value) is int and value >= 0 else None


def _event_sort_key(event: Mapping[str, Any], max_turn: int) -> tuple[Any, ...]:
    source_turn = event.get("_path_turn")
    if type(source_turn) is int:
        return (source_turn, event.get("seq", 0), event.get("_database", ""))
    turn = event.get("_turn")
    if type(turn) is int:
        return (turn, event.get("seq", 0), event.get("_database", ""))
    kind = event.get("kind")
    if kind in _INITIAL_KINDS:
        return (-1, event.get("seq", 0), event.get("_database", ""))
    if kind in _TERMINAL_KINDS or kind in {"exit", "session_ended"}:
        return (max_turn + 1, event.get("seq", 0), event.get("_database", ""))
    return (0, event.get("seq", 0), event.get("_database", ""))


def _mapping_sources(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    sources = [value]
    for key in ("state", "result", "outcome", "terminal_action", "meta"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            sources.append(nested)
    return sources


def _first_value(sources: list[Mapping[str, Any]], key: str) -> Any:
    for source in sources:
        if key in source:
            return source[key]
    return None


def _first_bool(sources: list[Mapping[str, Any]], key: str) -> bool | None:
    value = _first_value(sources, key)
    return value if type(value) is bool else None


def _checkpoint_task(path: Path, sources: list[Mapping[str, Any]]) -> str | None:
    value = _first_value(sources, "task_id")
    if isinstance(value, str) and value:
        return value
    parts = path.parts
    if "checkpoints" in parts:
        index = len(parts) - 1 - parts[::-1].index("checkpoints")
        if index + 1 < len(parts):
            candidate = parts[index + 1]
            if not candidate.startswith(("turn-", "epoch-")):
                return candidate
    return None


def _checkpoint_turn(path: Path, sources: list[Mapping[str, Any]]) -> int | None:
    for key in ("turn", "epoch_turn"):
        value = _first_value(sources, key)
        if type(value) is int and value >= 0:
            return value
    for part in reversed(path.parts):
        match = re.match(r"(?:turn|epoch)-?(\d+)", part)
        if match:
            return int(match.group(1))
    return _turn_from_path(path)


def _action(content: object) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(content.strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("type") != "tool_call":
        return None
    name = value.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = value.get("arguments", {})
    return {"name": name, "arguments": arguments if isinstance(arguments, Mapping) else {}}


def _loop_state(content: object) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    states = _LOOP_STATE_RE.findall(content)
    for raw in reversed(states):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _checkpoint_actions(raw: Mapping[str, Any], turn: int | None) -> list[dict[str, Any]]:
    transcripts: list[list[Any]] = []
    value = raw.get("transcript")
    if isinstance(value, list):
        transcripts.append(value)
    content = raw.get("content")
    if isinstance(content, Mapping):
        for key in ("provider_messages", "continuation_suffix"):
            value = content.get(key)
            if isinstance(value, list):
                transcripts.append(value)
    actions: list[dict[str, Any]] = []
    for transcript in transcripts:
        for index, message in enumerate(transcript):
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                continue
            parsed = _action(message.get("content"))
            if parsed is None:
                continue
            action_turn = turn
            for following in transcript[index + 1 :]:
                if not isinstance(following, Mapping):
                    continue
                state = _loop_state(following.get("content"))
                if state is not None and type(state.get("turn")) is int:
                    action_turn = state["turn"]
                    break
                if following.get("role") == "assistant":
                    break
            actions.append({**parsed, "turn": action_turn})
    return actions


def _read_checkpoints(session: Path, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    roots = [session / ".cambium" / "checkpoints", session / "checkpoints"]
    paths = {path for root in roots if root.is_dir() for path in root.rglob("*.json")}
    for event in events:
        payload = event["payload"]
        state_ref = payload.get("state_ref")
        if isinstance(state_ref, str):
            path = Path(state_ref)
            if path.is_file():
                paths.add(path)
    checkpoints: list[dict[str, Any]] = []
    for path in sorted(paths):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        sources = _mapping_sources(raw)
        state = _loop_state("\n".join(
            str(message.get("content", ""))
            for message in raw.get("transcript", [])
            if isinstance(message, Mapping)
        ))
        if state is not None:
            sources.append(state)
        turn = _checkpoint_turn(path, sources)
        actions = _checkpoint_actions(raw, turn)
        files = _file_values(sources)
        for action in actions:
            if action.get("name") in _WRITE_TOOLS and isinstance(action.get("arguments"), Mapping):
                files.extend(_argument_paths(action["arguments"]))
        checkpoint = {
            "_path": str(path),
            "_path_turn": _turn_from_path(path),
            "_turn": turn,
            "_task_id": _checkpoint_task(path, sources),
            "_generation": _first_value(sources, "generation"),
            "_code_changed": _first_bool(sources, "code_changed"),
            "_verified": _first_bool(sources, "verified_after_change"),
            "_verification_failed": _first_bool(sources, "verification_failed"),
            "_compaction": _first_bool(sources, "compaction_deferred"),
            "_deferrals": _first_value(sources, "consecutive_compaction_deferrals"),
            "_files": files,
            "_actions": actions,
        }
        checkpoints.append(checkpoint)
    return checkpoints


def _file_values(sources: list[Mapping[str, Any]]) -> list[str]:
    values: list[str] = []
    for source in sources:
        for key in (
            "files_changed",
            "files_touched",
            "changed_files",
            "touched_files",
            "files",
        ):
            value = source.get(key)
            if isinstance(value, str) and value:
                values.append(value)
            elif isinstance(value, list):
                values.extend(item for item in value if isinstance(item, str) and item)
    return values


def _argument_paths(arguments: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("path", "paths", "file", "files", "target"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str) and item)
    return values


def _owner(event: Mapping[str, Any]) -> tuple[Any, Any]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    task_id = event.get("task_id") or payload.get("task_id")
    generation = event.get("generation")
    if generation is None:
        generation = payload.get("generation")
    return task_id, generation


def _checkpoint_matches(event: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> bool:
    task_id, generation = _owner(event)
    checkpoint_task = checkpoint.get("_task_id")
    checkpoint_generation = checkpoint.get("_generation")
    if task_id is not None and checkpoint_task is not None and task_id != checkpoint_task:
        return False
    if (
        generation is not None
        and checkpoint_generation is not None
        and generation != checkpoint_generation
    ):
        return False
    event_path_turn = event.get("_path_turn")
    checkpoint_path_turn = checkpoint.get("_path_turn")
    if (
        type(event_path_turn) is int
        and type(checkpoint_path_turn) is int
        and event_path_turn != checkpoint_path_turn
    ):
        return False
    return True


def _latest_checkpoint(
    event: Mapping[str, Any], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    candidates = [
        checkpoint for checkpoint in checkpoints if _checkpoint_matches(event, checkpoint)
    ]
    event_turn = _worker_turn(event)
    if type(event_turn) is int:
        candidates = [
            checkpoint
            for checkpoint in candidates
            if checkpoint.get("_turn") is None or checkpoint.get("_turn") <= event_turn
        ]
    return max(
        candidates,
        key=lambda checkpoint: (
            checkpoint.get("_turn") if type(checkpoint.get("_turn")) is int else -1,
            checkpoint.get("_path", ""),
        ),
        default=None,
    )


def _action_for(
    event: Mapping[str, Any], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    turn = _worker_turn(event)
    actions = [
        action
        for checkpoint in checkpoints
        if _checkpoint_matches(event, checkpoint)
        for action in checkpoint.get("_actions", [])
        if type(turn) is int and action.get("turn") == turn
    ]
    return actions[-1] if actions else None


def _canonical(value: object) -> str:
    value = _canonical_value(value)
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        return repr(value)


def _canonical_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return " ".join(value.split()) if isinstance(value, str) else value


def _tool_details(
    event: Mapping[str, Any], checkpoints: list[dict[str, Any]]
) -> tuple[str | None, object, str | None]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    action = _action_for(event, checkpoints)
    name = payload.get("tool") or payload.get("name")
    if not isinstance(name, str) or not name:
        name = action.get("name") if action else None
    arguments: object = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("args")
    if not isinstance(arguments, Mapping | list | str):
        arguments = action.get("arguments", {}) if action else {}
    command = payload.get("cmd")
    if command is not None and not isinstance(payload.get("arguments"), Mapping):
        arguments = _command_arguments(name, command)
    if name is None and isinstance(command, str):
        name, arguments = _split_command(command)
    error_class = _error_class(payload)
    return name, arguments, error_class


def _split_command(command: str) -> tuple[str | None, object]:
    name, _, rest = command.strip().partition(" ")
    if not name:
        return None, {}
    return name, _command_arguments(name, rest)


def _command_arguments(name: str | None, command: object) -> object:
    if not isinstance(command, str):
        return command if isinstance(command, Mapping | list) else {}
    text = command.strip()
    if name and text.startswith(name):
        text = text[len(name) :].strip()
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"cmd": " ".join(text.split())}
    return {}


def _error_class(payload: Mapping[str, Any]) -> str | None:
    for key in ("error_class", "error_type", "exception_type"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    for key in ("error", "failure_reason", "message", "exception"):
        value = payload.get(key)
        text = value if isinstance(value, str) else str(value) if value is not None else ""
        match = _ERROR_CLASS_RE.search(text)
        if match:
            return match.group(1).casefold()
    return None


def _tool_name(event: Mapping[str, Any], checkpoints: list[dict[str, Any]]) -> str | None:
    return _tool_details(event, checkpoints)[0]


def _turn_ref(value: Mapping[str, Any]) -> int | str | None:
    turn = value.get("_turn")
    if type(turn) is int:
        return turn
    sequence = value.get("seq")
    return f"seq-{sequence}" if type(sequence) is int else None


def _finding(
    detector: str, evidence: list[Mapping[str, Any]], fire_at: Mapping[str, Any]
) -> dict[str, Any]:
    refs: list[int | str] = []
    for item in evidence:
        ref = _turn_ref(item)
        if ref is not None and ref not in refs:
            refs.append(ref)
    fire_index = fire_at.get("_index")
    if type(fire_index) is not int:
        fire_index = fire_at.get("_turn", 0)
    return {
        "detector": detector,
        "evidence": refs,
        "first_turn": refs[0] if refs else None,
        "_fire_index": fire_index,
    }


def _retry_finding(
    events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    for event in events:
        if event.get("kind") not in _TOOL_KINDS:
            continue
        groups.setdefault(_owner(event), []).append(event)
    runs: list[list[dict[str, Any]]] = []
    for group in groups.values():
        for mode in ("arguments", "errors"):
            current: list[dict[str, Any]] = []
            signature: str | None = None
            for event in group:
                name, arguments, error_class = _tool_details(event, checkpoints)
                candidate = (
                    f"{name}:{_canonical(arguments)}"
                    if mode == "arguments" and name
                    else error_class if mode == "errors" else None
                )
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
    run = min(runs, key=lambda item: item[2].get("_index", 0))
    return _finding("retry-loop", run, run[2])


def _result_status(event: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return ""
    for source in _mapping_sources(payload):
        value = source.get("status")
        if isinstance(value, str):
            return value.casefold()
    return ""


def _state_for_result(
    event: dict[str, Any], events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> tuple[bool, bool]:
    payload = event["payload"]
    sources = _mapping_sources(payload)
    checkpoint = _latest_checkpoint(event, checkpoints)
    changed = _first_bool(sources, "code_changed")
    verified = _first_bool(sources, "verified_after_change")
    if checkpoint is not None:
        if changed is None:
            changed = checkpoint.get("_code_changed")
        if verified is None:
            verified = checkpoint.get("_verified")
    if changed is None:
        changed = _write_activity(events, checkpoints, event["_index"], event)
    if verified is None:
        verified = False
    return bool(changed), bool(verified)


def _result_evidence(
    event: dict[str, Any], checkpoints: list[dict[str, Any]]
) -> dict[str, Any]:
    if _worker_turn(event) is not None:
        return event
    checkpoint = _latest_checkpoint(event, checkpoints)
    if checkpoint is None or type(checkpoint.get("_turn")) is not int:
        return event
    result = dict(event)
    result["_turn"] = checkpoint["_turn"]
    return result


def _changed_paths(
    events: list[dict[str, Any]], checkpoints: list[dict[str, Any]], before: int | None = None
) -> set[str]:
    paths: set[str] = set()
    for checkpoint in checkpoints:
        paths.update(checkpoint.get("_files", []))
    for event in events:
        if before is not None and event.get("_index", 0) > before:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        paths.update(_file_values(_mapping_sources(payload)))
        if event.get("kind") not in _TOOL_KINDS:
            continue
        name, arguments, _ = _tool_details(event, checkpoints)
        if name not in _WRITE_TOOLS or not isinstance(arguments, Mapping):
            continue
        paths.update(_file_values([arguments]))
        paths.update(_argument_paths(arguments))
    return paths


def _write_activity(
    events: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    before: int,
    result_event: Mapping[str, Any],
) -> bool:
    for event in events:
        if event.get("_index", 0) > before or event.get("kind") not in _TOOL_KINDS:
            continue
        name, _arguments, _error = _tool_details(event, checkpoints)
        if name in _WRITE_TOOLS:
            return True
    for checkpoint in checkpoints:
        if _checkpoint_matches(result_event, checkpoint) and any(
            action.get("name") in _WRITE_TOOLS for action in checkpoint.get("_actions", [])
        ):
            return True
    return False


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
            failures.append(_result_evidence(event, checkpoints))
    return _finding("finish-without-verification", failures, failures[0]) if failures else None


def _objective_finding(
    events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    failures: list[dict[str, Any]] = []
    for event in events:
        if (
            event.get("kind") not in _TERMINAL_KINDS
            or _result_status(event) not in _SUCCESS_STATUSES
        ):
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        objective = _first_value(_mapping_sources(payload), "objective_met")
        if type(objective) is bool and not objective:
            failures.append(_result_evidence(event, checkpoints))
    return _finding("objective_met-override", failures, failures[0]) if failures else None


def _read_churn_finding(
    events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    reads = [
        event
        for event in events
        if event.get("kind") in _TOOL_KINDS and _tool_name(event, checkpoints) == "read_batch"
    ]
    touched = _changed_paths(events, checkpoints)
    if reads and len(reads) > 3 * len(touched):
        return _finding("read-churn", reads, reads[-1])
    return None


def _compaction_finding(
    events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]
) -> dict[str, Any] | None:
    evidence: list[Mapping[str, Any]] = []
    for event in events:
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        sources = _mapping_sources(payload)
        if (
            event.get("kind") == "compaction_deferred"
            or any(source.get("compaction_deferred") is True for source in sources)
            or any(
                type(source.get("consecutive_compaction_deferrals")) is int
                and source["consecutive_compaction_deferrals"] >= 1
                for source in sources
            )
        ):
            evidence.append(event)
    for checkpoint in checkpoints:
        if checkpoint.get("_compaction") is True or (
            type(checkpoint.get("_deferrals")) is int and checkpoint["_deferrals"] >= 1
        ):
            evidence.append(checkpoint)
    if not evidence:
        return None
    return _finding("compaction-stall", evidence, evidence[0])


def _session_failed(events: list[dict[str, Any]]) -> bool:
    for event in events:
        if event.get("kind") in {"worker_failed", "task_failed", "timeout"}:
            return True
        if event.get("kind") in _TERMINAL_KINDS and _result_status(event) not in _SUCCESS_STATUSES:
            return True
    return False


def analyze_session(session: Path | str) -> dict[str, Any]:
    """Return one stable failure-attribution record for ``session``."""
    session_path = Path(session)
    events = _read_events(session_path)
    checkpoints = _read_checkpoints(session_path, events)
    findings = [
        finding
        for finding in (
            _retry_finding(events, checkpoints),
            _verification_finding(events, checkpoints),
            _objective_finding(events, checkpoints),
            _read_churn_finding(events, checkpoints),
            _compaction_finding(events, checkpoints),
        )
        if finding is not None
    ]
    order = {name: index for index, name in enumerate(_DETECTOR_ORDER)}
    findings.sort(key=lambda finding: (finding["_fire_index"], order[finding["detector"]]))
    return {
        "session": str(session_path),
        "verdict": "failed" if findings or _session_failed(events) else "clean",
        "detectors_fired": [
            {
                "detector": finding["detector"],
                "evidence": finding["evidence"],
                "first_turn": finding["first_turn"],
            }
            for finding in findings
        ],
        "confidence": 1.0,
    }


def _unique_sessions(raw_sessions: list[str]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for raw in raw_sessions:
        path = Path(raw)
        resolved = path.resolve()
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
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(records[0] if len(records) == 1 else records, indent=2, sort_keys=True))
    else:
        _print_table(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
