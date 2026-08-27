# Operations

## PLAN MODE

Plan file `P` is JSON; minimal plan. (src/cambium/supervisor.py:8558-8563,8586-8601)

```json
{"tasks": [
  {"task_id": "api", "task": "Implement the API change", "repo": "/work/repo",
   "worktree_path": "/work/S/api", "branch": "cambium/api",
   "requires_commit": true, "max_restarts": 1}
]}
```

`task_id`, `task`, `repo`, `worktree_path`, and `branch` are checked at plan admission;
`requires_commit` is sent to the worker and `max_restarts` is read by the supervisor.
(src/cambium/supervisor.py:7369-7388,3200-3221,4414-4419)

```sh
PYTHONPATH=src python -m cambium supervisor --session-dir S --plan P
```

The CLI requires `--session-dir` and one of `--plan`, `--task-spec`, or `--demo`, then delegates to
`supervisor.main`, which loads and runs `P`.
(src/cambium/cli.py:198-211,707-725; src/cambium/supervisor.py:8554-8611)

`N` independent flat-plan entries create `N` concurrent trees in one `TaskGroup`. (src/cambium/supervisor.py:8290-8292,8421-8426)

## ADMISSION

For an unpinned `model_candidates` task, admission intersects authorized provider
identities with enabled providers whose API-key or OAuth credential is locally ready.
(src/cambium/supervisor.py:1160-1186,7512-7567)

Credential-infeasible providers are recorded and emitted as `provider_infeasible`;
an empty feasible set raises `NoCredentialFeasibleProvidersError` and becomes a
failed task, without starting its worker. (src/cambium/supervisor.py:7568-7585,4045-4062,4311-4319)

An explicit empty `authorized_providers` list is deny-all, not “use every
configured provider”: the worker raises and returns
`authorized_providers explicitly empty`. (src/cambium/supervisor.py:7402-7412;
src/cambium/worker.py:1034-1053,5541-5552)

## STALL/RESTART LIFECYCLE

The worker calls repeated or empty action signatures stalled after the configured
no-progress threshold and returns an `agent made no progress` failure.
(src/cambium/worker.py:2817-2844,4378-4395,5146-5147)

Each Diffundo provider attempt gets the smaller of the call deadline and its
effort-aware deadline; `reasoning_effort: max` multiplies the base by `2.0`.
(src/cambium/diffundo.py:164,695-697,2007-2057)

One-shot plans materialize `max_restarts: 1` when unset; explicit values, including zero,
remain explicit. (src/cambium/oneshot.py:800-821)

A restart-eligible failed generation consumes restart budget, emits `restart_scheduled`,
sleeps with bounded jitter, starts a fresh process, and receives a fresh wall window.
(src/cambium/supervisor.py:4542-4547,4955-5002)

Every ordinary worker checkpoint records the tracked-workspace
`workspace_hash`; the worker rejects resume if the current hash differs.
(src/cambium/worker.py:3085-3108,3920-4003,4717-4724)

The supervisor resumes only when its newest valid checkpoint hash matches, then
advances only the generation fence instead of resetting the worktree.
(src/cambium/supervisor.py:2818-2888,5003-5008)

On a mismatch, resume is abandoned: recovery captures
`salvage/<task>/<gen>/workspace.diff` and `salvage.json`, emits
`worktree_salvaged`, then resets to `base_commit` and cleans the tree.
(src/cambium/supervisor.py:2758-2816,2860-2869,2940-2977,5003-5017)

## SUCCESS INVARIANT

The worker finalizer stages non-`.cambium` changes, makes at most one fenced commit,
and reports `requires_commit`; the envelope repeats that boolean.
(src/cambium/worker.py:5630-5658,5781-5841,6002-6050)

The supervisor requires a boolean `requires_commit` and cross-checks commits,
files, diff, base, and actual `HEAD` before entering merge.
(src/cambium/supervisor.py:1998-2019,4757-4873)

A reported success with a dirty worker tree fails integrity before merge, and
normal cleanup retains it with `worktree_cleanup_deferred` rather than deleting
it. (src/cambium/supervisor.py:6043-6070,4757-4804,2979-3103)

A verified clean no-op is accepted only with `requires_commit=false`: the
worker reports no commit, and the supervisor accepts `HEAD == base_commit`.
(src/cambium/worker.py:5744-5780;
src/cambium/supervisor.py:2005-2019,4848-4872)

## CONTENT-FLAG RECOVERY

`CONTENT_FLAGGED` is request-level fall-through: Diffundo moves to the normal
cascade without changing provider health or spending retry backoff.
(src/cambium/diffundo.py:311-329,1994-2005,2058-2107,2676-2763)

A moderation/content-flagged summary gets one retry with a transformed tail; the second flag fails summary compaction. (src/cambium/worker.py:4477-4540)

Worker provider failure strings append the parseable `(content_flagged)`
suffix when the outcome is content-flagged. (src/cambium/worker.py:2760-2778,4983-4993)

## CAPACITIES

The structural defaults are `MAX_WIDTH=8` and `MAX_DEPTH=3`; `build_tree`
enforces per-parent fan-out `<=8` and depth `<=3`. Architectus also defaults
its in-flight `max_width` to `8`; supervisor hierarchy waves resolve to the
same default. (src/cambium/tasktree.py:44-50,245-269;
src/cambium/architectus.py:288-311,544-648;
src/cambium/supervisor.py:8071-8110)

The admission semaphore bounds live worker processes; parallel dispatch is
unlimited by default, while `--max-workers N` opts into an explicit cap.
(`max_concurrent_tasks=0` disables the semaphore.) (src/cambium/supervisor.py:2146-2150,4376-4402,8253-8254)

**Can it start 50 subagents?** Yes: 50 flat top-level plan entries are
configurable and all `N` entries are scheduled concurrently by default; no
source-level count cap is present. Passing `--max-workers N` caps simultaneous
processes, while hierarchical trees still obey fan-out `8`, depth `3`, and
wave/core width `8`. (src/cambium/supervisor.py:8249-8254,8322-8343,8421-8426;
src/cambium/tasktree.py:245-269; src/cambium/architectus.py:297-311)
