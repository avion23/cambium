"""One read-only current-state view for the model, CLI and terminal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .branch_history import _session_event_stores
from .branch_state import BranchState, Identity, inspect_state, reduce
from .situation import render_situation_frame
from .store import read_events_file


def _is_child_admission_for(event: Mapping[str, Any], task_id: str) -> bool:
    """Return whether a parent-owned admission names ``task_id``."""

    if event.get("kind") != "child_admitted":
        return False
    payload = event.get("payload")
    return isinstance(payload, Mapping) and payload.get("child_task_id") == task_id


def _event_parent_id(event: Mapping[str, Any]) -> str | None:
    """Return the durable parent identity carried by one child-owned event."""
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        for key in ("parent_task_id", "parent_branch_id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    for key in ("parent_task_id", "parent_branch_id"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _project_child_admission(
    state: BranchState, event: Mapping[str, Any], task_id: str
) -> BranchState:
    """Promote one parent-owned admission into the focused child state.

    ``child_admitted`` is owned by the parent, but its durable payload is the
    only evidence for a child that has not started yet.  The reducer already
    validates and records that event as a child, so use it once and promote
    the resulting child fields into the focused branch identity instead of
    manufacturing a child-owned event.
    """

    admitted = reduce(state, event)
    child = next(
        (candidate for candidate in admitted.children if candidate.branch_id == task_id),
        None,
    )
    if child is None:
        raise ValueError(f"child admission did not record child {task_id}")
    return replace(
        admitted,
        identity=replace(
            admitted.identity,
            branch_id=task_id,
            parent_branch_id=child.parent_branch_id,
            generation=child.generation,
            lifecycle=child.lifecycle,
            turn=child.turn,
        ),
        children=tuple(candidate for candidate in admitted.children if candidate is not child),
    )


def load_state(session_dir: str | Path, task_id: str | None = None) -> BranchState:
    """Replay the latest relevant turn, never interleave turn-local sequence IDs."""
    for store in reversed(_session_event_stores(Path(session_dir))):
        events = read_events_file(store)
        if not events:
            continue
        if task_id is None:
            state = inspect_state(events)
        else:
            if not any(
                event.get("task_id") == task_id or _is_child_admission_for(event, task_id)
                for event in events
            ):
                continue
            descendants = {task_id}
            state = BranchState(identity=Identity(branch_id=task_id))
            for event in events:
                payload = dict(event.get("payload") or {})
                if _is_child_admission_for(event, task_id):
                    state = _project_child_admission(state, event, task_id)
                    continue
                if payload.get("parent_task_id") in descendants:
                    child = payload.get("child_task_id")
                    if isinstance(child, str):
                        descendants.add(child)
                owner = event.get("task_id")
                if owner not in descendants:
                    continue
                if owner == task_id:
                    parent_id = _event_parent_id(event) or state.identity.parent_branch_id
                    payload.pop("parent_task_id", None)
                    payload.pop("parent_branch_id", None)
                    event = {
                        **event,
                        "parent_task_id": None,
                        "parent_branch_id": None,
                        "payload": payload,
                    }
                state = reduce(state, event)
                if owner == task_id and parent_id is not None:
                    state = replace(
                        state,
                        identity=replace(state.identity, parent_branch_id=parent_id),
                    )
        return replace(
            state,
            identity=replace(
                state.identity,
                session_id=str(store.parent.parent),
            ),
        )
    raise ValueError(f"no recorded state for {task_id or 'session'} under {session_dir}")


def state_text(session_dir: str | Path, task_id: str | None = None) -> str:
    """Use the same bounded projection as the worker; full JSON remains CLI-owned."""
    return render_situation_frame(load_state(session_dir, task_id))
