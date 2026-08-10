"""Approval gates for commands outside a task's declared command policy.

The policy is deliberately list-form: each pattern is a tuple of command
tokens and matches a prefix of the requested command.  There is no shell
parsing or wildcard expansion.  ``git`` patterns normally include the
subcommand, so ``["git", "checkout"]`` does not authorize ``git push``.

``ApprovalGate.check`` is the pure classification step.  ``is_approved`` is
the I/O boundary: an injected async callback may answer a human approval
request for an otherwise unknown command.  A missing callback fails closed by
default.  ``fail_open`` exists for controlled development use only; it is a
dangerous setting because an unavailable approval service then authorizes an
unknown command.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any


class Approval(Enum):
    """The deterministic result of classifying one command."""

    ALLOWED = auto()
    DENIED = auto()
    REQUIRES_APPROVAL = auto()


CommandPattern = tuple[str, ...]
ApprovalCallback = Callable[..., Awaitable[bool]]


def _patterns(raw: Any, field_name: str) -> tuple[CommandPattern, ...]:
    """Validate and freeze list-form command-prefix patterns."""
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise TypeError(f"{field_name} must be a list of token lists")

    parsed: list[CommandPattern] = []
    for index, raw_pattern in enumerate(raw):
        if not isinstance(raw_pattern, Sequence) or isinstance(raw_pattern, (str, bytes)):
            raise TypeError(f"{field_name}[{index}] must be a list of command tokens")
        pattern = tuple(raw_pattern)
        if not pattern or not all(isinstance(token, str) for token in pattern):
            raise TypeError(f"{field_name}[{index}] must contain at least one string token")
        if not pattern[0]:
            raise ValueError(f"{field_name}[{index}] must start with a non-empty command token")
        parsed.append(pattern)
    return tuple(parsed)


def _boolean(config: Mapping[str, Any], name: str, default: bool) -> bool:
    """Read one strict boolean policy option."""
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")
    return value


@dataclass(frozen=True, slots=True, init=False)
class ApprovalPolicy:
    """Immutable command policy built from a configuration mapping.

    Configuration keys are:

    ``allowlist``
        A list of list-form command prefixes.  For example,
        ``[["git", "checkout"], ["pytest"]]``.
    ``deny``
        A list of list-form prefixes checked before the allowlist.
    ``interactive``
        Whether the composition root intends to provide a human callback.
    ``fail_open``
        Opt-in dangerous behavior for an unknown command when no callback is
        available.  The default is ``False``.
    """

    _allowlist: tuple[CommandPattern, ...]
    _denylist: tuple[CommandPattern, ...]
    _interactive: bool
    _fail_open: bool

    def __init__(self, config: Mapping[str, Any]) -> None:
        if not isinstance(config, Mapping):
            raise TypeError("approval policy config must be a mapping")
        allowlist = _patterns(config.get("allowlist", ()), "allowlist")
        denylist = _patterns(config.get("deny", ()), "deny")
        interactive = _boolean(config, "interactive", False)
        fail_open = _boolean(config, "fail_open", False)

        object.__setattr__(self, "_allowlist", allowlist)
        object.__setattr__(self, "_denylist", denylist)
        object.__setattr__(self, "_interactive", interactive)
        object.__setattr__(self, "_fail_open", fail_open)

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> ApprovalPolicy:
        """Build a policy from a config mapping."""
        return cls(config)

    @property
    def allowlist(self) -> tuple[CommandPattern, ...]:
        """The frozen allowlist command prefixes."""
        return self._allowlist

    @property
    def denylist(self) -> tuple[CommandPattern, ...]:
        """The frozen denylist command prefixes."""
        return self._denylist

    @property
    def interactive(self) -> bool:
        """Whether interactive approval is enabled for this policy."""
        return self._interactive

    @property
    def fail_open(self) -> bool:
        """Whether missing approval input may authorize an unknown command."""
        return self._fail_open


def _matches(command: Sequence[str], patterns: Sequence[CommandPattern]) -> bool:
    """Return whether one command starts with any exact token-prefix pattern."""
    return any(
        len(command) >= len(pattern) and tuple(command[: len(pattern)]) == pattern
        for pattern in patterns
    )


def _validate_command(command: list[str]) -> None:
    """Reject malformed list-form commands before policy evaluation."""
    if not isinstance(command, list):
        raise TypeError("command must be a list of strings")
    if not command:
        raise ValueError("command must contain at least one token")
    if not all(isinstance(token, str) for token in command):
        raise TypeError("command must be a list of strings")
    if not command[0]:
        raise ValueError("command must start with a non-empty executable token")


def _callback_call(
    callback: ApprovalCallback, command: list[str], cwd: Path | None
) -> Awaitable[bool]:
    """Call a callback with its supported command/cwd shape.

    The canonical callback takes ``command``.  A callback may also declare a
    second positional ``cwd`` argument, or a keyword-only ``cwd`` argument,
    when the host wants the worktree context in its approval prompt.  The
    signature is inspected before invocation so a callback's own ``TypeError``
    is never mistaken for an argument-shape mismatch.
    """
    try:
        parameters = tuple(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        return callback(command)

    cwd_parameter = next((parameter for parameter in parameters if parameter.name == "cwd"), None)
    if cwd_parameter is not None and cwd_parameter.kind is inspect.Parameter.KEYWORD_ONLY:
        return callback(command, cwd=cwd)

    positional = tuple(
        parameter
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    if len(positional) >= 2:
        return callback(command, cwd)
    return callback(command)


class ApprovalGate:
    """Apply an :class:`ApprovalPolicy` and, when configured, ask a host.

    ``callback`` and ``approval_callback`` are equivalent injection points;
    the latter is explicit at composition roots.  The callback is only used
    for ``REQUIRES_APPROVAL``.  An allowlisted command never prompts, and a
    denied command can never be rescued by a callback.
    """

    __slots__ = ("_policy", "_callback")

    def __init__(
        self,
        policy: ApprovalPolicy,
        callback: ApprovalCallback | None = None,
        *,
        approval_callback: ApprovalCallback | None = None,
    ) -> None:
        if not isinstance(policy, ApprovalPolicy):
            raise TypeError("policy must be an ApprovalPolicy")
        if callback is not None and approval_callback is not None:
            raise ValueError("provide callback or approval_callback, not both")
        self._policy = policy
        self._callback = approval_callback if approval_callback is not None else callback

    @property
    def policy(self) -> ApprovalPolicy:
        """The immutable policy applied by this gate."""
        return self._policy

    def check(self, command: list[str], *, cwd: Path | None = None) -> Approval:
        """Classify a list-form command without prompting.

        The denylist is evaluated first, so a command matching both lists is
        always ``DENIED``.  A matching allowlist prefix is ``ALLOWED``.  Every
        other valid command is ``REQUIRES_APPROVAL``.  ``cwd`` is accepted as
        operation context for the optional host callback; it does not alter
        static prefix matching.
        """
        _validate_command(command)
        del cwd
        if _matches(command, self._policy.denylist):
            return Approval.DENIED
        if _matches(command, self._policy.allowlist):
            return Approval.ALLOWED
        return Approval.REQUIRES_APPROVAL

    async def is_approved(self, command: list[str], *, cwd: Path | None = None) -> bool:
        """Return whether the command may run, asking the injected host if needed."""
        result = self.check(command, cwd=cwd)
        if result is Approval.ALLOWED:
            return True
        if result is Approval.DENIED:
            return False
        if self._policy.interactive and self._callback is not None:
            return bool(await _callback_call(self._callback, command, cwd))
        return self._policy.fail_open
