# DSPy optimization and OpenCode data

**Status:** executable contract.

## Commands

```sh
uv run cambium optimize should_decompose --dry-run
uv run cambium optimize should_decompose --optimizer zero --budget-usd 2
uv run cambium optimize should_decompose --optimizer bootstrap --budget-usd 2
```

The optimizer writes one replace-in-place artifact set:

```text
optimized/<module>/
  program.json
  lm.json
  report.json
```

A failed gate does not become a runtime default.

## OpenCode extraction

The installed CLI reads one or more local OpenCode SQLite databases, or a
fixture/storage directory containing them.  The command is read-only and does
not discover or use a user's database unless that path is supplied explicitly.
Use a repository fixture or an intentionally exported test database when
testing:

```sh
uv run cambium optimize extract \
  --database /path/to/opencode-fixture.db \
  --repo /path/to/example-repo \
  --from 2026-01-01T00:00:00Z \
  --to 2026-01-31T23:59:59Z \
  --output accepted-trajectories.jsonl
```

`--session-dir` may be used instead of `--database` when the fixture is an
OpenCode storage directory; `--database` and `--session-dir` are repeatable.
The time bounds accept epoch seconds/milliseconds or ISO-8601 timestamps.
The extractor is schema-aware, read-only, bounded, deduplicated, and redacts
credentials, personal identifiers, local paths, URLs with credentials, and
sensitive code blocks. It writes a versioned JSONL dataset plus
`<output>.meta.json` containing source file digests, filters, counts, and
provenance. Records contain only explicit visible decision/rationale pairs;
the extractor never invents trajectories.

The normal output is the accepted set (`review_status: "approved"`). To keep
newly extracted records out of the accepted set until a human reviews them,
use the review gate:

```json
{"candidate": true, "review_status": "needs_review", "redacted": true}
```

```sh
uv run cambium optimize extract \
  --session-dir /path/to/opencode-fixture \
  --repo example-repo \
  --output review-queue.jsonl \
  --review-gate
```

The review queue is not training data. Reviewers must explicitly change every
admitted record to `review_status: "approved"`; pending or unknown statuses
fail closed when the optimizer loads candidates. The legacy fixture-only
script remains available as `uv run python
scripts/extract_opencode_transcript_candidates.py`, and retains its
review-queue default.

## Dataset report

Report counts per repository, UTC day, label, review status, and tool
vocabulary from either an accepted set or a review queue:

```sh
uv run cambium optimize stats accepted-trajectories.jsonl
uv run cambium optimize report --dataset review-queue.jsonl --json
```

## Review gate

DSPy accepts transcript candidates only when each admitted record has:

```json
{"candidate": true, "review_status": "approved", "redacted": true}
```

`rejected`/`excluded` records are ignored. Any remaining `needs_review` or
unknown status fails closed. This prevents an opt-in flag from silently turning
raw model output into labels.

Use either the module-local reviewed file:

```sh
uv run cambium optimize should_decompose \
  --include-transcript-candidates
```

or an explicit reviewed file:

```sh
uv run cambium optimize should_decompose \
  --transcript-candidates /path/to/reviewed-candidates.jsonl
```

## Split discipline

Do not randomly split adjacent turns from the same coding session. Training,
validation, evaluation, and canaries must be separated by source session and,
where possible, repository and time. Otherwise near-duplicate trajectories leak
across splits and invalidate the measured gain.

The frozen evaluation and canary sets never receive transcript candidates.
Candidate deduplication uses canonical `(task, context)` pairs and excludes
collisions with every frozen split.

## Provider and OAuth behavior

The optimizer uses the same provider configuration and Diffundo transport as the
runtime. Codex ChatGPT OAuth credentials are refreshed through `TokenManager`
before the LM is constructed; an expired access token is never passed directly
to DSPy. The public Codex client identifier is pinned in the provider profile,
with `CAMBIUM_CODEX_CLIENT_ID` retained only as an override.

## Promotion gate

A compiled program is promotable only when:

- frozen evaluation meets the absolute threshold;
- it does not regress beyond the baseline tolerance;
- every canary passes;
- budget accounting is complete;
- the artifact and report are written atomically.

Train gain without held-out/canary gain is reported as an anti-reward gap, not
as evidence of improvement.
