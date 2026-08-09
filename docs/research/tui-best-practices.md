# TUI Best Practices for Cambium (Janus)

**Date:** 2026-08-09
**Author:** research task (docs-only, no code)
**Scope:** interface strategy for the Cambium coding-agent harness (`docs/architecture/system-design.md`, module M10 "Janus"). Cambium is Python 3.14 (free-threaded target), a reusable leaf module inside a larger proto-AGI system, and the higher layer spawns/persists Cambium instances programmatically. The interface must therefore be **embeddable**: TUI optional, never core.

All local claims cite the exact command and output. All web claims cite URLs. Anything not verifiable is marked **UNVERIFIED**.

---

## 1. State of the art (local evidence)

Two dominant coding agents are installed on this machine. Their interface architecture directly answers "what does the current generation actually ship?"

### 1.1 opencode — TUI-first monolith, opentui + SolidJS + Zig core

```bash
$ opencode --version
0.0.0-dev-202608071959
```

`opencode --help` shows the TUI is the **default** command and everything else is an escape hatch from it:

```
Commands:
  opencode completion          generate shell completion script
  opencode acp                 start ACP (Agent Client Protocol) server
  opencode mcp                 manage MCP (Model Context Protocol) servers
  opencode [project]           start opencode tui                                          [default]
  opencode attach <url>        attach to a running opencode server
  opencode run [message..]     run opencode with a message
  ...
  opencode serve               starts a headless opencode server
  opencode web                 start opencode server and open web interface
```

Relevant flags (from `opencode --help`):

```
  --mini          start the minimal interactive interface             [boolean] [default: false]
  --no-replay     disable mini session history replay on resume and after resize       [boolean]
```

`opencode run --help` exposes the machine interface:

```
  --format       format: default (formatted) or json (raw JSON events)
                 [string] [choices: "default", "json"] [default: "default"]
  --interactive  run in direct interactive split-footer mode          [boolean] [default: false]
```

TUI internals (verified from the installed binary + upstream repo):

```bash
$ strings ~/.local/bin/opencode | grep -o "_appName=\"[a-z]*\"" | sort -u
_appName="opentui"
```

`~/.config/opencode/tui.json` (the whole TUI configuration surface):

```json
{ "$schema": "https://opencode.ai/tui.json", "theme": "dark" }
```

Upstream `packages/tui/package.json` (branch `dev`) declares: `@opentui/core`, `@opentui/keymap`, `@opentui/solid`, `solid-js`, `opentui-spinner`. The npm registry describes the core (https://registry.npmjs.org/@opentui%2fcore):

> "OpenTUI is a TypeScript library on a native Zig core for building terminal user interfaces (TUIs)" — `@opentui/core` 0.5.1, last modified 2026-08-08.

**Interpretation.** opencode ships one ~147 MB Bun-compiled ELF containing a full-screen TUI (tree-sitter syntax highlighting, command palette, etc.), a headless HTTP server, ACP server, and web UI. This is the "feature-heavy, slow" archetype users complain about. Notably, even the TypeScript ecosystem has moved **off** React/Ink to a native-Zig-core TUI for performance — see §3.4.

### 1.2 codex (OpenAI) — Rust TUI, but non-interactive first-class

```bash
$ codex --version
codex-cli 0.146.1
```

Distributed via npm platform packages (`@openai/codex` 0.146.1 → `@openai/codex-linux-arm64`), but the payload is a statically-linked Rust binary:

```bash
$ file ~/.local/npm-global/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex
ELF 64-bit LSB executable, ARM aarch64, version 1 (SYSV), statically linked, stripped

$ strings <same binary> | grep -iE "ratatui|crossterm" | sort -u | head -3
ratatui::backend::crossterm
crossterm::terminal::EnterAlternateScreen
crossterm::terminal::BeginSynchronizedUpdate
```

So codex's TUI is **ratatui + crossterm** (Rust). It ships an explicit escape hatch from the full-screen TUI:

```
  --no-alt-screen
          Disable alternate screen mode
          Runs the TUI in inline mode, preserving terminal scrollback history.
```

And a first-class non-interactive path (`codex exec --help`):

```
  --json
          Print events to stdout as JSONL
  -o, --output-last-message <FILE>
          Specifies file where the last message from the agent should be written
```

plus `codex mcp-server` ("Start Codex as an MCP server (stdio)"), `codex app-server`, `codex exec-server`. `~/.codex/config.toml` contains TUI-specific configuration sections (`[tui.model_availability_nux]`), confirming the TUI is configurable but not load-bearing.

**Interpretation.** Codex is the closest existing precedent for the recommendation in §5: a headless core with a thin interactive layer, a `--no-alt-screen` inline fallback, and JSON-Lines + MCP machine interfaces.

### 1.3 What this means for the premise

The task premise ("most coding agents use Ink/React") is **outdated on this machine**:

| Agent | TUI stack | Non-interactive / machine path |
|---|---|---|
| opencode | opentui (TS, Zig core) + SolidJS | `run --format json`, `serve`, `attach`, `acp`, `mcp` |
| codex | Rust ratatui + crossterm | `exec --json` (JSONL), `mcp-server`, `app-server` |
| Toad (Textual, Python) | Textual | wraps agents via ACP (WebSocket) |

Ink/React TUIs still exist (Ink: 39,603 GitHub stars) but are not the stack of the current coding-agent leaders.

---

## 2. Python TUI ecosystem (versions/dates)

Verified via the PyPI JSON API on 2026-08-09 (`https://pypi.org/pypi/<pkg>/json`).

| Library | Latest | Released | Requires Python | Maintenance signal |
|---|---|---|---|---|
| **textual** (Textualize) | 8.2.8 | 2026-06-30 | `>=3.9,<4.0` | Active; repo pushed 2026-07-11; 36,894 ★ |
| **rich** (Textualize) | 15.0.0 | 2026-04-12 | `>=3.9.0` | Active; 57,043 ★ |
| **prompt_toolkit** | 3.0.53 | 2026-07-26 | `>=3.10` | Active; 10,549 ★ |
| **urwid** | 4.0.8 | 2026-07-26 | `>=3.9.0` | Active but low velocity; 3,012 ★ |
| **py_cui** | 0.1.6 | 2022-09-28 | `>=3.6` | **Stale** — no release in ~4 years |
| **asciimux** | — | — | — | **Unavailable** — 404 on both PyPI and GitHub (`vimalloc/asciimux`) as of 2026-08-09 |

### 2.1 textual — the only full-screen framework worth considering in Python

- Async-first: "Textual is powered by Python's asyncio framework" (https://textual.textualize.io/guide/app/). This matters for Cambium because the supervisor (Custos) and orchestrator are already asyncio-native (system-design §3).
- Python 3.14 compatible by metadata (`requires_python >=3.9,<4.0`).
- **Inline mode** (`App.run(inline=True)`, added in 0.55.0): "the app will appear beneath the prompt (and won't go into application mode)"; caveat: not supported on Windows (https://textual.textualize.io/guide/app/). This is the Python equivalent of codex's `--no-alt-screen`.
- **ANSI color opt-in** (`ansi_color=True`, added 0.80.0): preserves the user's terminal theme — an accessibility/customization lever (same URL).
- **Suspension**: `App.suspend()` context manager can drop to an external editor; "unavailable with textual-web" (same URL).
- **Browser/remote serving**: textual-web (https://github.com/Textualize/textual-web, 1,437 ★) and textual-serve (https://github.com/Textualize/textual-serve, 398 ★) run TUIs in a browser; "Textual apps can run over SSH".
- **Headless testing**: `textual.pilot` API + `App.run_test()` exist for programmatic driving (https://textual.textualize.io/api/pilot/).
- Reference apps: **Harlequin** SQL IDE (6,313 ★), **Posting** API client (12,244 ★), Toolong log viewer (3,938 ★), Memray, Dolphie.

Known cost: Textual is a *widget/DOM* framework (CSS, layouts, reactivity). It is more abstraction than Cambium needs for a status/control pane, and it brings real startup weight and a learning curve. It is the right choice only if Cambium actually ships a full-screen TUI.

### 2.2 rich — the right tool for plain ANSI output

rich is a render-library, not a full-screen framework. For streaming logs, tables, and spinners to a pipe or TTY it is the pragmatic default and is already the rendering layer Textual sits on. For a headless core with a *minimal* CLI face, rich + a JSONL machine interface covers ~95% of needs with ~1% of Textual's API surface.

### 2.3 prompt_toolkit — line-oriented, REPL-style

prompt_toolkit (10,549 ★) is the right tool for a prompt/auto-suggest interface (it powers IPython, pgcli, etc.) but not for a multi-pane agent dashboard. It is an "input" library, not a "dashboard" library.

### 2.4 urwid — legacy, viable but dated

urwid (4.0.8, 3,012 ★) is the old workhorse (palette/attribute model, mainloop). It has no async-native event model, no CSS, no inline mode, and its docs/community trail Textual by a wide margin. Not recommended for new work.

### 2.5 py_cui, asciimux — dead ends

- **py_cui**: last release 0.1.6 on 2022-09-28. Unmaintained for ~4 years.
- **asciimux**: 404 on PyPI and GitHub as of 2026-08-09 (verified: `urllib` → `HTTP Error 404` on both `pypi.org/pypi/asciimux/json` and `github.com/vimalloc/asciimux`). Treat as gone; **UNVERIFIED** why (no archived page reachable).

### 2.6 The node/Go/Rust comparison the premise needs

| Library | Stack | Signal |
|---|---|---|
| Ink | Node.js/React | 39,603 ★; the "slow React in a terminal" archetype users complain about |
| opentui | TypeScript + **native Zig core** | used by opencode; exists *because* pure-JS TUIs were too slow |
| ratatui + crossterm | Rust | used by codex; immediate-mode, minimal |
| Bubble Tea v2 | Go | used by gh-dash, charmbracelet |
| tcell / tview | Go | used by lazygit / k9s (low-level cell model) |

The common thread in the fast implementations (lazygit, k9s, gh-dash, codex): **a thin, low-abstraction rendering layer over a plain event loop, with the business logic kept out of the renderer.** Textual is the Python analog of that family, but heavier than the Go/Rust ones.

---

## 3. Patterns from good TUIs

### 3.1 lazygit — the design bar (81,164 ★)

`go.mod` (https://raw.githubusercontent.com/jesseduffield/lazygit/master/go.mod) shows a deliberately thin stack: `gdamore/tcell/v3` (low-level cell terminal), no full widget framework, plus a small `lazycore` helper. Design philosophy (docs/README): "90% of git commands in 10% of the keystrokes", panels preload and render on demand, every panel is bounded and cheap, keyboard-first, no animation churn. Speed comes from *what it doesn't do*: no DOM, no reactive diffing, no per-frame full redraws.

### 3.2 k9s — minimal redraw + caching (34,306 ★)

k9s (`go.mod`: `derailed/tview`, `derailed/tcell`) is a cluster dashboard that stays responsive under thousands of rows by caching API responses, diffing views, and only redrawing changed cells. Lesson: **the TUI is a *view* over an event/state stream, not a database.**

### 3.3 gh-dash — Bubble Tea composability (12,273 ★)

gh-dash (`go.mod`: `charm.land/bubbletea/v2`, `charm.land/bubbles/v2`, `charm.land/glamour/v2`) models the UI as a pure state machine (`Model` with `Init/Update/View`) — an Elm-style loop. The entire UI is a pure function of state; side effects (GitHub API) happen as messages. This is directly composable with Cambium's existing CSP/asyncio event loop (system-design §2.1).

### 3.4 Toad — the exact precedent for Cambium (Python + agents + ACP)

Toad (https://github.com/batrachianai/toad, 3,361 ★, Python) is "A unified interface for AI in your terminal" — a **Textual front-end that connects to external coding agents** (Claude Code, Gemini CLI, Codex, OpenHands) over the **Agent Client Protocol** (https://agentclientprotocol.com/overview/introduction). It does *not* re-implement the agent; it is a client. This is the architectural role Janus should play, inverted: Cambium is the *agent*, and its TUI is an optional client of Cambium's own machine interface.

### 3.5 Aggregate patterns

1. **Event-driven core, renderer is a view.** lazygit/k9s/gh-dash/Toad all keep business logic out of the renderer. Cambium already has this shape: the supervisor emits an event log (`.cambium/events.jsonl`); the TUI should subscribe to that stream, not own it.
2. **Full-screen is opt-in.** codex `--no-alt-screen`, Textual `inline=True`, opencode `--mini` — every serious tool provides a non-full-screen mode for CI, pipes, and scrollback preservation.
3. **Machine output is a first-class citizen.** `codex exec --json` (JSONL), `opencode run --format json`, `opencode serve` (HTTP), MCP/ACP servers. The interactive TUI is the *optional* consumer.
4. **Low-abstraction renderers win on speed.** tcell/ratatui/crossterm beat React-Ink and feature-heavy DOM TUIs. In Python, that means: prefer rich + prompt_toolkit for thin interfaces; only reach for Textual if a real dashboard is needed.
5. **ANSI inline over alternate screen** for anything log-heavy, so scrollback, copying, and `tee` keep working.

---

## 4. Recommendation for Cambium (headless-first, embeddable)

### 4.1 Core-as-library (non-negotiable)

Keep the supervisor (Custos), orchestrator (Architectus), and event log **pure library code with no terminal import**. Nothing in M1–M9 may touch `sys.stdout` for UI purposes (worker stdout is already reserved for Nuntius JSON-Lines per system-design §4.1). Janus (M10) becomes **three thin adapters**, each an optional entry point:

1. **`cambium serve` / `python -m cambium`** — headless process that reads a task spec from stdin/args and emits **JSON-Lines events to stdout** (identical framing to Nuntius §4.1: `{"type": "...", ...}` per line, flush after write). This is the *default* and the only interface the proto-AGI upper layer ever needs.
2. **`cambium cli`** — no full-screen: rich table/spinner/progress over the same event stream; falls back to plain text when `stdout` is not a TTY or `NO_COLOR`/`TERM=dumb` is set.
3. **`cambium tui`** — optional full-screen **Textual** dashboard, only when attached to a TTY and explicitly requested.

### 4.2 Why Textual (and only for the optional TUI)

- Python 3.14-compatible (`requires_python >=3.9,<4.0`), active, best-maintained Python TUI.
- asyncio-native — same event loop model as Custos; the TUI can subscribe to the supervisor's event bus directly via `asyncio.Queue` without threads.
- `inline=True` mode gives the codex-style `--no-alt-screen` fallback.
- `ansi_color=True` and 16-ANSI-color awareness address terminal-theme accessibility; inline mode preserves scrollback for screen-reader and copy workflows.
- Pilot/`run_test()` gives headless TUI tests.

**Do not** use Textual for the machine interface or the CLI tier. It is a view layer, not a protocol.

### 4.3 Subprocess supervision and live log streaming

Custos already supervises worker processes and reads their stdout (Nuntius JSONL) and stderr (free text). Concretely:

- Route worker **stderr** into a `RichLog`/`Log` widget in the TUI via an `asyncio.Queue`; never let the TUI block the supervisor loop (system-design review finding F1 already forbids blocking I/O in the event loop — the TUI must consume through a queue, and backpressure must drop-to-latest for log lines, not stall workers).
- Route worker **stdout events** (heartbeat/tool_event/checkpoint/result) to a per-task status pane. These are already structured — render them as a table, not a log.
- `cambium cli`/`serve` simply echo the same events, optionally filtered with `--events tool_event,heartbeat` and `--json`.

### 4.4 ANSI vs full-screen

- Default to **inline/streaming** (ANSI + rich), matching codex's `--no-alt-screen` and Textual's inline mode.
- Full alternate-screen only in `cambium tui`, and honor `CAMBIUM_NO_ALT_SCREEN` / `--no-alt-screen` so logs stay in scrollback.
- Strip ANSI when output is piped: `sys.stdout.isatty()` gating + `NO_COLOR` (https://no-color.org).

### 4.5 Terminal fallbacks (CI, no TTY)

Order of degradation, all automatically detected:

1. TTY + requested → Textual full-screen.
2. TTY → rich ANSI dashboard.
3. Not a TTY (`sys.stdout.isatty() is False`), or `TERM=dumb`, or `NO_COLOR` set → plain, uncolored JSONL/table output (this is the CI case; `codex exec --json` and `opencode run --format json` prove agents must behave when piped).
4. Never crash on `BrokenPipeError` when downstream closes (standard Unix CLI hygiene).

### 4.6 Accessibility notes

- Keyboard-first: every action reachable without mouse (Textual keybindings, `Binding`).
- Inline mode preserves terminal scrollback — required for screen-reader users and terminal-emulator search.
- `ansi_color=True` keeps the user's theme; avoid hardcoded low-contrast colors.
- Marked **UNVERIFIED**: dedicated screen-reader integration (e.g., BRLTTY/accesskit) for Textual as of 2026-08-09; no authoritative doc located.

---

## 5. Machine interfaces for the upper layer (tradeoffs)

The proto-AGI layer spawns and persists Cambium instances programmatically. The interface must be (a) trivially parseable, (b) replayable for crash recovery, (c) idle-cheap when no client is attached.

### 5.1 JSON-Lines on stdio — RECOMMENDED primary

- **Already the house protocol.** Nuntius is newline-delimited JSON over pipes (system-design §4.1). Reusing it means zero new serialization, zero framing, and the event log (`.cambium/events.jsonl`) is byte-identical to what a client sees — replay *is* the interface.
- **Cost:** streaming one-way by default. Add a `request_id`/`correlation_id` field and a small `{"type":"control","request_id":...}` message family to get request/response for commands like `submit_task`/`cancel`.
- **Ops story:** `tail -f`, `jq`, `rg`, `split` all work on it; log rotation is `mv`; a crashed instance is inspected by replaying the log (system-design already relies on this).
- **Performance:** no sockets, no handshake, no server lifecycle; a detached instance consumes zero resources when its stdout is a closed file/pipe.

### 5.2 MCP (Model Context Protocol, stdio JSON-RPC 2.0)

- Expose Cambium as an **MCP server over stdio** (`cambium mcp-server`), mirroring `codex mcp-server` and `opencode mcp`. Best when the upper layer wants Cambium's capabilities as *tools* in an agent loop rather than a long-lived session.
- Tradeoffs: MCP is a tool registry + call protocol (JSON-RPC), so it is request/response shaped, not a live event stream; you still need the JSONL log for streaming/telemetry. MCP adds a schema dependency (https://modelcontextprotocol.io) for marginal gain over JSONL at this scale.
- **Recommendation: optional second adapter, not the core.**

### 5.3 ACP (Agent Client Protocol, JSON-RPC over stdio/WebSocket)

- Purpose-built for *agent↔client* UX (diffs, turn lifecycle), reuses MCP's JSON shapes (https://agentclientprotocol.com/overview/introduction). This is what Toad speaks to render agents, and opencode implements it (`opencode acp`).
- **Recommendation:** the right path if the proto-AGI layer (or a future Toad-like front-end) wants editor-grade agent UX. Adopt as a later adapter; do not build the core around it.

### 5.4 WebSocket / HTTP — only for remote attach

- `opencode serve` + `attach` is the model: a long-lived headless server, multiple/detached clients, cross-host. Adds port binding, auth, CORS — meaningful operational surface for a leaf module that normally runs on the same host.
- **Recommendation: defer.** If needed later, add a thin `cambium serve --ws` that wraps the same JSONL event stream (Textual already ships WebSocket support via textual-web, but do not couple the core to it).

### 5.5 gRPC — not recommended

Binary framing, codegen, server lifecycle, and no story for tail-able logs. Optimizes for a cross-host microservice fleet; Cambium is a same-host leaf module. **Skip.**

### 5.6 Decision table

| Interface | Role for Cambium | Verdict |
|---|---|---|
| JSON-Lines stdio | canonical session/telemetry protocol | **Primary (matches Nuntius)** |
| MCP stdio server | expose Cambium as tools to the upper layer | Optional adapter |
| ACP (stdio/WS) | editor-grade agent UX for upper layer / Toad-like clients | Later adapter |
| HTTP/WebSocket | remote attach, multi-client | Defer |
| gRPC | — | Skip |

---

## 6. Sources

### Local (commands cited with outputs above)
- `opencode --version`, `opencode --help`, `opencode run --help`, `opencode acp --help`, `opencode serve --help`
- `codex --version`, `codex --help`, `codex exec --help`
- `strings ~/.local/bin/opencode` (opentui/tree-sitter), `strings <codex rust binary>` (ratatui/crossterm)
- `cat ~/.config/opencode/tui.json`, `cat ~/.codex/config.toml`
- `cat ~/.local/npm-global/lib/node_modules/@openai/codex/package.json`

### Web
- https://pypi.org/pypi/<textual|rich|prompt-toolkit|urwid|py_cui|asciimux>/json (versions/dates, 2026-08-09)
- https://api.github.com/repos/<repo> for star counts: Textualize/textual, Textualize/rich, prompt-toolkit/python-prompt-toolkit, urwid/urwid, vadimdemedes/ink, derailed/k9s, jesseduffield/lazygit, dlvhdr/gh-dash, tconbeer/harlequin, batrachianai/toad, Textualize/textual-web, Textualize/textual-serve, Textualize/toolong, darrenburns/posting
- https://raw.githubusercontent.com/sst/opencode/dev/packages/tui/package.json (opentui deps)
- https://registry.npmjs.org/@opentui%2fcore (opentui description: "TypeScript library on a native Zig core")
- https://raw.githubusercontent.com/jesseduffield/lazygit/master/go.mod (tcell), https://raw.githubusercontent.com/dlvhdr/gh-dash/main/go.mod (Bubble Tea v2), https://raw.githubusercontent.com/derailed/k9s/master/go.mod (tview/tcell)
- https://textual.textualize.io/guide/app/ (inline mode ≥0.55.0, `ansi_color` ≥0.80.0, suspend, asyncio)
- https://textual.textualize.io/ (Textualize claim: run in terminal or browser, over SSH, "single board computer"; Toad/Posting/Toolong/Memray/Dolphie/Harlequin gallery)
- https://github.com/batrachianai/toad (Toad README: agents via Agent Client Protocol)
- https://agentclientprotocol.com/overview/introduction (ACP: JSON-RPC over stdio local / HTTP+WebSocket remote; reuses MCP JSON)
- https://modelcontextprotocol.io (MCP: stdio JSON-RPC 2.0 tool protocol)
- https://no-color.org (NO_COLOR convention)

### UNVERIFIED
- asciimux project status/disappearance (404 on PyPI and GitHub; no archived page reachable).
- Textual screen-reader/accesskit integration status.
- Historical claim that opencode "used Ink/React" in earlier versions (current source shows opentui; the premise as stated is outdated on this machine).
- Claude Code's TUI stack (not inspected; not required).

---

## Appendix: verifiable stats included in this document

1. **textual 8.2.8** on PyPI, released 2026-06-30 (`requires_python >=3.9,<4.0`) — Python 3.14 compatible.
2. **rich 15.0.0** on PyPI, released 2026-04-12.
3. **prompt_toolkit 3.0.53** on PyPI, released 2026-07-26.
4. **urwid 4.0.8** on PyPI, released 2026-07-26.
5. **py_cui 0.1.6** on PyPI, released 2022-09-28 (stale ≈4 years).
6. **lazygit 81,164 ★**, **k9s 34,306 ★**, **gh-dash 12,273 ★**, **ink 39,603 ★**, **textual 36,894 ★**, **rich 57,043 ★**, **Toad 3,361 ★**, **Harlequin 6,313 ★** (GitHub API, 2026-08-09).
7. **codex-cli 0.146.1** (local), Rust binary with `ratatui`/`crossterm` strings.
8. **opencode 0.0.0-dev-202608071959** (local), TUI = opentui (Zig core) + SolidJS; `run --format json`; `--mini` mode.
