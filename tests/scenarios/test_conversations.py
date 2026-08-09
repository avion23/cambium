"""Scenario tests for the branchable SQLite WAL conversation store."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys

from cambium.conversations import ConversationStore


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
        assert columns == ["id", "node_id", "parent_id", "turn", "role", "content", "ts", "seq"]
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
        await asyncio.gather(*(
            asyncio.to_thread(store.append, "node", "assistant", str(index))
            for index in range(100)
        ))

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


def test_crash_durability_reopen_keeps_committed_rows(tmp_path) -> None:
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
