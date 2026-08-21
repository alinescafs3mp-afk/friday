from __future__ import annotations

import os
from pathlib import Path

import pytest

from friday.organs.obsidian.syncthing import (
    SyncthingProcessSupervisor,
    SyncthingProfileSpec,
)

PHONE_ID = "YZJBJFX-RDBL7WY-6ZGKJ2D-4MJB4E7-ZATSDUY-LD6Y3L3-MLFUYWE-AEMXJAC"


@pytest.mark.skipif(
    not os.environ.get("FRIDAY_REAL_SYNCTHING_BINARY"),
    reason="set FRIDAY_REAL_SYNCTHING_BINARY for the pinned-binary smoke",
)
def test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract(tmp_path: Path) -> None:
    binary = os.environ["FRIDAY_REAL_SYNCTHING_BINARY"]
    spec = SyncthingProfileSpec.for_owner(
        tmp_path / "obs",
        "live-smoke-owner",
        binary=binary,
    )
    supervisor = SyncthingProcessSupervisor(spec)
    try:
        readiness = supervisor.start(readiness_timeout=15.0)
        assert readiness.version.version == "v2.1.3"
        client = supervisor.client
        connectivity = client.apply_discovery_relay()
        if connectivity.restart_required:
            supervisor.stop()
            readiness = supervisor.start(readiness_timeout=15.0)
            assert readiness.version.version == "v2.1.3"
            client = supervisor.client
        assert client.get_options().is_discovery_relay
        assert client.list_folders() == ()
        local_devices = client.list_devices()
        assert len(local_devices) == 1
        assert local_devices[0].device_id == readiness.status.server_device_id

        client.post_device(
            {
                "deviceID": PHONE_ID,
                "name": "Android smoke",
                "addresses": ["dynamic"],
                "autoAcceptFolders": False,
                "introducer": False,
                "paused": False,
            }
        )
        configured = client.list_devices()
        phone = next(item for item in configured if item.device_id == PHONE_ID)
        assert {item.device_id for item in configured} == {
            readiness.status.server_device_id,
            PHONE_ID,
        }
        assert phone.auto_accept_folders is False
        assert phone.introducer is False

        vault = spec.vault_root / "Friday"
        vault.mkdir(mode=0o700)
        client.post_folder(
            {
                "id": "friday-live-smoke",
                "label": "Friday",
                "path": str(vault),
                "type": "sendreceive",
                "devices": [{"deviceID": PHONE_ID}],
                "versioning": {
                    "type": "staggered",
                    "params": {"cleanoutDays": "365", "maxAge": "31536000"},
                },
                "paused": False,
            }
        )
        folder = client.list_folders()[0]
        assert folder.folder_id == "friday-live-smoke"
        assert folder.path == str(vault)
        assert set(folder.device_ids) == {readiness.status.server_device_id, PHONE_ID}
        assert folder.folder_type == "sendreceive"
        assert folder.versioning_type == "staggered"
        assert dict(folder.versioning_params) == {
            "cleanoutDays": "365",
            "maxAge": "31536000",
        }
        assert folder.versioning_cleanup_interval_s == 3600
        assert folder.versioning_fs_path == ""
        assert folder.versioning_fs_type == "basic"
    finally:
        supervisor.stop()
