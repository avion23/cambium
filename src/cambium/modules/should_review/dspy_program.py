"""Optional review program; discovery does not import DSPy's SDKs."""

from .decide import Decision, ReviewOutput


def __getattr__(name: str) -> type:
    if name != "ShouldReviewModuleDSPy":
        raise AttributeError(name)
    from cambium.modules.dspy_module import DSPyModuleBase

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

    # Export one stable class identity, with an ordinary inheritance tree.
    ShouldReviewModuleDSPy.__qualname__ = name
    globals()[name] = ShouldReviewModuleDSPy
    return ShouldReviewModuleDSPy
