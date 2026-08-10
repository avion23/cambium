"""Focused security scenarios for the Cambium auth store and launcher."""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from cambium import auth, cli, doctor, supervisor

SECRET = "key-value-must-never-appear-in-diagnostics"


def _store_path(root: Path) -> Path:
    return root / ".local" / "share" / "cambium" / "auth.json"


def _config(path: Path, *, provider: str = "openai", required: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": provider,
                        "tier": "fast",
                        "base_url": "https://api.example.test/v1",
                        "api_key_env": auth.derived_env_name(provider),
                        "required": required,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_fixed_path_uses_effective_uid_home_not_home_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    passwd_home = tmp_path / "passwd-home"
    monkeypatch.setenv("HOME", str(tmp_path / "home-env"))
    monkeypatch.setattr(auth.os, "geteuid", lambda: 4242)
    monkeypatch.setattr(
        auth.pwd,
        "getpwuid",
        lambda uid: SimpleNamespace(pw_dir=str(passwd_home)) if uid == 4242 else None,
    )

    assert auth.auth_store_path() == _store_path(passwd_home)


@pytest.mark.parametrize(
    "document",
    [
        b'{"version":1,"version":1,"providers":{}}',
        b'{"version":1,"providers":{"openai":{"api_key":"x","api_key":"y"}}}',
        b'{"version":1,"providers":{"openai":{"api_key":"x"},"openai":{"api_key":"y"}}}',
        b'{"version":2,"providers":{}}',
        b'{"version":1,"providers":{},"extra":{}}',
        b'{"version":1,"providers":{"OpenAI":{"api_key":"x"}}}',
        b'{"version":1,"providers":{"openai":{"key":"x"}}}',
        b'{"version":1,"providers":{"openai":{"api_key":""}}}',
        b'{"version":1,"providers":{"openai":{"api_key":"a\\u0000b"}}}',
    ],
)
def test_schema_rejects_duplicates_unknowns_versions_and_bad_keys(document: bytes) -> None:
    with pytest.raises(auth.AuthSchemaError) as raised:
        auth.parse_document(document)

    assert SECRET not in str(raised.value)


def test_schema_rejects_key_over_16_kib_and_surrogate() -> None:
    oversized = json.dumps(
        {"version": 1, "providers": {"openai": {"api_key": "x" * (16 * 1024 + 1)}}}
    ).encode()
    surrogate = b'{"version":1,"providers":{"openai":{"api_key":"\\ud800"}}}'

    for document in (oversized, surrogate):
        with pytest.raises(auth.AuthSchemaError):
            auth.parse_document(document)


def test_canonical_env_name_and_collision_are_rejected() -> None:
    assert auth.derived_env_name("foo.bar-baz") == "CAMBIUM_PROVIDER_FOO_BAR_BAZ_API_KEY"
    collision = (
        b'{"version":1,"providers":{'
        b'"foo-bar":{"api_key":"one"},"foo_bar":{"api_key":"two"}}}'
    )

    with pytest.raises(auth.AuthSchemaError, match="conflicts"):
        auth.parse_document(collision)


def test_secret_bearing_representations_are_safe() -> None:
    credential = auth.ProviderCredential("openai", SECRET)
    document = auth.AuthDocument(1, (credential,))

    assert SECRET not in repr(credential)
    assert SECRET not in repr(document)


def test_secure_create_update_remove_and_atomic_replacement(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = auth.AuthStore(path)

    store.set_provider("openai", SECRET)
    directory_stat = path.parent.stat()
    file_stat = path.stat()
    assert stat.S_IMODE(directory_stat.st_mode) == 0o700
    assert directory_stat.st_uid == os.geteuid()
    assert stat.S_IMODE(file_stat.st_mode) == 0o600
    assert file_stat.st_uid == os.geteuid()
    assert file_stat.st_nlink == 1
    assert store.read().providers[0].api_key == SECRET
    first_inode = file_stat.st_ino

    store.set_provider("openai", "replacement")
    assert path.stat().st_ino != first_inode
    assert store.read().providers[0].api_key == "replacement"
    assert not list(path.parent.glob("*.bak*"))
    assert not list(path.parent.glob(".auth.json.tmp-*"))

    assert store.remove_provider("openai") is True
    assert store.read().providers == ()
    assert store.remove_provider("openai") is False


@pytest.mark.parametrize("mode", [0o640, 0o644])
def test_insecure_file_mode_is_rejected(tmp_path: Path, mode: int) -> None:
    path = _store_path(tmp_path)
    store = auth.AuthStore(path)
    store.set_provider("openai", SECRET)
    path.chmod(mode)

    with pytest.raises(auth.AuthStoreError):
        store.read()


def test_symlink_hardlink_and_directory_mode_are_rejected(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = auth.AuthStore(path)
    store.set_provider("openai", SECRET)

    hardlink = path.parent / "linked-auth.json"
    os.link(path, hardlink)
    with pytest.raises(auth.AuthStoreError):
        store.read()
    hardlink.unlink()

    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("not the store", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(auth.AuthStoreError):
        store.read()
    with pytest.raises(auth.AuthStoreError):
        store.set_provider("openai", SECRET)
    path.unlink()

    path.parent.chmod(0o755)
    with pytest.raises(auth.AuthStoreError):
        store.read()


def test_foreign_file_owner_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _store_path(tmp_path)
    store = auth.AuthStore(path)
    store.set_provider("openai", SECRET)
    actual_uid = path.stat().st_uid
    monkeypatch.setattr(auth.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(auth.AuthStoreError):
        store.read()


def test_auth_directory_creation_rejects_symlinked_local_component(tmp_path: Path) -> None:
    """A symlinked intermediate (~/.local) in the fixed auth path must be rejected
    before anything is written through it to the outside target."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)
    path = home / ".local" / "share" / "cambium" / "auth.json"
    store = auth.AuthStore(path)

    with pytest.raises(auth.AuthStoreError, match="symlink"):
        store.set_provider("openai", SECRET)

    assert not (outside / "share").exists()
    assert not (outside / "share" / "cambium").exists()
    assert not list(outside.iterdir())
    assert not (home / ".local" / "share").exists()


def test_auth_directory_creation_rejects_symlinked_final_component(tmp_path: Path) -> None:
    """A symlinked final auth directory is rejected; nothing is written through it."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".local").mkdir()
    (home / ".local" / "share").mkdir()
    (home / ".local" / "share" / "cambium").symlink_to(outside, target_is_directory=True)
    path = home / ".local" / "share" / "cambium" / "auth.json"
    store = auth.AuthStore(path)

    with pytest.raises(auth.AuthStoreError, match="symlink"):
        store.set_provider("openai", SECRET)

    assert not (outside / "auth.json").exists()
    assert not list(outside.iterdir())


def test_launch_environment_scrubs_inherited_credentials() -> None:
    document = auth.AuthDocument(1, (auth.ProviderCredential("foo-bar", SECRET),))
    base = {
        "PATH": "/bin",
        "CAMBIUM_TASK_ID": "task",
        "OPENAI_API_KEY": "inherited",
        "GITHUB_TOKEN": "inherited",
        "CAMBIUM_PROVIDER_OLD_API_KEY": "inherited",
    }

    environment = auth.build_launch_environment(document, base)

    assert environment["PATH"] == "/bin"
    assert environment["CAMBIUM_TASK_ID"] == "task"
    assert environment["CAMBIUM_PROVIDER_FOO_BAR_API_KEY"] == SECRET
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "CAMBIUM_PROVIDER_OLD_API_KEY" not in environment


def test_supervisor_worker_env_allows_only_canonical_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CAMBIUM_PROVIDER_OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "generic-secret")
    monkeypatch.setenv("CAMBIUM_PROVIDER_bad_API_KEY", "noncanonical-secret")
    monkeypatch.setenv("PATH", "/bin")

    environment = supervisor._Runtime._worker_env(
        None, {"task_id": "task"}, generation=3
    )

    assert environment["CAMBIUM_PROVIDER_OPENAI_API_KEY"] == SECRET
    assert environment["PATH"] == "/bin"
    assert environment["CAMBIUM_TASK_ID"] == "task"
    assert environment["CAMBIUM_GENERATION"] == "3"
    assert "OPENAI_API_KEY" not in environment
    assert "CAMBIUM_PROVIDER_bad_API_KEY" not in environment


def test_stdin_key_removes_only_line_ending() -> None:
    assert auth.read_stdin_key(io.BytesIO(b" key with spaces \n")) == " key with spaces "


def test_cli_has_only_fixed_auth_run_profile() -> None:
    parser = cli._build_parser()
    args = parser.parse_args(
        ["auth", "run", "supervisor", "--session-dir", "/tmp/session"]
    )
    assert args.profile == "supervisor"

    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--", "secret-command"])
    with pytest.raises(SystemExit):
        parser.parse_args(["auth", "run", "--", "secret-command"])


def test_auth_supervisor_exec_is_list_form_and_does_not_include_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeStore:
        def launch_environment(self) -> dict[str, str]:
            return {"PATH": "/bin", "CAMBIUM_PROVIDER_OPENAI_API_KEY": SECRET}

    def fake_exec(path: str, argv: list[str], env: dict[str, str]) -> None:
        captured.update(path=path, argv=argv, env=env)

    monkeypatch.setattr(cli, "AuthStore", FakeStore)
    monkeypatch.setattr(cli.os, "execve", fake_exec)
    args = argparse.Namespace(session_dir="/tmp/session", plan=None, task_spec=None)

    assert cli._run_auth_supervisor(args) == 0
    assert captured["argv"] == [
        os.path.abspath(cli.sys.executable),
        "-m",
        "cambium.supervisor",
        "--session-dir",
        "/tmp/session",
    ]
    assert SECRET not in " ".join(captured["argv"])


def test_doctor_auth_checks_have_metadata_schema_and_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _store_path(tmp_path / "home")
    monkeypatch.setattr(auth, "auth_store_path", lambda: path)
    config = _config(tmp_path / "repo" / ".cambium" / "providers.json")

    status, detail = doctor.check_auth_metadata()
    assert status is doctor.Status.WARN
    assert "CAMBIUM_PROVIDER_OPENAI_API_KEY" not in detail

    status, detail = doctor.check_auth_coverage(config.parents[1])
    assert status is doctor.Status.WARN
    assert "CAMBIUM_PROVIDER_OPENAI_API_KEY" not in detail

    auth.AuthStore(path).set_provider("openai", SECRET)
    assert doctor.check_auth_metadata()[0] is doctor.Status.PASS
    assert doctor.check_auth_schema()[0] is doctor.Status.PASS
    status, detail = doctor.check_auth_coverage(config.parents[1])
    assert status is doctor.Status.PASS
    assert "CAMBIUM_PROVIDER_OPENAI_API_KEY" in detail
    assert SECRET not in detail


def test_doctor_fails_closed_on_invalid_schema_without_secret_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _store_path(tmp_path / "home")
    store = auth.AuthStore(path)
    store.set_provider("openai", SECRET)
    path.write_text(
        '{"version":1,"providers":{"openai":{"api_key":"bad", "api_key":"bad2"}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(auth, "auth_store_path", lambda: path)

    status, detail = doctor.check_auth_schema()
    assert status is doctor.Status.FAIL
    assert SECRET not in detail
