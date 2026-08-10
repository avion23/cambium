"""Line-oriented terminal front end for Cambium one-shot sessions.

``run_tui`` reads prompt lines from ``input_stream``, sends each one together
with ``config`` to ``cambium.oneshot.run``, and prints the returned response
via ``cambium.render.text``.  Both backends are imported lazily so importing
this module allocates no terminal and no provider session.  EOF exits 0,
Ctrl-C exits 130, a closed output pipe exits 0, and a missing backend exits 1.
Backend failures are written to ``error_stream`` and the loop continues.
"""

from __future__ import annotations

import asyncio
import sys

_PROMPT = "cambium> "
_EXIT_EOF = 0
_EXIT_INTERRUPT = 130
_EXIT_BROKEN_PIPE = 0
_EXIT_BACKEND_MISSING = 1


def run_tui(config, *, input_stream=None, output_stream=None, error_stream=None) -> int:
    """Run the line-oriented one-shot terminal loop and return an exit code."""
    source = sys.stdin if input_stream is None else input_stream
    out = sys.stdout if output_stream is None else output_stream
    err = sys.stderr if error_stream is None else error_stream

    try:
        from cambium import oneshot, render
    except ImportError as exc:
        err.write(f"cambium tui: {exc}\n")
        return _EXIT_BACKEND_MISSING

    try:
        while True:
            out.write(_PROMPT)
            out.flush()
            line = source.readline()
            if line == "":
                out.write("\n")
                out.flush()
                return _EXIT_EOF
            prompt = line.rstrip("\r\n")
            if not prompt.strip():
                continue
            try:
                response = oneshot.run(config, prompt)
                if asyncio.iscoroutine(response):
                    response = asyncio.run(response)
                text = render.text(response)
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
