"""One read-only current-state view for the model, CLI and terminal."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .branch_history import _session_event_stores
from .branch_state import BranchState, Identity, inspect_state, reduce
from .situation import render_situation_frame
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
    """Use the same bounded projection as the worker; full JSON remains CLI-owned."""
    return render_situation_frame(load_state(session_dir, task_id))
