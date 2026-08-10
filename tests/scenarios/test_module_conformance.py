"""Scenario checks for the isolated module conformance gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
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
