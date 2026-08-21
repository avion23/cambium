"""Small, standard-library-only host health measurements.

Memory is read from ``/proc/meminfo`` first, using ``MemAvailable`` and
``MemTotal``.  When that Linux interface is unavailable or incomplete, the
probe falls back to ``os.sysconf``: it reads ``SC_PAGE_SIZE`` (then
``SC_PAGESIZE``), multiplies it by ``SC_AVPHYS_PAGES`` and ``SC_PHYS_PAGES``,
and reports the resulting byte counts.  If neither source is available, the
memory values are ``None``.

Load averages use ``os.getloadavg`` on platforms that provide it.  Windows
does not provide that interface, so its three load values are ``None``.
``open_fds`` is a cheap Linux-only descriptor count from
``/proc/self/fd``; it is deliberately not a process count heuristic.  All
measurements are taken at call time.  The module keeps no mutable state and
has no third-party dependencies.
"""

from __future__ import annotations

import math
import os
import shutil
from typing import Any


def _parse_meminfo(contents: str) -> tuple[int, int] | None:
    """Return ``(available_bytes, total_bytes)`` from procfs text."""
    values: dict[str, int] = {}
    for line in contents.splitlines():
        name, separator, remainder = line.partition(":")
        if not separator:
            continue
        fields = remainder.split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        if value < 0:
            continue
        unit = fields[1].lower() if len(fields) > 1 else ""
        multiplier = {"kb": 1024, "mb": 1024**2, "gb": 1024**3}.get(unit, 1)
        values[name.strip()] = value * multiplier

    available = values.get("MemAvailable")
    total = values.get("MemTotal")
    if available is None or total is None or total <= 0:
        return None
    return available, total


def _memory_from_proc() -> tuple[int, int] | None:
    """Read the primary Linux memory source."""
    try:
        with open("/proc/meminfo", encoding="ascii") as stream:
            return _parse_meminfo(stream.read())
    except (OSError, UnicodeError):
        return None


def _memory_from_sysconf() -> tuple[int, int] | None:
    """Read the portable page-count fallback, if the platform exposes it."""
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        try:
            page_size = os.sysconf("SC_PAGESIZE")
        except (AttributeError, OSError, ValueError):
            return None

    try:
        available_pages = os.sysconf("SC_AVPHYS_PAGES")
        total_pages = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None

    if page_size <= 0 or available_pages < 0 or total_pages <= 0:
        return None
    return available_pages * page_size, total_pages * page_size


def _memory_metrics() -> tuple[int | None, int | None, float | None]:
    """Return available bytes, total bytes, and their bounded fraction."""
    memory = _memory_from_proc() or _memory_from_sysconf()
    if memory is None:
        return None, None, None

    available, total = memory
    fraction = max(0.0, min(1.0, available / total))
    return available, total, fraction


def _load_averages() -> tuple[float | None, float | None, float | None]:
    """Return one-, five-, and fifteen-minute loads where supported."""
    try:
        loads = os.getloadavg()
    except (AttributeError, NotImplementedError, OSError):
        return None, None, None
    return loads[0], loads[1], loads[2]


def _disk_metrics(path: str | os.PathLike[str] | None) -> tuple[int | None, int | None]:
    """Return free and total bytes for ``path``, or ``None`` on an OS error."""
    try:
        usage = shutil.disk_usage(os.getcwd() if path is None else path)
    except OSError:
        return None, None
    return usage.free, usage.total


def _open_fd_count() -> int | None:
    """Count descriptors for this process without enumerating system processes."""
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def health(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Return a point-in-time snapshot of host resources.

    ``path`` selects the filesystem for the disk measurement.  ``None`` uses
    the current working directory at call time.  Unsupported host interfaces
    are represented by ``None`` instead of process-global defaults.
    """
    available, total, available_frac = _memory_metrics()
    load1, load5, load15 = _load_averages()
    disk_free, disk_total = _disk_metrics(path)

    return {
        "mem": {
            "available": available,
            "total": total,
            "available_frac": available_frac,
        },
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "cpu_count": os.cpu_count(),
        "disk": {
            "free": disk_free,
            "total": disk_total,
        },
        "open_fds": _open_fd_count(),
    }


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def decide_heavy_work(
    available_frac: float | None,
    load1: float | None,
    cpu_count: int | None,
    disk_free: int | None,
    thresholds: dict | None = None,
) -> tuple[bool, list[str]]:
    """Decide whether readings meet the heavy-operation budget.

    Threshold keys are minimum ``mem_available_frac``, maximum
    ``load1_per_cpu`` (the multiplier applied to the logical CPU count), and
    minimum ``disk_free`` bytes.  Defaults are 0.10, 2.0, and 1 GiB.
    Minimum thresholds and the maximum load threshold are inclusive. Missing
    or invalid readings and thresholds fail closed and add a reason.
    """
    configured = {} if thresholds is None else dict(thresholds)
    reasons: list[str] = []

    try:
        minimum_memory = float(configured.get("mem_available_frac", 0.10))
    except (TypeError, ValueError, OverflowError):
        minimum_memory = None
    if (
        minimum_memory is None
        or not _is_finite_number(minimum_memory)
        or not 0.0 <= minimum_memory <= 1.0
    ):
        reasons.append("mem_available_frac threshold invalid")

    try:
        maximum_load_per_cpu = float(configured.get("load1_per_cpu", 2.0))
    except (TypeError, ValueError, OverflowError):
        maximum_load_per_cpu = None
    if (
        maximum_load_per_cpu is None
        or not _is_finite_number(maximum_load_per_cpu)
        or maximum_load_per_cpu < 0.0
    ):
        reasons.append("load1_per_cpu threshold invalid")

    raw_minimum_disk_free = configured.get("disk_free", 1 << 30)
    try:
        minimum_disk_free = int(raw_minimum_disk_free)
    except (TypeError, ValueError, OverflowError):
        minimum_disk_free = None
    if (
        minimum_disk_free is None
        or not _is_finite_number(raw_minimum_disk_free)
        or minimum_disk_free < 0
        or minimum_disk_free != raw_minimum_disk_free
    ):
        reasons.append("disk_free threshold invalid")

    if available_frac is None:
        reasons.append("mem_available_frac unavailable")
    elif (
        not _is_finite_number(available_frac)
        or not 0.0 <= available_frac <= 1.0
    ):
        reasons.append("mem_available_frac invalid")
    elif minimum_memory is not None and available_frac < minimum_memory:
        reasons.append(
            f"mem_available_frac {available_frac:.3f} < {minimum_memory:.3f}"
        )

    if load1 is None:
        reasons.append("load1 unavailable")
    elif not _is_finite_number(load1) or load1 < 0.0:
        reasons.append("load1 invalid")
    elif (
        not isinstance(cpu_count, int)
        or isinstance(cpu_count, bool)
        or cpu_count <= 0
        or not _is_finite_number(cpu_count)
    ):
        reasons.append("cpu_count unavailable")
    elif maximum_load_per_cpu is not None:
        cutoff = cpu_count * maximum_load_per_cpu
        if load1 > cutoff:
            reasons.append(f"load1 {load1:.3f} > {cutoff:.3f}")

    if disk_free is None:
        reasons.append("disk_free unavailable")
    elif not _is_finite_number(disk_free) or disk_free < 0:
        reasons.append("disk_free invalid")
    elif minimum_disk_free is not None and disk_free < minimum_disk_free:
        reasons.append(f"disk_free {disk_free} < {minimum_disk_free}")

    return not reasons, reasons


def can_run_heavy(thresholds: dict | None = None) -> tuple[bool, list[str]]:
    """Check whether the current host meets the heavy-operation budget."""
    snapshot = health()
    memory = snapshot["mem"]
    return decide_heavy_work(
        memory["available_frac"],
        snapshot["load1"],
        snapshot["cpu_count"],
        snapshot["disk"]["free"],
        thresholds,
    )


def format_health(snapshot: dict[str, Any]) -> str:
    """Format a compact, one-line summary suitable for prompt injection."""
    memory = snapshot["mem"]
    available_frac = memory["available_frac"]
    memory_text = "n/a" if available_frac is None else f"{available_frac:.0%}"

    load1 = snapshot["load1"]
    load_text = "n/a" if load1 is None else f"{load1:.1f}"
    cpu_count = snapshot["cpu_count"]
    cpu_text = "?" if cpu_count is None else str(cpu_count)

    disk_free = snapshot["disk"]["free"]
    disk_text = "n/a" if disk_free is None else f"{disk_free / 1024**3:.0f}"
    return (
        f"Host health: mem {memory_text} avail, load {load_text}/{cpu_text} cores, "
        f"disk {disk_text} GiB free"
    )
