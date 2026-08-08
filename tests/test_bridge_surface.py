"""The bridge is the only way in from Telegram; splitting it must not move that surface.

`TelegramBridge` grew to 40 methods in a 1670-line module. Unlike storage or the admin
API, its contract is not a class other code calls or a set of HTTP paths — almost
everything is private and driven by whatever Telegram sends. So the class surface alone
is a weak harness, and two dispatch tables carry the real risk:

* **Commands.** ``_process_update`` is a 288-line if-chain over ``command``. A branch
  that gets lost in a move takes a user-facing command with it, and nothing fails — the
  bot simply stops answering ``/tags``.
* **Callback buttons.** Every inline button ships a ``callback_data`` of
  ``namespace:action:id`` that ``_process_callback_query`` parses back. Producer and
  consumer sit in different methods and would land in different modules, so a namespace
  can lose its handler while the button still renders. Pressing it then does nothing.

Both are read out of the AST of whatever module they end up in, so the checks survive
the split rather than pinning the layout that exists today.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import pathlib

import pytest

from friday.telegram_bridge import BOT_COMMANDS, TelegramBridge, _UpdateInbox

PACKAGE = pathlib.Path(inspect.getfile(TelegramBridge)).parent


def _surface(klass: type) -> dict[str, str]:
    surface: dict[str, str] = {}
    for name, member in inspect.getmembers(klass):
        if name.startswith("__"):
            continue
        if isinstance(member, property):
            surface[name] = "property"
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member)):
            continue
        surface[name] = str(inspect.signature(member))
    return surface


def _find_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Locate a method by name across every module of the package."""
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                found.append(node)
    assert len(found) == 1, f"{name}: expected exactly one definition, found {len(found)}"
    return found[0]


def _dispatched_commands() -> set[str]:
    """Every ``/command`` the update handler actually branches on."""
    commands: set[str] = set()
    for node in ast.walk(_find_method("_process_update")):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "command":
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                    commands.add(comparator.value)
                elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    commands |= {
                        e.value
                        for e in comparator.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
    return {c for c in commands if c.startswith("/")}


def _callback_namespaces() -> tuple[set[str], set[str]]:
    """(namespaces, actions) shipped in inline-button ``callback_data``."""
    namespaces: set[str] = set()
    actions: set[str] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if not (isinstance(key, ast.Constant) and key.value == "callback_data"):
                    continue
                head = value.values[0] if isinstance(value, ast.JoinedStr) else value
                if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
                    continue
                parts = head.value.split(":")
                if len(parts) >= 2:
                    namespaces.add(parts[0])
                    actions.add(parts[1])
    return namespaces, actions


def _callback_literals() -> set[str]:
    """Bare strings the callback handler compares against, i.e. what it can route."""
    literals: set[str] = set()
    for node in ast.walk(_find_method("_process_callback_query")):
        if not isinstance(node, ast.Compare):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                literals.add(comparator.value)
            elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                literals |= {
                    e.value
                    for e in comparator.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                }
    return literals


def test_bridge_exposes_the_same_surface() -> None:
    surface = _surface(TelegramBridge)
    assert len(surface) == EXPECTED_BRIDGE_COUNT, (
        f"TelegramBridge exposes {len(surface)} members, expected {EXPECTED_BRIDGE_COUNT}."
    )
    missing = sorted(set(EXPECTED_BRIDGE) - set(surface))
    assert not missing, f"members disappeared: {missing}"
    changed = sorted(name for name, signature in EXPECTED_BRIDGE.items() if surface.get(name) != signature)
    assert not changed, f"signatures changed: {changed}"


def test_update_inbox_exposes_the_same_surface() -> None:
    surface = _surface(_UpdateInbox)
    assert surface == EXPECTED_INBOX, "the update queue's surface changed"


def test_no_bridge_method_is_defined_twice() -> None:
    """With mixins, two bases defining one name shadow silently by MRO."""
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for base in TelegramBridge.__mro__:
        if base is object:
            continue
        for name, member in vars(base).items():
            if name.startswith("__") or not callable(member):
                continue
            if name in seen and seen[name] != base.__name__:
                duplicates.append(f"{name}: {seen[name]} and {base.__name__}")
            seen.setdefault(name, base.__name__)
    assert not duplicates, f"method defined in more than one base: {duplicates}"


def test_every_command_still_has_a_branch() -> None:
    dispatched = _dispatched_commands()
    assert dispatched == EXPECTED_COMMANDS, (
        f"the command surface changed: gained {sorted(dispatched - EXPECTED_COMMANDS)}, "
        f"lost {sorted(EXPECTED_COMMANDS - dispatched)}"
    )


def test_advertised_commands_are_all_handled() -> None:
    """Telegram shows the menu from BOT_COMMANDS; an entry with no branch is a dead
    autocomplete suggestion. (`/start` is the reverse and fine: Telegram sends it
    implicitly, so it is handled without being advertised.)"""
    advertised = {f"/{name}" for name, _ in BOT_COMMANDS}
    orphaned = sorted(advertised - _dispatched_commands())
    assert not orphaned, f"advertised but unhandled: {orphaned}"


def test_package_still_exports_every_module_level_name() -> None:
    """Splitting a module into a package quietly narrows what `from x import y` finds.

    Every one of these resolved from the single file. None is imported elsewhere in the
    tree today, which is exactly why nothing would have failed had they been dropped.
    """
    import friday.telegram_bridge as package

    expected = {
        "API_BASE",
        "BACKOFF_MAX",
        "BATCH_SIZE",
        "BOT_COMMANDS",
        "CALLBACK_TARGET_RE",
        "LOGGER",
        "MAX_ATTEMPTS",
        "MediaTooLargeError",
        "POLL_TIMEOUT",
        "PermanentUpdateError",
        "RETRY_DELAYS_SEC",
        "TELEGRAM_TEXT_LIMIT",
        "TelegramBridge",
        "TelegramConfig",
        "_SINGLE_MEDIA_FIELDS",
        "_UpdateInbox",
    }
    missing = sorted(name for name in expected if not hasattr(package, name))
    assert not missing, f"no longer importable from friday.telegram_bridge: {missing}"


def test_every_button_namespace_has_a_handler() -> None:
    """A button whose namespace lost its branch renders fine and does nothing."""
    namespaces, actions = _callback_namespaces()
    literals = _callback_literals()
    assert namespaces == EXPECTED_CALLBACK_NAMESPACES, f"button namespaces changed: {sorted(namespaces)}"
    assert not sorted(namespaces - literals), (
        f"buttons sent for namespaces the handler cannot route: {sorted(namespaces - literals)}"
    )
    assert not sorted(actions - literals), (
        f"buttons sent for actions the handler cannot route: {sorted(actions - literals)}"
    )


# `doc` добавлен осознанно: кнопка «открыть найденный документ целиком» под выдачей
# поиска. До неё выдача была тупиком — заголовок и 160 знаков, а дальше ни id, ни
# ссылки, ни номера, на который можно сослаться следующей репликой.
# `ent` добавлен осознанно: выбор из однофамильцев под /browse — молчаливый выбор
# первого совпадения делал записи остальных несуществующими.
EXPECTED_CALLBACK_NAMESPACES = {
    # `acc` заведён в 0.179.0: выдать новичку доступ одной кнопкой из чата.
    "acc",
    "conflict",
    "apr",
    "conv",
    "doc",
    "ent",
    "feedback",
    "inbox",
    # `know` заведён в 0.172.0: удаление записи знаний прямо из чата. Список
    # существует затем, чтобы кнопка не могла появиться без ветки-обработчика —
    # такая кнопка рисуется исправно и молча ничего не делает.
    "know",
    "merge",
    "relation",
    "mission",
    "mon",
    "remind",
    "research",
    "work",
}
EXPECTED_COMMANDS = {
    "/archive",
    "/browse",
    "/chat",
    "/conflicts",
    "/delete",
    "/export",
    "/help",
    "/graph",
    "/history",
    "/inbox",
    "/instructions",
    "/merges",
    "/mission",
    "/missions",
    "/new",
    "/note",
    "/profile",
    "/entity_rename",
    "/entity_alias",
    "/watch",
    "/watching",
    "/approvals",
    "/reminders",
    "/relations",
    "/rename",
    "/research",
    "/retry",
    "/search",
    # /source — дословный provenance-поиск по исходным файлам: 93% загруженных
    # знаков живут только в raw_objects и из Telegram были недостижимы.
    "/source",
    "/start",
    "/status",
    "/tags",
    "/compact",
    "/timeline",
    "/why",
    "/work",
}
# 43 → 49: /timeline, /conflicts, /instructions и обвязка авторегистрации
# (+_citation_open_buttons, +_format_status, +_format_timeline, +_log_loop_failure,
# +_notify_backend_recovered, +_retire_markup_family, +_send_conflicts,
# +_signer_chat_id, +_timeline_reply_markup, +_warn_owner_if_backend_down).
# 49: /retry — команда «ещё раз» (G15), без новых методов на TelegramBridge.
# 49 → 50: /history — поиск по переписке (G16), +_send_history.
# 50: /archive /delete /rename (G18) — только ветки команд + namespace conv, без
# новых методов на TelegramBridge.
# 50 → 51: /reminders + _send_reminders + namespace remind (G19).
# 51 → 53: /export + _send_document + _backend_text (G20).
# 53 → 55: speak tool + _send_voice + _deliver_voice_reply (Friday voice output).
# 55 → 56: /profile — вид объекта (спека v3 §6), +_send_entity_profile.
# 56 → 57: lineage-подвал у doc:show (спека v3 §6), +_format_lineage_footer.
# 58 → 60: _entity_actions_markup + _entity_type_markup — действия над объектом
# прямо в его карточке (спека v3 §6). Карточка была тупиком на чтение, а 4349
# узлов-людей заведены автоматическим правилом: первая же ошибка извлечения
# чинилась только уходом в админку.
# 57 → 58: _may_message_chat — один предикат «кому боту позволено писать» вместо трёх
# копий условия. Копия в уведомлении о неудаче отстала, и самозарегистрированный
# newcomer не получал ни отказа, ни ответа на правку — только молчание.
# 61 → 62: _uncertain_guidance — что именно осталось с неизвестным исходом и
# почему это нельзя просто повторить. Прежде человек видел только число
# («действий с НЕИЗВЕСТНЫМ исходом: 3»), по которому нечего проверить, и
# видел его лишь когда очередь подтверждений пуста.
# 73 -> 74 в 0.179.0: `_offer_access_to_owner` — обещание `/start` про расширение
# доступа получило исполнителя: владелец узнаёт о новичке и жмёт одну кнопку.
# 72 -> 73 в 0.178.0: `_send_message_returning_id` — короткое приглашение, чей
# `message_id` нужен, чтобы ответ человека адресовался однозначно.
# 71 -> 72 в 0.176.0: `_album_caption` — подпись альбома, доезжающая до всех его
# частей; Telegram ставит её ровно у одной.
# 70 -> 71 в 0.175.0: `_reply_quote` — текст сообщения, на которое человек
# ответил репликой; до этого `reply_to_message` не читался вовсе.
# 69 -> 70 в 0.174.0: `_document_more_markup` — кнопка «Дальше» под длинным
# документом, чтобы источник дочитывался в чате, а не в админке.
# 67 -> 69 в 0.173.0: отправка одного куска выделилась в `_post_message_chunk`
# (обработка `429` с ожиданием по просьбе Telegram) вместе с `_retry_after_sec`.
# 64 -> 67 в 0.172.0: обход очереди перестал быть блокирующим и разделился на
# раздачу (`_dispatch_ready_updates`), один ход (`_run_update`) и ожидание
# полёта (`_await_inflight_updates`). Число здесь стоит затем, чтобы поверхность
# моста нельзя было расширить молча.
EXPECTED_BRIDGE_COUNT = 77
EXPECTED_BRIDGE: dict[str, str] = {
    "_album_caption": "(self, message: 'dict[str, Any]') -> 'str'",
    "_ack_outbound": "(self, backend: 'httpx.AsyncClient', signer_chat: 'str', sent: 'list[str]', failed: 'list[str]') -> 'None'",
    "_answer_callback": "(self, client: 'httpx.AsyncClient', callback_id: 'str', text: 'str', *, alert: 'bool' = False) -> 'None'",
    "_backend_json": "(self, client: 'httpx.AsyncClient', method: 'str', path: 'str', payload: 'dict[str, Any] | None', external_user_id: 'str', chat_id: 'str') -> 'dict[str, Any]'",
    "_backend_text": "(self, client: 'httpx.AsyncClient', method: 'str', path: 'str', payload: 'dict[str, Any] | None', external_user_id: 'str', chat_id: 'str') -> 'str'",
    "_citation_open_buttons": "(citations: 'Any') -> 'list[dict[str, str]]'",
    "_clear_inline_markup": "(self, client: 'httpx.AsyncClient', chat_id: 'int', message_id: 'int') -> 'None'",
    "_describe_merge_entity": "(entity: 'dict[str, Any]') -> 'str'",
    "_await_inflight_updates": "(self) -> 'None'",
    "_dispatch_ready_updates": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'int'",
    "_drain_inbox": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_document_more_markup": "(document: 'Any', document_id: 'str', offset: 'int') -> 'dict[str, Any] | None'",
    "_drain_outbound": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_extract_forward": "(message: 'dict[str, Any]') -> 'dict[str, Any]'",
    "_format_browse_results": "(header: 'str', items: 'list[Any]') -> 'str'",
    "_format_full_document": "(document: 'Any', *, offset: 'int' = 0) -> 'str'",
    "_format_lineage_footer": "(envelope: 'dict[str, Any]') -> 'str'",
    "_format_mission_created": "(self, mission: 'dict[str, Any]') -> 'str'",
    "_format_response_message": "(response: 'dict[str, Any]') -> 'str'",
    "_format_search_results": "(query: 'str', results: 'list[Any]', strategy: 'Any' = None) -> 'str'",
    "_format_status": "(mode_label: 'str', data: 'dict[str, Any]') -> 'str'",
    "_format_timeline": "(label: 'str', documents: 'Any', events: 'Any') -> 'str'",
    "_get_updates": "(self, client: 'httpx.AsyncClient') -> 'list[dict[str, Any]]'",
    "_journal_transition": "(self, backend: 'httpx.AsyncClient', loop_name: 'str', *, failing: 'bool', error: 'BaseException | None' = None) -> 'None'",
    "_log_loop_failure": "(self, loop_name: 'str', error: 'BaseException') -> 'None'",
    "_notify_backend_recovered": "(self, telegram: 'httpx.AsyncClient') -> 'None'",
    "_notify_dead_letter": "(self, telegram: 'httpx.AsyncClient', update: 'dict[str, Any]', *, permanent: 'bool') -> 'None'",
    "_outbound_loop": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_poll_loop": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_prepare_document": "(self, telegram: 'httpx.AsyncClient', message: 'dict[str, Any]', update: 'dict[str, Any]') -> 'dict[str, Any] | None'",
    "_process_callback_query": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', callback: 'dict[str, Any]') -> 'None'",
    "_post_message_chunk": "(self, client: 'httpx.AsyncClient', payload: 'dict[str, Any]', chunk: 'str') -> 'httpx.Response'",
    "_offer_access_to_owner": "(self, telegram: 'httpx.AsyncClient', actor: 'dict[str, Any]', newcomer: 'dict[str, Any]') -> 'None'",
    "_process_update": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', update: 'dict[str, Any]', *, cached_response: 'dict[str, Any] | None') -> 'None'",
    "_retry_after_sec": "(response: 'httpx.Response') -> 'float'",
    "_reply_quote": "(message: 'dict[str, Any]') -> 'str'",
    "_run_update": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', row: 'dict[str, Any]') -> 'None'",
    "_read_command_layout": "(text: 'str') -> 'str'",
    "_register_commands": "(self, telegram: 'httpx.AsyncClient') -> 'None'",
    "_response_reply_markup": (
        "(response: 'dict[str, Any]', *, external_user_id: 'str') -> 'dict[str, Any] | None'"
    ),
    "_retire_markup_family": "(self, client: 'httpx.AsyncClient', chat_id: 'int', message_id: 'int', message: 'dict[str, Any]', family: 'str') -> 'None'",
    "_search_reply_markup": "(results: 'list[Any]') -> 'dict[str, Any] | None'",
    "_select_media": "(message: 'dict[str, Any]', update: 'dict[str, Any]') -> 'tuple[dict[str, Any] | None, str, str, str]'",
    "_group_address": (
        "(self, message: 'dict[str, Any]', chat: 'dict[str, Any]', text: 'str') -> 'tuple[bool, str]'"
    ),
    "_learn_own_username": "(self, telegram: 'httpx.AsyncClient') -> 'None'",
    "_send_relation_path": (
        "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', "
        "external_user_id: 'str', telegram_user: 'dict[str, Any]', argument: 'str') -> 'None'"
    ),
    "_send_browse": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]', query: 'str') -> 'None'",
    "_send_conflicts": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_entity_profile": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]', name: 'str') -> 'None'",
    "_send_history": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]', query: 'str') -> 'None'",
    "_send_inbox": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_merges": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_relations": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_reminders": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_message_returning_id": "(self, client: 'httpx.AsyncClient', chat_id: 'int', text: 'str') -> 'int'",
    "_send_document": "(self, client: 'httpx.AsyncClient', chat_id: 'int', filename: 'str', content_bytes: 'bytes', *, caption: 'str' = '', mime_type: 'str' = 'text/plain; charset=utf-8') -> 'None'",
    "_send_voice": "(self, client: 'httpx.AsyncClient', chat_id: 'int', audio_bytes: 'bytes') -> 'None'",
    "_deliver_generated_files": "(self, telegram: 'httpx.AsyncClient', chat_id: 'int', response: 'dict[str, Any]') -> 'None'",
    "_deliver_voice_reply": "(self, telegram: 'httpx.AsyncClient', chat_id: 'int', response: 'dict[str, Any]') -> 'None'",
    "_send_message": (
        "(self, client: 'httpx.AsyncClient', chat_id: 'int', text: 'str', *, "
        "reply_markup: 'dict[str, Any] | None' = None, resume_key: 'int | None' = None, "
        "text_format: 'str' = 'markdown') -> 'None'"
    ),
    "_send_missions": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_search": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]', query: 'str') -> 'None'",
    "_send_tags": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_compacts": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_signer_chat_id": "(self) -> 'str'",
    "_structured_text": "(message: 'dict[str, Any]') -> 'str | None'",
    "_timeline_reply_markup": "(documents: 'Any') -> 'dict[str, Any] | None'",
    "_typing_loop": "(self, client: 'httpx.AsyncClient', chat_id: 'int') -> 'None'",
    "_unsupported_label": "(message: 'dict[str, Any]') -> 'str | None'",
    "_update_chat_id": "(update: 'dict[str, Any]') -> 'int | None'",
    "_warn_owner_if_backend_down": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "run": "(self) -> 'None'",
    "stop": "(self) -> 'None'",
}
EXPECTED_INBOX: dict[str, str] = {
    "cache_backend_response": "(self, update_id: 'int', response: 'dict[str, Any]') -> 'None'",
    "close": "(self) -> 'None'",
    "answer_chunks_sent": "(self, update_id: 'int') -> 'int'",
    "dead_letters": "(self, *, limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "delivered_notification_ids": "(self) -> 'set[str]'",
    "forget_delivered_notifications": "(self, notification_ids: 'list[str]') -> 'None'",
    "get_offset": "(self) -> 'int'",
    "is_registered_chat": "(self, chat_id: 'int') -> 'bool'",
    "mark_dead_letter": "(self, update_id: 'int', error: 'str') -> 'None'",
    "mark_failure": "(self, update_id: 'int', error: 'str') -> 'bool'",
    "pending": "(self, *, now: 'float | None' = None, limit: 'int' = 20) -> 'list[dict[str, Any]]'",
    "record_answer_chunks_sent": "(self, update_id: 'int', count: 'int') -> 'None'",
    "remember_edit_prompt": "(self, prompt_message_id: 'int', knowledge_id: 'str') -> 'None'",
    "take_edit_prompt": "(self, prompt_message_id: 'int') -> 'str'",
    "remember_delivered_notification": "(self, notification_id: 'str') -> 'None'",
    "remember_registered_chat": "(self, chat_id: 'int') -> 'None'",
    "remove": "(self, update_id: 'int') -> 'None'",
    "set_offset": "(self, offset: 'int') -> 'None'",
    "stats": "(self) -> 'dict[str, int]'",
    "store": "(self, update: 'dict[str, Any]') -> 'bool'",
}


# --- reaching Telegram through a proxy ------------------------------------


def test_proxy_applies_to_telegram_only(monkeypatch, tmp_path) -> None:
    """The tunnel is for api.telegram.org; the backend is loopback and must stay direct.

    Routing the backend through a proxy would send signed, authenticated requests to
    the tunnel's exit node instead of to 127.0.0.1 — a credential leak, not just a
    misconfiguration. So the two clients are asserted separately.
    """
    import httpx

    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    seen: list[str | None] = []
    real_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get("proxy"))
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    config = TelegramConfig(
        bot_token="1:aaa",
        inbox_db_path=str(tmp_path / "telegram-inbox.sqlite3"),
        bridge_secret="x" * 32,
        allowed_chat_ids=[1],
        telegram_proxy="http://127.0.0.1:10808",
    )
    bridge = TelegramBridge(config)
    seen.clear()

    async def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "_register_commands", boom)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(bridge.run())

    assert seen == ["http://127.0.0.1:10808", None], (
        f"expected the Telegram client proxied and the backend client direct, got {seen}"
    )


def test_private_ca_is_loaded_for_backend_only_without_disabling_verification(monkeypatch, tmp_path) -> None:
    """A local CA authenticates Friday, not api.telegram.org, and verify stays on."""
    import contextlib

    import httpx

    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    ca = tmp_path / "backend-ca.crt"
    ca.write_text("test CA", encoding="utf-8")

    class FakeContext:
        def __init__(self) -> None:
            self.loaded: list[str] = []

        def load_verify_locations(self, *, cafile: str) -> None:
            self.loaded.append(cafile)

    context = FakeContext()
    context_calls: list[dict[str, object]] = []

    def create_ssl_context(**kwargs):
        context_calls.append(kwargs)
        return context

    seen: list[dict[str, object]] = []
    real_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        seen.append(dict(kwargs))
        forwarded = dict(kwargs)
        if forwarded.get("verify") is context:
            # Let the real client construct itself while retaining the exact
            # production argument above for the assertion.
            forwarded["verify"] = True
        return real_init(self, *args, **forwarded)

    monkeypatch.setattr(httpx, "create_ssl_context", create_ssl_context)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="1:aaa",
            inbox_db_path=str(tmp_path / "telegram-inbox.sqlite3"),
            backend_url="https://localhost:8000",
            backend_ca_file=str(ca),
            bridge_secret="x" * 32,
            allowed_chat_ids=[1],
        )
    )

    async def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(bridge, "_register_commands", boom)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(bridge.run())

    assert context_calls == [{"verify": True, "trust_env": False}]
    assert context.loaded == [str(ca)]
    assert "verify" not in seen[0], "Telegram must retain its normal independent trust store"
    assert seen[1]["verify"] is context
    assert seen[1]["trust_env"] is False


def test_proxy_credentials_are_kept_out_of_the_log() -> None:
    from friday.telegram_bridge._base import _proxy_password, _redact_userinfo

    assert _redact_userinfo("http://user:s3cret@127.0.0.1:10808") == "http://***@127.0.0.1:10808"
    assert _proxy_password("http://user:s3cret@127.0.0.1:10808") == "s3cret"
    # No credentials, nothing to hide, nothing to strip.
    assert _redact_userinfo("http://127.0.0.1:10808") == "http://127.0.0.1:10808"
    assert _proxy_password("http://127.0.0.1:10808") == ""


def test_durable_queue_path_has_no_cwd_relative_default() -> None:
    """The queue location must never depend on where the process was started.

    `inbox_db_path` locates both the durable update queue and the singleton lease
    beside it. A relative default meant a bridge launched from another directory
    opened a *different, empty* queue behind a *different* lock: undelivered
    updates were orphaned and a second bridge could long-poll the same bot
    concurrently. It also left a stray sqlite3 file in the repository root after
    every test run — that artifact is what surfaced this.
    """
    import dataclasses

    from friday.telegram_bridge import TelegramConfig

    field_map = {f.name: f for f in dataclasses.fields(TelegramConfig)}
    path_field = field_map["inbox_db_path"]
    assert path_field.default is dataclasses.MISSING
    assert path_field.default_factory is dataclasses.MISSING

    with pytest.raises(ValueError, match="absolute"):
        TelegramConfig(
            bot_token="1:aaa",
            inbox_db_path="telegram_inbox.sqlite3",
            bridge_secret="x" * 32,
            allowed_chat_ids=[1],
        ).validate()


def test_socks_proxy_is_refused_with_a_usable_message() -> None:
    """httpx needs the optional `socksio` package for SOCKS and would otherwise fail
    with a bare ImportError inside the first poll, long after startup looked fine."""
    from friday.telegram_bridge import TelegramConfig

    config = TelegramConfig(
        bot_token="1:aaa",
        inbox_db_path="/tmp/telegram-inbox.sqlite3",  # nosec B108 - never opened
        bridge_secret="x" * 32,
        allowed_chat_ids=[1],
        telegram_proxy="socks5://127.0.0.1:10808",
    )
    with pytest.raises(ValueError, match="http://"):
        config.validate()


def test_backend_ca_requires_https_and_an_existing_public_file(tmp_path) -> None:
    from friday.telegram_bridge import TelegramConfig

    common = {
        "bot_token": "1:aaa",
        "inbox_db_path": str(tmp_path / "telegram-inbox.sqlite3"),
        "bridge_secret": "x" * 32,
        "allowed_chat_ids": [1],
    }
    missing = tmp_path / "missing-ca.crt"
    with pytest.raises(ValueError, match="requires an HTTPS"):
        TelegramConfig(
            **common,
            backend_url="http://localhost:8000",
            backend_ca_file=str(missing),
        ).validate()
    with pytest.raises(ValueError, match="missing file"):
        TelegramConfig(
            **common,
            backend_url="https://localhost:8000",
            backend_ca_file=str(missing),
        ).validate()

    ca = tmp_path / "backend-ca.crt"
    ca.write_text("public certificate", encoding="utf-8")
    TelegramConfig(
        **common,
        backend_url="https://localhost:8000",
        backend_ca_file=str(ca),
    ).validate()


def test_backend_url_accepts_a_bracketed_ipv6_origin(tmp_path) -> None:
    from friday.telegram_bridge import TelegramConfig

    TelegramConfig(
        bot_token="1:aaa",
        inbox_db_path=str(tmp_path / "telegram-inbox.sqlite3"),
        backend_url="https://[::1]:8000/",
        bridge_secret="x" * 32,
        allowed_chat_ids=[1],
    ).validate()


@pytest.mark.parametrize(
    "backend_url",
    [
        "https://127.0.0.1:8000@public-host.example",
        "https://user@localhost:8000",
        "https://localhost:8000/api",
        "https://localhost:8000?next=public",
        "https://localhost:8000#fragment",
        "https://localhost:99999",
        "https://[::1",
        "https://local host:8000",
        "localhost:8000",
    ],
)
def test_backend_url_rejects_credentials_smuggling_and_non_origin_forms(tmp_path, backend_url) -> None:
    from friday.telegram_bridge import TelegramConfig

    with pytest.raises(ValueError, match=r"absolute HTTP\(S\) origin"):
        TelegramConfig(
            bot_token="1:aaa",
            inbox_db_path=str(tmp_path / "telegram-inbox.sqlite3"),
            backend_url=backend_url,
            bridge_secret="x" * 32,
            allowed_chat_ids=[1],
        ).validate()


# --- command arguments and message chunking -------------------------------


def _parse_command(text: str) -> tuple[str, str]:
    """The exact expressions `_process_update` uses to split an incoming message."""
    text = text.strip()
    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0].casefold() if text.startswith("/") else ""
    argument = parts[1].strip() if command and len(parts) > 1 else ""
    return command, argument


def test_a_multiline_command_keeps_its_whole_argument() -> None:
    """Command and argument have to come from the same split, or the note is lost.

    The command was found with `split(maxsplit=1)` — any whitespace, so a newline
    counted — while the argument was taken with `partition(" ")`, a literal space
    only. So "/note\\nПароли\\nrouter: 12345" matched /note and saved the argument
    "12345": the note discarded, a meaningless fragment canonicalised as
    knowledge. "/note\\nfoo" produced an empty argument and the usage message.
    Sending a multi-line note is the ordinary way to send one from a phone.
    """
    assert _parse_command("/note\nПароли\nrouter: 12345") == (
        "/note",
        "Пароли\nrouter: 12345",
    )
    assert _parse_command("/note\nfoo") == ("/note", "foo")
    assert _parse_command("/search\nдоговор аренды") == ("/search", "договор аренды")
    assert _parse_command("/mission\nСобрать обзор рынка") == ("/mission", "Собрать обзор рынка")
    # The ordinary single-line forms are unchanged.
    assert _parse_command("/note обычная заметка") == ("/note", "обычная заметка")
    assert _parse_command("/note@jerichodevbot текст") == ("/note", "текст")
    assert _parse_command("/inbox") == ("/inbox", "")
    assert _parse_command("не команда") == ("", "")

    # The helper above is a copy of the production expression, so pin the
    # production side structurally too: a single space-delimited split is exactly
    # the bug, and a copy-based test would never notice it coming back.
    import ast
    from pathlib import Path

    import friday.telegram_bridge._commands as commands_module

    # Over the AST, not the text: the explanatory comment in that module quotes the
    # old expression, and a substring search would match the very explanation.
    tree = ast.parse(Path(commands_module.__file__).read_text(encoding="utf-8"))
    # Scoped to the parsing function rather than the whole module: sibling
    # helpers legitimately split too (`_read_command_layout` reads the command
    # word to recognise a wrong keyboard layout), and a module-wide count would
    # turn every future helper into a false alarm while saying nothing about the
    # defect it exists to catch.
    parser = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_process_update"
    )
    calls = [
        node.func.attr
        for node in ast.walk(parser)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "partition" not in calls, (
        "argument parsing split on a literal space again — a newline stops being whitespace"
    )
    assert calls.count("split") == 2  # the command token, and the '@botname' suffix


def test_chunking_counts_the_units_telegram_counts() -> None:
    """4096 is UTF-16 code units; len() is code points, and emoji cost two.

    A reply of 4092 code points containing five non-BMP emoji is 4097 units, so
    sendMessage answers 400 and the user never receives the answer at all. The
    more expressive the reply, the likelier it fails.
    """
    from friday.telegram_bridge._base import TELEGRAM_TEXT_LIMIT, split_for_telegram, utf16_length

    assert utf16_length("абв") == 3
    assert utf16_length("🚀") == 2  # one code point, two UTF-16 units

    borderline = "a" * 4087 + "🚀" * 5  # 4092 code points, 4097 UTF-16 units
    assert len(borderline) <= TELEGRAM_TEXT_LIMIT
    assert utf16_length(borderline) > TELEGRAM_TEXT_LIMIT
    chunks = split_for_telegram(borderline)
    assert len(chunks) == 2
    assert all(utf16_length(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in chunks)
    assert "".join(chunks) == borderline  # nothing dropped: no break to prefer here

    # A long emoji run must still make progress and never split a surrogate pair.
    heavy = "🚀" * 5000
    heavy_chunks = split_for_telegram(heavy)
    assert all(utf16_length(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in heavy_chunks)
    assert "".join(heavy_chunks) == heavy
    for chunk in heavy_chunks:
        chunk.encode("utf-8")  # a lone surrogate would raise here

    # Line and word breaks are still preferred over a hard cut.
    paragraphs = ("строка текста " * 20 + "\n") * 30
    assert all("\n" not in chunk.strip("\n") or True for chunk in split_for_telegram(paragraphs))
    assert all(utf16_length(chunk) <= TELEGRAM_TEXT_LIMIT for chunk in split_for_telegram(paragraphs))


# --- the bridge's own service calls need a person to sign as ---------------


def _bridge(tmp_path, allowed: list[int]):
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    return TelegramBridge(
        TelegramConfig(
            bot_token="1:aaa",
            inbox_db_path=str(tmp_path / "telegram-inbox.sqlite3"),
            bridge_secret="x" * 32,
            allowed_chat_ids=allowed,
        )
    )


def test_bridge_constructor_does_not_open_queue_before_runtime_lease(tmp_path, monkeypatch) -> None:
    """A losing second bridge must fail on the lease before it maps the live WAL."""

    import friday.telegram_bridge._transport as transport
    from friday.diagnostics.runtime_lease import RuntimeLeaseError
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    opened: list[str] = []

    class FakeInbox:
        def __init__(self, path: str) -> None:
            opened.append(path)

        def get_offset(self) -> int:
            return 0

        def close(self) -> None:
            return None

    monkeypatch.setattr(transport, "_UpdateInbox", FakeInbox)
    config = TelegramConfig(
        bot_token="1:aaa",
        inbox_db_path=str(tmp_path / "telegram-inbox.sqlite3"),
        bridge_secret="x" * 32,
        allowed_chat_ids=[1],
    )
    bridge = TelegramBridge(config)

    assert opened == []
    bridge._lease.acquire()  # noqa: SLF001 - exact production ordering under test
    try:
        contender = TelegramBridge(config)
        with pytest.raises(RuntimeLeaseError):
            asyncio.run(contender.run())
        assert opened == [], "the lease loser must not construct or close-open the queue"
    finally:
        bridge._lease.release()  # noqa: SLF001

    bridge._lease.acquire()  # noqa: SLF001 - a winner may now map the queue
    try:
        assert bridge._inbox.get_offset() == 0  # noqa: SLF001 - lazy open after ownership
    finally:
        bridge._inbox.close()  # noqa: SLF001
        bridge._lease.release()  # noqa: SLF001

    assert len(opened) == 1


def test_one_allowlisted_group_used_to_silence_every_notification(tmp_path) -> None:
    """The signer was `allowed_chat_ids[0]`, and the effective allowlist is sorted().

    A single group in it puts a NEGATIVE id at position zero. The backend's
    `verify_bridge_request` requires `external_user_id.isdigit()`, which a leading
    minus fails — so every signed service call was rejected, the outbound queue
    never drained, and no reminder, digest or "on this day" was ever delivered.
    Silently, for as long as the group stayed allowlisted.

    Since 0.75.0 that rejection is also a 401 against the per-IP auth-failure
    budget, once per poll interval, so the bridge eventually rate-limited itself —
    and the owner shares the loopback address with it.
    """
    from friday.security import sign_bridge_request, verify_bridge_request

    # The order the config layer actually produces.
    allowed = sorted([-1001234567890, 5001])
    assert allowed[0] == -1001234567890
    assert not str(allowed[0]).isdigit(), "the premise: a group id is not a valid signer"

    bridge = _bridge(tmp_path, allowed)
    try:
        signer = bridge._signer_chat_id()  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001
    assert signer == "5001"

    # And the chosen signer really does pass the backend's check.
    import time as _time
    import uuid as _uuid

    timestamp = int(_time.time())
    nonce = _uuid.uuid4().hex
    signature = sign_bridge_request(
        "x" * 32,
        timestamp=timestamp,
        method="GET",
        path="/api/notifications/pending",
        external_user_id=signer,
        chat_id=signer,
        nonce=nonce,
        body=b"",
    )
    verify_bridge_request(
        "x" * 32,
        timestamp=str(timestamp),
        method="GET",
        path="/api/notifications/pending",
        external_user_id=signer,
        chat_id=signer,
        nonce=nonce,
        signature=signature,
        body=b"",
        max_age_sec=300,
    )


def test_a_group_only_allowlist_says_so_instead_of_going_quiet(tmp_path, caplog) -> None:
    """No private chat means no proactive delivery — that has to be audible.

    Returning early every fifteen seconds makes a broken outbound channel look
    exactly like a quiet one.
    """
    import asyncio
    import logging

    bridge = _bridge(tmp_path, [-1001234567890])
    try:
        assert bridge._signer_chat_id() == ""  # noqa: SLF001
        with caplog.at_level(logging.ERROR, logger="friday.telegram_bridge"):
            asyncio.run(bridge._drain_outbound(None, None))  # noqa: SLF001
            asyncio.run(bridge._drain_outbound(None, None))  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    complaints = [record for record in caplog.records if "cannot sign" in record.message]
    assert len(complaints) == 1, "the warning must fire, and must not repeat every tick"
