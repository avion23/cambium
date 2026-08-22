#!/usr/bin/env python3
"""Apply provider continuity, quota scheduling, OAuth, and tool-runtime upgrades."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def add_import(path: str, statement: str) -> None:
    text = read(path)
    if statement in text:
        return
    marker = "from __future__ import annotations\n"
    if marker not in text:
        raise RuntimeError(f"{path}: future-import marker not found")
    write(path, text.replace(marker, marker + "\n" + statement + "\n", 1))


def function_node(path: str, name: str, class_name: str | None = None):
    text = read(path)
    tree = ast.parse(text)
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
    indent = " " * node.col_offset
    inner = indent + "    "
    rendered_body = "".join(
        inner + line + "\n" if line else "\n" for line in body.splitlines()
    )
    wrapper = header + rendered_body + "\n" + renamed_header
    lines[start:body_start] = [wrapper]
    write(path, "".join(lines))


def insert_into_init(path: str, class_name: str, code: str, marker: str) -> None:
    text = read(path)
    if marker in text:
        return
    node = function_node(path, "__init__", class_name)
    lines = text.splitlines(keepends=True)
    insert_at = node.body[0].end_lineno or node.body[0].lineno
    if not (
        isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        insert_at = node.body[0].lineno - 1
    indent = " " * (node.col_offset + 4)
    rendered = "".join(indent + line + "\n" for line in code.splitlines())
    lines.insert(insert_at, rendered)
    write(path, "".join(lines))


def patch_provider_config() -> None:
    path = "src/cambium/provider_config.py"
    add_import(path, "from .provider_scheduler import BillingMode, QuotaWindowSpec")
    text = read(path)
    fields_marker = '        "reasoning_effort",\n'
    if '        "max_concurrency",\n' not in text:
        if text.count(fields_marker) != 1:
            raise RuntimeError("provider fields marker mismatch")
        text = text.replace(
            fields_marker,
            fields_marker
            + '        "max_concurrency",\n'
            + '        "billing_mode",\n'
            + '        "quota_windows",\n'
            + '        "price_per_1m_in",\n'
            + '        "price_per_1m_cached_in",\n'
            + '        "price_per_1m_out",\n'
            + '        "pricing_known",\n'
            + '        "throughput_hint_tps",\n'
            + '        "quality_weight",\n'
            + '        "supports_native_tools",\n'
            + '        "supports_python_tool",\n'
            + '        "allow_model_substitution",\n',
            1,
        )
    defaults_marker = '    "context_window": 0,\n'
    if '    "max_concurrency": 1,\n' not in text:
        if text.count(defaults_marker) != 1:
            raise RuntimeError("provider defaults marker mismatch")
        text = text.replace(
            defaults_marker,
            defaults_marker
            + '    "max_concurrency": 1,\n'
            + '    "billing_mode": "metered",\n'
            + '    "quota_windows": (),\n'
            + '    "price_per_1m_in": 0.0,\n'
            + '    "price_per_1m_cached_in": 0.0,\n'
            + '    "price_per_1m_out": 0.0,\n'
            + '    "pricing_known": False,\n'
            + '    "throughput_hint_tps": 0.0,\n'
            + '    "quality_weight": 1.0,\n'
            + '    "supports_native_tools": True,\n'
            + '    "supports_python_tool": True,\n'
            + '    "allow_model_substitution": False,\n',
            1,
        )
    typed_marker = "    reasoning_effort: str | None\n"
    if "    max_concurrency: int\n" not in text:
        if text.count(typed_marker) != 1:
            raise RuntimeError("provider TypedDict marker mismatch")
        text = text.replace(
            typed_marker,
            typed_marker
            + "    max_concurrency: int\n"
            + "    billing_mode: BillingMode\n"
            + "    quota_windows: tuple[QuotaWindowSpec, ...]\n"
            + "    price_per_1m_in: float\n"
            + "    price_per_1m_cached_in: float\n"
            + "    price_per_1m_out: float\n"
            + "    pricing_known: bool\n"
            + "    throughput_hint_tps: float\n"
            + "    quality_weight: float\n"
            + "    supports_native_tools: bool\n"
            + "    supports_python_tool: bool\n"
            + "    allow_model_substitution: bool\n",
            1,
        )
    helper_marker = "\ndef _validate_provider_mapping(raw: object, index: int) -> _ProviderMapping:\n"
    if "def _parse_quota_windows(" not in text:
        helpers = '''
def _parse_billing_mode(value: object, location: str) -> BillingMode:
    if not isinstance(value, str):
        raise _error(location, "must be a billing-mode string")
    try:
        return BillingMode(value)
    except ValueError as exc:
        choices = ", ".join(mode.value for mode in BillingMode)
        raise _error(location, f"invalid billing mode {value!r}; expected {choices}") from exc


def _parse_quota_windows(value: object, location: str) -> tuple[QuotaWindowSpec, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise _error(location, "must be a list")
    windows: list[QuotaWindowSpec] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _error(f"{location}[{index}]", "must be an object")
        try:
            window = QuotaWindowSpec.from_mapping(item)
        except ValueError as exc:
            raise _error(f"{location}[{index}]", str(exc)) from exc
        if window.name in names:
            raise _error(f"{location}[{index}].name", "must be unique per provider")
        names.add(window.name)
        windows.append(window)
    return tuple(windows)


def _require_bool(value: object, location: str) -> bool:
    if type(value) is not bool:
        raise _error(location, "must be a boolean")
    return value

'''
        if helper_marker not in text:
            raise RuntimeError("provider validation function marker mismatch")
        text = text.replace(helper_marker, "\n" + helpers + helper_marker.lstrip("\n"), 1)
    validation_marker = "    # Optional Responses-API reasoning effort"
    if "    max_concurrency = _require_integer(" not in text:
        insert = '''    max_concurrency = _require_integer(
        values["max_concurrency"], f"{location}.max_concurrency"
    )
    if max_concurrency <= 0:
        raise _error(f"{location}.max_concurrency", "must be greater than 0")
    billing_mode = _parse_billing_mode(
        values["billing_mode"], f"{location}.billing_mode"
    )
    quota_windows = _parse_quota_windows(
        raw.get("quota_windows", []), f"{location}.quota_windows"
    )
    legacy_price = price
    price_per_1m_in = _require_number(
        raw.get("price_per_1m_in", legacy_price), f"{location}.price_per_1m_in"
    )
    price_per_1m_cached_in = _require_number(
        raw.get("price_per_1m_cached_in", price_per_1m_in),
        f"{location}.price_per_1m_cached_in",
    )
    price_per_1m_out = _require_number(
        raw.get("price_per_1m_out", legacy_price), f"{location}.price_per_1m_out"
    )
    for key, amount in (
        ("price_per_1m_in", price_per_1m_in),
        ("price_per_1m_cached_in", price_per_1m_cached_in),
        ("price_per_1m_out", price_per_1m_out),
    ):
        if amount < 0:
            raise _error(f"{location}.{key}", "must not be negative")
    pricing_known = _require_bool(
        raw.get(
            "pricing_known",
            "price" in raw
            or any(
                key in raw
                for key in (
                    "price_per_1m_in",
                    "price_per_1m_cached_in",
                    "price_per_1m_out",
                )
            ),
        ),
        f"{location}.pricing_known",
    )
    throughput_hint_tps = _require_number(
        values["throughput_hint_tps"], f"{location}.throughput_hint_tps"
    )
    quality_weight = _require_number(
        values["quality_weight"], f"{location}.quality_weight"
    )
    if throughput_hint_tps < 0 or quality_weight < 0:
        raise _error(location, "throughput_hint_tps and quality_weight must be non-negative")
    supports_native_tools = _require_bool(
        values["supports_native_tools"], f"{location}.supports_native_tools"
    )
    supports_python_tool = _require_bool(
        values["supports_python_tool"], f"{location}.supports_python_tool"
    )
    allow_model_substitution = _require_bool(
        values["allow_model_substitution"], f"{location}.allow_model_substitution"
    )

'''
        index = text.find(validation_marker)
        if index < 0:
            raise RuntimeError("provider validation insertion marker mismatch")
        text = text[:index] + insert + text[index:]
    return_marker = '        "reasoning_effort": reasoning_effort,\n'
    if '        "max_concurrency": max_concurrency,\n' not in text:
        if text.count(return_marker) != 1:
            raise RuntimeError("provider return mapping marker mismatch")
        text = text.replace(
            return_marker,
            return_marker
            + '        "max_concurrency": max_concurrency,\n'
            + '        "billing_mode": billing_mode,\n'
            + '        "quota_windows": quota_windows,\n'
            + '        "price_per_1m_in": price_per_1m_in,\n'
            + '        "price_per_1m_cached_in": price_per_1m_cached_in,\n'
            + '        "price_per_1m_out": price_per_1m_out,\n'
            + '        "pricing_known": pricing_known,\n'
            + '        "throughput_hint_tps": throughput_hint_tps,\n'
            + '        "quality_weight": quality_weight,\n'
            + '        "supports_native_tools": supports_native_tools,\n'
            + '        "supports_python_tool": supports_python_tool,\n'
            + '        "allow_model_substitution": allow_model_substitution,\n',
            1,
        )
    config_marker = '        "reasoning_effort": values["reasoning_effort"],\n'
    if '        "max_concurrency": values["max_concurrency"],\n' not in text:
        if text.count(config_marker) != 1:
            raise RuntimeError("provider constructor marker mismatch")
        text = text.replace(
            config_marker,
            config_marker
            + '        "max_concurrency": values["max_concurrency"],\n'
            + '        "billing_mode": values["billing_mode"],\n'
            + '        "quota_windows": values["quota_windows"],\n'
            + '        "price_per_1m_cached_in": values["price_per_1m_cached_in"],\n'
            + '        "pricing_known": values["pricing_known"],\n'
            + '        "throughput_hint_tps": values["throughput_hint_tps"],\n'
            + '        "quality_weight": values["quality_weight"],\n'
            + '        "supports_native_tools": values["supports_native_tools"],\n'
            + '        "supports_python_tool": values["supports_python_tool"],\n'
            + '        "allow_model_substitution": values["allow_model_substitution"],\n',
            1,
        )
    if 'config_values["price_per_1m_in"] = values["price_per_1m_in"]' not in text:
        old = '''    price = values["price"]
    provider_fields = {field.name for field in fields(ProviderConfigType)}
    if "price" in provider_fields:
        config_values["price"] = price
    elif {"price_per_1m_in", "price_per_1m_out"} <= provider_fields:
        config_values["price_per_1m_in"] = price
        config_values["price_per_1m_out"] = price
    else:
        raise RuntimeError("ProviderConfig has no supported price field")
'''
        new = '''    price = values["price"]
    provider_fields = {field.name for field in fields(ProviderConfigType)}
    if {"price_per_1m_in", "price_per_1m_out"} <= provider_fields:
        config_values["price_per_1m_in"] = values["price_per_1m_in"]
        config_values["price_per_1m_out"] = values["price_per_1m_out"]
    elif "price" in provider_fields:
        config_values["price"] = price
    else:
        raise RuntimeError("ProviderConfig has no supported price field")
'''
        if old not in text:
            raise RuntimeError("legacy provider price block mismatch")
        text = text.replace(old, new, 1)
    write(path, text)


def patch_diffundo() -> None:
    path = "src/cambium/diffundo.py"
    add_import(
        path,
        "from .provider_scheduler import (\n"
        "    BillingMode, ProviderLease, QuotaLedger, QuotaWindowSpec, quota_snapshot_json\n"
        ")",
    )
    text = read(path)
    field_marker = "    reasoning_effort: str | None = None\n"
    if "    max_concurrency: int = 1\n" not in text:
        if text.count(field_marker) != 1:
            raise RuntimeError("Diffundo ProviderConfig field marker mismatch")
        text = text.replace(
            field_marker,
            field_marker
            + "    max_concurrency: int = 1\n"
            + "    billing_mode: BillingMode = BillingMode.METERED\n"
            + "    quota_windows: tuple[QuotaWindowSpec, ...] = ()\n"
            + "    price_per_1m_cached_in: float = 0.0\n"
            + "    pricing_known: bool = False\n"
            + "    throughput_hint_tps: float = 0.0\n"
            + "    quality_weight: float = 1.0\n"
            + "    supports_native_tools: bool = True\n"
            + "    supports_python_tool: bool = True\n"
            + "    allow_model_substitution: bool = False\n",
            1,
        )
    result_marker = "    provider_cache_hit: bool | None = None\n"
    if "    quota_windows: tuple[dict[str, Any], ...] | None = None\n" not in text:
        if text.count(result_marker) != 1:
            raise RuntimeError("CallResult field marker mismatch")
        text = text.replace(
            result_marker,
            result_marker + "    quota_windows: tuple[dict[str, Any], ...] | None = None\n",
            1,
        )
    write(path, text)
    insert_into_init(
        path,
        "Diffundo",
        "self._provider_lease: ProviderLease | None = None\n"
        "self._quota_ledger = (\n"
        "    QuotaLedger() if any(provider.quota_windows for provider in self._providers) else None\n"
        ")",
        "self._provider_lease: ProviderLease | None",
    )
    node = function_node(path, "_candidates", "Diffundo")
    args = [arg.arg for arg in [*node.args.posonlyargs, *node.args.args]]
    model_name = "model" if "model" in args else None
    forwarded = _forward_arguments(node)
    wrapper_body = f'''candidates = list(self._candidates_unleased({forwarded}))
lease = self._provider_lease
if lease is not None:
    candidates = [
        provider
        for provider in candidates
        if provider.name == lease.provider and provider.model == lease.model
    ]
requested_model = {model_name or "None"}
if isinstance(requested_model, str) and requested_model:
    exact = [provider for provider in candidates if provider.model == requested_model]
    if exact or not any(provider.allow_model_substitution for provider in candidates):
        candidates = exact
return candidates'''
    wrap_function(
        path,
        "_candidates",
        "_candidates_unleased",
        wrapper_body,
        class_name="Diffundo",
    )
    text = read(path)
    if "    def bind_provider(" not in text:
        index = text.find("    def _candidates(")
        if index < 0:
            raise RuntimeError("Diffundo candidates wrapper marker missing")
        methods = '''    @property
    def provider_lease(self) -> ProviderLease | None:
        """Current strict semantic-branch lease, if the first call has succeeded."""

        return self._provider_lease

    def bind_provider(self, provider: str, model: str, *, root_task_id: str = "task") -> None:
        """Pin every later call on this router to one provider/model branch."""

        if not provider or not model:
            raise ValueError("provider lease requires provider and model")
        existing = self._provider_lease
        if existing is not None:
            if existing.provider != provider or existing.model != model:
                raise RuntimeError(
                    "provider continuity violation: attempted to move a live semantic branch"
                )
            return
        configured = next(
            (
                item
                for item in self._providers
                if item.name == provider and item.model == model and item.enabled
            ),
            None,
        )
        if configured is None:
            raise ValueError("provider lease does not match an enabled configured lane")
        self._provider_lease = ProviderLease(provider, model, root_task_id)

    def clear_provider_lease(self) -> None:
        """Clear task-local state when a warm worker is rebound to another task."""

        self._provider_lease = None

'''
        text = text[:index] + methods + text[index:]
        write(path, text)
    source = read(path)
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo")
    attempt = None
    for candidate in cls.body:
        if not isinstance(candidate, ast.AsyncFunctionDef):
            continue
        names = {arg.arg for arg in [*candidate.args.posonlyargs, *candidate.args.args]}
        segment = ast.get_source_segment(source, candidate) or ""
        if "provider" in names and "prompt" in names and "CallResult" in segment:
            if candidate.name not in {"call", "_candidates", "_candidates_unleased"}:
                attempt = candidate
                break
    if attempt is not None and "_quota_wrapped_attempt" not in read(path):
        forwarded = _forward_arguments(attempt)
        body = f'''policy = provider
ledger = self._quota_ledger
reservation = None
estimated_tokens = 0
if ledger is not None and policy.quota_windows:
    messages = prompt.get("messages", []) if isinstance(prompt, dict) else []
    estimated_tokens = max(
        1,
        sum(len(str(message.get("content", "")).encode("utf-8")) for message in messages) // 4
        + 4096,
    )
    reservation = await asyncio.to_thread(
        ledger.reserve, policy.name, policy.quota_windows, estimated_tokens
    )
    if reservation is None:
        raise ProviderError(
            policy.name,
            ProviderOutcome.QUOTA,
            "configured subscription quota window is exhausted",
        )
try:
    result = await self._quota_wrapped_attempt({forwarded})
except BaseException:
    if reservation is not None and ledger is not None:
        await asyncio.to_thread(ledger.reconcile, reservation, policy.quota_windows, 0)
    raise
if reservation is not None and ledger is not None:
    usage = result.usage if isinstance(result.usage, dict) else {{}}
    total = usage.get("total_tokens")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total < 0:
        total = estimated_tokens
    await asyncio.to_thread(
        ledger.reconcile, reservation, policy.quota_windows, int(total)
    )
    snapshots = await asyncio.to_thread(ledger.snapshots, policy.name)
    result = replace(
        result,
        quota_windows=tuple(quota_snapshot_json(snapshot) for snapshot in snapshots),
    )
return result'''
        wrap_function(
            path,
            attempt.name,
            "_quota_wrapped_attempt",
            body,
            class_name="Diffundo",
        )


def patch_worker() -> None:
    path = "src/cambium/worker.py"
    text = read(path)
    if "def _native_tool_action(" not in text:
        marker = "\ndef _canonical_action_message("
        helper = '''
def _native_tool_action(result: CallResult) -> dict[str, Any] | None:
    """Translate exactly one provider-native function call to Cambium's action ADT."""

    calls = result.tool_calls
    if not calls:
        return None
    if len(calls) != 1:
        raise ValueError("provider returned more than one tool call for a sequential turn")
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    if not isinstance(function, dict):
        raise ValueError("provider native tool call has no function object")
    name = function.get("name")
    arguments = function.get("arguments", {})
    if not isinstance(name, str) or not name:
        raise ValueError("provider native tool call has no function name")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("provider native tool arguments are invalid JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("provider native tool arguments must be an object")
    return {"type": "tool_call", "name": name, "arguments": arguments}


def _bind_router_provider(router: Any, result: CallResult, task_id: str) -> None:
    """Bind provider continuity when the concrete router exposes the lease port."""

    binder = getattr(router, "bind_provider", None)
    if callable(binder):
        binder(result.provider, result.model, root_task_id=task_id)

'''
        if marker not in text:
            raise RuntimeError("worker canonical action marker mismatch")
        text = text.replace(marker, "\n" + helper + marker.lstrip("\n"), 1)
    action_marker = "action = _parse_agent_action(result.content)"
    if "_native_tool_action(result) or _parse_agent_action" not in text:
        if action_marker not in text:
            raise RuntimeError("worker action parse marker missing")
        text = text.replace(
            action_marker,
            "action = _native_tool_action(result) or _parse_agent_action(result.content)",
        )
    invalid_marker = "            invalid_usage_fields = _invalid_usage_fields(result.usage)\n"
    if "_bind_router_provider(router, result, config.task_id)" not in text:
        if invalid_marker not in text:
            raise RuntimeError("worker result usage marker mismatch")
        text = text.replace(
            invalid_marker,
            "            _bind_router_provider(router, result, config.task_id)\n"
            + invalid_marker,
            1,
        )
    summary_marker = "            invalid_usage_fields = _invalid_usage_fields(summary_result.usage)\n"
    if "_bind_router_provider(router, summary_result, config.task_id)" not in text:
        if summary_marker in text:
            text = text.replace(
                summary_marker,
                "            _bind_router_provider(router, summary_result, config.task_id)\n"
                + summary_marker,
                1,
            )
    prompt_return = '    return {"messages": messages}\n'
    if '    return {"messages": messages, "tools": tools}\n' not in text:
        if prompt_return not in text:
            raise RuntimeError("worker prompt return marker mismatch")
        text = text.replace(prompt_return, '    return {"messages": messages, "tools": tools}\n', 1)
    success_marker = "    if result.provider_cache_hit is not None:\n        event[\"provider_cache_hit\"] = result.provider_cache_hit\n"
    if "result.quota_windows" not in text:
        if success_marker not in text:
            raise RuntimeError("worker success usage marker mismatch")
        text = text.replace(
            success_marker,
            success_marker
            + "    if result.quota_windows is not None:\n"
            + "        event[\"quota_windows\"] = [dict(item) for item in result.quota_windows]\n",
            1,
        )
    write(path, text)


def patch_supervisor() -> None:
    path = "src/cambium/supervisor.py"
    text = read(path)
    forward_marker = '        "fork_of",\n'
    if '        "quota_windows",\n' not in text:
        if forward_marker not in text:
            raise RuntimeError("supervisor usage forward marker mismatch")
        text = text.replace(forward_marker, forward_marker + '        "quota_windows",\n', 1)
    validation_marker = "    usage = msg.get(\"usage\")\n"
    if "quota_windows = msg.get(\"quota_windows\")" not in text:
        validation = '''    quota_windows = msg.get("quota_windows")
    if "quota_windows" in msg:
        allowed_quota_fields = {
            "provider", "name", "reset_at", "allowance_tokens", "used_tokens",
            "allowance_requests", "used_requests", "reserve_fraction",
            "remaining_tokens", "remaining_requests",
        }
        if (
            not isinstance(quota_windows, list)
            or len(quota_windows) > 16
            or any(
                not isinstance(item, dict)
                or set(item) - allowed_quota_fields
                or not isinstance(item.get("provider"), str)
                or not isinstance(item.get("name"), str)
                for item in quota_windows
            )
        ):
            invalid.append("quota_windows")

'''
        if validation_marker not in text:
            raise RuntimeError("supervisor usage validation marker mismatch")
        text = text.replace(validation_marker, validation + validation_marker, 1)
    semantic_marker = '''        if semantic_reuse:
            child_spec["summary_trunk_ref"] = epoch["checkpoint_ref"]
            return
'''
    if "Cold semantic children" not in text:
        replacement = '''        if semantic_reuse:
            # Cold semantic children choose an independent provider lane. Only
            # exact cache-compatible forks inherit the parent provider/model.
            child_spec.pop("assigned_provider", None)
            fanout = child_spec.get("fanout_config")
            if isinstance(fanout, dict):
                fanout.pop("provider", None)
                fanout.pop("assigned_provider", None)
            child_spec["summary_trunk_ref"] = epoch["checkpoint_ref"]
            return
'''
        if semantic_marker not in text:
            raise RuntimeError("supervisor semantic reuse marker mismatch")
        text = text.replace(semantic_marker, replacement, 1)
    for old, new in (
        ("max(1, provider.rpm)", "max(1, provider.max_concurrency)"),
        ("max(1, int(provider.rpm))", "max(1, int(provider.max_concurrency))"),
        ("limit=provider.rpm", "limit=provider.max_concurrency"),
        ("capacity=provider.rpm", "capacity=provider.max_concurrency"),
    ):
        text = text.replace(old, new)
    write(path, text)
    routing_path = "src/cambium/routing.py"
    routing = read(routing_path)
    for old, new in (
        ("max(1, provider.rpm)", "max(1, provider.max_concurrency)"),
        ("max(1, int(provider.rpm))", "max(1, int(provider.max_concurrency))"),
        ("limit=provider.rpm", "limit=provider.max_concurrency"),
        ("capacity=provider.rpm", "capacity=provider.max_concurrency"),
    ):
        routing = routing.replace(old, new)
    write(routing_path, routing)


def patch_tools() -> None:
    schemas_path = "src/cambium/schemas.py"
    schemas = read(schemas_path)
    if "_RUN_PYTHON_SCHEMA" not in schemas:
        schemas += '''

_RUN_PYTHON_SCHEMA_DIRECT = {
    "name": "run_python",
    "description": (
        "Run a short trusted Python 3 snippet in the worktree for structured "
        "data transformation, inspection, or calculations. Prefer read/search/edit "
        "tools for ordinary repository operations. The process is isolated from "
        "site packages and credential environment, but Cambium is not an OS sandbox."
    ),
    "parameters": {
        "type": "object",
        "properties": {"code": {"type": "string", "maxLength": 32768}},
        "required": ["code"],
        "additionalProperties": False,
    },
}
_RUN_PYTHON_SCHEMA = (
    {"type": "function", "function": _RUN_PYTHON_SCHEMA_DIRECT}
    if TOOL_SCHEMAS and isinstance(TOOL_SCHEMAS[0], dict) and "function" in TOOL_SCHEMAS[0]
    else _RUN_PYTHON_SCHEMA_DIRECT
)
if not any(
    isinstance(item, dict)
    and (
        item.get("name") == "run_python"
        or (
            isinstance(item.get("function"), dict)
            and item["function"].get("name") == "run_python"
        )
    )
    for item in TOOL_SCHEMAS
):
    TOOL_SCHEMAS = type(TOOL_SCHEMAS)([*TOOL_SCHEMAS, _RUN_PYTHON_SCHEMA])
'''
        write(schemas_path, schemas)
    tools_path = "src/cambium/tools.py"
    add_import(tools_path, "import sys")
    node = function_node(tools_path, "run_tool")
    arg_names = [arg.arg for arg in [*node.args.posonlyargs, *node.args.args]]
    if arg_names and arg_names[0] in {"self", "cls"}:
        arg_names = arg_names[1:]
    if len(arg_names) < 3:
        raise RuntimeError("run_tool signature has fewer than three positional arguments")
    name_arg, arguments_arg, context_arg = arg_names[:3]
    forwarded = _forward_arguments(node)
    body = f'''if {name_arg} == "run_python":
    payload = {arguments_arg}
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(code, str) or not code.strip():
        code = "raise SystemExit('run_python requires non-empty code')"
    elif len(code.encode("utf-8")) > 32768:
        code = "raise SystemExit('run_python code exceeds 32768 bytes')"
    return _run_tool_without_python(
        "run_shell",
        {{"cmd": [sys.executable, "-I", "-S", "-c", code]}},
        {context_arg},
    )
return _run_tool_without_python({forwarded})'''
    wrap_function(tools_path, "run_tool", "_run_tool_without_python", body)


def patch_oauth() -> None:
    path = "src/cambium/oauth.py"
    add_import(path, "import fcntl")
    add_import(path, "from contextlib import contextmanager")
    text = read(path)
    if "def _refresh_lock(" not in text:
        marker = "\nclass TokenManager"
        helper = '''
@contextmanager
def _refresh_lock(provider: str):
    """Cross-process single-flight lock for refresh-token rotation."""

    root = Path.home() / ".config" / "cambium" / "oauth-refresh-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", provider)
    path = root / f"{safe}.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

'''
        if marker not in text:
            raise RuntimeError("TokenManager class marker mismatch")
        text = text.replace(marker, "\n" + helper + marker.lstrip("\n"), 1)
        write(path, text)
    node = function_node(path, "ensure_fresh", "TokenManager")
    forwarded = _forward_arguments(node)
    body = f'''with _refresh_lock(self._provider):
    return self._ensure_fresh_unlocked({forwarded})'''
    wrap_function(
        path,
        "ensure_fresh",
        "_ensure_fresh_unlocked",
        body,
        class_name="TokenManager",
    )


def patch_docs() -> None:
    provider_doc = "docs/architecture/provider-routing.md"
    text = read(provider_doc)
    if "## Implemented production policy" not in text:
        text += '''

## Implemented production policy

Cambium treats the root agent's first successful provider/model as a strict
`ProviderLease`. Every later action and summary call on that recursive trunk is
filtered to the lease; an unavailable incumbent fails the branch rather than
silently moving it and destroying cache/context continuity. Exact
cache-compatible children inherit the lease. Provider-neutral semantic-summary
children and other cold parallel branches choose independently.

Provider configuration separates `rpm` from `max_concurrency`, supports known
free, metered, local, and subscription billing modes, and accepts multiple
independent quota windows. A five-hour, weekly, and monthly allowance are three
constraints, not one blended budget. `QuotaLedger` reserves and reconciles them
with SQLite `BEGIN IMMEDIATE`, while `ProviderScheduler` owns in-process lane
state through an asyncio mailbox.

Selection remains hard-feasibility first. Within a configured priority class it
uses shrinkage success evidence, measured/hinted output throughput, utilization,
known marginal price, cache-switch cost, and a deterministic rendezvous tie
break. Free models are useful for bounded independent work, review, search,
classification, and redundant verification, but cannot win tasks whose model,
context, quality, or tool requirements they do not satisfy.
'''
        write(provider_doc, text)
    terminal_doc = "docs/architecture/terminal-interface.md"
    text = read(terminal_doc)
    if "Provider resource introspection" not in text:
        text += '''

## Provider resource introspection

Durable usage records may include content-free quota-window snapshots. Operator
surfaces can display provider/model leases, reset times, remaining tokens and
requests, lane concurrency, cached/input/output tokens, output tokens/s, and
known marginal cost without reading worker memory.
'''
        write(terminal_doc, text)
    readme = read("README.md")
    if "Pi-style short Python snippets" not in readme:
        readme += '''

## Production routing and tools

A main semantic trunk is provider/model leased after its first successful call.
Cold subagents may use other providers, including configured free lanes, while
exact cache-compatible forks retain the parent lease. Provider configuration
supports independent concurrency and quota windows. Structured tools remain the
portable default; `run_python` adds Pi-style short Python snippets through the
same bounded subprocess boundary as `run_shell`.
'''
        write("README.md", readme)


def write_tests() -> None:
    write(
        "tests/scenarios/test_provider_scheduler.py",
        '''from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cambium.provider_scheduler import (
    BillingMode,
    ProviderEvidence,
    ProviderLease,
    ProviderPolicy,
    ProviderScheduler,
    QuotaLedger,
    QuotaWindowSpec,
    RoutingRequest,
    rank_policies,
)


def _policy(name: str, **kwargs) -> ProviderPolicy:
    return ProviderPolicy(name=name, model=kwargs.pop("model", "m"), **kwargs)


def test_root_lease_is_a_hard_constraint() -> None:
    policies = [_policy("a"), _policy("b", throughput_hint_tps=100)]
    lease = ProviderLease("a", "m", "root")
    ranked = rank_policies(policies, RoutingRequest("child", "m", lease=lease))
    assert [item.name for item in ranked] == ["a"]


def test_model_pin_is_strict_unless_substitution_is_explicit() -> None:
    policies = [_policy("a", model="wanted"), _policy("b", model="other")]
    strict = rank_policies(policies, RoutingRequest("t", "wanted"))
    substituted = rank_policies(
        policies, RoutingRequest("t", "missing", allow_model_substitution=True)
    )
    assert [item.name for item in strict] == ["a"]
    assert {item.name for item in substituted} == {"a", "b"}


def test_throughput_refines_only_equal_priority() -> None:
    policies = [
        _policy("slow", priority=0, throughput_hint_tps=1),
        _policy("fast", priority=0, throughput_hint_tps=50),
        _policy("lower-class", priority=1, throughput_hint_tps=1000),
    ]
    evidence = {
        "slow": ProviderEvidence(attempts=100, successes=95, ewma_tps=2),
        "fast": ProviderEvidence(attempts=100, successes=95, ewma_tps=40),
    }
    ranked = rank_policies(
        policies,
        RoutingRequest("t", "m", expected_output_tokens=1000),
        evidence=evidence,
    )
    assert [item.name for item in ranked] == ["fast", "slow", "lower-class"]


def test_rpm_and_concurrency_have_independent_units() -> None:
    policy = _policy("p", max_concurrency=2)
    assert rank_policies(
        [policy], RoutingRequest("t", "m"), in_flight={"p": 2}
    ) == []


def test_quota_ledger_reservation_is_atomic_across_threads(tmp_path: Path) -> None:
    ledger = QuotaLedger(tmp_path / "quota.db")
    window = QuotaWindowSpec("five-hour", 5 * 3600, request_allowance=10)

    def reserve(index: int):
        return ledger.reserve("zai", (window,), index, now=100.0)

    with ThreadPoolExecutor(max_workers=20) as pool:
        reservations = list(pool.map(reserve, range(20)))
    assert sum(item is not None for item in reservations) == 10
    assert ledger.snapshots("zai")[0].used_requests == 10


def test_scheduler_mailbox_serializes_lane_admission(tmp_path: Path) -> None:
    async def scenario() -> None:
        scheduler = ProviderScheduler(
            [_policy("free", billing_mode=BillingMode.FREE, max_concurrency=1)],
            quota_ledger=QuotaLedger(tmp_path / "quota.db"),
        )
        first = await scheduler.acquire(RoutingRequest("a", "m"))
        try:
            try:
                await scheduler.acquire(RoutingRequest("b", "m"))
            except RuntimeError as exc:
                assert "no provider" in str(exc)
            else:
                raise AssertionError("second acquire unexpectedly succeeded")
        finally:
            await scheduler.release(first, actual_tokens=10, success=True, latency_s=1.0)
        second = await scheduler.acquire(RoutingRequest("b", "m"))
        await scheduler.release(second, actual_tokens=5, success=True, latency_s=1.0)
        await scheduler.close()

    asyncio.run(scenario())
''',
    )
    write(
        "tests/scenarios/test_production_harness_v3.py",
        '''from __future__ import annotations

import json
from pathlib import Path

from cambium.diffundo import Diffundo, ProviderConfig, ProviderTier
from cambium.provider_config import load_providers
from cambium.schemas import TOOL_SCHEMAS


def _tool_names() -> set[str]:
    names = set()
    for item in TOOL_SCHEMAS:
        function = item.get("function") if isinstance(item, dict) else None
        if isinstance(function, dict):
            names.add(function.get("name"))
        elif isinstance(item, dict):
            names.add(item.get("name"))
    return {name for name in names if isinstance(name, str)}


def test_run_python_is_a_portable_structured_tool() -> None:
    assert "run_python" in _tool_names()


def test_provider_config_loads_subscription_resource_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "zai",
                        "tier": "fast",
                        "base_url": "https://example.com/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_ZAI_API_KEY",
                        "model": "glm",
                        "rpm": 100,
                        "max_concurrency": 3,
                        "billing_mode": "subscription",
                        "pricing_known": True,
                        "price_per_1m_in": 0,
                        "price_per_1m_cached_in": 0,
                        "price_per_1m_out": 0,
                        "throughput_hint_tps": 40,
                        "quota_windows": [
                            {
                                "name": "five-hour",
                                "duration_s": 18000,
                                "token_allowance": 1000000,
                                "reserve_fraction": 0.05,
                            },
                            {
                                "name": "weekly",
                                "duration_s": 604800,
                                "token_allowance": 5000000,
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    provider = load_providers(path)[0]
    assert provider.rpm == 100
    assert provider.max_concurrency == 3
    assert len(provider.quota_windows) == 2
    assert provider.quota_windows[0].name == "five-hour"


def test_diffundo_lease_filters_candidates_to_one_continuous_branch() -> None:
    providers = [
        ProviderConfig(
            name="a",
            tier=ProviderTier.FAST,
            base_url="http://127.0.0.1:1/v1",
            api_key_env="A",
            model="m",
        ),
        ProviderConfig(
            name="b",
            tier=ProviderTier.FAST,
            base_url="http://127.0.0.1:2/v1",
            api_key_env="B",
            model="m",
        ),
    ]
    router = Diffundo(providers)
    router.bind_provider("a", "m", root_task_id="root")
    assert [item.name for item in router._candidates(ProviderTier.FAST, "m")] == ["a"]
''',
    )


def main() -> None:
    if not (ROOT / "src/cambium/provider_scheduler.py").is_file():
        raise RuntimeError("provider_scheduler.py payload is missing")
    patch_provider_config()
    patch_diffundo()
    patch_worker()
    patch_supervisor()
    patch_tools()
    patch_oauth()
    patch_docs()
    write_tests()


if __name__ == "__main__":
    main()
