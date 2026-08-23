from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match for {old!r}, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/cambium/context_policy.py",
    'def from_mapping(cls, value: Mapping[str, Any]) -> "CastPolicy":',
    "def from_mapping(cls, value: Mapping[str, Any]) -> CastPolicy:",
)
replace_once(
    "src/cambium/worker.py",
    "from dataclasses import asdict, dataclass, field, replace",
    "from dataclasses import asdict, dataclass, field as dataclass_field, replace",
)
replace_once(
    "src/cambium/worker.py",
    "cast_policy: CastPolicy = field(default_factory=CastPolicy)",
    "cast_policy: CastPolicy = dataclass_field(default_factory=CastPolicy)",
)
replace_once(
    "src/cambium/summary_trunk.py",
    '    copied = [_copy_message(message, f"messages[{index}]") for index, message in enumerate(messages)]',
    '    copied = [\n        _copy_message(message, f"messages[{index}]")\n        for index, message in enumerate(messages)\n    ]',
)
