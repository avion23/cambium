"""TaskTree — deterministic task-DAG builder and supervisor scheduler inputs.

Implements the Task Tree of the current architecture (§3.7, invariants
I2.1-I2.7) as a pure JSON-in/JSON-out module under the normative module
template:

- I2.1 single root per session; every non-root node has exactly one parent
  (``parent_task_id``, event-schema-draft §3.1/§3.10, payload-first).
- I2.2 the decomposition graph is a DAG: no cycles, no self-loops, no
  multi-parent in v2. Cycle detection = Kahn topological sort on the
  decomposition graph before dispatch (architecture §18.1 DS-M6); a cycle is
  rejected with the cycle path named.
- I2.3 depth/width bounds: ``max_depth`` (default 3) is enforced at build
  time. The build-time ``max_width`` is a per-parent fan-out bound; the
  session-wide parallel-worker cap of the same name is the supervisor's job
  at dispatch (architecture §3.7 I2.3: "``max_width`` (per-session parallel
  worker cap, config) are enforced by the supervisor at dispatch").
- I2.4 info hiding: :func:`subtree_of` gives a node only its own subtree —
  never a sibling's context.
- I2.7 information hiding (envelope-only upward results): a node's upward
  envelope carries **exactly** the current arch §3.4 key set — ``parent_task_id``,
  ``unified_diff``, ``diff_truncated``, ``summary``, ``metric_score``,
  ``metric_breakdown``, ``commits``, ``files_changed``, terminal ``status`` —
  never the scratchpad/chain-of-thought/trajectory. The rule is structural,
  not a prompt convention.

The tree is frozen and stateless; the supervisor tracks progress externally
via the ``finished`` set it feeds to :func:`ready_tasks`. No hidden globals,
no mutable module state.
"""

from __future__ import annotations

import argparse
import copy
import heapq
import json
import sys
from dataclasses import dataclass
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from .cli import _SafeArgumentParser

# Build-time per-parent fan-out bound (I2.3). This is NOT the session-wide
# parallel-worker cap of the same name: per architecture §3.7 I2.3 that cap
# is "enforced by the supervisor at dispatch" and is the supervisor's job.
# The build-time bound is the stricter structural check — no single node may
# have more than this many children.
MAX_DEPTH = 3
MAX_WIDTH = 8

# Upward result envelope: the current arch §3.4/§3.7 I2.7 field set, exactly.
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


class TaskKind(Enum):
    """The kind of work a task node performs (the enum norm; no str allowlists)."""

    FEATURE = "feature"
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    TEST = "test"
    DOCS = "docs"
    INVESTIGATION = "investigation"


class NodeStatus(StrEnum):
    """Lifecycle state of one task node (architecture §7.1 state machine).

    Nodes are immutable once built; the supervisor transitions external state
    and feeds ``finished`` sets to :func:`ready_tasks`.
    """

    PENDING = "pending"
    SPAWNING = "spawning"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"
    CRASHED = "crashed"


class TaskTreeError(Exception):
    """Base error for task-tree construction and traversal."""


class TaskPlanError(TaskTreeError):
    """A planner payload is malformed (shape, types, or missing fields)."""


class DuplicateTaskError(TaskTreeError):
    """Two tasks in the plan share a ``task_id``."""


class MissingDependencyError(TaskTreeError):
    """A task's ``depends_on`` names a task that is not in the plan."""


class NoRootError(TaskTreeError):
    """The plan does not have exactly one root task (I2.1)."""


class MultiParentError(TaskTreeError):
    """A task depends on more than one task; v2 forbids multi-parent (I2.2)."""


class CycleError(TaskTreeError):
    """The dependency graph contains a cycle (I2.2); the cycle path is named."""


class DepthBoundError(TaskTreeError):
    """A node's depth exceeds ``max_depth`` (I2.3)."""


class WidthBoundError(TaskTreeError):
    """A node's fan-out exceeds the build-time ``max_width`` bound.

    This is the stricter per-parent structural check at build time. The
    session-wide parallel-worker cap — also called ``max_width`` in the
    architecture (I2.3) — is a dispatch-time config enforced by the
    supervisor and is not implemented here.
    """


@dataclass(frozen=True, slots=True)
class TaskNode:
    """One node of the task tree: a sub-LLM session (a worker)."""

    task_id: str
    kind: TaskKind
    parent_task_id: str | None
    spec: dict[str, Any]
    depth: int
    width_idx: int
    status: NodeStatus


@dataclass(frozen=True, slots=True)
class TaskTree:
    """A validated task DAG: its nodes and ``(parent, child)`` edges."""

    nodes: tuple[TaskNode, ...]
    edges: tuple[tuple[str, str], ...]


def _parse_kind(raw: Any) -> TaskKind:
    """Accept a TaskKind, its value string, or its member name (the enum norm)."""
    if isinstance(raw, TaskKind):
        return raw
    if isinstance(raw, str):
        try:
            return TaskKind(raw)
        except ValueError:
            pass
        try:
            return TaskKind[raw]
        except KeyError:
            pass
    raise TaskPlanError(
        f"invalid task kind {raw!r}; expected one of "
        f"{', '.join(member.name for member in TaskKind)}"
    )


def _find_cycle(task_ids: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
    """Return one directed cycle as ``[a, b, c, a]``, or None if acyclic.

    Iterative-color DFS over ``edges`` in ``(parent, child)`` direction. A
    back-edge to a gray (on-stack) node closes a cycle; the trace from that
    node to the back-edge target, plus the target, is the named cycle.
    """
    # graphlib.TopologicalSorter was considered; its CycleError does expose the
    # cycle path via .args[1]. The iterative-color DFS is retained for
    # deterministic ordering and cycle-message control.
    children: dict[str, list[str]] = {tid: [] for tid in task_ids}
    for parent, child in edges:
        children.setdefault(parent, []).append(child)
    white, gray, black = 0, 1, 2
    color: dict[str, int] = {tid: white for tid in task_ids}
    trace: list[str] = []
    sorted_children = {tid: sorted(kids) for tid, kids in children.items()}

    for tid in sorted(task_ids):
        if color[tid] != white:
            continue
        color[tid] = gray
        trace.append(tid)
        stack: list[tuple[str, int]] = [(tid, 0)]
        while stack:
            current, child_index = stack[-1]
            current_children = sorted_children.get(current, [])
            if child_index == len(current_children):
                stack.pop()
                trace.pop()
                color[current] = black
                continue

            child = current_children[child_index]
            stack[-1] = (current, child_index + 1)
            child_color = color.get(child, white)
            if child_color == gray:
                start = trace.index(child)
                return trace[start:] + [child]
            if child_color == white:
                color[child] = gray
                trace.append(child)
                stack.append((child, 0))
    return None


def _assign_layout(
    children: dict[str, list[str]], root: str
) -> tuple[dict[str, int], dict[str, int]]:
    """Return ``({task_id: depth}, {task_id: width_idx})`` for a rooted tree.

    BFS from the root; a node's ``width_idx`` is its position among its
    parent's children, children ordered by ``task_id`` for determinism.
    """
    depth: dict[str, int] = {}
    width_idx: dict[str, int] = {}
    depth[root] = 0
    width_idx[root] = 0
    queue = [root]
    head = 0
    while head < len(queue):
        parent = queue[head]
        head += 1
        for index, child in enumerate(sorted(children.get(parent, []))):
            depth[child] = depth[parent] + 1
            width_idx[child] = index
            queue.append(child)
    return depth, width_idx


def build_tree(
    plan: dict[str, Any],
    *,
    max_depth: int = MAX_DEPTH,
    max_width: int = MAX_WIDTH,
) -> TaskTree:
    """Build and validate a :class:`TaskTree` from a planner payload.

    ``plan`` is ``{"tasks": [{"task_id", "kind", "depends_on", "spec"?}, ...]}``
    matching the ``task_decomposed``/``submitted`` payloads (event-schema-draft
    §3.1/§3.10). ``depends_on`` is the parent link; a task with one dependency
    is that dependency's child. Validates, in order:

    1. unique ``task_id`` values,
    2. every ``depends_on`` reference exists,
    3. exactly one root (I2.1),
    4. no multi-parent (I2.2),
    5. ``depth <= max_depth`` (before cycle analysis),
    6. no cycles (I2.2) — raises :class:`CycleError` naming the cycle,
    7. per-parent fan-out ``<= max_width``.

    Both bounds in (5) and (7) are build-time structural checks. The session-wide
    ``max_width`` parallel-worker cap named in architecture §3.7 I2.3
    ("``max_width`` (per-session parallel worker cap, config) are enforced by
    the supervisor at dispatch") is the supervisor's job, not this module's.

    Raises :class:`TaskTreeError` (a subclass) on any violation.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("tasks"), list):
        raise TaskPlanError("plan must be a dict with a 'tasks' list")
    if not isinstance(max_depth, int) or isinstance(max_depth, bool):
        raise TaskPlanError("max_depth must be an integer")
    if not isinstance(max_width, int) or isinstance(max_width, bool):
        raise TaskPlanError("max_width must be an integer")
    tasks = plan["tasks"]

    order: list[str] = []
    kinds: dict[str, TaskKind] = {}
    specs: dict[str, dict[str, Any]] = {}
    deps: dict[str, list[str]] = {}
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise TaskPlanError(f"tasks[{index}] is not an object")
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise TaskPlanError(f"tasks[{index}] has invalid task_id {task_id!r}")
        if task_id in kinds:
            raise DuplicateTaskError(f"duplicate task_id {task_id!r}")
        order.append(task_id)
        kinds[task_id] = _parse_kind(task.get("kind"))
        raw_deps = task.get("depends_on", [])
        if raw_deps is None:
            raw_deps = []
        if not isinstance(raw_deps, list) or not all(
            isinstance(dep, str) and dep for dep in raw_deps
        ):
            raise TaskPlanError(f"task {task_id!r} has invalid depends_on {raw_deps!r}")
        spec = task.get("spec", {})
        if not isinstance(spec, dict):
            raise TaskPlanError(f"task {task_id!r} has a non-dict spec")
        deps[task_id] = list(raw_deps)
        try:
            specs[task_id] = copy.deepcopy(spec)
        except RecursionError as exc:
            raise TaskPlanError(f"task {task_id!r} spec is too deeply nested") from exc

    known = set(order)
    for tid, task_deps in deps.items():
        for dep in task_deps:
            if dep not in known:
                raise MissingDependencyError(f"task {tid!r} depends on unknown task {dep!r}")

    roots = [tid for tid in order if not deps[tid]]
    if len(roots) != 1:
        raise NoRootError(
            f"expected exactly one root task, found {len(roots)}: {roots or '(none)'}"
        )
    root = roots[0]

    for tid, task_deps in deps.items():
        if len(task_deps) > 1:
            raise MultiParentError(
                f"task {tid!r} depends on {task_deps!r}; v2 forbids multi-parent "
                "(I2.2): a task-tree node has exactly one parent"
            )

    children: dict[str, list[str]] = {tid: [] for tid in order}
    edges: list[tuple[str, str]] = []
    for tid, task_deps in deps.items():
        for dep in task_deps:
            children[dep].append(tid)
            edges.append((dep, tid))

    depth, width_idx = _assign_layout(children, root)
    # The rooted traversal is iterative. Check its depth before cycle analysis
    # so an oversized valid tree raises the documented bound error directly.
    for tid in order:
        if tid not in depth:
            continue
        task_depth = depth[tid]
        if task_depth > max_depth:
            raise DepthBoundError(
                f"task {tid!r} has depth {task_depth}, exceeding max_depth {max_depth}"
            )

    cycle = _find_cycle(order, edges)
    if cycle is not None:
        raise CycleError(f"cycle in task DAG: {' -> '.join(cycle)}")

    for parent, kids in children.items():
        if len(kids) > max_width:
            raise WidthBoundError(
                f"task {parent!r} fans out to {len(kids)} children, "
                f"exceeding max_width {max_width}"
            )

    nodes = tuple(
        TaskNode(
            task_id=tid,
            kind=kinds[tid],
            parent_task_id=deps[tid][0] if deps[tid] else None,
            spec=specs[tid],
            depth=depth[tid],
            width_idx=width_idx[tid],
            status=NodeStatus.PENDING,
        )
        for tid in order
    )
    return TaskTree(nodes=nodes, edges=tuple(sorted(edges)))


def topological_order(tree: TaskTree) -> list[str]:
    """Return the task ids in a deterministic topological order (Kahn).

    Raises :class:`CycleError` naming the cycle if ``tree`` is cyclic (I2.2)
    — this is the dispatch-time cycle check (architecture §18.1 DS-M6).
    """
    in_degree: dict[str, int] = {node.task_id: 0 for node in tree.nodes}
    children: dict[str, list[str]] = {node.task_id: [] for node in tree.nodes}
    for parent, child in tree.edges:
        children.setdefault(parent, []).append(child)
        in_degree[child] = in_degree.get(child, 0) + 1

    heap = [tid for tid, degree in in_degree.items() if degree == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        tid = heapq.heappop(heap)
        order.append(tid)
        for child in sorted(children.get(tid, [])):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                heapq.heappush(heap, child)

    if len(order) != len(in_degree):
        cycle = _find_cycle(list(in_degree), list(tree.edges))
        path = " -> ".join(cycle) if cycle else "(unknown)"
        raise CycleError(f"cycle in task DAG: {path}")
    return order


def leaves(tree: TaskTree) -> list[TaskNode]:
    """Terminal nodes: no outgoing ``(parent, child)`` edge (no children)."""
    parents = {edge[0] for edge in tree.edges}
    result = [node for node in tree.nodes if node.task_id not in parents]
    result.sort(key=lambda node: (node.depth, node.width_idx, node.task_id))
    return result


def ready_tasks(tree: TaskTree, finished: set[str]) -> list[TaskNode]:
    """Tasks whose dependencies are met and which are not themselves finished.

    The supervisor's spawn-scheduler input: a node is ready when its parent
    (its only dependency) is in ``finished``, or it is the root. Deterministic
    order: depth first, then sibling index, then id.
    """
    done = set(finished)
    result = [
        node
        for node in tree.nodes
        if node.task_id not in done
        and (node.parent_task_id is None or node.parent_task_id in done)
    ]
    result.sort(key=lambda node: (node.depth, node.width_idx, node.task_id))
    return result


def subtree_of(tree: TaskTree, task_id: str) -> TaskTree:
    """The subtree rooted at ``task_id`` — the node's own context (I2.4).

    The returned tree is re-based: the subtree root's ``parent_task_id`` is
    ``None``, depths restart at 0, and sibling ``width_idx`` values are
    recomputed. A node's subtree never contains a sibling or its descendants.
    """
    by_id = {node.task_id: node for node in tree.nodes}
    if task_id not in by_id:
        raise TaskTreeError(f"task {task_id!r} is not in the tree")

    children: dict[str, list[str]] = {tid: [] for tid in by_id}
    for parent, child in tree.edges:
        children.setdefault(parent, []).append(child)

    subtree_ids: list[str] = []
    queue = [task_id]
    head = 0
    while head < len(queue):
        tid = queue[head]
        head += 1
        subtree_ids.append(tid)
        queue.extend(sorted(children.get(tid, [])))
    subtree_set = set(subtree_ids)

    sub_children: dict[str, list[str]] = {tid: [] for tid in subtree_set}
    for parent, child in tree.edges:
        if parent in subtree_set:
            sub_children[parent].append(child)

    depth, width_idx = _assign_layout(sub_children, task_id)
    nodes = tuple(
        TaskNode(
            task_id=node.task_id,
            kind=node.kind,
            parent_task_id=None if node.task_id == task_id else node.parent_task_id,
            spec=copy.deepcopy(node.spec),
            depth=depth[node.task_id],
            width_idx=width_idx[node.task_id],
            status=node.status,
        )
        for node in (by_id[tid] for tid in subtree_ids)
    )
    edges = tuple(sorted((parent, child) for parent, child in tree.edges if parent in subtree_set))
    return TaskTree(nodes=nodes, edges=edges)


def upward_result(node: TaskNode) -> dict[str, Any]:
    """The upward result envelope for a finished node (arch §3.4, §3.7 I2.7).

    Returns **exactly** the current normative envelope key set:
    ``parent_task_id``, ``unified_diff``, ``diff_truncated``, ``summary``,
    ``metric_score``, ``metric_breakdown``, ``commits``, ``files_changed``,
    ``status``. The child's result fields are read from ``node.spec`` (empty
    containers, ``""``, ``False``, or ``None`` when the node has no data);
    ``status`` comes from the node's :class:`NodeStatus`. There is no
    scratchpad/reasoning/trajectory field to send, so a parent can never
    receive one — structural info hiding, enforced by the key set itself
    (:data:`_ENVELOPE_KEYS` drives the returned dict).
    """
    spec = node.spec
    unified_diff = spec.get("unified_diff", "")
    if not isinstance(unified_diff, str):
        raise TaskTreeError("upward result unified_diff must be a string")
    summary = spec.get("summary", "")
    if not isinstance(summary, str):
        raise TaskTreeError("upward result summary must be a string")
    commits = spec.get("commits", [])
    if not isinstance(commits, list) or not all(
        isinstance(commit, str) for commit in commits
    ):
        raise TaskTreeError("upward result commits must be a list of strings")
    files_changed = spec.get("files_changed", [])
    if not isinstance(files_changed, list) or not all(
        isinstance(path, str) for path in files_changed
    ):
        raise TaskTreeError("upward result files_changed must be a list of strings")
    values = {
        "parent_task_id": node.parent_task_id,
        "unified_diff": unified_diff,
        "diff_truncated": spec.get("diff_truncated", False),
        "summary": summary,
        "metric_score": spec.get("metric_score", None),
        "metric_breakdown": spec.get("metric_breakdown", {}),
        "commits": commits,
        "files_changed": files_changed,
        "status": node.status,
    }
    return {key: copy.deepcopy(values[key]) for key in _ENVELOPE_KEYS}


def _build_cli_parser() -> argparse.ArgumentParser:
    """Build the standalone tasktree argument parser."""
    parser = _SafeArgumentParser(
        prog="python -m cambium.tasktree",
        description=(
            "Read a task plan JSON object from PLAN or stdin and print its "
            "topological order."
        ),
    )
    parser.add_argument(
        "plan",
        nargs="?",
        metavar="PLAN",
        help="path to a plan JSON file; omit or use '-' to read stdin",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the tasktree CLI from a JSON file or stdin.

    With no plan argument and a TTY stdin, print help without reading stdin. A
    non-TTY stdin remains the D8a pipe contract used by existing callers; an
    empty piped stream also prints help. ``-`` always reads stdin explicitly.
    """
    parser = _build_cli_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.plan is None and sys.stdin.isatty():
        parser.print_help()
        return 0

    if args.plan is None or args.plan == "-":
        payload = sys.stdin.buffer.read()
        if args.plan is None and not payload.strip():
            parser.print_help()
            return 0
    else:
        try:
            payload = Path(args.plan).read_bytes()
        except OSError as exc:
            parser.error(f"cannot read plan file {args.plan!r}: {exc}")

    try:
        plan = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        source = "stdin" if args.plan in (None, "-") else f"plan file {args.plan!r}"
        print(f"tasktree: invalid JSON in {source}: {exc}", file=sys.stderr)
        return 1

    try:
        order = topological_order(build_tree(plan))
    except TaskTreeError as exc:
        print(f"tasktree: {exc}", file=sys.stderr)
        return 1
    for task_id in order:
        print(json.dumps(task_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
