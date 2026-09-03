"""``cambium doctor`` — harness diagnostics command (architecture.md §13).

A health check modeled on established coding-harness doctor commands.
It exists to surface early the drift failure mode Codex's local install
exhibits: state rows pointing at missing or unusable files. The Cambium
analogue checked here: worktree entries whose directory is gone, an event
store that fails ``PRAGMA integrity_check``, and module-owned JSONL datasets
that contain invalid records.

Exit status: 0 when no check fails (warnings and skips are allowed), 1 when
any check fails.

Run::

    python -m cambium.doctor [--session-dir <dir>]
    python -m cambium.doctor --cache-report <session-dir>
"""

from __future__ import annotations

import argparse
import enum
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, metadata
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from . import auth
from .auth import AuthError, AuthStore
from .oauth import (
    DEFAULT_ISSUER,
    DEFAULT_REFRESH_MARGIN_S,
    InvalidGrantError,
    OAuthError,
    OAuthMissingError,
    OAuthStore,
    RefreshUnavailableError,
    refresh_access_token,
    resolve_codex_client_id,
)
from .provider_config import (
    DEFAULT_PROVIDER_PATH,
    DEFAULT_SAMPLE,
    AuthMode,
    load_provider_specs,
    load_providers,
    validate_provider_specs,
)
from .routing import DebtStore
from .system_health import format_health, health


def _minimum_python() -> tuple[int, int]:
    """Read the supported Python floor from project or installed metadata.

    Source checkouts use the authoritative ``project.requires-python`` value
    from the repository's ``pyproject.toml``. A wheel does not need to carry
    that file, so installed distributions use their ``Requires-Python``
    metadata instead. The fallback keeps the diagnostic usable from an
    unusual stripped-down source install while matching the package contract.
    """
    project_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    requires_python: object = None
    try:
        with project_path.open("rb") as stream:
            requires_python = tomllib.load(stream)["project"]["requires-python"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        try:
            requires_python = metadata("cambium").get("Requires-Python")
        except PackageNotFoundError:
            requires_python = None

    if isinstance(requires_python, str):
        match = re.search(r"(?:^|,)\s*>=\s*(\d+)\.(\d+)", requires_python)
        if match is not None:
            return int(match.group(1)), int(match.group(2))

    # Keep doctor usable even when invoked from a stripped-down source bundle.
    # This mirrors pyproject.toml's current package declaration.
    return (3, 12)


MIN_PYTHON = _minimum_python()
MIN_GIT = (2, 40)
EVENTS_DB_REL = ".cambium/events.db"
CONVERSATIONS_DB_REL = ".cambium/conversations.db"
MODULES_ROOT = Path(__file__).resolve().parent / "modules"


def _omp_models_yml() -> Path:
    """Return the OMP model path under the effective user's home."""
    return auth.effective_home() / ".omp" / "agent" / "models.yml"


def _sqlite_read_only_uri(db: Path) -> str:
    """Return a read-only SQLite URI with the filesystem path encoded safely."""
    return f"file:{quote(str(Path(db).resolve()), safe='/:')}?mode=ro"


class Status(enum.StrEnum):
    """Outcome of one diagnostic check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    INFO = "info"


class DatasetIntegrityError(Exception):
    """Raised when a module-owned dataset cannot be discovered or validated."""


@dataclass(slots=True)
class CacheProviderStats:
    """Provider cache evidence accumulated from usage events."""

    calls: int = 0
    cache_hits: int = 0
    cache_known: int = 0
    cached_token_total: float = 0.0
    input_token_total: float = 0.0
    token_pairs: int = 0


def _cache_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _usage_input_tokens(usage: Mapping[str, Any]) -> float | None:
    for key in ("input_tokens", "prompt_tokens"):
        value = _cache_number(usage.get(key))
        if value is not None:
            return value
    return None


def _usage_cached_tokens(usage: Mapping[str, Any]) -> float | None:
    for details_key in ("input_tokens_details", "prompt_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, Mapping) and "cached_tokens" in details:
            return _cache_number(details.get("cached_tokens"))
    for key in ("cache_read_input_tokens", "cached_tokens"):
        if key in usage:
            return _cache_number(usage.get(key))
    return None


def _cache_provider_name(payload: Mapping[str, Any]) -> str:
    provider = payload.get("provider")
    return provider if isinstance(provider, str) and provider else "<unknown>"


def record_cache_event(providers: dict[str, CacheProviderStats], event: Mapping[str, Any]) -> None:
    """Fold one usage event into per-provider cache evidence."""
    if event.get("kind") != "usage_event":
        return
    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, Mapping) else {}
    stats = providers.setdefault(_cache_provider_name(payload), CacheProviderStats())
    stats.calls += 1

    cache_hit = payload.get("provider_cache_hit")
    if type(cache_hit) is bool:
        stats.cache_known += 1
        stats.cache_hits += int(cache_hit)

    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return
    input_tokens = _usage_input_tokens(usage)
    cached_tokens = _usage_cached_tokens(usage)
    if input_tokens is None or cached_tokens is None:
        return
    stats.input_token_total += input_tokens
    stats.cached_token_total += cached_tokens
    stats.token_pairs += 1


def cache_provider_status(stats: CacheProviderStats) -> Status:
    """Return WARN when one or more calls lack provider cache evidence."""
    return Status.PASS if stats.cache_known == stats.calls else Status.WARN


def _cache_percentage(numerator: float, denominator: float) -> str:
    if denominator <= 0:
        return "n/a"
    return f"{100.0 * numerator / denominator:.1f}%"


def _cache_token_number(value: float) -> str:
    return f"{value:g}"


def format_cache_provider_detail(stats: CacheProviderStats) -> str:
    """Format the operator-facing metrics for one provider."""
    unknown = stats.calls - stats.cache_known
    hit_detail = _cache_percentage(stats.cache_hits, stats.cache_known)
    hit_detail += f" ({stats.cache_hits}/{stats.cache_known} known)"
    if stats.token_pairs and stats.input_token_total > 0:
        share = _cache_percentage(stats.cached_token_total, stats.input_token_total)
        token_detail = (
            f"{share} ({_cache_token_number(stats.cached_token_total)} cached/"
            f"{_cache_token_number(stats.input_token_total)} input)"
        )
    else:
        token_detail = "n/a"
    return (
        f"total calls: {stats.calls}; cache-hit calls: {stats.cache_hits}; "
        f"unknown cache fields: {unknown}; hit %: {hit_detail}; "
        f"cached-token share: {token_detail}"
    )


@dataclass(frozen=True, slots=True)
class Check:
    """One numbered diagnostic check with its outcome and detail."""

    number: int
    name: str
    status: Status
    detail: str


def check_python() -> tuple[Status, str]:
    version = sys.version_info[:2]
    required = ".".join(map(str, MIN_PYTHON))
    status = Status.PASS if version >= MIN_PYTHON else Status.FAIL
    return status, f"{sys.version.split()[0]} (>= {required})"


def check_uv() -> tuple[Status, str]:
    path = shutil.which("uv")
    if path:
        return Status.PASS, path
    return Status.FAIL, "uv not found on PATH"


def _git_version() -> tuple[int, int] | None:
    try:
        output = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, check=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"git version (\d+)\.(\d+)", output)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def check_git() -> tuple[Status, str]:
    if shutil.which("git") is None:
        return Status.FAIL, "git not found on PATH"
    version = _git_version()
    if version is None:
        return Status.FAIL, "could not parse `git --version`"
    found = f"{version[0]}.{version[1]}"
    required = f"{MIN_GIT[0]}.{MIN_GIT[1]}"
    status = Status.PASS if version >= MIN_GIT else Status.FAIL
    return status, f"{found} (>= {required})"


def _git_toplevel(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(result.stdout.strip())


def _parse_worktrees(porcelain: str) -> list[Path]:
    paths: list[Path] = []
    for entry in porcelain.split("\n\n"):
        for line in entry.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree ") :]))
    return paths


def check_worktrees(cwd: Path) -> tuple[Status, str]:
    """Flag worktree entries whose directory is missing — the codex-doctor drift class."""
    if _git_toplevel(cwd) is None:
        return Status.SKIP, "not inside a git repository"
    try:
        output = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return Status.FAIL, f"`git worktree list --porcelain` failed: {exc}"
    worktrees = _parse_worktrees(output)
    if not worktrees:
        return Status.PASS, "no linked worktrees"
    missing = [path for path in worktrees if not path.is_dir()]
    if missing:
        shown = ", ".join(str(path) for path in missing[:3])
        return Status.FAIL, (
            f"{len(missing)}/{len(worktrees)} worktree(s) have a missing directory: {shown}"
        )
    return Status.PASS, f"{len(worktrees)} worktree(s), all directories present"


def _event_store(db: Path) -> tuple[int | None, list[str]]:
    """Return (row count, integrity problems). Count is None when integrity failed."""
    conn = sqlite3.connect(_sqlite_read_only_uri(db), uri=True)
    try:
        integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        problems = [line for line in integrity if line != "ok"]
        if problems:
            return None, problems
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return count, []
    finally:
        conn.close()


def check_event_store(session_dir: Path | None) -> tuple[Status, str]:
    if session_dir is None:
        return Status.SKIP, "no --session-dir given"
    db = session_dir / EVENTS_DB_REL
    if not db.is_file():
        return Status.SKIP, f"{db} does not exist"
    try:
        count, problems = _event_store(db)
    except sqlite3.Error as exc:
        return Status.FAIL, f"{db}: {exc}"
    if problems:
        return Status.FAIL, f"{db}: integrity_check: {problems[:3]}"
    return Status.PASS, f"{db}: integrity ok, {count} events"


def _cache_report_event_dbs(session_dir: Path) -> list[Path]:
    """Return session event stores without following event-store symlinks."""
    databases: list[Path] = []
    root_db = session_dir / EVENTS_DB_REL
    if root_db.is_file() and not root_db.is_symlink():
        databases.append(root_db)
    databases.extend(
        path
        for path in sorted(session_dir.glob("turn-*/.cambium/events.db"))
        if path.is_file() and not path.is_symlink()
    )
    return databases


def _read_cache_report_db(db: Path, providers: dict[str, CacheProviderStats]) -> int:
    """Fold usage rows from one event store, using a read-only connection."""
    calls = 0
    connection = sqlite3.connect(_sqlite_read_only_uri(db), uri=True)
    try:
        rows = connection.execute(
            "SELECT payload FROM events WHERE kind = ? ORDER BY seq", ("usage_event",)
        )
        for row in rows:
            try:
                payload = json.loads(row[0])
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"{db}: invalid usage_event payload") from exc
            record_cache_event(
                providers,
                {"kind": "usage_event", "payload": payload if isinstance(payload, Mapping) else {}},
            )
            calls += 1
    finally:
        connection.close()
    return calls


def _scan_cache_report(
    session_dir: Path,
) -> tuple[Status, str, dict[str, CacheProviderStats]]:
    """Read cache evidence from a session without modifying any artifact."""
    databases = _cache_report_event_dbs(session_dir)
    if not databases:
        return (
            Status.SKIP,
            f"{session_dir}: no event database found (expected .cambium/events.db "
            "or turn-*/.cambium/events.db)",
            {},
        )

    providers: dict[str, CacheProviderStats] = {}
    calls = 0
    for db in databases:
        try:
            calls += _read_cache_report_db(db, providers)
        except (OSError, sqlite3.Error, ValueError) as exc:
            return Status.FAIL, f"{db}: cache report unavailable: {exc}", providers

    if calls == 0:
        return (
            Status.SKIP,
            f"{session_dir}: {len(databases)} event database(s), no usage_event rows",
            providers,
        )
    status = (
        Status.WARN
        if any(cache_provider_status(stats) is Status.WARN for stats in providers.values())
        else Status.PASS
    )
    return (
        status,
        f"{session_dir}: {len(databases)} event database(s), {calls} usage_event call(s)",
        providers,
    )


def check_cache_report(session_dir: Path) -> tuple[Status, str]:
    """Return the overall status for a provider cache report."""
    status, detail, _providers = _scan_cache_report(Path(session_dir))
    return status, detail


def _provider_config_path(cwd: Path) -> tuple[Path, bool]:
    configured = os.environ.get("CAMBIUM_PROVIDERS")
    if configured:
        path = Path(configured)
        return (path if path.is_absolute() else cwd / path), True
    return DEFAULT_PROVIDER_PATH, False


@dataclass(frozen=True, slots=True)
class _DoctorProvider:
    """Provider fields used by credential diagnostics."""

    name: str
    required: bool
    api_key_env: str
    api_key: str | None
    auth: AuthMode | None
    model: str


def _oauth_session_present(store: OAuthStore, name: str) -> bool:
    """Return whether the OAuth store holds a usable session for ``name``."""
    record = store.read_provider(name)
    if record is None or record.disabled:
        return False
    return record.doc.expires_at - time.time() > DEFAULT_REFRESH_MARGIN_S or bool(
        record.doc.refresh_token
    )


def _doctor_providers(
    specs: Sequence[Any], providers: Sequence[Any]
) -> tuple[_DoctorProvider, ...]:
    return tuple(
        _DoctorProvider(
            spec.name,
            spec.required,
            spec.api_key_env,
            getattr(provider, "api_key", spec.api_key),
            getattr(provider, "auth", None),
            str(getattr(provider, "model", "") or ""),
        )
        for spec, provider in zip(specs, providers, strict=True)
    )


def _doctor_credential_label(provider: _DoctorProvider) -> str:
    if provider.auth is AuthMode.CODEX_CHATGPT:
        return "oauth"
    if provider.auth is AuthMode.NONE:
        return "none"
    return provider.api_key_env


def check_provider_env(cwd: Path) -> tuple[Status, str]:
    """Check provider credential presence without printing names or values.

    API-key providers are present when their file-backed ``api_key`` is set;
    ``none`` providers need no credential; OAuth (``codex_chatgpt``) providers
    are present when the OAuth store holds a usable session. A missing
    credential is WARN by default; a missing required provider is FAIL.
    Invalid provider configuration always FAILs.
    """

    path, explicit = _provider_config_path(cwd)
    configured: Sequence[Any]
    if path.exists():
        if not path.is_file():
            return Status.FAIL, f"{path}: provider config path is not a file"
        try:
            specs = load_provider_specs(path)
            configured = load_providers(path)
        except (OSError, ValueError) as exc:
            return Status.FAIL, f"{path}: provider config validation failed: {exc}"
        source = str(path)
    elif explicit:
        return Status.FAIL, f"{path}: configured provider file does not exist"
    else:
        try:
            specs = validate_provider_specs(DEFAULT_SAMPLE)
            configured = [
                SimpleNamespace(auth=AuthMode(item.get("auth", "api_key")))
                for item in DEFAULT_SAMPLE["providers"]
            ]
        except ValueError as exc:  # The shipped sample is still a config input.
            return Status.FAIL, f"default provider sample validation failed: {exc}"
        source = "default sample"

    providers = _doctor_providers(specs, configured)
    disable_reasons: dict[str, str] = {}
    try:
        debt_store = DebtStore()
        debt_store.load()
        disable_reasons = {
            name: debt.disable_reason
            for name, debt in debt_store.as_mapping().items()
            if debt.disable_reason is not None
        }
    except (OSError, ValueError):
        # Advisory only: a missing/corrupt/unreadable routing ledger never
        # fails the provider-env check; it just loses the annotation.
        disable_reasons = {}
    oauth_store = OAuthStore()
    try:
        presence = {
            p.name: (
                _oauth_session_present(oauth_store, p.name)
                if p.auth is AuthMode.CODEX_CHATGPT
                else True
                if p.auth is AuthMode.NONE
                else bool(p.api_key)
            )
            for p in providers
        }
    except OAuthError as exc:
        return Status.FAIL, f"{source}: OAuth store unavailable: {exc}"
    states = ", ".join(
        f"{p.name}(model={p.model})={'set' if presence[p.name] else 'missing'}"
        + (f" (disabled: {disable_reasons[p.name]})" if p.name in disable_reasons else "")
        for p in providers
    )
    missing_required = [p.name for p in providers if p.required and not presence[p.name]]
    missing_optional = [p.name for p in providers if not p.required and not presence[p.name]]
    if missing_required:
        return Status.FAIL, (
            f"{source}: {states}; required provider credential missing for "
            f"{', '.join(missing_required)}"
        )
    if missing_optional:
        return Status.WARN, (
            f"{source}: {states}; missing provider credential is WARN unless "
            f"required=true ({', '.join(missing_optional)})"
        )
    return Status.PASS, f"{source}: {states or 'no providers'}"


def _auth_path(path: Path | None) -> Path:
    return auth.auth_store_path() if path is None else Path(path)


def check_auth_metadata(path: Path | None = None) -> tuple[Status, str]:
    """Check fixed auth directory/file ownership, modes, type, and link count."""
    target = _auth_path(path)
    metadata = auth.inspect_metadata(target)
    if not metadata.directory_exists:
        return Status.WARN, "auth store is not configured"
    if metadata.issue is not None:
        return Status.FAIL, f"auth store metadata invalid: {metadata.issue}"
    if not metadata.file_exists:
        return Status.WARN, "auth store file is not present"
    if not metadata.directory_secure or not metadata.file_secure:
        return Status.FAIL, "auth store metadata is not secure"
    return Status.PASS, "auth store directory and file metadata are secure"


def check_auth_schema(path: Path | None = None) -> tuple[Status, str]:
    """Check the exact auth schema without printing a key or a key name."""
    target = _auth_path(path)
    metadata = auth.inspect_metadata(target)
    if not metadata.directory_exists:
        return Status.WARN, "auth store schema is unavailable because the directory is absent"
    if metadata.issue is not None or not metadata.directory_secure:
        return Status.FAIL, "auth store schema cannot be checked on insecure metadata"
    if not metadata.file_exists:
        return Status.WARN, "auth store schema is unavailable because the file is absent"
    if not metadata.file_secure:
        return Status.FAIL, "auth store schema cannot be checked on insecure metadata"
    try:
        document = AuthStore(target).read()
    except AuthError as exc:
        return Status.FAIL, f"auth store schema invalid: {exc}"
    return Status.PASS, f"auth store schema valid ({len(document.providers)} provider entries)"


def _doctor_provider_specs(cwd: Path) -> tuple[str, tuple[_DoctorProvider, ...]]:
    path, explicit = _provider_config_path(cwd)
    configured: Sequence[Any]
    if path.exists():
        if not path.is_file():
            raise ValueError("provider config path is not a file")
        specs = load_provider_specs(path)
        configured = load_providers(path)
        source = str(path)
    elif explicit:
        raise ValueError("configured provider file does not exist")
    else:
        specs = validate_provider_specs(DEFAULT_SAMPLE)
        configured = [
            SimpleNamespace(auth=AuthMode(item.get("auth", "api_key")))
            for item in DEFAULT_SAMPLE["providers"]
        ]
        source = "default sample"
    return source, _doctor_providers(specs, configured)


def check_auth_coverage(cwd: Path, path: Path | None = None) -> tuple[Status, str]:
    """Compare configured providers with stored credentials; never use the network.

    API-key providers are covered when the auth store holds their entry; OAuth
    (``codex_chatgpt``) providers are covered when the OAuth store holds a
    usable session (their credential is never an auth-store entry).
    """
    target = _auth_path(path)
    try:
        source, providers = _doctor_provider_specs(cwd)
    except (OSError, ValueError) as exc:
        return Status.SKIP, f"auth coverage skipped: provider config validation failed: {exc}"

    oauth_store = OAuthStore()
    try:
        auth_names = set(AuthStore(target).read().provider_names())
    except AuthError:
        auth_names = set()  # Insecure or unreadable store metadata is check 9-11's finding.

    try:
        coverage = {
            p.name: (
                _oauth_session_present(oauth_store, p.name)
                if p.auth is AuthMode.CODEX_CHATGPT
                else True
                if p.auth is AuthMode.NONE
                else p.name in auth_names
            )
            for p in providers
        }
    except OAuthError as exc:
        return Status.FAIL, f"{source}: OAuth store unavailable: {exc}"

    def covered(p: _DoctorProvider) -> bool:
        return coverage[p.name]

    missing_required = [p.name for p in providers if p.required and not covered(p)]
    missing_optional = [p.name for p in providers if not p.required and not covered(p)]
    present = [f"{p.name}={_doctor_credential_label(p)}" for p in providers if covered(p)]
    configured_names = {p.name for p in providers}
    extra = sorted(name for name in auth_names if name not in configured_names)

    states = ", ".join(f"{p.name}={'covered' if covered(p) else 'missing'}" for p in providers)
    if missing_required:
        detail = (
            f"{source}: {states}; required provider credential missing for "
            f"{', '.join(missing_required)}"
        )
        if present:
            detail += f"; covered: {', '.join(present)}"
        return Status.FAIL, detail
    if missing_optional:
        detail = (
            f"{source}: {states}; missing provider credential is WARN unless "
            f"required=true ({', '.join(missing_optional)})"
        )
        if present:
            detail += f"; covered: {', '.join(present)}"
        return Status.WARN, detail
    if extra:
        return Status.WARN, f"{source}: unconfigured auth entries: {', '.join(extra)}"
    if present:
        return Status.PASS, f"{source}: auth coverage complete; covered: {', '.join(present)}"
    return Status.PASS, f"{source}: auth coverage complete"


def check_provider_runnable(cwd: Path, path: Path | None = None) -> tuple[Status, str]:
    """Distinguish configured providers from providers the one-shot CLI can run.

    A provider is runnable when the one-shot CLI can resolve its credential
    without reading a value: an API-key provider is runnable when its
    file-backed ``api_key`` is set or the auth store holds the provider name;
    ``none`` providers are always runnable; an OAuth (``codex_chatgpt``)
    provider is runnable when the OAuth store holds a usable session. A
    configured provider that is not runnable is WARN; the ``required`` flag
    decides FAIL in the provider-env check. The report uses provider metadata
    and store names only and never exposes a key value.
    """
    target = _auth_path(path)
    try:
        source, providers = _doctor_provider_specs(cwd)
    except (OSError, ValueError) as exc:
        return Status.SKIP, (f"provider runnable skipped: provider config validation failed: {exc}")
    if not providers:
        return Status.PASS, f"{source}: no configured providers"

    oauth_store = OAuthStore()
    try:
        auth_names = set(AuthStore(target).read().provider_names())
    except AuthError:
        auth_names = set()  # Insecure or unreadable store metadata is check 9-11's finding.

    try:
        readiness = {
            p.name: (
                _oauth_session_present(oauth_store, p.name)
                if p.auth is AuthMode.CODEX_CHATGPT
                else True
                if p.auth is AuthMode.NONE
                else bool(p.api_key) or p.name in auth_names
            )
            for p in providers
        }
    except OAuthError as exc:
        return Status.FAIL, f"{source}: OAuth store unavailable: {exc}"

    def runnable_for(p: _DoctorProvider) -> bool:
        return readiness[p.name]

    runnable = [p.name for p in providers if runnable_for(p)]
    not_runnable = [p.name for p in providers if not runnable_for(p)]
    if not_runnable:
        detail = f"{source}: configured but not runnable: {', '.join(not_runnable)}"
        if runnable:
            detail += f"; runnable: {', '.join(runnable)}"
        return Status.WARN, detail
    return Status.PASS, f"{source}: all configured providers runnable: {', '.join(runnable)}"


def _sqlite_integrity(db: Path) -> list[str]:
    conn = sqlite3.connect(_sqlite_read_only_uri(db), uri=True)
    try:
        return [row[0] for row in conn.execute("PRAGMA integrity_check") if row[0] != "ok"]
    finally:
        conn.close()


_CONVERSATION_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "node_id",
        "parent_id",
        "turn",
        "role",
        "content",
        "ts",
        "seq",
        "tokens",
        "kind",
        "meta",
    }
)


def _conversation_schema_problems(db: Path) -> list[str]:
    conn = sqlite3.connect(_sqlite_read_only_uri(db), uri=True)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'conversations'"
        ).fetchone()
        if table is None:
            return ["missing conversations table"]
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    finally:
        conn.close()
    missing = sorted(_CONVERSATION_REQUIRED_COLUMNS - columns)
    if missing:
        return [f"missing conversations column(s): {', '.join(missing)}"]
    return []


def check_conversation_store(session_dir: Path | None) -> tuple[Status, str]:
    if session_dir is None:
        return Status.SKIP, "no --session-dir given"
    db = session_dir / CONVERSATIONS_DB_REL
    if not db.is_file():
        return Status.SKIP, f"{db} does not exist"
    try:
        problems = _sqlite_integrity(db)
        problems.extend(_conversation_schema_problems(db))
    except sqlite3.Error as exc:
        return Status.FAIL, f"{db}: {exc}"
    if problems:
        return Status.FAIL, f"{db}: integrity/schema check: {problems[:3]}"
    return Status.PASS, f"{db}: integrity ok"


def check_system_health(path: Path) -> tuple[Status, str]:
    """Return advisory host health; resource-probe failures never fail doctor."""

    try:
        return Status.INFO, format_health(health(path))
    except Exception as exc:  # Advisory output must not change doctor exit status.
        return Status.SKIP, f"system health unavailable: {exc}"


def _directory_entries(directory: Path) -> list[Path] | None:
    """List a directory, returning ``None`` only when it does not exist."""
    try:
        with os.scandir(directory) as entries:
            return [Path(entry.path) for entry in entries]
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DatasetIntegrityError(f"{directory}: cannot read directory: {exc}") from exc


def _is_directory(path: Path) -> bool:
    try:
        return S_ISDIR(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DatasetIntegrityError(f"{path}: cannot inspect directory: {exc}") from exc


def _is_regular_file(path: Path) -> bool:
    try:
        return S_ISREG(path.stat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise DatasetIntegrityError(f"{path}: cannot inspect dataset file: {exc}") from exc


def _dataset_jsonl_files() -> list[Path]:
    root_entries = _directory_entries(MODULES_ROOT)
    if root_entries is None:
        return []

    files: list[Path] = []
    for module_dir in sorted(path for path in root_entries if _is_directory(path)):
        dataset_dir = module_dir / "datasets"
        dataset_entries = _directory_entries(dataset_dir)
        if dataset_entries is None:
            continue
        files.extend(
            path for path in dataset_entries if path.suffix == ".jsonl" and _is_regular_file(path)
        )
    return sorted(files)


def _jsonl_record_count(path: Path) -> int:
    records = 0
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetIntegrityError(f"{path}:{line_number}: invalid JSON") from exc
            if not isinstance(value, dict):
                raise DatasetIntegrityError(f"{path}:{line_number}: record is not a JSON object")
            records += 1
    return records


def check_dataset() -> tuple[Status, str]:
    """Check module-owned JSONL records without importing a decision module."""
    try:
        files = _dataset_jsonl_files()
    except DatasetIntegrityError as exc:
        return Status.FAIL, f"dataset integrity check failed: {exc}"
    except OSError as exc:
        return Status.FAIL, f"could not discover module datasets: {exc}"
    if not files:
        return Status.SKIP, f"{MODULES_ROOT} has no module-owned JSONL datasets"

    try:
        records = sum(_jsonl_record_count(path) for path in files)
    except DatasetIntegrityError as exc:
        return Status.FAIL, f"dataset integrity check failed: {exc}"
    except (OSError, UnicodeError, ValueError) as exc:
        return Status.FAIL, f"dataset integrity check failed: {exc}"
    dataset_word = "dataset" if len(files) == 1 else "datasets"
    record_word = "record" if records == 1 else "records"
    return Status.PASS, f"{len(files)} module-owned JSONL {dataset_word}, {records} {record_word}"


def _git_tracked(repo: Path, relative: str) -> bool:
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo,
            capture_output=True,
            timeout=10,
        )
        if inside.returncode != 0:
            return False
        listed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative],
            cwd=repo,
            capture_output=True,
            timeout=10,
        )
    except OSError:
        return False
    return listed.returncode == 0


def check_secrets() -> tuple[Status, str]:
    """WARN (never FAIL) when ~/.omp/agent/models.yml is git-tracked."""
    models = _omp_models_yml()
    if not models.is_file():
        return Status.PASS, f"{models} not present"
    if _git_tracked(models.parent, models.name):
        return Status.WARN, (
            f"{models} is git-tracked — plaintext API keys (credential safety invariant)"
        )
    return Status.PASS, f"{models} present but not git-tracked"


def _issuer_reachable(issuer: str, timeout_s: float) -> tuple[bool, str]:
    """Any HTTP response proves endpoint reachability; failures are detailed."""
    try:
        with urllib.request.urlopen(issuer, timeout=timeout_s) as response:
            response.read(256)
            return True, f"issuer HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return True, f"issuer HTTP {exc.code}"
    except Exception as exc:  # TimeoutError, URLError, OSError
        return False, f"issuer unreachable: {exc}"


def check_oauth_live(
    cwd: Path,
    *,
    provider_config: Path | None = None,
    oauth_store: OAuthStore | None = None,
    client_id: str | None = None,
    issuer: str | None = None,
    timeout_s: float = 10.0,
) -> tuple[Status, str]:
    """OPT-IN live oauth probe for codex_chatgpt providers.

    Runs only with ``cambium doctor --oauth-live``. It probes issuer
    reachability and performs one real refresh-token exchange per configured
    codex provider (consuming quota); it never makes a model call. ``issuer``,
    ``oauth_store``, ``provider_config``, and ``client_id`` are injectable so
    tests can probe a loopback fake issuer without the network or a store at
    the real path.
    """
    if provider_config is None:
        provider_config = _provider_config_path(cwd)[0]
    try:
        providers = load_providers(provider_config)
    except (OSError, ValueError) as exc:
        return Status.SKIP, f"oauth live check skipped: provider config failed: {exc}"
    codex = [provider for provider in providers if provider.auth is AuthMode.CODEX_CHATGPT]
    if not codex:
        return Status.PASS, "no codex_chatgpt providers configured; nothing to probe live"

    store = OAuthStore() if oauth_store is None else oauth_store
    effective_issuer = DEFAULT_ISSUER if issuer is None else issuer
    effective_client_id = resolve_codex_client_id(
        client_id if client_id is not None else os.environ.get("CAMBIUM_CODEX_CLIENT_ID")
    )
    reachable, reachability = _issuer_reachable(effective_issuer, timeout_s)

    details: list[str] = []
    failed = not reachable
    for provider in codex:
        name = provider.name
        try:
            doc = store.validate(name)
        except OAuthMissingError:
            details.append(f"{name}=no-session")
            continue
        except OAuthError:
            details.append(f"{name}=invalid-store")
            failed = True
            continue
        try:
            refresh_access_token(
                effective_issuer, effective_client_id, doc.refresh_token, timeout_s
            )
            details.append(f"{name}=refreshable")
        except InvalidGrantError:
            details.append(f"{name}=refresh-rejected")
            failed = True
        except RefreshUnavailableError:
            details.append(f"{name}=refresh-unavailable")
        except OAuthError:
            details.append(f"{name}=refresh-failed")
            failed = True

    detail = f"{reachability}; " + "; ".join(details) if details else reachability
    if failed:
        return Status.FAIL, detail
    if any(
        "no-session" in item or "refresh-skipped" in item or "refresh-unavailable" in item
        for item in details
    ):
        return Status.WARN, detail
    if not reachable:
        return Status.FAIL, detail
    return Status.PASS, detail


def run_checks(session_dir: Path | None, cwd: Path, *, oauth_live: bool = False) -> list[Check]:
    checks = [
        (1, "Python version", check_python()),
        (2, "uv", check_uv()),
        (3, "git", check_git()),
        (4, "Worktree hygiene", check_worktrees(cwd)),
        (5, "Event store integrity", check_event_store(session_dir)),
        (6, "Dataset integrity", check_dataset()),
        (7, "Secrets hygiene", check_secrets()),
        (8, "Provider env", check_provider_env(cwd)),
        (9, "Auth metadata", check_auth_metadata()),
        (10, "Auth schema", check_auth_schema()),
        (11, "Auth coverage", check_auth_coverage(cwd)),
        (12, "Provider runnable", check_provider_runnable(cwd)),
        (13, "Conversation store", check_conversation_store(session_dir)),
        (14, "System health", check_system_health(cwd)),
    ]
    if oauth_live:
        checks.append((15, "OAuth live", check_oauth_live(cwd)))
    return [
        Check(number=number, name=name, status=status, detail=detail)
        for number, name, (status, detail) in checks
    ]


def format_report(
    checks: list[Check], *, title: str = "cambium doctor — Cambium harness diagnostics"
) -> str:
    lines = [title]
    ordered_checks = sorted(checks, key=lambda check: check.number)
    for check in ordered_checks:
        lines.append(
            f"  {check.number:>2}. {check.name:<22} "
            f"{check.status.value.upper():<5} {_redact_report_detail(check.detail)}"
        )
    counts = Counter(check.status for check in ordered_checks)
    lines.append(
        f"Summary: {counts[Status.PASS]} pass · {counts[Status.WARN]} warn · "
        f"{counts[Status.SKIP]} skip · {counts[Status.INFO]} info · "
        f"{counts[Status.FAIL]} fail"
    )
    return "\n".join(lines)


def format_cache_report(
    session_dir: Path,
    status: Status,
    detail: str,
    providers: Mapping[str, CacheProviderStats],
) -> str:
    """Format a cache report using the standard doctor status lines."""
    checks = [Check(1, "Cache report", status, detail)]
    checks.extend(
        Check(
            number=index,
            name=f"Provider cache ({provider})",
            status=cache_provider_status(stats),
            detail=format_cache_provider_detail(stats),
        )
        for index, (provider, stats) in enumerate(sorted(providers.items()), start=2)
    )
    del session_dir
    return format_report(checks, title="cambium doctor — provider cache report")


def _redact_report_detail(detail: str) -> str:
    environment = dict(os.environ)
    safe_environment = auth.scrub_environment(environment)
    secret_values = sorted(
        {value for name, value in environment.items() if name not in safe_environment and value},
        key=lambda value: (-len(value), value),
    )
    for value in secret_values:
        detail = detail.replace(value, "***")
    return detail


def exit_code(checks: list[Check]) -> int:
    return 1 if any(check.status == Status.FAIL for check in checks) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cambium doctor",
        description="Harness diagnostics: python/uv/git availability, worktree "
        "hygiene, provider environment, session artifacts, dataset integrity, "
        "secrets hygiene, and advisory system health.",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="session dir whose Cambium artifacts are checked (optional)",
    )
    parser.add_argument(
        "--cache-report",
        type=Path,
        metavar="DIR",
        help="report provider cache hits from usage events in a session (read-only)",
    )
    parser.add_argument(
        "--oauth-live",
        action="store_true",
        help="opt-in live oauth probe for codex_chatgpt providers (consumes "
        "quota; never makes a model call)",
    )
    args = parser.parse_args(argv)
    if args.cache_report is not None:
        status, detail, providers = _scan_cache_report(args.cache_report)
        print(format_cache_report(args.cache_report, status, detail, providers))
        return 1 if status is Status.FAIL else 0
    checks = run_checks(args.session_dir, Path.cwd(), oauth_live=args.oauth_live)
    print(format_report(checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
