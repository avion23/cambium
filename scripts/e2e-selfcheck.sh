#!/usr/bin/env bash
#
# Cambium self-bootstrap end-to-end self-check (deterministic marker mode).
#
# Driver contract:
#   - builds a disposable clone of /home/ubuntu/cambium at
#     /tmp/opencode/cambium-e2e — the repository cambium is asked to work on
#   - seeds local git identity (user.name/user.email) and gc.auto 0 in the
#     clone (the supervisor never seeds identity in an existing repo; the
#     worker's fenced commit and the sequencer's rebase need it)
#   - writes a one-task plan (worker cambium.worker, deterministic marker
#     mode, no `gate` key, provider_env_keys [], max_wall_s 60,
#     max_restarts 0, worktree_path under the session dir) and runs
#     `python3.14 -m cambium.supervisor --session-dir "$SESSION" --plan
#     "$PLAN"` with PYTHONPATH pointing at this tree's src
#   - verifies: supervisor exit 0; clone main advanced by exactly one commit
#     touching only the e2e fixture with exactly one added line; worker
#     worktree pruned and branch deleted; .cambium/result.json
#     status=done exit_code=0 files_changed=[fixture]; and the durable event
#     kind chain contains task_assigned, spawned, init, ready, run_task,
#     result, exit, merge_started, merge_committed, worktree_pruned,
#     session_ended in order.
#
# Self-contained (bash, git, python3.14 only). Creates disposable artifacts
# under /tmp/opencode/cambium-e2e and cleans nothing up; stale state from a
# previous run is removed up front so every run is deterministic.
set -euo pipefail

REAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="python3.14"

CLONE=/tmp/opencode/cambium-e2e
SESSION=/tmp/opencode/cambium-e2e-session
PLAN="$SESSION/plan.json"
SUPERVISOR_LOG="$SESSION/supervisor.log"

TASK_ID=e2e-self-001
BRANCH=wt-e2e-self-001
FIXTURE=tests/fixtures/e2e/cambium-e2e-marker.txt
MARKER="cambium-e2e selfcheck marker"

fail() { printf 'e2e-selfcheck: FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf 'e2e-selfcheck: %s\n' "$*"; }

command -v "$PY" >/dev/null 2>&1 || fail "$PY is required"
command -v git >/dev/null 2>&1 || fail "git is required"
[ -f "$REAL/src/cambium/cli.py" ] || fail "REAL source tree missing: $REAL/src/cambium/cli.py"

# Deterministic start: drop only this driver's disposable state.
rm -rf "$CLONE" "$SESSION"
mkdir -p "$SESSION"

note "cloning /home/ubuntu/cambium -> $CLONE"
git clone -q /home/ubuntu/cambium "$CLONE" || fail "git clone of /home/ubuntu/cambium failed"

note "seeding local git identity and gc.auto 0 in the disposable clone"
git -C "$CLONE" config user.name "cambium-e2e" || fail "cannot seed user.name"
git -C "$CLONE" config user.email "cambium-e2e@example.com" || fail "cannot seed user.email"
git -C "$CLONE" config gc.auto 0 || fail "cannot seed gc.auto"

[ -f "$CLONE/$FIXTURE" ] || fail "fixture not inherited by the disposable clone: $CLONE/$FIXTURE"
BASE_COMMIT="$(git -C "$CLONE" rev-parse refs/heads/main)" || fail "cannot resolve clone refs/heads/main"
BASE_COUNT="$(git -C "$CLONE" rev-list --count main)" || fail "cannot count clone main commits"

cat > "$PLAN" <<JSON
{
  "tasks": [
    {
      "task_id": "$TASK_ID",
      "worker": "cambium.worker",
      "task": "append the selfcheck marker to the e2e fixture and commit",
      "repo": "$CLONE",
      "worktree_path": "$SESSION/wt-$TASK_ID",
      "branch": "$BRANCH",
      "target_file": "$FIXTURE",
      "marker": "$MARKER",
      "write_marker": true,
      "max_wall_s": 60,
      "max_restarts": 0,
      "provider_env_keys": []
    }
  ]
}
JSON
"$PY" -c 'import json, sys; json.load(open(sys.argv[1]))' "$PLAN" || fail "plan JSON is invalid"

note "running supervisor (harness src: $REAL/src)"
set +e
PYTHONPATH="$REAL/src" "$PY" -u -m cambium.supervisor --session-dir "$SESSION" --plan "$PLAN" 2>&1 | tee "$SUPERVISOR_LOG"
SUP_RC="${PIPESTATUS[0]}"
set -e
note "supervisor exit code: $SUP_RC"
[ "$SUP_RC" -eq 0 ] || fail "supervisor exited $SUP_RC"

# --- clone main advanced by exactly one commit touching only the fixture ----
NEW_COUNT="$(git -C "$CLONE" rev-list --count main)" || fail "cannot count post-run clone main commits"
[ "$NEW_COUNT" -eq $((BASE_COUNT + 1)) ] || fail "clone main advanced $((NEW_COUNT - BASE_COUNT)) commits, expected exactly 1"

CHANGED="$(git -C "$CLONE" diff-tree --no-commit-id --name-only -r main)" || fail "cannot diff clone main"
[ "$CHANGED" = "$FIXTURE" ] || fail "new commit changed [${CHANGED//$'\n'/, }], expected only $FIXTURE"

NUMSTAT="$(git -C "$CLONE" diff-tree --no-commit-id --numstat -r main)" || fail "cannot numstat clone main"
[ "$NUMSTAT" = "$(printf '1\t0\t%s' "$FIXTURE")" ] || fail "new commit numstat [$NUMSTAT], expected exactly one added line"

# Publication is ref-only: the clone's checked-out working tree is never
# refreshed, so read the committed blob, not the stale working-tree file.
FIXTURE_BLOB="$(git -C "$CLONE" show "refs/heads/main:$FIXTURE")" || fail "cannot read committed fixture from clone main"
LINES="$(printf '%s\n' "$FIXTURE_BLOB" | wc -l)"
[ "$LINES" -eq 2 ] || fail "fixture has $LINES lines, expected 2"
printf '%s\n' "$FIXTURE_BLOB" | grep -qx "cambium-e2e fixture baseline" || fail "fixture baseline line missing"
printf '%s\n' "$FIXTURE_BLOB" | grep -qx "cambium-e2e selfcheck marker" || fail "fixture marker line missing"

# --- worker worktree pruned and branch deleted ------------------------------
if git -C "$CLONE" worktree list | grep -q "wt-e2e-self-001"; then
    fail "worker worktree is still registered in the clone"
fi
[ ! -e "$SESSION/wt-$TASK_ID" ] || fail "worker worktree directory still exists: $SESSION/wt-$TASK_ID"
if git -C "$CLONE" branch --list "$BRANCH" | grep -q .; then
    fail "worker branch $BRANCH still exists in the clone"
fi

# --- canonical result.json ---------------------------------------------------
[ -f "$SESSION/.cambium/result.json" ] || fail "result.json missing: $SESSION/.cambium/result.json"
"$PY" - "$SESSION/.cambium/result.json" "$FIXTURE" <<'PY' || fail "result.json contract not satisfied"
import json, sys

path, fixture = sys.argv[1], sys.argv[2]
with open(path) as fh:
    result = json.load(fh)
assert result.get("status") == "done", f"status={result.get('status')!r}"
assert result.get("exit_code") == 0, f"exit_code={result.get('exit_code')!r}"
assert result.get("files_changed") == [fixture], f"files_changed={result.get('files_changed')!r}"
PY

# --- durable event kind chain ------------------------------------------------
EVENT_KINDS="$("$PY" - "$SESSION/.cambium/events.db" <<'PY' || fail "cannot read events.db"
import sqlite3, sys

conn = sqlite3.connect(sys.argv[1])
try:
    kinds = [row[0] for row in conn.execute("SELECT kind FROM events ORDER BY seq")]
finally:
    conn.close()
print("\n".join(kinds))
PY
)"
REQUIRED_CHAIN="task_assigned spawned init ready run_task result merge_started merge_committed worktree_pruned session_ended"
REQUIRED_ALTERNATES="exit reuse_ready"
"$PY" - "$EVENT_KINDS" "$REQUIRED_CHAIN" "$REQUIRED_ALTERNATES" <<'PY' || fail "event kind chain missing required kinds in order"
import sys

chain = sys.argv[1].split()
required = sys.argv[2].split()
alternates = sys.argv[3].split()
i = 0
for kind in chain:
    if i >= len(required):
        break
    if kind == required[i]:
        i += 1
        continue
    # A pooled worker emits reuse_ready where an exited worker emits exit;
    # accept either terminal event between result and merge_started.
    if required[i] == "result" and kind in alternates:
        continue
if i != len(required):
    raise SystemExit(f"missing after match: {required[i:]}")
PY
note "observed event kind chain:"
printf '%s\n' "$EVENT_KINDS"

note "PASS: self-bootstrap e2e self-check succeeded (harness src $REAL/src, clone main $BASE_COMMIT -> $(git -C "$CLONE" rev-parse main))"
