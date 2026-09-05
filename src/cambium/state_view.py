"""One read-only current-state view for the model, CLI and terminal."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .branch_history import _session_event_stores
from .branch_state import BranchState, Identity, inspect_state, reduce
from .store import read_events_file


def load_state(session_dir: str | Path, task_id: str | None = None) -> BranchState:
    """Replay the latest relevant turn, never interleave turn-local sequence IDs."""
    for store in reversed(_session_event_stores(Path(session_dir))):
        events = read_events_file(store)
        if not events:
            continue
        if task_id is None:
            state = inspect_state(events)
        else:
            if not any(event.get("task_id") == task_id for event in events):
                continue
            descendants = {task_id}
            state = BranchState(identity=Identity(branch_id=task_id))
            for event in events:
                payload = dict(event.get("payload") or {})
                if payload.get("parent_task_id") in descendants:
                    child = payload.get("child_task_id")
                    if isinstance(child, str):
                        descendants.add(child)
                owner = event.get("task_id")
                if owner not in descendants:
                    continue
                if owner == task_id:
                    payload.pop("parent_task_id", None)
                    event = {**event, "parent_task_id": None, "payload": payload}
                state = reduce(state, event)
        return replace(state, identity=replace(
            state.identity, session_id=str(store.parent.parent),
        ))
    raise ValueError(f"no recorded state for {task_id or 'session'} under {session_dir}")


def state_text(session_dir: str | Path, task_id: str | None = None) -> str:
    """Bounded facts from the shared reducer; detailed evidence stays in history."""
    state = load_state(session_dir, task_id)
    data = state.to_dict()
    context = data["context"]
    result = data["result"]
    view: dict[str, Any] = {
        "identity": data["identity"],
        "source_watermark": state.source_watermark,
        "objective": data["mission"]["objective"],
        "artifacts": data["artifacts"],
        "control": data["control"],
        "resources": data["resources"],
        "usage": data["usage"],
        "context": {key: context.get(key) for key in (
            "epoch", "lineage", "summary_segments", "raw_tail_bytes", "checkpoint_ref",
        )},
        "children": [{key: child.get(key) for key in (
            "branch_id", "lifecycle", "provider", "model", "current_tool", "context_mode",
            "placement", "artifact_status", "accepted_artifact_head",
        )} for child in data["children"]],
        "result": {key: result.get(key) for key in ("status", "failure_reason", "summary")},
        "last_tool_output": (data.get("last_tool_output") or "")[-2000:],
    }
    # The reducer retains detailed history; this view is deliberately small.
    for key in ("objective",):
        if isinstance(view[key], str):
            view[key] = view[key][:2000]
    if isinstance(view["result"].get("summary"), str):
        view["result"]["summary"] = view["result"]["summary"][:2000]
    return json.dumps(view, ensure_ascii=False, indent=2)
