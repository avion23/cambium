"""Shared test streams for group 2's terminal scenarios."""

import io


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FlushCountingTty(_Tty):
    flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()
