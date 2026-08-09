# Benchmark Harness — objective measurement for Cambium scenario tests

Research date: 2026-08-09. Purpose: design the measurement layer that turns
Cambium scenario tests into an objective, repeatable evidence base — the seed
of **Ascensus** (M9, the DSPy optimization harness; `docs/architecture/system-design.md`
§M9). Every claim below is either a command output verified on this machine
(pytest 9.1.1, CPython 3.14.7, installed via `uv`) or a file reference.
Anything that could not be checked is marked **UNVERIFIED**.

**Current-main note (2026-08-09):** the scaffold-era six-test baseline in this
document is historical. Current main collects 108 tests and the full run reports
108 passed; the source ruff gate is clean.

## 1. Purpose

The harness measures four things for every Cambium module, and stores them so
later runs can be compared:

1. **Test durations** — wall time per scenario test, summarized as p50/p90.
   Catches perf regressions (a module that suddenly takes 10× the LLM budget)
   before they ship.
2. **Dataset health** — schema validity, duplicate/leak detection, class
   balance, canary presence and coverage. The anti-rot layer: a dataset that
   silently gained a bad record poisons every downstream eval.
3. **Metric baselines** — the module's own metric scored over the train/eval/
   canary records with the current decision logic. This is the number Ascensus
   hill-climbs against; without a recorded baseline, "the optimization
   improved things" is unprovable.
4. **Canary hit rates** — the **anti-reward-hacking evidence layer**. Canaries
   are planted to be misaligned with surface heuristics; an optimizer that
   memorizes train heuristics scores 1.0 on train but fails canaries. A
   recorded canary baseline makes a train-gain-with-canary-fail unmistakable.

The harness is **objective and repeatable**: the same command in the same
worktree must reproduce the same numbers to a stated tolerance. The stored
baseline JSON is the ground truth that drift checks compare against.

Deliverable shape: a small pytest plugin (`src/cambium/bench.py`) plus the
option of running it as a module (`python -m cambium.bench`). It uses **stdlib
+ pytest only** — no new runtime dependency. `pytest` itself is already the
scenario runner (`[project.optional-dependencies].test`).

## 2. Commands

### Baseline (today)

```sh
uv run --python 3.14.7 pytest -q
```

The current-main verification is `uv run --python 3.14.7 --extra test pytest
--collect-only -q` → `108 tests collected`; the full command reports 108 passed.
In a fresh checkout, pytest is declared
under the `test` extra, so the documented form is
`uv run --python 3.14.7 --extra test pytest -q` (matches `README.md`).

The earlier six-test output is retained only as the historical scaffold
baseline for this design.

### Proposed: bench harness

```sh
# pytest-plugin form (durations + dataset integrity + drift, one command)
uv run --python 3.14.7 --extra test pytest -q -p cambium.bench --bench=report

# CLI form (full report, same plugin code path)
uv run --python 3.14.7 --extra test python -m cambium.bench report
```

What the harness reports:

| Section | Content |
|---|---|
| Test times | per-test wall time; p50, p90, max across the run |
| Dataset integrity | per module: records, schema check, duplicate IDs, cross-split leaks, class balance, canary count + canary kinds present |
| Metric baseline | mean/std of the module metric on train, eval, canaries |
| Canary coverage | fraction of the dataset-format canary taxonomy covered; pass condition results |
| Drift check | compares the above against the stored baseline JSON; fails on regression > threshold |

The drift check exit code matters: **exit 1 on regression** so CI gates on it.

## 3. Baseline artifact format

### Location

Two choices were weighed:

- `src/cambium/modules/<name>/tests/baselines/` — committed next to each module,
  versioned with the repo, and reviewable in PRs.
  **Chosen for the committed reference** baselines: they are source of truth
  and must survive machine changes.
- `.cambium/` — already in `.gitignore`, machine-generated, unbounded growth.

Split: the **committed reference** lives in
`src/cambium/modules/<name>/tests/baselines/baseline.json` (one file per module).
Run artifacts and ephemeral drift reports
are written to `.cambium/baselines/` (gitignored). The committed file is the
anchor for the drift gate; the `.cambium/` copies are forensic history.

### JSON schema (`src/cambium/modules/<name>/tests/baselines/baseline.json`)

```jsonc
{
  "schema_version": 1,                  // format version of THIS file
  "module": "should_decompose",         // module name, must equal Module.name
  "dataset_version": null,              // null in v2 (see §3 "Versioning")
  "git_sha": "191543e4c587f4481e060c1fcf0373d6bcd23db6",  // full sha of the run
  "date": "2026-08-09T21:00:00Z",       // ISO 8601 UTC
  "python": "3.14.7",
  "pytest": "9.1.1",                    // plugin runs under a real pytest
  "metric": {                           // means over the scored split
    "train":   {"mean": 1.0, "std": 0.0, "count": 4},
    "eval":    {"mean": 1.0, "std": 0.0, "count": 3},
    "canaries":{"mean": 1.0, "std": 0.0, "count": 2}
  },
  "canaries": {
    "total": 2,
    "kinds_present": ["trivially_atomic", "must_decompose"],
    "taxonomy_coverage": 0.4,           // 2 of 5 dataset-format kinds
    "failed": 0                         // canary pass conditions not met
  },
  "dataset": {
    "records": 9,
    "duplicate_ids": 0,
    "cross_split_leaks": 0,
    "decompose_true": 4,                // class balance
    "decompose_false": 5,
    "canaries": 2
  },
  "tests": {
    "count": 108,                 // current-main collection; nodeids abbreviated
    "wall_seconds": {
      "p50": 0.004, "p90": 0.011, "max": 0.02
    },
    "by_nodeid": {
      "src/cambium/modules/example/tests/test_example_module.py::test_dataset_is_loadable_and_schema_valid": 0.004,
      "src/cambium/modules/example/tests/test_example_module.py::test_malformed_record_is_rejected": 0.011
    }
  },
  "drift_thresholds": {                 // defaults; configurable per module
    "metric_mean_delta": 0.05,          // absolute; regression only
    "wall_p90_ratio": 1.5,              // multiplicative vs baseline
    "canary_failed_delta": 0,           // any new failed canary is fatal
    "dataset": {"duplicate_ids": 0, "cross_split_leaks": 0}
  }
}
```

### Versioning

- `schema_version` (top level) is the format version of this file; bump only
  on a backward-incompatible schema change. Migration is a pure function like
  the dataset migrators in `dataset-format.md` §5.
- `dataset_version` keys the file: a dataset bump invalidates old metric
  baselines. The drift check compares **only against the last baseline with
  the same `dataset_version`**; when `dataset_version` changes, the harness
  records a new anchor instead of failing.
   - **Current main: `dataset_version` is populated from `datasets/meta.json`.**
     The example module ships the three split files plus `meta.json`; its
     `ExampleDatasetLoader` (`src/cambium/modules/example/dataset.py`) exposes
     `load_all()` and validates `input`/`expected`/`canary` per record. The
     legacy `example_pairs.jsonl` remains a loader fallback for the flat v2
     format.
   - **Future module target:** new modules populate `dataset_version` from
     `datasets/meta.json` (`src/cambium/modules/<name>/datasets/meta.json` per
     `dataset-format.md` §5), read through the load contract's
     `Dataset.dataset_version` (`dataset-format.md` §9).
- `git_sha` makes the recorded number attributable to a concrete tree.

## 4. What to run when

| Gate | When | Command | Contents | Fail on |
|---|---|---|---|---|
| **Pre-merge gate** | every PR, fast (<1 min) | `uv run --python 3.14.7 --extra test pytest -q -p cambium.bench --bench=gate` | scenario tests + dataset integrity + canary presence | any test failure, `DatasetError`, duplicate IDs, leaked records, missing canaries |
| **Nightly** | daily, full | `uv run --python 3.14.7 --extra test python -m cambium.bench report --full` | gate contents + metric baselines + canary hit rates + **DSPy hill-climb baseline** (train a candidate program, report train-gain vs canary-fail gap) | metric regression vs anchor > threshold; any canary failure |
| **Release** | on tag | `uv run --python 3.14.7 --extra test python -m cambium.bench report --full --drift-report` | nightly contents + drift report artifact written to `.cambium/baselines/` | regression + drift report exceeds thresholds |

The nightly "DSPy hill-climb baseline" is the Ascensus seed: it optimizes the
`should_decompose` module against `should_decompose_metric` on train, then
scores the optimized program on eval and canaries. The number that matters is
**train_gain − canary_gain**: a positive gap is the reward-hacking signature
the harness exists to surface. (DSPy is not yet a dependency; this stage is
documented and stubbed behind the `decide` seam until Ascensus lands.)

## 5. Implementation sketch

### Where it lives

`src/cambium/bench.py` — a single module exposing:

- `pytest_plugin.py`-style hooks (as a `pytest11` entry point via
  `[project.entry-points.pytest11]` in `pyproject.toml`, or loaded with
  `-p cambium.bench` — the `-p` form needs no packaging metadata).
- a `main()` guarded by `if __name__ == "__main__":` so
  `python -m cambium.bench` works without any new machinery.

### Why a pytest plugin (over a standalone CLI)

- Durations and pass/fail per test come from pytest's own report objects; a
  standalone CLI would have to re-run or re-parse tests.
- Dataset integrity and metric baselines are sidecars, not test failures: a
  plugin can *report* them without failing the run when a legit new module
  lacks a baseline yet.
- It composes with existing flags (`-q`, `-m`, `-k`, `-x`, `--durations`).

### Pytest hooks used (all verified in installed pytest 9.1.1)

`pytest_runtest_makereport` is the central timing/outcome hook. Verified at
`_pytest/hookspec.py:758` and implemented at `_pytest/runner.py:384`:

```python
@hookspec(firstresult=True)
def pytest_runtest_makereport(item: Item, call: CallInfo[None]) -> TestReport | None:
```

It fires once per setup/call/teardown phase. `CallInfo` carries `.duration`,
`.when`, `.start`, `.stop`, `.excinfo` (`_pytest/runner.py:293-330`).
`TestReport` carries `.nodeid`, `.when`, `.outcome`, `.duration`, `.passed`,
`.failed`, `.skipped` (type annotations at `_pytest/reports.py:62-69`; the
`passed`/`failed`/`skipped` properties at `:148-160`; instance fields set in
`__init__`: `nodeid` :338, `outcome` :352, `when` :358, `duration` :371). So
the plugin timestamps `report.when == "call"` per `report.nodeid`.

Full hook list used (all verified present in `_pytest/hookspec.py`):

| Hook | Use |
|---|---|
| `pytest_addoption(parser, pluginmanager)` (:97) | add `--bench=gate\|report` and threshold overrides |
| `pytest_configure(config)` (:138) | stash config, record git_sha/pytest version |
| `pytest_collection_finish(session)` (:290) | enumerate collected modules |
| `pytest_runtest_makereport(item, call)` (:758) | collect per-test wall time + outcome |
| `pytest_sessionfinish(session, exitstatus)` (:901) | compute stats, run integrity+metric+drift, write artifact, set exit code on regression |
| `pytest_terminal_summary(terminalreporter, exitstatus, config)` (:1100) | print the human-readable summary section |

### Plugin skeleton

```python
# src/cambium/bench.py (sketch — stdlib + pytest only)
from __future__ import annotations
import json, statistics, subprocess
from pathlib import Path

import pytest

DEFAULTS = {"metric_mean_delta": 0.05, "wall_p90_ratio": 1.5}

class Bench:
    """Registered as a plugin instance so hooks are bound methods."""

    def __init__(self) -> None:
        self.times: dict[str, float] = {}      # nodeid -> call-phase seconds
        self.results: list[dict] = []
        self.modules: set[str] = set()

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        if call.when == "call":
            self.times[item.nodeid] = call.duration

    def pytest_sessionfinish(self, session, exitstatus):
        if exitstatus != 0:
            return  # do not anchor a baseline on a red run
        report = compute_report(self, ...)     # times, integrity, metric, canaries
        drift = compare_against_anchor(report) # src/cambium/modules/<name>/tests/baselines/baseline.json
        write_artifact(report, ".cambium/baselines/")
        if drift.regressions:
            session.exitstatus = 1             # gate on regression

    def pytest_terminal_summary(self, terminalreporter, exitstatus, config):
        terminalreporter.section("cambium bench", yellow=True)
        ...

def pytest_addoption(parser):
    group = parser.getgroup("cambium-bench")
    group.addoption("--bench", choices=("gate", "report"), help="run cambium bench")

def pytest_configure(config):
    if config.getoption("bench") is not None:
        config.pluginmanager.register(Bench(), "cambium-bench")

def pytest_collection_finish(session):
    # discover modules from collected test nodeids' path prefix
    ...
```

### CLI form

```python
if __name__ == "__main__":
    # python -m cambium.bench report
    sys.exit(main(argv[1:]))
```

`main()` runs the same pure functions (dataset integrity, metric baseline,
canary coverage, drift compare) against all discovered modules, writing the
same JSON shape. It shares the compute core with the plugin; only the input
(durations come from a `--junitxml` run parsed with stdlib `xml.etree` when
invoked standalone, or are skipped for gate mode) differs. **UNVERIFIED**: the
`--junitxml` parse path is not yet implemented; the standalone CLI reuses
pytest's own report objects only in plugin mode.

### Canary coverage computation

Reuses the example module's loader (`ExampleDatasetLoader` from
`src/cambium/modules/example/dataset.py`), which validates each record and
flags `canary`. Coverage is measured against the canary taxonomy in
`docs/architecture/module-template/dataset-format.md` §6:

- `trivially_atomic`, `must_decompose`, `ambiguous_calibration`,
  `format_only_hack`, `keyword_hack`.

Algorithm per module:

```python
def canary_coverage(examples, taxonomy) -> dict:
    kinds = {k: 0 for k in taxonomy}
    for ex in examples:
        if ex.canary:
            kinds[classify(ex)] += 1        # classify from expected/data
    return {
        "total": sum(kinds.values()),
        "kinds_present": [k for k, n in kinds.items() if n],
        "taxonomy_coverage": len([k for k, n in kinds.items() if n]) / len(taxonomy),
    }
```

`taxonomy_coverage < 1` is **reported, not failed** in gate mode — a module may
legitimately need only some trap kinds. What is always failed: `total == 0`
(no canaries at all defeats the anti-reward-hacking layer). The current example
dataset uses the three split files (`train.jsonl`, `eval.jsonl`, and
`canaries.jsonl`) plus `meta.json`: 260 records total, with 200 train, 50 eval,
and 10 canaries. The legacy `example_pairs.jsonl` remains available as the
loader's flat-format fallback.

### Drift comparison

Pure function `compare_against_anchor(report, anchor) -> list[Regression]`:

- metric regression: `anchor.metric.X.mean − report.metric.X.mean > threshold`
  (only means falling is a regression).
- wall-time: `report.tests.wall_seconds.p90 > anchor * wall_p90_ratio`.
- dataset: any `duplicate_ids > 0`, `cross_split_leaks > 0`.
- canaries: `report.canaries.failed > anchor.canaries.failed`.

First regression → exit 1. All thresholds overridable via
`--bench-metric-delta`, etc., or per-module in the baseline file's
`drift_thresholds`.

## 6. Test scenarios for the harness itself

The harness is exercised by its own tests in
`tests/bench/test_bench.py` (uses pytest's own plugin hooks, no network):

1. **Timing capture** — run two trivial tests through
   `pytest_runtest_makereport` and assert `Bench.times[nodeid]` is populated
   for `when == "call"`, positive, and monotone in wall-clock. Uses a real
   pytest session via a fixture that invokes the plugin's hook wrapper
   directly with a fabricated `CallInfo` (verified: `CallInfo.duration` is a
   real attribute, `_pytest/runner.py:324`) — no subprocess needed.

2. **Percentile math** — feed a known duration list `[0.001]*99 + [1.0]` into
   the `p50/p90` summarizer and assert `p50 ≈ 0.001` and `p90 < 0.2` using
   `statistics.quantiles` (verified: p50 at index 49 and p90 at index 89 of
   `quantiles(data, n=100)`).

3. **Canary coverage** — build a 5-record dataset with 2 canaries of distinct
   kinds; assert `taxonomy_coverage == 2/5`, `kinds_present` is correct, and a
   zero-canary dataset yields `total == 0` with the gate flag set.

4. **Drift gate** — write a baseline JSON with `metric.train.mean == 1.0`,
   run `compare_against_anchor` against a report with `0.9`; assert a
   regression is returned and the exit-code policy maps it to failure.
   Negative case: equal means → no regression.

## 7. Open questions (recorded, not blocking)

- Whether nightly hill-climb should also run against `deepseek-v4-flash`
  (the free model noted in `system-design.md` §M9) for zero-cost iteration.
- Whether `taxonomy_coverage` should become a hard gate per module after
  datasets migrate to the three-split format — deferred until real modules
  exist beyond the reference example.
- Exact naming collision check: `cambium.bench` (the Python module) vs
  `--bench=gate|report`, a **proposed custom option** registered by the
  cambium pytest plugin's own `pytest_addoption` (`-p cambium.bench`), vs the
  repo's `bench-harness` dirs in `/home/ubuntu`. Premise corrected after
  review: pytest 9.1.1 defines **no builtin `--bench` flag** (verified:
  `pytest --help` outputs 0 hits for `bench`); the flag only exists once the
  plugin is loaded, so there is no collision with a pytest core option. The
  plugin name stays namespaced (`cambium-bench`) to stay distinct from any
  future third-party plugin.

## 8. DRAFT (v2.1, M8 scope) — Mock git eval environment and AST-assert evaluation

> **Status: DRAFT.** Not implemented. This section adopts the critique-4
> evaluation enhancements (`#16`): a **mock git environment** so a DSPy
> optimizer never touches real source, and **AST-assert** scoring so
> "structure survived" is checked in addition to "tests passed". Both target
> the Ascensus optimization loop (`docs/architecture/architecture.md` §17.4)
> and the nightly hill-climb baseline (§4). Nothing here is evidenced by an
> M8 run; every unmeasured claim is flagged **UNVERIFIED**. Files referenced
> that do not exist at `main@6109a6a` are explicitly called out as forward
> references.

### 8.1 Mock git environment — the optimizer never touches real source

**The critique (adopt #16).** In the §17.4 loop today, the candidate is
scored against the frozen dataset, but the candidate itself — a DSPy program
behind the `decide()` seam (§17.1) or, later, a diff-producing worker — must
*act on files* to be evaluated. If that acting happens inside
`src/cambium/`, the optimizer gains two things it must not have:

1. **Out-of-scope write access.** It could edit its own dataset, metric, or
   sibling pins — the edits §17.4 step 9 requires human approval for
   (`architecture.md` §17.4, "Human approval for out-of-scope harness edits").
2. **Real-repo observation.** The real tree's structure, tests, and canaries
   are signal the optimizer could overfit — the reward-hacking surface
   `metric-design.md` §2.3/G7/G8 is explicitly built to deny it ("the
   optimizer never sees `canaries.jsonl`"; G8 "game the git state").

**Design.** Every optimization iteration runs against a **frozen scratch
repo**; the real repo is never in the optimization loop:

1. `git init` a scratch repo under `.cambium/mock-envs/<module>/<run_id>/`
   (gitignored run artifact, same policy as `.cambium/baselines/`, §3).
2. Populate it with **dummy code fixtures per module** — a deterministic
   copy of the module scaffold reduced to fixture stubs. For
   `should_decompose`: `decide.py` + `metric.py` + `dataset.py` headers,
   a stub `tests/`, and `datasets/train.jsonl` **without canaries**
   (canaries load only at promotion, §17.4 step 8).
3. Record the initial commit as `base_ref`. The candidate edits the scratch;
   scoring reads the scratch state — `git diff base_ref..HEAD` (the same
   git ops `metric-design.md` §2.2 verifies at ~9 ms), the metric over the
   frozen splits, and the AST-assert script (§8.2), all run with the scratch
   as `cwd`.
4. Promotion (§17.4 step 9) is the **only** step that touches the real tree,
   via the human-approved versioned-pointer swap (`optimized/<name>/v<N>/`,
   §17.3 harness state). The optimizer process never has the real repo as
   `cwd` and never receives real-repo paths.

**Frozen-input rule (citation).** Scratch fixtures are generated only from
versioned, frozen inputs, per the dataset-format frozen-split rule
(`docs/architecture/module-template/dataset-format.md` §4):
`eval.jsonl` is **immutable once frozen** (`eval.frozen_at` in `meta.json`),
`canaries.jsonl` is frozen and additive-only, `train.jsonl` is grow-only,
and no record exists in two splits (the loader enforces this with a
canonical-hash collision check, `src/cambium/modules/example/dataset.py`
`_check_no_cross_split_collisions`). The shipped `should_decompose` dataset
already ships the three files plus `meta.json`
(`src/cambium/modules/example/datasets/{train,eval,canaries}.jsonl`, per
commit `fe160fd`). Because fixtures derive only from
`(module, dataset_version, candidate_hash)`, any iteration's environment is
reconstructible, and fixture drift is impossible without a `dataset_version`
bump that also invalidates the metric baseline (§3 versioning).

**Eval-cache integration (when merged).** Per-iteration scoring is
deterministic and therefore cacheable: key `(dataset_version,
candidate_hash, fixture_hash)`. Reference: `docs/research/feedback-4-assessment.md`
#15 and `src/cambium/eval_cache.py`. **⚠️ UNVERIFIED — neither file exists at
`main@6109a6a`**; this is a forward reference per the adoption task, and the
cache design is carried here only as the keying contract.

### 8.2 AST-assert evaluation — assert structure, not just test outcome

**Motivation.** The §2.1 floor says "tests pass"; the §2.3 brake says "no
canary tripped". Neither says *the module's seam survived*. A DSPy candidate
can preserve decision behavior while drifting the module interface — and the
exact-match metric (`metric.py::should_decompose_metric`) would still score
1.0 — while breaking every sibling that consumes the module (§17.1). AST
asserts close that gap by asserting **structural change** via
definitions/references, machine-checkable and cheap.

**Mechanism.** Use the **stdlib `ast`** module (always present, no new
dependency — consistent with this harness's stdlib-only rule) to parse the
candidate's module file and compare a **pre-registered structural
fingerprint**: definitions (class/function names, parameter names, defaults,
annotations as source text, decorators) and references (the names siblings
and the harness import from the module).

**Precedent (verified, on a parallel branch).**
`docs/research/treesitter-context.md` §3 proves signature coverage is
machine-checkable via a stdlib `ast` walk plus a regex boundary check —
100% coverage (10/10 top-level, 23/23 all names) on the example module's 4
files. **⚠️ Citation note:** that doc lives on branch
`wt-research-treesitter`, not `main@6109a6a`. Its *compressed view* is
deliberately not parseable (`ast.parse` accepts 0/20 — §3); the AST asserts
here run on **parseable candidate source** (the actual module file), so the
view's "never feed to a compiler" caveat does not apply.

**Placement.** AST asserts ride the cheap path before the scenario suite
(`metric-design.md` §1 R3 cheap-first ordering): a candidate whose
fingerprint fails is scored 0 without paying the 10–30 s suite cost.

**Three concrete asserts for the example (`should_decompose`) module:**

| # | AST assert | Fingerprint (source) | Why a test pass is not enough |
|---|---|---|---|
| A1 | **`decide()` signature intact** | `ShouldDecomposeModule` still defines `async def decide(self, input: TaskInput) -> DecomposeOutput` — exactly two params, annotation intact (`decide.py:155`) | The eval path calls `module.decide(example.input)` (`metric.py::evaluate_split_async`, `metric.py:38`); a renamed param or dropped return annotation is an integration break no fixture test catches |
| A2 | **Input/output dataclass fields intact** | `TaskInput` keeps `task: str`, `context: str = ""`; `DecomposeOutput` keeps `decompose: bool`, `reason: str`, `confidence: float = 1.0` (`decide.py:51-65`) | The loader builds inputs with `TaskInput(**record["input"])` (`dataset.py:162`) and the metric reads `prediction.decompose` (`metric.py:24`) — renamed/dropped fields break both |
| A3 | **Metric seam preserved** | `decide.py` still imports/defines `should_decompose_metric` and the class still exposes `metric(self, example: Example) -> float` bound to it (`decide.py:159-161`, `metric.py:11`) | The bench baseline and drift gate score every split through this method (§3 `metric` block); a candidate that inlines a fake metric or deletes the method silently corrupts every recorded baseline |

Fingerprint sketch (pre-registered, derived from the frozen fixture):

```jsonc
// .cambium/mock-envs/should_decompose/fingerprint.json
{
  "module": "should_decompose",
  "file": "decide.py",
  "classes": {
    "TaskInput":       {"fields": [{"name": "task", "annotation": "str", "default": null},
                                   {"name": "context", "annotation": "str", "default": "\"\""}]},
    "DecomposeOutput": {"fields": [{"name": "decompose", "annotation": "bool", "default": null},
                                   {"name": "reason", "annotation": "str", "default": null},
                                   {"name": "confidence", "annotation": "float", "default": "1.0"}]},
    "ShouldDecomposeModule": {
      "methods": {
        "decide": {"params": ["self", "input"], "returns": "DecomposeOutput", "async": true},
        "metric": {"params": ["self", "example"], "returns": "float", "async": false}
      }
    }
  },
  "references": ["should_decompose_metric"]
}
```

Assert pseudocode (stdlib `ast` only; **UNVERIFIED** — not implemented):

```python
def assert_fingerprint(candidate_src: str, fp: dict) -> list[str]:
    """Return violations; empty list = pass."""
    tree = ast.parse(candidate_src)
    for klass in fp["classes"]:
        node = next(c for c in tree.body
                    if isinstance(c, ast.ClassDef) and c.name == klass)
        # fields: ast.AnnAssign targets + ast.unparse() of annotation and value
        # methods: ast.FunctionDef / ast.AsyncFunctionDef, args.arg names,
        #          ast.unparse() of returns annotation
    # references: top-level imports / assigned names resolve to fp["references"]
    ...
```

### 8.3 Falsification note — mock-env + AST asserts must not regress real-metric correlation

The mock env exists to keep the optimizer off the real tree, **not** to
change what "good" means. Before the mock env is trusted for optimization, a
**calibration step** is mandatory:

- Run **N baseline tasks in mock vs real env** — same tasks, same candidate
  program, scored with the same metric (`metric-design.md` §5.1 separation
  methodology) — and compare the metric deltas (train-gain vs eval-gain, plus
  canary outcome) between the two envs.
- If mock-env deltas diverge from real-env deltas beyond a stated tolerance
  (sign flips, or a mean gap beyond a configurable bound), the fixtures or
  AST asserts are over-/under-constraining: the optimizer is hill-climbing a
  fake surface and its gains do not transfer to the real metric.
- This mirrors the paired-trial discipline already required elsewhere: M9's
  compile-success criterion (`docs/research/v2-1-review.md` §3 M9) and M8's
  falsification clause — no candidate meets all gates within budget ⇒ the
  deterministic baseline stays production (`v2-1-review.md` §3 M8).

**⚠️ UNVERIFIED until M8 runs.** No M8 DSPy optimization exists at
`main@6109a6a`; N, the tolerance, and the calibration numbers are unmeasured.
The nightly "train_gain − canary_gain" signature (§4) must be computed in the
mock env, and only §8.3's calibration lets that number stand in for a
real-env number. This section is design, not evidence.
