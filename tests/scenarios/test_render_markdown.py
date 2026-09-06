"""Behavioral checks for the shared Rich terminal Markdown renderer."""

from __future__ import annotations

import asyncio
import re
from io import StringIO

import pytest

from cambium import repl
from cambium.oneshot import OneShotConfig
from cambium.render_markdown import render_markdown, render_markdown_if_tty
from cambium.supervisor import PlanResult, TaskResult

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Tty(StringIO):
    def isatty(self) -> bool:
        return True


def _visible(value: str) -> str:
    return _ANSI.sub("", value)


def test_rich_markdown_renders_structure_with_distinct_styles() -> None:
    h1 = render_markdown("# Primary", width=60)
    h2 = render_markdown("## Secondary", width=60)
    document = render_markdown(
        "- **Done** with `make test`\n\n> quoted\n\n```python\nprint(1)\n```",
        width=60,
    )

    assert "\x1b[" in h1 and "\x1b[" in h2
    assert h1 != h2
    visible = _visible(document)
    assert "Done" in visible and "make test" in visible and "quoted" in visible
    assert "print(1)" in visible
    assert "╭" in visible and "╯" in visible  # shared compact code panel
    assert "\x1b[40m" not in document  # no forced dark background on light terminals
    assert all(line == line.rstrip() for line in visible.splitlines())


@pytest.mark.parametrize(
    "raw",
    [
        "a\x1b[31mesc\x07b\tc\x00d\ne\r\f\n",
        "café 中\x80between\x9b31mend\n",
        "left\x1b]2;secret\x07right\u202eabc\u202c\n",
    ],
)
def test_terminal_controls_are_removed_before_rich_parses(raw: str) -> None:
    rendered = render_markdown(raw, width=60)
    visible = _visible(rendered)
    assert "secret" not in visible
    assert "\x00" not in visible and "\x07" not in visible and "\x9b" not in visible
    if "\u202e" in raw:
        assert r"\u202E" in visible and r"\u202C" in visible


def test_unicode_line_separators_are_normalized() -> None:
    visible = _visible(render_markdown("a\u0085b\u2028c\u2029d", width=60))
    assert all(letter in visible for letter in "abcd")
    assert "\u0085" not in visible and "\u2028" not in visible and "\u2029" not in visible


@pytest.mark.parametrize(("name", "value"), [("NO_COLOR", "1"), ("TERM", "dumb")])
def test_disabled_color_returns_sanitized_plain_markdown(
    monkeypatch, name: str, value: str
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setenv(name, value)
    text = "safe\x1b[31m\n# Title\n"
    assert render_markdown_if_tty(text, _Tty()) == "safe\n# Title\n"


def test_non_tty_returns_sanitized_plain_markdown(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    text = "# Title\n**bold**\n"
    assert render_markdown_if_tty(text, StringIO()) == text


def test_repl_uses_rich_only_for_tty(monkeypatch) -> None:
    result = PlanResult(
        (
            TaskResult(task_id="a", status="succeeded", exit_code=0, summary="# Done"),
            TaskResult(
                task_id="b", status="succeeded", exit_code=0, summary="used `make` and **won**"
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
    assert (
        asyncio.run(
            repl.run_repl(
                config,
                input_stream=StringIO("go\n/exit\n"),
                output_stream=tty_out,
                error_stream=StringIO(),
            )
        )
        == 0
    )
    assert "\x1b[" in tty_out.getvalue()
    assert "Done" in _visible(tty_out.getvalue())
    assert "make" in _visible(tty_out.getvalue()) and "won" in _visible(tty_out.getvalue())

    plain_out = StringIO()
    assert (
        asyncio.run(
            repl.run_repl(
                config,
                input_stream=StringIO("go\n/exit\n"),
                output_stream=plain_out,
                error_stream=StringIO(),
            )
        )
        == 0
    )
    assert "\x1b[" not in plain_out.getvalue()
    assert "used `make` and **won**" in plain_out.getvalue()
