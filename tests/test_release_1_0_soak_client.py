"""Ingress observations only; these tests do not certify a persistent soak.

All HTTP stays in MockTransport. The real bridge verifier authenticates the
actual prepared wire bytes. This file is executed only by the owning runner.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest

from friday.security import verify_bridge_request

SECRET = "synthetic-ingress-test-key-never-a-live-credential"
CANARY = b"private-canary-163\x00\xff\r\n" + "Секретная строка ё".encode()


@pytest.fixture
def ingress():
    path = Path(__file__).resolve().parents[1] / "tools" / "release_1_0_soak_client.py"
    name = "_private_soak_ingress_under_test"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def _verify(request, **changes):
    values = {
        "timestamp": request.headers["x-friday-timestamp"],
        "method": request.method,
        "path": request.url.raw_path.decode("ascii"),
        "external_user_id": request.headers["x-friday-user"],
        "chat_id": request.headers["x-friday-chat"],
        "nonce": request.headers["x-friday-nonce"],
        "body": request.read(),
        "signature": request.headers["x-friday-signature"],
        "max_age_sec": 60,
    }
    values.update(changes)
    return verify_bridge_request(SECRET, **values)


@pytest.fixture
def make_session(ingress, tmp_path):
    with ExitStack() as stack:
        created = []

        def make(handler, *, chat_id=7101):
            def authenticated(request):
                identity = _verify(request)
                assert identity.chat_id == identity.external_user_id == str(chat_id)
                return handler(request)

            client = stack.enter_context(
                httpx.Client(
                    transport=httpx.MockTransport(authenticated),
                    base_url="http://testserver",
                )
            )
            evidence = tmp_path / f"evidence-{len(created)}"
            session = ingress.SoakSession(
                client,
                bridge_secret=SECRET,
                chat_id=chat_id,
                evidence=evidence,
            )
            created.append(session)
            stack.callback(session.close)
            return session

        yield make


def _record(session, suffix, sequence=1):
    return json.loads((session.evidence / f"request-{sequence:06d}.{suffix}.json").read_bytes())


def _assert_observation(session, request, response):
    prefix = f"request-{response.sequence:06d}"
    request_bytes = (session.evidence / f"{prefix}.request.bin").read_bytes()
    response_bytes = (session.evidence / f"{prefix}.response.bin").read_bytes()
    assert request_bytes == request.read()
    assert response_bytes == response.body
    start = _record(session, "start", response.sequence)
    terminal = _record(session, "terminal", response.sequence)
    assert start["sequence"] == terminal["sequence"] == response.sequence
    assert (start["method"], start["path"]) == (request.method, request.url.path)
    assert start["request_sha256"] == hashlib.sha256(request_bytes).hexdigest()
    assert start["request_bytes"] == len(request_bytes)
    assert terminal["response_sha256"] == hashlib.sha256(response_bytes).hexdigest()
    assert terminal["response_bytes"] == len(response_bytes)
    assert terminal["outcome"] == "HTTP_OBSERVED"
    assert terminal["status_code"] == response.status_code
    assert terminal["elapsed_ns"] == response.elapsed_ns >= 0
    assert stat.S_IMODE(session.evidence.stat().st_mode) == 0o700
    for path in session.evidence.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        private = path.read_bytes()
        assert SECRET.encode() not in private
        assert request.headers["x-friday-signature"].encode() not in private
        assert request.headers["x-friday-nonce"].encode() not in private


def test_json_signs_exact_utf8_bytes_and_repeated_actor_uses_fresh_nonce(
    ingress,
    make_session,
    monkeypatch,
):
    monkeypatch.setattr(ingress.time, "time", lambda: 1_800_000_000)
    captured = []

    def handler(request):
        captured.append(request)
        # START must exist before the transport can observe a request.
        assert _record(session, "start", len(captured))["request_bytes"] == len(request.read())
        assert not (session.evidence / f"request-{len(captured):06d}.terminal.json").exists()
        return httpx.Response(200, json={"answer": "Принято"})

    session = make_session(handler)
    payload = {"source_ref": "case-163", "message": "Проверка ёж"}
    observed = [session.json("POST", "/api/chat", payload) for _ in range(3)]
    expected_wire = '{"message":"Проверка ёж","source_ref":"case-163"}'.encode()
    assert [item.sequence for item in observed] == [1, 2, 3]
    assert len({request.headers["x-friday-nonce"] for request in captured}) == 3
    assert len({request.headers["x-friday-signature"] for request in captured}) == 3
    assert {request.headers["x-friday-timestamp"] for request in captured} == {"1800000000"}
    for request, response in zip(captured, observed, strict=True):
        assert request.read() == expected_wire
        assert request.headers["content-type"] == "application/json"
        assert response.object() == {"answer": "Принято"}
        _assert_observation(session, request, response)
        for mutation in (
            {"body": request.read() + b" "},
            {"path": "/api/files"},
            {"method": "GET"},
            {"chat_id": "7102"},
            {"external_user_id": "7102"},
            {"nonce": "f" * 32},
        ):
            with pytest.raises(ValueError, match="Invalid bridge signature"):
                _verify(request, **mutation)


def test_upload_signs_prepared_multipart_with_cyrillic_filename_and_binary_canary(make_session):
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(201, json={"id": "fixture_163"})

    session = make_session(handler)
    response = session.upload(
        filename="памятка ё.txt",
        content=CANARY,
        source_ref="источник-163",
    )
    (request,) = captured
    assert (request.method, request.url.path) == ("POST", "/api/files")
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    raw = request.read()
    assert 'filename="памятка ё.txt"'.encode() in raw
    envelope = BytesParser(policy=policy.default).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n" + raw,
    )
    assert envelope.is_multipart()
    parts = list(envelope.iter_parts())
    assert len(parts) == 2
    fields = {part.get_param("name", header="content-disposition"): part for part in parts}
    assert fields["source_ref"].get_payload(decode=True) == "источник-163".encode()
    assert fields["file"].get_filename() == "памятка ё.txt"
    assert fields["file"].get_content_type() == "text/plain"
    assert fields["file"].get_payload(decode=True) == CANARY
    boundary = envelope.get_boundary().encode()
    assert raw.startswith(b"--" + boundary + b"\r\n")
    assert raw.endswith(b"--" + boundary + b"--\r\n")
    with pytest.raises(ValueError, match="Invalid bridge signature"):
        _verify(request, body=raw.replace(CANARY, CANARY + b"tampered"))
    assert response.status_code == 201
    _assert_observation(session, request, response)


@pytest.mark.parametrize("status", [302, 307, 401, 429, 500, 503])
def test_http_refusal_or_redirect_is_observed_once_without_retry_or_redirect(status, make_session):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=b"refused", headers={"Location": "/api/files"})

    session = make_session(handler)
    response = session.json("GET", "/api/me")
    assert len(calls) == 1
    assert response.status_code == status and response.body == b"refused"
    assert calls[0].read() == b""
    _assert_observation(session, calls[0], response)
    assert not list(session.evidence.glob("*.error.json"))
    # A completely recorded HTTP response permits a new explicit observation.
    assert session.json("GET", "/api/me").sequence == 2
    assert len(calls) == 2


def test_transport_error_has_no_success_receipt_or_retry_and_poisons_session(ingress, make_session):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadError(
            SECRET + request.headers["x-friday-signature"],
            request=request,
        )

    session = make_session(handler)
    with pytest.raises(httpx.ReadError):
        session.json("GET", "/api/me")
    assert len(calls) == 1
    assert _record(session, "start")["sequence"] == 1
    error = _record(session, "error")
    assert error["outcome"] == "INCOMPLETE_OR_ERROR"
    assert error["exception_type"] == "ReadError"
    assert "status_code" not in error
    assert not list(session.evidence.glob("*.terminal.json"))
    assert not list(session.evidence.glob("*.response.bin"))
    for path in session.evidence.iterdir():
        assert SECRET.encode() not in path.read_bytes()
        assert calls[0].headers["x-friday-signature"].encode() not in path.read_bytes()
    with pytest.raises(ingress.SoakIngressError, match="session_closed_or_exhausted"):
        session.json("GET", "/api/me")
    assert len(calls) == 1


@pytest.mark.parametrize("failed_stage", ["start", "terminal"])
def test_evidence_failure_cannot_turn_an_unfinished_attempt_into_success(
    failed_stage,
    ingress,
    make_session,
    monkeypatch,
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"partial-observation")

    session = make_session(handler)
    original = session._record

    def fail_record(name, value):
        if name.endswith((f".{failed_stage}.json", ".error.json")):
            raise OSError("synthetic evidence failure")
        return original(name, value)

    monkeypatch.setattr(session, "_record", fail_record)
    with pytest.raises(OSError, match="synthetic evidence failure"):
        session.json("GET", "/api/me")
    assert len(calls) == (1 if failed_stage == "terminal" else 0)
    assert not list(session.evidence.glob("*.terminal.json"))
    assert not list(session.evidence.glob("*.error.json"))
    if failed_stage == "terminal":
        assert _record(session, "start")["sequence"] == 1
        assert (session.evidence / "request-000001.response.bin").read_bytes() == b"partial-observation"
    with pytest.raises(ingress.SoakIngressError, match="session_closed_or_exhausted"):
        session.json("GET", "/api/me")
    assert len(calls) == (1 if failed_stage == "terminal" else 0)


class _Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


@pytest.mark.parametrize("overflow", [False, True])
def test_response_byte_cap_accepts_exact_boundary_and_rejects_overflow(
    overflow,
    ingress,
    make_session,
    monkeypatch,
):
    monkeypatch.setattr(ingress, "MAX_RESPONSE_BYTES", 11)
    stream = _Chunks([b"12345", b"678901"] + ([b"x"] if overflow else []))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    session = make_session(handler)
    if overflow:
        with pytest.raises(ingress.SoakIngressError, match="response_oversized"):
            session.json("GET", "/api/me")
        assert _record(session, "error")["outcome"] == "INCOMPLETE_OR_ERROR"
        assert not list(session.evidence.glob("*.response.bin"))
        assert not list(session.evidence.glob("*.terminal.json"))
        with pytest.raises(ingress.SoakIngressError, match="session_closed_or_exhausted"):
            session.json("GET", "/api/me")
    else:
        response = session.json("GET", "/api/me")
        assert response.body == b"12345678901"
        _assert_observation(session, calls[0], response)
    assert stream.closed and len(calls) == 1


def test_evidence_reservation_accounts_for_prior_files_before_dispatch(
    ingress,
    make_session,
    monkeypatch,
):
    monkeypatch.setattr(ingress, "MAX_RESPONSE_BYTES", 11)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"ok")

    session = make_session(handler)
    session.json("GET", "/api/me")
    snapshot = {p.name: p.read_bytes() for p in session.evidence.iterdir()}
    used = sum(map(len, snapshot.values()))
    # The documented ingress reserve is one full response plus 16 KiB receipts.
    monkeypatch.setattr(ingress, "MAX_EVIDENCE_BYTES", used + 11 + 16384 - 1)
    with pytest.raises(ingress.SoakIngressError, match="insufficient_evidence_budget_for_dispatch"):
        session.json("GET", "/api/me")
    assert len(calls) == 1
    assert {p.name: p.read_bytes() for p in session.evidence.iterdir()} == snapshot


def test_actual_evidence_write_cap_cannot_leave_a_terminal_success(
    ingress,
    make_session,
    monkeypatch,
):
    monkeypatch.setattr(ingress, "MAX_RESPONSE_BYTES", 11)
    calls = []

    def handler(request):
        calls.append(request)
        used = sum(p.stat().st_size for p in session.evidence.iterdir())
        # Simulate the remaining evidence budget disappearing after START.
        monkeypatch.setattr(ingress, "MAX_EVIDENCE_BYTES", used + 1)
        return httpx.Response(200, content=b"ok")

    session = make_session(handler)
    with pytest.raises(ingress.SoakIngressError, match="evidence_budget_exhausted"):
        session.json("GET", "/api/me")
    assert len(calls) == 1
    assert _record(session, "start")["sequence"] == 1
    assert sum(p.stat().st_size for p in session.evidence.iterdir()) <= ingress.MAX_EVIDENCE_BYTES
    assert not list(session.evidence.glob("*.terminal.json"))
    assert not list(session.evidence.glob("*.response.bin"))
    with pytest.raises(ingress.SoakIngressError, match="session_closed_or_exhausted"):
        session.json("GET", "/api/me")
    assert len(calls) == 1


def test_request_cap_checks_encoded_json_and_full_multipart_before_dispatch(
    ingress,
    make_session,
    monkeypatch,
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"ok")

    session = make_session(handler)
    monkeypatch.setattr(ingress, "MAX_REQUEST_BYTES", 16)
    session.json("POST", "/api/chat", {"x": "a" * 8})
    assert calls[0].read() == b'{"x":"aaaaaaaa"}'
    with pytest.raises(ingress.SoakIngressError, match="request_oversized"):
        session.json("POST", "/api/chat", {"x": "a" * 9})
    monkeypatch.setattr(ingress, "MAX_REQUEST_BYTES", 128)
    with pytest.raises(ingress.SoakIngressError, match="request_oversized"):
        session.upload(filename="ё.txt", content=b"a" * 64, source_ref="case-163")
    assert len(calls) == 1
    assert len(list(session.evidence.glob("*.start.json"))) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/api/files/abc"),
        ("POST", "/api/admin/users"),
        ("GET", "https://untrusted.invalid/api/me"),
        ("GET", "//untrusted.invalid/api/me"),
        ("GET", "/api/files/../me"),
        ("GET", "/api/files/%2e%2e"),
        ("GET", "/api/me?admin=1"),
        ("GET", "/api/files/id/extra"),
    ],
)
def test_unsafe_route_is_refused_before_dispatch_or_evidence(method, path, ingress, make_session):
    def forbidden(_request):
        pytest.fail("unsafe route reached HTTP transport")

    session = make_session(forbidden)
    with pytest.raises(ingress.SoakIngressError, match="route_outside_soak_ingress"):
        session.json(method, path)
    assert list(session.evidence.iterdir()) == []


def test_concurrent_same_actor_request_and_close_are_rejected_without_second_dispatch(
    ingress,
    make_session,
):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def handler(request):
        calls.append(request)
        entered.set()
        assert release.wait(10), "test failed to release blocked mock transport"
        return httpx.Response(200, content=b"ok")

    session = make_session(handler)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(session.json, "GET", "/api/me")
        try:
            assert entered.wait(10), "first request never entered mock transport"
            with pytest.raises(ingress.SoakIngressError, match="concurrent_request_for_same_principal"):
                session.json("GET", "/api/me")
            with pytest.raises(ingress.SoakIngressError, match="request_still_in_flight"):
                session.close()
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=10).sequence == 1
    assert session.json("GET", "/api/me").sequence == 2
    assert len(calls) == 2


def test_request_count_and_closed_session_reject_further_dispatch(ingress, make_session, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"ok")

    monkeypatch.setattr(ingress, "MAX_REQUESTS", 2)
    session = make_session(handler)
    assert session.json("GET", "/api/me").sequence == 1
    assert session.json("GET", "/api/files").sequence == 2
    with pytest.raises(ingress.SoakIngressError, match="session_closed_or_exhausted"):
        session.json("GET", "/api/me")
    session.close()
    session.close()
    with pytest.raises(ingress.SoakIngressError, match="session_closed_or_exhausted"):
        session.json("GET", "/api/me")
    assert len(calls) == 2


class _Bindings:
    """Scripted persisted identity bindings, independent of actor JSON."""

    def __init__(self, bindings):
        self.bindings = dict(bindings)
        self.calls = []

    def resolve_identity(self, source, external_id):
        self.calls.append((source, external_id))
        return self.bindings.get((source, external_id))


def _principal_sessions(make_session, *, aliases=False, mutate=None, status=200):
    bindings, sessions, wires = {}, [], []
    for index in range(4):
        chat_id = 8101 + index
        user_id = "owner-account" if aliases else f"actual-user-{index}"
        preset = "owner" if aliases or index == 0 else "user"
        identity = {
            "actor": {"user_id": user_id, "source": "telegram-bridge", "preset_key": preset},
            "user": {"id": user_id, "status": "active", "preset_key": preset},
        }
        if index == 0 and mutate is not None:
            mutate(identity)
        bindings[("telegram", str(chat_id))] = user_id

        def handler(request, identity=identity):
            wires.append(request)
            assert request.method == "GET" and request.url.path == "/api/me"
            return httpx.Response(status, json=identity)

        sessions.append(make_session(handler, chat_id=chat_id))
    return sessions, _Bindings(bindings), wires


def test_four_principals_bind_actual_actor_and_user_to_each_resolved_identity(ingress, make_session):
    sessions, storage, wires = _principal_sessions(make_session)
    assert ingress.observe_isolated_principals(sessions, storage, shared_archive=False) == (
        "actual-user-0",
        "actual-user-1",
        "actual-user-2",
        "actual-user-3",
    )
    assert storage.calls == [("telegram", str(8101 + index)) for index in range(4)]
    assert len(wires) == 4
    assert len({request.headers["x-friday-user"] for request in wires}) == 4


def test_four_distinct_owner_chats_aliasing_one_account_are_rejected(ingress, make_session):
    sessions, storage, wires = _principal_sessions(make_session, aliases=True)
    assert len({session.chat_id for session in sessions}) == 4
    with pytest.raises(ingress.SoakIngressError, match="principal_aliases_are_not_four_tenants"):
        ingress.observe_isolated_principals(sessions, storage, shared_archive=False)
    assert storage.calls == [("telegram", str(8101 + index)) for index in range(4)]
    assert len(wires) == 4


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("actor", "user_id", "different-user"),
        ("user", "id", "different-user"),
        ("user", "status", "disabled"),
        ("actor", "source", "web"),
        ("actor", "preset_key", "owner-mismatch"),
        ("user", "preset_key", "user"),
    ],
)
def test_principal_response_binding_mismatch_is_rejected(
    section,
    key,
    value,
    ingress,
    make_session,
):
    sessions, storage, wires = _principal_sessions(
        make_session,
        mutate=lambda identity: identity[section].__setitem__(key, value),
    )
    with pytest.raises(ingress.SoakIngressError, match="principal_binding_invalid"):
        ingress.observe_isolated_principals(sessions, storage, shared_archive=False)
    assert len(wires) == 1


@pytest.mark.parametrize("resolved", [None, "another-actual-account"])
def test_valid_actor_user_pair_must_also_match_storage_identity(resolved, ingress, make_session):
    sessions, storage, wires = _principal_sessions(make_session)
    storage.bindings[("telegram", "8101")] = resolved
    with pytest.raises(ingress.SoakIngressError, match="principal_binding_invalid"):
        ingress.observe_isolated_principals(sessions, storage, shared_archive=False)
    assert storage.calls == [("telegram", "8101")]
    assert len(wires) == 1


@pytest.mark.parametrize("shared_archive", [True, None, 0, "false"])
def test_shared_archive_must_be_explicitly_false_before_identity_dispatch(
    shared_archive,
    ingress,
    make_session,
):
    sessions, storage, wires = _principal_sessions(make_session)
    with pytest.raises(ingress.SoakIngressError, match="four_isolated_principals_required"):
        ingress.observe_isolated_principals(sessions, storage, shared_archive=shared_archive)
    assert wires == storage.calls == []


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_repeated_chat_does_not_count_as_four_principals(duplicate, ingress, make_session):
    sessions, storage, wires = _principal_sessions(make_session)
    selected = sessions[:3] + ([sessions[0]] if duplicate else [])
    with pytest.raises(ingress.SoakIngressError, match="four_isolated_principals_required"):
        ingress.observe_isolated_principals(selected, storage, shared_archive=False)
    assert wires == storage.calls == []


@pytest.mark.parametrize("status", [401, 503])
def test_refused_identity_response_cannot_count_as_verified_principal(status, ingress, make_session):
    sessions, storage, wires = _principal_sessions(make_session, status=status)
    with pytest.raises(ingress.SoakIngressError, match="principal_http_refused"):
        ingress.observe_isolated_principals(sessions, storage, shared_archive=False)
    assert len(wires) == 1 and storage.calls == []


@pytest.mark.parametrize(
    "body,error",
    [
        (b"{", "response_json_invalid"),
        (b"\xff", "response_json_invalid"),
        (b"[]", "response_object_required"),
        (b"null", "response_object_required"),
    ],
)
def test_invalid_observed_json_cannot_be_used_as_identity(body, error, ingress, make_session):
    session = make_session(lambda _request: httpx.Response(200, content=body))
    response = session.json("GET", "/api/me")
    with pytest.raises(ingress.SoakIngressError, match=error):
        response.object()
