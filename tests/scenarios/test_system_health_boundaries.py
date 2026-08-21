from __future__ import annotations

import math

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
    disk_free: int | None = 100,
    thresholds: dict | None = None,
) -> tuple[bool, list[str]]:
    return decide_heavy_work(
        available_frac,
        load1,
        cpu_count,
        disk_free,
        BOUNDARY_THRESHOLDS if thresholds is None else thresholds,
    )


@pytest.mark.parametrize(
    ("load1", "allowed"),
    [(7.999, True), (8.0, True), (8.001, False)],
)
def test_load_cutoff_is_inclusive(load1: float, allowed: bool) -> None:
    result, reasons = _decision(load1=load1)

    assert result is allowed, reasons
    assert (reasons == []) is allowed


@pytest.mark.parametrize(
    ("available_frac", "allowed"),
    [(0.499, False), (0.5, True), (0.501, True)],
)
def test_memory_cutoff_is_inclusive(available_frac: float, allowed: bool) -> None:
    result, reasons = _decision(available_frac=available_frac)

    assert result is allowed, reasons
    assert (reasons == []) is allowed


@pytest.mark.parametrize(
    ("disk_free", "allowed"),
    [(99, False), (100, True), (101, True)],
)
def test_disk_cutoff_is_inclusive(disk_free: int, allowed: bool) -> None:
    result, reasons = _decision(disk_free=disk_free)

    assert result is allowed, reasons
    assert (reasons == []) is allowed


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


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"available_frac": None}, "mem_available_frac unavailable"),
        ({"load1": None}, "load1 unavailable"),
        ({"cpu_count": None}, "cpu_count unavailable"),
        ({"cpu_count": 0}, "cpu_count unavailable"),
        ({"disk_free": None}, "disk_free unavailable"),
    ],
)
def test_unavailable_or_zero_cpu_readings_fail_closed(
    kwargs: dict[str, object], reason: str
) -> None:
    result, reasons = _decision(**kwargs)

    assert result is False
    assert reason in reasons


@pytest.mark.parametrize(
    "contents",
    ["", "MemAvailable: 0 kB\nMemTotal: 0 kB\n", "MemAvailable: 1 kB\n"],
)
def test_empty_or_zero_proc_memory_is_unavailable(contents: str) -> None:
    assert system_health._parse_meminfo(contents) is None


def test_missing_memory_sources_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_health, "_memory_from_proc", lambda: None)
    monkeypatch.setattr(system_health, "_memory_from_sysconf", lambda: None)

    assert system_health._memory_metrics() == (None, None, None)
    result, reasons = _decision(available_frac=None)

    assert result is False
    assert "mem_available_frac unavailable" in reasons


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"available_frac": -0.01}, "mem_available_frac invalid"),
        ({"available_frac": 1.01}, "mem_available_frac invalid"),
        ({"available_frac": math.nan}, "mem_available_frac invalid"),
        ({"load1": -0.01}, "load1 invalid"),
        ({"load1": math.inf}, "load1 invalid"),
        ({"load1": math.nan}, "load1 invalid"),
        ({"disk_free": -1}, "disk_free invalid"),
        ({"disk_free": math.inf}, "disk_free invalid"),
    ],
)
def test_negative_or_absurd_readings_fail_closed(
    kwargs: dict[str, object], reason: str
) -> None:
    result, reasons = _decision(**kwargs)

    assert result is False
    assert reason in reasons


@pytest.mark.parametrize(
    ("thresholds", "reason"),
    [
        ({"mem_available_frac": -0.1}, "mem_available_frac threshold invalid"),
        ({"mem_available_frac": 1.1}, "mem_available_frac threshold invalid"),
        ({"load1_per_cpu": -0.1}, "load1_per_cpu threshold invalid"),
        ({"load1_per_cpu": math.inf}, "load1_per_cpu threshold invalid"),
        ({"disk_free": -1}, "disk_free threshold invalid"),
        ({"disk_free": 1.5}, "disk_free threshold invalid"),
    ],
)
def test_invalid_thresholds_fail_closed(thresholds: dict, reason: str) -> None:
    result, reasons = _decision(thresholds=thresholds)

    assert result is False
    assert reason in reasons


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
