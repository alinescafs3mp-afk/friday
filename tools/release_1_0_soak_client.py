"""Signed, bounded API observations for a persistent application soak.

This is an ingress component, not a soak verdict or a Telegram delivery probe.
The contained driver owns the single app lifetime, workload and process deadline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_REQUEST_BYTES = 1 << 20
MAX_RESPONSE_BYTES = 8 << 20
MAX_REQUESTS = 4096
MAX_EVIDENCE_BYTES = 64 << 20
_DOWNLOAD = re.compile(r"/api/files/[A-Za-z0-9_-]{1,128}\Z")


class SoakIngressError(RuntimeError):
    pass


@dataclass(frozen=True)
class ObservedResponse:
    sequence: int
    status_code: int
    body: bytes
    elapsed_ns: int

    def object(self) -> Mapping[str, Any]:
        try:
            value = json.loads(self.body)
        except (ValueError, UnicodeError):
            raise SoakIngressError("response_json_invalid") from None
        if not isinstance(value, dict):
            raise SoakIngressError("response_object_required")
        return value


class SoakSession:
    """One principal, at most one request in flight, no implicit retry.

    Every dispatch has an fsynced START before HTTP and a separate terminal
    record. A process crash therefore leaves an incomplete observed attempt.
    Signature headers never enter evidence. Request/response bytes are private.
    """

    def __init__(self, client: Any, *, bridge_secret: str, chat_id: int, evidence: Path) -> None:
        if not bridge_secret or type(chat_id) is not int or chat_id <= 0:
            raise SoakIngressError("principal_invalid")
        evidence.mkdir(mode=0o700)
        self._fd = os.open(evidence, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(self._fd)
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            os.close(self._fd)
            raise SoakIngressError("evidence_directory_not_private")
        self.client, self.secret, self.chat_id = client, bridge_secret, str(chat_id)
        self.evidence = evidence
        self._lock = threading.Lock()
        self._sequence = 0
        self._evidence_bytes = 0
        self._closed = False
        self._failed = False

    def close(self) -> None:
        if not self._lock.acquire(blocking=False):
            raise SoakIngressError("request_still_in_flight")
        try:
            if not self._closed:
                os.close(self._fd)
                self._closed = True
        finally:
            self._lock.release()

    def _write(self, name: str, value: bytes) -> None:
        if self._evidence_bytes + len(value) > MAX_EVIDENCE_BYTES:
            raise SoakIngressError("evidence_budget_exhausted")
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._fd,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            self._evidence_bytes += len(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(self._fd)

    def _record(self, name: str, value: Mapping[str, Any]) -> None:
        self._write(name, (json.dumps(value, sort_keys=True) + "\n").encode())

    @staticmethod
    def _route(method: str, path: str) -> None:
        allowed = (
            method == "GET"
            and (
                path in {"/api/me", "/api/files", "/api/me/reminders"}
                or _DOWNLOAD.fullmatch(path) is not None
            )
        ) or (method == "POST" and path in {"/api/chat", "/api/files"})
        if not allowed:
            raise SoakIngressError("route_outside_soak_ingress")

    def _dispatch(self, method: str, path: str, body: bytes, content_type: str) -> ObservedResponse:
        from friday.security import sign_bridge_request

        self._route(method, path)
        if len(body) > MAX_REQUEST_BYTES:
            raise SoakIngressError("request_oversized")
        if not self._lock.acquire(blocking=False):
            raise SoakIngressError("concurrent_request_for_same_principal")
        try:
            if self._closed or self._failed or self._sequence >= MAX_REQUESTS:
                raise SoakIngressError("session_closed_or_exhausted")
            if self._evidence_bytes + len(body) + MAX_RESPONSE_BYTES + 16384 > MAX_EVIDENCE_BYTES:
                raise SoakIngressError("insufficient_evidence_budget_for_dispatch")
            self._sequence += 1
            sequence = self._sequence
            prefix = f"request-{sequence:06d}"
            timestamp, nonce = int(time.time()), secrets.token_hex(16)
            signature = sign_bridge_request(
                self.secret,
                timestamp=timestamp,
                method=method,
                path=path,
                external_user_id=self.chat_id,
                chat_id=self.chat_id,
                nonce=nonce,
                body=body,
            )
            headers = {
                "Content-Type": content_type,
                "X-Friday-Timestamp": str(timestamp),
                "X-Friday-User": self.chat_id,
                "X-Friday-Chat": self.chat_id,
                "X-Friday-Nonce": nonce,
                "X-Friday-Signature": signature,
            }
            started = time.monotonic_ns()
            self._write(prefix + ".request.bin", body)
            self._record(
                prefix + ".start.json",
                {
                    "sequence": sequence,
                    "method": method,
                    "path": path,
                    "started_monotonic_ns": started,
                    "started_unix_s": timestamp,
                    "request_sha256": hashlib.sha256(body).hexdigest(),
                    "request_bytes": len(body),
                },
            )
            try:
                # Bound retained observations. TestClient's ASGI transport may
                # buffer first; the contained worker still needs a memory cap.
                with self.client.stream(
                    method, path, content=body, headers=headers, follow_redirects=False
                ) as response:
                    observed = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(observed) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise SoakIngressError("response_oversized")
                        observed.extend(chunk)
                    status_code = response.status_code
                payload = bytes(observed)
                elapsed = time.monotonic_ns() - started
                self._write(prefix + ".response.bin", payload)
                self._record(
                    prefix + ".terminal.json",
                    {
                        "sequence": sequence,
                        "outcome": "HTTP_OBSERVED",
                        "status_code": status_code,
                        "elapsed_ns": elapsed,
                        "response_sha256": hashlib.sha256(payload).hexdigest(),
                        "response_bytes": len(payload),
                    },
                )
                return ObservedResponse(sequence, status_code, payload, elapsed)
            except BaseException as exc:
                # Do not persist exception text: transports can include headers.
                self._record(
                    prefix + ".error.json",
                    {
                        "sequence": sequence,
                        "outcome": "INCOMPLETE_OR_ERROR",
                        "exception_type": type(exc).__name__[:128],
                        "elapsed_ns": time.monotonic_ns() - started,
                    },
                )
                raise
        except BaseException:
            # An unknown transport effect or partial evidence write requires
            # reconciliation; a new request must not silently bypass it.
            self._failed = True
            raise
        finally:
            self._lock.release()

    def json(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> ObservedResponse:
        if method == "GET" and payload is not None:
            raise SoakIngressError("get_body_not_allowed")
        body = (
            b""
            if payload is None
            else json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        )
        return self._dispatch(method, path, body, "application/json")

    def upload(self, *, filename: str, content: bytes, source_ref: str) -> ObservedResponse:
        import httpx

        if not filename or any(c in filename for c in ("\r", "\n", "\x00", "/", "\\")):
            raise SoakIngressError("fixture_filename_invalid")
        if not isinstance(content, bytes) or len(content) > MAX_REQUEST_BYTES:
            raise SoakIngressError("fixture_content_invalid")
        # Prepare the actual multipart wire body before signing. Reconstructing
        # multipart after signing would choose another boundary and invalidate it.
        prepared = httpx.Request(
            "POST",
            "http://testserver/api/files",
            data={"source_ref": source_ref},
            files={"file": (filename, content, "text/plain")},
        )
        body = prepared.read()
        return self._dispatch("POST", "/api/files", body, prepared.headers["content-type"])


def observe_isolated_principals(
    sessions: Sequence[SoakSession], storage: Any, *, shared_archive: bool
) -> tuple[str, ...]:
    """Observe four distinct actual users; multiple owner aliases must fail."""
    if (
        type(shared_archive) is not bool
        or shared_archive
        or len(sessions) != 4
        or len({s.chat_id for s in sessions}) != 4
    ):
        raise SoakIngressError("four_isolated_principals_required")
    observed = []
    for session in sessions:
        response = session.json("GET", "/api/me")
        if response.status_code != 200:
            raise SoakIngressError("principal_http_refused")
        identity = response.object()
        actor, user = identity.get("actor"), identity.get("user")
        if not isinstance(actor, dict) or not isinstance(user, dict):
            raise SoakIngressError("principal_identity_missing")
        user_id = actor.get("user_id")
        if (
            not isinstance(user_id, str)
            or not user_id
            or user.get("id") != user_id
            or user.get("status") != "active"
            or actor.get("source") != "telegram-bridge"
            or actor.get("preset_key") != user.get("preset_key")
            or actor.get("preset_key") not in {"owner", "user"}
            or storage.resolve_identity("telegram", session.chat_id) != user_id
        ):
            raise SoakIngressError("principal_binding_invalid")
        observed.append(user_id)
    if len(set(observed)) != 4:
        raise SoakIngressError("principal_aliases_are_not_four_tenants")
    return tuple(observed)
