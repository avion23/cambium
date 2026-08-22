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
    lines[node.lineno - 1] = re.sub(rf"\b{re.escape(old)}\b", new, lines[node.lineno - 1], count=1)
    target.write_text("".join(lines), encoding="utf-8")


path = ROOT / "src/cambium/provider_resources.py"
source = path.read_text(encoding="utf-8")
if "CAMBIUM_FEASIBLE_AFFINITY" not in source:
    pattern = re.compile(
        r'        if intent\.role == "main" and intent\.incumbent:\n'
        r'(?:            .*\n)+?'
        r'        current = time\.time\(\) if now is None else now\n',
    )
    replacement = '''        current = time.time() if now is None else now
        # CAMBIUM_FEASIBLE_AFFINITY: keep the main lane only while every
        # configured resource constraint still admits the expected turn.
        if intent.role == "main" and intent.incumbent:
            incumbent_profile = self._profiles.get(intent.incumbent)
            if incumbent_profile is None:
                return ordered
            incumbent_score = score_profile(incumbent_profile, intent, now=current)
            if not math.isinf(incumbent_score):
                return ordered
'''
    source, count = pattern.subn(replacement, source, count=1)
    if count != 1:
        raise RuntimeError("could not replace main affinity block")
    path.write_text(source, encoding="utf-8")

append_once("src/cambium/provider_resources.py", "CAMBIUM_NORMALIZED_STATUS_COMMAND", r'''# CAMBIUM_NORMALIZED_STATUS_COMMAND
@dataclass(frozen=True, slots=True)
class NormalizedStatus:
    provider: str
    tokens_per_second: float | None = None
    balance_usd: float | None = None
    windows: tuple[QuotaWindow, ...] = ()


def parse_normalized_status(provider: str, payload: Any) -> NormalizedStatus:
    """Validate one adapter/CLI status document without vendor-specific guesses."""
    if not isinstance(payload, Mapping):
        raise ValueError("status payload must be an object")
    unknown = set(payload) - {"tokens_per_second", "balance_usd", "windows"}
    if unknown:
        raise ValueError(f"unknown status keys: {sorted(unknown)}")
    tps_raw = payload.get("tokens_per_second")
    balance_raw = payload.get("balance_usd")
    tps = None
    if tps_raw is not None:
        if isinstance(tps_raw, bool) or not isinstance(tps_raw, (int, float)) or not math.isfinite(tps_raw) or tps_raw < 0:
            raise ValueError("tokens_per_second must be a finite non-negative number")
        tps = float(tps_raw)
    balance = None
    if balance_raw is not None:
        if isinstance(balance_raw, bool) or not isinstance(balance_raw, (int, float)) or not math.isfinite(balance_raw) or balance_raw < 0:
            raise ValueError("balance_usd must be a finite non-negative number")
        balance = float(balance_raw)
    windows_raw = payload.get("windows", [])
    if not isinstance(windows_raw, list):
        raise ValueError("windows must be a list")
    windows = tuple(_window(value) for value in windows_raw if isinstance(value, Mapping))
    if len(windows) != len(windows_raw):
        raise ValueError("every window must be an object")
    return NormalizedStatus(provider, tps, balance, windows)


async def run_normalized_status_command(
    provider: str,
    command: Sequence[str],
    *,
    timeout_s: float = 15.0,
) -> NormalizedStatus:
    """Execute a trusted adapter without a shell and parse bounded JSON output."""
    import asyncio

    if not command:
        raise ValueError("status command must not be empty")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError("status command timed out") from None
    if process.returncode != 0:
        detail = stderr.decode("utf-8", "replace")[:200]
        raise ValueError(f"status command failed ({process.returncode}): {detail}")
    if len(stdout) > 1_048_576:
        raise ValueError("status command output exceeds 1 MiB")
    try:
        payload = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"status command did not return JSON: {exc}") from None
    return parse_normalized_status(provider, payload)


def apply_normalized_status(
    status: NormalizedStatus,
    *,
    path: Path | None = None,
) -> ResourceSnapshot:
    return record_observation(
        status.provider,
        tokens_per_second=status.tokens_per_second,
        windows=status.windows or None,
        balance_usd=status.balance_usd,
        path=path,
    )
''')

# Add `poll` as a thin wrapper around the normalized adapter contract.
source = path.read_text(encoding="utf-8")
if "CAMBIUM_RESOURCE_CLI_V4" not in source:
    rename_top_level("src/cambium/provider_resources.py", "main", "_resource_main_v3")
    append_once("src/cambium/provider_resources.py", "CAMBIUM_RESOURCE_CLI_V4", r'''# CAMBIUM_RESOURCE_CLI_V4
def main(argv: Sequence[str] | None = None) -> int:
    import asyncio
    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] != "poll":
        return _resource_main_v3(arguments)
    arguments.pop(0)
    parser = argparse.ArgumentParser(prog="cambium-quota poll")
    parser.add_argument("provider")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(arguments)
    command = list(parsed.command)
    if command and command[0] == "--":
        command.pop(0)
    status = asyncio.run(
        run_normalized_status_command(
            parsed.provider,
            command,
            timeout_s=max(0.1, parsed.timeout),
        )
    )
    apply_normalized_status(status, path=parsed.path)
    return 0
''')

# Tests cover exhaustion failover, independent child portfolio scheduling,
# normalized multi-window status, and concurrent refresh single-flight.
(ROOT / "tests/scenarios/test_provider_portfolio_e2e.py").write_text(r'''from __future__ import annotations

from dataclasses import dataclass

from cambium.provider_resources import (
    BillingMode,
    DispatchIntent,
    ProviderPortfolioPolicy,
    ProviderResourceProfile,
    QuotaWindow,
    ResourceSnapshot,
    parse_normalized_status,
)


@dataclass(frozen=True)
class Candidate:
    name: str
    priority: int = 0


def test_main_stays_incumbent_until_hard_window_exhaustion():
    policy = ProviderPortfolioPolicy(
        ResourceSnapshot(
            (
                ProviderResourceProfile(
                    "codex",
                    billing=BillingMode.SUBSCRIPTION,
                    quality=0.95,
                    windows=(QuotaWindow("week", 50_000, 100_000, 10_000),),
                ),
                ProviderResourceProfile("zai", billing=BillingMode.SUBSCRIPTION, quality=0.85),
            ),
            0,
        )
    )
    candidates = [Candidate("codex"), Candidate("zai")]
    intent = DispatchIntent(
        role="main",
        task_class="main",
        incumbent="codex",
        expected_input_tokens=10_000,
        expected_output_tokens=5_000,
    )
    assert policy.order(candidates, intent, now=0)[0].name == "codex"

    exhausted = ProviderPortfolioPolicy(
        ResourceSnapshot(
            (
                ProviderResourceProfile(
                    "codex",
                    billing=BillingMode.SUBSCRIPTION,
                    quality=0.95,
                    windows=(QuotaWindow("week", 1_000, 100_000, 10_000),),
                ),
                ProviderResourceProfile("zai", billing=BillingMode.SUBSCRIPTION, quality=0.85),
            ),
            0,
        )
    )
    assert exhausted.order(candidates, intent, now=0)[0].name == "zai"


def test_free_child_lane_and_paid_fallback_form_a_portfolio():
    policy = ProviderPortfolioPolicy(
        ResourceSnapshot(
            (
                ProviderResourceProfile(
                    "openrouter-free",
                    billing=BillingMode.FREE,
                    quality=0.5,
                    tokens_per_second=200,
                    weak=True,
                    verification_required=True,
                    allowed_task_classes=("research",),
                ),
                ProviderResourceProfile(
                    "openrouter-paid",
                    billing=BillingMode.PREPAID,
                    quality=0.8,
                    tokens_per_second=40,
                    balance_usd=20,
                ),
            ),
            0,
        )
    )
    candidates = [Candidate("openrouter-paid"), Candidate("openrouter-free")]
    assert policy.order(
        candidates,
        DispatchIntent(task_class="research", branch_key="read-only"),
        now=0,
    )[0].name == "openrouter-free"
    assert policy.order(
        candidates,
        DispatchIntent(task_class="code", branch_key="write", mutating_tools=True),
        now=0,
    )[0].name == "openrouter-paid"


def test_normalized_status_accepts_multiple_conjunctive_windows():
    status = parse_normalized_status(
        "codex",
        {
            "tokens_per_second": 42,
            "windows": [
                {"name": "5h", "remaining_tokens": 100, "limit_tokens": 200, "resets_at": 10},
                {"name": "week", "remaining_tokens": 300, "limit_tokens": 400, "resets_at": 20},
                {"name": "month", "remaining_tokens": 500, "limit_tokens": 600, "resets_at": 30},
            ],
        },
    )
    assert [window.name for window in status.windows] == ["5h", "week", "month"]
''', encoding="utf-8")

(ROOT / "tests/scenarios/test_oauth_refresh_singleflight.py").write_text(r'''from __future__ import annotations

import concurrent.futures
import threading
import time

from cambium import oauth


def test_codex_refresh_is_single_flight_per_provider(monkeypatch):
    active = 0
    peak = 0
    guard = threading.Lock()

    def fake(_self, *args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with guard:
            active -= 1
        return "token"

    monkeypatch.setattr(oauth, "_CMB_ORIGINAL_ENSURE_FRESH", fake)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: oauth._cambium_ensure_fresh(object(), provider="codex"),
                range(16),
            )
        )
    assert results == ["token"] * 16
    assert peak == 1
''', encoding="utf-8")

# Document the normalized status adapter contract without embedding unstable
# vendor URLs, cookies, or undocumented endpoints.
doc = ROOT / "docs/operator-runtime.md"
if doc.exists():
    text = doc.read_text(encoding="utf-8")
    if "cambium-quota poll" not in text:
        text += '''

## Automatic quota status adapters

A provider-specific CLI or trusted plugin can emit the normalized document:

```json
{
  "tokens_per_second": 42.0,
  "balance_usd": 18.5,
  "windows": [
    {"name": "5h", "remaining_tokens": 100000, "limit_tokens": 200000, "resets_at": 1770000000},
    {"name": "week", "remaining_tokens": 500000, "limit_tokens": 1000000, "resets_at": 1770500000}
  ]
}
```

Poll it without a shell:

```sh
uv run cambium-quota poll codex -- /path/to/codex-status-adapter --json
uv run cambium-quota poll zai -- /path/to/zai-status-adapter --json
```

The adapter owns authentication and vendor parsing. Cambium validates bounded
JSON and atomically merges only the normalized resource observation.
'''
        doc.write_text(text, encoding="utf-8")

for candidate in (ROOT / "src").rglob("*.py"):
    ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
print("final runtime corrections applied")
