"""Durable one-phone Obsidian onboarding and synchronization orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import urllib.parse
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from .contracts import (
    NoteDocument,
    NoteSearchResult,
    NoteSummary,
    ObsidianNoteError,
    ObsidianVaultConvention,
    PropertyValue,
    VaultDeliveryState,
)
from .operations import DurableNoteResult, NoteSyncRequest, ObsidianOperationService
from .service import ObsidianService
from .syncthing import (
    SyncthingError,
    SyncthingHTTPError,
    SyncthingProcessManager,
    SyncthingProfileSpec,
)
from .vault_store import VaultStore

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")
_DELIVERY_FILE = "Friday Connection Test.md"


class _SyncthingNoteSyncAdapter:
    """Synchronous adapter used inside the note-operation worker thread."""

    def __init__(
        self,
        client: Any,
        *,
        store: VaultStore,
        device_id: str | None,
        obsidian_opened: bool,
    ) -> None:
        self._client = client
        self._store = store
        self._device_id = device_id
        self._obsidian_opened = bool(obsidian_opened)

    def request_scan(self, request: NoteSyncRequest) -> None:
        self._client.scan_folder(request.folder_id, subpath=request.note_path)

    def observe_delivery(self, request: NoteSyncRequest) -> VaultDeliveryState:
        try:
            current = self._store.read(request.note_path)
        except (ObsidianNoteError, ValueError):
            current = None
        if current is None or current.revision != request.revision:
            # Syncthing reports the current path version, not an arbitrary
            # historical revision. Never let a later edit prove delivery of
            # the operation revision being reconciled.
            return VaultDeliveryState.local_only()
        file_state_before = self._client.file_status(request.folder_id, request.note_path)
        if self._device_id is None:
            file_state = self._client.file_status(request.folder_id, request.note_path)
            try:
                confirmed = self._store.read(request.note_path)
            except (ObsidianNoteError, ValueError):
                return VaultDeliveryState.local_only()
            if (
                confirmed.revision != request.revision
                or confirmed.generation != current.generation
                or file_state != file_state_before
            ):
                return VaultDeliveryState.local_only()
            server_scan = bool(file_state.local is not None and file_state.local_matches_global)
            return VaultDeliveryState(
                local_write_complete=True,
                server_scan_complete=server_scan,
                android_connected=False,
                android_completion=None,
                android_received=False,
                obsidian_opened=self._obsidian_opened,
            )
        connected = any(
            item.device_id == self._device_id and item.connected and not item.paused
            for item in self._client.connections()
        )
        completion = self._client.remote_completion(
            request.folder_id,
            self._device_id,
        )
        file_state = self._client.file_status(request.folder_id, request.note_path)
        try:
            confirmed = self._store.read(request.note_path)
        except (ObsidianNoteError, ValueError):
            return VaultDeliveryState.local_only()
        if (
            confirmed.revision != request.revision
            or confirmed.generation != current.generation
            or file_state != file_state_before
        ):
            return VaultDeliveryState.local_only()
        server_scan = bool(file_state.local is not None and file_state.local_matches_global)
        received = bool(
            connected
            and server_scan
            and completion.is_complete
            and str(completion.remote_state or "").casefold() == "valid"
            and file_state.available_on(self._device_id)
        )
        return VaultDeliveryState(
            local_write_complete=True,
            server_scan_complete=server_scan,
            android_connected=connected,
            android_completion=completion.completion_percent,
            android_received=received,
            obsidian_opened=self._obsidian_opened,
        )


class ObsidianCompatibilityError(RuntimeError):
    """The managed Syncthing build is outside Friday's tested contract."""


class ObsidianContainmentError(RuntimeError):
    """Friday could not prove that an untrusted Syncthing profile was stopped."""


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", errors="strict")).hexdigest()


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(str(value or "").strip())
    if match is None:
        raise ObsidianCompatibilityError("unsupported Syncthing version format")
    return tuple(int(item) for item in match.groups())  # type: ignore[return-value]


def _json_value(value: Any) -> Any:
    if isinstance(value, PropertyValue):
        return {"type": value.type.value, "value": _json_value(value.value)}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _note_summary(item: NoteSummary) -> dict[str, Any]:
    return {
        "path": item.path,
        "title": item.title,
        "revision": item.revision,
        "size_bytes": item.size_bytes,
        "modified_at": item.modified_at.isoformat(),
    }


def _search_result(item: NoteSearchResult) -> dict[str, Any]:
    return {
        "path": item.path,
        "title": item.title,
        "revision": item.revision,
        "modified_at": item.modified_at.isoformat(),
        "excerpt": item.excerpt,
        "score": item.score,
        "match_channels": list(item.match_channels),
    }


def _note_document(item: NoteDocument) -> dict[str, Any]:
    return {
        "path": item.path,
        "title": item.title,
        "content": item.content,
        "body": item.body,
        "properties": _json_value(item.properties),
        "revision": item.revision,
        "size_bytes": item.size_bytes,
        "modified_at": item.modified_at.isoformat(),
    }


def _operation_result(item: DurableNoteResult) -> dict[str, Any]:
    return {
        "operation_id": item.operation_id,
        "method": item.method,
        "status": item.status,
        "path": item.path,
        "revision": item.revision,
        "previous_revision": item.previous_revision,
        "created": item.created,
        "applied": item.applied,
        "replayed": item.replayed,
        "delivery": {
            "local_write_complete": item.delivery.local_write_complete,
            "server_scan_complete": item.delivery.server_scan_complete,
            "android_connected": item.delivery.android_connected,
            "android_completion": item.delivery.android_completion,
            "android_received": item.delivery.android_received,
            "obsidian_opened": item.delivery.obsidian_opened,
        },
    }


def _daily_day(value: date | datetime | str | None) -> date | datetime | None:
    if value is None or isinstance(value, (date, datetime)):
        return value
    if not isinstance(value, str) or len(value) > 10:
        raise ValueError("day must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("day must be an ISO date") from exc


def _property_inputs(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or len(value) > 100:
        raise ValueError("properties must be an object with at most 100 fields")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError("property names must be bounded non-empty strings")
        if isinstance(item, Mapping):
            if set(item) != {"type", "value"}:
                raise ValueError("typed properties accept only type and value")
            kind = item.get("type")
            raw = item.get("value")
            if kind == "date" and isinstance(raw, str):
                try:
                    raw = date.fromisoformat(raw)
                except ValueError as exc:
                    raise ValueError("date property must use ISO YYYY-MM-DD") from exc
            elif kind == "datetime" and isinstance(raw, str):
                try:
                    raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("datetime property must use ISO 8601") from exc
            result[key] = {"type": kind, "value": raw}
        else:
            result[key] = item
    return result


class ObsidianRuntime:
    """Own the process, pairing, folder and exact delivery state for each actor."""

    def __init__(self, settings: Any, storage: Any, manager: SyncthingProcessManager) -> None:
        self.settings = settings
        self.storage = storage
        self.manager = manager
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _owner_lock(self, owner_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(str(owner_id), asyncio.Lock())

    def _prototype(self, owner_id: str, *, profile_id: str | None = None) -> SyncthingProfileSpec:
        return SyncthingProfileSpec.for_owner(
            self.settings.obsidian_effective_root,
            owner_id,
            profile_id=profile_id,
            gui_mode="unix",
            binary=self.settings.obsidian_syncthing_binary,
        )

    def _spec(self, owner_id: str, profile: Mapping[str, Any]) -> SyncthingProfileSpec:
        spec = self._prototype(owner_id, profile_id=str(profile["id"]))
        expected = {
            "config_root": str(spec.config_root),
            "database_root": str(spec.data_root),
            "api_endpoint": spec.gui_address,
        }
        if any(str(profile.get(key) or "") != value for key, value in expected.items()):
            raise ObsidianCompatibilityError("persisted Syncthing profile paths do not match its owner")
        return spec

    def _check_version(self, version: str) -> None:
        observed = _version_tuple(version)
        minimum = _version_tuple(self.settings.obsidian_syncthing_min_version)
        maximum = _version_tuple(self.settings.obsidian_syncthing_max_version)
        if not minimum <= observed < maximum:
            raise ObsidianCompatibilityError("Syncthing version is outside the tested range")

    async def _stop_untrusted_profile(
        self,
        owner_id: str,
        profile_id: str,
        failure: BaseException,
    ) -> None:
        """Quiesce an untrusted profile before propagating an attestation failure."""

        stop_failures: list[BaseException] = []
        stop_succeeded = False
        cancelled_during_cleanup = False
        for _attempt in range(2):
            stop_task = asyncio.create_task(
                asyncio.to_thread(self.manager.stop_profile, profile_id)
            )
            while not stop_task.done():
                try:
                    await asyncio.shield(stop_task)
                except asyncio.CancelledError:
                    # Cleanup is a security boundary. A repeated cancellation
                    # must not detach the stop thread and let the daemon outlive
                    # the request that rejected its configuration.
                    cancelled_during_cleanup = True
                except BaseException:  # noqa: BLE001 - classified from the completed task below
                    pass
            try:
                stop_task.result()
            except BaseException as exc:  # noqa: BLE001 - retry and surface containment failure
                stop_failures.append(exc)
            else:
                stop_succeeded = True
                break

        state_failure: Exception | None = None
        try:
            self.storage.update_obsidian_profile(owner_id, state="failed")
        except Exception as exc:  # noqa: BLE001 - preserve the attestation failure
            state_failure = exc

        if not stop_succeeded:
            containment = ObsidianContainmentError(
                "managed Syncthing profile could not be stopped after attestation failure"
            )
            for index, stop_failure in enumerate(stop_failures, start=1):
                containment.add_note(
                    f"stop attempt {index} failed with {type(stop_failure).__name__}: {stop_failure}"
                )
            if state_failure is not None:
                containment.add_note(
                    "failed profile state could not be persisted: "
                    f"{type(state_failure).__name__}: {state_failure}"
                )
            raise containment from failure

        if state_failure is not None:
            failure.add_note("Could not persist failed Syncthing profile state after attestation failure")
        if cancelled_during_cleanup and not isinstance(failure, asyncio.CancelledError):
            cancelled = asyncio.CancelledError()
            cancelled.add_note("Cancellation arrived while an untrusted Syncthing profile was being stopped")
            raise cancelled from failure

    async def _ensure_running(
        self, owner_id: str, *, readiness_timeout: float = 30.0
    ) -> tuple[Mapping[str, Any], Any]:
        profile = self.storage.get_obsidian_profile(owner_id)
        if profile is None:
            raise ValueError("Obsidian profile not found")
        spec = self._spec(owner_id, profile)
        profile_id = str(profile["id"])
        try:
            readiness = await asyncio.to_thread(
                self.manager.ensure_profile,
                spec,
                readiness_timeout=readiness_timeout,
                poll_interval=0.1,
            )
            self._check_version(readiness.version.version)
            client = self.manager.client_for(profile_id)
            connectivity = await asyncio.to_thread(client.apply_discovery_relay)
            if connectivity.restart_required:
                await asyncio.to_thread(self.manager.stop_profile, profile_id)
                readiness = await asyncio.to_thread(
                    self.manager.ensure_profile,
                    spec,
                    readiness_timeout=readiness_timeout,
                    poll_interval=0.1,
                )
                self._check_version(readiness.version.version)
                client = self.manager.client_for(profile_id)
                options = await asyncio.to_thread(client.get_options)
                if not options.is_discovery_relay:
                    raise ObsidianCompatibilityError(
                        "discovery-and-relay policy was not retained after restart"
                    )
            await self._assert_bound_configuration_unchecked(
                owner_id,
                client,
                readiness.status.server_device_id,
            )
            profile = self.storage.update_obsidian_profile(
                owner_id,
                state="running",
                server_device_id=readiness.status.server_device_id,
                syncthing_version=readiness.version.version,
            )
        except BaseException as exc:
            await self._stop_untrusted_profile(owner_id, profile_id, exc)
            raise
        return profile, client

    async def start(self, owner_id: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            return await self._start_locked(owner_id)

    async def _start_locked(self, owner_id: str) -> dict[str, Any]:
        prototype = self._prototype(owner_id)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        expires_at = _iso(_now() + timedelta(seconds=self.settings.obsidian_pairing_ttl_sec))
        try:
            bundle = self.storage.create_obsidian_bundle(
                owner_id,
                config_root=str(prototype.config_root),
                database_root=str(prototype.data_root),
                api_endpoint=prototype.gui_address,
                api_key_ref=f"file:{prototype.config_file}#gui.apikey",
                server_path=str(prototype.vault_root / "Friday"),
                folder_id=f"friday-{prototype.owner_fs_key.removeprefix('owner-')[:32]}",
                setup_token_hash=token_hash,
                expires_at=expires_at,
                convention={"daily_folder": "Daily", "daily_format": "YYYY-MM-DD"},
                max_profiles=self.settings.obsidian_max_profiles,
            )
        except ValueError as exc:
            if str(exc) != "Obsidian profile limit reached":
                raise
            panel = await self._panel(owner_id, error_code="profile_limit")
            return {
                **panel,
                "message": "Достигнут лимит изолированных профилей Obsidian; обратитесь к владельцу Friday.",
                "actions": [],
                "error_code": "profile_limit",
                "sync_state": "unavailable",
            }
        session = bundle["session"]
        if str(session["state"]) == "ready":
            # Repairs a pre-atomic-finalize crash from an older deployment.
            self.storage.finalize_obsidian_onboarding(owner_id)
            try:
                await self._ensure_running(owner_id)
            except (SyncthingError, ObsidianCompatibilityError):
                self.storage.update_obsidian_profile(owner_id, state="failed")
                return await self._panel(owner_id, error_code="syncthing_unavailable")
            return await self._panel(owner_id)

        self.storage.rotate_obsidian_setup_token(
            owner_id,
            setup_token_hash=token_hash,
            expires_at=expires_at,
        )
        state = str(session["state"])
        if state in {"not_connected", "failed", "disconnected", "cancelled"}:
            session = self.storage.transition_obsidian_onboarding(owner_id, "provisioning_server_profile")
            state = str(session["state"])
        try:
            _profile, client = await self._ensure_running(owner_id)
        except (SyncthingError, ObsidianCompatibilityError):
            self.storage.update_obsidian_profile(owner_id, state="failed")
            if state != "failed":
                self.storage.transition_obsidian_onboarding(owner_id, "failed")
            return await self._panel(owner_id, error_code="syncthing_unavailable")

        if state == "provisioning_server_profile":
            resumable = self._resumable_device(owner_id)
            if resumable is not None:
                await self._configure_bound_folder(owner_id, client, resumable)
                state = str(self.storage.get_obsidian_onboarding(owner_id)["state"])
            else:
                session = self.storage.transition_obsidian_onboarding(
                    owner_id,
                    "awaiting_device_id_handoff",
                    device_id_presented=True,
                )
                state = str(session["state"])
        if state == "awaiting_device_id_handoff":
            self.storage.transition_obsidian_onboarding(owner_id, "awaiting_android_device")
        try:
            await self._advance_with_client(owner_id, client, discover_pending=False)
        except (SyncthingError, ObsidianCompatibilityError, ValueError):
            return await self._panel(owner_id, error_code="sync_observation_unavailable")
        setup_url = f"{self.settings.obsidian_public_base_url}/obsidian/setup#{token}"
        return await self._panel(owner_id, setup_url=setup_url)

    async def onboarding(self, owner_id: str) -> dict[str, Any]:
        return await self.status(owner_id)

    async def status(self, owner_id: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            return await self._panel(owner_id)

    async def check(self, owner_id: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            return await self._check_locked(owner_id)

    async def _check_locked(self, owner_id: str, *, readiness_timeout: float = 30.0) -> dict[str, Any]:
        session = self.storage.get_obsidian_onboarding(owner_id)
        if session is None:
            return await self._start_locked(owner_id)
        if str(session["state"]) in {"cancelled", "disconnected", "failed"}:
            return await self._panel(owner_id)
        try:
            _profile, client = await self._ensure_running(owner_id, readiness_timeout=readiness_timeout)
            await self._advance_with_client(owner_id, client)
        except (SyncthingError, ObsidianCompatibilityError, ValueError) as exc:
            if isinstance(exc, (SyncthingError, ObsidianCompatibilityError)):
                profile = self.storage.get_obsidian_profile(owner_id)
                if profile is not None:
                    self.storage.update_obsidian_profile(owner_id, state="failed")
            return await self._panel(owner_id, error_code="sync_observation_unavailable")
        return await self._panel(owner_id)

    def _resumable_device(self, owner_id: str) -> Mapping[str, Any] | None:
        """Recover the durable peer selected before a crash or cancellation."""

        device = self.storage.get_obsidian_device(owner_id)
        if device is not None:
            return device
        session = self.storage.get_obsidian_onboarding(owner_id)
        selected_id = str((session or {}).get("pending_device_id") or "")
        if not selected_id:
            return None
        for candidate in self.storage.list_obsidian_pairing_candidates(owner_id):
            if str(candidate.get("syncthing_device_id") or "") == selected_id:
                return candidate
        return None

    async def _advance_with_client(
        self, owner_id: str, client: Any, *, discover_pending: bool = True
    ) -> None:
        """Idempotently converge every externally visible onboarding effect."""

        session = self.storage.get_obsidian_onboarding(owner_id)
        if session is None:
            raise ValueError("Obsidian onboarding not found")
        state = str(session["state"])
        resumable = self._resumable_device(owner_id)
        if resumable is not None and state in {
            "provisioning_server_profile",
            "awaiting_device_id_handoff",
            "awaiting_android_device",
            "android_device_detected",
            "offering_folder",
        }:
            await self._configure_bound_folder(owner_id, client, resumable)

        session = self.storage.get_obsidian_onboarding(owner_id) or session
        state = str(session["state"])
        if discover_pending and state in {"awaiting_android_device", "multiple_pending_devices"}:
            pending = await asyncio.to_thread(client.list_pending_devices)
            candidates = self.storage.record_obsidian_pairing_candidates(
                owner_id,
                [
                    {
                        "syncthing_device_id": item.device_id,
                        "display_name": item.name or "Android",
                    }
                    for item in pending
                ],
            )
            if len(candidates) == 1:
                selected = self.storage.select_obsidian_pairing_candidate(owner_id, str(candidates[0]["id"]))
                await self._configure_bound_folder(owner_id, client, selected)
            elif len(candidates) > 1 and state != "multiple_pending_devices":
                self.storage.transition_obsidian_onboarding(owner_id, "multiple_pending_devices")

        session = self.storage.get_obsidian_onboarding(owner_id) or session
        state = str(session["state"])
        if state in {"android_device_detected", "offering_folder"}:
            resumable = self._resumable_device(owner_id)
            if resumable is None:
                raise ObsidianCompatibilityError("selected Android device is unavailable")
            await self._configure_bound_folder(owner_id, client, resumable)
        session = self.storage.get_obsidian_onboarding(owner_id) or session
        state = str(session["state"])
        if state == "awaiting_android_folder_acceptance":
            await self._observe_folder_acceptance(owner_id, client)
        session = self.storage.get_obsidian_onboarding(owner_id) or session
        if str(session["state"]) == "initial_sync":
            await self._prepare_verification_note(owner_id, client)
            self.storage.transition_obsidian_onboarding(owner_id, "awaiting_obsidian_vault_registration")
            self.storage.update_obsidian_vault(owner_id, state="awaiting_vault_registration")
            session = self.storage.get_obsidian_onboarding(owner_id) or session
        if str(session["state"]) in {
            "awaiting_obsidian_vault_registration",
            "round_trip_verification",
        }:
            await self._finish_verification(owner_id, client)
        if self.storage.get_obsidian_device(owner_id) is not None:
            await self._assert_bound_configuration(owner_id, client)
            await self._refresh_connection(owner_id, client)
            await asyncio.to_thread(self._scan_conflicts, owner_id)

    async def select_device(self, owner_id: str, candidate_id: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            selected = self.storage.select_obsidian_pairing_candidate(owner_id, candidate_id)
            try:
                _profile, client = await self._ensure_running(owner_id)
                await self._configure_bound_folder(owner_id, client, selected)
            except (SyncthingError, ObsidianCompatibilityError):
                return await self._panel(owner_id, error_code="syncthing_unavailable")
            return await self._panel(owner_id)

    async def _configure_bound_folder(
        self,
        owner_id: str,
        client: Any,
        candidate: Mapping[str, Any],
    ) -> None:
        profile = self.storage.get_obsidian_profile(owner_id)
        if profile is None:
            raise ValueError("Obsidian onboarding not found")
        try:
            await self._configure_bound_folder_unchecked(owner_id, client, candidate)
        except BaseException as exc:
            await self._stop_untrusted_profile(owner_id, str(profile["id"]), exc)
            raise

    async def _configure_bound_folder_unchecked(
        self,
        owner_id: str,
        client: Any,
        candidate: Mapping[str, Any],
    ) -> None:
        device_id = str(candidate["syncthing_device_id"])
        display_name = str(candidate.get("display_name") or "Android")
        profile = self.storage.get_obsidian_profile(owner_id)
        session = self.storage.get_obsidian_onboarding(owner_id)
        if profile is None or session is None:
            raise ValueError("Obsidian onboarding not found")
        server_device_id = str(profile.get("server_device_id") or "")
        if not server_device_id:
            raise ObsidianCompatibilityError("managed Syncthing identity is unavailable")
        configured = {item.device_id: item for item in await asyncio.to_thread(client.list_devices)}
        if set(configured) - {device_id, server_device_id}:
            raise ObsidianCompatibilityError("managed Syncthing profile contains an unexpected remote device")
        if device_id in configured and (
            configured[device_id].auto_accept_folders or configured[device_id].introducer
        ):
            raise ObsidianCompatibilityError("managed remote-device policy is unsafe")
        if device_id not in configured:
            await asyncio.to_thread(
                client.post_device,
                {
                    "deviceID": device_id,
                    "name": display_name,
                    "addresses": ["dynamic"],
                    "autoAcceptFolders": False,
                    "introducer": False,
                    "paused": False,
                },
            )
        try:
            await asyncio.to_thread(client.delete_pending_device, device_id)
        except SyncthingHTTPError as exc:
            if exc.status != 404:
                raise
        self.storage.bind_obsidian_android_device(
            owner_id,
            syncthing_device_id=device_id,
            display_name=display_name,
        )
        session = self.storage.get_obsidian_onboarding(owner_id) or session
        if str(session["state"]) in {
            "provisioning_server_profile",
            "awaiting_device_id_handoff",
            "awaiting_android_device",
            "multiple_pending_devices",
        }:
            session = self.storage.transition_obsidian_onboarding(
                owner_id,
                "android_device_detected",
                pending_device_id=device_id,
            )
        elif str(session["state"]) not in {
            "android_device_detected",
            "offering_folder",
            "awaiting_android_folder_acceptance",
        }:
            raise ObsidianCompatibilityError("Android binding cannot be resumed from this state")

        vault = self.storage.get_obsidian_vault(owner_id)
        if vault is None:
            raise ValueError("Obsidian vault not found")
        folders = {item.folder_id: item for item in await asyncio.to_thread(client.list_folders)}
        if set(folders) - {str(vault["folder_id"])}:
            raise ObsidianCompatibilityError("managed Syncthing profile contains an unexpected folder")
        existing = folders.get(str(vault["folder_id"]))
        if existing is None:
            await asyncio.to_thread(
                client.post_folder,
                {
                    "id": str(vault["folder_id"]),
                    "label": str(vault["display_name"]),
                    "path": str(vault["server_path"]),
                    "type": "sendreceive",
                    "devices": [{"deviceID": device_id}],
                    "versioning": {
                        "type": "staggered",
                        "params": {"cleanoutDays": "365", "maxAge": "31536000"},
                    },
                    "paused": False,
                },
            )
        else:
            if Path(existing.path) != Path(str(vault["server_path"])):
                raise ObsidianCompatibilityError("folder ID is already bound to another path")
            if existing.folder_type != "sendreceive":
                raise ObsidianCompatibilityError("vault folder has an unexpected type")
            if set(existing.device_ids) - {device_id, server_device_id}:
                raise ObsidianCompatibilityError("vault folder is shared with an unexpected device")
            if device_id not in existing.device_ids:
                await asyncio.to_thread(
                    client.patch_folder,
                    str(vault["folder_id"]),
                    {"devices": [{"deviceID": device_id}]},
                )
        # State advances only after each external effect is observable. A crash
        # anywhere above leaves enough durable identity for an idempotent retry.
        session = self.storage.get_obsidian_onboarding(owner_id) or session
        if str(session["state"]) == "android_device_detected":
            session = self.storage.transition_obsidian_onboarding(owner_id, "offering_folder")
        if str(session["state"]) == "offering_folder":
            self.storage.update_obsidian_vault(owner_id, state="offering_folder")
            self.storage.update_obsidian_vault(owner_id, state="awaiting_folder_acceptance")
            self.storage.transition_obsidian_onboarding(owner_id, "awaiting_android_folder_acceptance")

    async def _observe_folder_acceptance(self, owner_id: str, client: Any) -> bool:
        vault = self.storage.get_obsidian_vault(owner_id)
        device = self.storage.get_obsidian_device(owner_id)
        if vault is None or device is None:
            return False
        try:
            completion = await asyncio.to_thread(
                client.remote_completion,
                str(vault["folder_id"]),
                str(device["syncthing_device_id"]),
            )
        except SyncthingHTTPError as exc:
            if exc.status in {404, 409}:
                return False
            raise
        if str(completion.remote_state or "").casefold() != "valid":
            return False
        # Create and scan idempotently before publishing the state that depends
        # on those effects. A crash retries from folder acceptance safely.
        await self._prepare_verification_note(owner_id, client)
        self.storage.transition_obsidian_onboarding(owner_id, "initial_sync")
        self.storage.update_obsidian_vault(owner_id, state="initial_sync")
        self.storage.transition_obsidian_onboarding(owner_id, "awaiting_obsidian_vault_registration")
        self.storage.update_obsidian_vault(owner_id, state="awaiting_vault_registration")
        return True

    async def _prepare_verification_note(self, owner_id: str, client: Any) -> None:
        vault = self.storage.get_obsidian_vault(owner_id)
        session = self.storage.get_obsidian_onboarding(owner_id)
        if vault is None or session is None:
            raise ValueError("Obsidian onboarding aggregate not found")
        operation_id = f"verify:{session['id']}"
        arguments = {
            "path": _DELIVERY_FILE,
            "content": "# Friday Connection Test\n\nIf you can read this note, the free Android sync path works.\n",
        }
        operation, created = self.storage.prepare_obsidian_operation(
            owner_id,
            operation_id=operation_id,
            vault_id=str(vault["id"]),
            method="verification_note",
            arguments_digest=_digest(arguments),
        )
        if created or str(operation["status"]) == "prepared":
            service = ObsidianService(VaultStore(str(vault["server_path"])))
            result = service.create_note(
                _DELIVERY_FILE,
                arguments["content"],
                operation_id=operation_id,
            )
            operation = self.storage.transition_obsidian_operation(
                owner_id,
                operation_id,
                "committed",
                result={
                    "path": result.path,
                    "revision": result.revision,
                    "applied": result.applied,
                },
                delivery={
                    "local_write_complete": True,
                    "server_scan_complete": False,
                    "android_connected": False,
                    "android_completion": None,
                    "android_received": False,
                    "obsidian_opened": False,
                },
            )
        if str(operation["status"]) == "committed":
            await asyncio.to_thread(
                client.scan_folder,
                str(vault["folder_id"]),
                subpath=_DELIVERY_FILE,
            )
            self.storage.transition_obsidian_operation(owner_id, operation_id, "scan_pending")

    async def confirm_open(self, owner_id: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            session = self.storage.get_obsidian_onboarding(owner_id)
            if session is None:
                return await self._start_locked(owner_id)
            state = str(session["state"])
            if state == "awaiting_obsidian_vault_registration":
                operation = self._verification_operation(owner_id)
                delivery = self._stored_delivery(operation)
                if not bool(delivery.get("android_received")):
                    return await self._panel(owner_id, error_code="delivery_pending")
                self.storage.transition_obsidian_onboarding(
                    owner_id,
                    "round_trip_verification",
                    obsidian_opened=True,
                )
                self.storage.update_obsidian_vault(owner_id, state="verifying")
            elif state == "round_trip_verification":
                self.storage.transition_obsidian_onboarding(
                    owner_id,
                    state,
                    obsidian_opened=True,
                )
            else:
                return await self._panel(owner_id)
            try:
                _profile, client = await self._ensure_running(owner_id)
                await self._assert_bound_configuration(owner_id, client)
                await self._finish_verification(owner_id, client)
            except (SyncthingError, ObsidianCompatibilityError):
                return await self._panel(owner_id, error_code="delivery_pending")
            return await self._panel(owner_id)

    async def _finish_verification(self, owner_id: str, client: Any) -> bool:
        vault = self.storage.get_obsidian_vault(owner_id)
        device = self.storage.get_obsidian_device(owner_id)
        session = self.storage.get_obsidian_onboarding(owner_id)
        if vault is None or device is None or session is None:
            return False
        operation_id = f"verify:{session['id']}"
        operation = self.storage.get_obsidian_operation(owner_id, operation_id)
        if operation is None:
            return False
        state = str(operation["status"])
        stored_delivery = self._stored_delivery(operation)
        if state == "delivered" and bool(stored_delivery.get("android_received")):
            if bool(session.get("obsidian_opened_at")):
                stored_delivery["obsidian_opened"] = True
                self.storage.transition_obsidian_operation(
                    owner_id, operation_id, "delivered", delivery=stored_delivery
                )
                if str(session["state"]) == "round_trip_verification":
                    self.storage.finalize_obsidian_onboarding(owner_id)
                return True
            return False
        try:
            operation_result = json.loads(str(operation.get("result_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ObsidianCompatibilityError("verification operation result is invalid") from exc
        expected_revision = operation_result.get("revision")
        if not isinstance(expected_revision, str) or re.fullmatch(r"[0-9a-f]{64}", expected_revision) is None:
            raise ObsidianCompatibilityError("verification operation revision is invalid")
        delivery = await self._delivery(
            client,
            folder_id=str(vault["folder_id"]),
            device_id=str(device["syncthing_device_id"]),
            path=_DELIVERY_FILE,
            vault_root=str(vault["server_path"]),
            expected_revision=expected_revision,
            obsidian_opened=False,
        )
        if delivery["server_scan_complete"] and state == "scan_pending":
            operation = self.storage.transition_obsidian_operation(
                owner_id, operation_id, "scan_complete", delivery=delivery
            )
            state = str(operation["status"])
        if state == "scan_complete" and not delivery["android_received"]:
            operation = self.storage.transition_obsidian_operation(
                owner_id, operation_id, "delivery_pending", delivery=delivery
            )
            state = str(operation["status"])
        if delivery["android_received"] and state in {"scan_complete", "delivery_pending"}:
            self.storage.transition_obsidian_operation(owner_id, operation_id, "delivered", delivery=delivery)
            if bool(session.get("obsidian_opened_at")) and str(session["state"]) == "round_trip_verification":
                self.storage.finalize_obsidian_onboarding(owner_id)
                return True
        return False

    def _verification_operation(self, owner_id: str) -> Mapping[str, Any] | None:
        session = self.storage.get_obsidian_onboarding(owner_id)
        if session is None:
            return None
        return self.storage.get_obsidian_operation(owner_id, f"verify:{session['id']}")

    @staticmethod
    def _stored_delivery(operation: Mapping[str, Any] | None) -> dict[str, Any]:
        try:
            value = json.loads(str((operation or {}).get("delivery_json") or "{}"))
        except (json.JSONDecodeError, TypeError):
            return {}
        return dict(value) if isinstance(value, dict) else {}

    async def _assert_bound_configuration(self, owner_id: str, client: Any) -> None:
        profile = self.storage.get_obsidian_profile(owner_id)
        if profile is None:
            return
        try:
            await self._assert_bound_configuration_unchecked(
                owner_id,
                client,
                str(profile.get("server_device_id") or ""),
            )
        except BaseException as exc:
            await self._stop_untrusted_profile(owner_id, str(profile["id"]), exc)
            raise

    async def _assert_bound_configuration_unchecked(
        self,
        owner_id: str,
        client: Any,
        observed_server_device_id: str,
    ) -> None:
        device = self.storage.get_obsidian_device(owner_id)
        vault = self.storage.get_obsidian_vault(owner_id)
        profile = self.storage.get_obsidian_profile(owner_id)
        session = self.storage.get_obsidian_onboarding(owner_id)
        if vault is None or profile is None or session is None:
            raise ObsidianCompatibilityError("managed Syncthing binding state is incomplete")

        observed_server_id = str(observed_server_device_id or "").strip().upper()
        stored_server_id = str(profile.get("server_device_id") or "").strip().upper()
        if not observed_server_id:
            raise ObsidianCompatibilityError("managed Syncthing identity is unavailable")
        if stored_server_id and stored_server_id != observed_server_id:
            raise ObsidianCompatibilityError("managed Syncthing identity changed")

        state = str(session["state"])
        pending_device_id = str(session.get("pending_device_id") or "").strip().upper()
        bound_device_id = (
            "" if device is None else str(device.get("syncthing_device_id") or "").strip().upper()
        )
        if bound_device_id and pending_device_id and bound_device_id != pending_device_id:
            raise ObsidianCompatibilityError("selected Android identity does not match its binding")
        selected_device_id = bound_device_id or pending_device_id
        if selected_device_id == observed_server_id:
            raise ObsidianCompatibilityError("managed Syncthing identity cannot be its own Android peer")

        devices = {item.device_id: item for item in await asyncio.to_thread(client.list_devices)}
        folders = {item.folder_id: item for item in await asyncio.to_thread(client.list_folders)}
        allowed_device_ids = {observed_server_id}
        if selected_device_id:
            allowed_device_ids.add(selected_device_id)
        if set(devices) - allowed_device_ids:
            raise ObsidianCompatibilityError("managed Syncthing profile remote-device allowlist changed")
        configured = devices.get(selected_device_id)
        if configured is not None and (configured.auto_accept_folders or configured.introducer):
            raise ObsidianCompatibilityError("managed remote-device policy changed")
        if bound_device_id and configured is None:
            raise ObsidianCompatibilityError("bound Android device is absent from Syncthing configuration")

        pre_binding_states = {
            "provisioning_server_profile",
            "awaiting_device_id_handoff",
            "awaiting_android_device",
            "multiple_pending_devices",
        }
        folder_required_states = {
            "offering_folder",
            "awaiting_android_folder_acceptance",
            "initial_sync",
            "awaiting_obsidian_vault_registration",
            "round_trip_verification",
            "ready",
            "disconnected",
        }
        if state in pre_binding_states and not bound_device_id:
            if folders:
                raise ObsidianCompatibilityError(
                    "managed Syncthing profile contains a folder before durable binding"
                )
            return

        if state == "android_device_detected" and (not bound_device_id or configured is None):
            raise ObsidianCompatibilityError(
                "detected Android state has no durable configured device"
            )
        if state in folder_required_states and (not bound_device_id or configured is None):
            raise ObsidianCompatibilityError("bound Android device is required for this onboarding state")

        folder_id = str(vault["folder_id"])
        folder_required = state in folder_required_states
        folder_allowed = bool(bound_device_id) and (
            state in pre_binding_states
            or state == "android_device_detected"
            or folder_required
            or state in {"failed", "cancelled"}
        )
        if not folders:
            if folder_required:
                raise ObsidianCompatibilityError("managed vault folder is absent from Syncthing configuration")
            return
        if not folder_allowed or set(folders) != {folder_id}:
            raise ObsidianCompatibilityError("managed Syncthing profile folder allowlist changed")
        folder = folders[folder_id]
        if (
            folder.label != str(vault["display_name"])
            or Path(folder.path) != Path(str(vault["server_path"]))
            or bound_device_id not in folder.device_ids
            or set(folder.device_ids) - {bound_device_id, observed_server_id}
            or folder.paused
            or folder.folder_type != "sendreceive"
            or folder.versioning_type != "staggered"
            or dict(folder.versioning_params) != {"cleanoutDays": "365", "maxAge": "31536000"}
            or folder.versioning_cleanup_interval_s != 3600
            or folder.versioning_fs_path != ""
            or folder.versioning_fs_type != "basic"
        ):
            raise ObsidianCompatibilityError("managed vault binding changed")

    async def _delivery(
        self,
        client: Any,
        *,
        folder_id: str,
        device_id: str,
        path: str,
        vault_root: str,
        expected_revision: str,
        obsidian_opened: bool,
    ) -> dict[str, Any]:
        try:
            current = await asyncio.to_thread(VaultStore(vault_root).read, path)
        except (ObsidianNoteError, ValueError):
            current = None
        if current is None or current.revision != expected_revision:
            return {
                "local_write_complete": True,
                "server_scan_complete": False,
                "android_connected": False,
                "android_completion": None,
                "android_received": False,
                "obsidian_opened": bool(obsidian_opened),
            }
        connected = False
        for item in await asyncio.to_thread(client.connections):
            if item.device_id == device_id:
                connected = bool(item.connected and not item.paused)
                break
        file_state_before = await asyncio.to_thread(client.file_status, folder_id, path)
        completion = await asyncio.to_thread(client.remote_completion, folder_id, device_id)
        file_state = await asyncio.to_thread(client.file_status, folder_id, path)
        try:
            confirmed = await asyncio.to_thread(VaultStore(vault_root).read, path)
        except (ObsidianNoteError, ValueError):
            confirmed = None
        if (
            confirmed is None
            or confirmed.revision != expected_revision
            or confirmed.generation != current.generation
            or file_state != file_state_before
        ):
            return {
                "local_write_complete": True,
                "server_scan_complete": False,
                "android_connected": False,
                "android_completion": None,
                "android_received": False,
                "obsidian_opened": bool(obsidian_opened),
            }
        server_scan = bool(file_state.local is not None and file_state.local_matches_global)
        received = bool(
            connected
            and server_scan
            and completion.is_complete
            and str(completion.remote_state or "").casefold() == "valid"
            and file_state.available_on(device_id)
        )
        return {
            "local_write_complete": True,
            "server_scan_complete": server_scan,
            "android_connected": connected,
            "android_completion": completion.completion_percent,
            "android_received": received,
            "obsidian_opened": bool(obsidian_opened),
        }

    async def _refresh_connection(self, owner_id: str, client: Any) -> None:
        device = self.storage.get_obsidian_device(owner_id)
        if device is None:
            return
        device_id = str(device["syncthing_device_id"])
        connection = next(
            (item for item in await asyncio.to_thread(client.connections) if item.device_id == device_id),
            None,
        )
        connected = bool(connection and connection.connected and not connection.paused)
        self.storage.update_obsidian_device(
            owner_id,
            state="connected" if connected else "offline",
            seen=connected,
        )

    async def retry(self, owner_id: str) -> dict[str, Any]:
        session = self.storage.get_obsidian_onboarding(owner_id)
        if session is not None and str(session["state"]) in {"failed", "cancelled", "disconnected"}:
            return await self.start(owner_id)
        return await self.check(owner_id)

    async def cancel(self, owner_id: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            session = self.storage.get_obsidian_onboarding(owner_id)
            profile = self.storage.get_obsidian_profile(owner_id)
            if session is None or str(session["state"]) == "ready":
                return await self._panel(owner_id)
            if str(session["state"]) != "cancelled":
                self.storage.transition_obsidian_onboarding(owner_id, "cancelled")
            if profile is not None:
                await asyncio.to_thread(self.manager.stop_profile, str(profile["id"]))
                self.storage.update_obsidian_profile(owner_id, state="stopped")
            self.storage.update_obsidian_vault(owner_id, state="disconnected")
            if self.storage.get_obsidian_device(owner_id) is not None:
                self.storage.update_obsidian_device(owner_id, state="disconnected")
            return await self._panel(owner_id)

    async def resolve_public_setup(self, token: str) -> dict[str, Any] | None:
        raw = str(token or "")
        if not 32 <= len(raw) <= 128 or not raw.isascii():
            return None
        digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
        resolved = self.storage.consume_obsidian_setup_token(digest)
        if resolved is None:
            return None
        return {
            "server_device_id": str(resolved["server_device_id"]),
            "vault_name": str(resolved["display_name"]),
            "android_path_hint": str(resolved["android_path_hint"]),
            "steps": [
                "Install Obsidian and Syncthing-Fork from their official Android sources.",
                "In Syncthing-Fork add a remote device and paste the Friday Device ID.",
                "Return to Telegram and press Check connection.",
                "Accept the Friday folder, then open that folder as an Obsidian vault.",
            ],
            "requires_obsidian_account": False,
            "requires_qr": False,
        }

    async def set_vault_alias(self, owner_id: str, alias: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            self.storage.update_obsidian_vault_alias(owner_id, alias)
            return await self._panel(owner_id)

    async def _panel(
        self,
        owner_id: str,
        *,
        setup_url: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        session = self.storage.get_obsidian_onboarding(owner_id)
        profile = self.storage.get_obsidian_profile(owner_id)
        vault = self.storage.get_obsidian_vault(owner_id)
        device = self.storage.get_obsidian_device(owner_id)
        if session is None:
            return {
                "state": "not_connected",
                "message": "Obsidian ещё не подключён.",
                "actions": ["retry"],
            }
        state = str(session["state"])
        verification_delivery = self._stored_delivery(self._verification_operation(owner_id))
        verification_received = bool(verification_delivery.get("android_received"))
        messages = {
            "provisioning_server_profile": "Friday готовит отдельный защищённый профиль Syncthing.",
            "awaiting_device_id_handoff": "Скопируйте Friday Device ID в Syncthing-Fork на Android.",
            "awaiting_android_device": "Добавьте Friday как удалённое устройство в Syncthing-Fork и нажмите «Проверить».",
            "multiple_pending_devices": "Обнаружено несколько устройств. Выберите свой Android явно.",
            "android_device_detected": "Android обнаружен; Friday настраивает общий vault.",
            "offering_folder": "Friday предлагает Android папку vault.",
            "awaiting_android_folder_acceptance": "Примите папку Friday в Syncthing-Fork.",
            "initial_sync": "Началась первоначальная синхронизация.",
            "awaiting_obsidian_vault_registration": (
                "Откройте Friday Connection Test.md в Obsidian и подтвердите результат."
                if verification_received
                else "Тестовая заметка создана; ждём её точной доставки на подключённый Android."
            ),
            "round_trip_verification": "Тестовая заметка сохранена; ждём точного подтверждения доставки на Android.",
            "ready": "Obsidian подключён; проверяю текущее состояние Android.",
            "cancelled": "Настройка Obsidian отменена; файлы сохранены.",
            "disconnected": "Obsidian отключён; файлы сохранены.",
            "failed": "Syncthing сейчас недоступен. Можно безопасно повторить настройку.",
        }
        actions: list[str] = []
        if state in {
            "awaiting_device_id_handoff",
            "awaiting_android_device",
            "multiple_pending_devices",
            "android_device_detected",
            "offering_folder",
            "awaiting_android_folder_acceptance",
            "initial_sync",
        }:
            actions.extend(["check", "cancel"])
        elif state == "awaiting_obsidian_vault_registration":
            actions.extend(["check", "cancel"])
            if verification_received:
                actions.extend(["open_test_note", "confirm_open"])
        elif state == "round_trip_verification":
            actions.extend(["check", "cancel"])
        elif state in {"failed", "cancelled", "disconnected"}:
            actions.append("retry")
        elif state == "ready":
            actions.append("check")
        sync_state = "onboarding"
        message = messages.get(state, "Настройка Obsidian продолжается.")
        if state == "ready":
            profile_state = str((profile or {}).get("state") or "")
            device_state = str((device or {}).get("state") or "")
            if error_code or profile_state != "running":
                sync_state = "unavailable"
                message = "Vault подключён, но локальная служба синхронизации сейчас недоступна."
            elif device_state != "connected":
                sync_state = "android_offline"
                message = "Vault подключён; Android сейчас офлайн, новые записи будут ждать доставки."
            else:
                sync_state = "android_connected"
                message = "Vault подключён; Android сейчас на связи."
        candidates = (
            [
                {
                    "id": str(item["id"]),
                    "display_name": str(item["display_name"] or "Android"),
                    "short_suffix": str(item["short_suffix"]),
                }
                for item in self.storage.list_obsidian_pairing_candidates(owner_id)
            ]
            if state == "multiple_pending_devices"
            else []
        )
        result: dict[str, Any] = {
            "state": state,
            "message": message,
            "server_device_id": str(profile["server_device_id"] if profile else ""),
            "setup_url": setup_url,
            "candidates": candidates,
            "actions": actions,
            "error_code": error_code,
            "sync_state": sync_state,
            "vault": None
            if vault is None
            else {
                "id": str(vault["id"]),
                "name": str(vault["display_name"]),
                "android_alias": str(vault["android_vault_name"]),
                "state": str(vault["state"]),
                "open_uri": "obsidian://open?"
                + urllib.parse.urlencode(
                    {
                        "vault": str(vault["android_vault_name"]),
                        "file": _DELIVERY_FILE,
                    }
                ),
                "open_url": f"{self.settings.obsidian_public_base_url}/obsidian/open#"
                + urllib.parse.urlencode(
                    {
                        "vault": str(vault["android_vault_name"]),
                        "file": _DELIVERY_FILE,
                    }
                ),
            },
            "android": None
            if device is None
            else {
                "name": str(device["display_name"] or "Android"),
                "state": str(device["state"]),
                "last_seen_at": device["last_seen_at"],
            },
        }
        return result

    async def vaults(self, owner_id: str) -> list[dict[str, Any]]:
        vault = self.storage.get_obsidian_vault(owner_id)
        if vault is None:
            return []
        return [
            {
                "id": str(vault["id"]),
                "name": str(vault["display_name"]),
                "state": str(vault["state"]),
                "android_alias": str(vault["android_vault_name"]),
            }
        ]

    async def diagnostics(self, owner_id: str) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            return await self._diagnostics_locked(owner_id)

    async def _diagnostics_locked(self, owner_id: str) -> dict[str, Any]:
        await asyncio.to_thread(self._scan_conflicts, owner_id)
        panel = await self._panel(owner_id)
        conflicts = self.storage.list_obsidian_conflicts(owner_id)
        session = self.storage.get_obsidian_onboarding(owner_id)
        terminal = bool(
            session is not None
            and str(session["state"]) in {"cancelled", "failed", "disconnected"}
        )
        connection: dict[str, Any] = (
            {"state": "offline", "transport": "none"}
            if terminal
            else {"state": "unavailable", "transport": "unknown"}
        )
        device = self.storage.get_obsidian_device(owner_id)
        if device is not None and not terminal:
            try:
                _profile, client = await self._ensure_running(owner_id)
                await self._assert_bound_configuration(owner_id, client)
                observed = next(
                    (
                        item
                        for item in await asyncio.to_thread(client.connections)
                        if item.device_id == str(device["syncthing_device_id"])
                    ),
                    None,
                )
                if observed is None or not observed.connected or observed.paused:
                    connection = {"state": "offline", "transport": "none"}
                else:
                    connection = {
                        "state": "connected",
                        "transport": "relay" if observed.via_relay else "direct",
                        "connection_type": str(observed.connection_type or "unknown")[:128],
                    }
            except (SyncthingError, ObsidianCompatibilityError, ValueError):
                pass
        if terminal:
            panel = {**panel, "sync_state": "unavailable"}
        return {
            **panel,
            "conflict_count": len(conflicts),
            "conflicts": [
                {
                    "id": str(item["id"]),
                    "canonical_path": str(item["canonical_path"]),
                    "conflict_path": str(item["conflict_path"]),
                    "detected_at": str(item["detected_at"]),
                }
                for item in conflicts
            ],
            "profile": {
                key: value
                for key, value in (self.storage.get_obsidian_profile(owner_id) or {}).items()
                if key in {"state", "server_device_id", "syncthing_version", "updated_at"}
            },
            "connection": connection,
        }

    def _scan_conflicts(self, owner_id: str) -> int:
        vault = self.storage.get_obsidian_vault(owner_id)
        if vault is None:
            return 0
        store = VaultStore(str(vault["server_path"]))
        count = 0
        for conflict_path in store.list_sync_conflict_paths():
            canonical = re.sub(
                r"\.sync-conflict-[^/]+(?=\.md$)",
                "",
                conflict_path,
                flags=re.IGNORECASE,
            )
            self.storage.record_obsidian_conflict(
                owner_id,
                vault_id=str(vault["id"]),
                canonical_path=canonical,
                conflict_path=conflict_path,
            )
            count += 1
        return count

    def _note_service(self, owner_id: str) -> ObsidianService:
        vault = self.storage.get_obsidian_vault(owner_id)
        if vault is None or str(vault.get("state") or "") != "ready":
            raise ValueError("Obsidian vault is not ready")
        try:
            raw_convention = json.loads(str(vault.get("convention_json") or "{}"))
        except json.JSONDecodeError as exc:
            raise ObsidianCompatibilityError("stored vault convention is invalid") from exc
        if not isinstance(raw_convention, Mapping):
            raise ObsidianCompatibilityError("stored vault convention is invalid")
        defaults = ObsidianVaultConvention()
        values: dict[str, str] = {}
        for name in (
            "daily_folder",
            "daily_format",
            "template_folder",
            "attachment_folder",
        ):
            value = raw_convention.get(name, getattr(defaults, name))
            if not isinstance(value, str) or not value or len(value) > 200:
                raise ObsidianCompatibilityError("stored vault convention is invalid")
            values[name] = value
        return ObsidianService(
            VaultStore(str(vault["server_path"])),
            convention=ObsidianVaultConvention(**values),
        )

    async def _operation_service(
        self,
        owner_id: str,
        *,
        synchronize: bool,
        readiness_timeout: float = 30.0,
    ) -> ObsidianOperationService:
        notes = await asyncio.to_thread(self._note_service, owner_id)
        adapter: _SyncthingNoteSyncAdapter | None = None
        if synchronize:
            _profile, client = await self._ensure_running(owner_id, readiness_timeout=readiness_timeout)
            await self._assert_bound_configuration(owner_id, client)
            device = self.storage.get_obsidian_device(owner_id)
            adapter = _SyncthingNoteSyncAdapter(
                client,
                store=notes.store,
                device_id=None if device is None else str(device["syncthing_device_id"]),
                # Onboarding proves that the vault was opened once. It does
                # not prove that Obsidian displayed this particular note.
                obsidian_opened=False,
            )
        return ObsidianOperationService(
            self.storage,
            notes,
            owner_id=owner_id,
            sync=adapter,
        )

    async def list_notes(self, owner_id: str) -> list[dict[str, Any]]:
        service = await self._operation_service(owner_id, synchronize=False)
        items = await asyncio.to_thread(service.list_notes)
        return [_note_summary(item) for item in items]

    async def search_notes(
        self,
        owner_id: str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        service = await self._operation_service(owner_id, synchronize=False)
        items = await asyncio.to_thread(service.search_notes, query, limit=limit)
        return [_search_result(item) for item in items]

    async def read_note(self, owner_id: str, path: str) -> dict[str, Any]:
        service = await self._operation_service(owner_id, synchronize=False)
        item = await asyncio.to_thread(service.read_note, path)
        return _note_document(item)

    async def _run_mutation(
        self,
        owner_id: str,
        method: str,
        operation_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            service = await self._operation_service(owner_id, synchronize=True)
            mutation = getattr(service, method)
            result = await asyncio.to_thread(
                mutation,
                operation_id,
                *args,
                **kwargs,
            )
            replayed = result.replayed
            with suppress(SyncthingError):
                result = await asyncio.to_thread(service.refresh_delivery, operation_id)
            if result.replayed != replayed:
                result = replace(result, replayed=replayed)
            return _operation_result(result)

    async def create_note(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        content: str = "",
        *,
        properties: Mapping[str, Any] | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._run_mutation(
            owner_id,
            "create_note",
            operation_id,
            path,
            content,
            properties=_property_inputs(properties),
            work_item_id=work_item_id,
        )

    async def append_note(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        text: str,
        *,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._run_mutation(
            owner_id,
            "append_note",
            operation_id,
            path,
            text,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )

    async def set_properties(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        properties: Mapping[str, Any],
        *,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._run_mutation(
            owner_id,
            "set_properties",
            operation_id,
            path,
            _property_inputs(properties),
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )

    async def daily_note(
        self,
        owner_id: str,
        operation_id: str,
        day: date | datetime | str | None = None,
        *,
        content: str = "",
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._run_mutation(
            owner_id,
            "daily_note",
            operation_id,
            _daily_day(day),
            content=content,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )

    async def get_operation(
        self,
        owner_id: str,
        operation_id: str,
        *,
        readiness_timeout: float = 30.0,
    ) -> dict[str, Any]:
        lock = await self._owner_lock(owner_id)
        async with lock:
            service = await self._operation_service(
                owner_id,
                synchronize=True,
                readiness_timeout=readiness_timeout,
            )
            try:
                result = await asyncio.to_thread(service.refresh_delivery, operation_id)
            except SyncthingError:
                result = await asyncio.to_thread(service.get_operation, operation_id)
            return _operation_result(result)

    async def execute_operation(
        self,
        owner_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("operation body must be an object")
        method = payload.get("method")
        operation_id = payload.get("operation_id")
        if not isinstance(method, str) or not isinstance(operation_id, str):
            raise ValueError("method and operation_id are required")
        common = {
            "expected_revision": payload.get("expected_revision"),
            "work_item_id": payload.get("work_item_id"),
        }
        if method == "create":
            allowed = {
                "method",
                "operation_id",
                "path",
                "content",
                "properties",
                "work_item_id",
            }
            if set(payload) - allowed or not isinstance(payload.get("path"), str):
                raise ValueError("invalid create operation fields")
            return await self.create_note(
                owner_id,
                operation_id,
                str(payload["path"]),
                payload.get("content", ""),
                properties=payload.get("properties"),
                work_item_id=common["work_item_id"],
            )
        if method == "append":
            allowed = {
                "method",
                "operation_id",
                "path",
                "text",
                "expected_revision",
                "work_item_id",
            }
            if (
                set(payload) - allowed
                or not isinstance(payload.get("path"), str)
                or not isinstance(payload.get("text"), str)
            ):
                raise ValueError("invalid append operation fields")
            return await self.append_note(
                owner_id,
                operation_id,
                str(payload["path"]),
                str(payload["text"]),
                expected_revision=common["expected_revision"],
                work_item_id=common["work_item_id"],
            )
        if method == "set_properties":
            allowed = {
                "method",
                "operation_id",
                "path",
                "properties",
                "expected_revision",
                "work_item_id",
            }
            if (
                set(payload) - allowed
                or not isinstance(payload.get("path"), str)
                or not isinstance(payload.get("properties"), Mapping)
            ):
                raise ValueError("invalid set_properties operation fields")
            return await self.set_properties(
                owner_id,
                operation_id,
                str(payload["path"]),
                payload["properties"],
                expected_revision=common["expected_revision"],
                work_item_id=common["work_item_id"],
            )
        if method == "daily_note":
            allowed = {
                "method",
                "operation_id",
                "day",
                "content",
                "expected_revision",
                "work_item_id",
            }
            if set(payload) - allowed:
                raise ValueError("invalid daily_note operation fields")
            return await self.daily_note(
                owner_id,
                operation_id,
                payload.get("day"),
                content=payload.get("content", ""),
                expected_revision=common["expected_revision"],
                work_item_id=common["work_item_id"],
            )
        raise ValueError("unsupported Obsidian operation method")

    async def reconcile(self) -> dict[str, int]:
        profiles = self.storage.list_obsidian_profiles(limit=self.settings.obsidian_max_profiles)
        selected_owners = {str(profile["user_id"]) for profile in profiles}
        concurrency = min(8, max(1, self.settings.obsidian_max_profiles))
        semaphore = asyncio.Semaphore(concurrency)
        timeout = min(5.0, float(self.settings.obsidian_rest_timeout_sec))

        async def check_owner(owner_id: str) -> bool:
            async with semaphore:
                lock = await self._owner_lock(owner_id)
                async with lock:
                    try:
                        await self._check_locked(owner_id, readiness_timeout=timeout)
                    except Exception:  # noqa: BLE001 - tenant-isolated sweep
                        return False
                    return True

        checks = await asyncio.gather(*(check_owner(str(profile["user_id"])) for profile in profiles))
        checked = sum(checks)
        failed = len(checks) - checked

        # One oldest operation per owner per tick prevents a noisy tenant from
        # starving every later owner while still draining each ledger over time.
        oldest_by_owner: dict[str, Mapping[str, Any]] = {}
        for operation in self.storage.list_pending_obsidian_operations(
            limit=max(1, self.settings.obsidian_max_profiles * 4)
        ):
            owner_id = str(operation["user_id"])
            if (
                owner_id in selected_owners
                and str(operation["status"]) != "uncertain"
                and owner_id not in oldest_by_owner
            ):
                oldest_by_owner[owner_id] = operation

        async def refresh_operation(operation: Mapping[str, Any]) -> bool:
            async with semaphore:
                owner_id = str(operation["user_id"])
                try:
                    await self.get_operation(
                        owner_id,
                        str(operation["id"]),
                        readiness_timeout=timeout,
                    )
                except Exception:  # noqa: BLE001 - durable row remains pending
                    return False
                return True

        refreshed = await asyncio.gather(
            *(refresh_operation(operation) for operation in oldest_by_owner.values())
        )
        operations_refreshed = sum(refreshed)
        failed += len(refreshed) - operations_refreshed
        return {
            "checked": checked,
            "failed": failed,
            "operations_refreshed": operations_refreshed,
        }

    async def close(self) -> None:
        await asyncio.to_thread(self.manager.close)


__all__ = ["ObsidianCompatibilityError", "ObsidianRuntime"]
