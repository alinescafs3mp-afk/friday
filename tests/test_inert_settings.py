"""A documented knob that nothing reads is worse than a missing one.

Five settings are parsed by `load_settings`, stored on `JerichoSettings`, listed in
`.env.example` and forwarded by Compose — and read by no code at all.
`JERICHO_TELEGRAM_SESSION_TTL_SECONDS` is the sharp one: it reads as a security
control, so an operator who sets it believes Telegram sessions expire. They do not.

Until they are wired or withdrawn — an owner's call, not a bug fix — validation says
so out loud. This test pins the list in both directions: a knob that gets wired must
leave it, and a new dead one must be added deliberately.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from dataclasses import replace

from jericho.config import INERT_SETTINGS, JerichoSettings, validate_settings

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_SOURCE = ROOT / "jericho" / "config" / "__init__.py"


def _fields_no_code_reads() -> set[str]:
    """Settings fields mentioned nowhere outside the config module itself."""
    names = {field.name for field in dataclasses.fields(JerichoSettings)}
    seen: set[str] = set()
    for path in list((ROOT / "jericho").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")):
        if path == CONFIG_SOURCE or path.name == pathlib.Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8")
        seen |= {name for name in names if re.search(rf"\b{name}\b", text)}
    # `data_dir` and `cache_dir` are consumed inside config to derive other paths,
    # which is a real use; they are not knobs that promise behaviour.
    return names - seen - {"data_dir", "cache_dir"}


def test_the_inert_list_matches_reality():
    assert _fields_no_code_reads() == set(INERT_SETTINGS), (
        "the set of settings nothing reads has drifted from the declared list"
    )


def test_setting_an_inert_knob_is_reported(settings):
    quiet = replace(settings, **INERT_SETTINGS)
    assert not [item for item in validate_settings(quiet) if "nothing reads them" in item]

    noisy = replace(quiet, telegram_session_ttl_seconds=3600)
    reported = [item for item in validate_settings(noisy) if "nothing reads them" in item]
    assert reported, "an operator configured session expiry and was told nothing"
    assert "telegram_session_ttl_seconds" in reported[0]
