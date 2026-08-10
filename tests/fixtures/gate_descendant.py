#!/usr/bin/env python3
"""Gate helper that leaves a child process for process-group cleanup tests."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    pid_file = Path(sys.argv[1])
    child = subprocess.Popen(["sleep", "60"])
    pid_file.write_text(str(child.pid), encoding="ascii")
    try:
        time.sleep(60)
    finally:
        child.kill()
        child.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
