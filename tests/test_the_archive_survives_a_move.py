"""Перенос на другую машину ломал «открыть оригинал» у КАЖДОГО документа.

Замерено на живой базе: у всех 1671 импортированного raw-объекта в метаданных лежали
АБСОЛЮТНЫЕ пути (3342 штуки, ни одного относительного), укоренённые в прежнем каталоге.
Отдача файла требует, чтобы путь лежал внутри текущего хранилища, поэтому после смены
`JERICHO_HOME`, переезда на другой диск или даже смены имени пользователя каждый файл
отдаёт 404 — причём «безопасный» 404 неотличим от «файла нет». Это полный отказ, а не
деградация, и узнаётся он только по клику.

Побочный эффект того же корня: дедупликация при повторном импорте проверяет
существование файла по тому же пути, и после переезда те же документы легли бы в
хранилище вторым экземпляром (+684 МБ на этом архиве).
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from jericho.api.deps import _safe_owned_file


@pytest.fixture
def storage_root(tmp_path):
    root = tmp_path / "files"
    (root / "owner" / "ab").mkdir(parents=True)
    target = root / "owner" / "ab" / "abcdef.txt"
    target.write_text("содержимое", encoding="utf-8")
    return root, target


def test_a_relative_path_resolves_against_the_current_storage(storage_root):
    """Правильная форма: после переезда она продолжает работать."""
    root, target = storage_root
    assert _safe_owned_file(root, "owner/ab/abcdef.txt") == target.resolve()


def test_an_absolute_path_still_works_for_rows_written_before(storage_root):
    """Их 3342 — заставлять человека править JSON в SQLite ради нашей ошибки нельзя."""
    root, target = storage_root
    assert _safe_owned_file(root, str(target)) == target.resolve()


def test_escaping_the_storage_root_is_still_refused(storage_root):
    """Относительная форма не должна открыть дорогу наружу."""
    root, _ = storage_root
    for candidate in ("../secrets.txt", "owner/../../secrets.txt", "/etc/passwd"):
        with pytest.raises(HTTPException):
            _safe_owned_file(root, candidate)


def test_an_empty_path_is_refused(storage_root):
    root, _ = storage_root
    with pytest.raises(HTTPException):
        _safe_owned_file(root, "")


# --- починка уже записанных строк ---------------------------------------------


def _raw_with_path(storage, user_id: str, stored: str, source: str = "") -> str:
    import hashlib

    from jericho.storage.models import RawObject, new_id

    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="file",
        source_ref=new_id("s"),
        raw_content="тело",
        content_type="text",
        content_hash=hashlib.sha256(stored.encode()).hexdigest(),
        metadata_json={"stored_path": stored, **({"import_source_path": source} if source else {})},
    )
    storage.store_raw_object(raw)
    return raw.id


def test_absolute_paths_inside_the_storage_become_relative(storage, settings):
    storage.ensure_user("alice")
    root = str(settings.files_dir)
    raw_id = _raw_with_path(storage, "alice", f"{root}/alice/ab/abcdef.txt")

    report = storage.relativize_stored_paths(root)

    assert report["changed"] == 1
    stored = json.loads(storage.get_raw_object(raw_id, "alice")["metadata_json"])
    assert stored["stored_path"] == "alice/ab/abcdef.txt"


def test_a_path_outside_the_storage_is_left_alone(storage, settings):
    """Он либо чужой, либо след прошлого переезда. Молча делать его относительным
    значило бы соврать о том, где лежит файл."""
    storage.ensure_user("alice")
    raw_id = _raw_with_path(storage, "alice", "/mnt/old-disk/files/alice/ab/abcdef.txt")

    storage.relativize_stored_paths(str(settings.files_dir))

    stored = json.loads(storage.get_raw_object(raw_id, "alice")["metadata_json"])
    assert stored["stored_path"] == "/mnt/old-disk/files/alice/ab/abcdef.txt"


def test_the_provenance_path_is_not_touched(storage, settings):
    """`import_source_path` — это ОТКУДА файл пришёл, а не где лежит. Он и должен
    остаться таким, каким был на исходной машине."""
    storage.ensure_user("alice")
    root = str(settings.files_dir)
    raw_id = _raw_with_path(
        storage, "alice", f"{root}/alice/ab/abcdef.txt", source="/run/media/флешка/архив/приказ.docx"
    )

    storage.relativize_stored_paths(root)

    stored = json.loads(storage.get_raw_object(raw_id, "alice")["metadata_json"])
    assert stored["stored_path"] == "alice/ab/abcdef.txt"
    assert stored["import_source_path"] == "/run/media/флешка/архив/приказ.docx"


def test_running_the_repair_twice_changes_nothing(storage, settings):
    storage.ensure_user("alice")
    root = str(settings.files_dir)
    _raw_with_path(storage, "alice", f"{root}/alice/ab/abcdef.txt")

    assert storage.relativize_stored_paths(root)["changed"] == 1
    assert storage.relativize_stored_paths(root)["changed"] == 0
