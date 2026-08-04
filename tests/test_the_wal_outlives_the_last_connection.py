"""Файлы WAL переживают закрытие последнего соединения.

Живой экземпляр упал 2026-08-05 в 00:22:29 внутри libsqlite3 с сигналом 7
(SIGBUS). Адрес обращения `0x79f73fdcd000` лежал внутри отображения
`79f73fdc9000-79f73fdd1000 rw-s … jericho.sqlite3-shm`, то есть файл общей памяти
WAL стал короче собственного отображения. В ту же минуту по той же базе шёл
проход `retag-documents --apply` вторым процессом.

Механизм у SQLite документирован: закрываясь последним, соединение удаляет `-wal`
и `-shm`. Пока к базе ходит один процесс, это безобидная уборка. Как только их
два — а у нас служба плюс проходы CLI, — второй процесс получает отображение в
никуда и падает по SIGBUS. Служба поднялась сама через 19 секунд, база уцелела,
но запросы человека в эти секунды оборвались.

`PRAGMA persist_wal=1` отменяет уборку. Проверяется здесь, потому что тест на сам
SIGBUS написать нельзя: он убивает процесс теста вместе с ошибкой.
"""

from __future__ import annotations

from friday.storage import FridayStorage


def test_the_pragma_is_set_on_every_connection(storage: FridayStorage) -> None:
    """Не на первой, а на КАЖДОЙ: соединения тут по одному на поток."""

    assert int(storage.execute("PRAGMA persist_wal").fetchone()[0]) == 1


def test_the_shm_file_survives_closing_the_last_connection(tmp_path) -> None:
    """Главное свойство: файлы WAL остаются на месте после закрытия.

    Именно их исчезновение и вырывало отображение из-под соседнего процесса.
    """

    from friday.config import FridaySettings
    from friday.storage import init_storage

    settings = FridaySettings(home=tmp_path, api_token="t" * 48)
    first = init_storage(settings)
    first.execute("SELECT 1").fetchone()
    database = tmp_path / "data" / "state" / "jericho.sqlite3"
    assert database.exists(), sorted(p.name for p in (tmp_path / "data" / "state").iterdir())
    shm = database.with_name(database.name + "-shm")
    assert shm.exists(), "WAL не включён — тест проверяет не то, что думает"

    first.close(final=True)

    assert shm.exists(), "-shm удалён при закрытии: соседний процесс получит SIGBUS"
