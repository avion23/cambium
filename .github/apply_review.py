from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def remove_between(text: str, start: str, end: str, *, label: str) -> str:
    start_index = text.find(start)
    end_index = text.find(end, start_index + len(start))
    if start_index < 0 or end_index < 0:
        raise RuntimeError(f"{label}: section markers not found")
    return text[:start_index] + end + text[end_index + len(end) :]


CONTEXT_POLICY = '''"""Typed policy for bounded CAST semantic projections.

The policy is intentionally provider-neutral. It decides only whether an active
semantic trunk has exceeded a declared resource bound and whether a deterministic
K0 projection restored that bound. Provider cache TTLs, prices, and transport
capabilities belong to provider configuration, not to this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_SEGMENTS = 16


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class CastPolicy:
    """Hard bounds for one active CAST trunk.

    Bounds are inclusive: rollover becomes due only after a value exceeds its
    configured maximum. Zero disables that dimension. ``max_segments`` is
    enabled by default because an unbounded append-only projection eventually
    recreates the long-context problem CAST is intended to avoid.
    """

    max_segments: int = DEFAULT_MAX_SEGMENTS
    max_trunk_tokens: int = 0
    min_rollover_savings_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "max_segments",
            "max_trunk_tokens",
            "min_rollover_savings_tokens",
        ):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CastPolicy":
        allowed = {
            "max_segments",
            "max_trunk_tokens",
            "min_rollover_savings_tokens",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"unknown CAST policy field(s): {unknown}")
        return cls(**dict(value))

    def rollover_due(self, segment_count: int, active_trunk_tokens: int) -> bool:
        segments = _non_negative_int(segment_count, "segment_count")
        tokens = _non_negative_int(active_trunk_tokens, "active_trunk_tokens")
        return bool(
            (self.max_segments and segments > self.max_segments)
            or (self.max_trunk_tokens and tokens > self.max_trunk_tokens)
        )

    def validate_rollover(
        self,
        *,
        before_segments: int,
        before_tokens: int,
        after_segments: int,
        after_tokens: int,
    ) -> None:
        """Fail closed unless a due rollover restores every hard bound."""
        before_segment_count = _non_negative_int(before_segments, "before_segments")
        before_token_count = _non_negative_int(before_tokens, "before_tokens")
        after_segment_count = _non_negative_int(after_segments, "after_segments")
        after_token_count = _non_negative_int(after_tokens, "after_tokens")
        if not self.rollover_due(before_segment_count, before_token_count):
            raise ValueError("CAST rollover was requested while the trunk was within policy")
        if self.rollover_due(after_segment_count, after_token_count):
            raise ValueError("CAST K0 rollover did not restore the configured trunk bounds")
        if (
            self.min_rollover_savings_tokens
            and before_token_count - after_token_count < self.min_rollover_savings_tokens
        ):
            raise ValueError("CAST K0 rollover did not meet the minimum token saving")


__all__ = ["CastPolicy"]
'''

CONTEXT_POLICY_TEST = '''from __future__ import annotations

import pytest

from cambium.context_policy import CastPolicy


def test_default_policy_bounds_segment_growth() -> None:
    policy = CastPolicy()
    assert policy.rollover_due(16, 1_000) is False
    assert policy.rollover_due(17, 1_000) is True


def test_policy_mapping_is_strict_and_boolean_safe() -> None:
    policy = CastPolicy.from_mapping(
        {
            "max_segments": 4,
            "max_trunk_tokens": 8_000,
            "min_rollover_savings_tokens": 100,
        }
    )
    assert policy.rollover_due(5, 1) is True
    assert policy.rollover_due(4, 8_001) is True
    with pytest.raises(ValueError, match="unknown CAST"):
        CastPolicy.from_mapping({"max_segments": 4, "ttl": 60})
    with pytest.raises(ValueError, match="non-negative integer"):
        CastPolicy(max_segments=True)  # type: ignore[arg-type]


def test_zero_thresholds_disable_automatic_rollover() -> None:
    policy = CastPolicy(max_segments=0, max_trunk_tokens=0)
    assert policy.rollover_due(1_000_000, 1_000_000_000) is False


def test_rollover_validation_requires_restored_bounds() -> None:
    policy = CastPolicy(max_segments=2, max_trunk_tokens=100)
    policy.validate_rollover(
        before_segments=3,
        before_tokens=90,
        after_segments=1,
        after_tokens=80,
    )
    with pytest.raises(ValueError, match="did not restore"):
        policy.validate_rollover(
            before_segments=3,
            before_tokens=120,
            after_segments=1,
            after_tokens=110,
        )


def test_optional_savings_floor_is_enforced() -> None:
    policy = CastPolicy(max_segments=1, min_rollover_savings_tokens=10)
    with pytest.raises(ValueError, match="minimum token saving"):
        policy.validate_rollover(
            before_segments=2,
            before_tokens=100,
            after_segments=1,
            after_tokens=95,
        )
'''


def patch_provider_scheduler() -> None:
    path = "src/cambium/provider_scheduler.py"
    text = read(path)
    text = remove_between(
        text,
        "\n# CAST scheduling defaults are deliberately conservative.",
        "\n\nclass BillingMode(StrEnum):",
        label="remove misplaced CAST policy",
    )
    write(path, text)


def patch_worker() -> None:
    path = "src/cambium/worker.py"
    text = read(path)
    text = replace_once(
        text,
        """The agent is instructed to emit a short ``plan`` action before any
``tool_call``; the plan is kept in the transcript. The transcript is
summarized (truncation plus a synthetic dropped-message marker, no LLM call)
when it exceeds a character budget, so it stays bounded across turns.
""",
        """The agent is instructed to emit a short ``plan`` action before any
``tool_call``; the plan is kept in the transcript. With durable context reuse,
completed raw tails become strict immutable semantic deltas. A typed CAST policy
rolls an overgrown delta sequence into a deterministic K0 materialized view while
retaining an immutable rollover manifest outside the active prompt. Without
context reuse, the legacy bounded transcript projection remains available.
""",
        label="worker module documentation",
    )
    text = replace_once(
        text,
        "from collections.abc import Callable, Mapping\nfrom dataclasses import asdict, dataclass, replace\n",
        "from collections.abc import Callable, Mapping, Sequence\nfrom dataclasses import asdict, dataclass, field, replace\n",
        label="worker standard imports",
    )
    text = replace_once(
        text,
        "from cambium.auth import oauth_env_suffix, scrub_environment\n",
        "from cambium.auth import oauth_env_suffix, scrub_environment\nfrom cambium.context_policy import CastPolicy\n",
        label="worker policy import",
    )
    text = replace_once(
        text,
        """from cambium.summary_trunk import (
    SUMMARY_PROTOCOL_LINES,
    SummaryTrunkError,
    append_summary_entry,
    build_summary_request,
    parse_summary_response,
    partition_summary_trunk,
    semantic_summary_messages,
    summary_entries,
)
""",
        """from cambium.summary_trunk import (
    SUMMARY_PROTOCOL_LINES,
    SummaryEntry,
    SummaryTrunkError,
    append_summary_entry,
    build_summary_request,
    entry_mapping,
    parse_summary_response,
    partition_summary_trunk,
    rollover_summary_trunk,
    semantic_summary_messages,
    summary_entries,
    summary_trunk_tokens,
)
""",
        label="worker summary imports",
    )
    helper_marker = "\n\n@dataclass(frozen=True, slots=True)\nclass AgentConfig:"
    helper = '''\n\ndef _cast_policy(value: Any, source: str) -> CastPolicy:
    if value is None:
        return CastPolicy()
    if not isinstance(value, Mapping):
        raise ValueError(f"{source} cast_policy must be a mapping")
    try:
        return CastPolicy.from_mapping(value)
    except ValueError as exc:
        raise ValueError(f"{source} cast_policy: {exc}") from exc
'''
    text = replace_once(
        text,
        helper_marker,
        helper + helper_marker,
        label="worker policy parser",
    )
    text = replace_once(
        text,
        "    checkpoint_root: Path | None\n    python_permission: bool = False\n",
        "    checkpoint_root: Path | None\n    cast_policy: CastPolicy = field(default_factory=CastPolicy)\n    python_permission: bool = False\n",
        label="AgentConfig policy field",
    )
    text = replace_once(
        text,
        '''    def __post_init__(self) -> None:
        """Derive threshold defaults for direct in-process configurations."""
        threshold_high = self.rolling_compact_threshold_high
''',
        '''    def __post_init__(self) -> None:
        """Derive threshold defaults for direct in-process configurations."""
        if not isinstance(self.cast_policy, CastPolicy):
            raise ValueError("cast_policy must be a CastPolicy")
        threshold_high = self.rolling_compact_threshold_high
''',
        label="AgentConfig policy validation",
    )
    text = replace_once(
        text,
        "            checkpoint_root=checkpoint_root,\n            requirements=_task_requirements(init, _provider_fanout_config(init)),\n",
        "            checkpoint_root=checkpoint_root,\n            cast_policy=_cast_policy(init.get(\"cast_policy\"), \"init\"),\n            requirements=_task_requirements(init, _provider_fanout_config(init)),\n",
        label="init policy parse",
    )
    text = replace_once(
        text,
        '''    summary_trunk_ref = config.summary_trunk_ref or _validate_summary_trunk_ref(
        run.get("summary_trunk_ref")
    )
    rolling_compact = config.rolling_compact
''',
        '''    summary_trunk_ref = config.summary_trunk_ref or _validate_summary_trunk_ref(
        run.get("summary_trunk_ref")
    )
    cast_policy = config.cast_policy
    if "cast_policy" not in init and "cast_policy" in run:
        cast_policy = _cast_policy(run.get("cast_policy"), "run_task")
    rolling_compact = config.rolling_compact
''',
        label="merged policy precedence",
    )
    text = replace_once(
        text,
        "        checkpoint_root=config.checkpoint_root,\n        requirements=requirements,\n",
        "        checkpoint_root=config.checkpoint_root,\n        cast_policy=cast_policy,\n        requirements=requirements,\n",
        label="merged policy constructor",
    )
    text = replace_once(
        text,
        "        checkpoint_root=None,\n        requirements=_task_requirements(run, _provider_fanout_config(run)),\n",
        "        checkpoint_root=None,\n        cast_policy=_cast_policy(run.get(\"cast_policy\"), \"run_task\"),\n        requirements=_task_requirements(run, _provider_fanout_config(run)),\n",
        label="run policy parse",
    )
    manifest_marker = "\n\ndef _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:"
    manifest_helper = '''\n\ndef _write_cast_rollover_manifest(
    config: AgentConfig,
    k0: SummaryEntry,
    source_entries: Sequence[SummaryEntry],
) -> Path | None:
    """Durably retain the exact source projection before publishing K0.

    K0 is a materialized read view, not the authority. The manifest is written
    first with exclusive-create semantics so a crash cannot publish a compacted
    checkpoint whose source projection never became durable.
    """
    if config.checkpoint_root is None:
        return None
    raw: dict[str, Any] = {
        "schema": "cambium.cast-rollover.v1",
        "task_id": config.task_id,
        "generation": config.generation,
        "source_sha256": k0.source_sha256,
        "through_turn": k0.through_turn,
        "entries": [entry_mapping(entry) for entry in source_entries],
        "redacted": False,
    }
    redactor = config.redactor or _checkpoint_redactor(config.provider_env_keys)
    payload = cast(dict[str, Any], redactor.redact_mapping(raw))
    payload["redacted"] = payload != raw
    content = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    content_digest = _sha256_hex(content.encode("utf-8"))[:16]
    safe_task = _safe_task_id(config.task_id)
    path = (
        config.checkpoint_root
        / safe_task
        / "rollovers"
        / (
            f"k0-{k0.through_turn:06d}-{k0.source_sha256[:16]}-"
            f"{content_digest}.json"
        )
    )
    try:
        _create_epoch_checkpoint(path, content)
    except ContextForkError:
        try:
            info = path.lstat()
            existing = path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            raise
        if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and existing == content:
            return path
        raise
    return path
'''
    text = replace_once(
        text,
        manifest_marker,
        manifest_helper + manifest_marker,
        label="rollover manifest writer",
    )
    text = replace_once(
        text,
        '''        local_checkpoint = (
            current_epoch_checkpoint is not None
            and current_epoch_checkpoint.task_id == config.task_id
            and current_epoch_checkpoint.generation == config.generation
        )
        try:
''',
        '''        local_checkpoint = (
            current_epoch_checkpoint is not None
            and current_epoch_checkpoint.task_id == config.task_id
            and current_epoch_checkpoint.generation == config.generation
        )
        rollover_reason: str | None = None
        try:
''',
        label="rollover reason state",
    )
    text = replace_once(
        text,
        '''            summary_entry = parse_summary_response(summary_result.content, expectation)
            new_trunk = append_summary_entry(trunk_messages, summary_entry)
            checkpoint = await asyncio.to_thread(
''',
        '''            summary_entry = parse_summary_response(summary_result.content, expectation)
            new_trunk = append_summary_entry(trunk_messages, summary_entry)
            before_entries = summary_entries(new_trunk)
            before_tokens = summary_trunk_tokens(new_trunk)
            if config.cast_policy.rollover_due(len(before_entries), before_tokens):
                rolled_trunk, _projection, source_entries = rollover_summary_trunk(new_trunk)
                after_entries = summary_entries(rolled_trunk)
                after_tokens = summary_trunk_tokens(rolled_trunk)
                config.cast_policy.validate_rollover(
                    before_segments=len(before_entries),
                    before_tokens=before_tokens,
                    after_segments=len(after_entries),
                    after_tokens=after_tokens,
                )
                k0 = after_entries[0]
                await asyncio.to_thread(
                    _write_cast_rollover_manifest,
                    config,
                    k0,
                    source_entries,
                )
                new_trunk = rolled_trunk
                # K0 starts a new cache lineage. The next provider call must
                # account its complete prompt instead of subtracting the old
                # lineage's prompt length.
                previous_prompt_tokens = 0
                rollover_reason = "cast_k0_rollover"
            checkpoint = await asyncio.to_thread(
''',
        label="runtime rollover integration",
    )
    text = replace_once(
        text,
        '''                    checkpoint=checkpoint,
                    folded_from_epoch=prior_epoch,
                    reason=None,
                )
''',
        '''                    checkpoint=checkpoint,
                    folded_from_epoch=prior_epoch,
                    reason=rollover_reason,
                )
''',
        label="rollover event reason",
    )
    write(path, text)


def patch_tests() -> None:
    scheduler_path = "tests/scenarios/test_provider_scheduler.py"
    scheduler = read(scheduler_path)
    scheduler = replace_once(
        scheduler,
        '''    assert not hasattr(provider_state, "ProviderScheduler")
    assert not hasattr(provider_state, "rank_policies")
''',
        '''    assert not hasattr(provider_state, "ProviderScheduler")
    assert not hasattr(provider_state, "rank_policies")
    assert not hasattr(provider_state, "CastConfig")
    assert not hasattr(provider_state, "CacheHorizonConfig")
''',
        label="provider boundary regression test",
    )
    write(scheduler_path, scheduler)

    epochs_path = "tests/scenarios/test_context_epochs.py"
    epochs = read(epochs_path)
    epochs = replace_once(
        epochs,
        "from cambium import worker\nfrom cambium.diffundo import ProviderTier\n",
        "from cambium import worker\nfrom cambium.context_policy import CastPolicy\nfrom cambium.diffundo import ProviderTier\n",
        label="epoch policy import",
    )
    epochs = replace_once(
        epochs,
        "from cambium.redact import Redactor\n",
        "from cambium.redact import Redactor\nfrom cambium.summary_trunk import is_k0_entry, summary_entries\n",
        label="epoch K0 imports",
    )
    insertion_marker = "\n\ndef test_no_suspend_when_context_reuse_disabled(tmp_path: Path) -> None:"
    test = '''\n\ndef test_cast_rollover_is_durable_before_epoch_publication(tmp_path: Path) -> None:
    worktree = _make_worktree(tmp_path / "repo")
    checkpoint_root = tmp_path / "ckpts"
    config = _agent_config(
        worktree,
        checkpoint_root=checkpoint_root,
        context_reuse=True,
        rolling_compact_threshold_high=1,
        rolling_compact_threshold_low=1,
        cast_policy=CastPolicy(max_segments=1),
    )
    writer = _FakeWriter()
    router = _ScriptedRouter(
        [
            '{"type":"plan","steps":["inspect"]}',
            '{"type":"finish","summary":"done"}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router, writer))

    assert outcome["status"] == "succeeded"
    advanced = [
        message
        for message in writer.messages()
        if message["type"] == "context_epoch_advanced"
        and message.get("reason") == "cast_k0_rollover"
    ]
    assert advanced
    checkpoint = worker._load_epoch_checkpoint(
        config,
        advanced[-1]["checkpoint_ref"],
        expect_task_id=True,
    )
    entries = summary_entries(checkpoint.provider_messages)
    assert len(entries) == 1
    assert is_k0_entry(entries[0])
    manifests = list((checkpoint_root / config.task_id / "rollovers").glob("k0-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["schema"] == "cambium.cast-rollover.v1"
    assert manifest["source_sha256"] == entries[0].source_sha256
    assert len(manifest["entries"]) == 2
'''
    epochs = replace_once(
        epochs,
        insertion_marker,
        test + insertion_marker,
        label="runtime rollover integration test",
    )
    write(epochs_path, epochs)


write("src/cambium/context_policy.py", CONTEXT_POLICY)
write("tests/scenarios/test_context_policy.py", CONTEXT_POLICY_TEST)
patch_provider_scheduler()
patch_worker()
patch_tests()
