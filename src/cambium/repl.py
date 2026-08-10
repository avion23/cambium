"""Line-oriented Cambium REPL.

``run_repl`` reads prompts from *input_stream*, runs each prompt through
``cambium.oneshot.run_oneshot``, and writes ``cambium.render.render`` output to
*output_stream*. The same *config* object is passed to every call, so one
repository/session context is preserved across prompts. EOF and the ``/exit``
command end the loop cleanly.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

from .oneshot import run_oneshot
from .render import render


def run_repl(
    config: Any,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
) -> int:
    """Run one interactive REPL session over a single repository context.

    Each non-empty input line is a prompt; blank lines are skipped. EOF or a
    line of exactly ``/exit`` ends the session cleanly. A prompt that raises
    while running or rendering is reported to *error_stream* and the session
    continues; the return value is 0 on a clean exit and 1 when at least one
    prompt failed.
    """
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
            result = run_oneshot(config, prompt)
            rendered = render(result)
        except Exception as exc:
            failed = True
            error_stream.write(f"repl: {exc}\n")
            continue
        output_stream.write(rendered)
        if not rendered.endswith("\n"):
            output_stream.write("\n")
    return 1 if failed else 0
