#!/usr/bin/env python3
"""Update current-main context documentation for the verified append-only trunk."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one marker, found {count}")
    return text.replace(old, new, 1)


def replace_section(text: str, start: str, end: str, replacement: str, label: str) -> str:
    start_index = text.find(start)
    if start_index < 0:
        raise RuntimeError(f"{label}: start marker not found")
    end_index = text.find(end, start_index + len(start))
    if end_index < 0:
        raise RuntimeError(f"{label}: end marker not found")
    return text[:start_index] + replacement.rstrip() + "\n\n" + text[end_index:]


def replace_reviewed_line(text: str, replacement: str, label: str) -> str:
    start = text.find("**Reviewed:**")
    if start < 0:
        raise RuntimeError(f"{label}: reviewed marker not found")
    end = text.find("\n\n", start)
    if end < 0:
        raise RuntimeError(f"{label}: reviewed line terminator not found")
    return text[:start] + replacement + text[end:]


def patch_context_engine() -> None:
    path = ROOT / "docs" / "architecture" / "context-engine.md"
    text = path.read_text(encoding="utf-8")

    text = replace_once(
        text,
        "# Cache-first context engine\n\n"
        "**Status:** target contract for context reuse, branching, compaction, and cache\n"
        "accounting. Source and tests remain authoritative for current behavior. This\n"
        "document replaces the design authority previously split across\n",
        "# Cache-first append-only context engine\n\n"
        "**Status:** active contract for the implemented semantic-summary trunk plus\n"
        "target contracts for remaining provider-cache, routing, and interactive-session\n"
        "work. Source and tests remain authoritative for current behavior. This document\n"
        "replaces the design authority previously split across\n",
        "context-engine status",
    )

    text = replace_section(
        text,
        "The source of truth is an append-only event/history log.",
        "## 2. Distinct mechanisms\n",
        '''The source of truth is an append-only event/history log. The implemented active
request projection is:

```text
H + S1 + S2 + ... + Sn + small raw working tail
```

`H` is the stable system/tool head. Every `Si` is an immutable semantic summary
entry covering one new, disjoint raw message range. A flush makes one additional
provider call over the existing trunk plus the current raw tail, validates the
strict result, appends exactly one new summary entry, and removes only that
covered raw tail from the active prompt. Earlier summary bytes are never edited
or included in a later summary source.

Raw events, ordinary checkpoints, and immutable epoch files remain the audit and
recovery authority outside the active prompt. Child agents fork an immutable
checkpoint and append a private continuation. Cache-compatible children reuse
the exact trunk prefix; incompatible providers receive the same semantic summary
entries under a fresh provider-specific head and accept a cold provider cache.

Appending a summary after the complete old transcript is still not compaction.
The covered raw region must leave the active projection while remaining durable
in the external history.''',
        "context-engine decision",
    )

    text = replace_once(
        text,
        "}\n```\n\n`digest` is over the canonical serialized epoch descriptor and its referenced\n",
        '''}
```

The implemented summary segment is:

```text
SummaryEntry = {
  sequence,
  source_sha256,
  source_message_count,
  through_turn,
  objective,
  outcome,
  decisions_added,
  decisions_superseded,
  facts_added,
  facts_invalidated,
  files_and_symbols_changed,
  verification_results,
  relevant_failed_approaches,
  open_items,
  entry_sha256
}
```

Sequence, source digest/count, turn coverage, and the canonical entry digest are
validated before publication. Semantic arrays are bounded and remain user-role
data; they do not acquire system authority.

`digest` is over the canonical serialized epoch descriptor and its referenced
''',
        "context-engine summary state",
    )

    text = replace_section(text,
        "2## C5. A summary is lossy and non-authoritative\n",
        "### C6. Bounded active context\n",
        '''### C5. A summary is lossy, append-only, and non-authoritative

An LLM summary is not assumed deterministic, associative, or commutative. The
published entry is immutable: `S1` must remain byte-identical when `S2` is
appended, and a later flush may summarize only the new raw tail. Publication is
idempotent and fail-closed through sequence, source digest/count, checkpoint
identity, and exclusive immutable file creation. Re-running a failed provider
call may yield different proposed text, but no invalid or duplicate proposal may
advance the trunk.''',
        "context-engine C5",
    )

    text = replace_section(
        text,
        "### 5.5 Compaction\n",
        "## 6. Provider cache capability contract\n",
        '''### 5.5 Append-only semantic-summary flush

A flush runs only between completed provider/tool turns:

1. Freeze the current raw working tail. Existing summary entries are not part of
   the source range.
2. Compute its canonical source digest, message count, next sequence number, and
   covered turn.
3. Make one additional provider call containing the immutable trunk, the raw
   tail, and a delimited summary-control request.
4. Account for this call exactly like every other provider call: usage, request
   debt, latency, cache evidence, token budget, cost, cancellation, and wall
   deadline all apply.
5. Parse and validate the strict `SummaryEntry`: exact sequence and source
   metadata, bounded semantic fields, canonical content digest, and no unknown
   fields.
6. Append the entry as one user-role trunk message, clear the covered raw tail,
   write a new immutable checkpoint, and publish the epoch transition only after
   durable creation succeeds.
7. Leave every prior summary message byte-stable. The next summary request begins
   with the complete existing trunk but summarizes only its newly supplied raw
   source block.
8. Retain the full raw history externally for replay, audit, recovery, and future
   re-projection.

Cambium forces a flush at delegation and terminal boundaries and performs a
threshold flush when the raw tail crosses the configured high-water mark. A
legacy transcript-heavy checkpoint is migrated by summarizing its unsummarized
continuation at the next flush; it is not recursively compacted.

If summary generation, validation, redaction, checkpoint creation, or publication
fails, the active checkpoint and raw tail remain unchanged and the task fails
closed. No earlier summary is rewritten to recover space. Future hierarchical
summary tiers, if needed, require a new explicit projection type rather than
silently summarizing summaries.

The economic decision remains an online optimal-stopping problem:

```text
summary_call + expected_cache_rebuild
    < expected_remaining_calls * per_call_context_saving + quality_benefit
```

Cambium currently uses configured thresholds and semantic boundaries; workload-
specific adaptive policies remain future work.''',
        "context-engine flush protocol",
    )

    text = replace_section(
        text,
        "## 9. Current implementation map (2026-08-20)\n",
        "## 10. Verification\n",
        '''## 9. Current implementation map (2026-08-21)

Verified in source and the full Python 3.14 test tiers:

- `src/cambium/summary_trunk.py` defines strict immutable `SummaryEntry`
  parsing, canonical serialization, source binding, sequence validation, and
  digest verification;
- provider-backed root agents enter trunk mode immediately when durable epoch
  storage is available;
- threshold, delegation, and terminal boundaries perform explicit additional
  summary calls, and those calls participate in normal usage and request-debt
  accounting;
- each raw range is summarized once; tests prove the existing head and `S1`
  remain byte-stable when later entries are appended;
- the active request is the immutable head and semantic trunk plus a bounded raw
  tail, not the old transcript followed by its summary;
- exact provider/model/protocol/tool-compatible children reuse the byte-identical
  trunk prefix, while incompatible providers reuse the semantic entries under a
  fresh head;
- legacy transcript checkpoints migrate on their next flush rather than being
  recursively folded;
- raw events, ordinary checkpoints, and epoch artifacts remain available for
  audit and replay outside the active prompt;
- redacted/corrupt/mismatched checkpoints and invalid summary responses fail
  closed before they can seed or advance a prompt.

Open deltas:

- REPL/TUI prompts still create separate one-shot session leaves instead of
  continuing one durable interactive branch;
- provider cache capability, namespace, TTL, granularity, isolation, and
  read/write pricing are not modeled as one typed contract;
- `prompt_prefix_bytes` is not yet a canonical digest/size of the complete
  provider request identity;
- repeated real-provider cache and held-out quality canaries are still required
  before claiming economic or quality gains across workloads;
- strict model pinning, rate/concurrency modeling, and cache-aware cost routing
  retain gaps described in `provider-routing.md`;
- the implemented trunk is flat append-only semantic segmentation; any future
  long-horizon hierarchy must introduce an explicit new projection and preserve
  the current non-recursive invariant.''',
        "context-engine implementation map",
    )

    path.write_text(text, encoding="utf-8")


def patch_cache_research() -> None:
    path = ROOT / "docs" / "research" / "cache-first-context-reuse-plan.md"
    text = path.read_text(encoding="utf-8")
    text = replace_reviewed_line(
        text,
        "**Reviewed:** 2026-08-21 against current `main` and the verified append-only\n"
        "summary-trunk implementation.",
        "cache research review date",
    )
    text = replace_section(
        text,
        "## 2. What is already implemented\n",
        "## 3. Corrections to the earlier plan\n",
        '''## 2. What is now implemented

The earlier prototype has been replaced by an append-only semantic trunk:

- `SummaryEntry` binds every segment to an exact raw source digest, message
  count, sequence number, covered turn, bounded semantic fields, and canonical
  entry digest;
- the active provider request is `stable head + S1..Sn + raw tail`;
- threshold, delegation, and terminal boundaries make an additional provider
  summary call, validate it, append one immutable entry, and clear only the
  covered raw tail;
- earlier summaries are never summarized again or rewritten; tests assert exact
  prefix and `S1` byte stability when `S2` is appended;
- compatible children reuse the exact trunk prefix, while incompatible
  providers receive the same semantic entries under a fresh provider-specific
  head;
- legacy transcript-heavy checkpoints migrate at their next summary boundary;
- raw events, ordinary turn checkpoints, and immutable epoch artifacts remain
  the external audit/recovery record;
- summary calls participate in token, request-debt, latency, cache, cancellation,
  wall-clock, and cost accounting;
- invalid summaries, redacted/corrupt checkpoints, and publication failures fail
  closed without advancing the trunk.

This establishes the mechanism and cold-path correctness. It does not by itself
prove provider cache retention, cost savings, or task-quality improvement; those
remain empirical questions under the verification protocol below.''',
        "cache research implementation section",
    )
    path.write_text(text, encoding="utf-8")


def patch_rolling_research() -> None:
    path = ROOT / "docs" / "research" / "rolling-context-and-agent-reuse.md"
    text = path.read_text(encoding="utf-8")
    text = replace_reviewed_line(
        text,
        "**Reviewed:** 2026-08-21 against current `main` and the verified append-only\n"
        "summary-trunk implementation.",
        "rolling research review date",
    )
    text = replace_section(
        text,
        "### 2.6 Roll the epoch\n",
        "## 3. Recursion must be typed and bounded\n",
        '''### 2.6 Append one semantic segment

At a configured threshold or a delegation/terminal boundary:

1. freeze only the current raw working tail;
2. compute its digest, message count, next sequence, and covered turn;
3. make one additional provider call over the immutable trunk plus that tail;
4. validate the strict semantic response and canonical entry digest;
5. append `Sn+1` as one immutable user-role message and clear the covered tail;
6. durably publish the new epoch, leaving `H + S1..Sn` byte-identical;
7. retain the complete raw history outside the active projection.

The next turn starts from `H + S1..Sn+1 + new raw tail`. A later flush receives
all earlier segments as context but may summarize only its explicitly supplied
new raw source. This prevents recursive information decay and preserves exact
prefix-cache locality. If the flat trunk eventually becomes too large, Cambium
must introduce a separately typed hierarchical projection rather than silently
summarizing summaries.''',
        "rolling research append lifecycle",
    )
    path.write_text(text, encoding="utf-8")


def patch_research_index() -> None:
    path = ROOT / "docs" / "research" / "README.md"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "- [`../architecture/context-engine.md`](../architecture/context-engine.md) —\n"
        "  immutable epochs, branching, compaction, and cache accounting.\n",
        "- [`../architecture/context-engine.md`](../architecture/context-engine.md) —\n"
        "  immutable epochs, append-only semantic summaries, branching, and cache\n"
        "  accounting.\n",
        "research index context description",
    )

    text = replace_once(
        text,
        "- [`cache-first-context-reuse-plan.md`(cache-first-context-reuse-plan.md) —\n"
        "  corrected hypothesis, provider-cache boundary, measurement protocol, and\n"
        "  remaining gaps.\n"
        "- [`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) —\n"
        "  corrected rolling/fork/merge model, bounded recursion, and evaluation plan.\n",
        "- [`cache-first-context-reuse-plan.md`](cache-first-context-reuse-plan.md) —\n"
        "  corrected hypothesis, implemented append-only trunk, provider-cache\n"
        "  boundary, measurement protocol, and remaining gaps.\n"
        "- [`rolling-context-and-agent-reuse.md`](rolling-context-and-agent-reuse.md) —\n"
        "  implemented non-recursive segment lifecycle, corrected fork/merge model,\n"
        "  bounded recursion, and evaluation plan.\n",
        "research index cache descriptions",
    )

    path.write_text(text, encoding="utf-8")


def main() -> None:
    patch_context_engine()
    patch_cache_research()
    patch_rolling_research()
    patch_research_index()


if __name__ == "__main__":
    main()
