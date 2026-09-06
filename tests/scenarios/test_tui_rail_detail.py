"""Operator lanes show useful state without blank or duplicated live-detail rows."""

from types import SimpleNamespace

import pytest

from cambium.tui_screen import _display_width, _rail_rows


@pytest.mark.parametrize("width", [23, 24, 32])
@pytest.mark.parametrize("completed", [0, 20])
def test_lane_identity_waiting_and_context_fit_without_blank_rows(
    width: int,
    completed: int,
) -> None:
    snapshot = SimpleNamespace(
        agents=(
            SimpleNamespace(
                task_id="root",
                parent_task_id=None,
                role="main",
                state="suspended",
                lineage="exact",
                epoch=3,
                provider="a",
                model="large",
                tool=None,
            ),
            *(
                SimpleNamespace(
                    task_id=f"old-{i}",
                    parent_task_id="root",
                    role="sub",
                    state="succeeded",
                    lineage="semantic",
                    epoch=3,
                )
                for i in range(completed)
            ),
            SimpleNamespace(
            SimpleNamespace(
                task_id="child-tool",
                parent_task_id="root",
                role="sub",
                state="active",
                lineage="semantic",
                epoch=3,
                provider="b",
                model="small",
                tool="read_batch",
                phase="waiting",
                last_provider_cache_hit=True,
            ),
            SimpleNamespace(
                task_id="child-provider",
                parent_task_id="root",
                role="sub",
                state="active",
                lineage="semantic",
                epoch=3,
                provider="c",
                model="medium",
                tool=None,
                phase="waiting",
                last_provider_cache_hit=False,
            ),
            ),
        ),
        context=SimpleNamespace(
            epoch=3,
            summary_segments=1,
            approximate=True,
            estimated_trunk_tokens=20,
            estimated_raw_tail_tokens=10,
            checkpoint_ref="root/epoch-3.json",
        ),
        recent_events=(),
    )
    rows = _rail_rows(snapshot, width, 20)
    text = "\n".join(value for _, value in rows)
    assert all(value.strip() and _display_width(value) <= width for _, value in rows)
    assert " CONTEXT" in text and "trunk" in text and "raw" in text
    assert "root" in text and "child-tool" in text and len(rows) <= 20
    if completed:
        assert "more: /agents" in text
    elif width >= 24:
        assert "a/large" in text and "b/small" in text and "c/medium" in text
        assert "waiting for children" in text and "tool read_batch" in text
        assert "provider wait" in text and "cache hit" in text and "cache miss" in text
    assert "checkpoint" not in text
