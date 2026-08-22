"""Optional one-shot Language Server Protocol query boundary.

The operator, not the model, chooses the language-server command through
``CAMBIUM_LSP_COMMAND`` as a JSON string array. Each query gets a fresh process
and bounded JSON-RPC session, avoiding stale-document races and global daemon
state. Portable structured code navigation remains the fast fallback.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "GOPATH",
        "GOROOT",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "JAVA_HOME",
        "NODE_PATH",
    }
)
_METHODS = {
    "definition": "textDocument/definition",
    "references": "textDocument/references",
    "hover": "textDocument/hover",
    "document_symbols": "textDocument/documentSymbol",
    "diagnostics": "textDocument/diagnostic",
}


class LspQueryError(RuntimeError):
    pass


def _configured_command() -> list[str]:
    raw = os.environ.get("CAMBIUM_LSP_COMMAND")
    if not raw:
        raise LspQueryError("CAMBIUM_LSP_COMMAND is not configured")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LspQueryError("CAMBIUM_LSP_COMMAND must be a JSON string array") from exc
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise LspQueryError("CAMBIUM_LSP_COMMAND must be a non-empty JSON string array")
    return value


def _safe_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _ENV_ALLOWLIST and isinstance(value, str)
    }


def _write_message(stream, payload: dict[str, Any]) -> None:
    body = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(body) > _MAX_MESSAGE_BYTES:
        raise LspQueryError("LSP request exceeds the message cap")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stream.write(body)
    stream.flush()


class _Reader:
    def __init__(self, file_descriptor: int) -> None:
        self.fd = file_descriptor
        self.buffer = bytearray()
        self.selector = selectors.DefaultSelector()
        self.selector.register(file_descriptor, selectors.EVENT_READ)

    def close(self) -> None:
        self.selector.close()

    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        if not self.selector.select(remaining):
            raise TimeoutError
        chunk = os.read(self.fd, 65536)
        if not chunk:
            raise LspQueryError("language server closed stdout")
        self.buffer.extend(chunk)
        if len(self.buffer) > _MAX_MESSAGE_BYTES * 2:
            raise LspQueryError("language server response buffer exceeds the cap")

    def message(self, deadline: float) -> dict[str, Any]:
        while b"\r\n\r\n" not in self.buffer:
            self._fill(deadline)
        header_end = self.buffer.index(b"\r\n\r\n")
        raw_headers = bytes(self.buffer[:header_end]).decode("ascii", "strict")
        del self.buffer[: header_end + 4]
        length = None
        for line in raw_headers.split("\r\n"):
            name, separator, value = line.partition(":")
            if separator and name.lower() == "content-length":
                try:
                    length = int(value.strip())
                except ValueError as exc:
                    raise LspQueryError("invalid LSP Content-Length") from exc
        if length is None or length < 0 or length > _MAX_MESSAGE_BYTES:
            raise LspQueryError("missing or invalid LSP Content-Length")
        while len(self.buffer) < length:
            self._fill(deadline)
        body = bytes(self.buffer[:length])
        del self.buffer[:length]
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LspQueryError("language server returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise LspQueryError("language server returned a non-object message")
        return value


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        process.terminate()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()
        process.wait(timeout=2.0)


def _language_id(path: Path) -> str:
    return {
        ".py": "python",
        ".pyi": "python",
        ".rs": "rust",
        ".go": "go",
        ".js": "javascript",
        ".jsx": "javascriptreact",
        ".ts": "typescript",
        ".tsx": "typescriptreact",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".java": "java",
        ".kt": "kotlin",
    }.get(path.suffix.lower(), "plaintext")


def query_lsp(
    root: str | Path,
    *,
    method: str,
    path: str,
    line: int = 1,
    column: int = 1,
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    """Run one bounded LSP query against an operator-configured server."""

    protocol_method = _METHODS.get(method)
    if protocol_method is None:
        raise ValueError(f"unsupported LSP query method {method!r}")
    if line <= 0 or column <= 0:
        raise ValueError("LSP line and column are one-based positive integers")
    if not 0 < timeout_s <= 60:
        raise ValueError("LSP timeout_s must be in (0, 60]")
    worktree = Path(root).resolve()
    target = (worktree / path).resolve()
    if not target.is_relative_to(worktree):
        raise ValueError("LSP path escapes the worktree")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("LSP target is not readable UTF-8") from exc
    command = _configured_command()
    process = subprocess.Popen(
        command,
        cwd=worktree,
        env=_safe_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdin is None or process.stdout is None:
        _terminate(process)
        raise LspQueryError("language server stdio pipes are unavailable")
    reader = _Reader(process.stdout.fileno())
    deadline = time.monotonic() + timeout_s
    diagnostics: list[Any] = []
    try:
        root_uri = worktree.as_uri()
        _write_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "processId": None,
                    "rootUri": root_uri,
                    "workspaceFolders": [{"uri": root_uri, "name": worktree.name}],
                    "capabilities": {},
                },
            },
        )
        while True:
            message = reader.message(deadline)
            if message.get("id") == 1:
                if "error" in message:
                    raise LspQueryError(f"language server initialize failed: {message['error']!r}")
                break
        _write_message(
            process.stdin,
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
        )
        uri = target.as_uri()
        _write_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": uri,
                        "languageId": _language_id(target),
                        "version": 1,
                        "text": text,
                    }
                },
            },
        )
        params: dict[str, Any] = {"textDocument": {"uri": uri}}
        if method not in {"document_symbols", "diagnostics"}:
            params["position"] = {"line": line - 1, "character": column - 1}
        if method == "references":
            params["context"] = {"includeDeclaration": True}
        _write_message(
            process.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": protocol_method,
                "params": params,
            },
        )
        result: Any = None
        while True:
            message = reader.message(deadline)
            if message.get("method") == "textDocument/publishDiagnostics":
                params_value = message.get("params")
                if isinstance(params_value, dict) and params_value.get("uri") == uri:
                    value = params_value.get("diagnostics")
                    if isinstance(value, list):
                        diagnostics = value
                if method == "diagnostics" and diagnostics:
                    result = {"items": diagnostics}
                    break
                continue
            if message.get("id") != 2:
                continue
            if "error" in message:
                if method == "diagnostics" and diagnostics:
                    result = {"items": diagnostics}
                    break
                raise LspQueryError(f"language server query failed: {message['error']!r}")
            result = message.get("result")
            break
        return {
            "method": method,
            "path": str(target.relative_to(worktree)),
            "result": result,
            "published_diagnostics": diagnostics,
        }
    except TimeoutError as exc:
        raise LspQueryError("language server query timed out") from exc
    finally:
        try:
            _write_message(
                process.stdin,
                {"jsonrpc": "2.0", "method": "exit", "params": {}},
            )
        except (BrokenPipeError, OSError, ValueError, LspQueryError):
            pass
        reader.close()
        _terminate(process)


__all__ = ["LspQueryError", "query_lsp"]
