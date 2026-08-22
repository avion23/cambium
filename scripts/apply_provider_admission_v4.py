#!/usr/bin/env python3
"""Wire quota pacing, lane mailboxes, quota CLI, and operator visibility."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8")


def replace_once(path: str, old: str, new: str, label: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    write(path, text.replace(old, new, 1))


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
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == name
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


def wrap_method(path: str, class_name: str, name: str, renamed: str, body: str) -> None:
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


def patch_scheduler() -> None:
    path = "src/cambium/provider_scheduler.py"
    text = read(path)
    if "def quota_db_path(" not in text:
        marker = "\n\nclass QuotaLedger:"
        helper = '''

def quota_db_path() -> Path:
    """Configured quota-ledger path without creating it."""

    return _state_path()


def quota_pressure(
    policy: ProviderPolicy,
    snapshots: Sequence[QuotaWindowSnapshot],
    *,
    now: float | None = None,
) -> float:
    """Largest pace deficit across a subscription lane's independent windows.

    A value of zero means remaining quota is at least proportional to remaining
    time. Positive values mean the lane is burning faster than its reset clock,
    so equal-priority free/metered alternatives should absorb suitable work.
    """

    if not policy.quota_windows:
        return 0.0
    timestamp = time.time() if now is None else float(now)
    indexed = {item.name: item for item in snapshots if item.provider == policy.name}
    pressure = 0.0
    for spec in policy.quota_windows:
        item = indexed.get(spec.name)
        if item is None:
            continue
        remaining_time = min(1.0, max(0.0, (item.reset_at - timestamp) / spec.duration_s))
        fractions: list[float] = []
        if item.allowance_tokens > 0:
            fractions.append(max(0.0, item.remaining_tokens or 0) / item.allowance_tokens)
        if item.allowance_requests > 0:
            fractions.append(max(0.0, item.remaining_requests or 0) / item.allowance_requests)
        if not fractions:
            continue
        remaining_quota = min(fractions)
        pressure = max(pressure, remaining_time - remaining_quota)
    return pressure


def quota_status_line(snapshots: Sequence[QuotaWindowSnapshot], *, now: float | None = None) -> str:
    """Compact, content-free quota summary for one dashboard corner."""

    if not snapshots:
        return ""
    timestamp = time.time() if now is None else float(now)
    constrained = []
    for item in snapshots:
        fractions = []
        if item.allowance_tokens > 0:
            fractions.append(max(0.0, item.remaining_tokens or 0) / item.allowance_tokens)
        if item.allowance_requests > 0:
            fractions.append(max(0.0, item.remaining_requests or 0) / item.allowance_requests)
        if fractions:
            constrained.append((min(fractions), item))
    if not constrained:
        return ""
    fraction, item = min(constrained, key=lambda pair: pair[0])
    reset_s = max(0, int(item.reset_at - timestamp))
    return f"quota={item.provider}/{item.name} {fraction:.0%} reset={reset_s}s"

'''
        if marker not in text:
            raise RuntimeError("QuotaLedger class marker not found")
        text = text.replace(marker, helper + marker, 1)
    signature_old = '''def rank_policies(
    policies: Iterable[ProviderPolicy],
    request: RoutingRequest,
    *,
    in_flight: Mapping[str, int] | None = None,
    evidence: Mapping[str, ProviderEvidence] | None = None,
) -> list[ProviderPolicy]:
'''
    signature_new = '''def rank_policies(
    policies: Iterable[ProviderPolicy],
    request: RoutingRequest,
    *,
    in_flight: Mapping[str, int] | None = None,
    evidence: Mapping[str, ProviderEvidence] | None = None,
    quota_pressure_by_provider: Mapping[str, float] | None = None,
) -> list[ProviderPolicy]:
'''
    if "quota_pressure_by_provider" not in text:
        if signature_old not in text:
            raise RuntimeError("rank_policies signature mismatch")
        text = text.replace(signature_old, signature_new, 1)
        text = text.replace(
            "    observations = {} if evidence is None else evidence\n",
            "    observations = {} if evidence is None else evidence\n"
            "    quota_pressures = (\n"
            "        {} if quota_pressure_by_provider is None else quota_pressure_by_provider\n"
            "    )\n",
            1,
        )
        text = text.replace(
            "            switch,\n            1.0 - success,\n",
            "            switch,\n"
            "            max(0.0, quota_pressures.get(policy.name, 0.0)),\n"
            "            1.0 - success,\n",
            1,
        )
    acquire_old = '''        ranked = rank_policies(
            self._policies,
            request,
            in_flight=self._in_flight,
            evidence=self._evidence,
        )
'''
    if "quota_pressure_by_provider=pressures" not in text:
        acquire_new = '''        snapshots = (
            ()
            if self._ledger is None
            else await asyncio.to_thread(self._ledger.snapshots)
        )
        pressures = {
            policy.name: quota_pressure(policy, snapshots)
            for policy in self._policies
        }
        ranked = rank_policies(
            self._policies,
            request,
            in_flight=self._in_flight,
            evidence=self._evidence,
            quota_pressure_by_provider=pressures,
        )
'''
        if acquire_old not in text:
            raise RuntimeError("scheduler acquire ranking block mismatch")
        text = text.replace(acquire_old, acquire_new, 1)
    if '    "quota_db_path",\n' not in text:
        text = text.replace(
            '    "quota_snapshot_json",\n',
            '    "quota_db_path",\n'
            '    "quota_pressure",\n'
            '    "quota_snapshot_json",\n'
            '    "quota_status_line",\n',
            1,
        )
    write(path, text)


def _find_runtime_class(tree: ast.Module) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "_Runtime":
            return node
    raise RuntimeError("supervisor _Runtime class not found")


def patch_lane_mailbox() -> None:
    path = "src/cambium/supervisor.py"
    text = read(path)
    tree = ast.parse(text)
    runtime = _find_runtime_class(tree)
    replacements: list[tuple[int, int, str]] = []
    transformed = 0
    for method in runtime.body:
        if not isinstance(method, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(method):
            delta = None
            target = None
            if (
                isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Attribute)
                and node.target.attr == "in_flight"
            ):
                target = node.target.value
                if isinstance(node.op, ast.Add):
                    delta = "increment"
                elif isinstance(node.op, ast.Sub):
                    delta = "decrement"
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Attribute)
                and node.targets[0].attr == "in_flight"
            ):
                source = ast.get_source_segment(text, node.value) or ""
                if "in_flight" in source and "- 1" in source:
                    target = node.targets[0].value
                    delta = "decrement"
            if delta is None or target is None or node.end_lineno is None:
                continue
            lane_expr = ast.unparse(target)
            indent = " " * node.col_offset
            call = (
                f"{indent}await self._lane_mailbox.{delta}({lane_expr})\n"
            )
            replacements.append((node.lineno - 1, node.end_lineno, call))
            transformed += 1
    lines = text.splitlines(keepends=True)
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    text = "".join(lines)
    if transformed == 0 and "self._lane_mailbox.increment" not in text:
        raise RuntimeError("no concurrent lane mutation found to mailbox-serialize")
    if "class _LaneAdmissionMailbox:" not in text:
        marker = "\nclass _Runtime"
        mailbox = '''
@dataclass(slots=True)
class _LaneMutation:
    lane: Any
    delta: int
    future: asyncio.Future[None]


@dataclass(slots=True)
class _LaneMailboxClose:
    future: asyncio.Future[None]


class _LaneAdmissionMailbox:
    """Single writer for mutable provider-lane admission counters."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_LaneMutation | _LaneMailboxClose] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def _ensure_started(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._serve(), name="provider-lane-mailbox")

    async def increment(self, lane: Any) -> None:
        await self._change(lane, 1)

    async def decrement(self, lane: Any) -> None:
        await self._change(lane, -1)

    async def _change(self, lane: Any, delta: int) -> None:
        await self._ensure_started()
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(_LaneMutation(lane, delta, future))
        await future

    async def close(self) -> None:
        if self._task is None:
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._queue.put(_LaneMailboxClose(future))
        await future
        await self._task
        self._task = None

    async def _serve(self) -> None:
        while True:
            message = await self._queue.get()
            if isinstance(message, _LaneMailboxClose):
                message.future.set_result(None)
                return
            message.lane.in_flight = max(0, int(message.lane.in_flight) + message.delta)
            message.future.set_result(None)

'''
        if marker not in text:
            raise RuntimeError("supervisor _Runtime insertion marker missing")
        text = text.replace(marker, "\n" + mailbox + marker.lstrip("\n"), 1)
    write(path, text)
    tree = ast.parse(read(path))
    runtime = _find_runtime_class(tree)
    init = next(
        node for node in runtime.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    if "self._lane_mailbox = _LaneAdmissionMailbox()" not in read(path):
        assignment = None
        for node in ast.walk(init):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr == "_lanes"
                    for target in targets
                ):
                    assignment = node
                    break
        if assignment is None or assignment.end_lineno is None:
            raise RuntimeError("runtime _lanes assignment not found")
        lines = read(path).splitlines(keepends=True)
        lines.insert(
            assignment.end_lineno,
            "        self._lane_mailbox = _LaneAdmissionMailbox()\n",
        )
        write(path, "".join(lines))
    text = read(path)
    if "await self._lane_mailbox.close()" not in text:
        tree = ast.parse(text)
        runtime = _find_runtime_class(tree)
        shutdown = next(
            node
            for node in runtime.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "shutdown"
        )
        insert_line = shutdown.end_lineno or shutdown.lineno
        if shutdown.body and isinstance(shutdown.body[-1], ast.Return):
            insert_line = shutdown.body[-1].lineno - 1
        lines = text.splitlines(keepends=True)
        lines.insert(insert_line, "        await self._lane_mailbox.close()\n")
        write(path, "".join(lines))


def patch_cli() -> None:
    pyproject = read("pyproject.toml")
    if 'cambium-quota = "cambium.quota_cli:main"' not in pyproject:
        marker = 'cambium-monitor = "cambium.monitor:main"\n'
        if marker not in pyproject:
            marker = 'cambium = "cambium.cli:main"\n'
        if marker not in pyproject:
            raise RuntimeError("pyproject script marker not found")
        pyproject = pyproject.replace(
            marker, marker + 'cambium-quota = "cambium.quota_cli:main"\n', 1
        )
        write("pyproject.toml", pyproject)
    path = "src/cambium/cli.py"
    text = read(path)
    text = text.replace(
        "tui,monitor,optimize,session",
        "tui,monitor,optimize,quota,session",
    )
    if 'commands.add_parser(\n        "quota"' not in text:
        marker = "    session = commands.add_parser(\n"
        block = '''    quota = commands.add_parser(
        "quota",
        help="inspect or update provider quota windows",
        description="Inspect or update content-free provider quota observations.",
    )
    quota.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    quota_commands = quota.add_subparsers(dest="quota_command", required=True)
    quota_status = quota_commands.add_parser("status")
    quota_status.add_argument("--provider")
    quota_status.add_argument("--json", action="store_true")
    quota_observe = quota_commands.add_parser("observe")
    quota_observe.add_argument("provider")
    quota_observe.add_argument("window")
    quota_observe.add_argument("--reset-in-s", type=float, required=True)
    quota_observe.add_argument("--allowance-tokens", type=int, default=0)
    quota_observe.add_argument("--remaining-tokens", type=int)
    quota_observe.add_argument("--allowance-requests", type=int, default=0)
    quota_observe.add_argument("--remaining-requests", type=int)
    quota_observe.add_argument("--reserve-fraction", type=float, default=0.0)

'''
        if marker not in text:
            raise RuntimeError("CLI session parser marker not found")
        text = text.replace(marker, block + marker, 1)
    if "quota_cli.run_namespace(args)" not in text:
        marker = '    if args.command == "session":\n'
        dispatch = '''    if args.command == "quota":
        from . import quota_cli

        try:
            return quota_cli.run_namespace(args)
        except (OSError, ValueError) as exc:
            print(f"cambium quota: {exc}", file=sys.stderr)
            return ExitCode.USAGE

'''
        if marker not in text:
            raise RuntimeError("CLI session dispatch marker not found")
        text = text.replace(marker, dispatch + marker, 1)
    write(path, text)


def patch_render() -> None:
    path = "src/cambium/render.py"
    text = read(path)
    if "def render_quota_status(" not in text:
        marker = "\ndef render_status_bar("
        helper = '''
def render_quota_status(events: Any) -> str:
    """Latest compact quota-window projection from durable usage events."""

    for event in reversed(list(events or ())):
        if not isinstance(event, Mapping) or event.get("kind") != "usage_event":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        windows = payload.get("quota_windows")
        if not isinstance(windows, list):
            continue
        constrained: list[tuple[float, Mapping[str, Any]]] = []
        for item in windows:
            if not isinstance(item, Mapping):
                continue
            fractions = []
            for remaining_key, allowance_key in (
                ("remaining_tokens", "allowance_tokens"),
                ("remaining_requests", "allowance_requests"),
            ):
                remaining = _finite_number(item.get(remaining_key))
                allowance = _finite_number(item.get(allowance_key))
                if remaining is not None and allowance is not None and allowance > 0:
                    fractions.append(max(0.0, remaining) / allowance)
            if fractions:
                constrained.append((min(fractions), item))
        if not constrained:
            return ""
        fraction, item = min(constrained, key=lambda pair: pair[0])
        provider = _sanitize_field(str(item.get("provider", "?")))
        name = _sanitize_field(str(item.get("name", "?")))
        reset = _finite_number(item.get("reset_at"))
        reset_text = ""
        if reset is not None:
            import time

            reset_text = f" reset={max(0, int(reset - time.time()))}s"
        return f"quota={provider}/{name} {fraction:.0%}{reset_text}"
    return ""

'''
        if marker not in text:
            raise RuntimeError("render status bar marker not found")
        text = text.replace(marker, "\n" + helper + marker.lstrip("\n"), 1)
    if "quota = render_quota_status(records)" not in text:
        marker = "    right_parts: list[str] = []\n"
        insertion = (
            marker
            + "    quota = render_quota_status(records)\n"
            + "    if quota:\n"
            + "        right_parts.append(quota)\n"
        )
        if marker not in text:
            raise RuntimeError("render right-parts marker not found")
        text = text.replace(marker, insertion, 1)
    if '    "render_quota_status",\n' not in text:
        text = text.replace(
            '    "render_live_status_line",\n',
            '    "render_live_status_line",\n    "render_quota_status",\n',
            1,
        )
    write(path, text)


def patch_docs() -> None:
    path = "docs/architecture/provider-routing.md"
    text = read(path)
    if "### Example resource policies" not in text:
        text += '''

### Example resource policies

A subscription provider with independent five-hour, weekly, and monthly
allowances should declare all three windows and a real concurrency limit. RPM is
not concurrency:

```json
{
  "name": "subscription-strong",
  "tier": "strong",
  "model": "provider-model",
  "rpm": 60,
  "max_concurrency": 4,
  "billing_mode": "subscription",
  "pricing_known": true,
  "price_per_1m_in": 0,
  "price_per_1m_cached_in": 0,
  "price_per_1m_out": 0,
  "throughput_hint_tps": 35,
  "quality_weight": 1.0,
  "quota_windows": [
    {"name": "five-hour", "duration_s": 18000, "token_allowance": 1000000},
    {"name": "weekly", "duration_s": 604800, "token_allowance": 5000000},
    {"name": "monthly", "duration_s": 2592000, "token_allowance": 15000000}
  ]
}
```

Known-free OpenRouter or Zen lanes use `billing_mode: "free"`,
`pricing_known: true`, zero prices, a lower `quality_weight`, and the measured
throughput hint. Put them in the same priority class only for task classes where
substitution is semantically legal; otherwise use a lower tier/priority. The
scheduler then absorbs bounded review, search, classification, and redundant
verification work on free capacity without moving the leased root trunk.

Provider dashboard/header observations can be recorded without secrets:

```sh
cambium quota observe zai five-hour --reset-in-s 7200 \
  --allowance-tokens 1000000 --remaining-tokens 420000
cambium quota status
```
'''
        write(path, text)


def write_tests() -> None:
    write(
        "tests/scenarios/test_provider_admission_v4.py",
        '''from __future__ import annotations

import json
import time
from pathlib import Path

from cambium import cli, render
from cambium.provider_scheduler import (
    BillingMode,
    ProviderPolicy,
    QuotaLedger,
    QuotaWindowSpec,
    RoutingRequest,
    quota_pressure,
    quota_status_line,
    rank_policies,
)


def test_quota_pressure_paces_subscription_against_reset_clock(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    now = 1000.0
    ledger.observe(
        "zai",
        "five-hour",
        reset_at=now + 9000,
        allowance_tokens=1000,
        remaining_tokens=100,
        now=now,
    )
    policy = ProviderPolicy(
        "zai",
        "glm",
        billing_mode=BillingMode.SUBSCRIPTION,
        quota_windows=(QuotaWindowSpec("five-hour", 18000, token_allowance=1000),),
    )
    assert quota_pressure(policy, ledger.snapshots("zai"), now=now) == 0.4


def test_quota_pressure_can_move_free_lane_ahead_within_same_priority(tmp_path: Path) -> None:
    paid = ProviderPolicy(
        "subscription",
        "m",
        priority=0,
        billing_mode=BillingMode.SUBSCRIPTION,
        quota_windows=(QuotaWindowSpec("five-hour", 18000, token_allowance=1000),),
        throughput_hint_tps=100,
    )
    free = ProviderPolicy(
        "free",
        "m",
        priority=0,
        billing_mode=BillingMode.FREE,
        pricing_known=True,
        throughput_hint_tps=20,
    )
    ranked = rank_policies(
        [paid, free],
        RoutingRequest("review", "m", expected_output_tokens=100),
        quota_pressure_by_provider={"subscription": 0.5, "free": 0.0},
    )
    assert [item.name for item in ranked] == ["free", "subscription"]


def test_quota_cli_observe_and_status_json(tmp_path: Path, capsys) -> None:
    db = tmp_path / "quota.db"
    assert cli.main([
        "quota", "--db", str(db), "observe", "codex", "weekly",
        "--reset-in-s", "3600", "--allowance-requests", "100",
        "--remaining-requests", "70",
    ]) == 0
    assert cli.main(["quota", "--db", str(db), "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload[0]["provider"] == "codex"
    assert payload[0]["remaining_requests"] == 70


def test_quota_status_is_visible_in_status_bar(monkeypatch) -> None:
    now = time.time()
    events = [{
        "kind": "usage_event",
        "payload": {
            "quota_windows": [{
                "provider": "zai", "name": "five-hour", "reset_at": now + 100,
                "allowance_tokens": 1000, "remaining_tokens": 250,
                "allowance_requests": 0, "remaining_requests": None,
            }]
        },
    }]
    line = render.render_quota_status(events)
    assert "quota=zai/five-hour 25%" in line
    assert "reset=" in line


def test_compact_quota_status_line_chooses_tightest_window(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    now = 100.0
    ledger.observe(
        "p", "five-hour", reset_at=200, allowance_tokens=100, remaining_tokens=80, now=now
    )
    ledger.observe(
        "p", "weekly", reset_at=300, allowance_tokens=100, remaining_tokens=20, now=now
    )
    assert "p/weekly 20%" in quota_status_line(ledger.snapshots("p"), now=now)
''',
    )
    write(
        "tests/scenarios/test_lane_mailbox_source.py",
        '''from __future__ import annotations

import ast
from pathlib import Path


def test_runtime_lane_mutations_are_owned_by_mailbox() -> None:
    path = Path(__file__).resolve().parents[2] / "src" / "cambium" / "supervisor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    runtime = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_Runtime"
    )
    direct = []
    for method in runtime.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute):
                if node.target.attr == "in_flight":
                    direct.append((method.name, node.lineno))
    assert direct == []
''',
    )


def main() -> None:
    patch_scheduler()
    patch_lane_mailbox()
    patch_cli()
    patch_render()
    patch_docs()
    write_tests()


if __name__ == "__main__":
    main()
