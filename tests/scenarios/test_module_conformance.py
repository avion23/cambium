"""Scenario checks for the isolated module conformance gate."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cambium import module_conformance

EXPECTED_SPLIT_DIGESTS = {
    "train": "e2f72c5ec10d9f7723e5064825d65ec3382943556a76a8f7eeaeecc97b1f407b",
    "eval": "ce2b6a304e3b93ce7450b258828fd0eb1b11afd72f2827021a0aef61a1dedbf0",
    "canaries": "07d334d1488c415efef65c977efb8bbafea028f3913f5baef0d8a419bf5d058b",
}


def _one_discovered_module() -> str:
    names = module_conformance.discover_modules()
    if not names:
        pytest.skip("no decision modules are installed")
    return names[0]


def _validated_spec_or_skip():
    name = _one_discovered_module()
    try:
        return module_conformance.validate_module(name)
    except module_conformance.ModuleConformanceError as exc:
        pytest.skip(f"module has pending dataset/baseline conformance findings: {exc}")


def test_reference_module_discovery_uses_tracked_contract() -> None:
    names = module_conformance.discover_modules()
    if not names:
        pytest.skip("no decision modules are installed")
    name = names[0]

    try:
        spec = module_conformance.validate_module(name)
    except module_conformance.ModuleConformanceError as exc:
        assert name in str(exc)
        return
    assert spec.package_name == f"cambium.modules.{name}"
    assert spec.tests_dir.is_dir()
    assert any(path.name == "baseline.json" for path in spec.baseline_files)
    assert any(path.name == "eval.jsonl" for path in spec.dataset_files)
    assert spec.test_files


def test_reference_module_has_no_forbidden_imports() -> None:
    spec = _validated_spec_or_skip()

    module_conformance.scan_module_imports(spec)


def test_reverse_import_audit_enumerates_concrete_symbols() -> None:
    findings = module_conformance.scan_reverse_imports()
    rendered = [finding.format() for finding in findings]

    assert len(findings) == 7
    assert any("src/cambium/bench.py:223:build_module_report" in item for item in rendered)
    assert any("scripts/check_dataset_v1.py:25:ExampleDatasetLoader" in item for item in rendered)
    assert any(
        "scripts/generate_should_decompose_v1.py:30:should_decompose" in item
        for item in rendered
    )


def test_layout_audit_names_only_external_module_tooling() -> None:
    findings = module_conformance.scan_external_module_files()

    assert {finding.path.as_posix() for finding in findings} == {
        "scripts/check_dataset_v1.py",
        "scripts/generate_should_decompose_v1.py",
    }


def test_split_digests_anchor_metadata_baseline_and_content() -> None:
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


def test_gate_fails_on_record_metadata_version_mismatch() -> None:
    with pytest.raises(module_conformance.ModuleConformanceError) as raised:
        module_conformance.validate_module(_one_discovered_module())

    message = str(raised.value)
    assert "record dataset_version must match meta.json" in message
    assert "'1.0.0' != '1.1.0'" in message


def test_gate_fails_on_metadata_digest_mismatch(monkeypatch) -> None:
    original_load_json = module_conformance._load_json

    def load_with_bad_train_digest(path: Path):
        value = original_load_json(path)
        if path.name == "meta.json" and isinstance(value, dict):
            value["split_digests"]["train"] = "0" * 64
        return value

    monkeypatch.setattr(module_conformance, "_load_json", load_with_bad_train_digest)

    with pytest.raises(module_conformance.ModuleConformanceError) as raised:
        module_conformance.validate_module(_one_discovered_module())

    assert "metadata digest does not match content" in str(raised.value)


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


def test_offline_child_denies_absolute_network_client_path() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    probe = (
        "import subprocess, sys; "
        f"subprocess.run(['/usr/bin/curl', '--fail', {url!r}], check=False); "
        "sys.exit('absolute curl unexpectedly started')"
    )
    try:
        with module_conformance.module_offline_environment() as env:
            result = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env=env,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode != 0
    assert "network client denied during module conformance: /usr/bin/curl" in result.stderr


def test_offline_child_denies_shell_network_client() -> None:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    probe = (
        "import subprocess; "
        f"subprocess.run('/usr/bin/curl --fail {url}', shell=True, check=False)"
    )
    try:
        with module_conformance.module_offline_environment() as env:
            result = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env=env,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode != 0
    assert "network client denied during module conformance: /usr/bin/curl" in result.stderr


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


@pytest.mark.parametrize("flag", ["-E", "-S"])
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


def test_provider_finder_rejects_every_provider_root() -> None:
    finder = module_conformance.ProviderImportBlocker()

    for provider in module_conformance.PROVIDER_IMPORTS:
        with pytest.raises(ModuleNotFoundError):
            finder.find_spec(provider)
        with pytest.raises(ModuleNotFoundError):
            finder.find_spec(f"{provider}.submodule")


def test_dataset_probe_input_is_a_json_object() -> None:
    spec = _validated_spec_or_skip()

    payload = module_conformance._dataset_input(spec)

    assert isinstance(payload, dict)
    assert json.dumps(payload)


def test_module_tests_path_is_inside_module_package() -> None:
    spec = _validated_spec_or_skip()
    relative = spec.tests_dir.relative_to(module_conformance.MODULES_DIR)

    assert relative == Path(spec.name) / "tests"


def _validate_with_baseline_change(monkeypatch, change) -> str:
    original_load_json = module_conformance._load_json

    def changed_load_json(path: Path):
        value = original_load_json(path)
        if path.name == "baseline.json" and isinstance(value, dict):
            value = json.loads(json.dumps(value))
            change(value)
        return value

    monkeypatch.setattr(module_conformance, "_load_json", changed_load_json)
    with pytest.raises(module_conformance.ModuleConformanceError) as raised:
        module_conformance.validate_module(_one_discovered_module())
    return str(raised.value)


@pytest.mark.parametrize("field", ["git_sha", "date", "python", "pytest"])
def test_baseline_rejects_null_provenance(monkeypatch, field: str) -> None:
    message = _validate_with_baseline_change(
        monkeypatch, lambda baseline: baseline.__setitem__(field, None)
    )

    assert f":{field}: must contain plausible non-null provenance" in message


@pytest.mark.parametrize("value", [None, -0.1, "0.1"])
def test_baseline_rejects_invalid_wall_times(monkeypatch, value: object) -> None:
    def change(baseline: dict) -> None:
        baseline["tests"]["wall_seconds"]["p90"] = value

    message = _validate_with_baseline_change(monkeypatch, change)

    assert "tests.wall_seconds.p90: must be a finite non-negative number" in message


def test_baseline_rejects_empty_drift_thresholds(monkeypatch) -> None:
    message = _validate_with_baseline_change(
        monkeypatch, lambda baseline: baseline.__setitem__("drift_thresholds", {})
    )

    assert "drift_thresholds: missing required thresholds" in message


def test_baseline_rejects_foreign_test_nodeid(monkeypatch) -> None:
    name = _one_discovered_module()
    prefix = module_conformance._module_prefix(name)
    tracked = module_conformance._module_files(name, prefix)
    test_files = tuple(
        path
        for path in tracked
        if path.suffix == ".py"
        and path.name.startswith("test_")
        and path.parent == prefix / "tests"
    )
    spec = module_conformance.ModuleSpec(
        name=name,
        path=module_conformance.MODULES_DIR / name,
        tracked_files=tracked,
        python_files=(),
        test_files=test_files,
        baseline_files=(),
        dataset_files=(),
    )
    baseline_path = spec.tests_dir / "baselines" / "baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    committed_findings = module_conformance._baseline_fact_findings(
        baseline, baseline_path, spec
    )
    committed_foreign = [
        finding
        for finding in committed_findings
        if finding.detail == "test nodeid does not belong to this module's tests"
    ]

    assert len(baseline["tests"]["by_nodeid"]) == 211
    assert len(committed_foreign) == 182

    def change(baseline: dict) -> None:
        baseline["tests"]["by_nodeid"] = {"tests/scenarios/test_harness.py::test_foreign": 0.1}
        baseline["tests"]["count"] = 1

    message = _validate_with_baseline_change(monkeypatch, change)

    assert "test nodeid does not belong to this module's tests" in message


def test_installed_package_ignores_unrelated_git_and_normalizes_nodeids(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "unrelated"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    site = checkout / ".venv" / "lib" / "python3.14" / "site-packages"
    package = site / "cambium"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(module_conformance.PACKAGE_ROOT / "module_conformance.py", package)
    shutil.copy2(module_conformance.PACKAGE_ROOT / "bench.py", package)
    shutil.copytree(module_conformance.MODULES_DIR, package / "modules")
    probe = """
import json
from cambium import module_conformance as m

name = m.discover_modules()[0]
prefix = m._module_prefix(name)
tracked = m._module_files(name, prefix)
test_files = tuple(
    path for path in tracked
    if path.suffix == '.py' and path.name.startswith('test_') and path.parent == prefix / 'tests'
)
spec = m.ModuleSpec(
    name=name,
    path=m.MODULES_DIR / name,
    tracked_files=tracked,
    python_files=(),
    test_files=test_files,
    baseline_files=(),
    dataset_files=(),
)
baseline_path = spec.tests_dir / 'baselines' / 'baseline.json'
baseline = json.loads(baseline_path.read_text(encoding='utf-8'))
committed_findings = m._baseline_fact_findings(baseline, baseline_path, spec)
committed_foreign = [
    finding for finding in committed_findings if 'does not belong' in finding.detail
]
owned = {
    nodeid: duration for nodeid, duration in baseline['tests']['by_nodeid'].items()
    if nodeid.startswith('src/cambium/modules/example/tests/')
}
baseline['tests']['by_nodeid'] = owned
baseline['tests']['count'] = len(owned)
findings = m._baseline_fact_findings(baseline, baseline_path, spec)
foreign = [finding for finding in findings if 'does not belong' in finding.detail]
reverse = m.scan_reverse_imports()
print(json.dumps({
    'repo_root': str(m.REPO_ROOT),
    'package_root': str(m.PACKAGE_ROOT),
    'tracked_are_wheel_paths': all(path.parts[:2] == ('modules', name) for path in tracked),
    'resources_exist': all(m._resource_path(path).is_file() for path in tracked),
    'committed_foreign_count': len(committed_foreign),
    'owned_count': len(owned),
    'owned_foreign_count': len(foreign),
    'reverse_paths': [finding.path.as_posix() for finding in reverse],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=checkout,
        env={**os.environ, "PYTHONPATH": str(site)},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["repo_root"] == observed["package_root"]
    assert observed["tracked_are_wheel_paths"] is True
    assert observed["resources_exist"] is True
    assert observed["committed_foreign_count"] == 182
    assert observed["owned_count"] == 29
    assert observed["owned_foreign_count"] == 0
    assert "bench.py" in observed["reverse_paths"]


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
