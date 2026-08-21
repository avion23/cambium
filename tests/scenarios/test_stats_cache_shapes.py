"""Provider cache accounting shapes accepted by the user-facing stats path."""

import pytest

from cambium.stats import usage_stats_from_events


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"cached_tokens": 11}, 11),
        ({"cache_read_input_tokens": 12}, 12),
        ({"prompt_tokens_details": {"cached_tokens": 13}}, 13),
        ({"input_tokens_details": {"cached_tokens": 14}}, 14),
    ],
)
def test_cached_token_shapes_are_normalized(usage, expected) -> None:
    stats = usage_stats_from_events(
        [{"kind": "usage_event", "payload": {"turn": 1, "usage": usage}}]
    )
    assert stats is not None
    assert stats.cached_tokens == expected


def test_nested_and_normalized_cache_counts_are_not_double_counted() -> None:
    stats = usage_stats_from_events(
        [
            {
                "kind": "usage_event",
                "payload": {
                    "turn": 1,
                    "usage": {
                        "cached_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 20},
                    },
                },
            }
        ]
    )
    assert stats is not None
    assert stats.cached_tokens == 20
