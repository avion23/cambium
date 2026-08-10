"""``cambium doctor`` — harness diagnostics command (architecture.md §13).

A health check modeled on ``codex doctor`` (see ``docs/research/codex.md``).
It exists to surface early the drift failure mode Codex's local install
exhibits: state rows pointing at missing or unusable files. The Cambium
analogue checked here: worktree entries whose directory is gone, an event
store that fails ``PRAGMA integrity_check``, and module-owned JSONL datasets
that contain invalid records.

Exit status: 0 when no check fails (warnings and skips are allowed), 1 when
any check fails.

Run::

    python -m cambium.doctor [--session-dir <dir>]
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISREG

from . import auth
from .auth import AuthError, AuthStore
from .provider_config import (
    DEFAULT_SAMPLE,
    env_report,
    load_provider_specs,
    validate_provider_specs,
)
from .system_health import format_health, health

MIN_PYTHON = (3, 14)
MIN_GIT = (2, 40)
EVENTS_DB_REL = ".cambium/events.db"
CONVERSATIONS_DB_REL = ".cambium/sessions/conversations.db"
MODULES_ROOT = Path(__file__).resolve().parent / "modules"
OMP_MODELS_YML = Path.home() / ".omp" / "agent" / "models.yml"


class Status(enum.StrEnum):
    """Outcome of one diagnostic check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    INFO = "info"


class DatasetIntegrityError(Exception):
    """Raised when a module-owned dataset cannot be discovered or validated."""


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
            cwd=cwd, capture_output=True, text=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(result.stdout.strip())


def _parse_worktrees(porcelain: str) -> list[Path]:
    paths: list[Path] = []
    for entry in porcelain.split("\n\n"):
        for line in entry.splitlines():
            if line.startswith("worktree "):
                paths.append(Path(line[len("worktree "):]))
    return paths


def check_worktrees(cwd: Path) -> tuple[Status, str]:
    """Flag worktree entries whose directory is missing — the codex-doctor drift class."""
    if _git_toplevel(cwd) is None:
        return Status.SKIP, "not inside a git repository"
    try:
        output = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=30,
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
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
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


def _provider_config_path(cwd: Path) -> tuple[Path, bool]:
    configured = os.environ.get("CAMBIUM_PROVIDERS")
    if configured:
        path = Path(configured)
        return (path if path.is_absolute() else cwd / path), True
    return cwd / ".cambium" / "providers.json", False


def check_provider_env(cwd: Path) -> tuple[Status, str]:
    """Check provider key presence without printing environment names or values.

    A provider with a missing ``api_key_env`` is WARN by default. Configurations
    may set ``required: true`` to make that missing key FAIL; ``required`` must
    be a boolean and invalid provider configuration always FAILs.
    """

    path, explicit = _provider_config_path(cwd)
    if path.exists():
        if not path.is_file():
            return Status.FAIL, f"{path}: provider config path is not a file"
        try:
            providers = load_provider_specs(path)
        except (OSError, ValueError) as exc:
            return Status.FAIL, f"{path}: provider config validation failed: {exc}"
        source = str(path)
    elif explicit:
        return Status.FAIL, f"{path}: configured provider file does not exist"
    else:
        try:
            providers = validate_provider_specs(DEFAULT_SAMPLE)
        except ValueError as exc:  # The shipped sample is still a config input.
            return Status.FAIL, f"default provider sample validation failed: {exc}"
        source = "default sample"

    presence = env_report(providers)
    states = ", ".join(
        f"{provider.name}={'set' if presence[provider.name] else 'missing'}"
        for provider in providers
    )
    missing_required = [
        provider.name
        for provider in providers
        if provider.required and not presence[provider.name]
    ]
    missing_optional = [
        provider.name
        for provider in providers
        if not provider.required and not presence[provider.name]
    ]
    if missing_required:
        return Status.FAIL, (
            f"{source}: {states}; required provider key missing for "
            f"{', '.join(missing_required)}"
        )
    if missing_optional:
        return Status.WARN, (
            f"{source}: {states}; missing provider key is WARN unless required=true "
            f"({', '.join(missing_optional)})"
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


def _doctor_provider_specs(cwd: Path) -> tuple[str, tuple[object, ...]]:
    path, explicit = _provider_config_path(cwd)
    if path.exists():
        if not path.is_file():
            raise ValueError("provider config path is not a file")
        return str(path), load_provider_specs(path)
    if explicit:
        raise ValueError("configured provider file does not exist")
    return "default sample", validate_provider_specs(DEFAULT_SAMPLE)


def check_auth_coverage(cwd: Path, path: Path | None = None) -> tuple[Status, str]:
    """Compare configured providers with stored entries; never use the network."""
    target = _auth_path(path)
    try:
        source, raw_specs = _doctor_provider_specs(cwd)
    except (OSError, ValueError) as exc:
        return Status.SKIP, f"auth coverage skipped: provider config validation failed: {exc}"

    metadata = auth.inspect_metadata(target)
    if not metadata.directory_exists:
        return Status.WARN, "auth store coverage is unavailable because the directory is absent"
    if metadata.issue is not None or not metadata.directory_secure:
        return Status.FAIL, "auth store coverage cannot be checked on insecure metadata"
    if not metadata.file_exists:
        return Status.WARN, "auth store coverage is unavailable because the file is absent"
    if not metadata.file_secure:
        return Status.FAIL, "auth store coverage cannot be checked on insecure metadata"
    try:
        document = AuthStore(target).read()
    except AuthError as exc:
        return Status.FAIL, f"auth store coverage cannot be checked: {exc}"

    specs = tuple(raw_specs)
    stored = {credential.provider: credential for credential in document.providers}
    missing_required = [
        spec.name for spec in specs if spec.required and spec.name not in stored
    ]
    missing_optional = [
        spec.name for spec in specs if not spec.required and spec.name not in stored
    ]
    present = [
        f"{spec.name}={spec.api_key_env}"
        for spec in specs
        if spec.name in stored
    ]
    configured_names = {spec.name for spec in specs}
    extra = sorted(name for name in stored if name not in configured_names)

    if missing_required:
        detail = (
            f"{source}: required providers missing from auth store: "
            f"{', '.join(missing_required)}"
        )
        if present:
            detail += f"; configured entries: {', '.join(present)}"
        return Status.FAIL, detail
    if missing_optional:
        detail = (
            f"{source}: optional providers missing from auth store: "
            f"{', '.join(missing_optional)}"
        )
        if present:
            detail += f"; configured entries: {', '.join(present)}"
        return Status.WARN, detail
    if extra:
        return Status.WARN, f"{source}: unconfigured auth entries: {', '.join(extra)}"
    if present:
        return Status.PASS, (
            f"{source}: auth coverage complete; configured entries: {', '.join(present)}"
        )
    return Status.PASS, f"{source}: auth coverage complete"


def _sqlite_integrity(db: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [row[0] for row in conn.execute("PRAGMA integrity_check") if row[0] != "ok"]
    finally:
        conn.close()


def check_conversation_store(session_dir: Path | None) -> tuple[Status, str]:
    if session_dir is None:
        return Status.SKIP, "no --session-dir given"
    db = session_dir / CONVERSATIONS_DB_REL
    if not db.is_file():
        return Status.SKIP, f"{db} does not exist"
    try:
        problems = _sqlite_integrity(db)
    except sqlite3.Error as exc:
        return Status.FAIL, f"{db}: {exc}"
    if problems:
        return Status.FAIL, f"{db}: integrity_check: {problems[:3]}"
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
            path
            for path in dataset_entries
            if path.suffix == ".jsonl" and _is_regular_file(path)
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
                raise DatasetIntegrityError(
                    f"{path}:{line_number}: record is not a JSON object"
                )
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
            cwd=repo, capture_output=True, timeout=10,
        )
        if inside.returncode != 0:
            return False
        listed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative],
            cwd=repo, capture_output=True, timeout=10,
        )
    except OSError:
        return False
    return listed.returncode == 0


def check_secrets() -> tuple[Status, str]:
    """WARN (never FAIL) when ~/.omp/agent/models.yml is git-tracked."""
    models = OMP_MODELS_YML
    if not models.is_file():
        return Status.PASS, f"{models} not present"
    if _git_tracked(models.parent, models.name):
        return Status.WARN, (
            f"{models} is git-tracked — plaintext API keys "
            "(provider-landscape.md §6)"
        )
    return Status.PASS, f"{models} present but not git-tracked"


def run_checks(session_dir: Path | None, cwd: Path) -> list[Check]:
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
        (12, "Conversation store", check_conversation_store(session_dir)),
        (13, "System health", check_system_health(cwd)),
    ]
    return [
        Check(number=number, name=name, status=status, detail=detail)
        for number, name, (status, detail) in checks
    ]


def format_report(checks: list[Check]) -> str:
    lines = ["cambium doctor — Cambium harness diagnostics"]
    for check in checks:
        lines.append(
            f"  {check.number:>2}. {check.name:<22} "
            f"{check.status.value.upper():<5} {check.detail}"
        )
    counts = Counter(check.status for check in checks)
    lines.append(
        f"Summary: {counts[Status.PASS]} pass · {counts[Status.WARN]} warn · "
        f"{counts[Status.SKIP]} skip · {counts[Status.INFO]} info · "
        f"{counts[Status.FAIL]} fail"
    )
    return "\n".join(lines)


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
    args = parser.parse_args(argv)
    checks = run_checks(args.session_dir, Path.cwd())
    print(format_report(checks))
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
