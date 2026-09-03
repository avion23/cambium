"""Shared test-only environment setup."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _test_provider_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give loopback provider scenarios a disposable credential."""
    monkeypatch.setenv(
        "CAMBIUM_PROVIDER_TEST_PROVIDER_API_KEY",
        "test-dummy-key-0123456789",
    )
