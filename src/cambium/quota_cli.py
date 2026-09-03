"""Operator CLI for provider quota-window observations and status."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .provider_scheduler import QuotaLedger, QuotaLedgerError, quota_snapshot_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium quota",
        description="Inspect or update content-free provider quota windows.",
    )
    parser.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="quota_command", required=True)
    status = commands.add_parser("status", help="show known provider quota windows")
    status.add_argument("--provider")
    status.add_argument("--json", action="store_true")
    observe = commands.add_parser(
        "observe",
        help="record a provider/dashboard/header quota observation",
    )
    observe.add_argument("provider")
    observe.add_argument("window")
    observe.add_argument("--reset-in-s", type=float, required=True)
    observe.add_argument("--allowance-tokens", type=int, default=0)
    observe.add_argument("--remaining-tokens", type=int)
    observe.add_argument("--allowance-requests", type=int, default=0)
    observe.add_argument("--remaining-requests", type=int)
    observe.add_argument("--reserve-fraction", type=float, default=0.0)
    return parser


def _format_reset(reset_at: float) -> str:
    return datetime.fromtimestamp(reset_at, UTC).isoformat().replace("+00:00", "Z")


def run_namespace(args: argparse.Namespace) -> int:
    ledger = QuotaLedger(getattr(args, "db", None))
    command = args.quota_command
    if command == "observe":
        if args.reset_in_s <= 0:
            raise ValueError("--reset-in-s must be positive")
        ledger.observe(
            args.provider,
            args.window,
            reset_at=time.time() + args.reset_in_s,
            allowance_tokens=args.allowance_tokens,
            remaining_tokens=args.remaining_tokens,
            allowance_requests=args.allowance_requests,
            remaining_requests=args.remaining_requests,
            reserve_fraction=args.reserve_fraction,
        )
        return 0
    snapshots = ledger.snapshots(getattr(args, "provider", None))
    if getattr(args, "json", False):
        print(json.dumps([quota_snapshot_json(item) for item in snapshots], sort_keys=True))
        return 0
    if not snapshots:
        print("no provider quota observations")
        return 0
    now = time.time()
    for item in snapshots:
        reset_in = max(0, int(item.reset_at - now))
        tokens = (
            "unbounded"
            if item.remaining_tokens is None
            else f"{item.remaining_tokens}/{item.allowance_tokens} tokens"
        )
        requests = (
            "unbounded"
            if item.remaining_requests is None
            else f"{item.remaining_requests}/{item.allowance_requests} requests"
        )
        print(
            f"{item.provider}/{item.name}: {tokens}, {requests}, "
            f"reset={_format_reset(item.reset_at)} ({reset_in}s)"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run_namespace(args)
    except (OSError, QuotaLedgerError, ValueError) as exc:
        print(f"cambium quota: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
