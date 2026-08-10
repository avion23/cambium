"""Rule-engine implementation of the should_decompose decision.

The DSPy seam: a future DSPy classification program can replace the rule
engine behind the same :class:`ShouldDecomposeModule` interface without
touching callers, the dataset, or the metric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from cambium.modules.base import Example, Module

from .metric import should_decompose_metric

ACTION_VERBS = frozenset(
    {
        "add",
        "update",
        "refactor",
        "implement",
        "migrate",
        "build",
        "fix",
        "create",
        "remove",
        "rewrite",
        "backfill",
        "introduce",
        "restructure",
        "split",
        "port",
    }
)

HIGH_SIGNAL = (
    "multiple",
    "several",
    "both",
    "subtasks",
    "components",
    "services",
    "independently",
    "in parallel",
    "separately",
    "decompose",
)


@dataclass(frozen=True, slots=True)
class TaskInput:
    """Input schema for the should_decompose module."""

    task: str
    context: str = ""


class Decision(Enum):
    """Domain decision for whether a task should be decomposed."""

    DECOMPOSE = "decompose"
    DO_NOT_DECOMPOSE = "do_not_decompose"


@dataclass(frozen=True, slots=True)
class DecomposeOutput:
    """Prediction: whether the task should be decomposed into subtasks.

    ``decompose`` is a read-only compatibility shim for the former boolean
    field. Domain code must use ``decision``.
    """

    decision: Decision
    reason: str
    confidence: float = 1.0

    @property
    def decompose(self) -> bool:
        """Return the legacy boolean view; use ``decision`` as the domain model."""
        return self.decision is Decision.DECOMPOSE


def should_decompose(task: str, context: str = "") -> DecomposeOutput:
    """Decide whether a task warrants decomposition into subtasks.

    Evidence-based rules: sentences, description length, parallel-work
    keywords, "each"-style per-item phrasing, file references, itemized
    lists, and verb-led workstream clauses each contribute evidence; two
    or more pieces of evidence trigger decomposition. A context that
    already names subtasks suppresses decomposition.
    """
    lowered = task.lower()
    context_lowered = context.lower()

    if "subtask" in context_lowered or "decompos" in context_lowered:
        return DecomposeOutput(
            decision=Decision.DO_NOT_DECOMPOSE,
            reason="context already provides a decomposition",
            confidence=0.9,
        )

    evidence = 0
    reasons: list[str] = []

    sentences = [s for s in re.split(r"[.;]\s+", task.strip()) if s]
    if len(sentences) >= 3:
        evidence += 1
        reasons.append("three or more distinct requirement clauses")

    if len(task) > 220:
        evidence += 1
        reasons.append("long task description")

    keyword_hits = [k for k in HIGH_SIGNAL if k in lowered]
    if len(keyword_hits) >= 2:
        evidence += 1
        reasons.append("parallel-work keywords")

    if re.search(r"\beach\b", lowered):
        evidence += 1
        reasons.append("per-item work signaled by 'each'")

    file_refs = re.findall(
        r"[A-Za-z0-9_./-]+\.(?:py|rs|ts|js|go|toml|json|yaml|md|sh|sql)\b", task
    )
    if len(file_refs) >= 3:
        evidence += 1
        reasons.append("three or more files touched")

    if len(re.findall(r"(?m)(?:^\s*[-*]\s+|\d+[).]\s)", task)) >= 3:
        evidence += 2
        reasons.append("explicit itemized list")

    clauses = [c.strip() for c in re.split(r"[,;]\s+", task)]
    action_verbs = [
        clause
        for clause in clauses
        if (words := clause.split())
        and words[0].lower().rstrip(".") in ACTION_VERBS
    ]
    if len(action_verbs) >= 3:
        evidence += 2
        reasons.append("three or more verb-led workstreams")
    elif len(action_verbs) == 2:
        evidence += 1
        reasons.append("two verb-led workstreams")

    if evidence >= 2:
        return DecomposeOutput(
            decision=Decision.DECOMPOSE,
            reason="; ".join(reasons) or "evidence threshold met",
            confidence=0.8,
        )
    return DecomposeOutput(
        decision=Decision.DO_NOT_DECOMPOSE,
        reason="task is atomic or already scoped",
        confidence=0.7,
    )


class ShouldDecomposeModule(Module):
    """Reference decision module: should a task be decomposed?

    Pure rule engine today; a DSPy program may replace the engine behind
    this interface later.
    """

    name = "should_decompose"

    async def decide(self, input: TaskInput) -> DecomposeOutput:
        """Run the rule engine over one task input."""
        return should_decompose(input.task, input.context)

    def metric(self, example: Example) -> float:
        """Score one example; exact match on the decision wins."""
        return should_decompose_metric(example)
