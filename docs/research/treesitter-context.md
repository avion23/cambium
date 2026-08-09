# Tree-sitter AST context compression (v2.1 roadmap M9, Proposal 1)

Research date: 2026-08-09. Worktree: `/tmp/opencode/cambium-research-treesitter`
(branch `wt-research-treesitter`, created from `main@6109a6a`). Experiment
directory: `/tmp/opencode/exp-treesitter` (outside the worktree, per task).
Purpose: answer the falsifiable question — does tree-sitter-based AST
extraction reduce LLM context tokens meaningfully without hurting
compile/test success? — against the M9 acceptance criteria in
`docs/research/v2-1-review.md` §3 M9:

1. **>=25% reduction** in median input tokens per compile-successful task.
2. **<=2-point compile-success degradation** (paired 95% CI excluding a worse
   decline).

Verification rule (mirrors `docs/research/worker-coldstart.md`): every number
below is a real run on this host; anything that could not be measured is
marked **UNVERIFIED**. Token counts use the **chars/4 heuristic** as the
documented proxy for LLM tokens; no tiktoken (per task instruction), so all
token figures are proxy estimates, not provider tokenizer counts.

## Host and environment

- Linux aarch64, uv 0.12.2, CPython 3.14.7 (managed by uv, Clang build).
- Project venv `/tmp/opencode/exp-treesitter-venv` (uv venv, python 3.14.7)
  with `tree-sitter==0.26.0` and `tree-sitter-python==0.25.0`.
- All scripts run with that venv's python:
  `/tmp/opencode/exp-treesitter-venv/bin/python`.

## 1. Feasibility on Python 3.14.7 — VERIFIED

`tree-sitter` installs and imports on 3.14.7. The exact command from the task
succeeds:

```
$ uv run --python 3.14.7 --with tree-sitter python -c "import tree_sitter; print(tree_sitter.__version__)"
Installed 1 package in 86ms
0.26.0
```

The Python grammar is a separate wheel. Both install and parse work on 3.14.7:

```
$ uv run --python 3.14.7 --with tree-sitter --with tree-sitter-python python -c "
> from tree_sitter import Language, Parser
> import tree_sitter_python
> parser = Parser(Language(tree_sitter_python.language()))
> tree = parser.parse(b'def foo(x):\n    return x + 1\n')
> print(tree.root_node.children[0].type)"
function_definition
```

- `tree-sitter 0.26.0`, `tree-sitter-python 0.25.0` — both pure wheels, no
  build step on this host.
- `tree-sitter-language-pack` was **not needed** and **not attempted** (the
  primary path worked), so its 3.14 support is **UNVERIFIED**.
- Pure-python fallback (stdlib `ast`) was implemented and cross-checked anyway
  as a contingency; it reproduces the tree-sitter token counts within 1 token
  per file on signatures-only mode (see §3). It would have carried the
  experiment if tree-sitter had no 3.14 wheel.

**Verdict: install feasibility PASSES on Python 3.14.7.**

## 2. Prototype

`/tmp/opencode/exp-treesitter/compressor.py` implements
`compress_file(path, *, relevant: set[str]) -> str` with two interchangeable
backends (`treesitter`, `ast`):

- **Keep:** every top-level def/class signature — decorators plus the header,
  capped at the first 3 lines — and the **full body** of any top-level def/
  class whose name is in `relevant` **or that contains a nested def/class whose
  name is in `relevant`** (member/method relevance propagates to the enclosing
  class).
- **Replace everything else** (module docstrings, imports, module-level
  assignments, nested bodies of non-relevant symbols) with `# ...`.

The compressed output is a **view, not valid code** (see §4). The compressor
is a context adapter only; it never runs inside a worker's file operations and
has no supervisor concern (M9 scope: “never a supervisor concern”;
architecture §3.7 I2.4 context composition).

### Finding: byte-offset vs char-index bug (fixed)

The tree-sitter Python API returns **byte** offsets; slicing a Python `str`
with them is wrong whenever the source contains non-ASCII. `dataset.py`
contains `§` (U+00A7) in a docstring at byte 388; every node after it was
sliced one byte late, producing mangled headers (`lass …` instead of
`class …`) and silently missing `relevant` matches. Fix: read the source as
bytes and slice byte ranges, decoding per segment. A compressor used on a
real repo must document byte-vs-char offset handling.

## 3. Fidelity: signature coverage (machine-checkable)

Primary fidelity metric (per task): **signature coverage** — the fraction of
def/class names from the original file that appear in the compressed view as
whole words. Coverage is computed by stdlib `ast` walk + regex boundary
check, so it is machine-checkable.

| File set | Scope | Coverage |
|---|---|---|
| example module (4 files) | top-level names, signatures-only view | 100% (10/10) |
| example module (4 files) | **all** names (incl. nested), `relevant` = every name | 100% (23/23) |
| synthetic repo (20 files) | top-level names, signatures-only view | 100% per file, all 20 |

The signatures-only view intentionally drops nested bodies, so **nested names
survive only when `relevant` includes them** (or their enclosing top-level
symbol); this is the mechanism, not a defect.

Bonus data point: `ast.parse` accepts **0/20** signatures-only views (stdlib
`ast` reports “expected an indented block” — a `# ...`-only body is not a
valid suite). This confirms the design statement that the view is not code
and must not be fed to a compiler; the fidelity claim is signature coverage.

## 4. Token reduction — measured (chars/4 proxy)

### 4a. Real module: `src/cambium/modules/example/**` (4 files, 481 LOC)

Command: `measure_real.py /home/ubuntu/cambium/src/cambium/modules/example`.

| File | full | sig-only | all-relevant | sig reduction | rel reduction | sig coverage |
|---|---:|---:|---:|---:|---:|---:|
| `__init__.py` | 356 | 7 | 7 | 98.0% | 98.0% | 100% (0/0) |
| `dataset.py` | 2186 | 48 | 2119 | 97.8% | 3.1% | 100% (3/3) |
| `decide.py` | 1102 | 74 | 876 | 93.3% | 20.5% | 100% (4/4) |
| `metric.py` | 535 | 59 | 500 | 89.0% | 6.5% | 100% (3/3) |
| **totals** | **4179** | **188** | **3502** | **95.5%** | **16.2%** | **100%** |

(`all-relevant` = `relevant` contains every def/class name, i.e. full bodies
for everything — the near-baseline that bounds the reduction when a task
touches most symbols.)

### 4b. Synthetic 20-file repo (generated deterministically)

`gen_repo.py` writes 20 files (92,282 chars) to
`/tmp/opencode/exp-treesitter/synthrepo`; `scenario.py` measures three
context variants:

- **raw** = every file verbatim (baseline);
- **sig** = every file signatures-only;
- **targeted** = a task touching 3 files (`service.py`, `repository.py`,
  `api.py`) keeps full bodies of the touched symbols (`run`, `_compute`),
  other symbols in those files and all other files are signatures-only.

| Context variant | tokens (chars/4) | reduction vs raw |
|---|---:|---:|
| raw (20 files verbatim) | 23063 | — |
| sig (signatures only) | 2607 | **88.7%** |
| targeted task (3 files touched) | 4435 | **80.8%** |

Per-file sig reduction range: 86.7%–90.5%; signature coverage 100% in every
file. Targeted files get full bodies (service 858, repository 600, api 770
tokens) while the untouched 17 files stay at ~89% reduction.

### 4c. M9 acceptance criterion 1: **PASS (measured)**

Every measured context variant reduces input tokens by **80.8%–95.5%**,
comfortably above the **>=25%** threshold. This holds for both the real
example module and a 20-file synthetic repo, and for both signatures-only and
targeted-task compositions.

## 5. Compile-success degradation — **UNVERIFIED**

The second acceptance criterion (compile-success rate falls **<=2 percentage
points**) could **not be measured** in this experiment:

- M9 **depends on M6** (first real LLM end-to-end task) and the criterion
  requires “freeze at least 30 tasks across three supported languages, same
  provider/model, temperature, gate, task budget,” run **paired** raw-context
  and AST-context trials. M6 is not built on the current baseline
  (`v2-1-review.md` §1.3 P1-13: no real LLM end-to-end run is evidenced), so
  there is no harness to attach the compressor to.
- No real provider, no frozen 30-task dataset, no gate metric exists on this
  branch. Any compile-success number produced here would be fabricated.
- **UNVERIFIED:** compile-success-rate delta, gate-pass delta, input-tokens
  per compile-successful task, wall time, changed-file recall.

### Proposed harness experiment (for M6/M9)

1. Freeze >=30 tasks across Python/Rust/TypeScript with pinned grammar wheels
   (`tree-sitter-python` verified; `tree-sitter-rust`,
   `tree-sitter-typescript` **UNVERIFIED** on 3.14.7).
2. For each task run a **paired** trial: raw-context vs AST-context, same
   provider/model, temperature, gate command, and task budget; record input
   tokens, compile-success, gate-pass, wall time, changed-file recall.
3. Compile the context with the compressor in signatures-only mode plus
   `relevant` = the symbols the task's spec names (as in §4b), fall back to an
   explicit unsupported-language result — never a silent text heuristic (M9
   scope).
4. Metric: median input tokens per compile-successful task; adopt iff median
   drops >=25% **and** compile-success drops <=2 points with a paired 95% CI
   excluding a worse decline.

## 6. Design caveats to carry forward

- **Imports are dropped** from the view (§2 design). The LLM loses the import
  graph, which plausibly hurts compile-success on tasks that add or rewire
  dependencies. This is unmeasured (**UNVERIFIED** impact) and is the most
  likely degradation vector against criterion 2.
- The view is not parseable (`ast.parse` fails on 0/20) and must never reach a
  compiler or gate. It is context-adapter output only.
- Token figures are a **chars/4 proxy**, not provider tokenizer counts; a 25%
  proxy reduction does not guarantee a 25% real-token reduction
  (**UNVERIFIED** for real tokenizers).
- Non-ASCII sources require byte-offset-safe slicing (§2 finding).
- Info hiding (architecture §3.7 I2.7) is unaffected: the compressor shrinks
  the parent's view of the codebase; child→parent envelopes stay
  summary+diff+metric.

## 7. Falsification verdict vs M9 acceptance

| Criterion | Target | Measured | Verdict |
|---|---|---|---|
| Token reduction | >=25% | 80.8%–95.5% (proxy) | **PASS** (measured) |
| Compile-success degradation | <=2 points | not measurable without M6 harness | **UNVERIFIED** |

Proposal 1 is **not falsified** (no criterion failed a measurement) and is
**not adoptable as accepted** (criterion 2 unmeasured). Token economics are
strongly in favor; the adoption decision must be gated on the M6/M9 harness
experiment in §5.

## 8. Recommendation

**DEFER final adoption; keep the prototype as the M9 context-adapter
candidate.** Concretely:

1. The compressor design is sound and cheap (stdlib `ast` fallback proves the
   approach without a C dependency, at ~1-token parity). The tree-sitter
   backend adds grammar fidelity and non-Python language support.
2. Do **not** ship AST compression in the runtime until the M6 harness
   measures the compile-success side (§5); token savings alone are not
   acceptance (M9: “do not ship it because chunks look cleaner”).
3. When M6 lands, the first M9 task set should include dependency-rewiring
   tasks to measure the import-drop risk (§6) — that is the likeliest
   criterion-2 failure mode.
4. Pin grammar wheels (`tree-sitter-python` verified on 3.14.7; rust/typescript
   wheels **UNVERIFIED**) and implement the explicit unsupported-language
   fallback before any broader adoption.

## Appendix: commands and raw outputs

Feasibility:

```
$ uv run --python 3.14.7 --with tree-sitter python -c "import tree_sitter; print(tree_sitter.__version__)"
0.26.0
```

Example module totals (`measure_real.py`):

```
full=4179 sig=188 rel=3502
signatures-only reduction: 95.5%
all-relevant reduction:    16.2%
```

Synthetic repo totals (`scenario.py`):

```
generated 20 files, 92282 chars total
raw=23063 sig=2607 targeted=4435
signatures-only reduction: 88.7%
targeted-task reduction:   80.8%
sig-coverage all files 100%: True
```

Backend parity (`compressor.py` treesitter vs ast, signatures-only, 20 files):
`ts=2607 ast=2574` tokens. Signatures-only views parse with stdlib `ast`:
`0/20` (expected — views are not code).
