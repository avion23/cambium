"""Executable implementations for the worker tool catalogue.

The LLM-facing schemas remain the source of truth for argument validation.
Every operation runs inside an injected :class:`ToolContext`; this keeps
linting and dependency wiring out of process-global state.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from typing import Any, cast

from .auth import scrub_environment
from .lint_diag import LintDiag
from .schemas import TOOL_SCHEMAS, validate_tool_call
from .tasktree import TaskKind

MAX_READ_BYTES = 100 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
GIT_TIMEOUT_S = 30
BATCH_READ_MAX_CONCURRENCY = 4
READ_TRUNCATION_MARKER = "\n... [file truncated]"
OUTPUT_TRUNCATION_MARKER = "\n... [output truncated]"
_ALLOWED_TASK_KINDS = frozenset(member.value for member in TaskKind)
_ALLOWED_TASK_KINDS_TEXT = ", ".join(member.value for member in TaskKind)

ToolEventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ToolPermissionPolicy:
    shell: bool = True
    network: bool = True


@dataclass(slots=True)
class ToolContext:
    """Dependencies needed by one tool invocation."""

    cwd: Path | str
    lint: LintDiag | None = None
    init: Mapping[str, Any] | None = None
    emit: ToolEventSink | None = None
    policy: ToolPermissionPolicy | None = None

    def __post_init__(self) -> None:
        self.cwd = Path(self.cwd).resolve()

    def close(self) -> None:
        return None

    def __enter__(self) -> ToolContext:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The bounded, LLM-facing result of one tool invocation."""

    ok: bool
    output: str = ""
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class _Outcome:
    ok: bool
    output: str = ""
    error: str | None = None


class _ToolFailure(Exception):
    """An expected tool-level failure that belongs in ``ToolResult.error``."""


ToolImplementation = Callable[[dict[str, Any], ToolContext], Awaitable[_Outcome]]


def _duration_ms(started_ns: int) -> int:
    return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)


def _truncate_bytes(raw: bytes, limit: int, marker: str) -> str:
    marker_bytes = marker.encode("utf-8")
    if len(raw) <= limit:
        return raw.decode("utf-8")
    prefix_limit = max(0, limit - len(marker_bytes))
    prefix = raw[:prefix_limit].decode("utf-8", errors="ignore")
    return prefix + marker


def _truncate_text(text: str, limit: int, marker: str) -> str:
    return _truncate_bytes(text.encode("utf-8"), limit, marker)


def _display_path(ctx: ToolContext, path: Path) -> str:
    try:
        relative = path.relative_to(Path(ctx.cwd))
    except ValueError:
        return str(path)
    return relative.as_posix() or "."


def _read_text(path: Path) -> str:
    try:
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(path)
        if not stat.S_ISREG(mode):
            raise _ToolFailure(f"path is not a regular file: {path}")
        with path.open("rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)
    except FileNotFoundError as exc:
        raise _ToolFailure(f"file not found: {path}") from exc
    except IsADirectoryError as exc:
        raise _ToolFailure(f"path is a directory: {path}") from exc
    except UnicodeDecodeError as exc:
        raise _ToolFailure(f"file is not valid UTF-8: {path}") from exc
    except OSError as exc:
        raise _ToolFailure(f"could not read {path}: {exc}") from exc

    if len(raw) > MAX_READ_BYTES:
        raise _ToolFailure(
            f"edit_file source exceeds MAX_READ_BYTES ({MAX_READ_BYTES} bytes): {path}"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ToolFailure(f"file is not valid UTF-8: {path}") from exc


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path: Path | None = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _read_file_sync(
    path: Path,
    display_path: str,
    offset: int | None = None,
    limit: int | None = None,
) -> _Outcome:
    try:
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(path)
        if not stat.S_ISREG(mode):
            raise _ToolFailure(f"path is not a regular file: {display_path}")

        if offset is not None or limit is not None:
            start_line = offset if offset is not None else 1
            max_lines = limit if limit is not None else MAX_READ_BYTES
            selected: list[str] = []
            total_lines = 0
            with path.open("r", encoding="utf-8", newline="") as handle:
                for line_number, line in enumerate(handle, start=1):
                    total_lines = line_number
                    if start_line <= line_number < start_line + max_lines:
                        selected.append(line)
            end_line = min(start_line + max_lines - 1, total_lines)
            return _Outcome(
                ok=True,
                output=(
                    f"showing lines {start_line}-{end_line} of {total_lines}\n"
                    + "".join(selected)
                ),
            )

        with path.open("rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)
    except FileNotFoundError as exc:
        raise _ToolFailure(f"file not found: {display_path}") from exc
    except IsADirectoryError as exc:
        raise _ToolFailure(f"path is a directory: {display_path}") from exc
    except OSError as exc:
        raise _ToolFailure(f"could not read {display_path}: {exc}") from exc

    if len(raw) > MAX_READ_BYTES:
        output = _truncate_bytes(raw, MAX_READ_BYTES, READ_TRUNCATION_MARKER)
    else:
        try:
            output = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ToolFailure(f"file is not valid UTF-8: {display_path}") from exc
    return _Outcome(ok=True, output=output)


async def _read_file(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    path = (Path(ctx.cwd) / Path(args["path"]).expanduser()).resolve()
    display_path = _display_path(ctx, path)
    offset = args.get("offset")
    limit = args.get("limit")
    if offset is not None or limit is not None:
        return await asyncio.to_thread(_read_file_sync, path, display_path, offset, limit)
    return await asyncio.to_thread(_read_file_sync, path, display_path)


def _lint_feedback(ctx: ToolContext, path: Path) -> str:
    if ctx.lint is None:
        return ""
    diagnostics = ctx.lint.lint_file(path)
    feedback = ctx.lint.format_diags(diagnostics)
    return feedback if isinstance(feedback, str) else str(feedback)


async def _write_file(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    path = (Path(ctx.cwd) / Path(args["path"]).expanduser()).resolve()
    try:
        await asyncio.to_thread(_atomic_write, path, args["content"])
    except OSError as exc:
        raise _ToolFailure(f"could not write {_display_path(ctx, path)}: {exc}") from exc

    output = f"wrote {_display_path(ctx, path)}"
    feedback = await asyncio.to_thread(_lint_feedback, ctx, path)
    if feedback:
        output += f"\nLint diagnostics:\n{feedback}"
    return _Outcome(ok=True, output=output)


def _edit_context(content: str, old_string: str) -> str:
    if old_string:
        position = content.find(old_string)
        if position >= 0:
            start = max(0, position - 80)
            end = min(len(content), position + len(old_string) + 80)
            return repr(content[start:end])
    return repr(content[:160]) if content else "<empty file>"


async def _edit_file(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    path = (Path(ctx.cwd) / Path(args["path"]).expanduser()).resolve()
    content = await asyncio.to_thread(_read_text, path)
    old_string = args["old_string"]
    occurrences = content.count(old_string)
    if occurrences != 1:
        context = _edit_context(content, old_string)
        raise _ToolFailure(
            "edit_file requires exactly one occurrence of old_string; "
            f"found {occurrences}. Context: {context}"
        )

    replacement = content.replace(old_string, args["new_string"], 1)
    try:
        await asyncio.to_thread(_atomic_write, path, replacement)
    except OSError as exc:
        raise _ToolFailure(f"could not edit {_display_path(ctx, path)}: {exc}") from exc
    output = f"edited {_display_path(ctx, path)}"
    feedback = await asyncio.to_thread(_lint_feedback, ctx, path)
    if feedback:
        output += f"\nLint diagnostics:\n{feedback}"
    return _Outcome(ok=True, output=output)


def _process_output(stdout: Any, stderr: Any) -> str:
    standard_output = (
        stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else str(stdout or "")
    )
    standard_error = (
        stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else str(stderr or "")
    )
    if standard_output and standard_error:
        separator = "" if standard_output.endswith("\n") else "\n"
        return standard_output + separator + standard_error
    return standard_output or standard_error


_PROCESS_CAPTURE_BYTES = MAX_OUTPUT_BYTES + 1
_PROCESS_READ_CHUNK_BYTES = 8192
_PROCESS_DRAIN_TIMEOUT_S = 0.5


async def _read_process_stream(stream: Any, chunks: list[bytes]) -> None:
    captured = 0
    while True:
        chunk = await stream.read(_PROCESS_READ_CHUNK_BYTES)
        if not chunk:
            return
        if captured < _PROCESS_CAPTURE_BYTES:
            kept = chunk[: _PROCESS_CAPTURE_BYTES - captured]
            chunks.append(kept)
            captured += len(kept)


async def _bounded_process_drain(completion: asyncio.Future[Any]) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(completion), _PROCESS_DRAIN_TIMEOUT_S)
    except (TimeoutError, asyncio.CancelledError):
        completion.cancel()
        await asyncio.gather(completion, return_exceptions=True)


async def _run_process(
    command: list[str], cwd: Path, timeout_s: int
) -> subprocess.CompletedProcess[str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=scrub_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=_PROCESS_CAPTURE_BYTES,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_task = asyncio.create_task(_read_process_stream(process.stdout, stdout_chunks))
    stderr_task = asyncio.create_task(_read_process_stream(process.stderr, stderr_chunks))
    wait_task = asyncio.create_task(process.wait())
    completion = asyncio.gather(wait_task, stdout_task, stderr_task)
    try:
        await asyncio.wait_for(asyncio.shield(completion), timeout_s)
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await _bounded_process_drain(completion)
        stdout = b"".join(stdout_chunks)
        stderr = b"".join(stderr_chunks)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise subprocess.TimeoutExpired(
            command,
            timeout_s,
            output=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        ) from exc

    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)
    return subprocess.CompletedProcess(
        command,
        cast(int, process.returncode),
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _git_schema_ops() -> frozenset[str]:
    schema = next(schema for schema in TOOL_SCHEMAS if schema["name"] == "git_op")
    values = schema["parameters"]["properties"]["op"]["enum"]
    return frozenset(value for value in values if isinstance(value, str))


GIT_OPS = _git_schema_ops()
UNSAFE_GIT_OPS = frozenset({"checkout", "reset"})


async def _git_op(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    op = args["op"]
    if op not in GIT_OPS and op not in UNSAFE_GIT_OPS:
        raise _ToolFailure(f"git operation is not allowlisted: {op!r}")
    try:
        argument_tokens = shlex.split(args["args"])
    except ValueError as exc:
        raise _ToolFailure(f"invalid git arguments: {exc}") from exc

    command = ["git", op, *argument_tokens]

    try:
        result = await _run_process(command, Path(ctx.cwd), GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        output = _truncate_text(
            _process_output(exc.stdout, exc.stderr), MAX_OUTPUT_BYTES, OUTPUT_TRUNCATION_MARKER
        )
        return _Outcome(False, output, f"git {op} timed out after {GIT_TIMEOUT_S}s")
    except OSError as exc:
        raise _ToolFailure(f"could not run git {op}: {exc}") from exc

    output = _truncate_text(
        _process_output(result.stdout, result.stderr), MAX_OUTPUT_BYTES, OUTPUT_TRUNCATION_MARKER
    )
    if result.returncode != 0:
        return _Outcome(False, output, f"git {op} exited with status {result.returncode}")
    return _Outcome(True, output)


async def _run_shell(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    command = args["cmd"]
    if not command:
        raise _ToolFailure("run_shell command must contain at least one token")
    timeout_s = args.get("timeout_s", 120)
    if timeout_s <= 0:
        raise _ToolFailure("run_shell timeout_s must be greater than zero")

    try:
        result = await _run_process(command, Path(ctx.cwd), timeout_s)
    except subprocess.TimeoutExpired as exc:
        output = _truncate_text(
            _process_output(exc.stdout, exc.stderr), MAX_OUTPUT_BYTES, OUTPUT_TRUNCATION_MARKER
        )
        return _Outcome(False, output, f"run_shell timed out after {timeout_s}s")
    except FileNotFoundError as exc:
        raise _ToolFailure(f"command not found: {command[0]!r}") from exc
    except OSError as exc:
        raise _ToolFailure(f"could not run command {command[0]!r}: {exc}") from exc

    output = _truncate_text(
        _process_output(result.stdout, result.stderr),
        MAX_OUTPUT_BYTES,
        OUTPUT_TRUNCATION_MARKER,
    )
    if result.returncode != 0:
        return _Outcome(False, output, f"run_shell exited with status {result.returncode}")
    return _Outcome(True, output)


async def _read_batch(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    paths = args["paths"]
    if not paths:
        raise _ToolFailure("read_batch requires at least one path")
    semaphore = asyncio.Semaphore(BATCH_READ_MAX_CONCURRENCY)

    async def _bounded_read(path: str) -> ToolResult:
        async with semaphore:
            read_args: dict[str, Any] = {"path": path}
            for key in ("offset", "limit"):
                if key in args:
                    read_args[key] = args[key]
            return await _run_read_result(read_args, ctx)

    results = await asyncio.gather(*(_bounded_read(path) for path in paths))
    parts: list[str] = []
    ok = True
    for path, result in zip(paths, results, strict=True):
        body = result.output if result.ok else (result.error or "read failed")
        parts.append(f"--- {path} ---\n{body}")
        if not result.ok:
            ok = False
    output = _truncate_text("\n\n".join(parts), MAX_OUTPUT_BYTES, OUTPUT_TRUNCATION_MARKER)
    return _Outcome(ok=ok, output=output)


async def _delegate(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    """Register one child proposal for supervisor validation.

    The tool never touches the worktree: it only acknowledges the proposal
    arguments (already validated against the ``delegate`` schema in
    ``run_tool``). The worker agent loop emits the ``propose_child`` wire
    message, and the supervisor re-validates the full revision with
    ``tasktree.build_tree`` at this task's terminal envelope, then admits or
    rejects the child.
    """
    child_task_id = args["child_task_id"]
    kind = args["kind"]
    if kind not in _ALLOWED_TASK_KINDS:
        return _Outcome(
            ok=False,
            error=(
                f"validation failed: unknown task kind {kind} (allowed: {_ALLOWED_TASK_KINDS_TEXT})"
            ),
        )
    return _Outcome(
        ok=True,
        output=(f"child {child_task_id} proposed; admission is validated when this task completes"),
    )


TOOL_DISPATCH: dict[str, ToolImplementation] = {
    "delegate": _delegate,
    "read_batch": _read_batch,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "git_op": _git_op,
    "run_shell": _run_shell,
}
_TOOL_PERMISSION_REQUIREMENTS = {"run_shell": "shell"}


async def _run_read_result(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    started_ns = time.monotonic_ns()
    try:
        outcome = await _read_file(args, ctx)
    except _ToolFailure as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            duration_ms=_duration_ms(started_ns),
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            error=f"read_batch failed: {exc}",
            duration_ms=_duration_ms(started_ns),
        )
    return ToolResult(
        ok=outcome.ok,
        output=outcome.output,
        error=outcome.error,
        duration_ms=_duration_ms(started_ns),
    )


def _read_batch_arguments(call: dict[str, Any]) -> dict[str, Any]:
    if "arguments" in call:
        return call["arguments"]
    if "name" in call:
        return {key: value for key, value in call.items() if key != "name"}
    return call


def _batch_failure_results(batch_size: int, reason: str) -> tuple[ToolResult, ...]:
    return tuple(ToolResult(ok=False, error=reason) for _ in range(batch_size))


async def _emit_read_batch_event(
    ctx: ToolContext, batch_index: int, batch_size: int, result: ToolResult
) -> None:
    if ctx.emit is None:
        return
    event = {
        "type": "tool_event",
        "tool": "read_batch",
        "batch_index": batch_index,
        "batch_size": batch_size,
        "ok": result.ok,
        "duration_ms": result.duration_ms,
    }
    emitted = ctx.emit(event)
    if isawaitable(emitted):
        await emitted


async def run_read_batch(
    calls: Sequence[dict[str, Any]], ctx: ToolContext
) -> tuple[ToolResult, ...]:
    """Validate and execute a batch of reads in one ``read_batch`` call.

    Validation covers the complete input before any file is opened. Relative
    paths use the worker cwd and absolute paths are allowed. Eligible reads run
    concurrently within a bounded limit, while results and completion events
    retain input order.
    """
    batch = tuple(calls)
    batch_size = len(batch)
    if batch_size == 0:
        return ()

    schema = next(
        (candidate for candidate in TOOL_SCHEMAS if candidate.get("name") == "read_batch"),
        None,
    )
    if schema is None:
        return _batch_failure_results(
            batch_size, "read_batch batch rejected atomically: read_batch schema is unavailable"
        )

    offered_tools = ctx.init.get("tools") if isinstance(ctx.init, Mapping) else None
    read_offered = (
        isinstance(offered_tools, Sequence)
        and not isinstance(offered_tools, str | bytes | bytearray)
        and "read_batch" in offered_tools
    )

    preflight_errors: list[str] = []
    if not read_offered:
        preflight_errors.append("read_batch is not offered in init.tools")
    for batch_index, call in enumerate(batch):
        if not isinstance(call, dict):
            preflight_errors.append(
                f"batch_index {batch_index}: validation failed: tool call must be an object"
            )
            continue
        arguments = _read_batch_arguments(call)
        if not isinstance(arguments, dict):
            preflight_errors.append(
                f"batch_index {batch_index}: validation failed: arguments must be an object"
            )
            continue
        raw_path = arguments.get("path")
        schema_arguments: dict[str, Any] = {"paths": [raw_path]}
        for key in ("offset", "limit"):
            if key in arguments:
                schema_arguments[key] = arguments[key]
        errors = validate_tool_call(schema, schema_arguments)
        preflight_errors.extend(f"batch_index {batch_index}: {error}" for error in errors)
    if preflight_errors:
        reason = "read_batch batch rejected atomically: " + "\n".join(preflight_errors)
        return _batch_failure_results(batch_size, reason)

    semaphore = asyncio.Semaphore(BATCH_READ_MAX_CONCURRENCY)

    async def _bounded_read(args: dict[str, Any]) -> ToolResult:
        async with semaphore:
            return await _run_read_result(args, ctx)

    tasks = [asyncio.create_task(_bounded_read(_read_batch_arguments(call))) for call in batch]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[ToolResult] = []
    for gathered_result in gathered:
        if isinstance(gathered_result, asyncio.CancelledError):
            raise gathered_result
        if isinstance(gathered_result, Exception):
            results.append(ToolResult(ok=False, error=f"read_batch failed: {gathered_result}"))
        else:
            results.append(cast(ToolResult, gathered_result))

    ordered_results = tuple(results)
    for batch_index, result in enumerate(ordered_results):
        await _emit_read_batch_event(ctx, batch_index, batch_size, result)
    return ordered_results


async def run_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Validate and execute one named worker tool."""
    started_ns = time.monotonic_ns()
    schema_by_name = {schema["name"]: schema for schema in TOOL_SCHEMAS}
    schema = schema_by_name.get(name) if isinstance(name, str) else None
    if schema is None:
        return ToolResult(
            ok=False,
            error=f"unknown tool: {name!r}",
            duration_ms=_duration_ms(started_ns),
        )

    validation_errors = validate_tool_call(schema, args)
    if validation_errors:
        return ToolResult(
            ok=False,
            error="\n".join(validation_errors),
            duration_ms=_duration_ms(started_ns),
        )

    required_permission = _TOOL_PERMISSION_REQUIREMENTS.get(name)
    if (
        required_permission is not None
        and ctx.policy is not None
        and not getattr(ctx.policy, required_permission)
    ):
        return ToolResult(
            ok=False,
            error=f"permission_denied:{required_permission}",
            duration_ms=_duration_ms(started_ns),
        )

    implementation = TOOL_DISPATCH[name]
    try:
        outcome = await implementation(args, ctx)
    except _ToolFailure as exc:
        return ToolResult(
            ok=False,
            error=str(exc),
            duration_ms=_duration_ms(started_ns),
        )
    except Exception as exc:
        return ToolResult(
            ok=False,
            error=f"{name} failed: {exc}",
            duration_ms=_duration_ms(started_ns),
        )
    return ToolResult(
        ok=outcome.ok,
        output=outcome.output,
        error=outcome.error,
        duration_ms=_duration_ms(started_ns),
    )


__all__ = [
    "BATCH_READ_MAX_CONCURRENCY",
    "MAX_OUTPUT_BYTES",
    "MAX_READ_BYTES",
    "TOOL_DISPATCH",
    "TOOL_SCHEMAS",
    "ToolContext",
    "ToolPermissionPolicy",
    "ToolResult",
    "run_read_batch",
    "run_tool",
]
