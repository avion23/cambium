"""``cambium doctor`` — harness diagnostics command (architecture.md §13).

A health check modeled on ``codex doctor`` (see ``docs/research/codex.md``).
It exists to surface early the drift failure mode Codex's local install
exhibits: state rows pointing at missing or unusable files. The Cambium
analogue checked here: worktree entries whose directory is gone, and an
event store that fails ``PRAGMA integrity_check``.

Exit status: 0 when no check fails (warnings and skips are allowed), 1 when
any check fails.

Run::

    python -m cambium.doctor [--session-dir <dir>]
"""

from __future__ import annotations

import argparse
import enum
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_PYTHON = (3, 14)
MIN_GIT = (2, 40)
EVENTS_DB_REL = ".cambium/events.db"
DATASET_CHECK = REPO_ROOT / "scripts" / "check_dataset_v1.py"
OMP_MODELS_YML = Path.home() / ".omp" / "agent" / "models.yml"


class Status(enum.StrEnum):
    """Outcome of one diagnostic check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Check:
    """One numbered diagnostic check with its outcome and detail."""

    number: int
    name: str
    status: Status
    detail: str


def check_python() -> tuple[Status, str]:
    version = sys.version_info[:2]
    required = ".".join(map(str, MIN_PYTHON))
    status = Status.PASS if version >= MIN_PYTHON else Status.FAIL
    return status, f"{sys.version.split()[0]} (>= {required})"


def check_uv() -> tuple[Status, str]:
    path = shutil.which("uv")
    if path:
        return Status.PASS, path
    return Status.FAIL, "uv not found on PATH"


def _git_version() -> tuple[int, int] | None:
    try:
        output = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"git version (\d+)\.(\d+)", output)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def check_git() -> tuple[Status, str]:
    if shutil.which("git") is None:
        return Status.FAIL, "git not found on PATH"
    version = _git_version()
    if version is None:
        return Status.FAIL, "could not parse `git --version`"
    found = f"{version[0]}.{version[1]}"
    required = f"{MIN_GIT[0]}.{MIN_GIT[1]}"
    status = Status.PASS if version >= MIN_GIT else Status.FAIL
    return status, f"{found} (>= {required})"


def _git_toplevel(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(result.stdout.strip())


def _parse_worktrees(porcelain: str) -> list[Path]:
    paths: list[Path] = []
    for entry in porcelain.split("\n\n"):
        for line in entry.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree "):]))
    return paths


def check_worktrees(cwd: Path) -> tuple[Status, str]:
    """Flag worktree entries whose directory is missing — the codex-doctor drift class."""
    if _git_toplevel(cwd) is None:
        return Status.SKIP, "not inside a git repository"
    try:
        output = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return Status.FAIL, f"`git worktree list --porcelain` failed: {exc}"
    worktrees = _parse_worktrees(output)
    if not worktrees:
        return Status.PASS, "no linked worktrees"
    missing = [path for path in worktrees if not path.is_dir()]
    if missing:
        shown = ", ".join(str(path) for path in missing[:3])
        return Status.FAIL, (
            f"{len(missing)}/{len(worktrees)} worktree(s) have a missing directory: {shown}"
        )
    return Status.PASS, f"{len(worktrees)} worktree(s), all directories present"


def _event_store(db: Path) -> tuple[int | None, list[str]]:
    """Return (row count, integrity problems). Count is None when integrity failed."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        problems = [line for line in integrity if line != "ok"]
        if problems:
            return None, problems
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return count, []
    finally:
        conn.close()


def check_event_store(session_dir: Path | None) -> tuple[Status, str]:
    if session_dir is None:
        return Status.SKIP, "no --session-dir given"
    db = session_dir / EVENTS_DB_REL
    if not db.is_file():
        return Status.SKIP, f"{db} does not exist"
    try:
        count, problems = _event_store(db)
    except sqlite3.Error as exc:
        return Status.FAIL, f"{db}: {exc}"
    if problems:
        return Status.FAIL, f"{db}: integrity_check: {problems[:3]}"
    return Status.PASS, f"{db}: integrity ok, {count} events"


def check_dataset() -> tuple[Status, str]:
    try:
        result = subprocess.run(
            [sys.executable, str(DATASET_CHECK)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Status.FAIL, f"could not run {DATASET_CHECK.name}: {exc}"
    if result.returncode == 0 and "ALL CHECKS PASSED" in result.stdout:
        return Status.PASS, "ALL CHECKS PASSED"
    tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-3:])
    return Status.FAIL, f"exit {result.returncode}: {tail or 'no output'}"


def _git_tracked(repo: Path, relative: str) -> bool:
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo, capture_output=True, timeout=10,
        )
        if inside.returncode != 0:
            return False
        listed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative],
            cwd=repo, capture_output=True, timeout=10,
        )
    except OSError:
        return False
    return listed.returncode == 0


def check_secrets() -> tuple[Status, str]:
    """WARN (never FAIL) when ~/.omp/agent/models.yml is git-tracked."""
    models = OMP_MODELS_YML
    if not models.is_file():
        return Status.PASS, f"{models} not present"
    if _git_tracked(models.parent, models.name):
        return Status.WARN, (
            f"{models} is git-tracked — plaintext API keys "
            "(provider-landscape.md §6)"
        )
    return Status.PASS, f"{models} present but not git-tracked"


def run_checks(session_dir: Path | None, cwd: Path) -> list[Check]:
    checks = [
        (1, "Python version", check_python()),
        (2, "uv", check_uv()),
        (3, "git", check_git()),
        (4, "Worktree hygiene", check_worktrees(cwd)),
        (5, "Event store integrity", check_event_store(session_dir)),
        (6, "Dataset integrity", check_dataset()),
        (7, "Secrets hygiene", check_secrets()),
    ]
    return [
        Check(number=number, name=name, status=status, detail=detail)
        for number, name, (status, detail) in checks
    ]


def format_report(checks: list[Check]) -> str:
    lines = ["cambium doctor — Cambium harness diagnostics"]
    for check in checks:
        lines.append(
            f"  {check.number:>2}. {check.name:<22} "
            f"{check.status.value.upper():<5} {check.detail}"
        )
    counts = Counter(check.status for check in checks)
    lines.append(
        f"Summary: {counts[Status.PASS]} pass · {counts[Status.WARN]} warn · "
        f"{counts[Status.SKIP]} skip · {counts[Status.FAIL]} fail"
    )
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(check.status == Status.FAIL for check in checks) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cambium doctor",
        description="Harness diagnostics: python/uv/git availability, worktree "
        "hygiene, event-store integrity, dataset integrity, secrets hygiene.",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="session dir whose .cambium/events.db is checked (optional)",
    )
    args = parser.parse_args(argv)
    checks = run_checks(args.session_dir, Path.cwd())
    print(format_report(checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
