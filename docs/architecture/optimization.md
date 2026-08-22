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

The extractor reads one or more OpenCode SQLite databases:

```sh
uv run python scripts/extract_opencode_transcript_candidates.py \
  --database ~/.local/share/opencode/opencode.db \
  --output reviewed-candidates.jsonl
```

Extraction is schema-aware, read-only, bounded, deduplicated, and redacts
credentials, personal identifiers, local paths, URLs with credentials, and
sensitive code blocks. Extracted records start with:

```json
{"candidate": true, "review_status": "needs_review", "redacted": true}
```

They are not training data yet.

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
