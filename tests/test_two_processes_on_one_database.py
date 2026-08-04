"""Проход, пишущий в базу, обязан знать, что её держит живая служба.

Живой экземпляр упал 2026-08-05 в 00:22:29: сигнал 7 (SIGBUS), стек целиком
внутри libsqlite3, адрес обращения `0x79f73fdcd000` — внутри отображения
`79f73fdc9000-79f73fdd1000 rw-s … jericho.sqlite3-shm`. Файл общей памяти WAL
стал короче собственного отображения. В ту же минуту по той же базе шёл проход
`retag-documents --apply` вторым процессом. База уцелела (`integrity_check` =
ok), служба поднялась сама через 19 секунд, но запросы человека в эти секунды
оборвались.

Прагмой это не лечится: `PRAGMA persist_wal` в SQLite НЕТ, неизвестные прагмы
молча игнорируются — проверено исполнением, файлы `-wal`/`-shm` после закрытия
удаляются как ни в чём не бывало. Поэтому лечение честное: сказать человеку, что
он делает, и чем это грозит.

Тест на сам SIGBUS написать нельзя — он убивает процесс теста вместе с ошибкой.
Проверяется то, что проверяемо: датчик и то, что предупреждение доходит.
"""

from __future__ import annotations

from friday.cli import warn_if_service_holds_the_database


def test_a_quiet_database_is_not_reported_as_busy(storage) -> None:
    """Пульса нет — служба не запущена, и пугать человека нечем."""

    assert storage.live_service_heartbeat_age() is None
    assert warn_if_service_holds_the_database(storage, action="переписывать теги") is False


def test_a_fresh_heartbeat_means_the_service_holds_the_database(storage) -> None:
    storage.kv_set("workers:health:embeddings_index", '{"enabled": true}')

    age = storage.live_service_heartbeat_age()
    assert age is not None
    assert age < 60


def test_an_old_heartbeat_is_not_a_running_service(storage) -> None:
    """Отметка недельной давности говорит о прошлом запуске, а не о текущем."""

    storage.kv_set("workers:health:embeddings_index", '{"enabled": true}')
    storage.execute(
        "UPDATE runtime_kv SET updated_at='2026-01-01T00:00:00+00:00' WHERE key=?",
        ("workers:health:embeddings_index",),
    )
    storage.conn.commit()

    assert storage.live_service_heartbeat_age() is None


def test_the_warning_reaches_the_person(storage, capsys) -> None:
    """Предупреждение идёт в stderr и называет и причину, и выход из неё."""

    storage.kv_set("workers:health:embeddings_index", '{"enabled": true}')

    assert warn_if_service_holds_the_database(storage, action="переписывать теги") is True
    printed = capsys.readouterr().err
    assert "живая служба" in printed
    assert "SIGBUS" in printed
    assert "systemctl --user stop friday-backend" in printed
