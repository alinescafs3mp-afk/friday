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

from jericho.telegram_bridge import BOT_COMMANDS, TelegramBridge, _UpdateInbox

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
    import jericho.telegram_bridge as package

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
    assert not missing, f"no longer importable from jericho.telegram_bridge: {missing}"


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


EXPECTED_CALLBACK_NAMESPACES = {"feedback", "inbox", "merge", "mission", "research", "work"}
EXPECTED_COMMANDS = {
    "/browse",
    "/chat",
    "/help",
    "/inbox",
    "/merges",
    "/mission",
    "/missions",
    "/new",
    "/note",
    "/research",
    "/search",
    "/start",
    "/status",
    "/tags",
    "/work",
}
EXPECTED_BRIDGE_COUNT = 34
EXPECTED_BRIDGE: dict[str, str] = {
    "_answer_callback": "(self, client: 'httpx.AsyncClient', callback_id: 'str', text: 'str', *, alert: 'bool' = False) -> 'None'",
    "_backend_json": "(self, client: 'httpx.AsyncClient', method: 'str', path: 'str', payload: 'dict[str, Any] | None', external_user_id: 'str', chat_id: 'str') -> 'dict[str, Any]'",
    "_clear_inline_markup": "(self, client: 'httpx.AsyncClient', chat_id: 'int', message_id: 'int') -> 'None'",
    "_describe_merge_entity": "(entity: 'dict[str, Any]') -> 'str'",
    "_drain_inbox": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_drain_outbound": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_extract_forward": "(message: 'dict[str, Any]') -> 'dict[str, Any]'",
    "_format_browse_results": "(header: 'str', items: 'list[Any]') -> 'str'",
    "_format_mission_created": "(self, mission: 'dict[str, Any]') -> 'str'",
    "_format_response_message": "(response: 'dict[str, Any]') -> 'str'",
    "_format_search_results": "(query: 'str', results: 'list[Any]') -> 'str'",
    "_get_updates": "(self, client: 'httpx.AsyncClient') -> 'list[dict[str, Any]]'",
    "_notify_dead_letter": "(self, telegram: 'httpx.AsyncClient', update: 'dict[str, Any]', *, permanent: 'bool') -> 'None'",
    "_outbound_loop": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_poll_loop": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient') -> 'None'",
    "_prepare_document": "(self, telegram: 'httpx.AsyncClient', message: 'dict[str, Any]', update: 'dict[str, Any]') -> 'dict[str, Any] | None'",
    "_process_callback_query": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', callback: 'dict[str, Any]') -> 'None'",
    "_process_update": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', update: 'dict[str, Any]', *, cached_response: 'dict[str, Any] | None') -> 'None'",
    "_register_commands": "(self, telegram: 'httpx.AsyncClient') -> 'None'",
    "_response_reply_markup": "(response: 'dict[str, Any]') -> 'dict[str, Any] | None'",
    "_select_media": "(message: 'dict[str, Any]', update: 'dict[str, Any]') -> 'tuple[dict[str, Any] | None, str, str, str]'",
    "_send_browse": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]', query: 'str') -> 'None'",
    "_send_inbox": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_merges": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_message": "(self, client: 'httpx.AsyncClient', chat_id: 'int', text: 'str', *, reply_markup: 'dict[str, Any] | None' = None) -> 'None'",
    "_send_missions": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_send_search": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]', query: 'str') -> 'None'",
    "_send_tags": "(self, telegram: 'httpx.AsyncClient', backend: 'httpx.AsyncClient', chat_id: 'int', external_user_id: 'str', telegram_user: 'dict[str, Any]') -> 'None'",
    "_structured_text": "(message: 'dict[str, Any]') -> 'str | None'",
    "_typing_loop": "(self, client: 'httpx.AsyncClient', chat_id: 'int') -> 'None'",
    "_unsupported_label": "(message: 'dict[str, Any]') -> 'str | None'",
    "_update_chat_id": "(update: 'dict[str, Any]') -> 'int | None'",
    "run": "(self) -> 'None'",
    "stop": "(self) -> 'None'",
}
EXPECTED_INBOX: dict[str, str] = {
    "cache_backend_response": "(self, update_id: 'int', response: 'dict[str, Any]') -> 'None'",
    "close": "(self) -> 'None'",
    "dead_letters": "(self, *, limit: 'int' = 100) -> 'list[dict[str, Any]]'",
    "get_offset": "(self) -> 'int'",
    "mark_dead_letter": "(self, update_id: 'int', error: 'str') -> 'None'",
    "mark_failure": "(self, update_id: 'int', error: 'str') -> 'bool'",
    "pending": "(self, *, now: 'float | None' = None) -> 'list[dict[str, Any]]'",
    "remove": "(self, update_id: 'int') -> 'None'",
    "set_offset": "(self, offset: 'int') -> 'None'",
    "stats": "(self) -> 'dict[str, int]'",
    "store": "(self, update: 'dict[str, Any]') -> 'bool'",
}


# --- reaching Telegram through a proxy ------------------------------------


def test_proxy_applies_to_telegram_only(monkeypatch) -> None:
    """The tunnel is for api.telegram.org; the backend is loopback and must stay direct.

    Routing the backend through a proxy would send signed, authenticated requests to
    the tunnel's exit node instead of to 127.0.0.1 — a credential leak, not just a
    misconfiguration. So the two clients are asserted separately.
    """
    import httpx

    from jericho.telegram_bridge import TelegramBridge, TelegramConfig

    seen: list[str | None] = []
    real_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get("proxy"))
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    config = TelegramConfig(
        bot_token="1:aaa",
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


def test_proxy_credentials_are_kept_out_of_the_log() -> None:
    from jericho.telegram_bridge._base import _proxy_password, _redact_userinfo

    assert _redact_userinfo("http://user:s3cret@127.0.0.1:10808") == "http://***@127.0.0.1:10808"
    assert _proxy_password("http://user:s3cret@127.0.0.1:10808") == "s3cret"
    # No credentials, nothing to hide, nothing to strip.
    assert _redact_userinfo("http://127.0.0.1:10808") == "http://127.0.0.1:10808"
    assert _proxy_password("http://127.0.0.1:10808") == ""


def test_socks_proxy_is_refused_with_a_usable_message() -> None:
    """httpx needs the optional `socksio` package for SOCKS and would otherwise fail
    with a bare ImportError inside the first poll, long after startup looked fine."""
    from jericho.telegram_bridge import TelegramConfig

    config = TelegramConfig(
        bot_token="1:aaa",
        bridge_secret="x" * 32,
        allowed_chat_ids=[1],
        telegram_proxy="socks5://127.0.0.1:10808",
    )
    with pytest.raises(ValueError, match="http://"):
        config.validate()
