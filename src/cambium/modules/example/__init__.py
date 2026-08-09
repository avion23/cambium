"""Reference decision module: should a task be decomposed?

The should_decompose module is the reference example of the Cambium
per-module pattern: a pure-Python rule engine behind a stable
:class:`~cambium.modules.base.Module` interface, its own JSONL dataset,
and its own metric. The DSPy seam is ``decide.py`` — a future DSPy
classification program can replace the rule engine without changing
callers, the metric, or the dataset.

Dataset schema
--------------
Each line of ``datasets/example_pairs.jsonl``::

    {"input": {"task": str, "context": str},
     "expected": {"decompose": bool, "reason": str}}

The optional top-level ``canary`` boolean marks dataset-integrity
entries planted to catch reward hacking in future evals: they are
deliberately misaligned with surface heuristics (a keyword-greedy
decomposer gets them wrong). Eval runs must process every entry,
canaries included — they are scored like any other entry.
"""

from .dataset import DatasetBundle, ExampleDatasetLoader, Split
from .decide import DecomposeOutput, ShouldDecomposeModule, TaskInput, should_decompose
from .metric import evaluate_split, should_decompose_metric

__all__ = [
    "DatasetBundle",
    "DecomposeOutput",
    "ExampleDatasetLoader",
    "ShouldDecomposeModule",
    "Split",
    "TaskInput",
    "evaluate_split",
    "should_decompose",
    "should_decompose_metric",
]
