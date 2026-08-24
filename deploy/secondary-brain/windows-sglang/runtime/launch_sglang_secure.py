"""Launch the pinned SGLang build with file-backed, repr-redacted API auth."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from hardware_runtime_contract import verify_live_hardware_runtime
from profile_contract import load_launch_profile
from source_model_manifest import verify_source_model_snapshot

_KEY_PATH = Path("/run/friday-secrets/sglang-api-key")
_PROFILE_PATH = Path("/run/friday-profile/accepted.json")
_PROFILE_ID_PATH = Path("/run/friday-profile/id")
_HARDWARE_RUNTIME_RECEIPT_PATH = Path("/run/friday-hardware/accepted.json")
_SOURCE_ROOT = Path("/source")


def main() -> None:
    """Build server state only in the parent process, never in spawn imports."""

    # Validate the exact gateway-served bytes and derive every capacity/runtime
    # argument before importing SGLang or constructing CUDA/server state.
    profile = load_launch_profile(
        _PROFILE_PATH,
        _PROFILE_ID_PATH,
        actual_runtime_image=os.environ["FRIDAY_SECONDARY_RUNTIME_IMAGE"],
    )
    verify_live_hardware_runtime(
        _HARDWARE_RUNTIME_RECEIPT_PATH,
        profile.hardware_runtime_receipt_sha256,
    )
    verify_source_model_snapshot(
        _SOURCE_ROOT,
        profile.source_model_manifest_sha256,
    )
    api_key = _KEY_PATH.read_text(encoding="ascii")
    if re.fullmatch(r"[0-9a-f]{64}", api_key) is None:
        raise RuntimeError("invalid file-backed SGLang API key")
    arguments = profile.server_arguments(api_key)

    from sglang.launch_server import run_server  # type: ignore[import-not-found]
    from sglang.srt.plugins import load_plugins  # type: ignore[import-not-found]
    from sglang.srt.server_args import (  # type: ignore[import-not-found]
        ServerArgs,
        prepare_server_args,
    )
    from sglang.srt.utils import kill_process_tree  # type: ignore[import-not-found]

    # Pinned SGLang logs ``ServerArgs`` and its generated dataclass repr includes
    # api_key. Patch that exact representation before parser construction.
    original_repr = ServerArgs.__repr__

    def redacted_repr(value: Any) -> str:
        rendered = str(original_repr(value))
        for secret in (getattr(value, "api_key", None), getattr(value, "admin_api_key", None)):
            if secret:
                rendered = rendered.replace(repr(secret), repr("<redacted>"))
        return rendered

    ServerArgs.__repr__ = redacted_repr
    load_plugins()
    server_args = prepare_server_args(arguments)
    arguments[arguments.index(api_key)] = "<redacted>"
    api_key = ""
    try:
        run_server(server_args)
    finally:
        kill_process_tree(os.getpid(), include_parent=False)


if __name__ == "__main__":
    main()
