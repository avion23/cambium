from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from cambium.tools import ToolContext, ToolPermissionPolicy, run_tool


def _run(name: str, args: dict, ctx: ToolContext):
    return asyncio.run(run_tool(name, args, ctx))


def test_run_shell_without_policy_keeps_default_output(tmp_path: Path) -> None:
    result = _run(
        "run_shell",
        {"cmd": [sys.executable, "-c", "print('default-permissive')"]},
        ToolContext(tmp_path),
    )

    assert result.ok is True
    assert result.output == "default-permissive\n"
    assert result.error is None


def test_policy_off_denies_run_shell_without_starting_process(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    result = _run(
        "run_shell",
        {
            "cmd": [
                sys.executable,
                "-c",
                "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('ran')",
                str(marker),
            ]
        },
        ToolContext(tmp_path, policy=ToolPermissionPolicy(shell=False, network=False)),
    )

    assert result.ok is False
    assert result.error
    assert result.output == ""
    assert not marker.exists()
