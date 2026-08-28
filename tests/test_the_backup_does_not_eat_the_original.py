"""Сохранность там, где ошибка стоит всего архива.

Три дефекта третьего аудита, каждый по-своему превращает страховку в потерю:

- сорвавшийся restore удалял живую базу и сообщал «предыдущей базы не
  существовало» — прямая ложь ровно в тот момент, когда база была;
- `prune_backups` обещал оставлять «verified» копии и не проверял ничего:
  испорченная новая вытесняла исправную старую;
- зеркало оригиналов сверяло только размер, поэтому один перевёрнутый байт
  оставался в копии навсегда и уезжал offsite как целый.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from friday.backup_files import backup_files_incremental


def _write(path: Path, payload: bytes) -> Path:
    """Положить файл под тем именем, под которым его хранит Friday: sha256."""
    digest = hashlib.sha256(payload).hexdigest()
    target = path / digest[:2] / f"{digest}.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def test_a_silently_corrupted_copy_is_found_and_repaired(tmp_path):
    """Мутация: вернуть сравнение только по размеру — тест краснеет.

    Замерено: в скопированном файле перевёрнут один байт без изменения длины, и
    следующий прогон отчитался `{'copied': 0, 'failed': 0, 'complete': True}` —
    документ остался испорченным навсегда. Усечение чинилось, порча — нет.
    """
    files_dir, target_dir = tmp_path / "files", tmp_path / "backup"
    files_dir.mkdir()
    source = _write(files_dir, "договор на двадцать страниц".encode() * 40)

    first = backup_files_incremental(files_dir, target_dir)
    assert first["copied"] == 1 and first["complete"] is True

    copy = target_dir / source.relative_to(files_dir)
    payload = bytearray(copy.read_bytes())
    payload[17] ^= 0xFF  # один байт, длина та же
    copy.write_bytes(bytes(payload))

    second = backup_files_incremental(files_dir, target_dir)
    assert second["repaired"] == 1, "испорченная копия не замечена"
    assert second["copied"] == 1
    assert copy.read_bytes() == source.read_bytes(), "копия осталась испорченной"


def test_an_untouched_copy_is_not_rewritten(tmp_path):
    """Контроль: проверка не превращает инкремент в полное копирование."""
    files_dir, target_dir = tmp_path / "files", tmp_path / "backup"
    files_dir.mkdir()
    _write(files_dir, "приказ".encode())

    backup_files_incremental(files_dir, target_dir)
    again = backup_files_incremental(files_dir, target_dir)
    assert again["copied"] == 0
    assert again["repaired"] == 0
    assert again["complete"] is True


def test_a_corrupted_original_never_overwrites_a_good_copy(tmp_path):
    """Порча оригинала не должна уезжать в бэкап и подтверждаться как целая.

    Зеркало сверяло копию с локальным бэкапом, а не с содержимым, — то есть
    аккуратно увозило испорченное offsite.
    """
    files_dir, target_dir = tmp_path / "files", tmp_path / "backup"
    files_dir.mkdir()
    source = _write(files_dir, "скан акта".encode())
    backup_files_incremental(files_dir, target_dir)
    good = (target_dir / source.relative_to(files_dir)).read_bytes()

    source.write_bytes("скан акта, испорченный носителем".encode())

    result = backup_files_incremental(files_dir, target_dir)
    assert result["corrupt_sources"] == 1, "порча оригинала не замечена"
    assert result["complete"] is False, "прогон с испорченным оригиналом объявлен полным"
    assert (target_dir / source.relative_to(files_dir)).read_bytes() == good, (
        "испорченный оригинал перезаписал годную копию"
    )


def test_a_file_not_named_by_its_hash_is_still_copied(tmp_path):
    """Проверять нечем — но копировать всё равно надо: не всё дерево адресуемо."""
    files_dir, target_dir = tmp_path / "files", tmp_path / "backup"
    files_dir.mkdir()
    (files_dir / "README.txt").write_bytes("не content-addressed".encode())

    result = backup_files_incremental(files_dir, target_dir)
    assert result["copied"] == 1
    assert result["complete"] is True


# --- prune и restore --------------------------------------------------------


def _corrupt_page(path: Path) -> None:
    """Испортить страницу базы, не меняя её длины."""
    data = bytearray(path.read_bytes())
    for offset in range(4096, min(len(data), 8192)):
        data[offset] ^= 0xFF
    path.write_bytes(bytes(data))


def test_prune_keeps_the_verified_copy_not_merely_the_newest(storage):
    """Мутация: вернуть срез `backups[keep:]` без проверки — тест краснеет.

    Воспроизведено: испорчена страница в НОВЕЙШЕЙ копии, старая исправна.
    `prune_backups(keep=1)` удалял исправную и оставлял битую, а доктор всё это
    время печатал «Latest backup: verified» — он читает первый валидный манифест.
    """
    old = storage.create_backup(label="старая")
    new = storage.create_backup(label="новая")
    assert Path(new["path"]).exists()
    _corrupt_page(Path(new["path"]))
    assert storage.verify_backup(Path(new["path"]).name)["ok"] is False
    assert storage.verify_backup(Path(old["path"]).name)["ok"] is True

    result = storage.prune_backups(keep=1)

    assert result["unverified"] == 1
    assert Path(old["path"]).exists(), "удалена единственная годная копия"
    assert not Path(new["path"]).exists(), "битая копия оставлена как «новейшая»"


def test_prune_never_leaves_zero_backups(storage):
    """Битый архив лучше, чем никакого: удалить последнее — решение человека."""
    only = storage.create_backup(label="единственная")
    _corrupt_page(Path(only["path"]))

    result = storage.prune_backups(keep=1)

    assert Path(only["path"]).exists(), "воркер снёс последнюю копию"
    assert result["unverified"] == 1
    assert result["kept"] == 1


def test_a_restore_that_never_started_leaves_the_database_alone(storage, monkeypatch):
    """Мутация: убрать `if rollback_snapshots` из ветки отказа — тест краснеет.

    `restore_backup` первым делом снимает откатную копию активной базы. Любая
    ошибка на этом шаге — ENOSPC (restore требует тройного размера), EIO на
    умирающем диске, remount read-only — уводила в ветку отказа, где активные
    файлы удалялись БЕЗУСЛОВНО. Восстановление не начиналось, а базы больше не
    было; следующее обращение молча создавало пустую, и Friday поднималась с
    нулевым архивом вместо ошибки.
    """
    import friday.storage._maintenance as maintenance

    # Аренда процесса — отдельный предохранитель, и он к этому дефекту отношения
    # не имеет: проверяется поведение ПОСЛЕ того, как восстановление разрешено.
    monkeypatch.setattr(maintenance, "process_owns_lease", lambda path, protocol: True, raising=False)
    import friday.diagnostics.runtime_lease as runtime_lease

    monkeypatch.setattr(runtime_lease, "process_owns_lease", lambda path, protocol: True)

    storage.ensure_user("alice")
    backup = storage.create_backup(label="до-беды")
    database_path = Path(storage.settings.database_path)
    active_paths = (
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
    )
    before = {path: path.read_bytes() for path in active_paths if path.is_file()}
    recovery_before = set(storage.settings.backups_dir.glob("recovery-*"))
    close_calls = 0
    original_close = storage.close

    def _unexpected_close(*, final=False):  # noqa: ANN001, ANN202
        nonlocal close_calls
        close_calls += 1
        return original_close(final=final)

    monkeypatch.setattr(storage, "close", _unexpected_close)

    def _no_space(source, target):  # noqa: ANN001, ARG001
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(maintenance, "_stage_private_copy", _no_space)

    with pytest.raises(RuntimeError) as failure:
        storage.restore_backup(Path(backup["path"]).name)

    assert close_calls == 0, "preflight закрыл SQLite до durable restore intent"
    assert {path: path.read_bytes() for path in active_paths if path.is_file()} == before, (
        "DB/WAL/SHM изменились до начала restore"
    )
    assert set(storage.settings.backups_dir.glob("recovery-*")) == recovery_before
    assert not list(database_path.parent.glob("*.restore-*.tmp"))
    message = str(failure.value)
    assert "no previous database existed" not in message, "сообщение лжёт о судьбе базы"
    assert "left untouched" in message
