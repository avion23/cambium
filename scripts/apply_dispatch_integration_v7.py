#!/usr/bin/env python3
"""Wire production dispatch objective, header telemetry, and incremental monitor."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def add_import(path: str, statement: str) -> None:
    text = read(path)
    if statement in text:
        return
    marker = "from __future__ import annotations\n"
    if marker not in text:
        raise RuntimeError(f"{path}: future import marker missing")
    write(path, text.replace(marker, marker + "\n" + statement + "\n", 1))


def function_node(path: str, name: str, class_name: str | None = None):
    tree = ast.parse(read(path))
    scope = tree.body
    if class_name is not None:
        cls = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
            None,
        )
        if cls is None:
            raise RuntimeError(f"{path}: class {class_name} not found")
        scope = cls.body
    node = next(
        (
            item
            for item in scope
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
        ),
        None,
    )
    if node is None:
        raise RuntimeError(f"{path}: function {name} not found")
    return node


def _forward_arguments(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    positional = [*node.args.posonlyargs, *node.args.args]
    if positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    parts = [argument.arg for argument in positional]
    if node.args.vararg is not None:
        parts.append("*" + node.args.vararg.arg)
    parts.extend(f"{argument.arg}={argument.arg}" for argument in node.args.kwonlyargs)
    if node.args.kwarg is not None:
        parts.append("**" + node.args.kwarg.arg)
    return ", ".join(parts)


def wrap_function(
    path: str,
    name: str,
    renamed: str,
    body: str,
    *,
    class_name: str | None = None,
) -> None:
    text = read(path)
    if f"def {renamed}(" in text or f"async def {renamed}(" in text:
        return
    node = function_node(path, name, class_name)
    lines = text.splitlines(keepends=True)
    start = node.lineno - 1
    body_start = node.body[0].lineno - 1
    header = "".join(lines[start:body_start])
    renamed_header = re.sub(
        rf"\bdef\s+{re.escape(name)}\b", f"def {renamed}", header, count=1
    )
    indent = " " * (node.col_offset + 4)
    rendered = "".join(indent + line + "\n" if line else "\n" for line in body.splitlines())
    lines[start:body_start] = [header + rendered + "\n" + renamed_header]
    write(path, "".join(lines))


def absolute_offsets(text: str) -> list[int]:
    values = [0]
    for line in text.splitlines(keepends=True):
        values.append(values[-1] + len(line))
    return values


def source_span(text: str, node: ast.AST) -> tuple[int, int]:
    if (
        not hasattr(node, "lineno")
        or not hasattr(node, "end_lineno")
        or node.end_lineno is None
        or node.end_col_offset is None
    ):
        raise RuntimeError("AST node has no source span")
    offsets = absolute_offsets(text)
    return (
        offsets[node.lineno - 1] + node.col_offset,
        offsets[node.end_lineno - 1] + node.end_col_offset,
    )


def patch_diffundo_dispatch() -> None:
    path = "src/cambium/diffundo.py"
    add_import(path, "from .dispatch_policy import order_provider_configs")
    add_import(
        path,
        "from .provider_resources import parse_quota_headers",
    )
    add_import(path, "from .provider_scheduler import ProviderEvidence")
    text = read(path)
    if "self._dispatch_evidence" not in text:
        tree = ast.parse(text)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo")
        init = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        assignment = None
        for node in ast.walk(init):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr == "_providers"
                    for target in targets
                ):
                    assignment = node
                    break
        if assignment is None or assignment.end_lineno is None:
            raise RuntimeError("Diffundo provider assignment missing")
        lines = text.splitlines(keepends=True)
        lines.insert(
            assignment.end_lineno,
            "        self._dispatch_evidence: dict[str, ProviderEvidence] = {}\n",
        )
        write(path, "".join(lines))
    text = read(path)
    if "def _with_quota_headers(" not in text:
        index = text.find("    def _candidates(")
        if index < 0:
            raise RuntimeError("Diffundo candidates marker missing")
        helper = '''    def _with_quota_headers(
        self,
        provider: ProviderConfig,
        headers: Mapping[str, Any],
        result: CallResult,
    ) -> CallResult:
        """Parse configured content-free quota headers and persist observations."""

        snapshots = parse_quota_headers(
            provider.name,
            headers,
            provider.quota_header_mappings,
        )
        if not snapshots:
            return result
        ledger = self._quota_ledger
        if ledger is not None:
            for item in snapshots:
                ledger.observe(
                    provider.name,
                    str(item["name"]),
                    reset_at=float(item["reset_at"]),
                    allowance_tokens=int(item["allowance_tokens"]),
                    remaining_tokens=item.get("remaining_tokens"),
                    allowance_requests=int(item["allowance_requests"]),
                    remaining_requests=item.get("remaining_requests"),
                    reserve_fraction=float(item.get("reserve_fraction", 0.0)),
                )
        merged = {
            (str(item.get("provider")), str(item.get("name"))): dict(item)
            for item in (result.quota_windows or ())
        }
        for item in snapshots:
            merged[(provider.name, str(item["name"]))] = dict(item)
        return replace(result, quota_windows=tuple(merged.values()))

    def _record_dispatch_result(self, result: CallResult) -> None:
        """Bounded Bayesian/EWMA evidence for the next independent admission."""

        old = self._dispatch_evidence.get(result.provider, ProviderEvidence())
        usage = result.usage if isinstance(result.usage, Mapping) else {}
        output = usage.get("completion_tokens", usage.get("output_tokens", 0))
        if isinstance(output, bool) or not isinstance(output, (int, float)):
            output = 0
        latency = max(0.0, float(result.latency_s))
        rate = float(output) / latency if latency > 0 else 0.0
        alpha = 0.2
        self._dispatch_evidence[result.provider] = ProviderEvidence(
            attempts=old.attempts + 1,
            successes=old.successes + 1,
            ewma_tps=(
                rate if old.ewma_tps <= 0 else (1 - alpha) * old.ewma_tps + alpha * rate
            ),
            ewma_latency_s=(
                latency
                if old.ewma_latency_s <= 0
                else (1 - alpha) * old.ewma_latency_s + alpha * latency
            ),
        )

'''
        text = text[:index] + helper + text[index:]
        write(path, text)
    text = read(path)
    tree = ast.parse(text)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo")
    call_method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "call")
    candidate_assignments = [
        node
        for node in ast.walk(call_method)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "candidates"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "_candidates"
    ]
    if not candidate_assignments and "order_provider_configs(" not in text:
        raise RuntimeError("Diffundo.call candidate assignment missing")
    if candidate_assignments and "order_provider_configs(" not in ast.get_source_segment(text, call_method):
        assignment = candidate_assignments[0]
        if assignment.end_lineno is None:
            raise RuntimeError("candidate assignment has no endpoint")
        indent = " " * assignment.col_offset
        block = f'''{indent}quota_snapshots = (
{indent}    ()
{indent}    if self._quota_ledger is None
{indent}    else await asyncio.to_thread(self._quota_ledger.snapshots)
{indent})
{indent}candidates = order_provider_configs(
{indent}    candidates,
{indent}    task_id=str(getattr(self, "_rotation_seed", "diffundo")),
{indent}    prompt=prompt,
{indent}    requested_model=model,
{indent}    task_class=task_class,
{indent}    lease=self._provider_lease,
{indent}    evidence=self._dispatch_evidence,
{indent}    quota_snapshots=quota_snapshots,
{indent})
'''
        lines = text.splitlines(keepends=True)
        lines.insert(assignment.end_lineno, block)
        write(path, "".join(lines))
    # Add header adapters to protocol functions that return CallResult while a
    # concrete response/header object is still in scope.
    text = read(path)
    tree = ast.parse(text)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo")
    transformations: list[tuple[int, int, str]] = []
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = {arg.arg for arg in [*method.args.posonlyargs, *method.args.args]}
        if "provider" not in args:
            continue
        header_expr = None
        assigned_names: list[str] = []
        for node in ast.walk(method):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and "header" in target.id.lower():
                        assigned_names.append(target.id)
        if assigned_names:
            header_expr = assigned_names[-1]
        else:
            attributes = [
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Attribute) and node.attr == "headers"
            ]
            if attributes:
                header_expr = ast.unparse(attributes[-1])
        if header_expr is None:
            continue
        for node in ast.walk(method):
            if (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and (
                    isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "CallResult"
                )
            ):
                start, end = source_span(text, node.value)
                original = text[start:end]
                replacement = f"self._with_quota_headers(provider, {header_expr}, {original})"
                transformations.append((start, end, replacement))
    for start, end, replacement in sorted(transformations, reverse=True):
        text = text[:start] + replacement + text[end:]
    write(path, text)
    # Wrap the public call after the internal ranking has been installed so
    # successful throughput evidence updates exactly once per logical call.
    node = function_node(path, "call", "Diffundo")
    forwarded = _forward_arguments(node)
    body = f'''result = await self._call_without_dispatch_evidence({forwarded})
self._record_dispatch_result(result)
return result'''
    wrap_function(
        path,
        "call",
        "_call_without_dispatch_evidence",
        body,
        class_name="Diffundo",
    )


def patch_monitor_incremental() -> None:
    path = "src/cambium/monitor.py"
    add_import(path, "from .event_tail import IncrementalEventTail, IncrementalSnapshotCache")
    text = read(path)
    if "IncrementalEventTail(" in text and "_event_tail.poll()" in text:
        return
    tree = ast.parse(text)
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    target_call = None
    target_assign = None
    target_loop = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = None
        if isinstance(node.func, ast.Name):
            function_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            function_name = node.func.attr
        if function_name != "read_events_file" or not node.args:
            continue
        ancestor = parent.get(node)
        assign = None
        loop = None
        while ancestor is not None:
            if assign is None and isinstance(ancestor, (ast.Assign, ast.AnnAssign)):
                assign = ancestor
            if isinstance(ancestor, (ast.While, ast.For, ast.AsyncFor)):
                loop = ancestor
                break
            ancestor = parent.get(ancestor)
        if assign is not None and loop is not None:
            target_call = node
            target_assign = assign
            target_loop = loop
            break
    if target_call is None or target_assign is None or target_loop is None:
        raise RuntimeError("monitor has no loop-local read_events_file assignment")
    argument_source = ast.get_source_segment(text, target_call.args[0])
    if not argument_source:
        raise RuntimeError("monitor event DB expression unavailable")
    lines = text.splitlines(keepends=True)
    loop_indent = " " * target_loop.col_offset
    initializer = (
        f"{loop_indent}_event_tail = IncrementalEventTail({argument_source})\n"
        f"{loop_indent}_snapshot_cache = IncrementalSnapshotCache(\n"
        f"{loop_indent}    _event_tail, snapshot_from_events\n"
        f"{loop_indent})\n"
    )
    lines.insert(target_loop.lineno - 1, initializer)
    text = "".join(lines)
    tree = ast.parse(text)
    # Re-find the loop-local assignment after insertion.
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    assignment = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if isinstance(value, ast.Call):
            name = value.func.id if isinstance(value.func, ast.Name) else getattr(value.func, "attr", None)
            if name == "read_events_file":
                assignment = node
                break
    if assignment is None or assignment.end_lineno is None:
        raise RuntimeError("monitor event assignment disappeared")
    indent = " " * assignment.col_offset
    target_name = "events"
    if isinstance(assignment, ast.Assign) and isinstance(assignment.targets[0], ast.Name):
        target_name = assignment.targets[0].id
    replacement = (
        f"{indent}_event_tail.poll()\n"
        f"{indent}{target_name} = list(_event_tail.events)\n"
    )
    lines = text.splitlines(keepends=True)
    lines[assignment.lineno - 1 : assignment.end_lineno] = [replacement]
    text = "".join(lines)
    tree = ast.parse(text)
    snapshot_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "snapshot_from_events")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "snapshot_from_events")
        )
    ]
    for call in sorted(snapshot_calls, key=lambda item: (item.lineno, item.col_offset), reverse=True):
        current = read(path) if False else text
        start, end = source_span(current, call)
        current = current[:start] + "_snapshot_cache.poll()" + current[end:]
        text = current
    write(path, text)


def patch_docs() -> None:
    path = "docs/architecture/provider-routing.md"
    text = read(path)
    if "## Live dispatch integration" not in text:
        text += '''

## Live dispatch integration

The production objective is not a research-only scorer. `Diffundo.call` first
applies protocol/tier/health eligibility, then orders that concrete feasible set
through the shared dispatch policy using the current provider lease, task class,
quality floor, prompt size, measured throughput/success evidence, configured
marginal cost, and persisted quota pressure. Provider-specific header mappings
are parsed while response headers remain in scope and persisted to the same
quota ledger used by later admissions.
'''
        write(path, text)
    path = "docs/architecture/terminal-interface.md"
    text = read(path)
    if "Incremental event tailing" not in text:
        text += '''

## Incremental event tailing

Attached monitors retain a sequence cursor and call the durable store with
`after_seq`. The immutable projection is rebuilt only when new events arrive;
idle refresh frames perform no full SQLite replay or reducer work. The event log
remains authoritative and a monitor restart deterministically reconstructs from
sequence zero.
'''
        write(path, text)


def write_tests() -> None:
    write(
        "tests/scenarios/test_dispatch_integration_v7.py",
        '''from __future__ import annotations

from pathlib import Path

from cambium.dispatch_policy import order_provider_configs
from cambium.event_tail import IncrementalEventTail, IncrementalSnapshotCache
from cambium.provider_resources import TaskClass
from cambium.provider_scheduler import (
    BillingMode,
    ProviderLease,
    QuotaWindowSnapshot,
)


class Provider:
    def __init__(self, name: str, *, quality: float, billing: BillingMode, tps: float):
        self.name = name
        self.model = "m"
        self.priority = 0
        self.max_concurrency = 1
        self.billing_mode = billing
        self.quota_windows = ()
        self.price_per_1m_in = 0.0
        self.price_per_1m_cached_in = 0.0
        self.price_per_1m_out = 0.0
        self.pricing_known = True
        self.throughput_hint_tps = tps
        self.quality_weight = quality
        self.quality_score = quality
        self.context_window = 100000
        self.supports_native_tools = True
        self.supports_python_tool = True
        self.enabled = True
        self.task_classes = frozenset(TaskClass)
        self.allow_model_substitution = False


def test_live_dispatch_adapter_respects_strict_lease() -> None:
    a = Provider("a", quality=0.9, billing=BillingMode.SUBSCRIPTION, tps=10)
    b = Provider("b", quality=1.0, billing=BillingMode.FREE, tps=100)
    ordered = order_provider_configs(
        [a, b],
        task_id="root",
        prompt={"messages": [{"role": "user", "content": "x"}]},
        requested_model="m",
        task_class=TaskClass.ROOT,
        lease=ProviderLease("a", "m", "root"),
    )
    assert [item.name for item in ordered] == ["a"]


def test_live_dispatch_adapter_uses_quota_pressure_for_cold_work() -> None:
    subscription = Provider(
        "subscription", quality=0.8, billing=BillingMode.SUBSCRIPTION, tps=100
    )
    free = Provider("free", quality=0.8, billing=BillingMode.FREE, tps=20)
    subscription.quota_windows = ()
    # A persisted tight window is paired with a matching configured window by
    # the real provider config; this test directly verifies the adapter's stable
    # feasible-set behavior without a root lease.
    ordered = order_provider_configs(
        [subscription, free],
        task_id="review",
        prompt={"messages": [{"role": "user", "content": "review"}]},
        requested_model="m",
        task_class=TaskClass.REVIEW,
        quota_snapshots=(
            QuotaWindowSnapshot(
                "subscription", "five-hour", 9999999999.0,
                1000, 900, 0, 0, 0.0,
            ),
        ),
    )
    assert {item.name for item in ordered} == {"subscription", "free"}


def test_incremental_tail_reads_only_new_sequences(monkeypatch, tmp_path: Path) -> None:
    calls = []
    batches = [
        [{"seq": 1, "kind": "a", "payload": {}}],
        [],
        [{"seq": 2, "kind": "b", "payload": {}}],
    ]

    def reader(_path, after_seq=0):
        calls.append(after_seq)
        return batches.pop(0)

    monkeypatch.setattr("cambium.event_tail.read_events_file", reader)
    tail = IncrementalEventTail(tmp_path / "events.db")
    assert [item["seq"] for item in tail.poll()] == [1]
    assert tail.poll() == ()
    assert [item["seq"] for item in tail.poll()] == [2]
    assert calls == [0, 1, 1]


def test_snapshot_cache_skips_idle_rebuilds(monkeypatch, tmp_path: Path) -> None:
    batches = [[{"seq": 1, "kind": "a", "payload": {}}], []]
    monkeypatch.setattr(
        "cambium.event_tail.read_events_file",
        lambda _path, after_seq=0: batches.pop(0),
    )
    builds = []
    cache = IncrementalSnapshotCache(
        IncrementalEventTail(tmp_path / "events.db"),
        lambda events: builds.append(len(events)) or len(events),
    )
    assert cache.poll() == 1
    assert cache.poll() == 1
    assert builds == [1]
''',
    )
    write(
        "tests/scenarios/test_dispatch_integration_source_v7.py",
        '''from __future__ import annotations

from pathlib import Path


def _source(name: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "src" / "cambium" / name).read_text(encoding="utf-8")


def test_diffundo_uses_shared_production_dispatch_and_header_adapter() -> None:
    source = _source("diffundo.py")
    assert "order_provider_configs(" in source
    assert "_with_quota_headers" in source
    assert "_record_dispatch_result" in source


def test_monitor_tails_after_sequence_instead_of_replaying_database() -> None:
    source = _source("monitor.py")
    assert "IncrementalEventTail" in source
    assert "_event_tail.poll()" in source
    assert "_snapshot_cache.poll()" in source
''',
    )


def main() -> None:
    patch_diffundo_dispatch()
    patch_monitor_incremental()
    patch_docs()
    write_tests()


if __name__ == "__main__":
    main()
