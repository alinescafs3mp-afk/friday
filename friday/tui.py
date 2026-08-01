"""Pseudographic launcher for Friday (``jericho tui``).

A small curses front-end to configure the LLM/embeddings endpoint and token
(written to ``.env.local``), start the backend, and read status — so pointing
Friday at a model on the LAN does not require hand-editing env files.

The state machine (``LauncherState`` + :func:`handle_key`) and the ``.env.local``
editing are pure and unit-tested; the curses layer is a thin renderer/driver.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Ordered form fields: (env var, human label, is_secret).
LLM_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("FRIDAY_LLM_BASE_URL", "LLM base URL", False),
    ("FRIDAY_LLM_MODEL", "LLM model", False),
    ("FRIDAY_LLM_ENABLED", "LLM enabled (1/0)", False),
    ("FRIDAY_LLM_API_KEY", "LLM API token", True),
    ("FRIDAY_EMBEDDINGS_BASE_URL", "Embeddings base URL", False),
    ("FRIDAY_EMBEDDINGS_API_KEY", "Embeddings token", True),
)

MENU_ITEMS: tuple[str, ...] = (
    "Настроить LLM-эндпоинт и токен",
    "Запустить backend (up)",
    "Статус / диагностика",
    "Выход",
)


class Command(Enum):
    """Something the curses driver must act on outside the pure state machine."""

    NONE = "none"
    QUIT = "quit"
    START_BACKEND = "start_backend"
    SHOW_STATUS = "show_status"
    SAVED = "saved"


# --- pure .env.local editing ---------------------------------------------


def parse_env(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, ignoring blanks and ``#`` comments."""

    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def upsert_env_var(text: str, key: str, value: str) -> str:
    """Set ``key=value``, updating the first active line in place or appending.

    Comments, blank lines and unrelated variables are preserved verbatim. Only an
    active (uncommented) ``key=`` line is replaced; a commented example is left
    alone and a fresh active line is appended.
    """

    prefix = f"{key}="
    new_line = f"{key}={value}"
    trailing_newline = text.endswith("\n") or text == ""
    lines = text.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        if not replaced and line.strip().startswith(prefix):
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    result = "\n".join(out)
    if trailing_newline and result and not result.endswith("\n"):
        result += "\n"
    return result


def apply_values(text: str, values: dict[str, str]) -> str:
    """Upsert non-empty field values into the env text (fields applied in order).

    Empty values are NOT written: a var left blank stays absent so its documented
    fallback still fires (e.g. ``FRIDAY_EMBEDDINGS_API_KEY`` → the LLM key). Writing
    ``KEY=`` would set it to an empty string and silently break that fallback.
    """

    for key, _label, _secret in LLM_FIELDS:
        value = values.get(key, "")
        if value:
            text = upsert_env_var(text, key, value)
    return text


def mask_secret(value: str) -> str:
    """Render a token for display without revealing it."""

    if not value:
        return "(не задан)"
    if len(value) <= 6:
        return "•" * len(value)
    return f"{value[:3]}…{value[-2:]}"


def env_path(env_file: str | None = None) -> Path:
    """Resolve the target ``.env.local`` (explicit → FRIDAY_ENV_FILE → cwd)."""

    candidate = env_file or os.environ.get("FRIDAY_ENV_FILE") or str(Path.cwd() / ".env.local")
    return Path(candidate).expanduser()


def initial_values(env: dict[str, str]) -> dict[str, str]:
    """Seed the form from a parsed env mapping (missing → empty string)."""

    return {key: env.get(key, "") for key, _label, _secret in LLM_FIELDS}


# --- pure state machine ---------------------------------------------------


@dataclass
class LauncherState:
    screen: str = "main"  # "main" | "config" | "status"
    menu_index: int = 0
    field_index: int = 0
    editing: bool = False
    edit_buffer: str = ""
    values: dict[str, str] = field(default_factory=dict)
    message: str = ""
    status_text: str = ""
    dirty: bool = False

    def current_field_key(self) -> str:
        return LLM_FIELDS[self.field_index][0]


# Key codes we handle without importing curses (curses.KEY_* equal these ncurses
# values; kept explicit so the state machine is importable and testable headless).
KEY_UP = 259
KEY_DOWN = 258
KEY_ENTER = 10  # newline / Enter
KEY_ESC = 27
KEY_BACKSPACE = 263
_BACKSPACE_ALIASES = {KEY_BACKSPACE, 127, 8}


def handle_key(state: LauncherState, key: int) -> Command:
    """Advance the pure state machine for one key. Returns a driver Command."""

    if state.screen == "status":
        state.screen = "main"  # any key dismisses the status screen
        return Command.NONE
    if state.screen == "main":
        return _handle_main(state, key)
    return _handle_config(state, key)


def _handle_main(state: LauncherState, key: int) -> Command:
    if key in (KEY_UP, ord("k")):
        state.menu_index = (state.menu_index - 1) % len(MENU_ITEMS)
    elif key in (KEY_DOWN, ord("j")):
        state.menu_index = (state.menu_index + 1) % len(MENU_ITEMS)
    elif key in (KEY_ENTER, ord(" ")):
        return _activate_menu(state)
    elif key in (ord("q"), KEY_ESC):
        return Command.QUIT
    return Command.NONE


def _activate_menu(state: LauncherState) -> Command:
    choice = MENU_ITEMS[state.menu_index]
    if choice.startswith("Настроить"):
        state.screen = "config"
        state.field_index = 0
        state.editing = False
        state.message = ""
        return Command.NONE
    if choice.startswith("Запустить"):
        return Command.START_BACKEND
    if choice.startswith("Статус"):
        return Command.SHOW_STATUS
    return Command.QUIT


def _handle_config(state: LauncherState, key: int) -> Command:
    if state.editing:
        return _handle_edit(state, key)
    if key in (KEY_UP, ord("k")):
        state.field_index = (state.field_index - 1) % len(LLM_FIELDS)
    elif key in (KEY_DOWN, ord("j")):
        state.field_index = (state.field_index + 1) % len(LLM_FIELDS)
    elif key == KEY_ENTER:
        _, _label, is_secret = LLM_FIELDS[state.field_index]
        state.editing = True
        # A secret field starts empty (never re-displays the stored token); a plain
        # field pre-fills its current value so it can be edited in place.
        state.edit_buffer = "" if is_secret else state.values.get(state.current_field_key(), "")
        state.message = (
            "Новый токен (Enter — заменить, пустой Enter — оставить прежний, Esc — отмена)."
            if is_secret
            else ""
        )
    elif key == ord("s"):
        return Command.SAVED
    elif key in (ord("q"), KEY_ESC):
        state.screen = "main"
        state.message = "Есть несохранённые изменения (вернитесь и нажмите s)." if state.dirty else ""
    return Command.NONE


def _handle_edit(state: LauncherState, key: int) -> Command:
    _, _label, is_secret = LLM_FIELDS[state.field_index]
    if key == KEY_ENTER:
        # Empty Enter on a secret keeps the previous token (no accidental clearing).
        if state.edit_buffer or not is_secret:
            state.values[state.current_field_key()] = state.edit_buffer
            state.dirty = True
            state.message = "Изменено (не сохранено). Нажмите s, чтобы записать .env.local."
        else:
            state.message = "Токен не изменён."
        state.editing = False
    elif key == KEY_ESC:
        state.editing = False  # discard the in-progress edit
    elif key in _BACKSPACE_ALIASES:
        state.edit_buffer = state.edit_buffer[:-1]
    elif 32 <= key < 127:  # printable ASCII only — reject curses KEY_* codes and UTF-8 bytes
        state.edit_buffer += chr(key)
    return Command.NONE


# --- curses driver (thin; imports curses lazily) --------------------------


def _gather_status(settings, values: dict[str, str]) -> str:
    from dataclasses import replace

    from friday.diagnostics import collect_diagnostics

    # Probe the endpoint the user is currently editing (even before saving), so the
    # status answers "is the model I just typed reachable?" rather than the value
    # loaded at launch.
    effective = replace(
        settings,
        llm_base_url=(values.get("FRIDAY_LLM_BASE_URL") or settings.llm_base_url).rstrip("/"),
        llm_model=values.get("FRIDAY_LLM_MODEL") or settings.llm_model,
        llm_api_key=values.get("FRIDAY_LLM_API_KEY", settings.llm_api_key),
    )
    try:
        report = collect_diagnostics(effective, check_llm_port=True)
    except Exception as exc:  # noqa: BLE001 - status must never crash the launcher
        return f"Диагностика недоступна: {exc}"
    llm = report.get("llm_endpoint") or {}
    served = ", ".join(llm.get("served_models") or []) or "—"
    lines = [
        f"LLM endpoint : {effective.llm_base_url}",
        f"  auth       : {'да' if effective.llm_api_key else 'нет'}",
        f"  reachable  : {llm.get('reachable')}",
        f"  model      : {effective.llm_model}  (обслуживается: {llm.get('model_served')})",
        f"  served     : {served}",
    ]
    if llm.get("models_error"):
        lines.append(f"  error      : {llm['models_error']}")
    database = report.get("database") or {}
    lines.append(f"DB           : schema {database.get('schema_version')}  ({database.get('state')})")
    lines.append(f"Итог OK      : {report.get('ok')}")
    return "\n".join(lines)


def _save(state: LauncherState, target: Path) -> None:
    from friday.cli import _write_private_text_atomic

    base = target.read_text(encoding="utf-8") if target.is_file() else ""
    try:
        _write_private_text_atomic(target, apply_values(base, state.values))
    except OSError as exc:
        state.message = f"Ошибка записи {target}: {exc}"
        return
    state.dirty = False
    state.message = f"Сохранено в {target} (chmod 600). Перезапустите backend, чтобы применить."


def _safe(win, y: int, x: int, text: str, attr: int = 0) -> None:
    import curses

    height, width = win.getmaxyx()
    if 0 <= y < height and 0 <= x < width - 1:
        with contextlib.suppress(curses.error):
            win.addnstr(y, x, text, max(0, width - x - 1), attr)


def _render(stdscr, state: LauncherState) -> None:
    import curses

    stdscr.erase()
    height, width = stdscr.getmaxyx()
    with contextlib.suppress(curses.error):
        stdscr.border()
    _safe(stdscr, 0, 2, " Friday — лаунчер ", curses.A_BOLD)
    if state.screen == "main":
        _render_main(stdscr, state)
    elif state.screen == "config":
        _render_config(stdscr, state)
    else:
        _render_status(stdscr, state)
    if state.message:
        _safe(stdscr, height - 2, 2, state.message, curses.A_DIM)
    stdscr.noutrefresh()
    curses.doupdate()


def _render_main(stdscr, state: LauncherState) -> None:
    import curses

    _safe(stdscr, 2, 3, "Выберите действие (↑/↓, Enter):")
    for index, item in enumerate(MENU_ITEMS):
        attr = curses.A_REVERSE if index == state.menu_index else 0
        _safe(stdscr, 4 + index, 5, f" {item} ", attr)
    if state.dirty:
        # "Запустить backend" auto-saves, but make the pending state visible.
        _safe(stdscr, 5 + len(MENU_ITEMS), 3, "● есть несохранённые изменения", curses.A_BOLD)


def _render_config(stdscr, state: LauncherState) -> None:
    import curses

    _safe(stdscr, 2, 3, "LLM / embeddings эндпоинт (↑/↓, Enter — изменить, s — сохранить, q — назад):")
    for index, (_key, label, secret) in enumerate(LLM_FIELDS):
        key = LLM_FIELDS[index][0]
        if state.editing and index == state.field_index:
            shown = state.edit_buffer + "▏"
        else:
            raw = state.values.get(key, "")
            shown = mask_secret(raw) if secret else (raw or "(пусто)")
        attr = curses.A_REVERSE if index == state.field_index else 0
        _safe(stdscr, 4 + index, 5, f"{label:<24}: {shown}", attr)


def _render_status(stdscr, state: LauncherState) -> None:
    _safe(stdscr, 2, 3, "Статус (любая клавиша — назад):")
    for index, line in enumerate(state.status_text.splitlines()):
        _safe(stdscr, 4 + index, 5, line)


def _loop(stdscr, state: LauncherState, settings, target: Path) -> Command:
    import curses

    with contextlib.suppress(curses.error):
        curses.curs_set(0)  # some terminals cannot hide the cursor — not fatal
    stdscr.keypad(True)
    while True:
        _render(stdscr, state)
        key = stdscr.getch()
        if key == curses.KEY_RESIZE:
            continue
        command = handle_key(state, key)
        if command == Command.QUIT:
            return command
        if command == Command.START_BACKEND:
            # Never launch the backend on stale config: flush pending edits first.
            if state.dirty:
                _save(state, target)
            return command
        if command == Command.SHOW_STATUS:
            state.status_text = _gather_status(settings, state.values)
            state.screen = "status"
        elif command == Command.SAVED:
            _save(state, target)


def _exec_up(env_file: str | None) -> None:
    import os
    import sys

    argv = [sys.executable, "-m", "friday.cli"]
    if env_file:
        argv += ["--env-file", env_file]
    argv.append("up")
    os.execvp(sys.executable, argv)  # replaces this process; the terminal is restored


def run(args) -> int:
    """CLI entry for ``jericho tui`` (argparse handler)."""

    import curses

    from friday.config import load_settings

    env_file = getattr(args, "env_file", None)
    target = env_path(env_file)
    settings = load_settings()
    env_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    state = LauncherState(values=initial_values(parse_env(env_text)))
    if not target.is_file():
        state.message = f"{target} не найден — сохранение создаст его (лучше сперва `jericho init`)."

    try:
        result = curses.wrapper(_loop, state, settings, target)
    except KeyboardInterrupt:
        return 0
    if result == Command.START_BACKEND:
        _exec_up(env_file)  # does not return on success
    return 0
