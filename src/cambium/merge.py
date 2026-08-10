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
- The fast-forward is *enforced*: ``new_tip`` must be a descendant of
  ``expected_old`` (``git merge-base --is-ancestor``) before the atomic
  ``update-ref`` runs, so a rewind or sideways publish is refused even when the
  old-value check would pass.
- ``publish_merge`` requires ``refs/heads/main`` to already exist and rejects
  an empty/zero ``expected_old`` — git reads an empty old-value as "the ref
  must not exist" and would otherwise *create* ``main`` as a backdoor. The
  first publish goes through ``create_main`` (empty-old "must not exist"
  primitive, race-safe).

All git calls are ``subprocess.run`` with list-form args (no shell=True),
``cwd`` set to the repo or the throwaway worktree, output captured.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from cambium.process_env import build_subprocess_env

MAIN_REF = "refs/heads/main"
STAGING_REF_PREFIX = "refs/cambium/staging"

# git reads an all-zero old-value (like the empty string) as "the ref must not
# exist" — an empty/zero expected_old would silently CREATE refs/heads/main.
ZERO_SHA = "0" * 40

# Index status pairs that porcelain v1 reports for unmerged (conflicted) paths.
_UNMERGED_PAIRS = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
_QUARANTINE_ENV = "GIT_QUARANTINE_PATH"


class NonFastForwardError(RuntimeError):
    """A publish was rejected: it is not a safe fast-forward of ``refs/heads/main``.

    Raised for every refusal of the publish contract: an empty/zero/missing
    ``expected_old`` (the ``git update-ref`` old-value check that would
    otherwise silently *create* ``main``), ``main`` absent (first publish goes
    through :meth:`create_main`), ``new_tip`` not a descendant of
    ``expected_old``, or the atomic ``update-ref`` refusing because ``main``
    moved between the caller's read of ``expected_old`` and the publish. The
    orchestrator treats the last as "main advanced; re-merge against the new
    main."
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
        display_old = expected_old or "(empty/zero)"
        message = (
            f"non-fast-forward publish of {new_tip}: refs/heads/main is at "
            f"{where} but expected {display_old}"
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
    """A git invocation failed to run. Carries the command and output.

    ``result`` is None when git could not even be spawned (e.g. the ``cwd``
    is a file, not a directory); ``cause`` then explains the failure.
    """

    def __init__(
        self,
        cwd: Path,
        args: list[str],
        result: subprocess.CompletedProcess[str] | None = None,
        cause: str = "",
    ) -> None:
        self.cwd = cwd
        self.args = args
        self.returncode: int | None = result.returncode if result is not None else None
        self.stdout = result.stdout if result is not None else ""
        self.stderr = result.stderr if result is not None else ""
        message = f"git {args[0]} failed (rc={self.returncode}) in {cwd}"
        if result is not None and result.stderr:
            message += f": {result.stderr.strip()[:512]}"
        if cause:
            message += f": {cause[:512]}"
        super().__init__(message)


def _parse_conflicts(rebase_output: str) -> list[str]:
    """Best-effort conflict path extraction from ``git rebase`` output.

    Content conflicts are reported as ``CONFLICT (content): Merge conflict in
    <path>`` — the path is NOT quoted, even when it contains spaces;
    modify/delete conflicts carry the surviving path before ``left in tree.``
    Deduplicated, order preserved.
    """
    paths: list[str] = []
    for line in rebase_output.splitlines():
        stripped = line.strip()
        content = re.search(r"CONFLICT \([^)]*\): Merge conflict in (.+)$", stripped)
        if content:
            paths.append(content.group(1))
            continue
        version = re.search(r"Version \S+ of (.+) left in tree", stripped)
        if version:
            paths.append(version.group(1))
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
    def _git_env(
        cwd: str | Path | None = None,
        overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Strict environment for git and its hooks, quarantine-free."""
        return build_subprocess_env(
            worktree=Path(cwd) if cwd is not None else None,
            overrides=overrides,
        )

    def _run(
        self,
        cwd: str | Path,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        trusted_overrides = {
            key: env[key]
            for key in ("GIT_EDITOR", "GIT_SEQUENCE_EDITOR")
            if env is not None and key in env
        }
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=self._git_env(cwd, trusted_overrides),
                start_new_session=True,
            )
        except OSError as exc:
            # e.g. the repo path is a file, not a directory (NotADirectoryError)
            raise GitError(Path(cwd), list(args), cause=f"{type(exc).__name__}: {exc}") from exc
        if check and result.returncode != 0:
            raise GitError(Path(cwd), list(args), result)
        return result

    def _run_repo(
        self, repo: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
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
        """Resolve the worker branch tip, fetching it from origin if not local.

        The worker's commits may live on a remote branch this repo has not
        fetched yet. ``git fetch origin <branch>`` populates the remote-tracking
        ref ``refs/remotes/origin/<branch>`` (never a local
        ``refs/heads/<branch>``), which is then resolved directly.
        """
        try:
            return self._rev_parse(repo, f"refs/heads/{branch}")
        except GitError:
            pass
        self._run_repo(repo, "fetch", "origin", branch, check=False)
        try:
            return self._rev_parse(repo, f"refs/remotes/origin/{branch}")
        except GitError:
            raise GitError(
                repo,
                ["rev-parse", "--verify", f"refs/heads/{branch}"],
                cause=(
                    f"branch {branch!r} is not a local branch and could not be "
                    "fetched from origin"
                ),
            ) from None

    @staticmethod
    def _rebase_env(cwd: str | Path | None = None) -> dict[str, str]:
        return MergeSequencer._git_env(
            cwd,
            {"GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": "true"},
        )

    def _conflicted_paths(
        self, worktree_path: Path, rebase_output: str
    ) -> list[str]:
        """Unmerged paths from ``--porcelain=v1 -z``, falling back to rebase output.

        ``-z`` emits NUL-separated records with unquoted pathnames, so a path
        containing spaces is reported intact (porcelain v1 without ``-z`` would
        C-quote it as ``UU "my file.txt"``).
        """
        status = self._run(
            worktree_path,
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "-z",
            check=False,
        )
        if status.returncode == 0:
            conflicts = [
                record[3:]
                for record in status.stdout.split("\0")
                if len(record) >= 3 and record[:2] in _UNMERGED_PAIRS and record[3:]
            ]
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
            worktree_path, "rebase", base_tip, check=False,
            env=self._rebase_env(worktree_path),
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

    def _check_quarantine(self) -> None:
        """Refuse ref-mutating calls made from inside a quarantine environment.

        git treats ``GIT_QUARANTINE_PATH`` as "no ref updates" (finding F5);
        the caller must unset it before handing control to the sequencer.
        """
        if _QUARANTINE_ENV in os.environ:
            raise QuarantineError(
                f"{_QUARANTINE_ENV} is set; git update-ref forbids ref updates inside a "
                "quarantine environment. The caller must unset it before publishing."
            )

    def _main_exists(self, repo: Path) -> bool:
        result = self._run_repo(repo, "rev-parse", "--verify", MAIN_REF, check=False)
        return result.returncode == 0

    def _is_ancestor(self, repo: Path, ancestor: str, descendant: str) -> bool:
        result = self._run_repo(
            repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise GitError(repo, ["merge-base", "--is-ancestor", ancestor, descendant], result)

    def create_main(self, repo: Path, tip: str) -> None:
        """Create ``refs/heads/main`` at ``tip`` — the first-publish path.

        The supervisor calls this once before the first ``publish_merge``
        (which requires ``main`` to already exist and rejects an empty/zero
        ``expected_old``). Creation is race-safe: ``git update-ref <ref> <new>
        ""`` uses git's empty-old "must not exist" primitive, so a concurrent
        creation is rejected atomically rather than silently overwritten.

        Raises:
            QuarantineError: the process runs inside a quarantine environment.
            GitError: ``tip`` does not resolve to a commit.
            NonFastForwardError: ``refs/heads/main`` already exists.
        """
        repo = Path(repo)
        self._check_quarantine()
        if self._main_exists(repo):
            current = self._rev_parse(repo, MAIN_REF)
            raise NonFastForwardError(
                new_tip=tip,
                expected_old="(absent)",
                current=current,
                detail=f"{MAIN_REF} already exists; use publish_merge() to advance it",
            )
        tip_commit = self._rev_parse(repo, tip)
        result = self._run_repo(repo, "update-ref", MAIN_REF, tip_commit, "", check=False)
        if result.returncode == 0:
            return
        detail = (result.stderr + result.stdout).strip()
        if "reference already exists" in detail:
            raise NonFastForwardError(
                new_tip=tip_commit,
                expected_old="(absent)",
                detail=f"{MAIN_REF} was created concurrently: {detail[:512]}",
            )
        raise RuntimeError(f"git update-ref {MAIN_REF} failed: {detail[:512]}")

    def publish_merge(self, repo: Path, new_tip: str, expected_old: str | None) -> None:
        """Atomically fast-forward ``refs/heads/main`` to ``new_tip``.

        Ref-only: never touches a working tree or the index. Two invariants are
        enforced before the atomic ``git update-ref refs/heads/main <new> <old>``
        runs:

        - ``expected_old`` is a real commit SHA — an empty/zero value would make
          git *create* ``main`` (empty old-value = "must not exist") and is
          rejected; ``main`` must already exist, so the first publish goes
          through :meth:`create_main`.
        - ``new_tip`` is a verified descendant of ``expected_old``
          (``git merge-base --is-ancestor``), so a rewind or sideways move is
          refused even when the old-value check alone would pass.

        A concurrent ``main`` move (another sequencer, or a human push) fails
        loudly on the expected-old check instead of being silently overwritten.

        Raises:
            QuarantineError: the process runs inside a quarantine environment.
            GitError: the repo path is unusable or a git call fails.
            NonFastForwardError: ``expected_old`` is empty/zero, ``main`` is
                absent, ``new_tip`` is not a descendant of ``expected_old``, or
                ``refs/heads/main`` moved away from ``expected_old``.
        """
        repo = Path(repo)
        self._check_quarantine()
        if expected_old is None or expected_old == "" or expected_old == ZERO_SHA:
            raise NonFastForwardError(
                new_tip=new_tip,
                expected_old=expected_old or "(empty/zero)",
                detail=(
                    "expected_old must be a real commit SHA: git reads an empty/"
                    "zero old-value as 'the ref must not exist' and would CREATE "
                    f"{MAIN_REF}; use create_main() for the first publish"
                ),
            )
        if not self._main_exists(repo):
            raise NonFastForwardError(
                new_tip=new_tip,
                expected_old=expected_old,
                detail=f"{MAIN_REF} does not exist; use create_main() for the first publish",
            )
        if not self._is_ancestor(repo, expected_old, new_tip):
            raise NonFastForwardError(
                new_tip=new_tip,
                expected_old=expected_old,
                detail=f"{new_tip} is not a descendant of {expected_old} (no fast-forward)",
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
        if (
            current is not None
            or "reference already exists" in detail
            or "unable to resolve reference" in detail
        ):
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
