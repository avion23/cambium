# Cambium

> **License:** All rights reserved. No open-source license is included;
> redistribution requires the author's permission.

Cambium is a stdlib-first multi-agent coding harness. It supervises isolated
worker processes, records durable session events, routes model calls across
configured providers, and publishes successful worker commits through a fenced
merge sequencer.

The current runtime is intentionally small:

- one asyncio supervisor,
- one worker implementation,
- one provider router,
- one task-tree model,
- one durable event store,
- one merge/publication path,
- one provider-admission policy (`cambium.routing`),
- one active tool catalogue (`cambium.schemas` + `cambium.tools`).

## Status

Cambium currently requires Python 3.12 or newer, matching the authoritative
`project.requires-python = ">=3.12"` declaration in `pyproject.toml`.
On a real TTY, the `tui` command provides an interactive terminal cockpit: one
invocation accepts multiple prompts on a durable branch, and a later
invocation can reconnect to the newest reconnectable branch. It supports
steering with `!cancel` or Ctrl-C and queued follow-ups, and streams model
output into the cockpit. Redirected or `--quiet` output uses the line-oriented
adapter instead.

From a checkout, run commands with `PYTHONPATH=src`. After installation, use
the `cambium` console script or `python -m cambium` without `PYTHONPATH`.

```bash
PYTHONPATH=src python -m cambium version
PYTHONPATH=src python -m cambium --help
```

Research documents under `docs/research/` are design evidence, not additional
runtime implementations.

## Installation

Cambium supports Python 3.12+ and declares no mandatory third-party runtime
dependencies. Install the published/runtime package with:

```bash
python -m pip install .
```

For local development and tests, install the optional `test` extra. Quote the
requirement so shells do not expand the brackets:

```bash
python -m pip install -e '.[test]'
```

The `test` extra is declared in `pyproject.toml` and supplies the dependencies
used by the local test suite. An editable install is optional when running
straight from a checkout.

## Architecture boundaries

Provider concerns are deliberately separated without duplicating ownership:

- `cambium.routing` owns supervisor admission, durable usage debt, capability
  checks, and lane-aware provider/model assignment.
- `cambium.selection` is the shared pure equal-priority quality-ordering
  primitive used by admission and Diffundo.
- `cambium.diffundo` owns call-time health, retry, cooldown, and transport
  execution after a task is assigned.
- `cambium.provider_scheduler` retains only immutable provider leases and the
  transactional quota ledger. It contains no competing scheduler actor.

The worker uses `cambium.schemas` as the schema source of truth and
`cambium.tools` as the sole executable tool dispatcher. There is no parallel
plugin registry.

## Quickstart

Install Cambium, define one provider profile, export its API key, and start the
interactive terminal cockpit. Provider files contain environment variable
names, not secret values:

```bash
python -m pip install .
mkdir -p "$HOME/.config/cambium"
cat > "$HOME/.config/cambium/providers.json" <<'JSON'
{
  "providers": [
    {
      "name": "openai",
      "tier": "strong",
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "CAMBIUM_PROVIDER_OPENAI_API_KEY",
      "model": "gpt-5.6",
      "enabled": true
    }
  ]
}
JSON
export CAMBIUM_PROVIDER_OPENAI_API_KEY='replace-with-your-provider-key'
cambium tui --repo . --provider openai:gpt-5.6
```

Replace the example key with a real key in your shell environment; never commit
it or put it in `providers.json`. The same profile can be selected
automatically with `cambium tui --repo . --auto`.

## Interactive usage

Start the interactive terminal cockpit from a checkout:

```bash
PYTHONPATH=src python -m cambium tui --repo . --auto
```

One `tui` invocation stays at its prompt across turns. It streams model
Markdown while a turn is running, accepts `!cancel` or Ctrl-C to cancel the
active turn, and queues prompts entered while a turn is active as follow-ups.
An exit command (`/exit`, `/quit`, or exact `q`), EOF, or an input interrupt
while waiting for input ends the frontend process; its durable branch and turn
artifacts remain available to a later invocation.

To reopen the same semantic branch later, give it a stable interactive root:

```bash
PYTHONPATH=src python -m cambium tui \
  --repo . \
  --session-dir ~/.local/state/cambium/my-project \
  --auto
```

Start a line-oriented interactive session:

```bash
PYTHONPATH=src python -m cambium repl --repo . --auto
```

Run one prompt against the current repository:

```bash
PYTHONPATH=src python -m cambium run \
  --repo . \
  --provider openai:gpt-5.6 \
  "Inspect the current task-tree implementation"
```

For automatic selection across enabled providers with available credentials:

```bash
PYTHONPATH=src python -m cambium run \
  --repo . \
  --auto \
  "Inspect the current task-tree implementation"
```

Run the deterministic demo:

```bash
PYTHONPATH=src python -m cambium supervisor \
  --session-dir /tmp/cambium-demo \
  --demo
```

Within one invocation, the cockpit appends user prompts, model Markdown,
important tool/runtime events, and compact status rows to the terminal's normal
scrollback. It does not enter an alternate screen or maintain fixed
full-screen panes. Status rows include main and sub-agent state, provider/model,
per-agent tokens, output tokens/second, CAST trunk size, summary segments,
raw-tail size, epoch, checkpoint, cumulative usage, and recent durable events.
Native terminals keep readline editing and private history. Ctrl-C cancels an
active turn and returns to the cockpit without advancing the last successful
branch checkpoint; Ctrl-C while waiting for input ends the invocation.

The TUI carries the newest immutable context checkpoint into the next prompt,
keeps the provider/model lease when exact cache reuse is compatible, and falls
back to the provider-neutral semantic trunk when it is not. Each prompt still
runs in an isolated supervisor leaf and worktree. Non-TTY output remains
line-oriented and receives no cursor controls or ANSI colors.

## Operator commands

Enter these commands in the cockpit prompt:

| Command | Action |
| --- | --- |
| `/help` | Show the command reference. |
| `/status` | Show branch, context, agent, and usage details in one view. |
| `/usage` | Show cumulative calls, tokens, throughput, and cost. |
| `/agents` | Show main and sub-agent lifecycle state. |
| `/context` | Show the active trunk, raw tail, checkpoint, and epoch. |
| `/session` | Show the interactive session identity and provider lease. |
| `/new` | Start a fresh semantic branch without deleting old artifacts. |
| `/clear` | Clear only the local cockpit transcript; terminal scrollback remains. |
| `/fork` | Fork the current branch from its successful checkpoint. |
| `/branches` | List durable branch heads and checkpoint references. |
| `/compact` | Flush semantic context and roll over when a CAST K0 checkpoint is eligible. |
| `/dashboard` | Explain the visible live cockpit. |
| `/events` / `/tail` | Show recent durable event summaries. |
| `/model` | List enabled, credential-ready provider/model targets and mark the current one. |
| `/model PROVIDER` / `/model PROVIDER:MODEL` | Select an eligible routing target for subsequent turns. |
| `/cancel` | Explain that active-turn cancellation uses `!cancel` or Ctrl-C. |
| `/exit` / `/quit` | Close this TUI invocation. |
| `q` | Close the cockpit when the submitted prompt is exactly `q` after trimming whitespace. |

`!cancel` cancels an active turn; `v` toggles full command/output details for
tool entries. These are input controls rather than slash commands.

Use `<<<` and `>>>` on their own lines for multiline prompts. See
[`docs/architecture/interactive-tui.md`](docs/architecture/interactive-tui.md)
for the layout and correctness boundary.

Attach a read-only monitor to an existing supervisor leaf:

```bash
PYTHONPATH=src python -m cambium monitor /path/to/session
```

Inspect or record provider quota windows with the top-level `cambium quota`
command (there is no `/quota` TUI command):

```bash
PYTHONPATH=src python -m cambium quota status
PYTHONPATH=src python -m cambium quota observe openai five-hour \
  --reset-in-s 18000 \
  --allowance-tokens 1000000 \
  --remaining-tokens 750000
```

## CAST: cache-aligned semantic trunking

Cambium treats a long-running context as an append-only graph whose active
frontier continuously replaces new raw execution noise with immutable semantic
deltas:

```text
Durable graph

 t1 --> t2 --> t3 --> t4 --> t5
              \                 \
               +--> S1           +--> S2

Active model projection

 [ stable head H ][ summary S1 ][ summary S2 ][ small raw tail W ]
```

Earlier summary bytes never change when a new summary is appended. This preserves
an exact reusable prefix when the provider cache is still warm while keeping the
complete raw history outside the model prompt.

A compatible child receives the exact checkpoint prefix:

```text
 [ H ][ S1 ][ S2 ] --> child on the same provider/model
```

An opportunistic child on another provider starts cold but reuses the semantic
history:

```text
 provider A: [ H_A ][ S1 ][ S2 ]
                         |
                         +--> provider B: [ H_B ][ S1 ][ S2 ][ child task ]
                              semantic reuse, not a cache hit
```

The full paper proposal, fork-join diagrams, rollover economics, graph model,
and research questions are in
[`docs/architecture/cast.md`](docs/architecture/cast.md).

## Provider configuration

Cambium reads provider definitions from:

```text
~/.config/cambium/providers.json
```

Override the path with `CAMBIUM_PROVIDERS`. Provider files contain environment
variable names, never secret values. API keys are stored with:

```bash
PYTHONPATH=src python -m cambium auth set openai
PYTHONPATH=src python -m cambium auth list
PYTHONPATH=src python -m cambium doctor
```

For Codex/ChatGPT OAuth:

```bash
PYTHONPATH=src python -m cambium auth oauth login codex
PYTHONPATH=src python -m cambium auth oauth status codex
```

## Local validation

This repository does not use GitHub Actions or hosted continuous integration.
Run checks directly from the checkout:

```bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m ruff check src tests
PYTHONPATH=src python -m cambium doctor
```

`doctor` reports local environment, provider, and worktree state; it returns
nonzero when a required diagnostic fails, so its output is the useful result
rather than a universal smoke-test status.

The default pytest invocation excludes tests marked `slow`. Run the
process-boundary tier explicitly when needed:

```bash
PYTHONPATH=src python -m pytest -m slow -q
```

## Acceptance testing

The opt-in live-provider suite reads API keys read-only from supported local
credential stores when they are available; missing credentials skip the
corresponding checks. Run it with:

```bash
PYTHONPATH=src python -m pytest tests/acceptance/ -q
```

## DSPy trajectory extraction

The optimizer supports `zero`, `bootstrap`, and `gepa` from both the direct
module command and the top-level `cambium optimize` wrapper:

```bash
PYTHONPATH=src python -m cambium.optimize should_decompose \
  --optimizer gepa \
  --budget-usd 2 \
  --seed 20260824
```

GEPA uses the seeded 70/30 train/validation policy, requires at least four
reviewed non-canary records, spends through the per-run ledger, and records
its scores under `stage_gepa` in the report.

Evaluate a fresh or saved program against the train, eval, and canary splits:

```bash
PYTHONPATH=src python -m cambium optimize eval should_review \
  --dataset /path/to/reviewed-dataset \
  --program-dir optimized/should_review \
  --json
```

`MODULE` and `--dataset PATH` are required. `--program-dir PATH` loads
`program.json` from an explicit artifact directory; when omitted, the command
checks `optimized/<MODULE>/program.json` and evaluates a fresh program when no
saved state is present. The `--json` report has `module`, `program`
(`"fresh"` or `"optimized"`), `dataset`, and `splits`; each split contains
`mean`, `std`, `count`, and `records`, whose entries contain `index` and
`score`.

Extract redacted, deduplicated decision trajectories from one or more explicit
SQLite databases or storage directories. `--database` and `--session-dir` are
repeatable; the extractor is read-only and requires a source path explicitly:

```bash
PYTHONPATH=src python -m cambium optimize extract \
  --session-dir /path/to/provider-storage \
  --repo . \
  --output /tmp/cambium-trajectories.jsonl
```

Use `--review-gate` to write a review queue instead of the accepted dataset.
Review-gated rows are `candidate: true`, `redacted: true`, and
`review_status: "needs_review"`; they are not training data until a reviewer
changes every admitted row to `review_status: "approved"`. The optimizer fails
closed on pending or unknown statuses and ignores rejected/excluded rows.

Reviewed candidate files are not loaded implicitly; pass one with
`--include-transcript-candidates` or `--transcript-candidates PATH` when
augmenting a module's training pool. See
[`docs/architecture/optimization.md`](docs/architecture/optimization.md) for
the broader schema and approval gate.

## Documentation

- [Architecture overview](docs/architecture/architecture.md)
- [Acceptance testing](docs/architecture/acceptance.md)
- [CAST context trunking](docs/architecture/cast.md)
- [Context engine](docs/architecture/context-engine.md)
- [Interactive TUI](docs/architecture/interactive-tui.md)
- [Optimization](docs/architecture/optimization.md)
- [Profiling baseline](docs/architecture/profiling-baseline.md)
- [Provider routing](docs/architecture/provider-routing.md)
- [Terminal interface](docs/architecture/terminal-interface.md)
- [User CLI](docs/architecture/user-cli.md)
- [Changelog](CHANGELOG.md)

## Profiling baseline

The measured runtime-overhead baseline and its reproducible profiling harness
are documented in
[`docs/architecture/profiling-baseline.md`](docs/architecture/profiling-baseline.md).

## License

**All rights reserved.** This project is published without an open-source
license: you may read and reference the code, but redistribution, derivative
works, and commercial use require the author's explicit permission.

## Session artifacts

A supervisor leaf records its state below `<session-dir>/.cambium/`, including:

- `events.db` — durable event log,
- `result.json` — final session result,
- `conversations.db` — optional revision-boundary conversation history,
- `checkpoints/` — worker checkpoints and immutable context epochs.

An interactive TUI root additionally contains `turn-NNNN/` supervisor leaves
and `.cambium/interactive.json`, the atomic single-writer branch manifest. The
root remains durable after the TUI process exits, and reopening it restores
the branch. Native terminal history is stored at `.cambium/tui_history` with
mode `0600`.

The supervisor publishes through Git references. It does not refresh the
caller's working tree or index after advancing `main`.
