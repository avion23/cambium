"""Replay one frozen Cambium checkpoint prefix and check its next action."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cambium import worker  # noqa: E402
from cambium.diffundo import Diffundo  # noqa: E402
from cambium.provider_config import load_providers  # noqa: E402
from cambium.store import read_events_file  # noqa: E402

ACTION_TYPES = ("plan", "tool_call", "finish")
Transport = Callable[[dict[str, Any]], Any]


class PrefixRegressionError(ValueError):
    """The session does not contain a usable replay checkpoint."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    turn: int
    kind: str
    reference: str
    task_id: str | None
    sequence: int


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrefixRegressionError(f"cannot read checkpoint {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PrefixRegressionError(f"checkpoint {path} is not a JSON object")
    return data


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise PrefixRegressionError("checkpoint contains duplicate JSON fields")
        data[key] = value
    return data


def _reject_json_constant(value: str) -> object:
    raise PrefixRegressionError(f"non-standard JSON constant {value!r}")


def _root_paths(session: Path) -> tuple[Path, Path]:
    db = next(
        (
            path
            for path in (session / ".cambium" / "events.db", session / "events.db")
            if path.is_file()
        ),
        None,
    )
    root = next(
        (
            path.resolve()
            for path in (session / ".cambium" / "checkpoints", session / "checkpoints")
            if path.is_dir() and not path.is_symlink()
        ),
        None,
    )
    if db is None:
        raise PrefixRegressionError(f"event database not found under {session}")
    if root is None:
        raise PrefixRegressionError(f"checkpoint directory not found under {session}")
    return db, root


def _safe_path(root: Path, reference: str) -> Path:
    raw = Path(reference)
    try:
        relative = raw.relative_to(root) if raw.is_absolute() else raw
    except ValueError as exc:
        raise PrefixRegressionError("checkpoint reference escapes the session") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise PrefixRegressionError("checkpoint reference has unsafe path components")
    path = root / relative
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except ValueError as exc:
        raise PrefixRegressionError("checkpoint reference escapes the session") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise PrefixRegressionError("checkpoint path is a symlink")
    if not resolved.is_file():
        raise PrefixRegressionError(f"checkpoint is missing: {reference}")
    return resolved


def _task_id(event: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    for value in (event.get("task_id"), payload.get("task_id")):
        if isinstance(value, str) and value:
            return value
    return None


def _turn(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _locate(
    session: Path, requested_turn: int, task_id: str | None
) -> tuple[_Candidate, Path, list[Mapping[str, Any]], dict[str, str]]:
    db, root = _root_paths(session)
    try:
        events = read_events_file(db)
    except Exception as exc:
        raise PrefixRegressionError(
            f"cannot read event database: {exc.__class__.__name__}"
        ) from exc
    tasks = {
        event["task_id"]: payload["task"]
        for event in events
        if event.get("kind") == "task_assigned"
        and isinstance(event.get("task_id"), str)
        and isinstance(payload := event.get("payload"), Mapping)
        and isinstance(payload.get("task"), str)
    }
    candidates: list[_Candidate] = []
    for index, event in enumerate(events, 1):
        kind = event.get("kind")
        payload = event.get("payload")
        if kind not in {"checkpoint", "context_checkpoint", "context_epoch_advanced"}:
            continue
        if not isinstance(payload, Mapping):
            continue
        value = payload.get("turn")
        reference = payload.get("state_ref" if kind == "checkpoint" else "checkpoint_ref")
        if (checkpoint_turn := _turn(value)) is None or not isinstance(reference, str):
            continue
        candidates.append(
            _Candidate(
                checkpoint_turn,
                "turn" if kind == "checkpoint" else "context",
                reference,
                _task_id(event, payload),
                event.get("seq") if isinstance(event.get("seq"), int) else index,
            )
        )
    if not candidates:
        for path in sorted(root.rglob("*.json")):
            try:
                data = _json(path)
            except PrefixRegressionError:
                continue
            body = data.get("content") if isinstance(data.get("content"), Mapping) else data
            meta = data.get("meta") if isinstance(data.get("meta"), Mapping) else {}
            checkpoint_turn = _turn(body.get("turn")) or _turn(meta.get("turn"))
            if checkpoint_turn is None:
                continue
            kind = "context" if isinstance(body.get("provider_messages"), list) else "turn"
            checkpoint_task = body.get("task_id")
            if not isinstance(checkpoint_task, str):
                meta = data.get("meta")
                checkpoint_task = meta.get("task_id") if isinstance(meta, Mapping) else None
            candidates.append(
                _Candidate(
                    checkpoint_turn,
                    kind,
                    path.relative_to(root).as_posix(),
                    checkpoint_task,
                    0,
                )
            )
    if task_id is not None:
        candidates = [candidate for candidate in candidates if candidate.task_id == task_id]
    elif len({candidate.task_id for candidate in candidates if candidate.task_id}) > 1:
        raise PrefixRegressionError("multiple task prefixes found; pass --task")
    eligible = [candidate for candidate in candidates if candidate.turn <= requested_turn]
    if not eligible:
        raise PrefixRegressionError(f"no checkpoint at or before turn {requested_turn}")
    chosen_turn = max(candidate.turn for candidate in eligible)
    chosen = max(
        (candidate for candidate in eligible if candidate.turn == chosen_turn),
        key=lambda candidate: (candidate.kind == "context", candidate.sequence),
    )
    return chosen, _safe_path(root, chosen.reference), events, tasks


def _messages(values: object, label: str, *, allow_empty: bool = False) -> list[dict[str, str]]:
    if not isinstance(values, list) or (not values and not allow_empty):
        raise PrefixRegressionError(f"checkpoint {label} is invalid")
    try:
        return [
            worker._context_message(value, f"{label}[{index}]")
            for index, value in enumerate(values)
        ]
    except Exception as exc:
        raise PrefixRegressionError(f"checkpoint {label} is invalid: {exc}") from exc


def _provider_hint(
    events: Sequence[Mapping[str, Any]], candidate: _Candidate
) -> dict[str, str]:
    """Recover provider identity for legacy turn checkpoints."""
    for event in reversed(events):
        if event.get("kind") != "usage_event":
            continue
        if candidate.task_id is not None and event.get("task_id") != candidate.task_id:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or _turn(payload.get("turn")) is None:
            continue
        if payload["turn"] > candidate.turn:
            continue
        provider = payload.get("provider")
        model = payload.get("model")
        if isinstance(provider, str) and provider:
            hint = {"provider": provider}
            if isinstance(model, str) and model:
                hint["model"] = model
            return hint
    return {}


def _body_and_meta(data: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    body = data.get("content") if isinstance(data.get("content"), Mapping) else data
    meta = data.get("meta") if isinstance(data.get("meta"), Mapping) else {}
    return body, meta


def _metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    body, meta = _body_and_meta(data)
    cache = body.get("cache_key")
    if not isinstance(cache, Mapping):
        cache = meta.get("cache_key") if isinstance(meta.get("cache_key"), Mapping) else {}
    task_id = body.get("task_id")
    if not isinstance(task_id, str):
        task_id = meta.get("task_id")
    return {"cache_key": dict(cache), "task_id": task_id}


def _prompt(
    data: Mapping[str, Any], task: str | None, cache_key: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    body, _meta = _body_and_meta(data)
    provider_messages = body.get("provider_messages")
    tools = worker._exposed_tool_schemas(worker._PROVIDER_TOOLS_CONFIG)
    if isinstance(provider_messages, list):
        messages = _messages(provider_messages, "provider_messages")
        suffix = _messages(
            body.get("continuation_suffix", []), "continuation_suffix", allow_empty=True
        )
        messages.extend(suffix)
        if messages[0]["role"] != "system":
            raise PrefixRegressionError("checkpoint provider prefix must start with system")
        if messages[-1]["role"] != "user":
            messages.append({"role": "user", "content": "Continue."})
        return {"messages": messages, "tools": tools}, "context"
    transcript = _messages(body.get("transcript"), "transcript", allow_empty=True)
    task = task or (body.get("task") if isinstance(body.get("task"), str) else None)
    if not task:
        raise PrefixRegressionError("ordinary checkpoint does not include its task")
    provider = cache_key.get("provider")
    model = cache_key.get("model")
    identity = f"{provider}/{model}" if isinstance(provider, str) and provider else str(model or "")
    return worker._build_agent_prompt(task, tools, transcript, identity), "turn"


def _strict_action(value: Mapping[str, Any]) -> str:
    try:
        return worker._parse_agent_action(json.dumps(value, separators=(",", ":")))["type"]
    except (TypeError, ValueError, KeyError) as exc:
        raise PrefixRegressionError(f"provider response is not a strict action: {exc}") from exc


def _action(response: Any) -> str:
    if isinstance(response, Mapping) and "choices" in response:
        choices = response.get("choices")
        if not isinstance(choices, Sequence) or isinstance(choices, str | bytes) or not choices:
            raise PrefixRegressionError("provider response has no choices")
        response = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    calls = (
        response.get("tool_calls")
        if isinstance(response, Mapping)
        else getattr(response, "tool_calls", None)
    )
    if calls:
        if not isinstance(calls, Sequence) or isinstance(calls, str | bytes) or len(calls) != 1:
            raise PrefixRegressionError("provider returned more than one native tool call")
        function = calls[0].get("function") if isinstance(calls[0], Mapping) else None
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            raise PrefixRegressionError("provider returned an invalid native tool call")
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise PrefixRegressionError(
                    "provider returned invalid native tool arguments"
                ) from exc
        if not isinstance(arguments, Mapping):
            raise PrefixRegressionError("provider returned invalid native tool arguments")
        return _strict_action(
            {"type": "tool_call", "name": function["name"], "arguments": dict(arguments)}
        )
    content = response
    if isinstance(response, Mapping):
        content = (
            json.dumps(response, separators=(",", ":"))
            if "type" in response
            else response.get("content", "")
        )
    else:
        content = getattr(response, "content", response)
    if not isinstance(content, str):
        raise PrefixRegressionError("provider response has no action content")
    try:
        return worker._parse_agent_action(content)["type"]
    except (TypeError, ValueError, KeyError) as exc:
        raise PrefixRegressionError(f"provider response is not a strict action: {exc}") from exc


def _await(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise PrefixRegressionError("async replay transport cannot run inside an event loop")


def _provider_response(
    prompt: dict[str, Any], metadata: Mapping[str, Any], task_id: str | None
) -> Any:
    cache_key = metadata.get("cache_key")
    cache_key = cache_key if isinstance(cache_key, Mapping) else {}
    boundary = cache_key.get("provider_boundary")
    boundary = boundary if isinstance(boundary, Mapping) else {}
    configured_path = boundary.get("provider_config_path")
    source = (
        configured_path
        if isinstance(configured_path, str) and Path(configured_path).is_file()
        else None
    )
    providers = load_providers(source)
    provider_name = cache_key.get("provider") or boundary.get("provider")
    selected = [provider for provider in providers if provider.name == provider_name]
    if not selected:
        raise PrefixRegressionError(f"checkpoint provider is not configured: {provider_name!r}")
    provider = selected[0]
    model = cache_key.get("model")
    if isinstance(model, str) and model and provider.model != model:
        raise PrefixRegressionError("checkpoint model differs from configured provider")
    router = Diffundo(
        [provider],
        call_budget_s=max(1.0, provider.timeout_s),
        primary_provider=provider.name,
        task_id=task_id or "prefix-regression",
    )
    return _await(router.call(provider.tier, prompt, model=provider.model))


def _expected(value: str | Sequence[str]) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else list(value)
    result = tuple(
        item.strip()
        for value in values
        for item in value.replace("|", ",").split(",")
        if item.strip()
    )
    if not result or any(item not in ACTION_TYPES for item in result):
        raise PrefixRegressionError(f"expect must contain only {', '.join(ACTION_TYPES)}")
    return tuple(dict.fromkeys(result))


def run_prefix_regression(
    session_dir: str | Path,
    turn: int,
    expect: str | Sequence[str],
    *,
    task_id: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Replay one turn from the latest checkpoint at or before ``turn``."""
    if isinstance(turn, bool) or not isinstance(turn, int) or turn <= 0:
        raise PrefixRegressionError("turn must be a positive integer")
    expected = _expected(expect)
    session = Path(session_dir).expanduser().resolve()
    if not session.is_dir():
        raise PrefixRegressionError(f"session directory not found: {session}")
    candidate, path, events, tasks = _locate(session, turn, task_id)
    data = _json(path)
    metadata = _metadata(data)
    cache_key = dict(metadata["cache_key"])
    for key, value in _provider_hint(events, candidate).items():
        cache_key.setdefault(key, value)
    metadata["cache_key"] = cache_key
    resolved_task = task_id or candidate.task_id or metadata.get("task_id")
    prompt, kind = _prompt(
        data,
        tasks.get(resolved_task) if isinstance(resolved_task, str) else None,
        metadata["cache_key"],
    )
    result: dict[str, Any] = {
        "session": str(session),
        "task_id": resolved_task,
        "requested_turn": turn,
        "checkpoint_turn": candidate.turn,
        "replay_turn": candidate.turn + 1,
        "checkpoint": candidate.reference,
        "checkpoint_kind": kind,
        "prefix_messages": len(prompt["messages"]),
        "expected": list(expected),
    }
    try:
        response = transport(prompt) if transport is not None else _provider_response(
            prompt, metadata, resolved_task
        )
        result["action_class"] = _action(_await(response))
    except Exception as exc:  # provider errors are failed measurements, not crashes
        detail = str(exc).strip()
        result.update(
            {
                "passed": False,
                "action_class": None,
                "error": (
                    f"{exc.__class__.__name__}: {detail}"
                    if detail
                    else exc.__class__.__name__
                )[:500],
            }
        )
        return result
    result["passed"] = result["action_class"] in expected
    return result


def assert_prefix_regression(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a prefix regression and raise when its action is outside the set."""
    result = run_prefix_regression(*args, **kwargs)
    if not result["passed"]:
        raise AssertionError(
            f"prefix regression failed: expected {result['expected']}, "
            f"got {result.get('action_class') or result.get('error')}"
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--expect", action="append", required=True, metavar="ACTION")
    parser.add_argument("--task", default=None, help="task id when a session has multiple tasks")
    parser.add_argument("--json", action="store_true", help="emit one JSON result")
    args = parser.parse_args(argv)
    try:
        result = run_prefix_regression(
            args.session_dir, args.turn, args.expect, task_id=args.task
        )
    except PrefixRegressionError as exc:
        result = {"passed": False, "error": str(exc)}
        exit_code = 2
    else:
        exit_code = 0 if result["passed"] else 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result.get("passed"):
        print(f"PASS action={result['action_class']} checkpoint_turn={result['checkpoint_turn']}")
    else:
        print(f"FAIL {result.get('error', 'action outside expected set')}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
