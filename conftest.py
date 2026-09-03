"""Make scenario-test subprocesses import cambium from this repository.

pyproject.toml's ``pythonpath = ["src"]`` already adds the source tree to
the pytest parent's sys.path. Scenario tests spawn subprocesses (``-m
cambium.worker``, ``-m cambium.cli``) that inherit
os.environ, so export the source tree via PYTHONPATH here, before
collection.
"""

import os
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parent / "src")

if _SRC not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    )


_PROVIDER_TIMING_FILES = frozenset(
    {
        "tests/scenarios/test_diffundo.py",
        "tests/scenarios/test_provider_storm.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep provider deadline scenarios together under xdist loadgroup.

    These modules use deliberately short call budgets and assert the typed
    Retry-After outcome.  Running their functions in one worker avoids making
    the assertion depend on another worker's CPU scheduling while retaining
    parallel execution for the rest of the suite.
    """

    repository_root = Path(__file__).resolve().parent
    for item in items:
        try:
            relative_path = Path(item.path).resolve().relative_to(repository_root).as_posix()
        except ValueError:
            continue
        if relative_path in _PROVIDER_TIMING_FILES:
            item.add_marker(pytest.mark.xdist_group("provider-timing"))


@pytest.fixture(autouse=True)
def _isolate_routing_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep every test away from the developer's real routing-state ledger.

    The DebtStore defaults to ``~/.config/cambium/routing-state.json``;
    without this fixture, supervisor tests recorded hundreds of loopback
    requests into the real ledger. Tests that exercise ledger persistence
    pass their own explicit path and are unaffected.
    """

    import cambium.routing

    isolated = tmp_path / "routing-state.json"
    monkeypatch.setattr(cambium.routing, "DEFAULT_ROUTING_STATE_PATH", isolated)
    # Subprocess supervisors inherit this, so scenario tests that spawn
    # ``-m cambium.supervisor`` also stay off the real ledger.
    monkeypatch.setenv("CAMBIUM_ROUTING_STATE", str(isolated))
    yield
