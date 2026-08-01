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

# --- sanitize_text (pure, no model) ----------------------------------------


def test_sanitize_text_collapses_whitespace():
    cleaned, truncated = sanitize_text("  привет   мир  \n", max_chars=100)
    assert cleaned == "привет мир"
    assert truncated is False


def test_sanitize_text_truncates_and_reports_it():
    cleaned, truncated = sanitize_text("a" * 50, max_chars=10)
    assert cleaned == "a" * 10
    assert truncated is True


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
        context, "Привет, ответь голосом", actor, tools=[{"type": "function"}], attachments=None
    )

    assert result["voice_clip"] == attachment


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
        context, "просто вопрос", actor, tools=[{"type": "function"}], attachments=None
    )

    assert result.get("voice_clip") is None
