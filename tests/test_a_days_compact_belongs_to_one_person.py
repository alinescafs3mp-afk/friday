"""Сводка за сутки: чья она, сколько её и что остаётся от оборванной ночи.

Заказ владельца 2026-08-04, проект — `artifacts/compactor_design.md`. Здесь
проверяется хранилище, а не сборка: три свойства, каждое из которых на этом
проекте уже было причиной дефекта.

ЛИЧНОСТЬ, А НЕ АРЕНДАТОР. В общем архиве корпус общий, а переписка личная. Класс
закрывался трижды — в правилах, поправках и заявках на подтверждение, — и каждый
раз выяснялось, что `user_id` здесь означает не человека.

ИДЕМПОТЕНТНОСТЬ ПО ПОСТРОЕНИЮ. Не «проверяем перед вставкой», а UNIQUE плюс
UPSERT: дубль создать нечем. Проверка перед вставкой — это гонка, и она уже
ловилась на этом проекте.

СЛЕД ОТ ОБОРВАННОЙ НОЧИ. Запись «начал» ставится ДО работы. Оборванная пара
«начал / нет конца» сама доказывает незавершённость; запись после работы не
оставляет следа вовсе, и оборванная ночь неотличима от ночи без происшествий.
"""

from __future__ import annotations


def test_a_compact_belongs_to_the_person_not_the_tenant(storage) -> None:
    """Мутация: писать по арендатору — сводки двух людей сливаются.

    Проверяются ОБЕ учётки, и это существенно: проверка «у сказавшего появилось»
    прошла бы и на сломанном коде.
    """
    for name in ("person-a", "person-b"):
        storage.ensure_user(name)

    first = storage.begin_day_compact("person-a", "2026-08-03")
    storage.finish_day_compact(
        first, source_turns=7, counters={"total_turns": 7}, incidents=[], patterns=[]
    )
    second = storage.begin_day_compact("person-b", "2026-08-03")
    storage.finish_day_compact(
        second, source_turns=2, counters={"total_turns": 2}, incidents=[], patterns=[]
    )

    mine = storage.get_day_compact("person-a", "2026-08-03")
    yours = storage.get_day_compact("person-b", "2026-08-03")
    assert mine["counters"]["total_turns"] == 7
    assert yours["counters"]["total_turns"] == 2
    assert storage.count_day_compacts("person-a") == 1


def test_a_second_run_of_the_same_day_makes_no_duplicate(storage) -> None:
    """Повторный прогон за те же сутки — одна строка, а не две.

    Идемпотентность здесь по построению: UNIQUE(principal, local_date). Мутация
    «снять UNIQUE и вставлять» краснеет, потому что счётчик строк удвоится.
    """
    storage.ensure_user("alice")

    first = storage.begin_day_compact("alice", "2026-08-03")
    storage.finish_day_compact(
        first, source_turns=5, counters={"total_turns": 5}, incidents=[], patterns=[]
    )
    again = storage.begin_day_compact("alice", "2026-08-03")
    storage.finish_day_compact(
        again, source_turns=5, counters={"total_turns": 5}, incidents=[], patterns=[]
    )

    assert again == first, "повторный прогон завёл вторую запись"
    assert storage.count_day_compacts("alice") == 1
    assert storage.get_day_compact("alice", "2026-08-03")["counters"]["total_turns"] == 5


def test_a_rerun_rebuilds_the_day_instead_of_adding_to_it(storage) -> None:
    """Сутки пересобираются целиком, а не дополняются.

    Иначе повторный прогон удваивал бы счётчики внутри одной строки — дубль без
    второй строки, который заметить труднее.
    """
    storage.ensure_user("alice")
    first = storage.begin_day_compact("alice", "2026-08-03")
    storage.finish_day_compact(
        first,
        source_turns=5,
        counters={"total_turns": 5},
        incidents=[{"code": "model_silent", "severity": "high", "count": 1}],
        patterns=[],
    )

    again = storage.begin_day_compact("alice", "2026-08-03")
    fresh = storage.get_day_compact("alice", "2026-08-03")

    assert fresh["status"] == "started", "прошлый результат остался на месте"
    assert fresh["counters"] == {}, "счётчики прошлой сборки пережили пересборку"
    assert fresh["incidents"] == []
    storage.finish_day_compact(
        again, source_turns=6, counters={"total_turns": 6}, incidents=[], patterns=[]
    )
    assert storage.get_day_compact("alice", "2026-08-03")["counters"]["total_turns"] == 6


def test_an_interrupted_night_leaves_a_trace(storage) -> None:
    """Оборванная сборка видна, и следующий прогон знает, что переделать.

    Мутация: ставить запись после работы — от оборванной ночи не остаётся
    ничего, и сутки наблюдений теряются молча.
    """
    storage.ensure_user("alice")

    compact_id = storage.begin_day_compact("alice", "2026-08-03")
    # Здесь процесс «умирает»: терминальной записи нет.

    left = storage.get_day_compact("alice", "2026-08-03")
    assert left is not None, "об оборванной ночи не осталось ни одной записи"
    assert left["status"] == "started"
    assert storage.days_needing_a_compact("alice", ["2026-08-03"]) == ["2026-08-03"], (
        "оборванная ночь считается готовой"
    )
    storage.abandon_day_compact(compact_id)
    assert storage.get_day_compact("alice", "2026-08-03")["status"] == "uncertain"


def test_a_finished_day_is_not_redone(storage) -> None:
    """Обратная сторона: готовые сутки не пересобираются каждый час.

    Без этого орган переделывал бы всю неделю при каждом тике — и счётчики
    менялись бы под наблюдателем без причины.
    """
    storage.ensure_user("alice")
    done = storage.begin_day_compact("alice", "2026-08-02")
    storage.finish_day_compact(
        done, source_turns=1, counters={}, incidents=[], patterns=[]
    )

    pending = storage.days_needing_a_compact("alice", ["2026-08-01", "2026-08-02"])

    assert pending == ["2026-08-01"], pending
