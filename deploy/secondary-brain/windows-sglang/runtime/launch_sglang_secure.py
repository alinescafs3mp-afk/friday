"""Launch the pinned SGLang build with file-backed, repr-redacted API auth."""

from __future__ import annotations

import os
import re
from pathlib import Path

from sglang.launch_server import run_server  # type: ignore[import-not-found]
from sglang.srt.plugins import load_plugins  # type: ignore[import-not-found]
from sglang.srt.server_args import (  # type: ignore[import-not-found]
    ServerArgs,
    prepare_server_args,
)
from sglang.srt.utils import kill_process_tree  # type: ignore[import-not-found]

_KEY_PATH = Path("/run/friday-secrets/sglang-api-key")

# Pinned SGLang v0.5.16 logs ``ServerArgs`` and its generated dataclass repr
# includes api_key. Patch that one representation before argument parsing or
# server construction; the live admission scan proves the secret is absent
# from argv, environment and complete container logs.
_original_repr = ServerArgs.__repr__


def _redacted_repr(value: ServerArgs) -> str:
    rendered = _original_repr(value)
    for secret in (getattr(value, "api_key", None), getattr(value, "admin_api_key", None)):
        if secret:
            rendered = rendered.replace(repr(secret), repr("<redacted>"))
    return rendered


ServerArgs.__repr__ = _redacted_repr


def main() -> None:
    """Build server state only in the parent process, never in spawn imports."""

    api_key = _KEY_PATH.read_text(encoding="ascii")
    if re.fullmatch(r"[0-9a-f]{64}", api_key) is None:
        raise RuntimeError("invalid file-backed SGLang API key")
    arguments = [
        "--model-path",
        "/models/gpt-oss-20b-nvfp4-modelopt/snapshot",
        "--served-model-name",
        "friday-secondary-gptoss20b",
        "--host",
        "0.0.0.0",
        "--port",
        "30000",
        "--api-key",
        api_key,
        "--reasoning-parser",
        "gpt-oss",
        "--tool-call-parser",
        "gpt-oss",
        "--attention-backend",
        "triton",
        "--fp4-gemm-backend",
        "flashinfer_cutlass",
        "--kv-cache-dtype",
        "fp8_e4m3",
        "--chunked-prefill-size",
        "1024",
        "--max-running-requests",
        "1",
        "--cuda-graph-max-bs",
        "1",
        "--context-length",
        os.environ["FRIDAY_SECONDARY_CONTEXT_TOKENS"],
        "--max-total-tokens",
        os.environ["FRIDAY_SECONDARY_MAX_TOTAL_TOKENS"],
        "--mem-fraction-static",
        os.environ["FRIDAY_SECONDARY_MEM_FRACTION_STATIC"],
        "--enable-metrics",
        "--enable-cache-report",
    ]
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
