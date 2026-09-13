"""One read-only current-state view for the model, CLI and terminal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .branch_history import _session_event_stores
from .branch_state import (
    _GLOSSARY_KINDS,
    BranchState,
    Identity,
    _advance_metadata,
    _event_kind,
    reduce,
)
from .situation import render_situation_frame
from .store import iter_event_pages

_EVENT_PAGE_SIZE = 4096
_UNKNOWN_EVENT_KIND_LIMIT = 256


def _iter_store_events(store: Path):
    """Yield one durable store's rows without materializing the turn."""

    for page in iter_event_pages(store, page_size=_EVENT_PAGE_SIZE):
        yield from page


def _reduce_bounded(state: BranchState, event: Mapping[str, Any]) -> BranchState:
    """Fold one event without retaining an unbounded unknown-kind tuple."""

    kind = _event_kind(event)
    if kind is not None and kind in _GLOSSARY_KINDS:
        return reduce(state, event)
    updated = _advance_metadata(state, event, kind)
    unknown_kind = kind or "<missing>"
    kinds = (*updated.unknown_event_kinds, unknown_kind)
    if len(kinds) > _UNKNOWN_EVENT_KIND_LIMIT:
        kinds = kinds[-_UNKNOWN_EVENT_KIND_LIMIT:]
    return replace(
        updated,
        unknown_events=updated.unknown_events + 1,
        unknown_event_kinds=kinds,
    )


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
        if task_id is None:
            state = BranchState()
            for event in _iter_store_events(store):
                state = _reduce_bounded(state, event)
            if state.source_watermark == 0:
                continue
        else:
            descendants = {task_id}
            state = BranchState(identity=Identity(branch_id=task_id))
            found = False
            for event in _iter_store_events(store):
                payload = dict(event.get("payload") or {})
                if _is_child_admission_for(event, task_id):
                    found = True
                    state = _project_child_admission(state, event, task_id)
                    continue
                if event.get("task_id") == task_id:
                    found = True
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
                state = _reduce_bounded(state, event)
                if owner == task_id and parent_id is not None:
                    state = replace(
                        state,
                        identity=replace(state.identity, parent_branch_id=parent_id),
                    )
            if not found:
                continue
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
