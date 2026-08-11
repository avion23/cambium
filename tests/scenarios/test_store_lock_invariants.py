"""Canary: EventStore and ConversationStore locks are load-bearing (Claim 7).

The stores' admission/close locks protect two invariants that look strippable
on a fast single-threaded path but are not: sequence uniqueness under
concurrency, and the atomicity of close() vs. a concurrent append.

With ``N`` threads hammering appends while the main thread calls ``close()``:

  - every append returns an int (a sequence number) or raises a *defined*
    error (``StoreError``/``StoreTimeout``/``RuntimeError`` for EventStore,
    ``ConversationStoreError``/``RuntimeError`` for ConversationStore); no
    append hangs and no thread crashes the process.
  - the returned sequence numbers are pairwise distinct (the admission lock
    keeps the reserved-counter increment race-free).
  - every acknowledged append is present after a clean reopen, or is counted
    by the store's ``dropped`` counter (non-critical overflow/eviction).
  - acknowledged *critical* appends are durable: their row is present after
    reopen (an ack is only issued after the writer's fsync barrier).

These must PASS on current main: they pin the lock-protected behavior so a
future "optimization" that strips a lock fails loudly instead of silently
corrupting the sequence space or hanging a close.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from cambium.conversations import ConversationStore, ConversationStoreError
from cambium.store import EventStore, StoreError, StoreTimeout


def _hammer(
    append: Any,
    *,
    threads: int,
    per_thread: int,
    delay_s: float,
    close: Any,
    defined_errors: tuple[type[BaseException], ...],
) -> list[list[Any]]:
    """Run ``threads`` append workers and call ``close()`` mid-flight.

    Returns one result list per thread. A result is either the int sequence
    returned by append, ``None`` (a dropped non-critical EventStore append),
    or the exception raised by append. The main thread calls ``close()`` after
    ``delay_s`` so admission and shutdown race in every run.
    """
    per_thread_results: list[list[Any]] = [None] * threads  # type: ignore[list-item]
    barrier = threading.Barrier(threads + 1)

    def worker(index: int) -> None:
        local: list[Any] = []
        barrier.wait()
        for i in range(per_thread):
            try:
                local.append(append(index, i))
            except defined_errors as exc:
                local.append(exc)
        per_thread_results[index] = local

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    barrier.wait()
    time.sleep(delay_s)
    close()
    for worker_thread in workers:
        worker_thread.join(timeout=60)
    assert all(not worker_thread.is_alive() for worker_thread in workers), (
        "an append hung while close() was running"
    )
    return per_thread_results


def _flatten(results: list[list[Any]]) -> list[Any]:
    return [item for local in results for item in local]


def _assert_defined_outcomes(
    outcomes: list[Any], defined_errors: tuple[type[BaseException], ...]
) -> None:
    for outcome in outcomes:
        assert outcome is None or isinstance(outcome, int) or isinstance(
            outcome, defined_errors
        ), f"append returned an undefined outcome: {outcome!r}"


def _close_event_store(store: EventStore) -> None:
    """Close, tolerating only the store's designed close-deadline timeout.

    A ``StoreTimeout`` close force-stops the writer and counts every
    not-yet-written item in ``store.dropped``, so the accounting invariants
    below still hold under load. Any other error (e.g. a dead writer) is a
    real defect and propagates.
    """
    try:
        store.close()
    except StoreTimeout:
        store._thread.join(timeout=10)


def _reopen_event_store(db):
    return EventStore(db, fsync_interval_s=0.1)


def _event_append(store: EventStore, index: int, i: int, critical_at: int) -> int | None:
    # Mostly non-critical (fast admission) with a single critical tail per
    # thread so the fsync-acknowledged path is exercised under the close race
    # without multiplying fsync work. The total critical volume stays small
    # enough for the writer to drain within the store's close deadline.
    kind = "result" if i == critical_at else "log"
    return store.append({"kind": kind, "payload": {"thread": index, "i": i}})


@pytest.mark.slow
def test_event_store_close_race_preserves_seq_uniqueness_and_durability(tmp_path) -> None:
    db = tmp_path / "events.db"
    store = EventStore(db, fsync_interval_s=0.1)
    try:
        results = _hammer(
            lambda index, i: _event_append(store, index, i, critical_at=2),
            threads=200,
            per_thread=3,
            delay_s=0.02,
            close=lambda: _close_event_store(store),
            defined_errors=(StoreError, StoreTimeout, RuntimeError),
        )
    finally:
        if not store._closed:  # pragma: no cover - only when the hammer failed
            _close_event_store(store)

    outcomes = _flatten(results)
    _assert_defined_outcomes(outcomes, (StoreError, StoreTimeout, RuntimeError))
    seqs = [value for value in outcomes if isinstance(value, int)]
    assert seqs, "every append was rejected by close() before any was acknowledged"
    assert len(seqs) == len(set(seqs)), "sequence numbers are not pairwise distinct"

    reopened = _reopen_event_store(db)
    try:
        present = {row["seq"] for row in reopened.events_after(0)}
    finally:
        reopened.close()

    missing = set(seqs) - present
    assert len(missing) <= store.dropped, (
        f"{len(missing)} acknowledged append(s) are neither present after reopen "
        f"nor counted by store.dropped={store.dropped}"
    )


@pytest.mark.slow
def test_event_store_overflow_drops_are_accounted(tmp_path) -> None:
    db = tmp_path / "events.db"
    store = EventStore(db, fsync_interval_s=0.05, max_queue_size=8, critical_timeout_s=5.0)
    try:
        results = _hammer(
            lambda index, i: store.append({"kind": "log", "payload": {"thread": index, "i": i}}),
            threads=200,
            per_thread=10,
            delay_s=0.02,
            close=lambda: _close_event_store(store),
            defined_errors=(StoreError, StoreTimeout, RuntimeError),
        )
    finally:
        if not store._closed:  # pragma: no cover - only when the hammer failed
            _close_event_store(store)

    outcomes = _flatten(results)
    _assert_defined_outcomes(outcomes, (StoreError, StoreTimeout, RuntimeError))
    seqs = [value for value in outcomes if isinstance(value, int)]
    assert len(seqs) == len(set(seqs))

    reopened = _reopen_event_store(db)
    try:
        present = {row["seq"] for row in reopened.events_after(0)}
    finally:
        reopened.close()

    missing = set(seqs) - present
    assert len(missing) <= store.dropped, (
        f"overflow accounting lost {len(missing)} acknowledged append(s) "
        f"(store.dropped={store.dropped})"
    )


def _conversation_append(store: ConversationStore, node_index: int, i: int) -> int:
    return store.append(f"node-{node_index}", "user", f"message {i}")


@pytest.mark.slow
def test_conversation_store_close_race_preserves_rowid_uniqueness(tmp_path) -> None:
    db = tmp_path / "conversations.db"
    store = ConversationStore(db, fsync_interval_s=0.05)
    try:
        results = _hammer(
            lambda index, i: _conversation_append(store, index, i),
            threads=200,
            per_thread=5,
            delay_s=0.02,
            close=store.close,
            defined_errors=(ConversationStoreError, RuntimeError),
        )
    finally:
        if not store._closed:  # pragma: no cover - only when the hammer failed
            store.close()

    outcomes = _flatten(results)
    _assert_defined_outcomes(outcomes, (ConversationStoreError, RuntimeError))
    row_ids = [value for value in outcomes if isinstance(value, int)]
    assert row_ids, "every append was rejected by close() before any was acknowledged"
    assert len(row_ids) == len(set(row_ids)), "row ids are not pairwise distinct"

    reopened = ConversationStore(db, fsync_interval_s=0.05)
    try:
        acked_by_node: dict[str, set[int]] = {}
        for node_index, local in enumerate(results):
            acked_by_node[f"node-{node_index}"] = {
                value for value in local if isinstance(value, int)
            }
        for node_id, acked in acked_by_node.items():
            present = {row["id"] for row in reopened.history(node_id)}
            assert acked <= present, (
                f"{len(acked - present)} acknowledged conversation row(s) missing "
                f"after reopen for {node_id}"
            )
    finally:
        reopened.close()
