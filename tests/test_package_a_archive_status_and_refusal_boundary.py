"""Package A keeps ordinary answers useful without leaking retrieval machinery.

The corpus is frozen and wholly synthetic.  Unit-level acceptance fixes the
classifier boundary; the runtime tests fix the placement of that boundary in
the complete delivery path, after bounded repair and before persistence or a
late document build.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from friday.agent_runtime import (
    _ARCHIVE_STATUS_FALLBACK,
    _REFUSAL_ALTERNATIVE,
    AgentContext,
    AgentRuntime,
    _carrier_projection_passes,
    _guard_model_carrier_payload,
    add_useful_refusal_alternative,
    refusal_lacks_useful_alternative,
    strip_unasked_archive_status,
)
from friday.permissions import ActorContext

_FIXTURE_PATH = Path(__file__).with_name("fixtures") / "package_a_honesty_holdout.json"
_FIXTURE = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
assert _FIXTURE["schema"] == "friday.package-a-honesty-holdout.v1"
assert _FIXTURE["synthetic_only"] is True

_K17_STRIP = _FIXTURE["k17_unasked_archive_status"]["must_strip"]
_K17_KEEP = _FIXTURE["k17_unasked_archive_status"]["must_keep"]
_K11_AUGMENT = _FIXTURE["k11_refusal_without_next_step"]["must_augment"]
_K11_KEEP = _FIXTURE["k11_refusal_without_next_step"]["must_keep"]


def test_k17_strips_all_eight_service_statuses_and_keeps_the_three_answer_suffixes() -> None:
    assert len(_K17_STRIP) == 8

    stripped = [strip_unasked_archive_status(answer) for answer in _K17_STRIP]
    assert sum(changed for _, changed, _ in stripped) == 8
    assert [has_model_content for _, _, has_model_content in stripped] == [
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        True,
    ]
    assert [cleaned for cleaned, _, remains in stripped if not remains] == [_ARCHIVE_STATUS_FALLBACK] * 5
    assert [cleaned.casefold() for cleaned, _, remains in stripped if remains] == [
        "общий принцип можно объяснить так: данные проверяют по контрольной сумме.",
        "по общим сведениям формат поддерживает таблицы.",
        "ответ по существу: операция занимает линейное время.",
    ]


def test_k17_keeps_all_ten_legitimate_answers_byte_for_byte() -> None:
    assert len(_K17_KEEP) == 10

    for answer in _K17_KEEP:
        assert strip_unasked_archive_status(answer) == (answer, False, True)


def test_k17_fallback_suffix_and_already_clean_text_are_idempotent() -> None:
    for answer in [*_K17_STRIP, *_K17_KEEP]:
        cleaned, _, has_model_content = strip_unasked_archive_status(answer)
        assert strip_unasked_archive_status(cleaned) == (cleaned, False, bool(cleaned.strip()))
        assert bool(cleaned.strip()) is True
        if cleaned == _ARCHIVE_STATUS_FALLBACK:
            assert has_model_content is False


def test_k11_augments_all_eight_bare_refusals_once_and_only_at_the_tail() -> None:
    assert len(_K11_AUGMENT) == 8

    for answer in _K11_AUGMENT:
        assert refusal_lacks_useful_alternative(answer) is True
        augmented = add_useful_refusal_alternative(answer)
        assert augmented.startswith(answer)
        assert augmented.endswith(_REFUSAL_ALTERNATIVE)
        assert augmented.count(_REFUSAL_ALTERNATIVE) == 1
        assert refusal_lacks_useful_alternative(augmented) is False
        assert add_useful_refusal_alternative(augmented) == augmented


def test_k11_keeps_all_ten_useful_or_non_capability_answers_byte_for_byte() -> None:
    assert len(_K11_KEEP) == 10

    for answer in _K11_KEEP:
        assert refusal_lacks_useful_alternative(answer) is False
        assert add_useful_refusal_alternative(answer) == answer


class _EnabledButPatchedLLM:
    enabled = True
    model = "synthetic-package-a-double"
    total_budget_sec = 5.0

    async def chat(self, messages, **kwargs):  # pragma: no cover - every model seam is patched
        del messages, kwargs
        raise AssertionError("unexpected model call")


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def _runtime(settings, storage, monkeypatch, *, mode: str, verify_answers: bool = False) -> AgentRuntime:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=verify_answers, verify_min_answer_chars=1),
        storage,
        llm=_EnabledButPatchedLLM(),
    )

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            answer_mode=mode,
        )

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    return runtime


def _stored_assistant_content(storage, reply: dict) -> str:
    stored = storage.get_message(str(reply["message_id"]), "alice")
    assert stored is not None
    assert stored["role"] == "assistant"
    return str(stored["content"])


def _stored_assistant_metadata(storage, reply: dict) -> dict:
    stored = storage.get_message(str(reply["message_id"]), "alice")
    assert stored is not None
    return json.loads(str(stored["metadata_json"] or "{}"))


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("general_conversation", _ARCHIVE_STATUS_FALLBACK),
        ("personal_knowledge", _K17_STRIP[0]),
    ],
)
@pytest.mark.asyncio
async def test_k17_runtime_is_mode_gated_and_persists_exactly_what_it_returns(
    settings,
    storage,
    monkeypatch,
    mode: str,
    expected: str,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch, mode=mode)

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": _K17_STRIP[0], "tools_used": []}

    monkeypatch.setattr(runtime, "_generate_response", generate)

    reply = await runtime.chat(
        "alice",
        "Объясни синтетический общий принцип.",
        actor=_actor(),
        enable_tools=False,
    )

    assert reply["message"] == expected
    assert _stored_assistant_content(storage, reply) == expected
    metadata = _stored_assistant_metadata(storage, reply)
    if mode == "general_conversation":
        assert metadata["structural"]["output_guards"]["archive_status_replaced"] is True
    else:
        assert "output_guards" not in metadata["structural"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Что означает статус личной базы знаний?",
        "Что в моём архиве?",
        "Что в моем архиве?",
    ],
)
async def test_k17_keeps_an_archive_status_when_the_question_itself_is_about_storage(
    settings,
    storage,
    monkeypatch,
    question: str,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch, mode="general_conversation")

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": _K17_STRIP[0], "tools_used": []}

    monkeypatch.setattr(runtime, "_generate_response", generate)
    reply = await runtime.chat(
        "alice",
        question,
        actor=_actor(),
        enable_tools=False,
    )

    assert reply["message"] == _K17_STRIP[0]
    assert _stored_assistant_content(storage, reply) == _K17_STRIP[0]


def test_k17_keeps_general_statements_quotes_and_non_retrieval_negatives() -> None:
    legitimate = [
        "База знаний не содержит персональных данных.",
        "В моих документах ошибок нет.",
        "Мои документы ошибок не содержат.",
        "Модульная база знаний не содержит дубликатов.",
        "Творческие документы ошибок не содержат.",
        "«В моей личной базе ответа нет», — написано в синтетическом рассказе.",
        "В публичном архиве ответа на этот вопрос нет.",
        "В архиве городского музея ответа на этот вопрос нет.",
        "В архиве GitHub ответа нет.",
        "В государственном архиве таких сведений нет.",
        "В веб-архиве релевантных документов не найдено.",
        "В открытом архиве ничего подходящего не нашлось.",
        "В архиве ничего не найдено.",
        "Архив молчит по этой теме.",
        "Автор пишет: «В моей личной базе ответа нет».",
        "Пользователь сказал: в моей личной базе ответа нет.",
        "Цитата: В моей личной базе ответа нет.",
        "Перевод: В моей личной базе ответа нет.",
        "Фраза «В моей личной базе ответа нет» является примером.",
        "В моей базе персональных данных нет.",
        "В моей базе нет персональных данных.",
        "В моём архиве незаконных материалов нет.",
        "В моём архиве нет конфиденциальных документов.",
        "В моём архиве данных нет, потому что я его очистил.",
    ]
    for answer in legitimate:
        assert strip_unasked_archive_status(answer) == (answer, False, True)


@pytest.mark.parametrize(
    "answer",
    [
        "Поиск по внутренней базе ничего не дал.",
        "Поиск в личном архиве не дал результатов.",
        "В личной базе подходящих совпадений нет.",
        "В моей личной базе информации по этому вопросу нет.",
        "В вашем архиве нет релевантной информации.",
        "Среди ваших документов нужной информации не оказалось.",
        "В вашей базе ответ отсутствует.",
        "В моём хранилище ответа нет.",
        "По внутреннему поиску совпадений нет.",
        "Личный поиск ничего не вернул.",
    ],
)
def test_k17_catches_common_negative_retrieval_statuses(answer: str) -> None:
    assert strip_unasked_archive_status(answer) == (_ARCHIVE_STATUS_FALLBACK, True, False)


@pytest.mark.parametrize(
    "answer",
    [
        "Среди ваших документов подходящих сведений обнаружить не удалось.",
        "Поиск по личной базе оказался пустым.",
        "В вашей памяти по этому вопросу пусто.",
        "Личная база не дала ответа.",
        "Личный архив молчит по этой теме.",
        "Ничего подходящего среди сохранённых записей нет.",
        "По своим записям ответа не нашла.",
    ],
)
def test_k17_catches_natural_empty_internal_search_phrasings(answer: str) -> None:
    assert strip_unasked_archive_status(answer) == (_ARCHIVE_STATUS_FALLBACK, True, False)


@pytest.mark.parametrize(
    "answer",
    [
        "В семейном архиве документов по теме не нашлось.",
        "В корпоративном архиве ответа не найдено.",
        "В научном архиве ничего по теме не найдено.",
        "В коммерческой базе знаний ответа нет.",
        "В архиве компании релевантных сведений не найдено.",
        "В архиве проекта ответа по вопросу нет.",
    ],
)
def test_k17_keeps_a_negative_status_of_an_external_store(answer: str) -> None:
    assert strip_unasked_archive_status(answer) == (answer, False, True)


@pytest.mark.parametrize(
    "answer",
    [
        "В старой личной базе ответа нет.",
        "В локальном внутреннем архиве ничего не найдено.",
        "В нашей базе знаний ответа нет.",
        "В нашей памяти по этому вопросу пусто.",
        "В базе Пятницы ответа нет.",
        "В своей локальной базе ответа нет.",
    ],
)
def test_k17_recognises_an_explicitly_internal_store(answer: str) -> None:
    assert strip_unasked_archive_status(answer) == (_ARCHIVE_STATUS_FALLBACK, True, False)


def test_k17_ignores_invisible_format_controls_when_classifying() -> None:
    assert strip_unasked_archive_status("В моей личной ба\u200bзе ответа нет.") == (
        _ARCHIVE_STATUS_FALLBACK,
        True,
        False,
    )


def test_k17_classifies_the_markdown_text_telegram_actually_displays() -> None:
    assert strip_unasked_archive_status("В моей личной ба[зе](https://example.com) ответа нет.") == (
        _ARCHIVE_STATUS_FALLBACK,
        True,
        False,
    )


@pytest.mark.parametrize(
    "answer",
    [
        "В архиве отдела ответа не найдено.",
        "В архиве команды ничего по теме не нашлось.",
        "В архиве университета релевантных сведений нет.",
        "База знаний компании не содержит ответа.",
        "Архив суда молчит по этой теме.",
        "Архив клиента ответа по запросу не содержит.",
    ],
)
def test_k17_does_not_guess_that_an_external_store_is_fridays(answer: str) -> None:
    assert strip_unasked_archive_status(answer) == (answer, False, True)


def test_k17_can_cross_a_heading_but_keeps_the_substantive_suffix() -> None:
    answer = f"# Синтетический результат\n{_K17_STRIP[5]}"
    cleaned, changed, has_model_content = strip_unasked_archive_status(answer)
    assert changed is True
    assert has_model_content is True
    assert cleaned.casefold() == "общий принцип можно объяснить так: данные проверяют по контрольной сумме."


def test_k17_can_cross_more_than_three_display_headings() -> None:
    answer = "# Ответ\n## Поиск\n### Статус\nВ моей личной базе ответа нет."

    assert strip_unasked_archive_status(answer) == (_ARCHIVE_STATUS_FALLBACK, True, False)


@pytest.mark.asyncio
async def test_k17_status_only_discards_carriers_and_cannot_reach_the_late_builder(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch, mode="general_conversation")

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {
            "content": _K17_STRIP[0],
            "tools_used": [],
            "knowledge_object_ids": ["ko_synthetic"],
            "file_clips": [
                {
                    "kind": "document",
                    "filename": "discarded-synthetic.txt",
                    "mime_type": "text/plain",
                    "content_base64": "c3ludGhldGlj",
                }
            ],
            "voice_clip": {
                "kind": "voice",
                "mime_type": "audio/ogg",
                "audio_base64": "c3ludGhldGlj",
                "duration_sec": 1.0,
            },
        }

    late_builder_calls = 0

    async def build(  # noqa: ANN001
        request,
        answer,
        actor,
        *,
        evidence=None,
        context=None,
        literal_source_text=None,
    ):
        del request, answer, actor, evidence, context, literal_source_text
        nonlocal late_builder_calls
        late_builder_calls += 1
        raise AssertionError("the archive-status fallback reached the file builder")

    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", build)
    reply = await runtime.chat(
        "alice",
        "Сделай документ с синтетическим общим объяснением.",
        actor=_actor(),
        enable_tools=False,
    )

    assert reply["message"] == _ARCHIVE_STATUS_FALLBACK
    assert reply["files"] == []
    assert reply["voice"] is None
    assert reply["context"]["attributed_knowledge_count"] == 0
    assert late_builder_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "Оформи синтетический общий принцип в документ Word.",
        "Сделай Word с общим объяснением формата архивов ZIP.",
    ],
)
async def test_k17_still_guards_a_general_answer_requested_as_a_file(
    settings,
    storage,
    monkeypatch,
    question: str,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch, mode="general_conversation")
    expected, changed, has_model_content = strip_unasked_archive_status(_K17_STRIP[5])
    assert changed is True and has_model_content is True

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("файл", None),
            answer_mode="general_conversation",
        )

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": _K17_STRIP[5], "tools_used": []}

    built_from: list[str] = []

    async def build(  # noqa: ANN001
        request,
        answer,
        actor,
        *,
        evidence=None,
        context=None,
        literal_source_text=None,
    ):
        del request, actor, evidence, context, literal_source_text
        built_from.append(answer)
        return {
            "kind": "document",
            "filename": "clean-synthetic.txt",
            "mime_type": "text/plain",
            "content_base64": "c3ludGhldGlj",
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", build)
    reply = await runtime.chat(
        "alice",
        question,
        actor=_actor(),
        enable_tools=False,
    )

    assert reply["message"] == expected
    assert built_from == [expected]
    assert len(reply["files"]) == 1


def test_a_normal_hundred_row_table_fits_the_carrier_guard_budget() -> None:
    payload = {
        "kind": "xlsx",
        "title": "Синтетическая таблица",
        "blocks": [
            {
                "kind": "table",
                "rows": [
                    {"Колонка A": f"Строка {index}", "Колонка B": index, "Колонка C": index + 1}
                    for index in range(100)
                ],
            }
        ],
    }

    guarded, allowed = _guard_model_carrier_payload(payload, archive_status_guarded=True)

    assert allowed is True
    assert guarded == payload


def test_a_cyclic_native_carrier_fails_closed_without_recursing_forever() -> None:
    payload: dict = {"kind": "docx", "title": "Синтетический отчёт"}
    payload["blocks"] = payload

    assert _carrier_projection_passes(payload, archive_status_guarded=True) is False


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "xlsx",
            "title": "Я",
            "blocks": [{"kind": "table", "rows": [["перевела", "две тысячи", "рублей", "на карту"]]}],
        },
        {
            "kind": "xlsx",
            "title": "Сводка",
            "blocks": [
                {
                    "kind": "table",
                    "rows": [["В моей личной", "базе знаний", "релевантных документов", "не", "нашлось"]],
                }
            ],
        },
        {
            "kind": "xlsx",
            "title": "Сводка",
            "blocks": [{"kind": "table", "rows": [["У", "меня", "нет", "доступа", "к сервису"]]}],
        },
    ],
)
def test_a_whole_logical_carrier_row_cannot_split_an_output_violation(payload: dict) -> None:
    assert _carrier_projection_passes(payload, archive_status_guarded=True) is False


@pytest.mark.parametrize(
    "row",
    [
        ["Согласно журналу", "курьер", "заказан"],
        ["Автор пишет:", "я заказал курьера"],
        ["Цитата:", "Курьер заказан на утро"],
    ],
)
def test_a_whole_carrier_row_preserves_the_scope_of_a_source(row: list[str]) -> None:
    payload = {
        "kind": "xlsx",
        "title": "Синтетическая сводка",
        "blocks": [{"kind": "table", "rows": [row]}],
    }

    assert _carrier_projection_passes(payload, archive_status_guarded=True) is True
    guarded, allowed = _guard_model_carrier_payload(
        payload,
        archive_status_guarded=True,
        _text_guarded_by_projection=True,
    )
    assert allowed is True
    assert guarded == payload


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "docx",
            "title": "Сводка",
            "blocks": [
                {"kind": "heading", "text": "Я перевела"},
                {"kind": "text", "text": "две тысячи рублей на карту."},
            ],
        },
        {
            "kind": "docx",
            "title": "Сводка",
            "blocks": [
                {"kind": "heading", "text": "В моей личной"},
                {"kind": "text", "text": "базе знаний ответа не нашлось."},
            ],
        },
        {
            "kind": "docx",
            "title": "Сводка",
            "blocks": [
                {"kind": "heading", "text": "У меня нет"},
                {"kind": "text", "text": "доступа к сервису."},
            ],
        },
        {
            "kind": "docx",
            "title": "Сводка",
            "blocks": [{"kind": "paragraph", "text": "Я заказала курьера."}],
        },
        {
            "kind": "docx",
            "title": "Сводка",
            "blocks": [{"kind": "quote", "text": "Напоминание поставлено."}],
        },
        {
            "kind": "docx",
            "title": "Сводка",
            "filename": "Я заказала курьера.docx",
            "blocks": [{"kind": "text", "text": "Безопасный текст."}],
        },
        {
            "kind": "docx",
            "title": ["Я", "заказала курьера."],
            "blocks": [{"kind": "text", "text": "Безопасный текст."}],
        },
    ],
)
def test_the_carrier_guard_uses_the_same_visible_projection_as_the_renderer(payload: dict) -> None:
    assert _carrier_projection_passes(payload, archive_status_guarded=True) is False


@pytest.mark.parametrize(
    "title",
    [
        "Цитата: «Я заказала курьера.»",
        "Цитата: «Курьер заказан и уже едет далеко далеко»",
    ],
)
def test_xlsx_sheet_tab_transformation_is_part_of_the_carrier_projection(title: str) -> None:
    payload = {
        "kind": "xlsx",
        "title": title,
        "filename": "safe.xlsx",
        "blocks": [{"kind": "text", "text": "Безопасный синтетический текст."}],
    }

    assert _carrier_projection_passes(payload, archive_status_guarded=True) is False


@pytest.mark.parametrize(
    "payload",
    [
        {
            "kind": "xlsx",
            "title": "Сводка",
            "blocks": [
                {
                    "kind": "table",
                    "rows": [
                        {"a": "безопасно", "b": "безопасно"},
                        {"b": "Автор пишет:", "a": "Я заказала курьера."},
                    ],
                }
            ],
        },
        {
            "kind": "xlsx",
            "title": "Сводка",
            "blocks": [
                {
                    "kind": "table",
                    "rows": [
                        {key: "безопасно" for key in "abcdef"},
                        dict(
                            zip(
                                reversed("abcdef"),
                                reversed(["Я", "не", "могу", "выполнить", "это", "действие"]),
                                strict=True,
                            )
                        ),
                    ],
                }
            ],
        },
    ],
)
def test_mapping_rows_cannot_change_word_order_after_the_guard(payload: dict) -> None:
    assert _carrier_projection_passes(payload, archive_status_guarded=True) is False


@pytest.mark.asyncio
async def test_k17_hostile_repair_is_cleaned_before_return_and_persistence(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(
        settings,
        storage,
        monkeypatch,
        mode="general_conversation",
        verify_answers=True,
    )
    original = "Синтетический ответ, который намеренно отправляется на проверку."
    hostile_repair = _K17_STRIP[5]
    expected, changed, has_model_content = strip_unasked_archive_status(hostile_repair)
    assert changed is True and has_model_content is True

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {
            "content": original,
            "tools_used": ["synthetic_local_lookup"],
            "tool_evidence": [{"tool": "synthetic_local_lookup", "output": "Synthetic local evidence."}],
        }

    judged: list[str] = []

    async def verify(question, answer, context, *, tool_evidence=None):  # noqa: ANN001
        del question, context, tool_evidence
        judged.append(answer)
        if len(judged) == 1:
            return {"status": "failed", "ok": False, "score": 0.0, "issues": ["synthetic"]}
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def repair(question, answer, context, verdict, *, tool_evidence=None):  # noqa: ANN001
        del question, answer, context, verdict, tool_evidence
        return hostile_repair

    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime, "_repair_once", repair)

    reply = await runtime.chat(
        "alice",
        "Объясни ещё один синтетический общий принцип.",
        actor=_actor(),
        enable_tools=False,
    )

    assert judged == [original, expected]
    assert reply["message"] == expected
    assert hostile_repair not in repr(reply)
    assert _stored_assistant_content(storage, reply) == expected


@pytest.mark.parametrize(
    "generated",
    [
        _K17_STRIP[0],
        (
            "В твоих документах ответа нет. Но Синтетический отчёт\n\n"
            "Первый раздел содержит проверяемый общий принцип.\n\n"
            "Второй раздел содержит безопасный вывод."
        ),
    ],
)
@pytest.mark.asyncio
async def test_k17_guards_the_fresh_generation_inside_the_late_file_builder(
    generated: str,
) -> None:
    class _LateLLM:
        enabled = True

        async def chat(self, messages, *, tools=None, **kwargs):  # noqa: ANN001
            del messages, tools, kwargs
            return {"content": generated}

    class _RenderKernel:
        def __init__(self) -> None:
            self.arguments: list[dict] = []

        async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001
            del actor
            assert name == "make_file"
            self.arguments.append(arguments)

            class _Result:
                success = True
                attachment = {
                    "kind": "document",
                    "filename": "clean-late-synthetic.txt",
                    "mime_type": "text/plain",
                    "content_base64": "c3ludGhldGlj",
                }

            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.llm = _LateLLM()
    runtime.kernel = _RenderKernel()
    context = AgentContext(
        conversation_id="conv-late-k17",
        user_id="alice",
        person_id="alice",
        outward_verdict=("файл", None),
        answer_mode="general_conversation",
    )
    expected, changed, has_model_content = strip_unasked_archive_status(generated)
    assert changed is True

    made = await runtime._file_for_a_request_that_wanted_one(  # noqa: SLF001
        "Подготовь синтетический документ.",
        "Краткий ответ.",
        _actor(),
        evidence=[{"tool": "synthetic_local_lookup", "output": "Synthetic grounds."}],
        context=context,
    )

    if not has_model_content:
        assert made is None
        assert runtime.kernel.arguments == []
    else:
        assert made is not None
        rendered_arguments = json.dumps(runtime.kernel.arguments, ensure_ascii=False)
        for paragraph in expected.split("\n\n"):
            assert paragraph in rendered_arguments
        assert generated not in rendered_arguments


@pytest.mark.asyncio
async def test_k11_repair_is_augmented_and_a_bare_refusal_never_reaches_the_late_file_builder(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(
        settings,
        storage,
        monkeypatch,
        mode="general_conversation",
        verify_answers=True,
    )
    bare_refusal = _K11_AUGMENT[0]
    expected = add_useful_refusal_alternative(bare_refusal)

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("файл", None),
            answer_mode="general_conversation",
        )

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {
            "content": "Синтетический черновик для проверки.",
            "tools_used": ["synthetic_local_lookup"],
            "tool_evidence": [{"tool": "synthetic_local_lookup", "output": "Synthetic local evidence."}],
        }

    verification_calls = 0

    async def verify(question, answer, context, *, tool_evidence=None):  # noqa: ANN001
        del question, answer, context, tool_evidence
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 1:
            return {"status": "failed", "ok": False, "score": 0.0, "issues": ["synthetic"]}
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def repair(question, answer, context, verdict, *, tool_evidence=None):  # noqa: ANN001
        del question, answer, context, verdict, tool_evidence
        return bare_refusal

    late_builder_calls = 0

    async def build(  # noqa: ANN001
        request,
        answer,
        actor,
        *,
        evidence=None,
        context=None,
        literal_source_text=None,
    ):
        del request, answer, actor, evidence, context, literal_source_text
        nonlocal late_builder_calls
        late_builder_calls += 1
        return {
            "kind": "document",
            "filename": "must-not-exist.txt",
            "mime_type": "text/plain",
            "content_base64": "c3ludGhldGlj",
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    monkeypatch.setattr(runtime, "_repair_once", repair)
    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", build)

    reply = await runtime.chat(
        "alice",
        "Оформи синтетический результат в документ Word.",
        actor=_actor(),
        enable_tools=False,
    )

    assert verification_calls == 2
    assert reply["message"] == expected
    assert reply["message"].count(_REFUSAL_ALTERNATIVE) == 1
    assert late_builder_calls == 0
    assert reply["files"] == []
    assert _stored_assistant_content(storage, reply) == expected
    metadata = _stored_assistant_metadata(storage, reply)
    assert metadata["structural"]["output_guards"]["refusal_alternative_added"] is True


@pytest.mark.asyncio
async def test_k17_cannot_uncover_a_bare_refusal_after_k11_already_looked_at_the_prefix(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch, mode="general_conversation")
    composite = "В моей личной базе знаний ответа нет. Но я не могу выполнить это действие."
    expected_refusal = "я не могу выполнить это действие."

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": composite, "tools_used": []}

    monkeypatch.setattr(runtime, "_generate_response", generate)
    reply = await runtime.chat(
        "alice",
        "Объясни синтетический общий принцип.",
        actor=_actor(),
        enable_tools=False,
    )

    assert reply["message"] == f"{expected_refusal}\n\n{_REFUSAL_ALTERNATIVE}"
    metadata = _stored_assistant_metadata(storage, reply)
    assert metadata["structural"]["output_guards"] == {
        "outside_deed_replaced": False,
        "archive_status_replaced": True,
        "refusal_alternative_added": True,
    }


@pytest.mark.parametrize(
    "answer",
    [
        "Выполнить это я не могу.",
        "Доступа к внешнему сервису у меня нет.",
        "Я физически не могу это сделать.",
        "Я, увы, не могу это сделать.",
        "Сделать это не получится.",
        "Увы, доступа к сервису у меня нет.",
        "Я не могу подготовить этот текст.",
        "Заголовок\nНе могу подготовить документ.",
        "Раздел:\nНе могу оформить покупку.",
        "Этого я сделать не могу.",
        "Я, к сожалению, не могу выполнить это действие.",
        "У меня отсутствует доступ к сервису.",
        "Я лишена доступа к кабинету.",
        "Я не располагаю доступом к телефону.",
        "Я не в силах это выполнить.",
        "Это я сделать не умею.",
        "Я не мо\u200bгу выполнить это действие.",
        "Я не [могу](https://example.com) выполнить это действие.",
        "Я не `могу` выполнить это действие.",
    ],
)
def test_k11_handles_natural_refusal_word_orders(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True
    augmented = add_useful_refusal_alternative(answer)
    assert augmented.count(_REFUSAL_ALTERNATIVE) == 1
    assert add_useful_refusal_alternative(augmented) == augmented


@pytest.mark.parametrize(
    "answer",
    [
        "Без источника я не могу подтвердить этот факт.",
        "На этих данных я не могу сказать точно.",
        "По условию я не могу определить значение.",
        "Я не могу не отметить точность результата.",
        "«Я не могу выполнить это», — сказал персонаж.",
        "Цитата: Я не могу выполнить это действие.",
        "Перевод: I cannot — Я не могу выполнить это действие.",
        "Автор пишет: я не могу выполнить это действие.",
        "Согласно документу: Я не могу выполнить это действие.",
        "В документе написано: Я не могу выполнить это действие.",
        "По данным отчёта, я не могу выполнить это действие.",
        "Он сказал, что я не могу выполнить это действие.",
        "Пользователь написал: я не могу выполнить это действие.",
        "Не могу отправить документ наружу. Зато подготовлю документ здесь.",
        "Этого я сделать не могу?",
        "У меня нет доступа. Его можно запросить у администратора.",
        "У меня нет доступа, но администратор может его выдать.",
    ],
)
def test_k11_keeps_uncertainty_quotes_idioms_and_existing_alternatives(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is False
    assert add_useful_refusal_alternative(answer) == answer


@pytest.mark.parametrize(
    "answer",
    [
        "Я не могу помочь взломать чужой аккаунт.",
        "Не могу изготовить взрывное устройство.",
        "Я не могу раскрыть чужие персональные данные.",
        "Я не могу удалить все данные без подтверждения.",
    ],
)
def test_k11_never_advises_bypassing_a_safety_or_privacy_refusal(answer: str) -> None:
    augmented = add_useful_refusal_alternative(answer)

    assert augmented.startswith(answer)
    assert "безопасный" in augmented.casefold()
    assert "выполни это вручную" not in augmented.casefold()
    assert "сервису с нужным доступом" not in augmented.casefold()


@pytest.mark.parametrize(
    "answer",
    [
        "Вот краткий и исчерпывающий ответ на ваш вопрос. Я не могу выполнить это действие.",
        "Отчёт\nСтатус\nИтог\nНе могу выполнить это действие.",
    ],
)
def test_k11_reaches_the_first_substantive_refusal_after_display_preambles(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "Я не в состоянии это сделать.",
        "Такой возможности у меня нет.",
        "Возможности сделать это у меня нет.",
        "Мне недоступна эта функция.",
        "Это за пределами моих возможностей.",
        "Не смогу это сделать.",
        "Мне это не по силам.",
    ],
)
def test_k11_recognises_common_capability_refusal_synonyms(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is True


@pytest.mark.parametrize(
    "alternative",
    [
        "Предлагаю подготовить инструкцию.",
        "Лучше подготовить инструкцию.",
        "Можно подготовить инструкцию.",
        "Попробуй обратиться в поддержку.",
        "Давай составим пошаговый план.",
        "Советую проверить настройки.",
    ],
)
def test_k11_preserves_a_real_existing_next_step(alternative: str) -> None:
    answer = f"Я не могу выполнить это действие. {alternative}"

    assert refusal_lacks_useful_alternative(answer) is False
    assert add_useful_refusal_alternative(answer) == answer


@pytest.mark.parametrize(
    "answer",
    [
        "Пример отказа: я не могу это сделать.",
        "Сообщение сервиса: я не могу выполнить запрос.",
        "Я не могу согласиться с этим выводом.",
        "Я не могу понять, почему результат такой.",
    ],
)
def test_k11_does_not_rewrite_a_report_or_an_intellectual_disagreement(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is False


@pytest.mark.parametrize(
    "ending",
    [
        "Но я могу.",
        "Вместо этого — нет.",
        "Могу найти.",
        "Но могу проверить.",
    ],
)
def test_k11_does_not_accept_an_empty_alternative(ending: str) -> None:
    answer = f"Я не могу выполнить это действие. {ending}"

    assert refusal_lacks_useful_alternative(answer) is True


@pytest.mark.parametrize(
    "ending",
    [
        "Тогда я.",
        "Тогда можно.",
        "Зато подготовлю.",
        "Зато могу сделать.",
        "Открой.",
    ],
)
def test_k11_requires_an_action_and_an_object_in_the_alternative(ending: str) -> None:
    assert refusal_lacks_useful_alternative(f"Я не могу выполнить это действие. {ending}") is True


@pytest.mark.parametrize(
    "alternative",
    [
        "Как вариант, подготовлю инструкцию.",
        "Доступный вариант — обратиться в поддержку.",
        "Решение: обратиться в поддержку.",
        "Ты можешь обратиться в поддержку.",
        "Стоит обратиться в поддержку.",
    ],
)
def test_k11_recognises_more_natural_real_alternatives(alternative: str) -> None:
    assert refusal_lacks_useful_alternative(f"Я не могу выполнить это действие. {alternative}") is False


@pytest.mark.parametrize(
    "answer",
    [
        "Мне недоступна информация о результате.",
        "Я не смогу подтвердить точность ответа.",
        "Я не в состоянии определить точный результат.",
    ],
)
def test_k11_does_not_treat_epistemic_uncertainty_as_a_capability_refusal(answer: str) -> None:
    assert refusal_lacks_useful_alternative(answer) is False


@pytest.mark.asyncio
async def test_k11_does_not_rewrite_the_structural_model_outage_message(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch, mode="general_conversation")
    outage = "⚠️ Не могу связаться с моделью — она не отвечает. Отвечу, как только она поднимется."

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("файл", None),
            answer_mode="general_conversation",
        )

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {
            "content": outage,
            "tools_used": ["synthetic_local_lookup"],
            "tool_evidence": [{"tool": "synthetic_local_lookup", "output": "Synthetic grounds."}],
            "llm_failed": True,
        }

    late_builder_calls = 0

    async def build(  # noqa: ANN001
        request,
        answer,
        actor,
        *,
        evidence=None,
        context=None,
        literal_source_text=None,
    ):
        del request, answer, actor, evidence, context, literal_source_text
        nonlocal late_builder_calls
        late_builder_calls += 1
        raise AssertionError("model outage reached the late file builder")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", build)
    reply = await runtime.chat(
        "alice",
        "Оформи синтетический ответ в документ.",
        actor=_actor(),
        enable_tools=False,
    )

    assert reply["message"] == outage
    assert _REFUSAL_ALTERNATIVE not in reply["message"]
    assert reply["files"] == []
    assert late_builder_calls == 0


@pytest.mark.asyncio
async def test_k11_explicit_local_file_alternative_allows_the_late_builder(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch, mode="general_conversation")
    refusal = "Не могу отправить документ наружу. Зато подготовлю файл здесь."

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            outward_verdict=("файл", None),
            answer_mode="general_conversation",
        )

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message, attachments
        return {"content": refusal, "tools_used": []}

    built_from: list[str] = []

    async def build(  # noqa: ANN001
        request,
        answer,
        actor,
        *,
        evidence=None,
        context=None,
        literal_source_text=None,
    ):
        del request, actor, evidence, context, literal_source_text
        built_from.append(answer)
        return {
            "kind": "document",
            "filename": "local-synthetic.txt",
            "mime_type": "text/plain",
            "content_base64": "c3ludGhldGlj",
        }

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", build)
    reply = await runtime.chat(
        "alice",
        "Подготовь документ здесь и не отправляй наружу.",
        actor=_actor(),
        enable_tools=False,
    )

    assert reply["message"] == refusal
    assert built_from == [refusal]
    assert len(reply["files"]) == 1
