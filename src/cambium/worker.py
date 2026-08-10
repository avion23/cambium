"""Worker runtime (Opifex seed) — ``python -m cambium.worker``.

Speaks the Nuntius JSON-Lines wire protocol over stdio
(docs/architecture.md §5, docs/research/ipc-protocol-draft.md). One worker
executes one task and then exits:

    init                        ->  ready (echoes the init request_id and the
                                    generation fencing token)
    run_task                    ->  heartbeat(s) every ~1s while working
                                ->  result_envelope (echoes the run_task
                                    request_id) -> exit_message (connection
                                    level; carries NO request_id)
    check_health                ->  ok (echoes the request_id, generation)
    steer                       ->  {"action": "cancel"} aborts the current
                                    task (status cancelled); anything else is
                                    logged and ignored (v2.1 hook)
    cancel                      ->  ok (ack) then abort the current task with
                                    status "cancelled"
    shutdown                    ->  ok (ack), abort the current task, then
                                    exit_message (reason "shutdown") + exit 0

Defensive timeouts (worker self-protection if the supervisor dies):
    - init deadline: no init message within ``INIT_TIMEOUT_S`` (default 30 s,
      env ``CAMBIUM_INIT_TIMEOUT_S``) -> ``fatal_error`` + exit 1.
    - idle deadline: no message from the supervisor within ``IDLE_TIMEOUT_S``
      (default 300 s, env ``CAMBIUM_IDLE_TIMEOUT_S``) after ``ready`` -> the
      worker aborts any current task and exits gracefully (``exit_message``
      reason "idle", exit 0). No ``result_envelope`` is emitted for the
      aborted task — the supervisor is presumed gone.

Task spec (the ``run_task`` body) is compatible with
``scripts/fake_worker.py``'s task spec:

    task_id         stable task id (echoed everywhere)
    scratch_repo    git repo the throwaway worktree is branched from
    worktree_path   where the throwaway worktree is created (must stay under
                    the scratch repo's parent — path safety)
    branch          name of the throwaway branch
    target_file     file inside the worktree to edit (deterministic fallback;
                    must not escape it)
    marker          line appended to the target file (deterministic fallback)
    write_marker    bool; false forces the task to fail
    work_delay_s    optional float; pause before the edit (test hook so
                    cancellation is observable)

When ``init.fanout_config`` is present, the worker ignores ``target_file`` and
``marker``. It loads the provider file named by the worker's absolute
``CAMBIUM_PROVIDERS`` environment variable, makes one ``Diffundo.call`` using
the configured ``tier`` and ``model``, and accepts only this completion
contract (one line, no reasoning):

    append marker line to file <target_file>: <marker>

Malformed wire input is fatal: the worker emits ``fatal_error``, then
``exit_message`` (reason "fatal"), and exits nonzero (let-it-crash). The
process exit code is 0 when the task status is "succeeded" or when the exit
is a graceful supervisor- or worker-initiated close (shutdown, idle).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from cambium.auth import scrub_environment
from cambium.diffundo import CallResult, Diffundo, ProviderTier
from cambium.fencing import validate_worker_generation
from cambium.ipc import MAX_LINE_BYTES, MessageTooLong, read_message, write_message
from cambium.provider_config import load_providers

PROTO = 1
HEARTBEAT_INTERVAL_S = 1.0
INIT_TIMEOUT_S = 30.0
IDLE_TIMEOUT_S = 300.0
MAX_SUMMARY_CHARS = 2_000
MAX_DIFF_BYTES = 64 * 1024  # 64 KiB diff cap (ipc-protocol-draft.md §3)
EXIT_CODES = {"succeeded": 0, "failed": 1, "cancelled": 4}
PROVIDER_COMPLETION_PREFIX = "append marker line to file "
_COMPLETION_CONTRACT = re.compile(
    rf"\A{re.escape(PROVIDER_COMPLETION_PREFIX)}([^:\n]+): ([^\n]+)\Z"
)
_USAGE_COUNT_FIELDS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    }
)
_DIFFUNDO_OPTIONS = frozenset(
    {
        "call_budget_s",
        "pause_timeout_s",
        "breaker_window_size",
        "breaker_failure_threshold",
        "open_backoff_base",
        "retry_base_delay_s",
    }
)

logger = logging.getLogger(__name__)


class GenerationFenceError(RuntimeError):
    """The worker no longer owns the persisted worktree generation."""


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


async def send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    write_message(writer, msg)
    await writer.drain()


def git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=scrub_environment()
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _fenced_git(
    worktree: Path,
    generation: int,
    *args: str,
    cwd: str | Path | None = None,
) -> tuple[int, str, str]:
    """Run mutating git while continuously enforcing the generation fence."""
    _require_generation(worktree, generation)
    proc = subprocess.Popen(
        ["git", *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=scrub_environment(),
        start_new_session=True,
    )
    while proc.poll() is None:
        if validate_worker_generation(worktree, generation):
            time.sleep(0.001)
            continue
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise GenerationFenceError(
            f"generation mismatch for {worktree}: worker={generation}, "
            "persisted generation is different or missing"
        )
    stdout, stderr = proc.communicate()
    _require_generation(worktree, generation)
    return proc.returncode, stdout.strip(), stderr.strip()


def _require_generation(worktree: Path, generation: int) -> None:
    if not validate_worker_generation(worktree, generation):
        raise GenerationFenceError(
            f"generation mismatch for {worktree}: worker={generation}, "
            "persisted generation is different or missing"
        )


def _write_worktree_state(
    worktree: Path, generation: int, path: Path, content: str
) -> None:
    """Write worker state only while this process owns the current fence."""
    _require_generation(worktree, generation)
    path.write_text(content)


def cap_diff(diff: str) -> tuple[str, bool]:
    """Cap ``diff`` to ``MAX_DIFF_BYTES`` UTF-8 bytes, never splitting a
    codepoint; returns ``(diff, truncated)``."""
    raw = diff.encode("utf-8")
    if len(raw) <= MAX_DIFF_BYTES:
        return diff, False
    truncated = raw[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
    return truncated + "\n... [diff truncated]", True


def _provider_fanout_config(run: dict[str, Any]) -> dict[str, Any] | None:
    config = run.get("fanout_config")
    if not isinstance(config, dict) or not config:
        return None
    return config


def _provider_path() -> Path:
    configured = os.environ.get("CAMBIUM_PROVIDERS")
    if not configured:
        raise RuntimeError("provider configuration is not set in CAMBIUM_PROVIDERS")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _fanout_section(config: dict[str, Any]) -> dict[str, Any]:
    for key in ("diffundo", "router"):
        section = config.get(key)
        if isinstance(section, dict):
            return section
    return config


def _fanout_value(config: dict[str, Any], section: dict[str, Any], key: str) -> Any:
    value = config.get(key)
    if value is not None:
        return value
    return section.get(key)


def _provider_router(config: dict[str, Any]) -> tuple[Diffundo, ProviderTier, str]:
    providers = load_providers(_provider_path())
    section = _fanout_section(config)
    tier_value = _fanout_value(config, section, "tier")
    model = _fanout_value(config, section, "model")
    if not isinstance(tier_value, str) or not tier_value:
        raise ValueError("fanout_config requires a provider tier")
    if not isinstance(model, str) or not model:
        raise ValueError("fanout_config requires a provider model")
    try:
        tier = ProviderTier(tier_value)
    except ValueError as exc:
        raise ValueError(f"unsupported provider tier {tier_value!r}") from exc

    options: dict[str, Any] = {}
    for key in _DIFFUNDO_OPTIONS:
        value = _fanout_value(config, section, key)
        if value is not None:
            options[key] = value
    return Diffundo(providers, **options), tier, model


def _provider_prompt(run: dict[str, Any]) -> dict[str, Any]:
    task = run.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("provider task requires a non-empty task description")
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Cambium's deterministic coding worker.\n"
                    "Return exactly one append-marker decision.\n"
                    "Change only the requested file."
                ),
            },
            {
                "role": "user",
                "content": f"task={task.strip()}\nReturn one decision now.",
            },
        ]
    }


def _parse_provider_completion(content: str) -> tuple[str, str]:
    match = _COMPLETION_CONTRACT.fullmatch(content.strip())
    if match is None:
        raise ValueError("completion does not match the append-marker contract")
    target_file, marker = match.groups()
    target_path = Path(target_file)
    if target_path.is_absolute() or ".." in target_path.parts:
        raise ValueError("completion target_file escapes the worktree")
    if not marker.strip():
        raise ValueError("completion marker is empty")
    return target_file, marker


def _usage_counts(usage: dict[str, Any] | None) -> dict[str, int | float]:
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key, value in usage.items()
        if key in _USAGE_COUNT_FIELDS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    }


def _provider_metadata(result: CallResult, requested_model: str) -> dict[str, Any]:
    return {
        "provider": result.provider,
        # The requested model is host-authored. Never copy the provider's
        # response model into the result envelope or durable event payload.
        "model": requested_model,
        "usage": _usage_counts(result.usage),
        "latency_s": max(0.0, float(result.latency_s)),
    }


def _provider_edit(run: dict[str, Any], config: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    router, tier, model = _provider_router(config)
    try:
        result = asyncio.run(router.call(tier, _provider_prompt(run), model=model))
    except Exception as exc:
        # Provider errors may contain response text or request details. Only the
        # exception class is safe to carry into the result envelope.
        raise RuntimeError(f"provider completion failed: {exc.__class__.__name__}") from None
    if result.model != model:
        raise ValueError("provider response model mismatch")
    target_file, marker = _parse_provider_completion(result.content)
    return target_file, marker, _provider_metadata(result, model)


def do_work(run: dict[str, Any], stop: threading.Event) -> dict[str, Any]:
    """Execute one task: throwaway worktree, one-file edit, commit.

    Returns the outcome dict:

        status          "succeeded" | "failed" | "cancelled"
        failure_reason  str | None (set when status != "succeeded")
        commits         list[str] of SHAs produced
        files_changed   list[str] of paths changed
        diff            ``git diff <base_commit>..HEAD`` in the worktree,
                        capped at ``MAX_DIFF_BYTES`` UTF-8 bytes
        diff_truncated  bool; true when the diff was capped
        summary         worker-authored, <= ``MAX_SUMMARY_CHARS``

    Cooperative cancellation via ``stop``: the worker checks it between git
    steps and reports status "cancelled" if it was set.
    """
    outcome: dict[str, Any] = {
        "status": "failed",
        "failure_reason": None,
        "commits": [],
        "files_changed": [],
        "diff": "",
        "diff_truncated": False,
        "summary": "",
    }
    try:
        scratch = Path(run["scratch_repo"]).resolve()
        worktree = Path(run["worktree_path"]).resolve()
        generation = run.get("generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
        ):
            outcome["failure_reason"] = "invalid worker generation"
            return outcome
        write_marker = bool(run.get("write_marker", True))
        provider_metadata: dict[str, Any] | None = None

        fanout_config = _provider_fanout_config(run)
        if fanout_config is None:
            target_file = run["target_file"]
            marker = run["marker"]
        else:
            try:
                target_file, marker, provider_metadata = _provider_edit(run, fanout_config)
            except ValueError:
                outcome["failure_reason"] = "provider completion violated the edit contract"
                return outcome
            except RuntimeError as exc:
                outcome["failure_reason"] = str(exc)
                return outcome

        session_root = scratch.parent
        if not worktree.is_relative_to(session_root):
            outcome["failure_reason"] = (
                f"worktree_path {worktree} outside session scratch root {session_root}")
            return outcome
        target = (worktree / target_file).resolve()
        if not target.is_relative_to(worktree):
            outcome["failure_reason"] = f"target_file {target_file!r} escapes the worktree"
            return outcome

        def guarded_git(*args: str, cwd: str | Path | None = None) -> tuple[int, str, str]:
            _require_generation(worktree, generation)
            if args and args[0] in {"add", "commit"}:
                return _fenced_git(worktree, generation, *args, cwd=cwd)
            return git(*args, cwd=cwd)

        _require_generation(worktree, generation)
        rc, _out, err = guarded_git("rev-parse", "main", cwd=scratch)
        if rc != 0:
            outcome["failure_reason"] = f"no main branch in scratch repo: {err}"
            return outcome
        base_commit = _out

        if not worktree.exists():
            outcome["failure_reason"] = f"worker worktree is missing: {worktree}"
            return outcome
        rc, _out, err = guarded_git("rev-parse", "HEAD", cwd=worktree)
        if rc != 0:
            outcome["failure_reason"] = f"cannot resolve worktree HEAD: {err}"
            return outcome
        worker_identity = secrets.token_hex(16)

        # Optional work_delay_s pauses before the edit (testing hook); the
        # pause polls ``stop`` so cancellation stays responsive.
        delay = float(run.get("work_delay_s", 0.0) or 0.0)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if stop.is_set():
                outcome["status"] = "cancelled"
                return outcome
            time.sleep(min(0.05, deadline - time.monotonic()))

        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome
        _require_generation(worktree, generation)
        if not write_marker:
            outcome["failure_reason"] = "marker not written (write_marker=false)"
            return outcome
        if not target.exists():
            outcome["failure_reason"] = f"target file missing: {target_file}"
            return outcome
        _require_generation(worktree, generation)
        _write_worktree_state(
            worktree,
            generation,
            target,
            target.read_text().rstrip("\n") + "\n" + marker + "\n",
        )
        _require_generation(worktree, generation)
        if marker not in target.read_text():
            outcome["failure_reason"] = "edit missing: marker not present after write"
            return outcome
        if stop.is_set():
            outcome["status"] = "cancelled"
            return outcome

        guarded_git("add", target_file, cwd=worktree)
        rc, _out, err = guarded_git(
            "commit",
            "-m",
            f"cambium-ipc: {run['task_id']}",
            "-m",
            f"Cambium-Worker-Generation: {generation}\n"
            f"Cambium-Worker-Identity: {worker_identity}",
            cwd=worktree,
        )
        if rc != 0:
            outcome["failure_reason"] = f"commit failed: {err}"
            return outcome
        _rc, sha, _err = guarded_git("rev-parse", "HEAD", cwd=worktree)
        _rc, diff, _err = guarded_git("diff", f"{base_commit}..HEAD", cwd=worktree)
        diff, diff_truncated = cap_diff(diff)
        _require_generation(worktree, generation)
        outcome.update(
            status="succeeded",
            failure_reason=None,
            commits=[sha],
            files_changed=[target_file],
            diff=diff,
            diff_truncated=diff_truncated,
            summary=f"appended marker to {target_file}"[:MAX_SUMMARY_CHARS],
            provider_metadata=provider_metadata,
        )
        return outcome
    except GenerationFenceError as exc:
        outcome["failure_reason"] = str(exc)
        return outcome
    except Exception as exc:  # let-it-crash: report as a failure, not a hang
        outcome["failure_reason"] = f"task crashed: {exc}"
        return outcome


async def _heartbeat_loop(
    writer: asyncio.StreamWriter,
    task_id: str,
    generation: int,
    stop: threading.Event,
) -> None:
    turn = 0
    while not stop.is_set():
        await send(writer, {
            "type": "heartbeat",
            "task_id": task_id,
            "generation": generation,
            "turn": turn,
            "tool": None,
            "status": "working",
            "monotonic_ms": _monotonic_ms(),
        })
        turn += 1
        if stop.is_set():
            # Observed the stop flag right after this send: exit at the safe
            # point (between iterations) instead of starting another send.
            break
        await asyncio.sleep(HEARTBEAT_INTERVAL_S)


async def _run_task(
    writer: asyncio.StreamWriter,
    run: dict[str, Any],
    task_id: str,
    generation: int,
    stop: threading.Event,
) -> dict[str, Any]:
    """Run the task body with heartbeats; returns the terminal outcome."""
    started_at = time.time()
    run_rid = run["request_id"]

    hb = asyncio.create_task(_heartbeat_loop(writer, task_id, generation, stop))
    try:
        outcome = await asyncio.to_thread(do_work, run, stop)
    finally:
        stop.set()
        # Heartbeat stop: the write is enqueued synchronously and atomically,
        # but cancel() between the write and its drain could leave the next
        # (result) message written against a mid-drain heartbeat. Never cancel
        # mid-send: set the stop flag and let the loop observe it at its safe
        # point (after the in-flight send completes, before the next one).
        # A hard cancel is only a fallback if the loop fails to drain promptly.
        try:
            await asyncio.wait_for(hb, timeout=HEARTBEAT_INTERVAL_S + 1.0)
        except (TimeoutError, asyncio.CancelledError):
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
    outcome["request_id"] = run_rid
    outcome["task_id"] = task_id
    outcome["generation"] = generation
    outcome["started_at"] = started_at
    outcome["ended_at"] = time.time()
    return outcome


def _exit_reason(status: str) -> str:
    return {"succeeded": "done", "failed": "failed", "cancelled": "cancelled"}.get(
        status, "failed")


async def _emit_result_envelope(writer: asyncio.StreamWriter, outcome: dict[str, Any]) -> None:
    status = outcome["status"]
    envelope = {
        "type": "result_envelope",
        "request_id": outcome["request_id"],
        "task_id": outcome["task_id"],
        "generation": outcome["generation"],
        "status": status,
        "exit_code": EXIT_CODES.get(status, 1),
        "commits": outcome.get("commits", []),
        "files_changed": outcome.get("files_changed", []),
        "diff": outcome.get("diff", ""),
        "diff_truncated": bool(outcome.get("diff_truncated", False)),
        "summary": (outcome.get("summary") or "")[:MAX_SUMMARY_CHARS],
        "failure_reason": outcome.get("failure_reason"),
        "started_at": outcome.get("started_at"),
        "ended_at": outcome.get("ended_at"),
    }
    provider_metadata = outcome.get("provider_metadata")
    if isinstance(provider_metadata, dict):
        envelope["provider_metadata"] = provider_metadata
    await send(writer, envelope)


async def _emit_result(writer: asyncio.StreamWriter, outcome: dict[str, Any]) -> None:
    """Emit result_envelope + the authoritative exit_message (normal completion)."""
    await _emit_result_envelope(writer, outcome)
    await send(writer, {
        "type": "exit_message",
        "task_id": outcome["task_id"],
        "generation": outcome["generation"],
        "reason": _exit_reason(outcome["status"]),
        "monotonic_ms": _monotonic_ms(),
    })


async def _send_ok(
    writer: asyncio.StreamWriter,
    msg: dict[str, Any],
    task_id: str,
    generation: int,
) -> None:
    await send(writer, {
        "type": "ok",
        "request_id": msg.get("request_id") if isinstance(msg, dict) else None,
        "task_id": task_id,
        "generation": generation,
        "monotonic_ms": _monotonic_ms(),
    })


async def _send_pong(
    writer: asyncio.StreamWriter,
    msg: dict[str, Any],
    task_id: str,
    generation: int,
) -> None:
    await send(writer, {
        "type": "pong",
        "request_id": msg.get("request_id"),
        "task_id": task_id,
        "generation": generation,
        "monotonic_ms": _monotonic_ms(),
    })


async def _fatal(writer: asyncio.StreamWriter, msg: Any, message: str) -> int:
    context = msg if isinstance(msg, dict) else {}
    await send(writer, {
        "type": "fatal_error",
        "request_id": context.get("request_id"),
        "task_id": context.get("task_id"),
        "generation": context.get("generation"),
        "error_type": "invalid_message",
        "message": message[:500],
        "recoverable": False,
    })
    await send(writer, {
        "type": "exit_message",
        "task_id": context.get("task_id"),
        "generation": context.get("generation"),
        "reason": "fatal",
        "monotonic_ms": _monotonic_ms(),
    })
    return 1


async def run(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    """The worker wire loop. Returns the process exit code."""
    init_timeout = _env_float("CAMBIUM_INIT_TIMEOUT_S", INIT_TIMEOUT_S)
    idle_timeout = _env_float("CAMBIUM_IDLE_TIMEOUT_S", IDLE_TIMEOUT_S)

    try:
        first = await asyncio.wait_for(read_message(reader), timeout=init_timeout)
    except TimeoutError:
        return await _fatal(writer, {}, "init timeout: no init message within deadline")
    except MessageTooLong:
        return await _fatal(writer, {}, "wire line exceeded the length cap")
    if first is None:
        return 1
    if not isinstance(first, dict) or first.get("type") != "init" or "request_id" not in first:
        return await _fatal(writer, first, "expected init as the first message")

    init_rid = first["request_id"]
    task_id = first.get("task_id", "unknown")
    generation = first.get("generation", 1)
    init_fanout_config = first.get("fanout_config")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        return await _fatal(writer, first, "init generation must be a positive integer")
    await send(writer, {
        "type": "ready",
        "request_id": init_rid,
        "task_id": task_id,
        "pid": os.getpid(),
        "generation": generation,
        "proto": first.get("proto", PROTO),
        "monotonic_ms": _monotonic_ms(),
    })

    current: asyncio.Task[dict[str, Any]] | None = None
    stop = threading.Event()

    while True:
        read_task = asyncio.create_task(
            asyncio.wait_for(read_message(reader), timeout=idle_timeout))
        pending = {read_task}
        if current is not None:
            pending.add(current)
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        if current is not None and current in done:
            task = current
            current = None
            read_task.cancel()
            try:
                await read_task
            except BaseException:
                pass
            try:
                outcome = task.result()
            except Exception as exc:
                return await _fatal(writer, {}, f"task crashed: {exc}")
            await _emit_result(writer, outcome)
            return EXIT_CODES.get(outcome["status"], 1)

        try:
            msg = read_task.result()
        except TimeoutError:
            # No message from the supervisor within the idle deadline: the
            # supervisor is presumed gone. Abort any current task and exit
            # gracefully (documented in the module docstring).
            stop.set()
            if current is not None:
                task = current
                current = None
                try:
                    await task
                except BaseException:
                    pass
            await send(writer, {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "idle",
                "monotonic_ms": _monotonic_ms(),
            })
            return 0
        except MessageTooLong:
            return await _fatal(writer, {}, "wire line exceeded the length cap")
        except Exception as exc:
            return await _fatal(writer, {}, f"wire read failed: {exc}")

        if msg is None:
            # stdin closed: no further requests can arrive.
            await send(writer, {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "crash",
                "monotonic_ms": _monotonic_ms(),
            })
            return 1

        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype == "run_task":
            if current is not None:
                return await _fatal(writer, msg, "run_task while a task is already running")
            if "request_id" not in msg:
                return await _fatal(writer, msg, "run_task without a request_id")
            claimed_generation = msg.get("generation", generation)
            if claimed_generation != generation:
                return await _fatal(writer, msg, "run_task generation does not match init")
            msg = {**msg, "generation": generation}
            stop = threading.Event()
            task_run = dict(msg)
            if init_fanout_config is not None:
                # Provider configuration belongs to init. It is kept in the
                # worker's local task context and never sent back over IPC.
                task_run["fanout_config"] = init_fanout_config
            current = asyncio.create_task(
                _run_task(writer, task_run, task_id, generation, stop))
        elif mtype == "check_health":
            await _send_ok(writer, msg, task_id, generation)
        elif mtype == "ping":
            await _send_pong(writer, msg, task_id, generation)
        elif mtype == "steer":
            payload = msg.get("payload") or {}
            # Structured parse: only an exact {"action": "cancel"} aborts.
            # Free text containing the word "cancel" must NOT abort.
            if isinstance(payload, dict) and payload.get("action") == "cancel":
                logger.info("steer: cancel requested")
                stop.set()
            else:
                logger.info("steer (v2.1 hook; continuing): %s",
                            json.dumps(payload)[:200])
        elif mtype == "cancel":
            logger.info("cancel: aborting current task")
            await _send_ok(writer, msg, task_id, generation)
            stop.set()
        elif mtype == "shutdown":
            await _send_ok(writer, msg, task_id, generation)
            if current is not None:
                stop.set()
                task = current
                current = None
                try:
                    outcome = await task
                    await _emit_result_envelope(writer, outcome)
                except BaseException:
                    pass
            await send(writer, {
                "type": "exit_message",
                "task_id": task_id,
                "generation": generation,
                "reason": "shutdown",
                "monotonic_ms": _monotonic_ms(),
            })
            return 0
        else:
            return await _fatal(writer, msg, f"unknown message type {mtype!r}")


class _WriterProtocol(asyncio.streams.FlowControlMixin):
    """Flow-control protocol for the stdout write transport.

    ``StreamWriter.wait_closed`` resolves the protocol's ``_closed`` future,
    so it must be tied to THIS transport's ``connection_lost`` — not the
    reader's (whose stdin pipe stays open for the worker's lifetime).
    """

    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: BaseException | None) -> None:
        if not self._closed.done():
            self._closed.set_result(exc)
        super().connection_lost(exc)

    def _get_close_waiter(self, stream: asyncio.StreamWriter) -> asyncio.Future:
        return self._closed


async def _open_stdio() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Wrap stdin/stdout in asyncio streams (protocol stream = stdout)."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    read_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: read_protocol, sys.stdin.buffer)
    write_protocol = _WriterProtocol()
    transport, _ = await loop.connect_write_pipe(
        lambda: write_protocol, sys.stdout.buffer)
    writer = asyncio.StreamWriter(transport, write_protocol, reader, loop)
    return reader, writer


async def _amain() -> int:
    reader, writer = await _open_stdio()
    try:
        return await run(reader, writer)
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    sys.exit(main())
