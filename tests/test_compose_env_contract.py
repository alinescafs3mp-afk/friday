"""What the operator writes in `.env` has to reach the container.

Compose reads `.env` for `${...}` interpolation only. The backend and bridge used
to receive exactly the keys hand-listed in `x-backend-environment`, so every other
setting in the file README tells the operator to copy was silently discarded on
the Docker path — 63 of the 120 keys `load_settings` reads, including
FRIDAY_TELEGRAM_PROXY, which exists precisely for the case where Telegram is only
reachable through a tunnel. `.env` and `.env.local` are both in `.dockerignore`
and the WORKDIR is `/runtime`, so `load_local_env_file` finds nothing inside the
container either.

`env_file:` fixes that wholesale. The two contracts below are what keeps it fixed:
nothing an operator can set may be dropped, and nothing they can set may break the
container's own filesystem layout.
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


@pytest.mark.parametrize("service", CONTAINERISED_SERVICES)
def test_the_env_file_reaches_every_containerised_service(service):
    entries = COMPOSE["services"][service].get("env_file")
    assert entries, f"{service} does not read .env, so most of it is discarded"
    paths = {entry["path"] if isinstance(entry, dict) else entry for entry in entries}
    assert ".env" in paths, f"{service} reads {paths}, not .env"
    # `required: false` so a shell-configured deployment without the file still boots.
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") == ".env":
            assert entry.get("required") is False


@pytest.mark.parametrize("service", CONTAINERISED_SERVICES)
def test_container_owned_paths_cannot_be_overridden_from_the_host(service):
    environment = COMPOSE["services"][service]["environment"]
    missing = sorted(key for key in CONTAINER_OWNED if key not in environment)
    assert not missing, (
        f"{service}: a host .env could redirect these into paths the container does not have: {missing}"
    )


def test_documented_settings_are_ones_something_actually_reads():
    """A key in the template that nothing reads is a promise the product breaks."""
    unread = sorted(_documented_keys() - _keys_read_by_code() - _keys_used_by_compose())
    assert not unread, f".env.example documents settings nothing reads: {unread}"
