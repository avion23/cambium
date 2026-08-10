"""Optional DSPy adapters that keep provider policy inside Diffundo."""

from __future__ import annotations

import asyncio
import json
import os
import sysconfig
import tempfile
import threading
from collections.abc import Mapping
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
_SECRET_MARKERS = frozenset(
    {
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
        "credential",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "privatekey",
        "sessionkey",
        "authtoken",
    }
)
_DSPY_LOAD_LOCK = threading.Lock()
_DSPY: Any | None = None


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

        previous_cache_dir = os.environ.get("DSPY_CACHEDIR")
        # DSPy 3.3 constructs its default disk cache during import. A valid,
        # non-HOME directory avoids its /dev/null fallback warning; the cache
        # is disabled before this function returns.
        with tempfile.TemporaryDirectory(prefix="cambium-dspy-cache-") as cache_dir:
            os.environ["DSPY_CACHEDIR"] = cache_dir
            try:
                try:
                    import dspy
                except ImportError as exc:
                    raise RuntimeError("CambiumLM requires the optional 'dspy' extra") from exc
            finally:
                if previous_cache_dir is None:
                    os.environ.pop("DSPY_CACHEDIR", None)
                else:
                    os.environ["DSPY_CACHEDIR"] = previous_cache_dir

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
        _DSPY = dspy
        return _DSPY


class CambiumLM:
    """Lazily construct a concrete subclass of ``dspy.LM``."""

    def __new__(cls, *args: Any, **kwargs: Any) -> CambiumLM:
        if cls is not CambiumLM:
            return super().__new__(cls)
        dspy = _load_dspy()
        implementation = type("CambiumLM", (_CambiumLMMixin, dspy.LM, CambiumLM), {})
        return implementation(*args, **kwargs)


class _CambiumLMMixin:
    """DSPy LM implementation whose only provider edge is Diffundo.call."""

    forward_contract = "typed_lm"

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
        self._provider_model = model
        self._budget_usd = budget_usd
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

    @staticmethod
    def _safe_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
        safe = {key: value for key, value in kwargs.items() if key not in _CACHE_FIELDS}
        forbidden = [
            key
            for key in safe
            if key not in _GENERATION_FIELDS
            if any(
                marker in "".join(character for character in key.lower() if character.isalnum())
                for marker in _SECRET_MARKERS
            )
        ]
        if forbidden:
            raise ValueError(
                "provider credentials belong to Diffundo configuration, not CambiumLM kwargs"
            )
        return safe

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
                if not isinstance(requested_model, str):
                    raise TypeError("model must be a string")
                model = requested_model
            requested_budget = extensions.get("budget_usd")
            if requested_budget is not None:
                if isinstance(requested_budget, bool) or not isinstance(
                    requested_budget, int | float
                ):
                    raise TypeError("budget_usd must be a number")
                budget_usd = float(requested_budget)
        return prompt, model, budget_usd

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
