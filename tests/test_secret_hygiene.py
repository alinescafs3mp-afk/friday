"""Jericho's own credentials, found where they should not be.

A live Telegram bot token spent two days in a plain file on the desktop of the machine
running this instance, inside a directory whose owner then asked to have it imported
into the knowledge base. `jericho doctor` checked the database, the workers, the backups
and the model endpoint, and had no opinion about that at all.

The check is by value, not by pattern: it compares against the exact credentials this
process was started with. A file either contains this bot token or it does not, so there
is no false-positive rate to tune — and no regex that quietly stops matching when a
token format changes.

Three properties are non-negotiable and each has a test: it finds the real case, it
never puts the secret into its own output, and it cannot become the reason diagnostics
are slow or fail.
"""

from __future__ import annotations

import os

import pytest

from jericho.secret_hygiene import MAX_FILE_BYTES, MIN_SECRET_LENGTH, named_secrets, scan

TOKEN = "7891234567:AAH-realistic-looking-bot-token-value"
API_KEY = "sk-" + "z" * 40


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "Рабочий стол").mkdir()
    (tmp_path / "Рабочий стол" / "TG_token.txt").write_text(TOKEN + "\n", encoding="utf-8")
    (tmp_path / "Рабочий стол" / "заметка.txt").write_text("обычная заметка", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# план\n\nничего секретного", encoding="utf-8")
    return tmp_path


def test_it_finds_the_case_that_prompted_it(tree):
    report = scan([tree], secrets={"JERICHO_TELEGRAM_BOT_TOKEN": TOKEN})

    assert len(report.exposures) == 1
    exposure = report.exposures[0]
    assert exposure.path.name == "TG_token.txt"
    assert exposure.secret_name == "JERICHO_TELEGRAM_BOT_TOKEN"


def test_the_secret_never_appears_in_the_report(tree):
    """A leak detector that prints the leak has widened it."""
    report = scan([tree], secrets={"JERICHO_TELEGRAM_BOT_TOKEN": TOKEN})

    assert TOKEN not in repr(report)
    assert TOKEN not in str(report.exposures[0])
    assert TOKEN not in str(report.exposures[0].path)


def test_files_without_the_secret_are_not_reported(tree):
    report = scan([tree], secrets={"JERICHO_TELEGRAM_BOT_TOKEN": TOKEN})
    reported = {exposure.path.name for exposure in report.exposures}
    assert "заметка.txt" not in reported and "notes.md" not in reported


def test_the_env_file_is_where_secrets_belong(tmp_path):
    """Reporting the configuration file itself would make the check unusable."""
    (tmp_path / ".env.local").write_text(f"JERICHO_TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")

    report = scan([tmp_path], secrets={"JERICHO_TELEGRAM_BOT_TOKEN": TOKEN})
    assert report.exposures == []


def test_a_backup_of_the_env_file_is_still_an_extra_copy(tmp_path):
    """`.env.local.bak.*` holds the same live credential and is one more place to lose
    it from. Found in practice on this machine, left by an earlier edit."""
    (tmp_path / ".env.local.bak.20260726").write_text(f"JERICHO_API_TOKEN={API_KEY}\n", encoding="utf-8")

    report = scan([tmp_path], secrets={"JERICHO_API_TOKEN": API_KEY})
    assert [e.path.name for e in report.exposures] == [".env.local.bak.20260726"]


def test_overlapping_roots_report_each_file_once(tmp_path):
    """JERICHO_HOME normally lives inside the owner's home; scanning both walks the same
    tree twice. Measured on the real machine before the fix: every finding doubled."""
    inner = tmp_path / ".jericho"
    inner.mkdir()
    (inner / "stray.txt").write_text(TOKEN, encoding="utf-8")

    report = scan([tmp_path, inner], secrets={"JERICHO_TELEGRAM_BOT_TOKEN": TOKEN})
    assert len(report.exposures) == 1


def test_a_protected_file_is_judged_on_its_permissions(tmp_path):
    key = tmp_path / "backup.key"
    key.write_text("k" * 64, encoding="utf-8")
    key.chmod(0o644)

    report = scan([tmp_path], secrets={}, protected=[key])

    assert [path.name for path, _mode in report.loose_permissions] == ["backup.key"]
    key.chmod(0o600)
    assert scan([tmp_path], secrets={}, protected=[key]).loose_permissions == []


def test_world_readability_is_reported_because_it_changes_the_urgency(tree):
    private = tree / "private.txt"
    private.write_text(TOKEN, encoding="utf-8")
    private.chmod(0o600)

    exposures = {e.path.name: e for e in scan([tree], secrets={"T": TOKEN}).exposures}
    assert exposures["private.txt"].world_readable is False
    assert exposures["TG_token.txt"].world_readable is True


def test_short_values_are_never_matched_on():
    """A four-character 'secret' would match half the disk."""
    assert named_secrets({"JERICHO_API_TOKEN": "abc"}) == {}
    assert named_secrets({"JERICHO_API_TOKEN": "x" * MIN_SECRET_LENGTH}) != {}


def test_only_credential_shaped_variables_are_treated_as_secrets():
    picked = named_secrets(
        {
            "JERICHO_API_TOKEN": "a" * 30,
            "JERICHO_LLM_API_KEY": "b" * 30,
            "JERICHO_TELEGRAM_BRIDGE_SECRET": "c" * 30,
            "JERICHO_LLM_BASE_URL": "http://192.168.1.5:8001/v1-and-padding",
            "PATH": "/usr/bin:" + "d" * 30,
        }
    )
    assert set(picked) == {"JERICHO_API_TOKEN", "JERICHO_LLM_API_KEY", "JERICHO_TELEGRAM_BRIDGE_SECRET"}


def test_large_files_are_skipped_rather_than_read(tmp_path):
    """Bounded by design: doctor is run when something is already wrong."""
    big = tmp_path / "huge.bin"
    big.write_bytes(b"." * (MAX_FILE_BYTES + 1024) + TOKEN.encode())

    report = scan([tmp_path], secrets={"T": TOKEN})
    assert report.exposures == []
    assert report.files_scanned == 0


def test_an_unreadable_file_does_not_stop_the_scan(tree):
    blocked = tree / "blocked.txt"
    blocked.write_text(TOKEN, encoding="utf-8")
    blocked.chmod(0o000)
    try:
        report = scan([tree], secrets={"T": TOKEN})
        # The desktop copy is still found; the unreadable one is silently skipped.
        assert "TG_token.txt" in {e.path.name for e in report.exposures}
    finally:
        blocked.chmod(0o600)


def test_diagnostics_do_not_pay_for_the_scan_unless_asked(settings, tmp_path, monkeypatch):
    """status and the admin endpoint stay fast; doctor and sentinel opt in."""
    from jericho.diagnostics import collect_diagnostics

    calls: list[int] = []

    def counting_scan(*args, **kwargs):
        calls.append(1)
        from jericho.secret_hygiene import Report

        return Report(exposures=[], loose_permissions=[], files_scanned=0, stopped_early=False)

    monkeypatch.setattr("jericho.secret_hygiene.scan", counting_scan)

    collect_diagnostics(settings)
    assert calls == []

    collect_diagnostics(settings, check_secrets=True)
    assert calls == [1]


def test_a_failing_scan_never_breaks_diagnostics(settings, monkeypatch):
    """Self-inspection that can take down the report is worse than none."""
    from jericho.diagnostics import collect_diagnostics

    def explode(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("jericho.secret_hygiene.scan", explode)
    report = collect_diagnostics(settings, check_secrets=True)
    assert "actions" in report and isinstance(report["actions"], list)


def test_the_alert_reaches_sentinel_and_names_the_file(settings, tmp_path, monkeypatch):
    from jericho.diagnostics import collect_diagnostics
    from jericho.organs.sentinel import _ALERT_SEVERITIES

    stray = tmp_path / "TG_token.txt"
    stray.write_text(settings.api_token, encoding="utf-8")
    monkeypatch.setenv("JERICHO_API_TOKEN", settings.api_token)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(os, "environ", {**os.environ, "JERICHO_API_TOKEN": settings.api_token})

    actions = collect_diagnostics(settings, check_secrets=True)["actions"]
    alerts = [action for action in actions if action["code"] == "secret_exposed_in_file"]

    assert alerts, "an exposed credential must reach the owner"
    assert alerts[0]["severity"] in _ALERT_SEVERITIES
    assert "TG_token.txt" in alerts[0]["detail"]
    assert settings.api_token not in alerts[0]["detail"], "the alert must not repeat the secret"
