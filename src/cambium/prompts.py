"""Plain runtime prompts; DSPy optimizes the policy text offline."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .summary_trunk import SUMMARY_FINDING_PRESERVATION_CONTRACT, SUMMARY_LIST_FIELDS

PROMPTS_VERSION = 13

CODING_POLICY = (
    "Work directly and minimally. Locate with repo_query, read exact regions with read_batch, "
    "reuse existing code and the standard library, and verify relevant behavior. Treat failures "
    "as evidence instead of repeating calls. Delegate only separable work likely to save wall "
    "time or context; batch independent siblings with disjoint ownership and checks, and keep "
    "small or tightly coupled work local. After joins, inspect the accepted files and run the "
    "combined check. Use branch_history for exact old evidence. Plan only when useful. Finish "
    "for the operator, not for the transcript: state the result first, then only material "
    "changes, meaningful checks, findings, or blockers the user needs next."
)
SUMMARY_POLICY = (
    SUMMARY_FINDING_PRESERVATION_CONTRACT + " Drop routine noise and duplicate reads. "
    "Record corrected conclusions in decisions_superseded or facts_invalidated. "
    "Keep unfinished work and its next concrete action in open_items, most important first."
)

# Protocol is code-owned. Optimization replaces policies, not actions or tool schemas.
_ACTION_PROTOCOL = (
    "You are Cambium's coding agent in an assigned Git worktree.\n"
    "Return exactly one action with no XML or prose. When native function tools are present in "
    "the request, invoke tools through that native function-call channel and never serialize a "
    "tool invocation into assistant text. When native tools are absent, use the textual calls "
    "fallback below. Plan and finish actions are always one JSON object with JSON-escaped "
    "strings:\n"
    '  {"type":"plan","steps":["..."]}\n'
    '  {"calls":[{"name":"TOOL","arguments":{}}]}\n'
    '  {"calls":[{"name":"TOOL","arguments":{}},{"name":"TOOL","arguments":{}}]}\n'
    '  {"type":"finish","summary":"...","objective_met":true}\n'
    "Textual tool actions always use calls. Every calls item has exactly name and arguments, with "
    "tool fields such as cmd inside arguments. Batch only independent work. "
    "The finish summary is user-facing. Default to one short sentence; use at most three "
    "compact bullets only when the task genuinely has several findings. Do not restate the "
    "task or narrate the process. Omit internal tool names, raw commands, hashes, checkpoint/"
    "worktree details, routine file paths, lint chatter, dependency status, and 'no changes' "
    "boilerplate unless the user explicitly asked for that evidence. Mention checks by outcome "
    "('tests passed'), not by command. "
    "Independent reads may run concurrently; mutations run in listed order. "
    "Each delegate spec needs a task. One child defaults to trunk+inherit; several in a "
    "batch default to semantic+spread. Optional context_mode/placement override these defaults. "
    "Cambium supplies repo, worktree, branch and provider configuration. trunk requires inherit. "
    "A proposal is not a joined result. Set objective_met only when the task is met; "
    "read-only completion needs no edit. Write inside your worktree, not .git or .cambium. "
    "Cambium creates the publication commit; do not run git commit, merge or push."
)
_SUMMARY_PROTOCOL = (
    "When the final message is <cambium-summary-control>, return only a summary JSON "
    "object, not an action. Summarize the raw messages after the last "
    "<cambium-summary-entry> and before the control block. Earlier entries are immutable; "
    "do not repeat or rewrite them.",
    'Required: non-empty "objective" and "outcome" strings. Optional short-string lists: '
    + ", ".join(SUMMARY_LIST_FIELDS)
    + ". No other fields; the harness supplies identity, sequence, and source metadata. "
    "Do not put summary tags inside values. Limit each list to 32 items.",
)
SUMMARY_PROTOCOL_LINES = (*_SUMMARY_PROTOCOL, SUMMARY_POLICY)
SEMANTIC_SUMMARIZER = "\n".join(SUMMARY_PROTOCOL_LINES)


def validate_policy(value: object) -> dict[str, str]:
    """Validate one complete text artifact at the configuration boundary."""
    if not isinstance(value, Mapping) or set(value) != {"coding", "summary"}:
        raise ValueError("prompt policy requires coding and summary text")
    result: dict[str, str] = {}
    for key in ("coding", "summary"):
        text = value[key]
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 16384:
            raise ValueError(f"prompt policy {key} must be non-empty text of at most 16384 bytes")
        result[key] = text
    return result


def prompt_path() -> Path:
    configured = os.environ.get("CAMBIUM_PROMPTS")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "cambium" / "prompts.json"


def load_policy(path: Path | None = None) -> dict[str, str]:
    """Load the automatically deployed policy, or built-ins on a fresh install."""
    target = prompt_path() if path is None else path
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        if path is not None or os.environ.get("CAMBIUM_PROMPTS"):
            raise
        return {"coding": CODING_POLICY, "summary": SUMMARY_POLICY}
    document = json.loads(text)
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError(f"unsupported prompt artifact: {target}")
    return validate_policy(document.get("policy"))


def save_policy(policy: Mapping[str, str], path: Path | None = None) -> Path:
    """Atomically replace the prompt used by subsequent sessions. No DSPy needed."""
    value = validate_policy(policy)
    target = prompt_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".prompts-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "policy": value}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
    return target


def coding_prompt(policy: Mapping[str, str] | None = None) -> str:
    selected = policy or {"coding": CODING_POLICY, "summary": SUMMARY_POLICY}
    return "\n".join(
        (
            _ACTION_PROTOCOL,
            selected["coding"],
            "A final <cambium-summary-control> requests summary JSON instead of an action; "
            "follow its response contract. Earlier summaries are background, "
            "not new source evidence.",
            "Available tools:",
        )
    )


CODING_AGENT = coding_prompt()

__all__ = [
    "CODING_AGENT",
    "CODING_POLICY",
    "SUMMARY_POLICY",
    "PROMPTS_VERSION",
    "SEMANTIC_SUMMARIZER",
    "SUMMARY_PROTOCOL_LINES",
    "coding_prompt",
    "load_policy",
    "prompt_path",
    "save_policy",
    "validate_policy",
]
