"""Decision module: does a worker result need an adversarial review?

The should_review module decides whether a worker result needs an
adversarial review pass before it is accepted. It is a pure-Python rule
engine behind a stable :class:`~cambium.modules.base.Module` interface,
with its own JSONL dataset and its own metric. The DSPy seam is
``decide.py`` — a future DSPy classification program can replace the rule
engine without changing callers, the metric, or the dataset.

Dataset schema
--------------
Each line of ``datasets/<split>.jsonl``::

    {"input": {"task": str, "context": str},
     "expected": {"review": bool, "decompose": bool, "reason": str}}

``expected.review`` is the module's domain label; ``expected.decompose``
mirrors it as the generic v1 class-balance field the bench harness reads
(its baseline carries ``decompose_true``/``decompose_false``). The
optional top-level ``canary`` boolean marks dataset-integrity entries
planted to catch reward hacking in future evals: they are deliberately
misaligned with surface heuristics (a keyword-greedy reviewer gets them
wrong). Eval runs must process every entry, canaries included — they are
scored like any other entry.
"""

from .dataset import DatasetBundle, ExampleDatasetLoader, Split
from .decide import (
    Decision,
    ReviewOutput,
    ShouldReviewModule,
    TaskInput,
    should_review,
)
from .metric import evaluate_split, evaluate_split_async, should_review_metric

__all__ = [
    "DatasetBundle",
    "Decision",
    "ExampleDatasetLoader",
    "ReviewOutput",
    "ShouldReviewModule",
    "Split",
    "TaskInput",
    "evaluate_split",
    "evaluate_split_async",
    "should_review",
    "should_review_metric",
]
