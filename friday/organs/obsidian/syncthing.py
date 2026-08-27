"""Managed, tenant-isolated Syncthing boundary for the Obsidian organ.

The module deliberately owns both sides of the local trust boundary:

* REST requests can only use a literal loopback address or an owner-private
  Unix-domain socket and always carry Syncthing's API key;
* each authenticated actor gets a hashed filesystem namespace, independent
  config and index roots, and a separately supervised process.

It uses only the standard library.  Process and HTTP adapters are injectable so
the integration can be exercised without a Syncthing binary or live network.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import socket
import stat
import subprocess  # nosec B404 - fixed argv, no shell
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_OWNER_KEY_BYTES = 512
MAX_CONFIG_BYTES = 2 * 1024 * 1024
RELAY_LISTEN_ADDRESSES = ("dynamic+https://relays.syncthing.net/endpoint",)
_DEVICE_ID_PARTS = 8
_DEVICE_ID_PART_LENGTH = 7
_DEVICE_ID_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
_ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH", "DELETE"})


class SyncthingError(Exception):
    """Base class for stable Syncthing adapter failures."""


class SyncthingConfigurationError(SyncthingError, ValueError):
    """The local profile or a caller-provided configuration is invalid."""


class SyncthingSecurityError(SyncthingError, PermissionError):
    """A local transport, directory, or secret file is not private."""


class SyncthingTransportError(SyncthingError, ConnectionError):
    """The local REST endpoint could not be reached."""


class SyncthingTimeoutError(SyncthingTransportError, TimeoutError):
    """A REST request exceeded its explicit deadline."""


class SyncthingProtocolError(SyncthingError):
    """Syncthing returned a response outside the supported REST contract."""


class SyncthingResponseTooLargeError(SyncthingProtocolError):
    """A response exceeded the configured in-memory budget."""


class SyncthingHTTPError(SyncthingError):
    """Syncthing returned a non-successful HTTP status."""

    def __init__(self, status: int, method: str, target: str) -> None:
        self.status = status
        self.method = method
        self.target = target
        super().__init__(f"Syncthing REST {method} {target} returned HTTP {status}")


class SyncthingProcessError(SyncthingError):
    """A managed Syncthing process could not be provisioned or supervised."""


class SyncthingProcessExitedError(SyncthingProcessError):
    """The managed process exited before its REST endpoint became ready."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        super().__init__(f"Syncthing exited before readiness (status {returncode})")


class SyncthingReadinessTimeoutError(SyncthingProcessError, TimeoutError):
    """The managed process did not become REST-ready in time."""


class SyncthingProfileLimitError(SyncthingProcessError):
    """The bounded process manager has reached its configured capacity."""


class SyncthingConnectivityPolicyError(SyncthingProtocolError):
    """Syncthing did not retain the discovery-and-relay configuration."""


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class SyncthingTransport(Protocol):
    """Minimal local HTTP transport used by :class:`SyncthingRestClient`."""

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> TransportResponse: ...


def _validate_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise SyncthingConfigurationError("timeout must be a positive number")
    return float(timeout)


def _validate_response_budget(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SyncthingConfigurationError("max_response_bytes must be a positive integer")
    return value


def _validate_rest_target(target: str) -> str:
    if not isinstance(target, str) or not target.startswith("/rest/"):
        raise SyncthingConfigurationError("REST target must start with /rest/")
    if len(target) > 4096 or "\\" in target or any(ord(character) < 32 for character in target):
        raise SyncthingConfigurationError("invalid REST target")
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise SyncthingConfigurationError("REST target must be origin-relative")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise SyncthingConfigurationError("REST target must not contain traversal segments")
    return target


def _validate_headers(headers: Mapping[str, str]) -> None:
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise SyncthingConfigurationError("HTTP headers must be strings")
        if not name or any(character in name for character in "\r\n:"):
            raise SyncthingConfigurationError("invalid HTTP header name")
        if any(character in value for character in "\r\n\x00"):
            raise SyncthingConfigurationError("invalid HTTP header value")


def _read_http_response(
    response: http.client.HTTPResponse,
    *,
    max_response_bytes: int,
) -> TransportResponse:
    raw_length = response.getheader("Content-Length")
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as exc:
            raise SyncthingProtocolError("invalid Content-Length from Syncthing") from exc
        if content_length < 0:
            raise SyncthingProtocolError("negative Content-Length from Syncthing")
        if content_length > max_response_bytes:
            raise SyncthingResponseTooLargeError("Syncthing REST response is too large")
    body = response.read(max_response_bytes + 1)
    if len(body) > max_response_bytes:
        raise SyncthingResponseTooLargeError("Syncthing REST response is too large")
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    return TransportResponse(status=response.status, headers=response_headers, body=body)


class LoopbackHTTPTransport:
    """Direct HTTP transport that refuses DNS names, proxies and non-loopback IPs."""

    def __init__(self, endpoint: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            host = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as exc:
            raise SyncthingConfigurationError("invalid loopback REST endpoint") from exc
        if (
            parsed.scheme != "http"
            or host is None
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise SyncthingConfigurationError("REST endpoint must be a plain loopback HTTP origin")
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise SyncthingSecurityError("REST endpoint must use a literal loopback IP") from exc
        if not address.is_loopback:
            raise SyncthingSecurityError("REST endpoint must use a loopback IP")
        self._host = host
        self._port = port

    @property
    def endpoint(self) -> str:
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"http://{host}:{self._port}"

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        method = method.upper()
        if method not in _ALLOWED_METHODS:
            raise SyncthingConfigurationError("unsupported REST method")
        target = _validate_rest_target(target)
        _validate_headers(headers)
        timeout = _validate_timeout(timeout)
        max_response_bytes = _validate_response_budget(max_response_bytes)
        connection = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            return _read_http_response(response, max_response_bytes=max_response_bytes)
        except TimeoutError as exc:
            raise SyncthingTimeoutError("Syncthing REST request timed out") from exc
        except http.client.HTTPException as exc:
            raise SyncthingProtocolError("invalid HTTP response from Syncthing") from exc
        except OSError as exc:
            raise SyncthingTransportError("could not reach local Syncthing REST endpoint") from exc
        finally:
            connection.close()


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, *, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(os.fspath(self._socket_path))
        except BaseException:
            connection.close()
            raise
        self.sock = connection


def _validate_private_directory(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SyncthingSecurityError(f"private directory is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SyncthingSecurityError(f"private directory is not a real directory: {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise SyncthingSecurityError(f"private directory has another owner: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SyncthingSecurityError(f"private directory is accessible to other users: {path}")
    return info


def _validate_private_socket(path: Path) -> None:
    _validate_private_directory(path.parent)
    try:
        info = path.lstat()
    except OSError as exc:
        raise SyncthingTransportError("Syncthing Unix socket is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise SyncthingSecurityError("Syncthing Unix endpoint is not a socket")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise SyncthingSecurityError("Syncthing Unix socket has another owner")


class UnixSocketTransport:
    """HTTP-over-Unix-socket transport guarded by an owner-private directory."""

    def __init__(self, socket_path: str | os.PathLike[str]) -> None:
        path = Path(socket_path)
        if not path.is_absolute() or len(os.fsencode(path)) > 107:
            raise SyncthingConfigurationError("Unix socket path must be absolute and fit sockaddr_un")
        self._socket_path = path

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def request(
        self,
        method: str,
        target: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        method = method.upper()
        if method not in _ALLOWED_METHODS:
            raise SyncthingConfigurationError("unsupported REST method")
        target = _validate_rest_target(target)
        _validate_headers(headers)
        timeout = _validate_timeout(timeout)
        max_response_bytes = _validate_response_budget(max_response_bytes)
        _validate_private_socket(self._socket_path)
        connection = _UnixHTTPConnection(self._socket_path, timeout=timeout)
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            return _read_http_response(response, max_response_bytes=max_response_bytes)
        except TimeoutError as exc:
            raise SyncthingTimeoutError("Syncthing Unix REST request timed out") from exc
        except http.client.HTTPException as exc:
            raise SyncthingProtocolError("invalid HTTP response from Syncthing") from exc
        except OSError as exc:
            raise SyncthingTransportError("could not reach Syncthing Unix REST endpoint") from exc
        finally:
            connection.close()


def validate_device_id(value: object) -> str:
    """Validate Syncthing's canonical shape, alphabet and four Luhn-32 digits."""

    if not isinstance(value, str):
        raise SyncthingProtocolError("Syncthing Device ID must be a string")
    parts = value.split("-")
    if len(parts) != _DEVICE_ID_PARTS or any(len(part) != _DEVICE_ID_PART_LENGTH for part in parts):
        raise SyncthingProtocolError("Syncthing Device ID has an invalid shape")
    if any(character not in _DEVICE_ID_ALPHABET for part in parts for character in part):
        raise SyncthingProtocolError("Syncthing Device ID uses invalid characters")
    compact = "".join(parts)
    for index in range(4):
        group = compact[index * 14 : index * 14 + 13]
        if compact[index * 14 + 13] != _luhn32(group):
            raise SyncthingProtocolError("Syncthing Device ID has an invalid check digit")
    return value


def _luhn32(value: str) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    factor = 1
    checksum = 0
    for character in value:
        codepoint = alphabet.index(character)
        addend = factor * codepoint
        factor = 1 if factor == 2 else 2
        checksum += addend // 32 + addend % 32
    return alphabet[(32 - checksum % 32) % 32]


def _required_text(value: object, *, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SyncthingProtocolError(f"Syncthing field {field} must be a bounded non-empty string")
    if any(ord(character) < 32 and character not in "\t" for character in value):
        raise SyncthingProtocolError(f"Syncthing field {field} contains control characters")
    return value


def _optional_text(value: object, *, field: str, maximum: int = 4096) -> str | None:
    if value is None or value == "":
        return None
    return _required_text(value, field=field, maximum=maximum)


def _integer(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SyncthingProtocolError(f"Syncthing field {field} must be an integer >= {minimum}")
    return value


def _boolean(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise SyncthingProtocolError(f"Syncthing field {field} must be a boolean")
    return value


def _number(value: object, *, field: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SyncthingProtocolError(f"Syncthing field {field} must be numeric")
    result = float(value)
    if not minimum <= result < float("inf"):
        raise SyncthingProtocolError(f"Syncthing field {field} is outside its supported range")
    return result


@dataclass(frozen=True, slots=True)
class SyncthingSystemStatus:
    server_device_id: str
    gui_address_used: str | None
    uptime_seconds: int | None


@dataclass(frozen=True, slots=True)
class SyncthingVersion:
    version: str
    long_version: str | None
    operating_system: str | None
    architecture: str | None


@dataclass(frozen=True, slots=True)
class DeviceConnection:
    device_id: str
    connected: bool
    paused: bool
    address: str | None
    connection_type: str | None
    client_version: str | None
    connected_at: str | None
    started_at: str | None
    is_local: bool | None
    in_bytes_total: int
    out_bytes_total: int

    @property
    def via_relay(self) -> bool:
        return bool(self.connection_type and "relay" in self.connection_type.casefold())


@dataclass(frozen=True, slots=True)
class PendingDevice:
    device_id: str
    name: str | None
    address: str | None
    discovered_at: str | None


@dataclass(frozen=True, slots=True)
class ConfiguredDevice:
    device_id: str
    name: str
    addresses: tuple[str, ...]
    paused: bool
    auto_accept_folders: bool = False
    introducer: bool = False


@dataclass(frozen=True, slots=True)
class ConfiguredFolder:
    folder_id: str
    label: str
    path: str
    device_ids: tuple[str, ...]
    paused: bool
    folder_type: str | None
    versioning_type: str | None = None
    versioning_params: tuple[tuple[str, str], ...] = ()
    versioning_cleanup_interval_s: int | None = None
    versioning_fs_path: str = ""
    versioning_fs_type: str | None = None


@dataclass(frozen=True, slots=True)
class FolderStatus:
    folder_id: str
    state: str
    state_changed: str | None
    error: str | None
    global_files: int
    local_files: int
    need_files: int
    need_bytes: int


@dataclass(frozen=True, slots=True)
class RemoteCompletion:
    folder_id: str
    device_id: str
    completion_percent: float
    need_bytes: int
    need_items: int
    global_bytes: int | None
    global_items: int | None
    remote_state: str | None

    @property
    def is_complete(self) -> bool:
        return self.completion_percent == 100.0 and self.need_bytes == 0 and self.need_items == 0


@dataclass(frozen=True, slots=True)
class SyncthingFileInfo:
    name: str
    size_bytes: int
    modified_at: str | None
    deleted: bool
    invalid: bool
    ignored: bool
    must_rescan: bool
    no_permissions: bool
    file_type: str
    version: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FileAvailability:
    device_id: str
    from_temporary: bool


@dataclass(frozen=True, slots=True)
class SyncthingFileStatus:
    folder_id: str
    file_path: str
    local: SyncthingFileInfo | None
    global_file: SyncthingFileInfo | None
    availability: tuple[FileAvailability, ...]

    @property
    def local_matches_global(self) -> bool:
        if self.local is None or self.global_file is None:
            return False
        return (
            self.local.version == self.global_file.version
            and self.local.size_bytes == self.global_file.size_bytes
            and self.local.deleted == self.global_file.deleted
            and not self.local.deleted
            and not self.local.invalid
            and not self.global_file.invalid
            and not self.local.ignored
            and not self.global_file.ignored
            and not self.local.must_rescan
            and not self.global_file.must_rescan
            and not self.local.no_permissions
            and not self.global_file.no_permissions
        )

    def available_on(self, device_id: str, *, include_temporary: bool = False) -> bool:
        canonical = validate_device_id(device_id)
        return any(
            item.device_id == canonical and (include_temporary or not item.from_temporary)
            for item in self.availability
        )


@dataclass(frozen=True, slots=True)
class SyncthingOptions:
    listen_addresses: tuple[str, ...]
    local_announce_enabled: bool
    global_announce_enabled: bool
    relays_enabled: bool

    @property
    def is_discovery_relay(self) -> bool:
        return (
            self.listen_addresses == RELAY_LISTEN_ADDRESSES
            and not self.local_announce_enabled
            and self.global_announce_enabled
            and self.relays_enabled
        )


@dataclass(frozen=True, slots=True)
class SyncthingOptionsPatch:
    listen_addresses: tuple[str, ...] | None = None
    local_announce_enabled: bool | None = None
    global_announce_enabled: bool | None = None
    relays_enabled: bool | None = None

    @classmethod
    def discovery_relay(cls) -> SyncthingOptionsPatch:
        return cls(
            listen_addresses=RELAY_LISTEN_ADDRESSES,
            local_announce_enabled=False,
            global_announce_enabled=True,
            relays_enabled=True,
        )

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.listen_addresses is not None:
            payload["listenAddresses"] = list(_listen_addresses(self.listen_addresses))
        if self.local_announce_enabled is not None:
            payload["localAnnounceEnabled"] = _configuration_bool(
                self.local_announce_enabled, field="localAnnounceEnabled"
            )
        if self.global_announce_enabled is not None:
            payload["globalAnnounceEnabled"] = _configuration_bool(
                self.global_announce_enabled, field="globalAnnounceEnabled"
            )
        if self.relays_enabled is not None:
            payload["relaysEnabled"] = _configuration_bool(self.relays_enabled, field="relaysEnabled")
        if not payload:
            raise SyncthingConfigurationError("options patch must not be empty")
        return payload


@dataclass(frozen=True, slots=True)
class DiscoveryRelayConfiguration:
    options: SyncthingOptions
    restart_required: bool


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SyncthingProtocolError(f"duplicate JSON key from Syncthing: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise SyncthingProtocolError(f"non-finite JSON value from Syncthing: {value}")


def _decode_json(body: bytes, *, target: str) -> object:
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SyncthingProtocolError(f"non-UTF-8 JSON from Syncthing at {target}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except SyncthingProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SyncthingProtocolError(f"invalid JSON from Syncthing at {target}") from exc


def _content_type(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "content-type":
            return value.partition(";")[0].strip().lower()
    return None


def _query(target: str, **parameters: str | None) -> str:
    encoded = urllib.parse.urlencode(
        [(name, value) for name, value in parameters.items() if value is not None]
    )
    return f"{target}?{encoded}" if encoded else target


def _identifier(value: object, *, field: str, maximum: int = 128) -> str:
    result = _required_text(value, field=field, maximum=maximum)
    if result in {".", ".."} or any(character in result for character in "/\\?#"):
        raise SyncthingConfigurationError(f"{field} is not a safe REST identifier")
    return result


def _object_payload(payload: Mapping[str, object], *, label: str) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise SyncthingConfigurationError(f"{label} must be a JSON object")
    normalized = dict(payload)
    if any(not isinstance(key, str) for key in normalized):
        raise SyncthingConfigurationError(f"{label} keys must be strings")
    if not normalized:
        raise SyncthingConfigurationError(f"{label} must not be empty")
    return normalized


def _configuration_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise SyncthingConfigurationError(f"{field} must be a boolean")
    return value


def _listen_addresses(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SyncthingConfigurationError("listenAddresses must be a non-empty array of strings")
    addresses = tuple(value)
    if not addresses or len(addresses) > 16:
        raise SyncthingConfigurationError("listenAddresses must contain between 1 and 16 addresses")
    normalized: list[str] = []
    for address in addresses:
        if not isinstance(address, str) or not address or len(address) > 1024:
            raise SyncthingConfigurationError("listenAddresses entries must be bounded strings")
        if any(ord(character) < 33 for character in address):
            raise SyncthingConfigurationError("listenAddresses entries contain unsafe whitespace")
        normalized.append(address)
    return tuple(normalized)


def _relative_syncthing_path(value: object, *, field: str) -> str:
    result = _required_text(value, field=field, maximum=4096)
    if result.startswith(("/", "\\")) or "\\" in result:
        raise SyncthingConfigurationError(f"{field} must be relative and slash-separated")
    if any(part in {"", ".", ".."} for part in result.split("/")):
        raise SyncthingConfigurationError(f"{field} contains an unsafe segment")
    return result


def _parse_file_info(value: object, *, field: str) -> SyncthingFileInfo | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SyncthingProtocolError(f"Syncthing field {field} must be an object or null")
    raw_version = value.get("version", [])
    if not isinstance(raw_version, list):
        raise SyncthingProtocolError(f"Syncthing field {field}.version must be an array")
    version = tuple(_required_text(item, field=f"{field}.version", maximum=256) for item in raw_version)
    raw_type = value.get("type", "")
    if isinstance(raw_type, bool) or not isinstance(raw_type, (str, int)):
        raise SyncthingProtocolError(f"Syncthing field {field}.type must be a string or integer")
    return SyncthingFileInfo(
        name=_required_text(value.get("name"), field=f"{field}.name", maximum=4096),
        size_bytes=_integer(value.get("size", 0), field=f"{field}.size"),
        modified_at=_optional_text(value.get("modified"), field=f"{field}.modified", maximum=128),
        deleted=_boolean(value.get("deleted", False), field=f"{field}.deleted"),
        invalid=_boolean(value.get("invalid", False), field=f"{field}.invalid"),
        ignored=_boolean(value.get("ignored", False), field=f"{field}.ignored"),
        must_rescan=_boolean(value.get("mustRescan", False), field=f"{field}.mustRescan"),
        no_permissions=_boolean(value.get("noPermissions", False), field=f"{field}.noPermissions"),
        file_type=str(raw_type),
        version=version,
    )


class SyncthingRestClient:
    """Typed subset of the Syncthing REST API used by the first release."""

    def __init__(
        self,
        transport: SyncthingTransport,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._transport = transport
        self._api_key = _validate_api_key(api_key)
        self._timeout = _validate_timeout(timeout)
        self._max_response_bytes = _validate_response_budget(max_response_bytes)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(api_key=<redacted>)"

    def _request(
        self,
        method: str,
        target: str,
        *,
        payload: Mapping[str, object] | None = None,
        allow_empty: bool = False,
    ) -> object | None:
        target = _validate_rest_target(target)
        headers = {"Accept": "application/json", "X-API-Key": self._api_key}
        body: bytes | None = None
        if payload is not None:
            try:
                body = json.dumps(
                    payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                ).encode()
            except (TypeError, ValueError, UnicodeEncodeError) as exc:
                raise SyncthingConfigurationError("REST payload is not finite UTF-8 JSON") from exc
            if len(body) > MAX_REQUEST_BYTES:
                raise SyncthingConfigurationError("REST payload is too large")
            headers["Content-Type"] = "application/json"
        response = self._transport.request(
            method,
            target,
            headers=headers,
            body=body,
            timeout=self._timeout,
            max_response_bytes=self._max_response_bytes,
        )
        if not isinstance(response, TransportResponse):
            raise SyncthingProtocolError("transport returned an invalid response object")
        if len(response.body) > self._max_response_bytes:
            raise SyncthingResponseTooLargeError("Syncthing REST response is too large")
        if response.status < 200 or response.status >= 300:
            raise SyncthingHTTPError(response.status, method, target)
        if not response.body:
            if allow_empty:
                return None
            raise SyncthingProtocolError(f"empty JSON response from Syncthing at {target}")
        media_type = _content_type(response.headers)
        if media_type is not None and media_type != "application/json" and not media_type.endswith("+json"):
            raise SyncthingProtocolError(f"non-JSON Content-Type from Syncthing at {target}")
        return _decode_json(response.body, target=target)

    def _object(
        self,
        method: str,
        target: str,
        *,
        payload: Mapping[str, object] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, object]:
        result = self._request(method, target, payload=payload, allow_empty=allow_empty)
        if not isinstance(result, dict):
            raise SyncthingProtocolError(f"expected JSON object from Syncthing at {target}")
        return result

    def _array(
        self,
        method: str,
        target: str,
        *,
        payload: Mapping[str, object] | None = None,
        allow_empty: bool = False,
    ) -> list[object]:
        result = self._request(method, target, payload=payload, allow_empty=allow_empty)
        if not isinstance(result, list):
            raise SyncthingProtocolError(f"expected JSON array from Syncthing at {target}")
        return result

    def system_status(self) -> SyncthingSystemStatus:
        payload = self._object("GET", "/rest/system/status")
        uptime = payload.get("uptime")
        return SyncthingSystemStatus(
            server_device_id=validate_device_id(payload.get("myID")),
            gui_address_used=_optional_text(payload.get("guiAddressUsed"), field="guiAddressUsed"),
            uptime_seconds=None if uptime is None else _integer(uptime, field="uptime"),
        )

    def system_version(self) -> SyncthingVersion:
        payload = self._object("GET", "/rest/system/version")
        return SyncthingVersion(
            version=_required_text(payload.get("version"), field="version", maximum=128),
            long_version=_optional_text(payload.get("longVersion"), field="longVersion", maximum=1024),
            operating_system=_optional_text(payload.get("os"), field="os", maximum=128),
            architecture=_optional_text(payload.get("arch"), field="arch", maximum=128),
        )

    def connections(self) -> tuple[DeviceConnection, ...]:
        payload = self._object("GET", "/rest/system/connections")
        raw_connections = payload.get("connections")
        if not isinstance(raw_connections, dict):
            raise SyncthingProtocolError("Syncthing connections must be a JSON object")
        connections: list[DeviceConnection] = []
        for raw_device_id, raw_connection in raw_connections.items():
            device_id = validate_device_id(raw_device_id)
            if not isinstance(raw_connection, dict):
                raise SyncthingProtocolError("Syncthing connection details must be a JSON object")
            raw_is_local = raw_connection.get("isLocal")
            connections.append(
                DeviceConnection(
                    device_id=device_id,
                    connected=_boolean(raw_connection.get("connected", False), field="connected"),
                    paused=_boolean(raw_connection.get("paused", False), field="paused"),
                    address=_optional_text(raw_connection.get("address"), field="address", maximum=1024),
                    connection_type=_optional_text(
                        raw_connection.get("type"), field="connection.type", maximum=128
                    ),
                    client_version=_optional_text(
                        raw_connection.get("clientVersion"), field="clientVersion", maximum=256
                    ),
                    connected_at=_optional_text(raw_connection.get("at"), field="connection.at", maximum=128),
                    started_at=_optional_text(
                        raw_connection.get("startedAt"), field="connection.startedAt", maximum=128
                    ),
                    is_local=None
                    if raw_is_local is None
                    else _boolean(raw_is_local, field="connection.isLocal"),
                    in_bytes_total=_integer(
                        raw_connection.get("inBytesTotal", 0), field="connection.inBytesTotal"
                    ),
                    out_bytes_total=_integer(
                        raw_connection.get("outBytesTotal", 0), field="connection.outBytesTotal"
                    ),
                )
            )
        return tuple(sorted(connections, key=lambda item: item.device_id))

    def list_pending_devices(self) -> tuple[PendingDevice, ...]:
        payload = self._object("GET", "/rest/cluster/pending/devices")
        pending: list[PendingDevice] = []
        for raw_device_id, details in payload.items():
            device_id = validate_device_id(raw_device_id)
            if not isinstance(details, dict):
                raise SyncthingProtocolError("pending device details must be a JSON object")
            pending.append(
                PendingDevice(
                    device_id=device_id,
                    name=_optional_text(details.get("name"), field="pending.name", maximum=256),
                    address=_optional_text(details.get("address"), field="pending.address", maximum=1024),
                    discovered_at=_optional_text(details.get("time"), field="pending.time", maximum=128),
                )
            )
        return tuple(sorted(pending, key=lambda item: item.device_id))

    def delete_pending_device(self, device_id: str) -> None:
        device_id = validate_device_id(device_id)
        self._request(
            "DELETE",
            _query("/rest/cluster/pending/devices", device=device_id),
            allow_empty=True,
        )

    def list_devices(self) -> tuple[ConfiguredDevice, ...]:
        return tuple(self._parse_device(item) for item in self._array("GET", "/rest/config/devices"))

    def get_device(self, device_id: str) -> ConfiguredDevice:
        device_id = validate_device_id(device_id)
        return self._parse_device(self._object("GET", f"/rest/config/devices/{device_id}"))

    def post_device(self, configuration: Mapping[str, object]) -> None:
        payload = _object_payload(configuration, label="device configuration")
        validate_device_id(payload.get("deviceID"))
        self._request("POST", "/rest/config/devices", payload=payload, allow_empty=True)

    def patch_device(self, device_id: str, changes: Mapping[str, object]) -> None:
        device_id = validate_device_id(device_id)
        payload = _object_payload(changes, label="device patch")
        if "deviceID" in payload and validate_device_id(payload["deviceID"]) != device_id:
            raise SyncthingConfigurationError("device patch cannot change deviceID")
        self._request("PATCH", f"/rest/config/devices/{device_id}", payload=payload, allow_empty=True)

    def delete_device(self, device_id: str) -> None:
        device_id = validate_device_id(device_id)
        self._request("DELETE", f"/rest/config/devices/{device_id}", allow_empty=True)

    def list_folders(self) -> tuple[ConfiguredFolder, ...]:
        return tuple(self._parse_folder(item) for item in self._array("GET", "/rest/config/folders"))

    def get_folder(self, folder_id: str) -> ConfiguredFolder:
        folder_id = _identifier(folder_id, field="folder_id")
        encoded = urllib.parse.quote(folder_id, safe="")
        return self._parse_folder(self._object("GET", f"/rest/config/folders/{encoded}"))

    def post_folder(self, configuration: Mapping[str, object]) -> None:
        payload = _object_payload(configuration, label="folder configuration")
        _identifier(payload.get("id"), field="folder_id")
        self._request("POST", "/rest/config/folders", payload=payload, allow_empty=True)

    def patch_folder(self, folder_id: str, changes: Mapping[str, object]) -> None:
        folder_id = _identifier(folder_id, field="folder_id")
        payload = _object_payload(changes, label="folder patch")
        if "id" in payload and _identifier(payload["id"], field="folder_id") != folder_id:
            raise SyncthingConfigurationError("folder patch cannot change id")
        encoded = urllib.parse.quote(folder_id, safe="")
        self._request("PATCH", f"/rest/config/folders/{encoded}", payload=payload, allow_empty=True)

    def delete_folder(self, folder_id: str) -> None:
        folder_id = _identifier(folder_id, field="folder_id")
        encoded = urllib.parse.quote(folder_id, safe="")
        self._request("DELETE", f"/rest/config/folders/{encoded}", allow_empty=True)

    def scan_folder(self, folder_id: str, *, subpath: str | None = None) -> None:
        folder_id = _identifier(folder_id, field="folder_id")
        if subpath is not None:
            subpath = _relative_syncthing_path(subpath, field="subpath")
        self._request("POST", _query("/rest/db/scan", folder=folder_id, sub=subpath), allow_empty=True)

    def folder_status(self, folder_id: str) -> FolderStatus:
        folder_id = _identifier(folder_id, field="folder_id")
        payload = self._object("GET", _query("/rest/db/status", folder=folder_id))
        return FolderStatus(
            folder_id=folder_id,
            state=_required_text(payload.get("state"), field="state", maximum=128),
            state_changed=_optional_text(payload.get("stateChanged"), field="stateChanged", maximum=128),
            error=_optional_text(payload.get("error"), field="error", maximum=4096),
            global_files=_integer(payload.get("globalFiles", 0), field="globalFiles"),
            local_files=_integer(payload.get("localFiles", 0), field="localFiles"),
            need_files=_integer(payload.get("needFiles", 0), field="needFiles"),
            need_bytes=_integer(payload.get("needBytes", 0), field="needBytes"),
        )

    def remote_completion(self, folder_id: str, device_id: str) -> RemoteCompletion:
        folder_id = _identifier(folder_id, field="folder_id")
        device_id = validate_device_id(device_id)
        payload = self._object(
            "GET",
            _query("/rest/db/completion", folder=folder_id, device=device_id),
        )
        completion = _number(payload.get("completion"), field="completion")
        if completion > 100.0:
            raise SyncthingProtocolError("Syncthing completion must not exceed 100")
        global_bytes = payload.get("globalBytes")
        global_items = payload.get("globalItems")
        return RemoteCompletion(
            folder_id=folder_id,
            device_id=device_id,
            completion_percent=completion,
            need_bytes=_integer(payload.get("needBytes", 0), field="needBytes"),
            need_items=_integer(payload.get("needItems", 0), field="needItems"),
            global_bytes=None if global_bytes is None else _integer(global_bytes, field="globalBytes"),
            global_items=None if global_items is None else _integer(global_items, field="globalItems"),
            remote_state=_optional_text(payload.get("remoteState"), field="remoteState", maximum=128),
        )

    def file_status(self, folder_id: str, file_path: str) -> SyncthingFileStatus:
        """Return local/global versions and devices that advertise this exact file."""

        folder_id = _identifier(folder_id, field="folder_id")
        file_path = _relative_syncthing_path(file_path, field="file")
        payload = self._object(
            "GET",
            _query("/rest/db/file", folder=folder_id, file=file_path),
        )
        raw_availability = payload.get("availability")
        if not isinstance(raw_availability, list):
            raise SyncthingProtocolError("Syncthing file availability must be a JSON array")
        availability: list[FileAvailability] = []
        for item in raw_availability:
            if not isinstance(item, dict):
                raise SyncthingProtocolError("Syncthing file availability entry must be an object")
            availability.append(
                FileAvailability(
                    device_id=validate_device_id(item.get("id")),
                    from_temporary=_boolean(
                        item.get("fromTemporary", False), field="availability.fromTemporary"
                    ),
                )
            )
        return SyncthingFileStatus(
            folder_id=folder_id,
            file_path=file_path,
            local=_parse_file_info(payload.get("local"), field="local"),
            global_file=_parse_file_info(payload.get("global"), field="global"),
            availability=tuple(sorted(availability, key=lambda item: item.device_id)),
        )

    def get_options(self) -> SyncthingOptions:
        payload = self._object("GET", "/rest/config/options")
        return SyncthingOptions(
            listen_addresses=_listen_addresses(payload.get("listenAddresses")),
            local_announce_enabled=_boolean(
                payload.get("localAnnounceEnabled"), field="localAnnounceEnabled"
            ),
            global_announce_enabled=_boolean(
                payload.get("globalAnnounceEnabled"), field="globalAnnounceEnabled"
            ),
            relays_enabled=_boolean(payload.get("relaysEnabled"), field="relaysEnabled"),
        )

    def patch_options(self, changes: SyncthingOptionsPatch | Mapping[str, object]) -> None:
        if isinstance(changes, SyncthingOptionsPatch):
            payload = changes.as_payload()
        else:
            raw = _object_payload(changes, label="options patch")
            unknown = set(raw) - {
                "listenAddresses",
                "localAnnounceEnabled",
                "globalAnnounceEnabled",
                "relaysEnabled",
            }
            if unknown:
                raise SyncthingConfigurationError("options patch contains unsupported fields")
            typed = SyncthingOptionsPatch(
                listen_addresses=None
                if "listenAddresses" not in raw
                else _listen_addresses(raw["listenAddresses"]),
                local_announce_enabled=None
                if "localAnnounceEnabled" not in raw
                else _configuration_bool(raw["localAnnounceEnabled"], field="localAnnounceEnabled"),
                global_announce_enabled=None
                if "globalAnnounceEnabled" not in raw
                else _configuration_bool(raw["globalAnnounceEnabled"], field="globalAnnounceEnabled"),
                relays_enabled=None
                if "relaysEnabled" not in raw
                else _configuration_bool(raw["relaysEnabled"], field="relaysEnabled"),
            )
            payload = typed.as_payload()
        self._request("PATCH", "/rest/config/options", payload=payload, allow_empty=True)

    def apply_discovery_relay(self) -> DiscoveryRelayConfiguration:
        """Disable direct listeners while retaining discovery and relay fallback.

        A discovered Android peer may still receive an outbound direct
        connection. This deliberately matches the product architecture; it is
        not presented as a relay-only transport guarantee.
        """

        self.patch_options(SyncthingOptionsPatch.discovery_relay())
        options = self.get_options()
        if not options.is_discovery_relay:
            raise SyncthingConnectivityPolicyError("Syncthing did not retain discovery-and-relay options")
        return DiscoveryRelayConfiguration(options=options, restart_required=self.restart_required())

    def restart_required(self) -> bool:
        payload = self._object("GET", "/rest/config/restart-required")
        return _boolean(payload.get("requiresRestart"), field="requiresRestart")

    @staticmethod
    def _parse_device(value: object) -> ConfiguredDevice:
        if not isinstance(value, dict):
            raise SyncthingProtocolError("configured device must be a JSON object")
        raw_addresses = value.get("addresses", [])
        if not isinstance(raw_addresses, list):
            raise SyncthingProtocolError("device addresses must be a JSON array")
        addresses = tuple(
            _required_text(item, field="device.address", maximum=1024) for item in raw_addresses
        )
        return ConfiguredDevice(
            device_id=validate_device_id(value.get("deviceID")),
            name=_required_text(value.get("name", "unnamed"), field="device.name", maximum=256),
            addresses=addresses,
            paused=_boolean(value.get("paused", False), field="device.paused"),
            auto_accept_folders=_boolean(
                value.get("autoAcceptFolders", False),
                field="device.autoAcceptFolders",
            ),
            introducer=_boolean(value.get("introducer", False), field="device.introducer"),
        )

    @staticmethod
    def _parse_folder(value: object) -> ConfiguredFolder:
        if not isinstance(value, dict):
            raise SyncthingProtocolError("configured folder must be a JSON object")
        raw_devices = value.get("devices", [])
        if not isinstance(raw_devices, list):
            raise SyncthingProtocolError("folder devices must be a JSON array")
        device_ids: list[str] = []
        for item in raw_devices:
            if not isinstance(item, dict):
                raise SyncthingProtocolError("folder device must be a JSON object")
            device_ids.append(validate_device_id(item.get("deviceID")))
        raw_versioning = value.get("versioning", {})
        if not isinstance(raw_versioning, dict):
            raise SyncthingProtocolError("folder versioning must be a JSON object")
        unknown_versioning = set(raw_versioning) - {
            "type",
            "params",
            "cleanupIntervalS",
            "fsPath",
            "fsType",
        }
        if unknown_versioning:
            raise SyncthingProtocolError("folder versioning contains unsupported fields")
        raw_params = raw_versioning.get("params", {})
        if not isinstance(raw_params, dict) or len(raw_params) > 32:
            raise SyncthingProtocolError("folder versioning params must be a bounded JSON object")
        versioning_params: list[tuple[str, str]] = []
        for key, item in raw_params.items():
            versioning_params.append(
                (
                    _required_text(key, field="folder.versioning.param", maximum=128),
                    _required_text(item, field="folder.versioning.value", maximum=256),
                )
            )
        cleanup_interval = raw_versioning.get("cleanupIntervalS")
        if cleanup_interval is not None:
            cleanup_interval = _integer(
                cleanup_interval,
                field="folder.versioning.cleanupIntervalS",
                minimum=0,
            )
        fs_path = raw_versioning.get("fsPath", "")
        if (
            not isinstance(fs_path, str)
            or len(fs_path) > 4096
            or any(ord(character) < 32 and character != "\t" for character in fs_path)
        ):
            raise SyncthingProtocolError("folder versioning fsPath must be bounded text")
        return ConfiguredFolder(
            folder_id=_identifier(value.get("id"), field="folder_id"),
            label=_required_text(value.get("label", "unnamed"), field="folder.label", maximum=256),
            path=_required_text(value.get("path"), field="folder.path", maximum=4096),
            device_ids=tuple(device_ids),
            paused=_boolean(value.get("paused", False), field="folder.paused"),
            folder_type=_optional_text(value.get("type"), field="folder.type", maximum=128),
            versioning_type=_optional_text(
                raw_versioning.get("type"), field="folder.versioning.type", maximum=128
            ),
            versioning_params=tuple(sorted(versioning_params)),
            versioning_cleanup_interval_s=cleanup_interval,
            versioning_fs_path=fs_path,
            versioning_fs_type=_optional_text(
                raw_versioning.get("fsType"), field="folder.versioning.fsType", maximum=128
            ),
        )


def _validate_api_key(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 1024:
        raise SyncthingConfigurationError("Syncthing API key must be a bounded string")
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise SyncthingConfigurationError("Syncthing API key must contain printable ASCII without spaces")
    return value


def owner_filesystem_key(owner_key: str) -> str:
    """Hash an authenticated actor's opaque ``own_id`` into a safe directory name.

    The caller must pass the actor identity, never an archive/shared tenant ID.
    Raw identity material is intentionally absent from the returned path.
    """

    if not isinstance(owner_key, str) or not owner_key or "\x00" in owner_key:
        raise SyncthingConfigurationError("owner_key must be a non-empty opaque actor identity")
    try:
        encoded = owner_key.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SyncthingConfigurationError("owner_key must be valid UTF-8") from exc
    if len(encoded) > MAX_OWNER_KEY_BYTES:
        raise SyncthingConfigurationError("owner_key is too large")
    return f"owner-{hashlib.sha256(encoded).hexdigest()}"


def _opaque_profile_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise SyncthingConfigurationError("profile_id must be a bounded opaque identifier")
    if any(ord(character) < 33 or character in "\x7f" for character in value):
        raise SyncthingConfigurationError("profile_id contains whitespace or control characters")
    return value


def _derived_runtime_root(base_root: Path) -> Path:
    # This pathname is part of the durable profile contract: ``api_endpoint`` is
    # persisted and checked byte-for-byte on every restart.  The backend service
    # deliberately owns a private TMPDIR which can change independently of an
    # existing Obsidian profile (and is removed on every service stop), so using
    # ``tempfile.gettempdir()`` here permanently stranded otherwise valid profiles
    # after unit hardening.  Keep the short, local Unix-socket namespace stable;
    # the installation digest prevents collisions and ``prepare_profile_directories``
    # still enforces an owner-only 0700 directory.  ``/tmp`` also preserves the
    # endpoint emitted by every profile created before this fix.
    installation_key = hashlib.sha256(os.fsencode(base_root.absolute())).hexdigest()[:16]
    return Path("/tmp") / f"friday-syncthing-{installation_key}"


@dataclass(frozen=True, slots=True)
class SyncthingProfileSpec:
    """Filesystem and GUI binding for one actor-owned Syncthing process."""

    profile_id: str
    owner_fs_key: str
    base_root: Path
    profile_root: Path
    config_root: Path
    data_root: Path
    vault_root: Path
    runtime_root: Path
    gui_mode: str
    gui_port: int | None
    gui_socket: Path | None
    binary: str

    def __post_init__(self) -> None:
        _opaque_profile_id(self.profile_id)
        if (
            len(self.owner_fs_key) != len("owner-") + 64
            or not self.owner_fs_key.startswith("owner-")
            or any(character not in "0123456789abcdef" for character in self.owner_fs_key[6:])
        ):
            raise SyncthingConfigurationError("owner_fs_key must be a full hashed owner identity")
        if not self.base_root.is_absolute():
            raise SyncthingConfigurationError("Syncthing base_root must be absolute")
        expected_profile = self.base_root / "users" / self.owner_fs_key
        if (
            self.profile_root != expected_profile
            or self.config_root != expected_profile / "syncthing-config"
            or self.data_root != expected_profile / "syncthing-db"
            or self.vault_root != expected_profile / "vaults"
            or self.runtime_root != _derived_runtime_root(self.base_root)
        ):
            raise SyncthingConfigurationError("profile paths must use the derived owner namespace")
        if self.gui_mode == "unix":
            expected_socket = self.runtime_root / f"st-{self.owner_fs_key[6:30]}.sock"
            if self.gui_port is not None or self.gui_socket != expected_socket:
                raise SyncthingConfigurationError("Unix GUI binding does not match the owner profile")
            if len(os.fsencode(expected_socket)) > 107:
                raise SyncthingConfigurationError("base_root is too long for the Syncthing Unix socket")
        elif self.gui_mode == "loopback":
            if (
                isinstance(self.gui_port, bool)
                or not isinstance(self.gui_port, int)
                or not 1 <= self.gui_port <= 65535
                or self.gui_socket is not None
            ):
                raise SyncthingConfigurationError("invalid loopback GUI binding")
        else:
            raise SyncthingConfigurationError("gui_mode must be unix or loopback")
        if not isinstance(self.binary, str) or not self.binary or "\x00" in self.binary:
            raise SyncthingConfigurationError("Syncthing binary must be a non-empty path or command")

    @classmethod
    def for_owner(
        cls,
        base_root: str | os.PathLike[str],
        owner_key: str,
        *,
        profile_id: str | None = None,
        gui_mode: str = "unix",
        gui_port: int | None = None,
        binary: str = "syncthing",
    ) -> SyncthingProfileSpec:
        base = Path(base_root)
        if not base.is_absolute():
            raise SyncthingConfigurationError("Syncthing base_root must be absolute")
        if gui_mode not in {"unix", "loopback"}:
            raise SyncthingConfigurationError("gui_mode must be unix or loopback")
        if not isinstance(binary, str) or not binary or "\x00" in binary:
            raise SyncthingConfigurationError("Syncthing binary must be a non-empty path or command")
        owner_name = owner_filesystem_key(owner_key)
        if profile_id is None:
            profile_id = f"stprof-{owner_name.removeprefix('owner-')[:32]}"
        profile_id = _opaque_profile_id(profile_id)
        profile_root = base / "users" / owner_name
        # The durable root is commonly a Windows-backed Docker bind mount, which
        # cannot reliably host Unix sockets.  Keep the endpoint in an ephemeral,
        # owner-private local directory and key it by the installation root so
        # two Friday instances do not collide.
        runtime_root = _derived_runtime_root(base)
        if gui_mode == "loopback":
            if isinstance(gui_port, bool) or not isinstance(gui_port, int) or not 1 <= gui_port <= 65535:
                raise SyncthingConfigurationError("loopback GUI requires a port from 1 to 65535")
            gui_socket = None
        else:
            if gui_port is not None:
                raise SyncthingConfigurationError("Unix GUI does not accept a TCP port")
            gui_socket = runtime_root / f"st-{owner_name.removeprefix('owner-')[:24]}.sock"
            if len(os.fsencode(gui_socket)) > 107:
                raise SyncthingConfigurationError("base_root is too long for the Syncthing Unix socket")
        return cls(
            profile_id=profile_id,
            owner_fs_key=owner_name,
            base_root=base,
            profile_root=profile_root,
            config_root=profile_root / "syncthing-config",
            data_root=profile_root / "syncthing-db",
            vault_root=profile_root / "vaults",
            runtime_root=runtime_root,
            gui_mode=gui_mode,
            gui_port=gui_port,
            gui_socket=gui_socket,
            binary=binary,
        )

    @property
    def config_file(self) -> Path:
        return self.config_root / "config.xml"

    @property
    def log_file(self) -> Path:
        return self.profile_root / "syncthing.log"

    @property
    def gui_address(self) -> str:
        if self.gui_mode == "unix":
            if self.gui_socket is None:  # pragma: no cover - dataclass invariant
                raise SyncthingConfigurationError("Unix GUI socket is missing")
            return f"unix://{self.gui_socket}"
        return f"127.0.0.1:{self.gui_port}"

    def make_transport(self) -> SyncthingTransport:
        if self.gui_mode == "unix":
            if self.gui_socket is None:  # pragma: no cover - dataclass invariant
                raise SyncthingConfigurationError("Unix GUI socket is missing")
            return UnixSocketTransport(self.gui_socket)
        return LoopbackHTTPTransport(f"http://127.0.0.1:{self.gui_port}")


def _make_private_directory(path: Path, *, create_parents: bool = False) -> None:
    try:
        path.mkdir(mode=0o700, parents=create_parents, exist_ok=True)
    except OSError as exc:
        raise SyncthingSecurityError(f"could not create private directory: {path}") from exc
    _validate_private_directory(path)


def prepare_profile_directories(spec: SyncthingProfileSpec) -> None:
    """Create and verify the owner-only directories used by one profile."""

    _make_private_directory(spec.base_root, create_parents=True)
    users_root = spec.base_root / "users"
    _make_private_directory(users_root)
    _make_private_directory(spec.runtime_root)
    _make_private_directory(spec.profile_root)
    _make_private_directory(spec.config_root)
    _make_private_directory(spec.data_root)
    _make_private_directory(spec.vault_root)


def _private_file_info(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SyncthingSecurityError(f"private file is unavailable: {path}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SyncthingSecurityError(f"private file is not a single regular file: {path}")
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise SyncthingSecurityError(f"private file has another owner: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise SyncthingSecurityError(f"private file is accessible to other users: {path}")
    return info


def _read_private_file(path: Path, *, maximum: int) -> bytes:
    before = _private_file_info(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SyncthingSecurityError(f"could not open private file: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise SyncthingSecurityError(f"private file changed while opening: {path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise SyncthingConfigurationError(f"private file is too large: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_api_key(config_file: str | os.PathLike[str]) -> str:
    """Read the generated key from private ``config.xml`` without putting it in argv."""

    payload = _read_private_file(Path(config_file), maximum=MAX_CONFIG_BYTES)
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise SyncthingConfigurationError("Syncthing config.xml must not contain DTD declarations")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise SyncthingConfigurationError("Syncthing config.xml is not valid XML") from exc
    keys = []
    for gui in root.iter():
        if gui.tag.rsplit("}", 1)[-1] != "gui":
            continue
        for child in gui:
            if child.tag.rsplit("}", 1)[-1] == "apikey":
                keys.append((child.text or "").strip())
    if len(keys) != 1:
        raise SyncthingConfigurationError("Syncthing config.xml must contain exactly one GUI API key")
    return _validate_api_key(keys[0])


def build_generate_command(spec: SyncthingProfileSpec) -> tuple[str, ...]:
    """Build key-free one-time profile generation argv."""

    return (
        spec.binary,
        "generate",
        f"--config={spec.config_root}",
        f"--data={spec.data_root}",
    )


def build_serve_command(spec: SyncthingProfileSpec) -> tuple[str, ...]:
    """Build hardened per-owner foreground serve argv."""

    return (
        spec.binary,
        "serve",
        f"--config={spec.config_root}",
        f"--data={spec.data_root}",
        f"--gui-address={spec.gui_address}",
        "--no-browser",
        "--no-port-probing",
        "--no-restart",
        "--no-upgrade",
    )


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int


class ProcessHandle(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class SyncthingRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult: ...

    def spawn(self, argv: Sequence[str], *, cwd: Path, log_path: Path) -> ProcessHandle: ...


def _open_private_log(path: Path) -> Any:
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SyncthingSecurityError(f"could not open private log file: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise SyncthingSecurityError(f"Syncthing log file is not private: {path}")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise SyncthingSecurityError(f"Syncthing log file has another owner: {path}")
        return os.fdopen(descriptor, "ab", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


class StdlibSyncthingRunner:
    """Default subprocess adapter; it never invokes a shell."""

    def run(self, argv: Sequence[str], *, timeout: float) -> CommandResult:
        try:
            completed = subprocess.run(  # nosec B603 - fixed list argv, no shell
                tuple(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_validate_timeout(timeout),
                check=False,
                close_fds=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise SyncthingProcessError("Syncthing profile generation timed out") from exc
        except OSError as exc:
            raise SyncthingProcessError("could not execute Syncthing profile generation") from exc
        return CommandResult(completed.returncode)

    def spawn(self, argv: Sequence[str], *, cwd: Path, log_path: Path) -> ProcessHandle:
        log = _open_private_log(log_path)
        try:
            return subprocess.Popen(  # nosec B603 - fixed list argv, no shell
                tuple(argv),
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise SyncthingProcessError("could not start managed Syncthing process") from exc
        finally:
            log.close()


@dataclass(frozen=True, slots=True)
class SyncthingReadiness:
    version: SyncthingVersion
    status: SyncthingSystemStatus


RestClientFactory = Callable[[SyncthingProfileSpec, str], SyncthingRestClient]


def _default_client_factory(spec: SyncthingProfileSpec, api_key: str) -> SyncthingRestClient:
    return SyncthingRestClient(spec.make_transport(), api_key)


class SyncthingProcessSupervisor:
    """Own one foreground Syncthing process for one actor profile."""

    def __init__(
        self,
        spec: SyncthingProfileSpec,
        *,
        runner: SyncthingRunner | None = None,
        client_factory: RestClientFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.spec = spec
        self._runner = runner or StdlibSyncthingRunner()
        self._client_factory = client_factory or _default_client_factory
        self._clock = clock
        self._sleeper = sleeper
        self._process: ProcessHandle | None = None
        self._client: SyncthingRestClient | None = None
        self._lock = threading.RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def client(self) -> SyncthingRestClient:
        with self._lock:
            if self._client is None or not self.is_running:
                raise SyncthingProcessError("managed Syncthing process is not running")
            return self._client

    def provision(self, *, timeout: float = 30.0) -> str:
        """Create an isolated profile when absent and return its private REST key."""

        timeout = _validate_timeout(timeout)
        prepare_profile_directories(self.spec)
        if not os.path.lexists(self.spec.config_file):
            result = self._runner.run(build_generate_command(self.spec), timeout=timeout)
            if result.returncode != 0:
                raise SyncthingProcessError(
                    f"Syncthing profile generation failed (status {result.returncode})"
                )
        return read_api_key(self.spec.config_file)

    def start(
        self,
        *,
        readiness_timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> SyncthingReadiness:
        readiness_timeout = _validate_timeout(readiness_timeout)
        poll_interval = _validate_timeout(poll_interval)
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                if self._client is None:  # pragma: no cover - internal invariant
                    raise SyncthingProcessError("running Syncthing process has no REST client")
                return SyncthingReadiness(
                    version=self._client.system_version(),
                    status=self._client.system_status(),
                )
            self._process = None
            self._client = None
            api_key = self.provision(timeout=readiness_timeout)
            client = self._client_factory(self.spec, api_key)
            process = self._runner.spawn(
                build_serve_command(self.spec),
                cwd=self.spec.profile_root,
                log_path=self.spec.log_file,
            )
            self._process = process
            deadline = self._clock() + readiness_timeout
            try:
                while True:
                    returncode = process.poll()
                    if returncode is not None:
                        raise SyncthingProcessExitedError(returncode)
                    try:
                        version = client.system_version()
                        status = client.system_status()
                    except SyncthingHTTPError as exc:
                        if exc.status not in {502, 503, 504}:
                            raise
                    except SyncthingTransportError:
                        pass
                    else:
                        self._client = client
                        return SyncthingReadiness(version=version, status=status)
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise SyncthingReadinessTimeoutError(
                            "Syncthing REST endpoint did not become ready in time"
                        )
                    self._sleeper(min(poll_interval, remaining))
            except BaseException:
                self._stop_locked(timeout=min(5.0, readiness_timeout), suppress_errors=True)
                raise

    def stop(self, *, timeout: float = 10.0) -> None:
        timeout = _validate_timeout(timeout)
        with self._lock:
            self._stop_locked(timeout=timeout, suppress_errors=False)

    def _stop_locked(self, *, timeout: float, suppress_errors: bool) -> None:
        process = self._process
        self._client = None
        if process is None:
            return
        if process.poll() is not None:
            self._process = None
            return
        try:
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, TimeoutError):
                process.kill()
                process.wait(timeout=timeout)
        except BaseException as exc:
            if not suppress_errors:
                raise SyncthingProcessError("could not stop managed Syncthing process") from exc
        else:
            self._process = None


SupervisorFactory = Callable[[SyncthingProfileSpec], SyncthingProcessSupervisor]


@dataclass(slots=True)
class _ManagedProfile:
    spec: SyncthingProfileSpec
    supervisor: SyncthingProcessSupervisor
    lock: threading.RLock
    active: bool = True


class SyncthingProcessManager:
    """Bounded registry preventing duplicate processes and GUI/profile collisions."""

    def __init__(
        self,
        *,
        max_profiles: int = 128,
        supervisor_factory: SupervisorFactory | None = None,
    ) -> None:
        if (
            isinstance(max_profiles, bool)
            or not isinstance(max_profiles, int)
            or not 1 <= max_profiles <= 4096
        ):
            raise SyncthingConfigurationError("max_profiles must be between 1 and 4096")
        self._max_profiles = max_profiles
        self._supervisor_factory = supervisor_factory or SyncthingProcessSupervisor
        self._profiles: dict[str, _ManagedProfile] = {}
        self._lock = threading.RLock()
        self._closed = False

    def ensure_profile(
        self,
        spec: SyncthingProfileSpec,
        *,
        readiness_timeout: float = 30.0,
        poll_interval: float = 0.1,
    ) -> SyncthingReadiness:
        """Start or reuse exactly one process for ``spec.profile_id``."""

        profile_id = _opaque_profile_id(spec.profile_id)
        with self._lock:
            if self._closed:
                raise SyncthingProcessError("Syncthing process manager is closed")
            entry = self._profiles.get(profile_id)
            if entry is not None:
                if entry.spec != spec:
                    raise SyncthingConfigurationError(
                        "profile_id is already registered with a different profile specification"
                    )
            else:
                for existing in self._profiles.values():
                    if existing.spec.profile_root == spec.profile_root:
                        raise SyncthingConfigurationError(
                            "actor profile root is already registered under another profile_id"
                        )
                    if existing.spec.gui_address == spec.gui_address:
                        raise SyncthingConfigurationError(
                            "Syncthing GUI endpoint is already assigned to another profile"
                        )
                if len(self._profiles) >= self._max_profiles:
                    raise SyncthingProfileLimitError("managed Syncthing profile limit reached")
                entry = _ManagedProfile(
                    spec=spec,
                    supervisor=self._supervisor_factory(spec),
                    lock=threading.RLock(),
                )
                self._profiles[profile_id] = entry
        with entry.lock:
            with self._lock:
                if not entry.active or self._profiles.get(profile_id) is not entry or self._closed:
                    raise SyncthingProcessError("Syncthing profile was stopped while being started")
            return entry.supervisor.start(
                readiness_timeout=readiness_timeout,
                poll_interval=poll_interval,
            )

    def client_for(self, profile_id: str) -> SyncthingRestClient:
        profile_id = _opaque_profile_id(profile_id)
        with self._lock:
            entry = self._profiles.get(profile_id)
            if entry is None or not entry.active:
                raise SyncthingProcessError("managed Syncthing profile is not registered")
        with entry.lock:
            with self._lock:
                if not entry.active or self._profiles.get(profile_id) is not entry:
                    raise SyncthingProcessError("managed Syncthing profile is not registered")
            return entry.supervisor.client

    def stop_profile(self, profile_id: str, *, timeout: float = 10.0) -> bool:
        profile_id = _opaque_profile_id(profile_id)
        with self._lock:
            entry = self._profiles.get(profile_id)
            if entry is None:
                return False
        with entry.lock:
            entry.supervisor.stop(timeout=timeout)
            with self._lock:
                if self._profiles.get(profile_id) is not entry:
                    return False
                entry.active = False
                del self._profiles[profile_id]
            return True

    def close(self, *, timeout: float = 10.0) -> None:
        timeout = _validate_timeout(timeout)
        with self._lock:
            if self._closed and not self._profiles:
                return
            self._closed = True
            entries = tuple(self._profiles.items())
        failures: list[BaseException] = []
        for profile_id, _entry in entries:
            try:
                self.stop_profile(profile_id, timeout=timeout)
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise SyncthingProcessError(
                f"could not stop {len(failures)} managed Syncthing profile(s)"
            ) from failures[0]

    def __enter__(self) -> SyncthingProcessManager:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
