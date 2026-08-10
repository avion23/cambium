"""Unified command-line entry point for Cambium.

The subcommands are thin adapters around the existing module CLIs.  The
adapters keep each module's implementation and exit-code contract in one
place while providing one installed ``cambium`` command.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cambium",
        description="Cambium multi-agent coding-agent harness",
    )
    commands = parser.add_subparsers(
        dest="command",
        metavar="{supervisor,doctor,bench,tasktree,version}",
        required=True,
    )

    supervisor = commands.add_parser(
        "supervisor",
        help="run the supervisor",
        description="Run one Cambium supervisor session.",
    )
    supervisor.add_argument("--session-dir", required=True, metavar="DIR")
    plan = supervisor.add_mutually_exclusive_group()
    plan.add_argument(
        "--plan",
        metavar="PATH",
        help="plan JSON path (passed to supervisor plan mode when available)",
    )
    plan.add_argument(
        "--task-spec",
        metavar="PATH",
        help="task-spec JSON path (compatibility flag for the slice supervisor)",
    )

    doctor = commands.add_parser(
        "doctor",
        help="run harness diagnostics",
        description="Run Cambium harness diagnostics.",
    )
    doctor.add_argument("--session-dir", type=Path, metavar="DIR")

    bench = commands.add_parser(
        "bench",
        help="run benchmark report or gate",
        description="Run the Cambium benchmark plugin CLI.",
    )
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    for mode in ("report", "gate"):
        mode_parser = bench_commands.add_parser(mode, help=f"run the bench {mode}")
        mode_parser.add_argument("--bench-root", type=Path, metavar="PATH")
        mode_parser.add_argument("--bench-metric-delta", type=float, metavar="FLOAT")
        mode_parser.add_argument("--bench-wall-ratio", type=float, metavar="FLOAT")

    tasktree = commands.add_parser(
        "tasktree",
        help="read a plan from a file or stdin and print its topological order",
        description="Read one task plan JSON object from PLAN or stdin and run tasktree.",
    )
    tasktree.add_argument(
        "plan",
        nargs="?",
        metavar="PLAN",
        help="path to a plan JSON file; omit or use '-' to read stdin",
    )
    commands.add_parser("version", help="print the Cambium version")
    return parser


def _supervisor_args(args: argparse.Namespace) -> list[str]:
    delegated = ["--session-dir", args.session_dir]
    if args.task_spec is not None:
        delegated.extend(("--task-spec", args.task_spec))
    elif args.plan is not None:
        delegated_flag = "--plan" if _supervisor_has_plan_mode() else "--task-spec"
        delegated.extend((delegated_flag, args.plan))
    return delegated


def _supervisor_has_plan_mode() -> bool:
    """Return whether the installed supervisor exposes its plan runtime.

    The vertical-slice supervisor uses ``--task-spec``.  The full supervisor
    keeps that mode and adds ``--plan``.  The capability check lets this CLI
    run against either implementation without reimplementing supervision.
    """
    from . import supervisor

    return hasattr(supervisor, "run_plan")


def _run_supervisor(args: argparse.Namespace) -> int:
    from . import supervisor

    return supervisor.main(_supervisor_args(args))


def _run_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    delegated = [] if args.session_dir is None else ["--session-dir", str(args.session_dir)]
    return doctor.main(delegated)


def _run_bench(args: argparse.Namespace) -> int:
    try:
        bench = importlib.import_module("cambium.bench")
    except ModuleNotFoundError as exc:
        if exc.name == "cambium.bench":
            print("cambium bench: cambium.bench is not installed", file=sys.stderr)
            return 1
        raise

    delegated = [args.bench_command]
    for option in ("bench_root", "bench_metric_delta", "bench_wall_ratio"):
        value = getattr(args, option)
        if value is not None:
            delegated.extend((f"--{option.replace('_', '-')}", str(value)))
    return bench.main(delegated)


def main(argv: list[str] | None = None) -> int:
    """Dispatch one unified Cambium CLI invocation and return its exit code."""
    args = _build_parser().parse_args(argv)

    if args.command == "supervisor":
        return _run_supervisor(args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "bench":
        return _run_bench(args)
    if args.command == "tasktree":
        from . import tasktree

        return tasktree.main([] if args.plan is None else [args.plan])
    if args.command == "version":
        print(__version__)
        return 0
    raise AssertionError(f"unhandled command: {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
