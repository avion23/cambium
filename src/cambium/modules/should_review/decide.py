"""Rule-engine implementation of the should_review decision.

Decides whether a worker result needs an adversarial review pass before it
is accepted. The DSPy seam: a future DSPy classification program can replace
the rule engine behind the same :class:`ShouldReviewModule` interface without
touching callers, the dataset, or the metric.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from cambium.modules.base import Example, Module

from .metric import should_review_metric

SENSITIVE_TERMS = frozenset(
    {
        "api key",
        "auth",
        "credit card",
        "credential",
        "encrypt",
        "gdpr",
        "hipaa",
        "password",
        "payment",
        "pii",
        "privacy",
        "secret",
        "security",
        "ssn",
        "token",
    }
)

DESTRUCTIVE_TERMS = frozenset(
    {
        "decommission",
        "delete",
        "destroy",
        "drop",
        "force",
        "migrate",
        "purge",
        "remove",
        "reset",
        "revert",
        "rewrite",
        "rollback",
    }
)

CONCURRENCY_TERMS = frozenset(
    {
        "atomicity",
        "concurrent",
        "consistency",
        "deadlock",
        "idempotent",
        "lock",
        "race",
        "retry",
        "transaction",
    }
)

FILE_EXTENSIONS = ("py", "rs", "ts", "js", "go", "toml", "json", "yaml", "yml", "md", "sh", "sql", "txt", "cfg")

_REFUSAL_RE = re.compile(
    r"\b(?:i\s+can'?t|i\s+cannot|cannot|can'?t\s+do|unable\s+to|not\s+able\s+to|refus[a-z]*)\b",
    re.IGNORECASE,
)
_MARKER_RE = re.compile(
    r"\b(?:todo|fixme|hack|not\s+implemented|placeholder|stub)\b", re.IGNORECASE
)
_TEST_RE = re.compile(r"\b(?:test|tests|spec|specs|assert|pytest|unittest)\b", re.IGNORECASE)
_NO_TESTS_RE = re.compile(r"\b(?:no\s+tests?|without\s+tests?|untested)\b", re.IGNORECASE)
_TERSE_RE = re.compile(r"\b(?:lgtm|done|applied|ok\b|looks\s+good|looks\s+fine)\b", re.IGNORECASE)
_FILE_REF_RE = re.compile(r"[A-Za-z0-9_./-]+\.(?:py|rs|ts|js|go|toml|json|yaml|yml|md|sh|sql|txt|cfg)\b")
_REVIEWED_SUPPRESSION_RE = re.compile(r"\balready (?:reviewed|approved)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TaskInput:
    """Input schema for the should_review module."""

    task: str
    context: str = ""


class Decision(Enum):
    """Domain decision for whether a worker result needs a review pass."""

    REVIEW = "review"
    DO_NOT_REVIEW = "do_not_review"


@dataclass(frozen=True, slots=True)
class ReviewOutput:
    """Prediction: whether the worker result needs an adversarial review.

    ``review`` is a read-only compatibility boolean for the wire boundary.
    Domain code must use ``decision``.
    """

    decision: Decision
    reason: str
    confidence: float = 1.0

    @property
    def review(self) -> bool:
        """Return the wire boolean view; use ``decision`` as the domain model."""
        return self.decision is Decision.REVIEW


def should_review(task: str, context: str = "") -> ReviewOutput:
    """Decide whether a worker result warrants an adversarial review pass.

    Evidence-based rules: refusal markers, leftover TODO/FIXME/HACK markers,
    high-stakes keywords, file references, a missing test signal for large
    diffs, and a terse result for a complex change each contribute evidence;
    two or more pieces of evidence trigger review. A context that already
    records the result as reviewed or approved suppresses review.
    """
    lowered = task.lower()
    lowered_context = context.lower()

    if _REVIEWED_SUPPRESSION_RE.search(lowered_context):
        return ReviewOutput(
            decision=Decision.DO_NOT_REVIEW,
            reason="context already reviewed or approved",
            confidence=0.9,
        )

    evidence = 0
    reasons: list[str] = []

    if _REFUSAL_RE.search(lowered):
        evidence += 3
        reasons.append("worker refusal marker")

    if _MARKER_RE.search(lowered):
        evidence += 2
        reasons.append("TODO/FIXME/HACK marker left in result")

    stake_hits = [
        term
        for term in SENSITIVE_TERMS | DESTRUCTIVE_TERMS | CONCURRENCY_TERMS
        if term in lowered
    ]
    if len(stake_hits) >= 4:
        evidence += 2
        reasons.append("four or more high-stakes keywords")
    elif len(stake_hits) >= 2:
        evidence += 1
        reasons.append("high-stakes keywords")

    task_files = _FILE_REF_RE.findall(task)
    context_files = _FILE_REF_RE.findall(context)
    if len(task_files) >= 2 and not context_files:
        evidence += 1
        reasons.append("task names files but result references none")

    if len(task_files) >= 3 and (
        _NO_TESTS_RE.search(lowered)
        or not _TEST_RE.search(f"{lowered} {lowered_context}")
    ):
        evidence += 1
        reasons.append("large diff without tests")

    if (
        _TERSE_RE.search(lowered)
        and len(task.strip()) <= 60
        and (len(stake_hits) >= 2 or len(task_files) >= 2)
    ):
        evidence += 1
        reasons.append("terse result for a complex task")

    if evidence >= 2:
        return ReviewOutput(
            decision=Decision.REVIEW,
            reason="; ".join(reasons) or "review threshold met",
            confidence=0.8,
        )
    return ReviewOutput(
        decision=Decision.DO_NOT_REVIEW,
        reason="result is complete and low-risk",
        confidence=0.7,
    )


class ShouldReviewModule(Module):
    """Decision module: does a worker result need an adversarial review?

    Pure rule engine today; a DSPy program may replace the engine behind
    this interface later.
    """

    name = "should_review"

    async def decide(self, input: TaskInput) -> ReviewOutput:
        """Run the rule engine over one task input."""
        return should_review(input.task, input.context)

    def metric(self, example: Example) -> float:
        """Score one example; exact match on the decision wins."""
        return should_review_metric(example)
