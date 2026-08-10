"""Pure Architectus scheduling core.

Architectus owns task-tree policy, but it does not own process or filesystem
I/O.  :class:`ArchitectusCore` keeps the small amount of scheduling state that
is needed between waves and emits plain action dictionaries for a later
Custos integration.  The LLM is an injected port; :class:`ScriptedLLM` is the
deterministic test adapter for that port.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from .conversations import ConversationStore
from .tasktree import NodeStatus, TaskTree, ready_tasks, topological_order

_ENVELOPE_KEYS = (
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
_ENVELOPE_KEY_SET = frozenset(_ENVELOPE_KEYS)
CORE_DIRECTIVE_MAX = 200
_CORE_DIRECTIVE_TRUNCATION_MARKER = "... [truncated]"
_RETRY_REMAINING_FIELDS = ("retries_remaining", "retries_left", "attempts_remaining")
_RESET_RETRY_ATTEMPTED_FIELDS = (
    "reset_retry_attempted",
    "reset_attempted",
    "step_back_attempted",
)
_RESET_RETRY_CONSUMED_KEY = "reset_retry_consumed"


class ActionKind(StrEnum):
    """Action intents understood by the pure scheduling core."""

    SPAWN = "spawn"
    STEER = "steer"
    AGGREGATE = "aggregate"
    REPLAN = "replan"
    RESET_RETRY = "reset_retry"
    ABORT_SUBTREE = "abort_subtree"


class FailureDecision(StrEnum):
    """The finite set of deterministic failure-policy decisions."""

    RESTART = "restart"
    RESOLVE = "resolve"
    ABORT_SUBTREE = "abort-subtree"
    REPLAN = "replan"
    MERGE_RESOLVE = "merge-resolve"
    RESET_RETRY = "reset_retry"


@runtime_checkable
class LLM(Protocol):
    """Port for the Architectus decision model."""

    async def decide(
        self, tree_state: dict[str, Any], events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the next action intents for one scheduling wave."""


LLMCallable = Callable[
    [dict[str, Any], list[dict[str, Any]]],
    Sequence[Mapping[str, Any]] | Awaitable[Sequence[Mapping[str, Any]]],
]


class ScriptedLLM:
    """Deterministic fake implementation of :class:`LLM`.

    ``next_actions`` may be one action list, a list of action lists (one per
    call), or a callable with the same arguments as :meth:`LLM.decide`.  A
    callable may be synchronous or asynchronous.  Script exhaustion returns
    an empty action list, which makes a finite scenario deterministic.
    """

    def __init__(
        self,
        next_actions: Sequence[Any] | LLMCallable,
    ) -> None:
        self._callable: LLMCallable | None = None
        self._waves: list[list[dict[str, Any]]] = []
        self._index = 0
        self.calls: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []

        if callable(next_actions):
            self._callable = next_actions
            return

        if isinstance(next_actions, (str, bytes)):
            raise TypeError("next_actions must be action mappings, action lists, or a callable")
        scripted = list(next_actions)
        if not scripted:
            return
        if all(isinstance(item, Mapping) for item in scripted):
            self._waves.append(self._copy_action_list(scripted))
            return

        for index, wave in enumerate(scripted):
            if isinstance(wave, (str, bytes)) or not isinstance(wave, Sequence):
                raise TypeError(f"scripted wave {index} must be a sequence of action mappings")
            if not all(isinstance(item, Mapping) for item in wave):
                raise TypeError(f"scripted wave {index} must contain action mappings")
            self._waves.append(self._copy_action_list(wave))

    async def decide(
        self, tree_state: dict[str, Any], events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the next scripted or callable-produced action list."""
        state_copy = copy.deepcopy(tree_state)
        events_copy = copy.deepcopy(events)
        self.calls.append((state_copy, events_copy))

        if self._callable is not None:
            result = self._callable(state_copy, events_copy)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
                raise TypeError("LLM callable must return a sequence of action mappings")
            if not all(isinstance(item, Mapping) for item in result):
                raise TypeError("LLM callable must return action mappings")
            return self._copy_action_list(result)

        if self._index >= len(self._waves):
            return []
        result = self._waves[self._index]
        self._index += 1
        return copy.deepcopy(result)

    @staticmethod
    def _copy_action_list(actions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [copy.deepcopy(dict(action)) for action in actions]


def _event_fields(event: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten the common event envelope without mutating the input."""
    if not isinstance(event, Mapping):
        raise TypeError("failure event must be a mapping")
    fields = dict(event)
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            fields.setdefault(key, value)
    return fields


def _positive_retries(fields: Mapping[str, Any]) -> bool:
    """Return whether a failure event explicitly has a retry available."""
    remaining = _retry_remaining(fields)
    if remaining is not None:
        return remaining > 0
    retryable = fields.get("retryable")
    if isinstance(retryable, bool):
        return retryable
    retry_count = fields.get("retry_count")
    max_retries = fields.get("max_retries")
    if (
        isinstance(retry_count, int)
        and not isinstance(retry_count, bool)
        and isinstance(max_retries, int)
        and not isinstance(max_retries, bool)
    ):
        return retry_count < max_retries
    return False


def _retry_remaining(fields: Mapping[str, Any]) -> int | None:
    """Return the canonical retry count from the supported field aliases."""
    for key in _RETRY_REMAINING_FIELDS:
        value = fields.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _reset_retry_attempted(fields: Mapping[str, Any]) -> bool:
    """Return whether the one step-back retry already ran."""
    return any(fields.get(key) is True for key in _RESET_RETRY_ATTEMPTED_FIELDS)


def decide_failure(event: Mapping[str, Any]) -> str:
    """Apply the deterministic Architectus failure table.

    The function consumes only the event and returns a plain string so it can
    be used by a rule engine or by an injected LLM policy without state. A gate
    failure with ``retries_remaining == 0`` returns ``reset_retry`` on its first
    exhaustion. The executor maps that decision to ``{"action": "reset_retry",
    "task_id": ...}``; a later gate failure marked
    ``reset_retry_attempted=True`` returns ``abort-subtree``.
    """
    fields = _event_fields(event)
    raw_kind = fields.get("kind", fields.get("type", fields.get("event", "")))
    kind = str(raw_kind).lower().replace("-", "_")
    raw_reason = fields.get("reason", "")
    reason = str(raw_reason).lower().replace("-", "_")
    status = str(fields.get("status", "")).lower().replace("-", "_")

    if (
        kind in {"crash", "node_crash", "node_crashed", "worker_crash", "worker_crashed"}
        or reason == "crash"
    ):
        return FailureDecision.RESTART.value

    if (
        "fenc" in kind
        or "fenc" in reason
        or kind in {"generation_mismatch", "generation_mismatch_fencing"}
    ):
        return FailureDecision.ABORT_SUBTREE.value

    if (
        "budget" in kind
        or "budget" in reason
        or kind in {"timeout", "timed_out", "cap_exhausted", "budget_exceeded"}
        or bool(fields.get("budget_exceeded"))
    ):
        return FailureDecision.ABORT_SUBTREE.value

    if kind in {
        "cyclic_plan",
        "cycle",
        "multi_parent",
        "over_depth",
        "over_width",
        "invalid_plan",
        "plan_rejected",
        "plan_error",
    } or reason in {"cycle", "cyclic", "multi_parent", "over_depth", "over_width"}:
        return FailureDecision.REPLAN.value

    if kind in {"provider_exhaustion", "providers_exhausted", "provider_unavailable"}:
        # The table pauses dispatch and waits for recovery.  ``resolve`` is
        # the non-dispatching decision available in this S1 action vocabulary.
        return FailureDecision.RESOLVE.value

    if kind == "merge_failed" or kind.startswith("merge_"):
        if reason == "conflict":
            return FailureDecision.REPLAN.value
        if reason in {"test_failure", "non_fast_forward"}:
            return FailureDecision.MERGE_RESOLVE.value
        return FailureDecision.MERGE_RESOLVE.value

    if kind in {"gate_failed", "gate_failure"} or "gate" in kind and "fail" in kind:
        if _positive_retries(fields):
            return FailureDecision.RESOLVE.value
        if _retry_remaining(fields) == 0:
            if _reset_retry_attempted(fields):
                return FailureDecision.ABORT_SUBTREE.value
            return FailureDecision.RESET_RETRY.value
        if fields.get("retries_left") == 0 or fields.get("exhausted"):
            return FailureDecision.ABORT_SUBTREE.value
        return FailureDecision.RESOLVE.value

    if kind in {"spec_error", "config_error", "configuration_error"} or (
        fields.get("recoverable") is False
    ):
        return FailureDecision.ABORT_SUBTREE.value

    if status in {"failed", "failure", "error"} or kind in {
        "failed",
        "node_failed",
        "result_failed",
        "task_failed",
    }:
        if _positive_retries(fields):
            return FailureDecision.RESOLVE.value
        return FailureDecision.ABORT_SUBTREE.value

    raise ValueError(f"unclassified failure event: {dict(event)!r}")


class ArchitectusCore:
    """Dependency-gated, bounded-width scheduling state machine."""

    def __init__(
        self,
        llm: LLM,
        *,
        tree: TaskTree,
        store: ConversationStore | None = None,
        max_width: int = 8,
        core_directive: str | None = None,
        durable_state: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(tree, TaskTree):
            raise TypeError("tree must be a TaskTree")
        if isinstance(max_width, bool) or not isinstance(max_width, int) or max_width < 1:
            raise ValueError("max_width must be a positive integer")
        if not hasattr(llm, "decide") or not callable(llm.decide):
            raise TypeError("llm must implement async decide(tree_state, events)")

        self._llm = llm
        self._tree = tree
        self._store = store
        self._max_width = max_width
        root_goal = self._root_goal(tree)
        if root_goal is not None and core_directive is not None and core_directive != root_goal:
            raise ValueError("core_directive must match the root goal")
        directive = root_goal if root_goal is not None else core_directive
        self._core_directive = self._normalise_core_directive(directive)
        if self._core_directive is None:
            raise ValueError("root directive is required")
        self._topological = topological_order(tree)
        self._nodes = {node.task_id: node for node in tree.nodes}
        self._children: dict[str, list[str]] = {node.task_id: [] for node in tree.nodes}
        for parent, child in tree.edges:
            self._children[parent].append(child)
        for children in self._children.values():
            children.sort(key=lambda task_id: self._node_order_key(self._nodes[task_id]))

        self._finished: dict[str, dict[str, Any]] = {}
        self._in_flight: set[str] = set()
        self._failed_subtrees: set[str] = set()
        self._reset_retry_tasks = self._restore_reset_retry_tasks(durable_state)
        self._action_history: list[dict[str, Any]] = []

    @property
    def tree(self) -> TaskTree:
        """The frozen task tree owned by this core."""
        return self._tree

    @property
    def finished(self) -> dict[str, dict[str, Any]]:
        """A defensive copy of accepted upward envelopes by task id."""
        return copy.deepcopy(self._finished)

    @property
    def in_flight(self) -> frozenset[str]:
        """Task ids admitted by spawn actions and not yet aggregated."""
        return frozenset(self._in_flight)

    @property
    def action_history(self) -> list[dict[str, Any]]:
        """A defensive copy of actions accepted from the LLM."""
        return copy.deepcopy(self._action_history)

    @property
    def reset_retry_tasks(self) -> frozenset[str]:
        """Task ids that have consumed their one architecture-owned reset retry."""
        return frozenset(self._reset_retry_tasks)

    @property
    def durable_state(self) -> dict[str, Any]:
        """Return the JSON-friendly state required to reconstruct this core."""
        return {_RESET_RETRY_CONSUMED_KEY: sorted(self._reset_retry_tasks)}

    async def step(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run one scheduling wave and return actions for the execution edge."""
        if not isinstance(events, list) or not all(isinstance(event, dict) for event in events):
            raise TypeError("events must be a list of dictionaries")

        failure_actions = self._failure_actions(events)
        if failure_actions:
            for action in failure_actions:
                self._record_action(action)
            return copy.deepcopy(failure_actions)

        blocked = self._blocked_task_ids()
        ready = [
            node
            for node in ready_tasks(self._tree, set(self._finished) | blocked)
            if node.task_id not in self._in_flight and node.task_id not in blocked
        ]
        state = self._tree_state(ready, blocked)
        proposed = await self._llm.decide(copy.deepcopy(state), copy.deepcopy(events))
        if isinstance(proposed, (str, bytes)) or not isinstance(proposed, Sequence):
            raise TypeError("LLM decide must return a sequence of action mappings")

        actions = self._admit_actions(proposed, ready)
        for action in actions:
            self._record_action(action)
        return copy.deepcopy(actions)

    def compose_context(self, task_id: str) -> dict[str, Any]:
        """Compose a static prefix followed by an info-hidden dynamic tail.

        The returned shape is deliberately JSON-friendly:

        ``static_prefix``
            Ordered system and module-instruction strings.
        ``dynamic_tail``
            Ordered records for the node's own conversation, parent summary,
            direct-child I2.7 envelopes, and relevant file names.
        ``prompt``
            A deterministic text rendering with the same static-before-
            dynamic ordering.  The static prefix is never evicted; dynamic
            records are evicted from their least-recent ends first.

        The root directive is compiled at construction and is the unalterable
        goal shared by all sub-agent contexts. Its deterministic token count is
        capped at :data:`CORE_DIRECTIVE_MAX`.
        """
        node = self._node(task_id)
        config = self._config_for(node)
        static_prefix = self._static_prefix(config, self._core_directive)
        dynamic_tail = self._dynamic_tail(node, config)
        max_tokens = self._context_budget(config)
        truncated = False

        if max_tokens is not None:
            static_tokens = sum(self._estimate_tokens(value) for value in static_prefix)
            truncated = self._evict_dynamic_tail(
                dynamic_tail,
                max_tokens=max_tokens,
                static_tokens=static_tokens,
            )

        rendered = [*static_prefix, *(self._render_dynamic(segment) for segment in dynamic_tail)]
        return {
            "task_id": task_id,
            "static_prefix": static_prefix,
            "dynamic_tail": dynamic_tail,
            "prompt": "\n".join(rendered),
            "truncated": truncated,
        }

    def aggregate(self, task_id: str, envelope: dict[str, Any]) -> None:
        """Accept one exact I2.7 envelope and mark its node finished."""
        self._node(task_id)
        if not isinstance(envelope, dict):
            raise TypeError("envelope must be a dictionary")
        keys = set(envelope)
        if keys != _ENVELOPE_KEY_SET:
            extras = sorted(keys - _ENVELOPE_KEY_SET)
            missing = sorted(_ENVELOPE_KEY_SET - keys)
            details: list[str] = []
            if extras:
                details.append(f"unknown keys: {extras}")
            if missing:
                details.append(f"missing keys: {missing}")
            raise ValueError("invalid upward envelope; " + "; ".join(details))
        expected_parent = self._nodes[task_id].parent_task_id
        if envelope["parent_task_id"] != expected_parent:
            raise ValueError(
                f"envelope parent_task_id {envelope['parent_task_id']!r} does not match "
                f"task {task_id!r} parent {expected_parent!r}"
            )
        if task_id in self._finished:
            raise ValueError(f"task {task_id!r} is already aggregated")

        accepted = copy.deepcopy(envelope)
        self._finished[task_id] = accepted
        self._in_flight.discard(task_id)
        if self._is_failed_status(accepted["status"]):
            self._failed_subtrees.add(task_id)

    @staticmethod
    def decide_failure(event: Mapping[str, Any]) -> str:
        """Apply the pure failure table without reading core state."""
        return decide_failure(event)

    def _failure_actions(self, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Route all exhausted gate failures through the stateful failure edge."""
        classified: list[tuple[str, dict[str, Any], str]] = []
        for event in events:
            try:
                decision = decide_failure(event)
            except ValueError:
                continue
            if decision not in {
                FailureDecision.RESET_RETRY.value,
                FailureDecision.ABORT_SUBTREE.value,
            }:
                continue
            fields = _event_fields(event)
            task_id = fields.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(f"{decision} failure event requires a non-empty task_id")
            self._node(task_id)
            classified.append((decision, fields, task_id))

        actions: list[dict[str, Any]] = []
        for decision, fields, task_id in classified:
            if (
                decision == FailureDecision.RESET_RETRY.value
                and task_id not in self._reset_retry_tasks
                and not _reset_retry_attempted(fields)
            ):
                self._reset_retry_tasks.add(task_id)
                actions.append({"action": ActionKind.RESET_RETRY.value, "task_id": task_id})
                continue

            self._mark_subtree_failed(task_id)
            actions.append({"action": ActionKind.ABORT_SUBTREE.value, "task_id": task_id})
        return actions

    def _mark_subtree_failed(self, task_id: str) -> None:
        """Record an abort and release the failed task's execution slot."""
        self._failed_subtrees.add(task_id)
        self._in_flight.discard(task_id)

    def _restore_reset_retry_tasks(
        self, durable_state: Mapping[str, Any] | None
    ) -> set[str]:
        """Restore reset consumption from the durable construction snapshot."""
        if durable_state is None:
            return set()
        if not isinstance(durable_state, Mapping):
            raise TypeError("durable_state must be a mapping")

        consumed = durable_state.get(_RESET_RETRY_CONSUMED_KEY, ())
        if isinstance(consumed, (str, bytes)) or not isinstance(
            consumed, (list, tuple, set, frozenset)
        ):
            raise TypeError("durable_state.reset_retry_consumed must be a sequence")

        restored: set[str] = set()
        for task_id in consumed:
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("durable_state.reset_retry_consumed must contain task ids")
            self._node(task_id)
            restored.add(task_id)
        return restored

    def _admit_actions(
        self,
        proposed: Sequence[Mapping[str, Any]],
        ready: Sequence[Any],
    ) -> list[dict[str, Any]]:
        ready_ids = [node.task_id for node in ready]
        ready_rank = {task_id: index for index, task_id in enumerate(ready_ids)}
        capacity = max(self._max_width - len(self._in_flight), 0)
        accepted_spawn_ids: list[str] = []
        aborted_spawn_ids: set[str] = set()
        non_spawn: list[tuple[int, dict[str, Any]]] = []
        spawn_positions: list[int] = []

        for raw_index, raw_action in enumerate(proposed):
            if not isinstance(raw_action, Mapping):
                raise TypeError("each LLM action must be a mapping")
            action = self._normalise_action(raw_action)
            kind = ActionKind(action["action"])
            if kind is ActionKind.SPAWN:
                task_id = self._action_task_id(action)
                if (
                    task_id not in ready_rank
                    or task_id in accepted_spawn_ids
                    or len(accepted_spawn_ids) >= capacity
                ):
                    continue
                accepted_spawn_ids.append(task_id)
                spawn_positions.append(raw_index)
                continue

            if kind in {
                ActionKind.STEER,
                ActionKind.AGGREGATE,
                ActionKind.RESET_RETRY,
                ActionKind.ABORT_SUBTREE,
            }:
                task_id = self._action_task_id(action)
                self._node(task_id)
                if kind is ActionKind.RESET_RETRY:
                    if task_id in self._reset_retry_tasks:
                        action = {
                            "action": ActionKind.ABORT_SUBTREE.value,
                            "task_id": task_id,
                        }
                        self._mark_subtree_failed(task_id)
                        aborted_spawn_ids.add(task_id)
                    else:
                        self._reset_retry_tasks.add(task_id)
                elif kind is ActionKind.ABORT_SUBTREE:
                    self._mark_subtree_failed(task_id)
                    aborted_spawn_ids.add(task_id)
            non_spawn.append((raw_index, action))

        accepted_spawn_ids.sort(key=ready_rank.__getitem__)
        spawn_iter = iter(accepted_spawn_ids)
        actions: list[dict[str, Any]] = []
        spawn_position_set = set(spawn_positions)
        for raw_index, _raw_action in enumerate(proposed):
            if raw_index in spawn_position_set:
                task_id = next(spawn_iter)
                action = {"action": ActionKind.SPAWN.value, "task_id": task_id}
                actions.append(action)
                continue
            for index, action in non_spawn:
                if index == raw_index:
                    actions.append(action)
                    break

        for task_id in accepted_spawn_ids:
            if task_id not in aborted_spawn_ids:
                self._in_flight.add(task_id)
        return actions

    def _record_action(self, action: dict[str, Any]) -> None:
        self._action_history.append(copy.deepcopy(action))

    def _normalise_action(self, raw_action: Mapping[str, Any]) -> dict[str, Any]:
        action = copy.deepcopy(dict(raw_action))
        raw_kind = action.get("action")
        if isinstance(raw_kind, ActionKind):
            kind = raw_kind
        elif isinstance(raw_kind, str):
            try:
                kind = ActionKind(raw_kind)
            except ValueError as exc:
                raise ValueError(f"unknown Architectus action {raw_kind!r}") from exc
        else:
            raise ValueError("Architectus action requires an 'action' kind")
        action["action"] = kind.value
        return action

    @staticmethod
    def _action_task_id(action: Mapping[str, Any]) -> str:
        task_id = action.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError(f"{action['action']} action requires a non-empty task_id")
        return task_id

    def _tree_state(self, ready: Sequence[Any], blocked: set[str]) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        for node in sorted(self._tree.nodes, key=self._node_order_key):
            if node.task_id in self._finished:
                status = self._finished[node.task_id]["status"]
            elif node.task_id in blocked:
                status = NodeStatus.FAILED.value
            elif node.task_id in self._in_flight:
                status = NodeStatus.RUNNING.value
            else:
                status = node.status.value
            nodes.append(
                {
                    "task_id": node.task_id,
                    "kind": node.kind.value,
                    "parent_task_id": node.parent_task_id,
                    "depth": node.depth,
                    "width_idx": node.width_idx,
                    "status": status,
                }
            )
        return {
            "nodes": nodes,
            "edges": [list(edge) for edge in self._tree.edges],
            "topological_order": list(self._topological),
            "ready": [node.task_id for node in ready],
            "in_flight": sorted(self._in_flight),
            "finished": copy.deepcopy(self._finished),
            "blocked": sorted(blocked),
        }

    def _blocked_task_ids(self) -> set[str]:
        blocked: set[str] = set()
        for root in self._failed_subtrees:
            pending = [root]
            while pending:
                task_id = pending.pop()
                if task_id in blocked:
                    continue
                blocked.add(task_id)
                pending.extend(self._children.get(task_id, ()))
        return blocked

    def _dynamic_tail(self, node: Any, config: Mapping[str, Any]) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        if self._store is not None:
            for record in self._store.history(node.task_id):
                segments.append({"kind": "conversation", "record": copy.deepcopy(record)})

        if node.parent_task_id is not None:
            parent_envelope = self._finished.get(node.parent_task_id)
            parent_summary = (
                parent_envelope.get("summary", "") if parent_envelope is not None else ""
            )
            if not parent_summary:
                parent_summary = self._nodes[node.parent_task_id].spec.get("summary", "")
            if isinstance(parent_summary, str) and parent_summary:
                segments.append({"kind": "parent_summary", "content": parent_summary})

        for child_id in self._children[node.task_id]:
            envelope = self._finished.get(child_id)
            if envelope is not None:
                # The stored object has already passed exact-key validation.
                segments.append(
                    {"kind": "child_envelope", "envelope": copy.deepcopy(envelope)}
                )

        files = self._relevant_files(node, config)
        for path in files:
            segments.append({"kind": "file", "path": path})
        return segments

    @staticmethod
    def _relevant_files(node: Any, config: Mapping[str, Any]) -> list[str]:
        raw_files: Any = config.get("relevant_files", config.get("files", ()))
        if isinstance(raw_files, str):
            raw_files = [raw_files]
        files: list[str] = [path for path in raw_files if isinstance(path, str)] if isinstance(
            raw_files, Sequence
        ) else []
        changed_files = config.get("files_changed", ())
        if isinstance(changed_files, str):
            changed_files = [changed_files]
        for value in changed_files if isinstance(changed_files, Sequence) else ():
            if isinstance(value, str) and value not in files:
                files.append(value)
        del node
        return files

    @staticmethod
    def _config_for(node: Any) -> Mapping[str, Any]:
        config = node.spec.get("config")
        if config is None:
            config = node.spec
        if not isinstance(config, Mapping):
            raise TypeError(f"task {node.task_id!r} config must be a mapping")
        merged = dict(node.spec)
        merged.update(config)
        return merged

    @staticmethod
    def _static_prefix(
        config: Mapping[str, Any], core_directive: str | None = None
    ) -> list[str]:
        static = config.get("static", {})
        static_config = static if isinstance(static, Mapping) else {}
        system = static_config.get(
            "system",
            static_config.get(
                "system_prompt", config.get("system", config.get("system_prompt", ""))
            ),
        )
        module = static_config.get(
            "module_instructions",
            static_config.get(
                "module",
                config.get(
                    "module_instructions", config.get("module", config.get("instructions", ""))
                ),
            ),
        )
        values: list[str] = []
        if core_directive is not None:
            values.append(core_directive)
        for label, value in (("system", system), ("module_instructions", module)):
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(f"{label} must be a string")
            if value:
                values.append(value)
        return values

    @staticmethod
    def _root_goal(tree: TaskTree) -> str | None:
        """Read an optional root ``goal`` carried in the root task spec."""
        root = next(node for node in tree.nodes if node.parent_task_id is None)
        goal = root.spec.get("goal")
        if goal is None:
            config = root.spec.get("config")
            if isinstance(config, Mapping):
                goal = config.get("goal")
        return goal

    @staticmethod
    def _normalise_core_directive(core_directive: str | None) -> str | None:
        """Validate and cap a core directive without mutating caller data."""
        if core_directive is None:
            return None
        if not isinstance(core_directive, str):
            raise TypeError("core_directive must be a string")
        tokens = core_directive.split()
        if not tokens:
            return None
        if ArchitectusCore._estimate_tokens(core_directive) <= CORE_DIRECTIVE_MAX:
            return core_directive
        marker_tokens = _CORE_DIRECTIVE_TRUNCATION_MARKER.split()
        keep = CORE_DIRECTIVE_MAX - ArchitectusCore._estimate_tokens(
            _CORE_DIRECTIVE_TRUNCATION_MARKER
        )
        return " ".join([*tokens[:keep], *marker_tokens])

    @staticmethod
    def _context_budget(config: Mapping[str, Any]) -> int | None:
        budget = config.get("budget", {})
        budget_config = budget if isinstance(budget, Mapping) else {}
        context = config.get("context", {})
        context_config = context if isinstance(context, Mapping) else {}
        raw = budget_config.get(
            "max_tokens",
            context_config.get(
                "max_tokens",
                config.get(
                    "max_tokens", config.get("context_budget", config.get("max_context_tokens"))
                ),
            ),
        )
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("context max_tokens must be a non-negative integer")
        return raw

    @classmethod
    def _evict_dynamic_tail(
        cls,
        segments: list[dict[str, Any]],
        *,
        max_tokens: int,
        static_tokens: int,
    ) -> bool:
        truncated = False
        while (
            static_tokens + sum(cls._segment_tokens(segment) for segment in segments) > max_tokens
        ):
            index = cls._oldest_segment_index(segments, "conversation")
            if index is None:
                index = cls._oldest_segment_index(segments, "child_envelope")
            if index is None:
                index = cls._oldest_segment_index(segments, "file")
            if index is None:
                index = cls._oldest_segment_index(segments, "parent_summary")
            if index is None:
                break
            segments.pop(index)
            truncated = True
        return truncated

    @staticmethod
    def _oldest_segment_index(segments: Sequence[Mapping[str, Any]], kind: str) -> int | None:
        for index, segment in enumerate(segments):
            if segment.get("kind") == kind:
                return index
        return None

    @classmethod
    def _segment_tokens(cls, segment: Mapping[str, Any]) -> int:
        return cls._estimate_tokens(cls._render_dynamic(segment))

    @staticmethod
    def _estimate_tokens(value: str) -> int:
        return max(1, len(value.split())) if value else 0

    @staticmethod
    def _render_dynamic(segment: Mapping[str, Any]) -> str:
        kind = segment.get("kind")
        if kind == "conversation":
            record = segment.get("record", {})
            if isinstance(record, Mapping):
                return str(record.get("content", ""))
        if kind == "parent_summary":
            return str(segment.get("content", ""))
        if kind == "child_envelope":
            envelope = segment.get("envelope", {})
            return json.dumps(envelope, sort_keys=True, default=str)
        if kind == "file":
            return str(segment.get("path", ""))
        return json.dumps(dict(segment), sort_keys=True, default=str)

    def _node(self, task_id: str) -> Any:
        if not isinstance(task_id, str) or task_id not in self._nodes:
            raise ValueError(f"unknown task_id {task_id!r}")
        return self._nodes[task_id]

    @staticmethod
    def _node_order_key(node: Any) -> tuple[int, int, str]:
        return (node.depth, node.width_idx, node.task_id)

    @staticmethod
    def _is_failed_status(status: Any) -> bool:
        return str(status).lower() in {"failed", "failure", "error"}


__all__ = [
    "ActionKind",
    "ArchitectusCore",
    "CORE_DIRECTIVE_MAX",
    "FailureDecision",
    "LLM",
    "ScriptedLLM",
    "decide_failure",
]
