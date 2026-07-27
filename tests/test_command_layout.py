"""A command typed without switching the layout is still that command.

`/inbox` on a Russian layout comes out as «.штищч» — even the slash changes,
because that key writes a full stop there. So it does not look like a command at
all: the bot answers it as an ordinary message, which for `/inbox` or `/new` is a
confusing non-answer rather than a small mistake.
"""

from __future__ import annotations

import pytest

from jericho.telegram_bridge import TelegramBridge

read = TelegramBridge._read_command_layout


@pytest.mark.parametrize(
    ("typed", "meant"),
    [
        (".штищч", "/inbox"),
        (".ыефегы", "/status"),
        (".рудз", "/help"),
        (".туц", "/new"),
    ],
)
def test_a_command_in_the_wrong_layout_is_recognised(typed, meant):
    assert read(typed) == meant


def test_a_multiline_argument_survives_the_re_reading():
    """The defect `_process_update` documents, in this helper's own words: a note
    sent from a phone is multi-line, and rebuilding it around a single space
    silently discards everything after the first newline."""
    assert read(".тщеу\nПароли\nrouter: 12345") == "/note\nПароли\nrouter: 12345"


def test_the_argument_is_left_exactly_as_typed():
    """Routing a message is one thing; rewriting its content is another.

    `/note` writes its argument into the knowledge base, so a wrong guess would
    be stored as the user's own words. If the argument was mistyped too, the
    search path repairs it at query time and a note shows the user their own
    text to resend.
    """
    assert read(".тщеу пароли роутера") == "/note пароли роутера"
    assert read(".ыуфкср график дежурств") == "/search график дежурств"


@pytest.mark.parametrize(
    "text",
    ["...ну ладно", "просто сообщение", ".тые", "", "точка в начале. и текст", "/inbox"],
)
def test_ordinary_messages_are_untouched(text):
    """A message legitimately starting with a full stop flips to a slash too —
    the re-reading is accepted only when it names a command that exists."""
    assert read(text) == text
