#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def append_once(path: str, marker: str, text: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    if marker not in source:
        target.write_text(source.rstrip() + "\n\n" + text.rstrip() + "\n", encoding="utf-8")


def replace_once(path: str, old: str, new: str, label: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    target.write_text(source.replace(old, new, 1), encoding="utf-8")


def rename_top_level(path: str, old: str, new: str) -> None:
    target = ROOT / path
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == old
    ]
    if len(nodes) != 1:
        raise RuntimeError(f"{path}: expected one {old}, found {len(nodes)}")
    node = nodes[0]
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1] = re.sub(
        rf"\b{re.escape(old)}\b", new, lines[node.lineno - 1], count=1
    )
    target.write_text("".join(lines), encoding="utf-8")


# Main-agent affinity never bypasses configured priority. The configured order
# already puts the incumbent first inside its feasible priority run.
provider_path = ROOT / "src/cambium/provider_resources.py"
provider_source = provider_path.read_text(encoding="utf-8")
old_main = '''        if intent.role == "main" and intent.incumbent:
            index = next((i for i, item in enumerate(ordered) if item.name == intent.incumbent), None)
            if index is not None:
                item = ordered.pop(index)
                ordered.insert(0, item)
                return ordered
'''
new_main = '''        if intent.role == "main" and intent.incumbent:
            if any(item.name == intent.incumbent for item in ordered):
                return ordered
'''
if old_main in provider_source:
    provider_path.write_text(provider_source.replace(old_main, new_main, 1), encoding="utf-8")
elif new_main not in provider_source:
    raise RuntimeError("main-affinity policy shape changed")

# Entry-point discovery is immutable for one process; do not rescan package
# metadata on every tool invocation.
extensions_path = ROOT / "src/cambium/extensions.py"
extensions = extensions_path.read_text(encoding="utf-8")
if "@cache\ndef _load" not in extensions:
    if "from functools import cache" not in extensions:
        extensions = extensions.replace(
            "from importlib.metadata import entry_points\n",
            "from functools import cache\nfrom importlib.metadata import entry_points\n",
            1,
        )
    extensions = extensions.replace("def _load(group: str)", "@cache\ndef _load(group: str)", 1)
    extensions_path.write_text(extensions, encoding="utf-8")

append_once("src/cambium/provider_resources.py", "CAMBIUM_RESOURCE_HARDENING", r'''# CAMBIUM_RESOURCE_HARDENING
@dataclass(frozen=True, slots=True)
class ScoreExplanation:
    provider: str
    feasible: bool
    score: float | None
    quality: float
    tokens_per_second: float
    expected_cost_usd: float
    scarcity: float | None
    reason: str


def explain_profile(
    profile: ProviderResourceProfile,
    intent: DispatchIntent,
    *,
    now: float | None = None,
) -> ScoreExplanation:
    current = time.time() if now is None else now
    demand = max(0, intent.expected_input_tokens) + max(0, intent.expected_output_tokens)
    if not profile.allows(intent.task_class):
        return ScoreExplanation(profile.provider, False, None, profile.quality, profile.tokens_per_second, 0.0, None, "task class not allowed")
    if profile.weak and intent.mutating_tools:
        return ScoreExplanation(profile.provider, False, None, profile.quality, profile.tokens_per_second, 0.0, None, "weak lane cannot mutate")
    if profile.active_requests >= profile.max_concurrency:
        return ScoreExplanation(profile.provider, False, None, profile.quality, profile.tokens_per_second, 0.0, None, "concurrency exhausted")
    scarcity = 0.0
    for window in profile.windows:
        pressure = window.scarcity(demand, now=current, measured_tps=profile.tokens_per_second)
        if math.isinf(pressure):
            return ScoreExplanation(profile.provider, False, None, profile.quality, profile.tokens_per_second, 0.0, None, f"{window.name} exhausted")
        scarcity = max(scarcity, pressure)
    cost = profile.expected_cost(intent.expected_input_tokens, intent.expected_output_tokens)
    if profile.balance_usd is not None and cost > profile.balance_usd:
        return ScoreExplanation(profile.provider, False, None, profile.quality, profile.tokens_per_second, cost, scarcity, "wallet balance exhausted")
    score = score_profile(profile, intent, now=current)
    return ScoreExplanation(profile.provider, True, score, profile.quality, profile.tokens_per_second, cost, scarcity, "eligible")


def update_snapshot(
    mutator: Callable[[ResourceSnapshot], ResourceSnapshot],
    path: Path | None = None,
) -> ResourceSnapshot:
    """Process-safe read/modify/write for CLI and telemetry writers."""
    import fcntl

    target = resource_path() if path is None else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = target.with_name(target.name + ".lock")
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current = load_snapshot(target)
        updated = mutator(current)
        save_snapshot(updated, target)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return updated


def record_observation(
    provider: str,
    *,
    tokens_per_second: float | None = None,
    windows: tuple[QuotaWindow, ...] | None = None,
    balance_usd: float | None = None,
    path: Path | None = None,
) -> ResourceSnapshot:
    """Merge one trusted quota/throughput observation atomically."""
    def mutate(snapshot: ResourceSnapshot) -> ResourceSnapshot:
        profiles = snapshot.by_provider()
        old = profiles.get(provider, ProviderResourceProfile(provider))
        observed_tps = old.tokens_per_second
        if tokens_per_second is not None and math.isfinite(tokens_per_second) and tokens_per_second >= 0:
            observed_tps = tokens_per_second if observed_tps <= 0 else 0.8 * observed_tps + 0.2 * tokens_per_second
        profiles[provider] = replace(
            old,
            tokens_per_second=observed_tps,
            windows=old.windows if windows is None else windows,
            balance_usd=old.balance_usd if balance_usd is None else max(0.0, balance_usd),
            observed_at=time.time(),
        )
        return ResourceSnapshot(tuple(profiles.values()), time.time())

    return update_snapshot(mutate, path)
''')

# Callable is needed by the atomic update seam.
provider_source = provider_path.read_text(encoding="utf-8")
if "from collections.abc import Callable," not in provider_source:
    provider_source = provider_source.replace(
        "from collections.abc import Mapping, Sequence\n",
        "from collections.abc import Callable, Mapping, Sequence\n",
        1,
    )
    provider_path.write_text(provider_source, encoding="utf-8")

# Add an explainable command surface without destabilizing the existing parser.
provider_source = provider_path.read_text(encoding="utf-8")
if "CAMBIUM_RESOURCE_CLI_V2" not in provider_source:
    rename_top_level("src/cambium/provider_resources.py", "main", "_resource_main")
    append_once("src/cambium/provider_resources.py", "CAMBIUM_RESOURCE_CLI_V2", r'''# CAMBIUM_RESOURCE_CLI_V2
def main(argv: Sequence[str] | None = None) -> int:
    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"recommend", "observe"}:
        return _resource_main(arguments)

    command = arguments.pop(0)
    parser = argparse.ArgumentParser(prog=f"cambium-quota {command}")
    parser.add_argument("--path", type=Path)
    if command == "recommend":
        parser.add_argument("--role", choices=("main", "subagent"), default="subagent")
        parser.add_argument("--task-class", default="code")
        parser.add_argument("--branch-key", default="operator")
        parser.add_argument("--incumbent")
        parser.add_argument("--input-tokens", type=int, default=8000)
        parser.add_argument("--output-tokens", type=int, default=2000)
        parser.add_argument("--mutating", action="store_true")
        parsed = parser.parse_args(arguments)
        intent = DispatchIntent(
            role=parsed.role,
            task_class=parsed.task_class,
            branch_key=parsed.branch_key,
            expected_input_tokens=max(0, parsed.input_tokens),
            expected_output_tokens=max(0, parsed.output_tokens),
            incumbent=parsed.incumbent,
            mutating_tools=parsed.mutating,
        )
        rows = [
            explain_profile(profile, intent)
            for profile in load_snapshot(parsed.path).profiles
        ]
        rows.sort(
            key=lambda row: (
                not row.feasible,
                -(row.score if row.score is not None else -math.inf),
                row.provider,
            )
        )
        for row in rows:
            score = "-" if row.score is None else f"{row.score:.3f}"
            scarcity = "?" if row.scarcity is None else f"{row.scarcity:.3f}"
            print(
                f"{row.provider} feasible={str(row.feasible).lower()} "
                f"score={score} q={row.quality:.2f} tps={row.tokens_per_second:.1f} "
                f"cost=${row.expected_cost_usd:.6f} scarcity={scarcity} {row.reason}"
            )
        return 0

    parser.add_argument("provider")
    parser.add_argument("--tps", type=float)
    parser.add_argument("--balance-usd", type=float)
    parser.add_argument("--window", action="append", default=[], type=_parse_window)
    parsed = parser.parse_args(arguments)
    record_observation(
        parsed.provider,
        tokens_per_second=parsed.tps,
        windows=tuple(parsed.window) if parsed.window else None,
        balance_usd=parsed.balance_usd,
        path=parsed.path,
    )
    return 0
''')

# Pin the fixed priority property and atomic updates.
test_path = ROOT / "tests/scenarios/test_provider_resources_hardening.py"
test_path.write_text(r'''from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from cambium.provider_resources import (
    DispatchIntent,
    ProviderPortfolioPolicy,
    ProviderResourceProfile,
    ResourceSnapshot,
    load_snapshot,
    record_observation,
)


@dataclass(frozen=True)
class Candidate:
    name: str
    priority: int = 0


def test_main_incumbent_never_crosses_priority_boundary():
    snapshot = ResourceSnapshot(
        (
            ProviderResourceProfile("preferred", quality=0.9),
            ProviderResourceProfile("incumbent", quality=0.9),
        ),
        0,
    )
    policy = ProviderPortfolioPolicy(snapshot)
    ordered = policy.order(
        [Candidate("preferred", 0), Candidate("incumbent", 10)],
        DispatchIntent(role="main", task_class="main", incumbent="incumbent"),
        now=0,
    )
    assert [item.name for item in ordered] == ["preferred", "incumbent"]


def test_atomic_observation_preserves_existing_profile_fields(tmp_path: Path):
    path = tmp_path / "resources.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "updated_at": 0,
                "providers": {
                    "codex": {
                        "billing": "subscription",
                        "quality": 0.9,
                        "tokens_per_second": 10,
                        "max_concurrency": 3,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    record_observation("codex", tokens_per_second=20, path=path)
    profile = load_snapshot(path).by_provider()["codex"]
    assert profile.quality == 0.9
    assert profile.max_concurrency == 3
    assert profile.tokens_per_second == 12
''', encoding="utf-8")

# Extend active docs rather than adding another speculative research file.
docs = ROOT / "docs/architecture/provider-resources.md"
if docs.exists():
    text = docs.read_text(encoding="utf-8")
    if "cambium-quota recommend" not in text:
        text += '''

## Operator workflow

Use `cambium-quota set` for stable billing/quality policy, `cambium-quota observe`
for trusted quota-window or measured-throughput updates, and
`cambium-quota recommend` to explain the current ordering for a task class.
Updates use a process-safe lock and atomic replacement. Unknown or stale quota
telemetry never becomes a fabricated zero; hard windows reject only when their
reported remaining resource is below the reserved demand.
'''
        docs.write_text(text, encoding="utf-8")

for path in (ROOT / "src").rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("runtime hardening applied")
