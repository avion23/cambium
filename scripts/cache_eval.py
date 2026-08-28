"""Report prompt-cache usage and estimated cost from Cambium sessions.

The report reads the existing durable ``usage_event`` rows through the same
reader as ``usage_evidence.py``.  Cache-hit percentage is token based:
``cached_tokens / prompt_tokens``.  Pricing comes from the provider config
when its tariffs are known; otherwise the cost is reported as
``subscription`` rather than pretending that zero is a measured price.

Run:
  python scripts/cache_eval.py SESSION_DIR...
  python scripts/cache_eval.py --repo PATH --provider-config PATH --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from cambium.provider_config import DEFAULT_PROVIDER_PATH, load_providers  # noqa: E402
from scripts.usage_evidence import (  # noqa: E402
    _resolve_sessions,
    _session_usage_events,
)

UNKNOWN_PROVIDER = "<unknown>"
_MILLION = 1_000_000


def _number(value: object) -> float | None:
    """Return a finite, non-negative usage number, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _usage_number(usage: Mapping[str, Any], *fields: str) -> float:
    """Return the first valid number for a provider field family."""
    for field_name in fields:
        value = _number(usage.get(field_name))
        if value is not None:
            return value
    return 0.0


def _usage_tokens(payload: Mapping[str, Any]) -> tuple[float, float, float]:
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return 0.0, 0.0, 0.0
    prompt = _usage_number(usage, "prompt_tokens", "input_tokens")
    output = _usage_number(usage, "output_tokens", "completion_tokens")
    cached = 0.0
    for details_name in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_name)
        if isinstance(details, Mapping):
            cached_value = _number(details.get("cached_tokens"))
            if cached_value is not None:
                cached = cached_value
                break
    else:
        cached = _usage_number(usage, "cache_read_input_tokens", "cached_tokens")
    return prompt, cached, output


@dataclass(slots=True)
class _Stats:
    calls: int = 0
    prompt_tokens: float = 0.0
    cached_tokens: float = 0.0
    output_tokens: float = 0.0
    sessions: set[str] = field(default_factory=set)

    def record(self, payload: Mapping[str, Any], session: str) -> None:
        prompt, cached, output = _usage_tokens(payload)
        self.calls += 1
        self.prompt_tokens += prompt
        self.cached_tokens += cached
        self.output_tokens += output
        self.sessions.add(session)

    def merge(self, other: _Stats) -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.cached_tokens += other.cached_tokens
        self.output_tokens += other.output_tokens
        self.sessions.update(other.sessions)


@dataclass(frozen=True, slots=True)
class _Pricing:
    input_per_million: float
    cached_input_per_million: float
    output_per_million: float
    known: bool

    def cost(self, stats: _Stats) -> float | None:
        if not self.known:
            return None
        uncached = max(0.0, stats.prompt_tokens - stats.cached_tokens)
        return round(
            (
                uncached * self.input_per_million
                + stats.cached_tokens * self.cached_input_per_million
                + stats.output_tokens * self.output_per_million
            )
            / _MILLION,
            12,
        )


@dataclass(slots=True)
class _Session:
    path: Path
    usage_events: int = 0
    missing_db: bool = False
    total: _Stats = field(default_factory=_Stats)
    providers: dict[str, _Stats] = field(default_factory=dict)


def _provider_name(payload: Mapping[str, Any]) -> str:
    provider = payload.get("provider")
    return provider if isinstance(provider, str) and provider else UNKNOWN_PROVIDER


def _record_session(path: Path) -> _Session:
    session = _Session(path)
    if not path.is_dir():
        return session
    usage_events = _session_usage_events(path)
    if usage_events is None:
        session.missing_db = True
        return session
    session.usage_events = len(usage_events)
    for event in usage_events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        provider = _provider_name(payload)
        provider_stats = session.providers.setdefault(provider, _Stats())
        provider_stats.record(payload, str(path))
        session.total.record(payload, str(path))
    return session


def _add_config_path(paths: list[Path], raw: object, base: Path) -> None:
    if not isinstance(raw, str) or not raw:
        return
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if path not in paths:
        paths.append(path)


def _plan_config_path(session: _Session, paths: list[Path]) -> None:
    plan = session.path / "plan.json"
    try:
        value = json.loads(plan.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return

    def visit(node: object) -> None:
        if isinstance(node, Mapping):
            _add_config_path(paths, node.get("provider_config_path"), session.path)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)


def _config_paths(sessions: list[_Session], explicit: list[Path]) -> list[Path]:
    if explicit:
        paths: list[Path] = []
        for path in explicit:
            _add_config_path(paths, str(path), Path.cwd())
        return paths
    configured = os.environ.get("CAMBIUM_PROVIDERS")
    if configured:
        paths = []
        _add_config_path(paths, configured, Path.cwd())
        return paths
    paths = []
    for session in sessions:
        _plan_config_path(session, paths)
    return paths or [DEFAULT_PROVIDER_PATH]


def _provider_pricing(paths: list[Path]) -> tuple[dict[str, _Pricing], list[str]]:
    pricing: dict[str, _Pricing] = {}
    warnings: list[str] = []
    for path in paths:
        try:
            providers = load_providers(path)
        except (OSError, ValueError) as exc:
            warnings.append(f"{path}: provider config unavailable: {exc}")
            continue
        for provider in providers:
            name = getattr(provider, "name", None)
            if not isinstance(name, str) or not name or name in pricing:
                continue
            values = tuple(
                _number(getattr(provider, field_name, None)) or 0.0
                for field_name in (
                    "price_per_1m_in",
                    "price_per_1m_cached_in",
                    "price_per_1m_out",
                )
            )
            capability = getattr(provider, "cache_capability", None)
            cache_read = _number(getattr(capability, "cache_read_price", None))
            cached_price = values[1]
            if cache_read is not None and cache_read > 0:
                cached_price = cache_read
            billing = getattr(getattr(provider, "billing_mode", None), "value", None)
            has_tariff = any(values) or (cache_read is not None and cache_read > 0)
            known = bool(getattr(provider, "pricing_known", False)) or has_tariff
            if billing == "subscription" and not has_tariff:
                known = False
            pricing[name] = _Pricing(
                input_per_million=values[0],
                cached_input_per_million=cached_price,
                output_per_million=values[2],
                known=known,
            )
    return pricing, warnings


def _cache_hit_percent(stats: _Stats) -> float | None:
    if stats.prompt_tokens <= 0:
        return None
    return round(min(1.0, stats.cached_tokens / stats.prompt_tokens) * 100, 6)


def _json_number(value: float) -> int | float:
    try:
        integer = int(value)
    except (OverflowError, ValueError):
        return value
    return integer if value == integer else value


def _metrics(
    stats: _Stats,
    pricing: _Pricing | None,
    *,
    cost_override: float | None = None,
    source_override: str | None = None,
) -> dict[str, Any]:
    cost = (
        cost_override if cost_override is not None else (pricing.cost(stats) if pricing else None)
    )
    source = source_override
    if source is None:
        source = (
            "none"
            if stats.calls == 0
            else ("provider_config" if cost is not None else "subscription")
        )
    return {
        "prompt_tokens": _json_number(stats.prompt_tokens),
        "cached_tokens": _json_number(stats.cached_tokens),
        "cache_hit_percent": _cache_hit_percent(stats),
        "output_tokens": _json_number(stats.output_tokens),
        "calls": stats.calls,
        "estimated_cost": _json_number(cost) if cost is not None else "subscription",
        "estimated_cost_usd": cost,
        "cost_source": source,
    }


def _session_cost(session: _Session, pricing: dict[str, _Pricing]) -> tuple[float | None, str]:
    if not session.providers:
        return 0.0, "none"
    costs: list[float | None] = []
    for name, stats in session.providers.items():
        provider_pricing = pricing.get(name)
        costs.append(provider_pricing.cost(stats) if provider_pricing is not None else None)
    if any(cost is None for cost in costs):
        return None, "subscription"
    return sum(cost for cost in costs if cost is not None), "provider_config"


def _report(
    sessions: list[_Session], pricing: dict[str, _Pricing], warnings: list[str]
) -> dict[str, Any]:
    providers: dict[str, _Stats] = {}
    for session in sessions:
        for name, stats in session.providers.items():
            providers.setdefault(name, _Stats()).merge(stats)

    session_rows: list[dict[str, Any]] = []
    for session in sessions:
        cost, source = _session_cost(session, pricing)
        row = {
            "dir": str(session.path),
            "usage_events": session.usage_events,
            "skipped": not bool(session.providers),
            "missing_db": session.missing_db,
            **_metrics(session.total, None, cost_override=cost, source_override=source),
            "providers": {
                name: _metrics(stats, pricing.get(name))
                for name, stats in sorted(session.providers.items())
            },
        }
        session_rows.append(row)

    provider_rows: dict[str, dict[str, Any]] = {}
    for name, stats in sorted(providers.items()):
        row = _metrics(stats, pricing.get(name))
        row["provider"] = name
        row["sessions"] = sorted(stats.sessions)
        provider_rows[name] = row
    return {"sessions": session_rows, "providers": provider_rows, "warnings": warnings}


def _fmt_count(value: int | float) -> str:
    if isinstance(value, int) or value.is_integer():
        return f"{value:,.0f}"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def _fmt_percent(value: object) -> str:
    return "n/a" if not isinstance(value, int | float) else f"{value:.1f}%"


def _fmt_cost(value: object) -> str:
    return value if isinstance(value, str) else f"${value:.6f}"


def _print_table(report: dict[str, Any]) -> None:
    for warning in report["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    print("session cache evaluation")
    print("  provider | prompt | cached | cache-hit % | output | calls | estimated cost")
    for session in report["sessions"]:
        print(f"session: {session['dir']}")
        for name, metrics in session["providers"].items():
            print(
                f"  {name} | {_fmt_count(metrics['prompt_tokens'])} | "
                f"{_fmt_count(metrics['cached_tokens'])} | "
                f"{_fmt_percent(metrics['cache_hit_percent'])} | "
                f"{_fmt_count(metrics['output_tokens'])} | {metrics['calls']} | "
                f"{_fmt_cost(metrics['estimated_cost'])}"
            )
        print(
            f"  total | {_fmt_count(session['prompt_tokens'])} | "
            f"{_fmt_count(session['cached_tokens'])} | "
            f"{_fmt_percent(session['cache_hit_percent'])} | "
            f"{_fmt_count(session['output_tokens'])} | {session['calls']} | "
            f"{_fmt_cost(session['estimated_cost'])}"
        )
    if report["providers"]:
        print("provider totals")
        for name, metrics in report["providers"].items():
            print(
                f"  {name}: prompt={_fmt_count(metrics['prompt_tokens'])} "
                f"cached={_fmt_count(metrics['cached_tokens'])} "
                f"cache-hit={_fmt_percent(metrics['cache_hit_percent'])} "
                f"output={_fmt_count(metrics['output_tokens'])} "
                f"calls={metrics['calls']} cost={_fmt_cost(metrics['estimated_cost'])}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report session cache-hit and cost metrics.")
    parser.add_argument("sessions", nargs="*", help="session dir(s) to read")
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="PATH",
        help="repo whose .cambium/sessions/* are read (repeatable)",
    )
    parser.add_argument(
        "--provider-config",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help="provider config supplying token prices (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    session_dirs = _resolve_sessions(args.sessions, args.repo)
    session_records = [_record_session(path) for path in session_dirs]
    config_paths = _config_paths(session_records, args.provider_config)
    pricing, config_warnings = _provider_pricing(config_paths)
    warnings = list(config_warnings)
    for session in session_records:
        if not session.path.is_dir():
            warnings.append(f"{session.path}: not a directory; skipped")
        elif session.missing_db:
            warnings.append(f"{session.path}: no event DB; skipped")
    report = _report(session_records, pricing, warnings)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
