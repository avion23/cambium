"""Executable implementations for the worker tool catalogue.

The LLM-facing schemas remain the source of truth for argument validation.
Every operation runs inside an injected :class:`ToolContext`; this keeps
linting and dependency wiring out of process-global state.
"""

from __future__ import annotations

import asyncio
import json
import keyword
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from inspect import isawaitable
from pathlib import Path
from queue import Empty, Queue
from threading import Lock, Thread, current_thread
from typing import Any

from . import ast_tools
from .auth import scrub_environment
from .lint_diag import LintDiag
from .schemas import TOOL_SCHEMAS, validate_tool_call

MAX_READ_BYTES = 100 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
GIT_TIMEOUT_S = 30
GET_SIGNATURE_READ_TIMEOUT_S = 1.0
BATCH_READ_MAX_CONCURRENCY = 4
READ_TRUNCATION_MARKER = "\n... [file truncated]"
OUTPUT_TRUNCATION_MARKER = "\n... [output truncated]"

ToolEventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass(slots=True)
class ToolContext:
    """Dependencies needed by one tool invocation."""

    cwd: Path | str
    lint: LintDiag | None = None
    init: Mapping[str, Any] | None = None
    emit: ToolEventSink | None = None
    _root: Path = field(init=False, repr=False)
    _root_fd: int | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        root = Path(self.cwd).resolve()
        self.cwd = root
        self._root = root
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        self._root_fd = os.open(root, flags)

    def close(self) -> None:
        root_fd = self._root_fd
        self._root_fd = None
        if root_fd is not None:
            os.close(root_fd)

    def __enter__(self) -> ToolContext:
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def __del__(self) -> None:
        root_fd = getattr(self, "_root_fd", None)
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                pass


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
_DaemonWorkItem = tuple[Future[Any], Callable[..., Any], tuple[Any, ...], dict[str, Any]] | None


class _DaemonSingleThreadExecutor(Executor):
    """Run one blocking operation without joining it during loop shutdown.

    The executor is one-shot and owns a daemon worker.  A timed-out read calls
    ``shutdown(wait=False)``, so ``asyncio.run`` does not wait for a blocked
    system call and the worker cannot consume the event loop's shared default
    executor.  The daemon thread can remain until the underlying call returns;
    this is the deliberate tradeoff for protecting the caller from an
    uninterruptible regular-file read.
    """

    def __init__(self) -> None:
        self._work_queue: Queue[_DaemonWorkItem] = Queue()
        self._shutdown_lock = Lock()
        self._shutdown = False
        self._thread = Thread(
            target=self._worker,
            name="cambium-get-signature-read",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        future: Future[Any] = Future()
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule work after executor shutdown")
            self._work_queue.put((future, fn, args, kwargs))
        return future

    def _worker(self) -> None:
        while True:
            work_item = self._work_queue.get()
            if work_item is None:
                return
            future, fn, args, kwargs = work_item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._shutdown_lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        work_item = self._work_queue.get_nowait()
                    except Empty:
                        break
                    if work_item is not None:
                        work_item[0].cancel()
            self._work_queue.put(None)

        if wait and current_thread() is not self._thread:
            self._thread.join()


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


def _serialize_signature_result(result: dict[str, Any]) -> str:
    def serialize(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    output = serialize(result)
    if len(output.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return output

    envelope = {**result, "signature": "", "truncated": True}
    if len(serialize(envelope).encode("utf-8")) > MAX_OUTPUT_BYTES:
        return serialize(
            {"signature": OUTPUT_TRUNCATION_MARKER, "truncated": True}
        )

    signature = result["signature"]
    low = 0
    high = len(signature)
    best_signature = ""
    while low <= high:
        midpoint = (low + high) // 2
        candidate = signature[:midpoint] + OUTPUT_TRUNCATION_MARKER
        envelope["signature"] = candidate
        if len(serialize(envelope).encode("utf-8")) <= MAX_OUTPUT_BYTES:
            best_signature = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1

    if not best_signature:
        return serialize(
            {"signature": OUTPUT_TRUNCATION_MARKER, "truncated": True}
        )
    envelope["signature"] = best_signature
    return serialize(envelope)


def _confined_path(ctx: ToolContext, raw_path: str) -> Path:
    root = ctx._root
    candidate = (root / raw_path).resolve()
    if not candidate.is_relative_to(root):
        raise _ToolFailure(f"path escapes worktree: {raw_path!r}")
    return candidate


def _open_confined_read_fd(ctx: ToolContext, path: Path) -> int:
    """Open a resolved worktree path without following a replacement symlink."""
    root = ctx._root
    try:
        components = path.relative_to(root).parts
    except ValueError as exc:
        raise _ToolFailure(f"path escapes worktree: {path!r}") from exc
    if not components:
        raise IsADirectoryError(path)

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | nofollow | directory | close_on_exec
    file_flags = os.O_RDONLY | nofollow | nonblocking | close_on_exec

    root_fd = ctx._root_fd
    if root_fd is None:
        raise _ToolFailure("worktree context is closed")
    directory_fd = os.dup(root_fd)
    try:
        for component in components[:-1]:
            next_directory_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_directory_fd
        return os.open(components[-1], file_flags, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


def _display_path(ctx: ToolContext, path: Path) -> str:
    relative = path.relative_to(ctx._root)
    return relative.as_posix() or "."


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise _ToolFailure(f"file not found: {path}") from exc
    except IsADirectoryError as exc:
        raise _ToolFailure(f"path is a directory: {path}") from exc
    except UnicodeDecodeError as exc:
        raise _ToolFailure(f"file is not valid UTF-8: {path}") from exc


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _read_file_sync(ctx: ToolContext, path: Path, display_path: str) -> _Outcome:
    descriptor: int | None = None
    try:
        descriptor = _open_confined_read_fd(ctx, path)
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(path)
        if not stat.S_ISREG(mode):
            raise _ToolFailure(f"path is not a regular file: {display_path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(MAX_READ_BYTES + 1)
    except FileNotFoundError as exc:
        raise _ToolFailure(f"file not found: {display_path}") from exc
    except IsADirectoryError as exc:
        raise _ToolFailure(f"path is a directory: {display_path}") from exc
    except OSError as exc:
        raise _ToolFailure(f"could not read {display_path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if len(raw) > MAX_READ_BYTES:
        output = _truncate_bytes(raw, MAX_READ_BYTES, READ_TRUNCATION_MARKER)
    else:
        try:
            output = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _ToolFailure(f"file is not valid UTF-8: {display_path}") from exc
    return _Outcome(ok=True, output=output)


async def _read_file(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    path = _confined_path(ctx, args["path"])
    display_path = _display_path(ctx, path)
    return await asyncio.to_thread(_read_file_sync, ctx, path, display_path)


def _lint_feedback(ctx: ToolContext, path: Path) -> str:
    if ctx.lint is None:
        return ""
    diagnostics = ctx.lint.lint_file(path)
    feedback = ctx.lint.format_diags(diagnostics)
    return feedback if isinstance(feedback, str) else str(feedback)


async def _write_file(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    path = _confined_path(ctx, args["path"])
    try:
        _atomic_write(path, args["content"])
    except OSError as exc:
        raise _ToolFailure(f"could not write {_display_path(ctx, path)}: {exc}") from exc

    output = f"wrote {_display_path(ctx, path)}"
    feedback = _lint_feedback(ctx, path)
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
    path = _confined_path(ctx, args["path"])
    content = _read_text(path)
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
        _atomic_write(path, replacement)
    except OSError as exc:
        raise _ToolFailure(f"could not edit {_display_path(ctx, path)}: {exc}") from exc
    return _Outcome(ok=True, output=f"edited {_display_path(ctx, path)}")


def _search_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise _ToolFailure(f"search path is not a file or directory: {root}")

    files: list[Path] = []
    for directory, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name != ".git")
        for name in sorted(names):
            path = Path(directory) / name
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved.is_relative_to(Path(root).resolve()) and resolved.is_file():
                files.append(resolved)
    return files


def _grep_fallback(pattern: str, root: Path, worktree: Path) -> str:
    try:
        expression = re.compile(pattern)
    except re.error as exc:
        raise _ToolFailure(f"invalid regular expression: {exc}") from exc

    matches: list[str] = []
    for path in _search_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\x00" in text:
            continue
        display = path.relative_to(worktree).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            if expression.search(line):
                matches.append(f"{display}:{line_number}:{line}")
    return "\n".join(matches)


async def _grep_code(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    raw_path = args.get("path")
    root = _confined_path(ctx, raw_path if raw_path is not None else ".")
    if not root.exists():
        raise _ToolFailure(f"search path not found: {raw_path!r}")

    rg = shutil.which("rg")
    if rg is None:
        output = await asyncio.to_thread(_grep_fallback, args["pattern"], root, Path(ctx.cwd))
        return _Outcome(ok=True, output=output)

    command = [rg, "-n", "--no-heading", args["pattern"]]
    if raw_path is not None:
        command.append(_display_path(ctx, root))
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=ctx.cwd,
            env=scrub_environment(),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        output = await asyncio.to_thread(_grep_fallback, args["pattern"], root, Path(ctx.cwd))
        return _Outcome(ok=True, output=output)
    except OSError as exc:
        raise _ToolFailure(f"could not run ripgrep: {exc}") from exc

    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise _ToolFailure(f"ripgrep failed: {detail}")
    return _Outcome(ok=True, output=result.stdout)


def _read_and_extract_signature(
    ctx: ToolContext, path: Path, display_path: str, symbol: str
) -> dict[str, Any] | None:
    descriptor: int | None = None
    try:
        descriptor = _open_confined_read_fd(ctx, path)
        mode = os.fstat(descriptor).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(path)
        if not stat.S_ISREG(mode):
            raise _ToolFailure(f"path is not a regular file: {display_path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(MAX_READ_BYTES + 1)
    except FileNotFoundError as exc:
        raise _ToolFailure(f"file not found: {display_path}") from exc
    except IsADirectoryError as exc:
        raise _ToolFailure(f"path is a directory: {display_path}") from exc
    except OSError as exc:
        raise _ToolFailure(f"could not read {display_path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    if len(raw) > MAX_READ_BYTES:
        raise _ToolFailure(
            f"get_signature source exceeds MAX_READ_BYTES ({MAX_READ_BYTES} bytes): "
            f"{display_path}"
        )
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ToolFailure(f"file is not valid UTF-8: {display_path}") from exc
    return ast_tools.extract_signature(source, symbol)


async def _get_signature(args: dict[str, Any], ctx: ToolContext) -> _Outcome:
    raw_path = args["path"]
    if not raw_path.strip() or "\x00" in raw_path:
        raise _ToolFailure("get_signature path must be a non-empty path")
    path = _confined_path(ctx, raw_path)

    symbol = args["symbol"]
    if not symbol.isidentifier() or keyword.iskeyword(symbol):
        raise _ToolFailure("get_signature symbol must be a Python identifier")

    display_path = _display_path(ctx, path)
    executor = _DaemonSingleThreadExecutor()
    try:
        signature = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                executor,
                _read_and_extract_signature, ctx, path, display_path, symbol
            ),
            timeout=GET_SIGNATURE_READ_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise _ToolFailure(
            f"get_signature read timed out after {GET_SIGNATURE_READ_TIMEOUT_S}s: "
            f"{display_path}"
        ) from exc
    except SyntaxError as exc:
        location = f" at line {exc.lineno}" if exc.lineno is not None else ""
        raise _ToolFailure(
            f"could not parse {display_path}{location}: {exc.msg}"
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    if signature is None:
        raise _ToolFailure(f"symbol not found: {symbol!r} in {display_path}")

    result = {"path": display_path, **signature}
    return _Outcome(ok=True, output=_serialize_signature_result(result))


def _process_output(stdout: Any, stderr: Any) -> str:
    standard_output = (
        stdout.decode("utf-8", errors="replace")
        if isinstance(stdout, bytes)
        else str(stdout or "")
    )
    standard_error = (
        stderr.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes)
        else str(stderr or "")
    )
    if standard_output and standard_error:
        separator = "" if standard_output.endswith("\n") else "\n"
        return standard_output + separator + standard_error
    return standard_output or standard_error


async def _run_process(
    command: list[str], cwd: Path, timeout_s: int
) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        command,
        cwd=cwd,
        env=scrub_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_s,
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
            return await _run_read_result({"path": path}, ctx)

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
    return _Outcome(
        ok=True,
        output=(
            f"child {child_task_id} proposed; "
            "admission is validated when this task completes"
        ),
    )


TOOL_DISPATCH: dict[str, ToolImplementation] = {
    "delegate": _delegate,
    "read_file": _read_file,
    "read_batch": _read_batch,
    "write_file": _write_file,
    "edit_file": _edit_file,
    "grep_code": _grep_code,
    "get_signature": _get_signature,
    "git_op": _git_op,
    "run_shell": _run_shell,
}


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
            error=f"read_file failed: {exc}",
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
        "tool": "read_file",
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
    """Validate and execute a batch of confined ``read_file`` calls.

    Validation covers the complete input before any file is opened, including
    every path's containment inside the worktree. Eligible reads run
    concurrently within a bounded limit, while results and completion events
    retain input order.
    """
    batch = tuple(calls)
    batch_size = len(batch)
    if batch_size == 0:
        return ()

    schema = next(
        (candidate for candidate in TOOL_SCHEMAS if candidate.get("name") == "read_file"),
        None,
    )
    if schema is None:
        return _batch_failure_results(
            batch_size, "read_file batch rejected atomically: read_file schema is unavailable"
        )

    validation_errors = [validate_tool_call(schema, call) for call in batch]
    offered_tools = ctx.init.get("tools") if isinstance(ctx.init, Mapping) else None
    read_offered = isinstance(offered_tools, Sequence) and not isinstance(
        offered_tools, (str, bytes, bytearray)
    ) and "read_file" in offered_tools

    preflight_errors: list[str] = []
    if not read_offered:
        preflight_errors.append("read_file is not offered in init.tools")
    for batch_index, errors in enumerate(validation_errors):
        preflight_errors.extend(
            f"batch_index {batch_index}: {error}" for error in errors
        )
    for batch_index, call in enumerate(batch):
        if not isinstance(call, dict):
            continue
        arguments = _read_batch_arguments(call)
        if not isinstance(arguments, dict):
            continue
        raw_path = arguments.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        try:
            _confined_path(ctx, raw_path)
        except _ToolFailure as exc:
            preflight_errors.append(f"batch_index {batch_index}: {exc}")
    if preflight_errors:
        reason = "read_file batch rejected atomically: " + "\n".join(preflight_errors)
        return _batch_failure_results(batch_size, reason)

    semaphore = asyncio.Semaphore(BATCH_READ_MAX_CONCURRENCY)

    async def _bounded_read(args: dict[str, Any]) -> ToolResult:
        async with semaphore:
            return await _run_read_result(args, ctx)

    tasks = [
        asyncio.create_task(_bounded_read(_read_batch_arguments(call)))
        for call in batch
    ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[ToolResult] = []
    for gathered_result in gathered:
        if isinstance(gathered_result, asyncio.CancelledError):
            raise gathered_result
        if isinstance(gathered_result, Exception):
            results.append(ToolResult(ok=False, error=f"read_file failed: {gathered_result}"))
        else:
            results.append(gathered_result)

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
    "ToolResult",
    "run_read_batch",
    "run_tool",
]
