#!/usr/bin/env python3
"""Wire immutable tool registry, code navigation, LSP, and lease persistence."""

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


def append_schemas() -> None:
    path = "src/cambium/schemas.py"
    text = read(path)
    if "_CAMBIUM_NAVIGATION_SCHEMAS" in text:
        return
    text += '''

_CAMBIUM_NAVIGATION_DIRECT = (
    {
        "name": "symbol_search",
        "description": "Find declarations by symbol name across the worktree.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "exact": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_references",
        "description": "Find exact identifier references across bounded source files.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "pattern": "^[A-Za-z_][A-Za-z0-9_]*$"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_symbol",
        "description": "Read a bounded source window around a known location.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "line": {"type": "integer", "minimum": 1},
                "context_lines": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["path", "line"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lsp_query",
        "description": (
            "Run one bounded definition, references, hover, document-symbols, or "
            "diagnostics query through the operator-configured language server."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["definition", "references", "hover", "document_symbols", "diagnostics"],
                },
                "path": {"type": "string", "minLength": 1},
                "line": {"type": "integer", "minimum": 1},
                "column": {"type": "integer", "minimum": 1},
                "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            },
            "required": ["method", "path"],
            "additionalProperties": False,
        },
    },
)
_CAMBIUM_NAVIGATION_SCHEMAS = tuple(
    {"type": "function", "function": item}
    if TOOL_SCHEMAS and isinstance(TOOL_SCHEMAS[0], dict) and "function" in TOOL_SCHEMAS[0]
    else item
    for item in _CAMBIUM_NAVIGATION_DIRECT
)
_existing_tool_names = {
    item.get("function", item).get("name")
    for item in TOOL_SCHEMAS
    if isinstance(item, dict) and isinstance(item.get("function", item), dict)
}
TOOL_SCHEMAS = type(TOOL_SCHEMAS)([
    *TOOL_SCHEMAS,
    *(item for item in _CAMBIUM_NAVIGATION_SCHEMAS if item.get("function", item).get("name") not in _existing_tool_names),
])
'''
    write(path, text)


def patch_tool_permissions() -> None:
    path = "src/cambium/tools.py"
    text = read(path)
    tree = ast.parse(text)
    policy = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ToolPermissionPolicy"),
        None,
    )
    if policy is None:
        raise RuntimeError("ToolPermissionPolicy class not found")
    if "allow_python" not in {
        target.id
        for node in policy.body
        if isinstance(node, ast.AnnAssign) and isinstance((target := node.target), ast.Name)
    }:
        field_nodes = [node for node in policy.body if isinstance(node, ast.AnnAssign)]
        if not field_nodes:
            raise RuntimeError("ToolPermissionPolicy has no annotated fields")
        shell = next(
            (
                node
                for node in field_nodes
                if isinstance(node.target, ast.Name) and node.target.id == "allow_shell"
            ),
            field_nodes[-1],
        )
        if shell.end_lineno is None:
            raise RuntimeError("ToolPermissionPolicy field has no source end")
        lines = text.splitlines(keepends=True)
        lines.insert(shell.end_lineno, "    allow_python: bool = False\n")
        text = "".join(lines)
        write(path, text)
    add_import(path, "import time")
    add_import(path, "from .code_index import find_references, locations_json, read_symbol, search_symbols")
    add_import(path, "from .lsp_query import LspQueryError, query_lsp")
    node = function_node(path, "run_tool")
    names = [arg.arg for arg in [*node.args.posonlyargs, *node.args.args]]
    if names and names[0] in {"self", "cls"}:
        names = names[1:]
    if len(names) < 3:
        raise RuntimeError("run_tool signature is not name/arguments/context compatible")
    name_arg, arguments_arg, context_arg = names[:3]
    forwarded = _forward_arguments(node)
    body = f'''policy = getattr({context_arg}, "permissions", None)
if policy is None:
    policy = getattr({context_arg}, "policy", None)
worktree = Path(getattr({context_arg}, "worktree"))
started = time.monotonic()
def result(ok: bool, output: str = "", error: str | None = None) -> ToolResult:
    encoded_output = output.encode("utf-8", "replace")[:65536].decode("utf-8", "replace")
    encoded_error = (
        None
        if error is None
        else error.encode("utf-8", "replace")[:65536].decode("utf-8", "replace")
    )
    return ToolResult(
        ok=ok,
        output=encoded_output,
        error=encoded_error,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
try:
    if {name_arg} == "run_python":
        if not bool(getattr(policy, "allow_python", False)):
            return result(False, error="run_python permission denied")
        code = {arguments_arg}.get("code") if isinstance({arguments_arg}, dict) else None
        if not isinstance(code, str) or not code.strip():
            return result(False, error="run_python requires non-empty code")
        if len(code.encode("utf-8")) > 32768:
            return result(False, error="run_python code exceeds 32768 bytes")
        safe_env = {{
            key: value
            for key, value in os.environ.items()
            if key in {{"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}}
        }}
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", code],
            cwd=worktree,
            env=safe_env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result(
            completed.returncode == 0,
            output=completed.stdout,
            error=completed.stderr or None,
        )
    if {name_arg} == "symbol_search":
        query = {arguments_arg}.get("query")
        exact = bool({arguments_arg}.get("exact", False))
        maximum = int({arguments_arg}.get("max_results", 50))
        return result(True, locations_json(search_symbols(worktree, query, exact=exact, max_results=maximum)))
    if {name_arg} == "find_references":
        symbol = {arguments_arg}.get("symbol")
        maximum = int({arguments_arg}.get("max_results", 100))
        return result(True, locations_json(find_references(worktree, symbol, max_results=maximum)))
    if {name_arg} == "read_symbol":
        value = read_symbol(
            worktree,
            {arguments_arg}.get("path"),
            int({arguments_arg}.get("line", 0)),
            context_lines=int({arguments_arg}.get("context_lines", 40)),
        )
        return result(True, json.dumps(value, ensure_ascii=False))
    if {name_arg} == "lsp_query":
        value = query_lsp(
            worktree,
            method={arguments_arg}.get("method"),
            path={arguments_arg}.get("path"),
            line=int({arguments_arg}.get("line", 1)),
            column=int({arguments_arg}.get("column", 1)),
            timeout_s=float({arguments_arg}.get("timeout_s", 8.0)),
        )
        return result(True, json.dumps(value, ensure_ascii=False))
except (OSError, ValueError, TypeError, TimeoutError, subprocess.SubprocessError, LspQueryError) as exc:
    return result(False, error=str(exc))
return _run_tool_without_runtime_extensions({forwarded})'''
    wrap_function(path, "run_tool", "_run_tool_without_runtime_extensions", body)


def patch_worker_permissions_and_registry() -> None:
    path = "src/cambium/worker.py"
    add_import(path, "from cambium.tool_runtime import registry_from_schemas")
    text = read(path)
    if "def _bind_checkpoint_router(" not in text:
        marker = "\ndef _bind_router_provider("
        helper = '''
def _bind_checkpoint_router(router: Any, checkpoint: ContextCheckpoint, task_id: str) -> None:
    """Restore strict provider continuity from an immutable compatible epoch."""

    provider = checkpoint.cache_key.provider
    model = checkpoint.cache_key.model
    if isinstance(provider, str) and provider and isinstance(model, str) and model:
        binder = getattr(router, "bind_provider", None)
        if callable(binder):
            binder(provider, model, root_task_id=task_id)

'''
        if marker not in text:
            raise RuntimeError("worker router-binding marker not found")
        text = text.replace(marker, "\n" + helper + marker.lstrip("\n"), 1)
    if "_bind_checkpoint_router(router, resume_checkpoint, config.task_id)" not in text:
        marker = "        current_epoch_checkpoint = resume_checkpoint\n"
        if marker not in text:
            raise RuntimeError("worker resume checkpoint activation marker not found")
        text = text.replace(
            marker,
            "        _bind_checkpoint_router(router, resume_checkpoint, config.task_id)\n"
            + marker,
            1,
        )
    if "_bind_checkpoint_router(router, fork_checkpoint, config.task_id)" not in text:
        marker = "                current_epoch_checkpoint = fork_checkpoint\n"
        if marker not in text:
            raise RuntimeError("worker fork checkpoint activation marker not found")
        text = text.replace(
            marker,
            "                _bind_checkpoint_router(router, fork_checkpoint, config.task_id)\n"
            + marker,
            1,
        )
    replacements = (
        ("tools = TOOL_SCHEMAS", "tool_registry = registry_from_schemas(TOOL_SCHEMAS)\n    tools = list(tool_registry.schemas)"),
        ("tools = list(TOOL_SCHEMAS)", "tool_registry = registry_from_schemas(TOOL_SCHEMAS)\n    tools = list(tool_registry.schemas)"),
    )
    if "tool_registry = registry_from_schemas(TOOL_SCHEMAS)" not in text:
        changed = False
        for old, new in replacements:
            if old in text:
                text = text.replace(old, new, 1)
                changed = True
                break
        if not changed:
            raise RuntimeError("worker TOOL_SCHEMAS assignment not found")
    allow_shell_marker = 'allow_shell=bool(permissions.get("shell", False)),\n'
    if "allow_python=bool(permissions.get(\"python\", False))" not in text:
        if allow_shell_marker in text:
            text = text.replace(
                allow_shell_marker,
                allow_shell_marker
                + '        allow_python=bool(permissions.get("python", False)),\n',
                1,
            )
    write(path, text)


def patch_oneshot_and_cli_permissions() -> None:
    path = "src/cambium/oneshot.py"
    text = read(path)
    tree = ast.parse(text)
    config = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "OneShotConfig"),
        None,
    )
    if config is not None:
        fields = [node for node in config.body if isinstance(node, ast.AnnAssign)]
        names = {
            node.target.id for node in fields if isinstance(node.target, ast.Name)
        }
        if "allow_python" not in names:
            shell = next(
                (
                    node
                    for node in fields
                    if isinstance(node.target, ast.Name) and node.target.id == "allow_shell"
                ),
                None,
            )
            if shell is not None and shell.end_lineno is not None:
                lines = text.splitlines(keepends=True)
                lines.insert(shell.end_lineno, "    allow_python: bool = False\n")
                text = "".join(lines)
    if '"python": config.allow_python' not in text:
        for marker in (
            '"shell": config.allow_shell,\n',
            '"allow_shell": config.allow_shell,\n',
        ):
            if marker in text:
                key = "python" if marker.startswith('"shell"') else "allow_python"
                text = text.replace(marker, marker + f'            "{key}": config.allow_python,\n', 1)
                break
    write(path, text)
    path = "src/cambium/cli.py"
    text = read(path)
    if '"--allow-python"' not in text:
        tree = ast.parse(text)
        insertion = None
        parser_name = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "add_argument" or not node.args:
                continue
            if isinstance(node.args[0], ast.Constant) and node.args[0].value == "--allow-shell":
                insertion = node.end_lineno
                parser_name = ast.unparse(node.func.value)
                break
        if insertion is not None and parser_name is not None:
            lines = text.splitlines(keepends=True)
            lines.insert(
                insertion,
                f'    {parser_name}.add_argument(\n'
                '        "--allow-python",\n'
                '        action="store_true",\n'
                '        help="allow bounded isolated Python scratch snippets",\n'
                '    )\n',
            )
            text = "".join(lines)
    if "allow_python=args.allow_python" not in text:
        for marker in (
            "allow_shell=args.allow_shell,\n",
            "allow_shell=getattr(args, \"allow_shell\", False),\n",
        ):
            if marker in text:
                text = text.replace(
                    marker,
                    marker.replace("\n", "")
                    + ",\n"
                    + "        allow_python=getattr(args, \"allow_python\", False),\n",
                    1,
                )
                break
    write(path, text)


def patch_native_request_bodies() -> None:
    path = "src/cambium/diffundo.py"
    add_import(path, "from .tool_runtime import normalize_native_tool_schemas")
    text = read(path)
    tree = ast.parse(text)
    targets: list[tuple[str, str | None, bool, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lowered = node.name.lower()
            if "request_body" in lowered and ("chat" in lowered or "codex" in lowered):
                args = [arg.arg for arg in [*node.args.posonlyargs, *node.args.args]]
                prompt = next((name for name in args if name == "prompt"), None)
                if prompt:
                    targets.append((node.name, None, "codex" in lowered, prompt))
        elif isinstance(node, ast.ClassDef):
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                lowered = method.name.lower()
                if "request_body" in lowered and ("chat" in lowered or "codex" in lowered):
                    args = [arg.arg for arg in [*method.args.posonlyargs, *method.args.args]]
                    prompt = next((name for name in args if name == "prompt"), None)
                    if prompt:
                        targets.append((method.name, node.name, "codex" in lowered, prompt))
    if not targets and "normalize_native_tool_schemas(" not in text:
        raise RuntimeError("no prompt-aware chat/codex request body builder found")
    for name, class_name, responses_api, prompt in targets:
        renamed = name + "_without_registry_tools"
        node = function_node(path, name, class_name)
        forwarded = _forward_arguments(node)
        receiver = "self." if class_name is not None else ""
        body = f'''body = {receiver}{renamed}({forwarded})
if isinstance({prompt}, Mapping):
    schemas = {prompt}.get("tools")
    if isinstance(schemas, Sequence) and not isinstance(schemas, (str, bytes)) and schemas:
        body["tools"] = normalize_native_tool_schemas(
            [schema for schema in schemas if isinstance(schema, Mapping)],
            responses_api={responses_api!r},
        )
        body["tool_choice"] = "auto"
        {"body[\"parallel_tool_calls\"] = False" if responses_api else ""}
return body'''
        wrap_function(path, name, renamed, body, class_name=class_name)


def patch_docs() -> None:
    path = "docs/architecture/architecture.md"
    text = read(path)
    if "### Hybrid tool runtime" not in text:
        text += '''

### Hybrid tool runtime

Tools are exposed through an immutable task-local registry. The registry owns
schema identity and provider-native normalization; execution remains capability
gated. The default set deliberately stays small:

- structured read/write/shell tools for reproducible repository operations;
- `symbol_search`, `find_references`, and `read_symbol` for compact portable
  navigation;
- optional `lsp_query` when the operator configures a trusted language-server
  command;
- `run_python` for short one-off transformations under a separate explicit
  permission, isolated from site packages and credential environment.

Python snippets complement rather than replace structured tools. They are best
for transient calculations and data reshaping; file mutation, shell execution,
navigation, and diagnostics retain typed boundaries and bounded output.
'''
        write(path, text)
    path = "docs/architecture/context-engine.md"
    text = read(path)
    if "Provider lease restoration" not in text:
        text += '''

## Provider lease restoration

A compatible immutable context checkpoint carries its provider/model boundary.
Resume and exact cache-compatible fork activation restore that boundary into the
router before the next provider call. Provider-neutral semantic-summary reuse is
intentionally cold and does not restore the parent lease.
'''
        write(path, text)
    path = "docs/architecture/user-cli.md"
    text = read(path)
    if "--allow-python" not in text:
        text += '''

Python scratch execution is disabled unless the operator passes
`--allow-python` or a plan explicitly grants `permissions.python`. The command
runs `python -I -S` with a credential-free environment and bounded output; this
is process hygiene, not an OS sandbox.
'''
        write(path, text)


def write_tests() -> None:
    write(
        "tests/scenarios/test_tool_runtime_v5.py",
        '''from __future__ import annotations

import json
from pathlib import Path

import pytest

from cambium.code_index import find_references, read_symbol, search_symbols
from cambium.lsp_query import LspQueryError, query_lsp
from cambium.tool_runtime import (
    ToolCapability,
    ToolDefinition,
    ToolRegistry,
    normalize_native_tool_schemas,
    registry_from_schemas,
)


def _definition(name: str) -> ToolDefinition:
    return ToolDefinition(
        name,
        f"tool {name}",
        {"type": "object", "properties": {}, "additionalProperties": False},
        ToolCapability.READ,
    )


def test_registry_is_frozen_ordered_and_rejects_duplicates() -> None:
    registry = ToolRegistry([_definition("a"), _definition("b")])
    assert [item.name for item in registry.definitions] == ["a", "b"]
    assert [item["function"]["name"] for item in registry.schemas] == ["a", "b"]
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([_definition("a"), _definition("a")])


def test_legacy_registry_and_native_schema_normalization() -> None:
    schemas = [{
        "type": "function",
        "function": {
            "name": "read",
            "description": "read",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    registry = registry_from_schemas(schemas)
    chat = normalize_native_tool_schemas(registry.schemas, responses_api=False)
    responses = normalize_native_tool_schemas(registry.schemas, responses_api=True)
    assert chat[0]["function"]["name"] == "read"
    assert responses[0]["name"] == "read"
    assert "function" not in responses[0]


def test_code_navigation_is_structured_bounded_and_path_safe(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text(
        "class Alpha:\n    pass\n\ndef beta():\n    return Alpha()\n",
        encoding="utf-8",
    )
    symbols = search_symbols(tmp_path, "Alpha", exact=True)
    refs = find_references(tmp_path, "Alpha")
    window = read_symbol(tmp_path, "module.py", 1, context_lines=3)
    assert symbols[0].kind == "class"
    assert len(refs) == 2
    assert window["start_line"] == 1
    with pytest.raises(ValueError, match="escapes"):
        read_symbol(tmp_path, "../outside.py", 1)


def test_lsp_query_is_operator_configured(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.delenv("CAMBIUM_LSP_COMMAND", raising=False)
    with pytest.raises(LspQueryError, match="not configured"):
        query_lsp(tmp_path, method="hover", path="a.py")


def test_navigation_schema_payload_is_json_serializable() -> None:
    from cambium.schemas import TOOL_SCHEMAS

    encoded = json.dumps(TOOL_SCHEMAS)
    assert "symbol_search" in encoded
    assert "lsp_query" in encoded
''',
    )
    write(
        "tests/scenarios/test_tool_runtime_source_v5.py",
        '''from __future__ import annotations

import ast
from pathlib import Path

from cambium.tools import ToolPermissionPolicy


def _source(name: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / "src" / "cambium" / name).read_text(encoding="utf-8")


def test_python_permission_is_explicit() -> None:
    assert hasattr(ToolPermissionPolicy, "__dataclass_fields__")
    assert "allow_python" in ToolPermissionPolicy.__dataclass_fields__
    assert ToolPermissionPolicy.__dataclass_fields__["allow_python"].default is False


def test_resume_and_compatible_fork_restore_provider_lease() -> None:
    source = _source("worker.py")
    assert "_bind_checkpoint_router(router, resume_checkpoint" in source
    assert "_bind_checkpoint_router(router, fork_checkpoint" in source


def test_worker_uses_immutable_tool_registry() -> None:
    source = _source("worker.py")
    assert "registry_from_schemas(TOOL_SCHEMAS)" in source


def test_diffundo_request_builders_have_native_tool_normalization() -> None:
    source = _source("diffundo.py")
    assert "normalize_native_tool_schemas" in source
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "normalize_native_tool_schemas"
    ]
    assert calls
''',
    )


def main() -> None:
    append_schemas()
    patch_tool_permissions()
    patch_worker_permissions_and_registry()
    patch_oneshot_and_cli_permissions()
    patch_native_request_bodies()
    patch_docs()
    write_tests()


if __name__ == "__main__":
    main()
