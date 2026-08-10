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
import secrets
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

MAIN_REF = "refs/heads/main"
STAGING_REF_PREFIX = "refs/cambium/staging"
DEFAULT_QUARANTINE_MAX_ENTRIES = 15
DEFAULT_QUARANTINE_MAX_BYTES = 1 << 30
DEFAULT_QUARANTINE_RETENTION_NS = 7 * 24 * 60 * 60 * 1_000_000_000
DEFAULT_QUARANTINE_MIN_FREE_BYTES = 1 << 30

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


class StagingCleanupError(RuntimeError):
    """A staging tree could not be removed or quarantined safely."""


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

    def __init__(
        self,
        task_id: str | None = None,
        *,
        session_dir: Path | None = None,
        quarantine_max_entries: int = DEFAULT_QUARANTINE_MAX_ENTRIES,
        quarantine_max_bytes: int = DEFAULT_QUARANTINE_MAX_BYTES,
        quarantine_retention_ns: int = DEFAULT_QUARANTINE_RETENTION_NS,
        quarantine_min_free_bytes: int = DEFAULT_QUARANTINE_MIN_FREE_BYTES,
        durable_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._task_id = task_id
        self._task_key = sha256((task_id or "unknown").encode()).hexdigest()[:16]
        self._session_dir = Path(session_dir).resolve() if session_dir is not None else None
        self._quarantine_max_entries = quarantine_max_entries
        self._quarantine_max_bytes = quarantine_max_bytes
        self._quarantine_retention_ns = quarantine_retention_ns
        self._quarantine_min_free_bytes = quarantine_min_free_bytes
        self._durable_event = durable_event
        self._worktree_path: Path | None = None
        self._staging_branch: str | None = None
        self._staging_ref: str | None = None
        self._events: list[tuple[str, dict[str, Any]]] = []

    def drain_events(self) -> list[tuple[str, dict[str, Any]]]:
        """Return and clear events produced by synchronous git operations."""
        events, self._events = self._events, []
        return events

    @property
    def staging_ref(self) -> str | None:
        return self._staging_ref

    @property
    def staging_branch(self) -> str | None:
        return self._staging_branch

    def _event(self, kind: str, **payload: Any) -> None:
        self._events.append((kind, payload))

    def _secure_directory(self, parent: Path, name: str, root: Path) -> Path:
        candidate = parent / name
        if candidate.is_symlink():
            raise StagingCleanupError("quarantine path contains a symlink")
        candidate.mkdir(mode=0o700, exist_ok=True)
        if candidate.is_symlink():
            raise StagingCleanupError("quarantine path contains a symlink")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise StagingCleanupError("quarantine path escapes the session directory")
        os.chmod(resolved, 0o700)
        return resolved

    def _quarantine_root(self) -> Path:
        if self._session_dir is None:
            raise StagingCleanupError("dirty staging requires a session quarantine directory")
        session_root = self._session_dir.resolve(strict=True)
        cambium = self._secure_directory(session_root, ".cambium", session_root)
        quarantine = self._secure_directory(cambium, "quarantine", session_root)
        return self._secure_directory(quarantine, "merge", session_root)

    @staticmethod
    def _open_directory(parent_fd: int, name: str) -> int:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise StagingCleanupError("quarantine path contains a symlink") from exc
        os.fchmod(fd, 0o700)
        return fd

    @staticmethod
    def _is_open_child(parent_fd: int, name: str, child_fd: int) -> bool:
        try:
            linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        opened = os.fstat(child_fd)
        return (
            stat.S_ISDIR(linked.st_mode)
            and not stat.S_ISLNK(linked.st_mode)
            and (linked.st_dev, linked.st_ino) == (opened.st_dev, opened.st_ino)
        )

    @staticmethod
    def _allocated_bytes(path: Path) -> int:
        """Allocated bytes below path, without following symlinks."""
        total = 0
        pending = [path]
        while pending:
            current = pending.pop()
            info = current.lstat()
            total += info.st_blocks * 512
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                continue
            with os.scandir(current) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        return total

    def _artifact_bytes(self, repo: Path, path: Path) -> int:
        total = self._allocated_bytes(path)
        git_dir = self._run(path, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
        admin = Path(git_dir)
        if admin.exists() and not admin.is_relative_to(path):
            total += self._allocated_bytes(admin)
        return total

    def _in_progress(self, worktree_path: Path) -> bool:
        markers = (
            "rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD",
            "REVERT_HEAD", "BISECT_LOG", "sequencer",
        )
        for marker in markers:
            result = self._run(worktree_path, "rev-parse", "--git-path", marker, check=False)
            if result.returncode == 0 and Path(result.stdout.strip()).exists():
                return True
        return False

    def _dirty_reasons(self, worktree_path: Path) -> list[str]:
        status_result = self._run(
            worktree_path, "status", "--porcelain=v1", "--untracked-files=all",
            "--ignored=matching", "-z", check=False,
        )
        if status_result.returncode != 0:
            raise StagingCleanupError("cannot inspect staging worktree state")
        reasons: set[str] = set()
        for record in status_result.stdout.split("\0"):
            if len(record) < 2:
                continue
            pair = record[:2]
            if pair == "??":
                reasons.add("untracked")
            elif pair == "!!":
                reasons.add("ignored")
            else:
                if pair[0] != " ":
                    reasons.add("indexed")
                if pair[1] != " ":
                    reasons.add("tracked")
        if self._in_progress(worktree_path):
            reasons.add("in-progress")
        return sorted(reasons)

    @staticmethod
    def _quarantine_entries(root: Path) -> list[Path]:
        if not root.exists():
            return []
        entries: list[Path] = []
        for task_dir in root.glob("task-[0-9a-f]" + "[0-9a-f]" * 15):
            if not task_dir.is_dir() or task_dir.is_symlink():
                continue
            entries.extend(
                entry for entry in task_dir.iterdir()
                if entry.is_dir() and not entry.is_symlink()
            )
        return entries

    def _registered_paths(self, repo: Path) -> set[Path]:
        result = self._run_repo(repo, "worktree", "list", "--porcelain")
        return {
            Path(line[9:]).resolve()
            for line in result.stdout.splitlines()
            if line.startswith("worktree ")
        }

    def _owning_repo(self, entry: Path) -> Path:
        common_dir = Path(
            self._run(
                entry, "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        ).resolve()
        if not common_dir.is_absolute() or not common_dir.is_dir():
            raise StagingCleanupError("quarantine worktree owner is not a repository")
        if entry.resolve() not in self._registered_paths(common_dir):
            raise StagingCleanupError("quarantine worktree is not registered in its repository")
        return common_dir

    def _delete_quarantine_entry(self, entry: Path) -> None:
        common_dir = self._owning_repo(entry)
        branch = self._run(
            entry, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        ).stdout.strip()
        result = self._run_repo(
            common_dir, "worktree", "remove", "--force", str(entry), check=False
        )
        if result.returncode != 0:
            raise StagingCleanupError("cannot prune registered quarantine worktree")
        if branch.startswith("cambium-merge/"):
            suffix = branch.removeprefix("cambium-merge/")
            self._run_repo(common_dir, "branch", "-D", branch, check=False)
            self._run_repo(
                common_dir, "update-ref", "-d", f"{STAGING_REF_PREFIX}/{suffix}",
                check=False,
            )

    def _prune_quarantine(
        self,
        repo: Path,
        *,
        newest: Path | None = None,
        newest_allocated_bytes: int | None = None,
        root: Path | None = None,
    ) -> None:
        root = self._quarantine_root() if root is None else root
        entries = self._quarantine_entries(root)
        if not entries:
            return
        self._event("merge_staging_prune_started", entries=len(entries))
        now = time.time_ns()

        def details(entry: Path) -> tuple[int, int]:
            return entry.stat().st_mtime_ns, self._artifact_bytes(repo, entry)

        measured = {entry: details(entry) for entry in entries}
        newest_entry = newest
        if newest is not None and newest not in measured:
            newest_stat = newest.stat()
            newest_entry = next(
                (entry for entry in entries if os.path.samestat(entry.stat(), newest_stat)), None
            )
        if newest_entry is not None and newest_allocated_bytes is not None:
            measured[newest_entry] = (
                measured[newest_entry][0], newest_allocated_bytes
            )
        newest_bytes = measured.get(newest_entry, (0, 0))[1]
        if newest_entry is not None and newest_bytes > self._quarantine_max_bytes:
            raise StagingCleanupError("newest quarantine artifact exceeds the byte cap")

        expired = sorted(
            (
                entry
                for entry in entries
                if now - measured[entry][0] >= self._quarantine_retention_ns
            ),
            key=lambda entry: measured[entry][0],
        )
        oldest = sorted(
            (entry for entry in entries if entry not in expired),
            key=lambda entry: measured[entry][0],
        )
        removed = 0
        removed_bytes = 0
        for entry in [*expired, *oldest]:
            current_entries = [item for item in entries if item.exists()]
            aggregate = sum(measured[item][1] for item in current_entries)
            free = shutil.disk_usage(root).free
            is_expired = entry in expired
            over = (
                len(current_entries) > self._quarantine_max_entries
                or aggregate > self._quarantine_max_bytes
                or free < self._quarantine_min_free_bytes
            )
            if not is_expired and not over:
                break
            if newest_entry is not None and entry == newest_entry:
                continue
            size = measured[entry][1]
            self._delete_quarantine_entry(entry)
            removed += 1
            removed_bytes += size
        remaining = [item for item in entries if item.exists()]
        remaining_bytes = sum(measured[item][1] for item in remaining)
        if removed:
            self._event(
                "merge_staging_pruned", entries=removed, allocated_bytes=removed_bytes
            )
        if (
            len(remaining) > self._quarantine_max_entries
            or remaining_bytes > self._quarantine_max_bytes
            or shutil.disk_usage(root).free < self._quarantine_min_free_bytes
        ):
            raise StagingCleanupError("quarantine bounds cannot be satisfied")

    def _quarantine_staging(
        self, repo: Path, worktree_path: Path, reasons: list[str]
    ) -> Path:
        root = self._quarantine_root()
        task_name = f"task-{self._task_key}"
        destination_name = f"{time.time_ns()}-{secrets.token_hex(8)}"
        staging_sha = self._rev_parse(worktree_path, "HEAD")
        source_parent_fd = session_fd = cambium_fd = quarantine_fd = root_fd = task_fd = -1
        destination_fd = -1
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            source_parent_fd = os.open(worktree_path.parent, flags)
            session_fd = os.open(self._session_dir, flags)
            cambium_fd = self._open_directory(session_fd, ".cambium")
            quarantine_fd = self._open_directory(cambium_fd, "quarantine")
            root_fd = self._open_directory(quarantine_fd, "merge")
            task_fd = self._open_directory(root_fd, task_name)
            chain = (
                (session_fd, ".cambium", cambium_fd),
                (cambium_fd, "quarantine", quarantine_fd),
                (quarantine_fd, "merge", root_fd),
                (root_fd, task_name, task_fd),
            )
            if not all(self._is_open_child(*link) for link in chain):
                raise StagingCleanupError("quarantine path changed before worktree move")
            anchored_destination = Path(
                f"/proc/{os.getpid()}/fd/{task_fd}/{destination_name}"
            )
            anchored_root = Path(f"/proc/{os.getpid()}/fd/{root_fd}")

            def restore_staging() -> None:
                try:
                    os.rename(
                        destination_name,
                        worktree_path.name,
                        src_dir_fd=task_fd,
                        dst_dir_fd=source_parent_fd,
                    )
                except OSError as exc:
                    raise StagingCleanupError(
                        "quarantine containment failed and staging could not be restored"
                    ) from exc
                repaired = self._run_repo(
                    repo, "worktree", "repair", str(worktree_path), check=False
                )
                if repaired.returncode != 0:
                    raise StagingCleanupError(
                        "quarantine containment failed and staging could not be restored"
                    )

            result = self._run_repo(
                repo, "worktree", "move", str(worktree_path), str(anchored_destination),
                check=False,
            )
            if result.returncode == 0 and not all(
                self._is_open_child(*link) for link in chain
            ):
                restore_staging()
                raise StagingCleanupError("quarantine path changed during worktree move")
            if result.returncode == 0:
                destination_fd = os.open(destination_name, flags, dir_fd=task_fd)
                opened_destination = Path(f"/proc/{os.getpid()}/fd/{destination_fd}")
                allocated = self._artifact_bytes(repo, opened_destination)
                if not all(self._is_open_child(*link) for link in chain):
                    restore_staging()
                    raise StagingCleanupError("quarantine path changed during worktree move")
            if result.returncode != 0:
                self._event(
                    "merge_staging_cleanup_failed", task=self._task_id,
                    staging_sha=staging_sha, reason="worktree-move-failed",
                )
                raise StagingCleanupError("cannot move dirty staging worktree to quarantine")

            destination = root / task_name / destination_name
            relative_id = Path("merge") / task_name / destination_name
            recording_event_index = len(self._events)
            durably_recorded = False

            def containment_failure(message: str, cause: Exception | None = None) -> None:
                if durably_recorded:
                    self._worktree_path = None
                    self._staging_branch = None
                    self._staging_ref = None
                    error = StagingCleanupError(message)
                    if cause is not None:
                        raise error from cause
                    raise error
                restore_staging()
                del self._events[recording_event_index:]
                error = StagingCleanupError(message)
                if cause is not None:
                    raise error from cause
                raise error

            try:
                destination_info = os.fstat(destination_fd)
                quarantine_payload = {
                    "task": self._task_id,
                    "staging_sha": staging_sha,
                    "quarantine_id": relative_id.as_posix(),
                    "allocated_bytes": allocated,
                    "reason": ",".join(reasons),
                    "expiry": time.time_ns() + self._quarantine_retention_ns,
                    "quarantine_device": destination_info.st_dev,
                    "quarantine_inode": destination_info.st_ino,
                }
                self._event("merge_staging_quarantined", **quarantine_payload)
                if self._durable_event is not None:
                    self._durable_event(
                        "merge_staging_quarantined", dict(quarantine_payload)
                    )
                    durably_recorded = True
            except Exception as exc:
                if not all(self._is_open_child(*link) for link in chain):
                    containment_failure("quarantine path changed during recording", exc)
                containment_failure("cannot durably record quarantined staging", exc)
            if not all(self._is_open_child(*link) for link in chain):
                containment_failure("quarantine path changed before recording completed")
            try:
                self._prune_quarantine(
                    repo,
                    newest=anchored_destination,
                    newest_allocated_bytes=allocated,
                    root=anchored_root,
                )
            except Exception as exc:
                if not all(self._is_open_child(*link) for link in chain):
                    containment_failure("quarantine path changed during recording", exc)
                if isinstance(exc, StagingCleanupError):
                    self._worktree_path = None
                    self._staging_branch = None
                    self._staging_ref = None
                raise
            if not all(self._is_open_child(*link) for link in chain):
                containment_failure("quarantine path changed during recording")
            self._worktree_path = None
            self._staging_branch = None
            self._staging_ref = None
            return destination
        finally:
            for fd in (
                destination_fd, task_fd, root_fd, quarantine_fd, cambium_fd, session_fd,
                source_parent_fd,
            ):
                if fd >= 0:
                    os.close(fd)

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
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=self._git_env() if env is None else env,
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
    def _rebase_env() -> dict[str, str]:
        env = MergeSequencer._git_env()
        env["GIT_EDITOR"] = "true"
        env["GIT_SEQUENCE_EDITOR"] = "true"
        return env

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

        ident = f"{self._task_key}-{secrets.token_hex(6)}"
        staging_ref = f"{STAGING_REF_PREFIX}/{ident}"
        staging_branch = f"cambium-merge/{ident}"

        base_tip = self._rev_parse(repo, base)
        worker_tip = self._ensure_worker_tip(repo, branch)

        if self._is_registered_worktree(repo, worktree_path):
            reasons = self._dirty_reasons(worktree_path)
            if reasons:
                self._quarantine_staging(repo, worktree_path, reasons)
            else:
                self._remove_clean_staging(repo, worktree_path)
                self._drop_staging_refs(repo)
            self._worktree_path = None
            self._staging_branch = None
            self._staging_ref = None

        if not self._is_registered_worktree(repo, worktree_path):
            # Fresh throwaway worktree on a new staging branch at the worker tip.
            # The worker branch itself stays checked out in the worker's own
            # worktree (finding F18: it cannot be rebased in place).
            self._run_repo(repo, "update-ref", staging_ref, worker_tip, check=True)
            try:
                self._run_repo(
                    repo, "worktree", "add", "-B", staging_branch, str(worktree_path),
                    worker_tip, check=True,
                )
            except Exception:
                self._run_repo(repo, "update-ref", "-d", staging_ref, check=False)
                raise

        self._worktree_path = worktree_path
        self._staging_branch = staging_branch
        self._staging_ref = staging_ref

        rebase = self._run(
            worktree_path, "rebase", base_tip, check=False, env=self._rebase_env()
        )
        if rebase.returncode != 0:
            conflicts = self._conflicted_paths(worktree_path, rebase.stdout + rebase.stderr)
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

    def ensure_staging_clean(self, repo: Path) -> None:
        """Fail closed before publish if staging gained uncommitted state."""
        if self._worktree_path is None:
            raise StagingCleanupError("no staging worktree is prepared")
        reasons = self._dirty_reasons(self._worktree_path)
        if not reasons:
            return
        self._quarantine_staging(Path(repo), self._worktree_path, reasons)
        raise StagingCleanupError("dirty staging was quarantined before publish")

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

    def _restore_recorded_quarantines(self, events: list[dict[str, Any]]) -> None:
        if self._session_dir is None:
            return
        root = self._quarantine_root()
        identity_pattern = re.compile(
            r"merge/task-[0-9a-f]{16}/[0-9]+-[0-9a-f]{16}"
        )
        for payload in events:
            quarantine_id = payload.get("quarantine_id")
            device = payload.get("quarantine_device")
            inode = payload.get("quarantine_inode")
            if (
                not isinstance(quarantine_id, str)
                or identity_pattern.fullmatch(quarantine_id) is None
                or not isinstance(device, int)
                or not isinstance(inode, int)
            ):
                continue
            expected = self._session_dir / ".cambium" / "quarantine" / quarantine_id
            if expected.is_symlink():
                raise StagingCleanupError("recorded quarantine path is a symlink")
            if expected.exists():
                info = expected.stat()
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                    device,
                    inode,
                ):
                    raise StagingCleanupError("recorded quarantine path has changed identity")
                continue

            displaced: Path | None = None
            for current, directories, _files in os.walk(
                self._session_dir, topdown=True, followlinks=False
            ):
                safe_directories: list[str] = []
                for name in directories:
                    candidate = Path(current) / name
                    info = candidate.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        continue
                    if (info.st_dev, info.st_ino) == (device, inode):
                        displaced = candidate
                        break
                    safe_directories.append(name)
                directories[:] = safe_directories
                if displaced is not None:
                    break
            if displaced is None:
                raise StagingCleanupError("recorded quarantine evidence cannot be located")

            task_name = Path(quarantine_id).parts[1]
            task_dir = self._secure_directory(root, task_name, root)
            if expected.parent != task_dir:
                raise StagingCleanupError("recorded quarantine path escapes its task directory")
            displaced.rename(expected)
            restored = expected.stat()
            if (restored.st_dev, restored.st_ino) != (device, inode):
                raise StagingCleanupError("restored quarantine path has changed identity")

    def reconcile(
        self,
        repo: Path,
        worktree_path: Path | None = None,
        *,
        scan_quarantine: bool = True,
        quarantine_events: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Return the current ``refs/heads/main`` SHA, or None if absent.

        Recovery hook: the caller compares the returned SHA to its last durable
        ``merge_committed`` event and appends a ``merge_reconciled`` event when
        the ref advanced without one (architecture §7.8 crash gap).
        """
        repo = Path(repo)
        reconciled_tip: str | None = None
        reconciled_ref: str | None = None
        if self._session_dir is not None and scan_quarantine:
            self._restore_recorded_quarantines(quarantine_events or [])
            root = self._quarantine_root()
            for entry in self._quarantine_entries(root):
                relative_id = entry.relative_to(self._session_dir / ".cambium" / "quarantine")
                task_key = entry.parent.name.removeprefix("task-")
                self._event(
                    "merge_staging_quarantined",
                    task=self._task_id if task_key == self._task_key else None,
                    staging_sha=self._rev_parse(entry, "HEAD"),
                    quarantine_id=relative_id.as_posix(),
                    allocated_bytes=self._artifact_bytes(repo, entry),
                    reason="startup-reconciled",
                    expiry=entry.stat().st_mtime_ns + self._quarantine_retention_ns,
                )
            self._prune_quarantine(repo)
        try:
            current = self._rev_parse(repo, MAIN_REF)
        except GitError:
            return None
        if worktree_path is not None and self._is_registered_worktree(repo, worktree_path):
            worktree_path = Path(worktree_path).resolve()
            self._worktree_path = worktree_path
            branch = self._run(
                worktree_path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
            ).stdout.strip()
            self._staging_branch = branch or None
            suffix = branch.removeprefix("cambium-merge/") if branch else ""
            self._staging_ref = f"{STAGING_REF_PREFIX}/{suffix}" if suffix else None
            reasons = self._dirty_reasons(worktree_path)
            if reasons:
                staging_ref = self._staging_ref
                staging_tip = (
                    self._rev_parse(repo, staging_ref) if staging_ref else None
                )
                if staging_tip == current:
                    if self._durable_event is None:
                        raise StagingCleanupError(
                            "dirty recovery requires durable terminal event persistence"
                        )
                    self._durable_event(
                        "merge_committed",
                        {
                            "task": self._task_id,
                            "new": current,
                            "repo": str(repo),
                            "staging_ref": staging_ref,
                            "reason": "recovered-ref-advance",
                        },
                    )
                self._quarantine_staging(repo, worktree_path, reasons)
                if staging_tip == current:
                    self._event(
                        "merge_reconciled", task=self._task_id, new=current,
                        repo=str(repo), staging_ref=staging_ref,
                        reason="ref-advanced-before-event",
                    )
            else:
                if self._staging_ref is not None:
                    reconciled_ref = self._staging_ref
                    reconciled_tip = self._rev_parse(repo, reconciled_ref)
        if reconciled_tip == current:
            self._event(
                "merge_committed", task=self._task_id, new=current, repo=str(repo),
                staging_ref=reconciled_ref, reason="recovered-ref-advance",
            )
            self._event(
                "merge_reconciled", task=self._task_id, new=current, repo=str(repo),
                staging_ref=reconciled_ref, reason="ref-advanced-before-event",
            )
        elif self._worktree_path is not None:
            self.cleanup_staging(repo)
        return current

    def _remove_clean_staging(self, repo: Path, worktree_path: Path) -> None:
        if self._dirty_reasons(worktree_path):
            raise StagingCleanupError("refusing to remove dirty staging worktree")
        removed = self._run_repo(repo, "worktree", "remove", str(worktree_path), check=False)
        if removed.returncode != 0:
            raise StagingCleanupError("cannot remove clean staging worktree")

    def _drop_staging_refs(self, repo: Path) -> None:
        failures: list[str] = []
        if self._staging_branch is not None:
            result = self._run_repo(repo, "branch", "-D", self._staging_branch, check=False)
            if result.returncode != 0:
                failures.append("branch")
        if self._staging_ref is not None:
            result = self._run_repo(repo, "update-ref", "-d", self._staging_ref, check=False)
            if result.returncode != 0:
                failures.append("ref")
        if failures:
            raise StagingCleanupError("cannot remove staging " + " and ".join(failures))

    def cleanup_staging(self, repo: Path) -> None:
        """Remove this instance's throwaway worktree, staging branch, and staging ref.

        Clean trees are removed. Dirty trees are moved intact to the bounded
        session quarantine. No reset, clean, checkout, or abort runs first.
        """
        repo = Path(repo)
        worktree_path = self._worktree_path
        if worktree_path is None:
            return

        try:
            if self._is_registered_worktree(repo, worktree_path):
                reasons = self._dirty_reasons(worktree_path)
                if reasons:
                    self._quarantine_staging(repo, worktree_path, reasons)
                    return
                self._remove_clean_staging(repo, worktree_path)
            self._drop_staging_refs(repo)
        except Exception as exc:
            if not any(kind == "merge_staging_cleanup_failed" for kind, _ in self._events):
                staging_sha = "unknown"
                if worktree_path.exists():
                    try:
                        staging_sha = self._rev_parse(worktree_path, "HEAD")
                    except GitError:
                        pass
                self._event(
                    "merge_staging_cleanup_failed", task=self._task_id,
                    staging_sha=staging_sha, reason=exc.__class__.__name__,
                )
            raise
        else:
            self._worktree_path = None
            self._staging_branch = None
            self._staging_ref = None
