from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from friday.organs.obsidian.runtime import (
    ObsidianCompatibilityError,
    ObsidianContainmentError,
    ObsidianRuntime,
)
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
_RUNTIME_ROOT_SEQUENCE = itertools.count()


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


class _DeletionAwareClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.deleted_paths: set[str] = set()

    def file_status(self, folder_id: str, path: str) -> SyncthingFileStatus:
        if path not in self.deleted_paths:
            return super().file_status(folder_id, path)
        tombstone = SyncthingFileInfo(
            path,
            0,
            None,
            True,
            False,
            False,
            False,
            False,
            "FILE_INFO_TYPE_FILE",
            ("AAAAAAA:2",),
        )
        return SyncthingFileStatus(folder_id, path, tombstone, tombstone, ())


class _Manager:
    def __init__(
        self,
        client: _Client,
        *,
        version: str = "v2.1.3",
        stop_error: Exception | None = None,
    ) -> None:
        self.client = client
        self.version = version
        self.server_device_id = SERVER_ID
        self.profile_id = ""
        self.ensure_calls = 0
        self.stopped_profile_ids: list[str] = []
        self.stop_error = stop_error
        self.closed = False

    def ensure_profile(self, spec, **_kwargs):
        self.ensure_calls += 1
        self.profile_id = spec.profile_id
        return SyncthingReadiness(
            SyncthingVersion(self.version, None, "linux", "amd64"),
            SyncthingSystemStatus(self.server_device_id, spec.gui_address, 1),
        )

    def client_for(self, profile_id: str):
        assert profile_id == self.profile_id
        return self.client

    def stop_profile(self, profile_id: str, **_kwargs) -> bool:
        assert profile_id == self.profile_id
        self.stopped_profile_ids.append(profile_id)
        if self.stop_error is not None:
            raise self.stop_error
        return True

    def close(self, **_kwargs) -> None:
        self.closed = True


class _BlockingEnsureManager(_Manager):
    def __init__(self, client: _Client) -> None:
        super().__init__(client)
        self.ensure_started = threading.Event()
        self.ensure_release = threading.Event()
        self.stop_started = threading.Event()
        self._profile_lock = threading.Lock()

    def ensure_profile(self, spec, **kwargs):
        with self._profile_lock:
            self.profile_id = spec.profile_id
            self.ensure_started.set()
            if not self.ensure_release.wait(timeout=5.0):
                raise TimeoutError("test did not release ensure_profile")
            return super().ensure_profile(spec, **kwargs)

    def stop_profile(self, profile_id: str, **kwargs) -> bool:
        self.stop_started.set()
        with self._profile_lock:
            return super().stop_profile(profile_id, **kwargs)


class _BlockingPolicyClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.policy_started = threading.Event()
        self.policy_release = threading.Event()

    def apply_discovery_relay(self) -> DiscoveryRelayConfiguration:
        self.policy_started.set()
        if not self.policy_release.wait(timeout=5.0):
            raise TimeoutError("test did not release policy attestation")
        return super().apply_discovery_relay()


def _runtime(settings, storage, tmp_path, client: _Client | None = None, *, version="v2.1.3"):
    identity_path = tmp_path / ".obsidian-runtime-root-id"
    try:
        runtime_identity = identity_path.read_text(encoding="ascii")
    except FileNotFoundError:
        runtime_identity = str(next(_RUNTIME_ROOT_SEQUENCE))
        identity_path.write_text(runtime_identity, encoding="ascii")
        identity_path.chmod(0o600)
    root_identity = f"{tmp_path}:{runtime_identity}".encode()
    short_root = tmp_path.parents[1] / f"obs-{hashlib.sha256(root_identity).hexdigest()[:8]}"
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
async def test_clean_onboarding_uses_the_configured_logical_vault_name(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, _client = _runtime(settings, storage, tmp_path)
    runtime.settings = replace(runtime.settings, obsidian_vault_name="Friday-Test")

    started = await runtime.start("alice")
    vault = storage.get_obsidian_vault("alice")

    assert started["state"] == "awaiting_android_device"
    assert vault is not None
    assert vault["display_name"] == "Friday-Test"
    assert vault["android_vault_name"] == "Friday-Test"
    assert vault["android_path_hint"] == "Documents/Obsidian/Friday-Test"
    assert Path(vault["server_path"]).name == "Friday-Test"


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
    panel = await runtime.status("alice")
    assert panel["conflict_count"] == 1
    assert panel["conflicts"][0]["canonical_path"] == "Note.md"
    assert panel["conflicts"][0]["conflict_path"] == conflict.name
    assert conflict.exists()
    diagnostics = await runtime.diagnostics("alice")
    assert diagnostics["conflict_count"] == 1
    assert diagnostics["connection"]["transport"] == "relay"
    assert diagnostics["conflicts"][0]["canonical_path"] == "Note.md"
    assert conflict.exists()


@pytest.mark.asyncio
async def test_cancelled_diagnostics_reports_offline_without_restarting_profile(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, _ = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.cancel("alice"))["state"] == "cancelled"
    profile = storage.get_obsidian_profile("alice")
    ensure_calls = runtime.manager.ensure_calls
    stopped_profile_ids = tuple(runtime.manager.stopped_profile_ids)

    diagnostics = await runtime.diagnostics("alice")

    assert diagnostics["state"] == "cancelled"
    assert diagnostics["sync_state"] == "unavailable"
    assert diagnostics["connection"] == {"state": "offline", "transport": "none"}
    assert diagnostics["profile"]["state"] == "stopped"
    assert storage.get_obsidian_profile("alice")["state"] == "stopped"
    assert runtime.manager.ensure_calls == ensure_calls
    assert tuple(runtime.manager.stopped_profile_ids) == stopped_profile_ids == (profile["id"],)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ["failed", "disconnected"])
async def test_other_terminal_diagnostics_never_touch_process_manager(
    settings, storage, tmp_path, terminal_state: str
) -> None:
    storage.ensure_user("alice")
    runtime, _ = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    storage.transition_obsidian_onboarding("alice", "failed")
    if terminal_state == "disconnected":
        storage.transition_obsidian_onboarding("alice", "disconnected")
    profile = storage.get_obsidian_profile("alice")
    runtime.manager.stop_profile(profile["id"])
    storage.update_obsidian_profile("alice", state=terminal_state)
    storage.update_obsidian_vault("alice", state=terminal_state)
    storage.update_obsidian_device("alice", state="disconnected")
    ensure_calls = runtime.manager.ensure_calls
    stopped_profile_ids = tuple(runtime.manager.stopped_profile_ids)

    diagnostics = await runtime.diagnostics("alice")

    assert diagnostics["state"] == terminal_state
    assert diagnostics["sync_state"] == "unavailable"
    assert diagnostics["connection"] == {"state": "offline", "transport": "none"}
    assert diagnostics["profile"]["state"] == terminal_state
    assert storage.get_obsidian_profile("alice")["state"] == terminal_state
    assert runtime.manager.ensure_calls == ensure_calls
    assert tuple(runtime.manager.stopped_profile_ids) == stopped_profile_ids == (profile["id"],)


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

    vault = storage.get_obsidian_vault("alice")
    assert vault is not None
    storage.prepare_obsidian_operation(
        "alice",
        operation_id="offline-panel-op",
        vault_id=str(vault["id"]),
        method="create",
        arguments_digest="a" * 64,
    )
    storage.transition_obsidian_operation(
        "alice",
        "offline-panel-op",
        "committed",
        result={"path": "Offline/Pending Delivery.md", "revision": "b" * 64},
        delivery={
            "local_write_complete": True,
            "server_scan_complete": False,
            "android_connected": False,
            "android_completion": None,
            "android_received": False,
            "obsidian_opened": False,
        },
    )

    client.connected = False
    offline = await runtime.check("alice")
    assert offline["sync_state"] == "android_offline"
    assert "офлайн" in offline["message"]
    assert "синхронизируются" not in offline["message"]
    assert offline["operations"][0] == {
        "operation_id": "offline-panel-op",
        "work_item_id": "",
        "method": "create",
        "status": "committed",
        "path": "Offline/Pending Delivery.md",
        "revision": "b" * 64,
        "server_scan_complete": False,
        "android_connected": False,
        "android_received": False,
    }

    runtime.manager.version = "v2.2.0"
    unavailable = await runtime.check("alice")
    assert unavailable["state"] == "ready"
    assert unavailable["sync_state"] == "unavailable"
    assert unavailable["error_code"] == "sync_observation_unavailable"


@pytest.mark.asyncio
async def test_pairing_effects_resume_after_selected_offering_initial_sync_and_cancel(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    candidate = storage.record_obsidian_pairing_candidates(
        "alice", [{"syncthing_device_id": PHONE_ID, "display_name": "Pixel"}]
    )[0]
    storage.select_obsidian_pairing_candidate("alice", candidate["id"])
    client.remote_state = "unknown"

    selected_recovered = await runtime.check("alice")
    assert selected_recovered["state"] == "awaiting_android_folder_acceptance"
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
async def test_folder_offer_resumes_after_post_folder_before_state_transition(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    client.devices[PHONE_ID] = ConfiguredDevice(PHONE_ID, "Pixel", ("dynamic",), False)
    storage.bind_obsidian_android_device("alice", syncthing_device_id=PHONE_ID, display_name="Pixel")
    storage.transition_obsidian_onboarding("alice", "android_device_detected", pending_device_id=PHONE_ID)
    vault = storage.get_obsidian_vault("alice")
    client.post_folder(
        {
            "id": vault["folder_id"],
            "label": vault["display_name"],
            "path": vault["server_path"],
            "type": "sendreceive",
            "devices": [{"deviceID": PHONE_ID}],
            "versioning": {
                "type": "staggered",
                "params": {"cleanoutDays": "365", "maxAge": "31536000"},
            },
            "paused": False,
        }
    )
    client.remote_state = "unknown"

    recovered = await runtime.check("alice")
    assert recovered["state"] == "awaiting_android_folder_acceptance"
    assert len(client.folders) == 1


@pytest.mark.asyncio
async def test_selected_device_posted_before_durable_bind_resumes_safely(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    candidate = storage.record_obsidian_pairing_candidates(
        "alice", [{"syncthing_device_id": PHONE_ID, "display_name": "Pixel"}]
    )[0]
    storage.select_obsidian_pairing_candidate("alice", candidate["id"])
    client.devices[PHONE_ID] = ConfiguredDevice(PHONE_ID, "Pixel", ("dynamic",), False)
    client.remote_state = "unknown"
    assert storage.get_obsidian_device("alice") is None

    recovered = await runtime.check("alice")

    assert recovered["state"] == "awaiting_android_folder_acceptance"
    assert storage.get_obsidian_device("alice")["syncthing_device_id"] == PHONE_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("rogue_binding", ["device", "folder"])
async def test_rogue_prebinding_stops_unselected_profile(
    settings, storage, tmp_path, rogue_binding: str
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    if rogue_binding == "device":
        client.devices[OTHER_ID] = ConfiguredDevice(OTHER_ID, "Rogue", ("dynamic",), False)
    else:
        client.folders["rogue-folder"] = ConfiguredFolder(
            "rogue-folder",
            "Rogue",
            str(tmp_path / "rogue"),
            (OTHER_ID,),
            False,
            "sendreceive",
        )

    panel = await runtime.start("alice")

    profile = storage.get_obsidian_profile("alice")
    assert panel["state"] == "failed"
    assert panel["error_code"] == "syncthing_unavailable"
    assert profile["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [profile["id"]]


@pytest.mark.asyncio
async def test_observed_server_identity_mismatch_stops_profile(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, _ = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    runtime.manager.server_device_id = OTHER_ID

    panel = await runtime.check("alice")

    profile = storage.get_obsidian_profile("alice")
    assert panel["error_code"] == "sync_observation_unavailable"
    assert profile["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [profile["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_state", ["detected_without_bound", "offering_without_folder"])
async def test_incomplete_durable_state_stops_profile(
    settings, storage, tmp_path, invalid_state: str
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    client.devices[PHONE_ID] = ConfiguredDevice(PHONE_ID, "Pixel", ("dynamic",), False)
    if invalid_state == "detected_without_bound":
        candidate = storage.record_obsidian_pairing_candidates(
            "alice", [{"syncthing_device_id": PHONE_ID, "display_name": "Pixel"}]
        )[0]
        storage.select_obsidian_pairing_candidate("alice", candidate["id"])
    else:
        storage.bind_obsidian_android_device("alice", syncthing_device_id=PHONE_ID, display_name="Pixel")
    storage.transition_obsidian_onboarding("alice", "android_device_detected", pending_device_id=PHONE_ID)
    if invalid_state == "offering_without_folder":
        storage.transition_obsidian_onboarding("alice", "offering_folder")

    panel = await runtime.check("alice")

    profile = storage.get_obsidian_profile("alice")
    assert panel["error_code"] == "sync_observation_unavailable"
    assert profile["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [profile["id"]]


@pytest.mark.asyncio
async def test_unsupported_syncthing_version_fails_closed(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, _ = _runtime(settings, storage, tmp_path, version="v2.2.0")

    panel = await runtime.start("alice")
    assert panel["state"] == "failed"
    assert panel["error_code"] == "syncthing_unavailable"
    assert storage.get_obsidian_profile("alice")["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [storage.get_obsidian_profile("alice")["id"]]


@pytest.mark.asyncio
async def test_unsupported_version_preserves_failure_when_profile_stop_also_fails(
    settings,
    storage,
    tmp_path,
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path, version="v2.2.0")
    runtime.manager = _Manager(client, version="v2.2.0", stop_error=OSError("cannot stop"))

    with pytest.raises(ObsidianContainmentError) as raised:
        await runtime.start("alice")

    profile = storage.get_obsidian_profile("alice")
    assert isinstance(raised.value.__cause__, ObsidianCompatibilityError)
    assert profile["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [profile["id"], profile["id"]]


@pytest.mark.asyncio
async def test_cancellation_during_profile_start_waits_for_exact_profile_stop(
    settings,
    storage,
    tmp_path,
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    manager = _BlockingEnsureManager(client)
    runtime.manager = manager

    started = asyncio.create_task(runtime.start("alice"))
    assert await asyncio.to_thread(manager.ensure_started.wait, 2.0)
    started.cancel()
    assert await asyncio.to_thread(manager.stop_started.wait, 2.0)
    assert manager.stopped_profile_ids == []
    manager.ensure_release.set()

    with pytest.raises(asyncio.CancelledError):
        await started
    profile = storage.get_obsidian_profile("alice")
    assert profile["state"] == "failed"
    assert manager.stopped_profile_ids == [profile["id"]]


@pytest.mark.asyncio
async def test_cancellation_during_policy_attestation_stops_exact_profile(
    settings,
    storage,
    tmp_path,
) -> None:
    storage.ensure_user("alice")
    client = _BlockingPolicyClient()
    runtime, _ = _runtime(settings, storage, tmp_path, client)

    started = asyncio.create_task(runtime.start("alice"))
    assert await asyncio.to_thread(client.policy_started.wait, 2.0)
    started.cancel()
    client.policy_release.set()

    with pytest.raises(asyncio.CancelledError):
        await started
    profile = storage.get_obsidian_profile("alice")
    assert profile["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [profile["id"]]


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

    vault = storage.get_obsidian_vault("alice")
    assert vault is not None
    vault_store = VaultStore(str(vault["server_path"]))
    vault_store.write_text(
        "Templates/Meeting.md",
        "---\ntitle: Meeting template\n---\nprivate template body",
        create_only=True,
    )
    templates = await runtime.list_templates("alice")
    assert templates == [
        {
            "name": "Meeting",
            "path": "Templates/Meeting.md",
            "title": "Meeting template",
            "revision": hashlib.sha256(
                b"---\ntitle: Meeting template\n---\nprivate template body"
            ).hexdigest(),
            "modified_at": templates[0]["modified_at"],
        }
    ]
    assert "body" not in templates[0] and "content" not in templates[0]

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
    created_binding = next(
        item
        for item in storage.list_obsidian_note_bindings("alice")
        if item["current_path"] == created["path"]
    )
    assert created_binding["origin"] == "friday"

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

    assert len(await runtime.list_notes("alice")) == 3  # connection test, template, project note
    matches = await runtime.search_notes("alice", "Implementation", limit=5)
    assert matches[0]["path"] == created["path"]
    assert client.scans[-1] == (
        storage.get_obsidian_vault("alice")["folder_id"],
        created["path"],
    )

    prepended = await runtime.execute_operation(
        "alice",
        {
            "method": "prepend",
            "operation_id": "prepend-architecture",
            "path": created["path"],
            "text": "Current context.",
            "expected_revision": appended["revision"],
        },
    )
    prepended_document = await runtime.read_note("alice", created["path"])
    assert prepended_document["body"].startswith("Current context.\n# Architecture")
    assert prepended_document["properties"]["status"] == {"type": "text", "value": "review"}

    with pytest.raises(ValueError, match="invalid replace operation fields"):
        await runtime.execute_operation(
            "alice",
            {
                "method": "replace",
                "operation_id": "replace-without-cas",
                "path": created["path"],
                "content": "unsafe",
            },
        )
    assert storage.get_obsidian_operation("alice", "replace-without-cas") is None

    replace_payload = {
        "method": "replace",
        "operation_id": "replace-architecture",
        "path": created["path"],
        "content": "# Architecture v2\n\nExact replacement.\n",
        "expected_revision": prepended["revision"],
    }
    replaced = await runtime.execute_operation("alice", replace_payload)
    replaced_replay = await runtime.execute_operation("alice", replace_payload)
    assert replaced["applied"] is True
    assert replaced_replay["replayed"] is True
    assert (await runtime.read_note("alice", created["path"]))["content"] == replace_payload["content"]

    daily = await runtime.daily_note(
        "alice",
        "daily-2026-08-21",
        "2026-08-21",
        content="- [ ] Verify Android sync",
    )
    assert daily["path"] == "Daily/2026-08-21.md"
    assert (await runtime.get_operation("alice", "daily-2026-08-21"))["status"] == "delivered"


@pytest.mark.asyncio
async def test_deleted_note_search_returns_only_an_explicit_tombstone(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, _client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    created = await runtime.create_note(
        "alice",
        "create-delete-me",
        "Scratch/Delete Me.md",
        "temporary",
    )
    assert (await runtime.search_notes("alice", "Delete Me"))[0]["path"] == created["path"]
    vault = storage.get_obsidian_vault("alice")
    store = VaultStore(Path(vault["server_path"]))
    store.delete(created["path"], expected_revision=created["revision"])

    matches = await runtime.search_notes(
        "alice",
        "Delete Me",
        context_key="conv-delete-lifecycle",
    )

    assert matches == [
        {
            "path": "Scratch/Delete Me.md",
            "title": "Delete Me",
            "revision": created["revision"],
            "modified_at": matches[0]["modified_at"],
            "excerpt": "Ранее известная заметка с этой identity была удалена.",
            "score": 1002.0,
            "match_channels": ["tombstone"],
        }
    ]
    frame = storage.get_obsidian_active_frame("alice", "conv-delete-lifecycle")
    assert frame is not None and frame["active_binding_id"] is None

    recreated = await runtime.create_note(
        "alice",
        "recreate-delete-me",
        "Scratch/Delete Me.md",
        "new active identity",
    )
    live = await runtime.search_notes("alice", "Delete Me")
    assert live[0]["path"] == recreated["path"]
    assert live[0]["revision"] == recreated["revision"]
    assert "tombstone" not in live[0]["match_channels"]


@pytest.mark.asyncio
async def test_resume_reuses_daily_operation_identity_without_duplicate_text(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    client.connected = False
    client.available = False
    original = await runtime.daily_note(
        "alice",
        "daily-interrupted",
        "2026-08-22",
        content="Проверка идемпотентности",
        context_key="conv-recovery",
    )
    assert original["status"] == "delivery_pending"

    restarted, _ = _runtime(settings, storage, tmp_path, client)
    client.connected = True
    client.available = True

    resumed = await restarted.workflow_write(
        "alice",
        "new-transport-operation",
        {"action": "resume_previous"},
        context_key="conv-recovery",
    )

    assert resumed["status"] == "resumed"
    assert resumed["operation_id"] == original["operation_id"] == "daily-interrupted"
    assert resumed["delivery"]["android_received"] is True
    assert storage.get_obsidian_operation("alice", "new-transport-operation") is None
    assert (await restarted.get_operation("alice", "daily-interrupted"))["status"] == "delivered"
    document = await restarted.read_note("alice", original["path"])
    assert document["content"].count("Проверка идемпотентности") == 1


@pytest.mark.asyncio
async def test_reconcile_incrementally_indexes_an_android_originated_note(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, _client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    vault = storage.get_obsidian_vault("alice")
    store = VaultStore(Path(vault["server_path"]))
    store.write_text(
        "Mobile/Created On Phone.md",
        "Фиолетовый маршрутизатор и тест обратной синхронизации.",
        create_only=True,
    )

    report = await runtime.reconcile()

    assert report["notes_indexed"] >= 1
    binding = next(
        item
        for item in storage.list_obsidian_note_bindings("alice")
        if item["current_path"] == "Mobile/Created On Phone.md"
    )
    assert binding["origin"] == "android"
    matches = await runtime.search_notes("alice", "фиолетовый маршрутизатор")
    match = next(item for item in matches if item["path"] == "Mobile/Created On Phone.md")
    assert match["origin"] == "android"
    assert match["ownership_mode"] == "user_owned"
    assert match["index_coverage"] == "complete"


@pytest.mark.asyncio
async def test_reconcile_settles_oldest_prepared_create_from_historical_sidecar_without_rewrite(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, _client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    vault = storage.get_obsidian_vault("alice")
    assert vault is not None
    notes = runtime._note_service("alice")
    operation_id = "prepared-create-historical-receipt"
    path = "Recovery/Historical.md"
    content = "Первоначальная версия Friday."
    target_revision = hashlib.sha256(notes.render_create_content(content).encode("utf-8")).hexdigest()
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(vault["id"]),
        method="create",
        arguments_digest=hashlib.sha256(b"historical-create").hexdigest(),
        prepared_result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": path,
            "target_revision": target_revision,
        },
    )
    committed = notes.create_note(path, content, operation_id=operation_id)
    assert committed.revision == target_revision
    user_edit = notes.store.write_text(
        path,
        "Изменённая человеком версия.",
        expected_revision=target_revision,
    )

    report = await runtime.reconcile()

    assert report["operations_refreshed"] == 1
    row = storage.get_obsidian_operation("alice", operation_id)
    assert row is not None
    assert row["status"] == "scan_pending"
    result = json.loads(str(row["result_json"]))
    assert result["schema"] == "friday.obsidian-note-operation.v2"
    assert result["reconciliation_proof"] == "sidecar_committed"
    assert result["revision"] == target_revision
    assert notes.store.read_text(path).revision == user_edit.revision
    assert notes.store.read_text(path).text() == "Изменённая человеком версия."


@pytest.mark.asyncio
async def test_reconcile_keeps_unproved_prepared_create_uncertain_without_vault_write(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, _client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    vault = storage.get_obsidian_vault("alice")
    assert vault is not None
    notes = runtime._note_service("alice")
    operation_id = "prepared-create-without-proof"
    path = "Recovery/Unproved.md"
    target_revision = hashlib.sha256(b"expected but never written").hexdigest()
    storage.prepare_obsidian_operation(
        "alice",
        operation_id=operation_id,
        vault_id=str(vault["id"]),
        method="create",
        arguments_digest=hashlib.sha256(b"unproved-create").hexdigest(),
        prepared_result={
            "schema": "friday.obsidian-note-operation.v1",
            "path": path,
            "target_revision": target_revision,
        },
    )

    report = await runtime.reconcile()

    assert report["operations_refreshed"] == 0
    row = storage.get_obsidian_operation("alice", operation_id)
    assert row is not None and row["status"] == "uncertain"
    assert not notes.store.exists(path)


@pytest.mark.asyncio
async def test_search_reports_partial_revision_pinned_index_coverage(settings, storage, tmp_path) -> None:
    storage.ensure_user("alice")
    runtime, _client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    vault = storage.get_obsidian_vault("alice")
    store = VaultStore(Path(vault["server_path"]))
    body = "Фиолетовый маршрутизатор и длинное описание поиска."
    created = store.write_text("Mobile/Partial Index.md", body, create_only=True)
    await runtime.search_notes("alice", "фиолетовый маршрутизатор")
    binding = next(
        item for item in storage.list_obsidian_note_bindings("alice") if item["current_path"] == created.path
    )
    storage.upsert_obsidian_note_index(
        "alice",
        binding_id=str(binding["id"]),
        revision=created.revision,
        metadata={},
        metadata_coverage="complete",
        body_text="Фиолетовый",
        body_coverage="partial",
        source_size_bytes=len(body.encode("utf-8")),
        title="Partial Index",
        source_modified_at=created.modified_at.isoformat(),
    )

    matches = await runtime.search_notes("alice", "фиолетовый маршрутизатор")
    match = next(item for item in matches if item["path"] == created.path)
    coverage = await runtime.search_index_coverage("alice")

    assert match["index_coverage"] == "partial"
    assert coverage["state"] == "partial"
    assert coverage["complete_notes"] < coverage["known_notes"]


@pytest.mark.asyncio
async def test_workflow_delete_reports_delivery_only_after_syncthing_tombstone(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    client = _DeletionAwareClient()
    runtime, _ = _runtime(settings, storage, tmp_path, client)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    vault = storage.get_obsidian_vault("alice")
    store = VaultStore(Path(vault["server_path"]))
    created = store.write_text("Scratch/Delete Me.md", "temporary", create_only=True)
    assert (await runtime.search_notes("alice", "Delete Me"))[0]["path"] == created.path
    client.deleted_paths.add(created.path)

    receipt = await runtime.workflow_write(
        "alice",
        "delete-through-runtime",
        {"action": "delete_note", "path": created.path},
        context_key="conv-delete-runtime",
    )

    assert receipt["delivery"]["server_scan_complete"] is True
    assert receipt["delivery"]["android_received"] is True
    assert receipt["open_uri"] is None
    assert not store.exists(created.path)


@pytest.mark.asyncio
async def test_runtime_continuation_uses_persisted_second_candidate_and_active_note(
    settings, storage, tmp_path
) -> None:
    storage.ensure_user("alice")
    runtime, _client = _runtime(settings, storage, tmp_path)
    await runtime.start("alice")
    await runtime.check("alice")
    assert (await runtime.confirm_open("alice"))["state"] == "ready"
    vault = storage.get_obsidian_vault("alice")
    store = VaultStore(Path(vault["server_path"]))
    store.write_text("Projects/First.md", "Friday поиск first", create_only=True)
    store.write_text("Projects/Second.md", "Friday поиск second", create_only=True)
    context_key = "conv-stable-candidates"
    matches = await runtime.search_notes(
        "alice",
        "Friday поиск",
        context_key=context_key,
    )
    assert len(matches) >= 2
    expected = matches[1]["path"]

    selected = await runtime.workflow_read(
        "alice",
        {"action": "select_candidate", "ordinal": 2},
        context_key=context_key,
    )
    changed = await runtime.workflow_write(
        "alice",
        "append-to-selected",
        {
            "action": "append_active_section",
            "section": "Следующие шаги",
            "item": "- Проверка семантического индекса",
        },
        context_key=context_key,
    )

    assert selected["path"] == changed["path"] == expected
    assert "## Следующие шаги" in store.read_text(expected).text()
    other = matches[0]["path"]
    assert "## Следующие шаги" not in store.read_text(other).text()


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
    vault = storage.get_obsidian_vault("alice")
    path = Path(vault["server_path"], "Offline.md")
    before = path.read_bytes()
    assert path.exists()

    client.connected = True
    client.available = True
    delivered = await runtime.get_operation("alice", "offline-create")
    repeated_observation = await runtime.get_operation("alice", "offline-create")
    panel = await runtime.status("alice")

    assert delivered["status"] == repeated_observation["status"] == "delivered"
    assert delivered["revision"] == repeated_observation["revision"] == result["revision"]
    assert delivered["delivery"]["android_received"] is True
    assert delivered["open_uri"] == "obsidian://open?vault=Friday&file=Offline.md"
    assert path.read_bytes() == before
    assert path.read_text(encoding="utf-8").count("Saved locally.") == 1
    assert [item["path"] for item in await runtime.list_notes("alice")].count("Offline.md") == 1
    assert (
        next(item for item in panel["operations"] if item["operation_id"] == "offline-create")["status"]
        == "delivered"
    )


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
    profile = storage.get_obsidian_profile("alice")
    assert profile["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [profile["id"]]


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
    profile = storage.get_obsidian_profile("alice")
    assert profile["state"] == "failed"
    assert runtime.manager.stopped_profile_ids == [profile["id"]]


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
