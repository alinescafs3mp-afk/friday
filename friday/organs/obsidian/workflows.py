"""Durable core workflows layered over synchronized Obsidian note primitives."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, time
from pathlib import PurePosixPath
from typing import Any

from .base_spec import evaluate_base, parse_base
from .contracts import NoteNotFoundError, VaultDeliveryState, validate_revision
from .indexing import refresh_incremental_index
from .note_merge import build_preserve_both_preview
from .operations import ObsidianOperationService, OperationCommitUncertain
from .service import ObsidianService
from .structured_notes import markdown_headings
from .structured_service import StructuredNoteRecord, StructuredNoteService
from .task_index import render_dated_task
from .wikilinks import build_vault_link_graph

_READ_ACTIONS = frozenset({"search_tasks", "select_candidate", "backlinks", "conflict_preview", "query_base"})
_WRITE_ACTIONS = frozenset(
    {
        "add_task",
        "update_metadata",
        "append_active_section",
        "move_note",
        "create_from_template",
        "save_summary",
        "append_summary_links",
        "create_base",
        "replace_active_section",
        "accept_conflict_merge",
        "resume_previous",
        "delete_note",
    }
)
_WORKFLOW_STATUSES = frozenset({"completed", "selected", "preview", "resumed", "pending"})


@dataclass(frozen=True, slots=True)
class WorkflowReceipt:
    action: str
    status: str
    body: str
    path: str | None = None
    revision: str | None = None
    operation_id: str | None = None
    changed_paths: tuple[str, ...] = ()
    open_uri: str | None = None
    delivery: VaultDeliveryState | None = None

    def __post_init__(self) -> None:
        if self.action not in _READ_ACTIONS | _WRITE_ACTIONS:
            raise ValueError("unsupported Obsidian workflow action")
        if self.status not in _WORKFLOW_STATUSES:
            raise ValueError("unsupported Obsidian workflow status")
        if not isinstance(self.body, str) or not self.body or len(self.body) > 40_000 or "\x00" in self.body:
            raise ValueError("workflow body is invalid")

    def as_dict(self) -> dict[str, Any]:
        delivery = None
        if self.delivery is not None:
            delivery = {
                "local_write_complete": self.delivery.local_write_complete,
                "server_scan_complete": self.delivery.server_scan_complete,
                "android_connected": self.delivery.android_connected,
                "android_completion": self.delivery.android_completion,
                "android_received": self.delivery.android_received,
                "obsidian_opened": self.delivery.obsidian_opened,
            }
        return {
            "action": self.action,
            "status": self.status,
            "path": self.path,
            "revision": self.revision,
            "operation_id": self.operation_id,
            "changed_paths": list(self.changed_paths),
            "body": self.body,
            "open_uri": self.open_uri,
            "delivery": delivery,
        }


class ObsidianWorkflowService:
    """Execute bounded workflows for one owner, vault and conversation frame."""

    def __init__(
        self,
        storage: Any,
        notes: ObsidianService,
        operations: ObsidianOperationService,
        *,
        owner_id: str,
        context_key: str,
    ) -> None:
        vault = storage.get_obsidian_vault(owner_id)
        if vault is None or str(vault["id"]) != operations.vault_id:
            raise ValueError("owner Obsidian vault is unavailable")
        self.storage = storage
        self.notes = notes
        self.operations = operations
        self.owner_id = str(owner_id)
        self.context_key = _context_key(context_key)
        self.vault = vault
        self.structured = StructuredNoteService()

    def execute_read(self, payload: Mapping[str, object]) -> WorkflowReceipt:
        data = _payload(payload)
        action = _action(data, _READ_ACTIONS)
        refresh_incremental_index(
            self.storage,
            self.notes,
            owner_id=self.owner_id,
            vault_id=str(self.vault["id"]),
        )
        if action == "search_tasks":
            _keys(data, {"action", "query", "incomplete_only"}, required={"action"})
            query = _text(data.get("query", ""), "query", maximum=1_000, allow_empty=True)
            if data.get("incomplete_only", True) is not True:
                raise ValueError("workflow task query supports incomplete tasks only")
            records = tuple(
                StructuredNoteRecord(
                    path=item.path,
                    content=item.content,
                    title=item.title,
                    modified_at=item.modified_at,
                    properties=item.properties,
                )
                for item in (self.notes.read_note(summary.path) for summary in self.notes.list_notes())
            )
            hits = self.structured.query_incomplete_tasks(records, query=query)
            body = "Незавершённые задачи в Obsidian: " + str(len(hits))
            if hits:
                body += "\n" + "\n".join(
                    f"- {hit.path}: {hit.task.text}"
                    + (f"; срок {hit.due_at.isoformat(timespec='minutes')}" if hit.due_at else "")
                    for hit in hits
                )
            return WorkflowReceipt(action, "completed", body)
        if action == "select_candidate":
            _keys(data, {"action", "ordinal"}, required={"action", "ordinal"})
            ordinal = _integer(data["ordinal"], "ordinal", minimum=1, maximum=100)
            frame = self._required_frame()
            candidate_set_id = str(frame.get("candidate_set_id") or "")
            if not candidate_set_id:
                raise ValueError("the active Obsidian search candidate set is unavailable")
            selected = self.storage.select_obsidian_candidate(
                self.owner_id,
                candidate_set_id,
                ordinal,
            )
            document = self.notes.read_note(str(selected["observed_path"]))
            if document.revision != str(selected["observed_revision"]):
                raise ValueError("selected Obsidian candidate changed before opening")
            frame_payload = _frame_payload(frame)
            used = _used_paths(frame_payload, document.path)
            self.storage.upsert_obsidian_active_frame(
                self.owner_id,
                vault_id=str(self.vault["id"]),
                frame_id=self.context_key,
                active_binding_id=str(selected["binding_id"]),
                candidate_set_id=candidate_set_id,
                selected_binding_id=str(selected["binding_id"]),
                frame={"kind": "selection", "ordinal": ordinal, "used_paths": used},
                ttl_seconds=900,
            )
            return WorkflowReceipt(
                action,
                "selected",
                f"Выбрана заметка №{ordinal}: {document.path}\nRevision: {document.revision}",
                path=document.path,
                revision=document.revision,
                open_uri=self._open_uri(document.path),
            )
        if action == "backlinks":
            _keys(data, {"action", "target_path"}, required={"action", "target_path"})
            target = _markdown_path(data["target_path"])
            graph = build_vault_link_graph(self.notes.store)
            if target not in graph.paths:
                raise NoteNotFoundError(target)
            paths = tuple(dict.fromkeys(item.source_path for item in graph.backlinks(target)))
            body = f"Обратные ссылки на {target}: {len(paths)}"
            if paths:
                body += "\n" + "\n".join(f"- {path}" for path in paths)
            return WorkflowReceipt(action, "completed", body, path=target, changed_paths=paths)
        if action == "query_base":
            _keys(data, {"action", "name"}, required={"action", "name"})
            name = _text(data["name"], "Base name", maximum=200)
            base_path = self.notes.store.normalize_path(f"Bases/{name}.base")
            stored = self.notes.store.read_text(base_path)
            spec = parse_base(stored.text())
            base_records: list[dict[str, Any]] = []
            for summary in self.notes.list_notes():
                document = self.notes.read_note(summary.path)
                base_records.append(
                    {
                        "path": document.path,
                        "title": document.title,
                        "modified_at": document.modified_at,
                        "properties": document.properties,
                    }
                )
            rows = evaluate_base(spec, base_records)
            body = (
                f"Base {name} вычислен Friday server-side по текущим revisions; "
                f"актуальных строк: {len(rows)}."
            )
            if rows:
                body += "\n" + "\n".join(
                    "- "
                    + " | ".join(
                        f"{column}={row.get(column) if row.get(column) is not None else ''}"
                        for column in spec.columns
                    )
                    for row in rows
                )
            return WorkflowReceipt(
                action,
                "completed",
                body,
                path=base_path,
                revision=stored.revision,
                open_uri=self._open_uri(base_path),
            )
        _keys(data, {"action"}, required={"action"})
        return self._conflict_preview()

    def execute_write(
        self,
        operation_id: str,
        payload: Mapping[str, object],
    ) -> WorkflowReceipt:
        operation = _text(operation_id, "operation_id", maximum=200)
        data = _payload(payload)
        action = _action(data, _WRITE_ACTIONS)
        if action == "update_metadata":
            required = {"action", "path", "status", "project", "tags"}
            _keys(data, required, required=required)
            path = _markdown_path(data["path"])
            document = self.notes.read_note(path)
            raw_tags = data["tags"]
            if not isinstance(raw_tags, list):
                raise ValueError("tags must be a list")
            status_value = _text(data["status"], "status", maximum=500)
            project_value = _text(data["project"], "project", maximum=500)
            tags = tuple(_text(item, "tag", maximum=128) for item in raw_tags)
            merged = self.structured.merge_properties_and_tags(
                document.content,
                {"status": status_value, "project": project_value},
                tags=tags,
            )
            properties = {
                "status": merged.properties["status"],
                "project": merged.properties["project"],
                "tags": merged.properties["tags"],
            }
            merged_tags = merged.properties["tags"].value
            if not isinstance(merged_tags, tuple):
                raise ValueError("merged Obsidian tags are not a typed list")
            result = self.operations.set_properties(
                operation,
                path,
                properties,
                expected_revision=document.revision,
            )
            return self._finish_mutation(
                action,
                result,
                f"Свойства status, project и tags атомарно обновлены в {path}.",
                replay={
                    "method": "set_properties",
                    "path": path,
                    "properties": {
                        "status": status_value,
                        "project": project_value,
                        "tags": list(merged_tags),
                    },
                    "expected_revision": document.revision,
                },
            )
        if action == "add_task":
            _keys(
                data,
                {"action", "day", "due_date", "due_time", "text"},
                required={"action", "day", "due_date", "due_time", "text"},
            )
            day = date.fromisoformat(_text(data["day"], "day", maximum=10))
            due_day = date.fromisoformat(_text(data["due_date"], "due_date", maximum=10))
            due_time = time.fromisoformat(_text(data["due_time"], "due_time", maximum=5))
            task_text = _text(data["text"], "text", maximum=10_000)
            task = render_dated_task(
                task_text,
                due_date=due_day,
                due_time=due_time,
                operation_id=operation,
            )
            result = self.operations.daily_note(
                operation,
                day,
                section="Friday",
                item=task,
            )
            return self._finish_mutation(
                action,
                result,
                f"Задача добавлена в {result.path}, раздел Friday; срок {due_day.isoformat()} {due_time.strftime('%H:%M')}.",
                replay={"method": "daily_note", "day": day.isoformat(), "section": "Friday", "item": task},
            )
        if action == "append_active_section":
            _keys(data, {"action", "section", "item"}, required={"action", "section", "item"})
            frame, path, revision = self._active_target()
            section = _text(data["section"], "section", maximum=500)
            item = _text(data["item"], "item", maximum=200_000)
            addition = f"## {section}\n\n{item}"
            result = self.operations.append_note(
                operation,
                path,
                addition,
                expected_revision=revision,
            )
            return self._finish_mutation(
                action,
                result,
                f"Раздел «{section}» добавлен в {path}.",
                previous_frame=frame,
                replay={
                    "method": "append_note",
                    "path": path,
                    "text": addition,
                    "expected_revision": revision,
                },
            )
        if action == "create_from_template":
            required = {
                "action",
                "template_name",
                "title",
                "project",
                "participants",
                "discussion",
                "actions",
                "day",
            }
            _keys(data, required, required=required)
            template_name = _text(data["template_name"], "template_name", maximum=200)
            template_path = f"{self.notes.convention.template_folder}/{template_name}.md"
            template = self.notes.read_note(template_path)
            participants = data["participants"]
            if not isinstance(participants, list) or not participants:
                raise ValueError("participants must be a non-empty list")
            title = _text(data["title"], "title", maximum=500)
            day = date.fromisoformat(_text(data["day"], "day", maximum=10))
            rendered = self.structured.render_from_template(
                template.content,
                {
                    "title": title,
                    "project": _text(data["project"], "project", maximum=500),
                    "participants": [_text(item, "participant", maximum=500) for item in participants],
                    "discussion": _text(data["discussion"], "discussion", maximum=200_000),
                    "actions": _text(data["actions"], "actions", maximum=200_000),
                },
                current_date=day,
            )
            path = f"Meetings/{day.isoformat()} {title}.md"
            result = self.operations.create_note(operation, path, rendered.content)
            return self._finish_mutation(
                action,
                result,
                f"Заметка создана по шаблону {template_path}.",
                replay={"method": "create_note", "path": path, "content": rendered.content},
            )
        if action == "save_summary":
            _keys(data, {"action", "path", "day"}, required={"action", "path", "day"})
            path = _markdown_path(data["path"])
            conclusions, questions, next_actions = self._conversation_summary_facts()
            content = self.structured.render_summary(
                conclusions=conclusions,
                open_questions=questions,
                next_actions=next_actions,
            )
            work_item_id = self._summary_work_item_id(operation)
            result = self.operations.create_note(
                operation,
                path,
                content,
                work_item_id=work_item_id,
            )
            return self._finish_mutation(
                action,
                result,
                "Структурированные итоги текущего разговора сохранены без внутренних трасс. "
                f"Work Item: {work_item_id}.",
                previous_frame=self._frame(optional=True),
                replay={
                    "method": "create_note",
                    "path": path,
                    "content": content,
                    "work_item_id": work_item_id,
                },
                work_item_id=work_item_id,
            )
        if action == "append_summary_links":
            _keys(data, {"action", "day"}, required={"action", "day"})
            frame, path, revision, work_item_id = self._summary_target()
            document = self.notes.read_note(path)
            frame_payload = _frame_payload(frame)
            paths = [item for item in frame_payload.get("used_paths", []) if isinstance(item, str)]
            paths = [item for item in paths if item != path]
            linked = self.structured.add_summary_links(document.content, paths)
            if not linked.changed:
                result = self.operations.append_note(
                    operation,
                    path,
                    "",
                    expected_revision=revision,
                    work_item_id=work_item_id,
                )
                return self._finish_mutation(
                    action,
                    result,
                    "Все использованные сегодня заметки уже связаны с итогами; durable no-op подтверждён.",
                    previous_frame=frame,
                    replay={
                        "method": "append_note",
                        "path": path,
                        "text": "",
                        "expected_revision": revision,
                        "work_item_id": work_item_id,
                    },
                    work_item_id=work_item_id,
                )
            if not linked.content.startswith(document.content):
                raise ValueError("summary link update is not append-only")
            addition = linked.content[len(document.content) :]
            result = self.operations.append_note(
                operation,
                path,
                addition,
                expected_revision=revision,
                work_item_id=work_item_id,
            )
            return self._finish_mutation(
                action,
                result,
                f"Добавлены ссылки на использованные заметки: {len(linked.added_paths)}.",
                previous_frame=frame,
                replay={
                    "method": "append_note",
                    "path": path,
                    "text": addition,
                    "expected_revision": revision,
                    "work_item_id": work_item_id,
                },
                work_item_id=work_item_id,
            )
        if action == "create_base":
            _keys(
                data,
                {"action", "name", "project", "excluded_status", "columns"},
                required={"action", "name", "project", "excluded_status", "columns"},
            )
            if _text(data["project"], "project", maximum=500).casefold() != "friday":
                raise ValueError("only the supported Friday BaseSpec is available")
            if _text(data["excluded_status"], "excluded_status", maximum=500).casefold() != "done":
                raise ValueError("unsupported Base exclusion")
            records = tuple(
                StructuredNoteRecord(
                    path=item.path,
                    content=item.content,
                    title=item.title,
                    modified_at=item.modified_at,
                    properties=item.properties,
                )
                for item in (self.notes.read_note(summary.path) for summary in self.notes.list_notes())
            )
            base = self.structured.generate_friday_base(
                records,
                name=_text(data["name"], "name", maximum=200),
            )
            method = getattr(self.operations, "create_base", None)
            if not callable(method):
                raise ValueError("durable Base operation is unavailable")
            result = method(operation, base.path, base.content)
            return self._finish_mutation(
                action,
                result,
                f"Base создан и проверен evaluator=friday; строк: {len(base.rows)}.",
                changed_paths=(base.path,),
                replay={"method": "create_base", "path": base.path, "content": base.content},
                index_markdown=False,
            )
        if action == "replace_active_section":
            _keys(data, {"action", "section", "text"}, required={"action", "section", "text"})
            method = getattr(self.operations, "replace_section", None)
            if not callable(method):
                raise ValueError("durable section replacement is unavailable")
            section = _text(data["section"], "section", maximum=500)
            replacement = _text(data["text"], "text", maximum=200_000)
            path, observed_revision = self._unique_heading_target(section)
            operation_row = self.storage.get_obsidian_operation(self.owner_id, operation)
            if operation_row is not None:
                if str(operation_row.get("method") or "") != "replace":
                    raise ValueError("operation ID belongs to a different Obsidian mutation")
                revision = validate_revision(str(operation_row.get("expected_revision") or ""))
            else:
                revision = observed_revision
            result = method(
                operation,
                path,
                section,
                replacement,
                expected_revision=revision,
            )
            return self._finish_mutation(
                action,
                result,
                f"Раздел «{section}» заменён в единственной заметке {path}; доставка отслеживается отдельно.",
                previous_frame=self._frame(optional=True),
                replay={
                    "method": "replace_section",
                    "path": path,
                    "section": section,
                    "text": replacement,
                    "expected_revision": revision,
                },
            )
        if action == "accept_conflict_merge":
            _keys(data, {"action"}, required={"action"})
            return self._accept_conflict_merge(operation)
        if action == "move_note":
            required = {"action", "source_path", "destination_path", "update_links"}
            _keys(data, required, required=required)
            source = _markdown_path(data["source_path"])
            destination = _markdown_path(data["destination_path"])
            update_links = data["update_links"]
            if not isinstance(update_links, bool):
                raise ValueError("update_links must be a bool")
            operation_row = self.storage.get_obsidian_operation(self.owner_id, operation)
            move_binding: dict[str, Any] | None = None
            if operation_row is None:
                observed = self.notes.read_note(source)
                expected_revision = observed.revision
                move_binding = self._binding_for_path(source)
            else:
                if str(operation_row.get("method") or "") != "move":
                    raise ValueError("operation ID belongs to a different Obsidian mutation")
                expected_revision = validate_revision(str(operation_row.get("expected_revision") or ""))
            method = getattr(self.operations, "move_note", None)
            if not callable(method):
                raise ValueError("durable note move is unavailable")
            result = method(
                operation,
                source,
                destination,
                expected_revision=expected_revision,
                update_links=update_links,
            )
            revision = validate_revision(str(getattr(result, "revision", "")))
            try:
                operation_row = self.storage.get_obsidian_operation(self.owner_id, operation)
                if operation_row is None:
                    raise ValueError("durable move operation disappeared")
                move_binding = move_binding or self._move_binding_for_projection(
                    source,
                    destination,
                    expected_revision=expected_revision,
                    target_revision=revision,
                    operation_created_at=str(operation_row.get("created_at") or ""),
                )
                self._project_moved_binding(move_binding, destination, revision)
            except Exception as exc:
                raise OperationCommitUncertain(
                    "note move committed but stable identity projection is incomplete; "
                    "retry the same operation ID"
                ) from exc
            changed = tuple(getattr(result, "changed_paths", (source, destination)))
            report = ["Заметка перемещена; однозначные ссылки обновлены."]
            for label, issues in (
                ("Неоднозначные ссылки", getattr(result, "ambiguous", ())),
                ("Неразрешённые ссылки", getattr(result, "unresolved", ())),
                ("Динамические ссылки", getattr(result, "dynamic", ())),
            ):
                if issues:
                    report.append(
                        label
                        + ": "
                        + "; ".join(
                            f"{issue.source_path} -> {issue.target}"
                            + (f" ({', '.join(issue.candidates)})" if issue.candidates else "")
                            for issue in issues
                        )
                    )
            return self._finish_mutation(
                action,
                result,
                "\n".join(report)[:40_000],
                changed_paths=changed,
                replay={
                    "method": "move_note",
                    "source_path": source,
                    "destination_path": destination,
                    "expected_revision": expected_revision,
                    "update_links": update_links,
                },
                target_path=destination,
            )
        if action == "delete_note":
            _keys(data, {"action", "path"}, required={"action", "path"})
            path = _markdown_path(data["path"])
            operation_row = self.storage.get_obsidian_operation(self.owner_id, operation)
            delete_binding: dict[str, Any] | None = None
            if operation_row is None:
                observed = self.notes.read_note(path)
                expected_revision = observed.revision
                delete_binding = self._binding_for_path(path)
            else:
                if str(operation_row.get("method") or "") != "delete":
                    raise ValueError("operation ID belongs to a different Obsidian mutation")
                expected_revision = validate_revision(str(operation_row.get("expected_revision") or ""))
            method = getattr(self.operations, "delete_note", None)
            if not callable(method):
                raise ValueError("durable note delete is unavailable")
            result = method(operation, path, expected_revision=expected_revision)
            try:
                operation_row = self.storage.get_obsidian_operation(self.owner_id, operation)
                if operation_row is None:
                    raise ValueError("durable delete operation disappeared")
                delete_binding = delete_binding or self._deleted_binding_for_projection(
                    path,
                    expected_revision=expected_revision,
                    operation_created_at=str(operation_row.get("created_at") or ""),
                )
                self.storage.tombstone_obsidian_note_binding(
                    self.owner_id,
                    str(delete_binding["integration_id"]),
                    vault_id=str(self.vault["id"]),
                    expected_revision=expected_revision,
                )
                self.storage.invalidate_obsidian_active_frame(
                    self.owner_id,
                    self.context_key,
                )
            except Exception as exc:
                raise OperationCommitUncertain(
                    "note deletion committed but its tombstone projection is incomplete; "
                    "retry the same operation ID"
                ) from exc
            return self._receipt_from_result(
                action,
                result,
                "Заметка удалена; её identity сохранена как tombstone, live-индекс и Active Frame закрыты.",
                changed_paths=(path,),
                target_path=path,
            )
        _keys(data, {"action"}, required={"action"})
        return self._resume_previous(operation)

    def _finish_mutation(
        self,
        action: str,
        result: Any,
        body: str,
        *,
        previous_frame: Mapping[str, Any] | None = None,
        changed_paths: tuple[str, ...] | None = None,
        replay: Mapping[str, Any],
        index_markdown: bool = True,
        target_path: str | None = None,
        work_item_id: str | None = None,
    ) -> WorkflowReceipt:
        path = target_path or str(getattr(result, "path", ""))
        operation_id = str(getattr(result, "operation_id", ""))
        if not operation_id:
            raise OperationCommitUncertain("durable workflow result has no operation identity")
        frame_payload = _frame_payload(previous_frame or self._frame(optional=True))
        used = (
            _used_paths(frame_payload, path)
            if path.endswith(".md")
            else list(frame_payload.get("used_paths", []))
        )
        raw_changed_paths = changed_paths
        if raw_changed_paths is None:
            result_changed_paths = getattr(result, "changed_paths", ())
            raw_changed_paths = (
                tuple(str(item) for item in result_changed_paths)
                if isinstance(result_changed_paths, (tuple, list))
                else ()
            )
        pending_frame = {
            "kind": "workflow_projection_pending_v1",
            "action": action,
            "used_paths": used,
            "replay": dict(replay),
            "index_markdown": index_markdown,
            "target_path": path,
            "changed_paths": list(raw_changed_paths),
        }
        try:
            self.storage.upsert_obsidian_active_frame(
                self.owner_id,
                vault_id=str(self.vault["id"]),
                frame_id=self.context_key,
                work_item_id=work_item_id,
                last_operation_id=operation_id,
                frame=pending_frame,
                ttl_seconds=24 * 60 * 60,
            )
            if index_markdown:
                refresh_incremental_index(
                    self.storage,
                    self.notes,
                    owner_id=self.owner_id,
                    vault_id=str(self.vault["id"]),
                    discovered_origin="friday",
                )
            binding = self._binding_for_path(path) if index_markdown else None
            self.storage.upsert_obsidian_active_frame(
                self.owner_id,
                vault_id=str(self.vault["id"]),
                frame_id=self.context_key,
                work_item_id=work_item_id,
                active_binding_id=None if binding is None else str(binding["id"]),
                last_operation_id=operation_id,
                frame={
                    "kind": "workflow",
                    "action": action,
                    "used_paths": used,
                    "replay": dict(replay),
                },
                ttl_seconds=24 * 60 * 60,
            )
        except Exception as exc:
            raise OperationCommitUncertain(
                "workflow mutation committed but its index or Active Frame is incomplete; "
                "continue the previous task"
            ) from exc
        return self._receipt_from_result(
            action,
            result,
            body,
            changed_paths=changed_paths,
            target_path=path,
        )

    def _receipt_from_result(
        self,
        action: str,
        result: Any,
        body: str,
        *,
        changed_paths: tuple[str, ...] | None = None,
        target_path: str | None = None,
        status: str = "completed",
    ) -> WorkflowReceipt:
        raw_path = target_path or getattr(result, "path", None)
        path = raw_path if isinstance(raw_path, str) and raw_path else None
        raw_revision = getattr(result, "revision", None)
        revision = raw_revision if isinstance(raw_revision, str) and raw_revision else None
        raw_operation_id = getattr(result, "operation_id", None)
        operation_id = raw_operation_id if isinstance(raw_operation_id, str) and raw_operation_id else None
        delivery = getattr(result, "delivery", None)
        if not isinstance(delivery, VaultDeliveryState):
            delivery = VaultDeliveryState.local_only() if operation_id else None
        paths = changed_paths
        if paths is None:
            raw = getattr(result, "changed_paths", ())
            paths = tuple(str(item) for item in raw) if raw else ((path,) if path else ())
        return WorkflowReceipt(
            action,
            status,
            body,
            path=path,
            revision=revision,
            operation_id=operation_id,
            changed_paths=paths,
            open_uri=(
                self._open_uri(path) if action != "delete_note" and path and path.endswith(".md") else None
            ),
            delivery=delivery,
        )

    def _resume_previous(self, operation_id: str) -> WorkflowReceipt:
        frame = self._required_frame()
        frame_payload = _frame_payload(frame)
        last_operation = str(
            frame.get("last_operation_id") or frame_payload.get("pending_operation_id") or ""
        )
        if not last_operation:
            raise ValueError("previous Obsidian operation is unavailable")
        if operation_id != last_operation:
            # The current transport identity is new; resumption deliberately
            # reuses the durable previous operation identity instead.
            operation_id = last_operation
        replay = frame_payload.get("replay")
        if not isinstance(replay, Mapping):
            result = self.operations.get_operation(operation_id)
            return self._receipt_from_result(
                "resume_previous",
                result,
                "Предыдущая операция найдена; её сохранённая квитанция проверена.",
                status="resumed",
            )
        method_name = _text(replay.get("method"), "replay method", maximum=100)
        method = getattr(self.operations, method_name, None)
        if not callable(method):
            raise ValueError("previous Obsidian operation cannot be resumed safely")
        arguments = {key: value for key, value in replay.items() if key != "method"}
        if method_name == "daily_note" and isinstance(arguments.get("day"), str):
            arguments["day"] = date.fromisoformat(str(arguments["day"]))
        result = method(operation_id, **arguments)
        if frame_payload.get("kind") == "workflow_projection_pending_v1":
            resumed_action = _action(frame_payload, _WRITE_ACTIONS - {"resume_previous"})
            target_path = _text(
                frame_payload.get("target_path", str(getattr(result, "path", ""))),
                "target_path",
                maximum=2_048,
                allow_empty=True,
            )
            raw_changed_paths = frame_payload.get("changed_paths", [])
            if not isinstance(raw_changed_paths, list):
                raise ValueError("pending workflow changed paths are invalid")
            changed_paths = tuple(_text(item, "changed path", maximum=2_048) for item in raw_changed_paths)
            self._finish_mutation(
                resumed_action,
                result,
                "Предыдущая операция восстановлена после незавершённой проекции контекста.",
                previous_frame=frame,
                changed_paths=changed_paths or None,
                replay=replay,
                index_markdown=frame_payload.get("index_markdown") is True,
                target_path=target_path or None,
                work_item_id=str(frame.get("work_item_id") or "") or None,
            )
        return self._receipt_from_result(
            "resume_previous",
            result,
            "Предыдущая операция возобновлена с той же operation ID; постусловие сверено до повтора.",
            status="resumed",
        )

    def _conflict_preview(self) -> WorkflowReceipt:
        conflicts = [
            item
            for item in self.storage.list_obsidian_conflicts(self.owner_id)
            if item.get("status") == "open"
        ]
        if len(conflicts) != 1:
            raise ValueError("one exact open Obsidian conflict could not be resolved")
        conflict = conflicts[0]
        conflict_id = _text(conflict.get("id"), "conflict_id", maximum=200)
        canonical_path = _markdown_path(conflict["canonical_path"])
        conflict_path = _markdown_path(conflict["conflict_path"])
        canonical = self.notes.store.read_text(canonical_path)
        other = self.notes.store.read_text(conflict_path)
        preview = build_preserve_both_preview(canonical.text(), other.text())
        if preview.canonical_revision != canonical.revision or preview.conflict_revision != other.revision:
            raise ValueError("conflict preview revisions do not match the vault snapshots")
        binding = self._binding_for_path(canonical_path)
        merged_revision = hashlib.sha256(preview.merged_content.encode("utf-8", errors="strict")).hexdigest()
        previous = _frame_payload(self._frame(optional=True))
        used = _used_paths(previous, canonical_path)
        used = _used_paths({"used_paths": used}, conflict_path)
        self.storage.upsert_obsidian_active_frame(
            self.owner_id,
            vault_id=str(self.vault["id"]),
            frame_id=self.context_key,
            frame={
                "kind": "conflict_preview_v1",
                "conflict_id": conflict_id,
                "canonical_binding_id": str(binding["id"]),
                "canonical_integration_id": str(binding["integration_id"]),
                "canonical_path": canonical_path,
                "canonical_revision": canonical.revision,
                "conflict_path": conflict_path,
                "conflict_revision": other.revision,
                "merged_revision": merged_revision,
                "strategy": "preserve_both_v1",
                "used_paths": used,
            },
            ttl_seconds=24 * 60 * 60,
        )
        body = (
            f"Conflict preview: {canonical_path} ↔ {conflict_path}\n"
            f"Canonical revision: {preview.canonical_revision}\n"
            f"Conflict revision: {preview.conflict_revision}\n\n"
            f"{preview.unified_diff}\n\nMerged preview (not applied):\n{preview.merged_content}"
        )
        return WorkflowReceipt(
            "conflict_preview",
            "preview",
            body[:40_000],
            path=canonical_path,
            revision=canonical.revision,
            changed_paths=(canonical_path, conflict_path),
            open_uri=self._open_uri(canonical_path),
        )

    def _accept_conflict_merge(self, operation_id: str) -> WorkflowReceipt:
        frame = self._required_frame()
        frozen = _frame_payload(frame)
        if frozen.get("kind") == "conflict_resolution_v1":
            previous_operation = str(frame.get("last_operation_id") or "")
            if not previous_operation:
                raise ValueError("the accepted conflict operation is unavailable")
            result = self.operations.get_operation(previous_operation)
            return self._receipt_from_result(
                "accept_conflict_merge",
                result,
                "Объединённая preserve-both версия уже применена; durable-квитанция повторно проверена.",
                changed_paths=(str(getattr(result, "path", "")),),
            )
        preview = _conflict_frame(frozen)
        conflict = self.storage.get_obsidian_conflict(
            self.owner_id,
            preview["conflict_id"],
        )
        if conflict is None or (
            str(conflict.get("vault_id") or "") != str(self.vault["id"])
            or str(conflict.get("canonical_path") or "") != preview["canonical_path"]
            or str(conflict.get("conflict_path") or "") != preview["conflict_path"]
            or str(conflict.get("status") or "") not in {"open", "resolved"}
        ):
            raise ValueError("the frozen Obsidian conflict identity is no longer available")
        binding = self._binding_by_id(preview["canonical_binding_id"])
        if (
            str(binding.get("integration_id") or "") != preview["canonical_integration_id"]
            or str(binding.get("current_path") or "") != preview["canonical_path"]
            or str(binding.get("current_revision") or "")
            not in {preview["canonical_revision"], preview["merged_revision"]}
        ):
            raise ValueError("the frozen canonical note identity changed")
        canonical = self.notes.store.read_text(preview["canonical_path"])
        artifact = self.notes.store.read_text(preview["conflict_path"])
        if artifact.revision != preview["conflict_revision"]:
            raise ValueError("the conflict artifact changed after preview")
        if canonical.revision == preview["canonical_revision"]:
            rebuilt = build_preserve_both_preview(canonical.text(), artifact.text())
            merged_revision = hashlib.sha256(
                rebuilt.merged_content.encode("utf-8", errors="strict")
            ).hexdigest()
            if (
                rebuilt.canonical_revision != preview["canonical_revision"]
                or rebuilt.conflict_revision != preview["conflict_revision"]
                or merged_revision != preview["merged_revision"]
            ):
                raise ValueError("the preserve-both preview no longer matches its frozen revisions")
        elif canonical.revision != preview["merged_revision"]:
            raise ValueError("the canonical note changed after preview")

        accepted_operation_id = operation_id
        if str(conflict.get("status") or "") == "resolved":
            resolution = conflict.get("resolution_json")
            if isinstance(resolution, str):
                try:
                    resolution = json.loads(resolution)
                except json.JSONDecodeError as exc:
                    raise ValueError("the stored Obsidian conflict resolution is invalid") from exc
            if not isinstance(resolution, Mapping):
                raise ValueError("the stored Obsidian conflict resolution is invalid")
            expected_resolution = {
                "schema": "friday.obsidian-conflict-resolution.v1",
                "strategy": "preserve_both",
                "conflict_id": preview["conflict_id"],
                "canonical_path": preview["canonical_path"],
                "conflict_path": preview["conflict_path"],
                "canonical_revision": preview["canonical_revision"],
                "conflict_revision": preview["conflict_revision"],
                "merged_revision": preview["merged_revision"],
            }
            if any(resolution.get(key) != value for key, value in expected_resolution.items()):
                raise ValueError("the stored Obsidian conflict resolution changed")
            accepted_operation_id = _text(
                resolution.get("operation_id"),
                "resolved conflict operation_id",
                maximum=200,
            )
            result = self.operations.get_operation(accepted_operation_id)
            if (
                str(getattr(result, "method", "")) != "conflict_merge"
                or str(getattr(result, "path", "")) != preview["canonical_path"]
                or str(getattr(result, "revision", "")) != preview["merged_revision"]
            ):
                raise ValueError("the durable conflict resolution receipt changed")
        else:
            method = getattr(self.operations, "apply_conflict_merge", None)
            if not callable(method):
                raise ValueError("durable conflict merge operation is unavailable")
            result = method(
                operation_id,
                preview["conflict_id"],
                preview["canonical_path"],
                preview["conflict_path"],
                expected_revision=preview["canonical_revision"],
                conflict_revision=preview["conflict_revision"],
            )
        committed = self.notes.store.read_text(preview["canonical_path"])
        preserved = self.notes.store.read_text(preview["conflict_path"])
        if (
            committed.revision != preview["merged_revision"]
            or preserved.revision != preview["conflict_revision"]
        ):
            raise OperationCommitUncertain(
                "conflict merge committed without its exact dual-revision postcondition"
            )
        if str(conflict.get("status") or "") == "open":
            try:
                self.storage.resolve_obsidian_conflict(
                    self.owner_id,
                    preview["conflict_id"],
                    vault_id=str(self.vault["id"]),
                    canonical_path=preview["canonical_path"],
                    conflict_path=preview["conflict_path"],
                    canonical_revision=preview["canonical_revision"],
                    conflict_revision=preview["conflict_revision"],
                    merged_revision=preview["merged_revision"],
                    operation_id=accepted_operation_id,
                )
            except Exception as exc:
                raise OperationCommitUncertain(
                    "canonical merge committed but conflict resolution receipt is incomplete"
                ) from exc
        try:
            refresh_incremental_index(
                self.storage,
                self.notes,
                owner_id=self.owner_id,
                vault_id=str(self.vault["id"]),
                discovered_origin="friday",
            )
            final_binding = self._binding_by_id(preview["canonical_binding_id"])
            if str(final_binding.get("current_revision") or "") != preview["merged_revision"]:
                raise ValueError("merged canonical binding was not refreshed")
            self.storage.upsert_obsidian_active_frame(
                self.owner_id,
                vault_id=str(self.vault["id"]),
                frame_id=self.context_key,
                active_binding_id=preview["canonical_binding_id"],
                last_operation_id=accepted_operation_id,
                frame={
                    "kind": "conflict_resolution_v1",
                    "action": "accept_conflict_merge",
                    **preview,
                },
                ttl_seconds=24 * 60 * 60,
            )
        except Exception as exc:
            raise OperationCommitUncertain(
                "conflict resolution committed but its index or Active Frame is incomplete; retry acceptance"
            ) from exc
        return self._receipt_from_result(
            "accept_conflict_merge",
            result,
            (
                "Объединённая preserve-both версия применена к canonical note; "
                f"conflict artifact сохранён: {preview['conflict_path']}."
            ),
            changed_paths=(preview["canonical_path"],),
            target_path=preview["canonical_path"],
        )

    def _active_target(self) -> tuple[dict[str, Any], str, str]:
        frame = self._required_frame()
        path = str(frame.get("selected_path") or frame.get("active_path") or "")
        revision = str(frame.get("selected_revision") or frame.get("active_revision") or "")
        if not path or not revision:
            raise ValueError("active Obsidian note is unavailable or expired")
        current = self.notes.read_note(path)
        if current.revision != revision:
            raise ValueError("active Obsidian note changed; re-resolve it explicitly")
        return frame, path, revision

    def _summary_work_item_id(self, operation_id: str) -> str:
        payload = (
            "friday.obsidian-summary-work-item.v1\0"
            f"{self.owner_id}\0{self.vault['id']}\0{self.context_key}\0{operation_id}"
        )
        return "obswork_" + hashlib.sha256(payload.encode("utf-8", errors="strict")).hexdigest()[:32]

    def _summary_target(self) -> tuple[dict[str, Any], str, str, str]:
        frame = self._required_frame()
        payload = _frame_payload(frame)
        if payload.get("kind") != "workflow" or payload.get("action") not in {
            "save_summary",
            "append_summary_links",
        }:
            raise ValueError("the active Work Item is not a conversation summary")
        work_item_id = _text(frame.get("work_item_id"), "work_item_id", maximum=200)
        path = str(frame.get("active_path") or "")
        revision = str(frame.get("active_revision") or "")
        if not path or not revision:
            raise ValueError("the conversation summary Work Item target is unavailable")
        current = self.notes.read_note(path)
        if current.revision != revision:
            raise ValueError("the conversation summary changed; re-resolve it explicitly")
        return frame, path, revision, work_item_id

    def _unique_heading_target(self, section: str) -> tuple[str, str]:
        wanted = unicodedata.normalize("NFC", section).casefold().strip()
        matches: list[tuple[str, str]] = []
        for stored in self.notes.store.iter_markdown_files():
            for level, title in markdown_headings(stored.text()):
                if level == 2 and unicodedata.normalize("NFC", title).casefold().strip() == wanted:
                    matches.append((stored.path, stored.revision))
                    if len(matches) > 1:
                        raise ValueError("section heading is not unique across ordinary Obsidian notes")
        if len(matches) != 1:
            raise ValueError("section heading does not resolve to one ordinary Obsidian note")
        return matches[0]

    def _required_frame(self) -> dict[str, Any]:
        frame = self._frame()
        if frame is None:
            raise ValueError("active Obsidian context is unavailable or expired")
        return frame

    def _binding_for_path(self, path: str) -> dict[str, Any]:
        for item in self.storage.list_obsidian_note_bindings(
            self.owner_id,
            vault_id=str(self.vault["id"]),
            limit=5_000,
        ):
            if str(item["current_path"]) == path:
                return item
        raise ValueError("current Obsidian note binding not found")

    def _binding_by_id(self, binding_id: str) -> dict[str, Any]:
        for item in self.storage.list_obsidian_note_bindings(
            self.owner_id,
            vault_id=str(self.vault["id"]),
            limit=5_000,
        ):
            if str(item["id"]) == binding_id:
                return item
        raise ValueError("frozen canonical Obsidian binding not found")

    def _move_binding_for_projection(
        self,
        source_path: str,
        destination_path: str,
        *,
        expected_revision: str,
        target_revision: str,
        operation_created_at: str,
    ) -> dict[str, Any]:
        bindings = self.storage.list_obsidian_note_bindings(
            self.owner_id,
            vault_id=str(self.vault["id"]),
            include_deleted=True,
            limit=5_000,
        )
        source_matches = [
            item
            for item in bindings
            if str(item.get("current_path") or "") == source_path
            and str(item.get("current_revision") or "") == expected_revision
            and (
                item.get("deleted_at") is None
                or not operation_created_at
                or str(item.get("deleted_at") or "") >= operation_created_at
            )
        ]
        if len(source_matches) == 1:
            return source_matches[0]
        if source_matches:
            raise ValueError("stable source binding is ambiguous after move")
        destination_matches = [
            item
            for item in bindings
            if item.get("deleted_at") is None
            and str(item.get("current_path") or "") == destination_path
            and str(item.get("current_revision") or "") == target_revision
        ]
        if len(destination_matches) != 1:
            raise ValueError("stable note binding cannot be recovered after move")
        return destination_matches[0]

    def _deleted_binding_for_projection(
        self,
        path: str,
        *,
        expected_revision: str,
        operation_created_at: str,
    ) -> dict[str, Any]:
        matches = [
            item
            for item in self.storage.list_obsidian_note_bindings(
                self.owner_id,
                vault_id=str(self.vault["id"]),
                include_deleted=True,
                limit=5_000,
            )
            if str(item.get("current_path") or "") == path
            and str(item.get("current_revision") or "") == expected_revision
            and (
                item.get("deleted_at") is None
                or not operation_created_at
                or str(item.get("deleted_at") or "") >= operation_created_at
            )
        ]
        if len(matches) != 1:
            raise ValueError("stable note binding cannot be recovered after deletion")
        return matches[0]

    def _project_moved_binding(
        self,
        binding: Mapping[str, Any],
        destination_path: str,
        target_revision: str,
    ) -> None:
        bindings = self.storage.list_obsidian_note_bindings(
            self.owner_id,
            vault_id=str(self.vault["id"]),
            include_deleted=True,
            limit=5_000,
        )
        for duplicate in bindings:
            if (
                duplicate.get("deleted_at") is None
                and str(duplicate.get("id") or "") != str(binding.get("id") or "")
                and str(duplicate.get("current_path") or "") == destination_path
            ):
                self.storage.tombstone_obsidian_note_binding(
                    self.owner_id,
                    str(duplicate["integration_id"]),
                    vault_id=str(self.vault["id"]),
                    expected_revision=str(duplicate["current_revision"]),
                )
        projection = binding.get("projection_json")
        if isinstance(projection, str):
            try:
                projection = json.loads(projection)
            except json.JSONDecodeError as exc:
                raise ValueError("stable note binding projection is invalid") from exc
        if not isinstance(projection, Mapping):
            raise ValueError("stable note binding projection is invalid")
        self.storage.upsert_obsidian_note_binding(
            self.owner_id,
            vault_id=str(self.vault["id"]),
            integration_id=str(binding["integration_id"]),
            current_path=destination_path,
            current_revision=target_revision,
            ownership_mode=str(binding["ownership_mode"]),
            origin=str(binding["origin"]),
            projection_kind=binding.get("projection_kind"),
            projection=dict(projection),
            friday_object_kind=binding.get("friday_object_kind"),
            friday_object_id=binding.get("friday_object_id"),
            expected_current_revision=str(binding["current_revision"]),
        )

    def _frame(self, *, optional: bool = False) -> dict[str, Any] | None:
        frame = self.storage.get_obsidian_active_frame(
            self.owner_id,
            self.context_key,
        )
        if frame is None and not optional:
            raise ValueError("active Obsidian context is unavailable or expired")
        return frame

    def _conversation_summary_facts(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        messages = self.storage.get_conversation_messages(
            self.context_key,
            user_id=self.owner_id,
            limit=50,
        )
        conclusions: list[str] = []
        unresolved: list[tuple[str, str]] = []
        pending: tuple[str, str] | None = None
        for item in messages:
            text = " ".join(str(item.get("content") or "").split()).strip()
            if not text or len(text) > 2_000 or "<tool" in text.casefold():
                continue
            role = str(item.get("role") or "")
            if role == "user":
                if pending is not None:
                    unresolved.append(pending)
                pending = (
                    ("question", text[:500])
                    if text.endswith("?")
                    else ("action", text[:500])
                    if _looks_like_action(text)
                    else None
                )
            elif role == "assistant":
                if _assistant_turn_completed(text):
                    conclusions.append(text[:500])
                    pending = None
                elif pending is not None:
                    unresolved.append(pending)
                    pending = None
        if pending is not None:
            unresolved.append(pending)
        questions = [text for kind, text in unresolved if kind == "question"]
        next_actions = [text for kind, text in unresolved if kind == "action"]
        return (
            tuple(conclusions[-8:]) or ("Нет явно подтверждённых выводов.",),
            tuple(questions[-8:]) or ("Нет явно нерешённых вопросов.",),
            tuple(next_actions[-8:]) or ("Нет явно незавершённых действий.",),
        )

    def _open_uri(self, path: str) -> str:
        alias = str(self.vault.get("android_vault_name") or "Friday")
        return "obsidian://open?" + urllib.parse.urlencode({"vault": alias, "file": path})


def _payload(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping) or len(value) > 32:
        raise ValueError("workflow payload must be a bounded object")
    return dict(value)


def _action(data: Mapping[str, object], allowed: frozenset[str]) -> str:
    action = _text(data.get("action"), "action", maximum=100)
    if action not in allowed:
        raise ValueError("unsupported Obsidian workflow action")
    return action


def _keys(data: Mapping[str, object], allowed: set[str], *, required: set[str]) -> None:
    if set(data) - allowed or not required <= set(data):
        raise ValueError("invalid fields for Obsidian workflow action")


def _text(value: object, label: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{label} must be text")
    normalized = value.strip()
    if (not normalized and not allow_empty) or len(normalized) > maximum:
        raise ValueError(f"{label} is empty or too large")
    return normalized


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside its bounds")
    return value


def _markdown_path(value: object) -> str:
    path = _text(value, "path", maximum=2_048)
    pure = PurePosixPath(path)
    if path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("path is unsafe")
    if pure.suffix == "":
        path += ".md"
    elif pure.suffix.casefold() != ".md":
        raise ValueError("path must name a Markdown note")
    return path


def _context_key(value: object) -> str:
    key = _text(value, "context_key", maximum=200)
    if any(character in key for character in "\r\n"):
        raise ValueError("context_key is invalid")
    return key


def _frame_payload(frame: Mapping[str, Any] | None) -> dict[str, Any]:
    if frame is None:
        return {}
    raw = frame.get("frame_json")
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _conflict_frame(value: Mapping[str, Any]) -> dict[str, str]:
    if value.get("kind") != "conflict_preview_v1" or value.get("strategy") != "preserve_both_v1":
        raise ValueError("an exact preserve-both conflict preview is not active")
    result = {
        "conflict_id": _text(value.get("conflict_id"), "conflict_id", maximum=200),
        "canonical_binding_id": _text(
            value.get("canonical_binding_id"),
            "canonical_binding_id",
            maximum=200,
        ),
        "canonical_integration_id": _text(
            value.get("canonical_integration_id"),
            "canonical_integration_id",
            maximum=200,
        ),
        "canonical_path": _markdown_path(value.get("canonical_path")),
        "canonical_revision": validate_revision(str(value.get("canonical_revision") or "")),
        "conflict_path": _markdown_path(value.get("conflict_path")),
        "conflict_revision": validate_revision(str(value.get("conflict_revision") or "")),
        "merged_revision": validate_revision(str(value.get("merged_revision") or "")),
    }
    if ".sync-conflict-" in PurePosixPath(result["canonical_path"]).name.casefold():
        raise ValueError("frozen canonical path is a conflict artifact")
    if ".sync-conflict-" not in PurePosixPath(result["conflict_path"]).name.casefold():
        raise ValueError("frozen conflict path is not an artifact")
    return result


def _used_paths(frame: Mapping[str, Any], path: str) -> list[str]:
    raw = frame.get("used_paths")
    used = [str(item) for item in raw if isinstance(item, str)] if isinstance(raw, list) else []
    used.append(path)
    return list(dict.fromkeys(used))[-100:]


def _looks_like_action(text: str) -> bool:
    first = text.casefold().split(maxsplit=1)[0]
    return first in {
        "добавь",
        "создай",
        "найди",
        "покажи",
        "проверь",
        "перемести",
        "замени",
        "удали",
        "сохрани",
        "продолжай",
        "прими",
        "примени",
        "установи",
        "обнови",
    }


def _assistant_turn_completed(text: str) -> bool:
    folded = text.casefold().replace("ё", "е")
    return not any(
        marker in folded
        for marker in (
            "не удалось",
            "не могу",
            "ошибка",
            "ожидается",
            "pending",
            "не подтвержден",
            "недоступ",
            "не выполн",
        )
    )


__all__ = ["ObsidianWorkflowService", "WorkflowReceipt"]
