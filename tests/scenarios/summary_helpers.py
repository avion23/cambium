"""Shared synthetic summary response for routing scenario servers."""

from __future__ import annotations

import json
from typing import Any

_SUMMARY_CONTROL_OPEN = "<cambium-summary-control>\n"
_SUMMARY_CONTROL_CLOSE = "\n</cambium-summary-control>"


def _summary_completion(body: dict[str, Any], *, default_model: str) -> dict[str, Any] | None:
    """Return a strict synthetic summary response without consuming actions."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    last = messages[-1]
    content = last.get("content") if isinstance(last, dict) else None
    if not isinstance(content, str) or not content.startswith(_SUMMARY_CONTROL_OPEN):
        return None
    try:
        control = json.loads(
            content.removeprefix(_SUMMARY_CONTROL_OPEN).removesuffix(_SUMMARY_CONTROL_CLOSE)
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    required = {
        "sequence",
        "source_sha256",
        "source_message_count",
        "through_turn",
    }
    if not required <= control.keys():
        return None
    summary = {
        "type": "summary_entry",
        "sequence": control["sequence"],
        "source_sha256": control["source_sha256"],
        "source_message_count": control["source_message_count"],
        "through_turn": control["through_turn"],
        "objective": "preserve the current coding objective",
        "outcome": "captured the completed work segment",
        "decisions_added": [],
        "decisions_superseded": [],
        "facts_added": [],
        "facts_invalidated": [],
        "files_and_symbols_changed": [],
        "verification_results": [],
        "relevant_failed_approaches": [],
        "open_items": [],
    }
    model = body.get("model")
    if not isinstance(model, str) or not model:
        model = default_model
    return {
        "id": "chatcmpl-summary-fixture",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(summary, sort_keys=True, separators=(",", ":")),
                },
                "finish_reason": "stop",
            }
        ],
        # Keep pre-existing action-usage assertions stable. Dedicated summary
        # tests cover accounting with non-zero usage.
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
