from __future__ import annotations

from pathlib import Path

import pytest

from cambium import doctor

CHECKS = (
    ("check_python", "Python version"),
    ("check_uv", "uv"),
    ("check_git", "git"),
    ("check_worktrees", "Worktree hygiene"),
    ("check_event_store", "Event store integrity"),
    ("check_dataset", "Dataset integrity"),
    ("check_secrets", "Secrets hygiene"),
    ("check_provider_env", "Provider env"),
    ("check_auth_metadata", "Auth metadata"),
    ("check_auth_schema", "Auth schema"),
    ("check_auth_coverage", "Auth coverage"),
    ("check_provider_runnable", "Provider runnable"),
    ("check_conversation_store", "Conversation store"),
    ("check_system_health", "System health"),
)


def _forced_failure(*args: object, **kwargs: object) -> tuple[doctor.Status, str]:
    return doctor.Status.FAIL, "forced diagnostic failure"


@pytest.mark.parametrize(("target", "name"), CHECKS)
def test_each_check_failure_has_stable_report_contract(
    target: str, name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        doctor, "check_python", lambda: (doctor.Status.PASS, "3.14.0 (>= 3.14)")
    )
    monkeypatch.setattr(doctor, target, _forced_failure)

    checks = doctor.run_checks(None, tmp_path)

    assert doctor.exit_code(checks) == 1
    assert [check.number for check in checks] == list(range(1, 15))
    report = doctor.format_report(checks)
    lines = report.splitlines()
    assert len(lines) == 16
    assert lines[0] == "cambium doctor — Cambium harness diagnostics"
    assert lines[-1].startswith("Summary:")
    assert f"{name:<22}" in report
    assert "1 fail" in report


def test_system_health_failure_is_advisory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_health(path: Path) -> object:
        raise RuntimeError("health probe failed")

    monkeypatch.setattr(doctor, "health", fail_health)

    status, detail = doctor.check_system_health(tmp_path)
    checks = [
        doctor.Check(1, "required", doctor.Status.PASS, "required pass"),
        doctor.Check(14, "System health", status, detail),
    ]

    assert status is doctor.Status.SKIP
    assert "system health unavailable" in detail
    assert doctor.exit_code(checks) == 0
    report = doctor.format_report(checks)
    assert "System health" in report
    assert "SKIP" in report
    assert "0 fail" in report


@pytest.mark.parametrize("failure", [doctor.urllib.error.URLError("offline"), TimeoutError("slow")])
def test_network_probe_failure_is_bounded_and_reported(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[float | None] = []

    def fail_urlopen(url: str, *, timeout: float | None = None) -> object:
        seen.append(timeout)
        raise failure

    monkeypatch.setattr(doctor.urllib.request, "urlopen", fail_urlopen)

    reachable, detail = doctor._issuer_reachable("https://issuer.example.test", 0.25)

    assert reachable is False
    assert seen == [0.25]
    assert detail.startswith("issuer unreachable:")
    checks = [doctor.Check(15, "OAuth live", doctor.Status.FAIL, detail)]
    assert doctor.exit_code(checks) == 1
    report = doctor.format_report(checks)
    assert report.splitlines()[1].startswith("  15. OAuth live")
    assert report.splitlines()[-1].endswith("1 fail")


def test_format_report_sorts_checks_without_mutating_input() -> None:
    checks = [
        doctor.Check(2, "Second", doctor.Status.WARN, "second"),
        doctor.Check(1, "First", doctor.Status.PASS, "first"),
    ]

    report = doctor.format_report(checks)

    assert [check.number for check in checks] == [2, 1]
    rows = report.splitlines()[1:-1]
    assert rows[0].startswith("   1. First")
    assert rows[1].startswith("   2. Second")


def test_format_report_keeps_credential_names_and_redacts_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credentials = {
        "CAMBIUM_PROVIDER_FAKE_API_KEY": "doctor-api-secret-9f3a",
        "CAMBIUM_OAUTH_ACCESS_CODEX": "doctor-access-secret-7b2c",
        "CUSTOM_REFRESH_TOKEN": "doctor-refresh-secret-5d1e",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    checks = [
        doctor.Check(
            8,
            "Provider env",
            doctor.Status.WARN,
            f"{credentials['CAMBIUM_PROVIDER_FAKE_API_KEY']} "
            "CAMBIUM_PROVIDER_FAKE_API_KEY",
        ),
        doctor.Check(
            15,
            "OAuth live",
            doctor.Status.FAIL,
            "CAMBIUM_OAUTH_ACCESS_CODEX="
            f"{credentials['CAMBIUM_OAUTH_ACCESS_CODEX']} "
            "CUSTOM_REFRESH_TOKEN="
            f"{credentials['CUSTOM_REFRESH_TOKEN']}",
        ),
    ]

    report = doctor.format_report(checks)

    for name, value in credentials.items():
        assert name in report
        assert value not in report
    assert report.count("***") == len(credentials)
