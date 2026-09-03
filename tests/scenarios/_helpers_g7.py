"""Shared subprocess fixtures for group 7's supervisor scenarios."""

from pathlib import Path
from textwrap import dedent

_SUPERVISOR_WORKER = dedent(
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    MODE = __MODE__

    def send(message):
        sys.stdout.write(json.dumps(message) + "\\n")
        sys.stdout.flush()

    init = json.loads(sys.stdin.readline())
    generation = init.get("generation", 1)
    send({
        "type": "ready",
        "request_id": (
            init["request_id"]
            if MODE != "wrong-ready" or generation > 1
            else "wrong-request-id"
        ),
        "task_id": init["task_id"],
        "pid": 0,
        "generation": generation,
        "proto": 1,
    })
    run = json.loads(sys.stdin.readline())
    worktree = Path(run["worktree_path"])
    target = worktree / run["target_file"]
    target.write_text(target.read_text().rstrip("\\n") + "\\n" + run["marker"] + "\\n")
    git_add = [run["target_file"]]
    if MODE == "dirty":
        (worktree / ".gitignore").write_text("leftover.txt\\n")
        git_add.append(".gitignore")
    subprocess.run(["git", "add", *git_add], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", MODE], cwd=worktree, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree, check=True, capture_output=True, text=True
    ).stdout.strip()
    if MODE == "branch-lock":
        lock = Path(run["repo"]) / ".git" / "refs" / "heads" / f"{run['branch']}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("concurrent ref lock\\n")
    elif MODE == "dirty":
        (worktree / "leftover.txt").write_text("dirty content\\n")
    result = {
        "type": "result_envelope",
        "request_id": run["request_id"],
        "task_id": init["task_id"],
        "status": "succeeded",
    }
    if MODE != "wrong-ready":
        result["commits"] = [commit]
    send(result)
    send({
        "type": "exit_message",
        "task_id": init["task_id"],
        "generation": generation,
        "reason": "done",
    })
    """
)


def write_supervisor_worker(path: Path, mode: str) -> None:
    path.write_text(_SUPERVISOR_WORKER.replace("__MODE__", repr(mode)), encoding="utf-8")
