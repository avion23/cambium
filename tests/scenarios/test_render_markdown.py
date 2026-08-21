"""Golden and gate scenarios for terminal markdown rendering."""

from __future__ import annotations

import asyncio
from io import StringIO

from cambium import repl
from cambium.oneshot import OneShotConfig
from cambium.render_markdown import render_markdown, render_markdown_if_tty
from cambium.supervisor import PlanResult, TaskResult


class _Tty(StringIO):
    def isatty(self) -> bool:
        return True


def test_atx_headings_render_bold_with_falling_brightness() -> None:
    assert render_markdown("# Title\n") == "\x1b[1;97mTitle\x1b[0m\n"
    assert render_markdown("## Mid\n") == "\x1b[1;37mMid\x1b[0m\n"
    assert render_markdown("### Low\n") == "\x1b[1;90mLow\x1b[0m\n"
    assert render_markdown("#### Deep\n") == "\x1b[1;30mDeep\x1b[0m\n"
    assert render_markdown("##### five hashes\n") == "##### five hashes\n"
    assert render_markdown("#NoSpace\n") == "#NoSpace\n"


def test_fenced_block_is_verbatim_dim_cyan_and_suppresses_inline() -> None:
    text = "```py\ncode **not bold** `not code`\n```\nafter *soft*\n"
    expected = (
        "\x1b[2;36m```py\x1b[0m\n"
        "\x1b[2;36mcode **not bold** `not code`\x1b[0m\n"
        "\x1b[2;36m```\x1b[0m\n"
        "after \x1b[2msoft\x1b[0m\n"
    )
    assert render_markdown(text) == expected


def test_inline_code_bold_and_italic_styles() -> None:
    assert render_markdown("run `make test` now\n") == (
        "run \x1b[33mmake test\x1b[0m now\n"
    )
    assert render_markdown("**big** deal *soft*\n") == (
        "\x1b[1mbig\x1b[0m deal \x1b[2msoft\x1b[0m\n"
    )
    assert render_markdown("2 * 3 + 4 * 5\n") == "2 * 3 + 4 * 5\n"


def test_lists_preserve_layout_verbatim() -> None:
    unordered = "- alpha\n* beta\n  - nested\n    - deeper\n"
    ordered = "1. one\n2. two\n  3. indented three\n"
    assert render_markdown(unordered) == unordered
    assert render_markdown(ordered) == ordered


def test_blockquote_gets_dim_italic_prefix_and_inline_body() -> None:
    assert render_markdown("> quoted **b**\n") == (
        "\x1b[2;3m>\x1b[0m quoted \x1b[1mb\x1b[0m\n"
    )


def test_paragraphs_and_blank_lines_pass_through_verbatim() -> None:
    text = "plain paragraph line\n\nanother one, with punctuation!\n"
    assert render_markdown(text) == text


def test_c0_controls_are_stripped_before_processing() -> None:
    raw = "a\x1b[31mesc\x07b\tc\x00d\ne\r\f\n"
    rendered = render_markdown(raw)
    assert rendered == "a[31mescb\tcd\ne\n"
    assert "\x1b" not in rendered


def test_non_tty_stream_returns_input_unchanged(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    text = "# Title\n**bold**\n"
    assert render_markdown_if_tty(text, StringIO()) == text


def test_no_color_and_dumb_term_gates_return_input_unchanged(monkeypatch) -> None:
    text = "# Title\n`code`\n"
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert render_markdown_if_tty(text, _Tty()) == text
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert render_markdown_if_tty(text, _Tty()) == text


def test_empty_no_color_on_real_term_renders(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert render_markdown_if_tty("# T\n", _Tty()) == "\x1b[1;97mT\x1b[0m\n"


def test_repl_emits_rendered_summaries_only_when_output_stream_is_a_tty(
    monkeypatch,
) -> None:
    result = PlanResult(
        (
            TaskResult(task_id="a", status="succeeded", exit_code=0, summary="# Done"),
            TaskResult(
                task_id="b",
                status="succeeded",
                exit_code=0,
                summary="used `make` and **won**",
            ),
        )
    )

    async def fake_run(config: OneShotConfig, on_event=None) -> PlanResult:
        return result

    monkeypatch.setattr(repl.oneshot, "run_oneshot", fake_run)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    config = OneShotConfig()
    tty_out = _Tty()
    assert asyncio.run(repl.run_repl(
        config,
        input_stream=StringIO("go\n/exit\n"),
        output_stream=tty_out,
        error_stream=StringIO(),
    )) == 0
    plain_out = StringIO()
    assert asyncio.run(repl.run_repl(
        config,
        input_stream=StringIO("go\n/exit\n"),
        output_stream=plain_out,
        error_stream=StringIO(),
    )) == 0

    value = tty_out.getvalue()
    assert "\x1b[1;97mDone\x1b[0m" in value
    assert "\x1b[33mmake\x1b[0m" in value
    assert "\x1b[1mwon\x1b[0m" in value
    plain = plain_out.getvalue()
    assert "\x1b[1;97m" not in plain
    assert "used `make` and **won**" in plain
