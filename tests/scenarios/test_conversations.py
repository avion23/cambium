"""Scenario tests for the branchable SQLite WAL conversation store."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import cambium.conversations as conversations
from cambium.conversations import ConversationStore, ConversationStoreError


def _open(path) -> ConversationStore:
    return ConversationStore(path, fsync_interval_s=60.0)


def test_append_read_roundtrip_and_wal_schema(tmp_path) -> None:
    path = tmp_path / "conversations.db"
    store = _open(path)
    try:
        first = store.append("node-a", "user", "hello")
        second = store.append("node-a", "assistant", "world")

        records = store.history("node-a")
        assert [record["id"] for record in records] == [first, second]
        assert [record["content"] for record in records] == ["hello", "world"]
        assert [record["turn"] for record in records] == [1, 2]
        assert [record["seq"] for record in records] == [1, 2]
        assert all(record["ts"] for record in records)
    finally:
        store.close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        columns = [row[1] for row in conn.execute("PRAGMA table_info(conversations)")]
        assert columns == [
            "id",
            "node_id",
            "parent_id",
            "turn",
            "role",
            "content",
            "ts",
            "seq",
            "tokens",
            "kind",
            "meta",
        ]
        indexes = conn.execute("PRAGMA index_list(conversations)").fetchall()
        assert any("node_id" in row[1] for row in indexes)
    finally:
        conn.close()


def test_parent_chain_order_and_path(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")
    try:
        root = store.append("root", "user", "root")
        child = store.append("child", "assistant", "child", parent_id=root)
        leaf = store.append("child", "tool", "leaf", parent_id=child)

        assert [record["id"] for record in store.history("child")] == [root, child, leaf]
        assert [record["turn"] for record in store.path("child", child)] == [1, 2]
        assert store.path("child", leaf) == store.history("child")
    finally:
        store.close()


def test_branch_reuses_prefix_without_copying_it(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")
    try:
        a1 = store.append("A", "user", "A1")
        a2 = store.append("A", "assistant", "A2", parent_id=a1)
        branch_root = store.branch("B", a1)

        b_history = store.history("B")
        assert [record["id"] for record in b_history] == [a1, branch_root]
        assert b_history[-1]["node_id"] == "B"
        assert b_history[-1]["parent_id"] == a1
        assert [record["id"] for record in store.history("A")] == [a1, a2]

        b_message = store.append("B", "assistant", "B", parent_id=branch_root)
        assert [record["content"] for record in store.history("B")] == ["A1", "", "B"]
        assert store.path("B", b_message)[-1]["id"] == b_message
    finally:
        store.close()


def test_history_tail_limits_context_rows(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")
    try:
        for index in range(5):
            store.append("node", "assistant", str(index))

        assert [record["content"] for record in store.history("node", tail=2)] == ["3", "4"]
        assert store.history("node", tail=0) == []
    finally:
        store.close()


def test_concurrent_async_appends_are_serialized(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")

    async def append_many() -> None:
        await asyncio.gather(
            *(
                asyncio.to_thread(store.append, "node", "assistant", str(index))
                for index in range(100)
            )
        )

    try:
        asyncio.run(append_many())
        records = store.history("node")
        assert len(records) == 100
        assert {record["content"] for record in records} == {str(index) for index in range(100)}
        assert [record["turn"] for record in records] == list(range(1, 101))
        assert len({record["id"] for record in records}) == 100
    finally:
        store.close()


def test_close_drains_and_reopen_reads_all_rows(tmp_path) -> None:
    path = tmp_path / "conversations.db"
    store = _open(path)
    for index in range(100):
        store.append("node", "assistant", str(index))
    store.close()

    reopened = _open(path)
    try:
        assert len(reopened.history("node")) == 100
        assert reopened.history("node")[-1]["content"] == "99"
    finally:
        reopened.close()


@pytest.mark.slow
def test_crash_durability_reopen_keeps_committed_rows(tmp_path) -> None:
    # Crash durability is a genuine process-boundary property: the writer
    # subprocess calls os._exit(9) mid-append, which in-process cannot show.
    path = tmp_path / "crash" / "conversations.db"
    count = 50
    script = (
        "import os, sys\n"
        "from cambium.conversations import ConversationStore\n"
        "store = ConversationStore(sys.argv[1], fsync_interval_s=60.0)\n"
        f"for i in range({count}):\n"
        "    store.append('node', 'assistant', str(i))\n"
        "os._exit(9)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 9, result.stderr

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == count
    finally:
        conn.close()

    store = _open(path)
    try:
        assert len(store.history("node")) == count
    finally:
        store.close()


def test_v1_schema_migrates_and_preserves_existing_rows(tmp_path) -> None:
    path = tmp_path / "conversations.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                node_id TEXT NOT NULL,
                parent_id INTEGER NULL REFERENCES conversations(id),
                turn INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                ts TEXT NOT NULL,
                seq INTEGER NOT NULL
            );
            PRAGMA user_version = 1;
            """
        )
        conn.execute(
            "INSERT INTO conversations"
            "(id, node_id, parent_id, turn, role, content, ts, seq)"
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (7, "legacy", None, 1, "user", "old", "2026-01-01T00:00:00+00:00", 1),
        )
        conn.commit()
    finally:
        conn.close()

    store = _open(path)
    try:
        record = store.history("legacy")[0]
        assert record["id"] == 7
        assert record["content"] == "old"
        assert record["tokens"] is None
        assert record["kind"] == "turn"
        assert record["meta"] is None
    finally:
        store.close()

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        columns = [row[1] for row in conn.execute("PRAGMA table_info(conversations)")]
        assert columns[-3:] == ["tokens", "kind", "meta"]
    finally:
        conn.close()


def test_tokens_metadata_and_kind_filtering(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")
    try:
        store.append("node", "user", "turn", tokens=12)
        store.append("node", "system", "system", tokens=4, kind="system", meta={"source": "test"})

        records = store.history("node")
        assert records[-1]["tokens"] == 4
        assert records[-1]["kind"] == "system"
        assert records[-1]["meta"] == {"source": "test"}
        assert [record["content"] for record in records if record["kind"] == "turn"] == ["turn"]
    finally:
        store.close()


def test_summary_and_token_accounting(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")
    try:
        first = store.append("node", "user", "first", tokens=100)
        second = store.append("node", "assistant", "second", tokens=80)
        summary = store.add_summary(
            "node",
            "condensed",
            covers_from=first,
            covers_to=second,
            tokens_before=180,
            tokens_after=25,
        )

        record = store.history("node")[-1]
        assert record["id"] == summary
        assert record["parent_id"] == second
        assert record["kind"] == "summary"
        assert record["tokens"] == 25
        assert record["meta"] == {
            "covers_from": first,
            "covers_to": second,
            "tokens_before": 180,
            "tokens_after": 25,
        }

        assert store.token_accounting("node") == {
            "tokens_by_kind": {"turn": 180, "summary": 25, "system": 0},
            "reduction": 155,
            "covered_range": {"from": first, "to": second},
        }

        following = store.append("node", "assistant", "following", tokens=8)
        assert store.history("node")[-1]["parent_id"] == summary
        assert [record["id"] for record in store.path("node", summary)] == [
            first,
            second,
            summary,
        ]
        assert store.history("node")[-1]["id"] == following
    finally:
        store.close()


def test_deep_chain_reads_in_root_to_head_order(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")
    try:
        ids = [store.append("node", "assistant", str(index), tokens=1) for index in range(1500)]

        history = store.history("node")
        path = store.path("node", ids[-1])
        accounting = store.token_accounting("node")

        assert [record["id"] for record in history] == ids
        assert [record["id"] for record in path] == ids
        assert len(history) == len(path) == 1500
        assert accounting["tokens_by_kind"] == {"turn": 1500, "summary": 0, "system": 0}
    finally:
        store.close()


def test_chain_reports_cycle_created_outside_store(tmp_path, monkeypatch) -> None:
    path = tmp_path / "conversations.db"
    # The recursive chain walk is bounded by _MAX_CHAIN_DEPTH (1M by default);
    # a cycle walks the full bound before being reported. 100 levels (same
    # residue class mod the 3-node cycle, so the reported id is unchanged)
    # proves the cycle detection without the 1M-step walk.
    monkeypatch.setattr(conversations, "_MAX_CHAIN_DEPTH", 100)
    store = _open(path)
    try:
        root = store.append("node", "user", "root")
        store.append("node", "assistant", "middle")
        leaf = store.append("node", "assistant", "leaf")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE conversations SET parent_id = ? WHERE id = ?", (leaf, root))

        with pytest.raises(
            ConversationStoreError,
            match=rf"^cycle in conversation parent chain at id {root}$",
        ):
            store.history("node")
    finally:
        store.close()


def test_chain_reports_missing_parent_created_outside_store(tmp_path) -> None:
    path = tmp_path / "conversations.db"
    store = _open(path)
    try:
        root = store.append("node", "user", "root")
        store.append("node", "assistant", "middle")
        store.append("node", "assistant", "leaf")
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM conversations WHERE id = ?", (root,))

        with pytest.raises(
            ConversationStoreError,
            match=rf"^conversation parent id does not exist: {root}$",
        ):
            store.history("node")
    finally:
        store.close()


def test_path_rejects_stop_id_outside_active_chain(tmp_path) -> None:
    store = _open(tmp_path / "conversations.db")
    try:
        head = store.append("node", "user", "root")
        outside = store.append("other", "user", "other")

        with pytest.raises(
            ValueError,
            match=rf"^conversation id is not on node {head}'s path: {outside}$",
        ):
            store.path("node", outside)
    finally:
        store.close()


def test_close_propagates_final_fsync_failure(tmp_path, monkeypatch) -> None:
    store = _open(tmp_path / "conversations.db")
    store.append("node", "user", "message")

    def fail_fsync(self) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(ConversationStore, "_fsync_now", fail_fsync)
    with pytest.raises(
        ConversationStoreError,
        match="conversation store failed while closing",
    ) as exc:
        store.close()
    assert isinstance(exc.value.__cause__, OSError)


def test_submit_times_out_when_writer_stalls(tmp_path, monkeypatch) -> None:
    store = _open(tmp_path / "conversations.db")
    writer_stalled = threading.Event()
    release_writer = threading.Event()
    original_insert = store._insert_row

    def stalled_insert(*args, **kwargs) -> int:
        writer_stalled.set()
        release_writer.wait(5.0)
        return original_insert(*args, **kwargs)

    monkeypatch.setattr(store, "_insert_row", stalled_insert)
    monkeypatch.setattr(conversations, "_WRITE_TIMEOUT_S", 0.05)
    try:
        start = time.monotonic()
        with pytest.raises(
            ConversationStoreError,
            match=r"^conversation write did not complete within 0\.05s$",
        ):
            store.append("node", "user", "message")
        elapsed = time.monotonic() - start
        assert writer_stalled.is_set()
        assert 0.04 <= elapsed < 1.0
    finally:
        release_writer.set()
        store.close()
