"""Экран ревизии не должен переклассифицировать весь архив на каждый запрос.

Замерено на боевом корпусе владельца (1533 документа, 25.5 млн символов): один
запрос страницы — 17.9 с, из них 15.1 с в `re.search`, ~30 паттернов на документ.
Полный обход здесь осознан — предикат читает содержимое и не выражается в SQL, а
счётчик, расходящийся со своей страницей, хуже медленной страницы. Но обход
платился заново за вторую страницу и заново завтра на неизменившемся архиве.

Здесь проверяются две правки, и обе не меняют ОТВЕТ:

* вердикт помнится по ревизии объекта (`updated_at` + длина) — это тот же вердикт,
  посчитанный один раз;
* «текст из трёх слов» решается на четвёртом уникальном слове, а не после
  casefold всех 4.7 млн (проверено на боевом корпусе: 0 расхождений из 1533).
"""

from __future__ import annotations

import pytest

from friday.ingestion import IngestionPipeline
from friday.ingestion._base import _has_few_distinct_words
from friday.knowledge_graph import KnowledgeGraph


@pytest.fixture()
def pipeline(settings, storage) -> IngestionPipeline:
    return IngestionPipeline(settings, storage, KnowledgeGraph(storage))


def _knowledge(storage, user_id: str, text: str) -> str:
    import hashlib

    from friday.storage.models import KnowledgeObject, RawObject, new_id

    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    item = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        title=text[:40],
        content=text,
        content_type="text",
    )
    storage.store_knowledge_object(item)
    return item.id


def test_the_same_object_is_classified_once(storage, pipeline):
    """Мутация: вернуть `self.assess_text(content)` напрямую — тест краснеет."""
    storage.ensure_user("alice")
    item_id = _knowledge(storage, "alice", "Договорились перенести релиз на пятницу, отвечает Иванов.")
    stored = storage.get_knowledge_object(item_id, "alice")

    calls: list[str] = []
    original = pipeline.assess_text

    def counting(content: str, **kwargs):
        calls.append(content)
        return original(content, **kwargs)

    pipeline.assess_text = counting  # type: ignore[method-assign]
    first = pipeline.assess_existing_knowledge("alice", stored)
    second = pipeline.assess_existing_knowledge("alice", stored)

    assert len(calls) == 1, f"неизменившийся объект классифицирован заново: {len(calls)} раз"
    assert first["risk_score"] == second["risk_score"]
    assert first["suspect"] == second["suspect"]


def test_an_edited_object_is_classified_again(storage, pipeline):
    """Память по ревизии, а не навсегда: правка содержимого обязана дать новый вердикт."""
    storage.ensure_user("alice")
    item_id = _knowledge(storage, "alice", "Договорились перенести релиз на пятницу, отвечает Иванов.")
    stored = storage.get_knowledge_object(item_id, "alice")
    before = pipeline.assess_existing_knowledge("alice", stored)

    storage.update_knowledge_fields(item_id, "alice", content="ну ок")
    edited = storage.get_knowledge_object(item_id, "alice")
    after = pipeline.assess_existing_knowledge("alice", edited)

    assert after["knowledge_object"]["content"] == "ну ок"
    assert after["risk_score"] != before["risk_score"], (
        "после правки содержимого показан прежний вердикт — память переживает объект"
    )


def test_two_objects_do_not_share_a_verdict(storage, pipeline):
    """Ключ — идентификатор объекта, а не его длина."""
    storage.ensure_user("alice")
    chatter = storage.get_knowledge_object(_knowledge(storage, "alice", "а что там?"), "alice")
    fact = storage.get_knowledge_object(
        _knowledge(storage, "alice", "Сервер oscar использует PostgreSQL 16, релиз 3 мая 2025 года."),
        "alice",
    )

    assert (
        pipeline.assess_existing_knowledge("alice", chatter)["risk_score"]
        != pipeline.assess_existing_knowledge("alice", fact)["risk_score"]
    ), "разным объектам достался один вердикт"


def test_few_distinct_words_answers_exactly_as_the_set_did():
    """Ранний выход обязан совпадать с прежней формулой на каждом входе."""
    cases = [
        [],
        ["да"],
        ["да", "да", "да", "да", "да"],
        ["раз", "два", "три"],
        ["раз", "два", "три", "четыре"],
        ["Раз", "раз", "РАЗ", "два"],
        ["слово"] * 500,
        [*(f"слово{index}" for index in range(50))],
    ]
    for words in cases:
        assert _has_few_distinct_words(words) == (len({word.casefold() for word in words}) <= 3), words


def test_few_distinct_words_stops_early():
    """Мутация: убрать ранний выход — тест краснеет (весь список будет прочитан)."""

    class Counting(str):
        seen = 0

        def casefold(self) -> str:  # noqa: D102
            Counting.seen += 1
            return str.casefold(self)

    words = [Counting("раз"), Counting("два"), Counting("три"), Counting("четыре")]
    words.extend(Counting(f"ещё{index}") for index in range(1000))
    assert _has_few_distinct_words(words) is False
    assert Counting.seen <= 8, f"прочитано {Counting.seen} слов там, где хватает четырёх"
