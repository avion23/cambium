#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
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
    lines[node.lineno - 1] = re.sub(rf"\b{old}\b", new, lines[node.lineno - 1], count=1)
    target.write_text("".join(lines), encoding="utf-8")


resources = ROOT / "src/cambium/provider_resources.py"
source = resources.read_text(encoding="utf-8")
if "CAMBIUM_RESOURCE_CLI_V3" not in source:
    rename_top_level("src/cambium/provider_resources.py", "main", "_resource_main_v2")
    append_once("src/cambium/provider_resources.py", "CAMBIUM_RESOURCE_CLI_V3", r'''# CAMBIUM_RESOURCE_CLI_V3
def example_snapshot() -> ResourceSnapshot:
    """Conservative policy template; quota values are observations, never guesses."""
    safe = tuple(sorted(SAFE_WEAK_TASKS))
    return ResourceSnapshot(
        (
            ProviderResourceProfile(
                "codex",
                billing=BillingMode.SUBSCRIPTION,
                quality=0.95,
                max_concurrency=1,
            ),
            ProviderResourceProfile(
                "zai",
                billing=BillingMode.SUBSCRIPTION,
                quality=0.82,
                max_concurrency=4,
            ),
            ProviderResourceProfile(
                "openrouter-paid",
                billing=BillingMode.PREPAID,
                quality=0.8,
                max_concurrency=8,
            ),
            ProviderResourceProfile(
                "openrouter-free",
                billing=BillingMode.FREE,
                quality=0.45,
                max_concurrency=4,
                weak=True,
                verification_required=True,
                allowed_task_classes=safe,
            ),
            ProviderResourceProfile(
                "opencode-zen",
                billing=BillingMode.FREE,
                quality=0.65,
                max_concurrency=4,
                weak=True,
                verification_required=True,
                allowed_task_classes=safe,
            ),
        ),
        time.time(),
    )


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"init", "validate"}:
        return _resource_main_v2(arguments)

    command = arguments.pop(0)
    parser = argparse.ArgumentParser(prog=f"cambium-quota {command}")
    parser.add_argument("--path", type=Path)
    if command == "init":
        parser.add_argument("--force", action="store_true")
        parsed = parser.parse_args(arguments)
        target = resource_path() if parsed.path is None else parsed.path
        if target.exists() and not parsed.force:
            parser.error(f"{target} already exists; pass --force to replace it")
        save_snapshot(example_snapshot(), target)
        print(target)
        return 0

    parsed = parser.parse_args(arguments)
    snapshot = load_snapshot(parsed.path)
    if not snapshot.profiles:
        parser.error("no provider profiles configured")
    names = [profile.provider for profile in snapshot.profiles]
    if len(names) != len(set(names)):
        parser.error("duplicate provider profiles")
    print(
        f"valid providers={len(snapshot.profiles)} "
        f"windows={sum(len(profile.windows) for profile in snapshot.profiles)}"
    )
    return 0
''')

example = ROOT / ".cambium/provider-resources.example.json"
example.parent.mkdir(parents=True, exist_ok=True)
if not example.exists():
    example.write_text(
        json.dumps(
            {
                "schema": 1,
                "updated_at": 0,
                "providers": {
                    "codex": {
                        "billing": "subscription",
                        "quality": 0.95,
                        "tokens_per_second": 0,
                        "max_concurrency": 1,
                        "windows": [],
                    },
                    "zai": {
                        "billing": "subscription",
                        "quality": 0.82,
                        "tokens_per_second": 0,
                        "max_concurrency": 4,
                        "windows": [],
                    },
                    "openrouter-paid": {
                        "billing": "prepaid",
                        "quality": 0.8,
                        "tokens_per_second": 0,
                        "max_concurrency": 8,
                        "balance_usd": 0,
                        "windows": [],
                    },
                    "openrouter-free": {
                        "billing": "free",
                        "quality": 0.45,
                        "tokens_per_second": 0,
                        "max_concurrency": 4,
                        "weak": True,
                        "verification_required": True,
                        "allowed_task_classes": [
                            "index",
                            "research",
                            "speculative",
                            "summarize",
                            "test_triage",
                        ],
                        "windows": [],
                    },
                    "opencode-zen": {
                        "billing": "free",
                        "quality": 0.65,
                        "tokens_per_second": 0,
                        "max_concurrency": 4,
                        "weak": True,
                        "verification_required": True,
                        "allowed_task_classes": [
                            "index",
                            "research",
                            "speculative",
                            "summarize",
                            "test_triage",
                        ],
                        "windows": [],
                    },
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

operator_doc = ROOT / "docs/operator-runtime.md"
operator_doc.write_text('''# Operator runtime

## Start

```sh
uv sync --extra dev --python 3.14
uv run cambium-quota init
uv run cambium tui --repo .
```

The TUI and REPL keep one durable checkpoint head across prompts. The attached
monitor replays the same event stream:

```sh
uv run cambium monitor /path/to/session
```

## Provider portfolio

Provider configuration and resource observations are deliberately separate.
The ordinary provider file contains endpoints, models, credentials, protocols,
and configured priority. The private resource file contains billing mode,
quality prior, measured output throughput, concurrency, wallet balance, and any
observed rolling windows.

```sh
uv run cambium-quota validate
uv run cambium-quota observe codex \
  --window 5h:REMAINING:LIMIT:SECONDS_TO_RESET \
  --window week:REMAINING:LIMIT:SECONDS_TO_RESET
uv run cambium-quota observe zai \
  --window 5h:REMAINING:LIMIT:SECONDS_TO_RESET
uv run cambium-quota observe openrouter-paid --balance-usd 20
uv run cambium-quota recommend --task-class code --mutating
uv run cambium-quota recommend --task-class research
```

Do not enter invented limits. Omit unknown windows until a trusted status source
or provider adapter reports them. Multiple windows are conjunctive: a healthy
five-hour window does not override an exhausted weekly or monthly window.

The main branch is sticky to its incumbent provider/model/protocol within its
feasible priority class. Child branches are portfolio-scheduled. Weak/free
profiles are read-only by default and limited to research, indexing,
summarization, speculative analysis, and test triage; their claims are verified
or escalated before admission.

## Interactive commands

```text
/new       start a new branch head
/usage     current cumulative usage
/quota     provider resource view
/model     active provider/model/checkpoint
/help      list local commands
/exit      close the frontend
```

## Tools and extensions

Cambium keeps typed built-ins for repository inspection and mutation, the
existing shell boundary for general commands, and `run_python` for short
`python -I -c` snippets. It is not a sandbox. Trusted Python packages can add
typed tools through `cambium.tools` and quota/status adapters through
`cambium.quota_adapters` entry points.
''', encoding="utf-8")

readme = ROOT / "README.md"
if readme.exists():
    text = readme.read_text(encoding="utf-8")
    marker = "## Operator runtime"
    if marker not in text:
        text += '''

## Operator runtime

```sh
uv run cambium-quota init
uv run cambium tui --repo .
```

See [`docs/operator-runtime.md`](docs/operator-runtime.md) for persistent
sessions, provider-window configuration, free-model safety, monitoring, and
extension points.
'''
        readme.write_text(text, encoding="utf-8")

index = ROOT / "docs/README.md"
if index.exists():
    text = index.read_text(encoding="utf-8")
    if "operator-runtime.md" not in text:
        text += "\n- [`operator-runtime.md`](operator-runtime.md) — runtime startup, quotas, commands, and extensions.\n"
        index.write_text(text, encoding="utf-8")

(ROOT / "tests/scenarios/test_provider_resources_productization.py").write_text(r'''from __future__ import annotations

from cambium.provider_resources import example_snapshot


def test_example_portfolio_keeps_weak_free_lanes_scoped():
    profiles = example_snapshot().by_provider()
    assert profiles["codex"].weak is False
    assert profiles["zai"].weak is False
    for name in ("openrouter-free", "opencode-zen"):
        assert profiles[name].weak is True
        assert profiles[name].verification_required is True
        assert "research" in profiles[name].allowed_task_classes
        assert "code" not in profiles[name].allowed_task_classes
''', encoding="utf-8")

for path in (ROOT / "src").rglob("*.py"):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
print("runtime productization applied")
