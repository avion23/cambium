"""Scenario tests for the stdlib host-health probe."""

from __future__ import annotations

import re

from cambium.system_health import can_run_heavy, format_health, health


def test_can_run_heavy_passes_with_generous_thresholds() -> None:
    allowed, reasons = can_run_heavy(
        {"mem_available_frac": 0.0, "load1_per_cpu": 1_000_000.0, "disk_free": 0}
    )

    assert allowed is True, reasons
    assert reasons == []


def test_can_run_heavy_reports_impossible_memory_threshold() -> None:
    allowed, reasons = can_run_heavy({"mem_available_frac": 1.0})

    assert allowed is False
    assert any("mem_available_frac" in reason for reason in reasons)


def test_format_health_is_a_parseable_one_line_summary() -> None:
    summary = format_health(health())

    match = re.fullmatch(
        r"Host health: mem (\d+)% avail, load ([0-9]+(?:\.[0-9]+)?)/([0-9]+) cores, "
        r"disk ([0-9]+) GiB free",
        summary,
    )
    assert match is not None, summary
    assert 0 <= int(match.group(1)) <= 100
