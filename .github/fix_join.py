from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str, *, label: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    write(path, text.replace(old, new, 1))


replace_once(
    "src/cambium/worker.py",
    '''    if loop_outcome["status"] != "succeeded":
        return _loop_failure_outcome(loop_outcome)
    outcome = await asyncio.to_thread(
        _finalize_worktree,
        run=run,
        config=config,
        worktree=worktree,
        generation=config.generation,
        worker_identity=worker_identity,
        stop=stop,
        loop_outcome=loop_outcome,
    )
    final_checkpoint = outcome.pop("_checkpoint_path", None)
    terminal_checkpoint = outcome.pop("_context_checkpoint", None)
''',
    '''    loop_status = loop_outcome["status"]
    if loop_status not in {"succeeded", TaskStatus.SUSPENDED.value}:
        return _loop_failure_outcome(loop_outcome)
    outcome = await asyncio.to_thread(
        _finalize_worktree,
        run=run,
        config=config,
        worktree=worktree,
        generation=config.generation,
        worker_identity=worker_identity,
        stop=stop,
        loop_outcome=loop_outcome,
    )
    if loop_status == TaskStatus.SUSPENDED.value and outcome["status"] == "succeeded":
        epoch = loop_outcome.get("epoch")
        checkpoint_ref = loop_outcome.get("checkpoint_ref")
        if type(epoch) is not int or epoch <= 0 or not isinstance(checkpoint_ref, str):
            return _loop_failure_outcome(
                {
                    "status": "failed",
                    "failure_reason": "suspension snapshot has no context checkpoint",
                }
            )
        outcome.update(
            status=TaskStatus.SUSPENDED.value,
            failure_reason=None,
            epoch=epoch,
            checkpoint_ref=checkpoint_ref,
            summary=loop_outcome.get("summary", "")[:MAX_SUMMARY_CHARS],
        )
    final_checkpoint = outcome.pop("_checkpoint_path", None)
    terminal_checkpoint = outcome.pop("_context_checkpoint", None)
''',
    label="worker suspension snapshot",
)

replace_once(
    "src/cambium/worker.py",
    '''        code_changed = resume_checkpoint.code_changed
        verified_after_change = resume_checkpoint.verified_after_change
        verification_failed = resume_checkpoint.verification_failed
''',
    '''        child_code_changed = any(
            child_result.get("status") == "succeeded"
            and bool(child_result.get("commits") or child_result.get("files_changed"))
            for child_result in resume["child_results"]
        )
        code_changed = resume_checkpoint.code_changed or child_code_changed
        verified_after_change = (
            resume_checkpoint.verified_after_change and not child_code_changed
        )
        verification_failed = (
            False if child_code_changed else resume_checkpoint.verification_failed
        )
''',
    label="combined-tree verification gate",
)

replace_once(
    "src/cambium/store.py",
    '''        "join_invariant_failed",
        "merge_staging_quarantined",
''',
    '''        "join_invariant_failed",
        "parent_snapshot",
        "child_integration_prepared",
        "child_integrated",
        "merge_staging_quarantined",
''',
    label="private integration durability kinds",
)

replace_once(
    "src/cambium/supervisor.py",
    '''    async def _admit_child(
        self,
        parent_spec: dict[str, Any],
        proposal: dict[str, Any],
        parent_envelope: dict[str, Any],
    ) -> list[str]:
''',
    '''    async def _admit_child(
        self,
        parent_spec: dict[str, Any],
        proposal: dict[str, Any],
        parent_envelope: dict[str, Any],
        *,
        private_integration_base: str | None = None,
    ) -> list[str]:
''',
    label="child admission signature",
)

replace_once(
    "src/cambium/supervisor.py",
    '''        try:
            child_spec = _child_spec(self._session_dir, parent_spec, proposal, parent_envelope)
        except ValueError as exc:
''',
    '''        try:
            child_spec = _child_spec(self._session_dir, parent_spec, proposal, parent_envelope)
            if private_integration_base is not None:
                if Path(child_spec["repo"]).resolve() != Path(parent_spec["repo"]).resolve():
                    raise ValueError("a suspended parent and its child must share one repository")
                child_spec["base_commit"] = private_integration_base
                child_spec["_private_parent_integration"] = True
        except ValueError as exc:
''',
    label="private child base override",
)

replace_once(
    "src/cambium/supervisor.py",
    '''    async def _admit_generation_children(
        self,
        parent_spec: dict[str, Any],
        parent_envelope: dict[str, Any],
        proposals: Sequence[dict[str, Any]],
        *,
        include_port: bool,
    ) -> list[str]:
        """Admit proposals after the permitted parent lifecycle verdict."""
        admitted: list[str] = []
        for proposal in proposals:
            admitted.extend(await self._admit_child(parent_spec, proposal, parent_envelope))
''',
    '''    async def _admit_generation_children(
        self,
        parent_spec: dict[str, Any],
        parent_envelope: dict[str, Any],
        proposals: Sequence[dict[str, Any]],
        *,
        include_port: bool,
        private_integration_base: str | None = None,
    ) -> list[str]:
        """Admit proposals after the permitted parent lifecycle verdict."""
        admitted: list[str] = []
        for proposal in proposals:
            admitted.extend(
                await self._admit_child(
                    parent_spec,
                    proposal,
                    parent_envelope,
                    private_integration_base=private_integration_base,
                )
            )
''',
    label="generation child integration base",
)

snapshot_method = '''    async def _accept_parent_suspension_snapshot(
        self,
        spec: dict[str, Any],
        worktree: Path,
        generation: int,
    ) -> tuple[str | None, str | None]:
        """Accept one worker-owned suspension commit as a private base.

        The worker has already exited and fenced every dirty file into at most
        one commit.  The supervisor verifies a clean attached branch, records
        the transition durably, and only then allows children to branch from
        that immutable snapshot.  The snapshot is not considered published.
        """
        integrity = await self._worker_success_integrity(spec, worktree)
        if integrity is not None:
            return None, integrity
        head = await self._git_stdout(
            worktree, "rev-parse", "--verify", "HEAD^{commit}", check=False
        )
        if head is None:
            return None, "worker_head_failed"
        prior_base = str(spec["base_commit"])
        base_was_published = bool(spec.get("_base_is_published", True))
        base_is_published = base_was_published and head == prior_base
        await self.emit(
            "parent_snapshot",
            task_id=spec["task_id"],
            generation=generation,
            old=prior_base,
            new=head,
            changed=head != prior_base,
            base_is_published=base_is_published,
            branch=spec["branch"],
            repo=spec["repo"],
        )
        spec["base_commit"] = head
        spec["_base_is_published"] = base_is_published
        return head, None

'''
replace_once(
    "src/cambium/supervisor.py",
    '''    async def _assert_parent_join_invariant(
''',
    snapshot_method + '''    async def _assert_parent_join_invariant(
''',
    label="suspension snapshot acceptor",
)

replace_once(
    "src/cambium/supervisor.py",
    '''                    if envelope_status == "suspended":
                        # A suspended generation is not a publishable success;
                        # its children may run before the bounded resume wait.
                        child_ids = await self._admit_generation_children(
                            spec,
                            parent_envelope,
                            outcome.proposals,
                            include_port=False,
                        )
''',
    '''                    if envelope_status == "suspended":
                        # Snapshot isolation: the worker owns the suspension
                        # commit; children integrate privately; only the
                        # resumed and verified parent may publish to main.
                        snapshot_head, snapshot_error = (
                            await self._accept_parent_suspension_snapshot(
                                spec, worktree, generation
                            )
                        )
                        if snapshot_error is not None or snapshot_head is None:
                            reason = snapshot_error or "parent_snapshot_failed"
                            await self.emit(
                                "worker_failed",
                                task_id=task_id,
                                generation=generation,
                                reason=reason,
                            )
                            await self._reject_child_proposals(
                                task_id,
                                outcome.proposals,
                                reason="ParentSnapshotFailed",
                                message="parent suspension snapshot failed integrity checks",
                            )
                            self._results[task_id] = TaskResult(
                                task_id=task_id,
                                status="failed",
                                exit_code=1,
                                reason=reason,
                                restarts=restarts,
                                summary=worker_summary,
                            )
                            return
                        child_ids = await self._admit_generation_children(
                            spec,
                            parent_envelope,
                            outcome.proposals,
                            include_port=False,
                            private_integration_base=snapshot_head,
                        )
''',
    label="suspended parent transaction",
)

replace_once(
    "src/cambium/supervisor.py",
    '''                    if head == spec["base_commit"]:
''',
    '''                    if head == spec["base_commit"] and bool(
                        spec.get("_base_is_published", True)
                    ):
''',
    label="unpublished private base publication gate",
)

replace_once(
    "src/cambium/supervisor.py",
    '''                    if merged is not None:
                        await self._admit_generation_children(
''',
    '''                    if merged is not None:
                        spec["base_commit"] = merged
                        spec["_base_is_published"] = True
                        await self._admit_generation_children(
''',
    label="published base transition",
)

replace_once(
    "src/cambium/supervisor.py",
    '''        if parent_head == integration_head:
            self._accepted_integration_heads.pop(parent_task_id, None)
            return True
''',
    '''        if parent_head == integration_head:
            integrity = await self._worker_success_integrity(parent_spec, worktree)
            if integrity is None:
                self._accepted_integration_heads.pop(parent_task_id, None)
                return True
''',
    label="join invariant includes tree integrity",
)

private_merge_method = '''    async def _integrate_child_into_suspended_parent(
        self, spec: dict[str, Any], handle: WorkerHandle
    ) -> str | None:
        """Integrate a child into its suspended parent without publishing main.

        ``prepare_staging`` rebases the child onto the parent's current private
        base.  A critical prepared event is the write-ahead record; then one
        fast-forward updates the clean parent branch and worktree; finally a
        critical committed event makes the new private base visible to resume.
        The staging ref is retained when the second barrier is not reached.
        """
        task_id = spec["task_id"]
        parent_task_id = spec.get("parent_task_id")
        if not isinstance(parent_task_id, str):
            return None
        parent_spec = self._session_spec(parent_task_id)
        if parent_spec is None:
            return None
        repo = Path(spec["repo"])
        if repo.resolve() != Path(parent_spec["repo"]).resolve():
            return None
        branch = spec["branch"]
        parent_worktree = Path(parent_spec["worktree_path"])
        await self.emit(
            "merge_started",
            task_id=task_id,
            branch=branch,
            generation=handle.generation,
            target="suspended_parent",
            parent_task_id=parent_task_id,
        )
        task_key = hashlib.sha256(task_id.encode()).hexdigest()[:16]
        throwaway = self._session_dir / ".cambium" / "merge-wt" / f"task-{task_key}"
        deferred: list[tuple[dict[str, Any], bool]] = []
        seq = self._make_sequencer(task_id, deferred)
        prepared_persisted = False
        integrated_persisted = False
        cleanup_failed = False
        merge_failed = False
        staging_tip: str | None = None
        parent_head: str | None = None
        try:
            async with self._merge_lock:
                integrity = await self._worker_success_integrity(parent_spec, parent_worktree)
                if integrity is not None:
                    raise RuntimeError(f"parent integration precondition failed: {integrity}")
                parent_head = await self._git_stdout(
                    parent_worktree,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                    check=False,
                )
                if parent_head is None or parent_head != parent_spec.get("base_commit"):
                    raise RuntimeError("parent private base changed before child integration")
                staging_tip = await asyncio.to_thread(
                    seq.prepare_staging, repo, throwaway, branch, parent_head
                )
                await self._flush_sequencer_events(seq, deferred_observers=deferred)
                if hasattr(seq, "ensure_staging_clean"):
                    await asyncio.to_thread(seq.ensure_staging_clean, repo)
                    await self._flush_sequencer_events(seq, deferred_observers=deferred)
                await self.emit(
                    "child_integration_prepared",
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    old=parent_head,
                    new=staging_tip,
                    repo=str(repo),
                    parent_branch=parent_spec["branch"],
                    child_branch=branch,
                    staging_ref=seq.staging_ref,
                    staging_branch=seq.staging_branch,
                    staging_worktree=str(throwaway),
                    generation=handle.generation,
                    _deferred_observers=deferred,
                )
                prepared_persisted = True
                advanced = await self._git(
                    parent_worktree,
                    "merge",
                    "--ff-only",
                    "--no-edit",
                    staging_tip,
                    check=False,
                )
                if advanced.returncode != 0:
                    raise RuntimeError("parent private integration fast-forward failed")
                accepted = await self._git_stdout(
                    parent_worktree,
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                    check=False,
                )
                if accepted != staging_tip:
                    raise RuntimeError("parent private integration head mismatch")
                integrity = await self._worker_success_integrity(parent_spec, parent_worktree)
                if integrity is not None:
                    raise RuntimeError(f"parent integration postcondition failed: {integrity}")
                await self.emit(
                    "child_integrated",
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    old=parent_head,
                    new=staging_tip,
                    repo=str(repo),
                    parent_branch=parent_spec["branch"],
                    child_branch=branch,
                    generation=handle.generation,
                    recovered=False,
                    _deferred_observers=deferred,
                )
                integrated_persisted = True
                parent_spec["base_commit"] = staging_tip
                parent_spec["_base_is_published"] = False
                self._accepted_integration_heads[parent_task_id] = staging_tip
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            merge_failed = True
            error_type = exc.__class__.__name__
            if isinstance(exc, MergeConflictError):
                summary = str(exc)[:512]
                diff_evidence = exc.diff_evidence
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    merge_error=error_type,
                    message=summary,
                    status="merge_conflict",
                    conflicted_files=exc.conflicted_files,
                    summary=summary,
                    diff_evidence=diff_evidence,
                    evidence=diff_evidence,
                    diff=diff_evidence,
                    unified_diff=diff_evidence,
                    diff_truncated=exc.diff_truncated,
                    integration_head=exc.integration_head or parent_head,
                    generation=handle.generation,
                )
            else:
                await self.emit(
                    "merge_failed",
                    task_id=task_id,
                    merge_error=error_type,
                    message=str(exc)[:512],
                    generation=handle.generation,
                    internal=True,
                )
        finally:
            try:
                if hasattr(seq, "cleanup_staging") and not (
                    prepared_persisted and not integrated_persisted
                ):
                    await asyncio.to_thread(seq.cleanup_staging, repo)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                cleanup_failed = True
                emitted = await self._flush_sequencer_events(
                    seq, deferred_observers=deferred
                )
                if integrated_persisted and "merge_staging_cleanup_failed" not in emitted:
                    await self.emit(
                        "merge_staging_cleanup_failed",
                        task_id=task_id,
                        staging_sha=staging_tip,
                        reason=exc.__class__.__name__,
                    )
            else:
                await self._flush_sequencer_events(seq, deferred_observers=deferred)
        try:
            await self._notify_deferred_observers(deferred)
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            await self.emit(
                "merge_failed",
                task_id=task_id,
                merge_error=exc.__class__.__name__,
                message=str(exc)[:512],
                generation=handle.generation,
                internal=True,
            )
            if not integrated_persisted:
                return None
        if merge_failed or (cleanup_failed and not integrated_persisted):
            return None
        return staging_tip

'''
replace_once(
    "src/cambium/supervisor.py",
    '''    async def _merge_task(self, spec: dict[str, Any], handle: WorkerHandle) -> str | None:
        """Stage and atomically publish the worker branch onto refs/heads/main.
''',
    private_merge_method
    + '''    async def _merge_task(self, spec: dict[str, Any], handle: WorkerHandle) -> str | None:
        """Stage and atomically publish the worker branch onto refs/heads/main.
''',
    label="private child integration method",
)

replace_once(
    "src/cambium/supervisor.py",
    '''        task_id = spec["task_id"]
        repo = Path(spec["repo"])
''',
    '''        if spec.get("_private_parent_integration") is True:
            return await self._integrate_child_into_suspended_parent(spec, handle)
        task_id = spec["task_id"]
        repo = Path(spec["repo"])
''',
    label="private merge dispatch",
)

join_test = '''\n\ndef test_private_child_waits_for_parent_publication(tmp_path: Path) -> None:
    session = tmp_path / "session"
    repo = tmp_path / "repo"
    base = _init_repo(repo)
    parent_worktree = tmp_path / "parent"
    _git(repo, "worktree", "add", "-b", "parent", str(parent_worktree), base)
    (parent_worktree / "parent.txt").write_text("parent\n", encoding="utf-8")
    _git(parent_worktree, "add", "parent.txt")
    _git(parent_worktree, "commit", "-m", "parent suspension snapshot")
    snapshot = _rev(parent_worktree)
    child_worktree = tmp_path / "child"
    child_tip = _branch_commit(
        repo, snapshot, "child", child_worktree, "child.txt", "child\n"
    )

    store = _Store()
    runtime = _runtime(session, store)
    parent_spec = {
        "task_id": "parent",
        "kind": "test",
        "repo": str(repo),
        "worktree_path": str(parent_worktree),
        "branch": "parent",
        "base_commit": snapshot,
        "_base_is_published": False,
    }
    runtime.set_session_tasks([parent_spec])
    child_spec = {
        "task_id": "child",
        "repo": str(repo),
        "branch": "child",
        "parent_task_id": "parent",
        "_private_parent_integration": True,
    }

    accepted = asyncio.run(runtime._merge_task(child_spec, WorkerHandle("child", 1)))

    assert accepted == child_tip
    assert _rev(repo, "main") == base
    assert _rev(parent_worktree) == child_tip
    assert parent_spec["base_commit"] == child_tip
    assert parent_spec["_base_is_published"] is False
    assert asyncio.run(runtime._assert_parent_join_invariant(parent_spec, ["child"], 1))
    assert [record for record in store.records if record["kind"] == "child_integrated"]

    published = asyncio.run(runtime._merge_task(parent_spec, WorkerHandle("parent", 2)))
    assert published == child_tip
    assert _rev(repo, "main") == child_tip
'''
replace_once(
    "tests/scenarios/test_join_invariant.py",
    '''\n\ndef test_conflict_emits_bounded_structured_envelope(tmp_path: Path) -> None:
''',
    join_test + '''\n\ndef test_conflict_emits_bounded_structured_envelope(tmp_path: Path) -> None:
''',
    label="private integration scenario",
)
