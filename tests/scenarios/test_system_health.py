"""Scenario tests for the stdlib host-health probe."""

from __future__ import annotations

import os
import re

from cambium.system_health import can_run_heavy, format_health, health


def test_health_returns_host_metrics_with_sane_types() -> None:
    metrics = health()

    assert set(metrics) >= {
        "mem",
        "load1",
        "load5",
        "load15",
        "cpu_count",
        "disk",
        "open_fds",
    }

    memory = metrics["mem"]
    assert isinstance(memory, dict)
    assert isinstance(memory["available"], int)
    assert isinstance(memory["total"], int)
    assert isinstance(memory["available_frac"], float)
    assert 0.0 <= memory["available_frac"] <= 1.0
    assert memory["total"] > 0

    assert all(
        isinstance(metrics[key], (float, type(None)))
        for key in ("load1", "load5", "load15")
    )
    assert isinstance(metrics["cpu_count"], (int, type(None)))

    disk = metrics["disk"]
    assert isinstance(disk, dict)
    assert isinstance(disk["free"], int)
    assert isinstance(disk["total"], int)
    assert disk["total"] > 0
    assert isinstance(metrics["open_fds"], (int, type(None)))


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


def test_health_survives_sysconf_failure(monkeypatch) -> None:
    def fail_sysconf(_name: str) -> int:
        raise OSError("sysconf unavailable")

    monkeypatch.setattr(os, "sysconf", fail_sysconf)

    metrics = health()

    assert isinstance(metrics, dict)
    assert isinstance(metrics["mem"], dict)
