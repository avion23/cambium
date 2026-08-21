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

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path
from typing import Any, cast

import pytest

from cambium.merge import (
    GitError,
    MergeConflictError,
    MergeSequencer,
    NonFastForwardError,
    QuarantineError,
    StagingCleanupError,
)
from cambium.store import EventStore
from cambium.supervisor import _Runtime, read_events, run_plan

pytestmark = pytest.mark.slow  # real git merges and worktrees: tier-2

SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")


# -- helpers ----------------------------------------------------------------


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _rev(cwd: Path, rev: str) -> str:
    return _run(cwd, "rev-parse", "--verify", f"{rev}^{{commit}}").stdout.strip()


def _write_git_config(repo: Path, git_dir: Path | None = None) -> None:
    """Write the fixed repo identity directly into the git config file.

    ``git config`` costs one subprocess per key; the merge scenarios spawn
    dozens of git subprocesses per test, so the three fixed settings are
    appended to the config file once instead. ``git_dir`` is the real admin
    directory for ``--separate-git-dir`` repos (``repo/.git`` is then a file).
    """
    config = (git_dir or repo / ".git") / "config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            "[user]\n"
            "\tname = merge-test\n"
            "\temail = merge@test\n"
            "[gc]\n"
            "\tauto = 0\n"
        )


def _init_repo(repo: Path) -> str:
    """Scratch repo on branch main with one base commit. Returns the base SHA."""
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _write_git_config(repo)
    (repo / "base.txt").write_text("base\n")
    _run(repo, "add", "base.txt")
    _run(repo, "commit", "-m", "initial")
    return _rev(repo, "HEAD")


def _init_separate_git_dir_repo(repo: Path, git_dir: Path) -> str:
    subprocess.run(
        ["git", "init", "-b", "main", f"--separate-git-dir={git_dir}", str(repo)],
        check=True, capture_output=True,
    )
    _write_git_config(repo, git_dir)
    (repo / "base.txt").write_text("base\n")
    _run(repo, "add", "base.txt")
    _run(repo, "commit", "-m", "initial")
    return _rev(repo, "HEAD")


def _worker_commit(repo: Path, branch: str, wt: Path, files: dict[str, str], from_: str) -> str:
    """Worker-style commit on ``branch`` with one commit. Returns the tip SHA.

    Built with git plumbing (``fast-import``) instead of a checkout in a
    worktree: the sequencer only consumes the branch tip and its tree, so one
    subprocess replaces ``worktree add`` + ``add`` + ``commit`` + ``rev-parse``.
    File bytes are preserved exactly.
    """
    lines = [
        f"commit refs/heads/{branch}",
        "mark :1",
        f"committer merge-test <merge@test> {int(time.time())} +0000",
    ]
    message = f"{branch}: {','.join(files)}\n"
    lines.append(f"data {len(message.encode('utf-8'))}")
    lines.append(message.rstrip("\n"))
    lines.append(f"from {from_}")
    for name, content in files.items():
        payload = content.encode("utf-8")
        lines.append(f"M 100644 inline {name}")
        lines.append(f"data {len(payload)}")
        lines.append(content.rstrip("\n"))
    subprocess.run(
        ["git", "fast-import", "--quiet"],
        input=("\n".join(lines) + "\n").encode("utf-8"),
        cwd=str(repo),
        capture_output=True,
        check=True,
    )
    return _rev(repo, f"refs/heads/{branch}")


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
    _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
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
    _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
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
    _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)

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

    seq = MergeSequencer(task_id="conf-1", session_dir=tmp_path)
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

    seq = MergeSequencer(task_id="dangle", session_dir=tmp_path)
    staging = tmp_path / "staging"
    staged = seq.prepare_staging(repo, staging, "wt-a", "main")
    assert staged != tip_a  # rewritten by the rebase; only refs keep it alive
    staging_ref = seq.staging_ref
    assert staging_ref is not None
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
    for trial in range(3):
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
    _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    staged_a = seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")
    seq.publish_merge(repo, staged_a, base)
    assert seq.reconcile(repo) == staged_a  # the ref-advance/event gap is closable
    seq.cleanup_staging(repo)

    # a missing ref reports None (no main yet)
    _run(repo, "update-ref", "-d", "refs/heads/main")
    assert seq.reconcile(repo) is None


def test_publish_rejects_empty_and_zero_expected_old(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    _run(repo, "update-ref", "-d", "refs/heads/main")  # first-publish state: main absent

    seq = MergeSequencer(task_id="empty-old")
    for bad in ("", "0" * 40):
        with pytest.raises(NonFastForwardError):
            seq.publish_merge(repo, tip_a, bad)
        # the backdoor is closed: git would read the empty/zero old-value as
        # "ref must not exist" and CREATE main — assert it was not created
        assert _run(repo, "rev-parse", "--verify", "refs/heads/main", check=False).returncode != 0

    # the legitimate first-publish path still works
    seq.create_main(repo, tip_a)
    assert _rev(repo, "refs/heads/main") == tip_a
    # a second create is refused (race-safety: create_main's "must not exist"
    # old-value never clobbers an existing main) and changes nothing
    with pytest.raises(NonFastForwardError):
        seq.create_main(repo, tip_a)
    assert _rev(repo, "refs/heads/main") == tip_a


def test_publish_rejects_non_fast_forward_tips(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    wt_a = tmp_path / "wt-a"
    tip_a = _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    wt_b = tmp_path / "wt-b"
    tip_b = _worker_commit(repo, "wt-b", wt_b, {"b.txt": "b\n"}, tip_a)

    seq = MergeSequencer(task_id="ff-1")
    seq.publish_merge(repo, tip_a, base)  # genuine fast-forward works
    assert _rev(repo, "refs/heads/main") == tip_a
    seq.publish_merge(repo, tip_b, tip_a)
    assert _rev(repo, "refs/heads/main") == tip_b

    # rewind: publishing the now-ancestor tip_a against the current tip_b must
    # be refused even though the expected-old check alone would pass
    with pytest.raises(NonFastForwardError) as exc:
        seq.publish_merge(repo, tip_a, tip_b)
    assert "descendant" in str(exc.value)
    assert _rev(repo, "refs/heads/main") == tip_b

    # sideways: a divergent commit is not a descendant either
    wt_c = tmp_path / "wt-c"
    tip_c = _worker_commit(repo, "wt-c", wt_c, {"c.txt": "c\n"}, base)
    with pytest.raises(NonFastForwardError):
        seq.publish_merge(repo, tip_c, tip_b)
    assert _rev(repo, "refs/heads/main") == tip_b


def test_staging_conflict_with_spaced_path(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / "my file.txt").write_text("orig\n")
    _run(repo, "add", "my file.txt")
    _run(repo, "commit", "-m", "add spaced file")
    base = _rev(repo, "HEAD")

    wt_a = tmp_path / "wt-a"
    _worker_commit(repo, "wt-a", wt_a, {"my file.txt": "from-a\n"}, base)
    wt_m = tmp_path / "wt-m"
    _worker_commit(repo, "wt-m", wt_m, {"my file.txt": "from-m\n"}, base)
    _publish(repo, _rev(repo, "refs/heads/wt-m"), base)

    seq = MergeSequencer(task_id="conf-space", session_dir=tmp_path)
    with pytest.raises(MergeConflictError) as exc:
        seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")
    assert "my file.txt" in exc.value.conflicts  # unquoted by the -z parse
    assert _rev(repo, "refs/heads/main") == _rev(repo, "refs/heads/wt-m")  # untouched
    seq.cleanup_staging(repo)


def test_registered_staging_path_with_literal_newline_is_reusable_and_cleaned(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    wt_a = tmp_path / "wt-a"
    _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)

    staging = tmp_path / "staging\npath"
    seq = MergeSequencer(task_id="newline-path")
    staged = seq.prepare_staging(repo, staging, "wt-a", "main")
    reused = seq.prepare_staging(repo, staging, "wt-a", "main")

    assert reused == staged
    assert staging.is_dir()
    seq.cleanup_staging(repo)
    assert not staging.exists()


def test_prepare_staging_fetches_branch_from_remote(tmp_path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True
    )

    repo = tmp_path / "repo"
    _init_repo(repo)
    _run(repo, "remote", "add", "origin", str(remote))
    _run(repo, "push", "-u", "origin", "main")

    builder = tmp_path / "builder"
    subprocess.run(["git", "clone", "-q", str(remote), str(builder)], check=True)
    _write_git_config(builder)
    _run(builder, "checkout", "-b", "wt-remote")
    (builder / "remote.txt").write_text("remote\n")
    _run(builder, "add", "remote.txt")
    _run(builder, "commit", "-m", "remote work")
    tip_remote = _rev(builder, "HEAD")
    _run(builder, "push", "origin", "wt-remote")

    # the main repo has neither the local branch nor the remote-tracking ref yet
    assert _run(repo, "rev-parse", "--verify", "refs/heads/wt-remote",
                check=False).returncode != 0
    assert _run(repo, "rev-parse", "--verify", "refs/remotes/origin/wt-remote",
                check=False).returncode != 0

    seq = MergeSequencer(task_id="fetch-1", session_dir=tmp_path)
    staged = seq.prepare_staging(repo, tmp_path / "staging", "wt-remote", "main")
    assert staged == tip_remote  # fetched and rebased onto the unchanged main
    assert seq.staging_ref is not None
    assert _rev(repo, seq.staging_ref) == staged
    seq.cleanup_staging(repo)


def test_repo_path_that_is_a_file_raises_git_error(tmp_path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.write_text("a file, not a directory\n")
    seq = MergeSequencer(task_id="bad-path")

    with pytest.raises(GitError) as exc:
        seq.publish_merge(not_a_repo, "a" * 40, "b" * 40)
    assert "not-a-repo" in str(exc.value)

    # reconcile is best-effort: a broken repo path reports None, not a crash
    assert seq.reconcile(not_a_repo) is None


def _install_post_checkout_hook(repo: Path, dump: Path) -> None:
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"with open({str(dump)!r}, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(dict(os.environ)) + chr(10))\n"
    )
    hook.chmod(0o755)


def _hook_records(dump: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in dump.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_post_checkout_hook_does_not_run_during_staging(tmp_path, monkeypatch) -> None:
    """MergeSequencer git operations must disable repository hooks."""
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", "hook-secret")
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    dump = tmp_path / "hook-env.jsonl"
    _install_post_checkout_hook(repo, dump)

    wt_a = tmp_path / "wt-a"
    _worker_commit(repo, "wt-a", wt_a, {"a.txt": "a\n"}, base)
    dump.write_text("")  # drop hook runs made by the test's own git (full env)

    seq = MergeSequencer(task_id="hook-1")
    staged = seq.prepare_staging(repo, tmp_path / "staging", "wt-a", "main")
    assert staged is not None
    seq.cleanup_staging(repo)

    assert _hook_records(dump) == []


def _quarantined(seq: MergeSequencer) -> tuple[dict, Path]:
    events = seq.drain_events()
    payload = next(payload for kind, payload in events if kind == "merge_staging_quarantined")
    session = seq._session_dir  # scenario assertion against the public on-disk contract
    assert session is not None
    return payload, session / ".cambium" / "quarantine" / payload["quarantine_id"]


def test_dirty_staging_kinds_are_moved_byte_for_byte(tmp_path) -> None:
    """All five dirty-kind detectors move the worktree byte-for-byte.

    One staging worktree is dirtied with every kind at once: a tracked
    modification (worker.txt), an added index entry, an untracked file, an
    ignored file, and an in-progress rebase marker. The single quarantine
    move must preserve every file's bytes, keep the staging branch and ref
    alive, and the emitted reason must name all five kinds — so a detector
    that fails to recognize any kind fails this test (the reason-set check)
    exactly as the per-kind scenario did.
    """
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    (repo / ".gitignore").write_text("secret-name.bin\n")
    _run(repo, "add", ".gitignore")
    _run(repo, "commit", "-m", "ignore forensic fixture")
    base = _rev(repo, "HEAD")
    worker = tmp_path / "worker"
    _worker_commit(repo, "worker", worker, {"worker.txt": "worker\n"}, base)
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="dirty-all", session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")

    evidence = {
        "worker.txt": b"modified\x00tracked\xff",  # tracked: in the committed tree
        "evidence.bin": b"untracked\x00bytes\xff",
        "indexed.bin": b"indexed\x00bytes\xff",
        "secret-name.bin": b"ignored\x00bytes\xff",
    }
    for name, blob in evidence.items():
        (staging / name).write_bytes(blob)
    _run(staging, "add", "indexed.bin")
    git_dir = Path(
        _run(staging, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    )
    (git_dir / "MERGE_HEAD").write_text(base + "\n")

    branch, ref = seq.staging_branch, seq.staging_ref
    seq.cleanup_staging(repo)
    payload, destination = _quarantined(seq)

    for name, blob in evidence.items():
        assert (destination / name).read_bytes() == blob
    assert set(payload["reason"].split(",")) == {
        "in-progress", "indexed", "ignored", "tracked", "untracked",
    }
    assert branch and _run(repo, "show-ref", "--verify", f"refs/heads/{branch}").returncode == 0
    assert ref and _run(repo, "show-ref", "--verify", ref).returncode == 0
    serialized = repr(payload)
    assert "secret-name.bin" not in serialized
    assert "forensic" not in serialized


def test_clean_cleanup_removes_worktree_branch_and_ref(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="clean", session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    branch, ref = seq.staging_branch, seq.staging_ref
    seq.cleanup_staging(repo)
    assert not staging.exists()
    assert branch and _run(
        repo, "show-ref", "--verify", f"refs/heads/{branch}", check=False
    ).returncode
    assert ref and _run(repo, "show-ref", "--verify", ref, check=False).returncode


def test_traversal_task_id_stays_below_quarantine_root(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    task_id = "../../escape/secret"
    seq = MergeSequencer(task_id=task_id, session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    (staging / "dirty").write_text("evidence")
    seq.cleanup_staging(repo)
    payload, destination = _quarantined(seq)
    root = tmp_path / ".cambium" / "quarantine"
    assert destination.resolve().is_relative_to(root.resolve())
    assert payload["quarantine_id"].startswith(
        "merge/task-" + hashlib.sha256(task_id.encode()).hexdigest()[:16] + "/"
    )
    assert task_id not in payload["quarantine_id"]


def test_oversized_newest_is_registered_and_fails_closed(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    seq = MergeSequencer(
        task_id="oversized", session_dir=tmp_path, quarantine_max_bytes=1,
        quarantine_min_free_bytes=0,
    )
    seq.prepare_staging(repo, staging, "worker", "main")
    (staging / "large").write_bytes(b"x" * 8192)
    with pytest.raises(StagingCleanupError, match="exceeds"):
        seq.cleanup_staging(repo)
    payload, destination = _quarantined(seq)
    assert destination.exists()
    assert str(destination.resolve()) in _run(repo, "worktree", "list", "--porcelain").stdout
    assert payload["allocated_bytes"] > 1


def test_symlink_target_is_not_counted(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    outside = tmp_path / "outside"
    outside.write_bytes(b"x" * 1024 * 1024)
    staging = tmp_path / "staging"
    seq = MergeSequencer(
        task_id="symlink", session_dir=tmp_path, quarantine_max_bytes=512 * 1024,
        quarantine_min_free_bytes=0,
    )
    seq.prepare_staging(repo, staging, "worker", "main")
    (staging / "link").symlink_to(outside)
    seq.cleanup_staging(repo)
    payload, _ = _quarantined(seq)
    assert payload["allocated_bytes"] < outside.stat().st_size


def test_precreated_quarantine_task_symlink_is_refused(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    task_id = "symlink-escape"
    seq = MergeSequencer(task_id=task_id, session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    evidence = staging / "evidence.bin"
    evidence.write_bytes(b"must stay here")
    outside = tmp_path / "outside"
    outside.mkdir()
    quarantine = tmp_path / ".cambium" / "quarantine" / "merge"
    quarantine.mkdir(parents=True)
    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    (quarantine / f"task-{task_key}").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StagingCleanupError, match="symlink"):
        seq.cleanup_staging(repo)

    assert evidence.read_bytes() == b"must stay here"
    assert not list(outside.iterdir())


def test_task_directory_swap_at_move_boundary_restores_staging(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    task_id = "swap-at-move"
    seq = MergeSequencer(task_id=task_id, session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    evidence = staging / "evidence.bin"
    evidence.write_bytes(b"must remain at source")
    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    task_dir = tmp_path / ".cambium" / "quarantine" / "merge" / f"task-{task_key}"
    outside = tmp_path / "outside"
    displaced = outside / "displaced"
    sink = outside / "sink"
    outside.mkdir()
    sink.mkdir()
    original = seq._run_repo
    swapped = False

    def swap_then_move(repo_path, *args, **kwargs):
        nonlocal swapped
        if args[:2] == ("worktree", "move") and not swapped:
            swapped = True
            task_dir.rename(displaced)
            task_dir.symlink_to(sink, target_is_directory=True)
        return original(repo_path, *args, **kwargs)

    monkeypatch.setattr(seq, "_run_repo", swap_then_move)
    with pytest.raises(StagingCleanupError, match="changed during worktree move"):
        seq.cleanup_staging(repo)

    assert evidence.read_bytes() == b"must remain at source"
    assert not list(displaced.iterdir())
    assert not list(sink.iterdir())


def test_task_directory_rename_before_recording_restores_staging(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    task_id = "rename-before-recording"
    seq = MergeSequencer(task_id=task_id, session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    branch = seq.staging_branch
    evidence = staging / "evidence.bin"
    evidence.write_bytes(b"must not be stranded")
    task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    task_dir = tmp_path / ".cambium" / "quarantine" / "merge" / f"task-{task_key}"
    displaced = tmp_path / "displaced"
    original = seq._event

    def rename_before_recording(kind, **payload):
        if kind == "merge_staging_quarantined":
            task_dir.rename(displaced)
        original(kind, **payload)

    monkeypatch.setattr(seq, "_event", rename_before_recording)

    with pytest.raises(StagingCleanupError, match="quarantine path changed"):
        seq.cleanup_staging(repo)

    assert evidence.read_bytes() == b"must not be stranded"
    registered = _run(repo, "worktree", "list", "--porcelain").stdout
    assert str(staging.resolve()) in registered
    assert str(displaced.resolve()) not in registered
    assert branch is not None
    assert _rev(staging, "HEAD") == _rev(repo, f"refs/heads/{branch}")
    assert not any(kind == "merge_staging_quarantined" for kind, _ in seq.drain_events())
    assert not list(displaced.iterdir())


def test_quarantine_is_durable_before_cleanup_returns_and_reconciles_rename(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    persisted: list[tuple[str, dict]] = []

    def persist(kind: str, payload: dict) -> None:
        destination = (
            tmp_path / ".cambium" / "quarantine" / payload["quarantine_id"]
        )
        assert destination.is_dir()
        persisted.append((kind, payload))

    seq = MergeSequencer(
        task_id="durable-before-return", session_dir=tmp_path, durable_event=persist
    )
    seq.prepare_staging(repo, staging, "worker", "main")
    (staging / "evidence.bin").write_bytes(b"must be recorded before descriptors close")

    seq.cleanup_staging(repo)
    assert [kind for kind, _ in persisted] == ["merge_staging_quarantined"]
    destination = (
        tmp_path / ".cambium" / "quarantine" / persisted[0][1]["quarantine_id"]
    )
    displaced = tmp_path / "displaced-after-cleanup"
    destination.rename(displaced)  # the old supervisor flush happened after this boundary

    recovery = MergeSequencer(task_id="durable-before-return", session_dir=tmp_path)
    recovery.reconcile(repo, quarantine_events=[persisted[0][1]])

    assert destination.is_dir()
    assert not displaced.exists()
    assert (destination / "evidence.bin").read_bytes() == (
        b"must be recorded before descriptors close"
    )


def test_operator_removes_quarantine_then_restart_records_removal_and_spawns(tmp_path) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    base = _init_repo(repo)
    _worker_commit(
        repo, "artifact-source", session / "artifact-source",
        {"artifact.txt": "artifact\n"}, base,
    )

    async def quarantine() -> tuple[str, Path]:
        store = EventStore(session / ".cambium" / "events.db")
        runtime = _Runtime(session, store)
        await runtime.start()
        seq = runtime._make_sequencer("artifact-owner")
        staging = session / "artifact-staging"
        seq.prepare_staging(repo, staging, "artifact-source", "main")
        (staging / "evidence.bin").write_bytes(b"operator may remove this evidence")
        await asyncio.to_thread(seq.cleanup_staging, repo)
        event = next(
            event for event in store.events_after(0)
            if event["kind"] == "merge_staging_quarantined"
        )
        quarantine_id = event["payload"]["quarantine_id"]
        artifact = session / ".cambium" / "quarantine" / quarantine_id
        await runtime.shutdown()
        return quarantine_id, artifact

    quarantine_id, artifact = asyncio.run(quarantine())
    _run(repo, "worktree", "remove", "--force", str(artifact))
    assert not artifact.exists()

    worker = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"
    plan = {"tasks": [{
        "task_id": "restart-worker",
        "task": "edit base.txt",
        "repo": str(repo),
        "worktree_path": str(session / "restart-worker"),
        "branch": "restart-worker",
        "worker": str(worker),
        "target_file": "base.txt",
        "marker": "// restarted",
        "write_marker": True,
        "gate": "grep -q '// restarted' base.txt",
        "base_commit": base,
    }]}

    result = asyncio.run(run_plan(session, plan))
    events = read_events(session)

    assert result.exit_code == 0
    assert result.results[0].status == "succeeded"
    spawned = next(event for event in events if event["kind"] == "spawned")
    removals = [
        event for event in events
        if event["kind"] == "merge_staging_pruned"
        and event["payload"].get("quarantine_id") == quarantine_id
        and event["payload"].get("reason") == "operator-removed"
    ]
    assert len(removals) == 1
    assert removals[0]["seq"] < spawned["seq"]


def test_operator_removed_tombstone_observer_cancellation_still_spawns(tmp_path) -> None:
    session = tmp_path / "session"
    repo = session / "repo"
    base = _init_repo(repo)
    _worker_commit(
        repo, "artifact-source", session / "artifact-source",
        {"artifact.txt": "artifact\n"}, base,
    )

    async def quarantine() -> tuple[str, Path]:
        store = EventStore(session / ".cambium" / "events.db")
        runtime = _Runtime(session, store)
        await runtime.start()
        seq = runtime._make_sequencer("artifact-owner")
        staging = session / "artifact-staging"
        seq.prepare_staging(repo, staging, "artifact-source", "main")
        (staging / "evidence.bin").write_bytes(b"observer cancellation must not stop recovery")
        await asyncio.to_thread(seq.cleanup_staging, repo)
        event = next(
            event for event in store.events_after(0)
            if event["kind"] == "merge_staging_quarantined"
        )
        quarantine_id = event["payload"]["quarantine_id"]
        artifact = session / ".cambium" / "quarantine" / quarantine_id
        await runtime.shutdown()
        return quarantine_id, artifact

    quarantine_id, artifact = asyncio.run(quarantine())
    _run(repo, "worktree", "remove", "--force", str(artifact))
    assert not artifact.exists()

    worker = Path(__file__).resolve().parents[2] / "scripts" / "fake_worker.py"
    plan = {"tasks": [{
        "task_id": "restart-worker",
        "task": "edit base.txt",
        "repo": str(repo),
        "worktree_path": str(session / "restart-worker"),
        "branch": "restart-worker",
        "worker": str(worker),
        "target_file": "base.txt",
        "marker": "// restarted",
        "write_marker": True,
        "gate": "grep -q '// restarted' base.txt",
        "base_commit": base,
    }]}

    async def cancel_on_tombstone(event: dict) -> None:
        if (
            event["kind"] == "merge_staging_pruned"
            and event["payload"].get("quarantine_id") == quarantine_id
            and event["payload"].get("reason") == "operator-removed"
        ):
            raise asyncio.CancelledError

    result = asyncio.run(run_plan(session, plan, on_event=cast(Any, cancel_on_tombstone)))
    events = read_events(session)
    tombstones = [
        event for event in events
        if event["kind"] == "merge_staging_pruned"
        and event["payload"].get("quarantine_id") == quarantine_id
        and event["payload"].get("reason") == "operator-removed"
    ]
    spawned = [event for event in events if event["kind"] == "spawned"]

    assert result.exit_code == 0
    assert result.results[0].status == "succeeded"
    assert len(tombstones) == 1
    assert len(spawned) == 1
    assert tombstones[0]["seq"] < spawned[0]["seq"]


def test_observer_failure_after_durable_append_does_not_restore_staging(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    store = EventStore(tmp_path / ".cambium" / "events.db")

    def reject_event(_event: dict) -> None:
        raise RuntimeError("observer rejected durable event")

    async def quarantine() -> None:
        runtime = _Runtime(tmp_path, store, on_event=reject_event)
        seq = runtime._make_sequencer("observer-failure")
        seq.prepare_staging(repo, staging, "worker", "main")
        (staging / "evidence.bin").write_bytes(b"observer cannot strand evidence")
        await asyncio.to_thread(seq.cleanup_staging, repo)

    try:
        asyncio.run(quarantine())
        events = [
            event for event in store.events_after(0)
            if event["kind"] == "merge_staging_quarantined"
        ]
    finally:
        store.close()

    assert len(events) == 1
    destination = tmp_path / ".cambium" / "quarantine" / events[0]["payload"]["quarantine_id"]
    assert destination.is_dir()
    assert not staging.exists()
    assert (destination / "evidence.bin").read_bytes() == b"observer cannot strand evidence"


def test_move_failure_preserves_original_and_emits_sanitized_failure(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="move-fail", session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    secret = staging / "secret-filename.txt"
    secret.write_text("secret-content")
    original = seq._run_repo

    def fail_move(repo_path, *args, **kwargs):
        if args[:2] == ("worktree", "move"):
            return subprocess.CompletedProcess(args, 1, "", "denied secret-filename.txt")
        return original(repo_path, *args, **kwargs)

    monkeypatch.setattr(seq, "_run_repo", fail_move)
    with pytest.raises(StagingCleanupError):
        seq.cleanup_staging(repo)
    assert secret.read_text() == "secret-content"
    events = seq.drain_events()
    payload = next(payload for kind, payload in events if kind == "merge_staging_cleanup_failed")
    assert "secret-filename" not in repr(payload)
    assert "secret-content" not in repr(payload)


def test_low_disk_keeps_newest_and_fails_closed(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="low-disk", session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    (staging / "dirty").write_text("evidence")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("cambium.merge.shutil.disk_usage", lambda _: usage(100, 100, 0))
    with pytest.raises(StagingCleanupError, match="bounds"):
        seq.cleanup_staging(repo)
    _, destination = _quarantined(seq)
    assert (destination / "dirty").read_text() == "evidence"


def test_count_and_aggregate_byte_caps_prune_oldest(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)

    first = MergeSequencer(task_id="first", session_dir=tmp_path, quarantine_min_free_bytes=0)
    first_path = tmp_path / "staging-first"
    first.prepare_staging(repo, first_path, "worker", "main")
    (first_path / "dirty").write_bytes(b"a" * 4096)
    first.cleanup_staging(repo)
    first_payload, first_destination = _quarantined(first)

    second_path = tmp_path / "staging-second"
    second = MergeSequencer(
        task_id="second", session_dir=tmp_path, quarantine_max_entries=1,
        quarantine_max_bytes=first_payload["allocated_bytes"] + 8192,
        quarantine_min_free_bytes=0,
    )
    second.prepare_staging(repo, second_path, "worker", "main")
    (second_path / "dirty").write_bytes(b"b" * 4096)
    second.cleanup_staging(repo)
    events = second.drain_events()
    second_payload = next(
        payload for kind, payload in events if kind == "merge_staging_quarantined"
    )
    second_destination = tmp_path / ".cambium" / "quarantine" / second_payload["quarantine_id"]

    assert not first_destination.exists()
    assert second_destination.exists()
    assert any(kind == "merge_staging_pruned" for kind, _ in events)


def test_count_cap_prunes_quarantine_entry_from_owning_repo(tmp_path) -> None:
    repo_a = tmp_path / "repo-a"
    base_a = _init_repo(repo_a)
    _worker_commit(repo_a, "worker-a", tmp_path / "worker-a", {"a.txt": "a\n"}, base_a)
    first = MergeSequencer(task_id="repo-a", session_dir=tmp_path, quarantine_min_free_bytes=0)
    first_staging = tmp_path / "staging-a"
    first.prepare_staging(repo_a, first_staging, "worker-a", "main")
    (first_staging / "dirty").write_text("repo a evidence")
    first.cleanup_staging(repo_a)
    _, first_destination = _quarantined(first)

    repo_b = tmp_path / "repo-b"
    base_b = _init_repo(repo_b)
    _worker_commit(repo_b, "worker-b", tmp_path / "worker-b", {"b.txt": "b\n"}, base_b)
    second = MergeSequencer(
        task_id="repo-b", session_dir=tmp_path, quarantine_max_entries=1,
        quarantine_min_free_bytes=0,
    )
    second_staging = tmp_path / "staging-b"
    second.prepare_staging(repo_b, second_staging, "worker-b", "main")
    (second_staging / "dirty").write_text("repo b evidence")
    second.cleanup_staging(repo_b)
    events = second.drain_events()
    second_payload = next(
        payload for kind, payload in events if kind == "merge_staging_quarantined"
    )
    second_destination = tmp_path / ".cambium" / "quarantine" / second_payload["quarantine_id"]

    assert not first_destination.exists()
    assert second_destination.exists()
    assert any(kind == "merge_staging_pruned" for kind, _ in events)


def test_expired_artifact_is_pruned_on_reconcile(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    staging = tmp_path / "staging"
    seq = MergeSequencer(
        task_id="expired", session_dir=tmp_path, quarantine_retention_ns=1,
        quarantine_min_free_bytes=0,
    )
    seq.prepare_staging(repo, staging, "worker", "main")
    (staging / "dirty").write_text("expired evidence")
    seq.cleanup_staging(repo)
    _, destination = _quarantined(seq)
    assert destination.exists()
    time.sleep(0.001)
    seq.reconcile(repo)
    assert not destination.exists()
    assert any(kind == "merge_staging_pruned" for kind, _ in seq.drain_events())


def test_count_cap_prunes_with_separate_git_dir(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_separate_git_dir_repo(repo, tmp_path / "repo-git")
    _worker_commit(repo, "worker-a", tmp_path / "worker-a", {"a.txt": "a\n"}, base)
    first = MergeSequencer(
        task_id="separate-first", session_dir=tmp_path, quarantine_min_free_bytes=0,
    )
    first_staging = tmp_path / "staging-a"
    first.prepare_staging(repo, first_staging, "worker-a", "main")
    (first_staging / "dirty").write_text("first evidence")
    first.cleanup_staging(repo)
    _, first_destination = _quarantined(first)

    _worker_commit(repo, "worker-b", tmp_path / "worker-b", {"b.txt": "b\n"}, base)
    second = MergeSequencer(
        task_id="separate-second", session_dir=tmp_path, quarantine_max_entries=1,
        quarantine_min_free_bytes=0,
    )
    second_staging = tmp_path / "staging-b"
    second.prepare_staging(repo, second_staging, "worker-b", "main")
    (second_staging / "dirty").write_text("second evidence")
    second.cleanup_staging(repo)
    _, second_destination = _quarantined(second)

    assert not first_destination.exists()
    assert second_destination.exists()


def test_sparse_dirty_staging_is_preserved(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(
        repo, "worker", tmp_path / "worker",
        {"keep/file.txt": "keep\n", "omit/file.txt": "omit\n"}, base,
    )
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="sparse", session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    _run(staging, "sparse-checkout", "set", "keep")
    (staging / "keep" / "evidence.bin").write_bytes(b"sparse evidence")
    seq.cleanup_staging(repo)
    _, destination = _quarantined(seq)
    assert (destination / "keep" / "evidence.bin").read_bytes() == b"sparse evidence"
    assert _run(destination, "sparse-checkout", "list").stdout.strip() == "keep"


def test_hook_cannot_generate_content_during_staging(tmp_path) -> None:
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    _worker_commit(repo, "worker", tmp_path / "worker", {"worker.txt": "ok\n"}, base)
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nprintf 'hook evidence' > hook-secret-name.txt\n")
    hook.chmod(0o700)
    staging = tmp_path / "staging"
    seq = MergeSequencer(task_id="hook-generated", session_dir=tmp_path)
    seq.prepare_staging(repo, staging, "worker", "main")
    seq.ensure_staging_clean(repo)

    assert not (staging / "hook-secret-name.txt").exists()
