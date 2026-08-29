from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/cambium/branch_history.py",
    '''class _Event:
    order: tuple[int, int]
    kind: str
    payload: dict[str, Any]
    task_id: str | None
''',
    '''class _Event:
    order: tuple[int, int]
    kind: str
    payload: dict[str, Any]
    task_id: str | None
    generation: int
''',
)
replace_once(
    "src/cambium/branch_history.py",
    '''    if not task_id or generation < 0 or turn < 1:
''',
    '''    if not task_id or generation < 1 or turn < 1:
''',
)
replace_once(
    "src/cambium/branch_history.py",
    '''            seq = row.get("seq")
            sequence = seq if type(seq) is int and seq >= 0 else row_index
            events.append(_Event((store_index, sequence), kind, payload, task_id))
''',
    '''            generation = row.get("generation", payload.get("generation"))
            if type(generation) is not int or generation < 0:
                generation = 0
            seq = row.get("seq")
            sequence = seq if type(seq) is int and seq >= 0 else row_index
            events.append(
                _Event((store_index, sequence), kind, payload, task_id, generation)
            )
''',
)
replace_once(
    "src/cambium/branch_history.py",
    '''        _int(event.payload, "generation"),
        _int(event.payload, "turn"),
''',
    '''        event.generation,
        _int(event.payload, "turn"),
''',
)
replace_once(
    "src/cambium/branch_history.py",
    '''        if generation is not None and _int(event.payload, "generation") != generation:
''',
    '''        if generation is not None and event.generation != generation:
''',
)
replace_once(
    "src/cambium/branch_history.py",
    '''        f"tool={tool_name} ok={str(bool(event.payload.get('ok'))).lower()} ",
''',
    '''        f"tool={tool_name} ok={str(bool(event.payload.get('ok'))).lower()}",
''',
)
replace_once(
    "tests/scenarios/test_branch_history.py",
    '''                "task_id": "child",
                "generation": 1,
                "turn": 2,
''',
    '''                "task_id": "child",
                "turn": 2,
''',
)
