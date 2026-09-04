"""Optional classifiers must remain ordinary, independently instantiable modules."""

from importlib import import_module

import pytest


@pytest.mark.parametrize(("package", "class_name"), [
    ("example", "ShouldDecomposeModuleDSPy"),
    ("should_review", "ShouldReviewModuleDSPy"),
])
def test_program_construction_does_not_mutate_its_class(package: str, class_name: str) -> None:
    dspy = pytest.importorskip("dspy")
    common = import_module("cambium.modules.dspy_module").DSPyModuleBase
    program_class = getattr(import_module(f"cambium.modules.{package}.dspy_program"), class_name)
    bases = program_class.__bases__
    first, second = program_class(None), program_class(None)
    assert program_class.__bases__ == bases
    assert issubclass(program_class, common)
    assert isinstance(first, dspy.Module)
    assert first._predict is not second._predict
    assert len(first.named_predictors()) == 1
    second.load_state(first.dump_state())
    assert second._predict.signature.instructions == first._predict.signature.instructions
