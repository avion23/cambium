#!/usr/bin/env bash
#
# Cambium eval isolation driver (implementation-plan item 3).
#
# The eval suite must never run from inside the mutable worktree an agent
# edits.  This driver snapshots the *committed* state of a repository with
# `git clone --shared` (working-tree tampering cannot reach a clone), makes
# the copy read-only, and runs the requested eval from the copy with
# PYTHONPATH pointing at the copy's src and the selected interpreter.
# Nothing is ever written into the source repository: the source HEAD is
# captured before the run and asserted unchanged afterwards.
#
# Read-only mechanism (preferred first):
#   - `sudo mount --bind` + `remount,ro,bind` when passwordless sudo permits.
#     Kernel-enforced EROFS keeps file modes intact, so `shutil.copytree`
#     scratch copies the module tests make under /tmp stay writable.
#   - fallback `chmod -R a-w`.  The module dataset trees
#     (`src/cambium/modules/*/datasets`) then get write restored, because
#     copytree propagates source modes and the suite mutates its /tmp scratch
#     copies; everything else in the copy stays read-only.
#
# Eval modes:
#   module-test  cambium module-conformance gate for every discovered module
#   bench        `cambium.bench gate` against the committed baseline anchors,
#                seeded into a bench-root that always lives under /tmp
#   pytest       full pytest suite (testpaths tests+src) from the copy
#   all          module-test + bench + pytest
#
# Usage: isolated_eval.sh [--repo PATH] [--python PATH] [--eval MODE] [--worktree PATH]
#                         [--bench-root PATH]
set -euo pipefail

REAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3.14}"
REPO="$REAL"
EVAL=all
WORKTREE=
BENCH_ROOT=

fail() { printf 'isolated-eval: FAIL: %s\n' "$*" >&2; exit 1; }
note() { printf 'isolated-eval: %s\n' "$*"; }

usage() {
    sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

CLEANUP_MOUNT=
cleanup() {
    if [ -n "$CLEANUP_MOUNT" ]; then
        (cd / && sudo umount "$CLEANUP_MOUNT" >/dev/null 2>&1) || true
    fi
}
trap cleanup EXIT

while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --python) PY="$2"; shift 2 ;;
        --eval) EVAL="$2"; shift 2 ;;
        --worktree) WORKTREE="$2"; shift 2 ;;
        --bench-root) BENCH_ROOT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done

case "$EVAL" in
    module-test|bench|pytest|all) ;;
    *) fail "unknown --eval mode: $EVAL (module-test|bench|pytest|all)" ;;
esac

[ -d "$REPO" ] || fail "--repo is not a directory: $REPO"
[ -d "$REPO/src/cambium" ] || fail "--repo has no src/cambium: $REPO"
command -v "$PY" >/dev/null 2>&1 || fail "interpreter missing: $PY"
if ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then
    fail "--repo is not a git checkout: $REPO"
fi
if [ -n "$BENCH_ROOT" ]; then
    case "$(readlink -f "$BENCH_ROOT")" in
        /tmp/*) ;;
        *) fail "--bench-root must live under /tmp (never the repo): $BENCH_ROOT" ;;
    esac
fi
if [ -n "$WORKTREE" ]; then
    [ -e "$WORKTREE/.git" ] || fail "--worktree is not a git checkout: $WORKTREE"
fi

TMP="$(mktemp -d /tmp/cambium-isolated-eval.XXXXXX)"
COPY="$TMP/repo"
RESULTS="$TMP/results.log"
: > "$RESULTS"

note "snapshotting committed state of $REPO -> $COPY"
git clone -q --shared "$REPO" "$COPY" || fail "git clone --shared of $REPO failed"
REPO_HEAD_BEFORE="$(git -C "$REPO" rev-parse HEAD)"
COPY_HEAD="$(git -C "$COPY" rev-parse HEAD)"
note "copy HEAD: $COPY_HEAD"

EVALROOT="$COPY"
RO_METHOD=chmod
if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    MOUNT="$TMP/repo-ro"
    mkdir -p "$MOUNT"
    if sudo mount --bind "$COPY" "$MOUNT" >/dev/null 2>&1 \
        && sudo mount -o remount,ro,bind "$MOUNT" >/dev/null 2>&1; then
        if touch "$MOUNT/.ro-probe" >/dev/null 2>&1; then
            # The mount is not actually read-only; drop it and fall back.
            (cd / && sudo umount "$MOUNT" >/dev/null 2>&1) || true
        else
            EVALROOT="$MOUNT"
            CLEANUP_MOUNT="$MOUNT"
            RO_METHOD=mount
        fi
    fi
fi
if [ "$RO_METHOD" = chmod ]; then
    chmod -R a-w "$COPY"
    # copytree mode propagation: the suite copies dataset fixtures into /tmp
    # scratch space and must mutate those copies, so the dataset trees keep
    # write permission.  Everything else in the copy stays read-only.
    for dataset_dir in "$COPY"/src/cambium/modules/*/datasets; do
        [ -d "$dataset_dir" ] && chmod -R u+w "$dataset_dir"
    done
fi
if touch "$EVALROOT/.ro-probe" >/dev/null 2>&1; then
    fail "isolated copy is not read-only: $EVALROOT"
fi
note "read-only enforcement: $RO_METHOD ($EVALROOT)"

FAIL_RC=0
run_eval() {
    local label="$1"
    shift
    note "eval: $label"
    printf '\n===== eval: %s (cwd %s) =====\n' "$label" "$EVALROOT" | tee -a "$RESULTS"
    set +e
    ( cd "$EVALROOT" && PYTHONPATH="$EVALROOT/src" "$PY" "$@" ) 2>&1 | tee -a "$RESULTS"
    rc=${PIPESTATUS[0]}
    set -e
    printf '===== eval exit code: %s =====\n' "$rc" | tee -a "$RESULTS"
    if [ "$rc" -ne 0 ]; then
        note "eval '$label' FAILED (exit $rc)"
        [ "$FAIL_RC" -eq 0 ] && FAIL_RC="$rc"
    else
        note "eval '$label' PASSED (exit 0)"
    fi
}

run_module_tests() {
    local modules
    modules="$(
        cd "$EVALROOT" && PYTHONPATH="$EVALROOT/src" "$PY" -c \
            'import cambium.module_conformance as m; print(" ".join(m.module_names()))'
    )" || fail "cannot discover modules in the isolated copy"
    [ -n "$modules" ] || fail "no modules discovered in the isolated copy"
    local name
    for name in $modules; do
        run_eval "module-test:$name" -m cambium.cli module-test "$name"
    done
}

run_bench() {
    local bench_root="$BENCH_ROOT"
    [ -n "$bench_root" ] || bench_root="$TMP/bench-root"
    mkdir -p "$bench_root"
    # Seed the committed baseline anchors so the gate compares live results
    # against the committed baselines; the anchors live under /tmp, never in
    # the repo or the read-only copy.
    local baseline module
    for baseline in "$EVALROOT"/src/cambium/modules/*/tests/baselines/baseline.json; do
        [ -f "$baseline" ] || continue
        module="$(
            cd "$EVALROOT" && PYTHONPATH="$EVALROOT/src" "$PY" -c \
                'import json, sys; print(json.load(open(sys.argv[1]))["module"])' "$baseline"
        )" || continue
        mkdir -p "$bench_root/$module"
        cp "$baseline" "$bench_root/$module/baseline.json"
    done
    run_eval "bench:gate" -m cambium.bench gate --bench-root "$bench_root"
}

case "$EVAL" in
    module-test) run_module_tests ;;
    bench) run_bench ;;
    pytest) run_eval "pytest" -m pytest ;;
    all)
        run_module_tests
        run_bench
        run_eval "pytest" -m pytest
        ;;
esac

# --- tamper-resistance verification (optional --worktree) --------------------
if [ -n "$WORKTREE" ]; then
    WT_HEAD="$(git -C "$WORKTREE" rev-parse HEAD)"
    note "worktree HEAD: $WT_HEAD; isolated copy HEAD: $COPY_HEAD"
    [ "$WT_HEAD" = "$COPY_HEAD" ] || fail "isolated copy HEAD differs from worktree HEAD; isolation broken"
    tamper_count=0
    verified=0
    untracked=0
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        tamper_count=$((tamper_count + 1))
        if git -C "$WORKTREE" cat-file -e "HEAD:$path" >/dev/null 2>&1; then
            expected="$(git -C "$WORKTREE" show "HEAD:$path" | sha256sum | cut -d' ' -f1)"
            actual="$(sha256sum "$EVALROOT/$path" | cut -d' ' -f1)"
            if [ "$expected" = "$actual" ]; then
                verified=$((verified + 1))
            else
                fail "tampered path $path differs between worktree HEAD and the isolated copy"
            fi
        else
            untracked=$((untracked + 1))
        fi
    done < <(git -C "$WORKTREE" diff --name-only HEAD -- src tests || true)
    if [ "$tamper_count" -eq 0 ]; then
        note "tamper-resistance: VERIFIED — no tracked working-tree changes under src/ or tests/ in $WORKTREE"
    else
        note "tamper-resistance: VERIFIED — $tamper_count tracked file(s) differ from HEAD in the \
worktree working tree; the isolated copy ran pristine committed content \
($verified files identical to HEAD, $untracked not part of the committed snapshot)"
    fi
fi

# --- source-repo integrity ----------------------------------------------------
REPO_HEAD_AFTER="$(git -C "$REPO" rev-parse HEAD)"
if [ "$REPO_HEAD_BEFORE" != "$REPO_HEAD_AFTER" ]; then
    fail "source repo HEAD changed during the run: $REPO_HEAD_BEFORE -> $REPO_HEAD_AFTER"
fi
note "source repo untouched: HEAD $REPO_HEAD_BEFORE before and after"

note "results file: $RESULTS"
note "isolated copy: $COPY"
exit "$FAIL_RC"
