from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.turn_intent_policy import (
    ADMIN_DIAGNOSTICS_CAPABILITY,
    CODE_OWNED_CAPABILITY_PROJECTION,
    WEATHER_LOCATION_CHALLENGE_RESPONSE,
    WEATHER_LOCATION_CLARIFICATION,
    AttachmentDisposition,
    DiagnosticsAuthority,
    DiagnosticsState,
    ImageGenerationProjection,
    IntegrationProjection,
    LocationSource,
    SafeDiagnosticsProjection,
    TurnIntent,
    TurnPolicyContext,
    WeatherHorizon,
    WebDisposition,
    decide_turn_policy,
    project_safe_diagnostics,
)


@pytest.mark.parametrize(
    "message",
    [
        "подскажи лучше погоду на сегодня",
        "Что по погоде?",
        "Прогноз на завтра, пожалуйста",
        "погода в пятницу",
        "погода во вторник",
        "погода для меня",
        "погода в моём городе",
        "погода здесь",
        "погода в городе",
    ],
)
def test_weather_without_explicit_location_closes_web_and_asks_once(message: str) -> None:
    decision = decide_turn_policy(message)

    assert decision.intent is TurnIntent.WEATHER_NEEDS_LOCATION
    assert decision.web is WebDisposition.DENY
    assert decision.attachments is AttachmentDisposition.NONE
    assert decision.location is None
    assert decision.public_response == "Для какого города или населённого пункта нужен прогноз?"


@pytest.mark.parametrize(
    ("message", "horizon"),
    [
        ("погоду на сегодня", WeatherHorizon.TODAY),
        ("прогноз на завтра", WeatherHorizon.TOMORROW),
        ("погода сейчас", WeatherHorizon.CURRENT),
        ("погода в пятницу", None),
    ],
)
def test_weather_horizon_is_closed_and_does_not_require_a_location(
    message: str,
    horizon: WeatherHorizon | None,
) -> None:
    decision = decide_turn_policy(message)

    assert decision.intent is TurnIntent.WEATHER_NEEDS_LOCATION
    assert decision.weather_horizon is horizon


@pytest.mark.parametrize(
    ("message", "location"),
    [
        ("Какая погода завтра в Донецке?", "Донецке"),
        ("погода на сегодня во Владимире", "Владимире"),
        ("В Москве какая погода завтра?", "Москве"),
        ("weather in Paris tomorrow", "Paris"),
        ("Прогноз для Нижнего Новгорода сегодня", "Нижнего Новгорода"),
        ("погода в пятницу в Донецке", "Донецке"),
    ],
)
def test_only_location_in_user_text_opens_weather_web(message: str, location: str) -> None:
    decision = decide_turn_policy(message)

    assert decision.intent is TurnIntent.WEATHER_WITH_LOCATION
    assert decision.web is WebDisposition.ALLOW_EXPLICIT_WEATHER
    assert decision.location == location
    assert decision.location_source is LocationSource.EXPLICIT_USER_TEXT
    assert decision.public_response is None
    assert decision.attachments is AttachmentDisposition.NONE


@pytest.mark.parametrize(
    ("message", "location"),
    [
        ("Меня Донецк интересовал))", "Донецк"),
        ("я про Ростов-на-Дону", "Ростов-на-Дону"),
        ("Донецк", "Донецк"),
    ],
)
def test_adjacent_weather_correction_uses_only_the_new_user_text(
    message: str,
    location: str,
) -> None:
    context = TurnPolicyContext(weather_followup=True)

    decision = decide_turn_policy(message, context=context)

    assert decision.intent is TurnIntent.WEATHER_WITH_LOCATION
    assert decision.location == location
    assert decision.location_source is LocationSource.EXPLICIT_USER_TEXT
    assert decision.web is WebDisposition.ALLOW_EXPLICIT_WEATHER
    assert context == TurnPolicyContext(weather_followup=True)


@pytest.mark.parametrize(
    "message",
    [
        "спасибо",
        "спасибо, всё понятно",
        "нет, не надо",
        "не надо",
        "ладно",
        "ок",
        "в интернете",
        "дома",
        "на работе",
    ],
)
def test_adjacent_weather_smalltalk_and_generic_places_never_open_web(message: str) -> None:
    decision = decide_turn_policy(
        message,
        context=TurnPolicyContext(weather_followup=True, weather_location_missing=True),
    )

    assert decision.intent is TurnIntent.PASSTHROUGH
    assert decision.location is None
    assert decision.web is WebDisposition.UNCHANGED


@pytest.mark.parametrize(
    ("message", "location"),
    [
        ("Донецк", "Донецк"),
        ("в Донецке", "Донецке"),
        ("Нижний Новгород", "Нижний Новгород"),
        ("Ростов-на-Дону", "Ростов-на-Дону"),
    ],
)
def test_adjacent_bare_city_grammar_remains_available(message: str, location: str) -> None:
    decision = decide_turn_policy(
        message,
        context=TurnPolicyContext(weather_followup=True, weather_location_missing=True),
    )

    assert decision.intent is TurnIntent.WEATHER_WITH_LOCATION
    assert decision.location == location
    assert decision.web is WebDisposition.ALLOW_EXPLICIT_WEATHER


@pytest.mark.parametrize("message", ["погода в интернете", "погода в моём городе", "погода дома"])
def test_weather_generic_location_substitutes_ask_for_a_real_place(message: str) -> None:
    decision = decide_turn_policy(message)

    assert decision.intent is TurnIntent.WEATHER_NEEDS_LOCATION
    assert decision.web is WebDisposition.DENY
    assert decision.location is None


@pytest.mark.parametrize(
    "message",
    [
        "найди прогноз погоды в нашей переписке",
        "найди в истории сообщения про погоду в Донецке",
        "найди сообщения где я писал про погоду",
        "найди в истории сообщения про диагностику системы",
        "что написано в файле про диагностику системы?",
        "в переписке упоминалась погода?",
        "где в переписке упоминалась погода?",
        "что было в прогнозе погоды, который я тебе отправил?",
        "прочитай отчёт о диагностике системы",
        "открой диагностика системы.pdf",
        "какая погода в отчёте?",
        "какая погода указана в документе?",
        "погода из присланного файла",
    ],
)
def test_data_read_subjects_never_become_live_weather_or_diagnostics(message: str) -> None:
    decision = decide_turn_policy(
        message,
        diagnostics=DiagnosticsAuthority(capability_allowed=True),
    )

    assert decision.intent is TurnIntent.PASSTHROUGH
    assert decision.web is WebDisposition.UNCHANGED
    assert decision.attachments is AttachmentDisposition.UNCHANGED
    assert decision.location is None


def test_adjacent_weather_correction_inherits_only_the_closed_horizon() -> None:
    context = TurnPolicyContext(
        weather_followup=True,
        weather_horizon=WeatherHorizon.TODAY,
    )

    decision = decide_turn_policy("Меня Донецк интересовал", context=context)

    assert decision.intent is TurnIntent.WEATHER_WITH_LOCATION
    assert decision.weather_horizon is WeatherHorizon.TODAY


def test_weather_horizon_rejects_ambient_free_text() -> None:
    with pytest.raises(ValueError, match="closed vocabulary"):
        TurnPolicyContext(weather_followup=True, weather_horizon="на выходных")  # type: ignore[arg-type]


def test_turn_policy_context_accepts_only_a_code_owned_attachment_marker() -> None:
    with pytest.raises(ValueError, match="current attachment marker"):
        TurnPolicyContext(current_attachment_present=1)  # type: ignore[arg-type]


def test_weather_correction_needs_adjacent_weather_context() -> None:
    assert decide_turn_policy("Меня Донецк интересовал))").intent is TurnIntent.PASSTHROUGH


def test_challenge_about_a_wrong_location_does_not_start_another_weather_search() -> None:
    decision = decide_turn_policy(
        "а почему во Владимире?",
        context=TurnPolicyContext(
            weather_followup=True,
            weather_horizon=WeatherHorizon.TODAY,
            weather_location_missing=True,
        ),
    )

    assert decision.intent is TurnIntent.WEATHER_LOCATION_CHALLENGE
    assert decision.web is WebDisposition.DENY
    assert decision.attachments is AttachmentDisposition.NONE
    assert decision.weather_horizon is WeatherHorizon.TODAY
    assert decision.public_response == WEATHER_LOCATION_CHALLENGE_RESPONSE
    assert "город не был указан" in decision.public_response
    assert "не использую геолокацию" in decision.public_response


def test_location_challenge_without_an_adjacent_missing_location_is_ordinary_text() -> None:
    assert decide_turn_policy("а почему во Владимире?").intent is TurnIntent.PASSTHROUGH


@pytest.mark.parametrize(
    "message",
    [
        "ты хранишь всю историю переписки?",
        "Ты понимаешь, что у тебя есть sqlite база?",
        "ты ведь понимаешь, что у тебя есть sqlite база где записаны вся переписка и все входящие файлы?",
        "можешь ли ты искать сообщения по датам?",
        "где ты хранишь принятые файлы?",
    ],
)
def test_meta_questions_have_code_owned_truth_and_never_restore_attachments(message: str) -> None:
    decision = decide_turn_policy(message)

    assert decision.intent is TurnIntent.META_CAPABILITIES
    assert decision.web is WebDisposition.DENY
    assert decision.attachments is AttachmentDisposition.NONE
    assert decision.capability_projection is CODE_OWNED_CAPABILITY_PROJECTION
    assert decision.capability_projection.accepted_own_chat_rows_durable is True
    assert decision.capability_projection.accepted_own_chat_rows_deletable is False
    assert decision.capability_projection.prompt_window_is_full_history is False
    assert decision.capability_projection.message_search_reads_full_own_history is True
    assert decision.capability_projection.message_search_paginated is True
    assert decision.capability_projection.accepted_own_history_complete_via_pagination is True
    assert decision.capability_projection.accepted_files_persisted is True
    assert "неудаляемой строке" in str(decision.public_response)
    assert "короткое актуальное окно" in str(decision.public_response)
    assert "всю принятую историю этого чата постранично" in str(decision.public_response)
    assert "срок хранения" not in str(decision.public_response)


@pytest.mark.parametrize(
    "message",
    [
        "выведи всю переписку за 13 августа",
        'найди файлы которые я загружал, где содержится слово "штатка"',
        "какие у меня есть загруженные файлы?",
        "что в этом файле?",
    ],
)
def test_data_read_commands_are_not_meta_capability_questions(message: str) -> None:
    decision = decide_turn_policy(message)

    assert decision.intent is TurnIntent.PASSTHROUGH
    assert decision.attachments is AttachmentDisposition.UNCHANGED


@pytest.mark.parametrize(
    "message",
    [
        "Можешь сделать картинку?",
        "а ты мне можешь картинку нарисовать?",
        "Пятница, а ты мне можешь картинку нарисовать?",
        "Ты вообще умеешь рисовать картинки?",
        "Ты можешь генерить картинки?",
        "Ты умеешь генерировать изображения?",
        "Умеешь ли ты создавать картинки?",
        "Способна ли ты нарисовать изображение по текстовому описанию?",
        "Поддерживаешь генерацию изображений?",
        "Есть ли у тебя генерация изображений?",
        "А изображения генерировать умеешь?",
        "Генерация картинок у тебя есть?",
        "Рисунки делать можешь?",
        "Ты картинки рисовать умеешь?",
        "Картинки умеешь рисовать?",
        "Есть ли у тебя возможность генерировать изображения?",
        "Скажи, ты умеешь рисовать картинки?",
        "Подскажи, можешь ли ты генерировать изображения?",
        "Ты генерируешь картинки?",
        "Ты умеешь генерировать фото?",
        "Ты умеешь создавать изображения из текстового промпта?",
        "Скажи, а ты умеешь рисовать картинки?",
        "Подскажи пожалуйста, а ты генерируешь изображения?",
        "Ты действительно умеешь рисовать картинки?",
        "Ты умеешь рисовать картинки или нет?",
        "Can you draw pictures?",
        "Friday can you generate images?",
        "Can you generate images from text prompts?",
        "Can you actually generate images?",
        "Do you generate images?",
        "Do you support generating images?",
        "Can you also generate images?",
        "Do you really generate images?",
        "Can Friday generate images?",
        "Do you have image generation?",
        "Are you able to generate an image from a text description?",
        "Are you capable of drawing pictures?",
        "Do you support image generation?",
        "Do you have the ability to generate images?",
        "Is image generation available?",
    ],
)
def test_image_generation_capability_has_code_owned_truth(message: str) -> None:
    projection = ImageGenerationProjection(structured_png_card_available=True)

    decision = decide_turn_policy(message, image_generation=projection)

    assert decision.intent is TurnIntent.META_IMAGE_GENERATION
    assert decision.web is WebDisposition.DENY
    assert decision.attachments is AttachmentDisposition.NONE
    assert decision.image_generation_projection is projection
    assert decision.public_response == projection.render_ru()
    assert "Обычные картинки и рисунки по описанию сейчас не генерирую" in str(decision.public_response)
    assert "текстовую карточку или сводку" in str(decision.public_response)


def test_image_generation_capability_fails_closed_without_a_visible_png_renderer() -> None:
    decision = decide_turn_policy("Ты умеешь рисовать картинки?")

    assert decision.intent is TurnIntent.META_IMAGE_GENERATION
    assert decision.image_generation_projection == ImageGenerationProjection(False)
    assert decision.public_response == ImageGenerationProjection(False).render_ru()
    assert "сейчас тоже недоступна" in str(decision.public_response)


@pytest.mark.parametrize(
    "message",
    [
        "Можешь сделать картинку?",
        "Скажи, ты умеешь рисовать картинки?",
        "Ты генерируешь картинки?",
        "Подскажи пожалуйста, а ты генерируешь изображения?",
        "Can you make an image?",
        "Can you actually generate images?",
        "Can Friday generate images?",
    ],
)
def test_current_attachment_keeps_terse_image_wording_on_the_runtime_path(
    message: str,
) -> None:
    decision = decide_turn_policy(
        message,
        context=TurnPolicyContext(current_attachment_present=True),
        image_generation=ImageGenerationProjection(structured_png_card_available=True),
    )

    assert decision.intent is TurnIntent.PASSTHROUGH
    assert decision.attachments is AttachmentDisposition.UNCHANGED
    assert decision.public_response is None


@pytest.mark.parametrize(
    ("category", "message"),
    [
        ("imperative", "Нарисуй картинку."),
        ("imperative", "Сгенерируй изображение."),
        ("imperative", "Скажи, нарисуй картинку."),
        ("content-brief", "Можешь нарисовать картинку с рыжим котом?"),
        ("content-brief", "Ты можешь генерить картинку космического корабля?"),
        ("content-brief", "Подскажи, можешь ли ты генерировать изображение кота?"),
        ("content-brief", "Подскажи, а можешь нарисовать картинку с котом?"),
        ("compound", "Можешь нарисовать картинку и поискать последние новости?"),
        ("compound", "Ты генерируешь картинки и ищешь новости?"),
        ("conditional-work", "Если можешь, нарисуй картинку."),
        ("history", "Ты уже нарисовала картинку?"),
        ("history", "Ты раньше умела рисовать картинки?"),
        ("current-work", "Ты сейчас генерируешь изображение?"),
        ("current-work", "Ты генерируешь эту картинку?"),
        ("current-work", "Скажи, а ты сейчас генерируешь картинки?"),
        ("current-work", "Ты действительно умеешь рисовать эту картинку?"),
        ("current-work", "Ты можешь сейчас сгенерировать картинку?"),
        ("different-capability", "Можешь проанализировать эту картинку?"),
        ("attached-work", "Можешь сделать картинку по этому файлу?"),
        ("reported", "Он спросил: «ты можешь картинку нарисовать?»"),
        ("reported", "Он спросил: «Ты генерируешь картинки?»"),
        ("quoted", "«Ты вообще умеешь рисовать картинки?»"),
        ("quoted", "`ты можешь картинку нарисовать?`"),
        ("english-content-brief", "Can you draw me a cat?"),
        ("english-content-brief", "Can you actually generate an image of a cat?"),
        ("english-content-brief", "Can you generate images from this text prompt?"),
        ("english-content-brief", "Can Friday generate an image of a cat?"),
        ("english-compound", "Can you draw an image and search for the latest news?"),
        ("english-compound", "Can you also generate images and search the web?"),
        ("english-compound", "Do you support generating images and video?"),
        ("english-content-brief", "Do you generate images from the attached file?"),
        ("english-history", "Did you generate images yesterday?"),
        ("english-current-work", "Are you generating an image now?"),
        ("english-current-work", "Do you generate images now?"),
        ("english-current-work", "Do you really generate images now?"),
        ("english-reported", "He asked: ‘Do you generate images?’"),
        ("english-reported", 'He said: "Can Friday generate images?"'),
        ("english-quoted", "`Do you have image generation?`"),
    ],
)
def test_image_creation_work_and_reported_capability_text_stay_on_the_runtime_path(
    category: str,
    message: str,
) -> None:
    decision = decide_turn_policy(
        message,
        image_generation=ImageGenerationProjection(structured_png_card_available=True),
    )

    assert decision.intent is TurnIntent.PASSTHROUGH, category
    assert decision.public_response is None
    assert decision.attachments is AttachmentDisposition.UNCHANGED


def test_image_generation_projection_accepts_only_the_shipped_capability_shape() -> None:
    with pytest.raises(ValueError, match="exact boolean"):
        ImageGenerationProjection(structured_png_card_available=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a shipped capability"):
        ImageGenerationProjection(
            structured_png_card_available=True,
            freeform_generation_available=True,
        )


@pytest.mark.parametrize(
    ("visible_tools", "supported_kinds", "available"),
    [
        (["make_file"], ["docx", "pdf", "png", "xlsx"], True),
        ([], ["docx", "pdf", "png", "xlsx"], False),
        (["make_file"], ["docx", "pdf", "xlsx"], False),
    ],
)
def test_live_image_projection_requires_both_caller_visibility_and_png_schema(
    visible_tools: list[str],
    supported_kinds: list[str],
    available: bool,
    settings: Any,
) -> None:
    del settings  # importing ``friday.server`` requires the isolated test environment
    from friday.server import _live_image_generation_projection

    class _Spec:
        parameters = {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": supported_kinds}},
        }

    class _Kernel:
        @staticmethod
        def get_tool(name: str) -> object | None:
            return _Spec() if name == "make_file" else None

        @staticmethod
        def get_tool_names(_actor: object) -> list[str]:
            return visible_tools

    projection = _live_image_generation_projection(_Kernel(), object())  # type: ignore[arg-type]

    assert projection.structured_png_card_available is available
    assert projection.freeform_generation_available is False


@pytest.mark.parametrize(
    ("message", "projection", "expected_fragment"),
    [
        (
            "у тебя есть MCP?",
            IntegrationProjection(False, False, 0),
            "MCP для этой установки не настроен.",
        ),
        (
            "у тебя подключён MCP?",
            IntegrationProjection(True, False, 7),
            "MCP настроен, но соединение сейчас недоступно.",
        ),
        (
            "Есть ли у тебя MCP?",
            IntegrationProjection(True, True, 7),
            "MCP подключён.",
        ),
        (
            "у тебя MCP подключен?",
            IntegrationProjection(True, True, 0),
            "MCP подключён.",
        ),
        (
            "а mcp какие тебе доступны?",
            IntegrationProjection(True, True, 4),
            "MCP подключён.",
        ),
        (
            "какие MCP-инструменты доступны?",
            IntegrationProjection(True, True, 4),
            "MCP подключён.",
        ),
        (
            "какие MCP серверы подключены?",
            IntegrationProjection(True, False, 4),
            "MCP настроен, но соединение сейчас недоступно.",
        ),
    ],
)
def test_mcp_meta_uses_only_the_supplied_live_projection(
    message: str,
    projection: IntegrationProjection,
    expected_fragment: str,
) -> None:
    decision = decide_turn_policy(message, integrations=projection)

    assert decision.intent is TurnIntent.META_INTEGRATIONS
    assert decision.web is WebDisposition.DENY
    assert decision.attachments is AttachmentDisposition.NONE
    assert decision.integration_projection is projection
    assert expected_fragment in str(decision.public_response)
    if projection.mcp_configured:
        assert str(projection.allowlisted_tool_count) in str(decision.public_response)
    if projection.mcp_connected:
        assert "MCP для этой установки не настроен" not in str(decision.public_response)


def test_mcp_meta_without_provider_fact_refuses_to_invent_status() -> None:
    decision = decide_turn_policy("MCP у тебя подключен?")

    assert decision.intent is TurnIntent.META_INTEGRATIONS
    assert decision.web is WebDisposition.DENY
    assert decision.integration_projection is None
    assert "не передал статус MCP" in str(decision.public_response)
    assert "MCP для этой установки не настроен" not in str(decision.public_response)


def test_mcp_control_request_is_not_rewritten_as_status_meta() -> None:
    decision = decide_turn_policy("подключи MCP сервер из локальной конфигурации")

    assert decision.intent is TurnIntent.PASSTHROUGH
    assert decision.web is WebDisposition.UNCHANGED


def test_integration_projection_is_frozen_and_has_no_names_or_paths() -> None:
    projection = IntegrationProjection(True, True, 12)

    assert {field.name for field in fields(IntegrationProjection)} == {
        "mcp_configured",
        "mcp_connected",
        "allowlisted_tool_count",
    }
    assert projection.render_ru().endswith("12.")
    with pytest.raises(FrozenInstanceError):
        projection.mcp_connected = False  # type: ignore[misc]


@pytest.mark.parametrize(
    "projection",
    [
        lambda: IntegrationProjection(False, True, 0),
        lambda: IntegrationProjection(False, False, 1),
        lambda: IntegrationProjection(True, True, -1),
        lambda: IntegrationProjection(True, True, True),
    ],
)
def test_integration_projection_rejects_incoherent_provider_facts(projection: object) -> None:
    with pytest.raises(ValueError):
        projection()  # type: ignore[operator]


def test_server_mcp_projection_uses_the_live_manager_and_exact_allowlist_count(settings: Any) -> None:
    from friday.server import _live_integration_projection

    class _Manager:
        def is_available(self, alias: str) -> bool:
            assert alias == "workspace"
            return True

    projection = _live_integration_projection(replace(settings, mcp_enabled=True), _Manager())

    assert projection == IntegrationProjection(
        mcp_configured=True,
        mcp_connected=True,
        allowlisted_tool_count=4,
    )
    assert "MCP подключён" in projection.render_ru()


@pytest.mark.parametrize(
    ("capability_allowed", "intent"),
    [
        (False, TurnIntent.LOCAL_DIAGNOSTICS_DENIED),
        (True, TurnIntent.LOCAL_DIAGNOSTICS),
    ],
)
def test_local_diagnostics_requires_exact_capability_only(
    capability_allowed: bool,
    intent: TurnIntent,
) -> None:
    decision = decide_turn_policy(
        "проведи самодиагностику через интернет",
        diagnostics=DiagnosticsAuthority(capability_allowed=capability_allowed),
    )

    assert decision.intent is intent
    assert decision.web is WebDisposition.DENY
    assert decision.attachments is AttachmentDisposition.NONE
    assert decision.required_capability == ADMIN_DIAGNOSTICS_CAPABILITY
    assert decision.local_diagnostics_allowed is capability_allowed


def test_diagnostics_authority_rejects_non_boolean_claims() -> None:
    with pytest.raises(ValueError, match="exact boolean"):
        DiagnosticsAuthority(capability_allowed=1)  # type: ignore[arg-type]


def test_safe_diagnostics_projection_drops_raw_fields_and_does_not_mutate_source() -> None:
    secret = "DIAGNOSTIC-SECRET-CANARY"
    report: dict[str, object] = {
        "state": "degraded",
        "api_key": secret,
        "actions": [
            {"severity": "error", "detail": secret, "command": secret},
            {"severity": "warning", "title": secret},
            {"severity": "setup", "path": secret},
        ],
    }
    before = deepcopy(report)

    projection = project_safe_diagnostics(report)
    rendered = projection.render_ru()

    assert projection == SafeDiagnosticsProjection(
        state=DiagnosticsState.DEGRADED,
        errors=1,
        warnings=1,
        setup_actions=1,
    )
    assert report == before
    assert secret not in repr(projection)
    assert secret not in rendered
    assert len(rendered) <= 640


def test_safe_diagnostics_projection_fails_closed_and_bounds_actions() -> None:
    report: dict[str, object] = {
        "state": "ready",
        "actions": [{"severity": "warning", "detail": str(index)} for index in range(300)],
    }

    projection = project_safe_diagnostics(report)

    assert projection.state is DiagnosticsState.UNKNOWN
    assert projection.warnings == 256
    assert projection.actions_truncated is True
    assert projection.source_valid is False
    assert "Проекция неполна" in projection.render_ru()


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"state": "invented", "actions": []},
        {"state": "ready", "actions": [{"severity": "invented"}]},
    ],
)
def test_safe_diagnostics_projection_rejects_malformed_source(report: dict[str, object]) -> None:
    projection = project_safe_diagnostics(report)

    assert projection.state is DiagnosticsState.UNKNOWN
    assert projection.source_valid is False


def test_casual_health_question_is_not_privileged_diagnostics() -> None:
    assert decide_turn_policy("Пятница, ты как?").intent is TurnIntent.PASSTHROUGH


def test_public_results_are_frozen_and_repeatable() -> None:
    first = decide_turn_policy("ты хранишь всю историю переписки?")
    second = decide_turn_policy("ты хранишь всю историю переписки?")

    assert first == second
    with pytest.raises(FrozenInstanceError):
        first.intent = TurnIntent.PASSTHROUGH  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        CODE_OWNED_CAPABILITY_PROJECTION.local_state = False  # type: ignore[misc]


def test_api_uses_code_owned_weather_meta_mcp_and_safe_diagnostics(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: bypassing policy in server/runtime reaches the forbidden kernel."""

    import friday.server as server_module

    secret = "RAW-DIAGNOSTIC-CANARY"
    monkeypatch.setattr(
        server_module,
        "collect_diagnostics",
        lambda *_args, **_kwargs: {
            "state": "degraded",
            "actions": [
                {"severity": "error", "detail": secret},
                {"severity": "warning", "path": secret},
            ],
            "secret": secret,
        },
    )
    app = server_module.create_app(replace(settings, verify_answers=False))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    expected_fragments = (
        (
            "Что по погоде?",
            WEATHER_LOCATION_CLARIFICATION,
            TurnIntent.WEATHER_NEEDS_LOCATION,
        ),
        (
            "ты хранишь всю историю переписки?",
            "message_search читает всю принятую историю",
            TurnIntent.META_CAPABILITIES,
        ),
        (
            "у тебя подключён MCP?",
            "MCP для этой установки не настроен.",
            TurnIntent.META_INTEGRATIONS,
        ),
        (
            "а mcp какие тебе доступны?",
            "MCP для этой установки не настроен.",
            TurnIntent.META_INTEGRATIONS,
        ),
        (
            "а ты мне можешь картинку нарисовать?",
            "Обычные картинки и рисунки по описанию сейчас не генерирую.",
            TurnIntent.META_IMAGE_GENERATION,
        ),
        (
            "Можешь сделать картинку?",
            "Обычные картинки и рисунки по описанию сейчас не генерирую.",
            TurnIntent.META_IMAGE_GENERATION,
        ),
        (
            "проведи самодиагностику",
            "Действия: ошибок — 1, предупреждений — 1",
            TurnIntent.LOCAL_DIAGNOSTICS,
        ),
    )

    with TestClient(app) as client:

        class _ForbiddenModel:
            enabled = True
            model = "forbidden-policy-model"
            total_budget_sec = 1.0

            async def chat(self, *_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("code-owned policy turn reached the model")

        async def forbidden_kernel(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("code-owned policy turn reached a tool")

        app.state.agent.llm = _ForbiddenModel()
        app.state.kernel.execute = forbidden_kernel
        conversation_ids: list[tuple[str, TurnIntent]] = []
        for message, expected, intent in expected_fragments:
            response = client.post("/api/chat", json={"message": message}, headers=headers)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert expected in str(payload["message"])
            assert secret not in response.text
            assert payload["tools_used"] == []
            conversation_ids.append((str(payload["conversation_id"]), intent))

        owner = str(client.get("/api/me", headers=headers).json()["actor"]["user_id"])
        for conversation_id, intent in conversation_ids:
            rows = app.state.storage.get_conversation_messages(
                conversation_id,
                user_id=owner,
                limit=10,
            )
            assert len(rows) == 2
            assert rows[0]["role"] == "user"
            assert rows[1]["role"] == "assistant"
            assert rows[1]["reply_to"] == rows[0]["id"]
            user_metadata = json.loads(str(rows[0]["metadata_json"] or "{}"))
            assistant_metadata = json.loads(str(rows[1]["metadata_json"] or "{}"))
            assert user_metadata["turn_policy_intent"] == intent.value
            assert assistant_metadata["turn_policy_intent"] == intent.value
            assert assistant_metadata["structural"]["model_spoke"] is False
        assert int(app.state.storage.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]) == 0


@pytest.mark.parametrize(
    "message",
    [
        "Ты понимаешь, что у тебя есть sqlite база?",
        ("ты ведь понимаешь, что у тебя есть sqlite база где записаны вся переписка и все входящие файлы?"),
    ],
)
def test_api_sqlite_meta_prompts_never_materialize_restored_attachments(
    settings: Any,
    message: str,
) -> None:
    """Historical attachment lineage is data, not authority for a meta question."""

    import friday.server as server_module

    class _ForbiddenModel:
        enabled = True
        model = "forbidden-meta-model"
        total_budget_sec = 1.0

        async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("code-owned SQLite meta turn reached the model")

    app = server_module.create_app(replace(settings, verify_answers=False))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        owner = str(client.get("/api/me", headers=headers).json()["actor"]["user_id"])
        conversation = app.state.storage.create_conversation(owner, title="meta attachment boundary")
        conversation_id = str(conversation["id"])
        raw_id = "raw_0000000000000001"
        lineage = {
            "conversation_attachment_raw_ids": [raw_id],
            "conversation_attachment_uploaders": {raw_id: owner},
            "private_context_lineage": True,
        }
        seed_user = app.state.storage.store_message(
            conversation_id,
            owner,
            "user",
            "синтетический прошлый файл",
            metadata={"had_attachments": True, "attachment_count": 1, **lineage},
        )
        app.state.storage.store_message(
            conversation_id,
            owner,
            "assistant",
            "синтетический прошлый ответ",
            metadata={"attachment_context_used": True, **lineage},
            reply_to=str(seed_user["id"]),
        )

        def forbidden_restore(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("SQLite meta turn attempted attachment materialization")

        for method_name in (
            "_restore_authorized_attachment_lineage",
            "_restore_explicit_citation_file_attachments",
            "_restore_latest_cited_file_attachments",
            "_restore_source_search_result_attachments",
            "_restore_latest_uploaded_attachments",
            "_restore_adjacent_used_attachment_set",
            "_restore_conversation_attachments",
            "_owned_file_attachment",
        ):
            setattr(app.state.agent, method_name, forbidden_restore)
        app.state.agent.llm = _ForbiddenModel()

        async def forbidden_kernel(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("SQLite meta turn reached a tool")

        app.state.kernel.execute = forbidden_kernel
        response = client.post(
            "/api/chat",
            json={"message": message, "conversation_id": conversation_id},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert "message_search читает всю принятую историю" in payload["message"]
        assert payload["tools_used"] == []
        assert payload["restored_attachment_count"] == 0
        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=owner,
            limit=10,
        )
        assert len(rows) == 4
        current_user, current_assistant = rows[-2:]
        assert current_user["content"] == message
        assert current_assistant["reply_to"] == current_user["id"]
        current_metadata = json.loads(str(current_user["metadata_json"] or "{}"))
        assert current_metadata == {"turn_policy_intent": TurnIntent.META_CAPABILITIES.value}
        assert raw_id not in response.text
        assert int(app.state.storage.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]) == 0


def test_adjacent_weather_correction_reaches_one_policy_authorized_web_query(settings: Any) -> None:
    """Policy weather bypasses private context and executes one bounded query."""

    import friday.server as server_module

    calls: list[tuple[str, dict[str, Any]]] = []

    class _WebResult:
        success = True
        attachment = None
        data = {
            "query": "погода Донецк сегодня",
            "outbound_attempted": True,
            "sources": [
                {
                    "url": "https://weather.example.test/donetsk",
                    "title": "Погода в Донецке",
                    "text": "Синтетический прогноз для теста.",
                    "text_length": len("Синтетический прогноз для теста."),
                    "status_code": 200,
                    "error": "",
                    "truncated": False,
                }
            ],
            "requested_sources": 1,
            "completed_sources": 1,
            "timed_out_sources": 0,
            "failed_sources": 0,
            "search_timed_out": False,
        }

        def to_llm_message(self) -> str:
            return "Результат web_research: синтетический прогноз."

    class _SynthesisModel:
        enabled = True
        model = "synthetic-weather-model"
        total_budget_sec = 10.0

        async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"content": "Синтетический ответ по найденному прогнозу."}

    app = server_module.create_app(replace(settings, verify_answers=False))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        seen_policies: list[Any] = []
        prepared_queries: list[str] = []
        original_chat = app.state.agent.chat
        original_prepare = app.state.agent._prepare_context

        async def observed_chat(*args: Any, **kwargs: Any) -> Any:
            seen_policies.append(kwargs.get("turn_policy"))
            return await original_chat(*args, **kwargs)

        async def observed_prepare(*args: Any, **kwargs: Any) -> Any:
            prepared_queries.append(str(kwargs.get("policy_web_query") or ""))
            return await original_prepare(*args, **kwargs)

        app.state.agent.chat = observed_chat
        app.state.agent._prepare_context = observed_prepare
        app.state.agent.llm = _SynthesisModel()

        async def synthetic_execute(
            name: str,
            arguments: dict[str, Any],
            *,
            actor: Any,
        ) -> Any:
            del actor
            calls.append((name, dict(arguments)))
            if name != "web_research":
                raise AssertionError(f"unexpected tool: {name}")
            return _WebResult()

        app.state.kernel.execute = synthetic_execute
        first = client.post(
            "/api/chat",
            json={"message": "Подскажи погоду на сегодня"},
            headers=headers,
        )
        assert first.status_code == 200, first.text
        assert first.json()["message"] == WEATHER_LOCATION_CLARIFICATION
        assert calls == []

        challenge = client.post(
            "/api/chat",
            json={
                "message": "а почему во Владимире?",
                "conversation_id": first.json()["conversation_id"],
            },
            headers=headers,
        )
        assert challenge.status_code == 200, challenge.text
        assert challenge.json()["message"] == WEATHER_LOCATION_CHALLENGE_RESPONSE
        assert calls == []

        second = client.post(
            "/api/chat",
            json={
                "message": "Меня Донецк интересовал))",
                "conversation_id": first.json()["conversation_id"],
            },
            headers=headers,
        )
        assert second.status_code == 200, second.text
        explicit = client.post(
            "/api/chat",
            json={"message": "Какая погода в Донецке сегодня?"},
            headers=headers,
        )
        assert explicit.status_code == 200, explicit.text
        assert [policy.intent for policy in seen_policies] == [
            TurnIntent.WEATHER_NEEDS_LOCATION,
            TurnIntent.WEATHER_LOCATION_CHALLENGE,
            TurnIntent.WEATHER_WITH_LOCATION,
            TurnIntent.WEATHER_WITH_LOCATION,
        ]
        assert seen_policies[2].location == "Донецк"
        assert seen_policies[3].location == "Донецке"
        assert [policy.weather_horizon for policy in seen_policies] == [
            WeatherHorizon.TODAY,
            WeatherHorizon.TODAY,
            WeatherHorizon.TODAY,
            WeatherHorizon.TODAY,
        ]
        # Explicit/adjacent weather now enters the one-way outbound chamber and
        # intentionally skips general context preparation altogether.
        assert prepared_queries == []
        assert calls == [
            ("web_research", {"query": "погода Донецк сегодня", "max_sources": 3}),
            (
                "web_research",
                {"query": "погода Донецке сегодня", "max_sources": 3},
            ),
        ]
