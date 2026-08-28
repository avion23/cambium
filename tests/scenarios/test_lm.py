"""Integration scenarios for the optional DSPy-to-Diffundo boundary."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from cambium.architectus import ArchitectusCore
from cambium.diffundo import CallResult, Diffundo, ProviderConfig, ProviderTier, _RawResponse
from cambium.lm import ArchitectusLM, CambiumLM
from cambium.tasktree import build_tree

if TYPE_CHECKING:
    dspy: Any


def _require_dspy() -> None:
    """Import dspy lazily: every dspy-consuming scenario is slow-tier, so the
    default fast run must not pay the ~2s dspy import at collection."""
    global dspy  # noqa: PLW0603
    try:
        import dspy  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        dspy = None  # type: ignore[assignment]
        pytest.skip("dspy extra is not installed")


def _tree(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))


class FakeDiffundo:
    def __init__(self, endpoint: str = "https://fake.invalid") -> None:
        self.endpoint = endpoint
        self.calls: list[dict[str, Any]] = []
        self.session_markers: list[object] = []

    async def call(
        self,
        tier: ProviderTier,
        prompt: dict[str, Any],
        *,
        model: str | None = None,
        budget_usd: float | None = None,
    ) -> CallResult:
        import dspy

        self.calls.append(
            {
                "endpoint": self.endpoint,
                "tier": tier,
                "prompt": prompt,
                "model": model,
                "budget_usd": budget_usd,
            }
        )
        self.session_markers.append(dspy.settings.cambium_session)
        content = "completion text"
        if prompt["messages"][0]["role"] == "system":
            content = '[{"action":"spawn","task_id":"root"}]'
        return CallResult(
            provider=self.endpoint,
            model=model or "fake-model",
            tier=tier,
            content=content,
            latency_s=0.01,
            usage={"prompt_tokens": 2, "completion_tokens": 2},
        )


def _call(lm: Any, prompt: str = "same prompt") -> list[str]:
    output = lm(messages=[{"role": "user", "content": prompt}])
    assert isinstance(output, list)
    return output


@pytest.mark.slow
def test_dspy_import_and_construction_do_not_write_home(tmp_path: Path) -> None:
    _require_dspy()
    source = Path(__file__).resolve().parents[2] / "src"
    real_cache = Path.home() / ".dspy_cache"
    real_cache_before_exists = real_cache.exists()
    real_cache_before = _tree(real_cache)
    script = """
import sys
from cambium.diffundo import ProviderTier
from cambium.lm import CambiumLM

fake_diffundo = type("FakeDiffundo", (), {"call": lambda *args, **kwargs: None})()
CambiumLM(fake_diffundo, ProviderTier.FAST)
import dspy
assert dspy.cache.enable_disk_cache is True
assert dspy.cache.enable_memory_cache is True
"""
    env = os.environ.copy()
    env.pop("DSPY_CACHEDIR", None)
    env.pop("DSPY_CACHE_LIMIT", None)
    env.pop("Py_GIL_DISABLED", None)
    env["HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(source)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert _tree(tmp_path) == ()
    assert real_cache.exists() is real_cache_before_exists
    assert _tree(real_cache) == real_cache_before


@pytest.mark.slow
def test_identical_calls_are_not_cached() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST, temperature=0.0)  # type: ignore[arg-type]
    assert _call(lm) == ["completion text"]
    assert _call(lm) == ["completion text"]
    assert len(diffundo.calls) == 2
    assert lm.cache is False
    assert lm.num_retries == 0
    assert lm.history == []
    assert all("cache" not in call["prompt"] for call in diffundo.calls)


def test_sync_lm_calls_can_cross_event_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synchronous DSPy seam may run one LM on many GEPA loops."""
    _require_dspy()
    payload = {
        "id": "chatcmpl-loop-local",
        "object": "chat.completion",
        "model": "m-loop-local",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "completion text"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2},
    }
    provider = ProviderConfig(
        name="p-loop-local",
        tier=ProviderTier.FAST,
        base_url="http://127.0.0.1:1",
        api_key_env="K_LOOP_LOCAL",
        api_key="sk-test-loop-local",
        model="m-loop-local",
        timeout_s=1.0,
        max_retries=0,
    )
    router = Diffundo((provider,), call_budget_s=1.0)

    def fake_post_sync(self, provider, prompt, timeout_s):
        del self, provider, prompt, timeout_s
        import time

        time.sleep(0.02)
        return _RawResponse(payload, 0.02)

    monkeypatch.setattr(Diffundo, "_post_sync", fake_post_sync)
    lm = CambiumLM(router, ProviderTier.FAST, model="m-loop-local")

    # Each synchronous call enters CambiumLM.forward, which creates its own
    # asyncio.run loop.  The sequential calls prove closed loops can be
    # discarded and recreated without retaining a loop-bound primitive.
    assert _call(lm, "sequential one") == ["completion text"]
    assert _call(lm, "sequential two") == ["completion text"]

    # GEPA can invoke the same synchronous LM from concurrent worker threads;
    # each worker creates another independent asyncio.run loop.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_call, lm, f"concurrent {index}") for index in range(2)]
        assert [future.result(timeout=5.0) for future in futures] == [
            ["completion text"],
            ["completion text"],
        ]


def test_session_context_is_isolated_and_retains_no_prompt() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    dspy = sys.modules["dspy"]
    original_marker = object()
    with dspy.context(cambium_session=original_marker):
        assert _call(lm, "PROMPT-CANARY") == ["completion text"]
        assert dspy.settings.cambium_session is original_marker
    assert not hasattr(dspy.settings, "cambium_session")
    assert diffundo.session_markers[0] is not original_marker
    assert lm.history == []


def test_architectus_real_decide_port_uses_cambium_lm() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    tree = build_tree(
        {
            "tasks": [
                {
                    "task_id": "root",
                    "kind": "FEATURE",
                    "depends_on": [],
                    "spec": {"goal": "deliver the feature"},
                }
            ]
        }
    )
    actions = asyncio.run(ArchitectusCore(ArchitectusLM(lm), tree=tree).step([{"kind": "tick"}]))
    assert actions == [{"action": "spawn", "task_id": "root"}]
    assert len(diffundo.calls) == 1


def test_core_module_imports_never_import_dspy() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import sys
import builtins

real_import = builtins.__import__
def reject_dspy(name, *args, **kwargs):
    if name == "dspy" or name.startswith("dspy."):
        raise AssertionError("core imports dspy eagerly")
    return real_import(name, *args, **kwargs)

builtins.__import__ = reject_dspy
sys.path.insert(0, sys.argv[1])
import cambium
import cambium.architectus
import cambium.diffundo
import cambium.lm
assert 'dspy' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(source)],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_cold_hostile_constructor_key_rejects_before_dspy_load() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import builtins
import sys

sys.path.insert(0, sys.argv[1])
import cambium.lm as lm_module
from cambium.diffundo import ProviderTier

assert "dspy" not in sys.modules
real_import = builtins.__import__
real_load = lm_module._load_dspy
load_calls = 0


def reject_dspy(name, *args, **kwargs):
    if name == "dspy" or name.startswith("dspy."):
        raise ImportError("blocked for cold-key regression")
    return real_import(name, *args, **kwargs)


def tracked_load():
    global load_calls
    load_calls += 1
    return real_load()


builtins.__import__ = reject_dspy
lm_module._load_dspy = tracked_load


class HostileKey(str):
    def __str__(self):
        return self


fake_diffundo = type("FakeDiffundo", (), {"call": lambda *args, **kwargs: None})()
outcome = None
try:
    lm_module.CambiumLM(
        fake_diffundo,
        ProviderTier.FAST,
        **{HostileKey("unexpected"): object()},
    )
except BaseException as exc:
    outcome = (type(exc).__name__, str(exc))

assert outcome == (
    "TypeError",
    "CambiumLM keyword keys must use exact builtin strings",
)
assert load_calls == 0
assert "dspy" not in sys.modules
assert lm_module._DSPY is None
assert "_CambiumLMImplementation" not in lm_module.__dict__
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(source)],
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_free_threaded_build_is_rejected_at_lm_import() -> None:
    source = Path(__file__).resolve().parents[2] / "src"
    script = "import sys; sys.path.insert(0, sys.argv[1]); import cambium.lm"
    env = os.environ.copy()
    env["Py_GIL_DISABLED"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script, str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "free-threaded CPython" in completed.stderr


@pytest.mark.parametrize(
    "key",
    [
        "auth",
        "AUTH",
        "bearer",
        "Bearer",
        "client_id",
        "client-id",
        "oauth",
        "session",
        "refresh",
        "access",
        "private",
        "pem",
        "passphrase",
        "credential",
        "CREDENTIAL",
        "api.key",
        "client_secret",
        "client-secret",
        "CLIENT-SECRET",
        "access_token",
        "ACCESS-TOKEN",
        "refresh_token",
        "refresh-token",
        "private_key",
        "PRIVATE-KEY",
        "session_key",
        "SESSION-KEY",
        "auth_token",
        "AUTH-TOKEN",
    ],
)
def test_credential_marker_variants_cannot_reach_dump_state(key: str) -> None:
    _require_dspy()
    with pytest.raises(ValueError, match="provider credentials"):
        CambiumLM(
            FakeDiffundo(),
            ProviderTier.FAST,
            **{key: "SENSITIVE_CANARY"},
        )  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["apiKey", "API-Key", "api_key", "api.key"])
def test_nested_extension_credential_keys_cannot_reach_dump_state(key: str) -> None:
    _require_dspy()
    with pytest.raises(ValueError, match="provider credentials"):
        CambiumLM(
            FakeDiffundo(),
            ProviderTier.FAST,
            extensions={"nested": [({key: "SENSITIVE_CANARY"},)]},
        )  # type: ignore[arg-type]


def test_post_construction_nested_mutation_cannot_reach_dump_state() -> None:
    _require_dspy()
    extensions: dict[str, Any] = {"nested": {}}
    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST, extensions=extensions)  # type: ignore[arg-type]

    extensions["nested"]["api_key"] = "SENSITIVE_CANARY"

    assert "SENSITIVE_CANARY" not in repr(lm.dump_state())
    assert "api_key" not in repr(lm.dump_state())
    copied_state = lm.copy().dump_state()
    assert "SENSITIVE_CANARY" not in repr(copied_state)
    assert "api_key" not in repr(copied_state)


def test_copy_rejects_credential_keys_and_keeps_dump_state_clean() -> None:
    _require_dspy()
    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="provider credentials"):
        lm.copy(**{"api.key": "SENSITIVE_CANARY"})

    copied = lm.copy(temperature=0.5)
    assert "SENSITIVE_CANARY" not in repr(copied.dump_state())
    assert "api.key" not in repr(copied.dump_state())


def test_predict_json_save_rejects_auth_bearer_credentials(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    predict = dspy.Predict("question -> answer")
    state_path = tmp_path / "state.json"

    predict.lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    cast(Any, predict.lm).kwargs["auth"] = "Bearer SENSITIVE_CANARY"

    with pytest.raises(ValueError, match="provider credentials"):
        predict.save(state_path)

    assert not state_path.exists()


def test_predict_json_save_rejects_byte_credential_keys(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    predict = dspy.Predict("question -> answer")
    state_path = tmp_path / "state.json"

    with pytest.raises(ValueError, match="provider credentials"):
        predict.lm = CambiumLM(  # type: ignore[arg-type]
            FakeDiffundo(),
            ProviderTier.FAST,
            extensions={b"api_key": "SENSITIVE_CANARY"},
        )
        predict.save(state_path)

    assert not state_path.exists()


@pytest.mark.parametrize("field", ["launch_kwargs", "train_kwargs"])
def test_toctou_mapping_credentials_cannot_be_retained(field: str) -> None:
    _require_dspy()

    class ChangingMapping(Mapping[str, str]):
        def __init__(self) -> None:
            self.reads = 0

        def __getitem__(self, key: str) -> str:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 1

        def items(self) -> Any:
            self.reads += 1
            if self.reads == 1:
                return (("benign", "value"),)
            return (("api_key", "SENSITIVE_CANARY"),)

    changing = ChangingMapping()
    lm = CambiumLM(  # type: ignore[arg-type]
        FakeDiffundo(), ProviderTier.FAST, **{field: changing}
    )

    assert changing.reads == 1
    assert getattr(lm, field) == {"benign": "value"}
    assert "SENSITIVE_CANARY" not in repr(lm.dump_state())
    assert "api_key" not in repr(lm.dump_state())


def test_copy_rejects_hostile_private_keys_without_tier_or_provider_corruption() -> None:
    _require_dspy()
    import dspy

    class HostileKey(str):
        def __hash__(self) -> int:
            return hash(("hidden", str.__str__(self)))

    provider_a = FakeDiffundo("https://provider-a.invalid")
    provider_b = FakeDiffundo("https://provider-b.invalid")
    lm = CambiumLM(provider_a, ProviderTier.FAST)  # type: ignore[arg-type]
    other = CambiumLM(provider_b, ProviderTier.FAST)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="exact builtin string"):
        lm.copy(**{HostileKey("_tier"): "invalid"})
    with pytest.raises(TypeError, match="exact builtin string"):
        lm.copy(
            **{
                HostileKey("_diffundo_reference"): other._diffundo_reference,
            }
        )

    assert provider_a.calls == []
    assert provider_b.calls == []
    state = lm.dump_state()
    assert _call(lm, "live prompt") == ["completion text"]
    loaded = dspy.BaseLM.load_state(state, allow_custom_lm_class=True)
    assert _call(loaded, "loaded prompt") == ["completion text"]
    assert [call["endpoint"] for call in provider_a.calls] == [
        "https://provider-a.invalid",
        "https://provider-a.invalid",
    ]
    assert provider_b.calls == []


@pytest.mark.parametrize(
    ("entry_point", "key"),
    [
        ("constructor", "diffundo"),
        ("constructor", "tier"),
        ("constructor", "model"),
        ("constructor", "budget_usd"),
        ("constructor", "temperature"),
        ("constructor", "max_tokens"),
        ("call", "prompt"),
        ("call", "messages"),
        ("call", "request"),
        ("acall", "prompt"),
        ("acall", "messages"),
        ("acall", "request"),
    ],
)
def test_hostile_former_parameter_key_cannot_change_provider(entry_point: str, key: str) -> None:
    _require_dspy()
    import dspy

    provider_a = FakeDiffundo("https://provider-a.invalid")
    provider_b = FakeDiffundo("https://provider-b.invalid")
    lm = CambiumLM(provider_a, ProviderTier.FAST)  # type: ignore[arg-type]
    other = CambiumLM(provider_b, ProviderTier.FAST)  # type: ignore[arg-type]
    provider_a_reference = lm._diffundo_reference

    class HostileKey(str):
        def __hash__(self) -> int:
            return hash(key)

        def __eq__(self, other_key: object) -> bool:
            if type(other_key) is str and other_key == key:
                lm._diffundo = provider_b
                lm._diffundo_reference = other._diffundo_reference
            return str.__eq__(self, other_key)

    hostile_kwargs = {HostileKey(key): object()}
    with pytest.raises(TypeError, match="exact builtin string"):
        if entry_point == "constructor":
            CambiumLM(provider_a, ProviderTier.FAST, **hostile_kwargs)  # type: ignore[arg-type]
        elif entry_point == "call":
            lm(**hostile_kwargs)
        else:

            async def invoke() -> None:
                await lm.acall(**hostile_kwargs)

            asyncio.run(invoke())

    assert lm._diffundo_reference == provider_a_reference
    state = lm.dump_state()
    assert _call(lm, "live prompt") == ["completion text"]
    loaded = dspy.BaseLM.load_state(state, allow_custom_lm_class=True)
    assert _call(loaded, "loaded prompt") == ["completion text"]
    assert [call["endpoint"] for call in provider_a.calls] == [
        "https://provider-a.invalid",
        "https://provider-a.invalid",
    ]
    assert provider_b.calls == []


def test_copy_model_override_routes_through_diffundo() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(  # type: ignore[arg-type]
        diffundo,
        ProviderTier.FAST,
        model="original",
    )
    copied = lm.copy(model="override")

    assert _call(copied, "model override prompt") == ["completion text"]
    assert diffundo.calls[0]["model"] == "override"
    assert copied.dump_state()["model"] == "override"


def test_post_construction_callback_does_not_observe_prompt() -> None:
    _require_dspy()

    observed: list[dict[str, Any]] = []

    from dspy.utils.callback import BaseCallback  # type: ignore[import-untyped]  # noqa: PLC0415

    class PromptCallback(BaseCallback):
        def on_lm_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
            del call_id, instance
            observed.append(inputs)

    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    callback = PromptCallback()
    with pytest.raises(TypeError, match="callbacks are immutable"):
        lm.callbacks.append(callback)

    list.append(lm.callbacks, callback)

    assert _call(lm, "PROMPT-CANARY") == ["completion text"]
    assert observed == []
    # copy is the second entry point: the constructor rejects a callback
    # kwarg outright (callbacks live in _FORBIDDEN_FIELDS) and a bypassed
    # callback never survives copy or observes the prompt.
    with pytest.raises(ValueError, match="callbacks"):
        lm.copy(callbacks=[callback])
    copied = lm.copy()
    assert copied.callbacks == []
    assert _call(copied, "PROMPT-CANARY") == ["completion text"]
    assert observed == []


def test_dump_and_load_state_round_trip_routes_through_diffundo() -> None:
    _require_dspy()
    import dspy

    import cambium.lm as lm_module

    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    state = lm.dump_state()
    assert state["_dspy_lm_class"] == "cambium.lm._CambiumLMImplementation"
    delattr(lm_module, "_CambiumLMImplementation")

    loaded = dspy.BaseLM.load_state(state, allow_custom_lm_class=True)

    assert _call(loaded, "reloaded prompt") == ["completion text"]
    assert diffundo.calls[0]["prompt"]["messages"][0]["content"] == "reloaded prompt"


def test_predict_json_save_and_load_round_trip_routes_through_diffundo(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    diffundo = FakeDiffundo()
    predict = dspy.Predict("question -> answer")
    predict.lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    state_path = tmp_path / "state.json"

    predict.save(state_path)
    predict.load(state_path, allow_unsafe_lm_state=True)

    assert _call(predict.lm, "JSON round-trip prompt") == ["completion text"]
    assert diffundo.calls[0]["prompt"]["messages"][0]["content"] == "JSON round-trip prompt"

    # a budget override survives save/load: the restored LM routes the copied
    # budget through diffundo rather than reverting to the base value
    copied_budget = cast(Any, predict.lm).copy(budget_usd=2.0)
    assert _call(copied_budget, "budget round-trip prompt") == ["completion text"]
    assert diffundo.calls[1]["budget_usd"] == 2.0


def test_per_call_max_tokens_reaches_diffundo() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]

    assert lm(messages=[{"role": "user", "content": "hello"}], max_tokens=1) == ["completion text"]
    assert diffundo.calls[0]["prompt"]["max_tokens"] == 1


@pytest.mark.parametrize(
    ("budget", "error", "message"),
    [
        (-1, ValueError, "budget_usd must be >= 0"),
        (True, TypeError, "budget_usd must be an exact builtin number"),
    ],
)
def test_request_budget_extension_rejects_invalid_values(
    budget: object, error: type[Exception], message: str
) -> None:
    _require_dspy()
    import dspy

    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    request = dspy.LMRequest(
        model="request-model",
        messages=cast(Any, [{"role": "user", "parts": [{"type": "text", "text": "hello"}]}]),
        config=cast(Any, {"extensions": {"budget_usd": budget}}),
    )

    with pytest.raises(error, match=message):
        lm(request=request)


def test_request_budget_extension_uses_validated_value() -> None:
    _require_dspy()
    import dspy

    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    request = dspy.LMRequest(
        model="request-model",
        messages=cast(Any, [{"role": "user", "parts": [{"type": "text", "text": "hello"}]}]),
        config=cast(Any, {"extensions": {"budget_usd": 10**1000}}),
    )

    lm(request=request)
    assert diffundo.calls[0]["budget_usd"] == 10**1000
    assert type(diffundo.calls[0]["budget_usd"]) is int


@pytest.mark.parametrize("entry_point", ["call", "acall"])
def test_explicit_request_response_format_credentials_are_rejected(entry_point: str) -> None:
    _require_dspy()
    import dspy

    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    request = dspy.LMRequest(
        model="request-model",
        messages=cast(Any, [{"role": "user", "parts": [{"type": "text", "text": "hello"}]}]),
        config=cast(Any, {"response_format": {"api_key": "SENSITIVE_CANARY"}}),
    )

    with pytest.raises(ValueError, match="provider credentials"):
        if entry_point == "call":
            lm(request=request)
        else:
            asyncio.run(lm.acall(request=request))

    assert diffundo.calls == []


@pytest.mark.parametrize("entry_point", ["call", "acall"])
def test_explicit_request_response_format_mapping_is_frozen_before_dispatch(
    entry_point: str,
) -> None:
    _require_dspy()
    import dspy

    class DelayedCredentialMapping(Mapping[str, Any]):
        def __init__(self) -> None:
            self._data: dict[str, str] = {"type": "json_object"}
            self.reads = 0

        def __getitem__(self, key: str) -> str:
            return self._data[key]

        def __iter__(self) -> Iterator[str]:
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

        def items(self) -> Any:
            self.reads += 1
            items = tuple(self._data.items())
            if self.reads == 1:
                self._data = {"api_key": "SENSITIVE_CANARY"}
            return items

    class SerializingDiffundo(FakeDiffundo):
        def __init__(self) -> None:
            super().__init__()
            self.serialized_prompts: list[str] = []

        async def call(
            self,
            tier: ProviderTier,
            prompt: dict[str, Any],
            *,
            model: str | None = None,
            budget_usd: float | None = None,
        ) -> CallResult:
            self.serialized_prompts.append(json.dumps(prompt))
            return await super().call(tier, prompt, model=model, budget_usd=budget_usd)

    diffundo = SerializingDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    response_format = DelayedCredentialMapping()
    request = dspy.LMRequest(
        model="request-model",
        messages=cast(Any, [{"role": "user", "parts": [{"type": "text", "text": "hello"}]}]),
        config=cast(Any, {"response_format": response_format}),
    )

    if entry_point == "call":
        lm(request=request)
    else:
        asyncio.run(lm.acall(request=request))

    assert len(diffundo.calls) == 1
    dispatched_prompt = diffundo.calls[0]["prompt"]
    assert dispatched_prompt["response_format"] == {"type": "json_object"}
    assert type(dispatched_prompt["response_format"]) is dict
    assert type(dispatched_prompt["messages"]) is list
    assert "api_key" not in diffundo.serialized_prompts[0]
    assert "SENSITIVE_CANARY" not in diffundo.serialized_prompts[0]
    assert response_format.reads == 1


@pytest.mark.slow
def test_state_round_trip_loads_in_a_fresh_process(tmp_path: Path) -> None:
    _require_dspy()
    from cambium.diffundo import Diffundo, ProviderConfig, ProviderTier
    from cambium.lm import CambiumLM

    diffundo = Diffundo(
        [
            ProviderConfig(
                name="p",
                tier=ProviderTier.FAST,
                base_url="https://fake.invalid",
                api_key_env="K_FAKE",
                api_key="sk-test-fake",
                model="m",
            )
        ]
    )
    lm = CambiumLM(diffundo, ProviderTier.FAST)
    state = lm.dump_state()
    assert "diffundo" in state, "saved state must carry a reconstructable Diffundo"
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    source = Path(__file__).resolve().parents[2] / "src"
    load_script = """
import json
import sys

sys.path.insert(0, sys.argv[1])
import dspy
from cambium.diffundo import Diffundo

state = json.load(open(sys.argv[2]))
loaded = dspy.BaseLM.load_state(state, allow_custom_lm_class=True)
assert isinstance(loaded._diffundo, Diffundo), "fresh-process load must rebuild a real Diffundo"
assert loaded._tier.value == "fast"
print("fresh-process load OK")
"""
    loaded = subprocess.run(
        [sys.executable, "-c", load_script, str(source), str(state_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert loaded.returncode == 0, loaded.stderr
    assert "fresh-process load OK" in loaded.stdout


def test_state_serialization_rejects_userinfo_base_url_raw_state_canary() -> None:
    _require_dspy()
    from cambium.diffundo import Diffundo, ProviderConfig

    url_secret = "URL_SECRET"
    credential_diffundo = Diffundo(
        [
            ProviderConfig(
                name="p",
                tier=ProviderTier.FAST,
                base_url=f"https://{url_secret}_user:{url_secret}_pass@fake.invalid",
                api_key_env="K_FAKE",
                api_key="sk-test-fake",
                model="m",
            )
        ]
    )
    lm = CambiumLM(credential_diffundo, ProviderTier.FAST)

    with pytest.raises(ValueError, match="URL credentials"):
        lm.dump_state()

    valid_diffundo = Diffundo(
        [
            ProviderConfig(
                name="p",
                tier=ProviderTier.FAST,
                base_url="https://api.example.invalid/v1",
                api_key_env="K_FAKE",
                api_key="sk-test-fake",
                model="m",
            )
        ]
    )
    raw_state = repr(CambiumLM(valid_diffundo, ProviderTier.FAST).dump_state())
    assert url_secret not in raw_state
    assert "https://api.example.invalid/v1" in raw_state

    # query/fragment canaries are rejected too, and the rejection reason must
    # not echo the canary back into the process
    for query_url in (
        "https://api.example.invalid/v1?api_key=QUERY_SECRET_CANARY",
        "https://api.example.invalid/v1#fragment=QUERY_SECRET_CANARY",
    ):
        credential_diffundo = Diffundo(
            [
                ProviderConfig(
                    name="p",
                    tier=ProviderTier.FAST,
                    base_url=query_url,
                    api_key_env="K_FAKE",
                    api_key="sk-test-fake",
                    model="m",
                )
            ]
        )
        lm = CambiumLM(credential_diffundo, ProviderTier.FAST)

        with pytest.raises(ValueError, match="query parameters"):
            lm.dump_state()
        with pytest.raises(ValueError) as excinfo:
            lm.dump_state()
        assert "QUERY_SECRET_CANARY" not in str(excinfo.value)

    # a queryless base_url still serializes into the raw state
    queryless_raw_state = repr(CambiumLM(valid_diffundo, ProviderTier.FAST).dump_state())
    assert "https://api.example.invalid/v1" in queryless_raw_state
    assert "QUERY_SECRET_CANARY" not in queryless_raw_state


def test_reasoning_and_tool_choice_reach_diffundo() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]

    lm(
        messages=[{"role": "user", "content": "hello"}],
        reasoning={"effort": "high"},
        tool_choice="none",
    )

    assert diffundo.calls[0]["prompt"] == {
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning": {"effort": "high"},
        "tool_choice": {"mode": "none"},
    }


def test_tool_call_completion_reaches_dspy_outputs() -> None:
    _require_dspy()
    import dspy
    from dspy.core.types import LMToolSpec  # type: ignore[import-untyped]

    class ToolCallDiffundo(FakeDiffundo):
        async def call(
            self,
            tier: ProviderTier,
            prompt: dict[str, Any],
            *,
            model: str | None = None,
            budget_usd: float | None = None,
        ) -> CallResult:
            self.calls.append(
                {"tier": tier, "prompt": prompt, "model": model, "budget_usd": budget_usd}
            )
            return CallResult(
                provider=self.endpoint,
                model=model or "fake-model",
                tier=tier,
                content="",
                latency_s=0.01,
                usage={"prompt_tokens": 2, "completion_tokens": 2},
                tool_calls=(
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"query": "dspy"}',
                        },
                    },
                ),
            )

    diffundo = ToolCallDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    request = dspy.LMRequest(
        model="cambium/fast",
        messages=cast(Any, [{"role": "user", "parts": [{"type": "text", "text": "use a tool"}]}]),
        tools=[
            LMToolSpec(
                name="search",
                description="search the web",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            )
        ],
    )

    response = lm(request=request)

    assert [tool.name for tool in response.tool_calls] == ["search"]
    assert response.tool_calls[0].args == {"query": "dspy"}
    assert response.tool_calls[0].id == "call_1"
    assert response.text is None
    assert len(diffundo.calls) == 1
    # The LM passes internal bare descriptors; the real transport performs
    # the chat-completions wrapping itself (double-wrapped tools were
    # silently dropped — see the lm.py tool-descriptor fix).
    assert diffundo.calls[0]["prompt"]["tools"] == [
        {
            "name": "search",
            "description": "search the web",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }
    ]


def test_tool_call_content_and_tool_calls_are_both_preserved() -> None:
    _require_dspy()
    import dspy

    class MixedDiffundo(FakeDiffundo):
        async def call(
            self,
            tier: ProviderTier,
            prompt: dict[str, Any],
            *,
            model: str | None = None,
            budget_usd: float | None = None,
        ) -> CallResult:
            return CallResult(
                provider=self.endpoint,
                model=model or "fake-model",
                tier=tier,
                content="explaining the call",
                latency_s=0.01,
                tool_calls=(
                    {"id": "c", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                ),
            )

    lm = CambiumLM(MixedDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    response = lm(
        request=dspy.LMRequest(
            model="cambium/fast",
            messages=cast(Any, [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}]),
        )
    )

    assert response.text == "explaining the call"
    assert [call.name for call in response.tool_calls] == ["f"]


@pytest.mark.slow
def test_concurrent_dspy_loads_preserve_cache_environment() -> None:
    _require_dspy()
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import builtins
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, sys.argv[1])
import cambium.lm as lm

assert lm._DSPY is None
assert "dspy" not in sys.modules

# Concurrent cold loads: 16 threads race on the first full dspy import; the
# pre-set DSPY_CACHEDIR must survive the load unclobbered. The repeated calls
# after the first load only exercise the cached singleton path, so keep a
# bounded sample instead of paying for thousands of identical lookups.
sentinel = "/tmp/cambium-dspy-cache-sentinel"
thread_count = 16
repeats_per_thread = 100
os.environ["DSPY_CACHEDIR"] = sentinel
cold_load_count = 0
cold_load_count_lock = threading.Lock()
start = threading.Barrier(thread_count)


real_temporary_directory = lm.tempfile.TemporaryDirectory


def tracked_temporary_directory(*args, **kwargs):
    global cold_load_count
    with cold_load_count_lock:
        cold_load_count += 1
        first_load = cold_load_count == 1
    if first_load:
        time.sleep(0.05)
    return real_temporary_directory(*args, **kwargs)


class TempfileProxy:
    TemporaryDirectory = staticmethod(tracked_temporary_directory)


lm.tempfile = TempfileProxy()


def load_repeatedly(_: int) -> list[object]:
    start.wait()
    return [lm._load_dspy() for _ in range(repeats_per_thread)]


with ThreadPoolExecutor(max_workers=thread_count) as executor:
    loads = list(executor.map(load_repeatedly, range(thread_count)))
results = [value for load in loads for value in load]
assert len(results) == thread_count * repeats_per_thread
assert len({id(value) for value in results}) == 1
assert lm._DSPY is results[0]
assert cold_load_count == 1, cold_load_count
assert os.environ["DSPY_CACHEDIR"] == sentinel

# Concurrent-writer-mid-import scenario: a writer changes DSPY_CACHEDIR while a
# fresh (top-level) dspy import is blocked; the running load must not clobber
# the writer's value once the import completes. Only the top-level package is
# dropped so the gated re-import re-runs the __init__ env dance without
# re-executing the cached submodule bodies.
del sys.modules["dspy"]
lm._DSPY = None
lm._DSPY_CACHE_DIR = None
assert lm._DSPY is None
assert "dspy" not in sys.modules

os.environ["DSPY_CACHEDIR"] = "/tmp/cambium-dspy-cache-stale"
import_started = threading.Event()
allow_import = threading.Event()
writer_errors = []
real_import = builtins.__import__


def gated_import(name, *args, **kwargs):
    if name == "dspy":
        import_started.set()
        if not allow_import.wait(5):
            raise RuntimeError("dspy import gate timed out")
    return real_import(name, *args, **kwargs)


builtins.__import__ = gated_import


def write_new_cache_dir():
    if not import_started.wait(5):
        writer_errors.append("dspy import did not start")
    else:
        os.environ["DSPY_CACHEDIR"] = "/tmp/cambium-dspy-cache-new"
    allow_import.set()


writer = threading.Thread(target=write_new_cache_dir)
writer.start()
lm._load_dspy()
writer.join(5)
assert not writer.is_alive()
assert writer_errors == []
assert os.environ["DSPY_CACHEDIR"] == "/tmp/cambium-dspy-cache-new"
"""
    env = os.environ.copy()
    env.pop("Py_GIL_DISABLED", None)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(source)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
