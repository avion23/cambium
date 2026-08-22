#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def append_once(path: str, marker: str, text: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    if marker not in source:
        target.write_text(source.rstrip() + "\n\n" + text.rstrip() + "\n", encoding="utf-8")


# Weak/free providers are useful only when the child has no mutating authority.
path = ROOT / "src/cambium/provider_resources.py"
source = path.read_text(encoding="utf-8")
source = source.replace(
    '    incumbent: str | None = None\n',
    '    incumbent: str | None = None\n    mutating_tools: bool = False\n',
    1,
)
source = source.replace(
    '    if not profile.allows(intent.task_class) or profile.active_requests >= profile.max_concurrency:\n',
    '    if not profile.allows(intent.task_class) or profile.active_requests >= profile.max_concurrency:\n        return -math.inf\n    if profile.weak and intent.mutating_tools:\n',
    1,
)
source = source.replace(
    '        incumbent=incumbent,\n    )\n',
    '        incumbent=incumbent,\n        mutating_tools=os.environ.get("CAMBIUM_MUTATING_TOOLS", "0") == "1",\n    )\n',
    1,
)
path.write_text(source, encoding="utf-8")

# Derive the mutating-tool bit once at the process boundary.
path = ROOT / "src/cambium/supervisor.py"
source = path.read_text(encoding="utf-8")
needle = 'environment["CAMBIUM_BRANCH_KEY"] = str(spec.get("branch") or spec.get("task_id") or "")\n'
if needle not in source:
    # The environment mapping name is discovered from the already-applied role lines.
    candidates = [line for line in source.splitlines() if 'CAMBIUM_BRANCH_KEY' in line]
    if len(candidates) != 1:
        raise RuntimeError("CAMBIUM_BRANCH_KEY assignment missing")
    needle = candidates[0].lstrip() + "\n"
    indent = candidates[0][: len(candidates[0]) - len(candidates[0].lstrip())]
    env_name = candidates[0].split('["CAMBIUM_BRANCH_KEY"]', 1)[0].strip()
else:
    line = next(line for line in source.splitlines() if 'CAMBIUM_BRANCH_KEY' in line)
    indent = line[: len(line) - len(line.lstrip())]
    env_name = line.split('["CAMBIUM_BRANCH_KEY"]', 1)[0].strip()
addition = (
    f'{indent}_permissions = spec.get("permissions") if isinstance(spec.get("permissions"), dict) else {{}}\n'
    f'{indent}_mutating = any(bool(_permissions.get(key)) for key in ("write", "shell", "git", "network"))\n'
    f'{indent}{env_name}["CAMBIUM_MUTATING_TOOLS"] = "1" if _mutating else "0"\n'
)
if "CAMBIUM_MUTATING_TOOLS" not in source:
    source = source.replace(indent + needle, indent + needle + addition, 1)
path.write_text(source, encoding="utf-8")

append_once("src/cambium/provider_resources.py", "CAMBIUM_TELEMETRY_COMMAND", r'''# CAMBIUM_TELEMETRY_COMMAND
@dataclass(frozen=True, slots=True)
class TelemetryCommand:
    provider: str
    command: tuple[str, ...]
    timeout_s: float = 10.0
    remaining_path: tuple[str, ...] = ("remaining_tokens",)
    limit_path: tuple[str, ...] = ("limit_tokens",)
    reset_path: tuple[str, ...] = ("resets_at",)
    window_name: str = "reported"


def _json_path(value: Any, path: tuple[str, ...]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


async def poll_telemetry(spec: TelemetryCommand) -> ProviderResourceProfile | None:
    """Run an explicitly configured JSON status command without a shell."""
    import asyncio
    if not spec.command:
        return None
    process = await asyncio.create_subprocess_exec(
        *spec.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=spec.timeout_s)
    except TimeoutError:
        process.kill(); await process.wait(); return None
    if process.returncode != 0 or len(stdout) > 1_048_576:
        return None
    try:
        payload = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError):
        return None
    remaining = _json_path(payload, spec.remaining_path)
    limit = _json_path(payload, spec.limit_path)
    reset = _json_path(payload, spec.reset_path)
    window = QuotaWindow(
        spec.window_name,
        max(0, int(remaining)) if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) else None,
        max(0, int(limit)) if isinstance(limit, (int, float)) and not isinstance(limit, bool) else None,
        float(reset) if isinstance(reset, (int, float)) and not isinstance(reset, bool) else None,
    )
    return ProviderResourceProfile(spec.provider, windows=(window,), observed_at=time.time())


def merge_observation(snapshot: ResourceSnapshot, observation: ProviderResourceProfile) -> ResourceSnapshot:
    profiles = snapshot.by_provider()
    existing = profiles.get(observation.provider)
    if existing is None:
        profiles[observation.provider] = observation
    else:
        profiles[observation.provider] = replace(
            existing,
            windows=observation.windows or existing.windows,
            tokens_per_second=observation.tokens_per_second or existing.tokens_per_second,
            observed_at=max(existing.observed_at, observation.observed_at),
        )
    return ResourceSnapshot(tuple(profiles.values()), time.time())
''')

# Local slash commands are handled by the same branch actor; they never create
# another model turn or mutate the checkpoint head accidentally.
append_once("src/cambium/interactive.py", "CAMBIUM_LOCAL_COMMANDS", r'''# CAMBIUM_LOCAL_COMMANDS
async def _interactive_local_command(self: InteractiveSession, prompt: str) -> Any | None:
    if not prompt.startswith("/"):
        return None
    from .provider_resources import load_snapshot
    from .stats import usage_stats_from_events
    from .supervisor import PlanResult, TaskResult

    command, _, argument = prompt.partition(" ")
    if command == "/new":
        self._checkpoint_ref = None; self._epoch = None
        text = "started a new branch head"
    elif command == "/usage":
        usage = usage_stats_from_events(self._events)
        text = "no usage yet" if usage is None else (
            f"calls={usage.calls} input={usage.input_tokens} output={usage.output_tokens} "
            f"cached={usage.cached_tokens} total={usage.total_tokens} cost=${usage.estimated_cost_usd:.6f}"
        )
    elif command == "/quota":
        snapshot = load_snapshot()
        text = "\n".join(
            f"{item.provider}: {item.billing.value} q={item.quality:.2f} tps={item.tokens_per_second:.1f} "
            + ",".join(f"{w.name}={w.remaining_tokens}/{w.limit_tokens}" for w in item.windows)
            for item in snapshot.profiles
        ) or "no provider resource observations"
    elif command == "/model":
        provider = model = "?"
        for event in reversed(self._events):
            if event.get("kind") != "usage_event": continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            provider = str(payload.get("provider") or "?"); model = str(payload.get("model") or "?"); break
        text = f"provider={provider} model={model} checkpoint={self._checkpoint_ref or '?'} epoch={self._epoch or 0}"
    elif command == "/help":
        text = "/new /usage /quota /model /help /exit"
    elif command == "/exit":
        text = "use the frontend exit command to close the session"
    else:
        return None
    return PlanResult((TaskResult(task_id="interactive-command", status="succeeded", exit_code=0, summary=text),))

_InteractiveOriginalHandle = InteractiveSession._handle
async def _interactive_handle_with_commands(self: InteractiveSession, command: Submit | Reset) -> Any:
    if isinstance(command, Submit):
        local = await _interactive_local_command(self, command.prompt)
        if local is not None:
            return local
    return await _InteractiveOriginalHandle(self, command)

InteractiveSession._handle = _interactive_handle_with_commands
''')

# Tests pin the authority boundary and local commands.
(ROOT / "tests/scenarios/test_provider_resource_commands.py").write_text(r'''from __future__ import annotations
import asyncio
from pathlib import Path
from cambium import interactive
from cambium.oneshot import OneShotConfig
from cambium.provider_resources import *


def test_weak_provider_rejected_when_child_can_mutate():
    weak=ProviderResourceProfile("free",billing=BillingMode.FREE,quality=1,tokens_per_second=1000,weak=True)
    intent=DispatchIntent(task_class="research",mutating_tools=True)
    assert score_profile(weak,intent,now=0)==float("-inf")


def test_local_quota_command_does_not_call_model(monkeypatch,tmp_path:Path):
    async def forbidden(*args,**kwargs): raise AssertionError("model called")
    monkeypatch.setattr(interactive.oneshot,"run_oneshot",forbidden)
    async def scenario():
        session=interactive.InteractiveSession(OneShotConfig(repo=tmp_path))
        result=await session.submit("/quota"); await session.close(); return result
    result=asyncio.run(scenario())
    assert result.results[0].status=="succeeded"
''', encoding="utf-8")

for path in (ROOT / "src").rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("commands and telemetry applied")
