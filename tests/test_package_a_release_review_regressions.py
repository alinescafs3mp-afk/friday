"""Synthetic regressions found by the independent Package A release review."""

from __future__ import annotations

from dataclasses import replace

import pytest

from friday.agent_runtime import (
    _ARCHIVE_STATUS_FALLBACK,
    _UNCONFIRMED_SUPPORTED_DEED,
    AgentContext,
    AgentRuntime,
    _carrier_projection_passes,
    _claims_an_unconfirmed_supported_deed,
    add_useful_refusal_alternative,
    claims_a_deed_it_cannot_do,
    refusal_lacks_useful_alternative,
    strip_unasked_archive_status,
)
from friday.permissions import ActorContext


@pytest.mark.parametrize(
    "answer",
    [
        "Согласно твоей просьбе, я заказала курьера.",
        "Согласно договорённости, я оплатила счёт.",
        "Согласно твоей договорённости, я заказала такси.",
        "Согласно инструкции, я включила лампу.",
    ],
)
def test_k18_an_action_rationale_is_not_mistaken_for_an_external_source(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Такси заказала.",
        "Курьера вызвала.",
        "Счёт оплатила.",
        "Заказ оформила.",
        "Такси уже заказала.",
        "Счёт только что оплатила.",
    ],
)
def test_k18_blocks_object_first_pro_drop_outside_deeds(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Мария такси заказала.",
        "Такси заказала Мария.",
        "Курьера вызвала диспетчер.",
        "Счёт оплатила клиентка.",
        "Заказ оформила менеджер.",
        "Цитата: «Такси заказала».",
        "Такси не заказала.",
        "Счёт бы оплатила.",
        "Если заказ оформила, пришли номер.",
        "Заказ оформила в таблице.",
        "Курьера вызвала в примере ответа.",
    ],
)
def test_k18_object_first_pro_drop_keeps_other_actors_and_nonactual_content(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Я распечатала результат в консоль.",
        "Я забронировала память.",
        "Для расчёта считаем, что счёт оплачен.",
        "Статус „счёт оплачен“ означает завершённый расчёт.",
    ],
)
def test_k18_keeps_technical_hypothetical_and_quoted_status_text(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Я распечатала результат в консоль и заказала курьера.",
        "Статус „счёт оплачен“ означает завершение и я заказала такси.",
    ],
)
def test_k18_a_safe_technical_or_quoted_prefix_cannot_hide_a_later_deed(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Сантехник уже вызван.",
        "Заказ размещён в интернет-магазине.",
        "Я зарегистрировала аккаунт на сайте.",
        "Я отправила сообщение в WhatsApp.",
    ],
)
def test_k18_still_blocks_new_unambiguous_outside_deeds(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Я вызвала скорую помощь.",
        "Я оформила возврат.",
        "Я связалась с курьером.",
        "Я включила сигнализацию.",
        "Я выключила духовку.",
        "Я включила насос.",
        "Я перезапустила сервер.",
        "Я перезагрузила ноутбук.",
        "Скорая помощь вызвана.",
        "Сигнализация включена.",
        "Я вызвала полицию.",
        "Я вызвала пожарных.",
        "Я вызвала МЧС.",
        "Я вызвала аварийную службу.",
        "Я вызвала службу газа.",
        "Полиция вызвана.",
        "Пожарные вызваны.",
        "Аварийная служба вызвана.",
        "Я вызвала спасателей.",
        "Я вызвала охрану.",
        "Я вызвала службу безопасности.",
        "Спасатели вызваны.",
        "Охрана вызвана.",
        "Я отключила сигнализацию.",
        "Я остановила насос.",
        "Сигнализация отключена.",
        "Насос остановлен.",
    ],
)
def test_k18_blocks_emergency_commerce_contact_and_device_completions(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Я отменила заказ.",
        "Заказ отменён.",
        "Я перенесла запись к врачу.",
        "Запись к врачу перенесена.",
        "Я вернула деньги клиенту.",
        "Возврат оформлен.",
    ],
)
def test_k18_blocks_cancellation_rescheduling_and_refund_completions(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Я отправила письмо Ивану.",
        "Я отправила письмо на ivan@example.invalid.",
        "Письмо отправлено на ivan@example.invalid.",
        "Я отправила сообщение Ивану в Telegram.",
        "Я написала Ивану.",
        "Я выслала письмо Ивану.",
    ],
)
def test_k18_blocks_named_and_addressed_external_messages(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Я подала заявку на сайте.",
        "Заявка подана на сайте.",
        "Я оформила подписку.",
        "Подписка отменена.",
        "Билеты заказаны.",
        "Пицца заказана.",
        "Номер в отеле забронирован.",
        "Доставка организована.",
        "Курьер нанят.",
        "Врач вызван.",
        "Эвакуатор вызван.",
        "Я отправила посылку.",
        "Посылка отправлена.",
        "Я открыла счёт в банке.",
        "Баланс пополнен.",
        "Ваша запись отменена.",
        "Запись на приём перенесена.",
        "Я перенесла ваш приём к врачу.",
        "Запись к стоматологу подтверждена.",
    ],
)
def test_k18_blocks_generic_and_appointment_external_results(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Я оформила заказ в виде таблицы.",
        "Я оформила заявку в таблице.",
        "Я оформила возврат как документ.",
        "Я организовала доставку как раздел плана.",
        "Я зарезервировала номер строки в таблице.",
        "Я отправила письмо Ивану в примере документа.",
        "Доставка данных организована между компонентами.",
        "Заказ не отменён.",
        "Запись к врачу не перенесена.",
        "Деньги не возвращены.",
        "Возврат не оформлен.",
        "Заказ будет отменён завтра.",
        "Запись будет перенесена завтра.",
        "Деньги будут возвращены.",
        "Возврат будет оформлен.",
        "Заказ отменён клиентом.",
        "Запись перенесена врачом.",
        "Возврат оформлен магазином.",
    ],
)
def test_k18_keeps_content_work_nonactual_results_and_external_actors(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Я не только оформила возврат, но и связалась с курьером.",
        "Я вызвала скорую помощь, хотя, возможно, поздно.",
        "Возможно, всё прошло штатно, я вызвала скорую помощь.",
        "Мною была вызвана скорая помощь.",
    ],
)
def test_k18_qualifiers_and_self_passive_order_cannot_hide_a_completed_deed(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Я вызвала полицию и охрана не вызвана.",
        "Охрана не вызвана и я вызвала полицию.",
        "Полиция вызвана и пожарные не вызваны.",
        "Я оплатила счёт и заказ не оформлен.",
        "Полиция будет вызвана завтра и я оплатила счёт.",
        "Я оплатила счёт и полиция будет вызвана завтра.",
    ],
)
def test_k18_negated_or_future_deed_cannot_hide_an_independent_completion(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Если завтра пойдёт дождь, я уже оплатила счёт.",
        "Когда ты спросил, я вызвала полицию.",
        "Согласно журналу, сервер работает, я заказала курьера.",
        "По данным отчёта, заказ не найден, я вызвала полицию.",
        "Судя по акту, дверь закрыта, я оплатила счёт.",
        "В документе сказано, что банк закрыт, я заказала такси.",
    ],
)
def test_k18_leading_condition_or_source_cannot_own_a_later_self_deed(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Сервис сообщил об ошибке, однако я заказала курьера.",
        "По данным отчёта заказ не найден, при этом я оплатила счёт.",
        "Курьер не вызван, хотя я включила лампу.",
        "Заказ не оформлен, всё же я вызвала такси.",
    ],
)
def test_k18_discourse_connectors_cannot_hide_a_later_completion(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Согласно журналу, заказ размещён в интернет-магазине техником.",
        "Согласно акту, сантехник уже вызван дежурным техником.",
        "Согласно инструкции, лампа включена техником.",
    ],
)
def test_k18_keeps_a_real_named_source_and_explicit_external_actor(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Счёт оплачен банковской картой.",
        "Курьер заказан мобильным приложением.",
        "Такси вызвано голосовой командой.",
        "Лампа включена умным реле.",
        "Заказ оформлен быстрым способом.",
        "Билет куплен кредитной картой.",
        "Документ распечатан лазерным принтером.",
    ],
)
def test_k18_an_instrument_or_method_is_not_mistaken_for_an_external_actor(answer: str) -> None:
    assert claims_a_deed_it_cannot_do(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "В моём архиве семьи ответа нет.",
        "В материалах моего архива семьи ответа нет.",
    ],
)
def test_k17_keeps_a_named_external_archive_even_with_a_possessive(answer: str) -> None:
    assert strip_unasked_archive_status(answer) == (answer, False, True)


class _UnusedLLM:
    enabled = True
    total_budget_sec = 30.0

    async def chat(self, messages, **kwargs):  # pragma: no cover - patched seam owns generation
        del messages, kwargs
        raise AssertionError("unexpected model call")


@pytest.mark.parametrize("has_attachment", [False, True])
@pytest.mark.asyncio
async def test_k17_preserves_a_status_grounded_in_current_attachment_evidence(
    has_attachment: bool,
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_UnusedLLM(),  # type: ignore[arg-type]
    )

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            answer_mode="general_conversation",
        )

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": "В твоём документе ответа нет.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    attachments = (
        [
            {
                "filename": "synthetic-current-turn.txt",
                "transient_text": "Синтетический документ не содержит искомого ответа.",
                "extraction_success": True,
                "verification_eligible": True,
            }
        ]
        if has_attachment
        else []
    )

    result = await runtime.chat(
        "alice",
        "Есть ли ответ в этом документе?",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=attachments,
        enable_tools=False,
    )

    expected = "В твоём документе ответа нет." if has_attachment else _ARCHIVE_STATUS_FALLBACK
    assert result["message"] == expected


@pytest.mark.parametrize(
    "answer",
    [
        "Нет прав.",
        "Нет полномочий.",
        "У меня нет прав выполнить это действие.",
        "У меня нет полномочий выполнить это действие.",
    ],
)
def test_k11_recognises_bare_rights_and_authority_refusals(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True
    augmented = add_useful_refusal_alternative(answer)
    assert augmented.startswith(answer)
    assert refusal_lacks_useful_alternative(augmented) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Не имею прав это сделать.",
        "Доступа нет.",
        "Полномочий нет.",
        "Мне запрещено это делать.",
        "Я лишена возможности это сделать.",
        "Я не вправе это делать.",
        "Я неспособна выполнить это действие.",
        "Эта функция мне недоступна.",
        "Такой функции у меня нет.",
        "Это вне моих возможностей.",
        "Я не располагаю такой возможностью.",
        "Мне не разрешено.",
        "Я не уполномочена.",
        "Это запрещено.",
        "Действие запрещено.",
        "Нет возможности.",
        "Возможности нет.",
    ],
)
def test_k11_recognises_closed_natural_capability_refusal_forms(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True
    assert refusal_lacks_useful_alternative(add_useful_refusal_alternative(answer)) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Не могу позвонить, зато отправлю письмо клиенту.",
        "Не могу позвонить. Зато отправлю письмо клиенту.",
        "Я не могу позвонить, зато могу отправить письмо клиенту.",
    ],
)
def test_k11_an_impossible_outside_action_is_not_a_useful_alternative(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True
    augmented = add_useful_refusal_alternative(answer)
    assert augmented != answer
    assert refusal_lacks_useful_alternative(augmented) is False


@pytest.mark.parametrize(
    "answer",
    [
        "Не могу позвонить, но могу подсказать, что сказать.",
        "Не могу сделать это, но могу объяснить порядок действий.",
        "Не могу оплатить, но покажу, где это сделать.",
        "Не могу войти, но ты можешь открыть кабинет самостоятельно.",
    ],
)
def test_k11_keeps_a_concrete_reachable_explanation_or_guidance(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is False


def test_k11_vague_somewhere_is_not_a_reachable_alternative() -> None:
    answer = "Действие запрещено; обратитесь куда-нибудь."
    assert refusal_lacks_useful_alternative(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Не могу подтвердить этот факт, но я не могу позвонить клиенту.",
        "Не могу найти документ, но не могу позвонить клиенту.",
        "Мне недоступна информация и я не могу вызвать курьера.",
    ],
)
def test_k11_an_uncertainty_cannot_hide_a_later_capability_refusal(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True
    assert refusal_lacks_useful_alternative(add_useful_refusal_alternative(answer)) is False


@pytest.mark.parametrize(
    "answer",
    [
        "У меня отсутствует возможность позвонить.",
        "Мне нельзя звонить клиентам.",
        "У меня нет такой функции.",
        "Я не поддерживаю внешние звонки.",
        "Звонки не поддерживаются.",
        "Я не выполняю внешние действия.",
        "Я не могу позвонить. Что делать?",
        "Я не могу позвонить. Хотите другой вариант?",
    ],
)
def test_k11_natural_capability_denials_still_need_a_reachable_next_step(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Не могу поверить, как всё быстро.",
        "Не могу дождаться результата.",
        "Я не могу сказать, что это плохо.",
        "Не могу вспомнить точную дату.",
    ],
)
def test_k11_idioms_and_knowledge_uncertainty_are_not_capability_refusals(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is False


def test_an_unconfirmed_supported_deed_offers_only_a_user_reachable_next_step() -> None:
    folded = _UNCONFIRMED_SUPPORTED_DEED.casefold()
    assert "могу повторить" not in folded
    assert "повторю" not in folded
    assert "проверь" in folded or "повтори запрос" in folded
    assert claims_a_deed_it_cannot_do(_UNCONFIRMED_SUPPORTED_DEED) is False


@pytest.mark.parametrize(
    ("answer", "delivery_scheduled", "expected_unconfirmed"),
    [
        ("Напоминание сохранено: отчёт, завтра.", False, False),
        ("Напомню вам завтра про отчёт.", False, True),
        ("Напомню вам завтра про отчёт.", True, False),
    ],
)
def test_a_reminder_delivery_promise_requires_a_scheduled_delivery(
    answer: str,
    delivery_scheduled: bool,
    expected_unconfirmed: bool,
) -> None:
    assert (
        _claims_an_unconfirmed_supported_deed(
            answer,
            has_file=False,
            reminder_succeeded=True,
            reminder_delivery_scheduled=delivery_scheduled,
            reminder_descriptors=["отчёт завтра"],
        )
        is expected_unconfirmed
    )


@pytest.mark.parametrize(
    ("claim", "descriptor"),
    [
        ("PDF готов и приложен.", "report.xlsx"),
        ("Excel готов и приложен.", "report.pdf"),
        ("Архив собран и прикреплён.", "report.docx"),
        ("Картинка готова.", "report.txt"),
        ("PNG отправлен.", "report.pdf"),
    ],
)
def test_supported_file_format_claim_must_match_delivered_artifact(
    claim: str,
    descriptor: str,
) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=[descriptor],
    )


@pytest.mark.parametrize(
    ("claim", "descriptor"),
    [
        ("PDF готов и приложен.", "report.pdf"),
        ("Excel готов и приложен.", "report.xlsx"),
        ("Архив собран и прикреплён.", "report.zip"),
        ("Картинка готова.", "report.webp"),
        ("PNG отправлен.", "report.png"),
    ],
)
def test_supported_file_format_claim_accepts_matching_artifact(
    claim: str,
    descriptor: str,
) -> None:
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=[descriptor],
    )


@pytest.mark.parametrize(
    ("claim", "descriptor"),
    [
        ("PDF-версия готова и приложена.", "report.pdf"),
        ("Файл из архива готов и приложен.", "report.pdf"),
    ],
)
def test_supported_file_format_binding_keeps_truthful_version_and_source_phrases(
    claim: str,
    descriptor: str,
) -> None:
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=[descriptor],
    )


def test_docx_completion_is_a_supported_file_claim_and_needs_matching_evidence() -> None:
    claim = "DOCX готов и приложен."
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=False,
        reminder_succeeded=False,
    )
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.pdf"],
    )
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.docx"],
    )


@pytest.mark.parametrize("claim", ["Я прикрепила PDF.", "Держи PDF."])
def test_natural_supported_file_delivery_claim_matches_the_delivered_format(claim: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=False,
        reminder_succeeded=False,
    )
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.xlsx"],
    )
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.pdf"],
    )


def test_guillemets_in_a_quoted_file_status_are_not_a_completion_claim() -> None:
    assert not _claims_an_unconfirmed_supported_deed(
        "Статус «PDF готов» означает успех.",
        has_file=False,
        reminder_succeeded=False,
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Документ готов в PDF.",
        "Отчёт готов в формате PDF.",
    ],
)
def test_trailing_format_qualifier_is_bound_to_the_file_claim(claim: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.xlsx"],
    )
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.pdf"],
    )


@pytest.mark.parametrize("claim", ["Сделано — PDF.", "Готово — DOCX."])
def test_leading_completion_ack_with_a_format_still_requires_a_file(claim: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=False,
        reminder_succeeded=False,
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Прилагаю PDF.",
        "Я прилагаю PDF.",
        "Я выгрузила PDF.",
        "PDF лежит во вложении.",
        "PDF успешно создан.",
        "PDF наконец готов.",
        "PDF полностью готов.",
        "PDF повторно выгружен.",
        "PDF заново сформирован.",
        "PDF теперь готов.",
        "PDF действительно готов.",
        "PDF точно готов.",
        "PDF тоже готов.",
        "Я повторно выгрузила PDF.",
    ],
)
def test_completion_verbs_and_modifiers_are_not_mistaken_for_the_artifact_subject(
    claim: str,
) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=False,
        reminder_succeeded=False,
    )
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.pdf"],
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Я сохранила PDF.",
        "Я сгенерировала PDF.",
        "PDF сгенерирован.",
        "Я экспортировала PDF.",
        "PDF сохранён.",
    ],
)
def test_file_save_generate_and_export_verbs_require_artifact_evidence(claim: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=False,
        reminder_succeeded=False,
    )
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.pdf"],
    )


@pytest.mark.parametrize(
    "claim",
    [
        "Документ готов в виде PDF.",
        "Документ готов как PDF.",
        "Документ готов в формате .pdf.",
        "Документ готов формата PDF.",
        "Документ готов в формате Adobe PDF.",
    ],
)
def test_natural_trailing_pdf_forms_bind_to_the_delivered_format(claim: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.xlsx"],
    )
    assert not _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=True,
        reminder_succeeded=False,
        file_descriptors=["report.pdf"],
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Чтобы отправить файл в чат, нажми кнопку.",
        "Файл в чате можно открыть кнопкой.",
        "Файл в чате отображается рядом.",
        "Проверь, есть ли файл в чате.",
        "Я объясню, как отправить файл в чат.",
        "В этом файле готовые примеры.",
        "Созданный документ содержит шаблон.",
        "Готовый PDF можно скачать.",
        "PDF готовится.",
        "PDF был готов вчера по словам автора.",
    ],
)
def test_explanatory_locative_and_progressive_file_prose_is_not_a_deed(answer: str) -> None:
    assert not _claims_an_unconfirmed_supported_deed(
        answer,
        has_file=False,
        reminder_succeeded=False,
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Напомню: дедлайн завтра.",
        "Кратко напомню порядок действий.",
        "Напомню, как открыть документ.",
        "Не напомню автоматически.",
        "Уведомление создано приложением.",
    ],
)
def test_discourse_or_external_reminder_prose_is_not_a_created_reminder(answer: str) -> None:
    assert not _claims_an_unconfirmed_supported_deed(
        answer,
        has_file=False,
        reminder_succeeded=False,
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Я запланировала напоминание.",
        "Напоминание запланировано.",
        "Я завела напоминание.",
        "Напоминание готово.",
        "Напоминание активировано.",
    ],
)
def test_natural_reminder_completion_words_require_persisted_evidence(answer: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        answer,
        has_file=False,
        reminder_succeeded=False,
    )


@pytest.mark.parametrize(
    "answer",
    [
        "Я отправила ответ прямо сюда.",
        "Я отправила текст в чат.",
        "Я записала текст в архив.",
        "Я записала ответ в блокнот.",
    ],
)
def test_text_delivery_and_notes_are_not_voice_completions(answer: str) -> None:
    assert not _claims_an_unconfirmed_supported_deed(
        answer,
        has_file=False,
        reminder_succeeded=False,
    )


@pytest.mark.parametrize(
    "answer",
    ["Я записала ответ голосом.", "Аудио прикреплено.", "Аудиофайл прикреплён."],
)
def test_natural_voice_completion_words_require_voice_evidence(answer: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        answer,
        has_file=False,
        reminder_succeeded=False,
        voice_succeeded=False,
    )


@pytest.mark.parametrize("spoofed_title", ["report.pdf", "application/pdf"])
def test_model_controlled_title_cannot_spoof_the_rendered_file_format(spoofed_title: str) -> None:
    assert not _carrier_projection_passes(
        {
            "kind": "xlsx",
            "title": spoofed_title,
            "blocks": [{"kind": "text", "text": "PDF готов и приложен."}],
        },
        archive_status_guarded=False,
    )


@pytest.mark.parametrize(
    "claim",
    [
        "PDF готов и DOCX не готов.",
        "PDF готов, DOCX ещё не готов.",
        "Я создала PDF и не создала DOCX.",
        "PDF не готов и DOCX готов.",
        "Я не создала PDF и создала DOCX.",
        "PDF готов и DOCX будет создан завтра.",
        "PDF готов, DOCX будет создан завтра.",
        "PDF будет создан завтра и DOCX готов сейчас.",
    ],
)
def test_negated_or_future_artifact_cannot_hide_an_independent_completion(claim: str) -> None:
    assert _claims_an_unconfirmed_supported_deed(
        claim,
        has_file=False,
        reminder_succeeded=False,
    )


class _SavedOnlyReminderKernel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return [{"type": "function", "function": {"name": "remind", "description": "напомнить"}}]

    async def execute(self, tool: str, params: dict, actor=None):  # noqa: ANN001, ARG002
        self.calls.append((tool, params))

        class _Result:
            success = True
            error = ""
            data = {
                "created": True,
                "what": str(params.get("what") or ""),
                "on": "2026-08-09",
                "at": "",
                "requested_when": str(params.get("when") or ""),
                "delivery_scheduled": False,
            }
            attachment = None

            def to_llm_message(self) -> str:
                return "Напоминание сохранено. Доставка недоступна."

        return _Result()


class _SavedOnlyReminderLLM:
    enabled = True
    total_budget_sec = 5.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        asked = " ".join(str(item.get("content") or "") for item in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет' in asked:
            return {"content": '{"вид": "действие", "правило": "", "запрос": "", "кто": "", "дни": []}'}
        if '"остаток"' in asked and "уже решена" in asked:
            return {"content": '{"остаток": "какой статус проекта"}'}
        if '"напоминание"' in asked:
            return {
                "content": (
                    '{"напоминание": "да", "что": "отчёт", "когда": "завтра", '
                    '"остаток": "какой статус проекта"}'
                )
            }
        return {"content": "Напомню вам завтра про отчёт. Проект идёт по плану."}


class _AgenticSavedOnlyReminderLLM:
    enabled = True
    total_budget_sec = 5.0

    def __init__(self) -> None:
        self.calls = 0
        self.tool_round_calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        self.calls += 1
        if not tools:
            return {"content": "нет"}
        self.tool_round_calls += 1
        if self.tool_round_calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "saved-only-reminder",
                        "function": {
                            "name": "remind",
                            "arguments": '{"what": "отчёт", "when": "завтра"}',
                        },
                    }
                ],
            }
        return {"content": "Готово."}


@pytest.mark.asyncio
async def test_saved_only_reminder_keeps_the_notice_and_rejects_a_delivery_promise(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    kernel = _SavedOnlyReminderKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_SavedOnlyReminderLLM(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )

    result = await runtime.chat(
        "alice",
        "Напомни завтра про отчёт и скажи, какой статус проекта.",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    said = str(result.get("message") or "")
    assert kernel.calls and kernel.calls[0][0] == "remind"
    assert "Напоминание сохранено" in said
    assert "Автоматическая доставка в чат сейчас недоступна" in said
    assert "Напомню вам завтра" not in said
    assert "Могу повторить" not in said
    assert "delivery_scheduled" not in said


@pytest.mark.asyncio
async def test_agentic_saved_only_reminder_retains_delivery_state(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    context = AgentContext(
        conversation_id="agentic-saved-only",
        user_id="alice",
        person_id="alice",
        answer_mode="general_conversation",
        outward_verdict=("интернет", None),
    )
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_AgenticSavedOnlyReminderLLM(),  # type: ignore[arg-type]
        kernel=_SavedOnlyReminderKernel(),  # type: ignore[arg-type]
    )

    await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Напомни завтра про отчёт.",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [{"type": "function", "function": {"name": "remind"}}],
        None,
    )

    assert context.successful_reminders == [
        {
            "what": "отчёт",
            "when": "2026-08-09",
            "requested_when": "завтра",
            "delivery_scheduled": False,
        }
    ], (runtime.llm.calls, runtime.kernel.calls)
    assert "Напоминание сохранено" in context.structural_answer
    assert "Автоматическая доставка в чат сейчас недоступна" in context.structural_answer
