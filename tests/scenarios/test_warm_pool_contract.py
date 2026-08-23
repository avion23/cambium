"""Contract tests for the opt-in supervisor warm-worker pool."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from asyncio.subprocess import Process

import pytest

from cambium import cli
from cambium import supervisor as supervisor_module
from cambium.supervisor import PlanResult, TaskResult, run_plan


class _RuntimeProbe:
    """Small run_plan seam that records the pool bound without spawning workers."""

    sizes: list[int] = []

    def __init__(
        self,
        session_dir: Path,
        store: Any,
        on_event: Any = None,
        *,
        warm_pool_size: int = 0,
        **_: Any,
    ) -> None:
        del session_dir, on_event
        self._store = store
        self._results: dict[str, TaskResult] = {}
        self._lanes: dict[str, Any] = {}
        self.last_envelope: dict[str, Any] | None = None
        self.sizes.append(warm_pool_size)

    async def start(self) -> None:
        return

    def set_session_tasks(self, specs: list[dict[str, Any]]) -> None:
        del specs

    async def reconcile(self, specs: list[dict[str, Any]]) -> None:
        del specs

    async def supervise_task(self, spec: dict[str, Any]) -> None:
        task_id = spec["task_id"]
        self._results[task_id] = TaskResult(
            task_id=task_id,
            status="succeeded",
            exit_code=0,
        )

    async def shutdown(self, session_status: str = "ended") -> None:
        del session_status
        await asyncio.to_thread(self._store.close)

    def plan_result(self) -> PlanResult:
        return PlanResult(results=tuple(self._results.values()))


@dataclass
class _FakeProcess:
    returncode: int | None = None


def _plan(session_dir: Path) -> dict[str, list[dict[str, str]]]:
    return {
        "tasks": [
            {
                "task_id": "probe",
                "task": "probe the warm-pool contract",
                "repo": str(session_dir / "repo"),
                "worktree_path": str(session_dir / "worktree"),
                "branch": "probe",
            }
        ]
    }


def test_cli_warm_pool_default_is_zero() -> None:
    args = cli._build_parser().parse_args(["supervisor", "--session-dir", "/tmp/session", "--demo"])

    assert args.warm_pool_size == 0


def test_cli_supervisor_forwards_explicit_warm_pool_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cambium import supervisor

    calls: list[list[str]] = []
    monkeypatch.setattr(
        supervisor,
        "main",
        lambda argv=None: cast(Any, calls.append(list(argv or []))) or 0,
    )

    assert (
        cli.main(
            [
                "supervisor",
                "--session-dir",
                str(tmp_path),
                "--demo",
                "--warm-pool-size",
                "3",
            ]
        )
        == 0
    )
    assert calls == [
        [
            "--session-dir",
            str(tmp_path),
            "--demo",
            "--warm-pool-size",
            "3",
        ]
    ]


def test_run_plan_defaults_and_forwards_warm_pool_size(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(supervisor_module, "_validate_task_repositories", lambda specs: None)
    monkeypatch.setattr(supervisor_module, "_Runtime", _RuntimeProbe)
    _RuntimeProbe.sizes.clear()

    default_session = tmp_path / "default"
    asyncio.run(run_plan(default_session, _plan(default_session)))

    positive_session = tmp_path / "positive"
    asyncio.run(
        run_plan(
            positive_session,
            _plan(positive_session),
            warm_pool_size=2,
        )
    )

    assert _RuntimeProbe.sizes == [0, 2]


def test_positive_pool_bound_rebinds_and_zero_bound_does_not() -> None:
    command = ["python", "-m", "cambium.worker"]
    environment = {"CAMBIUM_TEST": "same"}

    enabled = object.__new__(supervisor_module._Runtime)
    enabled._warm_pool_size = 1
    enabled._pool = []
    process = _FakeProcess()
    asyncio.run(enabled._pool_return(cast(Any, process), command, environment))

    assert enabled._pool_pop(command, environment) is process

    disabled = object.__new__(supervisor_module._Runtime)
    disabled._warm_pool_size = 0
    disabled._pool = []
    killed: list[_FakeProcess] = []

    async def record_kill(proc: Process) -> None:
        killed.append(cast(_FakeProcess, proc))

    cast(Any, disabled)._kill_pooled = record_kill
    process = _FakeProcess()
    asyncio.run(disabled._pool_return(cast(Any, process), command, environment))

    assert killed == [process]
    assert disabled._pool == []
    assert disabled._pool_pop(command, environment) is None


@pytest.mark.parametrize("env_value", ["0", "2", "-1", "not-an-integer"])
def test_warm_pool_environment_is_ignored_by_current_entry_points(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_value: str
) -> None:
    monkeypatch.setenv("CAMBIUM_WARM_POOL_SIZE", env_value)

    # Unified ``cambium supervisor`` does not consult the environment or add a
    # default-valued option to the delegated argv.
    from cambium import supervisor

    supervisor_main = supervisor.main
    delegated: list[list[str]] = []
    monkeypatch.setattr(
        supervisor,
        "main",
        lambda argv=None: cast(Any, delegated.append(list(argv or []))) or 0,
    )
    session = tmp_path / "cli"
    assert cli.main(["supervisor", "--session-dir", str(session), "--demo"]) == 0
    assert delegated == [["--session-dir", str(session), "--demo"]]

    # The module entry point also keeps its parser default, even for an invalid
    # environment value that the removed helper would have rejected.
    spec_path = tmp_path / "task.json"
    module_session = tmp_path / "module"
    spec_path.write_text(
        json.dumps(
            {
                "task_id": "module-probe",
                "task": "probe the module entry point",
                "repo": str(tmp_path / "repo"),
                "worktree_path": str(module_session / "worktree"),
                "branch": "module-probe",
            }
        ),
        encoding="utf-8",
    )
    module_sizes: list[int] = []

    async def fake_amain(*args: Any, **kwargs: Any) -> int:
        del args
        module_sizes.append(kwargs["warm_pool_size"])
        return 0

    monkeypatch.setattr(supervisor, "_validate_task_repositories", lambda specs: None)
    monkeypatch.setattr(supervisor, "_amain_plan", fake_amain)
    assert (
        supervisor_main(
            [
                "--session-dir",
                str(module_session),
                "--task-spec",
                str(spec_path),
            ]
        )
        == 0
    )
    assert module_sizes == [0]

    # Direct callers use the same literal default and do not read the env.
    monkeypatch.setattr(supervisor_module, "_Runtime", _RuntimeProbe)
    _RuntimeProbe.sizes.clear()
    run_session = tmp_path / "run-plan"
    asyncio.run(run_plan(run_session, _plan(run_session)))
    assert _RuntimeProbe.sizes == [0]
