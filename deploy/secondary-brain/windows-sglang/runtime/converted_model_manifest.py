"""Stdlib-only, content-free verifier for the internally converted model volume."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "friday.secondary-modelopt-conversion-output.v1"
SOURCE_REPOSITORY = "openai/gpt-oss-20b"
SOURCE_REVISION = "6cee5e81ee83917806bbde320786a8fb61efebee"
SOURCE_MANIFEST_SEMANTIC_SHA256 = "e75b176ed1817e762cf9b7f2262f6e58491a0f9d48d1ea51e466a6e2c3b8a3ab"
MODELOPT_COMMIT = "ec87a82927d003986d44fb7f4fa8b3d10c31b095"
PREFERRED_CONVERTER_IMAGE = (
    "nvcr.io/nvidia/tensorrt-llm/release@"
    "sha256:7202108ab373557e0562f78ef3c0f65bdc70e18cc0b040c8d6805a5cde897a0d"
)
SEALED_ALTERNATIVE_IMAGE = "sha256:b801dc95ca304701242aeeaaeaf64332d67134ba8e56c8c0e74ab2dc77569c7a"
CALIBRATION_SHA256 = "fab1ccffa64af207e663f4acbc382bb4332edd9981dec22b61ff502be3f9ab19"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILES = 4096
MIN_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024 * 1024

ARTIFACTS = {
    "accelerate-1.12.0-py3-none-any.whl": (
        "3e2091cd341423207e2f084a6654b1efcd250dc326f2a37d6dde446e07cabb11"
    ),
    "cast_mxfp4_to_nvfp4.py": ("cd4a14baf6e977581e016b8ceec3102b8304523b11f289692ec1826eb01c4018"),
    "example_utils.py": ("981c036c2c6ec0dbac4f1fb8cce33493d2fcc958dc248054e8863d4ede4b8549"),
    "hf_ptq.py": "4606bcb6a9ace89a9c6c29a95bd9903be56e93a1c859e8ffbc16323d40f670d1",
    "nvidia_modelopt-0.45.0-py3-none-any.whl": (
        "04e1d787898e44e7281022f4772ee57bf59d1224cbcdd10d9487c2a110687a30"
    ),
    "transformers-5.9.0-py3-none-any.whl": (
        "1d19509bcff7028ebc6b277d71caa712e8353778463d38764237d14b42b52788"
    ),
}
PACKAGE_VERSIONS = {
    "accelerate": "1.12.0",
    "nvidia-modelopt": "0.45.0",
    "transformers": "5.9.0",
}

_TOP_LEVEL_KEYS = {
    "schema",
    "status",
    "source",
    "converter",
    "recipe",
    "output_directory",
    "metadata",
    "file_count",
    "total_bytes",
    "files",
    "note",
}
_NOTE = (
    "Observed only. Accept only after the exact offline tensor and provenance audit; "
    "loader and quality acceptance belong to the bound runtime profile."
)


class ConvertedModelManifestError(RuntimeError):
    """A content-free launch rejection."""


@dataclass(frozen=True, slots=True)
class ConvertedModelReceipt:
    manifest_sha256: str
    source_revision: str
    file_count: int
    total_bytes: int


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _reject_constant(_value: str) -> None:
    raise ConvertedModelManifestError("converted model manifest contains a non-finite number")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ConvertedModelManifestError("converted model manifest contains a duplicate key")
        value[key] = item
    return value


def _load_manifest(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not _is_sha256(expected_sha256):
        raise ConvertedModelManifestError("converted model manifest expectation is invalid")
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_MANIFEST_BYTES:
            raise ConvertedModelManifestError("converted model manifest is absent or unsafe")
        raw = path.read_bytes()
    except OSError as exc:
        raise ConvertedModelManifestError("converted model manifest is unavailable") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ConvertedModelManifestError("converted model manifest hash differs from the profile")
    if not raw or raw.startswith(b"\xef\xbb\xbf"):
        raise ConvertedModelManifestError("converted model manifest encoding is invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_constant=_reject_constant,
            object_pairs_hook=_strict_object,
        )
    except ConvertedModelManifestError:
        raise
    except Exception:
        raise ConvertedModelManifestError("converted model manifest is not strict UTF-8 JSON") from None
    if not isinstance(value, dict) or raw != _canonical_json(value):
        raise ConvertedModelManifestError("converted model manifest is not canonical")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ConvertedModelManifestError(f"converted model {label} shape is invalid")
    return value


def _exact_int(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConvertedModelManifestError(f"converted model {label} is outside the closed range")
    return value


def _validate_identity(value: dict[str, Any]) -> None:
    if set(value) != _TOP_LEVEL_KEYS or value.get("schema") != SCHEMA:
        raise ConvertedModelManifestError("converted model manifest schema is invalid")
    if value.get("status") != "accepted":
        raise ConvertedModelManifestError("converted model manifest is not accepted")
    source = _exact_keys(
        value.get("source"),
        {"repository", "revision", "manifest_semantic_sha256"},
        "source",
    )
    if (
        source["repository"] != SOURCE_REPOSITORY
        or source["revision"] != SOURCE_REVISION
        or source["manifest_semantic_sha256"] != SOURCE_MANIFEST_SEMANTIC_SHA256
    ):
        raise ConvertedModelManifestError("converted model source identity is invalid")
    converter = _exact_keys(
        value.get("converter"),
        {
            "image",
            "accepted_converter_manifest_sha256",
            "modelopt_commit",
            "artifacts",
            "package_versions",
        },
        "converter",
    )
    image = converter["image"]
    converter_manifest_sha256 = converter["accepted_converter_manifest_sha256"]
    if image == PREFERRED_CONVERTER_IMAGE:
        if converter_manifest_sha256 is not None:
            raise ConvertedModelManifestError("preferred converter has unexpected derived provenance")
    elif image == SEALED_ALTERNATIVE_IMAGE:
        if not _is_sha256(converter_manifest_sha256):
            raise ConvertedModelManifestError("derived converter manifest identity is invalid")
    else:
        raise ConvertedModelManifestError("converted model image identity is invalid")
    if (
        converter["modelopt_commit"] != MODELOPT_COMMIT
        or converter["artifacts"] != ARTIFACTS
        or converter["package_versions"] != PACKAGE_VERSIONS
    ):
        raise ConvertedModelManifestError("converted model toolchain identity is invalid")
    recipe = _exact_keys(
        value.get("recipe"),
        {
            "qformat",
            "cast_mxfp4_to_nvfp4",
            "kv_cache_qformat",
            "calibration_sha256",
            "calib_size",
            "calib_seq",
            "batch_size",
            "use_seq_device_map",
            "gpu_max_mem_percentage",
            "skip_generate",
            "low_memory_mode",
            "network",
        },
        "recipe",
    )
    expected_recipe = {
        "qformat": "nvfp4_mlp_only",
        "cast_mxfp4_to_nvfp4": True,
        "kv_cache_qformat": "none",
        "calibration_sha256": CALIBRATION_SHA256,
        "calib_size": 256,
        "calib_seq": 512,
        "batch_size": 1,
        "use_seq_device_map": True,
        "gpu_max_mem_percentage": 0.70,
        "skip_generate": True,
        "low_memory_mode": False,
        "network": "none",
    }
    if recipe != expected_recipe:
        raise ConvertedModelManifestError("converted model recipe identity is invalid")
    metadata = _exact_keys(
        value.get("metadata"),
        {
            "architecture",
            "model_type",
            "modelopt_version",
            "quant_algo",
            "kv_cache_quant_algo",
            "safetensors_shards",
            "weight_map_entries",
        },
        "metadata",
    )
    if (
        metadata["architecture"] != "GptOssForCausalLM"
        or metadata["model_type"] != "gpt_oss"
        or metadata["modelopt_version"] != "0.45.0"
        or metadata["quant_algo"] != "NVFP4"
        or metadata["kv_cache_quant_algo"] != "none"
    ):
        raise ConvertedModelManifestError("converted model metadata identity is invalid")
    _exact_int(metadata["safetensors_shards"], minimum=1, maximum=16, label="shard count")
    _exact_int(
        metadata["weight_map_entries"],
        minimum=1,
        maximum=2_000_000,
        label="weight map size",
    )
    if value.get("output_directory") != "candidate" or value.get("note") != _NOTE:
        raise ConvertedModelManifestError("converted model output identity is invalid")


def _manifest_file_rows(value: dict[str, Any]) -> dict[str, tuple[int, str]]:
    rows = value.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FILES:
        raise ConvertedModelManifestError("converted model file manifest is invalid")
    expected: dict[str, tuple[int, str]] = {}
    ordered_paths: list[str] = []
    for row in rows:
        item = _exact_keys(row, {"path", "size", "sha256"}, "file row")
        relative = item["path"]
        size = item["size"]
        digest = item["sha256"]
        if not isinstance(relative, str) or not relative or len(relative) > 512:
            raise ConvertedModelManifestError("converted model file path is invalid")
        parsed = PurePosixPath(relative)
        if (
            parsed.is_absolute()
            or "." in parsed.parts
            or ".." in parsed.parts
            or len(parsed.parts) > 16
            or relative in expected
        ):
            raise ConvertedModelManifestError("converted model file path is unsafe")
        exact_size = _exact_int(size, minimum=0, maximum=MAX_OUTPUT_BYTES, label="file size")
        if not _is_sha256(digest):
            raise ConvertedModelManifestError("converted model file hash is invalid")
        expected[relative] = (exact_size, str(digest))
        ordered_paths.append(relative)
    if ordered_paths != sorted(ordered_paths):
        raise ConvertedModelManifestError("converted model file rows are not ordered")
    file_count = _exact_int(value.get("file_count"), minimum=1, maximum=MAX_FILES, label="file count")
    total_bytes = _exact_int(
        value.get("total_bytes"),
        minimum=MIN_OUTPUT_BYTES,
        maximum=MAX_OUTPUT_BYTES,
        label="aggregate size",
    )
    if file_count != len(expected) or total_bytes != sum(size for size, _ in expected.values()):
        raise ConvertedModelManifestError("converted model manifest aggregates are invalid")
    return expected


def _snapshot_files(root: Path) -> dict[str, Path]:
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ConvertedModelManifestError("converted model snapshot is unavailable") from exc
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ConvertedModelManifestError("converted model snapshot root is unsafe")
    files: dict[str, Path] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise ConvertedModelManifestError("converted model snapshot is unreadable") from exc
        for entry in entries:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ConvertedModelManifestError("converted model entry is unreadable") from exc
            path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(path)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ConvertedModelManifestError("converted model snapshot has an unsafe entry")
            relative = path.relative_to(root).as_posix()
            if len(relative) > 512 or len(PurePosixPath(relative).parts) > 16:
                raise ConvertedModelManifestError("converted model snapshot path is unsafe")
            files[relative] = path
            if len(files) > MAX_FILES:
                raise ConvertedModelManifestError("converted model snapshot is oversized")
    return files


def _file_sha256(path: Path, *, expected_size: int) -> str:
    digest = hashlib.sha256()
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ConvertedModelManifestError("converted model live file identity differs")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ConvertedModelManifestError("converted model file changed during verification")
    except OSError as exc:
        raise ConvertedModelManifestError("converted model file became unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def verify_converted_model_snapshot(
    snapshot_directory: Path,
    accepted_manifest_path: Path,
    expected_manifest_sha256: str,
) -> ConvertedModelReceipt:
    """Hash the accepted manifest and every exact model file before runtime import."""

    value = _load_manifest(accepted_manifest_path, expected_manifest_sha256)
    _validate_identity(value)
    expected = _manifest_file_rows(value)
    actual = _snapshot_files(snapshot_directory)
    if set(actual) != set(expected):
        raise ConvertedModelManifestError("converted model live file set differs")
    total_bytes = 0
    for relative in sorted(expected):
        expected_size, expected_digest = expected[relative]
        path = actual[relative]
        if _file_sha256(path, expected_size=expected_size) != expected_digest:
            raise ConvertedModelManifestError("converted model live file identity differs")
        total_bytes += expected_size
    return ConvertedModelReceipt(
        manifest_sha256=expected_manifest_sha256,
        source_revision=SOURCE_REVISION,
        file_count=len(actual),
        total_bytes=total_bytes,
    )


def verify_converted_model_manifest(
    accepted_manifest_path: Path,
    expected_manifest_sha256: str,
) -> ConvertedModelReceipt:
    """Validate an accepted manifest and its closed file projection without mounting the model."""

    value = _load_manifest(accepted_manifest_path, expected_manifest_sha256)
    _validate_identity(value)
    expected = _manifest_file_rows(value)
    return ConvertedModelReceipt(
        manifest_sha256=expected_manifest_sha256,
        source_revision=SOURCE_REVISION,
        file_count=len(expected),
        total_bytes=sum(size for size, _digest in expected.values()),
    )
