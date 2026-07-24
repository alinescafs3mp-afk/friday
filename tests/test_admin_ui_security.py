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
