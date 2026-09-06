from __future__ import annotations

import math
from typing import Any

import pytest

import cambium.system_health as system_health
from cambium.system_health import can_run_heavy, decide_heavy_work

BOUNDARY_THRESHOLDS = {
    "mem_available_frac": 0.5,
    "load1_per_cpu": 2.0,
    "disk_free": 100,
}


def _decision(
    *,
    available_frac: float | None = 0.5,
    load1: float | None = 0.0,
    cpu_count: int | None = 4,
    disk_free: int | float | None = 100,
    thresholds: dict | None = None,
) -> tuple[bool, list[str]]:
    return decide_heavy_work(
        available_frac,
        load1,
        cpu_count,
        disk_free,
        BOUNDARY_THRESHOLDS if thresholds is None else thresholds,
    )


def test_resource_cutoffs_are_inclusive() -> None:
    cases = (
        ({"load1": 7.999}, True),
        ({"load1": 8.0}, True),
        ({"load1": 8.001}, False),
        ({"available_frac": 0.499}, False),
        ({"available_frac": 0.5}, True),
        ({"available_frac": 0.501}, True),
        ({"disk_free": 99}, False),
        ({"disk_free": 100}, True),
        ({"disk_free": 101}, True),
    )
    for kwargs, allowed in cases:
        result, reasons = _decision(**kwargs)
        assert result is allowed, (kwargs, reasons)
        assert (reasons == []) is allowed, (kwargs, reasons)


def test_zero_readings_are_valid_at_zero_thresholds() -> None:
    result, reasons = _decision(
        available_frac=0.0,
        load1=0.0,
        disk_free=0,
        thresholds={
            "mem_available_frac": 0.0,
            "load1_per_cpu": 0.0,
            "disk_free": 0,
        },
    )

    assert result is True, reasons
    assert reasons == []


def test_unavailable_load_or_cpu_fails_closed() -> None:
    for kwargs, reason in (
        ({"load1": None}, "load1 unavailable"),
        ({"cpu_count": None}, "cpu_count unavailable"),
        ({"cpu_count": 0}, "cpu_count unavailable"),
    ):
        result, reasons = _decision(**kwargs)
        assert result is False, kwargs
        assert reason in reasons, kwargs


def test_unreadable_optional_readings_are_skipped() -> None:
    for kwargs in ({"available_frac": None}, {"disk_free": None}):
        result, reasons = _decision(**kwargs)
        assert result is True, (kwargs, reasons)
        assert reasons == []


def test_empty_or_zero_proc_memory_is_unavailable() -> None:
    for contents in ("", "MemAvailable: 0 kB\nMemTotal: 0 kB\n", "MemAvailable: 1 kB\n"):
        assert system_health._parse_meminfo(contents) is None


def test_missing_memory_sources_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_health, "_memory_from_proc", lambda: None)
    monkeypatch.setattr(system_health, "_memory_from_sysconf", lambda: None)

    assert system_health._memory_metrics() == (None, None, None)
    result, reasons = _decision(available_frac=None)
    assert result is True, reasons
    assert reasons == []


def test_invalid_readings_fail_closed() -> None:
    cases: tuple[tuple[dict[str, Any], str], ...] = (
        ({"available_frac": -0.01}, "mem_available_frac invalid"),
        ({"available_frac": 1.01}, "mem_available_frac invalid"),
        ({"available_frac": math.nan}, "mem_available_frac invalid"),
        ({"load1": -0.01}, "load1 invalid"),
        ({"load1": math.inf}, "load1 invalid"),
        ({"load1": math.nan}, "load1 invalid"),
        ({"disk_free": -1}, "disk_free invalid"),
        ({"disk_free": math.inf}, "disk_free invalid"),
    )
    for kwargs, reason in cases:
        result, reasons = _decision(**kwargs)
        assert result is False, kwargs
        assert reason in reasons, kwargs


def test_invalid_thresholds_fail_closed() -> None:
    cases = (
        ({"mem_available_frac": -0.1}, "mem_available_frac threshold invalid"),
        ({"mem_available_frac": 1.1}, "mem_available_frac threshold invalid"),
        ({"load1_per_cpu": -0.1}, "load1_per_cpu threshold invalid"),
        ({"load1_per_cpu": math.inf}, "load1_per_cpu threshold invalid"),
        ({"disk_free": -1}, "disk_free threshold invalid"),
        ({"disk_free": 1.5}, "disk_free threshold invalid"),
    )
    for thresholds, reason in cases:
        result, reasons = _decision(thresholds=thresholds)
        assert result is False, thresholds
        assert reason in reasons, thresholds


def test_can_run_heavy_keeps_io_at_the_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        system_health,
        "health",
        lambda: {
            "mem": {"available_frac": 0.5},
            "load1": 8.0,
            "cpu_count": 4,
            "disk": {"free": 100},
        },
    )

    result, reasons = can_run_heavy(BOUNDARY_THRESHOLDS)
    assert result is True, reasons
    assert reasons == []
