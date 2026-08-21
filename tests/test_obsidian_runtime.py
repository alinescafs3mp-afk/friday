from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from friday.organs.obsidian.runtime import ObsidianCompatibilityError, ObsidianRuntime
from friday.organs.obsidian.syncthing import (
    ConfiguredDevice,
    ConfiguredFolder,
    DeviceConnection,
    DiscoveryRelayConfiguration,
    FileAvailability,
    PendingDevice,
    RemoteCompletion,
    SyncthingFileInfo,
    SyncthingFileStatus,
    SyncthingOptions,
    SyncthingReadiness,
    SyncthingSystemStatus,
    SyncthingVersion,
)
from friday.organs.obsidian.vault_store import VaultStore

SERVER_ID = "DOVII4U-SQEEESM-VZ2CVTC-CJM4YN5-QNV7DCU-5U3ASRL-YVFG6TH-W5DV5AA"
PHONE_ID = "YZJBJFX-RDBL7WY-6ZGKJ2D-4MJB4E7-ZATSDUY-LD6Y3L3-MLFUYWE-AEMXJAC"
OTHER_ID = "AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA-AAAAAAA"


class _Client:
    def __init__(self) -> None:
        self.pending = [PendingDevice(PHONE_ID, "Pixel", None, None)]
        self.devices: dict[str, ConfiguredDevice] = {}
        self.folders: dict[str, ConfiguredFolder] = {}
        self.available = True
        self.connected = True
        self.remote_state = "valid"
        self.scans: list[tuple[str, str | None]] = []

    def apply_discovery_relay(self) -> DiscoveryRelayConfiguration:
        return DiscoveryRelayConfiguration(self.get_options(), False)

    def get_options(self) -> SyncthingOptions:
        return SyncthingOptions(("dynamic+https://relays.syncthing.net/endpoint",), False, True, True)

    def list_pending_devices(self):
        return tuple(self.pending)

    def delete_pending_device(self, device_id: str) -> None:
        self.pending = [item for item in self.pending if item.device_id != device_id]

    def list_devices(self):
        return tuple(self.devices.values())

    def post_device(self, configuration) -> None:
        device_id = configuration["deviceID"]
        self.devices[device_id] = ConfiguredDevice(
            device_id, configuration["name"], tuple(configuration["addresses"]), False
        )

    def list_folders(self):
        return tuple(self.folders.values())

    def post_folder(self, configuration) -> None:
        device_ids = tuple(item["deviceID"] for item in configuration["devices"])
        self.folders[configuration["id"]] = ConfiguredFolder(
            configuration["id"],
            configuration["label"],
            configuration["path"],
            device_ids,
            False,
            configuration["type"],
            configuration["versioning"]["type"],
            tuple(sorted(configuration["versioning"]["params"].items())),
            3600,
            "",
            "basic",
        )

    def patch_folder(self, folder_id: str, changes) -> None:
        current = self.folders[folder_id]
        self.folders[folder_id] = ConfiguredFolder(
            current.folder_id,
            current.label,
            current.path,
            tuple(item["deviceID"] for item in changes["devices"]),
            current.paused,
            current.folder_type,
            current.versioning_type,
            current.versioning_params,
            current.versioning_cleanup_interval_s,
            current.versioning_fs_path,
            current.versioning_fs_type,
        )

    def remote_completion(self, folder_id: str, device_id: str) -> RemoteCompletion:
        return RemoteCompletion(folder_id, device_id, 100.0, 0, 0, 1, 1, self.remote_state)

    def scan_folder(self, folder_id: str, *, subpath: str | None = None) -> None:
        self.scans.append((folder_id, subpath))

    def connections(self):
        return (
            DeviceConnection(
                PHONE_ID,
                self.connected,
                False,
                "relay://example",
                "relay-client",
                "syncthing v2.1.3",
                None,
                None,
                False,
                1,
                1,
            ),
        )

    def file_status(self, folder_id: str, path: str) -> SyncthingFileStatus:
        info = SyncthingFileInfo(
            path,
            10,
            None,
            False,
            False,
            False,
            False,
            False,
            "FILE_INFO_TYPE_FILE",
            ("AAAAAAA:1",),
        )
        availability = (FileAvailability(PHONE_ID, False),) if self.available else ()
        return SyncthingFileStatus(folder_id, path, info, info, availability)


class _Manager:
    def __init__(self, client: _Client, *, version: str = "v2.1.3") -> None:
        self.client = client
        self.version = version
        self.profile_id = ""
        self.closed = False

    def ensure_profile(self, spec, **_kwargs):
        self.profile_id = spec.profile_id
        return SyncthingReadiness(
            SyncthingVersion(self.version, None, "linux", "amd64"),
            SyncthingSystemStatus(SERVER_ID, spec.gui_address, 1),
        )

    def client_for(self, profile_id: str):
        assert profile_id == self.profile_id
        return self.client

    def stop_profile(self, profile_id: str, **_kwargs) -> bool:
        assert profile_id == self.profile_id
        return True

    def close(self, **_kwargs) -> None:
        self.closed = True


def _runtime(settings, storage, tmp_path, client: _Client | None = None, *, version="v2.1.3"):
    short_root = tmp_path.parents[1] / f"obs-{hashlib.sha256(str(tmp_path).encode()).hexdigest()[:8]}"
    configured = replace(
        settings,
        obsidian_enabled=True,
        obsidian_root=short_root,
        obsidian_syncthing_binary="/bin/true",
        obsidian_public_base_url="https://friday.example",
        obsidian_pairing_ttl_sec=900,
    )
    selected = client or _Client()
    return ObsidianRuntime(configured, storage, _Manager(selected, version=version)), selected


@pytest.mark.asyncio
async def test_one_phone_flow_uses_fragment_token_and_exact_delivery_evidence(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)

    started = await runtime.start("alice")
    assert started["state"] == "awaiting_android_device"
    assert started["server_device_id"] == SERVER_ID
    assert "/obsidian/setup#" in started["setup_url"]
    raw_token = started["setup_url"].split("#", 1)[1]
    assert raw_token not in str(storage.get_obsidian_onboarding("alice"))

    client.available = False
    offered = await runtime.check("alice")
    assert offered["state"] == "awaiting_obsidian_vault_registration"
    assert storage.get_obsidian_device("alice")["syncthing_device_id"] == PHONE_ID
    assert client.scans[-1][1] == "Friday Connection Test.md"

    pending = await runtime.confirm_open("alice")
    assert pending["state"] == "awaiting_obsidian_vault_registration"
    assert "confirm_open" not in pending["actions"]
    operation = storage.get_obsidian_operation(
        "alice", f"verify:{storage.get_obsidian_onboarding('alice')['id']}"
    )
    assert operation["status"] == "delivery_pending"

    client.available = True
    delivered = await runtime.check("alice")
    assert delivered["state"] == "awaiting_obsidian_vault_registration"
    assert "open_test_note" in delivered["actions"]
    ready = await runtime.confirm_open("alice")
    assert ready["state"] == "ready"
    assert ready["vault"]["open_uri"] == ("obsidian://open?vault=Friday&file=Friday+Connection+Test.md")

    vault_root = storage.get_obsidian_vault("alice")["server_path"]
    conflict = Path(vault_root) / "Note.sync-conflict-20260821.md"
    conflict.write_text("both versions stay", encoding="utf-8")
    diagnostics = await runtime.diagnostics("alice")
    assert diagnostics["conflict_count"] == 1
    assert diagnostics["connection"]["transport"] == "relay"
    assert diagnostics["conflicts"][0]["canonical_path"] == "Note.md"
    assert conflict.exists()


@pytest.mark.asyncio
async def test_multiple_pending_devices_are_never_guessed(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    client = _Client()
    client.pending.append(PendingDevice(OTHER_ID, "Tablet", None, None))
    runtime, _ = _runtime(settings, storage, tmp_path, client)

    await runtime.start("alice")
    panel = await runtime.check("alice")
    assert panel["state"] == "multiple_pending_devices"
    assert len(panel["candidates"]) == 2
    assert storage.get_obsidian_device("alice") is None

    selected = await runtime.select_device("alice", panel["candidates"][1]["id"])
    assert selected["state"] == "awaiting_android_folder_acceptance"
    assert storage.get_obsidian_device("alice")["syncthing_device_id"] in {PHONE_ID, OTHER_ID}


@pytest.mark.asyncio
async def test_setup_capability_is_hash_only_one_use(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, _ = _runtime(settings, storage, tmp_path)
    started = await runtime.start("alice")
    token = started["setup_url"].split("#", 1)[1]

    resolved = await runtime.resolve_public_setup(token)
    assert resolved and resolved["server_device_id"] == SERVER_ID
    assert resolved["requires_obsidian_account"] is False
    assert resolved["requires_qr"] is False
    assert await runtime.resolve_public_setup(token) is None


@pytest.mark.asyncio
async def test_unicode_vault_alias_survives_runtime_restart_and_drives_exact_open_links(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")

    aliased = await runtime.set_vault_alias("alice", "  Личный Vault  ")
    assert aliased["vault"]["android_alias"] == "Личный Vault"
    uri = urlsplit(aliased["vault"]["open_uri"])
    assert uri.scheme == "obsidian"
    assert parse_qs(uri.query) == {
        "vault": ["Личный Vault"],
        "file": ["Friday Connection Test.md"],
    }
    assert aliased["vault"]["open_url"].startswith("https://friday.example/obsidian/open#")

    restarted, _ = _runtime(settings, storage, tmp_path, client)
    restored = await restarted.status("alice")
    assert restored["vault"]["android_alias"] == "Личный Vault"
    assert restored["vault"]["open_uri"] == aliased["vault"]["open_uri"]
    with pytest.raises(ValueError, match="unsafe"):
        await restarted.set_vault_alias("alice", "wrong/name")


@pytest.mark.asyncio
async def test_ready_panel_never_claims_live_sync_when_android_or_daemon_is_offline(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"

    client.connected = False
    offline = await runtime.check("alice")
    assert offline["sync_state"] == "android_offline"
    assert "офлайн" in offline["message"]
    assert "синхронизируются" not in offline["message"]

    runtime.manager.version = "v2.2.0"
    unavailable = await runtime.check("alice")
    assert unavailable["state"] == "ready"
    assert unavailable["sync_state"] == "unavailable"
    assert unavailable["error_code"] == "sync_observation_unavailable"


@pytest.mark.asyncio
async def test_pairing_effects_resume_after_detected_offering_initial_sync_and_cancel(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    candidate = storage.record_obsidian_pairing_candidates(
        "alice", [{"syncthing_device_id": PHONE_ID, "display_name": "Pixel"}]
    )[0]
    storage.select_obsidian_pairing_candidate("alice", candidate["id"])
    storage.transition_obsidian_onboarding("alice", "android_device_detected", pending_device_id=PHONE_ID)
    client.remote_state = "unknown"

    detected_recovered = await runtime.check("alice")
    assert detected_recovered["state"] == "awaiting_android_folder_acceptance"
    assert len(client.devices) == 1
    assert len(client.folders) == 1

    cancelled = await runtime.cancel("alice")
    assert cancelled["state"] == "cancelled"
    retried = await runtime.retry("alice")
    assert retried["state"] == "awaiting_android_folder_acceptance"
    assert len(client.devices) == 1
    assert len(client.folders) == 1

    # A crash after publishing initial_sync is also converged idempotently.
    storage.transition_obsidian_onboarding("alice", "initial_sync")
    storage.update_obsidian_vault("alice", state="initial_sync")
    initial_recovered = await runtime.check("alice")
    assert initial_recovered["state"] == "awaiting_obsidian_vault_registration"
    assert (
        client.scans.count((storage.get_obsidian_vault("alice")["folder_id"], "Friday Connection Test.md"))
        == 1
    )


@pytest.mark.asyncio
async def test_folder_offer_resumes_when_a_crash_left_the_offering_state(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    client.devices[PHONE_ID] = ConfiguredDevice(PHONE_ID, "Pixel", ("dynamic",), False)
    storage.bind_obsidian_android_device("alice", syncthing_device_id=PHONE_ID, display_name="Pixel")
    storage.transition_obsidian_onboarding("alice", "android_device_detected", pending_device_id=PHONE_ID)
    storage.transition_obsidian_onboarding("alice", "offering_folder")
    client.remote_state = "unknown"

    recovered = await runtime.check("alice")
    assert recovered["state"] == "awaiting_android_folder_acceptance"
    assert len(client.folders) == 1


@pytest.mark.asyncio
async def test_unsupported_syncthing_version_fails_closed(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, _ = _runtime(settings, storage, tmp_path, version="v2.2.0")

    panel = await runtime.start("alice")
    assert panel["state"] == "failed"
    assert panel["error_code"] == "syncthing_unavailable"
    assert storage.get_obsidian_profile("alice")["state"] == "failed"


@pytest.mark.asyncio
async def test_profile_limit_refuses_a_new_bundle_without_starting_a_process(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    runtime, _ = _runtime(settings, storage, tmp_path)
    runtime.settings = replace(runtime.settings, obsidian_max_profiles=1)
    assert (await runtime.start("alice"))["state"] == "awaiting_android_device"

    refused = await runtime.start("bob")

    assert refused["state"] == "not_connected"
    assert refused["error_code"] == "profile_limit"
    assert refused["actions"] == []
    assert storage.get_obsidian_profile("bob") is None


@pytest.mark.asyncio
async def test_ready_vault_exposes_durable_native_note_operations(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"

    created = await runtime.create_note(
        "alice",
        "create-architecture",
        "Projects/Architecture",
        "# Architecture\n\nInitial plan.",
        properties={
            "status": "review",
            "due": {"type": "date", "value": "2026-08-22"},
        },
    )
    assert created["status"] == "delivered"
    assert created["path"] == "Projects/Architecture.md"
    assert created["replayed"] is False
    assert created["delivery"]["android_received"] is True
    assert created["delivery"]["obsidian_opened"] is False

    replay = await runtime.create_note(
        "alice",
        "create-architecture",
        "Projects/Architecture",
        "# Architecture\n\nInitial plan.",
        properties={
            "status": "review",
            "due": {"type": "date", "value": "2026-08-22"},
        },
    )
    assert replay["revision"] == created["revision"]
    assert replay["replayed"] is True

    appended = await runtime.append_note(
        "alice",
        "append-architecture",
        created["path"],
        "Implementation started.",
        expected_revision=created["revision"],
    )
    assert appended["status"] == "delivered"
    document = await runtime.read_note("alice", created["path"])
    assert "Implementation started." in document["body"]
    assert document["properties"]["due"] == {"type": "date", "value": "2026-08-22"}

    assert len(await runtime.list_notes("alice")) == 2  # includes the connection test
    matches = await runtime.search_notes("alice", "Implementation", limit=5)
    assert matches[0]["path"] == created["path"]
    assert client.scans[-1] == (
        storage.get_obsidian_vault("alice")["folder_id"],
        created["path"],
    )

    daily = await runtime.daily_note(
        "alice",
        "daily-2026-08-21",
        "2026-08-21",
        content="- [ ] Verify Android sync",
    )
    assert daily["path"] == "Daily/2026-08-21.md"
    assert (await runtime.get_operation("alice", "daily-2026-08-21"))["status"] == "delivered"


@pytest.mark.asyncio
async def test_offline_android_keeps_local_commit_pending_without_false_delivery(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"

    client.connected = False
    client.available = False
    result = await runtime.create_note(
        "alice",
        "offline-create",
        "Offline.md",
        "Saved locally.",
    )
    assert result["status"] == "delivery_pending"
    assert result["delivery"] == {
        "local_write_complete": True,
        "server_scan_complete": True,
        "android_connected": False,
        "android_completion": 100.0,
        "android_received": False,
        "obsidian_opened": False,
    }
    assert Path(storage.get_obsidian_vault("alice")["server_path"], "Offline.md").exists()


@pytest.mark.asyncio
async def test_unexpected_remote_device_blocks_note_writes_fail_closed(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    client.devices[OTHER_ID] = ConfiguredDevice(
        OTHER_ID,
        "unexpected peer",
        ("dynamic",),
        False,
    )

    with pytest.raises(ObsidianCompatibilityError):
        await runtime.create_note("alice", "must-not-run", "Private.md", "secret")

    vault = Path(storage.get_obsidian_vault("alice")["server_path"])
    assert not (vault / "Private.md").exists()
    assert storage.get_obsidian_operation("alice", "must-not-run") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["paused", "versioning", "versioning_path"])
async def test_unsafe_folder_policy_drift_blocks_note_writes_fail_closed(
    settings, storage, tmp_path, drift: str
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    folder_id, folder = next(iter(client.folders.items()))
    client.folders[folder_id] = replace(
        folder,
        paused=drift == "paused",
        versioning_type="" if drift == "versioning" else folder.versioning_type,
        versioning_fs_path="/outside-vault" if drift == "versioning_path" else "",
    )

    with pytest.raises(ObsidianCompatibilityError):
        await runtime.create_note("alice", f"drift-{drift}", "Private.md", "secret")

    vault = Path(storage.get_obsidian_vault("alice")["server_path"])
    assert not (vault / "Private.md").exists()
    assert storage.get_obsidian_operation("alice", f"drift-{drift}") is None


@pytest.mark.asyncio
async def test_newer_path_revision_never_proves_delivery_of_an_older_operation(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"

    client.available = False
    first = await runtime.create_note("alice", "revision-a", "Race.md", "revision A")
    assert first["status"] == "delivery_pending"
    assert first["delivery"]["android_received"] is False

    vault = storage.get_obsidian_vault("alice")
    VaultStore(vault["server_path"]).write_text(
        "Race.md",
        "revision B from another writer",
        expected_revision=first["revision"],
    )
    client.available = True
    observed = await runtime.get_operation("alice", "revision-a")
    assert observed["status"] == "delivery_pending"
    assert observed["revision"] == first["revision"]
    assert observed["delivery"]["android_received"] is False


@pytest.mark.asyncio
async def test_note_revision_changing_during_remote_observation_never_proves_delivery(
    settings, storage, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"

    client.available = False
    first = await runtime.create_note("alice", "delivery-race", "Delivery Race.md", "revision A")
    vault = storage.get_obsidian_vault("alice")
    store = VaultStore(vault["server_path"])
    revision_a = store.read("Delivery Race.md")
    original_file_status = client.file_status
    original_completion = client.remote_completion
    raced = False
    revision_b: str | None = None

    def racing_file_status(folder_id: str, path: str):
        nonlocal raced, revision_b
        if path == "Delivery Race.md" and not raced:
            raced = True
            revision_b = store.write_text(
                path,
                "revision B from Android",
                expected_revision=first["revision"],
            ).revision
        return original_file_status(folder_id, path)

    def racing_completion(folder_id: str, device_id: str):
        assert revision_b is not None
        store.write("Delivery Race.md", revision_a.content, expected_revision=revision_b)
        return original_completion(folder_id, device_id)

    monkeypatch.setattr(client, "file_status", racing_file_status)
    monkeypatch.setattr(client, "remote_completion", racing_completion)
    client.available = True
    observed = await runtime.get_operation("alice", "delivery-race")

    assert raced is True
    restored = store.read("Delivery Race.md")
    assert restored.revision == first["revision"]
    assert restored.generation != revision_a.generation
    assert observed["status"] == "delivery_pending"
    assert observed["delivery"]["android_received"] is False


@pytest.mark.asyncio
async def test_onboarding_revision_changing_during_remote_observation_stays_pending(
    settings, storage, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    client.available = False
    pending = await runtime.check("alice")
    assert pending["state"] == "awaiting_obsidian_vault_registration"

    session = storage.get_obsidian_onboarding("alice")
    operation_id = f"verify:{session['id']}"
    operation = storage.get_obsidian_operation("alice", operation_id)
    expected_revision = json.loads(operation["result_json"])["revision"]
    vault = storage.get_obsidian_vault("alice")
    store = VaultStore(vault["server_path"])
    revision_a = store.read("Friday Connection Test.md")
    original_file_status = client.file_status
    original_completion = client.remote_completion
    raced = False
    revision_b: str | None = None

    def racing_file_status(folder_id: str, path: str):
        nonlocal raced, revision_b
        if path == "Friday Connection Test.md" and not raced:
            raced = True
            revision_b = store.write_text(
                path,
                "a newer Android-side test note",
                expected_revision=expected_revision,
            ).revision
        return original_file_status(folder_id, path)

    def racing_completion(folder_id: str, device_id: str):
        assert revision_b is not None
        store.write("Friday Connection Test.md", revision_a.content, expected_revision=revision_b)
        return original_completion(folder_id, device_id)

    monkeypatch.setattr(client, "file_status", racing_file_status)
    monkeypatch.setattr(client, "remote_completion", racing_completion)
    client.available = True
    observed = await runtime.check("alice")
    stored = storage.get_obsidian_operation("alice", operation_id)

    assert raced is True
    restored = store.read("Friday Connection Test.md")
    assert restored.revision == expected_revision
    assert restored.generation != revision_a.generation
    assert observed["state"] == "awaiting_obsidian_vault_registration"
    assert "open_test_note" not in observed["actions"]
    assert stored["status"] != "delivered"
    assert json.loads(stored["delivery_json"])["android_received"] is False
