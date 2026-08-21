"""What the operator writes in `.env` has to reach the container.

Compose reads `.env` for interpolation only. The backend receives the complete
file because it owns all Friday settings. The Telegram bridge is a smaller trust
boundary: it receives an explicit allowlist and its own state volume, never the
backend's plaintext tenant data or unrelated credentials.
"""

from __future__ import annotations

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

# Keys naming a path that exists only inside the container, or that the volume map
# provides. A host `.env` setting one of these would point the container at a path
# that is not there, so `environment:` must pin them — it wins over `env_file:`.
CONTAINER_OWNED = {
    "FRIDAY_HOME",
    "FRIDAY_DATA_DIR",
    "FRIDAY_CACHE_DIR",
    "FRIDAY_LOG_DIR",
    "FRIDAY_MODEL_ROOT",
    "FRIDAY_STATE_DIR",
    "FRIDAY_DATABASE_PATH",
    "FRIDAY_FILES_DIR",
    "FRIDAY_MEMORY_VAULT_DIR",
    "FRIDAY_BACKUPS_DIR",
    "FRIDAY_EXPORTS_DIR",
    "FRIDAY_WHISPER_DOWNLOAD_ROOT",
    "FRIDAY_ENV_FILE",
    "FRIDAY_API_HOST",
    # Base Compose terminates external TLS at a separate proxy. These must stay
    # empty in BOTH services: otherwise env_file can hand a host-only key path to
    # the backend and, worse, expose that same private path to Telegram.
    "FRIDAY_SSL_CERTFILE",
    "FRIDAY_SSL_KEYFILE",
    "FRIDAY_BACKEND_CA_FILE",
}

CONTAINERISED_SERVICES = ("backend", "telegram")


def _keys_read_by_code() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "friday").rglob("*.py"):
        found |= set(re.findall(r'"(FRIDAY_[A-Z0-9_]+)"', path.read_text(encoding="utf-8")))
    return found


def _keys_used_by_compose() -> set[str]:
    """Host-side keys: consumed by `${...}` interpolation, never by the container."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return set(re.findall(r"\$\{(FRIDAY_[A-Z0-9_]+)", text))


def _documented_keys() -> set[str]:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r"^(FRIDAY_[A-Z0-9_]+)=", text, re.M))


def test_the_env_file_reaches_the_backend() -> None:
    entries = COMPOSE["services"]["backend"].get("env_file")
    assert entries, "backend does not read .env, so most settings are discarded"
    paths = {entry["path"] if isinstance(entry, dict) else entry for entry in entries}
    assert ".env" in paths, f"backend reads {paths}, not .env"
    # `required: false` so a shell-configured deployment without the file still boots.
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") == ".env":
            assert entry.get("required") is False


def test_backend_container_owned_paths_cannot_be_overridden_from_the_host() -> None:
    service = "backend"
    environment = COMPOSE["services"][service]["environment"]
    missing = sorted(key for key in CONTAINER_OWNED if key not in environment)
    assert not missing, (
        f"{service}: a host .env could redirect these into paths the container does not have: {missing}"
    )


def test_telegram_has_a_dedicated_state_and_no_backend_data_or_env_file() -> None:
    service = COMPOSE["services"]["telegram"]
    assert not service.get("env_file")
    environment = service["environment"]
    assert environment["FRIDAY_TELEGRAM_INBOX_DB_PATH"].startswith("/runtime/bridge-state/")
    assert environment["FRIDAY_DATA_DIR"].startswith("/runtime/bridge-state/")
    assert "FRIDAY_API_TOKEN" not in environment
    assert "FRIDAY_BRAVE_SEARCH_API_KEY" not in environment
    volumes = service["volumes"]
    rendered = "\n".join(str(volume) for volume in volumes)
    assert "/runtime/bridge-state" in rendered
    assert "/runtime/data" not in rendered
    assert "/runtime/models" not in rendered


def test_documented_settings_are_ones_something_actually_reads():
    """A key in the template that nothing reads is a promise the product breaks."""
    unread = sorted(_documented_keys() - _keys_read_by_code() - _keys_used_by_compose())
    assert not unread, f".env.example documents settings nothing reads: {unread}"


def test_base_compose_keeps_tls_keys_out_of_the_shared_backend_bridge_environment():
    """Native TLS needs a backend-only key mount; base Compose promises no such leak."""
    for service in CONTAINERISED_SERVICES:
        environment = COMPOSE["services"][service]["environment"]
        assert environment["FRIDAY_SSL_CERTFILE"] == ""
        assert environment["FRIDAY_SSL_KEYFILE"] == ""
        assert environment["FRIDAY_BACKEND_CA_FILE"] == ""
        volumes = COMPOSE["services"][service].get("volumes", [])
        assert not any("tls" in str(volume).casefold() for volume in volumes)

    telegram_environment = COMPOSE["services"]["telegram"]["environment"]
    assert telegram_environment["FRIDAY_BACKEND_URL"].startswith("http://backend:")
    healthcheck = " ".join(COMPOSE["services"]["backend"]["healthcheck"]["test"])
    assert "http://127.0.0.1:" in healthcheck
    assert "https://" not in healthcheck


def test_container_bootstrap_keeps_the_obsidian_root_private() -> None:
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "docker" / "Dockerfile.backend").read_text(encoding="utf-8")
    for source in (entrypoint, dockerfile):
        assert "install -d -m 0700" in source
        assert "/runtime/data/obsidian" in source
