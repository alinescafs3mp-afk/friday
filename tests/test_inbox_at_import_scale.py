"""Очередь на две тысячи файлов человек не разберёт поштучно — и не должен.

Живой замер того импорта: 66 поступлений, из них 36 нечитаемых картинок, 7
base64-дампов и 4 связных документа. Классификатор посоветовал `promote`
шестидесяти шести из шестидесяти шести — включая те 36, из которых не прочитано
ни байта, — потому что совет выводится из `promotion_score`, а тот дал
p25 = median = p75 = 0.90 и не разделил ничего.

Разделяющий признак при этом БЫЛ посчитан и лежал рядом: `quality_score` дал 0.13
нечитаемым, ровно 0.198 дампам и 0.88–0.996 связным текстам. Он не участвовал ни
в сортировке, ни в группировке, ни в совете. Цена — 65 решений руками, принятых
пачками за полторы минуты, потому что другого хода интерфейс не давал.

Здесь проверяется, что этот признак теперь виден: как отдельный разрез и как
характеристика ЛЮБОЙ группы, по какой бы оси её ни резали.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

# Три класса материала того самого импорта, с настоящими оценками качества.
CORPUS = (
    ("QR-codes/qr-%d.png", "image/png", 0.13, 36),
    ("Base64/dump-%d.txt", "text/plain", 0.198, 7),
    ("README-%d.md", "text/markdown", 0.94, 4),
)


def _pending(storage, user_id: str, path: str, mime: str, quality: float) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content="содержимое" * 20,
        content_type="file",
        content_hash=hashlib.sha256(path.encode()).hexdigest(),
        metadata_json={"import_source_path": f"/home/owner/архив/{path}", "mime_type": mime},
    )
    storage.store_raw_object(raw)
    item = InboxItem(
        id=new_id("inb"),
        user_id=user_id,
        raw_object_id=raw.id,
        status=InboxStatus.PENDING,
        quality_score=quality,
        # Тот самый совет: одинаковый у всех, включая нечитаемое.
        suggested_action="promote",
        promotion_score=0.9,
    )
    storage.store_inbox_item(item)
    return item.id


@pytest.fixture
def imported(storage):
    storage.ensure_user("owner")
    for template, mime, quality, count in CORPUS:
        for index in range(count):
            _pending(storage, "owner", template % index, mime, quality)
    return storage


def test_quality_is_a_grouping_axis_because_it_is_the_one_that_separates(imported):
    groups = imported.group_pending_inbox("owner", by="quality")["groups"]
    by_key = {group["key"]: group["total"] for group in groups}

    assert by_key == {"0.00–0.25 нечитаемое": 43, "0.75–1.00 содержательное": 4}, (
        f"качество не разложило материал по полкам: {by_key}"
    )


def test_the_advice_column_alone_would_have_said_nothing(imported):
    """Проверяется именно бесполезность прежнего сигнала — иначе непонятно, зачем правка."""
    groups = imported.group_pending_inbox("owner", by="extension")["groups"]
    advice = {key for group in groups for key in group["actions"]}
    assert advice == {"promote"}, (
        "совет перестал быть одинаковым — тогда этот тест надо переписать, а не удалять"
    )


def test_every_group_carries_its_quality_whatever_the_axis(imported):
    """Вопрос «смотреть или сносить» задаётся при любом разрезе, не только по качеству."""
    for axis in ("extension", "directory", "source"):
        groups = imported.group_pending_inbox("owner", by=axis)["groups"]
        assert groups, f"ось {axis} не дала групп"
        for group in groups:
            for field in ("quality_min", "quality_median", "quality_max"):
                assert field in group, f"ось {axis}: в группе нет {field}"
            assert group["quality_min"] <= group["quality_median"] <= group["quality_max"]


def test_the_junk_group_is_actionable_in_one_decision(imported):
    """Смысл разреза: 43 бесполезных файла отклоняются одним действием, а не 43."""
    groups = imported.group_pending_inbox("owner", by="quality")["groups"]
    junk = next(group for group in groups if group["key"].startswith("0.00"))

    assert junk["total"] == 43
    assert len(junk["inbox_ids"]) == 43, "идентификаторы не отданы — групповое действие невозможно"
    assert junk["truncated"] is False
    assert junk["quality_max"] < 0.25


def test_a_group_larger_than_the_id_budget_says_so(imported):
    """Иначе «отклонить группу» тихо тронет часть, а человек будет думать, что всю."""
    groups = imported.group_pending_inbox("owner", by="quality", limit_ids=10)["groups"]
    junk = next(group for group in groups if group["key"].startswith("0.00"))

    assert junk["total"] == 43
    assert len(junk["inbox_ids"]) == 10
    assert junk["truncated"] is True


def test_an_unscored_item_sinks_to_the_junk_band(storage):
    """Неоценённое обязано оседать к мусору, а не всплывать наверх.

    Подстановка здесь ноль, поэтому `or` и явная проверка на None дают одно и то
    же — распространённая ловушка falsy-нуля тут НЕ срабатывает. Закрепляется само
    правило («нет оценки = худшая»), а не форма записи.
    """
    storage.ensure_user("owner")
    _pending(storage, "owner", "битый.bin", "application/octet-stream", 0.0)
    groups = storage.group_pending_inbox("owner", by="quality")["groups"]

    assert groups[0]["key"] == "0.00–0.25 нечитаемое"
    assert groups[0]["quality_median"] == 0.0


def test_the_bands_are_fixed_not_quantiles(storage):
    """Полосы обязаны значить одно и то же на разных партиях.

    С квартилями «принять всё выше 0.75» означало бы разное вчера и сегодня, и
    решение нельзя было бы повторить.
    """
    assert storage.quality_band(0.13) == storage.quality_band(0.198)
    assert storage.quality_band(0.95) == storage.QUALITY_TOP_BAND
    assert storage.quality_band(None) == "0.00–0.25 нечитаемое"
    assert storage.quality_band(0.25) != storage.quality_band(0.24)


def test_the_route_offers_the_axis(settings):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]
        storage.ensure_user(user_id)
        _pending(storage, user_id, "qr.png", "image/png", 0.13)
        _pending(storage, user_id, "readme.md", "text/markdown", 0.94)

        response = client.get(f"/api/admin/inbox/groups?user_id={user_id}&by=quality", headers=owner)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "quality" in payload["axes"]
        assert payload["axis"] == "quality"
        assert {group["key"] for group in payload["groups"]} == {
            "0.00–0.25 нечитаемое",
            "0.75–1.00 содержательное",
        }


def test_a_truncated_answer_says_so_instead_of_blaming_the_model():
    """Обрыв по бюджету и мусор вместо JSON — разные беды; сообщение было одно.

    Замерено на этой установке 2026-07-29: 249 сбоёв «Local model did not return a
    JSON object» подряд. Модель была ни при чём — ей нужно 2516–3616 токенов
    (9 прогонов на документах в 1, 6 и 14 тысяч знаков), потому что до JSON она
    рассуждает, а потолок стоял 512. Ответ обрывался посреди мысли, фигурной скобки
    в нём не оказывалось ни одной, и журнал уверенно указывал не туда — на формат
    ответа вместо настройки. Диагноз обязан называть причину, которую можно устранить.
    """
    import pytest

    from friday.ingestion._base import _parse_model_response

    with pytest.raises(ValueError, match="cut off by the token budget"):
        _parse_model_response(
            {
                "content": "Сначала разберёмся, что это за документ и насколько он",
                "finish_reason": "length",
                "usage": {"completion_tokens": 512},
            }
        )

    # Дописанный до конца ответ разбирается как раньше, а дописанный мусор
    # по-прежнему винит модель — иначе новая ветка съела бы старую диагностику.
    assert _parse_model_response({"content": '{"title": "Протокол"}', "finish_reason": "stop"}) == {
        "title": "Протокол"
    }
    with pytest.raises(ValueError, match="did not return a JSON object"):
        _parse_model_response({"content": "просто текст", "finish_reason": "stop"})


def test_the_token_ceiling_admits_a_reasoning_model():
    """Потолок — настройка, и его значение по умолчанию должно быть достижимым.

    Числа из замера: максимум 3616 токенов на ответ. Значение по умолчанию обязано
    быть выше, иначе установка с рассуждающей моделью не работает «из коробки» и
    ломается молча — ошибкой разбора, а не отказом на старте.
    """
    from friday.config import load_settings

    assert load_settings().cognition_max_tokens >= 3616
