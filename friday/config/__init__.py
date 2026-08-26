"""Configuration and runtime profiles for Friday.

Configuration is deliberately environment-driven: the source tree never needs
secrets, and the whole installation can be moved or backed up as one directory.
"""

from __future__ import annotations

import ipaddress
import math
import os
import platform
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar, overload
from urllib.parse import urlparse

from friday.private_fs import ensure_private_directory

# Прежнее имя проекта. Переименование не должно ломать чужие запуски: у владельца
# `FRIDAY_*` стоят в systemd-юнитах, в `.env.local` и в скриптах, и «поменяли имя —
# перенастраивай всё заново» это не работа, а перекладывание её на человека.
# Читается ВТОРЫМ: если заданы обе переменные, побеждает новая.
_LEGACY_PREFIX = "JERICHO_"
_PREFIX = "FRIDAY_"
MEMORY_VAULT_MODES = ("disabled", "full_owner")


_Default = TypeVar("_Default")


@overload
def env(name: str) -> str | None: ...


@overload
def env(name: str, default: _Default) -> str | _Default: ...


def env(name: str, default: Any = None) -> Any:
    """Значение настройки по новому имени, с откатом на прежнее.

    Единственная точка чтения окружения в конфиге — чтобы совместимость нельзя
    было забыть в одном месте из шестидесяти. Тип возврата следует за умолчанием:
    вызов с `Path` умолчанием отдаёт `str | Path`, и вызывающему не приходится
    доказывать проверяющему типов то, что и так видно из вызова.
    """
    value = os.environ.get(name)
    if value is not None:
        return value
    if name.startswith(_PREFIX):
        legacy = os.environ.get(_LEGACY_PREFIX + name[len(_PREFIX) :])
        if legacy is not None:
            return legacy
    return default


def _bool_env(name: str, default: bool) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"", "0", "false", "no", "off"}


def _choice_env(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """A setting whose values are a vocabulary, not a spectrum.

    Raises on an unknown value rather than falling back to the default: a typo in
    a policy name would otherwise silently restore the very behaviour the operator
    was trying to change, and nothing about the running system would say so.
    Same shape as the `FRIDAY_PROFILE` check below.
    """
    value = (env(name) or default).strip().casefold()
    if value not in allowed:
        raise ValueError(f"Unknown {name}={value!r}. Valid values: {', '.join(sorted(allowed))}")
    return value


def _fail_closed_choice_env(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """Return the safe default for an unknown rollout value.

    Most policy typos should stop startup.  A runtime migration switch is the
    exception: an unknown value must preserve the proven legacy runtime rather
    than turning a deployment typo into an outage or selecting new code.
    """

    value = (env(name) or default).strip().casefold()
    return value if value in allowed else default


def _int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(env(name, str(default)) or default)
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def _float_env(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(env(name, str(default)) or default)
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def _fail_closed_rollout_int_env(name: str, default: int, *, invalid: int) -> int:
    """Parse a policy bound without turning malformed input into an admitted default."""

    raw = env(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return invalid


def _fail_closed_rollout_float_env(name: str, default: float, *, invalid: float) -> float:
    """Preserve finite rollout bounds and map malformed/non-finite input to a closed sentinel."""

    raw = env(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return invalid
    return value if math.isfinite(value) else invalid


def _list_env(name: str, default: list[str] | None = None) -> list[str]:
    raw = env(name)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def _int_list_env(name: str) -> list[int]:
    result: list[int] = []
    for item in _list_env(name):
        try:
            result.append(int(item))
        except ValueError:
            continue
    return result


def _absolute_lexical_path(value: str | Path) -> Path:
    """Normalize dot segments without resolving any existing symlink component."""

    return Path(os.path.abspath(Path(value).expanduser()))


def local_env_file_path(path: str | Path | None = None) -> Path:
    """The file this process is configured from: explicit, then FRIDAY_ENV_FILE, then ./.env.local.

    Public because more than one caller has to know WHICH file that is. The secret
    scanner in particular used to skip anything merely NAMED `.env` or `.env.local`
    anywhere in the tree, so a copy of a live token in some unrelated project's `.env`
    was invisible while the same token in `env.txt` beside it was reported.
    """
    if path is not None:
        return Path(path).expanduser()
    # Проверка и чтение — через ОДНУ функцию. Разошлись они ровно один раз, и
    # этого хватило: `env()` видел прежнее имя `JERICHO_ENV_FILE`, а строкой ниже
    # значение бралось напрямую из окружения по новому — и `friday model-check`
    # падал с KeyError у любого, кто ещё не переименовал переменные.
    configured = env("FRIDAY_ENV_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.cwd() / ".env.local"


def load_local_env_file(path: str | Path | None = None) -> list[str]:
    """Load ``KEY=value`` pairs without overwriting the process environment."""
    target = local_env_file_path(path)
    if not target.is_file():
        return []

    applied: list[str] = []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return applied
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value
        applied.append(key)
    return applied


def default_home() -> Path:
    """Где живут данные. Прежний каталог продолжает работать, пока он есть.

    Проект переименован (ex codename Friday), но каталог с данными — это база на
    гигабайты, файлы-первоисточники и резервные копии. Переименовать его молча
    нельзя, а требовать ручного переноса — значит сделать обновление опасным на
    ровном месте: пропустивший шаг получит ПУСТУЮ систему, которая выглядит
    исправной. Поэтому новый каталог используется, если он есть или если старого
    нет, а старый — пока он существует.
    """
    explicit = env("FRIDAY_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if platform.system().casefold() == "windows":
        return _existing_home(Path(r"D:\friday"), Path(r"D:\jericho"))
    if Path("/mnt/d").exists():
        return _existing_home(Path("/mnt/d/friday"), Path("/mnt/d/jericho"))
    return _existing_home(Path.home() / ".friday", Path.home() / ".jericho")


def _existing_database(state_dir: Path) -> Path:
    """Файл базы: новое имя, но НЕ в ущерб существующим данным.

    Та же осторожность, что и с каталогом, и по той же причине — только здесь она
    уже была нужна: массовое переименование задело имя файла, живой экземпляр
    создал рядом пустую `friday.sqlite3` (618 КБ) и отчитался «эталонов 0» при
    целой `jericho.sqlite3` на 323 МБ рядом. Данные не пострадали, но система
    выглядела исправной и пустой одновременно — ровно тот исход, ради которого
    здесь стоит проверка, а не безусловное новое имя.
    """
    preferred = state_dir / "friday.sqlite3"
    legacy = state_dir / "jericho.sqlite3"
    preferred_exists = preferred.exists()
    legacy_exists = legacy.exists()
    if preferred_exists and legacy_exists:
        try:
            if preferred.samefile(legacy):
                return preferred
        except OSError:
            pass
        preferred_size = preferred.stat().st_size
        legacy_size = legacy.stat().st_size
        if preferred_size == 0 and legacy_size > 0:
            return legacy
        if legacy_size == 0 and preferred_size > 0:
            return preferred
        if preferred_size == 0 and legacy_size == 0:
            return preferred
        raise RuntimeError(
            "Both friday.sqlite3 and jericho.sqlite3 contain data; "
            "set FRIDAY_DATABASE_PATH explicitly before starting Friday."
        )
    if preferred_exists:
        return preferred
    if legacy_exists:
        return legacy
    return preferred


def _existing_home(preferred: Path, legacy: Path) -> Path:
    """Новый каталог, но не в ущерб уже существующим данным."""
    if preferred.exists():
        return preferred
    if legacy.exists():
        return legacy
    return preferred


@dataclass(frozen=True)
class VllmExtraArgs:
    language_model_only: bool = False
    skip_mm_profiling: bool = False
    mm_processor_cache_gb: float | None = None
    max_num_batched_tokens: int | None = None
    reasoning_parser: str | None = None
    tool_call_parser: str | None = None
    enable_auto_tool_choice: bool = False
    limit_mm_per_prompt: str | None = None
    trust_remote_code: bool = False
    speculative_config: str | None = None
    async_scheduling: bool = False


@dataclass(frozen=True)
class SglangExtraArgs:
    """Code-owned launch facts for an externally managed SGLang endpoint."""

    mem_fraction_static: float
    max_total_tokens: int
    chunked_prefill_size: int
    mamba_ssm_dtype: str
    max_mamba_cache_size: int
    radix_cache_enabled: bool
    cuda_graph_backend_decode: str
    cuda_graph_max_bs_decode: int
    cuda_graph_bs_decode: tuple[int, ...]
    cuda_graph_backend_prefill: str
    attention_backend: str
    reasoning_parser: str
    tool_call_parser: str
    mm_feature_transport: str
    limit_mm_data_per_request: str
    metrics_enabled: bool
    weight_version: str = "default"
    speculative_algorithm: str | None = None


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    title: str
    description: str
    model_dir_name: str
    eager_mode: bool
    max_steps: int
    temperature: float
    max_model_len: int
    gpu_memory_utilization: float
    kv_cache_dtype: str
    max_num_seqs: int
    # Document hierarchy fan-out is an application workload limit, not a copy
    # of vLLM's scheduler capacity.  Keep it explicit so increasing endpoint
    # throughput cannot silently multiply long document-map generations.
    document_map_max_concurrency: int
    cpu_offload_gb: float
    kv_offloading_gb: int
    # These generic identity fields bind non-vLLM endpoints without pretending
    # that a served alias proves either model or runtime provenance.
    inference_backend: str = "vllm"
    model_repository: str = ""
    model_revision: str = ""
    model_quantization: str = ""
    runtime_image: str = ""
    runtime_source_revision: str = ""
    runtime_reported_version: str = ""
    engine_image_id: str = ""
    engine_base_image_digest: str = ""
    engine_base_image_id: str = ""
    model_snapshot_manifest_sha256: str = ""
    launch_manifest_sha256: str = ""
    proxy_image_id: str = ""
    proxy_policy_sha256: str = ""
    vllm_extra_args: VllmExtraArgs = field(default_factory=VllmExtraArgs)
    sglang_extra_args: SglangExtraArgs | None = None
    tokenizer_mode: str = "slow"
    # Explicit vLLM quantization selector. Mixed ModelOpt checkpoints must not be
    # allowed to fall back to the older FP8-only ``modelopt`` loader.
    quantization: str | None = None
    vllm_image: str = "vllm/vllm-openai:latest"
    vision_capable: bool = False
    suppress_model_thinking: bool = False
    certification: str = "unsupported"
    interactive_certified: bool = False
    default_recommended: bool = False
    research_only: bool = True
    readiness_deadline_sec: float = 300.0
    certification_reason: str = ""
    menu_visible: bool = False
    requires_experimental_opt_in: bool = True


# Кто попадает в Inbox до того, как стать каноническим знанием.
#
# `unless_explicit` — прямое продвижение остаётся только у явного намерения
#                    (`/note`, «запомни», `force_knowledge`); всё остальное ждёт
#                    решения человека. Это безопасное поведение по умолчанию. Загрузка
#                    файла — явное ДЕЙСТВИЕ, но не высказывание о содержимом,
#                    поэтому файлы сюда тоже попадают.
# `assessed`        — явный режим совместимости: решает классификатор, и его `promote`
#                    сразу становится каноническим знанием.
# `always`         — не продвигается ничто, включая явные сохранения.
#
# `force_review` у отдельного вызова — пол, а не альтернатива: политика может
# только ДОБАВИТЬ ревью. Поэтому массовый импорт, `/api/ingest/url` и импортёр
# остаются в Inbox при любой политике.
REVIEW_POLICIES = ("assessed", "unless_explicit", "always")

# These values intentionally match the known-good reference runtime profile.
PROFILES: dict[str, RuntimeProfile] = {
    "qwen36-vl": RuntimeProfile(
        name="qwen36-vl",
        title="Qwen3.6 35B-A3B NVFP4",
        description=(
            "Primary local vision-language model (35B total / about 3B active) "
            "using NVFP4 weights and fp8 KV cache."
        ),
        model_dir_name="qwen3.6-35b-a3b-nvfp4",
        eager_mode=False,
        max_steps=24,
        temperature=0.25,
        max_model_len=32768,
        gpu_memory_utilization=0.90,
        kv_cache_dtype="fp8",
        max_num_seqs=16,
        document_map_max_concurrency=3,
        cpu_offload_gb=0,
        kv_offloading_gb=0,
        tokenizer_mode="auto",
        vllm_image="jericho/vllm-openai:v0.25.1-asyncio-e4f88a8",
        vision_capable=True,
        suppress_model_thinking=True,
        vllm_extra_args=VllmExtraArgs(
            skip_mm_profiling=True,
            mm_processor_cache_gb=4.0,
            max_num_batched_tokens=4096,
            limit_mm_per_prompt='{"image":4,"video":1}',
        ),
        certification="certified",
        interactive_certified=True,
        default_recommended=False,
        research_only=False,
        readiness_deadline_sec=900.0,
        certification_reason="Primary Friday runtime profile.",
        menu_visible=True,
        requires_experimental_opt_in=False,
    )
}


# Dense, aligned multimodal Qwen3.6.  The pinned vLLM nightly is the first
# verified local image whose ModelOptMixedPrecisionConfig parses this
# checkpoint's FP8 + W4A16_NVFP4 map.  MM profiling deliberately remains on:
# with co-resident embedding/reranker services an honest startup failure is
# safer than a late image-request OOM hidden by ``--skip-mm-profiling``.
PROFILES["qwen36-27b-nvfp4-nvidia"] = RuntimeProfile(
    name="qwen36-27b-nvfp4-nvidia",
    title="Qwen3.6 27B NVIDIA NVFP4",
    description=("Aligned multimodal Qwen3.6 27B from NVIDIA's mixed FP8/W4A16 NVFP4 checkpoint."),
    model_dir_name="qwen3.6-27b-nvfp4-nvidia",
    eager_mode=False,
    max_steps=24,
    temperature=0.25,
    max_model_len=40960,
    # Exact values attested from the sole healthy remote container publishing
    # the Friday dispatcher port.  Document map fan-out remains independently
    # capped below; scheduler capacity is not permission to launch six costly
    # hierarchy leaves from one user turn.
    gpu_memory_utilization=0.80,
    kv_cache_dtype="fp8",
    max_num_seqs=6,
    document_map_max_concurrency=1,
    cpu_offload_gb=0,
    kv_offloading_gb=0,
    tokenizer_mode="auto",
    quantization="modelopt_mixed",
    vllm_image=("vllm/vllm-openai@sha256:2238154357f576523db1df2866cbf591734d70db8f6d50b9a7897f3c60e18940"),
    vision_capable=True,
    suppress_model_thinking=True,
    vllm_extra_args=VllmExtraArgs(
        language_model_only=False,
        mm_processor_cache_gb=4.0,
        max_num_batched_tokens=8192,
        reasoning_parser="qwen3",
        tool_call_parser="qwen3_coder",
        enable_auto_tool_choice=True,
        limit_mm_per_prompt='{"image":4,"video":0}',
        speculative_config='{"method":"mtp","num_speculative_tokens":1}',
    ),
    certification="pending_multimodal_smoke",
    interactive_certified=False,
    default_recommended=True,
    research_only=False,
    readiness_deadline_sec=900.0,
    certification_reason=(
        "Live dispatcher launch attested at 40K with FP8 KV and MTP1; "
        "multimodal boot and image smoke remain required for certification."
    ),
    menu_visible=True,
    requires_experimental_opt_in=False,
)


# Exact graph-only Qwen3.8 dispatcher measured on 2026-08-18.  The model and
# SGLang identities are code-owned provenance; ``dispatcher`` remains only the
# OpenAI-compatible served alias.  V12 authority is registered independently in
# ``friday.model_profiles`` and still requires a fresh live attestation.
PROFILES["qwen38-27b-nvfp4-sglang"] = RuntimeProfile(
    name="qwen38-27b-nvfp4-sglang",
    title="Qwen3.8 27B A2Genesis NVFP4 (SGLang)",
    description=("Aligned multimodal Qwen3.8 27B served by the pinned graph-only SGLang runtime."),
    model_dir_name="qwen3.8-27b-nvfp4-a2genesis-bfd9b312",
    eager_mode=False,
    max_steps=24,
    temperature=0.25,
    max_model_len=40_960,
    gpu_memory_utilization=0.90,
    kv_cache_dtype="fp8_e4m3",
    max_num_seqs=6,
    document_map_max_concurrency=1,
    cpu_offload_gb=0,
    kv_offloading_gb=0,
    inference_backend="sglang",
    model_repository="a2genesis/Qwen3.8-27B-NVFP4",
    model_revision="bfd9b31207712e0850eec9da32261e8c5ee16af7",
    model_quantization="W4A16_NVFP4",
    runtime_image=("lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124"),
    runtime_source_revision="c4271c3fe1262fc2adbd162c33b25de5255251c5",
    runtime_reported_version="0.0.0.dev0+qwen38.27b.g561c8f3",
    engine_image_id="sha256:4a38144134d84d6f78c1844314f209c48ef69c4bd8bf7da1e5c400f9abda6f26",
    engine_base_image_digest=(
        "lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124"
    ),
    engine_base_image_id="sha256:317b75ce527f3b6ee482e9437c753e98f4df6e6b17a335f8681af5d86a8a9de8",
    model_snapshot_manifest_sha256="da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333",
    launch_manifest_sha256="640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b",
    proxy_image_id="sha256:37ae13a39a5d8a0780b0b0f226065753c0d929c31956be27f7f375f79cdef750",
    proxy_policy_sha256="d51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe",
    tokenizer_mode="auto",
    quantization=None,
    vllm_image="",
    vision_capable=True,
    suppress_model_thinking=True,
    sglang_extra_args=SglangExtraArgs(
        mem_fraction_static=0.90,
        max_total_tokens=40_960,
        chunked_prefill_size=2_048,
        mamba_ssm_dtype="bfloat16",
        max_mamba_cache_size=6,
        radix_cache_enabled=False,
        cuda_graph_backend_decode="full",
        cuda_graph_max_bs_decode=6,
        cuda_graph_bs_decode=(1, 2, 3, 4, 5, 6),
        cuda_graph_backend_prefill="disabled",
        attention_backend="flashinfer",
        reasoning_parser="qwen3",
        tool_call_parser="qwen3_coder",
        mm_feature_transport="cpu",
        limit_mm_data_per_request='{"image":4,"video":0,"audio":0}',
        metrics_enabled=True,
        weight_version="default",
        speculative_algorithm=None,
    ),
    certification="certified",
    interactive_certified=True,
    default_recommended=False,
    research_only=False,
    readiness_deadline_sec=900.0,
    certification_reason=(
        "Live graph-only dispatcher attested at 40K/6 with FP8 KV, text/image "
        "smokes and soak; V12 authority still requires its own startup probe."
    ),
    menu_visible=True,
    requires_experimental_opt_in=False,
)


def profile_public_dict(profile: RuntimeProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "title": profile.title,
        "description": profile.description,
        "model_dir_name": profile.model_dir_name,
        "eager_mode": profile.eager_mode,
        "max_steps": profile.max_steps,
        "temperature": profile.temperature,
        "max_model_len": profile.max_model_len,
        "gpu_memory_utilization": profile.gpu_memory_utilization,
        "kv_cache_dtype": profile.kv_cache_dtype,
        "max_num_seqs": profile.max_num_seqs,
        "document_map_max_concurrency": profile.document_map_max_concurrency,
        "inference_backend": profile.inference_backend,
        "model_repository": profile.model_repository,
        "model_revision": profile.model_revision,
        "model_quantization": profile.model_quantization,
        "runtime_image": profile.runtime_image,
        "runtime_source_revision": profile.runtime_source_revision,
        "runtime_reported_version": profile.runtime_reported_version,
        "tokenizer_mode": profile.tokenizer_mode,
        "quantization": profile.quantization,
        "vllm_image": profile.vllm_image,
        "vision_capable": profile.vision_capable,
        "certification": profile.certification,
        "interactive_certified": profile.interactive_certified,
        "default_recommended": profile.default_recommended,
    }


@dataclass(frozen=True)
class FridaySettings:
    home: Path
    profile: RuntimeProfile
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    # `jericho up` copy-truncates a child log once it passes this size and keeps
    # `log_backups` numbered generations. Unbounded child logs are a real way to
    # fill the disk: the bridge alone polls the backend every 15s, and every poll
    # costs an access-log line for as long as the process lives. 0 = never rotate.
    log_max_bytes: int
    log_backups: int
    model_root: Path
    model_dir: Path
    state_dir: Path
    database_path: Path
    # Production recovery may pin one authoritative SQLite image.  In that
    # mode a vanished path is an outage, never permission to bootstrap an empty
    # replacement between configuration validation and sqlite3.connect().
    database_must_exist: bool
    files_dir: Path
    memory_vault_dir: Path
    # Plaintext Markdown is a second full-body representation, not a required
    # part of the knowledge store.  It therefore needs an explicit owner choice;
    # a missing setting is the body-free mode and an unknown value stops startup.
    memory_vault_mode: str
    backups_dir: Path
    # How many verified backups to keep locally. The schedule adds a full copy of the
    # database every 24 hours and nothing used to remove one, so the disk filled and
    # took the live instance down with it. 0 = keep everything (pre-0.86 behaviour).
    backup_keep: int
    exports_dir: Path
    # Offsite mirror for verified backups (external disk / synced folder).
    # Empty = mirroring off. A same-disk backup is not a real backup.
    backup_mirror_dir: Path | None
    # When set, mirror copies are AES-256 encrypted with this key file
    # (system `openssl`); local copies stay plain for fast restore.
    backup_encryption_key_file: Path | None
    # Experimental coordinated account erasure. Disabled until tombstones live
    # outside the SQLite image that restore replaces; otherwise an old backup can
    # resurrect both the account and every credential removed with it.
    account_hard_delete_enabled: bool

    # Owner-only engineering workbench. Keep the organ absent until the operator
    # deliberately admits its parser and outbound-diagnostics surface.
    engineer_mode_enabled: bool

    # Optional native Ubuntu capability plane. The backend talks only to the
    # unprivileged user agent over an authenticated Unix socket; package
    # mutation remains a separately disabled broker capability.
    host_control_enabled: bool
    host_agent_socket: Path
    host_agent_key_file: Path
    host_approval_signing_key_file: Path
    host_agent_id: str
    host_job_root: Path
    host_action_max_concurrency: int
    host_action_default_timeout_sec: float
    host_action_max_output_bytes: int
    host_package_install_enabled: bool
    host_desktop_control_enabled: bool
    host_one_shot_exec_enabled: bool
    host_public_network_enabled: bool
    host_allowed_cidrs: tuple[str, ...]
    host_allowed_path_roots: tuple[Path, ...]

    llm_base_url: str
    llm_model: str
    llm_enabled: bool
    llm_timeout_sec: float
    llm_max_tokens: int
    llm_api_key: str
    verify_answers: bool
    verify_min_answer_chars: int

    # Detachable, advisory-only text endpoint.  This namespace is deliberately
    # independent from the primary LLM configuration: an absent or broken
    # secondary must never alter primary startup or endpoint state.
    secondary_llm_enabled: bool
    secondary_llm_mode: str
    secondary_llm_base_url: str
    secondary_llm_model: str
    secondary_llm_api_key: str
    secondary_llm_ca_file: str
    secondary_llm_connect_timeout_sec: float
    secondary_llm_read_timeout_sec: float
    secondary_llm_call_budget_sec: float
    secondary_llm_admission_timeout_sec: float
    secondary_llm_health_interval_sec: float
    secondary_llm_cooldown_sec: float
    secondary_llm_max_context_tokens: int
    secondary_llm_max_concurrency: int
    secondary_llm_profile: str
    secondary_llm_workloads: tuple[str, ...]
    secondary_llm_allow_private_text: bool
    # Per-workload rollout keeps document mapping in discarded shadow while
    # the already-live Inbox extraction remains in assist.  Unknown values are
    # normalized to disabled by the closed env parser.
    secondary_llm_document_map_mode: str
    # Independent GPT-OSS semantic supervisor product policy.  Unknown values
    # fail closed to off.  P1 implements shadow observation only: assist/canary
    # are accepted as labels but never promote a proposal into execution.
    semantic_supervisor_mode: str
    semantic_supervisor_tasks: tuple[str, ...]
    semantic_supervisor_max_steps: int
    semantic_supervisor_max_review_rounds: int
    semantic_supervisor_timeout_sec: float

    embeddings_enabled: bool
    embeddings_base_url: str
    embeddings_api_key: str
    embeddings_model: str
    embeddings_index_batch: int
    embeddings_index_interval_sec: float
    # Потолок ОДНОГО тика в символах и доля времени на отдых после него.
    # «Объектов за тик» ничего не ограничивает: заметка и стостраничный документ
    # отличаются в сотни раз, и та же пачка из 64 штук весит то 6 тысяч символов,
    # то два миллиона.
    # Сколько живой поиск готов ждать вектор запроса. Отдельно от таймаута
    # фоновой индексации: та может ждать сколько угодно, человек — нет.
    retrieval_dense_query_budget_sec: float
    # Сколько времени тик досчёта может работать подряд. Бюджет в символах бережёт
    # ОДИН запрос от таймаута, а этот — определяет, сколько таких запросов уместится
    # в тик; вместе они подстраиваются под любую скорость сервиса.
    embeddings_index_tick_budget_sec: float
    embeddings_index_char_budget: int
    embeddings_index_rest_ratio: float
    embeddings_recall_candidates: int
    # Guard on the pure-Python dense-recall scan: cap how many (newest) vectors
    # are scored per query so latency stays bounded on a large corpus (0 = no cap).
    embeddings_dense_max_objects: int
    # Passage-level recall: a long Knowledge Object is additionally embedded in
    # overlapping chunks, so one relevant paragraph of a big import can carry the
    # object. 0 disables chunking entirely and restores pre-0.41 behaviour.
    embeddings_chunk_chars: int
    embeddings_chunk_overlap_chars: int
    embeddings_chunk_max_per_object: int
    # Corroboration weight when collapsing per-chunk cosines into one object score:
    # 0 is pure max-over-passages, higher values reward a document that matches in
    # several places over one lucky fragment.
    embeddings_chunk_blend: float
    # Row-level fuse over the (object-granular) dense cap, so one heavily chunked
    # corpus cannot blow up the scan even inside the object window.
    embeddings_chunk_scan_multiplier: int
    # Резидентные матрицы векторов в памяти процесса: снимают окно «новейшие N»
    # и чтение BLOB-ов на каждый запрос. Требует numpy; без него путь прежний.
    embeddings_resident_cache: bool
    # Cap on how many texts go into a single embeddings HTTP request; chunking
    # multiplies inputs per object, and a real endpoint rejects an oversized batch.
    embeddings_max_inputs_per_request: int
    # Near-duplicate Knowledge Object detection (cosine over stored vectors).
    dedup_threshold: float
    dedup_interval_sec: float
    # Incremental scan: objects probed against the corpus per tile (also the
    # granularity at which the scan cursor advances), and the wall-clock budget one
    # tick may spend across all tenants. Keep a tile well inside the supervisor's
    # 900 s worker timeout — asyncio.to_thread is NOT interruptible.
    dedup_scan_batch: int
    dedup_scan_max_seconds: float
    # Periodic retrieval-quality evaluation over the gold set (recall@k).
    eval_enabled: bool
    eval_interval_sec: float
    eval_k: int
    eval_mine_from_feedback: bool
    # Local speech-to-text for voice notes (§9). Optional 'jericho[voice]' extra;
    # transcripts are model-generated, so they route inbox-first like vision/OCR.
    whisper_enabled: bool
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_language: str
    whisper_max_audio_sec: float
    # Wall-clock ceiling for one local transcription await.  The native
    # CTranslate2 worker cannot be interrupted safely from Python, so timeout
    # returns the upload path to the caller while run_blocking keeps any
    # physically surviving work visible to admission/shutdown diagnostics.
    whisper_timeout_sec: float
    whisper_download_root: str
    # Local text-to-speech, spoken on request within a conversation. Same optional
    # 'jericho[voice]' extra as whisper above (piper-tts, onnxruntime-based; shares
    # PyAV, already pulled in transitively by faster-whisper, for OGG/Opus encoding).
    tts_enabled: bool
    tts_voice: str
    tts_max_chars: int
    tts_download_root: str

    purge_retention_days: int
    # Что попадает в ревью до того, как стать каноническим знанием. Перечисление,
    # а не рубильник: булев `ingestion_strict_review` описывал только текстовый
    # путь, файлы его не читали вовсе, и «строгий режим» на деле означал «строгий
    # к набранному руками, любой к стостраничному docx».
    ingestion_review_policy: str
    graph_max_depth: int
    # Ceiling on the lexical recall pool per search. Above it the fuzzy channel sees
    # only the most important/recent slice, and `strategy.lexical_pool_capped` says so.
    retrieval_pool_max: int
    # Cosine below which a dense score is not treated as evidence. Model-dependent:
    # the shipped default is measured against qwen3-embedding-0.6b.
    retrieval_dense_evidence_min: float
    rerank_base_url: str
    rerank_model: str
    rerank_api_key: str
    rerank_timeout_sec: float
    rerank_top: int
    rerank_confident_min: float

    api_host: str
    api_port: int
    api_token: str
    # TLS собственными силами uvicorn. Оба пути или ни одного: владелец слушает
    # 0.0.0.0 за пробросом, и без сертификата owner-токен и вся личная база
    # ходят через интернет открытым текстом.
    ssl_certfile: str
    ssl_keyfile: str
    # Additional public CA/certificate trusted by local clients of this backend
    # (Telegram bridge and live diagnostics).  This is deliberately separate from
    # the server key pair: a client never needs, and must never receive, the key.
    backend_ca_file: str
    api_require_token_on_loopback: bool
    api_user_rate_limit_per_minute: int
    api_auth_failure_limit_per_minute: int
    auth_failure_alert_threshold: int
    trust_proxy_headers: bool
    trusted_proxy_networks: list[str]
    cors_origins: list[str]

    telegram_bridge_secret: str
    telegram_realm_id: str
    telegram_user_rate_limit_per_minute: int
    telegram_global_rate_limit_per_minute: int
    telegram_allowed_chat_ids: list[int]
    telegram_owner_chat_ids: list[int]
    # Proxy for reaching api.telegram.org, e.g. "http://127.0.0.1:10808". Applies to
    # Telegram traffic only — the bridge always talks to its own backend directly.
    # Set this where Telegram is only reachable through a tunnel: it makes the bridge
    # ask for the tunnel explicitly instead of depending on one that rewrites the
    # host's routing table, which is both fragile and far harder to undo.
    telegram_proxy: str
    # Whether a NEW account auto-created for someone writing in an allowlisted GROUP
    # chat gets the full 'user' preset. Off by default: such an account would be able
    # to spend the owner's LLM budget, reach the web through this instance, upload
    # files and run background missions. A private chat is unaffected, and an existing
    # account never has its preset rewritten.
    telegram_group_members_full_access: bool
    telegram_signature_max_age_sec: int
    # Off by default: the bot stays deny-by-default (only allowlisted chats are
    # served). When on, anyone writing to it in a PRIVATE chat that is not on the
    # allowlist gets a real, isolated account of their own instead of silence —
    # provisioned with the 'newcomer' preset (chat, own knowledge, files, web
    # search — no missions, no code execution, no admin capability). Group chats
    # are unaffected: they still require the explicit allowlist, and
    # `telegram_group_members_full_access` governs their member preset as before.
    # An existing account's preset is never rewritten (`ensure_user` guarantee).
    telegram_open_registration: bool
    #: Какой набор прав получает учётка, заведённая автоматически при первом
    #: сообщении. Пусто — прежнее поведение (узкий пресет `newcomer`).
    #:
    #: Владелец 2026-08-02 попросил обратного: «все, кто первый раз написали,
    #: при создании учётки — с правами админа», чтобы люди видели документы и
    #: записи друг друга. Это ручка ровно для этого решения, и она же —
    #: единственная строка для отката.
    #:
    #: Взвесить стоит прямо здесь: вместе с открытой регистрацией
    #: (`FRIDAY_TELEGRAM_OPEN_REGISTRATION=1`) admin-пресет означает, что ЛЮБОЙ
    #: человек, написавший боту в Telegram, получает полный доступ к архиву —
    #: чужим документам, ФИО, суммам — и к административным действиям, включая
    #: чистку базы и выполнение кода. Список разрешённых чатов при выключенной
    #: открытой регистрации оставляет то же удобство без этой цены.
    new_account_preset: str
    #: Общий архив: знания, файлы, граф и «Входящие» — одни на всех.
    #:
    #: Владелец 2026-08-02 попросил, чтобы люди «видели документы и записи друг
    #: друга и могли с ними взаимодействовать». Одних админских прав для этого
    #: мало: они открывают админские МАРШРУТЫ, а обычный разговор по-прежнему
    #: ищет только в своём арендаторе — замерено, вопрос про чужую смету дал ноль
    #: попаданий и ушёл в интернет.
    #:
    #: Здесь снимается сама изоляция: все работают в одном арендаторе, поэтому
    #: любой находит, правит, подтверждает и удаляет материал любого. Авторство
    #: не теряется — кто добавил и кто действовал, пишется отдельно.
    #:
    #: Личная переписка общей НЕ становится: список разговоров остаётся своим у
    #: каждого. Общими становятся документы и знания — то, о чём просьба.
    shared_archive: bool
    #: «Я знаю, что открытая регистрация выдаёт всё это, и хочу именно так».
    #:
    #: Открытая регистрация вместе с широким пресетом или общим архивом отдаёт
    #: архив человеку, которого владелец не называл по идентификатору. Без этой
    #: подписи такое сочетание — ошибка конфигурации, то есть отказ подняться:
    #: попасть в него случайно, переключив один флаг, нельзя. С подписью оно
    #: работает и остаётся ГРОМКИМ — предупреждение никуда не девается, оно видно
    #: в `jericho doctor` и в панели.
    #:
    #: Почему подпись, а не запрет: живой экземпляр владельца работает в этом
    #: режиме с 2026-08-02 по его прямой просьбе, и валидатор, роняющий чужую
    #: работающую систему из-за несогласия с её хозяином, не защита, а поломка.
    open_registration_grants_full_access: bool

    autonomy_enabled: bool
    operator_full_autonomy: bool
    cognition_enabled: bool
    cognition_interval_sec: int
    cognition_max_tokens: int
    executive_max_active_missions: int
    executive_max_tasks_per_mission: int
    executive_task_tool_budget: int
    executive_tick_interval_sec: int
    #: Пределы ОДНОЙ миссии: сколько ей отпущено работы и до какого срока.
    #: Ноль в любом из них означает «без ограничения» — так что умолчания здесь
    #: и есть тот механизм, которого не хватало: столбцы, расход и проверка были
    #: на месте, а задавать бюджет было некому, и остановка не срабатывала ни
    #: разу. Числа щедрые намеренно: они отсекают зациклившуюся миссию, а не
    #: просто долгую.
    mission_budget_seconds: int
    mission_budget_tool_calls: int
    mission_budget_retries: int
    mission_deadline_hours: int
    workers_enabled: bool

    reminders_enabled: bool
    reminders_lead_days: int
    reminders_poll_interval_sec: int
    monitors_enabled: bool
    monitors_poll_interval_sec: int
    reflection_enabled: bool
    reflection_interval_sec: int
    reflection_min_knowledge: int
    chronicle_enabled: bool
    chronicle_interval_sec: int
    # Sentinel organ (§sentinel): self-monitoring — pushes health alerts
    # (crashed workers, stale/missing backups, unreachable vLLM) to the owner.
    sentinel_enabled: bool
    sentinel_interval_sec: int
    sentinel_check_llm: bool
    #: Lightweight one-token generation watchdog cadence.  Kept separate from
    #: ``sentinel_interval_sec`` so fast inference-stall detection never turns
    #: the full filesystem/database diagnostics into a once-a-minute scan.
    sentinel_generation_interval_sec: int
    # Inject the derived user model (people/projects/interests) into the
    # agent's untrusted context payload so answers can be personal.
    profile_in_context: bool
    # Quiet hours (UTC) apply to every proactive organ, not just reminders.
    quiet_hours_start: int
    quiet_hours_end: int

    brave_search_api_key: str
    tavily_api_key: str
    serper_api_key: str
    yandex_search_api_key: str
    yandex_search_type: str
    local_timezone: str
    llm_foreground_slots: int
    web_allow_private_networks: bool
    web_max_response_bytes: int
    #: Сколько раз ОДИН человек может выйти в интернет за сутки.
    #:
    #: Размер взят из замера, а не из головы: на живом архиве пик — 135 вызовов
    #: веб-инструментов на человека за сутки, медиана по человеко-дням 76.
    #: Потолок ниже этого сломал бы обычную работу, поэтому 400 — примерно
    #: тройной запас над настоящим пиком. Защита не от человека, а от цикла:
    #: способность `web.search` есть у пресета `user`, участников одиннадцать, и
    #: один зациклившийся research бьёт по платному ключу и по репутации адреса.
    web_daily_quota: int
    #: Пауза между обращениями к ОДНОМУ сайту, секунды.
    web_host_pause_sec: float

    max_upload_bytes: int
    max_extracted_text_chars: int
    pdf_parse_budget_sec: float
    max_archive_entries: int
    max_archive_uncompressed_bytes: int
    code_execution_enabled: bool
    code_execution_timeout_sec: int
    code_execution_max_output_bytes: int

    # MCP is an optional, code-owned connector boundary. Phase 1 accepts exactly
    # one local filesystem exchange: read-only inbox + create-only outbox. These
    # defaults keep every existing/direct FridaySettings constructor disabled.
    mcp_enabled: bool = False
    mcp_workspace_inbox_dir: Path | None = None
    mcp_workspace_outbox_dir: Path | None = None
    mcp_startup_timeout_sec: float = 15.0
    mcp_call_timeout_sec: float = 20.0
    mcp_result_chars: int = 7_000

    # Android-first Obsidian integration. Disabled by default so an upgrade does
    # not spawn a sync daemon or create plaintext vaults without owner consent.
    obsidian_enabled: bool = False
    obsidian_root: Path | None = None
    obsidian_vault_name: str = "Friday"
    obsidian_syncthing_binary: str = "/usr/local/bin/syncthing"
    obsidian_syncthing_min_version: str = "2.1.3"
    obsidian_syncthing_max_version: str = "2.2.0"
    obsidian_pairing_ttl_sec: int = 900
    obsidian_max_profiles: int = 64
    obsidian_transport_mode: str = "discovery_relay"
    obsidian_public_base_url: str = ""
    obsidian_reconcile_interval_sec: float = 10.0
    obsidian_rest_timeout_sec: float = 5.0
    obsidian_public_setup_rate_limit_per_minute: int = 10

    # V12 is a reversible orchestration migration over the same storage,
    # authorization and execution kernel. Defaults preserve the exact legacy
    # runtime; canary routes are inert until both the mode and a handler exist.
    router_mode: str = "legacy"
    router_canary_routes: tuple[str, ...] = ("file_read",)
    router_canary_user_ids: tuple[str, ...] = ()
    router_plan_timeout_sec: float = 12.0

    @property
    def is_loopback_bind(self) -> bool:
        return self.api_host in {"127.0.0.1", "localhost", "::1"}

    @property
    def api_tls_enabled(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)

    @property
    def llm_call_budget_sec(self) -> float:
        """Потолок ОДНОГО обращения к модели, повторы включены.

        Не `MAX_RETRIES * timeout`: это то самое число, которое здесь и режется.
        Один полный заход плюс половина второго оставляют место настоящему
        повтору после быстрого отказа, но не трём полным таймаутам подряд на
        эндпоинте, который соединение принимает и молчит.
        """

        return max(30.0, self.llm_timeout_sec * 1.5)

    @property
    def agent_turn_budget_sec(self) -> float:
        """Потолок ОДНОГО хода агента — единственный источник этого числа.

        До 0.171.0 его считали в двух местах по разным формулам: цикл
        инструментов брал `total_budget_sec * 2`, а мост — `llm_timeout_sec + 30`.
        На умолчаниях выходило 720 против 270: мост бросал запрос через четыре с
        половиной минуты, ядро имело право работать двенадцать и продолжало
        считать. Работа доводилась до конца и выбрасывалась, а обновление уходило
        на повтор — тот же дорогой ход считался заново.

        Два числа обязаны происходить из одного, иначе они разъедутся снова при
        первой же правке любой из формул.
        """

        return self.llm_call_budget_sec * 2

    @property
    def bridge_backend_timeout_sec(self) -> float:
        """Сколько мост ждёт ответа ядра: потолок хода плюс запас.

        Запас нужен на само HTTP-обращение, подпись, ожидание слота и запись
        ответа — то есть на всё, что ход тратит ВНЕ цикла инструментов. Строго
        больше потолка: равенство означало бы гонку, в которой мост иногда
        сдаётся за мгновение до готового ответа.
        """

        return self.agent_turn_budget_sec + 60.0

    @property
    def local_api_client_host(self) -> str:
        """Loopback destination matching the address family of a wildcard bind."""

        if self.api_host == "0.0.0.0":  # nosec B104 - destination, not a bind
            return "127.0.0.1"
        if self.api_host == "::":  # nosec B104 - destination, not a bind
            return "::1"
        return self.api_host

    @property
    def telegram_effective_allowed_chat_ids(self) -> list[int]:
        # Owner chats are always allowed; the union is the deny-by-default gate.
        # An empty result means no chat is allowed (a configured bridge must set
        # either an allowlist or an owner chat).
        return sorted({*self.telegram_allowed_chat_ids, *self.telegram_owner_chat_ids})

    @property
    def frontend_origin(self) -> str:
        # This only derives a browser origin from bind literals; no socket is opened here.
        host = self.local_api_client_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        scheme = "https" if self.api_tls_enabled else "http"
        return f"{scheme}://{host}:{self.api_port}"

    @property
    def obsidian_effective_root(self) -> Path:
        return Path(self.obsidian_root or (self.data_dir / "obsidian")).absolute()

    @property
    def secondary_llm_configured(self) -> bool:
        from friday.secondary_brain.contracts import (
            SecondaryEndpointConfig,
            secondary_configuration_is_admissible,
        )
        from friday.secondary_brain.profiles import get_secondary_runtime_admission

        admission = get_secondary_runtime_admission(
            self.secondary_llm_profile,
            mode=self.secondary_llm_mode,
        )
        profile = admission.profile if admission is not None else None
        endpoint = SecondaryEndpointConfig(
            base_url=self.secondary_llm_base_url,
            served_model_alias=self.secondary_llm_model,
            api_key=self.secondary_llm_api_key,
            ca_file=self.secondary_llm_ca_file,
            ca_sha256=profile.gateway_ca_certificate_sha256 if profile is not None else "",
            connect_timeout_sec=self.secondary_llm_connect_timeout_sec,
            read_timeout_sec=self.secondary_llm_read_timeout_sec,
            call_budget_sec=self.secondary_llm_call_budget_sec,
            admission_timeout_sec=self.secondary_llm_admission_timeout_sec,
            health_interval_sec=self.secondary_llm_health_interval_sec,
            cooldown_sec=self.secondary_llm_cooldown_sec,
            max_context_tokens=self.secondary_llm_max_context_tokens,
            max_concurrency=self.secondary_llm_max_concurrency,
            max_output_tokens=profile.max_output_tokens if profile is not None else 0,
            profile_id=self.secondary_llm_profile,
            profile_manifest_sha256=profile.manifest_sha256 if profile is not None else "",
        )
        return secondary_configuration_is_admissible(
            endpoint,
            primary_base_url=self.llm_base_url,
            primary_model=self.llm_model,
            primary_timeout_sec=self.llm_timeout_sec,
            workload_names=self.secondary_llm_workloads,
            mode=self.secondary_llm_mode,
            allow_private_text=self.secondary_llm_allow_private_text,
            document_map_mode=self.secondary_llm_document_map_mode,
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "profile": profile_public_dict(self.profile),
            "engineer_mode": {"enabled": self.engineer_mode_enabled},
            "host_control": {
                "enabled": self.host_control_enabled,
                "agent_id": self.host_agent_id if self.host_control_enabled else "",
                "package_install_enabled": self.host_package_install_enabled,
                "desktop_control_enabled": self.host_desktop_control_enabled,
                "one_shot_exec_enabled": self.host_one_shot_exec_enabled,
                "public_network_enabled": self.host_public_network_enabled,
                "allowed_cidr_count": len(self.host_allowed_cidrs),
                "allowed_path_root_count": len(self.host_allowed_path_roots),
            },
            "llm": {
                "enabled": self.llm_enabled,
                "base_url": self.llm_base_url,
                "model": self.llm_model,
                "max_tokens": self.llm_max_tokens,
                # Never expose the token itself — only whether auth is configured.
                "auth": bool(self.llm_api_key),
                "verify_answers": self.verify_answers,
                "verify_min_answer_chars": self.verify_min_answer_chars,
            },
            "secondary_llm": {
                "enabled": self.secondary_llm_enabled,
                "mode": self.secondary_llm_mode,
                "configured": self.secondary_llm_configured,
                "auth": bool(self.secondary_llm_api_key),
                "private_ca": bool(self.secondary_llm_ca_file),
                "max_context_tokens": self.secondary_llm_max_context_tokens,
                "max_concurrency": self.secondary_llm_max_concurrency,
                "profile": self.secondary_llm_profile,
                "allow_private_text": self.secondary_llm_allow_private_text,
                "document_map_mode": self.secondary_llm_document_map_mode,
                "workloads": list(self.secondary_llm_workloads),
            },
            "semantic_supervisor": {
                "mode": self.semantic_supervisor_mode,
                "tasks": list(self.semantic_supervisor_tasks),
                "max_steps": self.semantic_supervisor_max_steps,
                "max_review_rounds": self.semantic_supervisor_max_review_rounds,
                "timeout_sec": self.semantic_supervisor_timeout_sec,
                "promotion_admitted": False,
            },
            "orchestration": {
                "mode": self.router_mode,
                "canary_routes": list(self.router_canary_routes),
                "canary_user_count": len(self.router_canary_user_ids),
                "plan_timeout_sec": self.router_plan_timeout_sec,
            },
            "embeddings": {
                "enabled": self.embeddings_enabled,
                # What the retrieval backend will actually do, which is not the same
                # thing: `EmbeddingBackend.remote_enabled` needs a base URL and a model
                # name too, and publishing the raw flag reported a working semantic
                # search to an operator who had none.
                "effective": bool(
                    self.embeddings_enabled and self.embeddings_base_url and self.embeddings_model
                ),
                "base_url": self.embeddings_base_url,
                "auth": bool(self.embeddings_api_key),
                "model": self.embeddings_model,
                "index_batch": self.embeddings_index_batch,
                "index_char_budget": self.embeddings_index_char_budget,
                "index_tick_budget_sec": self.embeddings_index_tick_budget_sec,
                "index_rest_ratio": self.embeddings_index_rest_ratio,
                "index_interval_sec": self.embeddings_index_interval_sec,
                "recall_candidates": self.embeddings_recall_candidates,
                # Whether passage-level recall is on at all; the tuning knobs stay
                # internal, like embeddings_dense_max_objects.
                "chunk_chars": self.embeddings_chunk_chars,
                "chunk_overlap_chars": self.embeddings_chunk_overlap_chars,
                "dedup_threshold": self.dedup_threshold,
                "dedup_interval_sec": self.dedup_interval_sec,
                "eval_enabled": self.eval_enabled,
                "eval_k": self.eval_k,
            },
            "data": {
                "database_must_exist": self.database_must_exist,
                "memory_vault": {
                    "mode": self.memory_vault_mode,
                    "body_free_mode": self.memory_vault_mode == "disabled",
                },
                "purge_retention_days": self.purge_retention_days,
                "ingestion_review_policy": self.ingestion_review_policy,
                "backup_mirror_configured": self.backup_mirror_dir is not None,
                "backup_encryption_configured": self.backup_encryption_key_file is not None,
                "account_hard_delete_enabled": self.account_hard_delete_enabled,
            },
            "mcp": {
                "enabled": self.mcp_enabled,
                "workspace_configured": bool(self.mcp_workspace_inbox_dir and self.mcp_workspace_outbox_dir),
                "filesystem_mode": "inbox-read/outbox-create" if self.mcp_enabled else "disabled",
            },
            "obsidian": {
                "enabled": self.obsidian_enabled,
                "root": str(self.obsidian_effective_root),
                "vault_name": self.obsidian_vault_name,
                "transport_mode": self.obsidian_transport_mode,
                "public_setup_configured": bool(self.obsidian_public_base_url),
                "syncthing_version_range": [
                    self.obsidian_syncthing_min_version,
                    self.obsidian_syncthing_max_version,
                ],
                "max_profiles": self.obsidian_max_profiles,
            },
            "graph": {"max_depth": self.graph_max_depth},
            "api": {"host": self.api_host, "port": self.api_port, "tls": self.api_tls_enabled},
            "autonomy": {
                "enabled": self.autonomy_enabled,
                "operator_full_autonomy": self.operator_full_autonomy,
                "cognition_enabled": self.cognition_enabled,
            },
            "executive": {
                "max_active_missions": self.executive_max_active_missions,
                "max_tasks_per_mission": self.executive_max_tasks_per_mission,
                "task_tool_budget": self.executive_task_tool_budget,
                "tick_interval_sec": self.executive_tick_interval_sec,
                "mission_budget_seconds": self.mission_budget_seconds,
                "mission_budget_tool_calls": self.mission_budget_tool_calls,
                "mission_budget_retries": self.mission_budget_retries,
                "mission_deadline_hours": self.mission_deadline_hours,
            },
            "organs": {
                "reminders_enabled": self.reminders_enabled,
                "reminders_lead_days": self.reminders_lead_days,
                "reminders_poll_interval_sec": self.reminders_poll_interval_sec,
                "monitors_enabled": self.monitors_enabled,
                "monitors_poll_interval_sec": self.monitors_poll_interval_sec,
                "reflection_enabled": self.reflection_enabled,
                "reflection_interval_sec": self.reflection_interval_sec,
                "reflection_min_knowledge": self.reflection_min_knowledge,
                "chronicle_enabled": self.chronicle_enabled,
                "chronicle_interval_sec": self.chronicle_interval_sec,
                "sentinel_enabled": self.sentinel_enabled,
                "sentinel_interval_sec": self.sentinel_interval_sec,
                "sentinel_check_llm": self.sentinel_check_llm,
                "sentinel_generation_interval_sec": self.sentinel_generation_interval_sec,
                "profile_in_context": self.profile_in_context,
                "quiet_hours": [self.quiet_hours_start, self.quiet_hours_end],
            },
            "security": {
                "token_configured": bool(self.api_token),
                "telegram_bridge_configured": bool(self.telegram_bridge_secret),
                "telegram_allowlist_configured": bool(self.telegram_effective_allowed_chat_ids),
                "code_execution_enabled": self.code_execution_enabled,
            },
        }


def load_settings(profile_name: str | None = None) -> FridaySettings:
    load_local_env_file()
    home = default_home().resolve()
    selected_name = str(profile_name or env("FRIDAY_PROFILE", "qwen36-27b-nvfp4-nvidia"))
    profile = PROFILES.get(selected_name)
    if profile is None:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown FRIDAY_PROFILE={selected_name!r}. Valid profiles: {valid}")

    data_dir = Path(env("FRIDAY_DATA_DIR", home / "data")).expanduser().resolve()
    cache_dir = Path(env("FRIDAY_CACHE_DIR", home / "cache")).expanduser().resolve()
    log_dir = Path(env("FRIDAY_LOG_DIR", home / "logs")).expanduser().resolve()
    # The requested model location is <project>/models/<model-name>.
    model_root = Path(env("FRIDAY_MODEL_ROOT", home / "models")).expanduser().resolve()
    state_dir = Path(env("FRIDAY_STATE_DIR", data_dir / "state")).expanduser().resolve()
    database_override = env("FRIDAY_DATABASE_PATH", "").strip()
    database_path = (
        Path(database_override).expanduser().resolve() if database_override else _existing_database(state_dir)
    )
    database_must_exist = _bool_env("FRIDAY_DATABASE_MUST_EXIST", False)
    if database_must_exist:
        try:
            database_is_usable = database_path.is_file() and database_path.stat().st_size > 0
        except OSError:
            database_is_usable = False
        if not database_is_usable:
            raise RuntimeError(
                "The configured Friday database must already exist and contain data; "
                "refusing to create a replacement database."
            )
    llm_base_url = env("FRIDAY_LLM_BASE_URL", "http://127.0.0.1:8001/v1").rstrip("/")

    ssl_certfile = env("FRIDAY_SSL_CERTFILE", "").strip()
    ssl_keyfile = env("FRIDAY_SSL_KEYFILE", "").strip()
    api_port = _int_env("FRIDAY_API_PORT", 8000, minimum=1)
    cors_origins = _list_env("FRIDAY_CORS_ORIGINS")
    if not cors_origins:
        api_scheme = "https" if ssl_certfile and ssl_keyfile else "http"
        cors_origins = [
            f"{api_scheme}://127.0.0.1:{api_port}",
            f"{api_scheme}://localhost:{api_port}",
            f"{api_scheme}://[::1]:{api_port}",
        ]

    return FridaySettings(
        home=home,
        profile=profile,
        data_dir=data_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        log_max_bytes=_int_env("FRIDAY_LOG_MAX_BYTES", 16 * 1024 * 1024, minimum=0),
        log_backups=_int_env("FRIDAY_LOG_BACKUPS", 3, minimum=0),
        model_root=model_root,
        model_dir=model_root / profile.model_dir_name,
        state_dir=state_dir,
        database_path=database_path,
        database_must_exist=database_must_exist,
        files_dir=Path(env("FRIDAY_FILES_DIR", data_dir / "files")).expanduser().resolve(),
        # This path is a deletion boundary for legacy plaintext even when the
        # projector is disabled. Resolving it here would bless a symlink target
        # before MemoryVaultDeletionHandle can apply O_NOFOLLOW traversal.
        memory_vault_dir=_absolute_lexical_path(env("FRIDAY_MEMORY_VAULT_DIR", data_dir / "memory-vault")),
        memory_vault_mode=_choice_env(
            "FRIDAY_MEMORY_VAULT_MODE",
            "disabled",
            MEMORY_VAULT_MODES,
        ),
        backups_dir=Path(env("FRIDAY_BACKUPS_DIR", data_dir / "backups")).expanduser().resolve(),
        exports_dir=Path(env("FRIDAY_EXPORTS_DIR", data_dir / "exports")).expanduser().resolve(),
        backup_mirror_dir=(
            Path(os.environ["FRIDAY_BACKUP_MIRROR_DIR"]).expanduser().resolve()
            if env("FRIDAY_BACKUP_MIRROR_DIR", "").strip()
            else None
        ),
        backup_encryption_key_file=(
            Path(os.environ["FRIDAY_BACKUP_ENCRYPTION_KEY_FILE"]).expanduser().resolve()
            if env("FRIDAY_BACKUP_ENCRYPTION_KEY_FILE", "").strip()
            else None
        ),
        # Code-owned quarantine: this must not be an operator/env escape hatch.
        # Restore replaces the SQLite image which currently owns the tombstones.
        account_hard_delete_enabled=False,
        engineer_mode_enabled=_bool_env("FRIDAY_ENGINEER_MODE_ENABLED", False),
        host_control_enabled=_bool_env("FRIDAY_HOST_CONTROL_ENABLED", False),
        host_agent_socket=Path(
            env("FRIDAY_HOST_AGENT_SOCKET", "/run/friday-host-agent/agent.sock")
        ).expanduser(),
        host_agent_key_file=Path(
            env("FRIDAY_HOST_AGENT_KEY_FILE", data_dir / "host-control" / "agent.key")
        ).expanduser(),
        host_approval_signing_key_file=Path(
            env(
                "FRIDAY_HOST_APPROVAL_SIGNING_KEY_FILE",
                data_dir / "host-control" / "backend-approval-signing.key",
            )
        ).expanduser(),
        host_agent_id=env("FRIDAY_HOST_AGENT_ID", "local-user-agent").strip(),
        host_job_root=Path(env("FRIDAY_HOST_JOB_ROOT", data_dir / "host-control" / "jobs"))
        .expanduser()
        .absolute(),
        host_action_max_concurrency=_int_env(
            "FRIDAY_HOST_ACTION_MAX_CONCURRENCY",
            2,
            minimum=1,
        ),
        host_action_default_timeout_sec=_float_env(
            "FRIDAY_HOST_ACTION_DEFAULT_TIMEOUT_SEC",
            300.0,
            minimum=1.0,
        ),
        host_action_max_output_bytes=_int_env(
            "FRIDAY_HOST_ACTION_MAX_OUTPUT_BYTES",
            8 * 1024 * 1024,
            minimum=1024,
        ),
        host_package_install_enabled=_bool_env("FRIDAY_HOST_PACKAGE_INSTALL_ENABLED", False),
        host_desktop_control_enabled=_bool_env("FRIDAY_HOST_DESKTOP_CONTROL_ENABLED", False),
        host_one_shot_exec_enabled=_bool_env("FRIDAY_HOST_ONE_SHOT_EXEC_ENABLED", False),
        host_public_network_enabled=_bool_env("FRIDAY_HOST_PUBLIC_NETWORK_ENABLED", False),
        host_allowed_cidrs=tuple(_list_env("FRIDAY_HOST_ALLOWED_CIDRS")),
        host_allowed_path_roots=tuple(
            Path(item).expanduser().absolute() for item in _list_env("FRIDAY_HOST_ALLOWED_PATH_ROOTS")
        ),
        llm_base_url=llm_base_url,
        llm_model=env("FRIDAY_LLM_MODEL", "dispatcher"),
        llm_enabled=_bool_env("FRIDAY_LLM_ENABLED", True),
        llm_timeout_sec=_float_env("FRIDAY_LLM_TIMEOUT_SEC", 240.0, minimum=1.0),
        llm_max_tokens=_int_env("FRIDAY_LLM_MAX_TOKENS", 2048, minimum=64),
        llm_api_key=env("FRIDAY_LLM_API_KEY", "").strip(),
        # Code-owned quarantine: the same-model judge is not an independent
        # authority and its false negatives have already suppressed valid
        # attachment answers. Keep the machinery testable through an explicit
        # dataclass replacement, but do not expose it to runtime configuration.
        verify_answers=False,
        verify_min_answer_chars=_int_env("FRIDAY_VERIFY_MIN_ANSWER_CHARS", 300, minimum=1),
        secondary_llm_enabled=_bool_env("FRIDAY_SECONDARY_LLM_ENABLED", False),
        secondary_llm_mode=_fail_closed_choice_env(
            "FRIDAY_SECONDARY_LLM_MODE",
            "disabled",
            ("disabled", "shadow", "assist"),
        ),
        secondary_llm_base_url=env("FRIDAY_SECONDARY_LLM_BASE_URL", "").strip().rstrip("/"),
        secondary_llm_model=env("FRIDAY_SECONDARY_LLM_MODEL", "").strip(),
        secondary_llm_api_key=env("FRIDAY_SECONDARY_LLM_API_KEY", "").strip(),
        secondary_llm_ca_file=env("FRIDAY_SECONDARY_LLM_CA_FILE", "").strip(),
        secondary_llm_connect_timeout_sec=_float_env(
            "FRIDAY_SECONDARY_LLM_CONNECT_TIMEOUT_SEC", 1.0, minimum=0.05
        ),
        secondary_llm_read_timeout_sec=_float_env("FRIDAY_SECONDARY_LLM_READ_TIMEOUT_SEC", 12.0, minimum=0.1),
        secondary_llm_call_budget_sec=_float_env("FRIDAY_SECONDARY_LLM_CALL_BUDGET_SEC", 15.0, minimum=0.1),
        secondary_llm_admission_timeout_sec=_float_env(
            "FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC", 0.10, minimum=0.001
        ),
        secondary_llm_health_interval_sec=_float_env(
            "FRIDAY_SECONDARY_LLM_HEALTH_INTERVAL_SEC", 30.0, minimum=1.0
        ),
        secondary_llm_cooldown_sec=_float_env("FRIDAY_SECONDARY_LLM_COOLDOWN_SEC", 60.0, minimum=0.0),
        # A measured cap is mandatory before traffic can be admitted.  Zero is
        # therefore the safe, intentionally unusable default.
        secondary_llm_max_context_tokens=_int_env("FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS", 0, minimum=0),
        secondary_llm_max_concurrency=_int_env("FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY", 1, minimum=1),
        secondary_llm_profile=env("FRIDAY_SECONDARY_LLM_PROFILE", "").strip(),
        secondary_llm_workloads=tuple(
            _list_env(
                "FRIDAY_SECONDARY_LLM_WORKLOADS",
                ["extract"],
            )
        ),
        secondary_llm_allow_private_text=_bool_env("FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT", False),
        secondary_llm_document_map_mode=_fail_closed_choice_env(
            "FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE",
            "disabled",
            ("disabled", "shadow", "assist"),
        ),
        semantic_supervisor_mode=_fail_closed_choice_env(
            "FRIDAY_SEMANTIC_SUPERVISOR_MODE",
            "off",
            ("off", "shadow", "assist", "canary"),
        ),
        semantic_supervisor_tasks=tuple(
            item.casefold() for item in _list_env("FRIDAY_SEMANTIC_SUPERVISOR_TASKS") if item.strip()
        ),
        semantic_supervisor_max_steps=_fail_closed_rollout_int_env(
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS",
            6,
            invalid=0,
        ),
        semantic_supervisor_max_review_rounds=_fail_closed_rollout_int_env(
            "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS",
            1,
            invalid=-1,
        ),
        semantic_supervisor_timeout_sec=_fail_closed_rollout_float_env(
            "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
            12.0,
            invalid=0.0,
        ),
        embeddings_enabled=_bool_env("FRIDAY_EMBEDDINGS_ENABLED", False),
        embeddings_base_url=env("FRIDAY_EMBEDDINGS_BASE_URL", llm_base_url).rstrip("/"),
        # A separate embeddings service may share the LLM's token; default to it.
        # `.get(name, default)` only falls back when the variable is ABSENT, and an
        # env file that writes `FRIDAY_EMBEDDINGS_API_KEY=` supplies an empty VALUE —
        # so the intended inheritance silently did not happen and the backend sent no
        # Authorization header at all. Observed as 401 on every indexing request while
        # a single-key check passed, because it resolved the fallback differently.
        embeddings_api_key=(
            env("FRIDAY_EMBEDDINGS_API_KEY", "").strip() or env("FRIDAY_LLM_API_KEY", "").strip()
        ),
        embeddings_model=env("FRIDAY_EMBEDDINGS_MODEL", ""),
        embeddings_index_batch=_int_env("FRIDAY_EMBEDDINGS_INDEX_BATCH", 64, minimum=1),
        retrieval_dense_query_budget_sec=_float_env(
            "FRIDAY_RETRIEVAL_DENSE_QUERY_BUDGET_SEC", 4.0, minimum=0.5
        ),
        embeddings_index_tick_budget_sec=_float_env(
            "FRIDAY_EMBEDDINGS_INDEX_TICK_BUDGET_SEC", 60.0, minimum=5.0
        ),
        embeddings_index_char_budget=_int_env("FRIDAY_EMBEDDINGS_INDEX_CHAR_BUDGET", 200_000, minimum=1000),
        embeddings_index_rest_ratio=_float_env("FRIDAY_EMBEDDINGS_INDEX_REST_RATIO", 1.0, minimum=0.0),
        embeddings_index_interval_sec=_float_env("FRIDAY_EMBEDDINGS_INDEX_INTERVAL_SEC", 120.0, minimum=5.0),
        embeddings_recall_candidates=_int_env("FRIDAY_EMBEDDINGS_RECALL_CANDIDATES", 40, minimum=1),
        embeddings_dense_max_objects=_int_env("FRIDAY_EMBEDDINGS_DENSE_MAX_OBJECTS", 5000, minimum=0),
        # ~1200 characters is roughly 300-420 tokens, inside the 512-token window
        # multilingual embedding models are trained on. 0 = chunking off.
        embeddings_chunk_chars=_int_env("FRIDAY_EMBEDDINGS_CHUNK_CHARS", 1200, minimum=0),
        # ~17% overlap: any fact shorter than this lands whole in at least one chunk.
        embeddings_chunk_overlap_chars=_int_env("FRIDAY_EMBEDDINGS_CHUNK_OVERLAP_CHARS", 200, minimum=0),
        embeddings_chunk_max_per_object=_int_env("FRIDAY_EMBEDDINGS_CHUNK_MAX_PER_OBJECT", 64, minimum=1),
        embeddings_chunk_blend=_float_env("FRIDAY_EMBEDDINGS_CHUNK_BLEND", 0.25, minimum=0.0),
        embeddings_chunk_scan_multiplier=_int_env("FRIDAY_EMBEDDINGS_CHUNK_SCAN_MULTIPLIER", 4, minimum=1),
        embeddings_resident_cache=_bool_env("FRIDAY_EMBEDDINGS_RESIDENT_CACHE", True),
        embeddings_max_inputs_per_request=_int_env("FRIDAY_EMBEDDINGS_MAX_INPUTS_PER_REQUEST", 64, minimum=1),
        # 0.95, not the previous 0.92, which sat INSIDE the measured distribution of
        # non-duplicates (two weekly meeting notes from one template: 0.928; two
        # entries about one apartment: 0.917 and 0.914). Whether a series of minutes
        # got proposed for merging came down to the third decimal. 0.95 catches
        # exactly as many real duplicates on the measured stand — the classes it
        # could reach, 0.888 and below, are unreachable at any safe value — with no
        # false proposal. See `jericho/dedup.py::_MEASURED_NON_DUPLICATE_CEILING`
        # and `tools/dedup_threshold_probe.py`. Raising costs no rescan; lowering
        # triggers one, by design.
        dedup_threshold=_float_env("FRIDAY_DEDUP_THRESHOLD", 0.95, minimum=0.5),
        dedup_interval_sec=_float_env("FRIDAY_DEDUP_INTERVAL_SEC", 21600, minimum=300),
        dedup_scan_batch=_int_env("FRIDAY_DEDUP_SCAN_BATCH", 512, minimum=1),
        dedup_scan_max_seconds=_float_env("FRIDAY_DEDUP_SCAN_MAX_SECONDS", 600.0, minimum=1.0),
        eval_enabled=_bool_env("FRIDAY_EVAL_ENABLED", True),
        eval_interval_sec=_float_env("FRIDAY_EVAL_INTERVAL_SEC", 86400, minimum=300),
        eval_k=_int_env("FRIDAY_EVAL_K", 10, minimum=1),
        # Grow the eval gold set from confirmed positive answer feedback (the cited
        # KOs become the expected results for that query). Never overwrites a manual
        # case. Disable to keep the gold set hand-curated.
        eval_mine_from_feedback=_bool_env("FRIDAY_EVAL_MINE_FROM_FEEDBACK", True),
        whisper_enabled=_bool_env("FRIDAY_WHISPER_ENABLED", False),
        whisper_model=env("FRIDAY_WHISPER_MODEL", "small"),
        whisper_device=env("FRIDAY_WHISPER_DEVICE", "cpu"),
        whisper_compute_type=env("FRIDAY_WHISPER_COMPUTE_TYPE", "int8"),
        whisper_language=env("FRIDAY_WHISPER_LANGUAGE", ""),
        whisper_max_audio_sec=_float_env("FRIDAY_WHISPER_MAX_AUDIO_SEC", 900.0, minimum=0.0),
        whisper_timeout_sec=_float_env("FRIDAY_WHISPER_TIMEOUT_SEC", 180.0, minimum=1.0),
        whisper_download_root=env("FRIDAY_WHISPER_DOWNLOAD_ROOT", ""),
        tts_enabled=_bool_env("FRIDAY_TTS_ENABLED", False),
        tts_voice=env("FRIDAY_TTS_VOICE", "ru_RU-irina-medium"),
        tts_max_chars=_int_env("FRIDAY_TTS_MAX_CHARS", 2000, minimum=1),
        tts_download_root=env("FRIDAY_TTS_DOWNLOAD_ROOT", ""),
        purge_retention_days=_int_env("FRIDAY_PURGE_RETENTION_DAYS", 30, minimum=0),
        backup_keep=_int_env("FRIDAY_BACKUP_KEEP", 14, minimum=0),
        ingestion_review_policy=_choice_env(
            "FRIDAY_INGESTION_REVIEW_POLICY", "unless_explicit", REVIEW_POLICIES
        ),
        graph_max_depth=_int_env("FRIDAY_GRAPH_MAX_DEPTH", 2, minimum=1),
        retrieval_pool_max=_int_env("FRIDAY_RETRIEVAL_POOL_MAX", 400, minimum=10),
        # 0.35 перемерено на честном индексе — см. `_DENSE_EVIDENCE_MIN_DEFAULT` в
        # retrieval и §15 ARCHITECTURE. Число обязано совпадать в трёх местах (здесь,
        # там и в доках), и это стережёт тест.
        retrieval_dense_evidence_min=_float_env("FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN", 0.35, minimum=0.0),
        # Отдельная модель-переранжировщик (cross-encoder). Выключена, пока не задан
        # адрес. Замерено, ЧТО она чинит: внутри пула кандидатов прежний порядок был
        # подбрасыванием монеты (AUC 0.512), плотный канал тоже (0.488), cross-encoder
        # даёт 0.754 на отложенной половине. Подробности — в
        # `retrieval/_rerank_backend.py`.
        rerank_base_url=env("FRIDAY_RERANK_BASE_URL", "").strip(),
        rerank_model=env("FRIDAY_RERANK_MODEL", "").strip(),
        # Ключ падает обратно на ключ LLM — как у эмбеддингов, и по той же причине:
        # на этой установке все три сервиса за одним ключом, а пустой ключ в проверке
        # уже однажды увёл меня в ложный диагноз (401 приняли за требование сервиса).
        rerank_api_key=(env("FRIDAY_RERANK_API_KEY", "").strip() or env("FRIDAY_LLM_API_KEY", "").strip()),
        rerank_timeout_sec=_float_env("FRIDAY_RERANK_TIMEOUT_SEC", 20.0, minimum=1.0),
        # Сколько верхних кандидатов отдавать на переранжирование. 0 — выключено.
        # Для явно включённого reranker измеренная глубина — 40: на 20 трудных живых
        # эталонах recall@10 вырос 0.60 -> 0.70 (3 выигрыша, 1 потеря, net +2 при
        # заранее объявленном критерии +2), p50 полного поиска — 1857 -> 2523 мс.
        # Default остаётся 0: адрес и модель задаются установкой явно. Клиент сам
        # делит пары на части, чтобы не превысить предел службы.
        rerank_top=_int_env("FRIDAY_RERANK_TOP", 0, minimum=0),
        # Порог «похоже на ответ» по скору переранжировщика. ОТСЕКАЕТ: документ ниже
        # порога не доходит до человека, число отсеянных уходит в
        # `strategy.rerank_dropped`, причина — в explain-трейс как
        # `rerank_below_threshold`. Замер размена — в `_rerank_backend.py`.
        # `0` выключает отсев, не выключая переранжирование.
        # 0.10 обязано совпадать с `CONFIDENT_MIN_DEFAULT` в
        # `retrieval/_rerank_backend.py` — импортировать оттуда нельзя, там цикл через
        # пакет retrieval, поэтому расхождение стережёт тест.
        rerank_confident_min=_float_env("FRIDAY_RERANK_CONFIDENT_MIN", 0.10, minimum=0.0),
        api_host=env("FRIDAY_API_HOST", "127.0.0.1"),
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        backend_ca_file=env("FRIDAY_BACKEND_CA_FILE", "").strip(),
        api_port=api_port,
        api_token=env("FRIDAY_API_TOKEN", ""),
        api_require_token_on_loopback=_bool_env("FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK", True),
        api_user_rate_limit_per_minute=_int_env("FRIDAY_API_USER_RATE_LIMIT_PER_MINUTE", 240, minimum=1),
        api_auth_failure_limit_per_minute=_int_env("FRIDAY_API_AUTH_FAILURE_LIMIT_PER_MINUTE", 10, minimum=1),
        # Diagnostics/sentinel raise a warning when auth failures over the last 24h
        # reach this count (possible brute-force / leaked-token abuse). The 24h
        # window (vs. the hourly sentinel tick and quiet hours) means a sustained or
        # overnight burst is not aliased away. 0 disables.
        auth_failure_alert_threshold=_int_env("FRIDAY_AUTH_FAILURE_ALERT_THRESHOLD", 60, minimum=0),
        trust_proxy_headers=_bool_env("FRIDAY_TRUST_PROXY_HEADERS", False),
        trusted_proxy_networks=_list_env("FRIDAY_TRUSTED_PROXY_NETWORKS", ["127.0.0.1/32", "::1/128"]),
        cors_origins=cors_origins,
        telegram_bridge_secret=env("FRIDAY_TELEGRAM_BRIDGE_SECRET", ""),
        telegram_realm_id=env("FRIDAY_TELEGRAM_REALM_ID", "telegram"),
        telegram_user_rate_limit_per_minute=_int_env(
            "FRIDAY_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE", 30, minimum=1
        ),
        telegram_global_rate_limit_per_minute=_int_env(
            "FRIDAY_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE", 1200, minimum=1
        ),
        telegram_allowed_chat_ids=_int_list_env("FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS"),
        telegram_owner_chat_ids=_int_list_env("FRIDAY_TELEGRAM_OWNER_CHAT_IDS"),
        telegram_proxy=env("FRIDAY_TELEGRAM_PROXY", "").strip(),
        telegram_group_members_full_access=_bool_env("FRIDAY_TELEGRAM_GROUP_MEMBERS_FULL_ACCESS", False),
        telegram_signature_max_age_sec=_int_env("FRIDAY_TELEGRAM_SIGNATURE_MAX_AGE_SEC", 90, minimum=10),
        telegram_open_registration=_bool_env("FRIDAY_TELEGRAM_OPEN_REGISTRATION", False),
        new_account_preset=env("FRIDAY_NEW_ACCOUNT_PRESET", "").strip(),
        shared_archive=_bool_env("FRIDAY_SHARED_ARCHIVE", False),
        open_registration_grants_full_access=_bool_env("FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS", False),
        autonomy_enabled=_bool_env("FRIDAY_AUTONOMY_ENABLED", True),
        operator_full_autonomy=_bool_env("FRIDAY_OPERATOR_FULL_AUTONOMY", False),
        cognition_enabled=_bool_env("FRIDAY_COGNITION_ENABLED", True),
        cognition_interval_sec=_int_env("FRIDAY_COGNITION_INTERVAL_SEC", 300, minimum=30),
        # ⚠️ ЗАМЕР ИСПРАВЛЕН. Сначала здесь стояло «совету нужно 2516–3616 токенов,
        # потому что до JSON модель думает». Это измерено МИМО роутера, прямым
        # обращением к сервису, а рабочий путь идёт через роутер — и роутер шлёт
        # `chat_template_kwargs: {"enable_thinking": False}`. Перемерено ТЕМ ЖЕ путём,
        # которым ходит код: 9 прогонов на документах в 1, 6 и 14 тысяч знаков дают
        # **78–125 токенов**, потому что рассуждение подавлено.
        #
        # 249 сбоёв разбора были настоящими, но их причина — прежний рантайм, который
        # флаг игнорировал и жёг бюджет на монолог. После перезапуска vLLM с флагами
        # инструментов он флаг соблюдает.
        #
        # 4096 оставлены сознательно: потолок не тратится впустую (модель, которой
        # хватает ста токенов, на них и остановится), а рантайм могут перезапустить
        # без флага, и тогда 512 снова не хватит.
        cognition_max_tokens=_int_env("FRIDAY_COGNITION_MAX_TOKENS", 4096, minimum=64),
        executive_max_active_missions=_int_env("FRIDAY_EXECUTIVE_MAX_ACTIVE_MISSIONS", 8, minimum=1),
        executive_max_tasks_per_mission=_int_env("FRIDAY_EXECUTIVE_MAX_TASKS_PER_MISSION", 12, minimum=1),
        executive_task_tool_budget=_int_env("FRIDAY_EXECUTIVE_TASK_TOOL_BUDGET", 6, minimum=1),
        executive_tick_interval_sec=_int_env("FRIDAY_EXECUTIVE_TICK_INTERVAL_SEC", 15, minimum=5),
        # Два часа чистой работы, три сотни вызовов, тридцать повторов, двое
        # суток срока. Для сравнения: полный план — 12 шагов по 6 вызовов, то
        # есть 72; исчерпать три сотни может только миссия, ходящая по кругу.
        mission_budget_seconds=_int_env("FRIDAY_MISSION_BUDGET_SECONDS", 7200, minimum=0),
        mission_budget_tool_calls=_int_env("FRIDAY_MISSION_BUDGET_TOOL_CALLS", 300, minimum=0),
        mission_budget_retries=_int_env("FRIDAY_MISSION_BUDGET_RETRIES", 30, minimum=0),
        mission_deadline_hours=_int_env("FRIDAY_MISSION_DEADLINE_HOURS", 48, minimum=0),
        workers_enabled=_bool_env("FRIDAY_WORKERS_ENABLED", True),
        reminders_enabled=_bool_env("FRIDAY_REMINDERS_ENABLED", True),
        reminders_lead_days=_int_env("FRIDAY_REMINDERS_LEAD_DAYS", 1, minimum=0),
        reminders_poll_interval_sec=_int_env("FRIDAY_REMINDERS_POLL_INTERVAL_SEC", 900, minimum=30),
        monitors_enabled=_bool_env("FRIDAY_MONITORS_ENABLED", True),
        monitors_poll_interval_sec=_int_env("FRIDAY_MONITORS_POLL_INTERVAL_SEC", 900, minimum=60),
        reflection_enabled=_bool_env("FRIDAY_REFLECTION_ENABLED", True),
        reflection_interval_sec=_int_env("FRIDAY_REFLECTION_INTERVAL_SEC", 86400, minimum=300),
        reflection_min_knowledge=_int_env("FRIDAY_REFLECTION_MIN_KNOWLEDGE", 3, minimum=0),
        chronicle_enabled=_bool_env("FRIDAY_CHRONICLE_ENABLED", True),
        chronicle_interval_sec=_int_env("FRIDAY_CHRONICLE_INTERVAL_SEC", 86400, minimum=300),
        sentinel_enabled=_bool_env("FRIDAY_SENTINEL_ENABLED", True),
        # Пятнадцать минут вместо часа — решение владельца 2026-08-03 после
        # живого отказа: часовой ПОЛНЫЙ диагностический обход мог не застать его
        # вовсе. Быстрая однотокенная проверка теперь живёт на отдельной частоте
        # ниже и не превращает тяжёлый обход в ежеминутный.
        sentinel_interval_sec=_int_env("FRIDAY_SENTINEL_INTERVAL_SEC", 900, minimum=60),
        sentinel_check_llm=_bool_env("FRIDAY_SENTINEL_CHECK_LLM", True),
        # The generation probe itself has a 25-second socket deadline and a
        # 30-second coroutine deadline.  Capping this interval at 60 seconds
        # preserves the watchdog's product contract:
        # the worker has a 35-second total budget (including alert enqueue), so
        # an outage beginning just after a healthy tick is handled in at most
        # 60 + 35 = 95 seconds, with headroom for the outbound queue to drain.
        # Full diagnostics remain on their separate 15-minute cadence above.
        sentinel_generation_interval_sec=min(
            60,
            _int_env("FRIDAY_SENTINEL_GENERATION_INTERVAL_SEC", 60, minimum=30),
        ),
        profile_in_context=_bool_env("FRIDAY_PROFILE_IN_CONTEXT", True),
        quiet_hours_start=_int_env("FRIDAY_QUIET_HOURS_START", 22, minimum=0),
        quiet_hours_end=_int_env("FRIDAY_QUIET_HOURS_END", 8, minimum=0),
        brave_search_api_key=env("FRIDAY_BRAVE_SEARCH_API_KEY", ""),
        tavily_api_key=env("FRIDAY_TAVILY_API_KEY", ""),
        serper_api_key=env("FRIDAY_SERPER_API_KEY", ""),
        yandex_search_api_key=env("FRIDAY_YANDEX_SEARCH_API_KEY", ""),
        # SEARCH_TYPE_RU / _TR / _COM — какую языковую выдачу спрашивать.
        yandex_search_type=env("FRIDAY_YANDEX_SEARCH_TYPE", "SEARCH_TYPE_RU"),
        # Время в базе хранится в UTC, а человек спрашивает в своём: «что было
        # 26 июля в 15 часов» — это пятнадцать по его часам. Пустое значение
        # означает «часовой пояс машины», что для личного экземпляра и есть
        # часовой пояс владельца.
        local_timezone=env("FRIDAY_TIMEZONE", ""),
        # Сколько разговоров модель ведёт одновременно.
        #
        # ⚠️ Четыре — это ЗАМЕРЕННЫЙ оптимум, а не осторожность. Семь
        # одновременных вопросов на живом эндпойнте: при 4 слотах всё заняло
        # 38.5 с, при 7 — 57.1 с. Узкое место не в очереди Friday, а в самой
        # модели: больше одновременных запросов делят её ресурсы и замедляют
        # каждый. Настройка оставлена для эндпойнтов помощнее, но поднимать её
        # без замера — вредить.
        llm_foreground_slots=_int_env("FRIDAY_LLM_FOREGROUND_SLOTS", 4, minimum=1),
        web_allow_private_networks=_bool_env("FRIDAY_WEB_ALLOW_PRIVATE_NETWORKS", False),
        web_max_response_bytes=_int_env("FRIDAY_WEB_MAX_RESPONSE_BYTES", 5 * 1024 * 1024, minimum=64 * 1024),
        web_daily_quota=_int_env("FRIDAY_WEB_DAILY_QUOTA", 400, minimum=0),
        web_host_pause_sec=_float_env("FRIDAY_WEB_HOST_PAUSE_SEC", 1.0, minimum=0.0),
        max_upload_bytes=_int_env("FRIDAY_MAX_UPLOAD_BYTES", 50 * 1024 * 1024, minimum=1024),
        max_extracted_text_chars=_int_env("FRIDAY_MAX_EXTRACTED_TEXT_CHARS", 2_000_000, minimum=10_000),
        # Стенное время pypdf после того, как тело уже в памяти. Одна ручка на ОБА
        # пути приёма: измерено на стенде — PDF в 41 КБ (250 страниц, у каждой
        # content stream из 40 000 текстовых операторов) занимает поток на 35 с без
        # бюджета и на 8.3 с с ним. Потолок страниц такой файл не ловит: дорога не
        # каждая страница по отдельности, а разбор одной.
        pdf_parse_budget_sec=_float_env("FRIDAY_PDF_PARSE_BUDGET_SEC", 8.0, minimum=0.5),
        max_archive_entries=_int_env("FRIDAY_MAX_ARCHIVE_ENTRIES", 500, minimum=1),
        max_archive_uncompressed_bytes=_int_env(
            "FRIDAY_MAX_ARCHIVE_UNCOMPRESSED_BYTES", 250 * 1024 * 1024, minimum=1024
        ),
        code_execution_enabled=_bool_env("FRIDAY_CODE_EXECUTION_ENABLED", False),
        code_execution_timeout_sec=_int_env("FRIDAY_CODE_EXECUTION_TIMEOUT_SEC", 15, minimum=1),
        code_execution_max_output_bytes=_int_env(
            "FRIDAY_CODE_EXECUTION_MAX_OUTPUT_BYTES", 64 * 1024, minimum=1024
        ),
        mcp_enabled=_bool_env("FRIDAY_MCP_ENABLED", False),
        # Deliberately do not resolve these paths: resolving would erase the fact
        # that an operator configured a symlink. Validation and the MCP server
        # reject symlink components instead of silently accepting their targets.
        mcp_workspace_inbox_dir=Path(
            env("FRIDAY_MCP_WORKSPACE_INBOX_DIR", "").strip() or home / "mcp-exchange" / "inbox"
        )
        .expanduser()
        .absolute(),
        mcp_workspace_outbox_dir=Path(
            env("FRIDAY_MCP_WORKSPACE_OUTBOX_DIR", "").strip() or home / "mcp-exchange" / "outbox"
        )
        .expanduser()
        .absolute(),
        mcp_startup_timeout_sec=_float_env("FRIDAY_MCP_STARTUP_TIMEOUT_SEC", 15.0, minimum=1.0),
        mcp_call_timeout_sec=_float_env("FRIDAY_MCP_CALL_TIMEOUT_SEC", 20.0, minimum=1.0),
        mcp_result_chars=min(
            7_000,
            _int_env("FRIDAY_MCP_RESULT_CHARS", 7_000, minimum=1_000),
        ),
        obsidian_enabled=_bool_env("FRIDAY_OBSIDIAN_ENABLED", False),
        obsidian_root=Path(env("FRIDAY_OBSIDIAN_ROOT", "").strip() or home / "data" / "obsidian")
        .expanduser()
        .absolute(),
        obsidian_vault_name=env("FRIDAY_OBSIDIAN_VAULT_NAME", "Friday").strip(),
        obsidian_syncthing_binary=env("FRIDAY_SYNCTHING_BINARY", "/usr/local/bin/syncthing").strip(),
        obsidian_syncthing_min_version=env("FRIDAY_SYNCTHING_MIN_VERSION", "2.1.3").strip(),
        obsidian_syncthing_max_version=env("FRIDAY_SYNCTHING_MAX_VERSION", "2.2.0").strip(),
        obsidian_pairing_ttl_sec=min(
            3600,
            _int_env("FRIDAY_OBSIDIAN_PAIRING_TTL_SEC", 900, minimum=300),
        ),
        obsidian_max_profiles=min(
            512,
            _int_env("FRIDAY_OBSIDIAN_MAX_PROFILES", 64, minimum=1),
        ),
        obsidian_transport_mode=_fail_closed_choice_env(
            "FRIDAY_OBSIDIAN_TRANSPORT_MODE", "discovery_relay", ("discovery_relay",)
        ),
        obsidian_public_base_url=env("FRIDAY_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        obsidian_reconcile_interval_sec=min(
            60.0,
            _float_env("FRIDAY_OBSIDIAN_RECONCILE_INTERVAL_SEC", 10.0, minimum=2.0),
        ),
        obsidian_rest_timeout_sec=min(
            30.0,
            _float_env("FRIDAY_OBSIDIAN_REST_TIMEOUT_SEC", 5.0, minimum=1.0),
        ),
        obsidian_public_setup_rate_limit_per_minute=min(
            120,
            _int_env("FRIDAY_OBSIDIAN_PUBLIC_SETUP_RATE_LIMIT_PER_MINUTE", 10, minimum=1),
        ),
        router_mode=_fail_closed_choice_env(
            "FRIDAY_ROUTER_MODE",
            "legacy",
            ("legacy", "shadow", "canary", "v12"),
        ),
        router_canary_routes=tuple(
            item.casefold() for item in _list_env("FRIDAY_ROUTER_CANARY_ROUTES", ["file_read"])
        ),
        router_canary_user_ids=tuple(_list_env("FRIDAY_ROUTER_CANARY_USER_IDS")),
        router_plan_timeout_sec=min(
            60.0,
            _float_env("FRIDAY_ROUTER_PLAN_TIMEOUT_SEC", 12.0, minimum=1.0),
        ),
    )


def ensure_runtime_dirs(settings: FridaySettings) -> list[Path]:
    paths = [
        settings.home,
        settings.data_dir,
        settings.files_dir,
        settings.cache_dir,
        settings.log_dir,
        settings.model_root,
        settings.model_dir,
        settings.state_dir,
        settings.backups_dir,
        settings.exports_dir,
    ]
    if settings.mcp_enabled:
        mcp_errors = _mcp_workspace_errors(settings)
        if mcp_errors:
            raise ValueError("; ".join(mcp_errors))
        assert settings.mcp_workspace_inbox_dir is not None
        assert settings.mcp_workspace_outbox_dir is not None
        paths.extend((settings.mcp_workspace_inbox_dir, settings.mcp_workspace_outbox_dir))
    if settings.obsidian_enabled:
        root = settings.obsidian_effective_root
        root_errors = _obsidian_root_errors(settings)
        if root_errors:
            raise ValueError("; ".join(root_errors))
        paths.extend((root, root / "profiles", root / "run", root / "vaults", root / "logs"))
    for path in paths:
        ensure_private_directory(path)
    return paths


def _same_file(left: Path, right: Path) -> bool:
    """Identity comparison that catches aliases without reading either file."""

    try:
        return left.samefile(right)
    except (OSError, ValueError):
        # Missing/inaccessible paths are reported by the caller.  Resolved
        # equality still catches two lexical spellings when samefile is not
        # available on the platform or filesystem.
        try:
            return left.resolve(strict=False) == right.resolve(strict=False)
        except (OSError, RuntimeError):
            return False


def _path_is_within(path: Path, directory: Path) -> bool:
    """Lexical containment for already-absolute configured paths."""

    try:
        Path(path).absolute().relative_to(Path(directory).absolute())
        return True
    except (OSError, ValueError):
        return False


def _path_has_symlink_component(path: Path) -> bool:
    """Reject a configured exchange whose existing ancestry redirects elsewhere."""

    candidate = Path(path).absolute()
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _obsidian_root_errors(settings: FridaySettings) -> list[str]:
    """Validate the directory before any mkdir/chmod mutates host state."""

    root = settings.obsidian_effective_root
    errors: list[str] = []
    if _path_has_symlink_component(root):
        errors.append("FRIDAY_OBSIDIAN_ROOT must not contain a symlink component")
    broad_roots = {
        Path("/"),
        Path("/tmp"),
        Path("/var"),
        Path("/var/tmp"),
        Path.home().absolute(),
        settings.home.absolute(),
        settings.data_dir.absolute(),
        settings.cache_dir.absolute(),
        settings.log_dir.absolute(),
        settings.model_root.absolute(),
        settings.model_dir.absolute(),
    }
    if root.absolute() in broad_roots:
        errors.append("FRIDAY_OBSIDIAN_ROOT must be a dedicated, non-broad directory")
    protected = {
        settings.state_dir,
        settings.files_dir,
        settings.memory_vault_dir,
        settings.backups_dir,
        settings.exports_dir,
        # The documented/default Obsidian root is a dedicated child of
        # ``data_dir``.  Guard the database file itself, not its whole parent,
        # so sibling durable roots remain possible without allowing either
        # tree to contain the database.
        settings.database_path,
        settings.cache_dir,
        settings.log_dir,
        settings.model_root,
        settings.model_dir,
    }
    if any(_path_is_within(root, guarded) or _path_is_within(guarded, root) for guarded in protected):
        errors.append("FRIDAY_OBSIDIAN_ROOT must not overlap state, files, model, cache, log or backup paths")
    try:
        if root.exists():
            if not root.is_dir():
                errors.append("FRIDAY_OBSIDIAN_ROOT must point to a directory")
            else:
                root_stat = root.stat(follow_symlinks=False)
                if root_stat.st_uid != os.geteuid():
                    errors.append("FRIDAY_OBSIDIAN_ROOT must be owned by the Friday process user")
                if root_stat.st_mode & 0o077:
                    errors.append(
                        "FRIDAY_OBSIDIAN_ROOT already exists with non-private permissions; chmod it to 0700 explicitly"
                    )
        else:
            ancestor = root.parent
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            if ancestor.stat(follow_symlinks=False).st_uid != os.geteuid():
                errors.append(
                    "FRIDAY_OBSIDIAN_ROOT must have an existing parent owned by the Friday process user"
                )
    except OSError:
        errors.append("FRIDAY_OBSIDIAN_ROOT cannot be inspected safely")
    return list(dict.fromkeys(errors))


def _mcp_workspace_errors(settings: FridaySettings) -> list[str]:
    errors: list[str] = []
    inbox = settings.mcp_workspace_inbox_dir
    outbox = settings.mcp_workspace_outbox_dir
    if inbox is None or outbox is None:
        return [
            "FRIDAY_MCP_ENABLED requires FRIDAY_MCP_WORKSPACE_INBOX_DIR and FRIDAY_MCP_WORKSPACE_OUTBOX_DIR"
        ]
    workspace_paths = (
        ("FRIDAY_MCP_WORKSPACE_INBOX_DIR", Path(inbox).absolute()),
        ("FRIDAY_MCP_WORKSPACE_OUTBOX_DIR", Path(outbox).absolute()),
    )
    for label, candidate in workspace_paths:
        if any(component in {".", ".."} for component in candidate.parts[1:]):
            errors.append(f"{label} must not contain dot path segments")
        if _path_has_symlink_component(candidate):
            errors.append(f"{label} must not contain a symlink component")
        try:
            exists = candidate.exists()
            is_directory = candidate.is_dir()
        except OSError:
            exists = True
            is_directory = False
        if exists and not is_directory:
            errors.append(f"{label} must point to a directory")
    if _same_file(inbox, outbox) or _path_is_within(inbox, outbox) or _path_is_within(outbox, inbox):
        errors.append("MCP workspace inbox and outbox must be separate, non-nested directories")
    protected = {
        settings.data_dir,
        settings.cache_dir,
        settings.log_dir,
        settings.model_root,
        settings.state_dir,
        settings.files_dir,
        settings.memory_vault_dir,
        settings.backups_dir,
        settings.exports_dir,
        settings.database_path.parent,
    }
    for label, candidate in workspace_paths:
        if any(
            _path_is_within(candidate, guarded) or _path_is_within(guarded, candidate)
            for guarded in protected
        ):
            errors.append(f"{label} must not overlap Friday data, state, model, cache, log or backup paths")
            break
    if not 1.0 <= settings.mcp_startup_timeout_sec <= 120.0:
        errors.append("FRIDAY_MCP_STARTUP_TIMEOUT_SEC must be between 1 and 120")
    if not 1.0 <= settings.mcp_call_timeout_sec <= 300.0:
        errors.append("FRIDAY_MCP_CALL_TIMEOUT_SEC must be between 1 and 300")
    if not 1_000 <= settings.mcp_result_chars <= 7_000:
        errors.append("FRIDAY_MCP_RESULT_CHARS must be between 1000 and 7000")
    return errors


def validate_settings(settings: FridaySettings, *, production: bool = False) -> list[str]:
    """Return actionable configuration problems; raise only at the caller's boundary."""
    errors: list[str] = []
    warnings: list[str] = []
    require_token = settings.api_require_token_on_loopback or not settings.is_loopback_bind
    if require_token and len(settings.api_token) < 32:
        errors.append("FRIDAY_API_TOKEN must contain at least 32 characters")
    elif settings.api_token and len(settings.api_token) < 32:
        warnings.append("FRIDAY_API_TOKEN is shorter than 32 characters")
    if settings.telegram_bridge_secret and len(settings.telegram_bridge_secret) < 32:
        errors.append("FRIDAY_TELEGRAM_BRIDGE_SECRET must contain at least 32 characters")
    if settings.engineer_mode_enabled:
        bubblewrap = Path("/usr/bin/bwrap")
        try:
            bubblewrap_stat = bubblewrap.stat()
        except OSError:
            bubblewrap_stat = None
        if platform.system() != "Linux":
            errors.append("FRIDAY_ENGINEER_MODE_ENABLED requires the Linux sandbox profile")
        elif (
            bubblewrap_stat is None
            or not stat.S_ISREG(bubblewrap_stat.st_mode)
            or bubblewrap_stat.st_uid != 0
            or bubblewrap_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not os.access(bubblewrap, os.X_OK)
        ):
            errors.append("FRIDAY_ENGINEER_MODE_ENABLED requires trusted executable /usr/bin/bwrap")
    host_subfeatures = (
        settings.host_package_install_enabled
        or settings.host_desktop_control_enabled
        or settings.host_one_shot_exec_enabled
        or settings.host_public_network_enabled
    )
    if host_subfeatures and not settings.host_control_enabled:
        errors.append("Host-control subfeatures require FRIDAY_HOST_CONTROL_ENABLED=1")
    if settings.host_desktop_control_enabled:
        errors.append(
            "FRIDAY_HOST_DESKTOP_CONTROL_ENABLED is reserved and unsupported in this release; keep it 0"
        )
    if settings.host_one_shot_exec_enabled:
        errors.append(
            "FRIDAY_HOST_ONE_SHOT_EXEC_ENABLED is reserved and unsupported in this release; keep it 0"
        )
    if settings.host_control_enabled:
        if platform.system() != "Linux":
            errors.append("FRIDAY_HOST_CONTROL_ENABLED requires Linux")
        if not settings.host_agent_socket.is_absolute():
            errors.append("FRIDAY_HOST_AGENT_SOCKET must be an absolute Unix-socket path")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", settings.host_agent_id):
            errors.append("FRIDAY_HOST_AGENT_ID must be a stable 1-64 character identifier")
        try:
            host_job_root = settings.host_job_root.resolve(strict=True)
        except (OSError, RuntimeError):
            errors.append("FRIDAY_HOST_JOB_ROOT must be a pre-created canonical directory")
        else:
            if (
                not settings.host_job_root.is_absolute()
                or host_job_root != settings.host_job_root
                or _path_has_symlink_component(settings.host_job_root)
                or not host_job_root.is_dir()
            ):
                errors.append("FRIDAY_HOST_JOB_ROOT must be a pre-created canonical directory")
            else:
                host_job_stat = host_job_root.stat()
                if host_job_stat.st_uid != os.geteuid() or host_job_stat.st_mode & (
                    stat.S_IRWXG | stat.S_IRWXO
                ):
                    errors.append(
                        "FRIDAY_HOST_JOB_ROOT must be private and owned by the backend service user"
                    )
        if not 1 <= settings.host_action_max_concurrency <= 8:
            errors.append("FRIDAY_HOST_ACTION_MAX_CONCURRENCY must be between 1 and 8")
        if not 5.0 <= settings.host_action_default_timeout_sec <= 3_600.0:
            errors.append("FRIDAY_HOST_ACTION_DEFAULT_TIMEOUT_SEC must be between 5 and 3600")
        if not 1_024 <= settings.host_action_max_output_bytes <= 64 * 1024 * 1024:
            errors.append("FRIDAY_HOST_ACTION_MAX_OUTPUT_BYTES must be between 1024 and 67108864")
        key_fd = -1
        try:
            key_lstat = settings.host_agent_key_file.lstat()
            if stat.S_ISLNK(key_lstat.st_mode):
                raise OSError("key path is a symlink")
            key_fd = os.open(
                settings.host_agent_key_file,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            key_stat = os.fstat(key_fd)
            key_bytes = os.read(key_fd, 65)
        except OSError:
            key_stat = None
            key_bytes = b""
        finally:
            if key_fd >= 0:
                os.close(key_fd)
        if (
            key_stat is None
            or not stat.S_ISREG(key_stat.st_mode)
            or key_stat.st_uid not in {0, os.geteuid()}
            or key_stat.st_nlink != 1
            or key_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or not 32 <= len(key_bytes) <= 64
        ):
            errors.append(
                "FRIDAY_HOST_AGENT_KEY_FILE must be a private, non-symlink 32-64 byte file "
                "owned by the service user or root"
            )
        if settings.host_package_install_enabled or settings.host_public_network_enabled:
            try:
                from friday_package_broker.approval import load_backend_approval_signing_key

                load_backend_approval_signing_key(settings.host_approval_signing_key_file)
            except (OSError, ValueError):
                errors.append(
                    "FRIDAY_HOST_APPROVAL_SIGNING_KEY_FILE must be an exact private, "
                    "non-symlink 32-byte Ed25519 seed available only to a non-dumpable backend signer"
                )
        if len(settings.host_allowed_cidrs) > 32:
            errors.append("FRIDAY_HOST_ALLOWED_CIDRS accepts at most 32 exact networks")
        seen_cidrs: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw_cidr in settings.host_allowed_cidrs:
            try:
                network = ipaddress.ip_network(raw_cidr, strict=True)
            except ValueError:
                errors.append(f"FRIDAY_HOST_ALLOWED_CIDRS contains invalid exact CIDR: {raw_cidr!r}")
                continue
            if network.prefixlen == 0:
                errors.append("FRIDAY_HOST_ALLOWED_CIDRS cannot authorize an all-addresses network")
            if network.is_global and not settings.host_public_network_enabled:
                errors.append(f"public host network {network} requires FRIDAY_HOST_PUBLIC_NETWORK_ENABLED=1")
            if any(
                network.version == previous.version and network.overlaps(previous) for previous in seen_cidrs
            ):
                errors.append(f"FRIDAY_HOST_ALLOWED_CIDRS contains overlapping scope: {network}")
            seen_cidrs.append(network)
        if len(settings.host_allowed_path_roots) > 8:
            errors.append("FRIDAY_HOST_ALLOWED_PATH_ROOTS accepts at most 8 roots")
        forbidden_roots = {
            Path("/"),
            Path("/home"),
            Path("/etc"),
            Path("/usr"),
            Path("/var"),
            Path("/run"),
            Path("/proc"),
            Path("/sys"),
            Path("/dev"),
        }
        sensitive_roots = (
            settings.home,
            settings.data_dir,
            settings.state_dir,
            settings.files_dir,
            settings.backups_dir,
            settings.exports_dir,
            settings.log_dir,
            settings.cache_dir,
            settings.database_path.parent,
        )
        admitted_roots: list[Path] = []
        for configured_root in settings.host_allowed_path_roots:
            root = Path(configured_root)
            try:
                canonical_root = root.resolve(strict=True)
            except (OSError, RuntimeError):
                errors.append(f"host allowed path root does not exist: {root}")
                continue
            if (
                not root.is_absolute()
                or canonical_root != root
                or _path_has_symlink_component(root)
                or not canonical_root.is_dir()
            ):
                errors.append(f"host allowed path root must be an existing canonical directory: {root}")
                continue
            if canonical_root in forbidden_roots or any(
                _path_is_within(canonical_root, sensitive) or _path_is_within(sensitive, canonical_root)
                for sensitive in sensitive_roots
            ):
                errors.append(f"host allowed path root is too broad or contains Friday state: {root}")
                continue
            if any(
                _path_is_within(canonical_root, previous) or _path_is_within(previous, canonical_root)
                for previous in admitted_roots
            ):
                errors.append(f"host allowed path roots overlap: {root}")
                continue
            admitted_roots.append(canonical_root)
    if settings.secondary_llm_enabled:
        from friday.secondary_brain.profiles import get_secondary_runtime_admission

        try:
            secondary_url = urlparse(settings.secondary_llm_base_url)
            secondary_host = str(secondary_url.hostname or "")
        except ValueError:
            secondary_url = None
            secondary_host = ""
        secondary_is_loopback = secondary_host == "localhost"
        secondary_is_private_ip = False
        try:
            secondary_address = ipaddress.ip_address(secondary_host)
        except ValueError:
            pass
        else:
            secondary_is_loopback = secondary_address.is_loopback
            secondary_is_private_ip = secondary_address.is_private and not secondary_address.is_loopback
        secondary_admission = get_secondary_runtime_admission(
            settings.secondary_llm_profile,
            mode=settings.secondary_llm_mode,
        )
        secondary_profile = secondary_admission.profile if secondary_admission is not None else None
        missing_secondary = [
            name
            for name, present in (
                ("FRIDAY_SECONDARY_LLM_BASE_URL", bool(settings.secondary_llm_base_url)),
                ("FRIDAY_SECONDARY_LLM_MODEL", bool(settings.secondary_llm_model)),
                (
                    "FRIDAY_SECONDARY_LLM_API_KEY",
                    bool(re.fullmatch(r"[0-9a-f]{64}", settings.secondary_llm_api_key)),
                ),
                (
                    "FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS",
                    settings.secondary_llm_max_context_tokens > 0,
                ),
                (
                    "FRIDAY_SECONDARY_LLM_CA_FILE",
                    not secondary_is_private_ip or bool(settings.secondary_llm_ca_file),
                ),
                (
                    "FRIDAY_SECONDARY_LLM_PROFILE",
                    not secondary_is_private_ip or secondary_profile is not None,
                ),
            )
            if not present
        ]
        if settings.secondary_llm_mode == "disabled":
            warnings.append(
                "FRIDAY_SECONDARY_LLM_ENABLED is on but mode is disabled; "
                "the optional endpoint will remain inert"
            )
        elif missing_secondary:
            warnings.append(
                "optional secondary endpoint is incomplete and will remain unavailable: "
                + ", ".join(missing_secondary)
            )
        if secondary_url is None:
            warnings.append("optional secondary endpoint URL is invalid and will remain unavailable")
        elif not secondary_is_loopback and not secondary_is_private_ip:
            warnings.append(
                "optional secondary endpoint must use a numeric private LAN address and will "
                "remain unavailable"
            )
        elif secondary_url.scheme == "http" and not secondary_is_loopback:
            warnings.append(
                "optional secondary endpoint uses unsafe non-loopback HTTP and will remain unavailable"
            )
        if settings.secondary_llm_ca_file and not Path(settings.secondary_llm_ca_file).is_file():
            warnings.append(
                "FRIDAY_SECONDARY_LLM_CA_FILE is unavailable; the optional endpoint "
                "will fail soft without affecting primary service"
            )
        if settings.secondary_llm_max_concurrency != 1:
            warnings.append(
                "optional secondary concurrency is not certified as exactly one and will remain unavailable"
            )
        if (
            secondary_is_private_ip
            and secondary_profile is not None
            and not settings.secondary_llm_configured
        ):
            warnings.append(
                "optional secondary settings do not match the code-owned runtime profile and will remain unavailable"
            )
        if (
            settings.secondary_llm_base_url.rstrip("/") == settings.llm_base_url.rstrip("/")
            or settings.secondary_llm_model == settings.llm_model
        ):
            warnings.append(
                "optional secondary endpoint/model is not independent from primary and will remain unavailable"
            )
        if settings.secondary_llm_cooldown_sec <= 0.0:
            warnings.append("optional secondary cooldown must be positive and will remain unavailable")
        if not (
            0.0
            < settings.secondary_llm_connect_timeout_sec
            <= settings.secondary_llm_read_timeout_sec
            <= settings.secondary_llm_call_budget_sec
            < settings.llm_timeout_sec
            and settings.secondary_llm_call_budget_sec <= 30.0
            and 0.0 < settings.secondary_llm_admission_timeout_sec <= 0.25
        ):
            warnings.append("optional secondary timeout relationship is unsafe and will remain unavailable")
    if (
        settings.api_token
        and settings.telegram_bridge_secret
        and settings.api_token == settings.telegram_bridge_secret
    ):
        errors.append("API token and Telegram bridge secret must be different credentials")
    if not 1 <= settings.api_port <= 65535:
        errors.append("FRIDAY_API_PORT must be between 1 and 65535")
    if settings.trust_proxy_headers and not settings.trusted_proxy_networks:
        errors.append("FRIDAY_TRUSTED_PROXY_NETWORKS is required when proxy headers are trusted")
    for proxy_network in settings.trusted_proxy_networks:
        if proxy_network == "*":
            errors.append("Wildcard trusted proxy networks are not allowed")
            continue
        try:
            parsed_network = ipaddress.ip_network(proxy_network, strict=False)
        except ValueError:
            errors.append(f"Invalid trusted proxy network: {proxy_network}")
            continue
        if parsed_network.prefixlen == 0:
            errors.append(f"Unrestricted trusted proxy network is not allowed: {proxy_network}")
    if "*" in settings.cors_origins:
        errors.append("Wildcard CORS is not allowed for the authenticated Admin API")
    for origin in settings.cors_origins:
        parsed = urlparse(origin)
        valid_origin = bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.params
            and not parsed.query
            and not parsed.fragment
        )
        try:
            _ = parsed.port
        except ValueError:
            valid_origin = False
        if not valid_origin:
            errors.append(f"Invalid CORS origin: {origin}")
    if settings.telegram_bridge_secret and not settings.telegram_effective_allowed_chat_ids:
        # A configured Telegram bridge with no allowlist would accept any chat.
        # Deny-by-default requires an explicit allowlist or owner chat; treat this
        # as fatal in production and advisory on a loopback dev bind.
        message = (
            "FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS or FRIDAY_TELEGRAM_OWNER_CHAT_IDS "
            "must be set when the Telegram bridge is configured"
        )
        if production:
            errors.append(message)
        else:
            warnings.append(message)
    if settings.telegram_proxy:
        scheme = settings.telegram_proxy.split("://", 1)[0].lower()
        if scheme in {"socks4", "socks5", "socks5h"}:
            # httpx needs the optional `socksio` package for SOCKS and raises a bare
            # ImportError deep inside the first request otherwise. Friday ships no
            # mandatory dependency for this, and the usual local proxy accepts HTTP
            # CONNECT on the same port, so say that here instead of failing later.
            errors.append(
                f"FRIDAY_TELEGRAM_PROXY uses {scheme}://, which needs the optional "
                "'socksio' package; use http:// instead (a mixed SOCKS/HTTP proxy "
                "accepts HTTP CONNECT on the same port)"
            )
        elif scheme not in {"http", "https"}:
            errors.append("FRIDAY_TELEGRAM_PROXY must be an http:// or https:// URL")
    if settings.embeddings_enabled:
        if not settings.embeddings_model.strip():
            # Not a preference — a contradiction. `EmbeddingBackend.remote_enabled`
            # requires the model name, so this configuration turns semantic search
            # into a complete no-op while every indicator reports it working:
            # `public_dict` published `enabled: true`, and `model-check` probed with
            # `embeddings_model or llm_model`, so it hit a real endpoint with the
            # CHAT model and reported green dimensions. Both shipped templates leave
            # the key empty, so this is the state an operator lands in by following
            # the instructions.
            errors.append(
                "FRIDAY_EMBEDDINGS_ENABLED is on but FRIDAY_EMBEDDINGS_MODEL is empty: "
                "dense recall would be silently disabled — set the model or turn embeddings off"
            )
        try:
            import numpy  # noqa: F401
        except ImportError:
            # Not an error: the pure-Python fallback produces IDENTICAL decisions, which
            # is why numpy stays an optional extra. But with embeddings on, dense recall
            # scores the corpus on EVERY query, and that scan is the difference between
            # milliseconds and seconds once the corpus is real. Silence here would look
            # like Friday being slow rather than a missing extra.
            warnings.append(
                "semantic search is enabled without numpy: dense recall falls back to "
                "pure Python and gets slow as the corpus grows — install 'jericho[vectors]'"
            )
    if bool(settings.ssl_certfile) != bool(settings.ssl_keyfile):
        errors.append(
            "FRIDAY_SSL_CERTFILE and FRIDAY_SSL_KEYFILE must be set together — half a TLS pair cannot serve"
        )
    elif settings.ssl_certfile:
        cert_path = Path(settings.ssl_certfile)
        key_path = Path(settings.ssl_keyfile)
        for label, candidate in (
            ("FRIDAY_SSL_CERTFILE", settings.ssl_certfile),
            ("FRIDAY_SSL_KEYFILE", settings.ssl_keyfile),
        ):
            if not Path(candidate).is_file():
                errors.append(f"{label} points to a missing file: {candidate}")
        if cert_path.is_file() and key_path.is_file() and _same_file(cert_path, key_path):
            errors.append(
                "FRIDAY_SSL_CERTFILE and FRIDAY_SSL_KEYFILE must be different files; "
                "local clients may trust the public certificate but must never read the private key"
            )
    elif not settings.is_loopback_bind and not settings.trust_proxy_headers:
        # A warning, not an error: the live deployment binds 0.0.0.0 behind NAT
        # today, and an error here would refuse to start it. But the fact stands —
        # every request from outside carries the owner token and the archive's
        # content in cleartext, and one passive interception is full access.
        # Текст попадает В ПАНЕЛЬ владельца как есть (диагностика оборачивает его
        # в русский заголовок «Проверьте конфигурацию» и печатает `detail` без
        # перевода), поэтому он написан по-русски: предупреждение, которое человек
        # не читает, предупреждением не является.
        warnings.append(
            "API слушает не только localhost, а TLS не настроен: токен владельца и "
            "всё содержимое базы идут по сети открытым текстом. Задайте "
            "FRIDAY_SSL_CERTFILE и FRIDAY_SSL_KEYFILE (хватит самоподписанной пары) "
            "или поставьте перед ним TLS-прокси"
        )
    if settings.backend_ca_file and not Path(settings.backend_ca_file).is_file():
        errors.append(f"FRIDAY_BACKEND_CA_FILE points to a missing file: {settings.backend_ca_file}")
    elif settings.backend_ca_file and settings.ssl_keyfile:
        ca_path = Path(settings.backend_ca_file)
        key_path = Path(settings.ssl_keyfile)
        if ca_path.is_file() and key_path.is_file() and _same_file(ca_path, key_path):
            errors.append(
                "FRIDAY_BACKEND_CA_FILE must not point to FRIDAY_SSL_KEYFILE; "
                "bridge and diagnostics may read only public trust material"
            )
    if settings.rerank_top > 0 and not (settings.rerank_base_url.strip() and settings.rerank_model.strip()):
        # Same contradiction class as embeddings-without-model: `RerankBackend.enabled`
        # requires both, so this knob combination turns reranking AND the confidence
        # cut-off built on it into a silent no-op while the setting says it is on.
        errors.append(
            "FRIDAY_RERANK_TOP is set but FRIDAY_RERANK_BASE_URL/FRIDAY_RERANK_MODEL "
            "are empty: reranking and the confidence cut-off would silently never run — "
            "set both or set FRIDAY_RERANK_TOP=0"
        )
    if settings.mcp_enabled:
        errors.extend(_mcp_workspace_errors(settings))
    if settings.obsidian_enabled:
        vault_name = settings.obsidian_vault_name
        if (
            not vault_name
            or vault_name != unicodedata.normalize("NFC", vault_name).strip()
            or len(vault_name) > 100
            or vault_name in {".", ".."}
            or any(character in "/\\\r\n\x00" for character in vault_name)
            or any(ord(character) < 32 for character in vault_name)
        ):
            errors.append("FRIDAY_OBSIDIAN_VAULT_NAME must be one safe NFC directory segment")
        binary = Path(settings.obsidian_syncthing_binary)
        if not binary.is_absolute() or binary.is_symlink() or not binary.is_file():
            errors.append("FRIDAY_SYNCTHING_BINARY must be an existing absolute file")
        elif not os.access(binary, os.X_OK):
            errors.append("FRIDAY_SYNCTHING_BINARY must be executable")
        errors.extend(_obsidian_root_errors(settings))
        if settings.obsidian_transport_mode != "discovery_relay":
            errors.append("FRIDAY_OBSIDIAN_TRANSPORT_MODE supports only discovery_relay in this release")
        public_url = urlparse(settings.obsidian_public_base_url)
        if (
            public_url.scheme != "https"
            or not public_url.hostname
            or public_url.username is not None
            or public_url.password is not None
            or public_url.query
            or public_url.fragment
            or public_url.path not in {"", "/"}
        ):
            errors.append("FRIDAY_PUBLIC_BASE_URL must use HTTPS when Obsidian integration is enabled")
        version_pattern = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
        minimum = settings.obsidian_syncthing_min_version
        maximum = settings.obsidian_syncthing_max_version
        if not version_pattern.fullmatch(minimum) or not version_pattern.fullmatch(maximum):
            errors.append("FRIDAY_SYNCTHING_MIN_VERSION/MAX_VERSION must be numeric semantic versions")
        elif tuple(map(int, minimum.split("."))) >= tuple(map(int, maximum.split("."))):
            errors.append("FRIDAY_SYNCTHING_MIN_VERSION must be lower than MAX_VERSION")
    # Открытая регистрация впускает НЕЗНАКОМОГО человека — того, кого владелец не
    # называл ни по имени, ни по номеру чата. Что этот человек получит, решают две
    # соседние настройки, и оба сочетания отдают ему архив целиком. Цена ошибки
    # несимметрична: ложный отказ стоит одной правки `.env` (внести чат в список
    # разрешённых), пропуск стоит всего архива — и узнаётся позже всех. Поэтому
    # ошибка, а не предупреждение: предупреждение здесь — строка в журнале в
    # момент, когда архив уже открыт.
    #
    # Текст по-русски по той же причине, что у предупреждения про TLS ниже: это
    # решение владельца о доступе, и оно попадает в его панель как есть.
    #
    # `FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS=1` — подпись владельца под этим
    # сочетанием. С ней оно перестаёт быть ошибкой и остаётся ПРЕДУПРЕЖДЕНИЕМ: то
    # есть случайно, переключением одного флага, в него не попасть, а сознательно
    # — можно, и система при этом не молчит. Валидатор, роняющий чужую
    # работающую систему из-за несогласия с её хозяином, — не защита, а поломка;
    # это выяснилось не в рассуждении, а на живом экземпляре.
    acknowledged = bool(settings.open_registration_grants_full_access)
    speak = warnings.append if acknowledged else errors.append
    new_preset = str(getattr(settings, "new_account_preset", "") or "").strip()
    if settings.telegram_open_registration and new_preset.casefold() in {"owner", "admin"}:
        speak(
            "FRIDAY_TELEGRAM_OPEN_REGISTRATION=1 вместе с "
            f"FRIDAY_NEW_ACCOUNT_PRESET={new_preset} означает, что ЛЮБОЙ написавший "
            "боту незнакомый человек получает административные права: чужие "
            "документы, ФИО и суммы, чистку базы. Выберите одно — либо узкий пресет "
            "для самозаписи, либо список разрешённых чатов вместо открытой регистрации"
            + (" (сочетание подписано FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS=1)" if acknowledged else "")
        )
    if settings.telegram_open_registration and settings.shared_archive:
        speak(
            "FRIDAY_TELEGRAM_OPEN_REGISTRATION=1 вместе с FRIDAY_SHARED_ARCHIVE=1 "
            "открывает весь общий архив любому написавшему боту: общий архив кладёт "
            "всех в одного арендатора, а право читать знания есть даже у самого "
            "узкого пресета. Впускайте по списку разрешённых чатов или выключите "
            "общий архив"
            + (" (сочетание подписано FRIDAY_OPEN_REGISTRATION_GRANTS_FULL_ACCESS=1)" if acknowledged else "")
        )
    elif (
        settings.telegram_open_registration and new_preset and new_preset.casefold() not in {"owner", "admin"}
    ):
        # Состав произвольного пресета живёт в базе, а не в коде, поэтому здесь
        # его не проверить — но сказать, кому он достаётся, обязаны.
        warnings.append(
            f"FRIDAY_NEW_ACCOUNT_PRESET={new_preset} выдаётся при открытой регистрации "
            "КАЖДОМУ незнакомому человеку, написавшему боту; проверьте состав этого пресета"
        )
    if production and settings.code_execution_enabled:
        warnings.append("Host-side code execution is enabled; use a separate sandbox container")
    return errors + [f"warning: {item}" for item in warnings]


def detect_repeated_token_degeneration(text: str, *, min_repeats: int = 12) -> bool:
    cleaned = " ".join(str(text or "").split()).strip()
    if len(cleaned) < max(24, min_repeats):
        return False
    if re.search(r"(.)\1{" + str(max(8, min_repeats - 1)) + r",}", cleaned):
        return True
    tokens = cleaned.split()
    if len(tokens) >= min_repeats and len(set(tokens[-min_repeats:])) == 1:
        return True
    if len(tokens) >= min_repeats:
        window = tokens[-min_repeats:]
        for cycle in (1, 2, 3, 4):
            if min_repeats % cycle == 0 and window[:cycle] * (min_repeats // cycle) == window:
                return True
    return False
