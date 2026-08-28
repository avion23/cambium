"""Selected-run detail rows for the full-width operator rail."""

from types import SimpleNamespace

from cambium.tui_screen import _display_width, _rail_rows


def _snapshot(*agents):
    return SimpleNamespace(
        agents=agents,
        context=SimpleNamespace(
            epoch=3,
            summary_segments=1,
            approximate=True,
            estimated_trunk_tokens=2,
            summary_trunk_bytes=8,
            estimated_raw_tail_tokens=1,
            raw_tail_bytes=4,
            checkpoint_ref="root/epoch-0003.json",
        ),
        recent_events=(),
    )


def _run(task_id="root", **values):
    defaults = dict(
        task_id=task_id,
        parent_task_id=None if task_id == "root" else "root",
        role="main" if task_id == "root" else "sub",
        state="active",
        lineage="exact",
        epoch=3,
        tool="run_shell",
    )
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_selected_run_details_follow_its_summary_and_use_existing_formats() -> None:
    snapshot = _snapshot(_run(), _run("child", state="queued", tool=None))

    rows = _rail_rows(snapshot, 32, 32, activity_line="▸ streaming 17.8s · answer fragment")
    text = [value for _, value in rows]

    assert text[:8] == [
        " LANES",
        "└●= root E3",
        "   phase ▸ streaming",
        "   tail answer fragment",
        "   • run_shell",
        "   duration 17s",
        "   status active",
        "  └○= child E3",
    ]
    assert " CONTEXT" in text


def test_tool_detail_keeps_duration_first_and_rows_width_safe() -> None:
    snapshot = _snapshot(_run(duration_ms=3456))
    rows = _rail_rows(snapshot, 24, 32, activity_line="◌ thinking 2.9s · " + "x" * 120)

    assert any("3s • run_shell" in value for _, value in rows)
    assert any(value.endswith("…") for _, value in rows if "tail" in value)
    assert all(_display_width(value) <= 24 for _, value in rows)
    assert all("." not in value for _, value in rows if "duration" in value or "run_shell" in value)


def test_narrow_rail_keeps_summary_only() -> None:
    snapshot = _snapshot(_run())
    rows = _rail_rows(snapshot, 23, 32, activity_line="▸ streaming 4.9s · tail")

    assert [value for _, value in rows][:3] == [" LANES", "└●= root E3", " CONTEXT"]
    assert not any(
        value.strip().startswith(("phase", "tail", "duration", "status", "•"))
        for _, value in rows
    )


def test_context_index_stays_fixed_across_activity_and_tool_changes() -> None:
    snapshot = _snapshot(_run(tool=None))

    def context_index(activity_line: str) -> int:
        rows = _rail_rows(snapshot, 32, 32, activity_line=activity_line)
        return next(index for index, (_, text) in enumerate(rows) if text == " CONTEXT")

    indices = [
        context_index("… waiting 1s"),
        context_index("▸ streaming 2s · answer fragment"),
    ]
    snapshot.agents[0].tool = "run_shell"
    indices.append(context_index("▸ streaming 2s · answer fragment"))
    snapshot.agents[0].tool = None
    indices.append(context_index("▸ streaming 2s · answer fragment"))

    assert indices == [7, 7, 7, 7]
