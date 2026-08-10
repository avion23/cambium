# Python 3.14: verified capabilities for Cambium

**Snapshot (2026-08-09):** historical CPython 3.14.7 run for Cambium; confirm
current semantics in the [Python 3.14 documentation](https://docs.python.org/3.14/whatsnew/3.14.html).
Claims are command output or URLs; unchecked items are **UNVERIFIED**.

## Interpreters used

Installed via `uv python install` into `~/.local/share/uv/python/`:

- Regular (GIL) build: `cpython-3.14.7-linux-aarch64-gnu` (`python3.14`)
- Free-threaded build: `cpython-3.14.7+freethreaded-linux-aarch64-gnu` (`python3.14t`)

Platform: Linux aarch64; uv distributions compiled with Clang 22.1.3 (not
`python.org` binaries).

## Verified facts (with real outputs)

### Interpreter identity

```
$ python3.14 -c "import sys; print(sys.version)"
3.14.7 (main, Aug  5 2026, 15:42:44) [Clang 22.1.3 ]
$ python3.14 -c "import sys; print(sys.version_info)"
sys.version_info(major=3, minor=14, micro=7, releaselevel='final', serial=0)
```

Free-threaded build identifies itself explicitly:

```
$ python3.14t -c "import sys; print(sys.version)"
3.14.7 free-threading build (main, Aug  5 2026, 15:42:52) [Clang 22.1.3 ]
```

### PEP 649 / 749: deferred (lazy) annotation evaluation — default in 3.14

Verified: an annotation naming an undefined identifier raises nothing at
`def` time; it raises only when the annotation value is actually evaluated.

```
$ python3.14 <<'EOF'
def f(x: NonExistentNameAtDefTime) -> int:
    return 1
print("def with undefined annotation name: OK (lazy)")
try:
    f.__annotations__["x"]
except NameError as e:
    print("accessing value -> NameError:", e)
d = f.__annotations__
print("dict access alone: OK, keys:", list(d))
EOF
def with undefined annotation name: OK (lazy)
accessing value -> NameError: name 'NonExistentNameAtDefTime' is not defined
```

The traceback for the access error shows evaluation happens inside a lazily
created `__annotate__` function (the PEP 649 mechanism). The evaluated
`f.__annotations__` remains a normal dict; `f.__annotate__` is the separate
lazy evaluator:

```
Traceback (most recent call last):
  File "<stdin>", line 9, in <module>
  File "<stdin>", line 1, in __annotate__
NameError: name 'NonExistentNameAtDefTime' is not defined
```

`from __future__ import annotations` (PEP 563) still works and stringifies:

```
$ python3.14 <<'EOF'
from __future__ import annotations
def g(x: NonExistentNameAtDefTime) -> int:
    return 1
print("with __future__ annotations: def OK")
print("__annotations__:", g.__annotations__)
EOF
with __future__ annotations: def OK
__annotations__: {'x': 'NonExistentNameAtDefTime', 'return': 'int'}
```

The new `annotationlib` module (PEP 749) exposes three evaluation formats:

```
$ python3.14 <<'EOF'
from annotationlib import get_annotations, Format
def func(arg: UndefinedName) -> int: ...
print("VALUE:", end=" ")
try:
    print(get_annotations(func, format=Format.VALUE))
except NameError as e:
    print("NameError:", e)
print("FORWARDREF:", get_annotations(func, format=Format.FORWARDREF))
print("STRING:", get_annotations(func, format=Format.STRING))
EOF
VALUE: NameError: name 'UndefinedName' is not defined
FORWARDREF: {'arg': ForwardRef('UndefinedName', owner=<function func at 0xe6766b19f530>), 'return': <class 'int'>}
STRING: {'arg': 'UndefinedName', 'return': 'int'}
```

Module-level annotations are fully lazy too: after `x: SomeUndefinedModuleName = 1`,
the module `__annotations__` global is not even materialized until accessed.

### typing / stdlib availability

```
$ python3.14 -c "import typing; print('TypeIs:', hasattr(typing, 'TypeIs')); print('ReadOnly:', hasattr(typing, 'ReadOnly')); print('Self:', hasattr(typing, 'Self')); print('TypeAliasType:', hasattr(typing, 'TypeAliasType')); print('override:', hasattr(typing, 'override'))"
TypeIs: True
ReadOnly: True
Self: True
TypeAliasType: True
override: True
$ python3.14 -c "from typing import TypeVar; T = TypeVar('T', default=int); print('TypeVar(default=) works:', T)"
TypeVar(default=) works: ~T
$ python3.14 -c "import itertools; print('batched:', list(itertools.batched(range(7), 3)))"
batched: [(0, 1, 2), (3, 4, 5), (6,)]
$ python3.14 -c "import asyncio; print('to_thread:', hasattr(asyncio, 'to_thread'), '| timeout:', hasattr(asyncio, 'timeout'), '| TaskGroup:', hasattr(asyncio, 'TaskGroup'))"
to_thread: True | timeout: True | TaskGroup: True
$ python3.14 -c "from sys import monitoring; print('sys.monitoring available')"
sys.monitoring available
```

Note: `typing.TypeIs` and `typing.ReadOnly` are 3.13 features, not new in 3.14;
they are simply present. New in 3.14's typing is that `types.UnionType` and
`typing.Union` are now aliases, and `TypeAliasType` supports star unpacking
(source: https://docs.python.org/3.14/whatsnew/3.14.html#typing).

### Experimental flags on this build

`--help-xoptions` output (complete list from this build):

```
-X context_aware_warnings=[0|1]   (new in 3.14)
-X cpu_count=N
-X dev
-X disable-remote-debug            (new in 3.14, PEP 768)
-X faulthandler
-X frozen_modules=[on|off]
-X importtime[=2]                  (=2 new in 3.14)
-X int_max_str_digits=N
-X no_debug_ranges
-X perf
-X perf_jit
-X pycache_prefix=PATH
-X showrefcount
-X thread_inherit_context=[0|1]    (new in 3.14)
-X tracemalloc[=N]
-X utf8[=0|1]
-X warn_default_encoding
```

Notable: `-X gil`, `-X jit`, and `-X tlbc` do NOT appear in `--help-xoptions`
on this build even though they are documented in the manual and are accepted
by the interpreter. Gap in the CLI help text, not in the interpreter.

`-X gil` status (see GIL section below for behavior):

```
$ python3.14 -X gil=0 -c "import sys; print('with -X gil=0, _is_gil_enabled():', sys._is_gil_enabled())"
Fatal Python error: config_read_gil: Disabling the GIL is not supported by this build
Python runtime state: preinitialized
$ python3.14 -X gil=1 -c "import sys; print('with -X gil=1, _is_gil_enabled():', sys._is_gil_enabled())"
with -X gil=1, _is_gil_enabled(): True
```

`-X gil=0,1` is documented at
https://docs.python.org/3.14/using/cmdline.html#cmdoption-X
(`-X gil=0`
requires `--disable-gil`. `PYTHON_GIL` is the equivalent env switch (verified
on the FT build).

JIT (PEP 744, experimental): enabled only via the `PYTHON_JIT=1` environment
variable on this build; `-X jit`, `-X jit=1`, `-X jit=yes` are accepted but do
not enable it.

```
$ python3.14 -c "import sys; print('is_available:', sys._jit.is_available()); print('is_enabled:', sys._jit.is_enabled())"
is_available: True
is_enabled: False
$ PYTHON_JIT=1 python3.14 -c "import sys; print('is_enabled:', sys._jit.is_enabled())"
is_enabled: True
$ python3.14 -X jit -c "import sys; print('is_enabled:', sys._jit.is_enabled())"
is_enabled: False
```

`sys._jit` introspection namespace has `is_available()`, `is_enabled()`,
`is_active()` (source: https://docs.python.org/3.14/whatsnew/3.14.html#sys).

Caveat: official macOS/Windows binaries include the experimental JIT and
free-threaded builds do not (https://docs.python.org/3.14/whatsnew/3.14.html).
This uv Linux build reports it available (off by default). The 3,000,000-add
microbenchmark ran 0.160s without and 0.228s with it (≈43% slower); this is a
single-machine datapoint (documented range: 10% slower to 20% faster;
https://docs.python.org/3.14/whatsnew/3.14.html#whatsnew314-jit-compiler).

## What's new in 3.14 relevant to Cambium

All claims below are from https://docs.python.org/3.14/whatsnew/3.14.html
unless a real output is pasted.

- **PEP 649/749 deferred annotations (default).** Forward references need no
  quoting; see the verified behavior above. Runtime introspection should use
  `annotationlib.get_annotations()` for explicit format control.
- **PEP 734 subinterpreters + `concurrent.interpreters`.** Multiple
  interpreters in one process, each with its own GIL since 3.12 (PEP 684),
  exposed as a stdlib module in 3.14. Verified:
  ```
  $ python3.14 -c "import concurrent.interpreters as ci; print('concurrent.interpreters:', ci)"
  concurrent.interpreters: <module 'concurrent.interpreters' from '.../lib/python3.14/concurrent/interpreters/__init__.py'>
  $ python3.14 -c "from concurrent.futures import InterpreterPoolExecutor; print('InterpreterPoolExecutor OK')"
  InterpreterPoolExecutor OK
  ```
  Subinterpreters are opt-in; process isolation remains stronger for workers.
- **`python -m asyncio ps|pstree PID`** — introspect running tasks in a
  process. Verified present:
  ```
  $ python3.14 -m asyncio --help
  usage: python3 -m asyncio [-h] {ps,pstree} ...
  ```
  Useful for diagnosing a wedged orchestrator; standard asyncio benchmarks are
  reported 10-20% faster.
- **asyncio free-threading support.** Multiple event loops can run in parallel
  threads on the FT build; this does not affect subprocess-worker parallelism.
- **asyncio `create_task()` kwargs.** `asyncio.create_task()` and
  `TaskGroup.create_task()` pass arbitrary kwargs to the Task factory.
- **`multiprocessing` / `concurrent.futures`.** `forkserver` is now default on
  non-macOS Unix; 3.14 adds `terminate_workers()`, `kill_workers()`, and
  `Process.interrupt()`.
- **PEP 768 remote debugging.** `sys.remote_exec(pid, script)` and
  `python -m pdb -p PID` attach to a process; `-X disable-remote-debug` /
  `PYTHON_DISABLE_REMOTE_DEBUG` gates it.
- **GC note:** 3.14.0–3.14.4 shipped an incremental GC, reverted to the 3.13
  generational GC in 3.14.5+ after production memory-pressure reports.
  Verified on this 3.14.7 build (3-tuple = generational):
  ```
  $ python3.14 -c "import gc; print('gc.get_threshold():', gc.get_threshold())"
  gc.get_threshold(): (2000, 10, 10)
  ```
- **Improved error messages** (keyword typos, `elif` after `else`, unhashable
  types, and context-manager mismatches).
- **Tail-call interpreter** (opt-in `--with-tail-call-interp`): documented
  3-5% pyperformance gain on selected builds; not stock-install relevant.

## GIL / free-threading: the precise truth

The user's belief "the global interrupt lock is gone in 3.14" is **false for
the default build**. The precise truth, verified:

1. **The default CPython 3.14.7 build has a GIL.** `sys._is_gil_enabled()`
   returns `True` and the `Py_GIL_DISABLED` compile-time flag is `0`:
   ```
   $ python3.14 -c "import sys; print('_is_gil_enabled():', sys._is_gil_enabled())"
   _is_gil_enabled(): True
   $ python3.14 -c "import sysconfig; print('Py_GIL_DISABLED compile flag:', sysconfig.get_config_var('Py_GIL_DISABLED'))"
   Py_GIL_DISABLED compile flag: 0
   ```
   Free-threading (PEP 703) is an **opt-in alternate build**, distributed
   separately (uv: `cpython-3.14.7+freethreaded`, binary `python3.14t`). It is
   not what `apt install python3`, `uv python install 3.14.7` (default), or a
   python.org stock installer gives you.
2. **On the free-threaded build the GIL is gone by default**, and can be
   re-enabled at runtime:
   ```
   $ python3.14t -c "import sys; print('_is_gil_enabled():', sys._is_gil_enabled())"
   _is_gil_enabled(): False
   $ python3.14t -c "import sysconfig; print('Py_GIL_DISABLED:', sysconfig.get_config_var('Py_GIL_DISABLED'))"
   Py_GIL_DISABLED: 1
   $ python3.14t -X gil=1 -c "import sys; print('_is_gil_enabled():', sys._is_gil_enabled())"
   _is_gil_enabled(): True
   $ PYTHON_GIL=0 python3.14t -c "import sys; print('FT + PYTHON_GIL=0, enabled:', sys._is_gil_enabled())"
   FT + PYTHON_GIL=0, enabled: False
   ```
3. **Status: officially supported but still optional (phase II, PEP 779).**
   The 3.14 whatsnew states free-threaded "is now supported and no longer
   experimental", that it should be advertised as a supported build option,
   and that a future phase III (making it default) is "still undecided"
   (https://docs.python.org/3.14/whatsnew/3.14.html#free-threaded-python-is-officially-supported).
   So the review's "experimental as of 3.13/3.14" claim is outdated for 3.14,
   but the "not what you get by default" claim is exactly right.
4. **Cost/benefit in 3.14.** The single-threaded performance penalty of the FT
   build is now documented as ~5-10% (down from the 10-40% figure cited in the
   reviews, which was measured on 3.13t) and the specializing adaptive
   interpreter is enabled in FT mode
   (https://docs.python.org/3.14/whatsnew/3.14.html#whatsnew314-free-threaded-cpython).
   FT builds do **not** support the JIT. Two new 3.14 flags default differently
   on FT builds: `-X context_aware_warnings` and `-X thread_inherit_context`
   both default to `1` on FT, `0` on GIL builds.
5. **Per-interpreter GIL (PEP 684) is real but not the default either.** Since
   3.12, subinterpreters have separate GILs, so they run truly in parallel; in
   3.14 they became usable from Python via `concurrent.interpreters`
   (verified above). This is independent of free-threading and opt-in.

## Recommendation for Cambium

**Pin: `requires-python = ">=3.14,<3.15"` on the regular (GIL) build. Do not
target the free-threaded build by default.**

Reasoning (see `docs/architecture/reviews/`; review M5/M1). This is a
historical design recommendation, not a claim that the current repository has
only I/O-bound LLM threads:

- The historical design used subprocess workers plus async I/O, so process
  isolation already gave multi-core parallelism. Current source also has an
  `EventStore` SQLite writer thread (`src/cambium/store.py`) and uses
  `asyncio.to_thread` for event-store append/close, git, worker, tool, file,
  and LLM boundary work (`src/cambium/supervisor.py`, `src/cambium/worker.py`,
  `src/cambium/tools.py`); reassess GIL impact at those boundaries.
- The FT build would add only risk for Cambium: ~5-10% single-threaded
  overhead, no JIT, and C-extension compatibility exposure (DSPy, LiteLLM,
  tokenizers, torch, numpy, orjson). Whether those wheels are FT-safe on this
  platform is **UNVERIFIED**. This historical run did not install them or run
  an FT stress test; current optional extras are declared in `pyproject.toml`.
- Free-threading is officially supported in 3.14 (PEP 779) but optional;
  plain `python3.14` remains a GIL build.
- Keep free-threading **optional and additive**, gated by
  `sys._is_gil_enabled()`/`Py_GIL_DISABLED` if SIMBA needs thread-level CPU
  parallelism; use `ProcessPoolExecutor` or `InterpreterPoolExecutor` as the
  fallback. On the GIL build, the latter gives multi-core without FT risk.
- Use `>=3.14,<3.15` rather than an exact pin so security patch releases
  (3.14.x) are picked up; `>=3.14` alone is acceptable if no upper bound is
  wanted.
- One more 3.14 migration note for the orchestrator: `forkserver` is now the
  default multiprocessing start method on Linux — if Cambium forked child
  processes relying on `fork` semantics, that must be re-validated.

## Sources

- What's New in Python 3.14: https://docs.python.org/3.14/whatsnew/3.14.html
- Command-line and environment (`-X gil`, `-X thread_inherit_context`, etc.):
  https://docs.python.org/3.14/using/cmdline.html
- PEP 649 (deferred annotations):
  https://peps.python.org/pep-0649/
- PEP 749 (implementing PEP 649):
  https://peps.python.org/pep-0749/
- PEP 703 (free-threaded CPython):
  https://peps.python.org/pep-0703/
- PEP 779 (free-threaded officially supported):
  https://peps.python.org/pep-0779/
- PEP 684 (per-interpreter GIL):
  https://peps.python.org/pep-0684/
- PEP 734 (multiple interpreters in stdlib):
  https://peps.python.org/pep-0734/
- PEP 744 (experimental JIT):
  https://peps.python.org/pep-0744/
- PEP 768 (safe external debugger interface):
  https://peps.python.org/pep-0768/
- `annotationlib` module: https://docs.python.org/3.14/library/annotationlib.html
- `concurrent.interpreters` module: https://docs.python.org/3.14/library/concurrent.interpreters.html

## UNVERIFIED items

- Free-threading safety of the DSPy/LiteLLM/torch/numpy wheel set on aarch64
  Linux (no dependencies installed; no stress test run).
- Whether the free-threaded build changes Cambium workload performance on this
  hardware (no Cambium code in this repo, only docs; no benchmark run).
- JIT behavior on macOS/Windows official binaries (verified only on the uv
  Linux aarch64 build).
- The documented ~5-10% FT single-threaded overhead and 3-5% tail-call
  interpreter gains are the CPython project's measurements, not reproduced
  here.
