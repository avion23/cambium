"""Make scenario-test subprocesses import cambium from this repository.

pyproject.toml's ``pythonpath = ["src"]`` already adds the source tree to
the pytest parent's sys.path. Scenario tests spawn subprocesses (``-m
cambium.worker``, ``-m cambium.bench``, ``-m cambium.cli``) that inherit
os.environ, so export the source tree via PYTHONPATH here, before
collection.
"""

import os
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent / "src")

if _SRC not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    )


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_routing_state(tmp_path, monkeypatch):
    """Point the usage-debt ledger at a per-test scratch file.

    run_plan creates DebtStore() with the default path, which is the real
    ~/.config/cambium/routing-state.json. Without isolation, integration
    tests pollute the production ledger (glm-5.2 review finding P1). The
    env override is inherited by spawned worker subprocesses.
    """
    monkeypatch.setenv(
        "CAMBIUM_ROUTING_STATE_PATH", str(tmp_path / "routing-state.json")
    )
