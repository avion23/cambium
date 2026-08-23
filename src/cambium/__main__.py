"""Module entry point so ``python -m cambium`` matches the console script."""

import sys

from cambium.cli import main

if __name__ == "__main__":
    sys.exit(main())
