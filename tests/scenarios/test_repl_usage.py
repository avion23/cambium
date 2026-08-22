"""REPL usage visibility."""

from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

from cambium import repl
from cambium.oneshot import OneShotConfig


def test_repl_prints_current_and_cumulative_token_usage(monkeypatch) -> None:
    async def fake_run_oneshot(config, *, on_event=None):
        assert config.prompt in {"inspect", "fix"}
        assert on_event is not None
        multiplier = 1 if config.prompt == "inspect" else 2
        on_event(
            {
                "kind": "usage_event",
                "payload": {
                    "turn": 1,
                    "provider": "provider-a",
                    "model": "model-a",
                    "usage": {
                        "prompt_tokens": 100 * multiplier,
                        "completion_tokens": 50 * multiplier,
                        "total_tokens": 150 * multiplier,
                        "prompt_tokens_details": {"cached_tokens": 25 * multiplier},
                    },
                },
            }
        )
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(repl.oneshot, "run_oneshot", fake_run_oneshot)
    monkeypatch.setattr(
        repl.render,
        "render_event_line",
        lambda _record, stream=None: "usage event",
    )
    monkeypatch.setattr(repl.render, "render_live_status_line", lambda _events: "")
    monkeypatch.setattr(repl.render, "render_text_result", lambda _result: "done")

    output = StringIO()
    exit_code = asyncio.run(
        repl.run_repl(
            OneShotConfig(),
            input_stream=StringIO("inspect\nfix\n/exit\n"),
            output_stream=output,
            error_stream=StringIO(),
        )
    )

    assert exit_code == 0
    text = output.getvalue()
    assert text.count("done\n") == 2
    assert "stats: calls=1" in text
    assert "tokens=150" in text
    assert "in=100" in text
    assert "out=50" in text
    assert "cached=25" in text
    assert "stats: calls=2" in text
    assert "tokens=450" in text
    assert "in=300" in text
    assert "out=150" in text
    assert "cached=75" in text
    assert "model=model-a" in text
    assert "provider=provider-a" in text
    assert "last_turn=" not in text
