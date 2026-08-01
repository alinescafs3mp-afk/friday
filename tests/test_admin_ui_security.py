from __future__ import annotations

import re

from friday.admin_ui import STATIC_DIR


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


def test_every_bulk_action_batches_below_the_server_cap() -> None:
    """One unbatched action is a button that does nothing at all.

    Every bulk route refuses more than 200 ids, and it refuses BEFORE writing: the
    request fails whole. Only Inbox batched, so «выбрать все» on Ревизия, Качество or
    Граф applied nothing and showed the server's English refusal in a toast — after
    the user had already confirmed a soft delete in the worst of them.

    Pinned as a property of ALL of them rather than of one, because the previous
    version of this test guarded `bulkInbox` alone while four siblings shipped broken.
    """
    from pathlib import Path

    source = Path(__file__).parent.parent / "friday" / "admin_ui" / "static" / "app.js"
    text = source.read_text(encoding="utf-8")

    assert "const BULK_BATCH=200" in text
    assert "for(let i=0;i<ids.length;i+=BULK_BATCH)" in text
    assert "ids.slice(i,i+BULK_BATCH)" in text

    senders = (
        "actions.bulkInbox",
        "actions.dismissGroup",
        "actions.bulkReviewRelations",
        "actions.bulkReviewConflicts",
        "actions.applyLifecycle",
        "actions.applyCleanup",
    )
    for name in senders:
        body = text[text.index(f"{name}=") : text.index(f"{name}=") + 1200]
        assert "bulkApply(" in body, f"{name} sends its ids without batching"

    # No caller may hand the whole selection to a route again.
    for whole in ("inbox_ids:ids", "candidate_ids:ids", "conflict_ids:ids", "knowledge_ids:ids"):
        assert whole not in text, f"{whole} posts the entire selection in one body"


def test_a_failure_midway_reports_progress_and_re_reads() -> None:
    """Batching introduces a state the single request never had: partly applied.

    The batches before a failure are committed on the server. Discarding the counts
    and leaving the old rows on screen invites the user to run the same action over
    material that is already gone.
    """
    from pathlib import Path

    source = Path(__file__).parent.parent / "friday" / "admin_ui" / "static" / "app.js"
    text = source.read_text(encoding="utf-8")
    body = text[text.index("async function bulkApply") : text.index("async function bulkApply") + 900]

    assert "catch(e){error=e;break}" in body, "a failed batch must stop the loop, not be swallowed"
    assert "await refresh()" in body, "the list is not re-read after a partial application"
    assert "Применено ${ok} из ${ids.length}" in body, "partial progress is not reported"
