"""Line-oriented terminal front end for Cambium one-shot sessions."""

from __future__ import annotations

import asyncio
import inspect
import sys
from dataclasses import replace

_PROMPT = "cambium> "
_EXIT_EOF = 0
_EXIT_INTERRUPT = 130
_EXIT_BROKEN_PIPE = 0
_EXIT_BACKEND_MISSING = 1
_EXIT_RUN_FAILED = 1


def _run(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def run_tui(config, *, input_stream=None, output_stream=None, error_stream=None) -> int:
    """Run the line-oriented terminal loop and return an exit code."""
    source = sys.stdin if input_stream is None else input_stream
    out = sys.stdout if output_stream is None else output_stream
    err = sys.stderr if error_stream is None else error_stream

    try:
        from cambium import oneshot, render
    except ImportError as exc:
        err.write(f"cambium tui: {exc}\n")
        return _EXIT_BACKEND_MISSING

    failed = False
    try:
        while True:
            out.write(_PROMPT)
            out.flush()
            line = source.readline()
            if line == "":
                out.write("\n")
                out.flush()
                return _EXIT_RUN_FAILED if failed else _EXIT_EOF
            prompt = line.rstrip("\r\n")
            if not prompt.strip():
                continue
            try:
                prompt_config = replace(config, prompt=prompt)
                response = _run(oneshot.run_oneshot(prompt_config))
                text = render.render_text_result(response)
                if response.exit_code != 0:
                    failed = True
            except Exception as exc:
                err.write(f"cambium: {exc}\n")
                err.flush()
                continue
            out.write(text)
            if not text.endswith("\n"):
                out.write("\n")
            out.flush()
    except KeyboardInterrupt:
        out.write("\n")
        out.flush()
        return _EXIT_INTERRUPT
    except BrokenPipeError:
        return _EXIT_BROKEN_PIPE


__all__ = ["run_tui"]
