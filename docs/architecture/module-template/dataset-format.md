# Dataset Format — JSONL Schema, Versioning, Splits, Canaries

**Status:** Normative. Every decision module's datasets conform to this format. A
legacy v2 module may ship a single combined `<name>_pairs.jsonl` with inline
`canary: true` markers (see `docs/architecture/module-template/example-spec.md`
§7.1); current split-aware modules ship the `{train,eval,canaries}.jsonl`
three-file layout. A loader may retain the combined file as an explicit
backward-compatibility fallback.

---

## 1. Container format

- **File:** newline-delimited JSON (JSONL), UTF-8, no BOM.
- **One record per line.** Lines are independently parsed, but a malformed line
  is a hard `DatasetError`; loaders and the conformance gate never continue
  with a partially valid dataset.
- **Trailing newline** at end of file (POSIX convention; `git diff` clean).
- **No trailing whitespace** within lines.
- **No comments.** JSONL has no comment syntax.
- **Sort:** records are sorted by `id` (lexicographic) for deterministic diffs.

A reader:

```python
import json
from pathlib import Path

def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
```

---

## 2. Record schema

Every record shares a common envelope. Module-specific fields live under `data`.

```jsonc
{
  "id": "should_decompose-0001",          // stable, unique, sort-key
  "schema_version": 1,                     // integer, monotonic per module
  "dataset_version": "1.0.0",              // semver of the dataset file
  "split": "train",                        // "train" | "eval" | "canary"
  "added_at": "2026-08-09",                // ISO date
  "added_by": "agent:name" or "human:name",
  "source": "hand-authored" | "mined" | "synthetic" | "imported",
  "license": "apache-2.0" | "internal" | "...",
  "redacted": false,                       // true if any PII/secret was scrubbed
  "data": {
    // module-specific fields; see §3
  },
  "notes": ""                              // optional free-form, ≤500 chars
}
```

Invariants:

- `id` is unique within the module's dataset directory. Duplicate `id`s are a hard error in the loader.
- `schema_version` is **bumped** whenever the `data` schema changes in a backwards-incompatible way. Old records are migrated explicitly (see §5).
- `dataset_version` is bumped on every change that affects evaluation: adding records, fixing a label, re-splitting.
- `split` is fixed at file time. Records do not migrate between splits without a dataset_version bump.

The implemented v1 wire envelope keeps `input` and `expected` at the top
level, as shown by the reference module. The `data` wrapper described in §3 is
the typed v2.1 target and must not be inferred by a v1 loader. For the current
split-aware gate, every record must also carry `id`, `schema_version`,
`dataset_version`, and `split`; the record `dataset_version` must equal the
sidecar metadata version.

---

## 3. Module-specific `data` schema

Each module defines its own `data` shape and documents it in `src/cambium/modules/<name>/architecture.md` §7. The shape must be a frozen, typed dataclass in the module's primary implementation file (`decide.py` — the rule engine today; a future DSPy program implements the same interface behind it). For the v2 combined dataset the record shape is the scaffold's minimal `{input, expected, canary?}`; the extended envelope below is the v2.1 target:

```python
@dataclass(frozen=True)
class ShouldDecomposeDatum:
    task: str                    # v2: input.task
    context: str                 # v2: input.context
    decision: Decision           # domain value; wire expected.decompose is bool
    reason: str                  # v2: expected.reason
    expected_confidence: float   # v2.1 extension
    rationale_keywords: tuple[str, ...]   # v2.1 extension; must appear in a good rationale
```

The JSONL `data` field is the JSON serialization of this dataclass after the
explicit domain-to-wire mapping. The JSON record carries the stable boolean
`expected.decompose`; loaders use `cattrs` or hand-written `from_dict`/`to_dict`
at that boundary.

For the reference module, `Decision` is the domain enum defined in
`src/cambium/modules/example/decide.py`. The JSON wire field remains
`expected.decompose: true|false`; `ExampleDatasetLoader` maps `true` to
`Decision.DECOMPOSE` and `false` to `Decision.DO_NOT_DECOMPOSE`. Domain code
compares enum members, not the serialized boolean.

---

## 4. Splits

Three files per module:

```
src/cambium/modules/<name>/datasets/
├── train.jsonl       # used by SIMBA/GEPA to fit prompts
├── eval.jsonl        # frozen held-out; never used for training
└── canaries.jsonl    # reward-hacking traps; never used for training
```

Rules:

- **`eval.jsonl` is immutable once frozen.** A frozen marker (`eval.frozen_at` in a sidecar `meta.json`) records the freeze date. Changes require a dataset_version bump and re-running every module that pins this one.
- **`canaries.jsonl` is also frozen.** Canary additions are allowed (more traps are always welcome) but require dataset_version bump and a documented reason.
- **`train.jsonl` may grow.** Each addition is a commit; deletions are not permitted (deprecate via a `deprecated: true` flag instead).
- **No record exists in two splits.** A canonical hash of `data` (excluding metadata) is computed at load time; collisions across splits are a hard error.
- **Target sizes** (defaults; module may override with justification):
  - train: 200 records
  - eval: 50 records
  - canary: 15 records

### 4.1 Freeze dates and split digests

The implemented conformance contract freezes split content with exact-byte
SHA-256 digests. `datasets/meta.json` must contain:

```json
{
  "schema_version": 1,
  "dataset_version": "1.1.0",
  "eval_frozen_at": "2026-08-09",
  "canary_frozen_at": "2026-08-09",
  "split_digests": {
    "train": "<64 lowercase hex characters>",
    "eval": "<64 lowercase hex characters>",
    "canaries": "<64 lowercase hex characters>"
  }
}
```

Each digest is SHA-256 over the exact UTF-8 bytes of its corresponding JSONL
file, including line endings. The gate validates all three files, record IDs,
split labels, duplicate/cross-split canonical inputs, and the record
`dataset_version`. `eval_frozen_at` and `canary_frozen_at` are required
`YYYY-MM-DD` dates. A frozen split edit without a dataset-version bump is a
hard failure; a digest change with the same version is never silently
re-anchored.

Splits are produced deterministically:

```python
# deterministic split procedure (illustrative)
import random, json
records = load("datasets/raw.jsonl")
records.sort(key=lambda r: r["id"])
random.seed(42)  # module-specific seeds; documented per module
random.shuffle(records)
# train = records[:200], eval = records[200:250], canary = hand-picked 15
```

---

## 5. Versioning

Two orthogonal versions:

- **`schema_version`** (integer): the shape of `data`. Bump on incompatible change. Migration:
  ```python
  def migrate(record: dict, from_v: int, to_v: int) -> dict:
      if from_v == 1 and to_v == 2:
          # v1 used `expected_decision`; v2 standardizes on `decompose`
          # (matching the scaffold's expected.decompose field).
          record["data"]["decompose"] = bool(record["data"].pop("expected_decision"))
          record["schema_version"] = 2
      return record
  ```
  Migrations are pure functions, tested, and committed alongside the schema bump.

- **`dataset_version`** (semver): the contents of the dataset.
  - **Patch** (`1.0.0` → `1.0.1`): typo fixes, metadata-only changes, no label changes.
  - **Minor** (`1.0.0` → `1.1.0`): added records, added canaries. Frozen splits stay frozen.
  - **Major** (`1.0.0` → `2.0.0`): label changes, re-splits, schema bumps, frozen-set changes.

A sidecar `meta.json` records the current versions and the frozen-at timestamp:

```jsonc
// src/cambium/modules/<name>/datasets/meta.json
{
  "schema_version": 1,
  "dataset_version": "1.0.0",
  "eval_frozen_at": "2026-08-09",
  "canary_frozen_at": "2026-08-09",
  "split_digests": {
    "train": "<sha256 of exact train.jsonl bytes>",
    "eval": "<sha256 of exact eval.jsonl bytes>",
    "canaries": "<sha256 of exact canaries.jsonl bytes>"
  },
  "sibling_pins": {
    "TaskDecomposer": "0.3.1",
    "Opifex": "1.2.0"
  }
}
```

`sibling_pins` records the production versions of sibling modules against which this dataset was last validated. Optimization uses these to load matching stubs (see `architecture.md` §17.2).

### 5.1 Domain-enum compatibility

The `Decision` migration is domain-side. It leaves the JSON record shape and
the wire boolean `expected.decompose` unchanged, so `schema_version` remains
`1`. The reference module's current `src/cambium/modules/example/datasets/meta.json`
records `dataset_version: "1.1.0"`, which is the post-migration dataset and
baseline anchor. `dataset_version` identifies the evaluation dataset and its
scoring anchor; it is not an enum serialization. Bump it under the semver rules
above when records, labels, splits, loader semantics, or scoring change, and
keep the wire boolean stable when only the domain representation changes. A
domain-only migration may still re-anchor the dataset version, as the reference
module does here, without changing `schema_version` or rewriting any records.

### 5.2 Implemented gate and baseline agreement

The conformance gate requires one dataset version across the complete chain:

```text
JSONL record.dataset_version == meta.json.dataset_version
baseline.dataset_version == meta.json.dataset_version
baseline.split_digests == meta.json.split_digests == SHA256(current split bytes)
```

The baseline JSON is validated as a schema-bearing object, not treated as an
opaque benchmark artifact. It must contain `schema_version`, the logical
`module` name, `dataset_version`, all three `split_digests`, provenance and
runtime fields, per-split metrics, canary summary, dataset counts, test
timings, and drift thresholds. Missing fields, invalid types, stale counts,
or stale digests fail the gate.

At the current tree, the split records still say `1.0.0` while
`meta.json` and the committed baseline say `1.1.0`. That is a deliberate,
visible dataset-owner reconciliation failure. The module-conformance change
must report it and must not rewrite the dataset records.

---

## 6. Canaries (reward-hacking traps)

Canaries are records that should **not** pass trivially under a metric-gaming prompt. They are the brakes on the flywheel (`docs/architecture/architecture.md` §17.4 step 8).

Each canary record carries a `canary` field under `data`:

```jsonc
{
  "id": "should_decompose-canary-01",
  "schema_version": 1,
  "split": "canary",
  "data": {
    "task": "Refactor function `foo` to use list comprehension. Single file, single function.",
    "context": "",
    "decompose": false,
    "reason": "trivially atomic",
    "expected_confidence": 0.9,        // v2.1 extension
    "canary": {
      "kind": "trivially_atomic",                    // see taxonomy below
      "anti_expected": true,                          // what a hacked prompt would say
      "anti_expected_confidence_range": [0.5, 1.0],   // and how confident it would be
      "description": "A prompt that decomposes this is over-decomposing; trap rewards 'no'."
    }
  }
}
```

### Canary taxonomy (extend per module)

| Kind | What it traps | Trigger | Pass condition |
|---|---|---|---|
| `trivially_atomic` | Over-decomposition | A spec that should clearly NOT be decomposed. | Domain output `Decision.DO_NOT_DECOMPOSE` (wire `false`). |
| `must_decompose` | Under-decomposition | A spec with ≥3 distinct subtasks. | Domain output `Decision.DECOMPOSE` (wire `true`). |
| `ambiguous_calibration` | Over-confident on ambiguous input | A spec with no clear answer. | `confidence ≤ 0.6`. |
| `format_only_hack` | Format-valid but content-empty | Output with empty rationale. | `len(rationale) ≥ 50`. |
| `keyword_hack` | Rationale keyword-stuffed | Rationale includes gold keywords but wrong decision. | Decision must match; keyword match alone fails. |

Add module-specific kinds as needed. Every canary has a `description` explaining what gaming behavior it detects.

A canary fails if its `pass condition` is not met. **One failed canary = the optimized prompt is rejected.**

---

## 7. Data hygiene

- **No secrets.** The loader scans for common secret patterns (`sk-...`, `AIza...`, `ghp_...`) and refuses to load a record containing them. The repository check (`scripts/check_dataset_v1.py`) performs the dataset secret scan and integrity gate.
- **No PII** (names, emails, phone numbers, real repo URLs that imply an author). Real specs are paraphrased.
- **Redaction log:** if a record was redacted, `redacted: true` and a `redaction_notes` field describe what was scrubbed.
- **Licensing:** every record carries a `license`. Internal-only datasets use `"internal"`; shareable datasets use an OSI license. Mixed-license datasets are not permitted in a single file.

---

## 8. Review and contribution

- New records are added by PR. The PR template requires:
  - Source (hand-authored / mined / synthetic / imported).
  - For mined records: a link to the production event-log entry they were derived from (or a justification if redacted).
  - Schema and dataset versions, with bumps if required.
- **Two-reviewer rule** for the frozen `eval.jsonl`: changes require sign-off from both the module owner and the orchestrator owner.
- Canary additions require sign-off from at least one reviewer who did not author the canary.
- The dataset's `meta.json` is checked by `scripts/check_dataset_v1.py`, which fails the CI gate on inconsistencies (split leaks, duplicate IDs, missing fields, schema mismatches).

---

## 9. Loader contract (normative)

```python
# cambium.datasets.load — used by eval harnesses and Ascensus
from pathlib import Path
from dataclasses import dataclass

@dataclass(frozen=True)
class Dataset:
    name: str
    schema_version: int
    dataset_version: str
    train: tuple[dict, ...]
    eval: tuple[dict, ...]
    canaries: tuple[dict, ...]
    sibling_pins: dict[str, str]

def load(module_name: str, root: Path = DEFAULT_ROOT) -> Dataset:
    """Load all three splits for a module; validate; return frozen Dataset."""
```

The loader raises `DatasetError` on any inconsistency: duplicate IDs, cross-split leaks, missing `meta.json`, schema mismatch, secret-pattern hit. Eval harnesses do not catch `DatasetError`; a broken dataset is a hard gate. A concrete loader may map stable wire scalars to domain enums after validating the wire schema; that mapping does not change the dataset's JSON format.

---

## 10. Conformance, packaging, and removal

The dataset gate is run for the package-directory name, not the logical
dataset name:

```console
uv run --extra test cambium module-test <package_name>
```

For example, the reference dataset is logically `should_decompose`, but the
package directory and selector are `example`. Baselines use the logical name;
imports and wheel paths use `cambium.modules.example`.

Module tests run in an offline subprocess environment. Credentials and pytest
plugin injection are removed, Python socket clients and common command-line
network clients are denied, and child subprocesses inherit the same rule.
The module cannot import a sibling decision package. Harness production code,
`bench.py`, `scripts/`, and `tools/` cannot reverse-import a decision package;
the gate reports any violation with its file, line, and symbol.

The wheel must carry the module's code, JSON CLI, architecture document, all
three split files, `meta.json`, colocated tests, and baselines. The installed
wheel is probed outside the checkout with the same `module-test` command. A
module is removable only as a complete directory deletion: no dataset,
baseline, test, architecture, or package file may remain outside that module
directory as a hidden dependency.
