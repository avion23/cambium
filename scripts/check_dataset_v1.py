"""Load+validate the v1 should_decompose dataset (train/eval/canaries).

Independent of the generator: reads the three JSONL files from disk,
validates the dataset-format.md envelope, checks counts/balance/duplicates/
cross-split leaks/secrets, evaluates every record through the module's neutral
JSON CLI, and asserts the metric is perfect.

Run: python3.12 scripts/check_dataset_v1.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cambium.modules.base import load_module_manifest, run_module_cli  # noqa: E402

MODULE_DIR = ROOT / "src" / "cambium" / "modules" / "example"
MANIFEST = load_module_manifest(MODULE_DIR, "example")
DATASETS = MODULE_DIR / "datasets"
FILES = {
    "train": DATASETS / "train.jsonl",
    "eval": DATASETS / "eval.jsonl",
    "canary": DATASETS / "canaries.jsonl",
}
EXPECTED_COUNTS = {"train": 200, "eval": 50, "canary": 10}

ENVELOPE = {
    "id": str,
    "schema_version": int,
    "dataset_version": str,
    "split": str,
    "added_at": str,
    "added_by": str,
    "source": str,
    "license": str,
    "redacted": bool,
    "notes": str,
}

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bpassword\s*[=:]\s*\S+", re.IGNORECASE),
    re.compile(r"\b(token|api[_-]?key|secret)\s*[=:]\s*[A-Za-z0-9_\-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b\+?[0-9][0-9\s\-()]{8,}[0-9]\b"),
]

# Evidence signals used only for the report-only difficulty metric.  Decision
# labels and metric scores come from the neutral module CLI below.
ACTION_VERBS = frozenset(
    {
        "add",
        "update",
        "refactor",
        "implement",
        "migrate",
        "build",
        "fix",
        "create",
        "remove",
        "rewrite",
        "backfill",
        "introduce",
        "restructure",
        "split",
        "port",
    }
)
HIGH_SIGNAL = (
    "multiple",
    "several",
    "both",
    "subtasks",
    "components",
    "services",
    "independently",
    "in parallel",
    "separately",
    "decompose",
)
FILE_RE = re.compile(r"[A-Za-z0-9_./-]+\.(?:py|rs|ts|js|go|toml|json|yaml|md|sh|sql)\b")
ITEM_RE = re.compile(r"(?m)(?:^\s*[-*]\s+|\d+[).]\s)")


def evidence_band(task: str, context: str) -> tuple[int, list[str]]:
    low = task.lower()
    if "subtask" in context.lower() or "decompos" in context.lower():
        return 0, ["context_suppression"]
    evidence = 0
    why: list[str] = []
    sentences = [s for s in re.split(r"[.;]\s+", task.strip()) if s]
    if len(sentences) >= 3:
        evidence += 1
        why.append("sentences")
    if len(task) > 220:
        evidence += 1
        why.append("length")
    if len([k for k in HIGH_SIGNAL if k in low]) >= 2:
        evidence += 1
        why.append("keywords")
    if re.search(r"\beach\b", low):
        evidence += 1
        why.append("each")
    if len(FILE_RE.findall(task)) >= 3:
        evidence += 1
        why.append("files")
    if len(ITEM_RE.findall(task)) >= 3:
        evidence += 2
        why.append("itemized")
    clauses = [c.strip() for c in re.split(r"[,;]\s+", task)]
    verbs = [c for c in clauses if (w := c.split()) and w[0].lower().rstrip(".") in ACTION_VERBS]
    if len(verbs) >= 3:
        evidence += 2
        why.append("3+ verbs")
    elif len(verbs) == 2:
        evidence += 1
        why.append("2 verbs")
    return evidence, why


def load_records(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n"), f"{path.name}: missing trailing newline"
    bad_ws = [i for i, line in enumerate(text.splitlines(), 1) if line != line.rstrip()]
    assert not bad_ws, f"{path.name}: trailing whitespace on lines {bad_ws[:5]}"
    records = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name}:{line_no}: invalid JSON: {exc}") from exc
        assert isinstance(rec, dict), f"{path.name}:{line_no}: not an object"
        records.append(rec)
    return records


def main() -> int:
    all_records: dict[str, list[dict]] = {}
    for split, path in FILES.items():
        records = load_records(path)
        assert len(records) == EXPECTED_COUNTS[split], (
            f"{split}: expected {EXPECTED_COUNTS[split]}, got {len(records)}"
        )
        ids = [r["id"] for r in records]
        assert ids == sorted(ids), f"{split}: records not sorted by id"
        assert len(set(ids)) == len(ids), f"{split}: duplicate ids"
        all_records[split] = records

    print("files loaded, trailing-newline/sort/id checks passed")

    # --- envelope + schema checks ------------------------------------------
    for split, records in all_records.items():
        for r in records:
            rid = r["id"]
            for key, typ in ENVELOPE.items():
                assert isinstance(r.get(key), typ), f"{split} {rid}: envelope.{key} bad/missing"
            assert r["schema_version"] == 1, f"{rid}: schema_version != 1"
            assert r["dataset_version"] == "1.0.0", f"{rid}: dataset_version != 1.0.0"
            assert r["split"] == split, f"{rid}: split field mismatch"
            assert r["license"] == "internal", f"{rid}: license"
            assert r["redacted"] is False, f"{rid}: redacted"
            inp, exp = r["input"], r["expected"]
            assert isinstance(inp.get("task"), str) and inp["task"].strip(), f"{rid}: task"
            assert isinstance(inp.get("context"), str), f"{rid}: context"
            assert isinstance(exp.get("decompose"), bool), f"{rid}: decompose"
            assert isinstance(exp.get("reason"), str), f"{rid}: reason"
            assert (
                isinstance(r["expected_confidence"], (int, float))
                and 0 <= r["expected_confidence"] <= 1
            )
            assert isinstance(r["rationale_keywords"], list) and r["rationale_keywords"]
            assert all(isinstance(k, str) for k in r["rationale_keywords"])
            assert isinstance(r["notes"], str) and len(r["notes"]) <= 500, f"{rid}: notes"
            if split == "canary":
                assert r.get("canary") is True, f"{rid}: canary flag"
                ci = r.get("canary_info")
                assert isinstance(ci, dict), f"{rid}: canary_info"
                for k in ("name", "kind", "failure_mode", "description"):
                    assert isinstance(ci.get(k), str) and ci[k], f"{rid}: canary_info.{k}"
                assert isinstance(ci.get("anti_expected"), bool), f"{rid}: anti_expected"
                rng = ci.get("anti_expected_confidence_range")
                assert (
                    isinstance(rng, list)
                    and len(rng) == 2
                    and all(isinstance(x, (int, float)) for x in rng)
                )
    print("envelope + module-schema checks passed")

    # --- uniqueness + cross-split leak check ---------------------------------
    task_ids: dict[tuple[str, str], str] = {}
    data_hashes: dict[str, str] = {}
    for _split, records in all_records.items():
        for r in records:
            key = (r["input"]["task"], r["input"]["context"])
            assert key not in task_ids, (
                f"cross-split duplicate (task,context): {task_ids[key]} vs {r['id']}"
            )
            task_ids[key] = r["id"]
            payload = (r["input"]["task"], r["input"]["context"], r["expected"]["decompose"])
            digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
            assert digest not in data_hashes, (
                f"duplicate data payload: {data_hashes[digest]} vs {r['id']}"
            )
            data_hashes[digest] = r["id"]
    n_tasks = len(task_ids)
    print(f"uniqueness: {n_tasks} distinct (task, context) payloads, no cross-split leaks")

    # --- class balance + difficulty spread ------------------------------------
    for split, records in all_records.items():
        counts = Counter(r["expected"]["decompose"] for r in records)
        print(
            f"balance {split}: true={counts[True]} false={counts[False]} "
            f"(true {counts[True] / len(records):.2%})"
        )

    spread = Counter()
    spread_detail = Counter()
    for split in ("train", "eval"):
        for r in all_records[split]:
            ev, why = evidence_band(r["input"]["task"], r["input"]["context"])
            spread[f"{split}:{r['expected']['decompose']}:ev{ev}"] += 1
            for w in why:
                spread_detail[w] += 1
    print("difficulty spread (evidence bands by split/label):")
    for k in sorted(spread):
        print(f"    {k}: {spread[k]}")
    print("signal frequencies (train+eval):", dict(sorted(spread_detail.items())))

    # --- secrets scan (content fields only; envelope metadata like dates/ids is exempt) ---
    for split, records in all_records.items():
        for r in records:
            content = " ".join(
                [
                    r["input"]["task"],
                    r["input"]["context"],
                    r["expected"]["reason"],
                    r["notes"],
                    json.dumps(r.get("canary_info", {}), ensure_ascii=False),
                ]
            )
            for pat in SECRET_PATTERNS:
                assert not pat.search(content), f"{split} {r['id']}: secret pattern {pat.pattern}"
    print("secrets scan passed")

    # --- engine consistency through the neutral JSON module boundary ---------
    total = 0
    for split, _path in FILES.items():
        records = all_records[split]
        response = run_module_cli(
            MANIFEST.cli_module,
            {"operation": "evaluate", "records": records},
            cwd=ROOT,
            source_root=ROOT / "src",
        )
        results = response.get("results")
        assert isinstance(results, list), f"{split}: CLI did not return results"
        assert len(results) == len(records), f"{split}: CLI count mismatch"
        bad = []
        for record, result in zip(records, results, strict=True):
            assert isinstance(result, dict), f"{split} {record['id']}: bad CLI result"
            if result.get("score") != 1.0:
                bad.append(record["input"]["task"])
            prediction = result.get("prediction")
            assert isinstance(prediction, dict), f"{split} {record['id']}: missing prediction"
            assert prediction.get("decompose") == record["expected"]["decompose"], (
                f"{split} {record['id']}: decision mismatch"
            )
        assert not bad, f"{split}: {len(bad)} engine mismatches: {bad[:3]}"
        total += len(records)
    print(
        f"engine consistency: module metric == 1.0 on all {total} records "
        "through the neutral CLI"
    )

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
