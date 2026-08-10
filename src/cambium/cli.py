"""Unified command-line entry point for Cambium.

The subcommands are thin adapters around the existing module CLIs.  The
adapters keep each module's implementation and exit-code contract in one
place while providing one installed ``cambium`` command.
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .auth import (
    AuthError,
    AuthSchemaError,
    AuthStore,
    read_stdin_key,
    validate_provider_id,
)


class _SafeArgumentParser(argparse.ArgumentParser):
    """Do not echo arbitrary rejected tokens, which may be credentials."""

    def error(self, message: str) -> None:
        if "unrecognized arguments" in message:
            message = "invalid command arguments"
        super().error(message)


def _provider_argument(value: str) -> str:
    try:
        return validate_provider_id(value)
    except AuthSchemaError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _add_supervisor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-dir", required=True, metavar="DIR")
    plan = parser.add_mutually_exclusive_group()
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


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="cambium",
        description="Cambium multi-agent coding-agent harness",
    )
    commands = parser.add_subparsers(
        dest="command",
        metavar="{auth,supervisor,doctor,bench,tasktree,module-test,version}",
        required=True,
        parser_class=_SafeArgumentParser,
    )

    supervisor = commands.add_parser(
        "supervisor",
        help="run the supervisor",
        description="Run one Cambium supervisor session.",
    )
    _add_supervisor_arguments(supervisor)

    auth = commands.add_parser(
        "auth",
        help="manage Cambium provider credentials",
        description="Manage the fixed Cambium provider auth store.",
    )
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)

    auth_set = auth_commands.add_parser(
        "set",
        help="set one provider API key",
        description="Set one provider API key without placing it in argv.",
    )
    auth_set.add_argument("provider", type=_provider_argument, metavar="PROVIDER")
    auth_set.add_argument(
        "--stdin",
        action="store_true",
        help="read the API key from stdin instead of the terminal",
    )

    auth_remove = auth_commands.add_parser(
        "remove",
        help="remove one provider API key",
    )
    auth_remove.add_argument("provider", type=_provider_argument, metavar="PROVIDER")

    auth_commands.add_parser("list", help="list configured providers and derived names")

    auth_run = auth_commands.add_parser(
        "run",
        help="run one fixed launch profile",
        description="Run an authorized fixed Cambium profile.",
    )
    profiles = auth_run.add_subparsers(dest="profile", required=True)
    supervisor_profile = profiles.add_parser(
        "supervisor",
        help="run the Cambium supervisor with authorized provider keys",
    )
    _add_supervisor_arguments(supervisor_profile)

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
        mode_parser.add_argument("--full", action="store_true", help="full run")
        mode_parser.add_argument(
            "--drift-report",
            action="store_true",
            help="write a drift artifact to the baseline root",
        )
        mode_parser.add_argument("--bench-root", type=Path, metavar="PATH")
        mode_parser.add_argument("--bench-metric-delta", type=float, metavar="FLOAT")
        mode_parser.add_argument("--bench-wall-ratio", type=float, metavar="FLOAT")

    commands.add_parser(
        "tasktree",
        help="read a plan from a file or stdin and print its topological order",
    )
    module_test = commands.add_parser(
        "module-test",
        help="run one module's isolated conformance gate",
        description="Run the isolated conformance gate for one Cambium module.",
    )
    module_test.add_argument("name", metavar="NAME")
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


def _run_auth_set(args: argparse.Namespace) -> int:
    try:
        key = read_stdin_key() if args.stdin else getpass.getpass("API key: ")
        AuthStore().set_provider(args.provider, key)
    except (AuthError, EOFError, KeyboardInterrupt, OSError):
        print("cambium auth: could not store provider credential", file=sys.stderr)
        return 1
    print(f"stored provider {args.provider}")
    return 0


def _run_auth_remove(args: argparse.Namespace) -> int:
    try:
        removed = AuthStore().remove_provider(args.provider)
    except (AuthError, OSError):
        print("cambium auth: could not remove provider credential", file=sys.stderr)
        return 1
    if removed:
        print(f"removed provider {args.provider}")
    else:
        print(f"provider {args.provider} is not configured")
    return 0


def _run_auth_list() -> int:
    try:
        entries = AuthStore().listed_entries()
    except (AuthError, OSError):
        print("cambium auth: could not read provider credentials", file=sys.stderr)
        return 1
    for provider, env_name in entries:
        print(f"{provider}\t{env_name}")
    return 0


def _run_auth_supervisor(args: argparse.Namespace) -> int:
    try:
        environment = AuthStore().launch_environment()
    except (AuthError, OSError):
        print("cambium auth: could not prepare supervisor environment", file=sys.stderr)
        return 1

    executable = os.path.abspath(sys.executable)
    command = [executable, "-m", "cambium.supervisor", *_supervisor_args(args)]
    try:
        os.execve(executable, command, environment)
    except OSError:
        print("cambium auth: supervisor launch failed", file=sys.stderr)
        return 1
    return 0


def _run_auth(args: argparse.Namespace) -> int:
    if args.auth_command == "set":
        return _run_auth_set(args)
    if args.auth_command == "remove":
        return _run_auth_remove(args)
    if args.auth_command == "list":
        return _run_auth_list()
    if args.auth_command == "run" and args.profile == "supervisor":
        return _run_auth_supervisor(args)
    raise AssertionError(f"unhandled auth command: {args.auth_command!r}")


def _run_bench(args: argparse.Namespace) -> int:
    try:
        bench = importlib.import_module("cambium.bench")
    except ModuleNotFoundError as exc:
        if exc.name == "cambium.bench":
            print("cambium bench: cambium.bench is not installed", file=sys.stderr)
            return 1
        raise

    delegated = [args.bench_command]
    if args.full:
        delegated.append("--full")
    if args.drift_report:
        delegated.append("--drift-report")
    for option in ("bench_root", "bench_metric_delta", "bench_wall_ratio"):
        value = getattr(args, option)
        if value is not None:
            delegated.extend((f"--{option.replace('_', '-')}", str(value)))
    return bench.main(delegated)


def _run_module_test(args: argparse.Namespace) -> int:
    from . import module_conformance

    if args.name not in module_conformance.module_names():
        print(f"cambium module-test: unknown module {args.name!r}", file=sys.stderr)
        return 2

    tests_dir = module_conformance.MODULES_DIR / args.name / "tests"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "cambium.module_conformance",
        "--cambium-isolated-module",
        args.name,
        "--strict-config",
        "--strict-markers",
        "-q",
        str(tests_dir.resolve()),
    ]
    with module_conformance.module_offline_environment() as env:
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTEST_PLUGINS", None)
        result = subprocess.run(
            command,
            cwd=module_conformance.REPO_ROOT,
            env=env,
            check=False,
        )
    return 0 if result.returncode == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """Dispatch one unified Cambium CLI invocation and return its exit code."""
    command_line = sys.argv[1:] if argv is None else argv
    if command_line and command_line[0] == "tasktree":
        from . import tasktree

        return tasktree.main(command_line[1:])

    args = _build_parser().parse_args(command_line)

    if args.command == "supervisor":
        return _run_supervisor(args)
    if args.command == "auth":
        return _run_auth(args)
    if args.command == "doctor":
        return _run_doctor(args)
    if args.command == "bench":
        return _run_bench(args)
    if args.command == "module-test":
        return _run_module_test(args)
    if args.command == "version":
        print(__version__)
        return 0
    raise AssertionError(f"unhandled command: {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
