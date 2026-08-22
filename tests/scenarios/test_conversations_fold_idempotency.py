from __future__ import annotations

import sqlite3
import threading

import pytest

from cambium.conversations import (
    ConversationStore,
    ConversationStoreError,
    ConversationStoreInitError,
)


def test_fold_twice_does_not_change_the_store(tmp_path) -> None:
    path = tmp_path / "conversations.db"
    store = ConversationStore(path, fsync_interval_s=60.0)
    try:
        first = store.append("node", "user", "first", tokens=4)
        second = store.append("node", "assistant", "second", tokens=3)

        summary = store.fold(
            "node",
            "condensed",
            tokens_before=7,
            tokens_after=2,
            state_version=1,
            folded_from=first,
        )
        before = store.history("node")
        count_before = sqlite3.connect(path).execute(
            "SELECT count(*) FROM conversations"
        ).fetchone()[0]

        assert summary is not None
        assert store.fold("node", "condensed", tokens_before=7, tokens_after=2) == summary
        assert store.history("node") == before
        assert sqlite3.connect(path).execute(
            "SELECT count(*) FROM conversations"
        ).fetchone()[0] == count_before
        assert [record["id"] for record in store.raw_range("node", first, second)] == [
            first,
            second,
        ]
    finally:
        store.close()


def test_fold_race_keeps_an_append_on_the_active_path(tmp_path, monkeypatch) -> None:
    store = ConversationStore(tmp_path / "conversations.db", fsync_interval_s=60.0)
    ready = threading.Event()
    release = threading.Event()
    original_history = store.history
    fold_ids: list[int | None] = []
    errors: list[BaseException] = []

    def delayed_history(node_id: str, **kwargs):
        records = original_history(node_id, **kwargs)
        if node_id == "node" and not ready.is_set():
            ready.set()
            release.wait(5.0)
        return records

    monkeypatch.setattr(store, "history", delayed_history)
    try:
        first = store.append("node", "user", "first")

        def fold_one() -> None:
            try:
                fold_ids.append(store.fold("node", "folded", tokens_before=1, tokens_after=1))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=fold_one)
        thread.start()
        assert ready.wait(5.0)
        concurrent = store.append("node", "assistant", "concurrent")
        release.set()
        thread.join(5.0)

        assert not thread.is_alive()
        assert errors == []
        assert len(fold_ids) == 1
        assert fold_ids[0] is not None
        records = original_history("node")
        assert [record["id"] for record in records] == [first, concurrent, fold_ids[0]]
    finally:
        release.set()
        store.close()


def test_add_summary_requires_active_path_and_current_head(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations.db", fsync_interval_s=60.0)
    try:
        outside = store.append("other", "user", "outside")
        first = store.append("node", "user", "first")
        head = store.append("node", "assistant", "head")

        with pytest.raises(ValueError, match="covers_from.*active path"):
            store.add_summary(
                "node",
                "bad path",
                covers_from=outside,
                covers_to=head,
                tokens_before=2,
                tokens_after=1,
            )
        with pytest.raises(ValueError, match="current head"):
            store.add_summary(
                "node",
                "stale head",
                covers_from=first,
                covers_to=first,
                tokens_before=2,
                tokens_after=1,
            )
    finally:
        store.close()


def test_two_writers_preserve_complete_records_and_one_fold(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations.db", fsync_interval_s=60.0)
    barrier = threading.Barrier(2)
    contents = [f"writer-{index}-" + "x" * 10000 for index in range(2)]
    append_ids: list[int] = []
    fold_ids: list[int] = []
    errors: list[BaseException] = []

    def append_one(content: str) -> None:
        try:
            barrier.wait()
            append_ids.append(store.append("node", "assistant", content))
        except BaseException as exc:
            errors.append(exc)

    try:
        threads = [threading.Thread(target=append_one, args=(content,)) for content in contents]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(append_ids) == 2
        records = store.history("node")
        assert [record["content"] for record in records] == [
            "writer-0-" + "x" * 10000,
            "writer-1-" + "x" * 10000,
        ] or [record["content"] for record in records] == [
            "writer-1-" + "x" * 10000,
            "writer-0-" + "x" * 10000,
        ]
        assert all(len(record["content"]) == 10009 for record in records)

        covers_to = max(append_ids)
        fold_barrier = threading.Barrier(2)

        def fold_one() -> None:
            try:
                fold_barrier.wait()
                fold_ids.append(
                    store.add_summary(
                        "node",
                        "folded",
                        covers_from=min(append_ids),
                        covers_to=covers_to,
                        tokens_before=2,
                        tokens_after=1,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        fold_threads = [threading.Thread(target=fold_one) for _ in range(2)]
        for thread in fold_threads:
            thread.start()
        for thread in fold_threads:
            thread.join()

        assert errors == []
        assert len(fold_ids) == 2
        assert fold_ids[0] == fold_ids[1]
        assert sum(record["kind"] == "summary" for record in store.history("node")) == 1
    finally:
        store.close()


def test_interrupted_fold_rolls_back_before_reopen(tmp_path, monkeypatch) -> None:
    path = tmp_path / "conversations.db"
    store = ConversationStore(path, fsync_interval_s=60.0)
    first = store.append("node", "user", "before")
    second = store.append("node", "assistant", "after")
    original_insert = store._insert_row

    def interrupted(*args, **kwargs) -> int:
        original_insert(*args, **kwargs)
        raise RuntimeError("fold interrupted")

    monkeypatch.setattr(store, "_insert_row", interrupted)
    try:
        with pytest.raises(ConversationStoreError):
            store.add_summary(
                "node",
                "never committed",
                covers_from=first,
                covers_to=second,
                tokens_before=2,
                tokens_after=1,
            )
    finally:
        with pytest.raises(ConversationStoreError):
            store.close()

    reopened = ConversationStore(path, fsync_interval_s=60.0)
    try:
        assert [record["content"] for record in reopened.history("node")] == [
            "before",
            "after",
        ]
    finally:
        reopened.close()


def test_unicode_and_empty_folds_are_safe(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations.db", fsync_interval_s=60.0)
    try:
        assert store.fold("empty", "ignored") is None
        content = "\u65e5\u672c\u8a9e \U0001f30a \u043f\u0440\u0438\u0432\u0435\u0442"
        store.append("unicode", "user", content)
        summary = store.fold(
            "unicode", "\u8981\u7d04 \U0001f680", tokens_before=3, tokens_after=1
        )

        assert summary is not None
        assert store.fold("unicode", "\u8981\u7d04 \U0001f680") == summary
        assert store.history("unicode")[0]["content"] == content
        assert store.history("unicode")[-1]["content"] == "\u8981\u7d04 \U0001f680"
    finally:
        store.close()


def test_reopen_fails_closed_for_a_missing_fold_reference(tmp_path) -> None:
    path = tmp_path / "conversations.db"
    store = ConversationStore(path, fsync_interval_s=60.0)
    first = store.append("node", "user", "first")
    second = store.append("node", "assistant", "second")
    store.add_summary(
        "node",
        "summary",
        covers_from=first,
        covers_to=second,
        tokens_before=2,
        tokens_after=1,
    )
    store.close()

    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (first,))

    with pytest.raises(ConversationStoreInitError):
        ConversationStore(path, fsync_interval_s=60.0)
