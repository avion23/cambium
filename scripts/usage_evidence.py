"""Per-provider routing evidence from durable session usage events.

Reads durable ``usage_event`` rows from one or more session event stores
(``cambium.supervisor.read_events``) and prints per-provider routing evidence
aggregated across sessions: request counts (success vs failed), prompt+
completion tokens, latency, estimated cost, provider-reported cache-hit rate,
prompt-prefix stability, Retry-After occurrences, request-rate status
distribution, failure reasons (top 5), and distinct account-quota owners.

Session dirs come from positional arguments and/or ``--repo <path>``, which
globs ``.cambium/sessions/*`` under the repo (the ``session_root`` layout).
A session with no ``usage_event`` is skipped in marker mode; a session dir
with no event DB is skipped with a warning to stderr. Usage-event payloads
are already redacted by the EventStore; this script adds nothing and prints
only the aggregated fields above, never a raw payload.

Exit status: 0 even when nothing usable was found (the report says so);
nonzero only on argument or read errors.

Run:
  PYTHONPATH=src python3.14 scripts/usage_evidence.py SESSION_DIR...
  PYTHONPATH=src python3.14 scripts/usage_evidence.py --repo /path/to/repo
  PYTHONPATH=src python3.14 scripts/usage_evidence.py --json SESSION_DIR...
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cambium.store import read_events_file  # noqa: E402
from cambium.supervisor import read_events  # noqa: E402

UNKNOWN_PROVIDER = "<unknown>"


def _number(value: object) -> float | None:
    """One numeric field value, or None for missing/non-numeric values."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _prompt_completion_tokens(usage: object) -> float | None:
    """Prompt+completion tokens from a usage payload, either token family."""
    if not isinstance(usage, dict):
        return None
    prompt = _number(usage.get("prompt_tokens"))
    completion = _number(usage.get("completion_tokens"))
    if prompt is not None and completion is not None:
        return prompt + completion
    return _number(usage.get("total_tokens"))


class _ProviderStats:
    """Aggregated routing evidence for one provider."""

    def __init__(self) -> None:
        self.success = 0
        self.failed = 0
        self.token_total = 0.0
        self.token_events = 0
        self.latency_total = 0.0
        self.latency_events = 0
        self.cost_total = 0.0
        self.cache_hits = 0
        self.cache_known = 0
        self.prefix_values: set[int] = set()
        self.retry_after = 0
        self.rate_status: Counter[str] = Counter()
        self.failure_reasons: Counter[str] = Counter()
        self.quota_owners: set[str] = set()
        self.sessions: set[str] = set()


def _record_event(stats: _ProviderStats, payload: dict[str, Any], session: str) -> None:
    """Fold one usage_event payload into the provider aggregate."""
    failure_reason = payload.get("failure_reason")
    if isinstance(failure_reason, str) and failure_reason:
        stats.failed += 1
        stats.failure_reasons[failure_reason] += 1
    else:
        stats.success += 1
    tokens = _prompt_completion_tokens(payload.get("usage"))
    if tokens is not None:
        stats.token_total += tokens
        stats.token_events += 1
    latency = _number(payload.get("latency_s"))
    if latency is not None:
        stats.latency_total += latency
        stats.latency_events += 1
    cost = _number(payload.get("estimated_cost_usd"))
    if cost is not None:
        stats.cost_total += cost
    cache_hit = payload.get("provider_cache_hit")
    if isinstance(cache_hit, bool):
        stats.cache_known += 1
        stats.cache_hits += int(bool(cache_hit))
    prefix = payload.get("prompt_prefix_bytes")
    if isinstance(prefix, int) and not isinstance(prefix, bool):
        stats.prefix_values.add(prefix)
    if payload.get("retry_after_s") is not None:
        stats.retry_after += 1
    rate_status = payload.get("request_rate_status")
    if isinstance(rate_status, str) and rate_status:
        stats.rate_status[rate_status] += 1
    quota_owner = payload.get("account_quota_owner")
    if isinstance(quota_owner, str) and quota_owner:
        stats.quota_owners.add(quota_owner)
    stats.sessions.add(session)


def _provider_name(payload: dict[str, Any]) -> str:
    provider = payload.get("provider")
    return provider if isinstance(provider, str) and provider else UNKNOWN_PROVIDER


def _session_usage_events(session_dir: Path) -> list[dict[str, Any]] | None:
    """Durable usage rows from a session root or its per-turn stores.

    Interactive sessions keep one EventStore under each ``turn-NNNN``
    directory, while one-shot sessions keep it directly under ``.cambium``.
    Some archived sessions use ``turn-NNNN/events.db`` without the state
    directory, so read those stores directly as a compatibility fallback.
    """
    root_db = session_dir / ".cambium" / "events.db"
    nested_turn_dbs = sorted(session_dir.glob("turn-*/.cambium/events.db"))
    direct_turn_dbs = sorted(session_dir.glob("turn-*/events.db"))
    direct_db = session_dir / "events.db"
    if not (root_db.is_file() or nested_turn_dbs or direct_turn_dbs or direct_db.is_file()):
        return None
    events = read_events(session_dir)
    if direct_db.is_file():
        events.extend(read_events_file(direct_db))
    for event_db in direct_turn_dbs:
        events.extend(read_events_file(event_db))
    return [event for event in events if event.get("kind") == "usage_event"]


def _resolve_sessions(sessions: list[str], repos: list[str]) -> list[Path]:
    """Deduped, sorted session dirs from positional args and --repo globs."""
    candidates: list[Path] = [Path(raw) for raw in sessions]
    for repo in repos:
        sessions_root = Path(repo).expanduser().resolve() / ".cambium" / "sessions"
        if sessions_root.is_dir():
            candidates.extend(sorted(child for child in sessions_root.iterdir() if child.is_dir()))
    seen: set[Path] = set()
    resolved: list[Path] = []
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        resolved.append(path)
    return sorted(resolved)


def _aggregate(
    session_dirs: list[Path],
) -> tuple[dict[str, _ProviderStats], list[dict[str, Any]], list[str]]:
    """Return (per-provider stats, per-session records, warnings)."""
    providers: dict[str, _ProviderStats] = {}
    sessions: list[dict[str, Any]] = []
    warnings: list[str] = []
    for session_dir in session_dirs:
        if not session_dir.is_dir():
            warnings.append(f"{session_dir}: not a directory; skipped")
            continue
        usage_events = _session_usage_events(session_dir)
        if usage_events is None:
            warnings.append(f"{session_dir}: no event DB; skipped")
            sessions.append(
                {"dir": str(session_dir), "usage_events": 0, "skipped": True, "missing_db": True}
            )
            continue
        if not usage_events:
            sessions.append(
                {"dir": str(session_dir), "usage_events": 0, "skipped": True, "missing_db": False}
            )
            continue
        for event in usage_events:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            provider = _provider_name(payload)
            stats = providers.setdefault(provider, _ProviderStats())
            _record_event(stats, payload, str(session_dir))
        sessions.append(
            {
                "dir": str(session_dir),
                "usage_events": len(usage_events),
                "skipped": False,
                "missing_db": False,
            }
        )
    return providers, sessions, warnings


def _stats_json(name: str, stats: _ProviderStats) -> dict[str, Any]:
    """Machine-readable aggregate for one provider."""
    token_mean = stats.token_total / stats.token_events if stats.token_events else None
    latency_mean = stats.latency_total / stats.latency_events if stats.latency_events else None
    cache_rate = stats.cache_hits / stats.cache_known if stats.cache_known else None
    prefix_min = min(stats.prefix_values) if stats.prefix_values else None
    prefix_max = max(stats.prefix_values) if stats.prefix_values else None
    return {
        "provider": name,
        "requests": {"success": stats.success, "failed": stats.failed},
        "tokens": {
            "prompt_plus_completion_total": stats.token_total,
            "mean": token_mean,
            "events": stats.token_events,
        },
        "mean_latency_s": latency_mean,
        "estimated_cost_usd_total": stats.cost_total,
        "provider_cache_hit": {
            "hits": stats.cache_hits,
            "calls_with_cache_field": stats.cache_known,
            "rate": cache_rate,
        },
        "prompt_prefix_bytes": {
            "min": prefix_min,
            "max": prefix_max,
            "distinct": sorted(stats.prefix_values),
        },
        "retry_after_s_occurrences": stats.retry_after,
        "request_rate_status": dict(sorted(stats.rate_status.items())),
        "failure_reason_counts": dict(stats.failure_reasons.most_common(5)),
        "account_quota_owner_distinct": sorted(stats.quota_owners),
        "sessions": sorted(stats.sessions),
    }


def _fmt_number(value: float | None, digits: int) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _print_text(
    providers: dict[str, _ProviderStats],
    sessions: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    examined = len(sessions)
    with_usage = sum(1 for s in sessions if not s["skipped"])
    missing_db = sum(1 for s in sessions if s["missing_db"])
    skipped = examined - with_usage - missing_db
    print(
        f"sessions examined: {examined} "
        f"({with_usage} with usage events, {skipped} skipped: no usage_event, "
        f"{missing_db} skipped: no event DB)"
    )
    if warnings:
        print("warnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  {warning}", file=sys.stderr)
    if not providers:
        print("no usage events found; nothing to aggregate")
        return
    for name in sorted(providers):
        stats = providers[name]
        token_mean = stats.token_total / stats.token_events if stats.token_events else None
        latency_mean = stats.latency_total / stats.latency_events if stats.latency_events else None
        cache_rate = stats.cache_hits / stats.cache_known if stats.cache_known else None
        prefix_min = min(stats.prefix_values) if stats.prefix_values else None
        prefix_max = max(stats.prefix_values) if stats.prefix_values else None
        print(f"provider: {name}")
        print(f"  requests: {stats.success} success, {stats.failed} failed")
        print(
            f"  tokens prompt+completion: total {stats.token_total:.1f}, "
            f"mean {_fmt_number(token_mean, 1)} ({stats.token_events} events)"
        )
        print(f"  mean latency_s: {_fmt_number(latency_mean, 4)}")
        print(f"  estimated_cost_usd total: {stats.cost_total:.6f}")
        print(
            f"  provider_cache_hit rate: {stats.cache_hits}/{stats.cache_known} "
            f"({_fmt_number(cache_rate, 3)})"
        )
        print(
            f"  prompt_prefix_bytes: min {_fmt_number(prefix_min, 0)}, "
            f"max {_fmt_number(prefix_max, 0)}, distinct {sorted(stats.prefix_values)}"
        )
        print(f"  retry_after_s occurrences: {stats.retry_after}")
        statuses = ", ".join(f"{key}={value}" for key, value in sorted(stats.rate_status.items()))
        print(f"  request_rate_status: {statuses or '(none)'}")
        reasons = ", ".join(
            f"{count}x {reason!r}" for reason, count in stats.failure_reasons.most_common(5)
        )
        print(f"  failure_reason (top 5): {reasons or '(none)'}")
        owners = ", ".join(sorted(stats.quota_owners))
        print(f"  account_quota_owner distinct: {owners or '(none)'}")
        print(f"  sessions: {', '.join(sorted(stats.sessions))}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Per-provider routing evidence from durable session usage events."
    )
    parser.add_argument("sessions", nargs="*", help="session dir(s) to read")
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="PATH",
        help="repo whose .cambium/sessions/* are read (repeatable)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable JSON document instead of text",
    )
    args = parser.parse_args(argv)
    session_dirs = _resolve_sessions(args.sessions, args.repo)
    providers, sessions, warnings = _aggregate(session_dirs)
    if args.json:
        report = {
            "providers": {
                name: _stats_json(name, stats) for name, stats in sorted(providers.items())
            },
            "sessions": sessions,
            "warnings": warnings,
        }
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(providers, sessions, warnings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
