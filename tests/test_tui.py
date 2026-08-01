"""Launcher (``jericho tui``): pure ``.env.local`` editing, the key-driven state
machine, secret masking, save/status behaviour, and render-safety.

The curses layer itself needs a real terminal, so it is exercised through a fake
window + a stubbed ``curses`` module — enough to catch render bugs (bad attribute
names, out-of-range indexing) without a TTY.
"""

from __future__ import annotations

import stat
import sys
import types

import pytest

from friday import tui
from friday.tui import (
    LLM_FIELDS,
    Command,
    LauncherState,
    apply_values,
    handle_key,
    initial_values,
    mask_secret,
    parse_env,
    upsert_env_var,
)

KEY_UP = tui.KEY_UP
KEY_DOWN = tui.KEY_DOWN
KEY_ENTER = tui.KEY_ENTER
KEY_ESC = tui.KEY_ESC
KEY_BACKSPACE = tui.KEY_BACKSPACE


# --- pure .env.local editing ----------------------------------------------


def test_parse_env_ignores_comments_and_blanks():
    env = "# a comment\n\nFRIDAY_LLM_MODEL=dispatcher\n  FRIDAY_LLM_ENABLED=1  \nnot-a-pair\n"
    assert parse_env(env) == {"FRIDAY_LLM_MODEL": "dispatcher", "FRIDAY_LLM_ENABLED": "1"}


def test_upsert_updates_active_line_and_preserves_the_rest():
    env = "# comment\nFRIDAY_LLM_BASE_URL=http://old:8001/v1\nOTHER=x\n"
    out = upsert_env_var(env, "FRIDAY_LLM_BASE_URL", "http://192.168.1.5:8001/v1")
    assert "FRIDAY_LLM_BASE_URL=http://192.168.1.5:8001/v1" in out
    assert "http://old:8001/v1" not in out
    assert "# comment" in out and "OTHER=x" in out
    assert out.endswith("\n")


def test_upsert_appends_when_missing_and_never_clobbers_a_comment():
    # A commented example must be left alone; a fresh active line is appended.
    out = upsert_env_var("# FRIDAY_LLM_API_KEY=example\n", "FRIDAY_LLM_API_KEY", "sk-1")
    assert "# FRIDAY_LLM_API_KEY=example" in out
    assert "FRIDAY_LLM_API_KEY=sk-1" in out
    assert out.count("FRIDAY_LLM_API_KEY") == 2


def test_upsert_does_not_match_a_longer_key_prefix():
    out = upsert_env_var("FRIDAY_LLM_API_KEY_EXTRA=keep\n", "FRIDAY_LLM_API_KEY", "sk-1")
    assert "FRIDAY_LLM_API_KEY_EXTRA=keep" in out
    assert "FRIDAY_LLM_API_KEY=sk-1" in out


def test_apply_values_upserts_every_field():
    values = {key: f"v-{key}" for key, _, _ in LLM_FIELDS}
    out = apply_values("", values)
    for key in values:
        assert f"{key}=v-{key}" in out


def test_apply_values_skips_empty_to_preserve_fallback():
    # An empty value must NOT be written as `KEY=` (that would break the documented
    # "absent → fallback" behaviour, e.g. embeddings token defaulting to the LLM key).
    values = {key: "" for key, _, _ in LLM_FIELDS}
    values["FRIDAY_LLM_BASE_URL"] = "http://lan:8001/v1"
    out = apply_values("", values)
    assert "FRIDAY_LLM_BASE_URL=http://lan:8001/v1" in out
    assert "FRIDAY_EMBEDDINGS_API_KEY=" not in out  # empty → not written
    assert "FRIDAY_LLM_API_KEY=" not in out


def test_mask_secret():
    assert mask_secret("") == "(не задан)"
    assert mask_secret("abc") == "•••"
    assert mask_secret("sk-lan-secret-123") == "sk-…23"


# --- state machine --------------------------------------------------------


def test_menu_navigation_wraps_and_quits():
    state = LauncherState()
    assert handle_key(state, KEY_UP) is Command.NONE
    assert state.menu_index == len(tui.MENU_ITEMS) - 1  # wrapped to the bottom
    assert handle_key(state, KEY_DOWN) is Command.NONE
    assert state.menu_index == 0
    state.menu_index = len(tui.MENU_ITEMS) - 1  # "Выход"
    assert handle_key(state, KEY_ENTER) is Command.QUIT


def test_menu_start_and_status_commands():
    start = LauncherState(menu_index=1)  # "Запустить backend"
    assert handle_key(start, KEY_ENTER) is Command.START_BACKEND
    status = LauncherState(menu_index=2)  # "Статус"
    assert handle_key(status, KEY_ENTER) is Command.SHOW_STATUS


def test_enter_config_edit_field_commit_then_save():
    state = LauncherState(values=initial_values({}))
    handle_key(state, KEY_ENTER)  # activate "Настроить…"
    assert state.screen == "config"
    handle_key(state, KEY_ENTER)  # begin editing field 0 (base url), prefilled empty
    assert state.editing is True
    for char in "http://lan:9/v1":
        handle_key(state, ord(char))
    handle_key(state, KEY_ENTER)  # commit
    assert state.editing is False and state.dirty is True
    assert state.values[LLM_FIELDS[0][0]] == "http://lan:9/v1"
    assert handle_key(state, ord("s")) is Command.SAVED


def test_edit_prefills_current_value_and_escape_discards():
    state = LauncherState(screen="config", values={LLM_FIELDS[0][0]: "keep-me"})
    handle_key(state, KEY_ENTER)  # edit field 0
    assert state.edit_buffer == "keep-me"  # prefilled
    handle_key(state, ord("X"))
    handle_key(state, KEY_ESC)  # discard
    assert state.editing is False
    assert state.values[LLM_FIELDS[0][0]] == "keep-me"  # unchanged


def test_backspace_edits_the_buffer():
    state = LauncherState(screen="config", editing=True, field_index=0, edit_buffer="abc")
    handle_key(state, KEY_BACKSPACE)
    assert state.edit_buffer == "ab"


def test_edit_rejects_special_keys_and_high_bytes():
    # curses KEY_* codes (>=256) and raw UTF-8 bytes (128-255) must not land in the
    # buffer as chr() garbage; only printable ASCII is inserted.
    state = LauncherState(screen="config", editing=True, field_index=0, edit_buffer="")
    for bad in (KEY_UP, KEY_DOWN, 330, 261, 200, 0xC3, 0x110000):
        handle_key(state, bad)
    assert state.edit_buffer == ""
    for good in b"http://x:9":
        handle_key(state, good)
    assert state.edit_buffer == "http://x:9"


def test_secret_field_edit_starts_empty_and_empty_enter_keeps():
    # LLM_FIELDS[3] is the secret API token.
    secret_index = next(i for i, (_k, _l, sec) in enumerate(LLM_FIELDS) if sec)
    key = LLM_FIELDS[secret_index][0]
    state = LauncherState(screen="config", field_index=secret_index, values={key: "sk-old-token"})
    handle_key(state, KEY_ENTER)  # begin editing the secret
    assert state.edit_buffer == ""  # never re-displays the stored token
    handle_key(state, KEY_ENTER)  # empty Enter → keep previous
    assert state.values[key] == "sk-old-token"
    assert state.editing is False
    # Typing a new token replaces it.
    handle_key(state, KEY_ENTER)
    for char in "sk-new":
        handle_key(state, ord(char))
    handle_key(state, KEY_ENTER)
    assert state.values[key] == "sk-new"


def test_status_screen_any_key_returns_to_main():
    state = LauncherState(screen="status", status_text="x")
    assert handle_key(state, ord("z")) is Command.NONE
    assert state.screen == "main"


def test_config_quit_returns_to_main():
    state = LauncherState(screen="config")
    handle_key(state, ord("q"))
    assert state.screen == "main"


# --- save + status --------------------------------------------------------


def test_save_writes_private_env_file(tmp_path):
    target = tmp_path / ".env.local"
    target.write_text("# header\nFRIDAY_LLM_MODEL=old\nOTHER=keep\n", encoding="utf-8")
    state = LauncherState(values={"FRIDAY_LLM_MODEL": "dispatcher", "FRIDAY_LLM_API_KEY": "sk-lan-1"})
    tui._save(state, target)
    text = target.read_text(encoding="utf-8")
    assert "FRIDAY_LLM_MODEL=dispatcher" in text and "old" not in text
    assert "FRIDAY_LLM_API_KEY=sk-lan-1" in text
    assert "OTHER=keep" in text and "# header" in text
    assert state.dirty is False
    assert stat.S_IMODE(target.stat().st_mode) == 0o600  # private


def test_gather_status_reads_llm_endpoint_key_and_probes_edited_values(settings, monkeypatch):
    captured = {}

    def _fake(effective, **kwargs):
        captured["base_url"] = effective.llm_base_url
        captured["api_key"] = effective.llm_api_key
        return {
            "llm_endpoint": {"reachable": True, "model_served": True, "served_models": ["dispatcher"]},
            "database": {"schema_version": 15, "state": "ready"},
            "ok": True,
        }

    monkeypatch.setattr("friday.diagnostics.collect_diagnostics", _fake)
    values = {"FRIDAY_LLM_BASE_URL": "http://192.168.1.7:8001/v1", "FRIDAY_LLM_API_KEY": "sk-x"}
    out = tui._gather_status(settings, values)
    # It reads the real "llm_endpoint" key (not "llm") and probes the edited endpoint.
    assert captured["base_url"] == "http://192.168.1.7:8001/v1"
    assert captured["api_key"] == "sk-x"
    assert "reachable  : True" in out
    assert "dispatcher" in out
    assert "schema 15" in out


def test_gather_status_never_crashes_on_error(settings, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("diag exploded")

    monkeypatch.setattr("friday.diagnostics.collect_diagnostics", _boom)
    out = tui._gather_status(settings, {})
    assert "Диагностика недоступна" in out


# --- render safety (fake window + stubbed curses) -------------------------


class _FakeWin:
    def __init__(self, height: int = 24, width: int = 80) -> None:
        self._height, self._width = height, width
        self.writes: list[str] = []

    def getmaxyx(self):
        return (self._height, self._width)

    def erase(self):
        pass

    def border(self):
        pass

    def addnstr(self, y, x, text, n, attr=0):
        self.writes.append(text)

    def noutrefresh(self):
        pass


@pytest.mark.parametrize("width", [80, 20, 6])
def test_render_every_screen_without_crash(monkeypatch, width):
    fake_curses = types.SimpleNamespace(
        A_BOLD=1, A_REVERSE=2, A_DIM=4, error=Exception, doupdate=lambda: None
    )
    monkeypatch.setitem(sys.modules, "curses", fake_curses)
    win = _FakeWin(height=24, width=width)
    values = {key: ("sk-secret" if secret else "val") for key, _, secret in LLM_FIELDS}

    tui._render(win, LauncherState(screen="main", values=values, message="hi"))
    tui._render(win, LauncherState(screen="config", field_index=3, values=values))
    tui._render(win, LauncherState(screen="config", editing=True, field_index=3, edit_buffer="sk-typing"))
    tui._render(win, LauncherState(screen="status", status_text="line1\nline2\nline3"))
    # A secret value is masked in the config render, never shown raw.
    joined = " ".join(win.writes)
    assert "sk-secret" not in joined
