"""Pure append-only summary trunk invariants."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from cambium.summary_trunk import (
    SUMMARY_ENTRY_CLOSE,
    SUMMARY_ENTRY_OPEN,
    SUMMARY_ENTRY_PROVENANCE,
    SUMMARY_FINDING_PRESERVATION_CONTRACT,
    SUMMARY_LIST_FIELDS,
    SUMMARY_MAX_ENTRY_BYTES,
    SUMMARY_MAX_ITEMS,
    SUMMARY_MAX_TEXT_BYTES,
    SUMMARY_TRUNCATION_MARKER,
    SummaryExpectation,
    SummaryTrunkError,
    append_summary_entry,
    build_summary_request,
    entry_mapping,
    parse_summary_message,
    parse_summary_response,
    partition_summary_trunk,
    raw_tail_sha256,
    render_summary_message,
    semantic_summary_messages,
    summary_entries,
)

HEAD = [
    {"role": "system", "content": "stable system"},
    {"role": "user", "content": "<cambium-task>task</cambium-task>"},
]
TAIL_1 = [
    {"role": "assistant", "content": '{"type":"plan","steps":["inspect"]}'},
    {"role": "user", "content": "tool read_batch ok=True\nlarge output"},
]
TAIL_2 = [
    {"role": "assistant", "content": '{"type":"tool_call","name":"edit_file"}'},
    {"role": "user", "content": "tool edit_file ok=True\nchanged a.py"},
]


def _response(expectation: SummaryExpectation, *, label: str) -> str:
    return json.dumps(
        {
            "type": "summary_entry",
            "sequence": expectation.sequence,
            "source_sha256": expectation.source_sha256,
            "source_message_count": expectation.source_message_count,
            "through_turn": expectation.through_turn,
            "objective": f"objective {label}",
            "outcome": f"outcome {label}",
            "decisions_added": [f"decision {label}"],
            "decisions_superseded": [],
            "facts_added": [f"fact {label}"],
            "facts_invalidated": [],
            "files_and_symbols_changed": [f"file {label}"],
            "verification_results": [f"test {label}"],
            "relevant_failed_approaches": [],
            "open_items": [],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _append(trunk, tail, turn, label):
    request, expectation = build_summary_request(trunk, tail, through_turn=turn)
    assert request["messages"][: len(trunk) + len(tail)] == [*trunk, *tail]
    entry = parse_summary_response(_response(expectation, label=label), expectation)
    return append_summary_entry(trunk, entry), entry


def test_consecutive_entries_never_resummarize_prior_entries() -> None:
    trunk_1, entry_1 = _append(HEAD, TAIL_1, 2, "one")
    first_summary_bytes = trunk_1[-1]["content"].encode("utf-8")

    request_2, expectation_2 = build_summary_request(trunk_1, TAIL_2, through_turn=4)
    assert request_2["messages"][: len(trunk_1)] == trunk_1
    assert expectation_2.source_sha256 == raw_tail_sha256(TAIL_2)
    assert expectation_2.source_sha256 != entry_1.source_sha256
    assert expectation_2.source_message_count == len(TAIL_2)

    entry_2 = parse_summary_response(_response(expectation_2, label="two"), expectation_2)
    trunk_2 = append_summary_entry(trunk_1, entry_2)

    assert trunk_2[:-1] == trunk_1
    assert trunk_2[-2]["content"].encode("utf-8") == first_summary_bytes
    assert [entry.sequence for entry in summary_entries(trunk_2)] == [1, 2]
    assert {entry.source_sha256 for entry in summary_entries(trunk_2)} == {
        raw_tail_sha256(TAIL_1),
        raw_tail_sha256(TAIL_2),
    }


def test_summary_request_keeps_existing_trunk_as_exact_prefix() -> None:
    trunk, _entry = _append(HEAD, TAIL_1, 3, "one")
    request, expectation = build_summary_request(trunk, TAIL_2, through_turn=7)
    assert request["messages"][: len(trunk)] == trunk
    assert request["messages"][len(trunk) : -1] == TAIL_2
    assert expectation.sequence == 2


def test_summary_request_control_is_json_with_finding_contract() -> None:
    request, _expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    control_message = request["messages"][-1]
    control_content = control_message["content"]
    control = json.loads(
        control_content.removeprefix("<cambium-summary-control>\n").removesuffix(
            "\n</cambium-summary-control>"
        )
    )

    assert control_message["role"] == "user"
    assert control["type"] == "summarize_tail"
    assert control["finding_preservation_contract"] == SUMMARY_FINDING_PRESERVATION_CONTRACT


def test_legacy_checkpoint_tail_is_migrated_on_next_flush() -> None:
    legacy = [*HEAD, *TAIL_1]
    trunk, raw_tail = partition_summary_trunk(legacy)
    assert trunk == HEAD
    assert raw_tail == TAIL_1
    request, expectation = build_summary_request(trunk, raw_tail, through_turn=3)
    assert request["messages"][:-1] == legacy
    entry = parse_summary_response(_response(expectation, label="migration"), expectation)
    migrated = append_summary_entry(trunk, entry)
    assert len(migrated) == 3
    assert summary_entries(migrated)[0].source_sha256 == raw_tail_sha256(TAIL_1)


def test_semantic_provider_reuse_exports_summaries_not_parent_head() -> None:
    trunk, _entry = _append(HEAD, TAIL_1, 2, "one")
    summaries = semantic_summary_messages(trunk)
    assert summaries == trunk[2:]
    assert all(message not in HEAD for message in summaries)


def test_semantic_provider_reuse_rejects_legacy_raw_tail() -> None:
    with pytest.raises(SummaryTrunkError, match="not a summary-only trunk"):
        semantic_summary_messages([*HEAD, *TAIL_1])


def test_summary_response_identity_is_stamped_not_echoed() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    payload = json.loads(_response(expectation, label="one"))
    payload["source_sha256"] = "0" * 64
    payload["sequence"] = 99
    # A lying or sloppy model cannot corrupt identity: the parser stamps the
    # caller's expectation over whatever the response claimed.
    entry = parse_summary_response(json.dumps(payload), expectation)
    assert entry.source_sha256 == expectation.source_sha256
    assert entry.sequence == expectation.sequence


def test_summary_response_missing_type_is_normalized() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    payload = json.loads(_response(expectation, label="one"))
    payload.pop("type")

    entry = parse_summary_response(json.dumps(payload), expectation)

    assert entry.type == "summary_entry"


def test_summary_response_wrong_type_is_normalized() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    payload = json.loads(_response(expectation, label="one"))
    payload["type"] = "not-a-summary-entry"

    entry = parse_summary_response(json.dumps(payload), expectation)

    assert entry.type == "summary_entry"


def test_summary_response_non_object_is_rejected_cleanly() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)

    with pytest.raises(SummaryTrunkError, match="exactly one JSON object"):
        parse_summary_response('["not an object"]', expectation)


def test_summary_response_is_strict_and_bounded() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    payload = json.loads(_response(expectation, label="one"))
    payload["tool_calls"] = []
    with pytest.raises(SummaryTrunkError, match="unknown"):
        parse_summary_response(json.dumps(payload), expectation)

    payload = json.loads(_response(expectation, label="one"))
    payload["objective"] = "x" * 2_001
    entry = parse_summary_response(json.dumps(payload), expectation)
    assert SUMMARY_TRUNCATION_MARKER in entry.objective
    assert len(entry.objective.encode("utf-8")) <= SUMMARY_MAX_TEXT_BYTES

    payload = json.loads(_response(expectation, label="one"))
    payload.pop("objective")
    with pytest.raises(SummaryTrunkError, match="objective"):
        parse_summary_response(json.dumps(payload), expectation)

    payload = json.loads(_response(expectation, label="one"))
    payload["open_items"] = [" \t"]
    with pytest.raises(SummaryTrunkError, match=r"open_items\[0\].*non-empty"):
        parse_summary_response(json.dumps(payload), expectation)

    payload = json.loads(_response(expectation, label="one"))
    payload["open_items"] = ["x"] * 33
    with pytest.raises(SummaryTrunkError, match="item cap"):
        parse_summary_response(json.dumps(payload), expectation)


def test_summary_wrappers_are_validated() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    entry = parse_summary_response(_response(expectation, label="one"), expectation)
    trunk = append_summary_entry(HEAD, entry)
    message = dict(trunk[-1])
    assert message["content"].startswith(SUMMARY_ENTRY_OPEN)
    assert message["content"].endswith(SUMMARY_ENTRY_CLOSE)
    parsed = parse_summary_message(message)
    assert parsed == entry

    message["role"] = "assistant"
    with pytest.raises(SummaryTrunkError, match="user role"):
        parse_summary_message(message)


def test_summary_provenance_distinguishes_raw_delimited_json() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    entry = parse_summary_response(_response(expectation, label="one"), expectation)
    raw = {
        "role": "user",
        "content": SUMMARY_ENTRY_OPEN + json.dumps(entry_mapping(entry)) + SUMMARY_ENTRY_CLOSE,
    }

    assert parse_summary_message(raw) is None
    trunk, raw_tail = partition_summary_trunk([*HEAD, raw])
    assert trunk == HEAD
    assert raw_tail == [raw]


def test_summary_entry_cannot_hide_in_stable_head() -> None:
    _request, expectation = build_summary_request(HEAD, TAIL_1, through_turn=2)
    entry = parse_summary_response(_response(expectation, label="one"), expectation)

    with pytest.raises(SummaryTrunkError, match="stable head"):
        partition_summary_trunk([HEAD[0], render_summary_message(entry)])


def test_summary_append_enforces_source_identity_and_progress() -> None:
    trunk, entry = _append(HEAD, TAIL_1, 2, "one")

    duplicate = replace(entry, sequence=2, through_turn=3)
    with pytest.raises(SummaryTrunkError, match="source_sha256.*duplicate"):
        append_summary_entry(trunk, duplicate)

    empty = replace(entry, sequence=2, source_message_count=0, through_turn=3)
    with pytest.raises(SummaryTrunkError, match="source_message_count.*positive"):
        append_summary_entry(trunk, empty)

    regressed = replace(entry, sequence=2, source_sha256="0" * 64, through_turn=2)
    with pytest.raises(SummaryTrunkError, match="through_turn.*monotonically"):
        append_summary_entry(trunk, regressed)


def test_non_string_list_items_are_coerced_not_fatal() -> None:
    """Regression: a model emitting an object in a list field killed compaction.

    Live failure: 'summary entry verification_results[0] must be a string'
    aborted the run.  Summary content is model-owned untrusted JSON; coerce
    non-string items to canonical JSON text instead of failing recovery.
    """
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    decoded = json.loads(_response(expectation, label="one"))
    decoded["verification_results"] = [{"check": "unit tests", "ok": True}, 42]
    entry = parse_summary_response(json.dumps(decoded), expectation)

    assert entry.verification_results[0] == '{"check": "unit tests", "ok": true}'
    assert entry.verification_results[1] == "42"


def test_scalar_list_field_is_wrapped_not_fatal() -> None:
    """Regression: bare string where a list belongs killed compaction (live)."""
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    decoded = json.loads(_response(expectation, label="one"))
    decoded["verification_results"] = "all checks green"
    entry = parse_summary_response(json.dumps(decoded), expectation)

    assert entry.verification_results == ("all checks green",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("objective", "\ud800"),
        ("facts_added", ["\ud800"]),
        ("verification_results", [{"nested": "\ud800"}]),
    ],
)
def test_summary_response_lone_surrogates_are_backslash_escaped(field: str, value: object) -> None:
    """Model-owned surrogate text must not escape the SummaryTrunkError boundary."""
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    decoded = json.loads(_response(expectation, label="one"))
    decoded[field] = value

    entry = parse_summary_response(json.dumps(decoded, ensure_ascii=False), expectation)

    if field == "objective":
        assert entry.objective == r"\ud800"
    else:
        assert r"\ud800" in entry_mapping(entry)[field][0]


def test_summary_response_ten_thousand_nested_objects_is_bounded_or_rejected() -> None:
    """A fuzz-depth response becomes a bounded error, never a raw RecursionError."""
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    deep_object = '{"nested":' * 10_000 + "0" + "}" * 10_000
    response = _response(expectation, label="one").replace(
        '"verification_results":["test one"]', f'"verification_results":[{deep_object}]'
    )

    try:
        entry = parse_summary_response(response, expectation)
    except SummaryTrunkError:
        return

    assert entry.verification_results == ("<deep:unrepresentable>",)


def test_oversize_list_item_is_truncated_with_a_visible_marker() -> None:
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    decoded = json.loads(_response(expectation, label="one"))
    decoded["verification_results"] = ["😀" * 1_000]

    entry = parse_summary_response(json.dumps(decoded), expectation)

    item = entry.verification_results[0]
    assert SUMMARY_TRUNCATION_MARKER in item
    assert len(item.encode("utf-8")) <= SUMMARY_MAX_TEXT_BYTES
    assert (
        len(
            json.dumps(
                entry_mapping(entry),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= SUMMARY_MAX_ENTRY_BYTES
    )


def test_oversize_total_trims_low_priority_lists_before_core_fields() -> None:
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    decoded = json.loads(_response(expectation, label="one"))
    decoded["open_items"] = [f"open {index} " + "o" * 1_980 for index in range(32)]
    decoded["relevant_failed_approaches"] = [
        f"failed {index} " + "f" * 1_980 for index in range(32)
    ]

    entry = parse_summary_response(json.dumps(decoded), expectation)

    assert len(entry.open_items) == 1
    assert 1 < len(entry.relevant_failed_approaches) < SUMMARY_MAX_ITEMS
    assert entry.decisions_added == ("decision one",)
    assert entry.objective == "objective one"
    assert entry.outcome == "outcome one"
    assert (
        len(
            json.dumps(
                entry_mapping(entry),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= SUMMARY_MAX_ENTRY_BYTES
    )


def test_rendered_entry_revalidation_uses_tolerant_field_bounds() -> None:
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    decoded = json.loads(_response(expectation, label="one"))
    decoded["objective"] = "o" * (SUMMARY_MAX_TEXT_BYTES * 2)
    message = {
        "role": "user",
        "content": (
            SUMMARY_ENTRY_OPEN
            + SUMMARY_ENTRY_PROVENANCE
            + json.dumps(decoded, sort_keys=True, separators=(",", ":"))
            + SUMMARY_ENTRY_CLOSE
        ),
    }

    entry = parse_summary_message(message)

    assert entry is not None
    assert SUMMARY_TRUNCATION_MARKER in entry.objective


def test_pathological_huge_everything_fails_with_a_clean_bound_error() -> None:
    _, expectation = build_summary_request(HEAD, TAIL_1, through_turn=3)
    decoded = json.loads(_response(expectation, label="one"))
    decoded["objective"] = "o" * (SUMMARY_MAX_TEXT_BYTES * 2)
    decoded["outcome"] = "u" * (SUMMARY_MAX_TEXT_BYTES * 2)
    for field in SUMMARY_LIST_FIELDS:
        decoded[field] = ["x" * SUMMARY_MAX_TEXT_BYTES] * (SUMMARY_MAX_ITEMS + 1)

    with pytest.raises(SummaryTrunkError, match="item cap"):
        parse_summary_response(json.dumps(decoded), expectation)
