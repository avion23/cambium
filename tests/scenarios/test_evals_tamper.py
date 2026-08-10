"""Canary: the conformance gate must reject eval tampering (Claim 8, M8 scope).

The eval surface (``module_conformance`` gate + ``bench``) must not be gameable
by editing a module's own tests. A complete scratch module is built in
``tmp_path`` (module.json, ``__init__``/``__main__``, architecture.md, datasets
with correct digests/meta, a matching baseline, and a passing ``tests/test_x.py``);
``module_conformance.MODULES_DIR``/``REPO_ROOT``/``PACKAGE_ROOT`` and
``bench.MODULES_DIR`` are monkeypatched to it. One tamper is applied per fresh
copy, and the conformance-gate sequence must raise ``ModuleConformanceError``
naming the tamper:

  (i)   delete a locked test file            -> validate_module's tracked-file gate
  (ii)  append ``assert True`` to every      -> M8 AST-assert gate (not on main)
        test body
  (iii) replace a tested function body       -> M8 AST-assert gate (not on main)
        with ``pass``

IMPORTANT: this currently FAILS on main because the gate has no AST-assert
check — (ii) and (iii) pass the gate today (the seam-survival gap documented in
``docs/research/bench-harness-design.md`` §8.2). The test is therefore marked
``skip`` so the suite stays green; the assertion logic is written against the
gate sequence and flips green when the M8 AST-assert gate lands.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from cambium import bench, module_conformance

MODULE_NAME = "scratch"
DECIDE = (
    "def decide(task, context=\"\"):\n"
    '    return {"decompose": False, "reason": "atomic", "confidence": 1.0}\n'
)
SCORE = (
    "def score_prediction(prediction, expected):\n"
    '    return 1.0 if prediction.get("decompose") == expected.get("decompose") else 0.0\n'
)
MAIN = (
    "import json\n"
    "import sys\n"
    "from .decide import decide\n"
    "\n"
    "\n"
    "def main():\n"
    "    payload = json.loads(sys.stdin.buffer.read())\n"
    "    result = decide(payload.get(\"task\", \"\"), payload.get(\"context\", \"\"))\n"
    "    sys.stdout.write(json.dumps(result, sort_keys=True) + \"\\n\")\n"
    "    return 0\n"
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    raise SystemExit(main())\n"
)
INIT_PY = "from .decide import decide, score_prediction\n"
TEST_PY = (
    "from cambium.modules.scratch.decide import decide, score_prediction\n"
    "\n"
    "\n"
    "def test_decide_returns_atomic():\n"
    '    assert decide("do one thing")["decompose"] is False\n'
    "\n"
    "\n"
    "def test_score_prediction_perfect():\n"
    '    assert score_prediction({"decompose": False}, {"decompose": False}) == 1.0\n'
)
ARCHITECTURE_MD = (
    "# scratch\n\nReference decision module for the tamper canary. Decides "
    "decomposition and scores predictions.\n"
)
MANIFEST = {
    "contract_version": 1,
    "module_name": MODULE_NAME,
    "cli_module": f"cambium.modules.{MODULE_NAME}",
    "protocol": "json-v1",
    "dataset_schema_version": 1,
}
DATASET_VERSION = "1.0.0"
DATASET_RECORDS = [
    {
        "id": "scratch-0001",
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "split": "train",
        "input": {"task": "Do one thing", "context": ""},
        "expected": {"decompose": False, "reason": "atomic"},
    },
    {
        "id": "scratch-0002",
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "split": "train",
        "input": {"task": "Split the service, add a queue, and deploy both", "context": ""},
        "expected": {"decompose": True, "reason": "parallel workstreams"},
    },
    {
        "id": "scratch-0003",
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "split": "eval",
        "input": {"task": "Fix the login handler", "context": ""},
        "expected": {"decompose": False, "reason": "atomic"},
    },
    {
        "id": "scratch-canary-01",
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "split": "canary",
        "input": {"task": "Roll out the dashboard to several services", "context": ""},
        "expected": {"decompose": False, "reason": "atomic"},
        "canary": True,
        "canary_info": {"name": "keyword-dense rollout", "kind": "trivially_atomic"},
    },
]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=tamper-canary", "-c", "user.email=tamper@test.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _records_for_split(split: str) -> list[dict]:
    split_field = "canary" if split == "canaries" else split
    return [record for record in DATASET_RECORDS if record["split"] == split_field]


def _build_scratch(root: Path) -> Path:
    """Create ``root`` as a git repo holding a complete, valid scratch module.

    Returns the package root (``root/src/cambium``) for monkeypatching.
    """
    _git(root, "init", "-q")
    module = root / "src" / "cambium" / "modules" / MODULE_NAME
    datasets = module / "datasets"
    baselines = module / "tests" / "baselines"

    _write_text(root / "src" / "cambium" / "__init__.py", "")
    _write_text(root / "src" / "cambium" / "modules" / "__init__.py", "")
    _write_text(module / "__init__.py", INIT_PY)
    _write_text(module / "__main__.py", MAIN)
    _write_text(module / "architecture.md", ARCHITECTURE_MD)
    _write_text(module / "decide.py", DECIDE + "\n" + SCORE)
    _write_text(module / "module.json", json.dumps(MANIFEST, indent=2))
    _write_text(module / "tests" / "test_x.py", TEST_PY)

    splits = {"train": "train.jsonl", "eval": "eval.jsonl", "canaries": "canaries.jsonl"}
    digests: dict[str, str] = {}
    for split, filename in splits.items():
        path = datasets / filename
        content = "".join(json.dumps(record) + "\n" for record in _records_for_split(split))
        _write_text(path, content)
        digests[split] = hashlib.sha256(path.read_bytes()).hexdigest()
    meta = {
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "eval_frozen_at": "2026-08-09",
        "canary_frozen_at": "2026-08-09",
        "split_digests": digests,
        "sibling_pins": {},
    }
    _write_text(datasets / "meta.json", json.dumps(meta, indent=2))

    total = len(DATASET_RECORDS)
    decompose_true = sum(
        record["expected"]["decompose"] is True for record in DATASET_RECORDS
    )
    canary_kinds = sorted(
        record["canary_info"]["kind"]
        for record in DATASET_RECORDS
        if record["split"] == "canary"
    )
    baseline = {
        "schema_version": 1,
        "module": MODULE_NAME,
        "dataset_version": DATASET_VERSION,
        "split_digests": digests,
        "git_sha": "1" * 40,
        "date": "2026-08-10T00:00:00Z",
        "python": "3.14.7",
        "pytest": "9.1.1",
        "metric": {
            split: {
                "mean": 1.0,
                "std": 0.0,
                "count": len(_records_for_split(split)),
            }
            for split in splits
        },
        "canaries": {
            "total": len(canary_kinds),
            "kinds_present": canary_kinds,
            "taxonomy_coverage": 1.0,
            "failed": 0,
        },
        "dataset": {
            "records": total,
            "duplicate_ids": 0,
            "cross_split_leaks": 0,
            "decompose_true": decompose_true,
            "decompose_false": total - decompose_true,
            "canaries": len(canary_kinds),
        },
        "tests": {
            "by_nodeid": {
                "src/cambium/modules/scratch/tests/test_x.py::test_decide_returns_atomic": 0.01,
                "src/cambium/modules/scratch/tests/test_x.py::test_score_prediction_perfect": 0.01,
            },
            "count": 2,
            "wall_seconds": {"p50": 0.01, "p90": 0.02, "max": 0.05},
        },
        "drift_thresholds": {
            "metric_mean_delta": 0.05,
            "wall_p90_ratio": 1.5,
            "canary_failed_delta": 0,
            "dataset": {"duplicate_ids": 0, "cross_split_leaks": 0},
        },
    }
    _write_text(baselines / "baseline.json", json.dumps(baseline, indent=2))
    _commit_all(root, "scratch module fixture")
    return root / "src" / "cambium"


def _copy(root: Path) -> Path:
    copy = root.parent / f"{root.name}-copy-{uuid.uuid4().hex[:8]}"
    shutil.copytree(root, copy)
    return copy


def _patch_globals(monkeypatch: pytest.MonkeyPatch, repo: Path, package_root: Path) -> None:
    modules_dir = package_root / "modules"
    monkeypatch.setattr(module_conformance, "MODULES_DIR", modules_dir)
    monkeypatch.setattr(module_conformance, "REPO_ROOT", repo)
    monkeypatch.setattr(module_conformance, "PACKAGE_ROOT", package_root)
    monkeypatch.setattr(bench, "MODULES_DIR", modules_dir)


def _run_gate(name: str) -> module_conformance.ModuleSpec:
    """The conformance-gate sequence from ModuleConformancePlugin.pytest_sessionstart."""
    spec = module_conformance.validate_module(name)
    module_conformance.scan_module_imports(spec)
    reverse_imports = module_conformance.scan_reverse_imports()
    external_module_files = module_conformance.scan_external_module_files()
    if reverse_imports or external_module_files:
        findings = [*reverse_imports, *external_module_files]
        raise module_conformance.ModuleConformanceError(
            "static module-isolation findings:\n"
            + "\n".join(finding.format() for finding in findings)
        )
    module_conformance.probe_module_cli(spec)
    return spec


def _assert_module_tests_pass(root: Path) -> None:
    module_dir = root / "src" / "cambium" / "modules" / MODULE_NAME
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=module_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _tamper_delete_test_file(repo: Path) -> None:
    test_file = repo / "src" / "cambium" / "modules" / MODULE_NAME / "tests" / "test_x.py"
    test_file.unlink()
    _commit_all(repo, "tamper: delete a locked test file")


def _tamper_append_assert_true(repo: Path) -> None:
    test_file = repo / "src" / "cambium" / "modules" / MODULE_NAME / "tests" / "test_x.py"
    lines = test_file.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        out.append(lines[index])
        stripped = lines[index].lstrip()
        if stripped.startswith("def test_") and stripped.rstrip().endswith(":"):
            indent = len(lines[index]) - len(lines[index].lstrip())
            body_end = index + 1
            while body_end < len(lines) and (
                not lines[body_end].strip()
                or len(lines[body_end]) - len(lines[body_end].lstrip()) > indent
            ):
                body_end += 1
            out.extend(lines[index + 1 : body_end])
            out.append(" " * (indent + 4) + "assert True")
            index = body_end - 1
        index += 1
    test_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    _commit_all(repo, "tamper: append assert True to every test body")


def _tamper_pass_function_body(repo: Path) -> None:
    decide_file = repo / "src" / "cambium" / "modules" / MODULE_NAME / "decide.py"
    text = decide_file.read_text(encoding="utf-8")
    pristine = SCORE.rstrip("\n")
    tampered = "def score_prediction(prediction, expected):\n    pass"
    assert pristine in text, "scratch decide.py does not contain the expected score_prediction"
    decide_file.write_text(text.replace(pristine, tampered), encoding="utf-8")
    _commit_all(repo, "tamper: replace a tested function body with pass")


def _assert_tamper_rejected(monkeypatch: pytest.MonkeyPatch, repo: Path, token: str) -> None:
    package_root = repo / "src" / "cambium"
    _patch_globals(monkeypatch, repo, package_root)
    with pytest.raises(module_conformance.ModuleConformanceError) as raised:
        _run_gate(MODULE_NAME)
    message = str(raised.value)
    assert token in message, (
        f"conformance gate rejected the tamper but did not name it: {message!r}"
    )


@pytest.mark.skip(
    reason=(
        "M8 AST-assert gate not implemented: module_conformance currently accepts tests "
        "tampered with `assert True` bodies and module functions reduced to `pass` "
        "(docs/research/bench-harness-design.md §8.2 seam-survival gap). The gate only "
        "rejects the deleted-test-file tamper today; unskip when the AST-assert gate lands."
    )
)
def test_gate_rejects_eval_tampering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pristine = tmp_path / "repo"
    pristine.mkdir()
    package_root = _build_scratch(pristine)

    # The fixture itself is a valid, gate-passing module with passing tests.
    _patch_globals(monkeypatch, pristine, package_root)
    spec = _run_gate(MODULE_NAME)
    assert spec.package_name == f"cambium.modules.{MODULE_NAME}"
    assert bench.discover_modules() == [MODULE_NAME]
    _assert_module_tests_pass(pristine)

    # (i) delete a locked test file -> the tracked-file gate rejects it.
    tampered_delete = _copy(pristine)
    _tamper_delete_test_file(tampered_delete)
    _assert_tamper_rejected(monkeypatch, tampered_delete, "test")

    # (ii) append `assert True` to every test body -> the M8 AST-assert gate must
    # reject it (on main the gate has no AST-assert check, so this is skipped).
    tampered_assert = _copy(pristine)
    _tamper_append_assert_true(tampered_assert)
    _assert_tamper_rejected(monkeypatch, tampered_assert, "assert")

    # (iii) replace a tested function body with `pass` -> the M8 AST-assert gate
    # must reject it by naming the tampered seam symbol.
    tampered_pass = _copy(pristine)
    _tamper_pass_function_body(tampered_pass)
    _assert_tamper_rejected(monkeypatch, tampered_pass, "score_prediction")
