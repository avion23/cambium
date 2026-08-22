#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.rstrip() + "\n", encoding="utf-8")


def append_once(path: str, marker: str, text: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    if marker not in source:
        target.write_text(source.rstrip() + "\n\n" + text.rstrip() + "\n", encoding="utf-8")


def rename_top_level(path: str, old: str, new: str) -> tuple[bool, list[str]]:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == old
    ]
    if len(nodes) != 1:
        raise RuntimeError(f"{path}: expected one top-level {old}, found {len(nodes)}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1] = re.sub(rf"\b{re.escape(old)}\b", new, lines[node.lineno - 1], count=1)
    target.write_text("".join(lines), encoding="utf-8")
    return isinstance(node, ast.AsyncFunctionDef), [arg.arg for arg in node.args.args]


write("src/cambium/mailbox.py", r'''"""Bounded single-writer actors for mutable runtime state."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

CommandT = TypeVar("CommandT")
ReplyT = TypeVar("ReplyT")


@dataclass(slots=True)
class _Envelope(Generic[CommandT, ReplyT]):
    command: CommandT
    reply: asyncio.Future[ReplyT]


class MailboxClosed(RuntimeError):
    pass


class MailboxActor(Generic[CommandT, ReplyT]):
    """Serialize commands through one bounded mailbox and one owner task."""

    def __init__(self, handler: Callable[[CommandT], Awaitable[ReplyT]], *, capacity: int = 256, name: str = "mailbox") -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._handler = handler
        self._queue: asyncio.Queue[_Envelope[CommandT, ReplyT] | None] = asyncio.Queue(capacity)
        self._name = name
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def start(self) -> "MailboxActor[CommandT, ReplyT]":
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=self._name)
        return self

    async def ask(self, command: CommandT) -> ReplyT:
        if self._closed:
            raise MailboxClosed(self._name)
        self.start()
        reply: asyncio.Future[ReplyT] = asyncio.get_running_loop().create_future()
        await self._queue.put(_Envelope(command, reply))
        return await reply

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task is not None:
            await self._queue.put(None)
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            envelope = await self._queue.get()
            try:
                if envelope is None:
                    return
                if envelope.reply.cancelled():
                    continue
                try:
                    value = await self._handler(envelope.command)
                except BaseException as exc:
                    if not envelope.reply.done():
                        envelope.reply.set_exception(exc)
                else:
                    if not envelope.reply.done():
                        envelope.reply.set_result(value)
            finally:
                self._queue.task_done()

    async def __aenter__(self) -> "MailboxActor[CommandT, ReplyT]":
        return self.start()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
''')

write("src/cambium/provider_resources.py", r'''"""Quota-aware provider portfolio scheduling and operator configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TypeVar

from .mailbox import MailboxActor

SCHEMA = 1
DEFAULT_PATH = Path.home() / ".config" / "cambium" / "provider-resources.json"
SAFE_WEAK_TASKS = frozenset({"research", "index", "summarize", "test_triage", "speculative"})


class BillingMode(StrEnum):
    SUBSCRIPTION = "subscription"
    PREPAID = "prepaid"
    METERED = "metered"
    FREE = "free"


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    name: str
    remaining_tokens: int | None = None
    limit_tokens: int | None = None
    resets_at: float | None = None
    hard: bool = True

    def exhausted(self, demand: int) -> bool:
        return self.hard and self.remaining_tokens is not None and self.remaining_tokens < demand

    def scarcity(self, demand: int, *, now: float, measured_tps: float) -> float:
        if self.remaining_tokens is None:
            return 0.0
        if self.hard and self.remaining_tokens < demand:
            return math.inf
        after = max(0, self.remaining_tokens - demand)
        fraction_pressure = 0.0
        if self.limit_tokens and self.limit_tokens > 0:
            fraction_after = after / self.limit_tokens
            fraction_pressure = max(0.0, 0.25 - fraction_after) / 0.25
        pacing_pressure = 0.0
        if self.resets_at is not None and measured_tps > 0:
            sustainable = self.remaining_tokens / max(1.0, self.resets_at - now)
            pacing_pressure = math.log1p(max(0.0, measured_tps / max(1e-9, sustainable) - 1.0))
        return fraction_pressure + pacing_pressure


@dataclass(frozen=True, slots=True)
class ProviderResourceProfile:
    provider: str
    billing: BillingMode = BillingMode.METERED
    quality: float = 0.5
    tokens_per_second: float = 0.0
    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    balance_usd: float | None = None
    max_concurrency: int = 1
    active_requests: int = 0
    weak: bool = False
    verification_required: bool = False
    allowed_task_classes: tuple[str, ...] = ()
    windows: tuple[QuotaWindow, ...] = ()
    observed_at: float = 0.0

    def allows(self, task_class: str) -> bool:
        if self.allowed_task_classes and task_class not in self.allowed_task_classes:
            return False
        return not self.weak or task_class in SAFE_WEAK_TASKS

    def expected_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_usd_per_million + output_tokens * self.output_usd_per_million) / 1_000_000.0


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    profiles: tuple[ProviderResourceProfile, ...] = ()
    updated_at: float = 0.0

    def by_provider(self) -> dict[str, ProviderResourceProfile]:
        return {item.provider: item for item in self.profiles}


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    role: str = "subagent"
    task_class: str = "code"
    branch_key: str = ""
    expected_input_tokens: int = 8_000
    expected_output_tokens: int = 2_000
    incumbent: str | None = None


class Candidate(Protocol):
    name: str


T = TypeVar("T", bound=Candidate)


class ProviderPortfolioPolicy:
    """Rank an already-feasible list without crossing priority boundaries."""

    def __init__(self, snapshot: ResourceSnapshot) -> None:
        self._profiles = snapshot.by_provider()

    def order(self, candidates: Sequence[T], intent: DispatchIntent, *, now: float | None = None) -> list[T]:
        ordered = list(candidates)
        if intent.role == "main" and intent.incumbent:
            index = next((i for i, item in enumerate(ordered) if item.name == intent.incumbent), None)
            if index is not None:
                item = ordered.pop(index)
                ordered.insert(0, item)
                return ordered
        current = time.time() if now is None else now
        result: list[T] = []
        start = 0
        while start < len(ordered):
            priority = getattr(ordered[start], "priority", 0)
            end = start + 1
            while end < len(ordered) and getattr(ordered[end], "priority", 0) == priority:
                end += 1
            run = ordered[start:end]
            measured: list[tuple[float, str, T]] = []
            neutral: list[T] = []
            for item in run:
                profile = self._profiles.get(item.name)
                if profile is None:
                    neutral.append(item)
                    continue
                score = score_profile(profile, intent, now=current)
                if math.isinf(score) and score < 0:
                    neutral.append(item)
                    continue
                tie = rendezvous_fraction(intent.branch_key, item.name) * 1e-6
                measured.append((score + tie, item.name, item))
            measured.sort(key=lambda row: (-row[0], row[1]))
            result.extend(row[2] for row in measured)
            result.extend(neutral)
            start = end
        return result


def score_profile(profile: ProviderResourceProfile, intent: DispatchIntent, *, now: float) -> float:
    if not profile.allows(intent.task_class) or profile.active_requests >= profile.max_concurrency:
        return -math.inf
    demand = max(0, intent.expected_input_tokens) + max(0, intent.expected_output_tokens)
    scarcity = 0.0
    for window in profile.windows:
        pressure = window.scarcity(demand, now=now, measured_tps=profile.tokens_per_second)
        if math.isinf(pressure):
            return -math.inf
        scarcity = max(scarcity, pressure)
    cost = profile.expected_cost(intent.expected_input_tokens, intent.expected_output_tokens)
    if profile.balance_usd is not None and cost > profile.balance_usd:
        return -math.inf
    quality_weight = 6.0 if intent.task_class in {"code", "review", "main"} else 3.0
    billing_bonus = {BillingMode.FREE: 1.25, BillingMode.SUBSCRIPTION: 0.75, BillingMode.PREPAID: 0.1, BillingMode.METERED: 0.0}[profile.billing]
    return (
        quality_weight * profile.quality
        + 0.8 * math.log1p(max(0.0, profile.tokens_per_second))
        + billing_bonus
        - 4.0 * scarcity
        - 20.0 * cost
        - (0.5 if profile.verification_required else 0.0)
    )


def rendezvous_fraction(branch_key: str, provider: str) -> float:
    digest = hashlib.blake2b(f"{branch_key}\0{provider}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def resource_path() -> Path:
    value = os.environ.get("CAMBIUM_PROVIDER_RESOURCES")
    return Path(value).expanduser() if value else DEFAULT_PATH


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return float(value)


def _window(value: Mapping[str, Any]) -> QuotaWindow:
    def integer(name: str) -> int | None:
        item = value.get(name)
        return max(0, int(item)) if isinstance(item, (int, float)) and not isinstance(item, bool) else None
    reset = value.get("resets_at")
    return QuotaWindow(
        str(value.get("name") or "window"), integer("remaining_tokens"), integer("limit_tokens"),
        float(reset) if isinstance(reset, (int, float)) and not isinstance(reset, bool) else None,
        bool(value.get("hard", True)),
    )


def load_snapshot(path: Path | None = None) -> ResourceSnapshot:
    target = resource_path() if path is None else Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ResourceSnapshot()
    if not isinstance(raw, Mapping) or raw.get("schema") != SCHEMA or not isinstance(raw.get("providers"), Mapping):
        raise ValueError(f"{target}: invalid provider-resource schema")
    profiles: list[ProviderResourceProfile] = []
    for name, item in raw["providers"].items():
        if not isinstance(name, str) or not isinstance(item, Mapping):
            continue
        windows_raw = item.get("windows", ())
        windows = tuple(_window(value) for value in windows_raw if isinstance(value, Mapping)) if isinstance(windows_raw, list) else ()
        allowed_raw = item.get("allowed_task_classes", ())
        allowed = tuple(str(value) for value in allowed_raw) if isinstance(allowed_raw, list) else ()
        try:
            billing = BillingMode(str(item.get("billing", "metered")))
        except ValueError:
            billing = BillingMode.METERED
        profiles.append(ProviderResourceProfile(
            provider=name, billing=billing,
            quality=min(1.0, max(0.0, _number(item.get("quality"), 0.5))),
            tokens_per_second=max(0.0, _number(item.get("tokens_per_second"))),
            input_usd_per_million=max(0.0, _number(item.get("input_usd_per_million"))),
            output_usd_per_million=max(0.0, _number(item.get("output_usd_per_million"))),
            balance_usd=_number(item.get("balance_usd")) if item.get("balance_usd") is not None else None,
            max_concurrency=max(1, int(_number(item.get("max_concurrency"), 1))),
            active_requests=max(0, int(_number(item.get("active_requests")))),
            weak=bool(item.get("weak", False)), verification_required=bool(item.get("verification_required", False)),
            allowed_task_classes=allowed, windows=windows, observed_at=_number(item.get("observed_at")),
        ))
    return ResourceSnapshot(tuple(profiles), _number(raw.get("updated_at")))


def save_snapshot(snapshot: ResourceSnapshot, path: Path | None = None) -> None:
    target = resource_path() if path is None else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    providers: dict[str, Any] = {}
    for profile in snapshot.profiles:
        value = asdict(profile)
        value.pop("provider")
        value["billing"] = profile.billing.value
        providers[profile.provider] = value
    payload = {"schema": SCHEMA, "updated_at": snapshot.updated_at, "providers": providers}
    fd, temporary = tempfile.mkstemp(prefix=".provider-resources-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, target)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


@dataclass(frozen=True, slots=True)
class Reserve:
    provider: str
    tokens: int


@dataclass(frozen=True, slots=True)
class Release:
    provider: str
    tokens: int


@dataclass(frozen=True, slots=True)
class Snapshot:
    pass


class QuotaLedgerActor:
    """Single-writer reservations prevent quota oversubscription by parallel children."""

    def __init__(self, snapshot: ResourceSnapshot, *, capacity: int = 256) -> None:
        self._profiles = snapshot.by_provider()
        self._reserved: dict[str, int] = {}
        self._actor: MailboxActor[Reserve | Release | Snapshot, ResourceSnapshot] = MailboxActor(self._handle, capacity=capacity, name="quota-ledger")

    async def ask(self, command: Reserve | Release | Snapshot) -> ResourceSnapshot:
        return await self._actor.ask(command)

    async def close(self) -> None:
        await self._actor.close()

    async def _handle(self, command: Reserve | Release | Snapshot) -> ResourceSnapshot:
        if isinstance(command, Reserve):
            self._reserved[command.provider] = self._reserved.get(command.provider, 0) + max(0, command.tokens)
        elif isinstance(command, Release):
            self._reserved[command.provider] = max(0, self._reserved.get(command.provider, 0) - max(0, command.tokens))
        profiles = []
        for profile in self._profiles.values():
            reserved = self._reserved.get(profile.provider, 0)
            windows = tuple(replace(window, remaining_tokens=None if window.remaining_tokens is None else max(0, window.remaining_tokens - reserved)) for window in profile.windows)
            profiles.append(replace(profile, windows=windows))
        return ResourceSnapshot(tuple(profiles), time.time())


_CACHE: tuple[Path, int, ProviderPortfolioPolicy] | None = None


def policy_from_environment() -> ProviderPortfolioPolicy | None:
    global _CACHE
    path = resource_path()
    try: stamp = path.stat().st_mtime_ns
    except FileNotFoundError: return None
    if _CACHE and _CACHE[0] == path and _CACHE[1] == stamp: return _CACHE[2]
    policy = ProviderPortfolioPolicy(load_snapshot(path)); _CACHE = (path, stamp, policy); return policy


def environment_intent(*, incumbent: str | None) -> DispatchIntent:
    def integer(name: str, default: int) -> int:
        try: return max(0, int(os.environ.get(name, default)))
        except ValueError: return default
    return DispatchIntent(
        role=os.environ.get("CAMBIUM_TASK_ROLE", "subagent"),
        task_class=os.environ.get("CAMBIUM_TASK_CLASS", "code"),
        branch_key=os.environ.get("CAMBIUM_BRANCH_KEY", ""),
        expected_input_tokens=integer("CAMBIUM_EXPECTED_INPUT_TOKENS", 8000),
        expected_output_tokens=integer("CAMBIUM_EXPECTED_OUTPUT_TOKENS", 2000),
        incumbent=incumbent,
    )


def _parse_window(value: str) -> QuotaWindow:
    parts = value.split(":")
    if len(parts) != 4: raise argparse.ArgumentTypeError("NAME:REMAINING:LIMIT:RESET_SECONDS required")
    name, remaining, limit, reset = parts
    try: return QuotaWindow(name, max(0, int(remaining)), max(0, int(limit)), time.time() + max(0.0, float(reset)))
    except ValueError as exc: raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cambium-quota")
    parser.add_argument("--path", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show"); show.add_argument("--json", action="store_true")
    setp = sub.add_parser("set"); setp.add_argument("provider"); setp.add_argument("--billing", choices=[x.value for x in BillingMode], default="metered")
    setp.add_argument("--quality", type=float, default=.5); setp.add_argument("--tps", type=float, default=0); setp.add_argument("--balance-usd", type=float)
    setp.add_argument("--input-usd-per-million", type=float, default=0); setp.add_argument("--output-usd-per-million", type=float, default=0)
    setp.add_argument("--max-concurrency", type=int, default=1); setp.add_argument("--weak", action="store_true"); setp.add_argument("--verification-required", action="store_true")
    setp.add_argument("--allow", action="append", default=[]); setp.add_argument("--window", action="append", default=[], type=_parse_window)
    args = parser.parse_args(argv); snapshot = load_snapshot(args.path)
    if args.command == "show":
        if args.json: print(json.dumps({"schema": SCHEMA, "profiles": [asdict(x) for x in snapshot.profiles]}, default=str, sort_keys=True))
        else:
            for profile in sorted(snapshot.profiles, key=lambda x: x.provider):
                windows = ",".join(f"{w.name}:{w.remaining_tokens}/{w.limit_tokens}" for w in profile.windows) or "?"
                print(f"{profile.provider} {profile.billing.value} quality={profile.quality:.2f} tps={profile.tokens_per_second:.1f} quota={windows}")
        return 0
    profiles = snapshot.by_provider()
    profiles[args.provider] = ProviderResourceProfile(args.provider, BillingMode(args.billing), min(1, max(0, args.quality)), max(0, args.tps), max(0, args.input_usd_per_million), max(0, args.output_usd_per_million), args.balance_usd, max(1, args.max_concurrency), weak=args.weak, verification_required=args.verification_required, allowed_task_classes=tuple(args.allow), windows=tuple(args.window), observed_at=time.time())
    save_snapshot(ResourceSnapshot(tuple(profiles.values()), time.time()), args.path); return 0


if __name__ == "__main__": raise SystemExit(main())
''')

write("src/cambium/extensions.py", r'''"""Trusted extension seams inspired by pi's small extensible core."""
from __future__ import annotations
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class ToolExtension(Protocol):
    name: str
    schema: dict[str, Any]
    async def run(self, arguments: dict[str, Any], context: Any) -> Any: ...

@runtime_checkable
class QuotaExtension(Protocol):
    provider: str
    def observe(self, *, headers: dict[str, str], payload: dict[str, Any] | None = None) -> Any: ...

def _load(group: str) -> tuple[Any, ...]:
    result=[]
    for point in entry_points(group=group):
        value=point.load(); result.append(value() if isinstance(value, type) else value)
    return tuple(result)

def tool_extensions() -> tuple[ToolExtension, ...]:
    values=_load("cambium.tools")
    if any(not isinstance(value, ToolExtension) for value in values): raise TypeError("invalid cambium.tools extension")
    return values

def quota_extensions() -> tuple[QuotaExtension, ...]:
    values=_load("cambium.quota_adapters")
    if any(not isinstance(value, QuotaExtension) for value in values): raise TypeError("invalid cambium.quota_adapters extension")
    return values
''')

# Base routing remains the hard policy; portfolio ranking refines only its output.
rename_top_level("src/cambium/selection.py", "order_candidates", "_configured_order_candidates")
append_once("src/cambium/selection.py", "CAMBIUM_RESOURCE_POLICY", r'''# CAMBIUM_RESOURCE_POLICY
def order_candidates(*args: Any, **kwargs: Any) -> list[Any]:
    ordered = _configured_order_candidates(*args, **kwargs)
    from .provider_resources import environment_intent, policy_from_environment
    incumbent = kwargs.get("incumbent")
    intent = environment_intent(incumbent=incumbent)
    policy = policy_from_environment()
    if policy is not None:
        return policy.order(ordered, intent)
    if intent.role == "main" and incumbent:
        index = next((i for i, item in enumerate(ordered) if getattr(item, "name", None) == incumbent), None)
        if index is not None:
            item = ordered.pop(index); ordered.insert(0, item)
    return ordered
''')

# Short Python snippets reuse the existing shell authorization, timeout and output caps.
append_once("src/cambium/schemas.py", "CAMBIUM_RUN_PYTHON", r'''# CAMBIUM_RUN_PYTHON
_RUN_PYTHON_SCHEMA = {"type": "function", "function": {"name": "run_python", "description": "Run a short Python snippet in an isolated interpreter process in the worktree. Same host authority as run_shell; not a sandbox.", "parameters": {"type": "object", "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 16000}}, "required": ["code"], "additionalProperties": False}}}
TOOL_SCHEMAS = [*TOOL_SCHEMAS, _RUN_PYTHON_SCHEMA]
from .extensions import tool_extensions as _cambium_tool_extensions
for _extension in _cambium_tool_extensions(): TOOL_SCHEMAS = [*TOOL_SCHEMAS, _extension.schema]
''')
is_async, args = rename_top_level("src/cambium/tools.py", "run_tool", "_run_builtin_tool")
if len(args) < 3: raise RuntimeError(f"unexpected run_tool signature: {args}
")
name, arguments, context = args[:3]
if is_async:
    wrapper = f'''# CAMBIUM_TOOL_WRAPPER\nasync def run_tool({name}: str, {arguments}: dict[str, Any], {context}: Any) -> Any:\n    import shlex as _shlex, sys as _sys\n    from .extensions import tool_extensions as _extensions\n    if {name} == "run_python":\n        code = {arguments}.get("code")\n        if not isinstance(code, str) or not code or len(code) > 16000: raise ValueError("invalid Python snippet")\n        return await _run_builtin_tool("run_shell", {{"cmd": f"{{_shlex.quote(_sys.executable)}} -I -c {{_shlex.quote(code)}}"}}, {context})\n    for extension in _extensions():\n        if extension.name == {name}: return await extension.run({arguments}, {context})\n    return await _run_builtin_tool({name}, {arguments}, {context})\n'''
else:
    wrapper = f'''# CAMBIUM_TOOL_WRAPPER\ndef run_tool({name}: str, {arguments}: dict[str, Any], {context}: Any) -> Any:\n    import asyncio as _asyncio, shlex as _shlex, sys as _sys\n    from .extensions import tool_extensions as _extensions\n    if {name} == "run_python":\n        code = {arguments}.get("code")\n        if not isinstance(code, str) or not code or len(code) > 16000: raise ValueError("invalid Python snippet")\n        return _run_builtin_tool("run_shell", {{"cmd": f"{{_shlex.quote(_sys.executable)}} -I -c {{_shlex.quote(code)}}"}}, {context})\n    for extension in _extensions():\n        if extension.name == {name}: return _asyncio.run(extension.run({arguments}, {context}))\n    return _run_builtin_tool({name}, {arguments}, {context})\n'''
append_once("src/cambium/tools.py", "CAMBIUM_TOOL_WRAPPER", wrapper)

# Refresh-token rotation is single-flight across concurrent TaskGroup workers.
append_once("src/cambium/oauth.py", "CAMBIUM_OAUTH_SINGLEFLIGHT", r'''# CAMBIUM_OAUTH_SINGLEFLIGHT
import threading as _cambium_threading
_CMB_REFRESH_GUARD = _cambium_threading.Lock()
_CMB_REFRESH_LOCKS: dict[str, _cambium_threading.RLock] = {}
_CMB_ORIGINAL_ENSURE_FRESH = TokenManager.ensure_fresh

def _cambium_ensure_fresh(self: Any, *args: Any, **kwargs: Any) -> Any:
    provider = str(kwargs.get("provider") or (args[0] if args else "default"))
    with _CMB_REFRESH_GUARD: lock = _CMB_REFRESH_LOCKS.setdefault(provider, _cambium_threading.RLock())
    with lock: return _CMB_ORIGINAL_ENSURE_FRESH(self, *args, **kwargs)

TokenManager.ensure_fresh = _cambium_ensure_fresh
''')

# Worker role is immutable process input; mutable quota reservations stay in their actor.
p = ROOT / "src/cambium/supervisor.py"
source = p.read_text(encoding="utf-8"); tree = ast.parse(source)
fn = next((node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_worker_environment"), None)
if fn is None: raise RuntimeError("_worker_environment missing")
returns = [node for node in fn.body if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)]
if not returns: returns = [node for node in ast.walk(fn) if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)]
ret=max(returns,key=lambda node: node.lineno); env=ret.value.id; lines=source.splitlines(keepends=True); indent=" "*ret.col_offset
lines.insert(ret.lineno-1, f'{indent}_parent = spec.get("parent_task_id") or spec.get("parent_id") or spec.get("context_fork")\n{indent}{env}["CAMBIUM_TASK_ROLE"] = "subagent" if _parent else "main"\n{indent}{env}["CAMBIUM_TASK_CLASS"] = str(spec.get("task_class") or spec.get("kind") or ("code" if _parent else "main"))\n{indent}{env}["CAMBIUM_BRANCH_KEY"] = str(spec.get("branch") or spec.get("task_id") or "")\n')
p.write_text("".join(lines), encoding="utf-8")

# Display resource windows in the already event-sourced dashboard.
append_once("src/cambium/monitor.py", "CAMBIUM_RESOURCE_PANEL", r'''# CAMBIUM_RESOURCE_PANEL
def _resource_panel() -> list[str]:
    try:
        from .provider_resources import load_snapshot
        snapshot = load_snapshot()
    except (OSError, ValueError): return []
    lines=[]
    for profile in sorted(snapshot.profiles, key=lambda item: item.provider):
        windows=",".join(f"{w.name}:{w.remaining_tokens if w.remaining_tokens is not None else '?'}/{w.limit_tokens if w.limit_tokens is not None else '?'}" for w in profile.windows) or "?"
        lines.append(f"{profile.provider} {profile.billing.value} q={profile.quality:.2f} tps={profile.tokens_per_second:.1f} quota={windows}")
    return lines

if "AnsiDashboard" in globals() and hasattr(AnsiDashboard, "render"):
    _ResourceBaseDashboard = AnsiDashboard
    class AnsiDashboard(_ResourceBaseDashboard):
        def render(self, *args: Any, **kwargs: Any) -> str:
            frame=super().render(*args, **kwargs); panel=_resource_panel()
            return frame if not panel else frame.rstrip()+"\n\nPROVIDER RESOURCES\n"+"\n".join(panel)+"\n"
''')

# Operator entry point.
p = ROOT / "pyproject.toml"; source=p.read_text(encoding="utf-8")
if "cambium-quota" not in source:
    marker="[project.scripts]"; pos=source.index(marker)+len(marker)
    source=source[:pos]+'\ncambium-quota = "cambium.provider_resources:main"'+source[pos:]
    p.write_text(source, encoding="utf-8")

write("docs/architecture/provider-resources.md", '''# Provider resources and dispatch

The main branch owns one cache-affinity lease and stays on its provider/model/protocol while that lane remains feasible. Children are scheduled independently.

Eligibility, health, capacity, quota windows, monetary cost, quality, throughput, and cache switching cost remain separate dimensions. Five-hour, weekly, and monthly subscription windows are conjunctive resources paced with shadow prices. Prepaid providers use wallet balance and marginal cost. Weak/free models are confined by default to research, indexing, summarization, test triage, and speculative work whose result is verified or escalated.

Mutable reservations belong to one bounded mailbox actor. `CAMBIUM_PROVIDER_RESOURCES` selects the private resource JSON; `cambium-quota` manages it. Provider-specific quota telemetry plugs into `cambium.quota_adapters`.
''')
write("docs/architecture/tools-and-extensions.md", '''# Tools and extensions

Cambium keeps a small typed core. `run_python` executes a short snippet through the existing authorized shell boundary with `python -I`; it is an ergonomics/latency feature, not containment. Trusted packages can provide `cambium.tools` and `cambium.quota_adapters` entry points. Weak/free child profiles should remain read-only; mutations are admitted and verified by the parent.
''')

write("tests/scenarios/test_provider_resources.py", r'''from __future__ import annotations
import asyncio, time
from dataclasses import dataclass
from cambium.mailbox import MailboxActor
from cambium.provider_resources import *

@dataclass(frozen=True)
class Candidate:
    name: str
    priority: int = 0

def test_main_affinity_is_strict_while_eligible():
    policy=ProviderPortfolioPolicy(ResourceSnapshot((ProviderResourceProfile("slow", quality=.8, tokens_per_second=10), ProviderResourceProfile("fast", quality=.9, tokens_per_second=100)),0))
    assert [x.name for x in policy.order([Candidate("fast"),Candidate("slow")],DispatchIntent(role="main",task_class="main",incumbent="slow"),now=0)]==["slow","fast"]

def test_priority_is_never_crossed_for_children():
    policy=ProviderPortfolioPolicy(ResourceSnapshot((ProviderResourceProfile("later", quality=1,tokens_per_second=100),ProviderResourceProfile("first",quality=.1,tokens_per_second=1)),0))
    assert policy.order([Candidate("first",0),Candidate("later",1)],DispatchIntent(task_class="research"),now=0)[0].name=="first"

def test_weak_free_models_are_only_used_for_safe_verified_work():
    policy=ProviderPortfolioPolicy(ResourceSnapshot((ProviderResourceProfile("free",billing=BillingMode.FREE,quality=.4,tokens_per_second=200,weak=True,verification_required=True),ProviderResourceProfile("strong",quality=.9,tokens_per_second=30)),0))
    assert policy.order([Candidate("free"),Candidate("strong")],DispatchIntent(task_class="code"),now=0)[0].name=="strong"
    assert policy.order([Candidate("strong"),Candidate("free")],DispatchIntent(task_class="research"),now=0)[0].name=="free"

def test_weekly_window_can_dominate_healthy_five_hour_window():
    now=time.time(); healthy=ProviderResourceProfile("p",tokens_per_second=20,windows=(QuotaWindow("5h",90000,100000,now+18000),QuotaWindow("week",900000,1000000,now+604800)))
    scarce=ProviderResourceProfile("p",tokens_per_second=20,windows=(QuotaWindow("5h",90000,100000,now+18000),QuotaWindow("week",5000,1000000,now+604800)))
    intent=DispatchIntent(task_class="research",expected_input_tokens=1000,expected_output_tokens=1000)
    assert score_profile(scarce,intent,now=now)<score_profile(healthy,intent,now=now)

def test_mailbox_serializes_writers():
    async def scenario():
        value=0
        async def handle(delta):
            nonlocal value
            old=value; await asyncio.sleep(0); value=old+delta; return value
        async with MailboxActor(handle,capacity=8) as actor:
            await asyncio.gather(*(actor.ask(1) for _ in range(100)))
        assert value==100
    asyncio.run(scenario())

def test_quota_actor_reserves_without_race():
    async def scenario():
        ledger=QuotaLedgerActor(ResourceSnapshot((ProviderResourceProfile("p",windows=(QuotaWindow("w",100,100,1000),)),),0))
        await asyncio.gather(*(ledger.ask(Reserve("p",1)) for _ in range(50)))
        snap=await ledger.ask(Snapshot()); assert snap.by_provider()["p"].windows[0].remaining_tokens==50
        await ledger.close()
    asyncio.run(scenario())
''')

index=ROOT/"docs/README.md"
if index.exists():
    source=index.read_text(encoding="utf-8")
    if "provider-resources.md" not in source: source += "\n- [`architecture/provider-resources.md`](architecture/provider-resources.md) — quota-aware dispatch and affinity.\n"
    if "tools-and-extensions.md" not in source: source += "- [`architecture/tools-and-extensions.md`](architecture/tools-and-extensions.md) — trusted tool/telemetry extensions.\n"
    index.write_text(source, encoding="utf-8")

for path in (ROOT/"src").rglob("*.py"): ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("runtime v2 applied")
