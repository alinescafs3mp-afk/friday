"""Сборка архива не выходит за хранилище и не читает файл, чтобы его отвергнуть.

Две занозы, найденные разбором Сола 2026-08-03 и подтверждённые проверкой.

ПЕРВАЯ. Принадлежность файла хранилищу проверялась сравнением НАЧАЛА СТРОКИ:
`str(source).startswith(str(root))`. При хранилище `/data/files` путь
`/data/files_backup/secret.pdf` такую проверку проходит — соседний каталог, чьё
имя начинается так же, границей не отделён вовсе. Проверено прямо:

    startswith('/data/files_backup/secret.pdf', '/data/files') -> True
    Path('/data/files_backup/secret.pdf').is_relative_to('/data/files') -> False

Путь берётся из базы, и это не оправдание: комментарий рядом сам объясняет, что
проверка стоит на случай записи, сделанной иначе. Значит она обязана работать.

ВТОРАЯ. Файл читался ЦЕЛИКОМ и лишь потом сверялся с потолком в 20 МБ. Файл на
несколько гигабайт сначала оказывался в памяти и только затем объявлялся не
поместившимся: потолок стоял, но защищал он архив, а не машину. Размер теперь
спрашивается у файловой системы до чтения.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from friday.execution_kernel import _MAX_ARCHIVE_BYTES, _pack_archive


def _rows(*names: str) -> list[dict]:
    return [{"stored_path": name, "filename": name.rsplit("/", 1)[-1]} for name in names]


def test_a_neighbour_directory_does_not_pass_as_the_store(tmp_path) -> None:
    """Мутация: вернуть сравнение начала строки — соседний каталог снова внутри."""
    store = tmp_path / "files"
    store.mkdir()
    (store / "свой.txt").write_bytes("свой".encode())
    neighbour = tmp_path / "files_backup"
    neighbour.mkdir()
    (neighbour / "чужой.txt").write_bytes("чужой".encode())

    payload, left_out, _size = _pack_archive(store, _rows("свой.txt", "../files_backup/чужой.txt"), name="a")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        inside = archive.namelist()
    assert inside == ["свой.txt"], f"в архив попало чужое: {inside}"
    assert any("чужой" in note for note in left_out), "про пропущенный файл человеку не сказано"


def test_a_parent_escape_is_still_refused(tmp_path) -> None:
    """Прежняя защита от `..` никуда не делась — правка её не ослабила."""
    store = tmp_path / "files"
    store.mkdir()
    (tmp_path / "секрет.txt").write_bytes("секрет".encode())

    payload, left_out, _size = _pack_archive(store, _rows("../секрет.txt"), name="a")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == []
    assert left_out


def test_a_huge_file_is_refused_without_being_read(tmp_path, monkeypatch) -> None:
    """Мутация: вернуть чтение до проверки — огромный файл снова съест память.

    Разреженный файл больше потолка создаётся мгновенно и места на диске не
    занимает. Чтение подменяется: если оно случится, тест это увидит.
    """
    store = tmp_path / "files"
    store.mkdir()
    huge = store / "огромный.bin"
    with huge.open("wb") as handle:
        handle.truncate(_MAX_ARCHIVE_BYTES + 1024)
    (store / "мелкий.txt").write_bytes("мелкий".encode())

    from pathlib import Path

    read_calls: list[str] = []
    original = Path.read_bytes

    def _watched(self):  # noqa: ANN001, ANN202
        read_calls.append(self.name)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", _watched)

    payload, left_out, _size = _pack_archive(store, _rows("огромный.bin", "мелкий.txt"), name="a")

    assert "огромный.bin" not in read_calls, "файл прочитан целиком, чтобы затем быть отвергнутым"
    assert "мелкий.txt" in read_calls, "нужный файл не прочитан — проверка потеряла смысл"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == ["мелкий.txt"]
    assert any("не поместился" in note for note in left_out)


def test_what_did_not_fit_is_named(tmp_path) -> None:
    """Молчаливый обрез запрещён: человек должен узнать, чего в архиве нет."""
    store = tmp_path / "files"
    store.mkdir()
    (store / "есть.txt").write_bytes("есть".encode())

    _payload, left_out, _size = _pack_archive(store, _rows("есть.txt", "нету.txt"), name="a")

    assert len(left_out) == 1 and "нету" in left_out[0]


@pytest.mark.parametrize("stored", ["", "   "])
def test_an_empty_path_is_not_the_store_itself(tmp_path, stored: str) -> None:
    """Пустой путь раскрывается в САМ каталог хранилища — он относителен себе.

    `is_relative_to` тут пропускает, и отсекает уже `is_file()`: каталог файлом не
    является. Проверка на случай, если порядок условий однажды поменяют местами.
    """
    store = tmp_path / "files"
    store.mkdir()

    payload, _left, _size = _pack_archive(store, [{"stored_path": stored, "filename": "x"}], name="a")

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == []
