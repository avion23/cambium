"""DSPy program implementing the should_review decision."""

from cambium.modules.base import DSPyModuleBase

from .decide import Decision, ReviewOutput


class ShouldReviewModuleDSPy(DSPyModuleBase):
    """DSPy classifier with the same decision and metric interface."""

    name = "should_review"
    label_field = "review"
    fallback_decision = Decision.REVIEW
    output_type = ReviewOutput
    decision_type = Decision
    signature_name = "ShouldReviewSignature"
    signature_docstring = (
        "Decide review when the worker result shows refusal markers, leftover TODOs, "
        "high-stakes domains, or missing verification."
    )
