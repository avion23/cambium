"""Scenario tests for the stdlib host-health probe."""

from __future__ import annotations

import pytest

from cambium.system_health import can_run_heavy


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
    allowed, reasons = can_run_heavy(thresholds)

    assert allowed is expected
    assert (not reasons) is expected
    assert not reason or any(reason in item for item in reasons)
