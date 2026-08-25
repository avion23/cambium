"""DSPy program implementing the should_decompose decision."""

from cambium.modules.base import DSPyModuleBase

from .decide import Decision, DecomposeOutput


class ShouldDecomposeModuleDSPy(DSPyModuleBase):
    """DSPy classifier with the same decision and metric interface."""

    name = "should_decompose"
    label_field = "decompose"
    fallback_decision = Decision.DO_NOT_DECOMPOSE
    output_type = DecomposeOutput
    decision_type = Decision
    signature_name = "ShouldDecomposeSignature"
