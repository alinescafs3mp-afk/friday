from __future__ import annotations

import re

from jericho.admin_ui import STATIC_DIR


def test_dynamic_handler_payloads_use_json_argument_encoding():
    """Delegated data-call payloads must JSON-encode every argument.

    The admin UI renders untrusted values (titles, ids) into innerHTML
    templates. Inline handlers are gone entirely (strict CSP), so the
    injection surface moved to the ``data-call``/``data-change`` attributes:
    they are safe only while built exclusively by the ``call()``/``chg()``
    helpers, which JSON-encode the payload and HTML-escape it as a whole.
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'const call=(fn,...args)=>`data-call="${esc(JSON.stringify([fn,...args]))}"`;' in js
    assert 'const chg=(fn,...args)=>`data-change="${esc(JSON.stringify([fn,...args]))}"`;' in js

    # No template may hand-build the attribute around raw interpolations.
    handmade = [
        m
        for m in re.findall(r'data-(?:call|change)="[^"\n]*"', js)
        if not m.startswith(('data-call="${esc(JSON', 'data-change="${esc(JSON'))
    ]
    assert handmade == []

    # The dispatcher resolves only registered actions, never arbitrary globals.
    assert "if(typeof actions[fn]==='function')actions[fn](...args)" in js


def test_bulk_inbox_batches_below_the_server_cap() -> None:
    """ "Выбрать все" on a real backlog made the whole action fail with a raw 400.

    The list is fetched with `limit=500` and the bulk route refuses more than 200
    ids per call, so selecting everything produced one oversized request, nothing
    was reviewed, and the toast showed the server's error text. The UI now sends
    batches and accumulates, so a 500-item queue is one click.
    """
    from pathlib import Path

    source = Path(__file__).parent.parent / "jericho" / "admin_ui" / "static" / "app.js"
    text = source.read_text(encoding="utf-8")

    assert "const BULK_INBOX_BATCH=200" in text
    body = text[text.index("actions.bulkInbox=") : text.index("actions.bulkInbox=") + 900]
    assert "for(let i=0;i<ids.length;i+=BULK_INBOX_BATCH)" in body
    assert "ids.slice(i,i+BULK_INBOX_BATCH)" in body
    # The whole selection must never be posted in one body again.
    assert "inbox_ids:ids" not in body
