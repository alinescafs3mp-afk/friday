"""Набор типов сущностей — решение, а не список, куда дописывают по вкусу.

Каждый тип обязывает: он появляется в очереди ревью, в фильтрах графа, в подсказках
извлечения. Спецификация V2 просила типы Risk и Metric, и они были ЗАМЕРЕНЫ и
отклонены — правилами 4% и 3% точности, локальной моделью 40% и 46%, с поправкой
«рядом число с единицей» 92% на 13 находках, но 50% на 48. Порог (точность выше 75%
на выборке не менее 30) объявлялся до замера; числа закреплены в комментарии у
`EntityType`.

Тест существует, чтобы вернуть их можно было только осознанно: дописать член в enum
мимо этого файла нельзя, а поправив его, автор прочитает, чем кончился прошлый заход,
и предъявит свой замер вместо чужой веры.
"""

from __future__ import annotations

from friday.storage.models import EntityType

EXPECTED_ENTITY_TYPES = {
    "person",
    "project",
    "concept",
    "event",
    "organization",
    "location",
    "document",
    "collection",
    "other",
}

# Замерены и отклонены. Возвращать — только с новым замером, побившим прежний.
MEASURED_AND_REJECTED = {"risk", "metric"}


def test_entity_types_are_pinned():
    actual = {member.value for member in EntityType}
    assert actual == EXPECTED_ENTITY_TYPES, (
        f"набор типов сущностей изменился: {sorted(actual)}. "
        "Правь EXPECTED_ENTITY_TYPES, только если тип добавлен или убран намеренно."
    )


def test_rejected_types_did_not_come_back_unmeasured():
    actual = {member.value for member in EntityType}
    returned = actual & MEASURED_AND_REJECTED
    assert not returned, (
        f"тип(ы) {sorted(returned)} были замерены и отклонены: точность 40% и 46% "
        "при объявленном пороге 75%. Если замер устарел — приложи новый и убери "
        "тип из MEASURED_AND_REJECTED вместе с обоснованием."
    )
