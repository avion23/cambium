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
