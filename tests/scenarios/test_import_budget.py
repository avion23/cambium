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

# Import budget guard: 30 fresh ``python3 -c "import cambium"`` probes, run in
# six batches with one-second pauses, measured 60.1 ms median, 104.9 ms p95,
# and 117.7 ms max on the current host.  Keep this at roughly twice the
# observed worst case, not at an arbitrary machine-dependent limit.  The
# baseline p95 is below the 120 ms decision threshold, so retain 0.20 s.
# Raise it deliberately if legitimate startup work grows, and update the
# measured baseline in this comment at the same time.
# The probe takes the BEST of three fresh-interpreter attempts: startup cost
# is a floor property, and parallel-suite contention only ever adds noise.
IMPORT_STARTUP_BUDGET_S = 0.20
_PROBE_ATTEMPTS = 3


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


class _TimedProbe:
    """One fresh-interpreter probe with its parent wall-clock duration."""

    def __init__(self, result: subprocess.CompletedProcess[str], elapsed_s: float) -> None:
        self.result = result
        self.elapsed_s = elapsed_s


def _timed_probe() -> _TimedProbe:
    started = time.perf_counter()
    result = _import_probe()
    return _TimedProbe(result, time.perf_counter() - started)


def test_import_cambium_stays_within_startup_budget() -> None:
    """Keep the CLI package import comfortably below its measured budget."""
    probes = [_timed_probe() for _ in range(_PROBE_ATTEMPTS)]
    best = min(probes, key=lambda p: p.elapsed_s)
    assert best.result.returncode == 0, best.result.stdout + best.result.stderr
    elapsed = best.elapsed_s
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
