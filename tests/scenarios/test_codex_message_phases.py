"""Keep Responses message boundaries; execute actions before completion claims."""

import json

from cambium.diffundo import (
    ProviderConfig,
    ProviderTier,
    _codex_input_item,
    _CodexRawResponse,
    _parse_codex_sse,
)
from cambium.summary_trunk import raw_tail_sha256
from cambium.worker import _context_message, _first_action_response, _parse_agent_action


def _provider() -> ProviderConfig:
    return ProviderConfig(
        name="codex", tier=ProviderTier.FAST, base_url="https://example.invalid",
        api_key_env="CODEX_KEY",
    )


def _message(phase: str, text: str) -> dict:
    return {"type": "message", "phase": phase, "content": [{
        "type": "output_text", "text": text,
    }]}


def test_codex_selects_final_text_but_worker_executes_first_action() -> None:
    provider = _provider()
    action = '{"type":"tool_call","name":"read_batch","arguments":{"paths":["calc.py"]}}'
    finish = '{"type":"finish","summary":"done","objective_met":true}'
    events = [
        {"type": "response.output_text.delta", "item_id": "comment", "delta": action},
        {"type": "response.output_text.delta", "item_id": "answer", "delta": finish},
        {"type": "response.completed", "response": {"output": [
            _message("commentary", action), _message("final_answer", finish),
        ]}},
    ]
    payload, text, error = _parse_codex_sse(
        provider, "\n".join("data: " + json.dumps(e) for e in events), "unused",
    )
    assert error is None
    assert text == finish  # General clients such as DSPy still get the final answer.
    result = _CodexRawResponse(payload, 1.0, text).to_result(provider, {})
    selected = _first_action_response(result)
    assert _parse_agent_action(selected.content)["type"] == "tool_call"
    assert selected.assistant_phase == "commentary"
    assert selected.usage == result.usage
    item = _codex_input_item({"role": "assistant", "content": action, "phase": "commentary"})
    assert item["phase"] == "commentary"
    assert "phase" not in _codex_input_item({"role": "user", "content": "next"})


def test_sparse_completion_keeps_per_message_output() -> None:
    events = [
        {"type": "response.output_text.delta", "item_id": "one", "delta": "First"},
        {"type": "response.output_text.delta", "item_id": "two", "delta": "Second"},
        {"type": "response.output_item.done", "item": {
            "id": "one", **_message("commentary", "First"),
        }},
        {"type": "response.output_item.done", "item": {
            "id": "two", **_message("final_answer", "Second"),
        }},
        {"type": "response.completed", "response": {}},
    ]
    _, text, error = _parse_codex_sse(
        _provider(), "\n".join("data: " + json.dumps(e) for e in events), "unused",
    )
    assert error is None
    assert text == "Second"


def test_phase_survives_context_and_is_part_of_its_identity() -> None:
    message = {"role": "assistant", "content": "{}", "phase": "commentary"}
    assert _context_message(message, "test") == message
    assert raw_tail_sha256([message]) != raw_tail_sha256([{**message, "phase": "final_answer"}])


