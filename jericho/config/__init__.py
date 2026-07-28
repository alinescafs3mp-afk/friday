"""Configuration and runtime profiles for Jericho.

Configuration is deliberately environment-driven: the source tree never needs
secrets, and the whole installation can be moved or backed up as one directory.
"""

from __future__ import annotations

import ipaddress
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"", "0", "false", "no", "off"}


def _choice_env(name: str, default: str, allowed: tuple[str, ...]) -> str:
    """A setting whose values are a vocabulary, not a spectrum.

    Raises on an unknown value rather than falling back to the default: a typo in
    a policy name would otherwise silently restore the very behaviour the operator
    was trying to change, and nothing about the running system would say so.
    Same shape as the `JERICHO_PROFILE` check below.
    """
    value = (os.environ.get(name) or default).strip().casefold()
    if value not in allowed:
        raise ValueError(f"Unknown {name}={value!r}. Valid values: {', '.join(sorted(allowed))}")
    return value


def _int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def _float_env(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value) if minimum is not None else value


def _list_env(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name)
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


def local_env_file_path(path: str | Path | None = None) -> Path:
    """The file this process is configured from: explicit, then JERICHO_ENV_FILE, then ./.env.local.

    Public because more than one caller has to know WHICH file that is. The secret
    scanner in particular used to skip anything merely NAMED `.env` or `.env.local`
    anywhere in the tree, so a copy of a live token in some unrelated project's `.env`
    was invisible while the same token in `env.txt` beside it was reported.
    """
    if path is not None:
        return Path(path).expanduser()
    if os.environ.get("JERICHO_ENV_FILE"):
        return Path(os.environ["JERICHO_ENV_FILE"]).expanduser()
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
    explicit = os.environ.get("JERICHO_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if platform.system().casefold() == "windows":
        return Path(r"D:\jericho")
    if Path("/mnt/d").exists():
        return Path("/mnt/d/jericho")
    return Path.home() / ".jericho"


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
    cpu_offload_gb: float
    kv_offloading_gb: int
    vllm_extra_args: VllmExtraArgs = field(default_factory=VllmExtraArgs)
    tokenizer_mode: str = "slow"
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
# `assessed`       — решает классификатор: что он счёл достойным продвижения, то и
#                    продвигается. Так система вела себя всегда.
# `unless_explicit` — прямое продвижение остаётся только у явного намерения
#                    (`/note`, «запомни», `force_knowledge`); всё остальное ждёт
#                    решения человека. Загрузка файла — явное ДЕЙСТВИЕ, но не
#                    высказывание о содержимом, поэтому файлы сюда тоже попадают.
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
        default_recommended=True,
        research_only=False,
        readiness_deadline_sec=900.0,
        certification_reason="Primary Jericho runtime profile.",
        menu_visible=True,
        requires_experimental_opt_in=False,
    )
}


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
        "tokenizer_mode": profile.tokenizer_mode,
        "vllm_image": profile.vllm_image,
        "vision_capable": profile.vision_capable,
        "certification": profile.certification,
        "interactive_certified": profile.interactive_certified,
        "default_recommended": profile.default_recommended,
    }


@dataclass(frozen=True)
class JerichoSettings:
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
    files_dir: Path
    memory_vault_dir: Path
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

    llm_base_url: str
    llm_model: str
    llm_enabled: bool
    llm_timeout_sec: float
    llm_max_tokens: int
    llm_api_key: str
    verify_answers: bool
    verify_min_answer_chars: int

    embeddings_enabled: bool
    embeddings_base_url: str
    embeddings_api_key: str
    embeddings_model: str
    embeddings_index_batch: int
    embeddings_index_interval_sec: float
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
    whisper_download_root: str

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

    api_host: str
    api_port: int
    api_token: str
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

    autonomy_enabled: bool
    operator_full_autonomy: bool
    cognition_enabled: bool
    cognition_interval_sec: int
    cognition_max_tokens: int
    executive_max_active_missions: int
    executive_max_tasks_per_mission: int
    executive_task_tool_budget: int
    executive_tick_interval_sec: int
    workers_enabled: bool

    reminders_enabled: bool
    reminders_lead_days: int
    reminders_poll_interval_sec: int
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
    # Inject the derived user model (people/projects/interests) into the
    # agent's untrusted context payload so answers can be personal.
    profile_in_context: bool
    # Quiet hours (UTC) apply to every proactive organ, not just reminders.
    quiet_hours_start: int
    quiet_hours_end: int

    brave_search_api_key: str
    tavily_api_key: str
    serper_api_key: str
    web_allow_private_networks: bool
    web_max_response_bytes: int

    max_upload_bytes: int
    max_extracted_text_chars: int
    max_archive_entries: int
    max_archive_uncompressed_bytes: int
    code_execution_enabled: bool
    code_execution_timeout_sec: int
    code_execution_max_output_bytes: int

    @property
    def is_loopback_bind(self) -> bool:
        return self.api_host in {"127.0.0.1", "localhost", "::1"}

    @property
    def telegram_effective_allowed_chat_ids(self) -> list[int]:
        # Owner chats are always allowed; the union is the deny-by-default gate.
        # An empty result means no chat is allowed (a configured bridge must set
        # either an allowlist or an owner chat).
        return sorted({*self.telegram_allowed_chat_ids, *self.telegram_owner_chat_ids})

    @property
    def frontend_origin(self) -> str:
        # This only derives a browser origin from bind literals; no socket is opened here.
        host = "127.0.0.1" if self.api_host in {"0.0.0.0", "::"} else self.api_host  # nosec B104
        return f"http://{host}:{self.api_port}"

    def public_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "profile": profile_public_dict(self.profile),
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
                "purge_retention_days": self.purge_retention_days,
                "ingestion_review_policy": self.ingestion_review_policy,
                "backup_mirror_configured": self.backup_mirror_dir is not None,
                "backup_encryption_configured": self.backup_encryption_key_file is not None,
            },
            "graph": {"max_depth": self.graph_max_depth},
            "api": {"host": self.api_host, "port": self.api_port},
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
            },
            "organs": {
                "reminders_enabled": self.reminders_enabled,
                "reminders_lead_days": self.reminders_lead_days,
                "reminders_poll_interval_sec": self.reminders_poll_interval_sec,
                "reflection_enabled": self.reflection_enabled,
                "reflection_interval_sec": self.reflection_interval_sec,
                "reflection_min_knowledge": self.reflection_min_knowledge,
                "chronicle_enabled": self.chronicle_enabled,
                "chronicle_interval_sec": self.chronicle_interval_sec,
                "sentinel_enabled": self.sentinel_enabled,
                "sentinel_interval_sec": self.sentinel_interval_sec,
                "sentinel_check_llm": self.sentinel_check_llm,
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


def load_settings(profile_name: str | None = None) -> JerichoSettings:
    load_local_env_file()
    home = default_home().resolve()
    selected_name = profile_name or os.environ.get("JERICHO_PROFILE", "qwen36-vl")
    profile = PROFILES.get(selected_name)
    if profile is None:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown JERICHO_PROFILE={selected_name!r}. Valid profiles: {valid}")

    data_dir = Path(os.environ.get("JERICHO_DATA_DIR", home / "data")).expanduser().resolve()
    cache_dir = Path(os.environ.get("JERICHO_CACHE_DIR", home / "cache")).expanduser().resolve()
    log_dir = Path(os.environ.get("JERICHO_LOG_DIR", home / "logs")).expanduser().resolve()
    # The requested model location is <project>/models/<model-name>.
    model_root = Path(os.environ.get("JERICHO_MODEL_ROOT", home / "models")).expanduser().resolve()
    state_dir = Path(os.environ.get("JERICHO_STATE_DIR", data_dir / "state")).expanduser().resolve()
    llm_base_url = os.environ.get("JERICHO_LLM_BASE_URL", "http://127.0.0.1:8001/v1").rstrip("/")

    cors_origins = _list_env("JERICHO_CORS_ORIGINS")
    if not cors_origins:
        cors_origins = ["http://127.0.0.1:8000", "http://localhost:8000"]

    return JerichoSettings(
        home=home,
        profile=profile,
        data_dir=data_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        log_max_bytes=_int_env("JERICHO_LOG_MAX_BYTES", 16 * 1024 * 1024, minimum=0),
        log_backups=_int_env("JERICHO_LOG_BACKUPS", 3, minimum=0),
        model_root=model_root,
        model_dir=model_root / profile.model_dir_name,
        state_dir=state_dir,
        database_path=Path(os.environ.get("JERICHO_DATABASE_PATH", state_dir / "jericho.sqlite3"))
        .expanduser()
        .resolve(),
        files_dir=Path(os.environ.get("JERICHO_FILES_DIR", data_dir / "files")).expanduser().resolve(),
        memory_vault_dir=Path(os.environ.get("JERICHO_MEMORY_VAULT_DIR", data_dir / "memory-vault"))
        .expanduser()
        .resolve(),
        backups_dir=Path(os.environ.get("JERICHO_BACKUPS_DIR", data_dir / "backups")).expanduser().resolve(),
        exports_dir=Path(os.environ.get("JERICHO_EXPORTS_DIR", data_dir / "exports")).expanduser().resolve(),
        backup_mirror_dir=(
            Path(os.environ["JERICHO_BACKUP_MIRROR_DIR"]).expanduser().resolve()
            if os.environ.get("JERICHO_BACKUP_MIRROR_DIR", "").strip()
            else None
        ),
        backup_encryption_key_file=(
            Path(os.environ["JERICHO_BACKUP_ENCRYPTION_KEY_FILE"]).expanduser().resolve()
            if os.environ.get("JERICHO_BACKUP_ENCRYPTION_KEY_FILE", "").strip()
            else None
        ),
        llm_base_url=llm_base_url,
        llm_model=os.environ.get("JERICHO_LLM_MODEL", "dispatcher"),
        llm_enabled=_bool_env("JERICHO_LLM_ENABLED", True),
        llm_timeout_sec=_float_env("JERICHO_LLM_TIMEOUT_SEC", 240.0, minimum=1.0),
        llm_max_tokens=_int_env("JERICHO_LLM_MAX_TOKENS", 2048, minimum=64),
        llm_api_key=os.environ.get("JERICHO_LLM_API_KEY", "").strip(),
        verify_answers=_bool_env("JERICHO_VERIFY_ANSWERS", True),
        verify_min_answer_chars=_int_env("JERICHO_VERIFY_MIN_ANSWER_CHARS", 300, minimum=1),
        embeddings_enabled=_bool_env("JERICHO_EMBEDDINGS_ENABLED", False),
        embeddings_base_url=os.environ.get("JERICHO_EMBEDDINGS_BASE_URL", llm_base_url).rstrip("/"),
        # A separate embeddings service may share the LLM's token; default to it.
        # `.get(name, default)` only falls back when the variable is ABSENT, and an
        # env file that writes `JERICHO_EMBEDDINGS_API_KEY=` supplies an empty VALUE —
        # so the intended inheritance silently did not happen and the backend sent no
        # Authorization header at all. Observed as 401 on every indexing request while
        # a single-key check passed, because it resolved the fallback differently.
        embeddings_api_key=(
            os.environ.get("JERICHO_EMBEDDINGS_API_KEY", "").strip()
            or os.environ.get("JERICHO_LLM_API_KEY", "").strip()
        ),
        embeddings_model=os.environ.get("JERICHO_EMBEDDINGS_MODEL", ""),
        embeddings_index_batch=_int_env("JERICHO_EMBEDDINGS_INDEX_BATCH", 64, minimum=1),
        embeddings_index_interval_sec=_float_env("JERICHO_EMBEDDINGS_INDEX_INTERVAL_SEC", 120.0, minimum=5.0),
        embeddings_recall_candidates=_int_env("JERICHO_EMBEDDINGS_RECALL_CANDIDATES", 40, minimum=1),
        embeddings_dense_max_objects=_int_env("JERICHO_EMBEDDINGS_DENSE_MAX_OBJECTS", 5000, minimum=0),
        # ~1200 characters is roughly 300-420 tokens, inside the 512-token window
        # multilingual embedding models are trained on. 0 = chunking off.
        embeddings_chunk_chars=_int_env("JERICHO_EMBEDDINGS_CHUNK_CHARS", 1200, minimum=0),
        # ~17% overlap: any fact shorter than this lands whole in at least one chunk.
        embeddings_chunk_overlap_chars=_int_env("JERICHO_EMBEDDINGS_CHUNK_OVERLAP_CHARS", 200, minimum=0),
        embeddings_chunk_max_per_object=_int_env("JERICHO_EMBEDDINGS_CHUNK_MAX_PER_OBJECT", 64, minimum=1),
        embeddings_chunk_blend=_float_env("JERICHO_EMBEDDINGS_CHUNK_BLEND", 0.25, minimum=0.0),
        embeddings_chunk_scan_multiplier=_int_env("JERICHO_EMBEDDINGS_CHUNK_SCAN_MULTIPLIER", 4, minimum=1),
        embeddings_max_inputs_per_request=_int_env(
            "JERICHO_EMBEDDINGS_MAX_INPUTS_PER_REQUEST", 64, minimum=1
        ),
        # 0.95, not the previous 0.92, which sat INSIDE the measured distribution of
        # non-duplicates (two weekly meeting notes from one template: 0.928; two
        # entries about one apartment: 0.917 and 0.914). Whether a series of minutes
        # got proposed for merging came down to the third decimal. 0.95 catches
        # exactly as many real duplicates on the measured stand — the classes it
        # could reach, 0.888 and below, are unreachable at any safe value — with no
        # false proposal. See `jericho/dedup.py::_MEASURED_NON_DUPLICATE_CEILING`
        # and `tools/dedup_threshold_probe.py`. Raising costs no rescan; lowering
        # triggers one, by design.
        dedup_threshold=_float_env("JERICHO_DEDUP_THRESHOLD", 0.95, minimum=0.5),
        dedup_interval_sec=_float_env("JERICHO_DEDUP_INTERVAL_SEC", 21600, minimum=300),
        dedup_scan_batch=_int_env("JERICHO_DEDUP_SCAN_BATCH", 512, minimum=1),
        dedup_scan_max_seconds=_float_env("JERICHO_DEDUP_SCAN_MAX_SECONDS", 600.0, minimum=1.0),
        eval_enabled=_bool_env("JERICHO_EVAL_ENABLED", True),
        eval_interval_sec=_float_env("JERICHO_EVAL_INTERVAL_SEC", 86400, minimum=300),
        eval_k=_int_env("JERICHO_EVAL_K", 10, minimum=1),
        # Grow the eval gold set from confirmed positive answer feedback (the cited
        # KOs become the expected results for that query). Never overwrites a manual
        # case. Disable to keep the gold set hand-curated.
        eval_mine_from_feedback=_bool_env("JERICHO_EVAL_MINE_FROM_FEEDBACK", True),
        whisper_enabled=_bool_env("JERICHO_WHISPER_ENABLED", False),
        whisper_model=os.environ.get("JERICHO_WHISPER_MODEL", "small"),
        whisper_device=os.environ.get("JERICHO_WHISPER_DEVICE", "cpu"),
        whisper_compute_type=os.environ.get("JERICHO_WHISPER_COMPUTE_TYPE", "int8"),
        whisper_language=os.environ.get("JERICHO_WHISPER_LANGUAGE", ""),
        whisper_max_audio_sec=_float_env("JERICHO_WHISPER_MAX_AUDIO_SEC", 900.0, minimum=0.0),
        whisper_download_root=os.environ.get("JERICHO_WHISPER_DOWNLOAD_ROOT", ""),
        purge_retention_days=_int_env("JERICHO_PURGE_RETENTION_DAYS", 30, minimum=0),
        backup_keep=_int_env("JERICHO_BACKUP_KEEP", 14, minimum=0),
        ingestion_review_policy=_choice_env("JERICHO_INGESTION_REVIEW_POLICY", "assessed", REVIEW_POLICIES),
        graph_max_depth=_int_env("JERICHO_GRAPH_MAX_DEPTH", 2, minimum=1),
        retrieval_pool_max=_int_env("JERICHO_RETRIEVAL_POOL_MAX", 400, minimum=10),
        retrieval_dense_evidence_min=_float_env("JERICHO_RETRIEVAL_DENSE_EVIDENCE_MIN", 0.40, minimum=0.0),
        api_host=os.environ.get("JERICHO_API_HOST", "127.0.0.1"),
        api_port=_int_env("JERICHO_API_PORT", 8000, minimum=1),
        api_token=os.environ.get("JERICHO_API_TOKEN", ""),
        api_require_token_on_loopback=_bool_env("JERICHO_API_REQUIRE_TOKEN_ON_LOOPBACK", True),
        api_user_rate_limit_per_minute=_int_env("JERICHO_API_USER_RATE_LIMIT_PER_MINUTE", 240, minimum=1),
        api_auth_failure_limit_per_minute=_int_env(
            "JERICHO_API_AUTH_FAILURE_LIMIT_PER_MINUTE", 10, minimum=1
        ),
        # Diagnostics/sentinel raise a warning when auth failures over the last 24h
        # reach this count (possible brute-force / leaked-token abuse). The 24h
        # window (vs. the hourly sentinel tick and quiet hours) means a sustained or
        # overnight burst is not aliased away. 0 disables.
        auth_failure_alert_threshold=_int_env("JERICHO_AUTH_FAILURE_ALERT_THRESHOLD", 60, minimum=0),
        trust_proxy_headers=_bool_env("JERICHO_TRUST_PROXY_HEADERS", False),
        trusted_proxy_networks=_list_env("JERICHO_TRUSTED_PROXY_NETWORKS", ["127.0.0.1/32", "::1/128"]),
        cors_origins=cors_origins,
        telegram_bridge_secret=os.environ.get("JERICHO_TELEGRAM_BRIDGE_SECRET", ""),
        telegram_realm_id=os.environ.get("JERICHO_TELEGRAM_REALM_ID", "telegram"),
        telegram_user_rate_limit_per_minute=_int_env(
            "JERICHO_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE", 30, minimum=1
        ),
        telegram_global_rate_limit_per_minute=_int_env(
            "JERICHO_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE", 1200, minimum=1
        ),
        telegram_allowed_chat_ids=_int_list_env("JERICHO_TELEGRAM_ALLOWED_CHAT_IDS"),
        telegram_owner_chat_ids=_int_list_env("JERICHO_TELEGRAM_OWNER_CHAT_IDS"),
        telegram_proxy=os.environ.get("JERICHO_TELEGRAM_PROXY", "").strip(),
        telegram_group_members_full_access=_bool_env("JERICHO_TELEGRAM_GROUP_MEMBERS_FULL_ACCESS", False),
        telegram_signature_max_age_sec=_int_env("JERICHO_TELEGRAM_SIGNATURE_MAX_AGE_SEC", 90, minimum=10),
        autonomy_enabled=_bool_env("JERICHO_AUTONOMY_ENABLED", True),
        operator_full_autonomy=_bool_env("JERICHO_OPERATOR_FULL_AUTONOMY", False),
        cognition_enabled=_bool_env("JERICHO_COGNITION_ENABLED", True),
        cognition_interval_sec=_int_env("JERICHO_COGNITION_INTERVAL_SEC", 300, minimum=30),
        cognition_max_tokens=_int_env("JERICHO_COGNITION_MAX_TOKENS", 512, minimum=64),
        executive_max_active_missions=_int_env("JERICHO_EXECUTIVE_MAX_ACTIVE_MISSIONS", 8, minimum=1),
        executive_max_tasks_per_mission=_int_env("JERICHO_EXECUTIVE_MAX_TASKS_PER_MISSION", 12, minimum=1),
        executive_task_tool_budget=_int_env("JERICHO_EXECUTIVE_TASK_TOOL_BUDGET", 6, minimum=1),
        executive_tick_interval_sec=_int_env("JERICHO_EXECUTIVE_TICK_INTERVAL_SEC", 15, minimum=5),
        workers_enabled=_bool_env("JERICHO_WORKERS_ENABLED", True),
        reminders_enabled=_bool_env("JERICHO_REMINDERS_ENABLED", True),
        reminders_lead_days=_int_env("JERICHO_REMINDERS_LEAD_DAYS", 1, minimum=0),
        reminders_poll_interval_sec=_int_env("JERICHO_REMINDERS_POLL_INTERVAL_SEC", 900, minimum=30),
        reflection_enabled=_bool_env("JERICHO_REFLECTION_ENABLED", True),
        reflection_interval_sec=_int_env("JERICHO_REFLECTION_INTERVAL_SEC", 86400, minimum=300),
        reflection_min_knowledge=_int_env("JERICHO_REFLECTION_MIN_KNOWLEDGE", 3, minimum=0),
        chronicle_enabled=_bool_env("JERICHO_CHRONICLE_ENABLED", True),
        chronicle_interval_sec=_int_env("JERICHO_CHRONICLE_INTERVAL_SEC", 86400, minimum=300),
        sentinel_enabled=_bool_env("JERICHO_SENTINEL_ENABLED", True),
        sentinel_interval_sec=_int_env("JERICHO_SENTINEL_INTERVAL_SEC", 3600, minimum=60),
        sentinel_check_llm=_bool_env("JERICHO_SENTINEL_CHECK_LLM", True),
        profile_in_context=_bool_env("JERICHO_PROFILE_IN_CONTEXT", True),
        quiet_hours_start=_int_env("JERICHO_QUIET_HOURS_START", 22, minimum=0),
        quiet_hours_end=_int_env("JERICHO_QUIET_HOURS_END", 8, minimum=0),
        brave_search_api_key=os.environ.get("JERICHO_BRAVE_SEARCH_API_KEY", ""),
        tavily_api_key=os.environ.get("JERICHO_TAVILY_API_KEY", ""),
        serper_api_key=os.environ.get("JERICHO_SERPER_API_KEY", ""),
        web_allow_private_networks=_bool_env("JERICHO_WEB_ALLOW_PRIVATE_NETWORKS", False),
        web_max_response_bytes=_int_env("JERICHO_WEB_MAX_RESPONSE_BYTES", 5 * 1024 * 1024, minimum=64 * 1024),
        max_upload_bytes=_int_env("JERICHO_MAX_UPLOAD_BYTES", 50 * 1024 * 1024, minimum=1024),
        max_extracted_text_chars=_int_env("JERICHO_MAX_EXTRACTED_TEXT_CHARS", 2_000_000, minimum=10_000),
        max_archive_entries=_int_env("JERICHO_MAX_ARCHIVE_ENTRIES", 500, minimum=1),
        max_archive_uncompressed_bytes=_int_env(
            "JERICHO_MAX_ARCHIVE_UNCOMPRESSED_BYTES", 250 * 1024 * 1024, minimum=1024
        ),
        code_execution_enabled=_bool_env("JERICHO_CODE_EXECUTION_ENABLED", False),
        code_execution_timeout_sec=_int_env("JERICHO_CODE_EXECUTION_TIMEOUT_SEC", 15, minimum=1),
        code_execution_max_output_bytes=_int_env(
            "JERICHO_CODE_EXECUTION_MAX_OUTPUT_BYTES", 64 * 1024, minimum=1024
        ),
    )


def ensure_runtime_dirs(settings: JerichoSettings) -> list[Path]:
    paths = [
        settings.home,
        settings.data_dir,
        settings.files_dir,
        settings.memory_vault_dir,
        settings.cache_dir,
        settings.log_dir,
        settings.model_root,
        settings.model_dir,
        settings.state_dir,
        settings.backups_dir,
        settings.exports_dir,
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return paths


def validate_settings(settings: JerichoSettings, *, production: bool = False) -> list[str]:
    """Return actionable configuration problems; raise only at the caller's boundary."""
    errors: list[str] = []
    warnings: list[str] = []
    require_token = settings.api_require_token_on_loopback or not settings.is_loopback_bind
    if require_token and len(settings.api_token) < 32:
        errors.append("JERICHO_API_TOKEN must contain at least 32 characters")
    elif settings.api_token and len(settings.api_token) < 32:
        warnings.append("JERICHO_API_TOKEN is shorter than 32 characters")
    if settings.telegram_bridge_secret and len(settings.telegram_bridge_secret) < 32:
        errors.append("JERICHO_TELEGRAM_BRIDGE_SECRET must contain at least 32 characters")
    if (
        settings.api_token
        and settings.telegram_bridge_secret
        and settings.api_token == settings.telegram_bridge_secret
    ):
        errors.append("API token and Telegram bridge secret must be different credentials")
    if not 1 <= settings.api_port <= 65535:
        errors.append("JERICHO_API_PORT must be between 1 and 65535")
    if settings.trust_proxy_headers and not settings.trusted_proxy_networks:
        errors.append("JERICHO_TRUSTED_PROXY_NETWORKS is required when proxy headers are trusted")
    for network in settings.trusted_proxy_networks:
        if network == "*":
            errors.append("Wildcard trusted proxy networks are not allowed")
            continue
        try:
            parsed_network = ipaddress.ip_network(network, strict=False)
        except ValueError:
            errors.append(f"Invalid trusted proxy network: {network}")
            continue
        if parsed_network.prefixlen == 0:
            errors.append(f"Unrestricted trusted proxy network is not allowed: {network}")
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
            "JERICHO_TELEGRAM_ALLOWED_CHAT_IDS or JERICHO_TELEGRAM_OWNER_CHAT_IDS "
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
            # ImportError deep inside the first request otherwise. Jericho ships no
            # mandatory dependency for this, and the usual local proxy accepts HTTP
            # CONNECT on the same port, so say that here instead of failing later.
            errors.append(
                f"JERICHO_TELEGRAM_PROXY uses {scheme}://, which needs the optional "
                "'socksio' package; use http:// instead (a mixed SOCKS/HTTP proxy "
                "accepts HTTP CONNECT on the same port)"
            )
        elif scheme not in {"http", "https"}:
            errors.append("JERICHO_TELEGRAM_PROXY must be an http:// or https:// URL")
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
                "JERICHO_EMBEDDINGS_ENABLED is on but JERICHO_EMBEDDINGS_MODEL is empty: "
                "dense recall would be silently disabled — set the model or turn embeddings off"
            )
        try:
            import numpy  # noqa: F401
        except ImportError:
            # Not an error: the pure-Python fallback produces IDENTICAL decisions, which
            # is why numpy stays an optional extra. But with embeddings on, dense recall
            # scores the corpus on EVERY query, and that scan is the difference between
            # milliseconds and seconds once the corpus is real. Silence here would look
            # like Jericho being slow rather than a missing extra.
            warnings.append(
                "semantic search is enabled without numpy: dense recall falls back to "
                "pure Python and gets slow as the corpus grows — install 'jericho[vectors]'"
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
