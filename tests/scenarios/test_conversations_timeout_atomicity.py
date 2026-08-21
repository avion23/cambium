"""Scenario coverage for cancelled conversation writes."""

from __future__ import annotations

import threading

import cambium.conversations as conversations
from cambium.conversations import ConversationStore, ConversationStoreError


def test_timed_out_append_is_absent_after_writer_release(tmp_path, monkeypatch) -> None:
    path = tmp_path / "conversations.db"
    store = ConversationStore(path, fsync_interval_s=60.0)
    writer_stalled = threading.Event()
    release_writer = threading.Event()
    original_insert = store._insert_row
    outcome: list[object] = []

    def stalled_insert(*args, **kwargs) -> int:
        writer_stalled.set()
        release_writer.wait(5.0)
        return original_insert(*args, **kwargs)

    def submit_timed_out_append() -> None:
        try:
            outcome.append(store.append("node", "timed", "ghost"))
        except BaseException as exc:
            outcome.append(exc)

    try:
        assert store.append("node", "user", "before") > 0

        monkeypatch.setattr(store, "_insert_row", stalled_insert)
        monkeypatch.setattr(conversations, "_WRITE_TIMEOUT_S", 0.05)

        submitter = threading.Thread(target=submit_timed_out_append)
        submitter.start()
        assert writer_stalled.wait(1.0)
        submitter.join(1.0)
        assert not submitter.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], ConversationStoreError)
        assert str(outcome[0]) == "conversation write did not complete within 0.05s"

        release_writer.set()
        assert store.append("node", "assistant", "after") > 0
        store.close()
    finally:
        release_writer.set()
        if store._thread.is_alive():
            store.close()

    reopened = ConversationStore(path, fsync_interval_s=60.0)
    try:
        assert [record["content"] for record in reopened.history("node")] == [
            "before",
            "after",
        ]
        assert all(record["content"] != "ghost" for record in reopened.history("node"))
    finally:
        reopened.close()
