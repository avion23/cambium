# Dataset Format — JSONL Schema, Versioning, Splits, Canaries

**Status:** Normative. Every decision module's datasets conform to this format. v2 modules ship a single combined `<name>_pairs.jsonl` with inline `canary: true` markers (see `docs/architecture/module-template/example-spec.md` §7.1); the `{train,eval,canaries}.jsonl` three-file split described here is the v2.1 target.

---

## 1. Container format

- **File:** newline-delimited JSON (JSONL), UTF-8, no BOM.
- **One record per line.** Lines are independent; a malformed line does not invalidate the file.
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

---

## 3. Module-specific `data` schema

Each module defines its own `data` shape and documents it in `src/cambium/modules/<name>/architecture.md` §7. The shape must be a frozen, typed dataclass in the module's primary implementation file (`decide.py` — the rule engine today; a future DSPy program implements the same interface behind it). For the v2 combined dataset the record shape is the scaffold's minimal `{input, expected, canary?}`; the extended envelope below is the v2.1 target:

```python
@dataclass(frozen=True)
class ShouldDecomposeDatum:
    task: str                    # v2: input.task
    context: str                 # v2: input.context
    decompose: bool              # v2: expected.decompose
    reason: str                  # v2: expected.reason
    expected_confidence: float   # v2.1 extension
    rationale_keywords: tuple[str, ...]   # v2.1 extension; must appear in a good rationale
```

The JSONL `data` field is the JSON serialization of this dataclass. Loaders use `cattrs` or hand-written `from_dict`/`to_dict`.

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

Splits are produced deterministically:

```python
# scripts/split_dataset.py
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
  "sibling_pins": {
    "TaskDecomposer": "0.3.1",
    "Opifex": "1.2.0"
  }
}
```

`sibling_pins` records the production versions of sibling modules against which this dataset was last validated. Optimization uses these to load matching stubs (see `architecture.md` §17.2).

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
| `trivially_atomic` | Over-decomposition | A spec that should clearly NOT be decomposed. | Output `decision=false`. |
| `must_decompose` | Under-decomposition | A spec with ≥3 distinct subtasks. | Output `decision=true`. |
| `ambiguous_calibration` | Over-confident on ambiguous input | A spec with no clear answer. | `confidence ≤ 0.6`. |
| `format_only_hack` | Format-valid but content-empty | Output with empty rationale. | `len(rationale) ≥ 50`. |
| `keyword_hack` | Rationale keyword-stuffed | Rationale includes gold keywords but wrong decision. | Decision must match; keyword match alone fails. |

Add module-specific kinds as needed. Every canary has a `description` explaining what gaming behavior it detects.

A canary fails if its `pass condition` is not met. **One failed canary = the optimized prompt is rejected.**

---

## 7. Data hygiene

- **No secrets.** The loader scans for common secret patterns (`sk-...`, `AIza...`, `ghp_...`) and refuses to load a record containing them. The dataset redaction script (`scripts/redact_dataset.py`) runs as a pre-commit hook.
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
- The dataset's `meta.json` is regenerated by `scripts/check_dataset.py`, which fails the CI gate on inconsistencies (split leaks, duplicate IDs, missing fields, schema mismatches).

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

The loader raises `DatasetError` on any inconsistency: duplicate IDs, cross-split leaks, missing `meta.json`, schema mismatch, secret-pattern hit. Eval harnesses do not catch `DatasetError`; a broken dataset is a hard gate.
