"""Scenarios for the unified ``cambium`` CLI.

Fast scenarios drive :func:`cambium.cli.main` in-process with ``capsys`` (and
a non-TTY fake stdin for stdin-reading commands). Only scenarios that
inherently need a real subprocess are marked slow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cambium import cli


def test_module_test_unknown_module_exits_two(capsys) -> None:
    assert cli.main(["module-test", "does_not_exist"]) == 2
    assert "unknown module" in capsys.readouterr().err


def test_module_test_rejects_arbitrary_pytest_arguments(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["module-test", "example", "--maxfail=1"])
    assert raised.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_architectus_live_without_provider_config_exits_two(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """A live run with an unreadable provider config fails before any LLM call."""
    missing = tmp_path / "missing" / "providers.json"
    monkeypatch.setattr(cli, "_architectus_provider_config_path", lambda: missing)

    assert cli.main(["architectus"]) == 2

    captured = capsys.readouterr()
    assert "cambium architectus:" in captured.err
    assert "provider selection failed" in captured.err
    assert "Traceback" not in captured.err
