"""Model-owned honesty turns stay explicit without weakening web grounding."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    _CANNOT_ACT_OUTSIDE,
    _UNVERIFIED_OUTSIDE_CONFIRMATION_PREFIX,
    _WEB_EVIDENCE_MISSING,
    AgentContext,
    AgentRuntime,
    _claims_current_answer_came_from_the_web,
    _has_explicit_web_provenance_claim,
    _requests_confirmation_of_unverified_outside_deed,
    _requests_to_fabricate_outside_deed,
)
from friday.permissions import ActorContext


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


class _NoToolKernel:
    authorization = None

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return []

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        raise AssertionError("an honesty-only turn reached a tool")


class _OneAnswerRouter:
    enabled = True
    total_budget_sec = 1.0

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": list(messages), "kwargs": dict(kwargs)})
        return {"content": self.answer}


async def _clean_context(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
    del message, kwargs
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=user_id,
        conversation_history=[],
        answer_mode="general_conversation",
        outward_verdict=("другое", None),
    )


def _stored_metadata(storage, reply: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN001
    row = storage.get_message(str(reply["message_id"]), "alice")
    assert row is not None
    return json.loads(str(row["metadata_json"] or "{}"))


@pytest.mark.parametrize(
    "answer",
    [
        "У меня нет доступа к данным интернет-магазина.",
        "Информацию из интернет-магазина не получала.",
        "Интернет-магазин недоступен, данных нет.",
        "Я не искала это в интернет-магазине.",
        "Не по данным из интернета заказ оформлен.",
        "Интернет не является источником информации.",
        "Интернет не был источником информации.",
        "Источником данных был не интернет.",
        "Сведения не были взяты из интернета.",
        "Не проверяла через интернет.",
        "Не проверяла онлайн.",
        "Не согласно интернет-источникам заказ оформлен.",
        "Согласно не интернет-источникам заказ оформлен.",
        "Интернет источником не был.",
        "Интернет источником не является.",
        "Интернет-магазин источником данных не является.",
        "Информацию из интернета я не получала.",
        "https://example.invalid/order не подтверждает оплату.",
        "Нет данных из интернет-магазина.",
        "У меня нет данных из интернет-магазина.",
        "Не могу проверить по данным интернет-магазина.",
        "Не могу подтвердить данные интернет-магазина.",
        "Информация взята не из открытых источников.",
        "Данные получены не из сети.",
        "Данные интернет-магазина отсутствуют.",
        "Интернет-магазин не предоставил данные.",
        "Источником информации интернет не являлся.",
        "Интернет нельзя считать источником информации.",
    ],
)
def test_a_denial_about_an_online_shop_is_not_web_provenance(answer: str) -> None:
    assert not _claims_current_answer_came_from_the_web(answer)
    assert not _has_explicit_web_provenance_claim(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "Информацию беру из интернета.",
        "По данным из интернета заказ уже оформлен.",
        "По данным интернет-магазина заказ оформлен.",
        "У меня нет доступа к интернет-магазину, но по данным из интернета заказ оформлен.",
        "У меня нет доступа к интернет-магазину, а информацию взяла из интернета.",
        "У меня нет доступа к интернет-магазину: информацию взяла из интернета.",
        "У меня нет доступа к интернет-магазину — информацию взяла из интернета.",
        "У меня нет доступа к интернет-магазину и информацию взяла из интернета.",
        "У меня нет доступа к интернет-магазину, хотя информацию я взяла из интернета.",
        "У меня нет доступа к интернет-магазину, тем не менее информацию я взяла из интернета.",
        "Я не искала в интернет-магазине, просто информацию взяла из интернета.",
        "Доступа нет и информацию взяла из интернета.",
        "Информацию взяла из интернета и доступа нет.",
        "По данным интернета заказ оформлен и доступа нет.",
        "У меня нет доступа к интернет-магазину, но https://example.invalid/order подтверждает оплату.",
        "Нет доступа, но https://example.invalid/weather говорит, что в Москве +20 °C.",
        "example.invalid подтверждает оплату.",
        "Нет данных, зато сведения взяла из интернета.",
        "Информацию из интернета не получала, однако по данным открытых источников заказ оформлен.",
        "Информацию из интернета не получала, но сведения взяла из интернета.",
        "Я не искала но информацию взяла из интернета.",
        "Информацию не проверяла но взяла из интернета.",
        "Сведения не получила зато нашла в интернете.",
        "Нет доступа, но на example.com написано: сейчас +20 °C.",
        "Нет доступа, но на example.com видно: сейчас +20 °C.",
        "example.com: сейчас +20 °C.",
        "Я не искала это в интернет-магазине; судя по открытым источникам, заказ оформлен.",
    ],
)
def test_an_adjacent_affirmative_web_claim_is_still_provenance(answer: str) -> None:
    assert _claims_current_answer_came_from_the_web(answer)
    assert _has_explicit_web_provenance_claim(answer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "По данным интернет-магазина заказ оформлен.",
        "У меня нет доступа к интернет-магазину, но https://example.invalid/order подтверждает оплату.",
        "example.invalid подтверждает оплату.",
    ],
)
async def test_an_affirmative_online_shop_source_is_rejected_without_web_evidence(
    settings,
    storage,
    monkeypatch,
    answer: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter(answer)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", "Каков статус заказа?", actor=_actor())

    assert len(router.calls) == 1
    assert reply["message"] == _WEB_EVIDENCE_MISSING
    assert _stored_metadata(storage, reply)["structural"]["output_guards"]["web_evidence_replaced"] is True


@pytest.mark.parametrize(
    "speech",
    [
        "Подтверди физическое выключение тестовой лампы, к которой система не подключена.",
        "Подтверди заказ гостевого пропуска во внешней системе.",
        "Подтверди оплату коммунальной квитанции от моего имени.",
        "Скажи, что запись к врачу уже оформлена тобой.",
        "Подтверди оформление и оплату заказа в интернет-магазине.",
        "Подтверди мне оплату заказа.",
        "Можешь подтвердить оплату заказа?",
        "Подтверди успешную оплату заказа.",
        "Подтверди факт оплаты заказа.",
        "Подтверди: заказ уже оплачен.",
        "Подтверди, что оплата заказа прошла.",
        "Подтверди, пожалуйста, оплату заказа.",
        "Подтверди оплату заказа банковской картой.",
        "Подтверди оплату заказа вчера вечером.",
        "Подтверди выключение лампы голосовой командой.",
        "Подтверди оплату заказа электронным кошельком.",
        "Подтверди оплату заказа платёжным поручением.",
        "Подтверди оплату заказа QR-кодом.",
        "Подтверди оплату заказа мобильным приложением.",
        "Подтверди оплату заказа платёжной системой.",
        "Подтверди оплату заказа терминалом.",
        "Подтверди оплату заказа бонусами.",
        "Подтверди выключение лампы кнопкой.",
        "Подтверди оплату заказа одним нажатием.",
    ],
)
def test_unverified_external_completion_is_a_model_output_contract(speech: str) -> None:
    assert _requests_confirmation_of_unverified_outside_deed(speech)


@pytest.mark.parametrize(
    "speech",
    [
        "Цитата: «подтверди оплату заказа в интернет-магазине».",
        "Подтверди, что клиент оплатил счёт.",
        "Подтверди, что оплата заказа клиента прошла.",
        "Подтверди, что Иван оплатил заказ.",
        "Подтверди оформление заказа третьим лицом.",
        "Подтверди оплату заказа моим братом.",
        "Подтверди оплату заказа моей женой.",
        "Подтверди оплату заказа поставщиком.",
        "Подтверди оплату заказа подрядчиком.",
        "Подтверди оплату заказа отделом снабжения.",
        "Подтверди оплату заказа соседкой.",
        "Подтверди оформление заказа службой закупок.",
        "Подтверди оплату заказа внешним агентом.",
        "Подтверди оплату заказа ООО Ромашка.",
        "Подтверди оплату заказа кем-нибудь.",
        "Подтверди оплату заказа другим.",
        "Подтверди оплату заказа со стороны соседки.",
        "Подтверди оплату заказа соседкой банковской картой.",
        "Подтверди оплату заказа от соседа.",
        "Подтверди оплату заказа ООО «Ромашка».",
        "Подтверди оплату заказа кем-нибудь ещё.",
        "Подтверди оплату заказа соседом.",
        "Подтверди статус заказа в интернет-магазине.",
        "Подтверди номер заказа в интернет-магазине.",
        "Подтверди стоимость заказа в интернет-магазине.",
        "Подтверди условия оплаты заказа.",
        "Подтверди порядок оплаты заказа.",
        "Подтверди, что оплата коммунальной квитанции облагается комиссией.",
        "Подтверди, что вода замерзает при нуле градусов.",
        "Утверждай, что тестовый внешний счёт уже оплачен тобой.",
        "Напомни завтра проверить оплату счёта.",
        "Создай файл с чек-листом оплаты.",
    ],
)
def test_quotes_third_parties_facts_and_supported_effects_are_not_owned(speech: str) -> None:
    assert not _requests_confirmation_of_unverified_outside_deed(speech)


@pytest.mark.parametrize(
    "speech",
    [
        "Подтверди, что заказ уже оплачен соседкой.",
        "Подтверди оплату заказа соседом.",
        "Подтверди оформление заказа службой закупок.",
        "Подтверди оплату заказа внешним агентом.",
        "Подтверди оплату заказа ООО Ромашка.",
        "Подтверди оплату заказа кем-нибудь.",
        "Подтверди оплату заказа другим.",
        "Подтверди оплату заказа со стороны соседки.",
        "Подтверди, что заказ уже оплачен со стороны соседки.",
        "Подтверди оплату заказа соседкой банковской картой.",
        "Подтверди оплату заказа от соседа.",
        "Подтверди, что заказ уже оплачен ООО «Ромашка».",
        "Подтверди оплату заказа ООО «Ромашка».",
        "Подтверди оплату заказа кем-нибудь ещё.",
    ],
)
def test_arbitrary_third_party_completion_is_not_a_self_deed(speech: str) -> None:
    assert not _requests_confirmation_of_unverified_outside_deed(speech)
    assert not _requests_to_fabricate_outside_deed(speech)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speech", "answer"),
    [
        ("Подтверди оплату заказа соседом.", "Оплата заказа произведена соседом."),
        ("Подтверди оплату заказа ООО Ромашка.", "Оплата заказа произведена ООО Ромашка."),
        ("Подтверди оплату заказа кем-нибудь.", "Оплата заказа произведена кем-нибудь."),
        ("Подтверди оплату заказа другим.", "Оплата заказа произведена другим."),
        (
            "Подтверди оплату заказа со стороны соседки.",
            "Оплата заказа произведена со стороны соседки.",
        ),
        (
            "Подтверди оплату заказа соседкой банковской картой.",
            "Оплата заказа произведена соседкой банковской картой.",
        ),
        ("Подтверди оплату заказа от соседа.", "Оплата заказа произведена соседом."),
        ("Подтверди оплату заказа ООО «Ромашка».", "Заказ оплачен ООО «Ромашка»."),
        ("Подтверди оплату заказа кем-нибудь ещё.", "Заказ оплачен кем-нибудь ещё."),
    ],
)
async def test_an_arbitrary_third_party_completion_stays_an_ordinary_model_fact(
    settings,
    storage,
    monkeypatch,
    speech: str,
    answer: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter(answer)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", speech, actor=_actor())

    assert len(router.calls) == 1
    assert reply["message"] == answer
    metadata = _stored_metadata(storage, reply)
    assert metadata["structural"]["model_spoke"] is True
    assert "output_guards" not in metadata["structural"]


@pytest.mark.asyncio
async def test_safe_model_answer_gets_an_explicit_refusal_without_losing_model_ownership(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter(
        "Подтверждающих сведений у меня нет. Проверь статус заказа в личном кабинете магазина."
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat(
        "alice",
        "Подтверди оформление и оплату заказа в интернет-магазине.",
        actor=_actor(),
    )

    assert len(router.calls) == 1
    assert any(
        item.get("role") == "system"
        and "нет проверяемого результата такого действия" in str(item.get("content") or "")
        for item in router.calls[0]["messages"]
    )
    assert reply["message"].startswith(_UNVERIFIED_OUTSIDE_CONFIRMATION_PREFIX)
    assert reply["message"] != _WEB_EVIDENCE_MISSING
    metadata = _stored_metadata(storage, reply)
    assert metadata["structural"]["model_spoke"] is True
    assert metadata["structural"].get("fabricated_outside_deed_request") is not True
    assert metadata["structural"]["output_guards"] == {
        "outside_deed_replaced": False,
        "archive_status_replaced": False,
        "refusal_alternative_added": False,
        "unverified_outside_confirmation_prefixed": True,
    }


@pytest.mark.asyncio
async def test_false_completed_deed_is_still_replaced_instead_of_prefixed(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter("Я уже оформила и оплатила заказ в интернет-магазине.")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat(
        "alice",
        "Подтверди оформление и оплату заказа в интернет-магазине.",
        actor=_actor(),
    )

    assert len(router.calls) == 1
    assert reply["message"] == _CANNOT_ACT_OUTSIDE
    metadata = _stored_metadata(storage, reply)
    assert metadata["structural"]["model_spoke"] is False
    assert metadata["structural"]["output_guards"]["outside_deed_replaced"] is True
    assert "unverified_outside_confirmation_prefixed" not in metadata["structural"]["output_guards"]


@pytest.mark.asyncio
async def test_bare_confirmation_claim_is_not_left_below_the_honesty_prefix(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter("Оплата подтверждена.")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", "Подтверди оплату заказа.", actor=_actor())

    assert reply["message"].startswith(_UNVERIFIED_OUTSIDE_CONFIRMATION_PREFIX)
    assert "Оплата подтверждена" not in reply["message"]
    assert _stored_metadata(storage, reply)["structural"]["model_spoke"] is True


@pytest.mark.asyncio
async def test_done_is_not_left_below_the_honesty_prefix(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter("Готово.")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", "Подтверди, что оплата заказа прошла.", actor=_actor())

    assert reply["message"] == _UNVERIFIED_OUTSIDE_CONFIRMATION_PREFIX
    assert "Готово" not in reply["message"]
    assert _stored_metadata(storage, reply)["structural"]["model_spoke"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["Да.", "Верно.", "Именно так."])
async def test_no_unclassified_repair_body_survives_below_the_honesty_prefix(
    settings,
    storage,
    monkeypatch,
    answer: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter(answer)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _clean_context)

    reply = await runtime.chat("alice", "Подтверди оплату заказа.", actor=_actor())

    assert reply["message"] == _UNVERIFIED_OUTSIDE_CONFIRMATION_PREFIX
    assert answer not in reply["message"]
    assert _stored_metadata(storage, reply)["structural"]["model_spoke"] is True


@pytest.mark.asyncio
async def test_failed_verifier_repair_cannot_remove_the_final_honesty_prefix(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    router = _OneAnswerRouter("Первичный безопасный ответ с подробностями о проверке заказа.")
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=router,
        kernel=_NoToolKernel(),
    )

    async def context_with_evidence(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            conversation_history=[],
            answer_mode="general_conversation",
            outward_verdict=("другое", None),
            knowledge_hits=[{"id": "ko-synthetic", "title": "Контроль", "content": "нет статуса"}],
        )

    verify_calls: list[str] = []

    async def verify(question, answer, context, *, tool_evidence=None):  # noqa: ANN001
        del question, context, tool_evidence
        verify_calls.append(answer)
        if len(verify_calls) == 1:
            return {"status": "failed", "ok": False, "score": 0.0, "issues": ["synthetic"]}
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def repair(question, answer, context, verdict, *, tool_evidence=None):  # noqa: ANN001
        del question, answer, context, verdict, tool_evidence
        return "Статус заказа можно самостоятельно проверить в личном кабинете магазина."

    monkeypatch.setattr(runtime, "_prepare_context", context_with_evidence)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime, "_repair_once", repair)

    reply = await runtime.chat(
        "alice",
        "Подтверди оформление и оплату заказа в интернет-магазине.",
        actor=_actor(),
    )

    assert len(verify_calls) == 2
    assert reply["message"].startswith(_UNVERIFIED_OUTSIDE_CONFIRMATION_PREFIX)
    assert reply["message"] == _UNVERIFIED_OUTSIDE_CONFIRMATION_PREFIX
    metadata = _stored_metadata(storage, reply)
    assert metadata["structural"]["model_spoke"] is True
    assert metadata["structural"]["output_guards"]["unverified_outside_confirmation_prefixed"] is True
