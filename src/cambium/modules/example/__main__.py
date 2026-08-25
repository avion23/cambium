"""Neutral JSON stdin/stdout adapter for the reference decision module."""

from cambium.modules.base import run_module_entrypoint

from .decide import Decision, DecomposeOutput, ShouldDecomposeModule, TaskInput


def main() -> int:
    """Read one JSON request and emit one JSON response."""
    return run_module_entrypoint(
        ShouldDecomposeModule,
        TaskInput,
        DecomposeOutput,
        Decision,
        "decompose",
        "cambium.modules.example",
    )


if __name__ == "__main__":
    raise SystemExit(main())
