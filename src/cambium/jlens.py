"""Thin client for the Mac jlens score service, used by optimizer metrics.

The score service runs on the host that owns the Jacobian-lens server and
returns, per requested layer, the rank of the expected decision token in the
model's internal readout at the last prompt position.  This module keeps the
DSPy dependency lazy: import time never touches dspy.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

_DEFAULT_LAYERS = [29, 41, 57, 61]


class JlenError(RuntimeError):
    """Raised when the jlens score service cannot be reached or responds badly."""


def render_messages(predictor: Any, inputs: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct the exact messages the DSPy adapter sent to the LM.

    ``predictor`` is the DSPy Predict instance recorded in the trace and
    ``inputs`` are the kwargs it was called with; the adapter formats the
    signature, the demos currently attached to the predictor, and the inputs
    into the message list that the LM call consumed.
    """
    import dspy  # type: ignore[import-untyped]

    adapter = dspy.settings.adapter or dspy.ChatAdapter()
    demos = getattr(predictor, "demos", None) or []
    return adapter.format(
        signature=predictor.signature,
        demos=demos,
        inputs=dict(inputs),
    )


class JlenClient:
    """HTTP client for the jlens score service."""

    def __init__(
        self,
        base_url: str,
        layers: list[int] | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.layers = layers or list(_DEFAULT_LAYERS)
        self.timeout = timeout

    def score(
        self,
        messages: list[dict[str, Any]],
        expected: list[str],
        alt: list[str] | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "messages": messages,
                "expected": expected,
                "alt": alt or [],
                "layers": self.layers,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/score",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not isinstance(result, Mapping):
                raise JlenError("jlens score service returned a non-object JSON value")
            return dict(result)
        except urllib.error.HTTPError as exc:
            raise JlenError(f"jlens score service returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise JlenError(f"jlens score service unreachable: {exc.reason}") from exc
        except (ValueError, OSError) as exc:
            raise JlenError(f"jlens score service failed: {exc}") from exc

    def signal(self, result: dict[str, Any], expected: list[str]) -> float:
        """Normalize the score service response into [0, 1].

        Prefers the service's calibrated ``commitment`` (mean over layers of
        P(correct commitment | rank)); falls back to a linear rank
        normalization when no calibration was loaded. A rank of 1 (token is
        the model's top internal choice) yields 1.0; rank 1000 or worse
        yields 0.0.  Layers that lack a usable rank are ignored; an empty
        result scores 0.0.
        """
        if not isinstance(result, Mapping):
            raise JlenError("jlens score service returned a non-object result")
        commitment = result.get("commitment")
        if (
            isinstance(commitment, (int, float))
            and not isinstance(commitment, bool)
            and math.isfinite(float(commitment))
        ):
            return max(0.0, min(1.0, float(commitment)))
        layers = result.get("layers")
        if not isinstance(layers, dict) or not layers:
            return 0.0
        values: list[float] = []
        for info in layers.values():
            if not isinstance(info, dict):
                continue
            rank = info.get("expected_rank")
            if (
                isinstance(rank, bool)
                or not isinstance(rank, (int, float))
                or not math.isfinite(float(rank))
            ):
                continue
            if rank < 1:
                continue
            values.append(max(0.0, 1.0 - (rank - 1) / 999.0))
        if not values:
            return 0.0
        return sum(values) / len(values)
