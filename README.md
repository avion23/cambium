# Cambium

Cambium is a Python-native multi-agent coding-agent harness: a
supervisor process manages worker processes, each running a coding agent
in an isolated git worktree. See `docs/system-design.md` for the system
design and `docs/architecture.md` for the implementation architecture.

This repository currently holds the project scaffold: the public
`Orchestrator` skeleton, the event-log seed, and the reference
`should_decompose` decision module (the per-module pattern that every
future Cambium module follows).

## Development

Requires Python 3.14.

```sh
uv run --python 3.14.7 --extra test pytest -q
```
