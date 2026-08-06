"""Friday's own credentials, found where they should not be.

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
from pathlib import Path

import pytest

from friday.secret_hygiene import MAX_FILE_BYTES, MAX_FILES, MIN_SECRET_LENGTH, named_secrets, scan

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
    report = scan([tree], secrets={"FRIDAY_TELEGRAM_BOT_TOKEN": TOKEN})

    assert len(report.exposures) == 1
    exposure = report.exposures[0]
    assert exposure.path.name == "TG_token.txt"
    assert exposure.secret_name == "FRIDAY_TELEGRAM_BOT_TOKEN"


def test_the_secret_never_appears_in_the_report(tree):
    """A leak detector that prints the leak has widened it."""
    report = scan([tree], secrets={"FRIDAY_TELEGRAM_BOT_TOKEN": TOKEN})

    assert TOKEN not in repr(report)
    assert TOKEN not in str(report.exposures[0])
    assert TOKEN not in str(report.exposures[0].path)


def test_files_without_the_secret_are_not_reported(tree):
    report = scan([tree], secrets={"FRIDAY_TELEGRAM_BOT_TOKEN": TOKEN})
    reported = {exposure.path.name for exposure in report.exposures}
    assert "заметка.txt" not in reported and "notes.md" not in reported


def test_the_env_file_is_where_secrets_belong(tmp_path):
    """Reporting the configuration file itself would make the check unusable.

    Identified by PATH, not by name. Skipping every file called `.env` or `.env.local`
    anywhere in the tree was blindness exactly where credentials live — see the test
    below, where an unrelated project's `.env` holds a copy of the live token.
    """
    env_file = tmp_path / ".env.local"
    env_file.write_text(f"FRIDAY_TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")

    report = scan([tmp_path], secrets={"FRIDAY_TELEGRAM_BOT_TOKEN": TOKEN}, protected=[env_file])
    assert report.exposures == []


def test_someone_elses_env_file_is_an_exposure(tmp_path):
    """The one file that is allowed to hold the token is the one this process reads."""
    env_file = tmp_path / ".env.local"
    env_file.write_text(f"FRIDAY_TELEGRAM_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    elsewhere = tmp_path / "projects" / "site"
    elsewhere.mkdir(parents=True)
    (elsewhere / ".env").write_text(f"BOT_TOKEN={TOKEN}\n", encoding="utf-8")

    report = scan([tmp_path], secrets={"FRIDAY_TELEGRAM_BOT_TOKEN": TOKEN}, protected=[env_file])
    reported = {exposure.path for exposure in report.exposures}
    assert elsewhere / ".env" in reported, "a copy of the live token in another project went unseen"
    assert env_file not in reported


def test_a_copy_of_the_backup_key_is_found_by_its_value(tmp_path):
    """The key is configured as a PATH, so its value was never among the searched secrets.

    `named_secrets` only collects variables whose NAME carries TOKEN/SECRET/API_KEY/
    PASSWORD, and the setting is FRIDAY_BACKUP_ENCRYPTION_KEY_FILE. The scanner knew
    where the key was and checked its permissions, while a loose copy of the key itself
    was invisible — and `jericho keygen` advises making that copy.
    """
    key_file = tmp_path / "backup.key"
    key_material = "k" * 44
    key_file.write_text(key_material, encoding="utf-8")
    copy = tmp_path / "docs" / "ключ-на-всякий-случай.txt"
    copy.parent.mkdir(parents=True)
    copy.write_text(key_material, encoding="utf-8")

    report = scan([tmp_path], secrets={}, protected=[key_file])
    assert copy in {exposure.path for exposure in report.exposures}
    assert key_file not in {exposure.path for exposure in report.exposures}


def test_non_utf8_protected_key_bytes_are_matched_losslessly(tmp_path):
    key_file = tmp_path / "backup.key"
    key_material = b"\xff" * 32
    key_file.write_bytes(key_material)
    copy = tmp_path / "backup-key-copy.bin"
    copy.write_bytes(key_material)

    report = scan([tmp_path], secrets={}, protected=[key_file])

    assert [item.path for item in report.exposures] == [copy]


def test_a_backup_of_the_env_file_is_still_an_extra_copy(tmp_path):
    """`.env.local.bak.*` holds the same live credential and is one more place to lose
    it from. Found in practice on this machine, left by an earlier edit."""
    (tmp_path / ".env.local.bak.20260726").write_text(f"FRIDAY_API_TOKEN={API_KEY}\n", encoding="utf-8")

    report = scan([tmp_path], secrets={"FRIDAY_API_TOKEN": API_KEY})
    assert [e.path.name for e in report.exposures] == [".env.local.bak.20260726"]


def test_overlapping_roots_report_each_file_once(tmp_path):
    """FRIDAY_HOME normally lives inside the owner's home; scanning both walks the same
    tree twice. Measured on the real machine before the fix: every finding doubled."""
    inner = tmp_path / ".jericho"
    inner.mkdir()
    (inner / "stray.txt").write_text(TOKEN, encoding="utf-8")

    report = scan([tmp_path, inner], secrets={"FRIDAY_TELEGRAM_BOT_TOKEN": TOKEN})
    assert len(report.exposures) == 1


def test_excluded_live_artifact_is_never_opened(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    live = tmp_path / "live.sqlite3"
    ordinary = tmp_path / "ordinary.txt"
    live.write_text(TOKEN, encoding="utf-8")
    ordinary.write_text("ordinary", encoding="utf-8")
    original = hygiene._scan_regular_file

    def forbidden(candidate, *args, **kwargs):
        assert candidate.path != live, "an excluded live artifact was opened"
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(hygiene, "_scan_regular_file", forbidden)

    report = scan([tmp_path], secrets={"T": TOKEN}, excluded=[live])

    assert report.exposures == []
    assert report.files_scanned == 1


def test_a_hardlink_to_an_excluded_live_inode_is_also_never_opened(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    live = tmp_path / "live.sqlite3"
    alias = tmp_path / "innocent-name.txt"
    live.write_text(TOKEN, encoding="utf-8")
    os.link(live, alias)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an excluded live inode was opened through a hardlink")

    monkeypatch.setattr(hygiene, "_scan_regular_file", forbidden)

    report = scan([tmp_path], secrets={"T": TOKEN}, excluded=[live])

    assert report.exposures == []
    assert report.files_scanned == 0


def test_excluded_boundary_overrides_a_protected_hardlink(tmp_path, monkeypatch):
    """`excluded` is the live-DB boundary even if a caller also marks an alias protected."""
    import friday.secret_hygiene as hygiene

    live = tmp_path / "live.sqlite3"
    protected_alias = tmp_path / "configured-secret"
    live.write_text(TOKEN, encoding="utf-8")
    os.link(live, protected_alias)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("an excluded inode was opened through protected")

    monkeypatch.setattr(hygiene, "_read_protected_value", forbidden)

    report = scan(
        [tmp_path],
        secrets={"T": TOKEN},
        protected=[protected_alias],
        excluded=[live],
    )

    assert report.exposures == []
    assert report.loose_permissions == []
    assert report.files_scanned == 0


def test_an_excluded_sidecar_appearing_after_discovery_excludes_its_inode(tmp_path, monkeypatch):
    """A WAL/SHM absent at scan start may appear before candidate files are opened."""
    import friday.secret_hygiene as hygiene

    candidate = tmp_path / "candidate.bin"
    live = tmp_path / "live.sqlite3-wal"
    candidate.write_text(TOKEN, encoding="utf-8")
    original_collect = hygiene._collect_candidates

    def collect_then_create_sidecar(*args, **kwargs):
        collected = original_collect(*args, **kwargs)
        os.link(candidate, live)
        return collected

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a newly excluded inode was opened through an earlier alias")

    monkeypatch.setattr(hygiene, "_collect_candidates", collect_then_create_sidecar)
    monkeypatch.setattr(hygiene, "_scan_regular_file", forbidden)

    report = scan([tmp_path], secrets={"T": TOKEN}, excluded=[live])

    assert report.exposures == []
    assert report.files_scanned == 0


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
    assert named_secrets({"FRIDAY_API_TOKEN": "abc"}) == {}
    assert named_secrets({"FRIDAY_API_TOKEN": "x" * MIN_SECRET_LENGTH}) != {}


def test_only_credential_shaped_variables_are_treated_as_secrets():
    picked = named_secrets(
        {
            "FRIDAY_API_TOKEN": "a" * 30,
            "FRIDAY_LLM_API_KEY": "b" * 30,
            "FRIDAY_TELEGRAM_BRIDGE_SECRET": "c" * 30,
            "FRIDAY_LLM_BASE_URL": "http://192.168.1.5:8001/v1-and-padding",
            "PATH": "/usr/bin:" + "d" * 30,
        }
    )
    assert set(picked) == {"FRIDAY_API_TOKEN", "FRIDAY_LLM_API_KEY", "FRIDAY_TELEGRAM_BRIDGE_SECRET"}


def test_large_files_are_streamed_instead_of_skipped(tmp_path):
    """A config dump or log is a normal place for a copied credential to land."""
    big = tmp_path / "huge.bin"
    big.write_bytes(b"." * (MAX_FILE_BYTES + 1024) + TOKEN.encode())

    report = scan([tmp_path], secrets={"T": TOKEN})
    assert [item.path for item in report.exposures] == [big]
    assert report.files_scanned == 1
    assert report.oversized_skipped == 0
    assert report.complete is True


def test_a_secret_crossing_a_stream_chunk_boundary_is_found(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    monkeypatch.setattr(hygiene, "SCAN_CHUNK_BYTES", 64)
    prefix = b"." * (64 - len(TOKEN.encode()) // 2)
    boundary = tmp_path / "boundary.bin"
    boundary.write_bytes(prefix + TOKEN.encode() + b"tail")

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert [item.path for item in report.exposures] == [boundary]


def test_hardlinks_are_read_once_but_every_path_is_reported(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text(TOKEN, encoding="utf-8")
    os.link(first, second)
    original = hygiene._scan_regular_file
    reads: list[str] = []

    def counted(*args, **kwargs):
        reads.append(args[0].path.name)
        return original(*args, **kwargs)

    monkeypatch.setattr(hygiene, "_scan_regular_file", counted)

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert {item.path for item in report.exposures} == {first, second}
    assert len(reads) == 1
    assert report.hardlink_aliases == 1
    assert report.files_scanned == 2


def test_a_midstream_read_error_cannot_retry_a_hardlink_or_escape_the_budget(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"." * 64)
    os.link(first, second)
    original_read = hygiene.os.read
    physical_bytes = 0
    successful_reads = 0
    injected = False

    def fail_after_one_chunk(descriptor, amount):
        nonlocal physical_bytes, successful_reads, injected
        if successful_reads == 1 and not injected:
            injected = True
            raise OSError("synthetic midstream failure")
        chunk = original_read(descriptor, amount)
        physical_bytes += len(chunk)
        successful_reads += 1
        return chunk

    monkeypatch.setattr(hygiene, "SCAN_CHUNK_BYTES", 8)
    monkeypatch.setattr(hygiene, "MAX_SCAN_BYTES", 16)
    monkeypatch.setattr(hygiene.os, "read", fail_after_one_chunk)

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert physical_bytes == report.bytes_scanned == 8
    assert report.hardlink_aliases == 1
    assert report.files_not_fully_scanned == 2
    assert report.unreadable_skipped == 2
    assert report.complete is False


def test_small_files_are_scanned_before_large_files_under_the_byte_budget(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    large = tmp_path / "a-large.bin"
    small = tmp_path / "z-small.txt"
    large.write_bytes(b"." * (MAX_FILE_BYTES + 1))
    small.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setattr(hygiene, "MAX_SCAN_BYTES", len(TOKEN.encode()))

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert [item.path for item in report.exposures] == [small]
    assert report.files_scanned == 1
    assert report.byte_budget_exhausted is True
    assert report.files_not_fully_scanned == 1
    assert report.clean is False


def test_byte_budget_exhaustion_is_explicit(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    (tmp_path / "one.bin").write_bytes(b"." * 64)
    (tmp_path / "two.bin").write_bytes(b"." * 64)
    monkeypatch.setattr(hygiene, "MAX_SCAN_BYTES", 32)

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert report.exposures == []
    assert report.bytes_scanned == 32
    assert report.byte_budget_exhausted is True
    assert report.files_not_fully_scanned == 2
    assert report.complete is False
    assert report.clean is False


def test_measured_working_tree_fits_the_file_bound():
    """The former 4,000 cap missed 493 measured eligible small files."""
    assert MAX_FILES > 4_493


def test_a_secret_after_the_measured_4493_path_boundary_is_reached(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    seed = tmp_path / "ordinary-seed"
    seed.write_bytes(b"")
    ordered_paths = []
    for index in range(4_494):
        alias = tmp_path / f"ordinary-{index:04d}"
        os.link(seed, alias)
        ordered_paths.append(alias)
    stray = tmp_path / "stray-after-old-bound.txt"
    stray.write_text(TOKEN, encoding="utf-8")
    ordered_paths.append(stray)

    def candidates(_roots, *, report):
        del report
        yield from ordered_paths

    monkeypatch.setattr(hygiene, "_candidate_files", candidates)

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert [item.path for item in report.exposures] == [stray]
    assert report.files_scanned == 4_495
    assert report.stopped_early is False


def test_file_count_exhaustion_is_explicit(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    (tmp_path / "one.txt").write_text("ordinary", encoding="utf-8")
    (tmp_path / "two.txt").write_text("ordinary", encoding="utf-8")
    monkeypatch.setattr(hygiene, "MAX_FILES", 1)

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert report.stopped_early is True
    assert report.complete is False
    assert report.clean is False


def test_entry_flood_hits_a_fail_honest_metadata_bound(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    for index in range(3):
        (tmp_path / f"directory-{index}").mkdir()
    monkeypatch.setattr(hygiene, "MAX_WALK_ENTRIES", 2)

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert report.discovery_limit_exhausted is True
    assert report.complete is False
    assert report.clean is False


def test_an_unreadable_directory_makes_coverage_incomplete(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    ordinary = tmp_path / "ordinary.txt"
    ordinary.write_text("ordinary", encoding="utf-8")
    original_open = hygiene.os.open

    def deny_one_directory(path, flags, *args, **kwargs):
        if Path(path) == blocked:
            raise PermissionError("synthetic directory denial")
        return original_open(path, flags, *args, **kwargs)

    if hygiene.os.scandir in hygiene.os.supports_fd:
        monkeypatch.setattr(hygiene.os, "open", deny_one_directory)
    else:
        original_scandir = hygiene.os.scandir

        def deny_one_scandir(path):
            if Path(path) == blocked:
                raise PermissionError("synthetic directory denial")
            return original_scandir(path)

        monkeypatch.setattr(hygiene.os, "scandir", deny_one_scandir)

    report = scan([tmp_path], secrets={"T": TOKEN})

    assert report.files_scanned == 1
    assert report.traversal_errors == 1
    assert report.complete is False


def test_an_oversized_protected_file_is_not_counted_twice(tmp_path):
    """The protected-paths loop and the candidate-walk loop both see this file
    when the protected file (the env file, the backup key) sits inside a scanned
    root — the normal deployment layout. Each loop skipping it for size must not
    both increment the same counter."""
    protected = tmp_path / ".env.local"
    protected.write_bytes(b"." * (MAX_FILE_BYTES + 1024))

    report = scan([tmp_path], secrets={"T": TOKEN}, protected=[protected])

    assert report.oversized_skipped == 1


def test_an_unreadable_protected_secret_source_is_fail_honest(tmp_path, monkeypatch):
    import friday.secret_hygiene as hygiene

    protected = tmp_path / "backup.key"
    protected.write_text("k" * 64, encoding="utf-8")

    def unreadable(*_args, **_kwargs):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(hygiene.os, "read", unreadable)

    report = scan([tmp_path], secrets={}, protected=[protected])

    assert report.unreadable_skipped == 1
    assert report.files_not_fully_scanned == 1
    assert report.complete is False
    assert report.clean is False


def test_attacker_controlled_path_and_label_cannot_echo_the_secret(tmp_path):
    stray = tmp_path / f"copy-{API_KEY}.txt"
    stray.write_text(API_KEY, encoding="utf-8")

    report = scan([tmp_path], secrets={f"credential-{API_KEY}": API_KEY})

    assert len(report.exposures) == 1
    assert API_KEY not in repr(report)
    assert API_KEY not in str(report.exposures[0].path)
    assert API_KEY not in report.exposures[0].secret_name


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
    from friday.diagnostics import collect_diagnostics

    calls: list[int] = []

    def counting_scan(*args, **kwargs):
        calls.append(1)
        from friday.secret_hygiene import Report

        return Report(exposures=[], loose_permissions=[], files_scanned=0, stopped_early=False)

    monkeypatch.setattr("friday.secret_hygiene.scan", counting_scan)

    collect_diagnostics(settings)
    assert calls == []

    collect_diagnostics(settings, check_secrets=True)
    assert calls == [1]


def test_a_failing_scan_never_breaks_diagnostics(settings, monkeypatch):
    """Self-inspection that can take down the report is worse than none."""
    from friday.diagnostics import collect_diagnostics

    def explode(*_args, **_kwargs):
        raise OSError(f"permission denied near {TOKEN}")

    monkeypatch.setattr("friday.secret_hygiene.scan", explode)
    report = collect_diagnostics(settings, check_secrets=True)
    assert "actions" in report and isinstance(report["actions"], list)
    assert "secret_scan_unavailable" in {action["code"] for action in report["actions"]}
    assert TOKEN not in repr(report["actions"])


def test_an_incomplete_scan_says_so_instead_of_looking_clean(settings, monkeypatch):
    """`clean` only speaks for files the scan actually opened. Skipped-for-size used
    to leave no trace at all — a report with zero exposures and an incomplete scan
    looked identical to a report with zero exposures and a complete one."""
    from friday.diagnostics import collect_diagnostics
    from friday.secret_hygiene import Report

    def incomplete_scan(*_args, **_kwargs):
        return Report(
            exposures=[],
            loose_permissions=[],
            files_scanned=5,
            stopped_early=False,
            oversized_skipped=3,
        )

    monkeypatch.setattr("friday.secret_hygiene.scan", incomplete_scan)

    actions = collect_diagnostics(settings, check_secrets=True)["actions"]
    warnings = [action for action in actions if action["code"] == "secret_scan_incomplete"]

    assert warnings, "a scan that skipped files must say so, not just report clean"
    assert "3" in warnings[0]["detail"]


def test_a_byte_limited_scan_reaches_diagnostics(settings, monkeypatch):
    from friday.diagnostics import collect_diagnostics
    from friday.secret_hygiene import Report

    def byte_limited_scan(*_args, **_kwargs):
        return Report(
            exposures=[],
            loose_permissions=[],
            files_scanned=4,
            stopped_early=False,
            files_not_fully_scanned=2,
            byte_budget_exhausted=True,
        )

    monkeypatch.setattr("friday.secret_hygiene.scan", byte_limited_scan)

    actions = collect_diagnostics(settings, check_secrets=True)["actions"]
    warnings = [action for action in actions if action["code"] == "secret_scan_incomplete"]

    assert len(warnings) == 1
    assert "2" in warnings[0]["detail"]
    assert "МиБ" in warnings[0]["detail"]


def test_a_metadata_limited_scan_reaches_diagnostics(settings, monkeypatch):
    from friday.diagnostics import collect_diagnostics
    from friday.secret_hygiene import Report

    def metadata_limited_scan(*_args, **_kwargs):
        return Report(
            exposures=[],
            loose_permissions=[],
            files_scanned=4,
            stopped_early=False,
            traversal_errors=2,
            discovery_limit_exhausted=True,
        )

    monkeypatch.setattr("friday.secret_hygiene.scan", metadata_limited_scan)

    actions = collect_diagnostics(settings, check_secrets=True)["actions"]
    warnings = [action for action in actions if action["code"] == "secret_scan_incomplete"]

    assert len(warnings) == 1
    assert "пределе обхода" in warnings[0]["detail"]
    assert "2 каталог" in warnings[0]["detail"]


def test_a_complete_scan_says_nothing_about_completeness(settings, monkeypatch):
    from friday.diagnostics import collect_diagnostics
    from friday.secret_hygiene import Report

    def complete_scan(*_args, **_kwargs):
        return Report(exposures=[], loose_permissions=[], files_scanned=5, stopped_early=False)

    monkeypatch.setattr("friday.secret_hygiene.scan", complete_scan)

    actions = collect_diagnostics(settings, check_secrets=True)["actions"]
    assert not [action for action in actions if action["code"] == "secret_scan_incomplete"]


def test_the_alert_reaches_sentinel_and_names_the_file(settings, tmp_path, monkeypatch):
    from friday.diagnostics import collect_diagnostics
    from friday.organs.sentinel import _ALERT_SEVERITIES

    stray = tmp_path / "TG_token.txt"
    stray.write_text(settings.api_token, encoding="utf-8")
    monkeypatch.setenv("FRIDAY_API_TOKEN", settings.api_token)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(os, "environ", {**os.environ, "FRIDAY_API_TOKEN": settings.api_token})

    actions = collect_diagnostics(settings, check_secrets=True)["actions"]
    alerts = [action for action in actions if action["code"] == "secret_exposed_in_file"]

    assert alerts, "an exposed credential must reach the owner"
    assert alerts[0]["severity"] in _ALERT_SEVERITIES
    assert "TG_token.txt" in alerts[0]["detail"]
    assert settings.api_token not in alerts[0]["detail"], "the alert must not repeat the secret"


def test_semantic_search_without_numpy_is_a_visible_warning(settings, monkeypatch):
    """The pure-Python fallback decides identically but scans the corpus per query.

    numpy stays optional — the fallback is correct, and a mandatory dependency would
    break the project's zero-required-deps rule. What is not acceptable is silence:
    without it Friday just looks slow, and the cause is invisible.
    """
    import builtins
    import dataclasses

    from friday.config import validate_settings

    real_import = builtins.__import__

    def without_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("No module named 'numpy'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_numpy)

    enabled = dataclasses.replace(settings, embeddings_enabled=True)
    assert any("numpy" in item for item in validate_settings(enabled))
    # Off, the scan never runs, so there is nothing to warn about.
    disabled = dataclasses.replace(settings, embeddings_enabled=False)
    assert not any("numpy" in item for item in validate_settings(disabled))
