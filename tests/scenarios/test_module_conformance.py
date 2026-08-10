"""Scenario checks for the isolated module conformance gate."""

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

OFFLINE_LIMITATION = (
    "This offline guard is a BEST-EFFORT, deterministic lint-style check for common forms of "
    "accidental network use; it is not a security boundary. It CANNOT prevent a hostile "
    "same-UID module from bypassing the check with os.system, posix_spawn, raw sockets, "
    "subprocess monkey-patching, or by killing a same-UID tracer. The harness does not start "
    "such a tracer or provide an in-harness sandbox. Real containment is the deployment-layer "
    "boundary."
)


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
    assert spec.manifest is not None
    assert spec.manifest.package_name == name
    assert spec.manifest.cli_module == spec.package_name


def test_reference_module_has_no_forbidden_imports() -> None:
    spec = _validated_spec_or_skip()

    module_conformance.scan_module_imports(spec)


def test_reverse_import_audit_accepts_neutral_module_boundary() -> None:
    findings = module_conformance.scan_reverse_imports()

    assert findings == ()


def test_layout_audit_accepts_decoupled_repository_tooling() -> None:
    findings = module_conformance.scan_external_module_files()

    assert findings == ()


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


def test_gate_accepts_module_scoped_baseline() -> None:
    name = _one_discovered_module()
    spec = module_conformance.validate_module(name)

    assert spec.name == "example"
    assert spec.name == name


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


def test_validate_module_requires_tracked_module_json(monkeypatch) -> None:
    name = _one_discovered_module()
    original_regular = module_conformance._is_regular_file

    def regular_except_module_json(path: Path) -> bool:
        if Path(path).name == "module.json":
            return False
        return original_regular(path)

    monkeypatch.setattr(module_conformance, "_is_regular_file", regular_except_module_json)

    with pytest.raises(module_conformance.ModuleConformanceError) as raised:
        module_conformance.validate_module(name)

    assert "missing tracked regular file" in str(raised.value)
    assert "module.json" in str(raised.value)


def test_validate_module_rejects_baseline_module_name_mismatch(monkeypatch) -> None:
    name = _one_discovered_module()
    original_load_json = module_conformance._load_json

    def renamed_baseline(path: Path):
        value = original_load_json(path)
        if path.name == "baseline.json" and isinstance(value, dict):
            value = dict(value)
            value["module"] = "not-the-logical-module"
        return value

    monkeypatch.setattr(module_conformance, "_load_json", renamed_baseline)

    with pytest.raises(module_conformance.ModuleConformanceError) as raised:
        module_conformance.validate_module(name)

    assert "must match module.json module_name" in str(raised.value)


def test_module_scan_fails_closed_on_non_literal_dynamic_import(tmp_path) -> None:
    spec = _validated_spec_or_skip()
    probe = tmp_path / "dynamic_import_probe.py"
    probe.write_text(
        "import importlib\n"
        "target = 'cambium.modules.' + 'example'\n"
        "importlib.import_module(target)\n"
        "__import__(target)\n",
        encoding="utf-8",
    )

    issues = module_conformance._scan_python_file(probe, spec)

    assert sum("non-literal target" in issue for issue in issues) == 2


def test_module_scan_flags_cambium_harness_import(tmp_path) -> None:
    spec = _validated_spec_or_skip()
    probe = tmp_path / "harness_import_probe.py"
    probe.write_text("from cambium import supervisor\n", encoding="utf-8")

    issues = module_conformance._scan_python_file(probe, spec)

    assert any(
        "cambium harness import is forbidden: cambium.supervisor" in issue
        for issue in issues
    )


def test_reverse_scan_flags_builtin_import_of_decision_package(
    tmp_path, monkeypatch
) -> None:
    name = _one_discovered_module()
    probe = tmp_path / "reverse_builtin_import_probe.py"
    probe.write_text(f"__import__('cambium.modules.{name}')\n", encoding="utf-8")
    monkeypatch.setattr(module_conformance, "_reverse_scan_paths", lambda: (probe,))
    monkeypatch.setattr(module_conformance, "REPO_ROOT", tmp_path)

    findings = module_conformance.scan_reverse_imports()

    assert any(
        finding.rule == "reverse-import"
        and f"loads decision package cambium.modules.{name}" in finding.detail
        for finding in findings
    )


def test_reverse_scan_fails_closed_on_non_literal_dynamic_import(
    tmp_path, monkeypatch
) -> None:
    probe = tmp_path / "reverse_dynamic_import_probe.py"
    probe.write_text(
        "import importlib\n"
        "target = 'cambium.modules.example'\n"
        "importlib.import_module(target)\n"
        "__import__(target)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module_conformance, "_reverse_scan_paths", lambda: (probe,))
    monkeypatch.setattr(module_conformance, "REPO_ROOT", tmp_path)

    findings = module_conformance.scan_reverse_imports()

    assert sum("non-literal target" in finding.detail for finding in findings) == 2


def test_probe_reports_siblings_loaded_inside_cli_probe(monkeypatch) -> None:
    spec = _validated_spec_or_skip()

    def fake_run(command, **kwargs):
        import_log = Path(kwargs["env"]["CAMBIUM_MODULE_PROBE_IMPORT_LOG"])
        import_log.write_text(
            f"cambium.modules.{spec.name}\n"
            "cambium.modules.sibling_package\n"
            "cambium.modules.sibling_package.extra\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(module_conformance, "module_names", lambda: [spec.name, "sibling_package"])
    monkeypatch.setattr(module_conformance.subprocess, "run", fake_run)

    with pytest.raises(module_conformance.ModuleConformanceError) as raised:
        module_conformance.probe_module_cli(spec)

    assert "sibling modules loaded inside the JSON CLI probe" in str(raised.value)
    assert "cambium.modules.sibling_package" in str(raised.value)


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


def test_offline_guard_documents_same_uid_limitations() -> None:
    docstring = " ".join((module_conformance.module_offline_environment.__doc__ or "").split())
    assert OFFLINE_LIMITATION in docstring.replace("`", "")

    docs = (
        "docs/architecture/module-template/architecture.md",
        "docs/architecture/module-template/example-spec.md",
        "docs/architecture/module-template/dataset-format.md",
    )
    for relative_path in docs:
        text = (module_conformance.REPO_ROOT / relative_path).read_text(encoding="utf-8")
        normalized = " ".join(text.replace("**", "").replace("`", "").split())
        assert OFFLINE_LIMITATION in normalized


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

    assert len(baseline["tests"]["by_nodeid"]) == 57
    assert len(committed_foreign) == 0

    def change(baseline: dict) -> None:
        baseline["tests"]["by_nodeid"] = {"tests/scenarios/test_harness.py::test_foreign": 0.1}
        baseline["tests"]["count"] = 1

    message = _validate_with_baseline_change(monkeypatch, change)

    assert "test nodeid does not belong to this module's tests" in message


def test_baseline_rejects_foreign_path_containing_module_prefix(monkeypatch) -> None:
    name = _one_discovered_module()

    def change(baseline: dict) -> None:
        baseline["tests"]["by_nodeid"] = {
            f"foreign/modules/{name}/tests/test_{name}_module.py::test_injected": 0.1
        }
        baseline["tests"]["count"] = 1

    message = _validate_with_baseline_change(monkeypatch, change)

    assert "test nodeid does not belong to this module's tests" in message


def test_installed_package_ignores_unrelated_git_and_normalizes_nodeids(
    tmp_path: Path,
) -> None:
    if not module_conformance.discover_modules():
        pytest.skip("no decision modules are installed")
    checkout = tmp_path / "unrelated"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    site = tmp_path / "site-packages"
    dist = tmp_path / "dist"
    uv = shutil.which("uv")
    assert uv is not None
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(dist)],
        cwd=module_conformance.REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(dist.glob("cambium-*.whl"))
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(site),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
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
    assert Path(observed["package_root"]).is_relative_to(site)
    assert observed["tracked_are_wheel_paths"] is True
    assert observed["resources_exist"] is True
    assert observed["committed_foreign_count"] == 0
    assert observed["owned_count"] == 57
    assert observed["owned_foreign_count"] == 0
    assert observed["reverse_paths"] == []


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


def test_module_deletion_leaves_shared_scenarios_green(tmp_path: Path) -> None:
    """Deleting one module directory must not break the shared scenarios.

    Copies the repository into a throwaway directory, deletes only
    ``src/cambium/modules/example/``, and runs the shared scenario suite plus
    the clean-wheel checks from the copy. Every module-dependent scenario must
    skip (tolerate absence) and everything else must stay green; any scenario
    that hardcodes the removed module fails this canary.
    """
    if not module_conformance.discover_modules():
        pytest.skip("no decision modules are installed; nothing to delete")
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
    result = subprocess.run(
        [
            "uv",
            "run",
            "--python",
            "3.14.7",
            "--extra",
            "test",
            "pytest",
            "-q",
            "tests/scenarios/test_module_conformance.py",
            "tests/scenarios/test_wheel_cli.py",
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
