"""Least-privilege environments for Cambium subprocesses.

The supervisor is the trust boundary for subprocess environments.  A child
gets a small deterministic base environment, plus values for explicitly
allowlisted names.  The source mapping is passed in by the caller so tests
and callers that construct an environment do not accidentally reintroduce
``os.environ`` wholesale.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

# These are the only names that trusted supervisor code may set in addition
# to the fixed base.  Provider values still require an explicit allowlist.
_OVERRIDE_NAMES = frozenset(
    {
        "CAMBIUM_GENERATION",
        "CAMBIUM_SESSION_ID",
        "CAMBIUM_TASK_ID",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_EDITOR",
        "GIT_SEQUENCE_EDITOR",
        "GIT_TERMINAL_PROMPT",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
    }
)


def _names(names: Iterable[str] | None) -> tuple[str, ...]:
    if names is None:
        return ()
    if isinstance(names, (str, bytes)):
        raise TypeError("environment allowlist must be an iterable of names")

    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or _ENV_NAME.fullmatch(name) is None:
            raise ValueError(f"invalid environment-variable name {name!r}")
        if name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


def build_subprocess_env(
    source: Mapping[str, str] | None = None,
    *,
    allowed_keys: Iterable[str] | None = None,
    worktree: Path | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a strict child environment.

    ``source`` supplies values for the explicit allowlist only.  It defaults
    to the supervisor's environment for production use.  No caller-supplied
    source key is copied implicitly.
    """
    source = os.environ if source is None else source
    names = _names(allowed_keys)
    env = {
        # ``os.defpath`` contains the system locations needed by git, sh, and
        # the standard command-line tools.  The host's PATH is never copied.
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONUNBUFFERED": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if worktree is not None:
        # Do not expose or use the supervisor user's HOME in a child.  Git
        # configuration for managed repositories is local to the repository.
        env["HOME"] = str(Path(worktree).resolve() / ".cambium" / "home")

    for name in names:
        value = source.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise TypeError(f"environment value for {name!r} must be a string")
            env[name] = value

    if overrides is not None:
        for name, value in overrides.items():
            if name not in _OVERRIDE_NAMES:
                raise ValueError(f"environment override is not allowlisted: {name!r}")
            if not isinstance(value, str):
                raise TypeError(f"environment override for {name!r} must be a string")
            env[name] = value
    return env


__all__ = ["build_subprocess_env"]
