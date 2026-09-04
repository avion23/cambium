"""Scenario tests for the stdlib host-health probe."""

from __future__ import annotations

import os

import pytest

from cambium.system_health import can_run_heavy, decide_heavy_work


@pytest.mark.parametrize(
    ("thresholds", "expected", "reason"),
    [
        ({"mem_available_frac": 0.0, "load1_per_cpu": 1_000_000.0, "disk_free": 0}, True, ""),
        ({"mem_available_frac": 1.0}, False, "mem_available_frac"),
    ],
    ids=["generous-thresholds", "impossible-memory"],
)
def test_can_run_heavy_respects_thresholds(
    thresholds: dict[str, float], expected: bool, reason: str
) -> None:
    # The impossible-memory case uses decide_heavy_work with an explicit
    # low-memory reading: a live probe is only valid where the host can
    # read memory, and unreadable readings are skipped by contract.
    if expected:
        allowed, reasons = can_run_heavy(thresholds)
    else:
        allowed, reasons = decide_heavy_work(
            0.05, 0.5, os.cpu_count() or 4, 10 * (1 << 30), thresholds
        )

    assert allowed is expected
    assert (not reasons) is expected
    assert not reason or any(reason in item for item in reasons)


def test_decide_heavy_work_skips_unreadable_memory() -> None:
    """macOS has no memory reading; heavy work must not be blocked by it."""
    allowed, reasons = decide_heavy_work(
        None, 0.5, 8, 10 * (1 << 30), {"mem_available_frac": 1.0}
    )

    assert allowed is True
    assert reasons == []
