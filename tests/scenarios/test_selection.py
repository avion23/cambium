"""Pure provider selection scenarios."""

from dataclasses import dataclass

from cambium.selection import QualityWeights, order_candidates, quality_score


@dataclass(frozen=True)
class Item:
    name: str
    priority: int = 0


NOW = 1_000_000.0
WEIGHTS = QualityWeights()


def _debt(*, failures=0, latency=1.0, cache=0, cost=0.0, seen=NOW):
    return {
        "requests": 10,
        "failed_requests": failures,
        "latency_count": 10,
        "latency_total_s": latency * 10,
        "cache_hit_count": cache,
        "cost": cost,
        "last_seen": seen,
    }


def _names(items):
    return [item.name for item in items]


def _order(items, debt=None, incumbent=None, offset=0, weights=WEIGHTS):
    return order_candidates(
        items,
        debt=debt,
        incumbent=incumbent,
        rotation_offset=offset,
        now=NOW,
        weights=weights,
    )


def test_priority_dominates_without_evidence() -> None:
    assert _names(_order([Item("later", 2), Item("first", 0)])) == ["first", "later"]


def test_quality_refines_measured_run_and_no_data_keeps_position() -> None:
    items = [Item("slow"), Item("unknown"), Item("bad"), Item("fast")]
    debt = {
        "slow": _debt(latency=8),
        "bad": _debt(failures=2),
        "fast": _debt(latency=1),
    }
    # Unknown is a neutral barrier: measured providers do not cross its slot.
    assert _names(_order(items, debt)) == ["slow", "unknown", "fast", "bad"]


def test_stale_debt_is_neutral() -> None:
    stale = _debt(latency=100, seen=NOW - 2 * 24 * 3600)
    assert quality_score(stale, now=NOW) is None
    assert _names(_order([Item("stale"), Item("fresh")], {"stale": stale})) == [
        "stale",
        "fresh",
    ]


def test_incumbent_leads_while_eligible() -> None:
    items = [Item("incumbent"), Item("better")]
    debt = {"incumbent": _debt(latency=8), "better": _debt(latency=1)}
    assert _names(_order(items, debt, incumbent="incumbent")) == ["incumbent", "better"]


def test_filtered_incumbent_reselects_and_new_incumbent_does_not_bounce() -> None:
    debt = {"old": _debt(latency=1), "fallback": _debt(latency=5)}
    eligible = [Item("fallback")]
    assert _names(_order(eligible, debt, incumbent="old")) == ["fallback"]
    recovered = [Item("old"), Item("fallback")]
    assert _names(_order(recovered, debt, incumbent="fallback")) == ["fallback", "old"]


def test_rotation_is_deterministic_spreads_and_preserves_priority_runs() -> None:
    items = [Item("a"), Item("b"), Item("c"), Item("x", 1), Item("y", 1)]
    assert _names(_order(items, offset=1)) == ["b", "c", "a", "y", "x"]
    assert _names(_order(items, offset=1)) == _names(_order(items, offset=1))
    assert {_names(_order(items, offset=i))[0] for i in range(3)} == {"a", "b", "c"}


def test_weights_are_tuning_seam() -> None:
    items = [Item("cached_slow"), Item("uncached_fast")]
    debt = {
        "cached_slow": _debt(latency=5, cache=10),
        "uncached_fast": _debt(latency=1, cache=0),
    }
    latency_first = QualityWeights(latency_weight=1, cache_weight=0)
    cache_first = QualityWeights(latency_weight=0, cache_weight=1)
    assert _names(_order(items, debt, weights=latency_first))[0] == "uncached_fast"
    assert _names(_order(items, debt, weights=cache_first))[0] == "cached_slow"


def test_empty_and_single_candidate() -> None:
    assert _order([]) == []
    only = Item("only")
    assert _order([only], offset=99) == [only]
