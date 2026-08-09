"""Approval-gate scenarios for D7 Q7.2 and the v2.1 M3 security boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

from cambium.approval import Approval, ApprovalGate, ApprovalPolicy


def _approved(gate: ApprovalGate, command: list[str], *, cwd: Path | None = None) -> bool:
    return asyncio.run(gate.is_approved(command, cwd=cwd))


def test_allowlist_hit() -> None:
    policy = ApprovalPolicy({"allowlist": [["pytest", "-q"], ["git", "status"]]})
    gate = ApprovalGate(policy)

    assert gate.check(["pytest", "-q", "tests"]) is Approval.ALLOWED
    assert gate.check(["git", "status", "--short"]) is Approval.ALLOWED
    assert _approved(gate, ["pytest", "-q", "tests"])


def test_deny_wins_over_allow() -> None:
    policy = ApprovalPolicy(
        {
            "allowlist": [["git", "checkout"]],
            "deny": [["git", "checkout"]],
        }
    )
    gate = ApprovalGate(policy)

    assert gate.check(["git", "checkout", "--", "main"]) is Approval.DENIED
    assert not _approved(gate, ["git", "checkout", "--", "main"])


def test_unknown_command_requires_approval() -> None:
    gate = ApprovalGate(ApprovalPolicy({"allowlist": [["git", "status"]]}))

    assert gate.check(["git", "push", "origin", "main"]) is Approval.REQUIRES_APPROVAL


def test_fail_closed_without_callback_by_default() -> None:
    gate = ApprovalGate(ApprovalPolicy({"interactive": True}))

    assert gate.check(["rm", "-rf", "build"]) is Approval.REQUIRES_APPROVAL
    assert not _approved(gate, ["rm", "-rf", "build"])


def test_fail_open_requires_explicit_dangerous_config() -> None:
    gate = ApprovalGate(ApprovalPolicy({"fail_open": True}))

    assert gate.check(["rm", "-rf", "build"]) is Approval.REQUIRES_APPROVAL
    assert _approved(gate, ["rm", "-rf", "build"])


def test_interactive_callback_grants_unknown_command(tmp_path: Path) -> None:
    prompts: list[tuple[list[str], Path | None]] = []

    async def approve(command: list[str], cwd: Path | None = None) -> bool:
        prompts.append((command, cwd))
        return True

    gate = ApprovalGate(
        ApprovalPolicy({"interactive": True}),
        approval_callback=approve,
    )

    command = ["python", "-m", "pytest"]
    assert _approved(gate, command, cwd=tmp_path)
    assert prompts == [(command, tmp_path)]


def test_interactive_callback_denies_unknown_command() -> None:
    async def reject(command: list[str]) -> bool:
        return False

    gate = ApprovalGate(ApprovalPolicy({"interactive": True}), reject)

    assert not _approved(gate, ["curl", "https://example.invalid"])


def test_git_subcommand_uses_two_token_prefix() -> None:
    gate = ApprovalGate(ApprovalPolicy({"allowlist": [["git", "checkout"]]}))

    assert gate.check(["git", "checkout", "feature"]) is Approval.ALLOWED
    assert gate.check(["git", "push", "origin", "main"]) is Approval.REQUIRES_APPROVAL
