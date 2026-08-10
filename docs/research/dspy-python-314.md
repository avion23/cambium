# DSPy on Python 3.14.7: compatibility research

**Snapshot (2026-08-09):** historical compatibility run; verify current
metadata at the [dspy PyPI JSON](https://pypi.org/pypi/dspy/json) and
[pydantic PyPI JSON](https://pypi.org/pypi/pydantic/json). Every local claim is
a command result; unchecked items are **UNVERIFIED**.

Environment: uv 0.12.2 (`aarch64-unknown-linux-gnu`), Linux aarch64; `uv run` in
`/tmp/opencode/exp-dspy` (not `/tmp/opencode/cambium-dspy`); interpreters are
`cpython-3.14.7` and `cpython-3.14.7+freethreaded` from `uv python list`.
The experiment's empty-dependency project is historical; it is not the current
repository packaging.

## Bottom line

- **DSPy works on Python 3.14.7 (GIL build).** Latest release **dspy 3.3.0**
  installs, imports, configures, and runs a `dspy.Predict` forward (with a
  fake LM) on 3.14.7. Verified end-to-end.
- **Newest working version: 3.3.0** (the latest release, 2026-08-03).
- **Metadata floor for 3.14 support: 3.1.0.** dspy 3.0.x and 2.6.x declare
  `requires-python = "<3.14,..."` on PyPI, excluding 3.14. 3.1.0+ declares
  `>=3.10,<3.15`, which includes 3.14.
- **pydantic v2 works on 3.14.7:** 2.13.4 installed and validated a model.
  Explicitly classified for Python 3.14 on PyPI.
- **Free-threaded 3.14 build: NO.** dspy 3.3.0 fails to build because its
  dependency `orjson 3.11.9` does not support free-threaded Python.

## Verified: dspy latest (3.3.0) installs and imports on 3.14.7

Command (workdir `/tmp/opencode/exp-dspy`):

```
$ uv run --python 3.14.7 --with dspy python -c "import dspy; print(dspy.__version__)"
Downloading litellm (25.3MiB)
Downloading aiohttp (1.7MiB)
Downloading tiktoken (1.1MiB)
Downloading pydantic-core (1.9MiB)
Downloading hf-xet (4.1MiB)
Downloading tokenizers (3.2MiB)
 Downloaded tiktoken
 Downloaded pydantic-core
 Downloaded hf-xet
 Downloaded aiohttp
 Downloaded tokenizers
 Downloaded litellm
Installed 57 packages in 593ms
3.3.0
```

All 57 packages came from wheels; no source build. Resolved versions in that
environment (verified via `import importlib.metadata as m`):

```
dspy 3.3.0
pydantic 2.13.4
pydantic_core 2.46.4
orjson 3.11.9
litellm 1.96.0
openai 2.53.0
tenacity 9.1.4
```

## Verified: smoke test — configure + Predict + forward (no network)

`dspy.configure(lm=dspy.LM('openai/gpt-4o-mini', api_key='dummy'))` does not
touch the network. A `dspy.Predict("question -> answer")` instantiates. A
forward with a fake LM (subclass of `dspy.LM` overriding `__call__` to return a
literal JSON string) completes without any HTTP request:

```
$ uv run --python 3.14.7 --with dspy python smoke_dspy.py
python: 3.14.7
dspy: 3.3.0
configure: OK (no network)
Predict instantiation: OK
forward: OK -> Prediction(
    answer='42'
)
```

First attempt with the fake LM returning non-JSON `"Fake answer"` reached the
adapter parse stage and raised `dspy.utils.exceptions.AdapterParseError`
("LM response cannot be serialized to a JSON object") — i.e. the full forward
pipeline (adapter formatting, fake LM call, parsing) ran with **zero network
I/O**; only the fake LM's output format was wrong. Returning
`'{"answer": "42"}'` completed the forward.

## Verified: version pins on 3.14.7

The newest release works, so older-version probing answers "what is the floor".
Full smoke (same script as above) run against two pins:

```
$ uv run --python 3.14.7 --with "dspy==3.1.0" python smoke_dspy.py
python: 3.14.7
dspy: 3.1.0
configure: OK (no network)
Predict instantiation: OK
forward: OK -> Prediction(
    answer='42'
)
```

dspy **3.1.0** (first release whose `requires-python` includes 3.14) passes the
same smoke. Curiosity: dspy **3.0.4**, whose metadata says
`Requires-Python: <3.14,>=3.10` (verified via
`importlib.metadata.distribution('dspy').metadata['Requires-Python']`), was
still installed by uv and passed the smoke:

```
$ uv run --python 3.14.7 --with "dspy==3.0.4" python smoke_dspy.py
python: 3.14.7
dspy: 3.0.4
configure: OK (no network)
Predict instantiation: OK
forward: OK -> Prediction(
    answer='42'
)
```

Note: uv 0.12.2 did **not** enforce the `<3.14` cap for a direct `--with
"dspy==3.0.4"` pin (no warning emitted). It works empirically, but its own
metadata declares 3.14 unsupported — do not rely on it; use `>=3.1.0`.

## Verified: pydantic v2 on 3.14.7

```
$ uv run --python 3.14.7 --with pydantic python -c "import pydantic; print(pydantic.__version__)"
Installed 5 packages in 9ms
pydantic 2.13.4
```

pydantic is functional, not just importable — a model roundtrip runs:

```
$ uv run --python 3.14.7 --with pydantic python -c "import pydantic; from pydantic import BaseModel
class M(BaseModel): x: int
m = M(x=1); assert m.model_dump() == {'x': 1}
print('pydantic', pydantic.__version__, 'model roundtrip OK')"
pydantic 2.13.4 model roundtrip OK
```

## Verified: free-threaded 3.14 build — FAILS

```
$ uv run --python 3.14.7+freethreaded --with dspy python -c "import dspy"
...
        orjson v3.11.9 does not support free-threaded Python
...
      💥 maturin failed
        Caused by: Failed to build a native library through cargo
...
hint: `orjson` (v3.11.9) was included because `dspy` (v3.3.0) depends on `orjson`
hint: Build failures usually indicate a problem with the package or the build environment
```

dspy 3.3.0 does not install on the freethreaded 3.14 build: its `orjson`
dependency has no free-threaded wheel and fails compiling from source. Whether
any dspy version installs there is **UNVERIFIED** (not probed further).

## Web cross-checks (URLs)

- dspy PyPI JSON: https://pypi.org/pypi/dspy/json
  - Latest `info.version` = `3.3.0`; `info.requires_python` =
    `<3.15,>=3.10` → Python 3.14 is within range. Classifiers only
    "Programming Language :: Python :: 3" (no per-minor classifiers).
  - Release `requires_python` history (from the same JSON), first column =
    newest file upload date for that version:
    - `2.6.0` (2025-01-30) → `>=3.9`
    - `2.6.5` (2025-02-20) → `>=3.9` (no upper cap)
    - `2.6.6`…`2.6.24` → `>=3.9,<3.13` (3.14 excluded)
    - `2.6.25`…`2.6.27` (2025-06) → `>=3.9,<3.14` (3.14 excluded)
    - `3.0.0`…`3.0.4` → `>=3.10,<3.14` (3.14 excluded)
    - `3.1.0` (2026-01-06) → `>=3.10,<3.15` (3.14 included) — **first**
    - `3.2.x`, `3.3.0` → `>=3.10,<3.15` (3.14 included)
- dspy GitHub latest release:
  https://api.github.com/repos/stanfordnlp/dspy/releases/latest
  - `tag_name` = `3.3.0`, `published_at` = `2026-08-03T20:06:03Z`,
    `prerelease` = `false`. Release notes state API/dependency changes
    (OpenAI ≥ 1.66.2, LiteLLM ≥ 1.65.8) but no explicit Python-version policy.
- dspy README: https://raw.githubusercontent.com/stanfordnlp/dspy/main/README.md
  - No explicit Python-version statement (badge removed). Install section is
    just `pip install dspy`. Official support claim therefore lives in the
    PyPI `requires-python` metadata above.
- pydantic PyPI JSON: https://pypi.org/pypi/pydantic/json
  - `info.version` = `2.13.4`; `info.requires_python` = `>=3.9`; classifier
    `Programming Language :: Python :: 3.14` present explicitly.

## Recommendation for Cambium

1. **Current repository packaging:** `pyproject.toml` declares runtime
   `dependencies = ["pytest>=9"]`, optional `dspy = ["dspy>=3.3.0,<3.4"]`,
   and repeats that pin in `test`/`dev` extras. The experiment's
   `dependencies = []` was historical, not current.
2. **Historical runtime recommendation:** per-module DSPy programs must run on
   the GIL 3.14 build. The
   freethreaded build cannot install dspy (orjson blocker). This matches the
   existing `docs/research/python-3.14.md` recommendation to target the regular
   build and keep free-threading optional/additive. The Cambium module
   protocol should import dspy lazily inside each module's `decide.py` so a
   missing dspy extra degrades to a clear error instead of failing the harness.
3. **No per-module design change is needed.** The
   `dspy.configure(lm=...)` + `dspy.Predict` + forward pattern works on 3.14.7;
   the fake-LM subclass supports offline tests.
4. Be aware of dependency weight: `--with dspy` resolves 57 packages (litellm,
   openai, httpx, tiktoken, tokenizers, pydantic-core, orjson, …). For
   environments that only need Predict/optimizers without LiteLLM provider
   calls, this is the price of the current dspy 3.x dependency set.

## Sources

See **Web cross-checks**; companion results are in
`docs/research/python-3.14.md`.

## UNVERIFIED items

- Whether any dspy version installs on the free-threaded 3.14 build (only
  3.3.0 tested; fails at `orjson 3.11.9`).
- A real provider round-trip (network) with dspy on 3.14.7 — the forward used a
  fake LM; no real API call was made.
- dspy optimizer runs (`dspy.GEPA`, `BootstrapFewShot`, …) on 3.14.7 — not
  exercised.
- Full test suite of dspy 3.3.0 on 3.14.7 — only import, configure, Predict
  instantiation, and a fake-LM forward were run.
