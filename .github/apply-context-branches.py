# ruff: noqa: E501  # long lines are byte-exact patch anchors, must not wrap
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str, *, label: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    write(path, text.replace(old, new, 1))


def replace_between(path: str, start: str, end: str, replacement: str, *, label: str) -> None:
    text = read(path)
    begin = text.find(start)
    finish = text.find(end, begin + len(start))
    if begin < 0 or finish < 0:
        raise RuntimeError(f"{label}: section markers not found")
    write(path, text[:begin] + replacement + text[finish:])


def insert_after(path: str, anchor: str, text_to_insert: str, *, label: str) -> None:
    text = read(path)
    index = text.find(anchor)
    if index < 0:
        raise RuntimeError(f"{label}: anchor not found")
    index += len(anchor)
    write(path, text[:index] + text_to_insert + text[index:])


def patch_schemas() -> None:
    path = "src/cambium/schemas.py"
    history_schema = '''    {
        "name": "branch_history",
        "description": (
            "Inspect the current session's task branches, historical tool calls, or a "
            "branch transcript without creating a second memory database. Tool references "
            "are stable branch/generation/turn identifiers. Use branches first, tools to "
            "list calls, tool to reopen one call, and transcript only when the summaries "
            "do not contain enough detail. Results are appended to the working tail; the "
            "cached trunk remains unchanged."
        ),
        "parameters": _parameters(
            {
                "action": {
                    "type": "string",
                    "enum": ["branches", "tools", "tool", "transcript"],
                    "description": "The history projection to read.",
                },
                "task_id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional task branch id; required for transcript.",
                },
                "ref": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Stable tool:<task>:<generation>:<turn> reference.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based row/message offset.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 64,
                    "description": "Maximum rows/messages returned.",
                },
            },
            ["action"],
        ),
    },
'''
    replace_once(
        path,
        '    {\n        "name": "delegate",',
        history_schema + '    {\n        "name": "delegate",',
        label="branch history schema",
    )
    replace_once(
        path,
        '''        "description": (
            "Propose one scoped child workload for work you should not do yourself — a "
            "separable subproblem with its own files or investigation area. The child is a "
            "full Cambium worker in an isolated git worktree. IMPORTANT: this call only "
            "PROPOSES the child. With context reuse, a successful proposal suspends this "
            "task; the supervisor may later resume it with a bounded child-result envelope. "
            "Without context reuse, child admission waits for this task's terminal boundary. "
            "Make spec.task self-contained. An exact compatible child may receive the "
            "immutable checkpoint prefix; otherwise it receives semantic summaries. It never "
            "receives sibling context or hidden reasoning."
        ),
''',
        '''        "description": (
            "Propose one recursive Cambium branch for a separable workload. The child is the "
            "same worker/session structure as its parent. Declare context_mode and placement "
            "explicitly: trunk+inherit is the normal cache-affine choice and receives the "
            "complete immutable trunk; semantic+spread starts from summaries and prefers "
            "another feasible provider for throughput; fresh+spread is an independent blind "
            "run. With context reuse, the parent suspends and later resumes with the bounded "
            "child result. Make spec.task self-contained and include objective, ownership, "
            "done criteria, and verification."
        ),
''',
        label="delegate description",
    )
    anchor = '''                        "requirements": {
                            "type": "object",
'''
    policy_properties = '''                        "context_mode": {
                            "type": "string",
                            "enum": ["trunk", "semantic", "fresh"],
                            "description": (
                                "trunk reuses the complete exact parent checkpoint; semantic "
                                "uses immutable summaries under a fresh head; fresh inherits "
                                "no parent conversation."
                            ),
                        },
                        "placement": {
                            "type": "string",
                            "enum": ["inherit", "spread"],
                            "description": (
                                "inherit preserves provider affinity; spread asks admission to "
                                "prefer another feasible provider. trunk requires inherit."
                            ),
                        },
'''
    replace_once(path, anchor, policy_properties + anchor, label="delegate policy properties")
    text = read(path)
    delegate = text.find('        "name": "delegate"')
    required = text.find('                    "required": ["task"],', delegate)
    if delegate < 0 or required < 0:
        raise RuntimeError("delegate required fields not found")
    text = (
        text[:required]
        + '                    "required": ["task", "context_mode", "placement"],'
        + text[required + len('                    "required": ["task"],') :]
    )
    write(path, text)


def patch_tools() -> None:
    path = "src/cambium/tools.py"
    replace_once(
        path,
        "from .auth import scrub_environment\n",
        "from .auth import scrub_environment\n"
        "from .branch_history import BranchHistoryError, query_branch_history\n"
        "from .child_policy import ChildPolicyError, parse_child_policy\n",
        label="tool policy imports",
    )
    replacement = '''async def _branch_history(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    """Read branch/session history through the existing event/checkpoint authority."""
    session_id = os.environ.get("CAMBIUM_SESSION_ID")
    if not session_id:
        raise _ToolFailure("branch_history requires a supervised Cambium session")
    try:
        output = await asyncio.to_thread(query_branch_history, session_id, args)
    except (BranchHistoryError, OSError) as exc:
        raise _ToolFailure(str(exc)) from exc
    return _Outcome(ok=True, output=output)


async def _delegate(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    """Register one explicit recursive child-branch proposal."""
    child_task_id = args["child_task_id"]
    kind = args["kind"]
    if kind not in _ALLOWED_TASK_KINDS:
        return _Outcome(
            ok=False,
            error=(
                f"validation failed: unknown task kind {kind} (allowed: {_ALLOWED_TASK_KINDS_TEXT})"
            ),
        )
    try:
        policy = parse_child_policy(args["spec"])
    except ChildPolicyError as exc:
        return _Outcome(ok=False, error=f"validation failed: {exc}")
    return _Outcome(
        ok=True,
        output=(
            f"child {child_task_id} proposed; context={policy.context_mode.value} "
            f"placement={policy.placement.value}; supervisor admission follows"
        ),
    )


'''
    replace_between(
        path,
        "async def _delegate(args: dict[str, Any], ctx: ToolContext) -> _Outcome:\n",
        "TOOL_DISPATCH: dict[str, ToolImplementation] = {\n",
        replacement,
        label="tool delegate/history implementation",
    )
    replace_once(
        path,
        'TOOL_DISPATCH: dict[str, ToolImplementation] = {\n    "delegate": _delegate,\n',
        'TOOL_DISPATCH: dict[str, ToolImplementation] = {\n'
        '    "branch_history": _branch_history,\n'
        '    "delegate": _delegate,\n',
        label="branch history dispatch",
    )


def patch_prompts() -> None:
    path = "src/cambium/prompts.py"
    replace_once(path, "PROMPTS_VERSION = 1", "PROMPTS_VERSION = 2", label="prompt version")
    anchor = 'SEMANTIC_SUMMARIZER = "\\n".join(SUMMARY_PROTOCOL_LINES)\n'
    components = '''

BRANCH_DECISION_POLICY = "\\n".join(
    (
        "Recursive branch policy:",
        "- Continue in the current branch when the next work depends tightly on its live "
        "working tail or is too small to repay delegation overhead.",
        "- When delegating, choose context_mode and placement explicitly.",
        "- Prefer context_mode=trunk, placement=inherit when the child benefits from the "
        "parent state. The complete trunk is intentionally the normal choice: it is small, "
        "information-dense, and usually cache-cheap on the current provider.",
        "- Use context_mode=semantic, placement=spread for an independently executable task "
        "whose immutable summaries are sufficient; spread asks routing to prefer another "
        "feasible provider and increase subscription throughput.",
        "- Use context_mode=fresh, placement=spread for blind review, independent reproduction, "
        "or when inherited conclusions would contaminate the task.",
        "- A child is the same recursive branch structure as its parent and may delegate again.",
    )
)

BRANCH_HISTORY_POLICY = "\\n".join(
    (
        "Historical context policy:",
        "- The growing semantic trunk is normal cached history. Do not replace or rewrite it "
        "to inspect detail.",
        "- Use branch_history only when the trunk or child result lacks evidence needed for "
        "the current decision.",
        "- Call action=branches to discover task branches, action=tools to obtain stable tool "
        "references, action=tool to reopen one call, and action=transcript only for broader "
        "branch reconstruction.",
        "- Retrieved history is a temporary tool-result suffix. Draw conclusions from it, then "
        "promote only durable decisions/facts through the normal summary trunk.",
    )
)
'''
    replace_once(path, anchor, anchor + components, label="prompt policy components")
    replace_once(
        path,
        '        "Approach:",\n',
        '        BRANCH_DECISION_POLICY,\n'
        '        BRANCH_HISTORY_POLICY,\n'
        '        "Approach:",\n',
        label="compose prompt policies",
    )
    replace_once(
        path,
        '''        "- For a scoped subtask, propose a child with the delegate tool; a supervisor admits it "
        "after your task reaches its terminal boundary.",
''',
        '''        "- For a separable subtask, use delegate with an explicit branch policy; with context "
        "reuse the current task suspends while the child runs, then resumes with its result.",
''',
        label="delegate prompt bullet",
    )
    replace_once(
        path,
        '    "CODING_AGENT",\n',
        '    "BRANCH_DECISION_POLICY",\n'
        '    "BRANCH_HISTORY_POLICY",\n'
        '    "CODING_AGENT",\n',
        label="prompt exports",
    )


def patch_supervisor() -> None:
    path = "src/cambium/supervisor.py"
    replace_once(
        path,
        "from .auth import MIN_API_KEY_BYTES, AuthStore, oauth_env_suffix, scrub_environment\n",
        "from .auth import MIN_API_KEY_BYTES, AuthStore, oauth_env_suffix, scrub_environment\n"
        "from .child_policy import ContextMode, Placement, parse_child_policy\n",
        label="supervisor child policy import",
    )
    replace_once(
        path,
        '''        try:
            proposal, budget_decision = _prepare_child_budget(parent_spec, proposal)
        except ValueError as exc:
''',
        '''        try:
            proposal, budget_decision = _prepare_child_budget(parent_spec, proposal)
            child_policy = parse_child_policy(proposal.get("spec", {}))
        except ValueError as exc:
''',
        label="child policy admission parse",
    )
    replace_once(
        path,
        '''            child_spec = _child_spec(self._session_dir, parent_spec, proposal, parent_envelope)
            if private_integration_base is not None:
''',
        '''            child_spec = _child_spec(self._session_dir, parent_spec, proposal, parent_envelope)
            child_spec["context_mode"] = child_policy.context_mode.value
            child_spec["placement"] = child_policy.placement.value
            self._validate_child_context_policy(child_spec, parent_task_id)
            if private_integration_base is not None:
''',
        label="child policy context preflight",
    )
    replace_once(
        path,
        '''                "child_kind": kind,
                "branch": child_spec.get("branch"),
            }
''',
        '''                "child_kind": kind,
                "branch": child_spec.get("branch"),
                "context_mode": child_policy.context_mode.value,
                "placement": child_policy.placement.value,
            }
''',
        label="child admission policy event",
    )

    method = '''    def _parent_context_provider(self, parent_task_id: str) -> tuple[str | None, str | None]:
        """Return the parent provider/model from its epoch or admitted spec."""
        epoch = self._task_epochs.get(parent_task_id)
        cache_key = epoch.get("cache_key") if isinstance(epoch, dict) else None
        if isinstance(cache_key, dict):
            provider = cache_key.get("provider")
            model = cache_key.get("model")
            return (
                provider if isinstance(provider, str) and provider else None,
                model if isinstance(model, str) and model else None,
            )
        parent = self._session_spec(parent_task_id)
        if parent is None:
            return None, None
        provider = parent.get("assigned_provider")
        fanout = parent.get("fanout_config")
        model = fanout.get("model") if isinstance(fanout, dict) else None
        return (
            provider if isinstance(provider, str) and provider else None,
            model if isinstance(model, str) and model else None,
        )

    def _validate_child_context_policy(
        self, child_spec: dict[str, Any], parent_task_id: str
    ) -> None:
        """Reject a requested context representation before durable admission."""
        policy = parse_child_policy(child_spec)
        if policy.context_mode is ContextMode.FRESH:
            return
        if not self._context_reuse:
            raise ValueError(
                f"child context_mode={policy.context_mode.value} requires context reuse"
            )
        epoch = self._task_epochs.get(parent_task_id)
        if epoch is None:
            raise ValueError(
                f"child context_mode={policy.context_mode.value} requires a parent checkpoint"
            )
        cache_key = epoch.get("cache_key")
        if policy.context_mode is ContextMode.SEMANTIC:
            if (
                not isinstance(cache_key, dict)
                or cache_key.get("redacted") is not False
                or not isinstance(epoch.get("checkpoint_ref"), str)
            ):
                raise ValueError("semantic child requires a non-redacted parent checkpoint")
            return
        authorized = frozenset(child_spec.get("authorized_providers") or ())
        compatible, reason = _fork_cache_compatible_supervisor(child_spec, epoch, authorized)
        if not compatible:
            raise ValueError(f"trunk child requires an exact compatible checkpoint: {reason}")

    def _set_child_provider_affinity(
        self,
        child_spec: dict[str, Any],
        parent_task_id: str,
        placement: Placement,
    ) -> None:
        """Apply provider affinity independently of the context representation."""
        provider, model = self._parent_context_provider(parent_task_id)
        fanout = child_spec.get("fanout_config")
        if not isinstance(fanout, dict):
            fanout = child_spec["fanout_config"] = {}
        if placement is Placement.SPREAD:
            child_spec.pop("assigned_provider", None)
            fanout.pop("provider", None)
            fanout.pop("assigned_provider", None)
            # Keep an explicit child model when requested. Otherwise remove an
            # inherited pin so admission can choose another provider/model.
            if child_spec.get("model_candidates"):
                fanout.pop("model", None)
            if provider is not None:
                child_spec["spread_from_provider"] = provider
            return
        if provider is None:
            return
        child_spec["assigned_provider"] = provider
        if model is not None:
            fanout["model"] = model
        lane = self._lanes.get(provider)
        if lane is not None and not child_spec.get("_lane_reserved", False):
            lane.in_flight += 1
            child_spec["_lane_reserved"] = True

    async def _pin_fork_child(
        self,
        child_spec: dict[str, Any],
        parent_task_id: str,
        child_task_id: str,
        kind: str | None,
    ) -> None:
        """Materialize the explicitly requested child context and placement."""
        policy = parse_child_policy(child_spec)
        epoch = self._task_epochs.get(parent_task_id)
        cache_key = epoch.get("cache_key") if isinstance(epoch, dict) else None
        child_spec.pop("context_fork", None)
        child_spec.pop("summary_trunk_ref", None)
        self._set_child_provider_affinity(child_spec, parent_task_id, policy.placement)

        compatible = False
        semantic_reuse = False
        reason: str | None = None
        if policy.context_mode is ContextMode.TRUNK:
            authorized = frozenset(child_spec.get("authorized_providers") or ())
            compatible, reason = _fork_cache_compatible_supervisor(
                child_spec, epoch or {}, authorized
            )
        elif policy.context_mode is ContextMode.SEMANTIC:
            semantic_reuse = True

        payload: dict[str, Any] = {
            "parent_task_id": parent_task_id,
            "child_task_id": child_task_id,
            "child_kind": kind,
            "epoch": epoch.get("epoch") if isinstance(epoch, dict) else None,
            "context_mode": policy.context_mode.value,
            "placement": policy.placement.value,
            "compatible": compatible,
            "semantic_reuse": semantic_reuse,
        }
        if reason is not None:
            payload["reason"] = reason
        await self.emit("context_fork", task_id=parent_task_id, **payload)

        if policy.context_mode is ContextMode.FRESH:
            return
        if policy.context_mode is ContextMode.SEMANTIC:
            child_spec["summary_trunk_ref"] = epoch["checkpoint_ref"]
            return
        descriptor = {
            "checkpoint_ref": epoch["checkpoint_ref"],
            "provider": cache_key["provider"],
            "model": cache_key["model"],
            "system_sha256": cache_key["system_sha256"],
            "tools_sha256": cache_key["tools_sha256"],
            "prefix_sha256": cache_key["prefix_sha256"],
            "suffix_sha256": cache_key["suffix_sha256"],
            "full_sha256": cache_key["full_sha256"],
            "prefix_bytes": cache_key["prefix_bytes"],
            "provider_boundary": cache_key["provider_boundary"],
        }
        child_spec["context_fork"] = descriptor

'''
    replace_between(
        path,
        "    async def _pin_fork_child(\n",
        "    async def _record_revision_conversation(\n",
        method,
        label="policy-aware child context materialization",
    )


def patch_tests() -> None:
    delegate_test = '''from __future__ import annotations

from typing import Any, cast

import pytest

from cambium.schemas import TOOL_SCHEMAS, validate_tool_call


def _schema(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], next(schema for schema in TOOL_SCHEMAS if schema["name"] == name))


def _child_spec(**extra: Any) -> dict[str, Any]:
    return {
        "task": "Inspect routing; own no files; report violations and reproductions.",
        "context_mode": "trunk",
        "placement": "inherit",
        **extra,
    }


def test_delegate_schema_requires_workload_context_and_placement() -> None:
    errors = validate_tool_call(
        _schema("delegate"),
        {"child_task_id": "child-review", "kind": "investigation", "spec": {}},
    )

    assert errors == [
        "validation failed: missing 'spec.task' (string)",
        "validation failed: missing 'spec.context_mode' (string)",
        "validation failed: missing 'spec.placement' (string)",
    ]


def test_delegate_schema_accepts_explicit_provider_constraints() -> None:
    errors = validate_tool_call(
        _schema("delegate"),
        {
            "child_task_id": "child-review",
            "kind": "investigation",
            "spec": _child_spec(
                requirements={"quality": "strong"},
                model_candidates=["gpt-5.6", "claude-opus"],
                authorized_providers=["openai", "anthropic"],
                authorized_providers_explicit=True,
                child_only=True,
            ),
        },
    )

    assert errors == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("max_turns", 0, ">= 1"),
        ("max_turns", -1, ">= 1"),
        ("max_turns", "two", "integer"),
        ("max_wall_s", 0, ">= 30"),
        ("max_wall_s", -1, ">= 30"),
        ("max_wall_s", "slow", "integer"),
    ],
)
def test_delegate_schema_rejects_invalid_budget_values(
    field: str, value: Any, expected: str
) -> None:
    errors = validate_tool_call(
        _schema("delegate"),
        {
            "child_task_id": "child-review",
            "kind": "investigation",
            "spec": _child_spec(**{field: value}),
        },
    )

    assert errors == [f"validation failed: 'spec.{field}' must be {expected}"]


def test_branch_history_schema_is_small_and_reference_based() -> None:
    schema = _schema("branch_history")

    assert validate_tool_call(schema, {"action": "branches"}) == []
    assert validate_tool_call(
        schema,
        {"action": "tool", "ref": "tool:child:1:2"},
    ) == []
    assert validate_tool_call(schema, {"action": "tools", "limit": 65}) == [
        "validation failed: 'limit' must be <= 64"
    ]
'''
    write("tests/scenarios/test_delegate_schema.py", delegate_test)

    path = "tests/scenarios/test_supervisor_delegation_contracts.py"
    replace_once(
        path,
        '''            "base_commit": "base",
        },
''',
        '''            "base_commit": "base",
            "context_mode": "fresh",
            "placement": "spread",
        },
''',
        label="supervisor proposal helper policy",
    )

    prompt_test = "tests/scenarios/test_prompts.py"
    text = read(prompt_test)
    text += '''


def test_branch_policy_components_are_named_and_composed() -> None:
    from cambium.prompts import (
        BRANCH_DECISION_POLICY,
        BRANCH_HISTORY_POLICY,
        CODING_AGENT,
        PROMPTS_VERSION,
    )

    assert PROMPTS_VERSION >= 2
    assert "context_mode=trunk, placement=inherit" in BRANCH_DECISION_POLICY
    assert "context_mode=semantic, placement=spread" in BRANCH_DECISION_POLICY
    assert "context_mode=fresh, placement=spread" in BRANCH_DECISION_POLICY
    assert "branch_history" in BRANCH_HISTORY_POLICY
    assert BRANCH_DECISION_POLICY in CODING_AGENT
    assert BRANCH_HISTORY_POLICY in CODING_AGENT
'''
    write(prompt_test, text)


patch_schemas()
patch_tools()
patch_prompts()
patch_supervisor()
patch_tests()
