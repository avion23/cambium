"""Unified command-line entry point for Cambium.

The subcommands are thin adapters around the existing module CLIs.  The
adapters keep each module's implementation and exit-code contract in one
place while providing one installed ``cambium`` command.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import inspect
import os
import sqlite3
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

    def parse_known_args(self, args=None, namespace=None):
        if args is not None and "--" in args and self.prog.endswith(" run"):
            self.error("invalid command arguments")
        return super().parse_known_args(args, namespace)

    def error(self, message: str) -> None:
        if "unrecognized arguments" in message or "invalid choice" in message:
            message = "invalid command arguments"
        super().error(message)


def _provider_argument(value: str) -> str:
    try:
        return validate_provider_id(value)
    except AuthSchemaError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


_COMMAND_NAMES = frozenset(
    {
        "auth",
        "supervisor",
        "doctor",
        "bench",
        "tasktree",
        "module-test",
        "version",
        "run",
        "repl",
        "tui",
        "session",
    }
)


# sysexits.h EX_TEMPFAIL: the session admission lock is held by another live
# supervisor. The condition is transient; callers may retry the same run.
_EXIT_SESSION_BUSY = 75


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


def _add_agent_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo",
        metavar="PATH",
        default=".",
        help="repository to work in (default: current directory)",
    )
    parser.add_argument("--session-dir", metavar="DIR", help="session directory")
    parser.add_argument(
        "--provider",
        type=_provider_argument,
        metavar="PROVIDER",
        help="provider id",
    )
    parser.add_argument("--model", metavar="MODEL", help="model name")


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "prompt",
        metavar="PROMPT",
        help="prompt to run against the repository",
    )
    _add_agent_arguments(parser)
    parser.add_argument(
        "--auto",
        action="store_true",
        help="route the run through the usage-debt selector (solution C): the "
        "supervisor picks (provider, model, tier) from all enabled configured "
        "providers with stored credentials instead of pinning --provider/--model",
    )
    parser.add_argument(
        "--max-wall-s",
        type=_positive_float,
        metavar="SECONDS",
        help="per-task wall-clock budget in seconds (default 300)",
    )
    parser.add_argument(
        "--max-tokens",
        type=_positive_int,
        metavar="N",
        help="total token budget across the run (default 200000)",
    )
    parser.add_argument(
        "--max-turns",
        type=_positive_int,
        metavar="N",
        help="maximum agent-loop turns (default 20)",
    )
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="cambium",
        description="Cambium multi-agent coding-agent harness",
    )
    commands = parser.add_subparsers(
        dest="command",
        metavar="{auth,supervisor,doctor,bench,tasktree,module-test,version,run,repl,tui,session}",
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
        help="run benchmark report, gate, or re-anchor",
        description="Run the Cambium benchmark plugin CLI.",
    )
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    for mode in ("report", "gate", "re-anchor"):
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

    run = commands.add_parser(
        "run",
        help="run one prompt against a repository",
        description="Run one Cambium oneshot turn against a repository.",
    )
    _add_run_arguments(run)

    repl = commands.add_parser(
        "repl",
        help="start an interactive prompt session",
        description="Start an interactive Cambium prompt session.",
    )
    _add_agent_arguments(repl)

    tui = commands.add_parser(
        "tui",
        help="start the terminal dashboard",
        description="Start the Cambium terminal dashboard.",
    )
    _add_agent_arguments(tui)

    session = commands.add_parser(
        "session",
        help="list, latest, or show sessions",
        description="List, latest, or show Cambium sessions.",
    )
    session_commands = session.add_subparsers(
        dest="session_command",
        required=True,
        parser_class=_SafeArgumentParser,
    )
    session_list = session_commands.add_parser("list", help="list sessions")
    session_list.add_argument("--session-dir", metavar="DIR", help="session directory")
    session_latest = session_commands.add_parser(
        "latest",
        help="show the latest session",
    )
    session_latest.add_argument("--session-dir", metavar="DIR", help="session directory")
    session_show = session_commands.add_parser(
        "show",
        help="show one session",
        description="Show one Cambium session.",
    )
    session_show.add_argument("--session-dir", metavar="DIR", help="session directory")
    session_show.add_argument(
        "session_id",
        metavar="SESSION",
        help="session id to show",
    )

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


def _import_or_fail(module_name: str, command: str):
    """Import one staged module, or report it as not installed."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            print(f"cambium {command}: {module_name} is not installed", file=sys.stderr)
            return None
        raise


def _looks_like_prompt(first: str) -> bool:
    return bool(first) and not first.startswith("-") and first not in _COMMAND_NAMES


def _bare_prompt_allowed(command_line: list[str]) -> bool:
    """Allow natural-language bare prompts without hiding command typos.

    A single shell token without whitespace is treated as a command and is
    parsed by the root parser.  A quoted sentence, or multiple bare words,
    uses the prompt path.  Known commands and leading options are always
    preserved by :func:`main`.
    """
    if not command_line or not _looks_like_prompt(command_line[0]):
        return False
    if any(char.isspace() for char in command_line[0]):
        return True
    return sum(not token.startswith("-") for token in command_line[:2]) > 1


def _prompt_text(value: str | list[str]) -> str:
    if isinstance(value, str):
        return value
    return " ".join(value)


def _run_bare_prompt(command_line: list[str]) -> int:
    parser = _SafeArgumentParser(prog="cambium run")
    _add_run_arguments(parser)
    prompt_words: list[str] = []
    remainder: list[str] = []
    index = 0
    while index < len(command_line):
        token = command_line[index]
        if token.startswith("--"):
            remainder.extend(command_line[index:])
            break
        prompt_words.append(token)
        index += 1
    normalized = [" ".join(prompt_words), *remainder]
    return _run_oneshot(parser.parse_args(normalized))


def _budget_or_default(value: float | int | None, default: float | int) -> float | int:
    return default if value is None else value


def _run_oneshot(args: argparse.Namespace) -> int:
    oneshot = _import_or_fail("cambium.oneshot", "run")
    if oneshot is None:
        return 1
    render = _import_or_fail("cambium.render", "run")
    if render is None:
        return 1
    from .supervisor import SessionAlreadyRunningError

    config = oneshot.OneShotConfig(
        prompt=_prompt_text(args.prompt),
        repo=args.repo,
        session_root=args.session_dir,
        provider=args.provider,
        model=args.model,
        auto=getattr(args, "auto", False),
        max_wall_s=_budget_or_default(
            getattr(args, "max_wall_s", None), oneshot.DEFAULT_WALL_BUDGET_S
        ),
        max_tokens=_budget_or_default(
            getattr(args, "max_tokens", None), oneshot.DEFAULT_MAX_TOKENS
        ),
        max_turns=_budget_or_default(
            getattr(args, "max_turns", None), oneshot.DEFAULT_MAX_TURNS
        ),
    )
    try:
        value = oneshot.run_oneshot(config)
        result = asyncio.run(value) if inspect.isawaitable(value) else value
    except KeyboardInterrupt:
        return 130
    except SessionAlreadyRunningError as exc:
        # The session admission lock is held by another live supervisor.
        # Report one sanitized diagnostic (no traceback) and return the
        # documented temporary-failure exit code so callers can retry.
        print(f"cambium run: {exc}", file=sys.stderr)
        return _EXIT_SESSION_BUSY
    except (AuthError, OSError, ValueError) as exc:
        print(f"cambium run: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(render.render_json_result(result))
    else:
        print(render.render_text_result(result))
    exit_code = getattr(result, "exit_code", 1)
    return exit_code if type(exit_code) is int else 1


def _run_repl(args: argparse.Namespace) -> int:
    repl = _import_or_fail("cambium.repl", "repl")
    if repl is None:
        return 1
    from . import oneshot

    config = oneshot.OneShotConfig(
        repo=args.repo,
        session_root=args.session_dir,
        provider=args.provider,
        model=args.model,
    )
    return repl.run_repl(config)


def _run_tui(args: argparse.Namespace) -> int:
    tui = _import_or_fail("cambium.tui", "tui")
    if tui is None:
        return 1
    from . import oneshot

    config = oneshot.OneShotConfig(
        repo=args.repo,
        session_root=args.session_dir,
        provider=args.provider,
        model=args.model,
    )
    return tui.run_tui(config)


def _run_session(args: argparse.Namespace) -> int:
    session = _import_or_fail("cambium.session", "session")
    if session is None:
        return 1
    render = _import_or_fail("cambium.render", "session")
    if render is None:
        return 1
    root = (
        Path(args.session_dir).expanduser().resolve()
        if args.session_dir is not None
        else session.session_root(Path.cwd())
    )
    if args.session_command == "list":
        try:
            paths = session.list_sessions(root)
        except OSError as exc:
            print(f"cambium session: {exc}", file=sys.stderr)
            return 1
        for path in paths:
            print(path)
        return 0
    if args.session_command == "latest":
        try:
            path = session.latest_session(root)
        except OSError as exc:
            print(f"cambium session: {exc}", file=sys.stderr)
            return 1
        if path is None:
            print(f"cambium session: no completed sessions under {root}", file=sys.stderr)
            return 1
        print(path)
        return 0
    if args.session_command == "show":
        candidate = Path(args.session_id).expanduser()
        path = candidate if candidate.is_absolute() else root / candidate
        try:
            view = session.show_session(path)
            rendered = render.render_json_result(view.result)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"cambium session: {exc}", file=sys.stderr)
            return 1
        print(rendered)
        return 0
    raise AssertionError(f"unhandled session command: {args.session_command!r}")


def main(argv: list[str] | None = None) -> int:
    """Dispatch one unified Cambium CLI invocation and return its exit code."""
    command_line = sys.argv[1:] if argv is None else argv
    if command_line and command_line[0] == "tasktree":
        from . import tasktree

        return tasktree.main(command_line[1:])

    if _bare_prompt_allowed(command_line):
        return _run_bare_prompt(command_line)

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
    if args.command == "run":
        return _run_oneshot(args)
    if args.command == "repl":
        return _run_repl(args)
    if args.command == "tui":
        return _run_tui(args)
    if args.command == "session":
        return _run_session(args)
    if args.command == "version":
        print(__version__)
        return 0
    raise AssertionError(f"unhandled command: {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
