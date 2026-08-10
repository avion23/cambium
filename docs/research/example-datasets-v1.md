# `should_decompose` — datasets v1 (train / eval / canaries)

**Status: HISTORICAL/SNAPSHOT (2026-08-09).** This research note records the
v1 generator and its checks. It is not a current-main claim. The live files
are `src/cambium/modules/example/datasets/{train,eval,canaries}.jsonl` and
`meta.json`; the current metadata version is `1.1.0`.

## Files and provenance

- `train.jsonl`: 200 records, split `train`;
- `eval.jsonl`: 50 records, split `eval`;
- `canaries.jsonl`: 10 records, split `canary`;
- `meta.json`: versions, freeze dates, and split digests;
- `scripts/generate_should_decompose_v1.py`: reproducible rule-based generator;
- `scripts/check_dataset_v1.py`: load/validate gate.

The candidates are hand-authored software-engineering prose across web,
payments, ETL, infrastructure, CLI, telemetry, security, observability,
messaging, mobile, auth, billing, and CI/CD. The generator enumerates evidence
profiles from `decide.py`, runs the real `should_decompose(task, context)`, and
aborts on a label mismatch. All 260 generated candidates matched the engine in
the snapshot. `expected.reason` and confidence are verbatim engine output;
`rationale_keywords` support future audit, while v2 scoring uses only
`expected.decompose`.

## 1. Balance and difficulty

| split | records | true | false | true ratio |
|---|---:|---:|---:|---:|
| train | 200 | 100 | 100 | 50.0% |
| eval | 50 | 25 | 25 | 50.0% |
| canaries | 10 | 3 | 7 | 30.0% |

Evidence bands from the report-only checker:

| band | train | eval |
|---|---:|---:|
| false, evidence 0 | 37 | 10 |
| false, evidence 1 | 63 | 15 |
| true, evidence 2 | 94 | 25 |
| true, evidence 3 | 5 | 0 |
| true, evidence 4 | 1 | 0 |

Signal frequencies over train+eval (signals may co-occur): keywords 71,
sentences 53, `each` 45, two verb-led 42, files 37, length 23, three-plus
verb-led 19, context suppression 14, itemized 13. False records include
keyword, sentence, length, `each`, file, and exactly-two-verb decoys; true
records combine signals or use standalone itemized/verb-led evidence. Canonical
hashing found no record in two splits.

## 2. Canary IDs and traps

Every record has `canary: true` and `canary_info` with `name`, `kind`,
`anti_expected`, confidence range where relevant, `failure_mode`, and
`description`. The complete snapshot inventory is:

| ID | Name | Kind | Trap |
|---|---|---|---|
| `canary-01` | keyword-dense dashboard rollout | `trivially_atomic` | 5 keywords but evidence 1; must be false |
| `canary-02` | verb-led dispatcher upgrade | `must_decompose` | no keywords, 3 workstreams; must be true |
| `canary-03` | keyword-stuffed single feature | `keyword_hack` | keyword counting over-decomposes |
| `canary-04` | three-sentence atomic fix | `ambiguous_calibration` | sentence counting over-decomposes |
| `canary-05` | duplicate-looking atomic (pair A) | `near_duplicate_contradiction` | 2 verb-led clauses; false |
| `canary-06` | duplicate-looking decomposed (pair B) | `near_duplicate_contradiction` | 3 verb-led workstreams; true |
| `canary-07` | context already decomposed | `context_suppression` | context-blind model over-decomposes |
| `canary-08` | long but atomic investigation | `trivially_atomic` | length heuristic over-decomposes |
| `canary-09` | format-only rationale trap | `format_only_hack` | filler rationale omits atomicity |
| `canary-10` | itemized no-keyword migration | `must_decompose` | 4 numbered items; must be true |

The 7/3 false/true balance intentionally over-weights over-decomposition. The
module-specific kinds extend the taxonomy in `dataset-format.md` §6. The
historical note's generated labels are rule-engine-consistent by construction;
empirical optimizer failure was not measured.

## 3. Schema and version notes

The v1 loader-compatible shape is:

```jsonc
{
  "id": "...", "schema_version": 1, "dataset_version": "1.0.0",
  "split": "train", "added_at": "2026-08-09",
  "added_by": "agent:data-builder-v1", "source": "hand-authored",
  "license": "internal", "redacted": false,
  "input": {"task": "...", "context": ""},
  "expected": {"decompose": false, "reason": "..."},
  "expected_confidence": 0.7, "rationale_keywords": ["atomic"],
  "canary": false, "notes": ""
}
```

This snapshot deliberately kept module fields at top level because
`ExampleDatasetLoader` validates `input`/`expected`; the normative v2.1
`data` wrapper and object canary remain target format. The split is curated,
not a random shuffle. Ten canaries are a documented override of the template
default 15. `ambiguous_calibration` uses the rule engine's fixed 0.7/0.8/0.9
tiers, and `format_only_hack` uses rationale keywords because v2 does not score
rationale length.

An earlier generator note used `dataset_version: "1.0.0"`; the live split
records, `meta.json`, and baseline are now all `1.1.0`. The current checker
therefore reports no owner reconciliation mismatch. Preserve the historical
`1.0.0` label only as provenance for that older snapshot; do not re-anchor live
records or documentation to it.

## 4. Verification recorded by the snapshot

Historical command:

```console
python3.12 scripts/check_dataset_v1.py
```

The check reported success for JSONL loading, trailing newline/ordering,
envelope types, 260 distinct payloads, no duplicate IDs or cross-split leaks,
secret scan, and engine consistency. Running the module over all 260 records
returned `should_decompose_metric == 1.0` for every record. The colocated
scenario test continued to target legacy `example_pairs.jsonl`.

## 5. Unverified or changed since the snapshot

- The generic checker and a v2.1 evaluator were not present in the snapshot;
  current live checks are `module_conformance` and the example CLI/evaluation
  surfaces.
- SIMBA/GEPA optimization, the `≥ 0.85` eval gate, and 100% canary promotion
  gate were not run; the canary trap property is a design claim only.
- Human second review of all task prose and realism review were not recorded.
- `sibling_pins` is empty because this is the first module.

## Appendix A. Generator edge cases and audit fields

The generator's evidence profile is not a random label source. It keeps prose
realistic while selecting one or more documented signals, then calls the real
engine to produce `expected.decompose`, `expected.reason`, and confidence. A
period-separated pair of verb-led sentences contributes one action clause
because action clauses split on comma/semicolon; a clause beginning with
`and` is not verb-led. Thus `A, B, and C` contributes two action clauses while
`A, B, C, and D` contributes three. These details explain the near-threshold
decoys and must remain stable for this snapshot's statistics.

Each record's `source` is `hand-authored`, `license` is `internal`, and the
snapshot generator used `added_by: agent:data-builder-v1`. The records carry
`expected_confidence` in `[0, 1]`, non-empty `rationale_keywords`, and notes
bounded by 500 characters. The checker also performed a content-only secrets
scan for API keys, private keys, passwords, emails, and phone numbers. These
provenance and hygiene fields are part of the dataset audit even though the v2
decision metric scores only the enum label.

The current split metadata uses exact SHA-256 byte digests and freeze dates;
the historical generator report did not rewrite `meta.json`. Re-running the
checker after an owner version reconciliation must compare record versions,
metadata, baseline digests, and all 260 labels before treating a new result as
comparable.

The snapshot's verification status is scoped: **VERIFIED** means the record
files loaded, schema/uniqueness checks passed, and labels matched the rule
engine at the recorded command. It does not mean the v2.1 nested `data`
schema, an optimizer, human realism review, or a current-main baseline was
verified. Those remain **UNVERIFIED** until their live commands and reviewers
provide evidence.

The split labels in the files are `train`, `eval`, and `canary`; the metadata
digest key is `canaries`. Keep that naming distinction when translating a
record-level label into a file-level report or baseline field.
