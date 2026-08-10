"""Scenario checks for the isolated module conformance gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cambium import module_conformance


def test_reference_module_discovery_uses_tracked_contract() -> None:
    assert module_conformance.discover_modules() == ["example"]

    spec = module_conformance.validate_module("example")
    assert spec.package_name == "cambium.modules.example"
    assert spec.tests_dir.is_dir()
    assert any(path.name == "baseline.json" for path in spec.baseline_files)
    assert any(path.name == "eval.jsonl" for path in spec.dataset_files)
    assert spec.test_files


def test_reference_module_has_no_forbidden_imports() -> None:
    spec = module_conformance.validate_module("example")

    module_conformance.scan_module_imports(spec)


def test_provider_finder_rejects_every_provider_root() -> None:
    finder = module_conformance.ProviderImportBlocker()

    for provider in module_conformance.PROVIDER_IMPORTS:
        with pytest.raises(ModuleNotFoundError):
            finder.find_spec(provider)
        with pytest.raises(ModuleNotFoundError):
            finder.find_spec(f"{provider}.submodule")


def test_dataset_probe_input_is_a_json_object() -> None:
    spec = module_conformance.validate_module("example")

    payload = module_conformance._dataset_input(spec)

    assert isinstance(payload, dict)
    assert json.dumps(payload)


def test_module_tests_path_is_inside_module_package() -> None:
    spec = module_conformance.validate_module("example")
    relative = spec.tests_dir.relative_to(module_conformance.MODULES_DIR)

    assert relative == Path("example") / "tests"
