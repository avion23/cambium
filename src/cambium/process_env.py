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
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path


def _uv_bin_dir() -> list[str]:
    """Directory of the ``uv`` executable the parent resolved, if any.

    The strict child PATH deliberately never copies the host PATH, but
    workers and doctor subprocesses invoke the same package manager the
    operator uses. Homebrew installs (macOS) keep ``uv`` outside
    ``os.defpath`` and the repo venv, so propagate only the resolved
    tool's directory — not the surrounding environment.
    """
    uv = shutil.which("uv")
    if uv is None:
        return []
    parent = str(Path(uv).resolve().parent)
    return [parent]


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

_FIXED_NAMES = frozenset(
    {
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
    }
)

_PROTECTED_NAMES = _OVERRIDE_NAMES | _FIXED_NAMES


def _names(names: Iterable[str] | None) -> tuple[str, ...]:
    if names is None:
        return ()
    if isinstance(names, str | bytes):
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
        # the standard command-line tools.  The host's PATH is never copied,
        # but the project's own tool dirs are appended so repo tools such as
        # ``ruff`` resolve for the worker.
        "PATH": os.pathsep.join(
            [
                os.defpath,
                str(Path(__file__).resolve().parents[2] / ".venv" / "bin"),
                str(Path.home() / ".local" / "bin"),
                *_uv_bin_dir(),
            ]
        ),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONUNBUFFERED": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/dev/null",
    }
    for name in names:
        if name in _PROTECTED_NAMES:
            continue
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
