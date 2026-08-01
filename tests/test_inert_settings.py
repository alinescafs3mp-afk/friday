"""No setting may exist that nothing reads.

Five did: `telegram_bot_id`, `telegram_session_ttl_seconds`, `telemetry_interval_sec`,
`health_interval_sec` and `default_city` were parsed by `load_settings`, stored on
`FridaySettings`, documented in `.env.example` and forwarded by Compose — and read
by no code at all. `FRIDAY_TELEGRAM_SESSION_TTL_SECONDS` reads as a security
control, so an operator who set it believed Telegram sessions expired. They did not.

They are gone. This test is what stops the next one appearing: a documented knob
that does nothing is worse than a missing one, because the operator believes they
configured something.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

from friday.config import FridaySettings

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_SOURCE = ROOT / "friday" / "config" / "__init__.py"

# `data_dir` and `cache_dir` are consumed INSIDE the config module to derive other
# paths, which is a real use. They promise no behaviour of their own.
DERIVED_INSIDE_CONFIG = {"data_dir", "cache_dir"}


def _fields_no_code_reads() -> set[str]:
    names = {field.name for field in dataclasses.fields(FridaySettings)}
    seen: set[str] = set()
    for path in list((ROOT / "friday").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")):
        if path == CONFIG_SOURCE or path.name == pathlib.Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        seen |= {name for name in names if re.search(rf"\b{name}\b", text)}
    return names - seen - DERIVED_INSIDE_CONFIG


def test_every_setting_is_read_by_something():
    unread = sorted(_fields_no_code_reads())
    assert not unread, (
        f"these settings are parsed and documented but nothing reads them: {unread} — "
        "wire them or remove them, do not ship a knob that does nothing"
    )


def test_the_removed_ones_are_gone_everywhere():
    removed = (
        "FRIDAY_TELEGRAM_BOT_ID",
        "FRIDAY_TELEGRAM_SESSION_TTL_SECONDS",
        "FRIDAY_TELEMETRY_INTERVAL_SEC",
        "FRIDAY_HEALTH_INTERVAL_SEC",
        "FRIDAY_DEFAULT_CITY",
        # Заменён на FRIDAY_INGESTION_REVIEW_POLICY: булев рубильник описывал
        # только текстовый путь, файлы его не читали вовсе.
        "FRIDAY_INGESTION_STRICT_REVIEW",
    )
    for path in (
        ROOT / ".env.example",
        ROOT / "docker-compose.yml",
        CONFIG_SOURCE,
        ROOT / "friday" / "cli.py",
    ):
        text = path.read_text(encoding="utf-8")
        present = [key for key in removed if key in text]
        assert not present, f"{path.name} still offers settings nothing reads: {present}"
