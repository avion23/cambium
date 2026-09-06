# Operations

**Status:** current plan/admission/recovery/publication behavior. Exact CLI
arguments come from `cambium --help`; source owns current defaults.

## Static plans

A supervisor plan is JSON with one or more task specs. A minimal task names its
identity, objective, repository, worktree, and branch:

```json
{
  "tasks": [
    {
      "task_id": "api",
      "task": "Implement the API change",
      "repo": "/work/repo",
      "worktree_path": "/work/session/api",
      "branch": "cambium/api"
    }
  ]
}
```

Run it with:

```sh
cambium supervisor --session-dir /work/session --plan plan.json
```

Flat top-level plan entries are independent roots. Dynamic model delegation is a
separate parent/child tree; do not describe many flat roots as children of one
agent.

## Admission

Unpinned CLI/interactive runs use all enabled providers with usable stored
credentials. Admission filters hard constraints first, then applies provider
capacity/quota/debt preferences. Pin `--provider` or `--model` only when the
operator wants to override the normal pool.

An explicit empty provider allowlist is deny-all. Missing credential-feasible
providers fail before useful worker execution where possible. Provider
assignment and actual serving provider can differ after call-time fallback; use
usage events to establish where tokens were generated.

## Recovery

Worker generations own immutable checkpoints and a fenced worktree generation.
A restart resumes only from a compatible checkpoint/workspace identity. When
that identity no longer matches, recovery preserves salvage evidence instead of
pretending the old checkpoint still describes the tree.

Turn, wall, token, and restart budgets bound execution. Exhausting a budget
without a terminal finish verdict is incomplete. Provider retry/fallback is
owned by Diffundo and routing state, not by prompt prose.

## Publication

The worker can create at most one fenced commit for its generation. The
supervisor validates the worker result against the actual worktree/head before
publication. A clean read-only success can keep `HEAD == base` with no empty
commit.

Child semantic results and child Git artifacts are accepted separately. A parent
resumes from code-changing children only after its worktree matches the accepted
integration head. Completion order does not choose join order.

## Context and child suspension

One blocking exact child can suspend its parent while sharing the compatible
trunk/provider. Independent children should be proposed together so they can be
admitted concurrently; semantic children share the needed fold instead of
forcing one summary per child.

The structural tree defaults are:

```text
max child depth:        3
max children per parent: 8
```

These are `tasktree.MAX_DEPTH` and `tasktree.MAX_WIDTH`. Therefore a request such
as “one child for each of 20 commits” cannot become 20 direct children of one
parent. It must be chunked or expressed as several waves/roots. A flat static
plan may contain more top-level roots because they do not share one tree parent.

`--max-workers N` bounds simultaneous worker processes; zero/default means no
additional CLI process cap beyond the runtime/provider limits. Provider request
rate and provider in-flight capacity remain separate from worker-process count.

## Content/provider failures

Content flags, quota, timeout, transport failure, and refusal are distinct
provider outcomes. They may fall through to another eligible provider according
to Diffundo policy; they do not become successful task results merely because a
fallback path exists.

Summary failures preserve the previously accepted CAST checkpoint/raw evidence.
Do not invalidate valid code or replay completed work only to regenerate an
administrative marker.

## Verification

Use focused checks for the changed owner, then broader runtime tests when the
change crosses process/context/publication boundaries:

```sh
ruff check src tests
python -m pytest -o addopts='' tests/scenarios/<focused>.py -q
python -m pytest -o addopts='' -n 2 -m 'not acceptance' -q
```

Real CLI/TUI provider exercises live in `tests/acceptance/test_live_frontends.py`.
They consume configured provider quota and should be reported as observed runs,
including failures.
