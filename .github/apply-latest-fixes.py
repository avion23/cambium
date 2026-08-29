from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0 and new in text:
        return
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new), encoding="utf-8")


def replace_between(path: str, start: str, end: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    begin = text.index(start)
    finish = text.index(end, begin)
    target.write_text(text[:begin] + new + text[finish:], encoding="utf-8")


replace_once(
    "src/cambium/oneshot.py",
    '''    current_branch = _git_stdout(target_repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current_branch is None or _git_stdout(
        target_repo, "rev-parse", "--verify", f"refs/heads/{current_branch}"
    ) is None:
        raise ValueError(f"one-shot repository has no checked-out branch: {target_repo}")
''',
    '''    if _git_stdout(target_repo, "rev-parse", "--verify", "refs/heads/main") is None:
        raise ValueError(f"one-shot repository has no refs/heads/main: {target_repo}")
''',
)
replace_once(
    "src/cambium/oneshot.py",
    '''    base_commit = config.base_commit
    if base_commit is None:
        base_commit = _git_stdout(target_repo, "rev-parse", "--verify", "HEAD^{commit}")
    if base_commit is not None:
        spec["base_commit"] = base_commit
''',
    '''    base_commit = config.base_commit
    if base_commit is None:
        base_commit = _git_stdout(
            target_repo, "rev-parse", "--verify", "refs/heads/main^{commit}"
        )
    if base_commit is not None:
        spec["base_commit"] = base_commit
''',
)

replace_once(
    "src/cambium/worker.py",
    '''                if (
                    forced_finalization
                    and not code_changed
                    and not action.get("objective_met", False)
                ):
                    return _loop_result(
                        outcome,
                        "failed",
                        "forced finalization: investigation incomplete, no changes made",
                        turn,
                        cumulative_usage,
                        transcript,
                    )
''',
    '''                if not action["objective_met"]:
                    incomplete = {
                        **outcome,
                        "summary": action["summary"],
                        "terminal_action": _terminal_action_record(action),
                    }
                    reason = (
                        "forced finalization: investigation incomplete, no changes made"
                        if forced_finalization and not code_changed
                        else "finish declared objective_met=false"
                    )
                    return _loop_result(
                        incomplete,
                        "failed",
                        reason,
                        turn,
                        cumulative_usage,
                        transcript,
                    )
''',
)

replace_once(
    "src/cambium/schemas.py",
    '''        "description": (
            "Propose one scoped child workload for work you should not do yourself — a "
            "separable subproblem with its own files or investigation area. The child is a "
            "full Cambium worker in an isolated git worktree. IMPORTANT: this call only "
            "PROPOSES the child; it starts only after your task finishes, and you never see "
            "its output. Put everything the child needs in spec.task — it inherits only "
            "immutable summaries, not your conversation."
        ),
''',
    '''        "description": (
            "Propose one scoped child workload for work you should not do yourself — a "
            "separable subproblem with its own files or investigation area. The child is a "
            "full Cambium worker in an isolated git worktree. IMPORTANT: this call only "
            "PROPOSES the child. With context reuse, a successful proposal suspends this "
            "task; the supervisor may later resume it with a bounded child-result envelope. "
            "Without context reuse, child admission waits for this task's terminal boundary. "
            "Make spec.task self-contained. An exact compatible child may receive the "
            "immutable checkpoint prefix; otherwise it receives semantic summaries. It never "
            "receives sibling context or hidden reasoning."
        ),
''',
)

oneshot_test = Path("tests/scenarios/test_oneshot_branch.py")
text = oneshot_test.read_text(encoding="utf-8")
text = text.replace("from typing import Any\n\n", "")
oneshot_test.write_text(text, encoding="utf-8")
replace_between(
    "tests/scenarios/test_oneshot_branch.py",
    "def test_non_main_checked_out_branch_is_used_as_one_shot_base(",
    "\n\n# --------------------------------------------------------------------------- #\n# --auto",
    '''def test_checked_out_feature_branch_still_uses_main_as_one_shot_base(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    main_base = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "refs/heads/main^{commit}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", "feature"],
        check=True,
        capture_output=True,
    )
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "feature.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "feature"],
        check=True,
        capture_output=True,
    )

    plan = oneshot.build_plan(
        oneshot.OneShotConfig(prompt="inspect the repository", repo=repo),
        repo,
        tmp_path / "session",
    )

    assert plan["tasks"][0]["base_commit"] == main_base


def test_repository_without_main_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-m", "feature"],
        check=True,
        capture_output=True,
    )

    with pytest.raises(ValueError, match="no refs/heads/main"):
        oneshot.preflight(oneshot.OneShotConfig(prompt="inspect the repository", repo=repo))
''',
)

replace_between(
    "tests/scenarios/test_delegate_schema.py",
    '    assert "IMPORTANT: this call only PROPOSES the child" in description\n',
    "\n\n    task_description",
    '''    assert "IMPORTANT: this call only PROPOSES the child" in description
    assert "With context reuse, a successful proposal suspends this task" in description
    assert "resume it with a bounded child-result envelope" in description
    assert "Without context reuse, child admission waits" in description
    assert "An exact compatible child may receive" in description
    assert "otherwise it receives semantic summaries" in description
    assert "sibling context or hidden reasoning" in description
    assert "you never see its output" not in description
''',
)

marker = "def test_forced_finish_without_code_change_but_objective_met_succeeds_review(\n"
addition = '''def test_non_forced_finish_with_objective_false_is_failed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = _make_worktree(repo)
    config = _agent_config(worktree, max_tokens=100)
    router = _ScriptedRouter(
        [
            '{"type":"finish","summary":"task remains incomplete",'
            '"objective_met":false}',
        ]
    )

    outcome = asyncio.run(_drive_loop(config, worktree, router))

    assert outcome["status"] == "failed"
    assert outcome["failure_reason"] == "finish declared objective_met=false"
    assert outcome["summary"] == "task remains incomplete"
    assert outcome["terminal_action"] == {
        "type": "finish",
        "objective_met": False,
        "summary_present": True,
        "summary": "task remains incomplete",
    }


'''
replace_once(
    "tests/scenarios/test_gap_premature_finish.py",
    marker,
    addition + marker,
)
