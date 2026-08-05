"""Объявленный код происшествия обязан либо срабатывать, либо стоять с причиной.

Список кодов человек читает как перечень того, за чем система следит. Код с
текстом и тяжестью, который не выставляется ни на одном ходу, — это обещание без
механизма, и оно ХУЖЕ отсутствия обещания: на него ссылаются. У такой мёртвой
цепочки два конца, и считать надо оба — кто пишет и кто читает.

Найдено чтением 2026-08-05: из девяти объявленных кодов детектора не было у
двух. Один (`structural_softened`) снят вовсе — пути, которым он мог бы
сработать, в системе нет; второй (`correction_not_applied`) оставлен с
записанной причиной, потому что мерить его пока нечем.
"""

from __future__ import annotations

import inspect

from friday.organs.compactor import (
    _INCIDENT_TEXT,
    _SEVERITY,
    _WITHOUT_A_DETECTOR,
    incident_text,
    incidents_of_a_turn,
)


def _codes_the_detector_can_emit() -> set[str]:
    """Коды, ЛИТЕРАЛЬНО названные в теле детектора.

    Читается исходный текст, а не список констант: код может лежать в словаре и
    не выставляться ниоткуда — ровно это и случилось.
    """

    source = inspect.getsource(incidents_of_a_turn)
    return {code for code in _INCIDENT_TEXT if f'"{code}"' in source}


def test_every_declared_code_is_emitted_or_explained() -> None:
    emitted = _codes_the_detector_can_emit()
    orphans = sorted(set(_INCIDENT_TEXT) - emitted - set(_WITHOUT_A_DETECTOR))
    assert not orphans, (
        f"эти коды объявлены, но не выставляются ничем: {orphans}. "
        "Либо детектор, либо запись в _WITHOUT_A_DETECTOR с причиной."
    )


def test_the_exemption_list_carries_a_reason_and_nothing_stale() -> None:
    for code, reason in _WITHOUT_A_DETECTOR.items():
        assert code in _INCIDENT_TEXT, f"{code} освобождён от детектора, но и не объявлен"
        assert len(str(reason).strip()) > 40, f"у {code} причина не записана, а обозначена"
    # Освобождение — не навсегда: код, у которого детектор появился, обязан
    # уйти из списка, иначе список превратится в свалку «когда-нибудь».
    emitted = _codes_the_detector_can_emit()
    stale = sorted(set(_WITHOUT_A_DETECTOR) & emitted)
    assert not stale, f"у этих кодов детектор уже есть, освобождение устарело: {stale}"


def test_text_and_severity_describe_the_same_set() -> None:
    """Код с текстом, но без тяжести (или наоборот) — половина объявления."""

    assert set(_INCIDENT_TEXT) == set(_SEVERITY)


def test_a_removed_code_is_gone_from_everywhere() -> None:
    """`structural_softened` снят целиком, а не забыт в одном из словарей."""

    assert "structural_softened" not in _INCIDENT_TEXT
    assert "structural_softened" not in _SEVERITY
    assert "structural_softened" not in _WITHOUT_A_DETECTOR
    # И в детекторе его тоже нет — иначе он выставлялся бы кодом без текста, и
    # сводка показала бы человеку голый идентификатор.
    assert "structural_softened" not in inspect.getsource(incidents_of_a_turn)


def test_an_unknown_code_reads_as_itself_not_as_a_crash() -> None:
    """Сводки лежат в базе кодами: код прежней редакции не должен ронять чтение."""

    assert incident_text("code_from_a_past_life") == "code_from_a_past_life"


def test_the_detector_still_fires_on_a_real_turn() -> None:
    """Сторож не должен быть выполним пустым детектором."""

    fired = incidents_of_a_turn({"grounding_warning": True, "structural": {"llm_failed": True}})
    assert set(fired) == {"claimed_archive_without_data", "model_silent"}
    assert all(incident_text(code) != code for code in fired), "у сработавшего кода нет формулировки"


def test_severity_values_are_from_the_known_three() -> None:
    """Читающая сторона красит значок по тяжести: четвёртое значение — серый значок."""

    assert set(_SEVERITY.values()) <= {"high", "medium", "low"}
