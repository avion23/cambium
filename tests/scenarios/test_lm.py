"""Integration scenarios for the optional DSPy-to-Diffundo boundary."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from cambium.architectus import ArchitectusCore
from cambium.diffundo import CallResult, ProviderTier
from cambium.lm import ArchitectusLM, CambiumLM
from cambium.tasktree import build_tree


def _require_dspy() -> None:
    if importlib.util.find_spec("dspy") is None:
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


def _call(lm: CambiumLM, prompt: str = "same prompt") -> list[str]:
    output = lm(messages=[{"role": "user", "content": prompt}])
    assert isinstance(output, list)
    return output


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
assert dspy.cache.enable_disk_cache is False
assert dspy.cache.enable_memory_cache is False
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


def test_copy_keeps_zero_retry_invariant_across_state_round_trip() -> None:
    _require_dspy()
    import dspy

    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    copied = lm.copy(num_retries=5)

    assert copied.num_retries == 0
    assert copied.cache is False
    state = copied.dump_state()
    loaded = dspy.BaseLM.load_state(state, allow_custom_lm_class=True)

    assert loaded.num_retries == 0
    assert loaded.cache is False


def test_same_prompt_to_two_provider_endpoints_reaches_each_once() -> None:
    _require_dspy()
    first = FakeDiffundo("https://provider-a.invalid")
    second = FakeDiffundo("https://provider-b.invalid")
    assert _call(CambiumLM(first, ProviderTier.FAST)) == ["completion text"]  # type: ignore[arg-type]
    assert _call(CambiumLM(second, ProviderTier.FAST)) == ["completion text"]  # type: ignore[arg-type]
    assert len(first.calls) == len(second.calls) == 1
    assert first.calls[0]["endpoint"] != second.calls[0]["endpoint"]


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
        {"tasks": [{"task_id": "root", "kind": "FEATURE", "depends_on": [], "spec": {}}]}
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
import cambium.orchestrator
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


@pytest.mark.parametrize("key", ["apiKey", "API-Key", "api_key"])
def test_secret_marker_variants_are_rejected(key: str) -> None:
    _require_dspy()
    with pytest.raises(ValueError, match="provider credentials"):
        CambiumLM(FakeDiffundo(), ProviderTier.FAST, **{key: "secret"})  # type: ignore[arg-type]


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
    lm = CambiumLM(
        FakeDiffundo(), ProviderTier.FAST, extensions=extensions
    )  # type: ignore[arg-type]

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


def test_copy_rejects_unknown_keyword_before_creating_unloadable_state() -> None:
    _require_dspy()
    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="unknown keyword argument: unexpected"):
        lm.copy(unexpected=True)


def test_copy_refreezes_existing_mutable_response_format() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    response_format: dict[str, Any] = {"type": "json_object"}
    lm.kwargs["response_format"] = response_format

    copied = lm.copy()
    response_format["api_key"] = "SENSITIVE_CANARY"

    assert _call(copied) == ["completion text"]
    assert diffundo.calls[0]["prompt"]["response_format"] == {"type": "json_object"}


def test_predict_json_save_rejects_auth_bearer_credentials(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    predict = dspy.Predict("question -> answer")
    state_path = tmp_path / "state.json"

    predict.lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    predict.lm.kwargs["auth"] = "Bearer SENSITIVE_CANARY"

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


def test_predict_json_save_rejects_tuple_credential_keys(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    predict = dspy.Predict("question -> answer")
    state_path = tmp_path / "state.json"

    with pytest.raises(ValueError, match="provider credentials"):
        predict.lm = CambiumLM(  # type: ignore[arg-type]
            FakeDiffundo(),
            ProviderTier.FAST,
            extensions={("api_key",): "SENSITIVE_CANARY"},
        )
        predict.save(state_path)

    assert not state_path.exists()


def test_predict_failed_json_save_preserves_existing_state(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    predict = dspy.Predict("question -> answer")
    predict.lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    predict.dump_state = lambda: {"not_json": object()}
    state_path = tmp_path / "state.json"
    state_path.write_text("existing state")

    with pytest.raises(RuntimeError, match="Failed to save state"):
        predict.save(state_path)

    assert state_path.read_text() == "existing state"


def test_copy_does_not_share_mutable_launch_kwargs() -> None:
    _require_dspy()
    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    copied = lm.copy()

    with pytest.raises(TypeError):
        copied.launch_kwargs["api_key"] = "SENSITIVE_CANARY"

    assert "SENSITIVE_CANARY" not in repr(lm.dump_state())
    assert "api_key" not in repr(lm.dump_state())
    assert "SENSITIVE_CANARY" not in repr(copied.dump_state())
    assert "api_key" not in repr(copied.dump_state())


def test_copy_launch_and_train_snapshots_reject_dict_base_mutators() -> None:
    _require_dspy()
    lm = CambiumLM(
        FakeDiffundo(),
        ProviderTier.FAST,
        launch_kwargs={"original": "launch"},
        train_kwargs={"original": "train"},
    )  # type: ignore[arg-type]
    copied = lm.copy()

    mutators = (
        lambda value: dict.__setitem__(value, "bypass", "changed"),
        lambda value: dict.__delitem__(value, "original"),
        lambda value: dict.update(value, {"bypass": "changed"}),
        lambda value: dict.clear(value),
        lambda value: dict.pop(value, "original"),
        lambda value: dict.popitem(value),
        lambda value: dict.setdefault(value, "bypass", "changed"),
        lambda value: dict.__ior__(value, {"bypass": "changed"}),
    )
    for attribute in ("launch_kwargs", "train_kwargs"):
        snapshot = getattr(copied, attribute)
        for mutate in mutators:
            with pytest.raises(TypeError):
                mutate(snapshot)
        assert getattr(lm, attribute) == snapshot
        assert "bypass" not in snapshot


def test_model_rejects_hostile_primitive_subclass() -> None:
    _require_dspy()

    class HostileStr(str):
        def __new__(cls, value: str, payload: dict[str, str]) -> HostileStr:
            instance = super().__new__(cls, value)
            instance.payload = payload
            return instance

        def __str__(self) -> HostileStr:
            return self

    payload = {"state": "original"}
    with pytest.raises(TypeError, match="exact builtin string"):
        CambiumLM(
            FakeDiffundo(),
            ProviderTier.FAST,
            model=HostileStr("provider-model", payload),
        )  # type: ignore[arg-type]


def test_copy_rejects_hostile_bytes_in_launch_kwargs() -> None:
    _require_dspy()

    class HostileBytes(bytes):
        def __new__(cls, value: bytes, payload: dict[str, str]) -> HostileBytes:
            instance = super().__new__(cls, value)
            instance.payload = payload
            return instance

        def __bytes__(self) -> HostileBytes:
            return self

    payload = {"state": "original"}
    with pytest.raises(TypeError, match="exact builtin primitive"):
        CambiumLM(
            FakeDiffundo(),
            ProviderTier.FAST,
            launch_kwargs={"nested": {"value": HostileBytes(b"secret", payload)}},
        )  # type: ignore[arg-type]


def test_nested_hostile_credential_key_cannot_reach_dump_state() -> None:
    _require_dspy()

    class HostileKey(str):
        def __str__(self) -> HostileKey:
            return self

        def lower(self) -> str:
            return "harmless"

    with pytest.raises(TypeError, match="exact builtin string"):
        CambiumLM(
            FakeDiffundo(),
            ProviderTier.FAST,
            extensions={"nested": [{HostileKey("api_key"): "SENSITIVE_CANARY"}]},
        )  # type: ignore[arg-type]


def test_copy_freezes_bytearrays_in_launch_and_train_kwargs() -> None:
    _require_dspy()
    launch_value = bytearray(b"launch")
    train_value = bytearray(b"train")
    lm = CambiumLM(  # type: ignore[arg-type]
        FakeDiffundo(),
        ProviderTier.FAST,
        launch_kwargs={"nested": {"value": launch_value}},
        train_kwargs={"nested": {"value": train_value}},
    )
    copied = lm.copy()

    launch_value[:] = b"changed"
    train_value[:] = b"changed"

    assert lm.launch_kwargs["nested"]["value"] == b"launch"
    assert copied.launch_kwargs["nested"]["value"] == b"launch"
    assert lm.train_kwargs["nested"]["value"] == b"train"
    assert copied.train_kwargs["nested"]["value"] == b"train"


def test_copy_rejects_mutable_primitive_subclasses() -> None:
    _require_dspy()

    class MutableInt(int):
        def __new__(cls, value: int, payload: dict[str, str]) -> MutableInt:
            instance = super().__new__(cls, value)
            instance.payload = payload
            return instance

    payload = {"state": "original"}
    value = MutableInt(7, payload)
    with pytest.raises(TypeError, match="exact builtin primitive"):
        CambiumLM(  # type: ignore[arg-type]
            FakeDiffundo(),
            ProviderTier.FAST,
            launch_kwargs={"nested": {"value": value}},
            train_kwargs={"nested": {"value": value}},
        )


def test_copy_revalidates_post_construction_hostile_snapshots() -> None:
    _require_dspy()

    class HostileStr(str):
        pass

    class HostileBytes(bytes):
        pass

    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    launch = {"value": HostileStr("launch")}
    train = {"value": HostileBytes(b"train")}
    lm.launch_kwargs = launch
    lm.train_kwargs = train

    with pytest.raises(TypeError, match="exact builtin primitive"):
        lm.copy()

    launch["value"] = "original unaffected"
    train["value"] = b"original unaffected"
    assert lm.launch_kwargs["value"] == "original unaffected"
    assert lm.train_kwargs["value"] == b"original unaffected"


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


def test_construction_registers_diffundo_once() -> None:
    _require_dspy()
    import cambium.lm as lm_module

    diffundos = [FakeDiffundo() for _ in range(100)]
    with lm_module._DIFFUNDO_REGISTRY_LOCK:
        lm_module._DIFFUNDO_REGISTRY.clear()

    instances = [CambiumLM(diffundo, ProviderTier.FAST) for diffundo in diffundos]  # type: ignore[arg-type]

    assert len(instances) == 100
    assert len(lm_module._DIFFUNDO_REGISTRY) == 100


def test_rejected_constructions_do_not_register_diffundo() -> None:
    _require_dspy()
    import cambium.lm as lm_module

    diffundo = FakeDiffundo()
    with lm_module._DIFFUNDO_REGISTRY_LOCK:
        lm_module._DIFFUNDO_REGISTRY.clear()
        registry_size = len(lm_module._DIFFUNDO_REGISTRY)

    for _ in range(100):
        with pytest.raises(ValueError, match="callbacks"):
            CambiumLM(diffundo, ProviderTier.FAST, callbacks=[object()])  # type: ignore[arg-type]

    assert len(lm_module._DIFFUNDO_REGISTRY) == registry_size


def test_rejected_copies_do_not_register_replacement_diffundo() -> None:
    _require_dspy()
    import cambium.lm as lm_module

    diffundo = FakeDiffundo()
    replacement = FakeDiffundo("https://replacement.invalid")
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    with lm_module._DIFFUNDO_REGISTRY_LOCK:
        lm_module._DIFFUNDO_REGISTRY.clear()

    for _ in range(100):
        with pytest.raises(ValueError, match="invalid"):
            lm.copy(diffundo=replacement, tier="invalid")

    assert len(lm_module._DIFFUNDO_REGISTRY) == 0


def test_budget_rejects_nan() -> None:
    _require_dspy()

    with pytest.raises(ValueError, match="finite"):
        CambiumLM(FakeDiffundo(), ProviderTier.FAST, budget_usd=float("nan"))  # type: ignore[arg-type]


def test_request_rejects_private_copy_and_direct_nan_budget_before_diffundo() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST, budget_usd=1.0)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="finite"):
        lm.copy(_budget_usd=float("nan"))

    lm._budget_usd = float("nan")
    with pytest.raises(ValueError, match="finite"):
        _call(lm, "budget bypass canary")

    assert diffundo.calls == []


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


def test_copy_rejects_hostile_key_before_equality_can_change_provider_reference() -> None:
    _require_dspy()
    import dspy

    provider_a = FakeDiffundo("https://provider-a.invalid")
    provider_b = FakeDiffundo("https://provider-b.invalid")
    lm = CambiumLM(provider_a, ProviderTier.FAST)  # type: ignore[arg-type]
    other = CambiumLM(provider_b, ProviderTier.FAST)  # type: ignore[arg-type]
    provider_a_reference = lm._diffundo_reference

    class HostileKey(str):
        def __hash__(self) -> int:
            return hash("self")

        def __eq__(self, other_key: object) -> bool:
            if type(other_key) is str and other_key == "self":
                lm._diffundo_reference = other._diffundo_reference
            return str.__eq__(self, other_key)

    with pytest.raises(TypeError, match="exact builtin string"):
        lm.copy(
            **{
                HostileKey("_diffundo_reference"): other._diffundo_reference,
            }
        )

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
def test_hostile_former_parameter_key_cannot_change_provider(
    entry_point: str, key: str
) -> None:
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


def test_kwargs_entry_points_have_no_bindable_named_parameters() -> None:
    _require_dspy()
    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]
    expected = [
        ("self", inspect.Parameter.POSITIONAL_ONLY),
        ("args", inspect.Parameter.VAR_POSITIONAL),
        ("kwargs", inspect.Parameter.VAR_KEYWORD),
    ]

    for entry_point in (type(lm).__init__, type(lm).__call__, type(lm).acall):
        parameters = inspect.signature(entry_point).parameters.values()
        assert [(parameter.name, parameter.kind) for parameter in parameters] == expected


@pytest.mark.parametrize("entry_point", ["constructor", "call", "acall"])
def test_kwargs_entry_points_reject_unknown_exact_keyword(entry_point: str) -> None:
    _require_dspy()
    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="unknown keyword argument: unexpected"):
        if entry_point == "constructor":
            CambiumLM(FakeDiffundo(), ProviderTier.FAST, unexpected=True)  # type: ignore[arg-type]
        elif entry_point == "call":
            lm(unexpected=True)
        else:
            asyncio.run(lm.acall(unexpected=True))


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


def test_copy_rejects_prompt_observing_callbacks() -> None:
    _require_dspy()
    import dspy

    observed: list[dict[str, Any]] = []

    class PromptCallback(dspy.utils.callback.BaseCallback):
        def on_lm_start(self, call_id: str, instance: Any, inputs: dict[str, Any]) -> None:
            del call_id, instance
            observed.append(inputs)

    callback = PromptCallback()
    lm = CambiumLM(FakeDiffundo(), ProviderTier.FAST)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="callbacks"):
        lm.copy(callbacks=[callback])

    lm.callbacks = [callback]
    copied = lm.copy()
    assert _call(copied, "PROMPT-CANARY") == ["completion text"]
    assert copied.callbacks == []
    assert observed == []


def test_post_construction_callback_does_not_observe_prompt() -> None:
    _require_dspy()
    import dspy

    observed: list[dict[str, Any]] = []

    class PromptCallback(dspy.utils.callback.BaseCallback):
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


def test_predict_json_round_trip_preserves_bytearray_snapshot(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    predict = dspy.Predict("question -> answer")
    predict.lm = CambiumLM(  # type: ignore[arg-type]
        FakeDiffundo(),
        ProviderTier.FAST,
        launch_kwargs={"value": bytearray(b"x")},
    )
    state_path = tmp_path / "state.json"

    predict.save(state_path)
    predict.load(state_path, allow_unsafe_lm_state=True)

    assert predict.lm.launch_kwargs["value"] == b"x"


def test_predict_json_round_trip_preserves_literal_byte_marker_mapping(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    marker = {"__cambium_bytes_base64__": "eA=="}
    predict = dspy.Predict("question -> answer")
    predict.lm = CambiumLM(  # type: ignore[arg-type]
        FakeDiffundo(),
        ProviderTier.FAST,
        launch_kwargs={"nested": marker},
    )
    state_path = tmp_path / "state.json"

    predict.save(state_path)
    predict.load(state_path, allow_unsafe_lm_state=True)

    assert predict.lm.launch_kwargs["nested"] == marker


def test_predict_json_round_trip_preserves_nested_tuple_mapping_key(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    expected = {("a", "b"): "value"}
    predict = dspy.Predict("question -> answer")
    predict.lm = CambiumLM(  # type: ignore[arg-type]
        FakeDiffundo(),
        ProviderTier.FAST,
        launch_kwargs={"nested": expected},
    )
    state_path = tmp_path / "state.json"

    predict.save(state_path)
    predict.load(state_path, allow_unsafe_lm_state=True)

    assert predict.lm.launch_kwargs["nested"] == expected


def test_copied_budget_round_trip_routes_with_override(tmp_path: Path) -> None:
    _require_dspy()
    import dspy

    diffundo = FakeDiffundo()
    predict = dspy.Predict("question -> answer")
    lm = CambiumLM(diffundo, ProviderTier.FAST, budget_usd=1.0)  # type: ignore[arg-type]
    predict.lm = lm.copy(budget_usd=2.0)
    state_path = tmp_path / "state.json"

    predict.save(state_path)
    predict.load(state_path, allow_unsafe_lm_state=True)
    assert _call(predict.lm, "budget round-trip prompt") == ["completion text"]

    assert diffundo.calls[0]["budget_usd"] == 2.0


def test_per_call_max_tokens_reaches_diffundo() -> None:
    _require_dspy()
    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]

    assert lm(messages=[{"role": "user", "content": "hello"}], max_tokens=1) == [
        "completion text"
    ]
    assert diffundo.calls[0]["prompt"]["max_tokens"] == 1


@pytest.mark.parametrize("entry_point", ["call", "acall"])
def test_explicit_request_response_format_credentials_are_rejected(entry_point: str) -> None:
    _require_dspy()
    import dspy

    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    request = dspy.LMRequest(
        model="request-model",
        messages=[{"role": "user", "parts": [{"type": "text", "text": "hello"}]}],
        config={"response_format": {"api_key": "SENSITIVE_CANARY"}},
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

    diffundo = FakeDiffundo()
    lm = CambiumLM(diffundo, ProviderTier.FAST)  # type: ignore[arg-type]
    response_format = DelayedCredentialMapping()
    request = dspy.LMRequest(
        model="request-model",
        messages=[{"role": "user", "parts": [{"type": "text", "text": "hello"}]}],
        config={"response_format": response_format},
    )

    if entry_point == "call":
        lm(request=request)
    else:
        asyncio.run(lm.acall(request=request))

    assert len(diffundo.calls) == 1
    assert dict(diffundo.calls[0]["prompt"]["response_format"].items()) == {
        "type": "json_object"
    }
    assert response_format.reads == 1


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
        "messages": ({"role": "user", "content": "hello"},),
        "reasoning": {"effort": "high"},
        "tool_choice": {"mode": "none"},
    }


def test_concurrent_dspy_loads_preserve_cache_environment() -> None:
    _require_dspy()
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, sys.argv[1])
import cambium.lm as lm

assert lm._DSPY is None
assert "dspy" not in sys.modules

sentinel = "/tmp/cambium-dspy-cache-sentinel"
os.environ["DSPY_CACHEDIR"] = sentinel
cold_load_count = 0
cold_load_count_lock = threading.Lock()
start = threading.Barrier(16)


real_temporary_directory = lm.tempfile.TemporaryDirectory


def tracked_temporary_directory(*args, **kwargs):
    global cold_load_count
    with cold_load_count_lock:
        cold_load_count += 1
        first_load = cold_load_count == 1
    if first_load:
        time.sleep(0.1)
    return real_temporary_directory(*args, **kwargs)


class TempfileProxy:
    TemporaryDirectory = staticmethod(tracked_temporary_directory)


lm.tempfile = TempfileProxy()


def load_repeatedly(_: int) -> list[object]:
    start.wait()
    return [lm._load_dspy() for _ in range(2000)]


with ThreadPoolExecutor(max_workers=16) as executor:
    loads = list(executor.map(load_repeatedly, range(16)))
results = [value for load in loads for value in load]
assert len(results) == 16 * 2000
assert len({id(value) for value in results}) == 1
assert lm._DSPY is results[0]
assert cold_load_count == 1, cold_load_count
assert os.environ["DSPY_CACHEDIR"] == sentinel
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


def test_dspy_load_preserves_cache_environment_writer_during_import() -> None:
    _require_dspy()
    source = Path(__file__).resolve().parents[2] / "src"
    script = """
import builtins
import os
import sys
import threading

sys.path.insert(0, sys.argv[1])
os.environ["DSPY_CACHEDIR"] = "/tmp/cambium-dspy-cache-stale"
import cambium.lm as lm

assert lm._DSPY is None
assert "dspy" not in sys.modules

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
