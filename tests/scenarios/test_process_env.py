"""Security scenarios for strict subprocess environments."""

from __future__ import annotations

from cambium.process_env import build_subprocess_env


def test_subprocess_environment_disables_git_hooks() -> None:
    environment = build_subprocess_env(source={})

    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert environment["GIT_CONFIG_VALUE_0"] == "/dev/null"
