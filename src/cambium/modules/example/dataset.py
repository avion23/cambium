"""Dataset loader for the should_decompose module."""

from cambium.modules.base import DatasetBundle, SharedDatasetLoader, Split

from .decide import Decision, TaskInput


class ExampleDatasetLoader(SharedDatasetLoader):
    """Load the shared v1 dataset contract for the example decision type."""

    input_type = TaskInput
    label_field = "decompose"
    positive_decision = Decision.DECOMPOSE
    negative_decision = Decision.DO_NOT_DECOMPOSE


__all__ = ["DatasetBundle", "ExampleDatasetLoader", "Split"]
