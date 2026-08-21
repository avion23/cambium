"""Measure provider cache evidence by context-reuse call class.

Reads durable ``usage_event`` rows and separates calls into parent baseline,
fork first/later turn, and resume first/later turn buckets. The provider cache
is treated as an observation only: this script does not add cache-control
fields, select providers, or require a cache hit. It also reports the token
economics around each durable epoch boundary. Boundary prefix metadata comes
from ``event["payload"]["cache_key"]["prefix_bytes"]`` and
``event["payload"]["cache_key"]["prefix_sha256"]`` on
``context_checkpoint`` and ``context_epoch_advanced`` rows. Usage token
metadata comes from the nearest same-task, same-generation ``usage_event``
rows before and after the boundary.

Run:
  PYTHONPATH=src python3.14 scripts/context_cache_evidence.py SESSION_DIR...
  PYTHONPATH=src python3.14 scripts/context_cache_evidence.py --repo PATH --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cambium.supervisor import read_events  # noqa: E402

UNKNOWN_PROVIDER = "<unknown>"
BUCKETS = ("baseline", "fork_first", "fork_later", "resume_first", "resume_later")
BOUNDARY_KINDS = ("context_checkpoint", "context_epoch_advanced")
DEFAULT_THRESHOLD = 0.8


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _provider_name(payload: dict[str, Any]) -> str:
    provider = payload.get("provider")
    return provider if isinstance(provider, str) and provider else UNKNOWN_PROVIDER


def _task_id(event: dict[str, Any], payload: dict[str, Any]) -> str:
    for source in (event, payload):
        value = source.get("task_id")
        if isinstance(value, str) and value:
            return value
    return "<unknown-task>"


def _generation(event: dict[str, Any]) -> int | None:
    generation = event.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int):
        return None
    return generation if generation > 0 else None


def _session_events(session_dir: Path) -> list[dict[str, Any]] | None:
    if not (session_dir / ".cambium" / "events.db").is_file():
        return None
    return read_events(session_dir)


def _session_usage_events(session_dir: Path) -> list[dict[str, Any]] | None:
    events = _session_events(session_dir)
    if events is None:
        return None
    return [event for event in events if event.get("kind") == "usage_event"]


def _resolve_sessions(sessions: list[str], repos: list[str]) -> list[Path]:
    candidates = [Path(raw) for raw in sessions]
    for repo in repos:
        root = Path(repo).expanduser().resolve() / ".cambium" / "sessions"
        if root.is_dir():
            candidates.extend(child for child in root.iterdir() if child.is_dir())
    seen: set[Path] = set()
    resolved: list[Path] = []
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path not in seen:
            seen.add(path)
            resolved.append(path)
    return sorted(resolved)


@dataclass(slots=True)
class _BucketStats:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    cache_hits: int = 0
    cache_known: int = 0
    prefix_bytes: set[int] = field(default_factory=set)
    epochs: set[int] = field(default_factory=set)
    fork_refs: set[str] = field(default_factory=set)
    sessions: set[str] = field(default_factory=set)

    def record(self, payload: dict[str, Any], session: str) -> None:
        self.calls += 1
        if isinstance(payload.get("failure_reason"), str):
            self.failures += 1
        else:
            self.successes += 1
        cache_hit = payload.get("provider_cache_hit")
        if isinstance(cache_hit, bool):
            self.cache_known += 1
            self.cache_hits += int(cache_hit)
        prefix = payload.get("prompt_prefix_bytes")
        if isinstance(prefix, int) and not isinstance(prefix, bool) and prefix >= 0:
            self.prefix_bytes.add(prefix)
        epoch = payload.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch > 0:
            self.epochs.add(epoch)
        fork_of = payload.get("fork_of")
        if isinstance(fork_of, str) and fork_of:
            self.fork_refs.add(fork_of)
        self.sessions.add(session)


def _classify_events(
    usage_events: list[dict[str, Any]], session: str
) -> list[tuple[str, dict[str, Any]]]:
    fork_seen: set[tuple[str, int | None, str]] = set()
    resume_seen: set[tuple[str, int | None, int]] = set()
    classified: list[tuple[str, dict[str, Any]]] = []
    for event in usage_events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        task_id = _task_id(event, payload)
        generation = _generation(event)
        fork_of = payload.get("fork_of")
        if isinstance(fork_of, str) and fork_of:
            key = (task_id, generation, fork_of)
            first = key not in fork_seen
            fork_seen.add(key)
            classified.append(("fork_first" if first else "fork_later", payload))
            continue
        epoch = payload.get("epoch")
        if isinstance(epoch, int) and not isinstance(epoch, bool) and epoch > 0:
            key = (task_id, generation, epoch)
            first = key not in resume_seen
            resume_seen.add(key)
            classified.append(("resume_first" if first else "resume_later", payload))
            continue
        classified.append(("baseline", payload))
    return classified


def _event_sequence(event: dict[str, Any]) -> int | None:
    sequence = event.get("seq")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        return None
    return sequence


def _ordered_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return event-log order without making missing synthetic seq values random."""
    if not all(_event_sequence(event) is not None for event in events):
        return list(events)
    return [
        event
        for _, event in sorted(
            enumerate(events),
            key=lambda item: (_event_sequence(item[1]), item[0]),
        )
    ]


def _positive_int(source: dict[str, Any], field: str) -> int | None:
    value = source.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _nonnegative_int(source: dict[str, Any], field: str) -> int | None:
    value = source.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _nonnegative_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if value >= 0 else None


def _usage_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    usage = payload.get("usage")
    return usage if isinstance(usage, dict) else {}


def _usage_value(
    usage: dict[str, Any], field: str
) -> int | float | None:
    return _nonnegative_number(usage.get(field))


def _usage_evidence(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    usage = _usage_mapping(payload)
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    if prompt_tokens is None:
        prompt_tokens = _usage_value(usage, "input_tokens")
    cached_tokens = _usage_value(usage, "cached_tokens")
    uncached_tokens: int | float | None = None
    if prompt_tokens is not None and cached_tokens is not None:
        remainder = prompt_tokens - cached_tokens
        if remainder >= 0:
            uncached_tokens = remainder
    return {
        "seq": _event_sequence(event),
        "turn": _positive_int(payload, "turn"),
        "prompt_prefix_bytes": _nonnegative_int(payload, "prompt_prefix_bytes"),
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "uncached_tokens": uncached_tokens,
        "completion_tokens": _usage_value(usage, "completion_tokens"),
        "total_tokens": _usage_value(usage, "total_tokens"),
        "latency_s": _nonnegative_number(payload.get("latency_s")),
    }


def _boundary_metadata(
    payload: dict[str, Any],
) -> tuple[int | None, str | None, list[str]]:
    """Read the canonical prefix length and hash from a boundary cache key."""
    cache_key = payload.get("cache_key")
    if not isinstance(cache_key, dict):
        return None, None, ["cache_key"]
    prefix_bytes = _nonnegative_int(cache_key, "prefix_bytes")
    prefix_sha256 = cache_key.get("prefix_sha256")
    if not isinstance(prefix_sha256, str) or not prefix_sha256:
        prefix_sha256 = None
    missing: list[str] = []
    if prefix_bytes is None:
        missing.append("cache_key.prefix_bytes")
    if prefix_sha256 is None:
        missing.append("cache_key.prefix_sha256")
    return prefix_bytes, prefix_sha256, missing


def _boundary_turn(payload: dict[str, Any]) -> int | None:
    return _positive_int(payload, "turn")


def _epoch_transitions(
    events: list[dict[str, Any]], session: str
) -> list[dict[str, Any]]:
    """Derive deterministic transition evidence from one replayed event list."""
    ordered = _ordered_events(events)
    usage_by_stream: dict[tuple[str, int | None], list[tuple[int, dict[str, Any]]]] = {}
    for position, event in enumerate(ordered):
        if event.get("kind") != "usage_event":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        stream = (
            _task_id(event, payload),
            _generation(event),
        )
        usage_by_stream.setdefault(stream, []).append((position, event))

    prior_epochs: dict[tuple[str, int | None], int] = {}
    transitions: list[dict[str, Any]] = []
    for position, event in enumerate(ordered):
        kind = event.get("kind")
        if kind not in BOUNDARY_KINDS:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        stream = (
            _task_id(event, payload),
            _generation(event),
        )
        epoch = _positive_int(payload, "epoch")
        folded_from_epoch = _positive_int(payload, "folded_from_epoch")
        from_epoch = folded_from_epoch
        if from_epoch is None:
            from_epoch = prior_epochs.get(stream)
        if from_epoch is None and epoch is not None:
            from_epoch = epoch - 1
        prefix_bytes, prefix_sha256, metadata_missing = _boundary_metadata(payload)
        if epoch is None:
            metadata_missing = [*metadata_missing, "epoch"]
        boundary_turn = _boundary_turn(payload)
        same_stream_usage = usage_by_stream.get(stream, [])
        before_event = next(
            (usage for usage_position, usage in reversed(same_stream_usage)
             if usage_position < position),
            None,
        )
        after_event = next(
            (usage for usage_position, usage in same_stream_usage
             if usage_position > position),
            None,
        )
        before = _usage_evidence(before_event) if before_event is not None else None
        after = _usage_evidence(after_event) if after_event is not None else None
        record = {
            "session": session,
            "task_id": stream[0],
            "generation": stream[1],
            "kind": kind,
            "seq": _event_sequence(event),
            "from_epoch": from_epoch,
            "to_epoch": epoch,
            "epoch": epoch,
            "folded_from_epoch": folded_from_epoch,
            "turn": boundary_turn,
            "prefix_bytes": prefix_bytes,
            "prefix_sha256": prefix_sha256,
            "prefix_bytes_before": (
                before["prompt_prefix_bytes"] if before is not None else None
            ),
            "prefix_bytes_after": (
                after["prompt_prefix_bytes"] if after is not None else None
            ),
            "usage_before": before,
            "usage_after": after,
            "boundary_metadata_missing": sorted(set(metadata_missing)),
        }
        transitions.append(record)
        if epoch is not None:
            prior_epochs[stream] = epoch
    return transitions


def _aggregate(
    session_dirs: list[Path],
) -> tuple[dict[str, dict[str, _BucketStats]], list[dict[str, Any]], list[str]]:
    providers: dict[str, dict[str, _BucketStats]] = {}
    sessions: list[dict[str, Any]] = []
    warnings: list[str] = []
    for session_dir in sorted(session_dirs):
        if not session_dir.is_dir():
            warnings.append(f"{session_dir}: not a directory; skipped")
            continue
        all_events = _session_events(session_dir)
        if all_events is None:
            # Keep the existing seam used by callers/tests that provide only
            # usage rows for a session without creating an events.db.
            all_events = _session_usage_events(session_dir)
        usage_events = (
            [event for event in all_events if event.get("kind") == "usage_event"]
            if all_events is not None else None
        )
        if all_events is None or usage_events is None:
            warnings.append(f"{session_dir}: no event DB; skipped")
            sessions.append({
                "dir": str(session_dir), "usage_events": 0,
                "usable_events": 0, "skipped": True, "missing_db": True,
            })
            continue
        transitions = _epoch_transitions(all_events or [], str(session_dir))
        usable = _classify_events(usage_events, str(session_dir))
        for bucket, payload in usable:
            provider = _provider_name(payload)
            provider_buckets = providers.setdefault(
                provider, {name: _BucketStats() for name in BUCKETS}
            )
            provider_buckets[bucket].record(payload, str(session_dir))
        session_record: dict[str, Any] = {
            "dir": str(session_dir), "usage_events": len(usage_events),
            "usable_events": len(usable), "skipped": not bool(usable),
            "missing_db": False,
        }
        if transitions:
            session_record["epoch_transitions"] = transitions
            missing = sorted({
                f"seq={transition['seq']}: {field}"
                for transition in transitions
                for field in transition["boundary_metadata_missing"]
            })
            if missing:
                session_record["boundary_metadata_missing"] = missing
                warnings.append(
                    f"{session_dir}: boundary metadata missing: {', '.join(missing)}"
                )
        sessions.append(session_record)
    return providers, sessions, warnings


def _cache_rate(stats: _BucketStats) -> float | None:
    if not stats.cache_known:
        return None
    return stats.cache_hits / stats.cache_known


def _bucket_json(stats: _BucketStats) -> dict[str, Any]:
    return {
        "calls": stats.calls,
        "requests": {"success": stats.successes, "failed": stats.failures},
        "provider_cache_hit": {
            "hits": stats.cache_hits,
            "calls_with_cache_field": stats.cache_known,
            "rate": _cache_rate(stats),
        },
        "prompt_prefix_bytes": {
            "distinct": sorted(stats.prefix_bytes),
            "stable": len(stats.prefix_bytes) <= 1 if stats.prefix_bytes else None,
        },
        "epochs": sorted(stats.epochs),
        "fork_refs": sorted(stats.fork_refs),
        "sessions": sorted(stats.sessions),
    }


def _relative_rate(bucket: _BucketStats, baseline: _BucketStats) -> float | None:
    baseline_rate = _cache_rate(baseline)
    bucket_rate = _cache_rate(bucket)
    if baseline_rate is None or baseline_rate <= 0 or bucket_rate is None:
        return None
    return bucket_rate / baseline_rate


def _comparison_json(
    buckets: dict[str, _BucketStats], threshold: float
) -> dict[str, Any]:
    baseline = buckets["baseline"]
    baseline_rate = _cache_rate(baseline)
    result: dict[str, Any] = {
        "baseline_cache_hit_rate": baseline_rate,
        "minimum_relative_rate": threshold,
    }
    for name in ("fork_first", "resume_first"):
        relative = _relative_rate(buckets[name], baseline)
        result[name] = {
            "cache_hit_rate": _cache_rate(buckets[name]),
            "relative_to_baseline": relative,
            "meets_threshold": (
                None if relative is None else relative >= threshold
            ),
        }
    return result


def _report(
    providers: dict[str, dict[str, _BucketStats]],
    sessions: list[dict[str, Any]],
    warnings: list[str],
    threshold: float,
) -> dict[str, Any]:
    transitions = sorted(
        (
            transition
            for session in sessions
            for transition in session.get("epoch_transitions", [])
        ),
        key=lambda transition: (
            transition["session"],
            transition["seq"] is None,
            transition["seq"] if transition["seq"] is not None else 0,
        ),
    )
    sessions_lacking_boundary_metadata = sorted(
        session["dir"]
        for session in sessions
        if session.get("boundary_metadata_missing")
    )
    return {
        "measurement": {
            "classification": {
                "baseline": "usage event without epoch or fork_of",
                "fork_first": "first usage event for a task/checkpoint fork",
                "fork_later": "later usage event for the same task/checkpoint fork",
                "resume_first": "first usage event for a task/epoch resume",
                "resume_later": "later usage event for the same task/epoch resume",
            },
            "minimum_relative_rate": threshold,
            "cache_policy_changed": False,
            "epoch_transition": {
                "boundary_kinds": list(BOUNDARY_KINDS),
                "prefix_bytes_source": "payload.cache_key.prefix_bytes",
                "prefix_sha256_source": "payload.cache_key.prefix_sha256",
                "usage_source": "usage_event.payload.usage",
            },
        },
        "providers": {
            provider: {
                "buckets": {
                    bucket: _bucket_json(stats)
                    for bucket, stats in buckets.items()
                },
                "comparison": _comparison_json(buckets, threshold),
            }
            for provider, buckets in sorted(providers.items())
        },
        "epoch_transitions": transitions,
        "sessions_lacking_boundary_metadata": sessions_lacking_boundary_metadata,
        "sessions": sessions,
        "warnings": warnings,
    }


def _fmt_rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _print_text(report: dict[str, Any]) -> None:
    sessions = report["sessions"]
    threshold = report["measurement"]["minimum_relative_rate"]
    print(f"sessions examined: {len(sessions)}")
    for warning in report["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    for provider, details in report["providers"].items():
        comparison = details["comparison"]
        print(f"provider: {provider}")
        print(
            "  baseline cache rate: "
            f"{_fmt_rate(comparison['baseline_cache_hit_rate'])}"
        )
        for bucket in ("fork_first", "resume_first"):
            item = comparison[bucket]
            status = item["meets_threshold"]
            print(
                f"  {bucket} cache rate: {_fmt_rate(item['cache_hit_rate'])}; "
                f"relative baseline: {_fmt_rate(item['relative_to_baseline'])}; "
                f"meets {threshold:.0%} gate: "
                f"{status if status is not None else 'n/a'}"
            )
        for bucket in BUCKETS:
            details_bucket = details["buckets"][bucket]
            print(
                f"  {bucket}: {details_bucket['calls']} calls, "
                f"prefixes={details_bucket['prompt_prefix_bytes']['distinct']}"
            )
    transitions = report["epoch_transitions"]
    print(f"epoch transitions: {len(transitions)}")
    for transition in transitions:
        print(
            f"  {transition['session']} {transition['kind']} "
            f"epoch {transition['from_epoch']}->{transition['to_epoch']} "
            f"turn={transition['turn']} prefix_bytes={transition['prefix_bytes']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure provider cache evidence by context-reuse call class."
    )
    parser.add_argument("sessions", nargs="*", help="session dir(s) to read")
    parser.add_argument(
        "--repo", action="append", default=[], metavar="PATH",
        help="repo whose .cambium/sessions/* are read (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD,
        help="minimum contextual hit-rate fraction relative to baseline (default: 0.8)",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.threshold) or not 0 <= args.threshold <= 1:
        parser.error("--threshold must be finite and between 0 and 1")
    session_dirs = _resolve_sessions(args.sessions, args.repo)
    providers, sessions, warnings = _aggregate(session_dirs)
    report = _report(providers, sessions, warnings, args.threshold)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
