"""Line-oriented Cambium REPL."""

from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import replace
from typing import TextIO

from . import oneshot, render
from .oneshot import OneShotConfig


def _config_for_prompt(config: OneShotConfig, prompt: str) -> OneShotConfig:
    return replace(config, prompt=prompt)


def _run(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def run_repl(
    config: OneShotConfig,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> int:
    """Run prompts with one fresh immutable config per prompt."""
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    error_stream = sys.stderr if error_stream is None else error_stream

    failed = False
    for line in input_stream:
        prompt = line.rstrip("\r\n")
        if prompt == "/exit":
            break
        if not prompt.strip():
            continue
        try:
            prompt_config = _config_for_prompt(config, prompt)
            result = _run(oneshot.run_oneshot(prompt_config))
            rendered = render.render_text_result(result)
            if result.exit_code != 0:
                failed = True
        except Exception as exc:
            failed = True
            error_stream.write(f"repl: {exc}\n")
            continue
        output_stream.write(rendered)
        if not rendered.endswith("\n"):
            output_stream.write("\n")
    return 1 if failed else 0


__all__ = ["run_repl"]
