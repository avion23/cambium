"""Paired bootstrap and McNemar regression checks."""

from __future__ import annotations

from cambium.stats import mcnemar_pvalue, paired_bootstrap, paired_significance


def test_paired_eight_two_split_is_significant() -> None:
    results_a = [True] * 8 + [False] * 2
    results_b = [False] * 8 + [True] * 2

    report = paired_significance(results_a, results_b, seed=7)

    assert report["verdict"] == "significant"
    assert report["p_value"] < 0.05
    assert report["a_only"] == 8
    assert report["b_only"] == 2


def test_identical_paired_results_are_not_significant() -> None:
    results = [True, False, True, True, False, False, True, False]

    report = paired_significance(results, results)

    assert report["verdict"] == "not significant"
    assert report["p_value"] == 1.0
    assert mcnemar_pvalue(results, results) == 1.0
    assert paired_bootstrap(results, results, seed=3) == (0.0, 0.0)
