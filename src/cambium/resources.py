"""Standalone reusable primitive for bounding concurrent CPU-heavy commands.

``CompileGate`` limits only commands whose token prefix is in
:data:`HEAVY_PATTERNS`.  The heuristic is deliberately lexical: a pattern is
split into command tokens and must match the beginning of the command exactly.
It does not inspect shell syntax, expand aliases, resolve executable paths, or
classify a command by its arguments.  For example, ``["cargo", "build"]`` is
heavy, while ``["python", "-m", "pytest"]`` is not because it does not start
with the ``pytest`` token prefix.

The gate is an instance-owned dependency with no runtime caller in
``cambium.supervisor.run_plan``: the supervisor gate runner was removed, so a
caller that needs the bound constructs the gate itself and pairs each heavy
acquisition with ``release``.  This module keeps no mutable process-wide state.
"""

from __future__ import annotations

import asyncio
import math
import os
from typing import Final

# These are command-token prefixes, not regular expressions.  Keep the public
# values as strings so the policy is easy to audit and change as a unit.
HEAVY_PATTERNS: Final[tuple[str, ...]] = (
    "make",
    "cargo",
    "npm install",
    "yarn",
    "pip install",
    "pytest",
    "cmake",
    "ninja",
    "gcc",
    "clang",
    "go build",
    "rustc",
    "mvn",
    "gradle",
)

DEFAULT_ACQUIRE_TIMEOUT_S: Final[float] = 60.0


class _AcquisitionToken:
    """Opaque marker for one successful heavy-command acquisition."""

    __slots__ = ()


def _matches_prefix(command: list[str], pattern: str) -> bool:
    """Return whether ``command`` starts with the exact tokens in ``pattern``."""
    pattern_tokens = pattern.split()
    return len(command) >= len(pattern_tokens) and command[: len(pattern_tokens)] == pattern_tokens


class CompileGate:
    """Bound concurrent CPU-heavy commands as a standalone reusable primitive.

    Non-heavy commands bypass the semaphore.  A successful heavy acquisition
    must be paired with ``release`` by the caller.  ``timeout_s`` is optional
    to keep timeout tests short; its default is 60 seconds.

    There is no runtime caller in ``run_plan``; callers that use the bound
    construct their own instance.  The instance is intended to be used by one
    asyncio loop.  Its counters are loop-affine and therefore need no
    shared-state lock.
    """

    __slots__ = (
        "_max_concurrent",
        "_timeout_s",
        "_semaphore",
        "_current",
        "_heavy",
        "_waits",
        "_timeouts",
        "_held",
    )

    def __init__(
        self,
        max_concurrent: int | None = None,
        *,
        timeout_s: float = DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> None:
        if max_concurrent is None:
            max_concurrent = os.cpu_count() or 1
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
            raise TypeError("max_concurrent must be an integer or None")
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool):
            raise TypeError("timeout_s must be a number")
        if not math.isfinite(float(timeout_s)) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and greater than zero")

        self._max_concurrent = max_concurrent
        self._timeout_s = float(timeout_s)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._current = 0
        self._heavy = 0
        self._waits = 0
        self._timeouts = 0
        self._held: set[_AcquisitionToken] = set()

    def is_heavy(self, command: list[str]) -> bool:
        """Return whether ``command`` has a configured heavy token prefix."""
        return any(_matches_prefix(command, pattern) for pattern in HEAVY_PATTERNS)

    async def acquire(self, command: list[str]) -> _AcquisitionToken | None | bool:
        """Acquire a heavy-command permit, or return ``False`` on timeout.

        A successful heavy acquisition returns an opaque token.  Non-heavy
        commands return ``None`` immediately and do not affect semaphore state
        or statistics.
        """
        if not self.is_heavy(command):
            return None

        if self._semaphore.locked():
            self._waits += 1
        try:
            acquired = await asyncio.wait_for(self._semaphore.acquire(), self._timeout_s)
        except TimeoutError:
            self._timeouts += 1
            return False
        if not acquired:
            return False
        self._current += 1
        self._heavy += 1
        token = _AcquisitionToken()
        self._held.add(token)
        return token

    def release(self, token: _AcquisitionToken | None) -> None:
        """Release a permit by its acquisition token.

        ``None`` is the no-op token for non-heavy commands.  Unknown and
        duplicate tokens are rejected so a permit cannot be leaked silently.
        """
        if token is None:
            return
        if not isinstance(token, _AcquisitionToken) or token not in self._held:
            raise ValueError("unknown or duplicate acquisition token")
        self._held.remove(token)
        self._current -= 1
        self._semaphore.release()

    def stats(self) -> dict[str, int]:
        """Return current use, total successful heavy acquisitions, and waits."""
        return {
            "current": self._current,
            "heavy": self._heavy,
            "max": self._max_concurrent,
            "waits": self._waits,
            "timeouts": self._timeouts,
        }
