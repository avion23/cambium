"""Scenario checks for the isolated module conformance gate.

The offline-environment probes must spawn real child interpreters and
command shims to verify network denial, credential stripping, and isolated
Python flags, so they are marked ``slow`` and run in the second tier. Pure
loader-validation checks stay in the first tier.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from cambium import module_conformance
from cambium.modules.base import (
    DatasetError,
    ModuleContractError,
    load_jsonl,
    load_module_manifest,
)


def test_jsonl_loader_rejects_duplicate_keys_and_nonstandard_constants(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text('{"id": 1, "id": 2}\n', encoding="utf-8")
    with pytest.raises(DatasetError, match="duplicate"):
        load_jsonl(duplicate)

    for constant in ("NaN", "Infinity", "-Infinity"):
        path = tmp_path / f"{constant.replace('-', 'negative')}.jsonl"
        path.write_text(f'{{"value": {constant}}}\n', encoding="utf-8")
        with pytest.raises(DatasetError, match="non-standard"):
            load_jsonl(path)


def test_manifest_loader_wraps_malformed_utf8(tmp_path: Path) -> None:
    module_dir = tmp_path / "example"
    module_dir.mkdir()
    manifest = module_dir / "module.json"
    manifest.write_bytes(b"{\xff")

    with pytest.raises(ModuleContractError, match="invalid"):
        load_module_manifest(module_dir)


@pytest.mark.slow
def test_offline_subprocess_environment_strips_credentials_and_denies_network(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "must-not-leak")

    with module_conformance.module_offline_environment() as env:
        assert "EXAMPLE_API_TOKEN" not in env

        curl = subprocess.run(
            ["curl", "--fail", "http://127.0.0.1:9/"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )
        socket_probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket; socket.create_connection(('127.0.0.1', 9), timeout=1)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert curl.returncode == 126
    assert "network client denied" in curl.stderr
    assert socket_probe.returncode != 0
    assert "network access is forbidden" in socket_probe.stderr


@pytest.mark.slow
def test_offline_child_denies_absolute_network_client_path() -> None:
    probe = (
        "import subprocess, sys; "
        "subprocess.run(['/usr/bin/curl', '--fail', 'http://127.0.0.1:9/'], check=False); "
        "sys.exit('absolute curl unexpectedly started')"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "network client denied during module conformance: /usr/bin/curl" in result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("client", ["curl", "wget", "nc", "ssh"])
def test_offline_child_denies_shell_network_client(client: str) -> None:
    probe = f"import subprocess; subprocess.run({client + ' --version'!r}, shell=True, check=False)"
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "network client denied during module conformance:" in result.stderr
    assert f"/{client}" in result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("api", ["run", "Popen"])
def test_offline_child_resolves_network_client_realpath(tmp_path: Path, api: str) -> None:
    curl = shutil.which("curl")
    assert curl is not None
    alias = tmp_path / "ordinary command"
    alias.symlink_to(curl)
    probe = (
        "import subprocess, sys; "
        f"result = subprocess.{api}([{str(alias)!r}, '--fail', 'http://127.0.0.1:9/']); "
        "sys.exit(result.wait() if hasattr(result, 'wait') else result.returncode)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert result.returncode != 7
    denied = f"network client denied during module conformance: {os.path.realpath(curl)}"
    assert denied in result.stderr


@pytest.mark.slow
def test_offline_child_denies_shell_network_client_realpath_with_whitespace_path(
    tmp_path: Path,
) -> None:
    curl = shutil.which("curl")
    assert curl is not None
    alias = tmp_path / "ordinary command"
    alias.symlink_to(curl)
    command = f"'{alias}' --fail http://127.0.0.1:9/"
    probe = (
        "import subprocess, sys; "
        f"result = subprocess.run([{command!r}], shell=True, check=False); "
        "sys.exit(result.returncode)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert result.returncode != 7
    denied = f"network client denied during module conformance: {os.path.realpath(curl)}"
    assert denied in result.stderr


@pytest.mark.slow
def test_offline_guard_does_not_require_strace(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    probe = (
        "import subprocess, sys; subprocess.run([sys.executable, '-c', 'print(42)'], check=True)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "42\n"


@pytest.mark.slow
def test_offline_child_inherits_provider_import_blocker() -> None:
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", "import cambium.provider_config"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "provider import blocked by module conformance: cambium.provider_config" in result.stderr


@pytest.mark.slow
@pytest.mark.parametrize("flag", ["-E", "-S", "-I"])
def test_offline_child_rejects_python_flags_that_bypass_provider_blocker(flag: str) -> None:
    probe = (
        "import subprocess, sys; "
        f"subprocess.run([sys.executable, {flag!r}, '-c', "
        "'import cambium.provider_config'], check=True)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert f"isolated Python flag denied during module conformance: {flag}" in result.stderr


@pytest.mark.slow
def test_offline_child_rejects_python_flag_after_option_argument() -> None:
    probe = (
        "import subprocess, sys; "
        "subprocess.run([sys.executable, '-W', 'ignore', '-I', '-c', "
        "'import cambium.provider_config'], check=True)"
    )
    with module_conformance.module_offline_environment() as env:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=env,
        )

    assert result.returncode != 0
    assert "isolated Python flag denied during module conformance: -I" in result.stderr


@pytest.mark.slow
def test_offline_child_rejects_python_flag_after_option_argument_with_executable() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        socket_probe = (
            f"import socket; socket.create_connection(('127.0.0.1', {port}), timeout=2).close()"
        )
        probe = (
            "import subprocess, sys; "
            "subprocess.run(['ordinary-python', '-W', 'ignore', '-I', '-c', "
            f"{socket_probe!r}], executable=sys.executable, check=True)"
        )
        with module_conformance.module_offline_environment() as env:
            result = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env=env,
            )

        listener.settimeout(0.5)
        try:
            connection, _ = listener.accept()
        except TimeoutError:
            connected = False
        else:
            connection.close()
            connected = True

    assert result.returncode != 0
    assert not connected
    assert "isolated Python flag denied during module conformance: -I" in result.stderr
