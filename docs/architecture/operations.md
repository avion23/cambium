# Operations

## PLAN MODE

Plan file `P` is JSON; minimal plan. (src/cambium/supervisor.py:7920-7929,7971-8048)

```json
{"tasks": [
  {"task_id": "api", "task": "Implement the API change", "repo": "/work/repo",
   "worktree_path": "/work/S/api", "branch": "cambium/api",
   "requires_commit": true, "max_restarts": 1}
]}
```

`task_id`, `task`, `repo`, `worktree_path`, and `branch` are checked at plan admission;
`requires_commit` is sent to the worker and `max_restarts` is read by the supervisor.
(src/cambium/supervisor.py:8010-8015,3300-3301,4730)

```sh
PYTHONPATH=src python -m cambium supervisor --session-dir S --plan P
```

The CLI requires `--session-dir` and one of `--plan`, `--task-spec`, or `--demo`, then delegates to
`supervisor.main`, which loads and runs `P`.
(src/cambium/cli.py:203-216,711-725; src/cambium/supervisor.py:9207-9270)

`N` independent flat-plan entries create `N` concurrent trees in one `TaskGroup`. (src/cambium/supervisor.py:8785-8815,8941-8948)

## ADMISSION

For an unpinned `model_candidates` task, admission intersects authorized provider
identities with enabled providers whose API-key or OAuth credential is locally ready.
(src/cambium/supervisor.py:8140-8160)

Credential-infeasible providers are recorded and emitted as `provider_infeasible`;
an empty feasible set raises `NoCredentialFeasibleProvidersError` and becomes a
failed task, without starting its worker. (src/cambium/supervisor.py:8157-8184,4358-4375,4625-4632)

An explicit empty `authorized_providers` list is deny-all, not “use every
configured provider”: the worker raises and returns
`authorized_providers explicitly empty`. (src/cambium/supervisor.py:8010-8015;
src/cambium/worker.py:1090-1093,7426-7430)

## STALL/RESTART LIFECYCLE

The worker calls repeated or empty action signatures stalled after the configured
no-progress threshold and returns an `agent made no progress` failure.
(src/cambium/worker.py:3191-3221,4894-4908,6229-6239)

Each Diffundo provider attempt gets the smaller of the call deadline and its
effort-aware deadline; `reasoning_effort: max` multiplies the base by `2.0`.
(src/cambium/diffundo.py:167,784-786,2469-2474)

One-shot plans materialize `max_restarts: 1` when unset; explicit values, including zero,
remain explicit. (src/cambium/oneshot.py:920-922)

A restart-eligible failed generation consumes restart budget, emits `restart_scheduled`,
sleeps with bounded jitter, starts a fresh process, and receives a fresh wall window.
(src/cambium/supervisor.py:5265-5331)

Every ordinary worker checkpoint records the tracked-workspace
`workspace_hash`; the worker rejects resume if the current hash differs.
(src/cambium/worker.py:3605-3617,5615-5619)

The supervisor resumes only when its newest valid checkpoint hash matches, then
advances only the generation fence instead of resetting the worktree.
(src/cambium/supervisor.py:2936-2969)

On a mismatch, resume is abandoned: recovery captures
`salvage/<task>/<gen>/workspace.diff` and `salvage.json`, emits
`worktree_salvaged`, then resets to `base_commit` and cleans the tree.
(src/cambium/supervisor.py:2834-2904,3021-3065)

## SUCCESS INVARIANT

The worker finalizer stages non-`.cambium` changes, makes at most one fenced commit,
and reports `requires_commit`; the envelope repeats that boolean.
(src/cambium/worker.py:7544-7780)

The supervisor requires a boolean `requires_commit` and cross-checks commits,
files, diff, base, and actual `HEAD` before entering merge.
(src/cambium/supervisor.py:2037-2060,6645-6674)

A reported success with a dirty worker tree fails integrity before merge, and
normal cleanup retains it with `worktree_cleanup_deferred` rather than deleting
it. (src/cambium/supervisor.py:5071-5108,3066-3235)

A verified clean no-op is accepted only with `requires_commit=false`: the
worker reports no commit, and the supervisor accepts `HEAD == base_commit`.
(src/cambium/worker.py:7667-7701;
src/cambium/supervisor.py:2037-2060)

## CONTENT-FLAG RECOVERY

`CONTENT_FLAGGED` is request-level fall-through: Diffundo moves to the normal
cascade without changing provider health or spending retry backoff.
(src/cambium/diffundo.py:314-332,2400-2412,2475-2524,3137-3217)

A moderation/content-flagged summary gets one retry with a transformed tail; the second flag fails summary compaction. (src/cambium/worker.py:2658-2682,5218-5227)

Worker provider failure strings append the parseable `(content_flagged)`
suffix when the outcome is content-flagged. (src/cambium/worker.py:3109-3132)

## CAPACITIES

The structural defaults are `MAX_WIDTH=8` and `MAX_DEPTH=3`; `build_tree`
enforces per-parent fan-out `<=8` and depth `<=3`. Architectus also defaults
its in-flight `max_width` to `8`; supervisor hierarchy waves resolve to the
same default. (src/cambium/tasktree.py:44-50,245-269;
src/cambium/architectus.py:288-311,544-648;
src/cambium/supervisor.py:8717-8727)

The admission semaphore bounds live worker processes; parallel dispatch is
unlimited by default, while `--max-workers N` opts into an explicit cap.
(`max_concurrent_tasks=0` disables the semaphore.) (src/cambium/supervisor.py:2342-2345,8949-8955;
src/cambium/cli.py:229-234)

Continuous integration is the enforced validation path for pushes to `main` and pull requests: after installing the `dev` extra with `python -m pip install -e ".[dev]"`, it runs `ruff check`, `python -m pytest -m "not slow" -q`, and then `python -m pytest -m slow -q` as a separate step; acceptance-marked checks are credential-gated but not hermetic because local provider configuration/auth can make them issue live calls, while CI supplies no credentials.

**Can it start 50 subagents?** Yes: 50 flat top-level plan entries are
configurable and all `N` entries are scheduled concurrently by default; no
source-level count cap is present. Passing `--max-workers N` caps simultaneous
processes, while hierarchical trees still obey fan-out `8`, depth `3`, and
wave/core width `8`. (src/cambium/supervisor.py:8785-8815,8941-8951,8717-8727;
src/cambium/tasktree.py:245-269; src/cambium/architectus.py:297-311)
