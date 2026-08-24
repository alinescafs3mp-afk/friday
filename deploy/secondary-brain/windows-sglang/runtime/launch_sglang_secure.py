"""Launch the pinned SGLang build with file-backed, repr-redacted API auth."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import stat
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
_RUNTIME_EPOCH_ROOT = Path("/run/friday-runtime-epoch")
_RUNTIME_EPOCH_PATH = _RUNTIME_EPOCH_ROOT / "process-start-time-seconds"
_SGLANG_REASONER_GRAMMAR_PATH = Path(
    "/sgl-workspace/sglang/python/sglang/srt/constrained/reasoner_grammar_backend.py"
)


def _verify_sglang_compat_patch(path: Path, expected_sha256: str) -> None:
    """Fail closed unless the mounted GPT-OSS grammar fix is the accepted file."""

    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError("SGLang compatibility patch is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or len(raw) == 0
        or len(raw) > 131_072
        or not secrets.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256)
    ):
        raise RuntimeError("SGLang compatibility patch does not match the accepted profile")


def _process_start_epoch(proc_stat: str, self_stat: str, clock_ticks: int) -> str:
    boot_rows = [line.split() for line in proc_stat.splitlines() if line.startswith("btime ")]
    closing_parenthesis = self_stat.rfind(")")
    fields = self_stat[closing_parenthesis + 2 :].split() if closing_parenthesis >= 1 else []
    if (
        len(boot_rows) != 1
        or len(boot_rows[0]) != 2
        or len(fields) <= 19
        or not isinstance(clock_ticks, int)
        or isinstance(clock_ticks, bool)
        or clock_ticks <= 0
    ):
        raise RuntimeError("Linux process epoch projection is invalid")
    boot_raw = boot_rows[0][1]
    start_raw = fields[19]
    if (
        re.fullmatch(r"[1-9][0-9]*", boot_raw) is None
        or re.fullmatch(r"[0-9]+", start_raw) is None
        or len(boot_raw) > 20
        or len(start_raw) > 20
    ):
        raise RuntimeError("Linux process epoch projection is invalid")
    try:
        boot_seconds = int(boot_raw)
        start_ticks = int(start_raw)
    except ValueError:
        raise RuntimeError("Linux process epoch projection is invalid") from None
    whole_seconds, remainder_ticks = divmod(start_ticks, clock_ticks)
    epoch_seconds = boot_seconds + whole_seconds
    fraction_nanoseconds = (remainder_ticks * 1_000_000_000) // clock_ticks
    if epoch_seconds <= 0:
        raise RuntimeError("Linux process epoch projection is invalid")
    if fraction_nanoseconds == 0:
        return str(epoch_seconds)
    fraction = f"{fraction_nanoseconds:09d}".rstrip("0")
    return f"{epoch_seconds}.{fraction}"


def _observed_process_start_epoch() -> str:
    proc_stat = Path("/proc/stat").read_text(encoding="ascii")
    self_stat = Path("/proc/self/stat").read_text(encoding="ascii")
    if len(proc_stat) > 1_048_576 or len(self_stat) > 4096:
        raise RuntimeError("Linux process epoch source is oversized")
    return _process_start_epoch(proc_stat, self_stat, int(os.sysconf("SC_CLK_TCK")))


def _publish_runtime_epoch(root: Path = _RUNTIME_EPOCH_ROOT) -> str:
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise RuntimeError("runtime epoch volume is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or stat.S_IMODE(root_metadata.st_mode) != 0o755
    ):
        raise RuntimeError("runtime epoch volume is unsafe")
    value = _observed_process_start_epoch()
    raw = value.encode("ascii")
    target = root / _RUNTIME_EPOCH_PATH.name
    temporary = root / f".{target.name}.tmp-{os.getpid()}-{secrets.token_hex(16)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o400,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o444)
        os.replace(temporary, target)
        directory_descriptor = os.open(
            root,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        installed = target.lstat()
        if (
            not stat.S_ISREG(installed.st_mode)
            or target.is_symlink()
            or stat.S_IMODE(installed.st_mode) != 0o444
            or target.read_bytes() != raw
        ):
            raise OSError("runtime epoch installation changed")
    except OSError as exc:
        raise RuntimeError("runtime epoch could not be published") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
    return value


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
    _verify_sglang_compat_patch(
        _SGLANG_REASONER_GRAMMAR_PATH,
        profile.sglang_compat_patch_sha256,
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
    if (
        server_args.mm_feature_transport != "cpu"
        or server_args.language_only
        or server_args.get_model_config().is_multimodal
    ):
        raise RuntimeError("runtime model/feature transport projection is invalid")
    arguments[arguments.index(api_key)] = "<redacted>"
    api_key = ""
    _publish_runtime_epoch()
    try:
        run_server(server_args)
    finally:
        kill_process_tree(os.getpid(), include_parent=False)


if __name__ == "__main__":
    main()
