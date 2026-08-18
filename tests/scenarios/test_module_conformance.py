"""Scenario checks for the isolated module conformance gate.

The offline-environment probes must spawn real child interpreters and
command shims to verify network denial, credential stripping, and isolated
Python flags, so they are marked ``slow`` and run in the second tier.  The
pure file/digest and frozen-content checks stay in the first tier.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from cambium import module_conformance

EXPECTED_SPLIT_DIGESTS = {
    "train": "e41f1f4ca9e1905122e1faa0955cd2833bf032635ea721d33d36d1b3b7caf136",
    "eval": "f43cb1501ba4ba10fc27e2333a3794db04d6f5afa95ebfe586f66cf9d486d7ca",
    "canaries": "54bf2e41663b29d1382fe965cacb553009567287dc722a7710533bfe3e92ff3e",
}


def _one_discovered_module() -> str:
    names = module_conformance.discover_modules()
    if not names:
        pytest.skip("no decision modules are installed")
    return names[0]


def test_split_digests_anchor_metadata_baseline_and_content() -> None:
    if "example" not in module_conformance.discover_modules():
        pytest.skip("reference module cambium.modules.example is absent")
    module_path = module_conformance.MODULES_DIR / _one_discovered_module()
    metadata = json.loads((module_path / "datasets" / "meta.json").read_text(encoding="utf-8"))
    baseline = json.loads(
        (module_path / "tests" / "baselines" / "baseline.json").read_text(encoding="utf-8")
    )
    actual = {
        split: hashlib.sha256((module_path / "datasets" / filename).read_bytes()).hexdigest()
        for split, filename in module_conformance.DECISION_SPLITS.items()
    }

    assert metadata["split_digests"] == EXPECTED_SPLIT_DIGESTS
    assert baseline["split_digests"] == EXPECTED_SPLIT_DIGESTS
    assert actual == EXPECTED_SPLIT_DIGESTS


def test_gate_accepts_module_scoped_baseline() -> None:
    if "example" not in module_conformance.discover_modules():
        pytest.skip("reference module cambium.modules.example is absent")
    name = _one_discovered_module()
    spec = module_conformance.validate_module(name)

    assert spec.name == "example"
    assert spec.name == name


@pytest.mark.slow
def test_offline_subprocess_environment_strips_credentials_and_denies_network(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "must-not-leak")

    with module_conformance.module_offline_environment() as env:
        assert "EXAMPLE_API_TOKEN" not in env

        curl = subprocess.run(
            ["curl", "--fail", "http://127.0.0.1:9/"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )
        socket_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket; socket.create_connection(('127.0.0.1', 9), timeout=1)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert curl.returncode == 126
    assert "network client denied" in curl.stderr
    assert socket_probe.returncode != 0
    assert "network access is forbidden" in socket_probe.stderr


@pytest.mark.slow
def test_offline_child_denies_absolute_network_client_path() -> None:
    probe = (
        "import subprocess, sys; "
        "subprocess.run(['/usr/bin/curl', '--fail', 'http://127.0.0.1:9/'], check=False); "
        "sys.exit('absolute curl unexpectedly started')"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "network client denied during module conformance: /usr/bin/curl" in result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("client", ["curl", "wget", "nc", "ssh"])
def test_offline_child_denies_shell_network_client(client: str) -> None:
    probe = (
        "import subprocess; "
        f"subprocess.run({client + ' --version'!r}, shell=True, check=False)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "network client denied during module conformance:" in result.stderr
    assert f"/{client}" in result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("api", ["run", "Popen"])
def test_offline_child_resolves_network_client_realpath(tmp_path: Path, api: str) -> None:
    curl = shutil.which("curl")
    assert curl is not None
    alias = tmp_path / "ordinary command"
    alias.symlink_to(curl)
    probe = (
        "import subprocess, sys; "
        f"result = subprocess.{api}([{str(alias)!r}, '--fail', 'http://127.0.0.1:9/']); "
        "sys.exit(result.wait() if hasattr(result, 'wait') else result.returncode)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert result.returncode != 7
    denied = f"network client denied during module conformance: {os.path.realpath(curl)}"
    assert denied in result.stderr


@pytest.mark.slow
def test_offline_child_denies_shell_network_client_realpath_with_whitespace_path(
    tmp_path: Path,
) -> None:
    curl = shutil.which("curl")
    assert curl is not None
    alias = tmp_path / "ordinary command"
    alias.symlink_to(curl)
    command = f"'{alias}' --fail http://127.0.0.1:9/"
    probe = (
        "import subprocess, sys; "
        f"result = subprocess.run([{command!r}], shell=True, check=False); "
        "sys.exit(result.returncode)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert result.returncode != 7
    denied = f"network client denied during module conformance: {os.path.realpath(curl)}"
    assert denied in result.stderr


@pytest.mark.slow
def test_offline_guard_does_not_require_strace(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    probe = (
        "import subprocess, sys; "
        "subprocess.run([sys.executable, '-c', 'print(42)'], check=True)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "42\n"


@pytest.mark.slow
def test_offline_child_inherits_provider_import_blocker() -> None:
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", "import cambium.provider_config"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "provider import blocked by module conformance: cambium.provider_config" in result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("flag", ["-E", "-S", "-I"])
def test_offline_child_rejects_python_flags_that_bypass_provider_blocker(flag: str) -> None:
    probe = (
        "import subprocess, sys; "
        f"subprocess.run([sys.executable, {flag!r}, '-c', "
        "'import cambium.provider_config'], check=True)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert f"isolated Python flag denied during module conformance: {flag}" in result.stderr


@pytest.mark.slow
def test_offline_child_rejects_python_flag_after_option_argument() -> None:
    probe = (
        "import subprocess, sys; "
        "subprocess.run([sys.executable, '-W', 'ignore', '-I', '-c', "
        "'import cambium.provider_config'], check=True)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "isolated Python flag denied during module conformance: -I" in result.stderr


@pytest.mark.slow
def test_offline_child_rejects_python_flag_after_option_argument_with_executable() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        socket_probe = (
            "import socket; "
            f"socket.create_connection(('127.0.0.1', {port}), timeout=2).close()"
        )
        probe = (
            "import subprocess, sys; "
            "subprocess.run(['ordinary-python', '-W', 'ignore', '-I', '-c', "
            f"{socket_probe!r}], executable=sys.executable, check=True)"
        )
        with module_conformance.module_offline_environment() as env:
            result = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env=env,
            )

        listener.settimeout(0.5)
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            connected = False
        else:
            connection.close()
            connected = True

    assert result.returncode != 0
    assert not connected
    assert "isolated Python flag denied during module conformance: -I" in result.stderr


def test_freeze_check_survives_unrelated_tip_commit(tmp_path: Path, monkeypatch) -> None:
    module_path = tmp_path / "src" / "cambium" / "modules" / "example"
    datasets = module_path / "datasets"
    baselines = module_path / "tests" / "baselines"
    datasets.mkdir(parents=True)
    baselines.mkdir(parents=True)
    eval_path = datasets / "eval.jsonl"
    meta_path = datasets / "meta.json"
    baseline_path = baselines / "baseline.json"
    eval_path.write_text('{"id":"before"}\n', encoding="utf-8")
    meta = {"dataset_version": "1.0.0"}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=Cambium Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "base datasets")
    baseline_path.write_text("{}\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "baseline")
    eval_path.write_text('{"id":"after"}\n', encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "change frozen eval without bump")
    (tmp_path / "unrelated.txt").write_text("tip\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "unrelated tip")

    relative_baseline = baseline_path.relative_to(tmp_path)
    spec = module_conformance.ModuleSpec(
        name="example",
        path=module_path,
        tracked_files=(),
        python_files=(),
        test_files=(),
        baseline_files=(relative_baseline,),
        dataset_files=(),
    )
    monkeypatch.setattr(module_conformance, "REPO_ROOT", tmp_path)

    findings = module_conformance._frozen_content_findings(spec, meta)

    assert len(findings) == 1
    assert findings[0].symbol == "eval"
    assert "without dataset_version bump (1.0.0)" in findings[0].detail


@pytest.mark.slow
def test_module_deletion_leaves_shared_scenarios_green(tmp_path: Path) -> None:
    """Deleting one module directory must not break the shared scenarios.

    Copies the repository into a throwaway directory, deletes only
    ``src/cambium/modules/example/``, and runs the shared scenario suite from
    the copy. Every module-dependent scenario must skip (tolerate absence) and
    everything else must stay green; any scenario that hardcodes the removed
    module fails this canary.
    """
    if not module_conformance.discover_modules():
        pytest.skip("no decision modules are installed; nothing to delete")
    if "example" not in module_conformance.discover_modules():
        pytest.skip("reference module cambium.modules.example is absent")
    copy = tmp_path / "repo"
    copy.mkdir()
    ignore = shutil.ignore_patterns(
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        ".cambium",
        "dist",
        "build",
    )
    for entry in sorted(os.scandir(module_conformance.REPO_ROOT), key=lambda e: e.name):
        if entry.name == ".git":
            continue
        source = Path(entry.path)
        destination = copy / entry.name
        if entry.is_dir():
            shutil.copytree(source, destination, symlinks=True, ignore=ignore)
        else:
            shutil.copy2(source, destination)
    git = ["git", "-c", "user.name=Cambium Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q"], cwd=copy, check=True, capture_output=True)
    subprocess.run([*git, "add", "-A"], cwd=copy, check=True, capture_output=True)
    subprocess.run(
        [*git, "commit", "-qm", "snapshot for deletion canary"],
        cwd=copy,
        check=True,
        capture_output=True,
    )
    shutil.rmtree(copy / "src" / "cambium" / "modules" / "example")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
    }
    # The canary checks the copied checkout with the same interpreter and test
    # dependencies as the parent suite; uv environment resolution is not part
    # of the deletion contract and only repeats interpreter startup.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/scenarios/test_module_conformance.py",
            "tests/scenarios/test_bench.py",
            "tests/scenarios/test_tooling.py",
        ],
        cwd=copy,
        env=environment,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_reverse_scan_excludes_optimizer_driver() -> None:
    optimize_path = module_conformance.REPO_ROOT / "src" / "cambium" / "optimize.py"

    assert optimize_path not in module_conformance._reverse_scan_paths()


def test_external_scan_excludes_dspy_scenarios(tmp_path: Path, monkeypatch) -> None:
    scenarios = tmp_path / "tests" / "scenarios"
    scenarios.mkdir(parents=True)
    for filename in ("test_dspy_program.py", "test_optimize.py"):
        (scenarios / filename).write_text(
            "import cambium.modules.example\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(module_conformance, "REPO_ROOT", tmp_path)

    findings = module_conformance.scan_external_module_files()
    excluded = {
        Path("tests/scenarios/test_dspy_program.py"),
        Path("tests/scenarios/test_optimize.py"),
    }

    assert not {finding.path for finding in findings} & excluded


def test_external_scan_flags_unlisted_module_scenario(tmp_path: Path, monkeypatch) -> None:
    scenario = tmp_path / "tests" / "scenarios" / "test_unlisted_module.py"
    scenario.parent.mkdir(parents=True)
    scenario.write_text("import cambium.modules.example\n", encoding="utf-8")
    monkeypatch.setattr(module_conformance, "REPO_ROOT", tmp_path)

    findings = module_conformance.scan_external_module_files()

    assert any(
        finding.rule == "layout" and finding.path == Path("tests/scenarios/test_unlisted_module.py")
        for finding in findings
    )
