"""Secure provider API-key storage and fixed-profile launch support.

The production store has one path and one schema.  The optional ``path``
arguments below exist only to make the module testable; the CLI never exposes
them and always uses :func:`auth_store_path`.

Keys are kept in memory only long enough to validate, serialize, or construct
the environment for a fixed launch profile.  They are never included in
exceptions, representations, command arguments, or diagnostic output.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pwd
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

AUTH_VERSION = 1
MAX_API_KEY_BYTES = 16 * 1024
MIN_API_KEY_BYTES = 5
AUTH_FILE_NAME = "auth.json"
AUTH_DIRECTORY_MODE = 0o700
AUTH_FILE_MODE = 0o600
PROVIDER_ID_PATTERN = r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?"

_PROVIDER_ID_RE = re.compile(PROVIDER_ID_PATTERN + r"\Z")
_PROVIDER_ENV_RE = re.compile(r"CAMBIUM_PROVIDER_[A-Z0-9]+(?:_[A-Z0-9]+)*_API_KEY\Z")
_CREDENTIAL_NAME_RE = re.compile(
    r"(?:api|key|token|secret|password|passwd|credential|authorization|(?:^|_)auth(?:_|$))",
    re.IGNORECASE,
)
_TEMP_NAME_PREFIX = ".auth.json.tmp-"


class AuthError(Exception):
    """Base class for auth-store failures."""


class AuthStoreError(AuthError):
    """The store could not be read or written securely."""


class AuthStoreMissing(AuthStoreError):
    """The auth file is not present."""


class AuthSchemaError(AuthError, ValueError):
    """The auth file does not satisfy the exact schema."""


class _JSONObject(dict[str, Any]):
    """JSON object that remembers duplicate keys before dict conversion hides them."""

    __slots__ = ("duplicate_keys",)

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        values: dict[str, Any] = {}
        duplicate_keys: list[str] = []
        for key, value in pairs:
            if key in values:
                duplicate_keys.append(key)
            values[key] = value
        super().__init__(values)
        self.duplicate_keys = tuple(duplicate_keys)


@dataclass(frozen=True, slots=True, repr=False)
class ProviderCredential:
    """One provider credential.  Its representation deliberately hides all fields."""

    provider: str
    api_key: str

    def __post_init__(self) -> None:
        validate_provider_id(self.provider)
        _validate_api_key(self.provider, self.api_key)


@dataclass(frozen=True, slots=True, repr=False)
class AuthDocument:
    """Validated auth document whose representation cannot expose credentials."""

    version: int
    providers: tuple[ProviderCredential, ...]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != AUTH_VERSION:
            raise AuthSchemaError("auth store version is unsupported")
        if not isinstance(self.providers, tuple):
            raise AuthSchemaError("auth store providers must be a tuple")

        names: set[str] = set()
        env_names: dict[str, str] = {}
        for credential in self.providers:
            if not isinstance(credential, ProviderCredential):
                raise AuthSchemaError("auth store contains an invalid provider")
            if credential.provider in names:
                raise AuthSchemaError(
                    f"provider {credential.provider!r} is duplicated in the auth store"
                )
            names.add(credential.provider)
            env_name = derived_env_name(credential.provider)
            previous = env_names.get(env_name)
            if previous is not None:
                raise AuthSchemaError(
                    f"provider {credential.provider!r} conflicts with provider {previous!r}"
                )
            env_names[env_name] = credential.provider

    @classmethod
    def empty(cls) -> AuthDocument:
        return cls(version=AUTH_VERSION, providers=())

    def provider_names(self) -> tuple[str, ...]:
        return tuple(credential.provider for credential in self.providers)

    def as_mapping(self) -> dict[str, dict[str, str]]:
        """Return the schema mapping for serialization or an atomic update."""
        return {
            credential.provider: {"api_key": credential.api_key} for credential in self.providers
        }


@dataclass(frozen=True, slots=True, repr=False)
class StoreMetadata:
    """Non-secret metadata observed for the fixed auth path."""

    path: Path
    directory_exists: bool
    directory_secure: bool
    directory_uid: int | None
    directory_mode: int | None
    file_exists: bool
    file_secure: bool
    file_uid: int | None
    file_mode: int | None
    file_nlink: int | None
    issue: str | None = None


def effective_home() -> Path:
    """Return the passwd home for the effective UID, never the ``HOME`` value."""
    uid = os.geteuid()
    try:
        record = pwd.getpwuid(uid)
    except KeyError as exc:
        raise AuthStoreError("could not resolve the effective user home") from exc
    if not record.pw_dir:
        raise AuthStoreError("the effective user has no home directory")
    return Path(record.pw_dir)


def auth_store_path() -> Path:
    """Return the only production auth-store path."""
    return effective_home() / ".local" / "share" / "cambium" / AUTH_FILE_NAME


def validate_provider_id(value: object) -> str:
    """Validate and return a provider identifier without echoing invalid input."""
    if not isinstance(value, str) or _PROVIDER_ID_RE.fullmatch(value) is None:
        raise AuthSchemaError("provider id is invalid")
    return value


def derived_env_name(provider: str) -> str:
    """Derive the sole authorized environment name for ``provider``."""
    provider = validate_provider_id(provider)
    normalized = re.sub(r"[._-]+", "_", provider.upper())
    return f"CAMBIUM_PROVIDER_{normalized}_API_KEY"


def oauth_env_suffix(provider: str) -> str:
    """Normalize a provider id for CAMBIUM_OAUTH_* environment names."""
    validate_provider_id(provider)
    return re.sub(r"[._-]+", "_", provider.upper())


def is_provider_env_name(value: object) -> bool:
    """Return whether ``value`` is in the canonical provider-key namespace."""
    return isinstance(value, str) and _PROVIDER_ENV_RE.fullmatch(value) is not None


def validate_derived_env_name(provider: str, env_name: object) -> str:
    """Require a provider mapping to use its canonical Cambium name."""
    provider = validate_provider_id(provider)
    expected = derived_env_name(provider)
    if not isinstance(env_name, str) or env_name != expected:
        raise AuthSchemaError(f"provider {provider!r} has an invalid environment mapping")
    return expected


def _validate_api_key(provider: str, value: object) -> str:
    if not isinstance(value, str):
        raise AuthSchemaError(f"provider {provider!r} api key is not a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuthSchemaError(f"provider {provider!r} api key is not valid UTF-8") from exc
    if not encoded:
        raise AuthSchemaError(f"provider {provider!r} api key is empty")
    if value.isspace():
        raise AuthSchemaError(f"provider {provider!r} api key is whitespace")
    if len(encoded) < MIN_API_KEY_BYTES:
        raise AuthSchemaError(f"provider {provider!r} api key is too short")
    if b"\x00" in encoded:
        raise AuthSchemaError(f"provider {provider!r} api key contains NUL")
    if len(encoded) > MAX_API_KEY_BYTES:
        raise AuthSchemaError(f"provider {provider!r} api key is too long")
    return value


def _check_duplicate_object(value: object, context: str) -> None:
    if isinstance(value, _JSONObject) and value.duplicate_keys:
        if context == "auth store providers":
            duplicate = next(
                (key for key in value.duplicate_keys if _PROVIDER_ID_RE.fullmatch(key)),
                None,
            )
            if duplicate is not None:
                raise AuthSchemaError(f"provider {duplicate!r} is duplicated in the auth store")
        raise AuthSchemaError(f"{context} contains duplicate JSON fields")


def _validate_raw_document(raw: object) -> AuthDocument:
    if not isinstance(raw, Mapping):
        raise AuthSchemaError("auth store root must be an object")
    _check_duplicate_object(raw, "auth store root")
    if set(raw) != {"version", "providers"}:
        raise AuthSchemaError("auth store root must contain exactly version and providers")

    version = raw.get("version")
    if type(version) is not int or version != AUTH_VERSION:
        raise AuthSchemaError("auth store version is unsupported")

    raw_providers = raw.get("providers")
    if not isinstance(raw_providers, Mapping):
        raise AuthSchemaError("auth store providers must be an object")
    _check_duplicate_object(raw_providers, "auth store providers")

    credentials: list[ProviderCredential] = []
    for raw_provider, raw_entry in raw_providers.items():
        provider = validate_provider_id(raw_provider)
        if not isinstance(raw_entry, Mapping):
            raise AuthSchemaError(f"provider {provider!r} entry must be an object")
        _check_duplicate_object(raw_entry, f"provider {provider!r}")
        if set(raw_entry) != {"api_key"}:
            raise AuthSchemaError(f"provider {provider!r} entry must contain only api_key")
        credentials.append(ProviderCredential(provider, cast(str, raw_entry.get("api_key"))))

    credentials.sort(key=lambda credential: credential.provider)
    return AuthDocument(version=version, providers=tuple(credentials))


def _reject_json_constant(_value: str) -> Any:
    raise AuthSchemaError("auth store contains an invalid JSON constant")


def parse_document(data: bytes) -> AuthDocument:
    """Decode and validate one complete UTF-8 auth document."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthSchemaError("auth store is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_JSONObject,
            parse_constant=_reject_json_constant,
        )
    except AuthSchemaError:
        raise
    except json.JSONDecodeError as exc:
        raise AuthSchemaError("auth store contains invalid JSON") from exc
    return _validate_raw_document(raw)


def serialize_document(document: AuthDocument) -> bytes:
    """Serialize a validated document without changing its schema."""
    raw = {
        "version": AUTH_VERSION,
        "providers": document.as_mapping(),
    }
    return (
        json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise AuthStoreError("secure directory flags are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


def _file_read_flags() -> int:
    required = ("O_CLOEXEC", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise AuthStoreError("secure file flags are unavailable")
    return os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _validate_directory_stat(value: os.stat_result) -> None:
    if not stat.S_ISDIR(value.st_mode):
        raise AuthStoreError("auth store directory is not a directory")
    if value.st_uid != os.geteuid():
        raise AuthStoreError("auth store directory owner is invalid")
    if stat.S_IMODE(value.st_mode) != AUTH_DIRECTORY_MODE:
        raise AuthStoreError("auth store directory permissions are invalid")


def _validate_file_stat(value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise AuthStoreError("auth store file is not a regular file")
    if value.st_uid != os.geteuid():
        raise AuthStoreError("auth store file owner is invalid")
    if stat.S_IMODE(value.st_mode) != AUTH_FILE_MODE:
        raise AuthStoreError("auth store file permissions are invalid")
    if value.st_nlink != 1:
        raise AuthStoreError("auth store file has an invalid link count")


def _open_directory(path: Path, *, create: bool) -> int | None:
    """Open ``path`` without resolving or releasing an intermediate component."""
    target = Path(path)
    if not target.is_absolute():
        target = Path.cwd() / target
    names = target.parts[1:]
    if any(name in {".", ".."} for name in names):
        raise AuthStoreError("auth store directory path contains traversal")

    flags = _directory_flags()
    opened: list[int] = []
    links: list[tuple[int, str, int]] = []
    try:
        parent_fd = os.open(target.anchor, flags)
        opened.append(parent_fd)
        for name in names:
            if create:
                try:
                    os.mkdir(name, AUTH_DIRECTORY_MODE, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise AuthStoreError("could not create the auth store directory") from exc
            try:
                child_fd = os.open(name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    return None
                raise AuthStoreError("could not open the auth store directory") from None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise AuthStoreError(
                        "auth store directory path must not contain a symlink "
                        "or non-directory component"
                    ) from exc
                raise AuthStoreError("could not open the auth store directory") from exc
            opened.append(child_fd)
            links.append((parent_fd, name, child_fd))
            parent_fd = child_fd

        _validate_directory_stat(os.fstat(parent_fd))
        for ancestor_fd, name, child_fd in links:
            current = os.stat(name, dir_fd=ancestor_fd, follow_symlinks=False)
            opened_stat = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(current.st_mode)
                or current.st_dev != opened_stat.st_dev
                or current.st_ino != opened_stat.st_ino
            ):
                raise AuthStoreError("auth store directory path changed during validation")

        opened.pop()
        return parent_fd
    except AuthError:
        raise
    except OSError as exc:
        raise AuthStoreError("could not open the auth store directory") from exc
    finally:
        for fd in reversed(opened):
            os.close(fd)


def _verify_directory_path(path: Path, expected_fd: int) -> None:
    """Require a fresh traversal of ``path`` to reach ``expected_fd``'s inode."""
    current_fd = _open_directory(path, create=False)
    if current_fd is None:
        raise AuthStoreError("auth store directory path changed during validation")
    try:
        expected = os.fstat(expected_fd)
        current = os.fstat(current_fd)
        if expected.st_dev != current.st_dev or expected.st_ino != current.st_ino:
            raise AuthStoreError("auth store directory path changed during validation")
    finally:
        os.close(current_fd)


def _open_secure_file(dir_fd: int, name: str) -> int:
    try:
        fd = os.open(name, _file_read_flags(), dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AuthStoreError("auth store file must not be a symlink") from exc
        raise AuthStoreError("could not open the auth store file") from exc
    valid = False
    try:
        _validate_file_stat(os.fstat(fd))
        valid = True
    finally:
        if not valid:
            os.close(fd)
    return fd


@contextmanager
def _exclusive_lock(directory_fd: int):
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
    except OSError as exc:
        raise AuthStoreError("could not lock the auth store") from exc
    try:
        yield
    finally:
        primary = sys.exception()
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        except OSError as exc:
            release_error = AuthStoreError("could not unlock the auth store")
            if primary is not None:
                primary.add_note(f"auth store lock release failed: {exc}")
            else:
                raise release_error from exc


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(fd, 1024 * 1024)
        except OSError as exc:
            raise AuthStoreError("could not read the auth store file") from exc
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_document_from_directory(dir_fd: int, name: str) -> AuthDocument:
    try:
        fd = _open_secure_file(dir_fd, name)
    except FileNotFoundError as exc:
        raise AuthStoreMissing("auth store file is not present") from exc
    try:
        return parse_document(_read_fd(fd))
    finally:
        os.close(fd)


def _read_document_if_present(dir_fd: int, name: str) -> AuthDocument:
    try:
        return _read_document_from_directory(dir_fd, name)
    except AuthStoreMissing:
        return AuthDocument.empty()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError as exc:
            raise AuthStoreError("could not write the auth store file") from exc
        if written <= 0:
            raise AuthStoreError("could not write the auth store file")
        view = view[written:]


def _new_temp_file(dir_fd: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    for _ in range(10):
        name = f".auth.json.tmp-{secrets.token_hex(16)}"
        try:
            fd = os.open(name, flags, AUTH_FILE_MODE, dir_fd=dir_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise AuthStoreError("could not create the auth store temporary file") from exc
        return fd, name
    raise AuthStoreError("could not create the auth store temporary file")


def _atomic_write(dir_fd: int, name: str, document: AuthDocument) -> None:
    data = serialize_document(document)
    fd: int | None = None
    temp_name: str | None = None
    try:
        fd, temp_name = _new_temp_file(dir_fd)
        try:
            os.fchmod(fd, AUTH_FILE_MODE)
            _validate_file_stat(os.fstat(fd))
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
            fd = None

        os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp_name = None
        check_fd = _open_secure_file(dir_fd, name)
        os.close(check_fd)
        os.fsync(dir_fd)
    except AuthError:
        raise
    except OSError as exc:
        raise AuthStoreError("could not atomically update the auth store") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def inspect_metadata(path: Path | None = None) -> StoreMetadata:
    """Inspect fixed-path metadata without reading a credential value."""
    target = auth_store_path() if path is None else Path(path)
    directory = target.parent
    directory_uid: int | None = None
    directory_mode: int | None = None
    file_uid: int | None = None
    file_mode: int | None = None
    file_nlink: int | None = None

    try:
        directory_fd = _open_directory(directory, create=False)
    except AuthError as exc:
        return StoreMetadata(
            path=target,
            directory_exists=True,
            directory_secure=False,
            directory_uid=directory_uid,
            directory_mode=directory_mode,
            file_exists=False,
            file_secure=False,
            file_uid=None,
            file_mode=None,
            file_nlink=None,
            issue=str(exc),
        )
    if directory_fd is None:
        return StoreMetadata(
            path=target,
            directory_exists=False,
            directory_secure=False,
            directory_uid=None,
            directory_mode=None,
            file_exists=False,
            file_secure=False,
            file_uid=None,
            file_mode=None,
            file_nlink=None,
        )

    directory_stat = os.fstat(directory_fd)
    directory_uid = directory_stat.st_uid
    directory_mode = stat.S_IMODE(directory_stat.st_mode)

    try:
        try:
            file_fd = _open_secure_file(directory_fd, target.name)
        except FileNotFoundError:
            return StoreMetadata(
                path=target,
                directory_exists=True,
                directory_secure=True,
                directory_uid=directory_uid,
                directory_mode=directory_mode,
                file_exists=False,
                file_secure=False,
                file_uid=None,
                file_mode=None,
                file_nlink=None,
            )
        except AuthError as exc:
            try:
                file_stat = os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                file_stat = None
            if file_stat is not None:
                file_uid = file_stat.st_uid
                file_mode = stat.S_IMODE(file_stat.st_mode)
                file_nlink = file_stat.st_nlink
            return StoreMetadata(
                path=target,
                directory_exists=True,
                directory_secure=True,
                directory_uid=directory_uid,
                directory_mode=directory_mode,
                file_exists=file_stat is not None,
                file_secure=False,
                file_uid=file_uid,
                file_mode=file_mode,
                file_nlink=file_nlink,
                issue=str(exc),
            )
        try:
            file_stat = os.fstat(file_fd)
            file_uid = file_stat.st_uid
            file_mode = stat.S_IMODE(file_stat.st_mode)
            file_nlink = file_stat.st_nlink
            return StoreMetadata(
                path=target,
                directory_exists=True,
                directory_secure=True,
                directory_uid=directory_uid,
                directory_mode=directory_mode,
                file_exists=True,
                file_secure=True,
                file_uid=file_uid,
                file_mode=file_mode,
                file_nlink=file_nlink,
            )
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


class AuthStore:
    """Read and atomically update the fixed Cambium auth store."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = auth_store_path() if path is None else Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> AuthDocument:
        directory_fd = _open_directory(self._path.parent, create=False)
        if directory_fd is None:
            return AuthDocument.empty()
        try:
            return _read_document_if_present(directory_fd, self._path.name)
        finally:
            os.close(directory_fd)

    def save(self, document: AuthDocument) -> None:
        if not isinstance(document, AuthDocument):
            raise AuthSchemaError("auth store document is invalid")
        directory_fd = _open_directory(self._path.parent, create=True)
        if directory_fd is None:
            raise AuthStoreError("auth store directory is unavailable")
        try:
            with _exclusive_lock(directory_fd):
                _verify_directory_path(self._path.parent, directory_fd)
                _atomic_write(directory_fd, self._path.name, document)
        finally:
            os.close(directory_fd)

    def set_provider(self, provider: str, api_key: str) -> None:
        provider = validate_provider_id(provider)
        _validate_api_key(provider, api_key)
        directory_fd = _open_directory(self._path.parent, create=True)
        if directory_fd is None:
            raise AuthStoreError("auth store directory is unavailable")
        try:
            with _exclusive_lock(directory_fd):
                current = _read_document_if_present(directory_fd, self._path.name)
                values = current.as_mapping()
                values[provider] = {"api_key": api_key}
                self.save_document_in_locked_directory(directory_fd, values)
        finally:
            os.close(directory_fd)

    def remove_provider(self, provider: str) -> bool:
        provider = validate_provider_id(provider)
        directory_fd = _open_directory(self._path.parent, create=False)
        if directory_fd is None:
            return False
        try:
            with _exclusive_lock(directory_fd):
                current = _read_document_if_present(directory_fd, self._path.name)
                values = current.as_mapping()
                if provider not in values:
                    return False
                del values[provider]
                self.save_document_in_locked_directory(directory_fd, values)
                return True
        finally:
            os.close(directory_fd)

    def save_document_in_locked_directory(
        self, directory_fd: int, values: Mapping[str, Mapping[str, str]]
    ) -> None:
        """Serialize ``values`` while the caller owns the directory lock."""
        raw = {"version": AUTH_VERSION, "providers": dict(values)}
        document = _validate_raw_document(raw)
        _verify_directory_path(self._path.parent, directory_fd)
        _atomic_write(directory_fd, self._path.name, document)

    def launch_environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build a scrubbed environment containing only stored provider keys."""
        return build_launch_environment(self.read(), base)

    def listed_entries(self) -> tuple[tuple[str, str], ...]:
        """Return provider names and derived names, never credential values."""
        document = self.read()
        return tuple(
            (credential.provider, derived_env_name(credential.provider))
            for credential in document.providers
        )

    def has_provider(self, provider: str) -> bool:
        """Return whether ``provider`` is configured in the auth store.

        Reuses the validated store read and never returns or logs a key value.
        """
        provider = validate_provider_id(provider)
        return provider in self.read().provider_names()


def build_launch_environment(
    document: AuthDocument | Mapping[str, str],
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a credential-scrubbed environment for an authorized profile."""
    if isinstance(document, AuthDocument):
        credentials = document.providers
    elif isinstance(document, Mapping):
        credentials = tuple(
            ProviderCredential(provider, api_key) for provider, api_key in sorted(document.items())
        )
        document = AuthDocument(AUTH_VERSION, credentials)
    else:
        raise AuthSchemaError("auth store document is invalid")

    environment = scrub_environment(base)
    for credential in document.providers:
        environment[derived_env_name(credential.provider)] = credential.api_key
    return environment


def scrub_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Remove credential-like variables from a subprocess environment.

    ``GIT_CONFIG_*`` variables are always preserved: they carry no secrets
    and the credential-name regex would otherwise strip ``GIT_CONFIG_KEY_0``
    while leaving its paired ``GIT_CONFIG_COUNT`` set, which git rejects
    with "unable to parse command-line config".
    """
    source = os.environ if base is None else base
    return {
        name: value
        for name, value in source.items()
        if name.startswith("GIT_CONFIG_")
        or (
            not name.startswith(("CAMBIUM_PROVIDER_", "CAMBIUM_OAUTH_"))
            and not _CREDENTIAL_NAME_RE.search(name)
        )
    }


def read_stdin_key(stream: TextIO | None = None) -> str:
    """Read one key from stdin, removing only the conventional line ending."""
    source: Any = stream if stream is not None else __import__("sys").stdin
    binary = getattr(source, "buffer", source)
    raw = binary.read(MAX_API_KEY_BYTES + 2)
    if isinstance(raw, str):
        try:
            raw = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise AuthSchemaError("stdin key is not valid UTF-8") from exc
    if not isinstance(raw, bytes):
        raise AuthSchemaError("stdin key could not be read")
    if len(raw) > MAX_API_KEY_BYTES + 2:
        raise AuthSchemaError("stdin key is too long")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuthSchemaError("stdin key is not valid UTF-8") from exc
    return _validate_api_key("stdin", value)


__all__ = [
    "AUTH_FILE_NAME",
    "AUTH_VERSION",
    "AUTH_DIRECTORY_MODE",
    "AUTH_FILE_MODE",
    "MIN_API_KEY_BYTES",
    "MAX_API_KEY_BYTES",
    "PROVIDER_ID_PATTERN",
    "AuthDocument",
    "AuthError",
    "AuthSchemaError",
    "AuthStore",
    "AuthStoreError",
    "AuthStoreMissing",
    "ProviderCredential",
    "StoreMetadata",
    "auth_store_path",
    "build_launch_environment",
    "derived_env_name",
    "effective_home",
    "inspect_metadata",
    "is_provider_env_name",
    "parse_document",
    "read_stdin_key",
    "serialize_document",
    "scrub_environment",
    "validate_derived_env_name",
    "validate_provider_id",
]
