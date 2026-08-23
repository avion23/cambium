"""Codex ChatGPT-subscription OAuth: hardened store, refresh manager, device flow.

This module authenticates to the Codex ChatGPT-subscription issuer
(https://auth.openai.com) using the pinned device flow: request a user code,
poll until the user approves in a browser, exchange the resulting
authorization code for tokens, and refresh access tokens when they expire.
It never stores the id_token or the account email; only the access token,
refresh token, expiry, and account id are persisted.

Storage reuses :mod:`cambium.auth` hardening primitives: a 0700 owner-only
directory, a 0600 owner-only no-symlink no-hardlink file, flocked atomic
updates, strict schema/version validation, duplicate-field rejection, and a
bounded document size. A corrupt store fails closed: reads raise
``OAuthStoreError`` and the only recovery path is an explicit ``repair()``.

The Codex OAuth public client id is pinned in the trusted provider profile.
Callers may override it for tests or another compatible public client; it is
configuration, not a credential.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import http.client
import json
import os
import secrets
import stat
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode, urlparse

from .auth import (
    AUTH_FILE_MODE,
    AuthStoreError,
    _JSONObject,
    _open_directory,
    _open_secure_file,
    _validate_file_stat,
    _verify_directory_path,
    _write_all,
    effective_home,
    validate_provider_id,
)
from .provider_config import CODEX_CHATGPT_PROFILE, is_loopback_host

OAUTH_VERSION = 1
OAUTH_FILE_NAME = "oauth.json"
MAX_OAUTH_DOC_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 16 * 1024
MAX_OAUTH_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_ISSUER = "https://auth.openai.com"
DEFAULT_EXPIRES_IN_S = 3600.0
DEFAULT_REFRESH_MARGIN_S = 60.0
DEFAULT_POLL_EXPIRY_S = 15 * 60.0
DEFAULT_HTTP_TIMEOUT_S = 30.0
DEFAULT_LOCK_TIMEOUT_S = 30.0
VERIFICATION_PATH = "/codex/device"
CALLBACK_PATH = "/deviceauth/callback"
_TEMP_NAME_PREFIX = ".oauth.json.tmp-"
_LOCK_FILE_PREFIX = "oauth."
_LOCK_FILE_SUFFIX = ".lock"
_REFRESH_ENDPOINT = "/oauth/token"
_USERCODE_ENDPOINT = "/api/accounts/deviceauth/usercode"
_DEVICE_TOKEN_ENDPOINT = "/api/accounts/deviceauth/token"


def resolve_codex_client_id(value: str | None = None) -> str:
    """Return an explicit override or the pinned public Codex OAuth client id."""

    if isinstance(value, str) and value.strip():
        return value.strip()
    profile_value = CODEX_CHATGPT_PROFILE.get("client_id")
    if isinstance(profile_value, str) and profile_value:
        return profile_value
    raise OAuthError("the trusted Codex OAuth profile has no client id")


class OAuthError(Exception):
    """Base class for oauth-store, refresh, and device-flow failures."""


class OAuthStoreError(OAuthError):
    """The oauth store could not be read or written securely."""


class OAuthSchemaError(OAuthStoreError, ValueError):
    """The oauth store does not satisfy the exact schema (fail closed)."""


class OAuthMissingError(OAuthError):
    """No oauth record exists for the requested provider."""


class LockTimeoutError(OAuthError):
    """The per-provider refresh lock could not be acquired in time."""


class RefreshUnavailableError(OAuthError):
    """A refresh failed (429/5xx/timeout); the last-good document is intact."""


class InvalidGrantError(OAuthError):
    """The refresh token was rejected; the provider is disabled until re-login."""


class DeviceFlowError(OAuthError):
    """A device-flow request failed."""


class DeviceFlowExpired(DeviceFlowError):
    """The user did not approve before the device code expired."""


class DeviceFlowCanceled(DeviceFlowError):
    """The device flow was canceled."""


def _validate_provider_id(value: object) -> str:
    """Validate a provider id, surfacing oauth-typed schema errors only."""
    try:
        return validate_provider_id(value)
    except ValueError as exc:
        raise OAuthSchemaError(str(exc)) from exc


@dataclass(frozen=True, slots=True, repr=False)
class OAuthDoc:
    """One provider's oauth tokens. The representation hides all token values."""

    provider: str
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str | None

    def __post_init__(self) -> None:
        _validate_provider_id(self.provider)
        _validate_token(self.access_token, "access token")
        _validate_token(self.refresh_token, "refresh token")
        if isinstance(self.expires_at, bool) or not isinstance(self.expires_at, int | float):
            raise OAuthSchemaError("oauth expires_at must be an epoch number")
        if not _finite(self.expires_at):
            raise OAuthSchemaError("oauth expires_at must be finite")
        if self.account_id is not None:
            if not isinstance(self.account_id, str) or not self.account_id:
                raise OAuthSchemaError("oauth account_id must be a non-empty string or null")
            _validate_token(self.account_id, "account id")

    def __repr__(self) -> str:
        return (
            f"OAuthDoc(provider={self.provider!r}, expires_at={self.expires_at!r}, "
            f"account_id={bool(self.account_id)})"
        )


@dataclass(frozen=True, slots=True)
class OAuthRecord:
    """A stored provider record: the token doc plus its disablement flag."""

    doc: OAuthDoc
    disabled: bool


@dataclass(frozen=True, slots=True)
class OAuthDocument:
    """Validated oauth store document (one record per provider)."""

    version: int
    records: tuple[OAuthRecord, ...]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != OAUTH_VERSION:
            raise OAuthSchemaError("oauth store version is unsupported")
        if not isinstance(self.records, tuple):
            raise OAuthSchemaError("oauth store records must be a tuple")
        names: set[str] = set()
        for record in self.records:
            if not isinstance(record, OAuthRecord):
                raise OAuthSchemaError("oauth store contains an invalid record")
            if record.doc.provider in names:
                raise OAuthSchemaError(
                    f"provider {record.doc.provider!r} is duplicated in the oauth store"
                )
            names.add(record.doc.provider)

    @classmethod
    def empty(cls) -> OAuthDocument:
        return cls(version=OAUTH_VERSION, records=())

    def by_provider(self, provider: str) -> OAuthRecord | None:
        for record in self.records:
            if record.doc.provider == provider:
                return record
        return None


@dataclass(frozen=True, slots=True, repr=False)
class UserCode:
    """Device-flow challenge. The representation hides every transient secret."""

    verification_url: str
    user_code: str
    device_auth_id: str
    interval: float

    def __repr__(self) -> str:
        return f"UserCode(verification_url={self.verification_url!r}, interval={self.interval!r})"


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationCode:
    """Approved device authorization code. The representation hides its values."""

    code: str
    code_verifier: str

    def __repr__(self) -> str:
        return "AuthorizationCode(approved=True)"


@dataclass(frozen=True, slots=True, repr=False)
class RefreshedTokens:
    """Validated refresh response; ``refresh_token``/``account_id`` may be absent."""

    access_token: str
    expires_in: float
    refresh_token: str | None = None
    account_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"RefreshedTokens(expires_in={self.expires_in!r}, "
            f"refresh_token={bool(self.refresh_token)}, account_id={bool(self.account_id)})"
        )


def oauth_store_path() -> Path:
    """Return the only production oauth-store path."""
    return effective_home() / ".local" / "share" / "cambium" / OAUTH_FILE_NAME


def codex_cli_auth_path() -> Path:
    """Return the codex CLI's session path under the effective user's home."""
    return effective_home() / ".codex" / "auth.json"


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _validate_token(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OAuthSchemaError(f"oauth {label} is not a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OAuthSchemaError(f"oauth {label} is not valid UTF-8") from exc
    if not encoded:
        raise OAuthSchemaError(f"oauth {label} is empty")
    if b"\x00" in encoded:
        raise OAuthSchemaError(f"oauth {label} contains NUL")
    if len(encoded) > MAX_TOKEN_BYTES:
        raise OAuthSchemaError(f"oauth {label} is too long")
    return value


def validate_issuer(value: str) -> str:
    """Require an absolute https issuer, or http for loopback test hosts only."""
    if not isinstance(value, str):
        raise OAuthError("issuer must be a string")
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise OAuthError("issuer must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise OAuthError("issuer must not contain URL credentials")
    if parsed.scheme.lower() == "http" and not is_loopback_host(parsed.hostname or ""):
        raise OAuthError(
            "http issuer is allowed only for loopback hosts; remote issuers require https"
        )
    return value


def _reject_json_constant(_value: str) -> Any:
    raise OAuthSchemaError("oauth store contains an invalid JSON constant")


def _check_duplicate_object(value: object, context: str) -> None:
    if isinstance(value, _JSONObject) and value.duplicate_keys:
        raise OAuthSchemaError(f"{context} contains duplicate JSON fields")


def _record_mapping(record: OAuthRecord) -> dict[str, object]:
    return {
        "access_token": record.doc.access_token,
        "refresh_token": record.doc.refresh_token,
        "expires_at": float(record.doc.expires_at),
        "account_id": record.doc.account_id,
        "disabled": record.disabled,
    }


def serialize_document(document: OAuthDocument) -> bytes:
    """Serialize a validated document without changing its schema."""
    raw = {
        "version": OAUTH_VERSION,
        "providers": {record.doc.provider: _record_mapping(record) for record in document.records},
    }
    return (
        json.dumps(raw, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _validate_raw_document(raw: object) -> OAuthDocument:
    if not isinstance(raw, Mapping):
        raise OAuthSchemaError("oauth store root must be an object")
    _check_duplicate_object(raw, "oauth store root")
    if set(raw) != {"version", "providers"}:
        raise OAuthSchemaError("oauth store root must contain exactly version and providers")

    version = raw.get("version")
    if type(version) is not int or version != OAUTH_VERSION:
        raise OAuthSchemaError("oauth store version is unsupported")

    raw_providers = raw.get("providers")
    if not isinstance(raw_providers, Mapping):
        raise OAuthSchemaError("oauth store providers must be an object")
    _check_duplicate_object(raw_providers, "oauth store providers")

    records: list[OAuthRecord] = []
    for raw_provider, raw_entry in raw_providers.items():
        provider = _validate_provider_id(raw_provider)
        if not isinstance(raw_entry, Mapping):
            raise OAuthSchemaError(f"provider {provider!r} entry must be an object")
        _check_duplicate_object(raw_entry, f"provider {provider!r}")
        if set(raw_entry) != {
            "access_token",
            "refresh_token",
            "expires_at",
            "account_id",
            "disabled",
        }:
            raise OAuthSchemaError(
                f"provider {provider!r} entry must contain exactly access_token, "
                "refresh_token, expires_at, account_id, and disabled"
            )
        access_token = cast(str, raw_entry.get("access_token"))
        refresh_token = cast(str, raw_entry.get("refresh_token"))
        expires_at = cast(float, raw_entry.get("expires_at"))
        doc = OAuthDoc(
            provider=provider,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            account_id=raw_entry.get("account_id"),
        )
        disabled = raw_entry.get("disabled")
        if not isinstance(disabled, bool):
            raise OAuthSchemaError(f"provider {provider!r} disabled must be a boolean")
        records.append(OAuthRecord(doc=doc, disabled=disabled))

    records.sort(key=lambda record: record.doc.provider)
    return OAuthDocument(version=version, records=tuple(records))


def parse_document(data: bytes) -> OAuthDocument:
    """Decode and validate one complete UTF-8 oauth document (fail closed)."""
    if len(data) > MAX_OAUTH_DOC_BYTES:
        raise OAuthSchemaError("oauth store exceeds the maximum document size")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OAuthSchemaError("oauth store is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_JSONObject,
            parse_constant=_reject_json_constant,
        )
    except OAuthSchemaError:
        raise
    except json.JSONDecodeError as exc:
        raise OAuthSchemaError("oauth store contains invalid JSON") from exc
    return _validate_raw_document(raw)


def _read_fd(fd: int) -> bytes:
    """Read at most one byte beyond the oauth document size cap."""
    chunks: list[bytes] = []
    remaining = MAX_OAUTH_DOC_BYTES + 1
    while remaining:
        try:
            chunk = os.read(fd, min(1024 * 1024, remaining))
        except OSError as exc:
            raise OAuthStoreError("could not read the oauth store file") from exc
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining == 0:
            raise OAuthSchemaError("oauth store exceeds the maximum document size")
    return b"".join(chunks)


def _new_temp_file(dir_fd: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    for _ in range(10):
        name = f".oauth.json.tmp-{secrets.token_hex(16)}"
        try:
            fd = os.open(name, flags, AUTH_FILE_MODE, dir_fd=dir_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise OAuthStoreError("could not create the oauth store temporary file") from exc
        return fd, name
    raise OAuthStoreError("could not create the oauth store temporary file")


def _atomic_write(dir_fd: int, name: str, data: bytes) -> None:
    if len(data) > MAX_OAUTH_DOC_BYTES:
        raise OAuthSchemaError("oauth store exceeds the maximum document size")
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
    except OAuthStoreError:
        raise
    except AuthStoreError as exc:
        raise OAuthStoreError(str(exc)) from exc
    except OSError as exc:
        raise OAuthStoreError("could not atomically update the oauth store") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except (FileNotFoundError, OSError):
                pass


class OAuthStore:
    """Read and atomically update the Cambium oauth store.

    ``path`` is injectable for tests; the CLI never passes it and always uses
    :func:`oauth_store_path`. Reads fail closed: any present-but-corrupt or
    insecure store raises ``OAuthStoreError``/``OAuthSchemaError`` instead of
    appearing empty; :meth:`repair` is the only recovery path.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = oauth_store_path() if path is None else Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def _read_locked_directory(self, directory_fd: int) -> OAuthDocument:
        try:
            try:
                fd = _open_secure_file(directory_fd, self._path.name)
            except FileNotFoundError:
                return OAuthDocument.empty()
            try:
                return parse_document(_read_fd(fd))
            finally:
                os.close(fd)
        except AuthStoreError as exc:
            raise OAuthStoreError(str(exc)) from exc

    def _open_directory_locked(self, *, create: bool) -> int:
        try:
            directory_fd = _open_directory(self._path.parent, create=create)
        except AuthStoreError as exc:
            raise OAuthStoreError(str(exc)) from exc
        if directory_fd is None:
            raise OAuthStoreError("oauth store directory is unavailable")
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
        except OSError as exc:
            os.close(directory_fd)
            raise OAuthStoreError("could not lock the oauth store") from exc
        try:
            _verify_directory_path(self._path.parent, directory_fd)
        except AuthStoreError as exc:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)
            raise OAuthStoreError(str(exc)) from exc
        return directory_fd

    def _save_records(self, directory_fd: int, records: dict[str, OAuthRecord]) -> None:
        """Write ``records`` while the caller owns the directory lock."""
        document = OAuthDocument(
            version=OAUTH_VERSION,
            records=tuple(sorted(records.values(), key=lambda r: r.doc.provider)),
        )
        _atomic_write(directory_fd, self._path.name, serialize_document(document))

    def read(self) -> OAuthDocument:
        """Read the whole store; empty when the file is absent, fail closed otherwise."""
        try:
            directory_fd = _open_directory(self._path.parent, create=False)
        except AuthStoreError as exc:
            raise OAuthStoreError(str(exc)) from exc
        if directory_fd is None:
            return OAuthDocument.empty()
        try:
            return self._read_locked_directory(directory_fd)
        finally:
            os.close(directory_fd)

    def read_provider(self, provider: str) -> OAuthRecord | None:
        provider = _validate_provider_id(provider)
        return self.read().by_provider(provider)

    def read_document(self, provider: str) -> OAuthDoc | None:
        """Return one provider's :class:`OAuthDoc`, or ``None`` when absent.

        Fail-closed like :meth:`read_provider`: a present-but-corrupt or
        insecure store raises instead of appearing empty. The supervisor and
        CLI use this for local-only reads (status, preflight); it never
        touches the network.
        """
        record = self.read_provider(provider)
        return None if record is None else record.doc

    def validate(self, provider: str) -> OAuthDoc:
        """Require one provider's :class:`OAuthDoc`; raise when absent or unreadable.

        The supervisor's fail-closed oauth preflight uses this to distinguish
        a missing session (``OAuthMissingError``) from a corrupt store
        (``OAuthSchemaError``/``OAuthStoreError``) with one local read.
        """
        provider = _validate_provider_id(provider)
        record = self.read().by_provider(provider)
        if record is None:
            raise OAuthMissingError(f"provider {provider!r} has no oauth credentials")
        return record.doc

    def providers(self) -> tuple[str, ...]:
        return tuple(record.doc.provider for record in self.read().records)

    def save_provider(self, doc: OAuthDoc, *, disabled: bool = False) -> None:
        """Atomically upsert one provider record under the directory lock."""
        if not isinstance(doc, OAuthDoc):
            raise OAuthSchemaError("oauth store document is invalid")
        if not isinstance(disabled, bool):
            raise OAuthSchemaError("oauth store disabled flag is invalid")
        directory_fd = self._open_directory_locked(create=True)
        try:
            current = self._read_locked_directory(directory_fd)
            records = {record.doc.provider: record for record in current.records}
            records[doc.provider] = OAuthRecord(doc=doc, disabled=disabled)
            self._save_records(directory_fd, records)
        finally:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)

    def _save_provider_if_current(
        self,
        doc: OAuthDoc,
        expected: OAuthRecord,
        *,
        disabled: bool = False,
    ) -> bool:
        """Save ``doc`` only when ``expected`` is still the stored record."""
        if not isinstance(doc, OAuthDoc):
            raise OAuthSchemaError("oauth store document is invalid")
        if not isinstance(expected, OAuthRecord) or expected.doc.provider != doc.provider:
            raise OAuthSchemaError("oauth store compare-and-swap record is invalid")
        if not isinstance(disabled, bool):
            raise OAuthSchemaError("oauth store disabled flag is invalid")
        if not self._path.parent.is_dir():
            return False
        directory_fd = self._open_directory_locked(create=False)
        try:
            current = self._read_locked_directory(directory_fd)
            if current.by_provider(doc.provider) != expected:
                return False
            records = {record.doc.provider: record for record in current.records}
            records[doc.provider] = OAuthRecord(doc=doc, disabled=disabled)
            self._save_records(directory_fd, records)
            return True
        finally:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)

    def _mark_disabled_if_current(self, expected: OAuthRecord) -> bool:
        """Disable ``expected`` only while it remains the stored record."""
        if not isinstance(expected, OAuthRecord):
            raise OAuthSchemaError("oauth store compare-and-swap record is invalid")
        provider = expected.doc.provider
        if not self._path.parent.is_dir():
            return False
        directory_fd = self._open_directory_locked(create=False)
        try:
            current = self._read_locked_directory(directory_fd)
            if current.by_provider(provider) != expected:
                return False
            if expected.disabled:
                return True
            records = {record.doc.provider: record for record in current.records}
            records[provider] = OAuthRecord(doc=expected.doc, disabled=True)
            self._save_records(directory_fd, records)
            return True
        finally:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)

    def mark_disabled(self, provider: str) -> None:
        """Durably disable a provider record; raises when no record exists."""
        provider = _validate_provider_id(provider)
        if not self._path.parent.is_dir():
            raise OAuthMissingError(f"provider {provider!r} has no oauth credentials to disable")
        directory_fd = self._open_directory_locked(create=False)
        try:
            current = self._read_locked_directory(directory_fd)
            record = current.by_provider(provider)
            if record is None:
                raise OAuthMissingError(
                    f"provider {provider!r} has no oauth credentials to disable"
                )
            if record.disabled:
                return
            records = {r.doc.provider: r for r in current.records}
            records[provider] = OAuthRecord(doc=record.doc, disabled=True)
            self._save_records(directory_fd, records)
        finally:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)

    def remove_provider(self, provider: str) -> bool:
        provider = _validate_provider_id(provider)
        if not self._path.parent.is_dir():
            return False
        directory_fd = self._open_directory_locked(create=False)
        try:
            current = self._read_locked_directory(directory_fd)
            if current.by_provider(provider) is None:
                return False
            records = {r.doc.provider: r for r in current.records if r.doc.provider != provider}
            self._save_records(directory_fd, records)
            return True
        finally:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)

    def repair(self) -> None:
        """The only corruption recovery: quarantine a bad file, then write an empty store.

        A present-but-unreadable store (invalid JSON, wrong schema, insecure
        metadata) is moved aside to ``oauth.json.corrupt-<hex>`` — preserving
        the bytes — and replaced with a valid empty document. A missing file
        is already an empty store and is a no-op. Repair never runs implicitly.
        """
        directory_fd = self._open_directory_locked(create=True)
        try:
            try:
                fd = _open_secure_file(directory_fd, self._path.name)
            except FileNotFoundError:
                return
            except AuthStoreError:
                fd = None
            valid = False
            if fd is not None:
                try:
                    parse_document(_read_fd(fd))
                    valid = True
                except (OAuthSchemaError, AuthStoreError):
                    pass
                finally:
                    os.close(fd)
            if valid:
                return  # Store is already valid; nothing to repair.
            quarantine = f"{self._path.name}.corrupt-{secrets.token_hex(8)}"
            try:
                os.replace(
                    self._path.name,
                    quarantine,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            except OSError as exc:
                raise OAuthStoreError("could not quarantine the corrupt oauth store") from exc
            _atomic_write(directory_fd, self._path.name, serialize_document(OAuthDocument.empty()))
        finally:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)


# --------------------------------------------------------------------------- #
# HTTP transport (urllib; no external dependencies)
# --------------------------------------------------------------------------- #


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail-closed: token endpoints must never redirect.

    urllib would otherwise replay the original request — including a bearer or
    refresh token in the body — against the redirect target.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "oauth endpoints must not redirect",
            headers,
            fp,
        )


def _opener_for_issuer(issuer: str) -> urllib.request.OpenerDirector:
    scheme = urlparse(issuer).scheme.lower()
    handlers: list[urllib.request.BaseHandler] = [_NoRedirectHandler()]
    if scheme == "http":
        # Loopback test issuers must never be routed through a proxy.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _request(
    issuer: str,
    path: str,
    *,
    payload: Mapping[str, str],
    form: bool,
    timeout_s: float,
) -> tuple[int, bytes]:
    url = f"{issuer.rstrip('/')}{path}"
    data = urlencode(payload).encode("utf-8") if form else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": ("application/x-www-form-urlencoded" if form else "application/json"),
        },
    )
    try:
        with _opener_for_issuer(issuer).open(request, timeout=timeout_s) as response:
            return response.status, _read_response(response)
    except urllib.error.HTTPError as exc:
        try:
            body = _read_response(exc)
        except OAuthError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            body = b""
        return exc.code, body
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise OAuthError("oauth request timed out") from exc
        raise OAuthError(f"oauth request failed: {reason}") from exc
    except TimeoutError as exc:
        raise OAuthError("oauth request timed out") from exc
    except (OSError, ValueError) as exc:
        raise OAuthError(f"oauth request failed: {exc}") from exc


def _read_response(response: Any) -> bytes:
    body = response.read(MAX_OAUTH_RESPONSE_BYTES + 1)
    if len(body) > MAX_OAUTH_RESPONSE_BYTES:
        raise OAuthError(f"oauth response exceeds {MAX_OAUTH_RESPONSE_BYTES} byte limit")
    return body


def _parse_json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_JSONObject)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthError("oauth endpoint returned invalid JSON") from exc
    if not isinstance(value, Mapping):
        raise OAuthError("oauth endpoint returned a non-object response")
    _check_duplicate_object(value, "oauth endpoint response")
    return dict(value)


def _parse_float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if _finite(parsed) and parsed > 0 else default


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    if not isinstance(token, str):
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (IndexError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _account_id_from_id_token(id_token: str | None) -> str | None:
    """Best-effort account id from id_token claims; never persisted raw."""
    if not id_token:
        return None
    claims = _decode_jwt_payload(id_token)
    if not claims:
        return None
    for key in ("account_id", "chatgpt_account_id"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    nested = claims.get("https://api.openai.com/auth")
    if isinstance(nested, Mapping):
        value = nested.get("chatgpt_account_id")
        if isinstance(value, str) and value:
            return value
    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        first = organizations[0]
        if isinstance(first, Mapping):
            value = first.get("id")
            if isinstance(value, str) and value:
                return value
    return None


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #


def refresh_access_token(
    issuer: str,
    client_id: str,
    refresh_token: str,
    timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
) -> RefreshedTokens:
    """Exchange a refresh token for a new access token.

    Raises ``InvalidGrantError`` when the issuer rejects the refresh token and
    ``RefreshUnavailableError`` for transient failures (429/5xx/timeout or a
    malformed issuer response) so the caller keeps the last-good document.
    """
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError("a client id is required to refresh oauth tokens")
    _validate_token(refresh_token, "refresh token")
    try:
        status, body = _request(
            validate_issuer(issuer),
            _REFRESH_ENDPOINT,
            payload={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            form=True,
            timeout_s=timeout_s,
        )
        if status != 200:
            error = _error_code(body)
            if status in (400, 401) and error == "invalid_grant":
                raise InvalidGrantError("refresh token was rejected; re-login is required")
            raise RefreshUnavailableError(f"refresh unavailable: HTTP {status}")
        payload = _parse_json_object(body)
        access_token = payload.get("access_token")
        access_token = _validate_token(access_token, "access token")
        account_id = _account_id_from_id_token(payload.get("id_token"))
        if account_id is None:
            value = payload.get("account_id")
            account_id = value if isinstance(value, str) and value else None
        refresh_token_out = payload.get("refresh_token")
        if refresh_token_out is not None:
            refresh_token_out = _validate_token(refresh_token_out, "refresh token")
        return RefreshedTokens(
            access_token=access_token,
            expires_in=_parse_float(payload.get("expires_in"), DEFAULT_EXPIRES_IN_S),
            refresh_token=refresh_token_out,
            account_id=account_id,
        )
    except (InvalidGrantError, RefreshUnavailableError):
        raise
    except OAuthError as exc:
        raise RefreshUnavailableError("refresh unavailable: malformed issuer response") from exc


def _error_code(body: bytes) -> str | None:
    try:
        value = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, Mapping):
        return None
    error = value.get("error")
    return error if isinstance(error, str) and error else None


# --------------------------------------------------------------------------- #
# Device flow
# --------------------------------------------------------------------------- #


def request_user_code(
    issuer: str,
    client_id: str,
    timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
) -> UserCode:
    """Request a device user code from the issuer."""
    if not isinstance(client_id, str) or not client_id:
        raise OAuthError("a client id is required for the device flow")
    try:
        status, body = _request(
            validate_issuer(issuer),
            _USERCODE_ENDPOINT,
            payload={"client_id": client_id},
            form=False,
            timeout_s=timeout_s,
        )
        if status != 200:
            raise DeviceFlowError(f"device code request failed with HTTP {status}")
        payload = _parse_json_object(body)
        device_auth_id = payload.get("device_auth_id")
        user_code = payload.get("user_code")
        device_auth_id = _validate_token(device_auth_id, "device auth id")
        user_code = _validate_token(user_code, "user code")
        interval = _parse_float(payload.get("interval"), 5.0)
    except DeviceFlowError:
        raise
    except OAuthError as exc:
        raise DeviceFlowError("device code request failed") from exc
    return UserCode(
        verification_url=f"{issuer.rstrip('/')}{VERIFICATION_PATH}",
        user_code=user_code,
        device_auth_id=device_auth_id,
        interval=max(1.0, interval),
    )


def poll_device_token(
    issuer: str,
    device_auth_id: str,
    user_code: str,
    timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
) -> AuthorizationCode | None:
    """Poll once; ``None`` means pending (403/404), a code means approved."""
    try:
        status, body = _request(
            validate_issuer(issuer),
            _DEVICE_TOKEN_ENDPOINT,
            payload={"device_auth_id": device_auth_id, "user_code": user_code},
            form=False,
            timeout_s=timeout_s,
        )
        if status == 200:
            payload = _parse_json_object(body)
            code = payload.get("authorization_code")
            code_verifier = payload.get("code_verifier")
            code = _validate_token(code, "authorization code")
            code_verifier = _validate_token(code_verifier, "code verifier")
            return AuthorizationCode(code=code, code_verifier=code_verifier)
        if status in (403, 404):
            return None
        raise DeviceFlowError(f"device auth failed with HTTP {status}")
    except DeviceFlowError:
        raise
    except OAuthError as exc:
        raise DeviceFlowError("device auth request failed") from exc


def exchange_code_for_tokens(
    issuer: str,
    client_id: str,
    code: str,
    code_verifier: str,
    timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
) -> tuple[str, str, float, str | None]:
    """Exchange an authorization code; returns access, refresh, expires_in, account id."""
    try:
        status, body = _request(
            validate_issuer(issuer),
            _REFRESH_ENDPOINT,
            payload={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{issuer.rstrip('/')}{CALLBACK_PATH}",
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
            form=True,
            timeout_s=timeout_s,
        )
        if status != 200:
            raise DeviceFlowError(f"token exchange failed with HTTP {status}")
        payload = _parse_json_object(body)
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        access_token = _validate_token(access_token, "access token")
        refresh_token = _validate_token(refresh_token, "refresh token")
        account_id = _account_id_from_id_token(payload.get("id_token"))
        if account_id is None:
            value = payload.get("account_id")
            account_id = value if isinstance(value, str) and value else None
    except DeviceFlowError:
        raise
    except OAuthError as exc:
        raise DeviceFlowError("token exchange failed") from exc
    return (
        access_token,
        refresh_token,
        _parse_float(payload.get("expires_in"), DEFAULT_EXPIRES_IN_S),
        account_id,
    )


class DeviceFlow:
    """Codex device flow: request user code, poll for approval, exchange, persist."""

    def __init__(
        self,
        provider: str,
        *,
        client_id: str | None = None,
        issuer: str = DEFAULT_ISSUER,
        store: OAuthStore | None = None,
        http_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
    ) -> None:
        self._provider = _validate_provider_id(provider)
        self._client_id = resolve_codex_client_id(client_id)
        self._issuer = validate_issuer(issuer)
        self._store = OAuthStore() if store is None else store
        self._http_timeout_s = http_timeout_s

    def request_user_code(self) -> UserCode:
        return request_user_code(self._issuer, self._client_id, self._http_timeout_s)

    def poll(
        self,
        code: UserCode,
        *,
        cancel: threading.Event | None = None,
        max_wait_s: float = DEFAULT_POLL_EXPIRY_S,
    ) -> AuthorizationCode:
        """Poll until approved, canceled, or the poll window expires."""
        deadline = time.monotonic() + max_wait_s
        while True:
            if cancel is not None and cancel.is_set():
                raise DeviceFlowCanceled("device flow was canceled")
            approved = poll_device_token(
                self._issuer, code.device_auth_id, code.user_code, self._http_timeout_s
            )
            remaining = deadline - time.monotonic()
            if approved is not None:
                if remaining <= 0:
                    raise DeviceFlowExpired("device code expired before approval")
                return approved
            if remaining <= 0:
                raise DeviceFlowExpired("device code expired before approval")
            time.sleep(min(code.interval, remaining))

    def exchange(self, approved: AuthorizationCode) -> OAuthDoc:
        access, refresh, expires_in, account_id = exchange_code_for_tokens(
            self._issuer,
            self._client_id,
            approved.code,
            approved.code_verifier,
            self._http_timeout_s,
        )
        return OAuthDoc(
            provider=self._provider,
            access_token=access,
            refresh_token=refresh,
            expires_at=time.time() + expires_in,
            account_id=account_id,
        )

    def run(
        self,
        *,
        cancel: threading.Event | None = None,
        max_wait_s: float = DEFAULT_POLL_EXPIRY_S,
        on_code: Callable[[str, str], None] | None = None,
    ) -> OAuthDoc:
        """Run the whole flow and persist the resulting document.

        ``on_code(verification_url, user_code)`` is invoked once with the
        transient user code so a CLI can print it to the controlling TTY; the
        module itself never logs or prints transient secrets.
        """
        code = self.request_user_code()
        if on_code is not None:
            on_code(code.verification_url, code.user_code)
        approved = self.poll(code, cancel=cancel, max_wait_s=max_wait_s)
        doc = self.exchange(approved)
        self._store.save_provider(doc)
        return doc


# --------------------------------------------------------------------------- #
# Codex CLI session import
# --------------------------------------------------------------------------- #


def import_codex_cli_session(path: str | Path | None = None) -> OAuthDoc:
    """Import the codex CLI's existing ChatGPT subscription session.

    Reads ``~/.codex/auth.json`` (or ``path``) in the real codex format:
    ``{"auth_mode": "chatgpt", "tokens": {access_token, refresh_token,
    account_id, id_token}}``. Only the access token, refresh token, account
    id, and an expiry are kept; the id_token and any email are never stored.
    The expiry is derived from the access token's ``exp`` claim, falling back
    to the id_token ``exp`` claim when the access token has no usable ``exp``.
    The returned document's ``expires_at`` is ``0.0`` when neither token
    yields a usable ``exp``, which forces a refresh on the first
    ``ensure_fresh`` call.
    """
    target = codex_cli_auth_path() if path is None else Path(path)
    try:
        raw_text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise OAuthError(f"codex cli session not found: {target}") from exc
    except OSError as exc:
        raise OAuthError(f"could not read the codex cli session: {exc}") from exc

    def _reject_constant(_value: str) -> Any:
        raise OAuthError("codex cli session contains an invalid JSON constant")

    try:
        raw = json.loads(raw_text, object_pairs_hook=_JSONObject, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise OAuthError("codex cli session is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise OAuthError("codex cli session must be a JSON object")
    _check_duplicate_object(raw, "codex cli session")
    if raw.get("auth_mode") != "chatgpt":
        raise OAuthError("codex cli session is not a ChatGPT subscription session")
    tokens = raw.get("tokens")
    if not isinstance(tokens, Mapping):
        raise OAuthError("codex cli session has no tokens")
    access_token = _validate_token(tokens.get("access_token"), "access token")
    refresh_token = _validate_token(tokens.get("refresh_token"), "refresh token")
    account_id = cast(str | None, tokens.get("account_id"))
    if account_id is not None:
        if not isinstance(account_id, str) or not account_id:
            raise OAuthError("codex cli session account_id is invalid")
        account_id = _validate_token(account_id, "account id")

    def _usable_exp(token: Any) -> float:
        """Return a valid ``exp`` epoch-seconds claim from a JWT, else 0.0."""
        if not isinstance(token, str) or not token:
            return 0.0
        claims = _decode_jwt_payload(token)
        if claims is None:
            return 0.0
        exp = claims.get("exp")
        if isinstance(exp, int | float) and not isinstance(exp, bool) and _finite(exp):
            return float(exp)
        return 0.0

    expires_at = _usable_exp(access_token) or _usable_exp(tokens.get("id_token"))
    return OAuthDoc(
        provider="codex",
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        account_id=account_id,
    )


# --------------------------------------------------------------------------- #
# TokenManager
# --------------------------------------------------------------------------- #


def _is_fresh(doc: OAuthDoc, *, now: float, margin_s: float) -> bool:
    return doc.expires_at - now > margin_s


def _validate_lock_file_stat(value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise OAuthStoreError("oauth lock file is not a regular file")
    if stat.S_IMODE(value.st_mode) != AUTH_FILE_MODE:
        raise OAuthStoreError("oauth lock file permissions are invalid")


class TokenManager:
    """Per-provider refresh manager with a persistent flock'd refresh transaction.

    ``ensure_fresh`` returns the current access token when it is fresh, and
    otherwise refreshes under a persistent per-provider lock file (created
    once, never deleted) so concurrent processes rotate the refresh token
    exactly once. Refresh failures that are transient (429/5xx/timeout) leave
    the last-good document intact and raise ``RefreshUnavailableError``; a
    rejected refresh token disables the provider until re-login.
    """

    def __init__(
        self,
        provider: str,
        store: OAuthStore | None = None,
        *,
        client_id: str | None = None,
        issuer: str = DEFAULT_ISSUER,
        refresh_timeout_s: float = DEFAULT_HTTP_TIMEOUT_S,
        lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
        refresh: Callable[[str], RefreshedTokens] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._provider = _validate_provider_id(provider)
        self._store = OAuthStore() if store is None else store
        # Empty environment overrides are absence, not a request to disable
        # the trusted public client id.
        self._client_id = resolve_codex_client_id(client_id)
        self._issuer = validate_issuer(issuer)
        self._refresh_timeout_s = refresh_timeout_s
        self._lock_timeout_s = lock_timeout_s
        self._clock = time.time if clock is None else clock
        if refresh is not None:
            self._refresh = refresh
        else:
            self._refresh = lambda refresh_token: refresh_access_token(
                self._issuer, self._client_id, refresh_token, self._refresh_timeout_s
            )

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def store(self) -> OAuthStore:
        return self._store

    def disabled(self, provider: str | None = None) -> bool:
        """Return whether the provider's stored session is disabled.

        A disabled record means the refresh token was rejected and re-login is
        required. A missing session counts as disabled (fail closed); the
        check is one local store read and never touches the network.
        """
        target = self._provider if provider is None else _validate_provider_id(provider)
        record = self._store.read_provider(target)
        return record is None or record.disabled

    def _lock_path(self) -> Path:
        return self._store.path.parent / f"{_LOCK_FILE_PREFIX}{self._provider}{_LOCK_FILE_SUFFIX}"

    def _acquire_lock(self) -> int:
        """Open the persistent lock file and flock it exclusively, with a timeout.

        The lock file lives next to the store, is created on first use, and is
        never deleted or recreated afterwards.
        """
        try:
            directory_fd = _open_directory(self._store.path.parent, create=True)
        except AuthStoreError as exc:
            raise OAuthStoreError(str(exc)) from exc
        if directory_fd is None:
            raise OAuthStoreError("oauth store directory is unavailable")
        try:
            flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
            try:
                lock_fd = os.open(
                    self._lock_path().name, flags, AUTH_FILE_MODE, dir_fd=directory_fd
                )
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise OAuthStoreError("oauth lock file must not be a symlink") from exc
                raise OAuthStoreError("could not open the oauth lock file") from exc
        finally:
            os.close(directory_fd)
        try:
            _validate_lock_file_stat(os.fstat(lock_fd))
        except OAuthStoreError:
            os.close(lock_fd)
            raise
        deadline = time.monotonic() + self._lock_timeout_s
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_fd
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    os.close(lock_fd)
                    raise OAuthStoreError("could not lock the oauth refresh transaction") from exc
                if time.monotonic() >= deadline:
                    os.close(lock_fd)
                    raise LockTimeoutError(
                        f"could not acquire the oauth refresh lock for provider {self._provider!r}"
                    ) from exc
                time.sleep(0.05)

    def _release_lock(self, lock_fd: int) -> None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)

    def ensure_fresh(self, rejected: str | None = None) -> tuple[str, str | None]:
        """Return ``(access_token, account_id)``, refreshing under the provider lock.

        ``rejected`` is an access token the caller observed being refused; a
        stored access token that differs from it is a concurrent rotation and
        is used without a refresh.
        """
        fast = self._store.read_provider(self._provider)
        if fast is None:
            raise OAuthMissingError(f"provider {self._provider!r} has no oauth credentials")
        if fast.disabled:
            raise InvalidGrantError(f"provider {self._provider!r} is disabled until re-login")
        if _is_fresh(fast.doc, now=self._clock(), margin_s=DEFAULT_REFRESH_MARGIN_S) and (
            rejected is None or rejected != fast.doc.access_token
        ):
            return fast.doc.access_token, fast.doc.account_id

        lock_fd = self._acquire_lock()
        try:
            current = self._store.read_provider(self._provider)
            if current is None:
                raise OAuthMissingError(f"provider {self._provider!r} has no oauth credentials")
            if current.disabled:
                raise InvalidGrantError(f"provider {self._provider!r} is disabled until re-login")
            if rejected is not None and current.doc.access_token != rejected:
                return current.doc.access_token, current.doc.account_id
            if rejected is None and _is_fresh(
                current.doc, now=self._clock(), margin_s=DEFAULT_REFRESH_MARGIN_S
            ):
                return current.doc.access_token, current.doc.account_id
            try:
                refreshed = self._refresh(current.doc.refresh_token)
            except InvalidGrantError:
                if self._store._mark_disabled_if_current(current):
                    raise
                latest = self._store.read_provider(self._provider)
                if latest is None:
                    raise OAuthMissingError(
                        f"provider {self._provider!r} has no oauth credentials"
                    ) from None
                if latest.disabled:
                    raise InvalidGrantError(
                        f"provider {self._provider!r} is disabled until re-login"
                    ) from None
                return latest.doc.access_token, latest.doc.account_id
            refreshed_refresh = refreshed.refresh_token or current.doc.refresh_token
            account_id = (
                refreshed.account_id if refreshed.account_id is not None else current.doc.account_id
            )
            updated = OAuthDoc(
                provider=self._provider,
                access_token=refreshed.access_token,
                refresh_token=refreshed_refresh,
                expires_at=self._clock() + refreshed.expires_in,
                account_id=account_id,
            )
            if self._store._save_provider_if_current(updated, current):
                return updated.access_token, updated.account_id
            latest = self._store.read_provider(self._provider)
            if latest is None:
                raise OAuthMissingError(f"provider {self._provider!r} has no oauth credentials")
            if latest.disabled:
                raise InvalidGrantError(f"provider {self._provider!r} is disabled until re-login")
            return latest.doc.access_token, latest.doc.account_id
        finally:
            self._release_lock(lock_fd)

    def mark_invalid_grant(self) -> None:
        """Disable the provider under the same lock until a re-login replaces it."""
        lock_fd = self._acquire_lock()
        try:
            current = self._store.read_provider(self._provider)
            if current is None:
                raise OAuthMissingError(f"provider {self._provider!r} has no oauth credentials")
            self._store._mark_disabled_if_current(current)
        finally:
            self._release_lock(lock_fd)


__all__ = [
    "OAUTH_VERSION",
    "OAUTH_FILE_NAME",
    "MAX_OAUTH_DOC_BYTES",
    "MAX_TOKEN_BYTES",
    "DEFAULT_ISSUER",
    "DEFAULT_EXPIRES_IN_S",
    "DEFAULT_REFRESH_MARGIN_S",
    "DEFAULT_POLL_EXPIRY_S",
    "DEFAULT_HTTP_TIMEOUT_S",
    "AuthorizationCode",
    "DeviceFlow",
    "DeviceFlowCanceled",
    "DeviceFlowError",
    "DeviceFlowExpired",
    "InvalidGrantError",
    "LockTimeoutError",
    "OAuthDoc",
    "OAuthDocument",
    "OAuthError",
    "OAuthMissingError",
    "OAuthRecord",
    "OAuthSchemaError",
    "OAuthStore",
    "OAuthStoreError",
    "RefreshedTokens",
    "RefreshUnavailableError",
    "TokenManager",
    "UserCode",
    "codex_cli_auth_path",
    "exchange_code_for_tokens",
    "import_codex_cli_session",
    "oauth_store_path",
    "parse_document",
    "poll_device_token",
    "refresh_access_token",
    "request_user_code",
    "resolve_codex_client_id",
    "serialize_document",
    "validate_issuer",
]
