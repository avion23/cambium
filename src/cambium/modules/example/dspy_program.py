"""Optional decomposition program; discovery does not import DSPy's SDKs."""

from .decide import Decision, DecomposeOutput


def __getattr__(name: str) -> type:
    if name != "ShouldDecomposeModuleDSPy":
        raise AttributeError(name)
    from cambium.modules.dspy_module import DSPyModuleBase

    class ShouldDecomposeModuleDSPy(DSPyModuleBase):
        """DSPy classifier with the same decision and metric interface."""

        name = "should_decompose"
        label_field = "decompose"
        fallback_decision = Decision.DO_NOT_DECOMPOSE
        output_type = DecomposeOutput
        decision_type = Decision
        signature_name = "ShouldDecomposeSignature"
        signature_docstring = (
            "Decide decomposition when at least two signals show multiple requirement clauses, "
            "a long description, parallel or per-item work, multiple files, itemized lists, or "
            "verb-led workstreams."
        )

    # Export one stable class identity, with an ordinary inheritance tree.
    ShouldDecomposeModuleDSPy.__qualname__ = name
    globals()[name] = ShouldDecomposeModuleDSPy
    return ShouldDecomposeModuleDSPy
