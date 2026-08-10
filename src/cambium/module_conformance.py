"""Pytest gate for the isolated Cambium module contract.

The gate is deliberately repository-aware.  A module is not conformant just
because its tests pass: its required files must be tracked, its data must be
readable, its imports must stay inside the module boundary, and its JSON CLI
must work without the checkout on ``sys.path``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


def _find_repo_root() -> Path:
    source = Path(__file__).resolve().parent
    for candidate in (source, *source.parents):
        if (candidate / ".git").exists():
            return candidate
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is not None and result.returncode == 0:
        return Path(result.stdout.strip()).resolve()
    return source.parents[1]


REPO_ROOT = _find_repo_root()
MODULES_DIR = REPO_ROOT / "src" / "cambium" / "modules"

PROVIDER_IMPORTS = (
    "cambium.diffundo",
    "cambium.provider_config",
    "anthropic",
    "cohere",
    "google.genai",
    "google.generativeai",
    "litellm",
    "mistralai",
    "openai",
)

_SENSITIVE_ENV_RE = re.compile(
    r"(?:api|key|token|secret|password|passwd|credential|authorization)", re.IGNORECASE
)
_OPTIONS_ADDED = False
_AUDIT_HOOK_INSTALLED = False


class ModuleConformanceError(ValueError):
    """Raised when a module violates the conformance contract."""


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """Tracked files and paths for one discovered module."""

    name: str
    path: Path
    tracked_files: tuple[Path, ...]
    python_files: tuple[Path, ...]
    test_files: tuple[Path, ...]
    baseline_files: tuple[Path, ...]
    dataset_files: tuple[Path, ...]

    @property
    def tests_dir(self) -> Path:
        """Return the colocated test directory."""
        return self.path / "tests"

    @property
    def package_name(self) -> str:
        """Return the import name for this module."""
        return f"cambium.modules.{self.name}"


def _is_provider_import(name: str) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in PROVIDER_IMPORTS)


def _is_regular_file(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except OSError:
        return False


def _git_ls_files(pathspec: str) -> tuple[Path, ...]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", pathspec],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ModuleConformanceError(f"git ls-files failed for {pathspec}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ModuleConformanceError(f"git ls-files failed for {pathspec}: {detail}")
    return tuple(Path(os.fsdecode(part)) for part in result.stdout.split(b"\0") if part)


def module_names() -> list[str]:
    """Return sorted immediate package children with a physical ``__init__.py``."""
    if not MODULES_DIR.is_dir():
        return []
    return sorted(
        child.name
        for child in MODULES_DIR.iterdir()
        if child.is_dir() and not child.is_symlink() and _is_regular_file(child / "__init__.py")
    )


def discover_modules() -> list[str]:
    """Return discovered module names without validating their contents."""
    return module_names()


def _load_json(path: Path) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant {value!r}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field {key!r}")
            result[key] = value
        return result

    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate,
        parse_constant=reject_constant,
    )


def _validate_json_files(
    module_name: str,
    baseline_files: tuple[Path, ...],
    dataset_files: tuple[Path, ...],
) -> None:
    errors: list[str] = []
    for path in baseline_files:
        if path.suffix.lower() != ".json":
            continue
        try:
            value = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: invalid baseline JSON: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"{path}: baseline JSON must be an object")

    for path in dataset_files:
        if path.suffix.lower() not in {".json", ".jsonl"}:
            continue
        current_line = 0
        try:
            if path.suffix.lower() == ".json":
                _load_json(path)
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                current_line = line_number
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("JSONL record must be an object")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            line_detail = f" at line {current_line}" if current_line else ""
            errors.append(f"{path}{line_detail}: invalid dataset JSON: {exc}")
    if errors:
        raise ModuleConformanceError(f"{module_name}:\n" + "\n".join(errors))


def validate_module(name: str) -> ModuleSpec:
    """Discover and validate one module's tracked shape and JSON files."""
    if name not in module_names():
        raise ModuleConformanceError(f"unknown module {name!r}")

    module_path = MODULES_DIR / name
    prefix = Path("src/cambium/modules") / name
    tracked = _git_ls_files(prefix.as_posix())
    tracked_set = set(tracked)
    errors: list[str] = []

    required_files = ("__init__.py", "__main__.py")
    for filename in required_files:
        relative = prefix / filename
        if relative not in tracked_set or not _is_regular_file(REPO_ROOT / relative):
            errors.append(f"missing tracked regular file {relative}")

    if not _is_regular_file(module_path / "__init__.py"):
        errors.append(f"missing regular package marker {module_path / '__init__.py'}")
    tests_dir = module_path / "tests"
    if not tests_dir.is_dir() or tests_dir.is_symlink():
        errors.append(f"missing regular tests directory {tests_dir}")

    def files_in(*parts: str) -> tuple[Path, ...]:
        selected: list[Path] = []
        for path in tracked:
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            if relative.parts[: len(parts)] == parts:
                selected.append(path)
        return tuple(sorted(selected))

    test_files = tuple(
        path
        for path in files_in("tests")
        if len(path.relative_to(prefix).parts) == 2
        and path.relative_to(prefix).parts[1].startswith("test_")
        and path.suffix == ".py"
        and _is_regular_file(REPO_ROOT / path)
    )
    if not test_files:
        errors.append(f"no tracked tests/test_*.py in {tests_dir}")

    baseline_files = tuple(
        path for path in files_in("tests", "baselines") if _is_regular_file(REPO_ROOT / path)
    )
    if not baseline_files or not any(path.suffix.lower() == ".json" for path in baseline_files):
        errors.append(f"no tracked regular baseline JSON in {tests_dir / 'baselines'}")

    dataset_files = tuple(
        path for path in files_in("datasets") if _is_regular_file(REPO_ROOT / path)
    )
    if not dataset_files or not any(
        path.suffix.lower() in {".json", ".jsonl"} for path in dataset_files
    ):
        errors.append(f"no tracked regular dataset JSON/JSONL in {module_path / 'datasets'}")

    python_files = tuple(path for path in tracked if path.suffix == ".py")
    for path in python_files:
        if not _is_regular_file(REPO_ROOT / path):
            errors.append(f"tracked Python file is not regular: {path}")

    if errors:
        raise ModuleConformanceError(f"{name}:\n" + "\n".join(errors))
    _validate_json_files(name, baseline_files, dataset_files)
    return ModuleSpec(
        name=name,
        path=module_path,
        tracked_files=tuple(sorted(tracked)),
        python_files=python_files,
        test_files=test_files,
        baseline_files=baseline_files,
        dataset_files=dataset_files,
    )


def _dataset_input(spec: ModuleSpec) -> dict[str, Any]:
    dataset_files = set(spec.dataset_files)
    dataset_dir = spec.path / "datasets"
    preferred = [
        dataset_dir / "eval.jsonl",
        dataset_dir / "train.jsonl",
    ]
    candidates = [path for path in preferred if path.relative_to(REPO_ROOT) in dataset_files]
    candidates.extend(
        REPO_ROOT / path
        for path in sorted(dataset_files)
        if path.suffix.lower() == ".jsonl" and REPO_ROOT / path not in candidates
    )
    for path in candidates:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ModuleConformanceError(f"{path}: first dataset object is not an object")
            value = record.get("input")
            if not isinstance(value, dict):
                raise ModuleConformanceError(f"{path}: first object input must be a JSON object")
            return value
    raise ModuleConformanceError(f"{spec.name}: no non-empty dataset JSONL available for CLI probe")


def _module_test_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not _SENSITIVE_ENV_RE.search(key)
    }


def probe_module_cli(spec: ModuleSpec) -> None:
    """Run the module CLI from an empty cwd with no import-path injection."""
    payload = json.dumps(_dataset_input(spec), separators=(",", ":")) + "\n"
    command = [sys.executable, "-I", "-m", spec.package_name]
    try:
        with tempfile.TemporaryDirectory(prefix="cambium-module-") as cwd:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=_module_test_env(),
                input=payload,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
    except subprocess.TimeoutExpired as exc:
        raise ModuleConformanceError(f"{spec.name}: JSON CLI timed out after 10 seconds") from exc
    except OSError as exc:
        raise ModuleConformanceError(f"{spec.name}: JSON CLI could not start: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or "no stderr diagnostics"
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI exited {result.returncode}: {detail}"
        )
    if not result.stdout.endswith("\n") or result.stdout[:-1].endswith("\n"):
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI stdout must contain one object and one trailing newline"
        )
    try:
        value, end = json.JSONDecoder().raw_decode(result.stdout[:-1])
    except json.JSONDecodeError as exc:
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI stdout is not one JSON object: {exc}"
        ) from exc
    if end != len(result.stdout) - 1:
        raise ModuleConformanceError(
            f"{spec.name}: JSON CLI stdout contains extra output after its JSON object"
        )
    if not isinstance(value, dict):
        raise ModuleConformanceError(f"{spec.name}: JSON CLI stdout must be a JSON object")


def _relative_package(path: Path, spec: ModuleSpec) -> list[str]:
    relative = path.relative_to(spec.path)
    package = ["cambium", "modules", spec.name]
    current = spec.path
    for directory in relative.parts[:-1]:
        current /= directory
        if _is_regular_file(current / "__init__.py"):
            package.append(directory)
        else:
            break
    return package


def _relative_import_target(
    path: Path, spec: ModuleSpec, level: int, module: str | None
) -> list[str]:
    package = _relative_package(path, spec)
    base_length = len(package) - level + 1
    if base_length < 0:
        return []
    target = package[:base_length]
    if module:
        target.extend(module.split("."))
    return target


def _target_name(parts: list[str]) -> str:
    return ".".join(parts)


def _is_sibling_target(target: str, spec: ModuleSpec) -> bool:
    prefix = "cambium.modules."
    if not target.startswith(prefix):
        return False
    child = target[len(prefix) :].split(".")[0]
    return child in module_names() and child != spec.name


def _check_import_target(target: str, path: Path, node: ast.AST, spec: ModuleSpec) -> str | None:
    if _is_provider_import(target):
        return f"{path}:{node.lineno}: provider import is forbidden: {target}"
    if _is_sibling_target(target, spec):
        return f"{path}:{node.lineno}: sibling import is forbidden: {target}"
    return None


def _scan_python_file(path: Path, spec: ModuleSpec) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [f"{path}: cannot parse tracked Python file: {exc}"]

    importlib_names = {"importlib"}
    import_module_names = {"import_module"}
    builtin_import_names = {"__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        import_module_names.add(alias.asname or alias.name)
            if node.module == "builtins":
                for alias in node.names:
                    if alias.name == "__import__":
                        builtin_import_names.add(alias.asname or alias.name)

    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                issue = _check_import_target(alias.name, path, node, spec)
                if issue:
                    issues.append(issue)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                target_parts = _relative_import_target(path, spec, node.level, node.module)
                if node.module:
                    issue = _check_import_target(_target_name(target_parts), path, node, spec)
                    if issue:
                        issues.append(issue)
                else:
                    for alias in node.names:
                        target = _target_name([*target_parts, alias.name])
                        issue = _check_import_target(target, path, node, spec)
                        if issue:
                            issues.append(issue)
            elif node.module:
                issue = _check_import_target(node.module, path, node, spec)
                if issue:
                    issues.append(issue)
                for alias in node.names:
                    issue = _check_import_target(
                        f"{node.module}.{alias.name}", path, node, spec
                    )
                    if issue:
                        issues.append(issue)
        elif isinstance(node, ast.Call):
            function = node.func
            is_import_call = isinstance(function, ast.Name) and (
                function.id in import_module_names or function.id in builtin_import_names
            )
            if isinstance(function, ast.Attribute) and function.attr == "import_module":
                is_import_call = (
                    isinstance(function.value, ast.Name)
                    and function.value.id in importlib_names
                )
            if isinstance(function, ast.Attribute) and function.attr == "__import__":
                is_import_call = isinstance(function.value, ast.Name) and function.value.id in {
                    "builtins",
                    *builtin_import_names,
                }
            if is_import_call and node.args and isinstance(node.args[0], ast.Constant):
                target = node.args[0].value
                if isinstance(target, str):
                    issue = _check_import_target(target, path, node, spec)
                    if issue:
                        issues.append(issue)
    return issues


def scan_module_imports(spec: ModuleSpec) -> None:
    """Reject sibling/provider imports and syntax errors in tracked Python files."""
    issues = [
        issue
        for path in spec.python_files
        for issue in _scan_python_file(REPO_ROOT / path, spec)
    ]
    if issues:
        raise ModuleConformanceError(f"{spec.name}:\n" + "\n".join(issues))


class ProviderImportBlocker:
    """Meta-path finder that fails closed on provider imports."""

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if _is_provider_import(fullname):
            raise ModuleNotFoundError(
                f"provider import blocked by module conformance: {fullname}"
            )
        return None


def _install_provider_blocker() -> None:
    if not any(isinstance(finder, ProviderImportBlocker) for finder in sys.meta_path):
        sys.meta_path.insert(0, ProviderImportBlocker())


def _install_socket_audit_hook() -> None:
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect":
            raise PermissionError("socket.connect is forbidden during module conformance")

    sys.addaudithook(audit)
    _AUDIT_HOOK_INSTALLED = True


def _loaded_siblings(module_name: str) -> list[str]:
    names = []
    for name in sys.modules:
        if not name.startswith("cambium.modules."):
            continue
        child = name.removeprefix("cambium.modules.").split(".")[0]
        if child in module_names() and child != module_name:
            names.append(name)
    return sorted(names)


def _loaded_providers() -> list[str]:
    return sorted(name for name in sys.modules if _is_provider_import(name))


class ModuleConformancePlugin:
    """Enforce one module's complete gate inside one pytest process."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.name = config.getoption("cambium_isolated_module")
        self.spec: ModuleSpec | None = None
        self.reports: dict[str, str] = {}
        self.failures: list[str] = []
        self.siblings_before: list[str] = []

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        if not self.name:
            return
        _install_provider_blocker()
        self.siblings_before = _loaded_siblings(self.name)
        if self.siblings_before:
            pytest.exit(
                f"module {self.name!r} started with sibling modules loaded: "
                + ", ".join(self.siblings_before),
                returncode=1,
            )
        loaded_providers = _loaded_providers()
        if loaded_providers:
            pytest.exit(
                "provider modules were loaded before isolated tests: "
                + ", ".join(loaded_providers),
                returncode=1,
            )
        _install_socket_audit_hook()
        try:
            self.spec = validate_module(self.name)
            scan_module_imports(self.spec)
            probe_module_cli(self.spec)
        except ModuleConformanceError as exc:
            pytest.exit(str(exc), returncode=1)

    def pytest_collection_modifyitems(
        self, session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        if self.spec is None:
            return
        tests_dir = self.spec.tests_dir.resolve()
        outside = []
        for item in items:
            item_path = Path(str(getattr(item, "path", item.fspath))).resolve()
            try:
                item_path.relative_to(tests_dir)
            except ValueError:
                outside.append(item.nodeid)
        if outside:
            pytest.exit(
                f"out-of-module collection for {self.name!r}: " + ", ".join(outside),
                returncode=1,
            )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call":
            self.reports[report.nodeid] = report.outcome

    def pytest_sessionfinish(
        self, session: pytest.Session, exitstatus: pytest.ExitCode | int
    ) -> None:
        if self.spec is None:
            return
        passed = sum(outcome == "passed" for outcome in self.reports.values())
        skipped = sum(outcome == "skipped" for outcome in self.reports.values())
        failed = sum(outcome == "failed" for outcome in self.reports.values())
        if passed == 0:
            if skipped and not failed:
                self.failures.append(f"all {skipped} collected module tests were skipped")
            else:
                self.failures.append("no module test passed")
        siblings_after = _loaded_siblings(self.name)
        if siblings_after:
            self.failures.append(
                "sibling modules loaded during isolated tests: " + ", ".join(siblings_after)
            )
        if self.failures:
            session.exitstatus = 1

    def pytest_terminal_summary(
        self, terminalreporter: Any, exitstatus: pytest.ExitCode | int, config: pytest.Config
    ) -> None:
        if self.spec is None:
            return
        counts = {outcome: sum(value == outcome for value in self.reports.values()) for outcome in (
            "passed",
            "failed",
            "skipped",
        )}
        terminalreporter.section("cambium module conformance")
        terminalreporter.write_line(
            f"{self.name}: passed={counts['passed']} failed={counts['failed']} "
            f"skipped={counts['skipped']}"
        )
        for failure in self.failures:
            terminalreporter.write_line(f"FAIL: {failure}", red=True)


def pytest_addoption(parser: Any) -> None:
    global _OPTIONS_ADDED
    if _OPTIONS_ADDED:
        return
    _OPTIONS_ADDED = True
    group = parser.getgroup("cambium-module-conformance")
    group.addoption(
        "--cambium-isolated-module",
        default=None,
        metavar="NAME",
        help="run the complete conformance gate for one module",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("cambium_isolated_module") is None:
        return
    if not config.pluginmanager.hasplugin("cambium-module-conformance"):
        config.pluginmanager.register(ModuleConformancePlugin(config), "cambium-module-conformance")
