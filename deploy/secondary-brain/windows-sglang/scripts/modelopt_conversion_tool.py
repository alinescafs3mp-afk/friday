#!/usr/bin/env python3
"""Validate and run the sealed, offline GPT-OSS MXFP4-to-NVFP4 conversion."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SOURCE_REPOSITORY = "openai/gpt-oss-20b"
SOURCE_REVISION = "6cee5e81ee83917806bbde320786a8fb61efebee"
SOURCE_SCHEMA = "friday.secondary-source-manifest.v1"
SOURCE_FILE_COUNT = 14
SOURCE_TOTAL_BYTES = 13_789_264_674
SOURCE_EXCLUDED_PREFIXES = ["metal/", "original/"]
SOURCE_MANIFEST_RAW_SHA256 = "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9"
SOURCE_MANIFEST_SEMANTIC_SHA256 = "e75b176ed1817e762cf9b7f2262f6e58491a0f9d48d1ea51e466a6e2c3b8a3ab"
SOURCE_FILES = {
    ".gitattributes": (1570, "34448b82c17d60fec9b65b1f093c115ddbaadc04beb1b0140b6bfed2e012a930"),
    "LICENSE": (11357, "58d1e17ffe5109a7ae296caafcadfdbe6a7d176f0bc4ab01e12a689b0499d8bd"),
    "README.md": (7095, "03c2fcf292549176757b85c911e7dcf527aef3e4241d64b6caec94af3ecf3ac2"),
    "USAGE_POLICY": (200, "d6387ef7985019c45db8d9801e6ac5fd9f98f02b9f1c56f8c5af80c3e8f385d0"),
    "chat_template.jinja": (
        16738,
        "a4c9919cbbd4acdd51ccffe22da049264b1b73e59055fa58811a99efbd7c8146",
    ),
    "config.json": (1806, "3a2a26ded679375b7928ddeca59764df7cea83220c1961035f6d6e232659e9ce"),
    "generation_config.json": (
        177,
        "f9970ada892d2d1f72e3ed0a6535ccebadd11897318794ca671d8c7014c957da",
    ),
    "model-00000-of-00002.safetensors": (
        4_792_272_488,
        "16d0f997dcfc4462089d536bffe51b4bcea2f872f5c430be09ef8ed392312427",
    ),
    "model-00001-of-00002.safetensors": (
        4_798_702_184,
        "4fbe328ab445455d6f58dc73852b85873bd626986310abd91cd4d2ce3245eaea",
    ),
    "model-00002-of-00002.safetensors": (
        4_170_342_232,
        "a18106b209e9ab35c3406db4f6f12a927364a058b21e9d1373d682e20674b303",
    ),
    "model.safetensors.index.json": (
        36355,
        "0e085b977c4c9942f85938828e8c989ed7d5cdabf852e4da6a67c116cd502cd1",
    ),
    "special_tokens_map.json": (
        98,
        "dd5e191d20c12d2fee1da5bae14ca1db0f5f4215300af691f23cdee97120a293",
    ),
    "tokenizer.json": (
        27_868_174,
        "0614fe83cadab421296e664e1f48f4261fa8fef6e03e63bb75c20f38e37d07d3",
    ),
    "tokenizer_config.json": (
        4200,
        "9279e942392b742d633c7adbb89ebe002c98399db8926a7af5125c726f404070",
    ),
}

MODELOPT_COMMIT = "ec87a82927d003986d44fb7f4fa8b3d10c31b095"
PREFERRED_CONVERSION_IMAGE = (
    "nvcr.io/nvidia/tensorrt-llm/release@"
    "sha256:7202108ab373557e0562f78ef3c0f65bdc70e18cc0b040c8d6805a5cde897a0d"
)
ALTERNATIVE_BASE_IMAGE = (
    "lmsysorg/sglang@sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738"
)

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

CALIBRATION_SCHEMA = "friday.secondary-brain.calibration.v1"
CALIBRATION_ROWS = 256
CALIBRATION_BYTES = 1_290_256
CALIBRATION_SHA256 = "fab1ccffa64af207e663f4acbc382bb4332edd9981dec22b61ff502be3f9ab19"
CALIBRATION_GENERATOR_SHA256 = "0a7ecd10a85966281fdb29642b4e1dc3c8e2f877c362b75f9d3ca82641d2ae16"

OUTPUT_SCHEMA = "friday.secondary-modelopt-conversion-output.v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_FILES = 4096
MIN_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 20 * 1024 * 1024 * 1024

ARTIFACT_DIRECTORY = Path("/artifacts")
SOURCE_ROOT = Path("/source")
SOURCE_SNAPSHOT = SOURCE_ROOT / "snapshot"
SOURCE_MANIFEST = SOURCE_ROOT / "source-manifest.json"
CALIBRATION_FILE = Path("/calibration/calibration.jsonl")
CALIBRATION_MANIFEST = Path("/calibration/calibration.observed.json")
OUTPUT_VOLUME = Path("/output")
OUTPUT_CANDIDATE = OUTPUT_VOLUME / "candidate"
OVERLAY_DIRECTORY = Path("/run/friday-python")


class ConversionError(RuntimeError):
    """A closed conversion invariant was not satisfied."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, *, limit: int = MAX_MANIFEST_BYTES) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ConversionError(f"{path.name} is absent, unsafe, or oversized")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError(f"{path.name} is unreadable") from exc
    if not isinstance(value, dict):
        raise ConversionError(f"{path.name} must contain one JSON object")
    return value


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _safe_root_files(root: Path, *, maximum: int = MAX_FILES) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ConversionError(f"{root} is absent or unsafe")
    files: list[Path] = []
    for entry in root.iterdir():
        if entry.is_symlink() or not entry.is_file():
            raise ConversionError(f"{root} must contain regular root files only")
        files.append(entry)
        if len(files) > maximum:
            raise ConversionError(f"{root} exceeds the file-count bound")
    return sorted(files, key=lambda path: path.name)


def validate_artifacts() -> dict[str, str]:
    files = _safe_root_files(ARTIFACT_DIRECTORY, maximum=len(ARTIFACTS))
    if [path.name for path in files] != sorted(ARTIFACTS):
        raise ConversionError("artifact directory differs from the exact closed file set")
    for path in files:
        if _sha256(path) != ARTIFACTS[path.name]:
            raise ConversionError(f"artifact hash mismatch: {path.name}")
    return dict(sorted(ARTIFACTS.items()))


def validate_source() -> tuple[dict[str, Any], str]:
    if not SOURCE_ROOT.is_dir() or SOURCE_ROOT.is_symlink():
        raise ConversionError("source volume root is absent or unsafe")
    if {entry.name for entry in SOURCE_ROOT.iterdir()} != {
        "snapshot",
        "source-manifest.json",
    }:
        raise ConversionError("source volume has an unexpected top-level entry")
    manifest = _load_json(SOURCE_MANIFEST)
    if _sha256(SOURCE_MANIFEST) != SOURCE_MANIFEST_RAW_SHA256:
        raise ConversionError("source manifest bytes differ from the sealed observation")
    expected_keys = {
        "schema",
        "status",
        "repository",
        "revision",
        "root_only",
        "excluded_prefixes",
        "file_count",
        "total_bytes",
        "files",
    }
    if set(manifest) != expected_keys:
        raise ConversionError("source manifest shape is not exact")
    expected_identity = {
        "schema": SOURCE_SCHEMA,
        "status": "verified",
        "repository": SOURCE_REPOSITORY,
        "revision": SOURCE_REVISION,
        "root_only": True,
        "excluded_prefixes": SOURCE_EXCLUDED_PREFIXES,
        "file_count": SOURCE_FILE_COUNT,
        "total_bytes": SOURCE_TOTAL_BYTES,
    }
    if any(manifest.get(key) != value for key, value in expected_identity.items()):
        raise ConversionError("source manifest identity is not exact and verified")
    rows = manifest.get("files")
    if not isinstance(rows, dict) or len(rows) != SOURCE_FILE_COUNT:
        raise ConversionError("source file manifest is invalid")
    expected_files: dict[str, tuple[int, str]] = {}
    for name, row in rows.items():
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 255
            or PurePosixPath(name).name != name
            or not isinstance(row, dict)
            or set(row) != {"bytes", "sha256"}
        ):
            raise ConversionError("source manifest contains an unsafe file row")
        size = row.get("bytes")
        digest = row.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or not _is_sha256(digest):
            raise ConversionError("source manifest contains an invalid file identity")
        expected_files[name] = (size, str(digest))
    if expected_files != SOURCE_FILES:
        raise ConversionError("source manifest differs from the code-owned clean snapshot")
    files = _safe_root_files(SOURCE_SNAPSHOT, maximum=SOURCE_FILE_COUNT)
    if [path.name for path in files] != sorted(expected_files):
        raise ConversionError("source snapshot differs from the verified file set")
    total_bytes = 0
    for path in files:
        expected_size, expected_digest = expected_files[path.name]
        size = path.stat().st_size
        if size != expected_size or _sha256(path) != expected_digest:
            raise ConversionError(f"source content mismatch: {path.name}")
        total_bytes += size
    if total_bytes != SOURCE_TOTAL_BYTES:
        raise ConversionError("source aggregate size differs from the pinned snapshot")
    semantic_sha256 = _canonical_sha256(manifest)
    if semantic_sha256 != SOURCE_MANIFEST_SEMANTIC_SHA256:
        raise ConversionError("source manifest semantic identity differs")
    return manifest, semantic_sha256


def validate_calibration() -> dict[str, Any]:
    manifest = _load_json(CALIBRATION_MANIFEST, limit=64 * 1024)
    expected = {
        "schema": CALIBRATION_SCHEMA,
        "status": "observed_unaccepted",
        "rows": CALIBRATION_ROWS,
        "bytes": CALIBRATION_BYTES,
        "sha256": CALIBRATION_SHA256,
        "generator_sha256": CALIBRATION_GENERATOR_SHA256,
        "synthetic_only": True,
        "operator_data_present": False,
    }
    if manifest != expected:
        raise ConversionError("calibration manifest differs from the code-owned corpus")
    if (
        CALIBRATION_FILE.is_symlink()
        or not CALIBRATION_FILE.is_file()
        or CALIBRATION_FILE.stat().st_size != CALIBRATION_BYTES
        or _sha256(CALIBRATION_FILE) != CALIBRATION_SHA256
    ):
        raise ConversionError("calibration corpus content differs from the code-owned corpus")
    rows = 0
    try:
        with CALIBRATION_FILE.open("r", encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if (
                    not isinstance(value, dict)
                    or set(value) != {"text"}
                    or not isinstance(value["text"], str)
                    or len(value["text"]) <= 2_000
                ):
                    raise ConversionError("calibration row is not an exact text record")
                rows += 1
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("calibration corpus is unreadable") from exc
    if rows != CALIBRATION_ROWS:
        raise ConversionError("calibration row count differs from the fixed recipe")
    return manifest


def _require_empty_output() -> None:
    if not OUTPUT_VOLUME.is_dir() or OUTPUT_VOLUME.is_symlink():
        raise ConversionError("output volume is absent or unsafe")
    if any(OUTPUT_VOLUME.iterdir()):
        raise ConversionError("conversion refuses a non-empty output volume")


def _conversion_environment() -> dict[str, str]:
    environment = {
        "HF_DATASETS_OFFLINE": "1",
        "HF_HOME": "/tmp/huggingface",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "TRANSFORMERS_OFFLINE": "1",
    }
    # Deliberately do not inherit credentials, proxy configuration, or arbitrary
    # Python paths into the network-disabled conversion subprocess.
    for name in ("CUDA_HOME", "LD_LIBRARY_PATH", "PATH"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def conversion_command() -> tuple[str, ...]:
    """Return the only permitted ModelOpt recipe, excluding the Python executable."""

    return (
        "/artifacts/hf_ptq.py",
        "--pyt_ckpt_path",
        "/source/snapshot",
        "--export_path",
        "/output/candidate",
        "--qformat",
        "nvfp4_mlp_only",
        "--cast_mxfp4_to_nvfp4",
        "--kv_cache_qformat",
        "none",
        "--dataset",
        "/calibration/calibration.jsonl",
        "--calib_size",
        "256",
        "--calib_seq",
        "512",
        "--batch_size",
        "1",
        "--use_seq_device_map",
        "--gpu_max_mem_percentage",
        "0.70",
        "--skip_generate",
    )


def _installed_versions(*, python_path: Path | None) -> dict[str, str]:
    environment = _conversion_environment()
    if python_path is not None:
        environment["PYTHONPATH"] = str(python_path)
    program = (
        "import importlib.metadata,json;"
        "names=('accelerate','nvidia-modelopt','transformers');"
        "print(json.dumps({n:importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    if result.returncode != 0:
        raise ConversionError("could not verify converter package versions")
    try:
        versions = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError("converter package version projection is invalid") from exc
    if versions != PACKAGE_VERSIONS:
        raise ConversionError("converter package versions differ from the pinned closure")
    return versions


def _pip_check(*, python_path: Path | None) -> None:
    environment = _conversion_environment()
    if python_path is not None:
        environment["PYTHONPATH"] = str(python_path)
    result = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    if result.returncode != 0 or result.stdout.strip() != "No broken requirements found.":
        raise ConversionError("converter dependency closure failed pip check")


def _install_overlay() -> None:
    if not OVERLAY_DIRECTORY.is_dir() or any(OVERLAY_DIRECTORY.iterdir()):
        raise ConversionError("conversion package overlay must start empty")
    wheels = [
        ARTIFACT_DIRECTORY / "nvidia_modelopt-0.45.0-py3-none-any.whl",
        ARTIFACT_DIRECTORY / "transformers-5.9.0-py3-none-any.whl",
        ARTIFACT_DIRECTORY / "accelerate-1.12.0-py3-none-any.whl",
    ]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--no-deps",
            "--no-index",
            "--target",
            str(OVERLAY_DIRECTORY),
            *(str(path) for path in wheels),
        ],
        check=False,
        env=_conversion_environment(),
        timeout=600,
    )
    if result.returncode != 0:
        raise ConversionError("offline converter package overlay installation failed")
    _installed_versions(python_path=OVERLAY_DIRECTORY)
    _pip_check(python_path=OVERLAY_DIRECTORY)


def validate_inputs(*, packages_mode: str) -> dict[str, Any]:
    artifacts = validate_artifacts()
    _, source_manifest_sha256 = validate_source()
    validate_calibration()
    _require_empty_output()
    if packages_mode == "preinstalled":
        versions = _installed_versions(python_path=None)
        _pip_check(python_path=None)
    else:
        versions = PACKAGE_VERSIONS
    return {
        "schema": "friday.secondary-modelopt-conversion-preflight.v1",
        "status": "passed",
        "source_manifest_semantic_sha256": source_manifest_sha256,
        "artifacts": artifacts,
        "package_versions": versions,
        "packages_mode": packages_mode,
        "output_empty": True,
        "network_required": False,
    }


def run_conversion(*, packages_mode: str) -> None:
    validate_inputs(packages_mode=packages_mode)
    if packages_mode == "overlay":
        _install_overlay()
        python_path: Path | None = OVERLAY_DIRECTORY
    else:
        python_path = None
    environment = _conversion_environment()
    if python_path is not None:
        environment["PYTHONPATH"] = f"{python_path}:{ARTIFACT_DIRECTORY}"
    else:
        environment["PYTHONPATH"] = str(ARTIFACT_DIRECTORY)
    result = subprocess.run(
        [sys.executable, *conversion_command()],
        check=False,
        cwd=ARTIFACT_DIRECTORY,
        env=environment,
    )
    if result.returncode != 0:
        raise ConversionError("ModelOpt conversion failed; partial output was retained")


def _safe_output_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ConversionError("conversion candidate is absent or unsafe")
    files: list[Path] = []
    for entry in root.rglob("*"):
        if entry.is_symlink():
            raise ConversionError("conversion candidate contains a symbolic link")
        if entry.is_file():
            files.append(entry)
        elif not entry.is_dir():
            raise ConversionError("conversion candidate contains a non-regular entry")
        if len(files) > MAX_FILES:
            raise ConversionError("conversion candidate exceeds the file-count bound")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _validate_output_metadata(files: list[Path]) -> dict[str, Any]:
    relative = {path.relative_to(OUTPUT_CANDIDATE).as_posix(): path for path in files}
    required = {
        "config.json",
        "hf_quant_config.json",
        "model.safetensors.index.json",
        "tokenizer.json",
        "tokenizer_config.json",
    }
    if not required <= set(relative):
        raise ConversionError("conversion candidate lacks required HF/ModelOpt metadata")
    if any(name.endswith((".bin", ".pt", ".pth")) for name in relative):
        raise ConversionError("conversion candidate contains an unsafe model serialization")
    config = _load_json(relative["config.json"])
    if config.get("model_type") != "gpt_oss" or config.get("architectures") != ["GptOssForCausalLM"]:
        raise ConversionError("converted model architecture identity is wrong")
    quantization = config.get("quantization_config")
    if not isinstance(quantization, dict):
        raise ConversionError("converted config lacks ModelOpt quantization metadata")
    producer = quantization.get("producer")
    if (
        quantization.get("quant_method") != "modelopt"
        or quantization.get("quant_algo") != "NVFP4"
        or not isinstance(producer, dict)
        or producer.get("name") != "modelopt"
        or producer.get("version") != "0.45.0"
        or quantization.get("kv_cache_scheme") not in (None, {})
    ):
        raise ConversionError("converted config differs from the NVFP4/KV-none recipe")
    hf_quantization = _load_json(relative["hf_quant_config.json"])
    hf_producer = hf_quantization.get("producer")
    hf_recipe = hf_quantization.get("quantization")
    if (
        not isinstance(hf_producer, dict)
        or hf_producer.get("name") != "modelopt"
        or hf_producer.get("version") != "0.45.0"
        or not isinstance(hf_recipe, dict)
        or hf_recipe.get("quant_algo") != "NVFP4"
        or hf_recipe.get("kv_cache_quant_algo") not in (None, "NONE")
    ):
        raise ConversionError("HF quantization metadata differs from NVFP4/KV-none")
    index = _load_json(relative["model.safetensors.index.json"], limit=16 * 1024 * 1024)
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        raise ConversionError("safetensors index has no bounded weight map")
    shard_rows = list(weight_map.values())
    if any(
        not isinstance(value, str) or PurePosixPath(value).name != value or not value.endswith(".safetensors")
        for value in shard_rows
    ):
        raise ConversionError("safetensors index contains an unsafe shard mapping")
    shards = set(shard_rows)
    if len(shards) < 1 or len(shards) > 16:
        raise ConversionError("safetensors index contains an unsafe shard mapping")
    actual_shards = {name for name in relative if name.endswith(".safetensors")}
    if shards != actual_shards:
        raise ConversionError("safetensors shards differ from the exact index projection")
    return {
        "architecture": "GptOssForCausalLM",
        "model_type": "gpt_oss",
        "modelopt_version": "0.45.0",
        "quant_algo": "NVFP4",
        "kv_cache_quant_algo": "none",
        "safetensors_shards": len(shards),
        "weight_map_entries": len(weight_map),
    }


def observed_output_manifest(
    *, conversion_image: str, accepted_converter_manifest_sha256: str | None = None
) -> dict[str, Any]:
    if accepted_converter_manifest_sha256 is not None and not _is_sha256(accepted_converter_manifest_sha256):
        raise ConversionError("accepted converter manifest hash is invalid")
    if {entry.name for entry in OUTPUT_VOLUME.iterdir()} != {"candidate"}:
        raise ConversionError("output volume contains an entry outside the candidate")
    artifacts = validate_artifacts()
    _, source_manifest_sha256 = validate_source()
    validate_calibration()
    files = _safe_output_files(OUTPUT_CANDIDATE)
    if not files:
        raise ConversionError("conversion candidate is empty")
    total_bytes = sum(path.stat().st_size for path in files)
    if not MIN_OUTPUT_BYTES <= total_bytes <= MAX_OUTPUT_BYTES:
        raise ConversionError("conversion candidate size is outside the GPT-OSS bound")
    metadata = _validate_output_metadata(files)
    rows = [
        {
            "path": path.relative_to(OUTPUT_CANDIDATE).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in files
    ]
    return {
        "schema": OUTPUT_SCHEMA,
        "status": "observed_unaccepted",
        "source": {
            "repository": SOURCE_REPOSITORY,
            "revision": SOURCE_REVISION,
            "manifest_semantic_sha256": source_manifest_sha256,
        },
        "converter": {
            "image": conversion_image,
            "accepted_converter_manifest_sha256": accepted_converter_manifest_sha256,
            "modelopt_commit": MODELOPT_COMMIT,
            "artifacts": artifacts,
            "package_versions": PACKAGE_VERSIONS,
        },
        "recipe": {
            "qformat": "nvfp4_mlp_only",
            "cast_mxfp4_to_nvfp4": True,
            "kv_cache_qformat": "none",
            "calibration_sha256": CALIBRATION_SHA256,
            "calib_size": CALIBRATION_ROWS,
            "calib_seq": 512,
            "batch_size": 1,
            "use_seq_device_map": True,
            "gpu_max_mem_percentage": 0.70,
            "skip_generate": True,
            "low_memory_mode": False,
            "network": "none",
        },
        "output_directory": "candidate",
        "metadata": metadata,
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "files": rows,
        "note": (
            "Observed only. Accept only after the exact offline tensor and provenance audit; "
            "loader and quality acceptance belong to the bound runtime profile."
        ),
    }


def verify_accepted_output(
    manifest_path: Path,
    *,
    conversion_image: str,
    accepted_converter_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    if manifest.get("status") != "accepted":
        raise ConversionError("output manifest is not explicitly accepted")
    observed = observed_output_manifest(
        conversion_image=conversion_image,
        accepted_converter_manifest_sha256=accepted_converter_manifest_sha256,
    )
    accepted_as_observed = dict(manifest)
    accepted_as_observed["status"] = "observed_unaccepted"
    if accepted_as_observed != observed:
        raise ConversionError("accepted output manifest differs from live candidate content")
    return {
        "schema": "friday.secondary-modelopt-conversion-verification.v1",
        "status": "passed",
        "source_revision": SOURCE_REVISION,
        "conversion_image": conversion_image,
        "file_count": observed["file_count"],
        "total_bytes": observed["total_bytes"],
        "accepted_manifest_file_sha256": _sha256(manifest_path),
        "manifest_semantic_sha256": _canonical_sha256(manifest),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-inputs", "convert"):
        command = subparsers.add_parser(name)
        command.add_argument("--packages-mode", choices=("overlay", "preinstalled"), required=True)
    observe = subparsers.add_parser("observe-output")
    observe.add_argument("--conversion-image", required=True)
    observe.add_argument("--accepted-converter-manifest-sha256")
    verify = subparsers.add_parser("verify-output")
    verify.add_argument("--conversion-image", required=True)
    verify.add_argument("--accepted-converter-manifest-sha256")
    verify.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-inputs":
            result = validate_inputs(packages_mode=args.packages_mode)
        elif args.command == "convert":
            run_conversion(packages_mode=args.packages_mode)
            result = {
                "schema": "friday.secondary-modelopt-conversion-execution.v1",
                "status": "completed_unaccepted",
            }
        elif args.command == "observe-output":
            result = observed_output_manifest(
                conversion_image=args.conversion_image,
                accepted_converter_manifest_sha256=(args.accepted_converter_manifest_sha256),
            )
        else:
            result = verify_accepted_output(
                args.manifest,
                conversion_image=args.conversion_image,
                accepted_converter_manifest_sha256=(args.accepted_converter_manifest_sha256),
            )
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0
    except ConversionError as exc:
        print(f"sealed conversion failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
