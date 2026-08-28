from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_EVENTS_SCHEMA = """CREATE TABLE events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    ts           TEXT,
    monotonic_ms INTEGER,
    task_id      TEXT,
    worker_id    TEXT,
    generation   INTEGER,
    request_id   TEXT
)"""


def _fixture_session(tmp_path: Path) -> tuple[Path, Path]:
    session = tmp_path / "session"
    turn_events = (
        (
            session / "turn-0001" / "events.db",
            [
                {
                    "provider": "priced",
                    "usage": {
                        "prompt_tokens": 1_000,
                        "cached_tokens": 250,
                        "completion_tokens": 100,
                    },
                },
                {
                    "provider": "priced",
                    "usage": {"prompt_tokens": 200, "cached_tokens": 100, "completion_tokens": 50},
                },
                {"status": "succeeded"},
            ],
        ),
        (
            session / "turn-0002" / "events.db",
            [
                {
                    "provider": "unpriced",
                    "usage": {"prompt_tokens": 100, "cached_tokens": 50, "completion_tokens": 25},
                },
                {"status": "succeeded"},
            ],
        ),
    )
    for database, events in turn_events:
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.execute(_EVENTS_SCHEMA)
            for sequence, payload in enumerate(events, 1):
                kind = "result" if sequence == len(events) else "usage_event"
                connection.execute(
                    "INSERT INTO events(seq, kind, payload) VALUES (?, ?, ?)",
                    (sequence, kind, json.dumps(payload)),
                )
    provider_config = tmp_path / "providers.json"
    provider_config.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "priced",
                        "tier": "fast",
                        "base_url": "https://example.invalid/v1",
                        "api_key_env": "CAMBIUM_PROVIDER_PRICED_API_KEY",
                        "api_key": "test-provider-secret-123456",
                        "model": "test-model",
                        "price_per_1m_in": 2.0,
                        "price_per_1m_cached_in": 0.5,
                        "price_per_1m_out": 4.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return session, provider_config


@pytest.mark.xdist_group("cache_eval")
def test_cache_eval_reports_fixture_metrics(tmp_path: Path) -> None:
    session, provider_config = _fixture_session(tmp_path)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "cache_eval.py"),
        "--provider-config",
        str(provider_config),
        str(session),
    ]
    json_result = subprocess.run(
        [*command[:4], "--json", *command[4:]],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(json_result.stdout)
    priced = report["providers"]["priced"]
    assert priced["prompt_tokens"] == 1_200
    assert priced["cached_tokens"] == 350
    assert priced["cache_hit_percent"] == pytest.approx(350 / 1_200 * 100)
    assert priced["output_tokens"] == 150
    assert priced["calls"] == 2
    assert priced["estimated_cost"] == pytest.approx(0.002475)
    assert priced["estimated_cost_usd"] == pytest.approx(0.002475)
    assert report["providers"]["unpriced"]["estimated_cost"] == "subscription"
    assert report["sessions"][0]["estimated_cost"] == "subscription"

    text_result = subprocess.run(
        command,
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
        check=True,
        capture_output=True,
        text=True,
    )
    assert "cache-hit %" in text_result.stdout
    assert "priced | 1,200 | 350 | 29.2% | 150 | 2 | $0.002475" in text_result.stdout
    assert "unpriced | 100 | 50 | 50.0% | 25 | 1 | subscription" in text_result.stdout
