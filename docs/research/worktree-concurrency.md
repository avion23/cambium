# Research: git worktree concurrency semantics

**Date:** 2026-08-09
**Environment:** git 2.43.0 (`arm-server-01`, aarch64), experiments run in throwaway repos under `/tmp/opencode/exp-wt` (NOT the worktree).
**Purpose:** verify the concurrency semantics that Cambium's **M3 Surculus** (worktree manager) and **M7 Unio** (merge sequencer, §7.8) rely on, per reviews DS-M1 / IMPL-M3 / IMPL-C1.
**Verification rule:** every claim cites the exact command + observed output (exit code where meaningful). Anything not reproduced is marked **UNVERIFIED**. Experiments are real concurrent runs (`&` + `wait`), not simulations.

Sources read before experimentation:
- `docs/architecture/system-design.md` — M3 Surculus (§M3), M7 Unio (§M7).
- `docs/architecture/reviews/review-distributed-systems.md` — DS-C5 (worktree locks), DS-M1 (merge serialization), IMPL-M3 (git worktree concurrency).
- `docs/architecture/reviews/review-implementation.md` — IMPL-C1 (merge sequencer no concurrency guard).
- `architecture.md` §7.5 (worktree recovery), §7.8 (atomic `refs/heads/main` update via `update-ref`).

---

## Methodology

All experiments ran in `/tmp/opencode/exp-wt` (a scratch area, deliberately NOT the worktree, which lives at `/tmp/opencode/cambium-wtexp`). Each experiment used a fresh throwaway repo (`git init -b main`), `gc.auto 0`, and only commits as needed. Scripts and raw logs: `/tmp/opencode/exp-wt/exp*.sh`, `/tmp/opencode/exp-wt/out/`. Refs used: `main`, `wt1..wt4`, `ur`, `probe3`.

Two harness bugs were found and fixed during the run and are reported here because they are themselves findings about git state:
1. Concurrent `--no-ff` merges leave `MERGE_HEAD` + a staged index behind; a harness that only moved the ref (`git update-ref`) between trials was polluted by the previous trial's leftovers (first-pass data discarded).
2. `git status`/`git diff`/`git log` are read-only and do NOT take `index.lock`; lock-blocking tests must use index-*writing* commands (`git add`, `git commit`, `git merge`).

---

## Experiment 1 — parallel worktree edits + concurrent merges into `main`

### 1a. Four worktrees committing in parallel (different files)

Setup: 4 worktrees on branches `wt1..wt4` from `main`; each wrote a distinct `file-N.txt`, then **all four ran `git add` + `git commit` concurrently** (`&` + `wait`).

```
parallel commit aggregate exit=0 (0=all succeeded)      # all 4 commits rc=0
refs/heads/wt1 e9b5a2d   wt2 53d78d7   wt3 a46d7c6   wt4 f99eb0a   main b3fa22e
git fsck --no-reflogs  →  (empty)
```

**Finding:** zero interference. Each worktree has its own index at `.git/worktrees/<id>/index` (its own lock), each branch update locks a distinct ref file, and object-DB writes are append-only. **The "parallel workers in separate worktrees" isolation claim is real.**

### 1b. Concurrent `git merge --ff-only` into `main` (40 trials, two divergent branches)

Both merges run simultaneously in the **same** checkout (the main repo):

| outcome | count |
|---|---|
| exactly one merge succeeds | 40/40 |
| both succeed | 0/40 |
| **lost updates (main missing a branch's file)** | **0/40** |
| `index.lock` error in the loser | 4/40 |

Loser failure modes observed (exact messages):

```
# most common (tree applied to index first, then ref check failed):
fatal: update_ref failed for ref 'HEAD': cannot lock ref 'HEAD':
  is at c861d7b1960c55632fc18da9afd80500845c0e4a but expected 090f2a87a1062666216e6af4dc93ffe665bf9c79
Updating 090f2a8..b957f81
Fast-forward

# genuine index.lock collision:
error: Unable to create '/tmp/opencode/exp-wt/repo/.git/index.lock': File exists.
Another git process seems to be running in this repository, e.g.
an editor opened by 'git commit'. ...

# or, if the loser started after main moved:
fatal: Not possible to fast-forward, aborting.
```

**Leftover state is the real hazard.** After a failed ff-only merge the loser's tree is left **staged in the shared checkout** even though the ref refused the update:

```
$ git status --porcelain
D  f1.txt        # winner's file staged for deletion
A  f2.txt        # loser's file staged
?? f1.txt        # winner's file now untracked in the worktree
```

### 1c. Concurrent `git merge --no-ff` into `main` (40 trials)

| outcome | count |
|---|---|
| exactly one merge succeeds | 38/40 |
| both succeed (serialized; main contains both files) | 2/40 |
| both fail | 0/40 |
| **lost updates** | **0/40** |
| `index.lock` error | 3/40 |
| leftover `MERGE_HEAD` + staged index after the race | 18/40 |

Loser fails loudly:

```
fatal: update_ref failed for ref 'HEAD': cannot lock ref 'HEAD':
  is at 0fe84b3b4f1d84bd911b3e214c6e4143270fb88f but expected b3fa22e27ed56aed9cda535a43834ffe24a2fade
```

and leaves `MERGE_HEAD` plus a staged, non-conflicted index that must be cleaned with `git merge --abort` + `git reset --hard`.

### 1d. Interpretation

- `git merge` passes the **expected old SHA** to its internal ref update, so a stale merge is rejected loudly (`is at X but expected Y`). **Silent HEAD corruption / lost update from two concurrent merges is a MYTH — 0/80 across both modes.**
- Concurrent merges in the **same checkout** are nonetheless a **real race**: `index.lock` collisions, `refs/heads/main.lock` contention, and — most dangerously — **leftover half-merged state** (`MERGE_HEAD`, staged loser-tree, dirty status) that poisons every subsequent git command in that repo until aborted/reset.
- This validates **IMPL-C1 / DS-M1**: the merge sequencer MUST be serialized (asyncio.Lock / single-consumer queue) and/or operate in a throwaway worktree so the shared checkout is never raced.

---

## Experiment 2 — ref-update atomicity (`git update-ref`)

### 2a. Stale ref lock → loud failure

```
$ echo "pid 424242" > .git/refs/heads/ur.lock
$ git update-ref refs/heads/ur <sha>
fatal: update_ref failed for ref 'refs/heads/ur': cannot lock ref 'refs/heads/ur':
  Unable to create '/tmp/.../exp2/.git/refs/heads/ur.lock': File exists.
Another git process seems to be running in this repository, e.g.
an editor opened by 'git commit'. ... remove the file manually to continue.
rc=128
```

### 2b. Expected-old mismatch (the §7.8 `NonFastForward` path)

```
$ git update-ref refs/heads/ur <new> <old>      # old is current  → rc=0
$ git update-ref refs/heads/ur <new> <stale>    # old is wrong    → rc=128
fatal: update_ref failed for ref 'refs/heads/ur': cannot lock ref 'refs/heads/ur':
  is at aa4c7fd... but expected 6db18e8...
```
The stale update fails loudly and the ref is unchanged. **This is exactly the mechanism §7.8 relies on.**

### 2c. Two concurrent `update-ref` calls, different values, **no** old-SHA arg (200 trials)

```
both-rc0(serialized, last-writer-wins)=200   at-least-one-failed=0   lockfile-errors=0
```

No lock collisions observed (the lockfile window is microseconds), but when the calls serialize, **the first value is silently overwritten by the second with no error**. Without the expected-old argument, `update-ref` gives **last-writer-wins, silent lost update**. UNVERIFIED: an actual lockfile collision in a real race (not reproducible in 200 trials); the deterministic pre-created lock in 2a proves the lock exists and fails loudly when contended.

### 2d. Two concurrent `update-ref` calls, **with** expected-old (200 trials)

```
both-rc0=0   at-least-one-failed(loud rejection)=200
```

100% of races end in exactly one success + one loud rejection. **The expected-old argument is what makes concurrent publishes safe** — this is why §7.8's `update-ref refs/heads/main <tip> <old>` is the correct primitive.

### 2e. `GIT_QUARANTINE_PATH` → ref updates forbidden

```
$ GIT_QUARANTINE_PATH=/tmp/quar git update-ref refs/heads/ur <sha>
fatal: update_ref failed for ref 'refs/heads/ur': ref updates forbidden inside quarantine environment
rc=128
```

**Real gotcha:** `update-ref` refuses to run inside a quarantine environment. Cambium does not set this var, but any code path that inherits a push-hook environment must `unset GIT_QUARANTINE_PATH` before calling `Unio.publish_merge`.

---

## Experiment 3 — `git worktree add/remove` during an active writer

### 3a. `git worktree add` while a commit is mid-flight in another worktree

A 2 s `pre-commit` hook widened the commit; `git worktree add -b wt-c` ran 0.5 s into it:

```
add rc=0 ; commit rc=0    (both succeed, no interference)
```

`git worktree add` does not touch the other worktree's index or branch ref.

### 3b. `git worktree remove` on a dirty worktree (no `--force`)

```
$ (cd ../wt-b && echo dirty > dirty.txt && git add dirty.txt && echo untracked > untracked.txt)
$ git worktree remove ../wt-b
fatal: '../wt-b' contains modified or untracked files, use --force to delete it   rc=128
```
Worktree and branch are preserved.

### 3c. `git worktree remove --force` on a dirty worktree

```
remove-force rc=0   ;  dir gone ; branch wt-b still exists
fsck --no-reflogs --unreachable → unreachable blob 6e5aa7c (the staged dirty.txt)
```

**Uncommitted changes are destroyed.** Only content that was `git add`-ed survives, as a **dangling blob** with no path context; unstaged modifications and untracked files are gone permanently. Surculus must never `--force`-remove a worktree that may hold uncommitted worker state without capturing it first.

### 3d. Locked worktree

```
$ git worktree lock ../wt-d
$ git worktree remove ../wt-d
fatal: cannot remove a locked working tree; use 'remove -f -f' to override or unlock first
$ git worktree remove --force ../wt-d     # STILL refused; needs -f -f
```

**Real:** `Surculus.remove(force=True)` (single `--force`) fails on a locked worktree. The `.git/worktrees/<id>/locked` file (from an interrupted operation or explicit lock) blocks removal.

### 3e/f. `git worktree remove --force` **mid-commit** in that worktree

```
remove-force rc=0
commit rc=128: fatal: could not open '.../worktrees/wt-f/COMMIT_EDITMSG': No such file or directory
branch wt-f EXISTS, still at the OLD commit; dangling tree/blob left in the object DB
```

**Real:** force-removing a worktree while a worker is mid-git-op kills the in-flight commit (its objects go dangling) but does not corrupt other worktrees or the shared refs.

---

## Experiment 4 — detached-HEAD commit reachability (codex-style)

Setup: `git worktree add --detach ../wt-dh main`, commit on the detached HEAD.

| phase | `git rev-list --all` | `git fsck` (dangling?) | `git log --all` |
|---|---|---|---|
| while worktree exists | contains the commit | **no** | shows it |
| after `worktree remove --force` | gone | **yes — dangling commit** | gone |

- While the worktree exists the commit is reachable: the worktree HEAD pseudo-ref counts as a ref for `--all`, and the worktree has its own reflog at `.git/worktrees/<id>/logs/HEAD`.
- Removing the worktree deletes the admin dir **and the reflog**; the commit becomes a dangling commit.
- `git gc` (default `prune.expire = 2.weeks.ago`) keeps it; `git gc --prune=now` **deletes it permanently** (`git cat-file -t <sha>` → `fatal: could not get object info`).

**Design implication:** codex-style detached-HEAD worker commits are only safely recoverable **while the worktree exists**. The merge sequencer must capture the worker's tip SHA and make it reachable (e.g. `update-ref` into a ref, or a real branch) **before** removing the worktree; otherwise the commit is dangling and gc-eligible.

---

## Experiment 5 — lockfiles

### 5a. Where `index.lock` lives, and what it blocks

- Main repo: `<repo>/.git/index.lock`
- Linked worktree: `<repo>/.git/worktrees/<id>/index.lock`

```
$ echo "pid 111" > .git/index.lock
$ git add -A
fatal: Unable to create '/tmp/.../exp5/.git/index.lock': File exists.
Another git process seems to be running in this repository, e.g.
an editor opened by 'git commit'. ... remove the file manually to continue.

$ git status / git diff / git log   → rc=0   # read-only: NO index.lock taken
```

**Read-only commands never take `index.lock`**; only index-writing commands (`add`, `commit`, `merge`, `checkout`, `stash`, `reset`) do. Parallel workers doing `git status`/`git diff`/`git log` cannot collide on the index lock.

### 5b/c. Does `git worktree add` respect an existing `index.lock`?

No. With `<repo>/.git/index.lock` pre-created, `git worktree add -b wt-b ../wt-b main` succeeded (rc=0). `worktree add` also succeeded during a mid-flight commit in another worktree (3a). It never writes the index.

### 5d. Stale-lock recovery

- **`index.lock`: no age-based recovery exists.** A 7 h-old `index.lock` still blocks `git add`; `git gc` runs fine and **leaves `index.lock` in place**. Only manual removal works — git's own message says so ("remove the file manually to continue"). This is the basis for `Surculus.recover()` step 1 (remove `*.lock` before respawn).
- **`gc.pid`: age-based expiry exists, hardcoded to 12 hours in git 2.43.0** (source: `builtin/gc.c`, `lock_repo_for_gc()`, `time(NULL) - st.st_mtime <= 12 * 3600`). The file format is `<pid> <hostname>`:
  - correct format + live pid + fresh mtime → `fatal: gc is already running on machine 'arm-server-01' pid 1789213 (use --force if not)` (rc=128)
  - dead pid, **or** mtime > 12 h → lock broken automatically, gc proceeds
  - `gc.pid.lock` (the lockfile git itself holds while writing `gc.pid`) → `fatal: Unable to create '.../gc.pid.lock': File exists.`
  - note: `gc.lockExpiry` as a **config** key does not exist in 2.43.0 (added in a later git); the review text citing it is anachronistic, the behavior is real with a hardcoded 12 h.

### 5e. Stale ref lock blocks commits

```
$ echo "pid 777" > .git/refs/heads/main.lock
$ git commit --allow-empty -m test
fatal: cannot lock ref 'HEAD': Unable to create '.../refs/heads/main.lock': File exists.   rc=128
```

---

## Experiment 6 — verifying the §7.8 architecture claims

Setup: `main` at `M0`; worker worktree `wt1` with 2 commits; throwaway worktree `merge-staging`.

### 6a. "Throwaway worktree, never mutate main from worker code" — CONFIRMED

- Worker commits only advance `refs/heads/wt1`; `main` stayed at `M0` (checked after each worker commit).
- `git merge --ff-only wt1` executed **inside the throwaway worktree** advanced only `refs/heads/merge-staging`; `main` stayed at `M0` throughout verify.

**Correction discovered:** `git rebase main wt1` fails when `wt1` is checked out in the worker's worktree:

```
fatal: 'wt1' is already used by worktree at '/tmp/opencode/exp-wt/wt1'    rc=128
```

The sequencer must copy the worker tip to a **local staging branch inside the throwaway worktree** and rebase *that* (`git branch -f src <worker-tip> && git checkout src && git rebase main`), not rebase the worker's branch ref directly. The v0.1 M7 sample (`git rebase main <branch>` from the main repo) would fail for every branch that is checked out in a worktree.

### 6b/c. "Atomic fast-forward of `refs/heads/main`" via `update-ref` — CONFIRMED

```
$ git update-ref refs/heads/main <staging-tip> <M0>      # publish rc=0, main now == tip
$ git update-ref refs/heads/main <tip> <M0>              # stale old (main moved)
fatal: update_ref failed for ref 'refs/heads/main': cannot lock ref 'refs/heads/main':
  is at c81e6ea... but expected c943110...                # rc=128, main unchanged
```

### 6d. Atomicity mechanism (strace of `git update-ref`)

```
openat(AT_FDCWD, ".../refs/heads/probe3.lock", O_RDWR|O_CREAT|O_EXCL, 0666)   # lockfile (O_EXCL)
openat(AT_FDCWD, ".../refs/heads/probe3", O_RDONLY)                            # read old (for old check)
openat(AT_FDCWD, ".../logs/refs/heads/probe3", O_WRONLY|O_CREAT|O_APPEND)     # reflog append
renameat(AT_FDCWD, ".../refs/heads/probe3.lock", AT_FDCWD, ".../refs/heads/probe3")  # atomic swap
```

Exactly the mechanism §7.8 describes. Crash before the `renameat` leaves `probe3.lock` and `probe3` unchanged; crash after leaves `probe3` at the new SHA. **No torn state.** A simulated crash (stale `main.lock` left behind) confirmed: `main` unchanged and the next publish fails loudly (5e).

### 6e. Ref-only publish leaves the main checkout STALE — real, but by design

After publishing, a `git status` in the main repo showed `D f1.txt` (the ref moved but the main checkout's index/worktree still reflect the old `main`). §7.8 states the working tree "is updated by the host system or a separate Cambium command, never automatically" — confirmed. A host that runs a persistent `main` checkout must refresh it explicitly; it will otherwise report dirty state.

---

## Findings table

| # | Claim / concern | Verdict | Reproduction |
|---|---|---|---|
| F1 | Parallel commits in separate worktrees interfere | **MYTH** | Exp 1a: 4/4 commits rc=0, fsck clean |
| F2 | Concurrent merges silently corrupt HEAD / lose updates | **MYTH** | Exp 1b/1c: 0 lost updates in 80 trials; stale merges rejected `is at X but expected Y` |
| F3 | Concurrent merges race on the shared checkout | **REAL** | Exp 1b/1c: 7/80 `index.lock` errors; 18/40 `MERGE_HEAD`+staged-index leftovers; loser's tree staged after failed ff |
| F4 | `update-ref` without old-SHA = silent last-writer-wins | **REAL** | Exp 2c: 200/200 serialized, no detection; with old-SHA 200/200 loud rejection (2d) |
| F5 | `update-ref` under `GIT_QUARANTINE` fails | **REAL** | Exp 2e: `ref updates forbidden inside quarantine environment` |
| F6 | `worktree add` during active commit fails | **MYTH** | Exp 3a/5c: add rc=0, commit rc=0 |
| F7 | `worktree remove` (no force) on dirty tree destroys work | **MYTH** | Exp 3b: refused rc=128, tree+branch preserved |
| F8 | `worktree remove --force` on dirty tree destroys uncommitted work | **REAL** | Exp 3c: dir gone, only staged blobs left dangling |
| F9 | `worktree remove --force` mid-commit kills the commit | **REAL** | Exp 3f: commit rc=128, branch stuck at old tip, objects dangling |
| F10 | Locked worktree removal (`--force`) works | **MYTH** | Exp 3d: refuses even with `--force`, needs `-f -f` |
| F11 | Detached-HEAD commit survives worktree removal | **MYTH** | Exp 4: becomes dangling; `gc --prune=now` deletes it |
| F12 | Read-only git ops take `index.lock` | **MYTH** | Exp 5a: status/diff/log rc=0 with lock present |
| F13 | Per-worktree index isolation (separate `index.lock`) | **REAL** | Exp 5a: `.git/worktrees/<id>/index.lock`; 1a no collisions |
| F14 | git auto-recovers stale `index.lock` by age | **MYTH** | Exp 5d: 7 h-old lock still blocks; gc leaves it |
| F15 | git auto-recovers stale `gc.pid` by age | **REAL** | Exp 5d: 12 h hardcoded expiry in 2.43.0; stale lock broken |
| F16 | `update-ref` is atomic (lockfile + rename) | **REAL** | Exp 6d strace: `O_EXCL` lock + `renameat` |
| F17 | ff-merge-in-throwaway-worktree leaves `main` untouched | **REAL** | Exp 6a |
| F18 | Rebase of a branch checked out in another worktree works | **MYTH** | Exp 6a: `fatal: 'wt1' is already used by worktree at ...` |
| F19 | Ref-only publish leaves main checkout stale/dirty | **REAL** (by design) | Exp 6e: `git status` → `D f1.txt` |

---

## Conclusion — real pitfalls vs myths, and design implications for Unio

**Myths (do not engineer around them):**
- Workers committing in different worktrees never collide on `index.lock` or branch refs (each worktree owns its index and its ref lock).
- Concurrent merges do **not** silently corrupt `refs/heads/main` — git's ref update carries the expected old SHA and rejects stale merges loudly.
- `git worktree add` is safe during other worktrees' writes; read-only git commands never take `index.lock`; stale `index.lock` is *not* auto-recovered by age (needs explicit cleanup).

**Real pitfalls (must engineer around them):**
1. **Concurrent merges in the same checkout** (the IMPL-C1 scenario): `index.lock` errors and, worse, **leftover half-merged state** (`MERGE_HEAD`, staged loser-tree, dirty status) that poisons the repo until `git merge --abort` + `reset --hard`. → serialize Unio (asyncio.Lock / single-consumer queue) **and** do all merging in a throwaway worktree; never merge in the shared checkout.
2. **Detached-HEAD worker commits vanish on worktree removal.** → capture the tip SHA and make it reachable (a ref) before `worktree remove`; never rely on `fsck` recovery.
3. **`update-ref` semantics are the load-bearing primitive.** Without the expected-old argument a concurrent publish is a silent lost update; with it, a concurrent `main` move (including a human push) is a loud `NonFastForward`. §7.8's `update-ref refs/heads/main <verified_tip> <old_sha>` is **correct and required**; the crash story (lockfile+rename, no torn state, stale `main.lock` detected loudly) is confirmed.

**Additional implications for the sequencer and Surculus:**
- Rebase **local staging branches** inside the throwaway worktree, not the worker's branch ref (F18).
- `Surculus.recover()` must remove `index.lock`/ref `.lock` files (no git auto-recovery, F14), abort `MERGE_HEAD`/rebase state (F3 leftovers), and reset to base — exactly §7.5.
- Set `gc.auto 0` (as the reviews recommend) so a background `git gc` never trips the 12 h `gc.pid` lock against a worker's commit; and `unset GIT_QUARANTINE_PATH` before `update-ref` if env could carry it (F5).
- Never `--force`-remove a worktree with uncommitted worker state (F8) or mid-git-op (F9); locked worktrees need `-f -f` (F10). Capture-then-remove is the only safe ordering.
