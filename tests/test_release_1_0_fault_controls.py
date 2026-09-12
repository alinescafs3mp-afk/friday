"""Faults injected at observed journey boundaries must make the harness red."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from tools import release_1_0_acceptance as acceptance
from tools import release_1_0_live_journeys as journeys


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("listing_missing", "backup_created_item_not_listed"),
        ("listing_count", "backup_after_listing_invalid"),
        ("listing_bool_count", "backup_after_listing_invalid"),
        ("listing_duplicate", "backup_after_listing_invalid"),
        ("download_failed", "backup_download_invalid"),
        ("download_html", "backup_download_invalid"),
        ("download_disposition", "backup_download_invalid"),
        ("download_digest", "backup_manifest_download_mismatch"),
        ("download_unrelated", "backup_download_source_missing"),
        ("unauthorized", "backup_unauthorized_access"),
        ("denied_effect", "backup_denied_create_effect"),
        ("escape", "backup_download_path_boundary"),
        ("missing", "backup_download_path_boundary"),
        ("audit", "backup_audit_missing"),
        ("audit_filename_target", "backup_audit_private_target_invalid"),
        ("audit_foreign_target", "backup_audit_private_target_invalid"),
        ("audit_filename_before", "backup_audit_filename_exposed"),
        ("audit_filename_after", "backup_audit_filename_exposed"),
        ("audit_filename_escaped", "backup_audit_filename_exposed"),
    ],
)
def test_backup_api_oracle_rejects_observed_contract_faults(settings, monkeypatch, fault, expected):
    from friday.server import create_app
    from friday.storage import FridayStorage as Storage

    original_get, original_post = TestClient.get, TestClient.post
    owner = journeys._owner_headers(settings)
    created_name = ""
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE raw_objects(id TEXT, raw_content TEXT)")
        unrelated = database.serialize()

    def altered_manifest(entry):
        if fault == "download_unrelated":
            entry.update(sha256=hashlib.sha256(unrelated).hexdigest(), size_bytes=len(unrelated))
        return entry

    def damaged_post(self, url, *args, **kwargs):
        nonlocal created_name
        denied = url == "/api/admin/backups" and kwargs.get("headers") != owner
        if denied and fault in {"unauthorized", "denied_effect"}:
            kwargs["headers"] = owner
        response = original_post(self, url, *args, **kwargs)
        if denied and fault == "denied_effect":
            return httpx.Response(403, json={"detail": "refused after effect"})
        if url == "/api/admin/backups" and response.status_code == 200:
            body = response.json()
            if kwargs.get("json", {}).get("label") == "r10-j08":
                created_name = body["backup"]["database"]
            altered_manifest(body["backup"])
            return httpx.Response(200, json=body)
        return response

    def damaged_get(self, url, *args, **kwargs):
        response = original_get(self, url, *args, **kwargs)
        if url == "/api/admin/backups" and response.status_code == 200:
            body = response.json()
            if body["items"]:
                if fault == "listing_missing":
                    body.update(items=[], count=0)
                elif fault == "listing_count":
                    body["count"] += 1
                elif fault == "listing_bool_count":
                    body["count"] = True
                elif fault == "listing_duplicate":
                    body["items"] *= 2
                    body["count"] = len(body["items"])
                for entry in body["items"]:
                    altered_manifest(entry)
            return httpx.Response(200, json=body)
        if url.endswith("/download") and url.startswith("/api/admin/backups/"):
            if fault in {"escape", "missing"} and f"r10-{fault}.sqlite3" in url:
                return httpx.Response(200, content=b"wrong path allowed")
            if response.status_code == 200:
                headers = dict(response.headers)
                if fault == "download_failed":
                    return httpx.Response(503, content=response.content, headers=headers)
                if fault == "download_html":
                    return httpx.Response(200, content=b"<html>OK</html>", headers=headers)
                if fault == "download_disposition":
                    headers.pop("content-disposition", None)
                if fault == "download_digest":
                    return httpx.Response(200, content=response.content + b"changed", headers=headers)
                if fault == "download_unrelated":
                    return httpx.Response(200, content=unrelated, headers=headers)
                return httpx.Response(200, content=response.content, headers=headers)
        return response

    monkeypatch.setattr(TestClient, "get", damaged_get)
    monkeypatch.setattr(TestClient, "post", damaged_post)
    if fault.startswith("audit"):
        original_audit = Storage.list_audit_log

        def damaged_audit(self, *args, **kwargs):
            rows = original_audit(self, *args, **kwargs)
            if fault == "audit":
                return [row for row in rows if not row["action"].startswith("admin.backup.")]
            for row in rows:
                if not row["action"].startswith("admin.backup."):
                    continue
                if fault == "audit_filename_target":
                    row["target_id"] = created_name
                elif fault == "audit_foreign_target":
                    row["target_id"] = row["target_id"][:-1] + ("1" if row["target_id"][-1] == "0" else "0")
                elif fault == "audit_filename_escaped":
                    escaped = "".join(f"\\u{ord(char):04x}" for char in created_name)
                    row["after_json"] = '{"filename":"' + escaped + '"}'
                else:
                    field = "before_json" if fault == "audit_filename_before" else "after_json"
                    row[field] = json.dumps({"filename": created_name})
            return rows

        monkeypatch.setattr(Storage, "list_audit_log", damaged_audit)
    with TestClient(create_app(settings)) as client:
        uploaded = journeys._upload_bytes(
            client, owner, "r10-j08.txt", journeys._canary("R10_J08").encode(), "text/plain"
        )
        assert uploaded["status_code"] == 200
        failures = []
        journeys._observe_backup_api(client, settings, uploaded["body"]["raw_object_id"], failures)
    assert expected in failures, failures


@pytest.mark.parametrize("status", [200, 503])
def test_upload_search_journey_rejects_empty_or_failed_search(settings, monkeypatch, status):
    original = TestClient.get

    def broken_search(self, url, *args, **kwargs):
        if url == "/api/search":
            return httpx.Response(status, json={"items": []})
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", broken_search)
    result = journeys.run_j01(settings)
    assert result["status"] == "FAIL", result
    assert any("search" in code for code in result["failure_codes"]), result


@pytest.mark.parametrize("fault", ["missing_challenge", "persisted_while_locked"])
def test_password_journey_rejects_broken_password_boundary(settings, monkeypatch, fault):
    original = TestClient.post
    original_count = journeys._file_count
    counts = 0

    def broken_upload(self, url, *args, **kwargs):
        if url == "/api/files":
            return httpx.Response(200, json={"archive_password_required": fault != "missing_challenge"})
        return original(self, url, *args, **kwargs)

    def changed_count(*args):
        nonlocal counts
        counts += 1
        return original_count(*args) + int(fault == "persisted_while_locked" and counts > 1)

    monkeypatch.setattr(TestClient, "post", broken_upload)
    monkeypatch.setattr(journeys, "_file_count", changed_count)
    result = journeys.run_password_zip(settings)
    assert result["status"] == "FAIL", result


def test_explicit_empty_runner_selection_cannot_run_every_case(settings):
    with pytest.raises(journeys.acceptance.AcceptanceError, match="zero_collected_cases"):
        journeys.run_deterministic_suite(settings, case_ids=[])


def test_missing_executable_handler_is_not_silently_dropped(settings, monkeypatch):
    case_id = "R10-J01-UPLOAD-SEARCH-RESTART"
    monkeypatch.delitem(journeys.RUNNERS, case_id)
    with pytest.raises(journeys.acceptance.AcceptanceError, match="executable_handler_missing"):
        journeys.run_deterministic_suite(settings, [case_id])


def test_unknown_surface_reaches_audit_exit_status(monkeypatch, capsys):
    monkeypatch.setattr(acceptance, "discover_surfaces", lambda: {"injected": ["ui:new_uncovered"]})
    assert acceptance.main(["--audit-only"]) == 2
    assert '"valid": false' in capsys.readouterr().out


@pytest.mark.parametrize("effect", ["conversation", "command_file"])
def test_engineer_refusal_detects_actual_state_changes(settings, monkeypatch, effect):
    original = TestClient.post

    def rejected_with_effect(self, url, *args, **kwargs):
        response = original(self, url, *args, **kwargs)
        if url == "/api/conversations/channel/mode":
            if effect == "conversation":
                storage = self.app.state.storage
                user = storage.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
                storage.create_conversation(user["id"], title="unexpected synthetic effect", mode="engineer")
            else:
                root = self.app.state.settings.engineer_command_store_dir
                root.mkdir(parents=True, exist_ok=True)
                (root / "unexpected-job-receipt").write_bytes(b"unexpected synthetic effect")
        return response

    monkeypatch.setattr(TestClient, "post", rejected_with_effect)
    result = journeys.run_j05(settings)
    assert result["status"] == "FAIL", result
    assert "forbidden_effect" in result["failure_codes"]


@pytest.mark.parametrize("fault", ["missing", "swapped", "corrupt"])
def test_table_version_journey_reads_the_actual_files(settings, monkeypatch, fault):
    original = TestClient.get

    def broken_download(self, url, *args, **kwargs):
        response = original(self, url, *args, **kwargs)
        if url.startswith("/api/files/"):
            if fault == "missing":
                return httpx.Response(404, content=b"")
            content = response.content
            if fault == "swapped":
                content = (
                    b"id,name\nBRK.B,beta-r10\n" if b"BRK.A" in content else b"id,name\nBRK.A,alpha-r10\n"
                )
            else:
                content = content.replace(b"BRK.", b"WRONG.")
            return httpx.Response(200, content=content)
        return response

    monkeypatch.setattr(TestClient, "get", broken_download)
    result = journeys.run_j03(settings)
    assert result["status"] == "FAIL", result
    assert result["failure_codes"]


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_restore_journey_rejects_lost_or_changed_original_bytes(settings, monkeypatch, damage):
    original = TestClient.get

    def damaged_download(self, url, *args, **kwargs):
        if url.startswith("/api/files/"):
            return httpx.Response(404 if damage == "missing" else 200, content=b"wrong restored source")
        return original(self, url, *args, **kwargs)

    monkeypatch.setattr(TestClient, "get", damaged_download)
    result = journeys.run_j08(settings)
    assert result["status"] == "FAIL", result
    assert any("file" in code or "artifact" in code for code in result["failure_codes"])


def test_restore_journey_cannot_use_the_original_runtime(settings, monkeypatch):
    import friday.server as server

    original = server.create_app
    homes = []
    original_was_unavailable = []

    def observe_restore(runtime_settings):
        if homes:
            original_was_unavailable.append(not homes[0].exists())
        homes.append(runtime_settings.home)
        return original(runtime_settings)

    monkeypatch.setattr(server, "create_app", observe_restore)
    result = journeys.run_j08(settings)
    assert result["status"] == "PASS", result
    assert len(homes) == 2 and homes[0] != homes[1]
    assert original_was_unavailable == [True]


@pytest.mark.parametrize(
    "surface",
    ["telegram:new_uncovered", "ui:new_uncovered", "cli:new-uncovered", "api:POST /api/admin/new-uncovered"],
)
def test_new_surface_is_a_coverage_gap(surface):
    result = acceptance.classify_surfaces({"injected": [surface]}, acceptance.load_matrix())
    assert result["required_gap"] == [surface]


@pytest.mark.parametrize(
    "fault", ["wrong_source", "missing_citation", "missing_artifact", "restart_lost_hit"]
)
def test_j01_rejects_observed_source_and_artifact_damage(settings, monkeypatch, fault):
    original = TestClient.get
    searches = 0

    def damaged_result(self, url, *args, **kwargs):
        nonlocal searches
        response = original(self, url, *args, **kwargs)
        if url == "/api/search":
            searches += 1
            body = response.json()
            if fault == "wrong_source":
                for hit in body.get("results", []):
                    hit["raw_object_id"] = "raw_ffffffffffffffff"
            if fault == "restart_lost_hit" and searches > 1:
                body["results"] = []
            return httpx.Response(response.status_code, json=body)
        if (fault == "missing_citation" and url.startswith("/api/knowledge/")) or (
            fault == "missing_artifact" and url.startswith("/api/files/")
        ):
            return httpx.Response(404, json={"detail": "synthetic damaged result"})
        return response

    monkeypatch.setattr(TestClient, "get", damaged_result)
    result = journeys.run_j01(settings)
    assert result["status"] == "FAIL", result
    assert result["failure_codes"]


def test_password_fixture_cannot_be_replaced_with_plain_zip(settings, monkeypatch):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("secret.txt", b"hidden-r10")
    monkeypatch.setattr(journeys, "_passworded_zip_bytes", output.getvalue)
    with pytest.raises(journeys.JourneyError, match="password_fixture_not_encrypted"):
        journeys.run_password_zip(settings)


@pytest.mark.parametrize("order", [("create", "inspect"), ("inspect", "create")])
def test_cases_have_separate_runtime_homes_and_cleanup(settings, monkeypatch, order):
    homes = []

    def runner(case_id, case_settings):
        homes.append(case_settings.home)
        marker = case_settings.files_dir / "another-case-marker"
        assert not marker.exists()
        marker.write_text("case-private", encoding="utf-8")
        return {"id": case_id, "status": "PASS", "failure_codes": []}

    monkeypatch.setattr(
        journeys, "RUNNERS", {name: (lambda value, case_id=name: runner(case_id, value)) for name in order}
    )
    import shutil

    from friday.config import ensure_runtime_dirs
    from tools import release_1_0_deterministic as deterministic

    original_matrix = journeys.acceptance.load_matrix()
    original_matrix["cases"].extend({"id": name, "timeout_s": 30} for name in order)
    monkeypatch.setattr(journeys.acceptance, "load_matrix", lambda: original_matrix)

    def execute(case_id, case_settings, _timeout):
        ensure_runtime_dirs(case_settings)
        result = journeys.RUNNERS[case_id](case_settings)
        shutil.rmtree(case_settings.home)
        return {**result, "cleanup_clear": True, "execution_observed": True}

    monkeypatch.setattr(deterministic, "run_case", execute)
    report = journeys.run_deterministic_suite(settings, order)
    assert report["status"] == "PASS", report
    assert len(set(homes)) == 2 and all(not home.exists() for home in homes)
    assert all(not home.is_relative_to(settings.home.parent) for home in homes)
