"""Unio merge-sequencer scenarios (architecture §7.8, research worktree-concurrency).

Real git, real subprocesses, no mocks. Each test builds a scratch repo with
worker worktrees, stages through the throwaway worktree, and asserts the
single-writer publish invariant. Maps to test-strategy §5 (merge concurrency)
and the six findings the sequencer encodes:

1. happy path: two sequential publishes, main tip advances (ff chain).
2. expected-old mismatch: publish with a stale old SHA is rejected loudly and
   main is unchanged; a poisoned staged index does not block a ref-only publish.
3. GIT_QUARANTINE_PATH: publish is refused with a clear error, main unchanged.
4. rebase conflict: prepare_staging raises MergeConflictError listing the file.
5. dangling prevention: the staging SHA survives worktree removal via the
   staging ref.
6. concurrent publish race: two processes, one expected old → exactly one
   winner per trial (5 trials).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from cambium.merge import (
    MergeConflictError,
    MergeSequencer,
    NonFastForwardError,
    QuarantineError,
)

SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")


# -- helpers ----------------------------------------------------------------


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _rev(cwd: Path, rev: str) -> str:
    return _run(cwd, "rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()


def _init_repo(repo: Path) -> str:
    """Scratch repo on branch main with one base commit. Returns the base SHA."""
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in (("user.name", "merge-test"), ("user.email", "merge@test"),
                       ("gc.auto", "0")):
        _run(repo, "config", key, value)
    (repo / "base.txt").write_text("base\n")
    _run(repo, "add", "base.txt")
    _run(repo, "commit", "-m", "initial")
    return _rev(repo, "HEAD")


def _worker_commit(repo: Path, branch: str, wt: Path, files: dict[str, str], from_: str) -> str:
    """Worker-style worktree on ``branch`` with one commit. Returns the tip SHA."""
    _run(repo, "worktree", "add", "-b", branch, str(wt), from_)
    for name, content in files.items():
        (wt / name).write_text(content)
        _run(wt, "add", name)
    _run(wt, "commit", "-m", f"{branch}: {','.join(files)}")
    return _rev(wt, "HEAD")


def _publish(repo: Path, tip: str, old: str) -> None:
    """Direct ref-only publish — simulates a second writer to refs/heads/main."""
    _run(repo, "update-ref", "refs/heads/main", tip, old)


_CHILD = (
    "import os, sys\n"
    "from pathlib import Path\n"
    "os.environ.pop('GIT_QUARANTINE_PATH', None)\n"
    "from cambium.merge import MergeSequencer\n"
    "repo, tip, old = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "try:\n"
    "    MergeSequencer(task_id='race-%s').publish_merge(Path(repo), tip, old)\n"
    "except Exception as exc:\n"
    "    print(type(exc).__name__, file=sys.stderr)\n"
    "    sys.exit(1)\n"
)


def _publish_child(repo: Path, tip: str, old: str, tag: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [SRC_DIR, env.get("PYTHONPATH")]))
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD % tag, str(repo), tip, old],
        cwd=str(repo),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# -- scenarios --------------------------------------------------------------


def test_happy_path_two_sequential_publishes(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)

    seq1 = MergeSequencer(task_id="seq-1")
    staged_a = seq1.prepare_staging(repo, tmp_path / "staging-1", "wt-a", "main")
    assert staged_a == tip_a  # rebase onto the unchanged base is a no-op

    seq1.publish_merge(repo, staged_a, base)
    assert _rev(repo, "refs/heads/main") == staged_a
    # publish is ref-only: the main working tree was never checked out
    assert not (repo / "a.txt").exists()

    wt_b = tmp_path / "wt-b"
    tip_b = _worker_commit(repo, "wt-b", wt_b, {"b.txt": "b\n"}, staged_a)

    seq2 = MergeSequencer(task_id="seq-2")
    staged_b = seq2.prepare_staging(repo, tmp_path / "staging-2", "wt-b", "main")
    assert staged_b == tip_b

    seq2.publish_merge(repo, staged_b, staged_a)
    assert _rev(repo, "refs/heads/main") == staged_b
    parent = _run(repo, "log", "--format=%P", "-n", "1", staged_b).stdout.strip()
    assert parent == staged_a  # strict ff chain
    assert len(_run(repo, "log", "--oneline", "refs/heads/main").stdout.splitlines()) == 3

    seq1.cleanup_staging(repo)
    seq2.cleanup_staging(repo)


def test_publish_rejects_stale_expected_old(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    wt_m = tmp_path / "wt-m"
    tip_m = _worker_commit(repo, "wt-m", wt_m, {"m.txt": "m\n"}, base)

    seq = MergeSequencer(task_id="nnf-1")
    staged_a = seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")

    # main moves behind the sequencer's back (concurrent writer / human push)
    _publish(repo, tip_m, base)
    assert _rev(repo, "refs/heads/main") == tip_m

    with pytest.raises(NonFastForwardError) as exc:
        seq.publish_merge(repo, staged_a, base)
    assert "but expected" in str(exc.value)

    # the failed publish changed nothing
    assert _rev(repo, "refs/heads/main") == tip_m
    seq.cleanup_staging(repo)


def test_publish_works_through_staged_poison(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    wt_b = tmp_path / "wt-b"
    tip_b = _worker_commit(repo, "wt-b", wt_b, {"b.txt": "b\n"}, base)

    seq = MergeSequencer(task_id="poison")
    staged_a = seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")

    # Experiment 1b poison: a failed ff-only merge applies the loser tree to the
    # index, then the ref check refuses — leaving the tree staged in the shared
    # checkout. read-tree reproduces exactly that "tree applied, ref refused"
    # state without moving the ref.
    _run(repo, "read-tree", tip_b)
    assert _run(repo, "status", "--porcelain").stdout  # the index is poisoned
    poisoned_tree = _run(repo, "write-tree").stdout.strip()

    # publish is update-ref only: the poisoned index cannot block it
    seq.publish_merge(repo, staged_a, base)
    assert _rev(repo, "refs/heads/main") == staged_a
    assert _run(repo, "write-tree").stdout.strip() == poisoned_tree  # index untouched
    seq.cleanup_staging(repo)


def test_publish_rejects_quarantine_env(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)

    seq = MergeSequencer(task_id="quar-1")
    staged_a = seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")

    monkeypatch.setenv("GIT_QUARANTINE_PATH", str(tmp_path / "quar"))
    with pytest.raises(QuarantineError) as exc:
        seq.publish_merge(repo, staged_a, base)
    assert "GIT_QUARANTINE_PATH" in str(exc.value)
    assert _rev(repo, "refs/heads/main") == base  # main unchanged

    monkeypatch.delenv("GIT_QUARANTINE_PATH")
    seq.publish_merge(repo, staged_a, base)  # guard is env-scoped
    assert _rev(repo, "refs/heads/main") == staged_a
    seq.cleanup_staging(repo)


def test_staging_conflict_lists_file_and_keeps_main(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    wt_a = tmp_path / "wt-a"
    _worker_commit(repo, "wt-a", wt_a, {"base.txt": "base\nfrom-a\n"}, base)
    wt_m = tmp_path / "wt-m"
    _worker_commit(repo, "wt-m", wt_m, {"base.txt": "base\nfrom-m\n"}, base)
    _publish(repo, _rev(repo, "refs/heads/wt-m"), base)

    seq = MergeSequencer(task_id="conf-1")
    with pytest.raises(MergeConflictError) as exc:
        seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")
    assert "base.txt" in exc.value.conflicts
    assert _rev(repo, "refs/heads/main") == _rev(repo, "refs/heads/wt-m")  # untouched
    seq.cleanup_staging(repo)  # conflict leaves no poison behind


def test_staging_sha_survives_worktree_removal(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    wt_m = tmp_path / "wt-m"
    tip_m = _worker_commit(repo, "wt-m", wt_m, {"m.txt": "m\n"}, base)
    _publish(repo, tip_m, base)  # main advances so the rebase rewrites the commit

    seq = MergeSequencer(task_id="dangle")
    staging = tmp_path / "staging"
    staged = seq.prepare_staging(repo, staging, "wt-a", "main")
    assert staged != tip_a  # rewritten by the rebase; only refs keep it alive
    staging_ref = "refs/cambium/staging/dangle"
    assert _rev(repo, staging_ref) == staged

    # the throwaway worktree dies (crash / external removal) — the commit must
    # survive because the staging ref was written BEFORE the removal
    _run(repo, "worktree", "remove", str(staging))
    assert _rev(repo, staging_ref) == staged
    assert _run(repo, "cat-file", "-e", staged).returncode == 0
    assert staged in _run(repo, "for-each-ref", "--format=%(objectname)").stdout.split()

    seq.cleanup_staging(repo)
    # cleanup drops the ref and the staging branch: the commit is now dangling,
    # exactly the state the ref was protecting against
    assert _run(repo, "rev-parse", "--verify", staging_ref, check=False).returncode != 0
    assert staged not in _run(repo, "for-each-ref", "--format=%(objectname)").stdout.split()


def test_concurrent_publish_exactly_one_winner(tmp_path) -> None:
    for trial in range(5):
        repo = tmp_path / f"repo-{trial}"
        base = _init_repo(repo)
        wt_a = tmp_path / f"wt-a-{trial}"
        tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
        wt_b = tmp_path / f"wt-b-{trial}"
        tip_b = _worker_commit(repo, "wt-b", wt_b, {"b.txt": "b\n"}, base)

        procs = [
            _publish_child(repo, tip_a, base, "a"),
            _publish_child(repo, tip_b, base, "b"),
        ]
        results = [proc.communicate(timeout=60) for proc in procs]
        rcs = [proc.returncode for proc in procs]

        assert sorted(rcs) == [0, 1], f"trial {trial}: expected exactly one winner, got {rcs}"
        winner = rcs.index(0)
        loser = 1 - winner
        assert "NonFastForwardError" in results[loser][1], results[loser][1]
        assert _rev(repo, "refs/heads/main") == (tip_a, tip_b)[winner]


def test_reconcile_reads_current_main(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)

    seq = MergeSequencer(task_id="rec-1")
    assert seq.reconcile(repo) == base

    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    staged_a = seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")
    seq.publish_merge(repo, staged_a, base)
    assert seq.reconcile(repo) == staged_a  # the ref-advance/event gap is closable
    seq.cleanup_staging(repo)

    # a missing ref reports None (no main yet)
    _run(repo, "update-ref", "-d", "refs/heads/main")
    assert seq.reconcile(repo) is None
