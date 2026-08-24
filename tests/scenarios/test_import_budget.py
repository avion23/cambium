"""Budget guards for Cambium's fresh-process import path.

These checks deliberately launch a new interpreter: importing the package in
the pytest process would measure collection state rather than startup cost.
If legitimate features make the budget too small, remeasure the fresh import
and raise the threshold deliberately here with an explanatory review.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# Import budget guard: the current fresh ``python3 -c "import cambium"``
# measurement is about 53 ms median (about 89 ms worst in the baseline sample).
# Keep this at roughly twice the observed worst case, not at an arbitrary
# machine-dependent limit.  Raise it deliberately if legitimate startup work
# grows, and update the measured baseline in this comment at the same time.
IMPORT_STARTUP_BUDGET_S = 0.20


def _subprocess_environment() -> dict[str, str]:
    """Make the fresh interpreter resolve this checkout's source tree."""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC), env.get("PYTHONPATH")]))
    return env


def _import_probe() -> subprocess.CompletedProcess[str]:
    """Import the public package in a fresh system Python interpreter."""
    return subprocess.run(
        [sys.executable, "-c", "import cambium"],
        cwd=ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )


def test_import_cambium_stays_within_startup_budget() -> None:
    """Keep the CLI package import comfortably below its measured budget."""
    started = time.perf_counter()
    result = _import_probe()
    elapsed = time.perf_counter() - started

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed < IMPORT_STARTUP_BUDGET_S, (
        f"fresh `import cambium` took {elapsed:.3f}s, over the "
        f"{IMPORT_STARTUP_BUDGET_S:.3f}s import budget; if legitimate "
        "features grew, remeasure and raise the budget deliberately"
    )


def test_import_cambium_keeps_provider_sdks_lazy() -> None:
    """Do not pay for the OpenAI SDK or DSPy before their feature paths run."""
    probe = """
import sys

import cambium

loaded = sorted(
    name
    for name in sys.modules
    if name == "openai"
    or name.startswith("openai.")
    or name == "dspy"
    or name.startswith("dspy.")
)
assert not loaded, f"provider SDKs imported during `import cambium`: {loaded}"
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=5.0,
    )

    assert result.returncode == 0, result.stdout + result.stderr
