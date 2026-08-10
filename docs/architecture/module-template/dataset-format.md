# Dataset Format — JSONL Schema, Versioning, Splits, Canaries

**Status: NORMATIVE TARGET.** Every decision module follows this format. A v2
interim module may retain one combined `<name>_pairs.jsonl` with inline
`canary: true`; current split-aware modules use `train.jsonl`, `eval.jsonl`,
and `canaries.jsonl`. A loader may use the combined file only as an explicit
backward-compatible fallback.

## 1. Container format

Files are UTF-8 JSONL without a BOM: one independently parsed record per line,
trailing newline, no trailing whitespace or comments, sorted lexicographically
by `id`. A malformed line, duplicate `id`, or partially valid file is a hard
`DatasetError`; the loader never continues with a partial dataset.

```python
import json
from pathlib import Path

def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
```

## 2. Record schema

The normative v2.1 envelope is:

```jsonc
{
  "id": "should_decompose-0001",
  "schema_version": 1,
  "dataset_version": "1.0.0",
  "split": "train",                 // train | eval | canary
  "added_at": "2026-08-09",
  "added_by": "agent:name",
  "source": "hand-authored",         // hand-authored | mined | synthetic | imported
  "license": "apache-2.0",
  "redacted": false,
  "data": {},
  "notes": ""                        // optional, at most 500 chars
}
```

`id` is unique within the module dataset directory. Bump `schema_version` for
an incompatible data-shape change and migrate old records explicitly. Bump
`dataset_version` whenever evaluation changes (record, label, or split).
`split` is fixed when written; moving a record requires a version bump.

The implemented v1 reference wire shape keeps `input` and `expected` at the
top level, not under `data`:

```json
{"input":{"task":"...","context":""},
 "expected":{"decompose":false,"reason":"..."},"canary":false}
```

The current split gate additionally requires `id`, `schema_version`,
`dataset_version`, and `split`, with record version equal to
`datasets/meta.json`. Do not infer the v2.1 `data` wrapper in a v1 loader.

## 3. Module-specific data

Each module documents a frozen typed dataclass in its own `architecture.md`
§7. For `should_decompose`, the domain fields are `task`, `context`,
`Decision`, `reason`, optional `expected_confidence`, and
`rationale_keywords`. The JSON boundary maps `Decision.DECOMPOSE` to the
stable boolean `expected.decompose: true` and
`Decision.DO_NOT_DECOMPOSE` to `false`; domain code compares enum members.

## 4. Splits and freezing

```text
datasets/
├── train.jsonl       # optimizer input
├── eval.jsonl        # frozen held-out set
└── canaries.jsonl    # frozen reward-hacking traps
```

Rules:

- `eval.jsonl` and `canaries.jsonl` are frozen. Additions or edits require a
  `dataset_version` bump, reason, and re-validation of every pinned module.
- `train.jsonl` is grow-only; do not delete records (deprecate explicitly).
- No canonical input may occur in two splits. Duplicate IDs and cross-split
  canonical-hash collisions are hard errors.
- Default targets are train 200, eval 50, canary 15; a module may override
  with a documented reason (the reference has 10 canaries).

`datasets/meta.json` records exact-byte SHA-256 digests and freeze dates:

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
  },
  "sibling_pins": {"TaskDecomposer": "0.3.1"}
}
```

Each digest covers the exact UTF-8 bytes, including line endings. The gate
checks IDs, split labels, versions, duplicate/cross-split inputs, and freeze
dates. A frozen edit with no version bump is a hard failure; a digest change
under the same version is never silently re-anchored.

Partitions must be deterministic and documented per module. An illustrative
seeded procedure is:

```python
records.sort(key=lambda r: r["id"])
random.seed(42)                 # module-specific seed
random.shuffle(records)
train, eval_ = records[:200], records[200:250]
canaries = hand_picked(records[250:])
```

## 5. Versioning

`schema_version` is an integer shape version. Migrations are pure, tested, and
committed with the bump. `dataset_version` is semver:

- patch (`1.0.0` → `1.0.1`): typo or metadata-only change;
- minor (`1.0.0` → `1.1.0`): added records/canaries while frozen sets stay fixed;
- major (`1.0.0` → `2.0.0`): label change, re-split, schema bump, or frozen-set change.

Changing a domain enum representation does not change the JSON boolean shape,
so `schema_version` remains 1; bump `dataset_version` if scoring or loader
semantics change. The reference metadata currently records `1.1.0`.

The conformance chain is one version and one digest map:

```text
record.dataset_version == meta.dataset_version == baseline.dataset_version
baseline.split_digests == meta.split_digests == SHA256(current split bytes)
```

The baseline is schema-bearing, not opaque: it includes version, logical
module, digests, provenance/runtime fields, per-split metrics, canary summary,
dataset counts, test timings, and drift thresholds. Missing fields or stale
counts/digests fail the gate. If records and metadata disagree, report the
owner reconciliation failure; do not rewrite records as a documentation fix.

## 6. Canaries

Canaries are frozen records that defeat metric-gaming prompts. A record carries
`canary: true` at the implemented wire boundary and a `canary_info`/`data`
object with `kind`, `anti_expected`, confidence range when relevant, and a
description of the trap. The taxonomy is extensible:

| Kind | Trap | Pass condition |
|---|---|---|
| `trivially_atomic` | over-decomposition | `Decision.DO_NOT_DECOMPOSE` / `false` |
| `must_decompose` | under-decomposition | `Decision.DECOMPOSE` / `true` |
| `ambiguous_calibration` | over-confidence | confidence ≤ 0.6 |
| `format_only_hack` | valid format, bad content | module-specific content check |
| `keyword_hack` | surface-keyword memorization | gold decision, not keyword count |
| module-specific | documented failure mode | documented condition |

Canaries are scored with ordinary records; dropping them is an integrity
failure. A canary suite has a 100% pass gate for promotion.

## 7. Data hygiene

Do not store credentials, secrets, or unnecessary PII. Every record has a
license and provenance. If redaction occurs, set `redacted: true` and record
`redaction_notes`; mixed licenses in one file require an explicit policy.

## 8. Review and contribution

Record author, date, source, and reason for every addition. A second reviewer
approves frozen eval changes; a reviewer who did not author a canary approves
canary additions. Dataset checks must report counts, labels, duplicate IDs,
cross-split collisions, secret scans, and digest/version agreement.

## 9. Loader contract (normative)

`DatasetLoader` (the reference is `ExampleDatasetLoader`) loads UTF-8 JSONL and
validates all required types. Split-aware loaders expose `load_split()` and
`load_all()`, exclude canaries from train/eval, and reject duplicate IDs,
version drift, invalid metadata, malformed JSON, non-object records, and
canonical cross-split collisions. A missing split may use
`example_pairs.jsonl` only when the fallback is explicit and the split-aware
metadata does not silently get ignored.

## 10. Conformance, packaging, and removal

The live command is:

```console
uv run --extra test cambium module-test <package_name>
```

It checks tracked module layout, manifest, schema/digests, imports, JSON CLI,
offline subprocess behavior, and colocated tests. This offline guard is a
**BEST-EFFORT, deterministic lint-style check for common forms of accidental
network use; it is not a security boundary. It CANNOT prevent a hostile
same-UID module from bypassing the check with os.system, posix_spawn, raw
sockets, subprocess monkey-patching, or by killing a same-UID tracer. The
harness does not start such a tracer or provide an in-harness sandbox. Real
containment is the deployment-layer boundary.**
The wheel includes code,
`__main__.py`, this architecture document, datasets, `meta.json`, tests, and
baselines. The tool is developed and run directly from source; the Hatch wheel
target and wheel acceptance tests remain for packaging, which is not the
primary delivery path. A module is removable by deleting its complete package
directory; shared harness scenarios remain.

## Appendix A. Implemented reference details

### A.1 Envelope compatibility

The minimal v2 record used by the reference loader is intentionally smaller
than the target envelope. It validates `input.task` as a string,
`input.context` as a string, `expected.decompose` as a boolean, and
`expected.reason` as a string. The optional `canary` marker is a boolean. A
split-aware record adds non-empty `id`, `schema_version`, `dataset_version`,
and `split`; all four version/split values must agree with `meta.json` and the
file being loaded. A legacy record without the envelope is accepted only by
the explicit `example_pairs.jsonl` fallback. Do not use that fallback to hide a
missing or unreadable required split when valid split metadata exists.

The domain mapping is deliberately one-way at the boundary: loaders turn
`expected.decompose: true` into `Decision.DECOMPOSE` and `false` into
`Decision.DO_NOT_DECOMPOSE`; metric code compares enum members. A domain-only
enum migration can leave `schema_version` at 1, but a changed label, split,
loader interpretation, or score requires a dataset-version bump and a new
baseline anchor.

### A.2 Digests and baseline agreement

The gate hashes bytes, not parsed objects. It includes the final newline and
does not normalize `\r\n`, whitespace, key order, or Unicode. It validates all
three split digests, record IDs, labels, metadata versions, freeze dates, and
canonical `(input, expected)` collisions. The exact agreement rule is:

```text
record.dataset_version == meta.dataset_version == baseline.dataset_version
meta.split_digests == baseline.split_digests == SHA256(exact split bytes)
```

A same-version digest change is a failure even if the mean metric improves. A
new dataset version may create an anchor only after its owner records the
reason, frozen dates, and sibling re-validation. Baseline counts are checked
against loaded records; a stale count is not informational.

### A.3 Canary record and review example

An implemented top-level canary record may look like:

```jsonc
{
  "id": "should_decompose-canary-01",
  "schema_version": 1,
  "dataset_version": "1.1.0",
  "split": "canary",
  "input": {"task": "Refactor one function in one file.", "context": ""},
  "expected": {"decompose": false, "reason": "trivially atomic"},
  "canary": true,
  "canary_info": {
    "kind": "trivially_atomic",
    "anti_expected": true,
    "description": "A keyword-greedy prompt would over-decompose this record."
  }
}
```

Canaries are not a separate scoring algorithm: they use the module metric and
are additionally subject to the 100% promotion gate. A canary author records
the failure mode and expected output. A different reviewer checks the text,
label, confidence range where relevant, and that the canary is not a duplicate
of train/eval content. Adding a canary is a minor dataset-version change even
when the aggregate score remains unchanged.

### A.4 Loader and packaging checks

`load_split()` returns only the requested split; train/eval exclude canaries,
and `load_all()` performs cross-split collision checks. Loader errors include
file and line context. Tests must exercise malformed JSON, a non-object record,
missing required keys, invalid field types, duplicate IDs, invalid metadata,
record/version drift, and a cross-split collision. The module conformance gate
also checks that every declared dataset, baseline, architecture file, test,
and manifest is tracked and included in the wheel. Removal
means deleting the
entire module directory, including its freeze metadata; no shared loader may
silently resurrect it.

### A.5 Authority and migration note

When this normative target and a live loader differ, the loader and its tests
establish current behavior, while this document establishes the intended
future boundary. Record the difference in the module architecture and in the
dataset owner issue. A migration must be a pure, reviewable function with a
fixture for every old version; editing records in place to make a check green
loses the evidence needed to compare scores across versions.
