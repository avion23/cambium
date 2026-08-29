"""OAuth and DSPy operator-path regressions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cambium import oauth
from cambium.modules.base import Example
from cambium.oauth import OAuthDoc, OAuthStore, RefreshedTokens, TokenManager
from cambium.provider_config import CODEX_CHATGPT_PROFILE


def test_empty_oauth_client_override_uses_pinned_public_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = OAuthStore(tmp_path / "oauth.json")
    store.save_provider(
        OAuthDoc(
            provider="codex",
            access_token="expired-access",
            refresh_token="refresh-token",
            expires_at=time.time() - 10,
            account_id="account",
        )
    )
    observed: dict[str, str] = {}

    def refresh(
        issuer: str,
        client_id: str,
        refresh_token: str,
        timeout_s: float,
    ) -> RefreshedTokens:
        del issuer, refresh_token, timeout_s
        observed["client_id"] = client_id
        return RefreshedTokens(access_token="fresh-access", expires_in=3600)

    monkeypatch.setattr(oauth, "refresh_access_token", refresh)
    token, account = TokenManager("codex", store, client_id="").ensure_fresh()

    assert token == "fresh-access"
    assert account == "account"
    assert observed["client_id"] == CODEX_CHATGPT_PROFILE["client_id"]


class _CandidateLoader:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.datasets_dir = self.path if self.path.is_dir() else self.path.parent

    def load(self) -> list[Example]:
        records = [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [
            Example(
                input=record["input"],
                expected=record["expected"],
                canary=bool(record.get("canary", False)),
            )
            for record in records
        ]


def _candidate(identifier: str, status: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "candidate": True,
        "review_status": status,
        "redacted": True,
        "input": {"task": identifier, "context": ""},
        "expected": {"decompose": False, "reason": "reviewed"},
    }


def test_transcript_candidates_fail_closed_while_review_is_pending(
    tmp_path: Path,
) -> None:
    # Deferred: optimize pulls in dspy (~2s import) and only these two tests
    # need it; keeping it out of collection time for the other cases.
    from cambium import optimize

    path = tmp_path / "transcript_candidates.jsonl"
    path.write_text(json.dumps(_candidate("pending", "needs_review")) + "\n")
    loader = _CandidateLoader(tmp_path)

    with pytest.raises(optimize.OptimizeError, match="still need review"):
        optimize._load_transcript_candidates(loader, path)


def test_transcript_candidates_load_only_approved_records(tmp_path: Path) -> None:
    path = tmp_path / "transcript_candidates.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_candidate("approved", "approved")),
                json.dumps(_candidate("rejected", "rejected")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    loader = _CandidateLoader(tmp_path)

    from cambium import optimize

    records = optimize._load_transcript_candidates(loader, path)

    assert len(records) == 1
    assert records[0].input["task"] == "approved"
