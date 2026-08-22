#!/usr/bin/env python3
"""Focused source-safe corrections after the production-harness generator."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _function_segment(
    text: str, name: str, class_name: str | None = None
) -> tuple[int, int, str]:
    tree = ast.parse(text)
    scope = tree.body
    if class_name is not None:
        cls = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            None,
        )
        if cls is None:
            raise RuntimeError(f"class {class_name} not found")
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
    if node is None or node.end_lineno is None:
        raise RuntimeError(f"function {name} not found")
    lines = text.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    return start, end, "".join(lines[start:end])


def _replace_in_function(
    path: Path,
    name: str,
    old: str,
    new: str,
    *,
    class_name: str | None = None,
) -> None:
    text = _read(path)
    start, end, segment = _function_segment(text, name, class_name)
    if new in segment:
        return
    count = segment.count(old)
    if count != 1:
        raise RuntimeError(f"{path.name}:{name}: expected one match, found {count}")
    replacement = segment.replace(old, new, 1)
    lines = text.splitlines(keepends=True)
    lines[start:end] = [replacement]
    _write(path, "".join(lines))


def _replace_exact(
    path: Path,
    old: str,
    new: str,
    *,
    expected: int = 1,
) -> None:
    text = _read(path)
    if new in text and old not in text:
        return
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path.name}: expected {expected} matches, found {count}")
    _write(path, text.replace(old, new))


def _move_quota_initialization() -> None:
    path = ROOT / "src" / "cambium" / "diffundo.py"
    text = _read(path)
    block = '''        self._provider_lease: ProviderLease | None = None
        self._quota_ledger = (
            QuotaLedger() if any(provider.quota_windows for provider in self._providers) else None
        )
'''
    count = text.count(block)
    if count != 1:
        raise RuntimeError(f"Diffundo quota-init block: expected one match, found {count}")
    text = text.replace(block, "", 1)
    tree = ast.parse(text)
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo"
    )
    init = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assignment = None
    for node in ast.walk(init):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
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
        raise RuntimeError("Diffundo self._providers assignment not found")
    lines = text.splitlines(keepends=True)
    lines.insert(assignment.end_lineno, block)
    _write(path, "".join(lines))


def _fix_model_candidate_policy() -> None:
    path = ROOT / "src" / "cambium" / "diffundo.py"
    _replace_in_function(
        path,
        "_candidates",
        '''    exact = [provider for provider in candidates if provider.model == requested_model]
    if exact or not any(provider.allow_model_substitution for provider in candidates):
        candidates = exact
''',
        '''    exact = [provider for provider in candidates if provider.model == requested_model]
    substitutes = [
        provider
        for provider in candidates
        if provider.model != requested_model and provider.allow_model_substitution
    ]
    candidates = [*exact, *substitutes]
''',
        class_name="Diffundo",
    )


def _fix_native_tool_prompt_wiring() -> None:
    path = ROOT / "src" / "cambium" / "worker.py"

    # The generator previously replaced the first generic prompt return in the
    # module, which belongs to _build_forked_prompt and has no local `tools`.
    _replace_in_function(
        path,
        "_build_forked_prompt",
        '    return {"messages": messages, "tools": tools}\n',
        '    return {"messages": messages}\n',
    )
    _replace_in_function(
        path,
        "_build_agent_prompt",
        '    return {"messages": messages}\n',
        '    return {"messages": messages, "tools": tools}\n',
    )

    text = _read(path)
    _start, _end, fork_segment = _function_segment(text, "_fork_prompt")
    if "    tools: list[dict[str, Any]] | None = None,\n" not in fork_segment:
        old = '''    continuation: list[dict[str, Any]],
) -> dict[str, Any]:
'''
        new = '''    continuation: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
'''
        _replace_in_function(path, "_fork_prompt", old, new)
    _replace_in_function(
        path,
        "_fork_prompt",
        '    return {"messages": messages}\n',
        '''    prompt: dict[str, Any] = {"messages": messages}
    if tools is not None:
        prompt["tools"] = tools
    return prompt
''',
    )

    text = _read(path)
    old_call = "prompt = _fork_prompt(base_messages, context_continuation)"
    new_call = "prompt = _fork_prompt(base_messages, context_continuation, tools)"
    if new_call not in text:
        count = text.count(old_call)
        if count != 1:
            raise RuntimeError(f"worker fork prompt call: expected one match, found {count}")
        _write(path, text.replace(old_call, new_call, 1))


def _fix_worker_optional_metadata() -> None:
    path = ROOT / "src" / "cambium" / "worker.py"
    _replace_in_function(
        path,
        "_native_tool_action",
        "    calls = result.tool_calls\n",
        '    calls = getattr(result, "tool_calls", None)\n',
    )
    _replace_in_function(
        path,
        "_success_usage_event",
        '''    if result.quota_windows is not None:
        event["quota_windows"] = [dict(item) for item in result.quota_windows]
''',
        '''    quota_windows = getattr(result, "quota_windows", None)
    if quota_windows is not None:
        event["quota_windows"] = [dict(item) for item in quota_windows]
''',
    )


def _ensure_oauth_regex_import() -> None:
    path = ROOT / "src" / "cambium" / "oauth.py"
    text = _read(path)
    if "\nimport re\n" in text:
        return
    marker = "import os\n"
    if text.count(marker) != 1:
        raise RuntimeError("oauth import marker mismatch")
    _write(path, text.replace(marker, marker + "import re\n", 1))


def _fix_generated_line_lengths() -> None:
    diffundo = ROOT / "src" / "cambium" / "diffundo.py"
    _replace_exact(
        diffundo,
        '                f"untrusted response model {reported!r} does not match configured {provider.model!r}",\n',
        '''                (
                    f"untrusted response model {reported!r} does not match "
                    f"configured {provider.model!r}"
                ),
''',
        expected=2,
    )
    _replace_exact(
        diffundo,
        '''                sum(len(str(message.get("content", "")).encode("utf-8")) for message in messages) // 4
''',
        '''                sum(
                    len(str(message.get("content", "")).encode("utf-8"))
                    for message in messages
                )
                // 4
''',
    )

    scheduler = ROOT / "src" / "cambium" / "provider_scheduler.py"
    _replace_exact(
        scheduler,
        '                    "used_tokens=excluded.used_tokens, allowance_requests=excluded.allowance_requests, "\n',
        '''                    "used_tokens=excluded.used_tokens, "
                    "allowance_requests=excluded.allowance_requests, "
''',
    )
    _replace_exact(
        scheduler,
        '                    "used_requests=excluded.used_requests, reserve_fraction=excluded.reserve_fraction, "\n',
        '''                    "used_requests=excluded.used_requests, "
                    "reserve_fraction=excluded.reserve_fraction, "
''',
    )
    _replace_exact(
        scheduler,
        '''        used_requests = 0 if remaining_requests is None else max(0, allowance_requests - remaining_requests)
''',
        '''        used_requests = (
            0
            if remaining_requests is None
            else max(0, allowance_requests - remaining_requests)
        )
''',
    )
    _replace_exact(
        scheduler,
        '''                    windows = () if self._ledger is None else await asyncio.to_thread(self._ledger.snapshots)
''',
        '''                    windows = (
                        ()
                        if self._ledger is None
                        else await asyncio.to_thread(self._ledger.snapshots)
                    )
''',
    )

    render_tests = ROOT / "tests" / "scenarios" / "test_render_stream.py"
    text = _read(render_tests)
    signal_count = text.count("\nimport signal\n")
    if signal_count == 1:
        text = text.replace("\nimport signal\n", "\n", 1)
    elif signal_count != 0:
        raise RuntimeError(f"test_render_stream signal imports: found {signal_count}")
    if "\nimport signal\n" not in text[:1000]:
        marker = "import shutil\n"
        if text.count(marker) != 1:
            raise RuntimeError("test_render_stream import marker mismatch")
        text = text.replace(marker, marker + "import signal\n", 1)
    long_event = '''        on_event({"kind": "tool_event", "payload": {"tool": "run_shell", "cmd": "df -h", "ok": True, "duration_ms": 5}})
'''
    if long_event in text:
        text = text.replace(
            long_event,
            '''        on_event(
            {
                "kind": "tool_event",
                "payload": {
                    "tool": "run_shell",
                    "cmd": "df -h",
                    "ok": True,
                    "duration_ms": 5,
                },
            }
        )
''',
            1,
        )
    _write(render_tests, text)

    repl_tests = ROOT / "tests" / "scenarios" / "test_repl_usage.py"
    text = _read(repl_tests)
    long_patch = '''    monkeypatch.setattr(repl.render, "render_event_line", lambda _record, stream=None: "usage event")
'''
    if long_patch in text:
        text = text.replace(
            long_patch,
            '''    monkeypatch.setattr(
        repl.render,
        "render_event_line",
        lambda _record, stream=None: "usage event",
    )
''',
            1,
        )
        _write(repl_tests, text)


def _update_legacy_tests_for_explicit_semantics() -> None:
    provider_config = ROOT / "tests" / "scenarios" / "test_provider_config.py"
    _replace_exact(
        provider_config,
        '''            price_per_1m_in=0.25,
            price_per_1m_out=0.25,
''',
        '''            price_per_1m_in=0.25,
            price_per_1m_out=0.25,
            price_per_1m_cached_in=0.25,
            pricing_known=True,
''',
    )

    diffundo_tests = ROOT / "tests" / "scenarios" / "test_diffundo.py"
    _replace_exact(
        diffundo_tests,
        '''    fast = FakeServer([(200, _ok_payload("fast"), 0.0)])
    fast2 = FakeServer([(200, _ok_payload("fast m2"), 0.0)])
    strong = FakeServer([(200, _ok_payload("strong"), 0.0)])
    balanced = FakeServer([(200, _ok_payload("balanced"), 0.0)])
''',
        '''    fast = FakeServer([(200, _ok_payload("fast", model="m1"), 0.0)])
    fast2 = FakeServer([(200, _ok_payload("fast m2", model="m2"), 0.0)])
    strong = FakeServer([(200, _ok_payload("strong", model="m-s"), 0.0)])
    balanced = FakeServer([(200, _ok_payload("balanced", model="m-b"), 0.0)])
''',
    )
    _replace_exact(
        diffundo_tests,
        '''            _config("p_other", sibling, "K_OTHER", model="m-other"),
''',
        '''            _config(
                "p_other",
                sibling,
                "K_OTHER",
                model="m-other",
                allow_model_substitution=True,
            ),
''',
        expected=2,
    )
    _replace_exact(
        diffundo_tests,
        '''    """A pinned model's matching provider failing mid-call cascades to a same
    tier sibling that declares a different model (cascade fix), instead of
    surfacing AllProvidersFailed."""
''',
        '''    """An explicitly substitution-enabled sibling may serve after the exact
    model lane fails; substitution is never an implicit fallback."""
''',
    )
    _replace_exact(
        diffundo_tests,
        '''    """A pinned model whose only matching provider fails into COOLDOWN cascades
    to the eligible same-tier sibling on the next selection."""
''',
        '''    """An explicitly substitution-enabled sibling remains eligible while the
    exact model lane is in cooldown."""
''',
    )


def main() -> None:
    _move_quota_initialization()
    _fix_model_candidate_policy()
    _fix_native_tool_prompt_wiring()
    _fix_worker_optional_metadata()
    _ensure_oauth_regex_import()
    _fix_generated_line_lengths()
    _update_legacy_tests_for_explicit_semantics()


if __name__ == "__main__":
    main()
