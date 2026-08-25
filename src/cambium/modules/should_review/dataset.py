"""Dataset loader for the should_review module."""

from cambium.modules.base import DatasetBundle, SharedDatasetLoader, Split

from .decide import Decision, TaskInput


class ExampleDatasetLoader(SharedDatasetLoader):
    """Load the shared v1 dataset contract for the review decision type."""

    input_type = TaskInput
    label_field = "review"
    positive_decision = Decision.REVIEW
    negative_decision = Decision.DO_NOT_REVIEW
    require_decompose_mirror = True


__all__ = ["DatasetBundle", "ExampleDatasetLoader", "Split"]
