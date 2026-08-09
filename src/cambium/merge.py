"""Unio — the serialized merge sequencer (atomic publish to ``refs/heads/main``).

Implements the architecture §7.8 merge terminal step: stage a worker branch
(rebase onto ``base`` inside a throwaway worktree), publish atomically with
``git update-ref`` under the expected-old-SHA single-writer invariant, close
the ref-advance/event crash gap on recovery, and clean up.

Empirical findings from ``docs/research/worktree-concurrency.md`` are baked
into the ordering:

- The staging SHA is captured into ``refs/cambium/staging/<id>`` BEFORE any
  worktree removal (Experiment 4: detached/rewritten commits become dangling
  once the worktree admin dir and its reflog are gone).
- ``GIT_QUARANTINE_PATH`` is stripped from every git subprocess env and a
  publish call made from inside a quarantine environment is refused loudly
  (Experiment 2e: ``update-ref`` fails under quarantine).
- The worker branch is never rebased in place; its tip is copied to a local
  staging branch inside the throwaway worktree first (Experiment 6a: rebasing
  a branch checked out in another worktree fails with "already used by
  worktree").
- Publish is ref-only: it never touches a working tree or index, so leftover
  staged state (Experiment 1b poison) does not block it.

All git calls are ``subprocess.run`` with list-form args (no shell=True),
``cwd`` set to the repo or the throwaway worktree, output captured.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

MAIN_REF = "refs/heads/main"
STAGING_REF_PREFIX = "refs/cambium/staging"

# Index status pairs that porcelain v1 reports for unmerged (conflicted) paths.
_UNMERGED_PAIRS = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
_QUARANTINE_ENV = "GIT_QUARANTINE_PATH"


class NonFastForwardError(RuntimeError):
    """The expected-old SHA no longer matches ``refs/heads/main``.

    ``git update-ref refs/heads/main <new> <old>`` is the atomic primitive;
    when it refuses, the ref moved between the caller's read of ``expected_old``
    and the publish. The orchestrator treats this as "main advanced; re-merge
    against the new main."
    """

    def __init__(
        self,
        *,
        new_tip: str,
        expected_old: str,
        current: str | None = None,
        detail: str = "",
    ) -> None:
        self.new_tip = new_tip
        self.expected_old = expected_old
        self.current = current
        self.detail = detail
        where = current or "unknown"
        message = (
            f"non-fast-forward publish of {new_tip}: refs/heads/main is at "
            f"{where} but expected {expected_old}"
        )
        if detail:
            message += f" ({detail})"
        super().__init__(message)


class MergeConflictError(RuntimeError):
    """A rebase/merge of the worker branch onto ``base`` hit conflicts."""

    def __init__(self, message: str, conflicts: list[str]) -> None:
        super().__init__(message)
        self.conflicts = list(conflicts)


class QuarantineError(RuntimeError):
    """Publish was attempted from inside a git quarantine environment.

    ``git update-ref`` refuses to run with ``GIT_QUARANTINE_PATH`` set
    ("ref updates forbidden inside quarantine environment"). The caller must
    unset it before handing control to the merge sequencer.
    """


class GitError(RuntimeError):
    """A git invocation failed. Carries the command, exit code, and output."""

    def __init__(self, cwd: Path, args: list[str], result: subprocess.CompletedProcess[str]) -> None:
        self.cwd = cwd
        self.args = args
        self.returncode = result.returncode
        self.stdout = result.stdout
        self.stderr = result.stderr
        message = f"git {args[0]} failed (rc={result.returncode}) in {cwd}"
        if result.stderr:
            message += f": {result.stderr.strip()[:512]}"
        super().__init__(message)


def _parse_conflicts(rebase_output: str) -> list[str]:
    """Best-effort conflict path extraction from ``git rebase`` output.

    Content conflicts are reported as ``CONFLICT (content): Merge conflict in
    <path>``; modify/delete conflicts carry the surviving path at the end of
    ``Version <src> of <path> left in tree.`` Deduplicated, order preserved.
    """
    paths: list[str] = []
    for match in re.finditer(r"CONFLICT \([^)]*\): Merge conflict in (\S+)", rebase_output):
        paths.append(match.group(1))
    for match in re.finditer(r"Version \S+ of (\S+) left in tree", rebase_output):
        paths.append(match.group(1))
    seen: set[str] = set()
    return [p for p in paths if not (p in seen or seen.add(p))]


class MergeSequencer:
    """Per-session merge sequencer. Holds no global or cross-session state.

    The only instance state is the throwaway-worktree bookkeeping of the most
    recent ``prepare_staging`` call on this instance, so ``cleanup_staging``
    can later remove exactly what this instance created.
    """

    def __init__(self, task_id: str | None = None) -> None:
        self._task_id = task_id
        self._worktree_path: Path | None = None
        self._staging_branch: str | None = None
        self._staging_ref: str | None = None

    # -- git plumbing -------------------------------------------------------

    @staticmethod
    def _git_env() -> dict[str, str]:
        """Environment for git subprocesses, quarantine-free (finding F5)."""
        return {key: value for key, value in os.environ.items() if key != _QUARANTINE_ENV}

    def _run(
        self,
        cwd: str | Path,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            env=self._git_env() if env is None else env,
        )
        if check and result.returncode != 0:
            raise GitError(Path(cwd), list(args), result)
        return result

    def _run_repo(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._run(repo, *args, check=check)

    def _rev_parse(self, cwd: str | Path, rev: str) -> str:
        result = self._run(cwd, "rev-parse", "--verify", f"{rev}^{{commit}}", check=True)
        return result.stdout.strip()

    def _is_registered_worktree(self, repo: Path, worktree_path: Path) -> bool:
        result = self._run_repo(repo, "worktree", "list", "--porcelain")
        wanted = os.path.abspath(worktree_path)
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current = os.path.abspath(line[len("worktree "):].strip())
                if current == wanted:
                    return True
        return False

    def _ensure_worker_tip(self, repo: Path, branch: str) -> str:
        try:
            return self._rev_parse(repo, f"refs/heads/{branch}")
        except GitError:
            # "fetch/ensure branch": a remote may hold it and the local ref is stale.
            self._run_repo(repo, "fetch", "origin", branch, check=False)
            return self._rev_parse(repo, f"refs/heads/{branch}")

    @staticmethod
    def _rebase_env() -> dict[str, str]:
        env = MergeSequencer._git_env()
        env["GIT_EDITOR"] = "true"
        env["GIT_SEQUENCE_EDITOR"] = "true"
        return env

    def _conflicted_paths(
        self, worktree_path: Path, rebase_output: str
    ) -> list[str]:
        """Unmerged paths from porcelain v1, falling back to rebase output."""
        status = self._run(worktree_path, "status", "--porcelain", check=False)
        conflicts: list[str] = []
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                if len(line) < 3:
                    continue
                pair = line[:2]
                if pair not in _UNMERGED_PAIRS:
                    continue
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1].strip()
                if path:
                    conflicts.append(path)
        if conflicts:
            return list(dict.fromkeys(conflicts))
        return _parse_conflicts(rebase_output)

    # -- public contract -----------------------------------------------------

    def prepare_staging(
        self, repo: Path, worktree_path: Path, branch: str, base: str
    ) -> str:
        """Rebase ``branch`` onto ``base`` in the throwaway worktree.

        Returns the staging tip SHA after making it reachable from
        ``refs/cambium/staging/<id>`` — written BEFORE any worktree removal, so
        the rebase-rewritten commits survive the throwaway worktree's death.

        Raises :class:`MergeConflictError` with the conflicted paths if the
        rebase stops on a conflict; the main ref is never touched.
        """
        repo = Path(repo)
        worktree_path = worktree_path.resolve()
        if worktree_path == repo.resolve():
            raise ValueError(f"throwaway worktree must not be the repo itself: {worktree_path}")

        ident = self._task_id or branch
        staging_ref = f"{STAGING_REF_PREFIX}/{ident}"
        staging_branch = f"cambium-merge/{ident}"

        base_tip = self._rev_parse(repo, base)
        worker_tip = self._ensure_worker_tip(repo, branch)

        if self._is_registered_worktree(repo, worktree_path):
            # Reuse our own throwaway worktree: clear any in-progress git state
            # (crash leftovers), then pin it to the worker tip on the staging
            # branch. This is a sequencer-owned worktree; --force discards only
            # its own previous staging state.
            self._run(worktree_path, "rebase", "--abort", check=False)
            self._run(worktree_path, "merge", "--abort", check=False)
            self._run(worktree_path, "cherry-pick", "--abort", check=False)
            self._run(
                worktree_path, "checkout", "--force", "-B", staging_branch, worker_tip, check=True
            )
        else:
            # Fresh throwaway worktree on a new staging branch at the worker tip.
            # The worker branch itself stays checked out in the worker's own
            # worktree (finding F18: it cannot be rebased in place).
            self._run_repo(
                repo, "worktree", "add", "-B", staging_branch, str(worktree_path), worker_tip,
                check=True,
            )

        self._worktree_path = worktree_path
        self._staging_branch = staging_branch
        self._staging_ref = staging_ref

        rebase = self._run(
            worktree_path, "rebase", base_tip, check=False, env=self._rebase_env()
        )
        if rebase.returncode != 0:
            conflicts = self._conflicted_paths(worktree_path, rebase.stdout + rebase.stderr)
            self._run(worktree_path, "rebase", "--abort", check=False)
            raise MergeConflictError(
                f"rebase of {branch} onto {base_tip} failed; "
                f"conflicted paths: {conflicts or '(none detected)'}",
                conflicts,
            )

        staging_tip = self._rev_parse(worktree_path, "HEAD")
        # Capture BEFORE any worktree removal (dangling-commit finding F11).
        self._run_repo(repo, "update-ref", staging_ref, staging_tip, check=True)
        return staging_tip

    def publish_merge(self, repo: Path, new_tip: str, expected_old: str | None) -> None:
        """Atomically fast-forward ``refs/heads/main`` to ``new_tip``.

        Ref-only: never touches a working tree or the index. The expected-old
        argument is the single-writer invariant — a concurrent ``main`` move
        (another sequencer, or a human push) fails loudly instead of being
        silently overwritten.

        Raises:
            ValueError: ``expected_old`` is None (required from the caller).
            QuarantineError: the process runs inside a quarantine environment.
            NonFastForwardError: ``refs/heads/main`` moved away from
                ``expected_old`` (the loud rejection).
        """
        if _QUARANTINE_ENV in os.environ:
            raise QuarantineError(
                f"{_QUARANTINE_ENV} is set; git update-ref forbids ref updates inside a "
                "quarantine environment. The caller must unset it before publishing."
            )
        if expected_old is None:
            raise ValueError(
                "publish_merge requires the expected old SHA of refs/heads/main "
                "(single-writer invariant); read it via reconcile() or git rev-parse."
            )

        result = self._run_repo(
            repo, "update-ref", MAIN_REF, new_tip, expected_old, check=False
        )
        if result.returncode == 0:
            return

        detail = (result.stderr + result.stdout).strip()
        if "ref updates forbidden inside quarantine environment" in detail:
            raise QuarantineError(
                f"git update-ref was refused by a quarantine environment: {detail[:512]}"
            )
        current: str | None = None
        match = re.search(r"is at ([0-9a-f]{40}) but expected", detail)
        if match:
            current = match.group(1)
        if current is not None or "reference already exists" in detail:
            raise NonFastForwardError(
                new_tip=new_tip, expected_old=expected_old, current=current, detail=detail[:512]
            )
        raise RuntimeError(f"git update-ref {MAIN_REF} failed: {detail[:512]}")

    def reconcile(self, repo: Path) -> str | None:
        """Return the current ``refs/heads/main`` SHA, or None if absent.

        Recovery hook: the caller compares the returned SHA to its last durable
        ``merge_committed`` event and appends a ``merge_reconciled`` event when
        the ref advanced without one (architecture §7.8 crash gap).
        """
        try:
            return self._rev_parse(repo, MAIN_REF)
        except GitError:
            return None

    def cleanup_staging(self, repo: Path) -> None:
        """Remove this instance's throwaway worktree, staging branch, and staging ref.

        Only removes the worktree path recorded by this instance's last
        ``prepare_staging``. The throwaway worktree is sequencer-owned, so its
        leftovers are reset/cleaned before removal — but an unknown path is
        never touched, and ``--force`` is never used.
        """
        repo = Path(repo)
        worktree_path = self._worktree_path
        staging_branch = self._staging_branch
        staging_ref = self._staging_ref
        if worktree_path is None:
            return

        removal_error: str | None = None
        if self._is_registered_worktree(repo, worktree_path):
            self._run(worktree_path, "rebase", "--abort", check=False)
            self._run(worktree_path, "merge", "--abort", check=False)
            self._run(worktree_path, "reset", "--hard", "HEAD", check=False)
            self._run(worktree_path, "clean", "-fd", check=False)
            removed = self._run_repo(repo, "worktree", "remove", str(worktree_path), check=False)
            if removed.returncode != 0:
                removal_error = (
                    f"could not remove throwaway worktree {worktree_path}: "
                    f"{(removed.stderr + removed.stdout).strip()[:512]}"
                )

        if staging_branch is not None:
            self._run_repo(repo, "branch", "-D", staging_branch, check=False)
        if staging_ref is not None:
            self._run_repo(repo, "update-ref", "-d", staging_ref, check=False)
        self._worktree_path = None
        self._staging_branch = None
        self._staging_ref = None

        if removal_error is not None:
            raise RuntimeError(removal_error)
