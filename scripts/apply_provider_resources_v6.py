#!/usr/bin/env python3
"""Wire task-class routing, prepaid budgets, and resource telemetry."""

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
        raise RuntimeError(f"{path}: future import marker not found")
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


def insert_call_keyword(path: str, call: ast.Call, keyword: str) -> None:
    text = read(path)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if call.end_lineno is None or call.end_col_offset is None:
        raise RuntimeError("call has no source endpoint")
    absolute = offsets[call.end_lineno - 1] + call.end_col_offset - 1
    before = text[:absolute]
    separator = "" if before.rstrip().endswith("(") else ", "
    write(path, before + separator + keyword + text[absolute:])


def patch_resource_module() -> None:
    path = "src/cambium/provider_resources.py"
    text = read(path)
    old = "from .provider_scheduler import AdmissionGrant, RoutingRequest, quota_db_path\n"
    if old in text:
        text = text.replace(
            old,
            "from typing import TYPE_CHECKING\n\n"
            "if TYPE_CHECKING:\n"
            "    from .provider_scheduler import AdmissionGrant, RoutingRequest\n",
            1,
        )
    if "def _budget_db_path(" not in text:
        marker = "\n\nclass BudgetLedger:"
        helper = '''

def _budget_db_path() -> Path:
    configured = os.environ.get("CAMBIUM_QUOTA_DB")
    if configured:
        return Path(configured).expanduser().resolve()
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "cambium" / "provider-quota.db"

'''
        if marker not in text:
            raise RuntimeError("BudgetLedger insertion marker missing")
        text = text.replace(marker, helper + marker, 1)
    text = text.replace(
        "self.path = quota_db_path() if path is None else Path(path)",
        "self.path = _budget_db_path() if path is None else Path(path)",
    )
    if "    def ensure_balance(" not in text:
        marker = "    def observe_balance(\n"
        method = '''    def ensure_balance(
        self,
        provider: str,
        balance_usd: float,
        *,
        floor_usd: float = 0.0,
        now: float | None = None,
    ) -> None:
        """Seed a configured prepaid balance only when no observation exists."""

        balance = usd_to_micros(balance_usd)
        floor = usd_to_micros(floor_usd)
        if floor > balance:
            raise ValueError("reserve floor cannot exceed the configured balance")
        timestamp = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO provider_balances(provider,balance_micros,"
                "reserved_micros,floor_micros,updated_at) VALUES(?,?,?,?,?)",
                (provider, balance, 0, floor, timestamp),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

'''
        if marker not in text:
            raise RuntimeError("observe_balance marker missing")
        text = text.replace(marker, method + marker, 1)
    write(path, text)


def patch_scheduler_task_classes() -> None:
    path = "src/cambium/provider_scheduler.py"
    add_import(path, "from .provider_resources import TaskClass")
    text = read(path)
    policy_marker = "    enabled: bool = True\n"
    if "    task_classes: frozenset[TaskClass]" not in text:
        if policy_marker not in text:
            raise RuntimeError("ProviderPolicy enabled marker missing")
        text = text.replace(
            policy_marker,
            policy_marker
            + "    task_classes: frozenset[TaskClass] = frozenset(TaskClass)\n"
            + "    quality_score: float = 1.0\n",
            1,
        )
    request_marker = "    lease: ProviderLease | None = None\n"
    if "    task_class: TaskClass" not in text:
        if request_marker not in text:
            raise RuntimeError("RoutingRequest lease marker missing")
        text = text.replace(
            request_marker,
            request_marker
            + "    task_class: TaskClass = TaskClass.ROOT\n"
            + "    min_quality_score: float = 0.0\n",
            1,
        )
    eligibility_marker = "    if not policy.enabled or in_flight.get(policy.name, 0) >= policy.max_concurrency:\n"
    if "request.task_class not in policy.task_classes" not in text:
        replacement = eligibility_marker + "        return False\n"
        old = eligibility_marker + "        return False\n"
        if old not in text:
            raise RuntimeError("scheduler eligibility marker mismatch")
        replacement += (
            "    if request.task_class not in policy.task_classes:\n"
            "        return False\n"
            "    if policy.quality_score < request.min_quality_score:\n"
            "        return False\n"
        )
        text = text.replace(old, replacement, 1)
    if "1.0 - policy.quality_score" not in text:
        marker = "            max(0.0, quota_pressures.get(policy.name, 0.0)),\n"
        if marker not in text:
            marker = "            1.0 - success,\n"
        if marker not in text:
            raise RuntimeError("scheduler ranking marker missing")
        text = text.replace(marker, marker + "            1.0 - policy.quality_score,\n", 1)
    write(path, text)


def patch_provider_config() -> None:
    path = "src/cambium/provider_config.py"
    add_import(
        path,
        "from .provider_resources import QuotaHeaderMapping, parse_task_classes",
    )
    text = read(path)
    field_marker = '        "allow_model_substitution",\n'
    if '        "task_classes",\n' not in text:
        if field_marker not in text:
            raise RuntimeError("provider resource field marker missing")
        text = text.replace(
            field_marker,
            field_marker
            + '        "task_classes",\n'
            + '        "quality_score",\n'
            + '        "prepaid_budget_usd",\n'
            + '        "prepaid_floor_usd",\n'
            + '        "quota_header_mappings",\n',
            1,
        )
    defaults_marker = '    "allow_model_substitution": False,\n'
    if '    "task_classes": None,\n' not in text:
        if defaults_marker not in text:
            raise RuntimeError("provider resource defaults marker missing")
        text = text.replace(
            defaults_marker,
            defaults_marker
            + '    "task_classes": None,\n'
            + '    "quality_score": 1.0,\n'
            + '    "prepaid_budget_usd": 0.0,\n'
            + '    "prepaid_floor_usd": 0.0,\n'
            + '    "quota_header_mappings": (),\n',
            1,
        )
    typed_marker = "    allow_model_substitution: bool\n"
    if "    task_classes: frozenset\n" not in text:
        if typed_marker not in text:
            raise RuntimeError("provider resource TypedDict marker missing")
        text = text.replace(
            typed_marker,
            typed_marker
            + "    task_classes: frozenset\n"
            + "    quality_score: float\n"
            + "    prepaid_budget_usd: float\n"
            + "    prepaid_floor_usd: float\n"
            + "    quota_header_mappings: tuple[QuotaHeaderMapping, ...]\n",
            1,
        )
    helper_marker = "\ndef _validate_provider_mapping(raw: object, index: int) -> _ProviderMapping:\n"
    if "def _parse_quota_header_mappings(" not in text:
        helper = '''
def _parse_quota_header_mappings(
    value: object, location: str
) -> tuple[QuotaHeaderMapping, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise _error(location, "must be a list")
    allowed = {
        "name", "duration_s", "token_limit_header", "token_remaining_header",
        "request_limit_header", "request_remaining_header", "reset_header",
    }
    parsed: list[QuotaHeaderMapping] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) - allowed:
            raise _error(f"{location}[{index}]", "has an invalid field set")
        try:
            parsed.append(QuotaHeaderMapping(**item))
        except (TypeError, ValueError) as exc:
            raise _error(f"{location}[{index}]", str(exc)) from exc
    return tuple(parsed)

'''
        if helper_marker not in text:
            raise RuntimeError("provider mapping function marker missing")
        text = text.replace(helper_marker, "\n" + helper + helper_marker.lstrip("\n"), 1)
    validation_marker = "    # Optional Responses-API reasoning effort"
    if "    task_classes = parse_task_classes(" not in text:
        block = '''    try:
        task_classes = parse_task_classes(raw.get("task_classes"))
    except ValueError as exc:
        raise _error(f"{location}.task_classes", str(exc)) from exc
    quality_score = _require_number(
        values["quality_score"], f"{location}.quality_score"
    )
    if not 0 <= quality_score <= 1:
        raise _error(f"{location}.quality_score", "must be in [0, 1]")
    prepaid_budget_usd = _require_number(
        values["prepaid_budget_usd"], f"{location}.prepaid_budget_usd"
    )
    prepaid_floor_usd = _require_number(
        values["prepaid_floor_usd"], f"{location}.prepaid_floor_usd"
    )
    if prepaid_budget_usd < 0 or prepaid_floor_usd < 0:
        raise _error(location, "prepaid budgets must be non-negative")
    if prepaid_floor_usd > prepaid_budget_usd and prepaid_budget_usd > 0:
        raise _error(f"{location}.prepaid_floor_usd", "must not exceed prepaid_budget_usd")
    quota_header_mappings = _parse_quota_header_mappings(
        raw.get("quota_header_mappings", []), f"{location}.quota_header_mappings"
    )

'''
        index = text.find(validation_marker)
        if index < 0:
            raise RuntimeError("provider resource validation marker missing")
        text = text[:index] + block + text[index:]
    return_marker = '        "allow_model_substitution": allow_model_substitution,\n'
    if '        "task_classes": task_classes,\n' not in text:
        if return_marker not in text:
            raise RuntimeError("provider resource return marker missing")
        text = text.replace(
            return_marker,
            return_marker
            + '        "task_classes": task_classes,\n'
            + '        "quality_score": quality_score,\n'
            + '        "prepaid_budget_usd": prepaid_budget_usd,\n'
            + '        "prepaid_floor_usd": prepaid_floor_usd,\n'
            + '        "quota_header_mappings": quota_header_mappings,\n',
            1,
        )
    config_marker = '        "allow_model_substitution": values["allow_model_substitution"],\n'
    if '        "task_classes": values["task_classes"],\n' not in text:
        if config_marker not in text:
            raise RuntimeError("provider resource constructor marker missing")
        text = text.replace(
            config_marker,
            config_marker
            + '        "task_classes": values["task_classes"],\n'
            + '        "quality_score": values["quality_score"],\n'
            + '        "prepaid_budget_usd": values["prepaid_budget_usd"],\n'
            + '        "prepaid_floor_usd": values["prepaid_floor_usd"],\n'
            + '        "quota_header_mappings": values["quota_header_mappings"],\n',
            1,
        )
    write(path, text)


def add_call_kwonly(path: str, class_name: str, name: str, declaration: str) -> None:
    text = read(path)
    node = function_node(path, name, class_name)
    header_lines = text.splitlines(keepends=True)[node.lineno - 1 : node.body[0].lineno - 1]
    header = "".join(header_lines)
    if declaration.split(":", 1)[0].strip() in header:
        return
    closing = header.rfind(")")
    if closing < 0:
        raise RuntimeError(f"{class_name}.{name}: signature closing parenthesis missing")
    prefix = header[:closing]
    suffix = header[closing:]
    indent = " " * (node.col_offset + 4)
    separator = "" if prefix.rstrip().endswith(("(", ",")) else ","
    addition = f"{separator}\n{indent}{declaration},\n{' ' * node.col_offset}"
    new_header = prefix + addition + suffix
    lines = text.splitlines(keepends=True)
    lines[node.lineno - 1 : node.body[0].lineno - 1] = [new_header]
    write(path, "".join(lines))


def patch_diffundo() -> None:
    path = "src/cambium/diffundo.py"
    add_import(
        path,
        "from .provider_resources import BudgetLedger, QuotaHeaderMapping, TaskClass",
    )
    text = read(path)
    field_marker = "    allow_model_substitution: bool = False\n"
    if "    task_classes: frozenset[TaskClass]" not in text:
        if field_marker not in text:
            raise RuntimeError("Diffundo provider resource field marker missing")
        text = text.replace(
            field_marker,
            field_marker
            + "    task_classes: frozenset[TaskClass] = frozenset(TaskClass)\n"
            + "    quality_score: float = 1.0\n"
            + "    prepaid_budget_usd: float = 0.0\n"
            + "    prepaid_floor_usd: float = 0.0\n"
            + "    quota_header_mappings: tuple[QuotaHeaderMapping, ...] = ()\n",
            1,
        )
    result_marker = "    quota_windows: tuple[dict[str, Any], ...] | None = None\n"
    if "    budget_remaining_usd: float | None = None\n" not in text:
        if result_marker not in text:
            raise RuntimeError("CallResult quota marker missing")
        text = text.replace(
            result_marker,
            result_marker + "    budget_remaining_usd: float | None = None\n",
            1,
        )
    write(path, text)
    text = read(path)
    if "self._budget_ledger" not in text:
        tree = ast.parse(text)
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo")
        init = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
        provider_assignment = None
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
                    provider_assignment = node
                    break
        if provider_assignment is None or provider_assignment.end_lineno is None:
            raise RuntimeError("Diffundo provider assignment missing")
        lines = text.splitlines(keepends=True)
        block = '''        self._budget_ledger = (
            BudgetLedger()
            if any(provider.prepaid_budget_usd > 0 for provider in self._providers)
            else None
        )
        if self._budget_ledger is not None:
            for provider in self._providers:
                if provider.prepaid_budget_usd > 0:
                    self._budget_ledger.ensure_balance(
                        provider.name,
                        provider.prepaid_budget_usd,
                        floor_usd=provider.prepaid_floor_usd,
                    )
'''
        lines.insert(provider_assignment.end_lineno, block)
        write(path, "".join(lines))
    node = function_node(path, "_candidates", "Diffundo")
    forwarded = _forward_arguments(node)
    # Current candidates are known to accept tier/model; retain that surface and
    # add a keyword-only semantic task class.
    body = '''value = task_class.value if isinstance(task_class, TaskClass) else str(task_class)
try:
    semantic_class = TaskClass(value)
except ValueError:
    semantic_class = TaskClass.CODE
return [
    provider
    for provider in self._candidates_without_task_capabilities(''' + forwarded + ''')
    if semantic_class in provider.task_classes
]'''
    wrap_function(
        path,
        "_candidates",
        "_candidates_without_task_capabilities",
        body,
        class_name="Diffundo",
    )
    # Replace the generated wrapper signature with a source-compatible explicit
    # form; existing callers retain the ROOT default.
    text = read(path)
    wrapper = function_node(path, "_candidates", "Diffundo")
    header_lines = text.splitlines(keepends=True)[
        wrapper.lineno - 1 : wrapper.body[0].lineno - 1
    ]
    header = "".join(header_lines)
    if "task_class" not in header:
        closing = header.rfind(")")
        prefix = header[:closing]
        suffix = header[closing:]
        addition = ", *, task_class: TaskClass | str = TaskClass.ROOT"
        if prefix.rstrip().endswith(","):
            addition = " *, task_class: TaskClass | str = TaskClass.ROOT"
        new_header = prefix + addition + suffix
        lines = text.splitlines(keepends=True)
        lines[wrapper.lineno - 1 : wrapper.body[0].lineno - 1] = [new_header]
        write(path, "".join(lines))
    add_call_kwonly(path, "Diffundo", "call", "task_class: TaskClass | str = TaskClass.ROOT")
    text = read(path)
    tree = ast.parse(text)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo")
    call_method = next(
        node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "call"
    )
    candidate_calls = [
        node
        for node in ast.walk(call_method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_candidates"
        and not any(keyword.arg == "task_class" for keyword in node.keywords)
    ]
    for call in sorted(candidate_calls, key=lambda item: (item.lineno, item.col_offset), reverse=True):
        insert_call_keyword(path, call, "task_class=task_class")
    text = read(path)
    tree = ast.parse(text)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Diffundo")
    attempt = None
    for method in cls.body:
        if not isinstance(method, ast.AsyncFunctionDef) or method.name == "call":
            continue
        args = {arg.arg for arg in [*method.args.posonlyargs, *method.args.args]}
        segment = ast.get_source_segment(text, method) or ""
        if {"provider", "prompt"} <= args and "CallResult" in segment:
            if "_money_wrapped_attempt" not in method.name:
                if "_quota_wrapped_attempt" in segment:
                    attempt = method
                    break
                if attempt is None:
                    attempt = method
    if attempt is not None and "_money_wrapped_attempt" not in text:
        forwarded = _forward_arguments(attempt)
        body = f'''policy = provider
ledger = self._budget_ledger
if ledger is None or policy.prepaid_budget_usd <= 0:
    return await self._money_wrapped_attempt({forwarded})
messages = prompt.get("messages", []) if isinstance(prompt, Mapping) else []
estimated_input = max(
    1,
    sum(len(str(message.get("content", "")).encode("utf-8")) for message in messages) // 4,
)
estimated_usd = (
    estimated_input * policy.price_per_1m_in
    + 4096 * policy.price_per_1m_out
) / 1_000_000.0
reservation = await asyncio.to_thread(ledger.reserve, policy.name, estimated_usd)
if reservation is None:
    raise ProviderError(
        policy.name,
        ProviderOutcome.QUOTA,
        "prepaid provider balance is exhausted or below its reserve floor",
    )
try:
    result = await self._money_wrapped_attempt({forwarded})
except BaseException:
    await asyncio.to_thread(ledger.reconcile, reservation, 0.0)
    raise
actual = max(0.0, float(result.estimated_cost_usd))
await asyncio.to_thread(ledger.reconcile, reservation, actual)
snapshot = await asyncio.to_thread(ledger.snapshot, policy.name)
return replace(
    result,
    budget_remaining_usd=(None if snapshot is None else snapshot.available_usd),
)'''
        wrap_function(
            path,
            attempt.name,
            "_money_wrapped_attempt",
            body,
            class_name="Diffundo",
        )


def patch_worker() -> None:
    path = "src/cambium/worker.py"
    add_import(path, "from cambium.provider_resources import TaskClass")
    text = read(path)
    tree = ast.parse(text)
    config = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AgentConfig"),
        None,
    )
    if config is None:
        raise RuntimeError("AgentConfig class missing")
    fields = [node for node in config.body if isinstance(node, ast.AnnAssign)]
    names = {node.target.id for node in fields if isinstance(node.target, ast.Name)}
    if "task_class" not in names:
        last = fields[-1]
        if last.end_lineno is None:
            raise RuntimeError("AgentConfig final field has no endpoint")
        lines = text.splitlines(keepends=True)
        lines.insert(last.end_lineno, '    task_class: str = "root"\n')
        text = "".join(lines)
        write(path, text)
    text = read(path)
    tree = ast.parse(text)
    config_function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_config_from_run"
        ),
        None,
    )
    if config_function is not None:
        calls = [
            node
            for node in ast.walk(config_function)
            if isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name) and node.func.id == "AgentConfig"
            )
            and not any(keyword.arg == "task_class" for keyword in node.keywords)
        ]
        for call in sorted(calls, key=lambda item: (item.lineno, item.col_offset), reverse=True):
            insert_call_keyword(path, call, 'task_class=str(run.get("task_class", "root"))')
    text = read(path)
    if "async def _call_router_for_task(" not in text:
        marker = "\nasync def _run_agent_loop("
        helper = '''
async def _call_router_for_task(
    router: Any,
    *args: Any,
    task_class: str,
    **kwargs: Any,
) -> CallResult:
    """Pass task capability metadata only to the concrete Diffundo router."""

    if isinstance(router, Diffundo):
        return await router.call(*args, task_class=task_class, **kwargs)
    return await router.call(*args, **kwargs)

'''
        if marker not in text:
            raise RuntimeError("worker run-agent-loop marker missing")
        text = text.replace(marker, "\n" + helper + marker.lstrip("\n"), 1)
        write(path, text)
    text = read(path)
    tree = ast.parse(text)
    loop = function_node(path, "_run_agent_loop")
    calls = [
        node
        for node in ast.walk(loop)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "router"
        and node.func.attr == "call"
    ]
    # Change calls from the end so source positions remain valid.
    for call in sorted(calls, key=lambda item: (item.lineno, item.col_offset), reverse=True):
        text = read(path)
        lines = text.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        start = offsets[call.func.lineno - 1] + call.func.col_offset
        end = offsets[call.func.end_lineno - 1] + call.func.end_col_offset
        text = text[:start] + "_call_router_for_task" + text[end:]
        write(path, text)
        tree = ast.parse(text)
        loop = function_node(path, "_run_agent_loop")
        replacement_call = next(
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_call_router_for_task"
            and not any(keyword.arg == "task_class" for keyword in node.keywords)
        )
        # Router becomes the first positional argument.
        call_text = read(path)
        lines = call_text.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        open_position = offsets[replacement_call.lineno - 1] + replacement_call.col_offset
        open_position = call_text.find("(", open_position) + 1
        call_text = call_text[:open_position] + "router, " + call_text[open_position:]
        write(path, call_text)
        tree = ast.parse(call_text)
        loop = function_node(path, "_run_agent_loop")
        replacement_call = next(
            node
            for node in ast.walk(loop)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_call_router_for_task"
            and not any(keyword.arg == "task_class" for keyword in node.keywords)
        )
        insert_call_keyword(path, replacement_call, "task_class=config.task_class")
    text = read(path)
    marker = "    if result.quota_windows is not None:\n"
    if "result.budget_remaining_usd" not in text:
        block = (
            "    if result.budget_remaining_usd is not None:\n"
            "        event[\"budget_remaining_usd\"] = result.budget_remaining_usd\n"
        )
        if marker in text:
            text = text.replace(marker, block + marker, 1)
        else:
            provider_hit = "    if result.provider_cache_hit is not None:\n"
            if provider_hit not in text:
                raise RuntimeError("worker usage event insertion marker missing")
            text = text.replace(provider_hit, block + provider_hit, 1)
    write(path, text)


def patch_supervisor() -> None:
    path = "src/cambium/supervisor.py"
    text = read(path)
    forward_marker = '        "quota_windows",\n'
    if '        "budget_remaining_usd",\n' not in text:
        if forward_marker not in text:
            raise RuntimeError("supervisor quota forwarding marker missing")
        text = text.replace(
            forward_marker,
            forward_marker + '        "budget_remaining_usd",\n',
            1,
        )
    numeric_marker = '    for field in ("estimated_cost_usd", "latency_s", "retry_after_s"):\n'
    if "budget_remaining_usd" not in numeric_marker and "budget_remaining_usd\")" not in text:
        if numeric_marker in text:
            text = text.replace(
                numeric_marker,
                '    for field in (\n'
                '        "estimated_cost_usd", "latency_s", "retry_after_s",\n'
                '        "budget_remaining_usd",\n'
                '    ):\n',
                1,
            )
    payload_marker = '            "task": spec.get("task", ""),\n'
    if '            "task_class": spec.get("task_class", "root"),\n' not in text:
        if payload_marker not in text:
            raise RuntimeError("supervisor run payload task marker missing")
        text = text.replace(
            payload_marker,
            payload_marker + '            "task_class": spec.get("task_class", "root"),\n',
            1,
        )
    write(path, text)
    if "def _task_class_for_child_kind(" not in read(path):
        text = read(path)
        marker = "\ndef _child_spec("
        helper = '''
def _task_class_for_child_kind(kind: object) -> str:
    value = str(kind or "").lower()
    if "search" in value:
        return "search"
    if "research" in value or "inspect" in value:
        return "research"
    if "review" in value or "critic" in value:
        return "review"
    if "test" in value or "verify" in value:
        return "test"
    if "triage" in value or "classif" in value:
        return "triage"
    if "summary" in value:
        return "summary"
    return "code"

'''
        if marker not in text:
            raise RuntimeError("supervisor child spec marker missing")
        text = text.replace(marker, "\n" + helper + marker.lstrip("\n"), 1)
        write(path, text)
    node = function_node(path, "_child_spec")
    forwarded = _forward_arguments(node)
    arg_names = [arg.arg for arg in [*node.args.posonlyargs, *node.args.args]]
    proposal_name = next((name for name in arg_names if name == "proposal"), None)
    if proposal_name is None:
        raise RuntimeError("_child_spec has no proposal argument")
    body = f'''child = _child_spec_without_task_class({forwarded})
child.setdefault("task_class", _task_class_for_child_kind({proposal_name}.get("kind")))
return child'''
    wrap_function(path, "_child_spec", "_child_spec_without_task_class", body)


def patch_quota_cli() -> None:
    path = "src/cambium/quota_cli.py"
    add_import(path, "from .provider_resources import BudgetLedger, balance_snapshot_json")
    text = read(path)
    if '"balance"' not in text[text.find("def _parser"): text.find("def _format_reset")]:
        marker = "    return parser\n"
        block = '''    balance = commands.add_parser(
        "balance", help="record one prepaid provider balance observation"
    )
    balance.add_argument("provider")
    balance.add_argument("--usd", type=float, required=True)
    balance.add_argument("--floor-usd", type=float, default=0.0)
    balances = commands.add_parser(
        "balances", help="show prepaid provider balances"
    )
    balances.add_argument("--provider")
    balances.add_argument("--json", action="store_true")
'''
        if marker not in text:
            raise RuntimeError("quota CLI parser return marker missing")
        text = text.replace(marker, block + marker, 1)
    run_marker = "    command = args.quota_command\n"
    if "if command == \"balance\":" not in text:
        block = '''    if command == "balance":
        BudgetLedger(getattr(args, "db", None)).observe_balance(
            args.provider, args.usd, floor_usd=args.floor_usd
        )
        return 0
    if command == "balances":
        ledger_balances = BudgetLedger(getattr(args, "db", None))
        snapshots = (
            (ledger_balances.snapshot(args.provider),)
            if args.provider
            else ledger_balances.snapshots()
        )
        snapshots = tuple(item for item in snapshots if item is not None)
        if args.json:
            print(json.dumps([balance_snapshot_json(item) for item in snapshots], sort_keys=True))
        elif not snapshots:
            print("no prepaid provider balances")
        else:
            for item in snapshots:
                print(
                    f"{item.provider}: balance=${item.balance_usd:.6f} "
                    f"available=${item.available_usd:.6f} floor=${item.floor_usd:.6f}"
                )
        return 0
'''
        if run_marker not in text:
            raise RuntimeError("quota CLI command marker missing")
        text = text.replace(run_marker, run_marker + block, 1)
    write(path, text)
    path = "src/cambium/cli.py"
    text = read(path)
    if 'quota_commands.add_parser("balance"' not in text:
        marker = "    quota_observe.add_argument(\"--reserve-fraction\", type=float, default=0.0)\n"
        block = '''    quota_balance = quota_commands.add_parser("balance")
    quota_balance.add_argument("provider")
    quota_balance.add_argument("--usd", type=float, required=True)
    quota_balance.add_argument("--floor-usd", type=float, default=0.0)
    quota_balances = quota_commands.add_parser("balances")
    quota_balances.add_argument("--provider")
    quota_balances.add_argument("--json", action="store_true")
'''
        if marker not in text:
            raise RuntimeError("main CLI quota parser marker missing")
        text = text.replace(marker, marker + block, 1)
    write(path, text)


def patch_render() -> None:
    path = "src/cambium/render.py"
    text = read(path)
    if "def render_budget_status(" not in text:
        marker = "\ndef render_quota_status("
        helper = '''
def render_budget_status(events: Any) -> str:
    """Latest prepaid balance from a durable provider usage event."""

    for event in reversed(list(events or ())):
        if not isinstance(event, Mapping) or event.get("kind") != "usage_event":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        remaining = _finite_number(payload.get("budget_remaining_usd"))
        provider = payload.get("provider")
        if remaining is not None and isinstance(provider, str):
            return f"balance={_sanitize_field(provider)}:${remaining:.4f}"
    return ""

'''
        if marker not in text:
            raise RuntimeError("render quota status marker missing")
        text = text.replace(marker, "\n" + helper + marker.lstrip("\n"), 1)
    if "budget_status = render_budget_status(records)" not in text:
        marker = "    quota = render_quota_status(records)\n"
        block = (
            "    budget_status = render_budget_status(records)\n"
            "    if budget_status:\n"
            "        right_parts.append(budget_status)\n"
        )
        if marker not in text:
            raise RuntimeError("render status bar quota marker missing")
        text = text.replace(marker, block + marker, 1)
    if '    "render_budget_status",\n' not in text:
        text = text.replace(
            '    "render_active_workers",\n',
            '    "render_active_workers",\n    "render_budget_status",\n',
            1,
        )
    write(path, text)


def patch_docs() -> None:
    path = "docs/architecture/provider-routing.md"
    text = read(path)
    if "## Task classes and prepaid balances" not in text:
        text += '''

## Task classes and prepaid balances

Provider capability is a hard set membership test. Root/code-changing work can
require high quality and root/code task classes; weaker free lanes can be
restricted to search, research, review, test triage, summaries, or redundant
verification. A free model never becomes root-eligible merely because it has
zero marginal cost.

Prepaid API providers use a transactional micro-dollar ledger. Cambium reserves
an estimated call cost before dispatch, reconciles the provider-reported cost,
and refuses work below the configured balance floor. Configure an initial
balance in the provider entry and update it from the operator surface when the
provider dashboard changes:

```sh
cambium quota balance openrouter --usd 25 --floor-usd 2
cambium quota balances
```

Subscription quota pressure, prepaid balance, per-call marginal price, free
capacity, health, concurrency, quality, throughput, and cache switching remain
separate state machines. They meet only after hard feasibility at the admission
objective.
'''
        write(path, text)


def write_tests() -> None:
    write(
        "tests/scenarios/test_provider_resources_v6.py",
        '''from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cambium.provider_resources import (
    BudgetLedger,
    QuotaHeaderMapping,
    TaskClass,
    parse_quota_headers,
    parse_reset_at,
    parse_task_classes,
    usd_to_micros,
)
from cambium.provider_scheduler import ProviderPolicy, RoutingRequest, rank_policies


def test_money_uses_integer_microdollars() -> None:
    assert usd_to_micros("1.2345674") == 1_234_567
    assert usd_to_micros("1.2345675") == 1_234_568


def test_budget_reservations_are_cross_thread_atomic(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "quota.db")
    ledger.observe_balance("openrouter", 1.0, floor_usd=0.1)

    def reserve(_index: int):
        return ledger.reserve("openrouter", 0.1)

    with ThreadPoolExecutor(max_workers=20) as pool:
        reservations = list(pool.map(reserve, range(20)))
    accepted = [item for item in reservations if item is not None]
    assert len(accepted) == 9
    for item in accepted:
        ledger.reconcile(item, 0.1)
    snapshot = ledger.snapshot("openrouter")
    assert snapshot is not None
    assert snapshot.available_usd == pytest.approx(0.0, abs=1e-9)
    assert snapshot.floor_usd == pytest.approx(0.1)


def test_task_classes_are_hard_capabilities() -> None:
    root = ProviderPolicy(
        "strong", "m", task_classes=frozenset({TaskClass.ROOT, TaskClass.CODE}),
        quality_score=0.95,
    )
    free = ProviderPolicy(
        "free", "m", task_classes=frozenset({TaskClass.SEARCH, TaskClass.REVIEW}),
        quality_score=0.5,
    )
    assert [item.name for item in rank_policies(
        [root, free], RoutingRequest("root", "m", task_class=TaskClass.ROOT, min_quality_score=0.9)
    )] == ["strong"]
    assert [item.name for item in rank_policies(
        [root, free], RoutingRequest("review", "m", task_class=TaskClass.REVIEW)
    )] == ["free"]


def test_task_class_parser_fails_closed() -> None:
    assert parse_task_classes(["search", "review"]) == frozenset(
        {TaskClass.SEARCH, TaskClass.REVIEW}
    )
    with pytest.raises(ValueError, match="invalid task class"):
        parse_task_classes(["cheap-ish"])


def test_quota_header_adapter_handles_duration_reset() -> None:
    now = 1000.0
    mapping = QuotaHeaderMapping(
        "five-hour",
        18000,
        token_limit_header="x-limit-tokens",
        token_remaining_header="x-remaining-tokens",
        reset_header="x-reset-tokens",
    )
    snapshots = parse_quota_headers(
        "zai",
        {"X-Limit-Tokens": "1000", "x-remaining-tokens": "250", "x-reset-tokens": "1h30m"},
        (mapping,),
        now=now,
    )
    assert snapshots[0]["remaining_tokens"] == 250
    assert snapshots[0]["reset_at"] == now + 5400
    assert parse_reset_at("30s", now=now, fallback_s=10) == now + 30
''',
    )
    write(
        "tests/scenarios/test_provider_resources_source_v6.py",
        '''from __future__ import annotations

from pathlib import Path


def _source(name: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "src" / "cambium" / name).read_text(encoding="utf-8")


def test_worker_passes_task_class_through_router_port() -> None:
    source = _source("worker.py")
    assert "_call_router_for_task" in source
    assert "task_class=config.task_class" in source


def test_supervisor_classifies_children_and_payloads() -> None:
    source = _source("supervisor.py")
    assert "_task_class_for_child_kind" in source
    assert '"task_class": spec.get("task_class", "root")' in source


def test_diffundo_has_prepaid_reservation_and_capability_filter() -> None:
    source = _source("diffundo.py")
    assert "prepaid provider balance is exhausted" in source
    assert "semantic_class in provider.task_classes" in source


def test_codex_oauth_has_public_client_refresh_and_cross_process_lock() -> None:
    oauth = _source("oauth.py")
    profile = _source("provider_config.py")
    assert "_refresh_lock" in oauth
    assert "resolve_codex_client_id" in oauth
    assert '"client_id": "app_' in profile
''',
    )
    write(
        "tests/scenarios/test_quota_balance_cli_v6.py",
        '''from __future__ import annotations

import json
from pathlib import Path

from cambium import cli


def test_prepaid_balance_operator_round_trip(tmp_path: Path, capsys) -> None:
    db = tmp_path / "quota.db"
    assert cli.main([
        "quota", "--db", str(db), "balance", "openrouter",
        "--usd", "12.5", "--floor-usd", "1.5",
    ]) == 0
    assert cli.main([
        "quota", "--db", str(db), "balances", "--provider", "openrouter", "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert payload[0]["balance_usd"] == 12.5
    assert payload[0]["available_usd"] == 11.0
''',
    )


def main() -> None:
    patch_resource_module()
    patch_scheduler_task_classes()
    patch_provider_config()
    patch_diffundo()
    patch_worker()
    patch_supervisor()
    patch_quota_cli()
    patch_render()
    patch_docs()
    write_tests()


if __name__ == "__main__":
    main()
