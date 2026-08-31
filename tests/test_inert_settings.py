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
WORD_TOKEN = re.compile(r"\b\w+\b")

# These fields are consumed INSIDE the config module. ``data_dir`` and
# ``cache_dir`` derive other paths; the two latency fields are strictly validated
# and forwarded by ``semantic_supervisor_promotion_activation_settings()``, whose
# result is consumed by the server.  Excluding config source from the lexical scan
# must not misclassify those real uses as inert knobs.
CONSUMED_INSIDE_CONFIG = {
    "data_dir",
    "cache_dir",
    "semantic_supervisor_promotion_latency_budget_file",
    "semantic_supervisor_promotion_latency_budget_sha256",
}


def _mentioned_settings(text: str, names: set[str]) -> set[str]:
    """Keep the existing exact word-boundary lexical semantics in one scan."""

    return names & set(WORD_TOKEN.findall(text))


def _fields_no_code_reads() -> set[str]:
    names = {field.name for field in dataclasses.fields(FridaySettings)}
    seen: set[str] = set()
    for path in list((ROOT / "friday").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")):
        if path == CONFIG_SOURCE or path.name == pathlib.Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        seen.update(_mentioned_settings(text, names))
    return names - seen - CONSUMED_INSIDE_CONFIG


def test_setting_mentions_are_exact_and_preserve_non_code_semantics():
    names = {"exact_name", "near_miss", "comment_name", "string_name", "prose_name"}
    text = """
settings.exact_name
prefix_near_miss = near_miss_suffix
# comment_name
label = "string_name"
arbitrary non-code prose_name text
"""

    assert _mentioned_settings(text, names) == {
        "exact_name",
        "comment_name",
        "string_name",
        "prose_name",
    }


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
