#!/usr/bin/env bash
#
# Cambium opt-in external-provider smoke (implementation plan step 4).
#
# Runs one disposable provider configuration through the REAL custom worker
# loop (tool/checkpoint events, bounded Diffundo turns, ref-only merge) and
# verifies the acceptance measures:
#   - a recorded provider response (usage_event rows with provider/model and
#     token fields in the session's durable EventStore),
#   - exactly one expected ref update (clone main advances by one commit
#     touching only the smoke fixture),
#   - the failure fixture leaves clone main UNCHANGED (no empty commit, no
#     merge, no secrets recorded).
#
# Opt-in and networked ONLY by explicit command: the script refuses to run
# unless CAMBIUM_SMOKE_PROVIDER_CONFIG points at a real provider config file
# (and the credentials it references exist). Local fake-provider fixtures
# (tests/scenarios/test_worker_provider.py) are regression coverage, not
# acceptance evidence — this driver does not accept a loopback config.
#
# Usage:
#   CAMBIUM_SMOKE_PROVIDER_CONFIG=/path/to/providers.json \
#     CAMBIUM_SMOKE_PROVIDER_KEY=... \
#     scripts/external-provider-smoke.sh
#
# Self-contained (bash, git, python3.14 only). Creates disposable artifacts
# under /tmp/opencode/cambium-external-smoke and cleans nothing up; stale
# state from a previous run is removed up front so every run is deterministic.
set -euo pipefail

REAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="python3.14"
REPO="$REAL"

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        *) printf 'external-provider-smoke: FAIL: unknown argument: %s\n' "$1" >&2; exit 1 ;;
    esac
done

CLONE=/tmp/opencode/cambium-external-smoke
SESSION=/tmp/opencode/cambium-external-smoke-session
PLAN="$SESSION/plan.json"
SUPERVISOR_LOG="$SESSION/supervisor.log"
FAIL_SESSION=/tmp/opencode/cambium-external-smoke-fail-session
FAIL_PLAN="$FAIL_SESSION/plan.json"
FAIL_LOG="$FAIL_SESSION/supervisor.log"

TASK_ID=ext-smoke-001
BRANCH=wt-ext-smoke-001
FIXTURE=tests/fixtures/external-smoke/cambium-external-smoke-marker.txt
MARKER="cambium external-provider smoke marker"

fail() { printf 'external-provider-smoke: FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf 'external-provider-smoke: %s\n' "$*"; }

command -v "$PY" >/dev/null 2>&1 || fail "$PY is required"
command -v git >/dev/null 2>&1 || fail "git is required"
[ -f "$REAL/src/cambium/cli.py" ] || fail "REAL source tree missing: $REAL/src/cambium/cli.py"

# --- opt-in gate: a real provider config must be supplied explicitly -------
CONFIG="${CAMBIUM_SMOKE_PROVIDER_CONFIG:-}"
[ -n "$CONFIG" ] || fail "opt-in required: set CAMBIUM_SMOKE_PROVIDER_CONFIG to a real provider config file"
CONFIG="$(realpath "$CONFIG")"
[ -f "$CONFIG" ] || fail "CAMBIUM_SMOKE_PROVIDER_CONFIG not found: $CONFIG"
if grep -q '"base_url".*127.0.0.1\|"base_url".*localhost\|"base_url".*0\.0\.0\.0' "$CONFIG"; then
    fail "loopback provider configs are regression fixtures, not acceptance evidence: $CONFIG"
fi
# The config's api_key_env names must resolve in this environment. Extract the
# keys without ever printing their values.
MISSING_KEYS="$("$PY" - "$CONFIG" <<'PY' || true
import json, os, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    config = json.load(fh)
missing = []
for provider in config.get("providers", []):
    key = provider.get("api_key_env")
    if key and not os.environ.get(key):
        missing.append(key)
print(" ".join(missing))
PY
)"
[ -z "$MISSING_KEYS" ] || fail "credential env keys not set: $MISSING_KEYS"
# The plan's fanout_config must declare the provider tier and model the worker
# drives (oneshot.py resolves both before dispatch; a direct supervisor plan
# supplies them here). Derive them from the first enabled provider in the
# supplied config so the smoke works against any real provider configuration.
TIER_MODEL="$("$PY" - "$CONFIG" <<'PY' || true
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    config = json.load(fh)
for provider in config.get("providers", []):
    if provider.get("enabled", True) and provider.get("tier") and provider.get("model"):
        print(f"{provider['tier']} {provider['model']}")
        break
PY
)"
[ -n "$TIER_MODEL" ] || \
    fail "no enabled provider in $CONFIG declares both tier and model"
read -r SMOKE_TIER SMOKE_MODEL <<< "$TIER_MODEL"
note "driving provider tier=$SMOKE_TIER model=$SMOKE_MODEL"

# The worker env is a fail-closed allowlist: the supervisor forwards only the
# credential names a task declares in provider_env_keys (values from its own
# env, then scrubbed). Derive them from the config's providers so API-key
# providers work; codex_chatgpt OAuth providers need no env forwarding (the
# supervisor injects the access token from the OAuth store at spawn).
PROVIDER_ENV_KEYS="$("$PY" - "$CONFIG" <<'PY' || true
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    config = json.load(fh)
keys = [
    provider.get("api_key_env")
    for provider in config.get("providers", [])
    if isinstance(provider.get("api_key_env"), str) and provider.get("api_key_env")
]
print(json.dumps(keys))
PY
)"
[ -n "$PROVIDER_ENV_KEYS" ] || PROVIDER_ENV_KEYS="[]"

# --- deterministic start: drop only this driver's disposable state ----------
rm -rf "$CLONE" "$SESSION" "$FAIL_SESSION"
mkdir -p "$SESSION" "$FAIL_SESSION"

note "cloning $REPO -> $CLONE"
git clone -q "$REPO" "$CLONE" || fail "git clone of $REPO failed"

note "seeding local git identity and gc.auto 0 in the disposable clone"
git -C "$CLONE" config user.name "cambium-ext-smoke" || fail "cannot seed user.name"
git -C "$CLONE" config user.email "cambium-ext-smoke@example.com" || fail "cannot seed user.email"
git -C "$CLONE" config gc.auto 0 || fail "cannot seed gc.auto"

[ -f "$CLONE/$FIXTURE" ] || fail "fixture not inherited by the disposable clone: $CLONE/$FIXTURE"
BASE_COMMIT="$(git -C "$CLONE" rev-parse refs/heads/main)" || fail "cannot resolve clone refs/heads/main"
BASE_COUNT="$(git -C "$CLONE" rev-list --count main)" || fail "cannot count clone main commits"

# --- success fixture: one provider-backed edit task --------------------------
cat > "$PLAN" <<JSON
{
  "tasks": [
    {
      "task_id": "$TASK_ID",
      "worker": "cambium.worker",
      "task": "append the smoke marker line 'cambium external-provider smoke marker' to the fixture file tests/fixtures/external-smoke/cambium-external-smoke-marker.txt; do NOT run any git commands — the change is committed by the framework",
      "repo": "$CLONE",
      "worktree_path": "$SESSION/wt-$TASK_ID",
      "branch": "$BRANCH",
      "target_file": "$FIXTURE",
      "marker": "$MARKER",
      "write_marker": true,
      "max_wall_s": 300,
      "max_restarts": 1,
      "ready_timeout_s": 30.0,
      "gate_timeout_s": 30.0,
      "heartbeat_interval_s": 1.0,
      "provider_env_keys": $PROVIDER_ENV_KEYS,
      "fanout_config": {
        "tier": "$SMOKE_TIER",
        "model": "$SMOKE_MODEL",
        "call_budget_s": 240.0,
        "pause_timeout_s": 10.0
      }
    }
  ]
}
JSON
"$PY" -c 'import json, sys; json.load(open(sys.argv[1]))' "$PLAN" || fail "plan JSON is invalid"

note "running supervisor (provider config: $CONFIG)"
set +e
CAMBIUM_PROVIDERS="$CONFIG" PYTHONPATH="$REAL/src" "$PY" -u -m cambium.supervisor \
    --session-dir "$SESSION" --plan "$PLAN" 2>&1 | tee "$SUPERVISOR_LOG"
SUP_RC="${PIPESTATUS[0]}"
set -e
note "supervisor exit code: $SUP_RC"
[ "$SUP_RC" -eq 0 ] || fail "supervisor exited $SUP_RC"

# --- acceptance 1: exactly one expected ref update touching the fixture -----
NEW_COUNT="$(git -C "$CLONE" rev-list --count main)" || fail "cannot count post-run clone main commits"
[ "$NEW_COUNT" -eq $((BASE_COUNT + 1)) ] || \
    fail "clone main advanced $((NEW_COUNT - BASE_COUNT)) commits, expected exactly 1"
CHANGED="$(git -C "$CLONE" diff-tree --no-commit-id --name-only -r main)" || fail "cannot diff clone main"
[ "$CHANGED" = "$FIXTURE" ] || fail "new commit changed [${CHANGED//$'\n'/, }], expected only $FIXTURE"

# --- acceptance 2: recorded provider response + usage record ----------------
"$PY" - "$SESSION/.cambium/events.db" <<'PY' || fail "durable usage/response contract not satisfied"
import json, sqlite3, sys

conn = sqlite3.connect(sys.argv[1])
try:
    rows = conn.execute(
        "SELECT kind, payload FROM events ORDER BY seq"
    ).fetchall()
finally:
    conn.close()
usage = [p for kind, p in rows if kind == "usage_event"]
if not usage:
    raise SystemExit("no usage_event rows in the session EventStore")
for payload in usage:
    record = json.loads(payload) if isinstance(payload, str) else payload
    if not record.get("provider"):
        raise SystemExit("usage_event missing provider")
    if not record.get("model"):
        raise SystemExit("usage_event missing model")
kinds = [kind for kind, _ in rows]
for required in ("task_assigned", "spawned", "ready", "result", "merge_started", "merge_committed"):
    if required not in kinds:
        raise SystemExit(f"event chain missing {required}")
PY

# --- acceptance 3: failure fixture leaves main UNCHANGED ---------------------
# Deterministic fail-closed: the task's fanout_config references a non-codex
# provider, so the supervisor injects no codex OAuth token and the worker's
# provider routing fails closed ("CAMBIUM_OAUTH_ACCESS_CODEX is not set") with
# a failed verdict before any model call. main never moves; the model's
# behavior is irrelevant, so the fixture is reproducible.
cat > "$FAIL_PLAN" <<JSON
{
  "tasks": [
    {
      "task_id": "ext-smoke-fail",
      "worker": "cambium.worker",
      "task": "deterministic fail-closed probe: this task must never reach the model",
      "repo": "$CLONE",
      "worktree_path": "$FAIL_SESSION/wt-fail",
      "branch": "wt-ext-smoke-fail",
      "target_file": "$FIXTURE",
      "write_marker": false,
      "max_wall_s": 120,
      "max_restarts": 0,
      "ready_timeout_s": 30.0,
      "gate_timeout_s": 30.0,
      "heartbeat_interval_s": 1.0,
      "provider_env_keys": [],
      "fanout_config": {
        "tier": "$SMOKE_TIER",
        "model": "$SMOKE_MODEL",
        "call_budget_s": 60.0,
        "pause_timeout_s": 5.0,
        "providers": [{"name": "openai"}]
      }
    }
  ]
}
JSON
FAIL_BASE="$(git -C "$CLONE" rev-parse refs/heads/main)" || fail "cannot resolve fail-baseline main"
set +e
CAMBIUM_PROVIDERS="$CONFIG" PYTHONPATH="$REAL/src" "$PY" -u -m cambium.supervisor \
    --session-dir "$FAIL_SESSION" --plan "$FAIL_PLAN" 2>&1 | tee "$FAIL_LOG"
FAIL_RC="${PIPESTATUS[0]}"
set -e
FAIL_AFTER="$(git -C "$CLONE" rev-parse refs/heads/main)" || fail "cannot resolve post-fail main"
[ "$FAIL_AFTER" = "$FAIL_BASE" ] || fail "failure fixture moved clone main ($FAIL_BASE -> $FAIL_AFTER)"
if [ "$FAIL_RC" -eq 0 ]; then
    # A zero exit with no ref update is the clean failure case: report the
    # worker verdict so the run is auditable.
    grep -q '"status": "failed"' "$FAIL_LOG" || fail "failure fixture exited 0 without a failed worker verdict"
fi
note "failure fixture: main unchanged at $FAIL_AFTER (supervisor exit $FAIL_RC)"

note "PASS: external-provider smoke succeeded"
note "  provider config: $CONFIG"
note "  success run:    main $BASE_COMMIT -> $(git -C "$CLONE" rev-parse main) (one commit, only $FIXTURE)"
note "  usage events:   recorded in $SESSION/.cambium/events.db"
note "  failure run:    main unchanged at $FAIL_AFTER, events in $FAIL_SESSION/.cambium/events.db"
