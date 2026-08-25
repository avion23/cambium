"""Neutral JSON stdin/stdout adapter for the should_review decision module."""

from cambium.modules.base import run_module_entrypoint

from .decide import Decision, ReviewOutput, ShouldReviewModule, TaskInput


def main() -> int:
    """Read one JSON request and emit one JSON response."""
    return run_module_entrypoint(
        ShouldReviewModule,
        TaskInput,
        ReviewOutput,
        Decision,
        "review",
        "cambium.modules.should_review",
    )


if __name__ == "__main__":
    raise SystemExit(main())
