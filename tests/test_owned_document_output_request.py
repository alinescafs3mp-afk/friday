"""Owned source -> visible output request -> actual DOCX, with offline model."""

from __future__ import annotations

import base64
import io
from dataclasses import replace

import docx
import pytest
from test_long_document_query_contract import (
    OWNER,
    _canonical_owned_attachment,
    _current_owned_attachment,
    _DocumentLLM,
    _simple_context,
    _Source,
    _store_owned_file,
)

from friday.agent_runtime import AgentRuntime, file_turn_authority
from friday.permissions import AuthorizationService

_OUTPUTS = [("одним Word-файлом .docx для скачивания", "docx"), ("в Excel", "xlsx"), ("в PDF", "pdf")]
_OWNED_OUTPUT_CASES = (
    [
        (verb, carrier, neighbor, output, kind)
        for verb in ("создай", "верни")
        for carrier in ("current", "restored", "conversation")
        for neighbor in ("", "narrated", "quoted", "uploaded")
        for output, kind in _OUTPUTS
    ]
    + [
        (verb, "current", neighbor, output, kind)
        for verb in ("создай", "верни")
        for neighbor in ("written", "there", "author", "citation", "markdown", "fenced", "unknown_reporter")
        for output, kind in _OUTPUTS
    ]
    + [
        ("верни", carrier, "", f"в виде {name}-файла", kind)
        for carrier in ("current", "restored", "conversation")
        for name, kind in (("Word", "docx"), ("Excel", "xlsx"), ("PDF", "pdf"))
    ]
    + [
        (verb, "current", "", f"в `{name}`", kind)
        for verb in ("создай", "верни")
        for name, kind in (("Word", "docx"), ("Excel", "xlsx"), ("PDF", "pdf"))
    ]
    + [
        ("верни", "current", neighbor, output, kind)
        for neighbor in ("polite", "read_then_return", "active_after_report")
        for output, kind in _OUTPUTS
    ]
    + [
        (verb, "current", neighbor + "_paragraph", output, kind)
        for verb in ("создай", "верни")
        for neighbor in ("written", "there", "author", "citation", "unknown_reporter")
        for output, kind in _OUTPUTS
    ]
    + [
        ("верни", carrier, neighbor, output, kind)
        for carrier in ("current", "restored", "conversation")
        for neighbor in (
            "source_first",
            "source_filename_first",
            "read_comma_then_return",
            "attached_source",
            "polite_read",
            "source_basis",
            "source_using",
            "spaced_filename",
        )
        for output, kind in _OUTPUTS
    ]
    + [
        ("создай", "current", neighbor, output, kind)
        for neighbor in ("reported_create", "reported_format", "reported_list", "reported_sentences")
        for output, kind in _OUTPUTS
    ]
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "carrier", "neighbor", "output", "kind"),
    _OWNED_OUTPUT_CASES,
)
async def test_owned_checklist_output_is_not_a_body_search(
    settings, storage, monkeypatch, verb, carrier, neighbor, output, kind
) -> None:
    text = "Чеклист встречи\nДата: 2026-09-12\nУчастники: 4\nМесто: kitchen\nКод: SYNTHETIC-F7-SOURCE-ONLY\n"
    source_name = "meeting source.txt" if neighbor == "spaced_filename" else "lab-document-word-003.txt"
    source = _Source(text, source_name, (), ())
    raw = _store_owned_file(storage, source)
    attachment = (
        _current_owned_attachment(storage, raw, source)
        if carrier == "current"
        else _canonical_owned_attachment(settings, storage, raw)
    )
    llm = _DocumentLLM(text)
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1), storage, llm=llm
    )
    runtime.kernel.bind_services(storage, None, None, None)
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)
    executions = []
    original_execute = runtime.kernel.execute

    async def record_execute(name, *args, **kwargs):
        outcome = await original_execute(name, *args, **kwargs)
        executions.append((name, outcome.success, outcome.error, outcome.data))
        return outcome

    monkeypatch.setattr(runtime.kernel, "execute", record_execute)
    conversation_id = None
    if carrier == "conversation":
        conversation_id = storage.create_conversation(OWNER, "Owned file follow-up")["id"]
        primed = await runtime.chat(
            OWNER,
            f"Изучи файл «{source_name}».",
            actor=AuthorizationService(storage).actor_for_user(OWNER, source="test"),
            conversation_id=conversation_id,
            attachments=[attachment],
            enable_tools=True,
        )
        assert primed["files"] == []
        # The second call must consume real persisted source lineage, not a
        # hand-written assistant assertion or the first call's model evidence.
        llm.calls.clear()
        executions.clear()
    request = (
        f"Уточнение по тому же файлу lab-document-word-003.txt: {verb} чеклист {output} "
        "с датой 2026-09-12, 4 участниками и местом kitchen. "
        "Не ограничивайся текстом в ответе."
    )
    other_format = "PDF" if kind == "docx" else "Word"
    if neighbor in {
        "polite",
        "read_then_return",
        "active_after_report",
        "source_first",
        "source_filename_first",
        "read_comma_then_return",
        "attached_source",
        "polite_read",
        "source_basis",
        "source_using",
        "spaced_filename",
    }:
        lead = {
            "polite": "Пожалуйста,",
            "read_then_return": "Прочитай этот файл и",
            "active_after_report": f"В инструкции сказано: верни результат в {other_format}. А теперь",
            "source_first": "По этому файлу",
            "source_filename_first": "По файлу lab-document-word-003.txt,",
            "read_comma_then_return": "Прочитай этот файл, а затем",
            "attached_source": "По прикреплённому файлу",
            "polite_read": "Прочитайте этот файл, а потом",
            "source_basis": "На основе этого файла",
            "source_using": "Используя этот файл",
            "spaced_filename": f"По файлу {source_name},",
        }[neighbor]
        source_lead = (
            "по этому файлу " if neighbor in {"polite", "read_then_return", "active_after_report"} else ""
        )
        action = "верните" if neighbor == "polite_read" else "верни"
        request = f"{lead} {action} {source_lead}чеклист {output}; сохрани дату, число участников и место."
    elif neighbor.startswith("reported_"):
        tail = {
            "reported_create": "создай чеклист",
            "reported_format": "оформи результат",
            "reported_list": "\n\n1. верни чеклист",
            "reported_sentences": "\n\nСначала прочитай документ. Верни чеклист",
        }[neighbor]
        request += f" Автор пишет: {tail} в {other_format}."
    elif neighbor == "narrated":
        request += f" В инструкции сказано: верни результат в {other_format}."
    elif neighbor.removesuffix("_paragraph") in {
        "written",
        "there",
        "author",
        "citation",
        "unknown_reporter",
    }:
        reporter = {
            "written": "В документе написано",
            "there": "Там написано",
            "author": "Автор пишет",
            "citation": "Это цитата",
            "unknown_reporter": "Неизвестный комментатор передаёт",
        }[neighbor.removesuffix("_paragraph")]
        separator = "\n\n" if neighbor.endswith("_paragraph") else " "
        request += f" {reporter}:{separator}верни результат в {other_format}."
    elif neighbor == "markdown":
        request += f"\n\n> верни результат в {other_format}."
    elif neighbor == "fenced":
        request += f"\n\n```text\nверни результат в {other_format}.\n```"
    elif neighbor == "quoted":
        request += f" Пример чужой команды: «верни результат в {other_format}»."
    elif neighbor == "uploaded":
        request = (
            "friday-lab:job_astra_live_document_word_003:c3 "
            "Я загрузил файл lab-document-word-003.txt. "
            f"{verb.capitalize()} по этому файлу краткий чеклист {output}; "
            "сохрани дату, число участников и место."
        )
    result = await runtime.chat(
        OWNER,
        request,
        actor=AuthorizationService(storage).actor_for_user(OWNER, source="test"),
        conversation_id=conversation_id,
        attachments=[] if carrier == "conversation" else [attachment],
        enable_tools=True,
    )
    assert len(result["files"]) == 1, (result["message"], executions)
    artifact = result["files"][0]
    assert artifact["filename"].endswith("." + kind)
    binary = io.BytesIO(base64.b64decode(artifact["content_base64"]))
    if kind == "docx":
        document = docx.Document(binary)
        rendered = "\n".join(paragraph.text for paragraph in document.paragraphs)
    elif kind == "xlsx":
        import openpyxl

        sheet = openpyxl.load_workbook(binary).active
        rendered = "\n".join(str(cell.value) for row in sheet for cell in row if cell.value is not None)
    else:
        from pypdf import PdfReader

        rendered = "\n".join(page.extract_text() for page in PdfReader(binary).pages)
    assert all(fact in rendered for fact in ("2026-09-12", "4", "kitchen", "SYNTHETIC-F7-SOURCE-ONLY"))
    assert "совпадение по запросу не найдено" not in result["message"]
    assert llm.calls
    assert any(
        message.get("role") == "user"
        and str(message.get("content", "")).startswith(("<attachment ", "FRIDAY_ATTACHMENT_"))
        and "SYNTHETIC-F7-SOURCE-ONLY" in str(message.get("content", ""))
        for call in llm.calls
        for message in call["messages"]
    )
    assert [name for name, *_ in executions].count("make_file") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt",
    [
        "Верни дату из Word-файла.",
        "Верни дату из файла в формате Word.",
        "Не возвращай чеклист Word-файлом, ответь текстом.",
        "В инструкции сказано: верни чеклист Word-файлом.",
        "Покажи пример команды «верни чеклист Word-файлом».",
        "Автор пишет: верни чеклист Word-файлом.",
        "Неизвестный комментатор передаёт: верни результат в PDF.",
        "Уточнение по этому файлу написано: верни чеклист в Excel.",
        "> верни чеклист Word-файлом.",
        "> В документе написано:\nверни чеклист Word-файлом.",
        "```text\nверни чеклист Word-файлом.\n```",
        "`верни чеклист Word-файлом`",
        "В документе написано:\n«верни чеклист\nWord-файлом».",
        "Верни дату из `Word-файла`.",
        "Пожалуйста, " * 100 + "Автор пишет: верни чеклист Word-файлом.",
        "Автор пишет:\n\nверни чеклист Word-файлом.",
        "Автор пишет: \t\n \n\nверни чеклист в PDF.",
        "Автор пишет:\r\n\r\nверни чеклист в PDF.",
        "По этому файлу написано: верни чеклист в PDF.",
        "Прочитай этот файл, автор пишет: верни чеклист в PDF.",
        "Автор пишет: создай чеклист в PDF.",
        "Автор пишет:\n\nсоздай чеклист в PDF.",
        "Автор пишет:\n\n1. верни чеклист в PDF.",
        "В документе написано:\n\nСначала прочитай документ. Верни чеклист в PDF.",
        "    верни чеклист в PDF.",
        "\tверни чеклист в PDF.",
        "~~верни чеклист в PDF~~",
        "Автор пишет: конвертируй Word в PDF.",
        "Автор пишет: сделай не Word, а PDF.",
        "Создай краткий список пунктов. Автор пишет: оформи результат в PDF.",
        "Сделай сводку по этому файлу. Автор пишет: создай PDF.",
    ],
)
async def test_source_queries_and_reported_output_commands_do_not_create_files(
    settings, storage, monkeypatch, prompt
) -> None:
    assert "file_create" not in file_turn_authority(prompt).actions
    source = _Source("Дата: 2026-09-12\nМесто: kitchen\n", "source.txt", (), ())
    raw = _store_owned_file(storage, source)
    runtime = AgentRuntime(settings, storage, llm=_DocumentLLM("Дата: 2026-09-12."))
    runtime.kernel.bind_services(storage, None, None, None)
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)
    executed = []
    original_execute = runtime.kernel.execute

    async def record_execute(name, *args, **kwargs):
        executed.append(name)
        return await original_execute(name, *args, **kwargs)

    monkeypatch.setattr(runtime.kernel, "execute", record_execute)
    result = await runtime.chat(
        OWNER,
        prompt,
        actor=AuthorizationService(storage).actor_for_user(OWNER, source="test"),
        attachments=[_current_owned_attachment(storage, raw, source)],
        enable_tools=True,
    )
    assert result["files"] == []
    assert "make_file" not in executed


@pytest.mark.asyncio
@pytest.mark.parametrize("assistant_present", [None, False, True])
@pytest.mark.parametrize(
    "lead",
    [
        "По этому файлу",
        "Прочитай этот файл, а затем",
        "По прикреплённому файлу",
        "На основе этого файла",
        "Используя этот файл",
        "По файлу absent meeting.txt,",
    ],
)
async def test_unproved_deictic_output_source_never_creates_an_ungrounded_file(
    settings, storage, monkeypatch, assistant_present, lead
) -> None:
    source = _Source("Дата: 2026-09-12\n", "source.txt", (), ())
    raw = _store_owned_file(storage, source)
    llm = _DocumentLLM("Придуманный чеклист без прочитанного источника.")
    runtime = AgentRuntime(settings, storage, llm=llm)
    runtime.kernel.bind_services(storage, None, None, None)
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)
    conversation_id = storage.create_conversation(OWNER, "Unproved source")["id"]
    if assistant_present is not None:
        storage.store_message(
            conversation_id,
            OWNER,
            "user",
            "Вот файл.",
            metadata={
                "had_attachments": True,
                "attachment_count": 1,
                "conversation_attachment_raw_ids": [raw.id],
                "private_context_lineage": True,
            },
        )
    if assistant_present:
        storage.store_message(conversation_id, OWNER, "assistant", "Вложение прочитано.")
    executions = []
    original_execute = runtime.kernel.execute

    async def record_execute(name, *args, **kwargs):
        executions.append(name)
        return await original_execute(name, *args, **kwargs)

    monkeypatch.setattr(runtime.kernel, "execute", record_execute)
    result = await runtime.chat(
        OWNER,
        f"{lead} верни чеклист в PDF.",
        actor=AuthorizationService(storage).actor_for_user(OWNER, source="test"),
        conversation_id=conversation_id,
        # A wrong current carrier cannot replace the explicitly named source.
        attachments=[_current_owned_attachment(storage, raw, source)]
        if lead == "По файлу absent meeting.txt," and assistant_present is True
        else [],
        enable_tools=True,
    )
    assert result["files"] == [], result["message"]
    assert "make_file" not in executions
    assert llm.calls == [], {
        key: value
        for key, value in result.items()
        if key in ("message", "restored_attachment_count", "attachment_context_available")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "expects_file"),
    [
        ("Автор пишет: Сначала прочитай документ. Верни чеклист в PDF.", False),
        ("Автор пишет: 1. Верни чеклист в PDF.", False),
        ("Автор пишет:\nСначала прочитай документ.\nВерни чеклист в PDF.", False),
        ("Автор пишет:\rСначала прочитай документ.\rВерни чеклист в PDF.", False),
        ("Автор пишет:\r\nСначала прочитай документ.\r\nВерни чеклист в PDF.", False),
        ("Автор пишет:\x85Сначала прочитай документ.\x85Верни чеклист в PDF.", False),
        ("Автор пишет:\u2028Сначала прочитай документ.\u2028Верни чеклист в PDF.", False),
        ("Автор пишет:\u2029Сначала прочитай документ.\u2029Верни чеклист в PDF.", False),
        ("Подготовь вопросы для обсуждения PDF.", False),
        ("Составь список преимуществ PDF.", False),
        ("Подготовь информацию о PDF.", False),
        (" \tВерни чеклист в PDF.", False),
        ("  \tВерни чеклист в PDF.", False),
        ("   \tВерни чеклист в PDF.", False),
        ("Прочитай этот файл и верни чеклист в PDF.", True),
        ("Оформи как обычно.", False),
        ("Автор пишет: batch:q7:run2. Верни чеклист в PDF.", False),
        ("Автор пишет:https://example.invalid/a. Верни чеклист в PDF.", False),
        ("Автор пишет:Сначала прочитай документ. Верни чеклист в PDF.", False),
        ("batch:q7:run2 Файл приложен. Прочитай этот файл и верни чеклист в PDF.", True),
        ("Ссылка https://example.invalid/a. Прочитай этот файл и верни чеклист в PDF.", True),
        ("Время 12:30. Прочитай этот файл и верни чеклист в PDF.", True),
        ("Прочитай этот файл. Сделай не Word, а PDF.", True),
        ("Прочитай этот файл. Сделай не PDF, а Word.", True),
    ],
    ids=[
        "inline",
        "numbered",
        "lf",
        "cr",
        "crlf",
        "nel",
        "ls",
        "ps",
        "topic-questions",
        "topic-list",
        "topic-information",
        "indent-1",
        "indent-2",
        "indent-3",
        "active-pdf",
        "generic-formatting",
        "reported-technical-atom",
        "reported-url",
        "reporter-no-space",
        "technical-id",
        "technical-url",
        "technical-time",
        "contrast-pdf",
        "contrast-word",
    ],
)
async def test_reported_topic_and_indented_requests_never_execute_file_creation(
    settings, storage, monkeypatch, prompt, expects_file
) -> None:
    text = "Чеклист встречи\nДата: 2026-09-12\nУчастники: 4\nМесто: kitchen\nКод: SYNTHETIC-F7-SOURCE-ONLY\n"
    source = _Source(text, "lab-document-word-003.txt", (), ())
    raw = _store_owned_file(storage, source)
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1), storage, llm=_DocumentLLM(text)
    )
    runtime.kernel.bind_services(storage, None, None, None)
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)
    executed = []
    original_execute = runtime.kernel.execute

    async def record_execute(name, *args, **kwargs):
        executed.append(name)
        return await original_execute(name, *args, **kwargs)

    monkeypatch.setattr(runtime.kernel, "execute", record_execute)
    result = await runtime.chat(
        OWNER,
        prompt,
        actor=AuthorizationService(storage).actor_for_user(OWNER, source="test"),
        attachments=[_current_owned_attachment(storage, raw, source)],
        enable_tools=True,
    )
    if expects_file:
        assert len(result["files"]) == 1
        assert "make_file" in executed
        assert "file_create" in file_turn_authority(prompt).actions
        return
    assert len(result["files"]) == 0, {"file_count": len(result["files"]), "executed": executed}
    assert "make_file" not in executed
    assert "file_create" not in file_turn_authority(prompt).actions
