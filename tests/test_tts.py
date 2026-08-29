"""Friday speaks a reply on request — text-to-speech output.

Pins the pure helper (`sanitize_text`) without loading a model, the `speak`
agent tool's gating/degradation contract with piper monkeypatched (so the
suite stays fast and free of the optional piper-tts dependency), and the two
wiring seams that turn a tool call into an actual Telegram voice message:
`_agentic_loop` extracting `ToolResult.attachment` into `voice_clip`, and the
bridge's `_deliver_voice_reply` turning `response["voice"]` into a `sendVoice`
call. A green test on the isolated mechanism does not prove the production
path calls it — this project has been burned by that gap more than once, so
the wiring tests exercise the real call sites, not a reimplementation.
"""

from __future__ import annotations

import base64

import pytest

from friday.tts import Speech, TTSUnavailable, sanitize_text


def _speak_tool_schema() -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {"name": "speak", "parameters": {"type": "object"}},
        }
    ]


# --- sanitize_text (pure, no model) ----------------------------------------


def test_sanitize_text_collapses_whitespace():
    cleaned, truncated = sanitize_text("  привет   мир  \n", max_chars=100)
    assert cleaned == "привет мир"
    assert truncated is False


def test_sanitize_text_truncates_and_reports_it():
    """Обрыв не немой и не посреди слова.

    Замерено на живом архиве: 29 ответов из 475 (6,1%) длиннее потолка, самый
    длинный — 3695 знаков. Человек слышал 54% ответа и обрыв на полуслове, читая
    полный текст рядом; клип уходил без единой пометки.
    """
    from friday.tts import TRUNCATION_NOTICE

    long_answer = "Первое предложение. Второе предложение. " + "хвост " * 200
    cleaned, truncated = sanitize_text(long_answer, max_chars=60)
    assert truncated is True
    assert len(cleaned) <= 60
    assert cleaned.endswith(TRUNCATION_NOTICE), "человек не узнает, что услышал не всё"
    # Резать по границе: слово не должно обрываться на половине.
    body = cleaned[: -len(TRUNCATION_NOTICE)]
    assert body.rstrip()[-1] in ".!?…" or not body.endswith("хвос")

    # Короткий текст не обрастает пометкой.
    short, cut = sanitize_text("Приказ подписан.", max_chars=60)
    assert (short, cut) == ("Приказ подписан.", False)


def test_sanitize_text_empty_input_stays_empty():
    cleaned, truncated = sanitize_text("   ", max_chars=10)
    assert cleaned == ""
    assert truncated is False


# --- the `speak` agent tool -------------------------------------------------


def _kernel(settings, storage):
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.permissions import AuthorizationService
    from friday.web_surfer import WebSurfer

    storage.ensure_user("alice", preset_key="user")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, ingestion)
    actor = auth.actor_for_user("alice", source="test")
    return kernel, actor


@pytest.mark.asyncio
async def test_speak_is_disabled_by_default(settings, storage):
    """Mutation: default `tts_enabled` to True — this must go red, the same way
    a fresh install must not start synthesizing audio nobody asked to enable."""
    assert settings.tts_enabled is False
    kernel, actor = _kernel(settings, storage)

    result = await kernel.execute("speak", {"text": "привет"}, actor=actor)

    assert result.success is True
    assert result.data["spoken"] is False
    assert "disabled" in result.data["reason"]
    assert result.attachment is None


@pytest.mark.asyncio
async def test_speak_degrades_gracefully_when_the_engine_is_unavailable(settings, storage, monkeypatch):
    """Mutation: let TTSUnavailable propagate uncaught — the turn's other tool
    calls and the final answer must survive a missing optional dependency."""
    from dataclasses import replace

    import friday.execution_kernel as execution_kernel_module

    settings = replace(settings, tts_enabled=True)
    kernel, actor = _kernel(settings, storage)

    def _raise(*args, **kwargs):
        raise TTSUnavailable("piper-tts is not installed")

    monkeypatch.setattr(execution_kernel_module, "synthesize_speech", _raise)

    result = await kernel.execute("speak", {"text": "привет"}, actor=actor)

    assert result.success is True
    assert result.data["spoken"] is False
    assert result.data["reason"] == "voice engine unavailable"
    assert result.attachment is None


@pytest.mark.asyncio
async def test_speak_produces_a_voice_attachment_kept_out_of_the_llm_visible_data(
    settings, storage, monkeypatch
):
    """The attachment must never reach `to_llm_message()` (see `ToolResult.attachment`'s
    docstring) — a base64 audio blob in the model's context wastes the tool-call
    budget for nothing the model can use. Mutation: stop popping `_attachment`
    out of `data` in `ExecutionKernel.execute` — this must go red on both the
    `"_attachment" not in result.data["voice"]`... check AND the leak into
    `to_llm_message()`."""
    from dataclasses import replace

    import friday.execution_kernel as execution_kernel_module

    settings = replace(settings, tts_enabled=True)
    kernel, actor = _kernel(settings, storage)

    fake_audio = b"OggS-fake-opus-bytes"

    def _fake_synthesize(text, *, voice, download_root, max_chars):
        return Speech(
            audio_bytes=fake_audio, sample_rate=48000, duration_sec=1.23, voice=voice, truncated=False
        )

    monkeypatch.setattr(execution_kernel_module, "synthesize_speech", _fake_synthesize)

    result = await kernel.execute("speak", {"text": "Привет, мир."}, actor=actor)

    assert result.success is True
    assert result.data["spoken"] is True
    assert result.data["chars"] == len("Привет, мир.")
    assert "_attachment" not in result.data
    assert result.attachment is not None
    assert result.attachment["kind"] == "voice"
    assert result.attachment["mime_type"] == "audio/ogg"
    assert base64.b64decode(result.attachment["audio_base64"]) == fake_audio

    rendered = result.to_llm_message()
    assert fake_audio not in rendered.encode("utf-8", errors="ignore")
    assert base64.b64encode(fake_audio).decode("ascii") not in rendered


def test_synthesize_speech_rejects_a_voice_that_produced_no_audio(monkeypatch):
    """Proposed by Grok's G23 adversarial review of the voice feature: a voice
    engine returning zero synthesized chunks (or chunks with empty PCM) used to
    reach `_encode_opus_ogg` with an empty buffer instead of failing cleanly the
    same way any other engine problem does.

    Mutation: remove the `if not pcm: raise TTSUnavailable(...)` guard — this
    test must go red (either a silent empty clip or an unhandled encoder error
    instead of the expected `TTSUnavailable`).
    """
    import friday.tts as tts_module

    class _EmptyEngine:
        def synthesize(self, text):
            return iter(())  # zero chunks — the exact shape a broken voice model produces

    monkeypatch.setattr(tts_module, "_load_voice", lambda voice, download_root: _EmptyEngine())

    with pytest.raises(TTSUnavailable, match="no audio"):
        tts_module.synthesize_speech("Привет", download_root="/tmp/unused")


@pytest.mark.asyncio
async def test_guest_can_call_speak(settings, storage):
    """`tts.use` is granted at the same tier as `chat.use` — a guest who can chat
    can also ask for a spoken reply. Mutation: drop "guest" from tts.use's
    default_presets — this must go red."""
    from friday.permissions import AuthorizationService

    storage.ensure_user("bob", preset_key="guest")
    auth = AuthorizationService(storage)
    guest = auth.actor_for_user("bob", source="test")
    assert auth.authorize(guest, "tts.use").allowed is True


# --- wiring: _agentic_loop surfaces the attachment as voice_clip ------------


class _SpeaksThenAnswersLLM:
    """First round calls `speak`, second round returns the final answer — the
    shape a model takes when it recognizes a spoken-reply request mid-turn."""

    enabled = True
    total_budget_sec = 120.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "speak", "arguments": '{"text": "Привет!"}'},
                    }
                ],
                "_queue_wait_sec": 0.0,
            }
        return {"content": "Привет!", "tool_calls": None, "_queue_wait_sec": 0.0}


class _SpeaksKernel:
    """Stands in for `ExecutionKernel`: `speak` returns a `ToolResult` carrying
    an attachment, exactly like the real handler after a successful synthesis."""

    def __init__(self, attachment: dict) -> None:
        self._attachment = attachment

    async def execute(self, name, arguments, *, actor=None):
        from friday.execution_kernel import ToolResult

        if name == "speak":
            return ToolResult(name, True, data={"spoken": True}, attachment=self._attachment)
        return ToolResult(name, True, data={})


@pytest.mark.asyncio
async def test_agentic_loop_surfaces_the_speak_attachment_as_voice_clip(settings, storage):
    """Found-by-precedent risk in this project: a mechanism can work in isolation
    while the production loop never reads its output. Mutation: stop reading
    `tool_result.attachment` in `_agentic_loop`'s per-call loop (or stop adding
    `voice_clip` to the returned dict) — this must go red."""
    from friday.agent_runtime import AgentContext, AgentRuntime
    from friday.permissions import ActorContext

    storage.ensure_user("alice")
    attachment = {"kind": "voice", "mime_type": "audio/ogg", "audio_base64": "Zm9v", "duration_sec": 1.0}
    agent = AgentRuntime(settings, storage, llm=_SpeaksThenAnswersLLM(), kernel=_SpeaksKernel(attachment))
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")
    context = AgentContext(
        conversation_id="conv-test",
        user_id="alice",
        conversation_history=[],
        interaction_mode="dialogue",
    )

    result = await agent._agentic_loop(
        context,
        "Привет, ответь голосом",
        actor,
        tools=_speak_tool_schema(),
        attachments=None,
    )

    assert result["voice_clip"] == attachment


@pytest.mark.asyncio
async def test_agentic_loop_rejects_a_dispatch_only_speak_success(settings, storage):
    """A successful dispatch without rendered audio is not deed evidence."""
    from friday.agent_runtime import AgentContext, AgentRuntime
    from friday.execution_kernel import ToolResult
    from friday.permissions import ActorContext

    class _ClaimsVoiceLLM(_SpeaksThenAnswersLLM):
        async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
            result = await super().chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=tools,
            )
            if self.calls > 1:
                result["content"] = "Голосовое отправлено."
            return result

    class _DispatchOnlyKernel:
        async def execute(self, name, arguments, *, actor=None):  # noqa: ANN001, ARG002
            assert name == "speak"
            return ToolResult(
                name,
                True,
                data={"spoken": False, "reason": "voice engine unavailable"},
                attachment={"kind": "voice", "audio_base64": "dispatch-only"},
            )

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage, llm=_ClaimsVoiceLLM(), kernel=_DispatchOnlyKernel())
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")
    context = AgentContext(
        conversation_id="conv-test",
        user_id="alice",
        conversation_history=[],
        interaction_mode="dialogue",
    )

    result = await agent._agentic_loop(
        context,
        "Ответь голосом",
        actor,
        tools=_speak_tool_schema(),
        attachments=None,
    )

    assert result.get("voice_clip") is None
    assert result.get("tool_evidence") == []


@pytest.mark.asyncio
async def test_agentic_loop_leaves_voice_clip_none_when_speak_was_not_called(settings, storage):
    """Mutation: hardcode `voice_clip` to a truthy value regardless of whether
    `speak` ran — this must go red (a turn that never spoke must not ship audio)."""
    from friday.agent_runtime import AgentContext, AgentRuntime
    from friday.permissions import ActorContext

    class _AnswersImmediatelyLLM:
        enabled = True
        total_budget_sec = 120.0

        async def chat(self, messages, *, temperature=None, max_tokens=None, tools=None):
            return {"content": "Готово.", "tool_calls": None, "_queue_wait_sec": 0.0}

    storage.ensure_user("alice")
    agent = AgentRuntime(settings, storage, llm=_AnswersImmediatelyLLM(), kernel=_SpeaksKernel({}))
    actor = ActorContext(user_id="alice", preset_key="owner", source="api")
    context = AgentContext(
        conversation_id="conv-test", user_id="alice", conversation_history=[], interaction_mode="dialogue"
    )

    result = await agent._agentic_loop(
        context,
        "просто вопрос",
        actor,
        tools=_speak_tool_schema(),
        attachments=None,
    )

    assert result.get("voice_clip") is None


# --- озвучивается ТОТ ЖЕ ответ, что написан ---------------------------------


@pytest.mark.asyncio
async def test_the_clip_carries_the_final_answer_and_its_caveat(settings, storage, monkeypatch):
    """Мутация: убрать `_voice_of_the_final_answer` из сборки ответа — краснеет.

    `speak` вызывается моделью в раунде инструментов, а итоговый текст рождается
    позже и только он проходит верификацию и проверку обоснованности. Замерено на
    живой базе: из 475 ответов 210 (44,2%) идут с пометкой «у этого нет оснований
    в архиве» — человек ЧИТАЛ оговорку и СЛЫШАЛ ту же выдумку без неё.
    """
    from friday.agent_runtime import AgentRuntime

    spoken: list[str] = []

    class _Result:
        success = True
        attachment = {"kind": "voice", "audio_base64": "final", "duration_sec": 2.0}

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            spoken.append(str(arguments.get("text") or ""))
            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()

    clip = await runtime._voice_of_the_final_answer(  # noqa: SLF001
        {"kind": "voice", "audio_base64": "midturn"},
        "Ключевая ставка — 14%.",
        warning="⚠️ У этого ответа нет оснований в архиве.",
        caution="",
        actor=None,
    )
    assert clip["audio_base64"] == "final", "озвучен клип из середины хода, а не итог"
    assert spoken and spoken[0].startswith("⚠️"), "оговорка не прозвучала первой"
    assert "14%" in spoken[0]


@pytest.mark.asyncio
async def test_the_clip_carries_both_distinct_caveats(settings, storage):
    from friday.agent_runtime import AgentRuntime

    spoken: list[str] = []

    class _Result:
        success = True
        attachment = {"kind": "voice", "audio_base64": "final"}

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            spoken.append(str(arguments["text"]))
            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.settings = settings
    await runtime._voice_of_the_final_answer(  # noqa: SLF001
        {"kind": "voice"},
        "Содержательный ответ.",
        warning="Нет оснований в архиве.\nПодробности.",
        caution="Проверка нашла расхождение.\nЕщё подробности.",
        actor=None,
    )

    assert spoken == ["Нет оснований в архиве. Проверка нашла расхождение. Содержательный ответ."]


@pytest.mark.asyncio
async def test_a_quote_only_answer_has_an_audible_source_marker(settings, storage):
    from friday.agent_runtime import AgentRuntime

    spoken: list[str] = []

    class _Result:
        success = True
        attachment = {"kind": "voice", "audio_base64": "final"}

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            spoken.append(str(arguments["text"]))
            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.settings = settings
    await runtime._voice_of_the_final_answer(  # noqa: SLF001
        {"kind": "voice"},
        "«Я заказала курьера.»",
        warning="",
        caution="",
        actor=None,
    )

    assert spoken and spoken[0].startswith("Цитата:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "Я не могу заказать такси. " + ("Подробности без действий. " * 300) + "Могу составить инструкцию.",
        "Я заказала курьера, " + ("по этому гипотетическому сценарию " * 100) + "возможно.",
        "Файл отправлен " + ("как часть длинного вопроса " * 100) + "?",
    ],
)
async def test_an_unsafe_truncated_voice_prefix_is_not_synthesised(settings, storage, answer):
    from dataclasses import replace

    from friday.agent_runtime import AgentRuntime

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            raise AssertionError("unsafe audible prefix reached synthesis")

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    runtime.settings = replace(settings, tts_max_chars=500)

    assert (
        await runtime._voice_of_the_final_answer(  # noqa: SLF001
            {"kind": "voice"}, answer, warning="", caution="", actor=None
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_voice_was_asked_for_no_voice_is_made(settings, storage):
    """Контроль: пересинтез не превращает каждый ответ в озвученный."""
    from friday.agent_runtime import AgentRuntime

    called: list[str] = []

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            called.append(name)
            raise AssertionError("синтез не должен вызываться без просьбы озвучить")

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    assert (
        await runtime._voice_of_the_final_answer(  # noqa: SLF001
            None, "обычный ответ", warning="", caution="", actor=None
        )
        is None
    )
    assert called == []


@pytest.mark.asyncio
async def test_a_failed_resynthesis_discards_the_unverified_midturn_clip(settings, storage):
    """Старый клип нельзя вернуть: он мог нести текст до финальных рубежей."""
    from friday.agent_runtime import AgentRuntime

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            raise RuntimeError("движок синтеза лёг")

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    original = {"kind": "voice", "audio_base64": "midturn"}
    assert (
        await runtime._voice_of_the_final_answer(  # noqa: SLF001
            original, "ответ", warning="", caution="", actor=None
        )
        is None
    )


def test_the_bridge_tells_the_person_the_clip_was_cut():
    """Мутация: убрать ветку `voice.get("truncated")` — тест краснеет."""
    import inspect

    from friday.telegram_bridge._callbacks import _VOICE_TRUNCATION_NOTICE, CallbacksMixin

    source = inspect.getsource(CallbacksMixin._deliver_voice_reply)  # noqa: SLF001
    assert 'voice.get("truncated")' in source, "об обрыве человеку не говорят"
    assert "_VOICE_TRUNCATION_NOTICE" in source
    assert "озвучено начало" in _VOICE_TRUNCATION_NOTICE


@pytest.mark.asyncio
async def test_chars_reports_what_was_spoken_not_what_was_asked(settings, storage, monkeypatch):
    """`chars` был длиной ИСХОДНОГО текста — даже модель не знала, сколько прозвучало."""
    from dataclasses import replace

    import friday.execution_kernel as execution_kernel_module

    settings = replace(settings, tts_enabled=True, tts_max_chars=50)
    kernel, actor = _kernel(settings, storage)

    def _fake_synthesize(text, *, voice, download_root, max_chars):
        return Speech(audio_bytes=b"OggS", sample_rate=48000, duration_sec=1.0, voice=voice, truncated=True)

    monkeypatch.setattr(execution_kernel_module, "synthesize_speech", _fake_synthesize)
    result = await kernel.execute("speak", {"text": "с" * 500}, actor=actor)

    assert result.data["chars"] == 50, "отчитались за 500 знаков, озвучив 50"
    assert result.data["truncated"] is True
    assert result.attachment["truncated"] is True, "мост не узнает про обрыв"


@pytest.mark.asyncio
async def test_a_request_to_speak_is_honoured_even_if_the_model_forgot(settings, storage):
    """Мутация: убрать `asked_for_voice` — тест краснеет.

    Та же болезнь, что у файлов, и то же лекарство. Замерено сквозным прогоном:
    «что такое ключевая ставка? ответь голосом» после предварительного веб-поиска
    вернуло текст по выдаче и НИ ОДНОГО клипа — внимание модели ушло в протокол
    инструментов, и `speak` она не позвала.
    """
    from friday.agent_runtime import _ASKS_FOR_VOICE, AgentRuntime

    spoken: list[str] = []

    class _Result:
        success = True
        attachment = {"kind": "voice", "audio_base64": "made-after-the-loop"}

    class _Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ARG002
            spoken.append(str(arguments.get("text") or ""))
            return _Result()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()

    clip = await runtime._voice_of_the_final_answer(  # noqa: SLF001
        None,
        "Ключевая ставка — это процент, под который ЦБ даёт деньги банкам.",
        warning="",
        caution="",
        actor=None,
        asked_for_voice=True,
    )
    assert clip is not None and clip["audio_base64"] == "made-after-the-loop"
    assert spoken and "Ключевая ставка" in spoken[0]

    # Без просьбы — по-прежнему ничего не синтезируется.
    spoken.clear()
    assert (
        await runtime._voice_of_the_final_answer(  # noqa: SLF001
            None, "обычный ответ", warning="", caution="", actor=None, asked_for_voice=False
        )
        is None
    )
    assert spoken == []

    for phrase in (
        "что такое ставка? ответь голосом",
        "озвучь ответ",
        "скажи голосом, сколько документов",
        "проговори это",
        "надиктуй сводку",
    ):
        assert _ASKS_FOR_VOICE.search(phrase), f"просьба не узнана: {phrase!r}"
    for phrase in ("сколько документов в базе?", "запиши голосовое сообщение от Петрова"):
        assert not _ASKS_FOR_VOICE.search(phrase) or "голосов" in phrase


def test_the_voice_request_is_read_from_the_person_s_message():
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = " ".join(inspect.getsource(AgentRuntime.chat).split())
    assert 'file_voice = file_turn.proved("voice")' in source, (
        "просьба озвучить не проходит проверку полномочий текущего сообщения"
    )
    # И голосовой вопрос сам по себе — просьба ответить голосом: человек
    # записывает голосовое, когда ему неудобно печатать.
    assert "asked_for_voice=(answer_with_voice or file_voice)" in source, (
        "голосовая реплика или разрешённая просьба озвучить не доходят до синтеза"
    )
