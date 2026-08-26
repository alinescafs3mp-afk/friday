"""Engineer workbench: static analysis, patch, allowlisted recon, secondary EXTRACT."""

from __future__ import annotations

import hashlib
import io
import socket
import struct
import threading
import zipfile
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from friday.organs import ServiceContext, build_registry
from friday.organs.engineer import artifacts, hosts, hunt
from friday.organs.engineer.advice import advise, unused
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage import normalize_conversation_mode
from friday.storage.models import RawObject, new_id


def _minimal_pe() -> bytes:
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 64)
    pe = bytearray()
    pe += dos
    pe += b"PE\x00\x00"
    pe += struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 0xE0, 0x0102)
    opt = bytearray(0xE0)
    struct.pack_into("<H", opt, 0, 0x10B)
    struct.pack_into("<I", opt, 16, 0x1000)
    struct.pack_into("<I", opt, 28, 0x400000)
    struct.pack_into("<I", opt, 32, 0x1000)
    struct.pack_into("<I", opt, 36, 0x200)
    struct.pack_into("<H", opt, 68, 3)
    struct.pack_into("<H", opt, 92, 16)
    pe += opt
    pe += b".text\x00\x00\x00"
    pe += struct.pack("<IIIIIIHHI", 0x200, 0x1000, 0x200, 0x200, 0, 0, 0, 0, 0x60000020)
    pe.extend(b"\x00" * (0x200 - len(pe)))
    pe.extend(b"VirtualProtect\x00" + b"\x90" * (0x200 - len(b"VirtualProtect\x00")))
    return bytes(pe)


def _minimal_elf() -> bytes:
    ident = bytes([0x7F, 0x45, 0x4C, 0x46, 2, 1, 1, 0]) + b"\x00" * 8
    section_offset = 128
    names = b"\x00.text\x00.shstrtab\x00"
    header = ident + struct.pack(
        "<HHIQQQIHHHHHH",
        2,
        62,
        1,
        0,
        0,
        section_offset,
        0,
        64,
        0,
        0,
        64,
        3,
        2,
    )
    prefix = header + b"/lib64/ld-linux-x86-64.so.2\x00libssl.so.3\x00"
    prefix += b"\x00" * (section_offset - len(prefix))
    section_table = b"\x00" * 64
    section_table += struct.pack("<IIQQQQIIQQ", 1, 1, 6, 0, 0, 0, 0, 0, 16, 0)
    names_offset = section_offset + 3 * 64
    section_table += struct.pack(
        "<IIQQQQIIQQ",
        7,
        3,
        0,
        0,
        names_offset,
        len(names),
        0,
        0,
        1,
        0,
    )
    return prefix + section_table + names


def _zip_with(name: str, payload: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, payload)
        archive.writestr("AndroidManifest.xml", b"manifest")
    return buffer.getvalue()


def test_conversation_mode_accepts_the_operator_spelling():
    assert normalize_conversation_mode("engineer") == "engineer"
    assert normalize_conversation_mode("engeneer") == "engineer"
    with pytest.raises(ValueError):
        normalize_conversation_mode("unsafe-autonomy")


def test_pe_elf_and_apk_reports_are_code_owned():
    pe = artifacts.analyze_bytes(_minimal_pe(), "guard.exe")
    assert pe["ok"] is True
    assert pe["kind"] == "pe"
    assert pe["format"]["readable"] is True
    assert pe["hashes"]["sha256"] == hashlib.sha256(_minimal_pe()).hexdigest()
    assert "suspicious_import" in pe["finding_codes"]

    elf = artifacts.analyze_bytes(_minimal_elf(), "agent.so")
    assert elf["kind"] == "elf"
    assert "libssl.so.3" in elf["format"]["needed"]
    assert elf["format"]["section_names"] == [".text", ".shstrtab"]

    macho = artifacts.analyze_bytes(
        b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x01000007) + b"\x00" * 24,
        "agent.dylib",
    )
    assert macho["kind"] == "macho"
    assert macho["format"]["cpu"] == "x86_64"

    fat_macho = artifacts.analyze_bytes(
        b"\xca\xfe\xba\xbe"
        + struct.pack(">I", 2)
        + struct.pack(">IIIII", 0x01000007, 3, 48, 0, 0)
        + struct.pack(">IIIII", 0x0100000C, 0, 48, 0, 0),
        "universal.dylib",
    )
    assert fat_macho["kind"] == "macho_fat"
    assert fat_macho["format"]["cpus"] == ["x86_64", "arm64"]

    apk = artifacts.analyze_bytes(_zip_with("classes.dex", b"dex\n035\x00"), "app.apk")
    assert apk["kind"] == "apk"
    assert apk["format"]["android_manifest"] is True
    assert "unsigned_apk" in apk["finding_codes"]


def test_replace_bytes_does_not_rewrite_the_source_copy():
    original = b"AAAASECRETAAAA"
    patched, log = artifacts.apply_patches(
        original,
        [{"kind": "replace_bytes", "find": "534543524554", "replace": "4e4f54534543"}],
    )
    assert original == b"AAAASECRETAAAA"
    assert patched == b"AAAANOTSECAAAA"
    assert log[0]["hits"] == 1


def test_zip_replace_marks_the_signature_invalid():
    source = _zip_with("payload.bin", b"old")
    patched, log = artifacts.apply_patches(
        source,
        [{"kind": "zip_replace", "name": "payload.bin", "bytes": "6e6577"}],
    )
    with zipfile.ZipFile(io.BytesIO(patched)) as archive:
        assert archive.read("payload.bin") == b"new"
    assert any(item.get("signature_invalidated") for item in log)


def test_loopback_is_allowed_and_an_open_port_is_reported():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _accept() -> None:
        conn, _addr = listener.accept()
        conn.sendall(b"SSH-2.0-test\r\n")
        conn.close()

    worker = threading.Thread(target=_accept, daemon=True)
    worker.start()
    try:
        report = hosts.audit_host("127.0.0.1", ports=[port])
    finally:
        listener.close()
        worker.join(timeout=2)
    assert report["ok"] is True
    assert port in report["open_ports"]
    assert report["payloads_sent"] is False


def test_a_named_public_host_is_taken_from_chat():
    from friday.organs.engineer.targets import extract_targets

    authority = hosts.resolve_target("8.8.8.8")
    assert authority["addresses"] == ["8.8.8.8"]
    pointed = extract_targets("посмотри 10.1.2.3 и https://branch.example:8443/login")
    hosts_found = [item["host"] for item in pointed]
    assert "10.1.2.3" in hosts_found
    assert "branch.example" in hosts_found


def test_cloud_metadata_stays_forbidden():
    with pytest.raises(ValueError):
        hosts.resolve_target("169.254.169.254")


def test_rehearsal_playbook_does_not_claim_a_payload():
    playbook = hosts.rehearsal_playbook(
        {"host": "10.0.0.5", "weaknesses": [{"code": "smb_open", "detail": "445/tcp"}]}
    )
    assert playbook["payloads_sent"] is False
    assert playbook["steps"][0]["id"] == "smb_open"


@pytest.mark.asyncio
async def test_secondary_advice_is_optional_and_skips_when_absent():
    ctx = SimpleNamespace(ingestion=SimpleNamespace(secondary_brain=None))
    result = await advise(ctx, "artifact", {"kind": "pe", "finding_codes": ["rwx_section"]})
    assert result == unused("absent")


def test_secondary_projection_has_no_target_identity_or_unbounded_artifact_details() -> None:
    host_payload = hosts.public_host_payload(
        {
            "host": "private.example",
            "addresses": ["192.168.1.9"],
            "open_ports": [22, 443],
            "weaknesses": [{"code": "tls_legacy", "detail": "private banner"}],
        }
    )
    assert host_payload == {"open_ports": [22, 443], "weakness_codes": ["tls_legacy"]}

    artifact_payload = artifacts.public_finding_payload(
        {
            "kind": "elf",
            "size_bytes": 99,
            "entropy": 7.5,
            "hashes": {"sha256": "a" * 64},
            "finding_codes": ["gnu_stack_unseen"],
            "format": {
                "section_names": [".text"],
                "imports": ["PrivateImport"],
                "needed": ["private-library.so"],
            },
        }
    )
    assert artifact_payload == {
        "kind": "elf",
        "hashes": {"sha256": "a" * 64},
        "finding_codes": ["gnu_stack_unseen"],
        "section_names": [".text"],
    }


@pytest.mark.asyncio
async def test_secondary_advice_uses_assist_extract(monkeypatch):
    from friday.secondary_brain import (
        ModelUsage,
        ModelWorkload,
        SecondaryMode,
        SecondaryResult,
    )

    captured: dict[str, object] = {}

    class _Scheduler:
        mode = SecondaryMode.ASSIST
        allowed_workloads = frozenset({ModelWorkload.EXTRACT})
        allow_private_text = True
        served_model_alias = "gpt-oss-20b"

        def new_advisory_deadline(self) -> float:
            return 1.0

        async def secondary_preferred_required_result(self, request, primary_fallback, *, validator):
            captured["workload"] = request.workload
            captured["private"] = request.contains_private_text
            result = SecondaryResult(
                visible_content='{"narrative":"RWX section in the sample.","priorities":["check packer"]}',
                structured_output={
                    "narrative": "RWX section in the sample.",
                    "priorities": ["check packer"],
                },
                served_model_alias="gpt-oss-20b",
                usage=ModelUsage(1, 1, 2),
            )
            assert validator(result) is True
            del primary_fallback
            return result

    ctx = SimpleNamespace(ingestion=SimpleNamespace(secondary_brain=_Scheduler()))
    result = await advise(ctx, "artifact", {"kind": "pe", "finding_codes": ["rwx_section"]})
    assert result["used"] is True
    assert result["narrative"].startswith("RWX")
    assert captured["workload"] is ModelWorkload.EXTRACT
    assert captured["private"] is True


@pytest.mark.asyncio
async def test_secondary_narrative_reaches_dossier_as_untrusted_evidence(monkeypatch):
    async def sharpen(_ctx, _kind, _payload):  # noqa: ANN001
        return {
            "used": True,
            "reason": "assist_extract",
            "narrative": "SYNTHETIC-SECONDARY-NARRATIVE",
            "priorities": ["verify the primary finding"],
        }

    monkeypatch.setattr(hunt.advice, "advise", sharpen)
    dossier = {
        "hosts": [{"ok": True, "markdown": "PRIMARY-HOST-EVIDENCE"}],
        "artifacts": [],
    }

    result = await hunt.with_secondary(SimpleNamespace(), dossier)

    assert "PRIMARY-HOST-EVIDENCE" in result["markdown"]
    assert "Untrusted secondary advisory" in result["markdown"]
    assert "SYNTHETIC-SECONDARY-NARRATIVE" in result["markdown"]


@pytest.mark.asyncio
async def test_unused_secondary_preserves_primary_markdown_byte_for_byte(monkeypatch):
    async def unavailable(_ctx, _kind, _payload):  # noqa: ANN001
        return unused("absent")

    monkeypatch.setattr(hunt.advice, "advise", unavailable)
    dossier = {
        "hosts": [{"ok": True, "markdown": "PRIMARY-HOST-EVIDENCE"}],
        "artifacts": [],
    }
    primary = hunt.dossier_markdown(dossier)

    result = await hunt.with_secondary(SimpleNamespace(), {**dossier, "markdown": primary})

    assert result["markdown"] == primary


def test_registry_includes_engineer(settings):
    settings = replace(settings, engineer_mode_enabled=True)
    names = {organ.name for organ in build_registry(settings).organs}
    assert "engineer" in names


def test_engineer_tools_are_owner_only(settings):
    from friday.permissions import ActorContext
    from friday.server import create_app

    app = create_app(replace(settings, engineer_mode_enabled=True))
    with TestClient(app):
        kernel = app.state.kernel
        owner = ActorContext(LEGACY_OWNER_USER_ID, "owner", "api-token")
        user = ActorContext("bob", "user", "telegram")
        owner_tools = set(kernel.get_tool_names(owner))
        user_tools = set(kernel.get_tool_names(user))
        assert "engineer_analyze_artifact" in owner_tools
        assert "engineer_hunt" in owner_tools
        assert "engineer_http_enum" in owner_tools
        assert "engineer_analyze_artifact" not in user_tools


def test_owner_can_enter_engineer_mode_and_a_guest_cannot(settings):
    from friday.server import create_app

    settings = replace(settings, engineer_mode_enabled=True)
    app = create_app(settings)
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        allowed = client.post(
            "/api/conversations/channel/mode",
            headers=owner_headers,
            json={"channel": "api", "channel_id": "owner-desk", "mode": "engineer"},
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["mode"] == "engineer"

        from tests.test_api_vertical_slice import _bridge_json

        denied = _bridge_json(
            client,
            settings,
            "POST",
            "/api/conversations/channel/mode",
            {"channel": "telegram", "channel_id": "5001", "mode": "engineer"},
        )
        assert denied.status_code == 403


def test_analyze_tool_reads_an_owned_file(settings, storage):
    from friday.organs.engineer.tools import build_engineer_tools
    from friday.permissions import ActorContext

    pytest.importorskip("asyncio")
    import asyncio

    owner = LEGACY_OWNER_USER_ID
    storage.ensure_user(owner, preset_key="owner")
    content = _minimal_pe()
    digest = hashlib.sha256(content).hexdigest()
    raw_id = new_id("raw")
    relative = f"owner/{raw_id}.exe"
    path = settings.files_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=owner,
            source="upload",
            source_ref=f"telegram-file:{raw_id}",
            raw_content="",
            content_type="file",
            content_hash=digest,
            metadata_json={
                "filename": "guard.exe",
                "mime_type": "application/vnd.microsoft.portable-executable",
                "uploaded_by": owner,
                "stored_path": relative,
                "sha256": digest,
                "size_bytes": len(content),
            },
        )
    )
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    tools = {tool.name: tool for tool in build_engineer_tools(ctx)}
    actor = ActorContext(owner, "owner", "api-token")
    result = asyncio.run(tools["engineer_analyze_artifact"].handler(actor=actor, raw_id=raw_id))
    assert result["ok"] is True
    assert result["kind"] == "pe"
    assert result["secondary"]["used"] is False

    before = storage.get_raw_object(raw_id, owner)
    source_bytes = path.read_bytes()
    patched = asyncio.run(
        tools["engineer_patch_artifact"].handler(
            actor=actor,
            raw_id=raw_id,
            operations=[{"kind": "write_at", "offset": 0, "bytes": "5a5a"}],
        )
    )
    after = storage.get_raw_object(raw_id, owner)
    assert patched["ok"] is True
    assert before == after
    assert path.read_bytes() == source_bytes
    assert patched["original_sha256"] == digest
    assert patched["patched_sha256"] != digest


@pytest.mark.asyncio
async def test_telegram_engineer_command_sets_the_mode(tmp_path):
    from friday.telegram_bridge import TelegramBridge, TelegramConfig
    from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            engineer_mode_enabled=True,
        )
    )
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/conversations/channel/mode": {"mode": "engineer"}})
    user = {"id": 1001, "first_name": "Alice"}
    await bridge._process_update(
        telegram,
        backend,
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 5001},
                "from": user,
                "text": "/engeneer",
            },
        },
        cached_response=None,
    )
    mode_call = next(call for call in backend.calls if call["path"] == "/api/conversations/channel/mode")
    assert mode_call["body"]["mode"] == "engineer"
    sent = [payload for url, payload in telegram.calls if str(url).endswith("/sendMessage")]
    assert any("Инженерный разбор" in str(item.get("text") or "") for item in sent)
