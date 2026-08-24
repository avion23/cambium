"""Unified command-line entry point for Cambium.

The subcommands are thin adapters around the existing module CLIs.  The
adapters keep each module's implementation and exit-code contract in one
place while providing one installed ``cambium`` command.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import importlib
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from enum import IntEnum
from pathlib import Path
from typing import Any, NoReturn, TypeVar, cast, overload

from . import __version__
from .auth import (
    AuthError,
    AuthSchemaError,
    AuthStore,
    read_stdin_key,
    validate_provider_id,
)
from .oauth import (
    DEFAULT_ISSUER,
    DeviceFlow,
    DeviceFlowCanceled,
    DeviceFlowExpired,
    OAuthError,
    OAuthStore,
    import_codex_cli_session,
    resolve_codex_client_id,
)
from .render_markdown import render_markdown_if_tty


class ExitCode(IntEnum):
    SUCCESS = 0
    FAILURE = 1
    USAGE = 2
    TEMPORARY_FAILURE = 75
    INTERRUPTED = 130


_NamespaceT = TypeVar("_NamespaceT")


class _SafeArgumentParser(argparse.ArgumentParser):
    """Do not echo arbitrary rejected tokens, which may be credentials."""

    @overload
    def parse_known_args(
        self,
        args: Iterable[str] | None = None,
        namespace: None = None,
    ) -> tuple[argparse.Namespace, list[str]]: ...

    @overload
    def parse_known_args(
        self,
        args: Iterable[str] | None,
        namespace: _NamespaceT,
    ) -> tuple[_NamespaceT, list[str]]: ...

    @overload
    def parse_known_args(
        self,
        *,
        namespace: _NamespaceT,
    ) -> tuple[_NamespaceT, list[str]]: ...

    def parse_known_args(
        self,
        args: Iterable[str] | None = None,
        namespace: _NamespaceT | None = None,
    ) -> tuple[argparse.Namespace | _NamespaceT, list[str]]:
        if args is not None and "--" in args and self.prog.endswith(" run"):
            self.error("invalid command arguments")
        return cast(
            tuple[argparse.Namespace | _NamespaceT, list[str]],
            super().parse_known_args(args, namespace),
        )

    def error(self, message: str) -> NoReturn:
        if "unrecognized arguments" in message or "invalid choice" in message:
            message = "invalid command arguments"
        super().error(message)


def _provider_argument(value: str) -> str:
    try:
        return validate_provider_id(value)
    except AuthSchemaError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _split_provider_model(provider: str | None, model: str | None) -> tuple[str | None, str | None]:
    """Resolve combined ``--provider NAME:MODEL`` / ``--model NAME/MODEL`` CLI
    forms into separate provider and model values.

    A model named through either form wins; naming conflicting models or
    providers across both forms is an error.
    """
    provider_name: str | None = None
    provider_model: str | None = None
    if provider is not None:
        if ":" in provider:
            provider_name, provider_model = provider.split(":", 1)
            if not provider_name or not provider_model:
                raise ValueError("--provider must be NAME or NAME:MODEL")
        else:
            provider_name = provider
            if not provider_name:
                raise ValueError("--provider must be NAME or NAME:MODEL")
    model_provider: str | None = None
    model_name: str | None = None
    if model is not None:
        if "/" in model:
            model_provider, model_name = model.split("/", 1)
            if not model_provider or not model_name:
                raise ValueError("--model must be MODEL or PROVIDER/MODEL")
        else:
            model_name = model
            if not model_name:
                raise ValueError("--model must be MODEL or PROVIDER/MODEL")
    if provider_model is not None and model_name is not None and provider_model != model_name:
        raise ValueError("--provider and --model specify conflicting models")
    if provider_name is not None and model_provider is not None and provider_name != model_provider:
        raise ValueError("--provider and --model specify conflicting providers")
    return (
        provider_name if provider_name is not None else model_provider,
        provider_model if provider_model is not None else model_name,
    )


def _agent_provider_argument(value: str) -> str:
    """``--provider`` accepts ``NAME`` or ``NAME:MODEL`` (returns the raw value;
    the handler splits it via :func:`_split_provider_model`)."""
    try:
        provider, _ = _split_provider_model(value, None)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    try:
        validate_provider_id(provider)
    except AuthSchemaError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _agent_model_argument(value: str) -> str:
    """``--model`` accepts ``MODEL`` or ``PROVIDER/MODEL`` (returns the raw
    value; the handler splits it via :func:`_split_provider_model`)."""
    try:
        provider, model = _split_provider_model(None, value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if not model:
        raise argparse.ArgumentTypeError("model name is invalid")
    if provider is not None:
        try:
            validate_provider_id(provider)
        except AuthSchemaError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


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
    if parsed is None or not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def _add_supervisor_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session-dir", required=True, metavar="DIR")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--plan",
        metavar="PATH",
        help="path to plan JSON for multi-worker mode",
    )
    inputs.add_argument(
        "--task-spec",
        metavar="PATH",
        help="path to task spec JSON for one-task mode",
    )
    inputs.add_argument("--demo", action="store_true", help="run the built-in mutating demo")
    parser.add_argument(
        "--warm-pool-size",
        type=int,
        default=0,
        help="maximum reusable idle workers (default: 0)",
    )
    parser.add_argument(
        "--conversations",
        action="store_true",
        help="persist child-revision conversations at "
        "<session-dir>/.cambium/conversations.db for the session",
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
        type=_agent_provider_argument,
        metavar="PROVIDER[:MODEL]",
        help="provider id, optionally with a model (NAME:MODEL)",
    )
    parser.add_argument(
        "--model",
        type=_agent_model_argument,
        metavar="[PROVIDER/]MODEL",
        help="model name, optionally with a provider (PROVIDER/MODEL)",
    )


def _add_routing_budget_arguments(parser: argparse.ArgumentParser) -> None:
    _add_agent_arguments(parser)
    parser.add_argument(
        "--auto",
        action="store_true",
        help="select from enabled providers with stored credentials using "
        "recorded usage instead of pinning --provider/--model",
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
        help="maximum agent-loop turns (default 50)",
    )


def _add_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "prompt",
        metavar="PROMPT",
        help="prompt to run against the repository",
    )
    _add_routing_budget_arguments(parser)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")


def _add_architectus_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        "--scripted",
        dest="dry_run",
        action="store_true",
        help="run one deterministic scripted step (no live LLM or credentials)",
    )
    parser.add_argument(
        "--provider",
        type=_agent_provider_argument,
        metavar="PROVIDER[:MODEL]",
        help="provider id, optionally with a model (NAME:MODEL)",
    )
    parser.add_argument(
        "--model",
        type=_agent_model_argument,
        metavar="[PROVIDER/]MODEL",
        help="model name, optionally with a provider (PROVIDER/MODEL)",
    )
    parser.add_argument(
        "--tier",
        metavar="TIER",
        help="provider tier for the live call (default: the selected provider's tier)",
    )
    parser.add_argument(
        "--waves",
        type=_positive_int,
        metavar="N",
        default=1,
        help="number of decision waves to run (default 1)",
    )
    parser.add_argument(
        "--task",
        metavar="TASK",
        help="root task (default: add a docstring to the task-tree builder)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        prog="cambium",
        description="Cambium multi-agent coding-agent harness",
    )
    commands = parser.add_subparsers(
        dest="command",
        metavar="{auth,supervisor,doctor,bench,module-test,version,run,repl,tui,monitor,quota,optimize,session,architectus}",
        required=True,
        parser_class=_SafeArgumentParser,
    )

    supervisor = commands.add_parser(
        "supervisor",
        help="run the supervisor",
        description="Run one session from a plan, task spec, or built-in demo.",
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

    auth_oauth = auth_commands.add_parser(
        "oauth",
        help="manage one provider's Codex ChatGPT OAuth session",
        description="Device-flow login, local status, locked local logout, and "
        "codex CLI session import for a codex_chatgpt provider. The device "
        "code is shown only on the controlling TTY and never logged.",
    )
    oauth_commands = auth_oauth.add_subparsers(dest="oauth_command", required=True)
    oauth_login = oauth_commands.add_parser("login", help="start a device-flow login")
    oauth_login.add_argument("provider", type=_provider_argument, metavar="PROVIDER")
    oauth_login.add_argument(
        "--client-id",
        metavar="ID",
        help="codex client id for the device flow (or CAMBIUM_CODEX_CLIENT_ID)",
    )
    for operation in ("status", "logout"):
        operation_parser = oauth_commands.add_parser(operation)
        operation_parser.add_argument("provider", type=_provider_argument, metavar="PROVIDER")
    oauth_commands.add_parser(
        "import-codex-cli",
        help="import the existing codex CLI session as provider 'codex'",
    )

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
    doctor.add_argument(
        "--oauth-live",
        action="store_true",
        help="opt-in live oauth probe for codex_chatgpt providers: endpoint "
        "reachability plus a real refresh-token exchange (consumes quota; "
        "never makes a model call)",
    )

    bench = commands.add_parser(
        "bench",
        help="run benchmark report, gate, re-anchor, or quality",
        description="Run the Cambium benchmark plugin CLI.",
    )
    bench_commands = bench.add_subparsers(dest="bench_command", required=True)
    for mode in ("report", "gate", "re-anchor", "quality"):
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
    _add_routing_budget_arguments(repl)

    tui = commands.add_parser(
        "tui",
        help="start the terminal dashboard",
        description="Start the Cambium terminal dashboard.",
    )
    _add_routing_budget_arguments(tui)
    tui.add_argument(
        "--quiet",
        action="store_true",
        help="suppress live event updates and print only completed results",
    )

    monitor = commands.add_parser(
        "monitor",
        help="attach the operator dashboard to a durable session",
        description="Watch main/sub-agent state, provider/model identity, usage, "
        "throughput, and context-trunk size from durable events.",
    )
    monitor.add_argument("session", nargs="?", metavar="SESSION")
    monitor.add_argument("--repo", default=None, metavar="PATH")
    monitor.add_argument("--interval", type=float, default=0.25, metavar="SECONDS")
    monitor.add_argument("--once", action="store_true")
    monitor.add_argument("--json", action="store_true")

    quota = commands.add_parser(
        "quota",
        help="inspect or update provider quota windows",
        description="Inspect or update content-free provider quota windows.",
    )
    quota.add_argument("--db", type=Path, help=argparse.SUPPRESS)
    quota_commands = quota.add_subparsers(dest="quota_command", required=True)
    quota_status = quota_commands.add_parser("status", help="show known provider quota windows")
    quota_status.add_argument("--provider")
    quota_status.add_argument("--json", action="store_true")
    quota_observe = quota_commands.add_parser(
        "observe",
        help="record a provider/dashboard/header quota observation",
    )
    quota_observe.add_argument("provider")
    quota_observe.add_argument("window")
    quota_observe.add_argument("--reset-in-s", type=float, required=True)
    quota_observe.add_argument("--allowance-tokens", type=int, default=0)
    quota_observe.add_argument("--remaining-tokens", type=int)
    quota_observe.add_argument("--allowance-requests", type=int, default=0)
    quota_observe.add_argument("--remaining-requests", type=int)
    quota_observe.add_argument("--reserve-fraction", type=float, default=0.0)

    optimize_command = commands.add_parser(
        "optimize",
        help="run one DSPy decision-module optimization",
        description=(
            "Run the reviewed-data DSPy optimizer, extract OpenCode trajectories, "
            "or report an extracted dataset."
        ),
    )
    optimize_command.add_argument("module_name", metavar="MODULE|extract|stats|eval")
    optimize_command.add_argument("source", nargs="?", type=Path, metavar="PATH")
    optimize_command.add_argument("--optimizer", choices=("zero", "bootstrap"), default="zero")
    optimize_command.add_argument("--budget-usd", type=float, default=2.0)
    optimize_command.add_argument("--seed", type=int, default=0)
    optimize_command.add_argument("--tier", default="fast")
    optimize_command.add_argument("--dry-run", action="store_true")
    candidate_source = optimize_command.add_mutually_exclusive_group()
    candidate_source.add_argument(
        "--include-transcript-candidates",
        action="store_true",
    )
    candidate_source.add_argument(
        "--transcript-candidates",
        type=Path,
        metavar="PATH",
    )
    optimize_command.add_argument(
        "--database",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help="OpenCode SQLite database for optimize extract (repeatable)",
    )
    optimize_command.add_argument(
        "--session-dir",
        action="append",
        type=Path,
        default=[],
        metavar="DIR",
        help="OpenCode storage/session directory for optimize extract (repeatable)",
    )
    optimize_command.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="PATH_OR_NAME",
        help="repository filter for optimize extract (repeatable)",
    )
    optimize_command.add_argument(
        "--from",
        "--since",
        dest="start_time",
        metavar="TIME",
        help="inclusive extraction start (epoch seconds or ISO-8601)",
    )
    optimize_command.add_argument(
        "--to",
        "--until",
        dest="end_time",
        metavar="TIME",
        help="inclusive extraction end (epoch seconds or ISO-8601)",
    )
    optimize_command.add_argument(
        "--exclude",
        action="append",
        type=Path,
        default=[],
        metavar="PATH",
        help="JSONL candidate file whose canonical pairs must be excluded",
    )
    optimize_command.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="output JSONL dataset for optimize extract",
    )
    optimize_command.add_argument(
        "--review-gate",
        action="store_true",
        help="write optimize extract candidates to a needs_review queue",
    )
    optimize_command.add_argument(
        "--dataset",
        type=Path,
        metavar="PATH",
        help="dataset path for optimize stats/report/eval",
    )
    optimize_command.add_argument(
        "--program-dir",
        type=Path,
        metavar="PATH",
        help="optimized program directory for optimize eval",
    )
    optimize_command.add_argument(
        "--json",
        action="store_true",
        help="emit optimize stats/report as JSON",
    )

    session = commands.add_parser(
        "session",
        help="list, latest, show, status, resume, or usage sessions",
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
    session_status = session_commands.add_parser(
        "status",
        help="show the live task status of one session",
        description="Read one session's durable event log and render the current "
        "state of every task that ran or is running in it.",
    )
    session_status.add_argument("--session-dir", metavar="DIR", help="session directory")
    session_status.add_argument(
        "session_id",
        metavar="SESSION",
        help="session id whose tasks to inspect",
    )
    session_resume = session_commands.add_parser(
        "resume",
        help="resume a crashed or interrupted session from its persisted plan",
        description="Re-run one supervisor session against an existing session "
        "directory. The persisted plan.json drives the re-entry; completed tasks "
        "whose merge was already reconciled are skipped, and interrupted tasks are "
        "re-spawned from their base commit.",
    )
    session_resume.add_argument(
        "session_id",
        metavar="SESSION",
        help="session directory to resume (must contain plan.json)",
    )
    session_usage = session_commands.add_parser(
        "usage",
        help="show aggregated token and cost usage of one session",
        description="Aggregate the usage_event rows of one session's durable event "
        "log, grouped by task and by provider, with estimated cost.",
    )
    session_usage.add_argument("--session-dir", metavar="DIR", help="session directory")
    session_usage.add_argument(
        "session_id",
        metavar="SESSION",
        help="session id whose usage to aggregate",
    )

    architectus = commands.add_parser(
        "architectus",
        help="run one live or scripted Architectus decision session",
        description="Run a live or scripted Architectus decomposition session: build one "
        "task tree, run one or more steps through the core, and print "
        "the resulting action intents. Use --dry-run/--scripted for a deterministic run "
        "that needs no provider credentials.",
    )
    _add_architectus_arguments(architectus)
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
    if args.plan:
        delegated.extend(["--plan", args.plan])
    elif args.task_spec:
        delegated.extend(["--task-spec", args.task_spec])
    else:
        delegated.append("--demo")
    if args.warm_pool_size:
        delegated.extend(["--warm-pool-size", str(args.warm_pool_size)])
    if getattr(args, "conversations", False):
        delegated.append("--conversations")
    return delegated


def _run_supervisor(args: argparse.Namespace) -> int:
    from . import supervisor

    return supervisor.main(_supervisor_args(args))


def _run_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    delegated = []
    if args.session_dir is not None:
        delegated.extend(["--session-dir", str(args.session_dir)])
    if getattr(args, "oauth_live", False):
        delegated.append("--oauth-live")
    return doctor.main(delegated)


def _run_auth_set(args: argparse.Namespace) -> int:
    from .oneshot import OneShotConfig, _provider_config_path
    from .provider_config import ProviderSelectionError, load_providers, select_provider

    try:
        providers = load_providers(_provider_config_path(OneShotConfig(), Path.cwd()))
        select_provider(providers, name=args.provider)
        key = read_stdin_key() if args.stdin else getpass.getpass("API key: ")
        AuthStore().set_provider(args.provider, key)
    except KeyboardInterrupt:
        print("cambium auth: credential input interrupted", file=sys.stderr)
        return ExitCode.INTERRUPTED
    except EOFError:
        print("cambium auth: credential input ended before a key was read", file=sys.stderr)
        return ExitCode.FAILURE
    except (ProviderSelectionError, cast(type[Exception], ValueError)) as exc:
        print(f"cambium auth: provider configuration is invalid: {exc}", file=sys.stderr)
        return ExitCode.USAGE
    except AuthSchemaError as exc:
        print(f"cambium auth: credential is invalid: {exc}", file=sys.stderr)
        return ExitCode.USAGE
    except (AuthError, OSError):
        print("cambium auth: could not store provider credential", file=sys.stderr)
        return ExitCode.FAILURE
    print(f"stored provider {args.provider}")
    return ExitCode.SUCCESS


def _run_auth_remove(args: argparse.Namespace) -> int:
    try:
        removed = AuthStore().remove_provider(args.provider)
    except (AuthError, OSError):
        print("cambium auth: could not remove provider credential", file=sys.stderr)
        return 1
    if removed:
        print(f"removed provider {args.provider}")
    else:
        print(f"provider {args.provider} is not configured (no change)")
    return ExitCode.SUCCESS


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


def _oauth_fingerprint(account_id: str | None) -> str:
    """A stable account fingerprint (first 8 hex of SHA-256), never the id."""
    if not account_id:
        return "unknown"
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:8]


def _oauth_status_text(store: OAuthStore, provider: str) -> str:
    """Local-only status: expiry plus an account fingerprint; no secrets."""
    doc = store.read_document(provider)
    if doc is None:
        return f"provider {provider}: no oauth session stored"
    remaining = doc.expires_at - time.time()
    state = "expired" if remaining <= 0 else f"{remaining:.0f}s remaining"
    return (
        f"provider {provider}: oauth session {state}; "
        f"account fingerprint {_oauth_fingerprint(doc.account_id)}"
    )


def _run_auth_oauth_status(store: OAuthStore, provider: str) -> int:
    try:
        text = _oauth_status_text(store, provider)
    except OAuthError as exc:
        print(f"cambium auth: oauth status unavailable: {exc}", file=sys.stderr)
        return 1
    if "no oauth session" in text:
        print(text, file=sys.stderr)
        return 1
    print(text)
    return 0


def _run_auth_oauth_logout(store: OAuthStore, provider: str) -> int:
    """Locked local removal only; no remote revocation is claimed."""
    try:
        removed = store.remove_provider(provider)
    except OAuthError as exc:
        print(f"cambium auth: oauth logout failed: {exc}", file=sys.stderr)
        return 1
    if removed:
        print(
            f"removed local oauth session for provider {provider} (the issuer session is unchanged)"
        )
    else:
        print(f"provider {provider} has no stored oauth session")
    return 0


def _run_auth_oauth_import(store: OAuthStore, path: str | Path | None = None) -> int:
    try:
        doc = import_codex_cli_session(path)
        store.save_provider(doc)
    except OAuthError as exc:
        print(f"cambium auth: could not import the codex CLI session: {exc}", file=sys.stderr)
        return 1
    print(f"imported the codex CLI session for provider {doc.provider}")
    return 0


def _controlling_tty_writer() -> Callable[[str], None]:
    """Return a writer that prints to the controlling TTY and nowhere else."""

    def write(text: str) -> None:
        try:
            with open("/dev/tty", "w", encoding="utf-8") as tty:
                tty.write(text)
                tty.flush()
        except OSError as exc:
            raise OAuthError(
                "no controlling TTY is available to display the device code; "
                "refusing to print it to stdout or logs"
            ) from exc

    return write


def _run_auth_oauth_device(
    provider: str,
    client_id: str | None,
    *,
    store: OAuthStore | None = None,
    issuer: str | None = None,
    tty: Callable[[str], None] | None = None,
) -> int:
    """Run the device flow; the user code reaches only the controlling TTY."""

    try:
        effective_client_id = resolve_codex_client_id(client_id)
    except OAuthError as exc:
        print(f"cambium auth: device flow configuration failed: {exc}", file=sys.stderr)
        return 1
    writer = _controlling_tty_writer() if tty is None else tty
    flow = DeviceFlow(
        provider,
        client_id=effective_client_id,
        issuer=DEFAULT_ISSUER if issuer is None else issuer,
        store=store,
    )
    try:
        flow.run(on_code=lambda url, code: writer(f"Open {url} and enter the code {code}" + "\n"))
    except KeyboardInterrupt:
        print("cambium auth: device flow canceled", file=sys.stderr)
        return 130
    except DeviceFlowCanceled:
        print("cambium auth: device flow canceled", file=sys.stderr)
        return 130
    except DeviceFlowExpired:
        print("cambium auth: device flow expired before approval", file=sys.stderr)
        return 1
    except OAuthError as exc:
        print(f"cambium auth: device flow failed: {exc}", file=sys.stderr)
        return 1
    print(f"stored oauth session for provider {provider}")
    return 0


def _run_auth_oauth(args: argparse.Namespace) -> int:
    if args.oauth_command == "import-codex-cli":
        return _run_auth_oauth_import(OAuthStore())
    store = OAuthStore()
    if args.oauth_command == "status":
        return _run_auth_oauth_status(store, args.provider)
    if args.oauth_command == "logout":
        return _run_auth_oauth_logout(store, args.provider)
    if args.oauth_command != "login":
        raise AssertionError(f"unhandled oauth command: {args.oauth_command!r}")
    client_id = args.client_id or os.environ.get("CAMBIUM_CODEX_CLIENT_ID")
    return _run_auth_oauth_device(args.provider, client_id, store=store)


def _run_auth(args: argparse.Namespace) -> int:
    if args.auth_command == "set":
        return _run_auth_set(args)
    if args.auth_command == "remove":
        return _run_auth_remove(args)
    if args.auth_command == "list":
        return _run_auth_list()
    if args.auth_command == "oauth":
        return _run_auth_oauth(args)
    if args.auth_command == "run" and args.profile == "supervisor":
        return _run_auth_supervisor(args)
    raise AssertionError(f"unhandled auth command: {args.auth_command!r}")


def _run_bench(args: argparse.Namespace) -> int:
    try:
        bench = importlib.import_module("cambium.bench")
    except ModuleNotFoundError as exc:
        if exc.name in {"pytest", "tree_sitter", "tree_sitter_python"}:
            missing = exc.name.replace("_", "-")
            print(
                f"cambium bench: {missing} is not installed; run `pip install cambium[test]`",
                file=sys.stderr,
            )
            return 1
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
    try:
        module_conformance = importlib.import_module("cambium.module_conformance")
    except ModuleNotFoundError as exc:
        if exc.name in {"pytest", "tree_sitter", "tree_sitter_python"}:
            missing = exc.name.replace("_", "-")
            print(
                f"cambium module-test: {missing} is not installed; run `pip install cambium[test]`",
                file=sys.stderr,
            )
            return 1
        raise

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


def _prompt_text(value: str | list[str]) -> str:
    if isinstance(value, str):
        return value
    return " ".join(value)


def _budget_or_default(value: float | int | None, default: float | int) -> float | int:
    return default if value is None else value


async def _run_oneshot(args: argparse.Namespace) -> int:
    oneshot = _import_or_fail("cambium.oneshot", "run")
    if oneshot is None:
        return 1
    render = _import_or_fail("cambium.render", "run")
    if render is None:
        return 1
    from .supervisor import SessionAlreadyRunningError

    try:
        provider, model = _split_provider_model(args.provider, args.model)
        config = oneshot.OneShotConfig(
            prompt=_prompt_text(args.prompt),
            repo=args.repo,
            session_root=args.session_dir,
            provider=provider,
            model=model,
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
            context_reuse=True,
        )
    except ValueError as exc:
        print(f"cambium run: {exc}", file=sys.stderr)
        return 2
    try:
        result = await oneshot.run_oneshot(config)
    except KeyboardInterrupt:
        return 130
    except SessionAlreadyRunningError as exc:
        # The session admission lock is held by another live supervisor.
        # Report one sanitized diagnostic (no traceback) and return the
        # documented temporary-failure exit code so callers can retry.
        print(f"cambium run: {exc}", file=sys.stderr)
        return ExitCode.TEMPORARY_FAILURE
    except (AuthError, OSError, ValueError) as exc:
        print(f"cambium run: {exc}", file=sys.stderr)
        return ExitCode.FAILURE
    if args.json:
        print(render.render_json_result(result))
    else:
        print(render.render_text_result(result))
        summaries = [
            entry.summary
            for entry in getattr(result, "results", ())
            if getattr(entry, "summary", None)
        ]
        if summaries:
            print(render_markdown_if_tty("\n\n".join(summaries), sys.stdout))
    exit_code = getattr(result, "exit_code", 1)
    return exit_code if type(exit_code) is int else 1


async def _run_repl(args: argparse.Namespace) -> int:
    repl = _import_or_fail("cambium.repl", "repl")
    if repl is None:
        return 1
    from . import oneshot

    try:
        provider, model = _split_provider_model(args.provider, args.model)
    except ValueError as exc:
        print(f"cambium repl: {exc}", file=sys.stderr)
        return 2
    config = oneshot.OneShotConfig(
        repo=args.repo,
        session_root=args.session_dir,
        provider=provider,
        model=model,
        auto=args.auto,
        # ``None`` lets the REPL resolve the interactive deadline from the
        # selected provider's hint and the branch's measured output rate.
        max_wall_s=args.max_wall_s,
        max_tokens=cast(int, _budget_or_default(args.max_tokens, oneshot.DEFAULT_MAX_TOKENS)),
        max_turns=cast(int, _budget_or_default(args.max_turns, oneshot.DEFAULT_MAX_TURNS)),
    )
    return await repl.run_repl(config)


async def _run_tui(args: argparse.Namespace) -> int:
    tui = _import_or_fail("cambium.tui", "tui")
    if tui is None:
        return 1
    from . import oneshot

    try:
        provider, model = _split_provider_model(args.provider, args.model)
    except ValueError as exc:
        print(f"cambium tui: {exc}", file=sys.stderr)
        return 2
    config = oneshot.OneShotConfig(
        repo=args.repo,
        session_root=args.session_dir,
        provider=provider,
        model=model,
        auto=args.auto,
        # ``None`` lets InteractiveSession resolve the throughput-aware
        # interactive deadline; an explicit CLI value remains authoritative.
        max_wall_s=args.max_wall_s,
        max_tokens=cast(int, _budget_or_default(args.max_tokens, oneshot.DEFAULT_MAX_TOKENS)),
        max_turns=cast(int, _budget_or_default(args.max_turns, oneshot.DEFAULT_MAX_TURNS)),
    )
    return await tui.run_tui(config, quiet=getattr(args, "quiet", False))


async def _run_monitor(args: argparse.Namespace) -> int:
    from . import monitor

    if not monitor.math_is_positive(args.interval):
        print(
            "cambium monitor: --interval must be a positive finite number",
            file=sys.stderr,
        )
        return 2
    try:
        session = monitor.resolve_session(args.session, repo=args.repo)
    except ValueError as exc:
        print(f"cambium monitor: {exc}", file=sys.stderr)
        return 1
    return await monitor.monitor_session_async(
        session,
        interval_s=args.interval,
        once=args.once,
        json_output=args.json,
    )


def _run_quota(args: argparse.Namespace) -> int:
    from . import quota_cli

    try:
        return quota_cli.run_namespace(args)
    except (OSError, ValueError) as exc:
        print(f"cambium quota: {exc}", file=sys.stderr)
        return ExitCode.USAGE


def _run_optimize(args: argparse.Namespace) -> int:
    if args.module_name == "extract":
        from . import opencode

        delegated: list[str] = []
        if args.source is not None:
            delegated.append(str(args.source))
        for option, values in (("--database", args.database), ("--session-dir", args.session_dir)):
            for value in values:
                delegated.extend([option, str(value)])
        for value in args.repo:
            delegated.extend(["--repo", value])
        if args.start_time is not None:
            delegated.extend(["--from", args.start_time])
        if args.end_time is not None:
            delegated.extend(["--to", args.end_time])
        for value in args.exclude:
            delegated.extend(["--exclude", str(value)])
        if args.output is not None:
            delegated.extend(["--output", str(args.output)])
        if args.review_gate:
            delegated.append("--review-gate")
        return opencode.extract_main(delegated)
    if args.module_name in {"stats", "report"}:
        from . import opencode

        delegated = []
        if args.source is not None:
            delegated.append(str(args.source))
        if args.dataset is not None:
            delegated.extend(["--dataset", str(args.dataset)])
        if args.json:
            delegated.append("--json")
        return opencode.stats_main(delegated)
    try:
        optimize = importlib.import_module("cambium.optimize")
    except ModuleNotFoundError as exc:
        if exc.name == "dspy":
            print(
                "cambium optimize: DSPy is not installed; run `uv sync --extra dspy --python 3.14`",
                file=sys.stderr,
            )
            return 1
        raise
    if args.module_name == "eval":
        if args.source is None:
            print("cambium optimize eval: MODULE is required", file=sys.stderr)
            return 2
        if args.dataset is None:
            print("cambium optimize eval: --dataset PATH is required", file=sys.stderr)
            return 2
        delegated = [
            "eval",
            str(args.source),
            "--dataset",
            str(args.dataset),
            "--budget-usd",
            str(args.budget_usd),
            "--tier",
            args.tier,
        ]
        if args.program_dir is not None:
            delegated.extend(["--program-dir", str(args.program_dir)])
        if args.json:
            delegated.append("--json")
        return optimize.main(delegated)
    delegated = [
        args.module_name,
        "--optimizer",
        args.optimizer,
        "--budget-usd",
        str(args.budget_usd),
        "--seed",
        str(args.seed),
        "--tier",
        args.tier,
    ]
    if args.dry_run:
        delegated.append("--dry-run")
    if args.include_transcript_candidates:
        delegated.append("--include-transcript-candidates")
    if args.transcript_candidates is not None:
        delegated.extend(["--transcript-candidates", str(args.transcript_candidates)])
    return optimize.main(delegated)


def _run_session(args: argparse.Namespace) -> int:
    session = _import_or_fail("cambium.session", "session")
    if session is None:
        return 1
    render = _import_or_fail("cambium.render", "session")
    if render is None:
        return 1
    root = (
        Path(args.session_dir).expanduser().resolve()
        if getattr(args, "session_dir", None) is not None
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
    if args.session_command == "status":
        from . import supervisor

        candidate = Path(args.session_id).expanduser()
        path = candidate if candidate.is_absolute() else root / candidate
        if not (path / ".cambium" / "events.db").is_file():
            print(
                f"cambium session: event log is missing: {path / '.cambium' / 'events.db'}",
                file=sys.stderr,
            )
            return 1
        try:
            events = supervisor.read_events(path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"cambium session: {exc}", file=sys.stderr)
            return 1
        text = render.render_subagent_status(events)
        print(text)
        return 0
    if args.session_command == "usage":
        from . import stats as stats_module

        candidate = Path(args.session_id).expanduser()
        path = candidate if candidate.is_absolute() else root / candidate
        try:
            breakdown = stats_module.session_usage_breakdown(path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"cambium session: {exc}", file=sys.stderr)
            return 1
        if breakdown is None:
            print(
                f"cambium session: no usage event log for {path}",
                file=sys.stderr,
            )
            return 1
        print(render.render_usage_breakdown(breakdown))
        return 0
    if args.session_command == "resume":
        from . import supervisor

        path = Path(args.session_id).expanduser().resolve()
        plan = path / "plan.json"
        if not plan.is_file():
            print(
                f"cambium session: cannot resume without a persisted plan: {plan}",
                file=sys.stderr,
            )
            return 1
        code = supervisor.main(["--session-dir", str(path), "--plan", str(plan)])
        return 130 if code == 130 else code
    raise AssertionError(f"unhandled session command: {args.session_command!r}")


def _architectus_provider_config_path() -> Path:
    from .oneshot import OneShotConfig, _provider_config_path

    return _provider_config_path(OneShotConfig(), Path.cwd())


def _live_architectus_llm(
    provider: str | None, model: str | None, tier: str | None
) -> tuple[Any, str, str]:
    """Construct one live :class:`ArchitectusLM` from the trusted provider config.

    The returned triple is ``(llm, provider name, tier label)``. API-key
    providers resolve their credential from the environment or the AuthStore
    and stage it process-locally (never logged); ``codex_chatgpt`` providers
    inject the stored OAuth access token through Diffundo's CredentialSource.
    The Diffundo router is pinned to the selected provider's model so the call
    has one deterministic candidate instead of cascading across providers.
    """
    from .diffundo import CredentialSource, Diffundo, ProviderTier
    from .lm import ArchitectusLM, CambiumLM
    from .oauth import OAuthError, TokenManager
    from .provider_config import (
        AuthMode,
        ProviderSelectionError,
        load_providers,
        select_provider,
    )

    provider, model = _split_provider_model(provider, model)
    config_path = _architectus_provider_config_path()
    try:
        providers = load_providers(config_path)
        if provider is None and model is not None:
            providers = [candidate for candidate in providers if candidate.model == model]
        selected = select_provider(providers, name=provider)
    except (OSError, ProviderSelectionError, ValueError) as exc:
        raise ValueError(f"provider selection failed: {exc}") from exc

    tier_value = tier or selected.tier.value
    if model is not None and selected.model != model:
        raise ValueError("selected model is not configured for the provider")
    try:
        selected_tier = ProviderTier(tier_value)
    except ValueError as exc:
        raise ValueError(f"unsupported provider tier {tier_value!r}") from exc

    options: dict[str, Any] = {}
    if selected.auth is AuthMode.CODEX_CHATGPT:
        try:
            access_token, account_id = TokenManager(selected.name).ensure_fresh()
        except OAuthError as exc:
            raise ValueError(
                f"provider {selected.name!r} oauth session is unavailable: {exc}"
            ) from exc
        options["credential_source"] = CredentialSource(
            access_token=access_token,
            account_id=account_id,
        )
    else:
        env_name = selected.api_key_env
        if not os.environ.get(env_name):
            raise ValueError("API-key Architectus calls require a credential-source interface")

    diffundo = Diffundo(providers, **options)
    lm = CambiumLM(diffundo, selected_tier, model=selected.model)
    return ArchitectusLM(lm), selected.name, selected_tier.value


async def _architectus_waves(core: Any, waves: int) -> list[list[dict[str, Any]]]:
    """Run ``waves`` decision waves and return the admitted action lists."""
    results: list[list[dict[str, Any]]] = []
    for _ in range(waves):
        actions = await core.step([{"kind": "tick"}])
        if not isinstance(actions, list) or not all(isinstance(action, dict) for action in actions):
            raise ValueError("decision actions are not a JSON array of action objects")
        results.append(actions)
    return results


async def _run_architectus(args: argparse.Namespace) -> int:
    """Run one live or scripted Architectus decomposition session end-to-end."""
    from .architectus import ArchitectusCore, ScriptedLLM
    from .tasktree import build_tree

    task = args.task or "Add a docstring to the build_tree function in src/cambium/tasktree.py"
    tree = build_tree(
        {
            "tasks": [
                {
                    "task_id": "root",
                    "kind": "FEATURE",
                    "depends_on": [],
                    "spec": {"goal": task},
                }
            ]
        }
    )
    if args.dry_run:
        llm = ScriptedLLM([{"action": "spawn", "task_id": "root"}])
        provider_name = "scripted"
        tier_label = "scripted"
    else:
        try:
            llm, provider_name, tier_label = _live_architectus_llm(
                args.provider, args.model, args.tier
            )
        except (AuthError, OSError, ValueError) as exc:
            print(f"cambium architectus: {exc}", file=sys.stderr)
            return 2

    core = ArchitectusCore(llm, tree=tree)
    try:
        waves = await _architectus_waves(core, args.waves)
    except (OSError, ValueError) as exc:
        print(f"cambium architectus: decision step failed: {exc}", file=sys.stderr)
        return 1

    print(f"provider: {provider_name} (tier: {tier_label})")
    for index, actions in enumerate(waves, start=1):
        print(f"wave {index}: {json.dumps(actions, sort_keys=True)}")
    return 0


async def async_main(argv: list[str] | None = None) -> int:
    """Dispatch one unified Cambium CLI invocation and return its exit code."""
    command_line = sys.argv[1:] if argv is None else argv
    args = _build_parser().parse_args(command_line)

    match args.command:
        case "run":
            return await _run_oneshot(args)
        case "repl":
            return await _run_repl(args)
        case "tui":
            return await _run_tui(args)
        case "monitor":
            return await _run_monitor(args)
        case "quota":
            return _run_quota(args)
        case "optimize":
            return await asyncio.to_thread(_run_optimize, args)
        case "architectus":
            return await _run_architectus(args)
        case "supervisor":
            return await asyncio.to_thread(_run_supervisor, args)
        case "auth":
            return _run_auth(args)
        case "doctor":
            return _run_doctor(args)
        case "bench":
            return await asyncio.to_thread(_run_bench, args)
        case "module-test":
            return await asyncio.to_thread(_run_module_test, args)
        case "session":
            return await asyncio.to_thread(_run_session, args)
        case "version":
            print(__version__)
            return ExitCode.SUCCESS
        case _:
            raise AssertionError(f"unhandled command: {args.command!r}")


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
