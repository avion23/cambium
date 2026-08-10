# TUI Best Practices for Cambium (Janus)

**Date:** 2026-08-09. **Scope:** future interface research only; docs-only, no code. **Important:** Cambium currently has **no TUI**. Janus is a future optional adapter for a Python 3.14, embeddable, headless-first harness. Local claims cite commands; web claims cite URLs; unsupported items are **UNVERIFIED**. This snapshot is not runtime authority.

## 1. State of the art (local evidence)

### 1.1 OpenCode

`opencode --version` → `0.0.0-dev-202608071959`. `opencode --help` makes the TUI the default but exposes `run`, `serve`, `attach`, `acp`, and `mcp`; `run --help` supports `--format json` and interactive split-footer mode; `--mini` and `--no-replay` are available. Installed strings identify OpenTUI. Upstream `packages/tui/package.json` uses OpenTUI 0.4.5, SolidJS 1.9.10, and OpenTUI’s native Zig core (`@opentui/core` registry 0.5.1, modified 2026-08-08). The local binary is roughly 147 MB and includes TUI, server, ACP, and web surfaces.

### 1.2 Codex

`codex --version` → `codex-cli 0.146.1`. The arm64 payload is a statically linked Rust binary; `strings` found `ratatui::backend::crossterm`, alternate-screen, and synchronized-update symbols. `codex exec --help` exposes `--json` JSONL, output-last-message, `mcp-server`, app-server, and exec-server. `--no-alt-screen` preserves scrollback. This is the closest headless-first precedent.

### 1.3 Premise check

| agent | TUI | machine path |
|---|---|---|
| OpenCode | OpenTUI (Zig) + SolidJS | `run --format json`, `serve`, ACP/MCP |
| Codex | Rust ratatui + crossterm | `exec --json`, MCP/app/exec servers |
| Toad | Python Textual client | external agents over ACP |

The premise that current leaders use Ink/React is outdated on this machine. Ink remains present (39,603 stars), but it is not the inspected leader stack.

## 2. Python TUI ecosystem (PyPI/API snapshot 2026-08-09)

| library | latest/release | Python | signal |
|---|---|---|---|
| textual | 8.2.8 / 2026-06-30 | >=3.9,<4.0 | active; 36,894 stars |
| rich | 15.0.0 / 2026-04-12 | >=3.9 | active; 57,043 stars |
| prompt_toolkit | 3.0.53 / 2026-07-26 | >=3.10 | active; 10,549 stars |
| urwid | 4.0.8 / 2026-07-26 | >=3.9 | active but dated; 3,012 stars |
| py_cui | 0.1.6 / 2022-09-28 | >=3.6 | stale |
| asciimux | unavailable | — | 404 PyPI/GitHub |

Textual is asyncio-first, Python 3.14-compatible by metadata, supports `App.run(inline=True)` (added 0.55.0), `ansi_color=True` (0.80.0), suspension, textual-web/serve, and headless `Pilot`/`run_test()`. It is the only full-screen Python framework worth future consideration, but its DOM/CSS/reactivity and startup/learning weight exceed a status pane. Rich is the practical ANSI/log/table layer. Prompt Toolkit fits line-oriented REPLs, not dashboards. Urwid is legacy; py_cui and asciimux are dead ends. Sources: https://textual.textualize.io/guide/app/ ; https://textual.textualize.io/api/pilot/ ; https://github.com/Textualize/textual-web ; https://github.com/Textualize/textual-serve

Fast non-Python precedents use thin renderers: Ink (Node/React), OpenTUI (TypeScript + Zig), ratatui/crossterm (Rust), Bubble Tea (Go), and tcell/tview (Go). The common rule is a low-abstraction renderer over an event loop with business logic outside the view.

## 3. Patterns from good TUIs

- **lazygit** (81,164 stars): tcell, bounded panels, keyboard-first, and render-on-demand design. https://raw.githubusercontent.com/jesseduffield/lazygit/master/go.mod
- **k9s** (34,306): tview/tcell, cached API data, diffed views, redraw only changed cells.
- **gh-dash** (12,273): Bubble Tea v2 Elm-style `Init/Update/View`; pure state view and side effects as messages. https://raw.githubusercontent.com/dlvhdr/gh-dash/main/go.mod
- **Toad** (3,361): Python Textual front-end to Claude/Gemini/Codex/OpenHands over ACP; it does not reimplement the agent. https://github.com/batrachianai/toad ; https://agentclientprotocol.com/overview/introduction

Aggregate pattern: an event-driven core emits state/events; renderer is a view. Full-screen is opt-in (`--no-alt-screen`, Textual inline, OpenCode mini). Machine output is first-class (JSONL, HTTP, MCP/ACP). ANSI inline mode preserves scrollback and works with pipes.

## 4. Future recommendation for Cambium

### 4.1 Core-as-library (non-negotiable)

Cambium M1–M9 must not import a terminal UI or use stdout for UI; worker stdout remains Nuntius JSON-lines. When Janus is implemented, expose three adapters:

1. `cambium serve` / module CLI: task input plus JSON-lines events (`type`, correlation/request IDs, flush after write) for the upper layer.
2. `cambium cli`: Rich ANSI status/log view over the same events; plain output when not a TTY, `NO_COLOR`, or `TERM=dumb`.
3. `cambium tui`: explicitly requested, optional Textual dashboard attached to a TTY.

The TUI should consume supervisor events through an `asyncio.Queue`; stderr can feed a bounded log widget and stdout events a per-task status table. Backpressure must drop stale log lines rather than stall workers.

### 4.2 Textual, only if a real dashboard is needed

Textual matches asyncio, Python 3.14 metadata, inline mode, ANSI theme handling, and headless pilot tests. Keep it out of machine protocol and thin CLI. Prefer Rich for streaming logs/tables/spinners.

### 4.3 Terminal and accessibility behavior

Default to inline/streaming; full alternate screen only in explicit `tui`, with `--no-alt-screen`/`CAMBIUM_NO_ALT_SCREEN`. Strip ANSI when piped; honor `sys.stdout.isatty()`, `NO_COLOR` (https://no-color.org), and `TERM=dumb`; ignore `BrokenPipeError`. Keyboard actions must not require a mouse. Inline scrollback supports copy/search/screen readers; dedicated Textual screen-reader/accesskit support is **UNVERIFIED**.

### 4.4 Machine interfaces

#### JSON-Lines stdio — event stream is the interface

Nuntius is newline-delimited JSON over pipes; reusing it keeps the event log byte-identical to the client stream, so replay is the interface. Add request/correlation IDs for control messages without changing the event shape.

| interface | role | verdict |
|---|---|---|
| JSON-lines stdio | canonical session/telemetry; replayable and `tail`/`jq` friendly | **Primary** |
| MCP stdio JSON-RPC 2.0 | expose Cambium as tools | optional adapter |
| ACP stdio/WebSocket | editor-grade client UX; later Toad/JetBrains integration | later adapter |
| HTTP/WebSocket | remote attach/multi-client | defer |
| gRPC | binary service with poor tail/log story | skip |

JSON-lines is already Nuntius’ protocol and avoids socket lifecycle/handshake. Add `request_id` and a small control message for submit/cancel. MCP is request/response, not event streaming; keep JSONL for telemetry. ACP is useful for future editor clients. HTTP adds auth/CORS/port lifecycle; gRPC is unjustified for a same-host leaf module.

## 5. Sources

### Local commands

`opencode --version`, `--help`, `run --help`, `acp --help`, `serve --help`; `codex --version`, `--help`, `exec --help`; `strings` on OpenCode and Codex binaries; `cat ~/.config/opencode/tui.json`, `cat ~/.codex/config.toml`; Codex package metadata. No Cambium TUI code exists; this document is future research.

### Web

https://pypi.org/pypi/textual/json ; https://pypi.org/pypi/rich/json ; https://pypi.org/pypi/prompt-toolkit/json ; https://pypi.org/pypi/urwid/json ; https://pypi.org/pypi/py_cui/json ; https://pypi.org/pypi/asciimux/json ; https://raw.githubusercontent.com/sst/opencode/dev/packages/tui/package.json ; https://registry.npmjs.org/@opentui%2fcore ; https://raw.githubusercontent.com/jesseduffield/lazygit/master/go.mod ; https://raw.githubusercontent.com/dlvhdr/gh-dash/main/go.mod ; https://raw.githubusercontent.com/derailed/k9s/master/go.mod ; https://textual.textualize.io/guide/app/ ; https://textual.textualize.io/ ; https://github.com/batrachianai/toad ; https://agentclientprotocol.com/overview/introduction ; https://modelcontextprotocol.io ; https://no-color.org

### UNVERIFIED

Asciimux disappearance rationale; Textual screen-reader/accesskit status; historical OpenCode Ink/React usage; Claude Code TUI stack. These were not needed for the future recommendation.

## Appendix: snapshot stats

1. Textual 8.2.8 (2026-06-30, Python >=3.9,<4.0); Rich 15.0.0; Prompt Toolkit 3.0.53; Urwid 4.0.8; py_cui 0.1.6 (2022-09-28).
2. Stars snapshot: lazygit 81,164; k9s 34,306; gh-dash 12,273; Ink 39,603; Textual 36,894; Rich 57,043; Toad 3,361; Harlequin 6,313.
3. Local versions: Codex 0.146.1 with ratatui/crossterm; OpenCode `0.0.0-dev-202608071959` with OpenTUI/Zig + SolidJS, JSON run mode, and mini mode.

## Appendix: implementation notes for the future Janus adapter

### Event and rendering boundary

The supervisor should own state transitions, persistence, and worker I/O. Janus should subscribe to a read-only event stream and derive a view model containing task ID, role, phase, last heartbeat, current tool, gate status, worktree, and result. A renderer must not call the provider, mutate a worktree, or decide restart policy. This is the direct application of the lazygit/k9s/gh-dash “view over state” pattern to Cambium’s existing Custos event log.

Use bounded queues for stderr and tool output. A worker may emit frequent logs while a TUI is resized or detached; the queue policy should preserve the latest status and drop old verbose lines rather than apply backpressure to Nuntius. Full tool output remains on disk with a path in the event. A reconnecting client can replay JSONL from a sequence/offset instead of requiring the TUI to own history.

### Screen states and controls

The first future screen can stay small: task list, selected task timeline, worker/gate status, and a detail pane for diffs/errors. Keyboard commands should map to control messages (`submit_task`, `cancel`, `approve`, `open_log`, `follow`) carrying correlation IDs. Destructive controls need the same approval policy as the headless interface; a TUI must not silently bypass Septum. Mouse support is optional.

### Terminal degradation matrix

1. Explicit `cambium tui` on a capable TTY → Textual full-screen.
2. `cambium cli` on a TTY → Rich inline status and logs.
3. Pipe/redirect, `TERM=dumb`, `NO_COLOR`, or CI → plain JSONL or stable tables with no ANSI.
4. Closed downstream pipe → exit cleanly on `BrokenPipeError`.

The same event names and fields must appear in all four modes. The machine interface therefore remains testable without a terminal, while the future full-screen view can be tested with Textual Pilot or a fake event queue. No TUI implementation should be added until the core event contract is stable.

### Protocol choice detail

JSON-lines is one-way unless control messages are added, but it is replayable and cheap for detached instances. MCP’s JSON-RPC request/response semantics are suited to “run this Cambium capability” calls, not continuous task telemetry. ACP is the best later choice for editor-grade lifecycle/diff presentation and can wrap the same event/control model. HTTP/WebSocket should be an explicit remote-attach process, not a hidden dependency of a local task.

The library-version evidence is time-sensitive. Textual’s metadata permits Python 3.14, but the inline-mode Windows caveat remains; Rich’s release and GitHub count do not establish a stable API. OpenCode’s installed TUI config is only `{ "$schema": "https://opencode.ai/tui.json", "theme": "dark" }`, while Codex’s TUI options live in `config.toml`; neither config is a Cambium design contract. Keep Janus future research until a real event schema and a user-facing need exist.
The future Janus adapter should expose no terminal import from the core package. A caller that embeds Cambium in a larger proto-AGI process must be able to consume events through an in-memory queue or stdio without allocating a screen, alternate buffer, color renderer, or TTY thread.

The original source command patterns were `https://pypi.org/pypi/<pkg>/json`, `https://pypi.org/pypi/<textual|rich|prompt-toolkit|urwid|py_cui|asciimux>/json`, and `https://api.github.com/repos/<repo>`; they are retained as provenance patterns, not literal endpoints. OpenCode’s TUI schema URL is `https://opencode.ai/tui.json`.
Future snapshots should retain local command output and library release dates, while keeping Janus recommendations clearly separate from the current no-TUI runtime.
