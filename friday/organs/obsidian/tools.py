"""Actor-scoped Friday tools for native Obsidian note operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Protocol, cast

from friday.execution_kernel import ToolSpec
from friday.organs import ServiceContext
from friday.permissions import ActorContext

_MAX_NOTE_PATH_CHARS = 2_048
_MAX_NOTE_TEXT_CHARS = 200_000
_MAX_OPERATION_ID_CHARS = 200
_REVISION_PATTERN = "^[0-9a-f]{64}$"


class ObsidianToolRuntime(Protocol):
    """The narrow runtime surface visible to model-callable tools."""

    async def vaults(self, owner_id: str) -> list[dict[str, Any]]: ...

    async def list_notes(self, owner_id: str) -> list[dict[str, Any]]: ...

    async def search_notes(self, owner_id: str, query: str, limit: int = 20) -> list[dict[str, Any]]: ...

    async def read_note(self, owner_id: str, path: str) -> dict[str, Any]: ...

    async def create_note(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        content: str = "",
        properties: Mapping[str, object] | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def append_note(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        text: str,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def set_properties(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        properties: Mapping[str, object],
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def daily_note(
        self,
        owner_id: str,
        operation_id: str,
        day: date | None = None,
        content: str = "",
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]: ...


_PATH = {
    "type": "string",
    "minLength": 1,
    "maxLength": _MAX_NOTE_PATH_CHARS,
    "description": "Относительный POSIX-путь заметки внутри собственного vault.",
}
_OPERATION_ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": _MAX_OPERATION_ID_CHARS,
    "description": "Стабильный ключ операции; при безопасном повторе используй тот же ключ.",
}
_WORK_ITEM_ID = {"type": "string", "minLength": 1, "maxLength": 200}
_EXPECTED_REVISION = {
    "type": "string",
    "pattern": _REVISION_PATTERN,
    "description": "SHA-256 revision, прочитанная перед изменением заметки.",
}
_PROPERTY_INPUT = {
    "anyOf": [
        {"type": "string"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "array", "items": {"type": "string"}, "maxItems": 256},
        {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["text", "list", "number", "checkbox", "date", "datetime"],
                },
                "value": {},
            },
            "required": ["type", "value"],
            "additionalProperties": False,
        },
    ]
}
_PROPERTIES = {
    "type": "object",
    "propertyNames": {"type": "string", "minLength": 1, "maxLength": 200},
    "additionalProperties": _PROPERTY_INPUT,
    "maxProperties": 100,
}


def _parameters(properties: Mapping[str, object], *, required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def build_obsidian_tools(ctx: ServiceContext) -> tuple[ToolSpec, ...]:
    """Build tools only when the optional runtime is actually available."""

    if ctx.obsidian is None:
        return ()
    runtime = cast(ObsidianToolRuntime, ctx.obsidian)

    async def list_vaults(*, actor: ActorContext) -> dict[str, Any]:
        vaults = await runtime.vaults(actor.own_id)
        return {"vaults": vaults, "count": len(vaults)}

    async def list_notes(*, actor: ActorContext) -> dict[str, Any]:
        notes = await runtime.list_notes(actor.own_id)
        return {"notes": notes, "count": len(notes)}

    async def search_notes(*, actor: ActorContext, query: str, limit: int = 20) -> dict[str, Any]:
        matches = await runtime.search_notes(actor.own_id, query, limit=limit)
        return {"matches": matches, "count": len(matches)}

    async def read_note(*, actor: ActorContext, path: str) -> dict[str, Any]:
        return await runtime.read_note(actor.own_id, path)

    async def create_note(
        *,
        actor: ActorContext,
        operation_id: str,
        path: str,
        content: str = "",
        properties: Mapping[str, object] | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        return await runtime.create_note(
            actor.own_id,
            operation_id,
            path,
            content=content,
            properties=properties,
            work_item_id=work_item_id,
        )

    async def append_note(
        *,
        actor: ActorContext,
        operation_id: str,
        path: str,
        text: str,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        return await runtime.append_note(
            actor.own_id,
            operation_id,
            path,
            text,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )

    async def set_properties(
        *,
        actor: ActorContext,
        operation_id: str,
        path: str,
        properties: Mapping[str, object],
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        return await runtime.set_properties(
            actor.own_id,
            operation_id,
            path,
            properties,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )

    async def daily_note(
        *,
        actor: ActorContext,
        operation_id: str,
        day: str | None = None,
        content: str = "",
        expected_revision: str | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        selected_day = None if day is None else date.fromisoformat(day)
        return await runtime.daily_note(
            actor.own_id,
            operation_id,
            day=selected_day,
            content=content,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
        )

    operation_properties = {
        "operation_id": _OPERATION_ID,
        "path": _PATH,
        "work_item_id": _WORK_ITEM_ID,
    }
    revision_properties = {**operation_properties, "expected_revision": _EXPECTED_REVISION}
    return (
        ToolSpec(
            name="obsidian_list_vaults",
            description="Показать подключённые Obsidian vault текущего человека и их состояние.",
            parameters=_parameters({}),
            security_id="obsidian.read",
            risk="observe",
            handler=list_vaults,
        ),
        ToolSpec(
            name="obsidian_list_notes",
            description="Показать обычные Markdown-заметки в собственном Obsidian vault.",
            parameters=_parameters({}),
            security_id="obsidian.read",
            risk="observe",
            handler=list_notes,
        ),
        ToolSpec(
            name="obsidian_search_notes",
            description="Найти заметки по пути, заголовку и тексту в собственном Obsidian vault.",
            parameters=_parameters(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 1_000},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                required=("query",),
            ),
            security_id="obsidian.read",
            risk="observe",
            handler=search_notes,
        ),
        ToolSpec(
            name="obsidian_read_note",
            description=(
                "Прочитать точное содержимое, свойства и revision одной заметки "
                "из собственного Obsidian vault."
            ),
            parameters=_parameters({"path": _PATH}, required=("path",)),
            security_id="obsidian.read",
            risk="observe",
            handler=read_note,
        ),
        ToolSpec(
            name="obsidian_create_note",
            description=(
                "Создать новую заметку без перезаписи существующей; повтор той же операции "
                "требует прежний operation_id."
            ),
            parameters=_parameters(
                {
                    **operation_properties,
                    "content": {"type": "string", "maxLength": _MAX_NOTE_TEXT_CHARS},
                    "properties": _PROPERTIES,
                },
                required=("operation_id", "path"),
            ),
            security_id="obsidian.write",
            risk="mutate",
            handler=create_note,
        ),
        ToolSpec(
            name="obsidian_append_note",
            description=(
                "Дописать текст в заметку идемпотентно; expected_revision защищает "
                "пользовательские изменения от перезаписи."
            ),
            parameters=_parameters(
                {
                    **revision_properties,
                    "text": {"type": "string", "maxLength": _MAX_NOTE_TEXT_CHARS},
                },
                required=("operation_id", "path", "text"),
            ),
            security_id="obsidian.write",
            risk="mutate",
            handler=append_note,
        ),
        ToolSpec(
            name="obsidian_set_properties",
            description=(
                "Изменить типизированные YAML-свойства заметки, сохранив неизвестные поля "
                "и пользовательский текст."
            ),
            parameters=_parameters(
                {**revision_properties, "properties": _PROPERTIES},
                required=("operation_id", "path", "properties"),
            ),
            security_id="obsidian.write",
            risk="mutate",
            handler=set_properties,
        ),
        ToolSpec(
            name="obsidian_daily_note",
            description=("Создать или идемпотентно дополнить ежедневную заметку по соглашению vault."),
            parameters=_parameters(
                {
                    "operation_id": _OPERATION_ID,
                    "day": {"type": "string", "format": "date"},
                    "content": {"type": "string", "maxLength": _MAX_NOTE_TEXT_CHARS},
                    "expected_revision": _EXPECTED_REVISION,
                    "work_item_id": _WORK_ITEM_ID,
                },
                required=("operation_id",),
            ),
            security_id="obsidian.write",
            risk="mutate",
            handler=daily_note,
        ),
    )


__all__ = ["ObsidianToolRuntime", "build_obsidian_tools"]
