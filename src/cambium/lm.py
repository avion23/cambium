"""Optional DSPy adapters that keep provider policy inside Diffundo."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sysconfig
import tempfile
import threading
import uuid
import weakref
from collections.abc import Mapping
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Any

if sysconfig.get_config_var("Py_GIL_DISABLED") or os.environ.get("Py_GIL_DISABLED") == "1":
    raise RuntimeError(
        "CambiumLM does not support free-threaded CPython; use a GIL-enabled CPython build"
    )

# isort: off
from .diffundo import CallResult, Diffundo, ProviderTier

# isort: on

_GENERATION_FIELDS = (
    "temperature",
    "max_tokens",
    "top_p",
    "stop",
    "n",
    "logprobs",
    "response_format",
)
_CACHE_FIELDS = frozenset({"cache", "rollout_id", "prompt_cache", "prompt_cache_key"})
_FORBIDDEN_FIELDS = frozenset({"callbacks"})
_SECRET_MARKERS = frozenset(
    {
        "apikey",
        "authorization",
        "auth",
        "bearer",
        "password",
        "secret",
        "token",
        "credential",
        "clientid",
        "clientsecret",
        "oauth",
        "session",
        "refresh",
        "access",
        "private",
        "pem",
        "passphrase",
        "accesstoken",
        "refreshtoken",
        "privatekey",
        "sessionkey",
        "authtoken",
    }
)
_DSPY_LOAD_LOCK = threading.Lock()
_IMPLEMENTATION_LOCK = threading.Lock()
_DIFFUNDO_REGISTRY_LOCK = threading.Lock()
_DIFFUNDO_REGISTRY: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()
_DSPY: Any | None = None


def _normalize_key(key: Any) -> Any:
    if type(key) is str:
        return key
    if isinstance(key, str):
        return str.__str__(key)
    return key


class _ImmutableCallbacks(list[Any]):
    """Disposable DSPy-compatible callback view that rejects normal mutation."""

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise TypeError("CambiumLM callbacks are immutable")

    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    __setitem__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation


def _freeze(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """Take a recursive immutable snapshot of JSON-shaped configuration."""
    if memo is None:
        memo = {}
    if isinstance(value, Mapping):
        if id(value) in memo:
            raise ValueError("CambiumLM kwargs must not contain reference cycles")
        memo[id(value)] = None
        frozen = MappingProxyType(
            {
                _freeze(_normalize_key(key), memo): _freeze(item, memo)
                for key, item in value.items()
            }
        )
        memo.pop(id(value))
        return frozen
    if isinstance(value, list | tuple):
        if id(value) in memo:
            raise ValueError("CambiumLM kwargs must not contain reference cycles")
        memo[id(value)] = None
        frozen = tuple(_freeze(item, memo) for item in value)
        memo.pop(id(value))
        return frozen
    if isinstance(value, bytearray | memoryview):
        if type(value) not in (bytearray, memoryview):
            raise TypeError(
                f"CambiumLM configuration values must use exact builtin primitive types, not "
                f"{type(value).__name__}"
            )
        return bytes(value)
    if value is None or type(value) in (str, bytes, int, float, bool):
        return value
    if isinstance(value, str | bytes | int | float | bool):
        raise TypeError(
            f"CambiumLM configuration values must use exact builtin primitive types, not "
            f"{type(value).__name__}"
        )
    raise TypeError(
        f"CambiumLM configuration values must be immutable JSON-shaped data, not "
        f"{type(value).__name__}"
    )


def _register_diffundo(diffundo: Any) -> str:
    reference = uuid.uuid4().hex
    try:
        with _DIFFUNDO_REGISTRY_LOCK:
            _DIFFUNDO_REGISTRY[reference] = diffundo
    except TypeError as exc:
        raise TypeError("diffundo must support weak references for DSPy state persistence") from exc
    return reference


def _resolve_diffundo(reference: str) -> Any:
    with _DIFFUNDO_REGISTRY_LOCK:
        diffundo = _DIFFUNDO_REGISTRY.get(reference)
    if diffundo is None:
        raise RuntimeError(f"Diffundo reference {reference!r} is not available in this process")
    return diffundo


def _install_atomic_dspy_save(dspy: Any) -> None:
    """Make DSPy state-file replacement atomic for adapters loaded by Cambium."""
    original_save = dspy.Module.save
    if getattr(original_save, "_cambium_atomic_save", False):
        return

    @wraps(original_save)
    def atomic_save(
        module: Any,
        path: Any,
        save_program: bool = False,
        modules_to_serialize: Any = None,
    ) -> None:
        target = Path(path)
        if save_program or target.suffix not in {".json", ".pkl"}:
            original_save(module, path, save_program, modules_to_serialize)
            return

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.stem}-",
                suffix=target.suffix,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
            original_save(module, temporary_path, False, modules_to_serialize)
            os.replace(temporary_path, target)
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    atomic_save._cambium_atomic_save = True  # type: ignore[attr-defined]
    dspy.Module.save = atomic_save


def _load_dspy() -> Any:
    """Load DSPy on first use without enabling its process-wide disk cache.

    Packaging has no GIL ABI environment marker, so uv can resolve DSPy for
    cp314t. CambiumLM rejects that build when this module is imported instead.
    """
    global _DSPY
    if _DSPY is not None:
        return _DSPY

    with _DSPY_LOAD_LOCK:
        if _DSPY is not None:
            return _DSPY

        # DSPy 3.3 constructs its default disk cache during import. When the
        # variable is absent, a valid non-HOME directory avoids its /dev/null
        # fallback warning; the cache is disabled before this function returns.
        with tempfile.TemporaryDirectory(prefix="cambium-dspy-cache-") as cache_dir:
            previous_cache_dir = os.environ.get("DSPY_CACHEDIR")
            os.environ.setdefault("DSPY_CACHEDIR", cache_dir)
            try:
                try:
                    import dspy
                except ImportError as exc:
                    raise RuntimeError("CambiumLM requires the optional 'dspy' extra") from exc
            finally:
                if previous_cache_dir is None and os.environ.get("DSPY_CACHEDIR") == cache_dir:
                    os.environ.pop("DSPY_CACHEDIR", None)

            configure_cache = getattr(dspy, "configure_cache", None)
            if not callable(configure_cache):
                raise RuntimeError("CambiumLM requires a DSPy version with cache configuration")
            cache = getattr(dspy, "cache", None)
            if (
                getattr(cache, "enable_disk_cache", True)
                or getattr(cache, "enable_memory_cache", True)
            ):
                configure_cache(
                    enable_disk_cache=False,
                    enable_memory_cache=False,
                    disk_cache_dir=None,
                )
                close_disk_cache = getattr(getattr(cache, "disk_cache", None), "close", None)
                if callable(close_disk_cache):
                    close_disk_cache()
            _install_atomic_dspy_save(dspy)
        _DSPY = dspy
        return _DSPY


class CambiumLM:
    """Lazily construct a concrete subclass of ``dspy.LM``."""

    def __new__(cls, *args: Any, **kwargs: Any) -> CambiumLM:
        if cls is not CambiumLM:
            return super().__new__(cls)
        del args, kwargs
        return object.__new__(_implementation_class())


class _CambiumLMMixin:
    """DSPy LM implementation whose only provider edge is Diffundo.call."""

    forward_contract = "typed_lm"

    @property
    def callbacks(self) -> _ImmutableCallbacks:
        """Return a disposable empty callback view instead of stored callbacks."""
        return _ImmutableCallbacks()

    @callbacks.setter
    def callbacks(self, value: Any) -> None:
        """Discard DSPy callback assignments at the instance boundary."""
        del value

    def __init__(
        self,
        diffundo: Diffundo,
        tier: ProviderTier | str,
        *,
        model: str | None = None,
        budget_usd: float | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> None:
        if not isinstance(diffundo, Diffundo) and not callable(getattr(diffundo, "call", None)):
            raise TypeError("diffundo must provide async call(tier, prompt, ...)")
        self._diffundo = diffundo
        self._tier = ProviderTier(tier)
        self._provider_model = self._validate_model(model)
        self._budget_usd = self._validate_budget(budget_usd)
        self._diffundo_reference = _register_diffundo(diffundo)
        kwargs = self._safe_kwargs(kwargs)
        super().__init__(
            model=f"cambium/{self._tier.value}",
            temperature=temperature,
            max_tokens=max_tokens,
            cache=False,
            callbacks=[],
            num_retries=0,
            **kwargs,
        )
        self.launch_kwargs = _freeze(self.launch_kwargs)
        self.train_kwargs = _freeze(self.train_kwargs)

    def __call__(
        self,
        *items: Any,
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        request: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Call synchronously with session-local, non-retaining settings."""
        dspy = _load_dspy()
        with self._session_context(dspy):
            return super().__call__(
                *items,
                prompt=prompt,
                messages=messages,
                request=request,
                callbacks=(),
                **self._call_kwargs(kwargs),
            )

    async def acall(
        self,
        *items: Any,
        prompt: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        request: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Call asynchronously with session-local, non-retaining settings."""
        dspy = _load_dspy()
        with self._session_context(dspy):
            return await super().acall(
                *items,
                prompt=prompt,
                messages=messages,
                request=request,
                callbacks=(),
                **self._call_kwargs(kwargs),
            )

    def _session_context(self, dspy: Any) -> Any:
        return dspy.context(
            lm=self,
            callbacks=[],
            trace=[],
            caller_modules=[],
            disable_history=True,
            max_history_size=0,
            track_usage=False,
            cambium_session=object(),
        )

    def update_history(self, entry: Any) -> None:
        """Keep prompts and request options out of DSPy's process-global history."""
        del entry

    def copy(self, **kwargs: Any) -> Any:
        """Copy this LM without bypassing the Diffundo credential boundary."""
        self._validate_model(self._provider_model)
        self._validate_budget(self._budget_usd)
        launch_kwargs = self._safe_kwargs({"launch_kwargs": self.launch_kwargs})[
            "launch_kwargs"
        ]
        train_kwargs = self._safe_kwargs({"train_kwargs": self.train_kwargs})["train_kwargs"]
        adapter_overrides = {
            key: kwargs[key]
            for key in ("diffundo", "tier", "model", "budget_usd")
            if key in kwargs
        }
        if "model" in adapter_overrides:
            self._validate_model(adapter_overrides["model"])
        if "budget_usd" in adapter_overrides:
            self._validate_budget(adapter_overrides["budget_usd"])
        safe_kwargs = self._safe_kwargs(
            {key: value for key, value in kwargs.items() if key not in adapter_overrides}
        )
        copied = super().copy(**safe_kwargs)
        copied.launch_kwargs = safe_kwargs.get("launch_kwargs", launch_kwargs)
        copied.train_kwargs = safe_kwargs.get("train_kwargs", train_kwargs)
        if "diffundo" in adapter_overrides:
            diffundo = adapter_overrides["diffundo"]
            if not isinstance(diffundo, Diffundo) and not callable(getattr(diffundo, "call", None)):
                raise TypeError("diffundo must provide async call(tier, prompt, ...)")
            copied._diffundo = diffundo
            copied._diffundo_reference = _register_diffundo(diffundo)
        if "tier" in adapter_overrides:
            copied._tier = ProviderTier(adapter_overrides["tier"])
            copied.model = f"cambium/{copied._tier.value}"
        if "model" in adapter_overrides:
            copied._provider_model = adapter_overrides["model"]
        if "budget_usd" in adapter_overrides:
            copied._budget_usd = adapter_overrides["budget_usd"]
        return copied

    def dump_state(self) -> dict[str, Any]:
        """Return enough trusted runtime state to reconstruct this adapter."""
        state = super().dump_state()
        state.pop("model_type", None)
        state.pop("cache", None)
        state.pop("num_retries", None)
        model = self._validate_model(self._provider_model)
        budget_usd = self._validate_budget(self._budget_usd)
        state.update(
            {
                "diffundo_reference": self._diffundo_reference,
                "tier": self._tier.value,
                "model": model,
                "budget_usd": budget_usd,
            }
        )
        return self._json_snapshot(self._safe_kwargs(state))

    @classmethod
    def load_state(
        cls,
        state: dict[str, Any],
        *,
        allow_custom_lm_class: bool = False,
    ) -> Any:
        """Reconstruct a trusted adapter state through its Diffundo reference."""
        del allow_custom_lm_class
        constructor_state = dict(state)
        constructor_state.pop("_dspy_lm_class", None)
        reference = constructor_state.pop("diffundo_reference")
        constructor_state["diffundo"] = _resolve_diffundo(reference)
        constructor_state = cls._restore_json_snapshot(constructor_state)
        return cls(**constructor_state)

    @staticmethod
    def _safe_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
        if type(kwargs) is dict and any(key in _FORBIDDEN_FIELDS for key in kwargs):
            forbidden = sorted(key for key in kwargs if key in _FORBIDDEN_FIELDS)
            raise ValueError(f"CambiumLM does not accept {', '.join(forbidden)}")
        frozen_kwargs = _freeze(kwargs)
        safe: dict[Any, Any] = {}
        forbidden: list[str] = []
        for key, value in frozen_kwargs.items():
            if key in _FORBIDDEN_FIELDS:
                forbidden.append(key)
            elif key not in _CACHE_FIELDS:
                safe[key] = value
        if forbidden:
            raise ValueError(f"CambiumLM does not accept {', '.join(sorted(forbidden))}")
        pending: list[Any] = [safe]
        visited: set[int] = set()
        while pending:
            value = pending.pop()
            if isinstance(value, Mapping):
                if id(value) in visited:
                    continue
                visited.add(id(value))
                for key, nested_value in value.items():
                    if type(key) is str and key not in _GENERATION_FIELDS:
                        normalized = "".join(
                            character
                            for character in str.lower(key)
                            if str.isalnum(character)
                        )
                        if any(marker in normalized for marker in _SECRET_MARKERS):
                            raise ValueError(
                                "provider credentials belong to Diffundo configuration, "
                                "not CambiumLM kwargs"
                            )
                    pending.append(nested_value)
                continue
            if isinstance(value, list | tuple):
                if id(value) in visited:
                    continue
                visited.add(id(value))
                pending.extend(value)
        return safe

    @staticmethod
    def _json_snapshot(value: Any) -> Any:
        if type(value) is bytes:
            return {"__cambium_bytes_base64__": base64.b64encode(value).decode("ascii")}
        if isinstance(value, Mapping):
            return {
                _CambiumLMMixin._json_snapshot(key): _CambiumLMMixin._json_snapshot(item)
                for key, item in value.items()
            }
        if isinstance(value, tuple):
            return tuple(_CambiumLMMixin._json_snapshot(item) for item in value)
        return value

    @staticmethod
    def _restore_json_snapshot(value: Any) -> Any:
        if type(value) is dict and set(value) == {"__cambium_bytes_base64__"}:
            encoded = value["__cambium_bytes_base64__"]
            if type(encoded) is not str:
                raise TypeError("CambiumLM byte snapshot must contain an exact builtin string")
            return base64.b64decode(encoded, validate=True)
        if type(value) is dict:
            return {
                key: _CambiumLMMixin._restore_json_snapshot(item)
                for key, item in value.items()
            }
        if type(value) is list:
            return [_CambiumLMMixin._restore_json_snapshot(item) for item in value]
        return value

    @classmethod
    def _call_kwargs(cls, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        safe = cls._safe_kwargs(kwargs)
        safe["cache"] = False
        return safe

    def forward(self, request: Any) -> Any:
        """Run the synchronous DSPy call shape through Diffundo."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aforward(request))
        raise RuntimeError("use `await lm.acall(...)` while an asyncio event loop is running")

    async def aforward(self, request: Any) -> Any:
        """Run the asynchronous DSPy call shape through Diffundo."""
        prompt, model, budget_usd = self._request_parts(request)
        result = await self._diffundo.call(
            self._tier,
            prompt,
            model=model,
            budget_usd=budget_usd,
        )
        return self._response(result)

    def _request_parts(self, request: Any) -> tuple[dict[str, Any], str | None, float | None]:
        config = request.config.model_dump(exclude_none=True)
        extensions = config.pop("extensions", {})
        config.pop("cache", None)
        config.pop("prompt_cache", None)

        prompt: dict[str, Any] = {
            "messages": [self._message(message) for message in request.messages]
        }
        for field in _GENERATION_FIELDS:
            if field in config:
                prompt[field] = config[field]
        if request.tools:
            prompt["tools"] = [self._tool(tool) for tool in request.tools]

        model = self._provider_model
        budget_usd = self._budget_usd
        if isinstance(extensions, Mapping):
            requested_model = extensions.get("model")
            if requested_model is not None:
                model = self._validate_model(requested_model)
            requested_budget = extensions.get("budget_usd")
            if requested_budget is not None:
                self._validate_budget(requested_budget)
                budget_usd = float(requested_budget)
        return prompt, model, budget_usd

    @staticmethod
    def _validate_model(model: Any) -> str | None:
        if model is not None and type(model) is not str:
            raise TypeError("model must be an exact builtin string")
        return model

    @staticmethod
    def _validate_budget(budget_usd: Any) -> int | float | None:
        if budget_usd is not None and type(budget_usd) not in (int, float):
            raise TypeError("budget_usd must be an exact builtin number")
        return budget_usd

    @staticmethod
    def _message(message: Any) -> dict[str, Any]:
        if all(part.type == "text" for part in message.parts):
            content: Any = "".join(part.text for part in message.parts)
        else:
            content = [part.model_dump(exclude_none=True) for part in message.parts]
        rendered = {"role": message.role, "content": content}
        if message.name is not None:
            rendered["name"] = message.name
        return rendered

    @staticmethod
    def _tool(tool: Any) -> dict[str, Any]:
        function: dict[str, Any] = {
            "name": tool.name,
            "parameters": tool.parameters,
        }
        if tool.description is not None:
            function["description"] = tool.description
        if tool.strict is not None:
            function["strict"] = tool.strict
        return {"type": "function", "function": function}

    @staticmethod
    def _response(result: CallResult) -> Any:
        dspy = _load_dspy()
        return dspy.LMResponse.from_text(
            result.content,
            model=result.model,
            usage=result.usage,
            cost=result.estimated_cost_usd,
            cache_hit=False,
            metadata={
                "provider": result.provider,
                "tier": result.tier.value,
                "latency_s": result.latency_s,
            },
        )


def _implementation_class() -> type[Any]:
    implementation = globals().get("_CambiumLMImplementation")
    if isinstance(implementation, type):
        return implementation

    dspy = _load_dspy()
    with _IMPLEMENTATION_LOCK:
        implementation = globals().get("_CambiumLMImplementation")
        if isinstance(implementation, type):
            return implementation
        implementation = type(
            "_CambiumLMImplementation",
            (_CambiumLMMixin, dspy.LM, CambiumLM),
            {"__module__": __name__},
        )
        globals()["_CambiumLMImplementation"] = implementation
        return implementation


def __getattr__(name: str) -> Any:
    if name == "_CambiumLMImplementation":
        return _implementation_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class ArchitectusLM:
    """Adapt CambiumLM to Architectus's asynchronous ``decide`` port."""

    _SYSTEM_PROMPT = (
        "You are Architectus. Return only a JSON array of action objects for the next "
        "scheduling wave. Do not include markdown."
    )

    def __init__(self, lm: CambiumLM) -> None:
        if not isinstance(lm, CambiumLM):
            raise TypeError("lm must be a CambiumLM")
        self._lm = lm

    async def decide(
        self, tree_state: dict[str, Any], events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        dynamic_tail = json.dumps(
            {"tree_state": tree_state, "events": events},
            sort_keys=True,
            separators=(",", ":"),
        )
        outputs = await self._lm.acall(
            messages=[
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": dynamic_tail},
            ]
        )
        if not isinstance(outputs, list) or len(outputs) != 1 or not isinstance(outputs[0], str):
            raise TypeError("CambiumLM must return exactly one completion text")
        actions = json.loads(outputs[0])
        if not isinstance(actions, list) or not all(isinstance(action, dict) for action in actions):
            raise TypeError("Architectus completion must be a JSON array of action objects")
        return actions


__all__ = ["ArchitectusLM", "CambiumLM"]
