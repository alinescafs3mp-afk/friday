"""Offline controls for the owner-only existing-Friday-bot route."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest

from tools import release_1_0_telegram_roundtrip as telegram

ROOT = Path(__file__).resolve().parents[1]
OWNER_ID = 710_000_001
BOT_ID = 720_000_001
TOKEN = "720000001:synthetic-owner-only-existing-bot-token"
_RUNTIME: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _installed_runtime(telegram_offline_runtime):
    with pytest.MonkeyPatch.context() as patch:
        for name, value in telegram_offline_runtime.items():
            patch.setitem(_RUNTIME, name, value)
        yield


def _private_write(path: Path, value: object | bytes) -> str:
    raw = value if isinstance(value, bytes) else telegram._json_bytes(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


def _policy_access(
    tmp_path: Path,
    *,
    authority_owner: int | str = OWNER_ID,
    authority_token: str = TOKEN,
    legacy_authority: bool = False,
    extra_authority_allowed: int | None = None,
    startup_commands: dict | None = None,
):
    tmp_path.chmod(0o700)
    candidate_root = tmp_path / "candidate"
    isolation = tmp_path / "run"
    candidate_root.mkdir(mode=0o700)
    isolation.mkdir(mode=0o700)
    authority = tmp_path / "established-friday.env"
    authority_prefix = "JERICHO" if legacy_authority else "FRIDAY"
    authority_text = (
        f"{authority_prefix}_TELEGRAM_ALLOWED_CHAT_IDS={authority_owner}\n"
        f"{authority_prefix}_TELEGRAM_OWNER_CHAT_IDS={authority_owner}\n"
        f"{authority_prefix}_TELEGRAM_BOT_TOKEN={authority_token}\n"
    )
    if extra_authority_allowed is not None:
        authority_text += f"FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS={extra_authority_allowed}\n"
    authority_sha = _private_write(authority, authority_text.encode())
    env_file = tmp_path / "isolated.env"
    env_sha = _private_write(
        env_file,
        (
            f"FRIDAY_TELEGRAM_BOT_TOKEN={TOKEN}\n"
            f"FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS={OWNER_ID}\n"
            f"FRIDAY_TELEGRAM_OWNER_CHAT_IDS={OWNER_ID}\n"
        ).encode(),
    )
    commands = startup_commands or {"commands": [{"command": "help", "description": "Help"}]}
    commands_path = tmp_path / "startup-commands.json"
    commands_sha = _private_write(commands_path, commands)
    manifest = tmp_path / "candidate-manifest.json"
    manifest_sha = _private_write(manifest, {})
    base_scope = tmp_path / "base-effect-scope.json"
    base_scope_sha = _private_write(
        base_scope,
        {"schema": "friday.astra-telegram-effect-scope.v1", "GO": False},
    )
    owner_directive = tmp_path / "owner-directive.json"
    owner_directive_sha = _private_write(
        owner_directive,
        {
            "schema": "friday.owner-telegram-recipient-restriction.v1",
            "recipient_scope": {"only_owner_private_chat": True},
            "GO": False,
        },
    )
    admission_path = tmp_path / "effect-admission.json"
    admission_sha = _private_write(
        admission_path,
        {
            "schema": telegram.EFFECT_ADMISSION_SCHEMA,
            "route_id": "existing-friday-bot-sole-consumer-handoff-installed-final",
            "candidate_sha": "1" * 40,
            "candidate_tree": "2" * 40,
            "wheel_sha256": _RUNTIME["WHEEL_SHA"],
            "harness_manifest_sha256": "b" * 64,
            "base_effect_scope": {"path": str(base_scope), "sha256": base_scope_sha},
            "owner_directive": {
                "path": str(owner_directive),
                "sha256": owner_directive_sha,
            },
            "existing_friday_bot_one_canary": True,
            "same_bot_production_consumer_stopped": True,
            "isolated_non_production_home": True,
            "installed_wheel_origin": True,
            "owner_private_recipient_only": True,
            "startup_effect": {
                "method": "setMyCommands",
                "max_attempts": 1,
                "target": "existing_friday_bot",
                "canonical_payload_digest_required": True,
                "restoration_claimed": False,
            },
            "frozen_limits": {
                "duration_s": 600,
                "genuine_inbound": 1,
                "bot_posts": 2,
                "total_attempts": 433,
                "method_caps": telegram.EFFECT_METHOD_CAPS,
            },
            "unknown_effect_policy": "latch_and_never_retry",
            "nonowner_ingress_policy": "fail_before_bridge_observation_no_reply_no_drain",
            "GO": False,
        },
    )
    access_path = tmp_path / "access.json"
    access_sha = "a" * 64
    candidate = telegram.CandidatePolicy(
        manifest_path=manifest,
        manifest_sha256=manifest_sha,
        source_root=candidate_root,
        candidate_sha="1" * 40,
        candidate_tree="2" * 40,
        suite_revision="r10-astra-harness-084",
        python_executable=_RUNTIME["INSTALLED"] / "bin/python",
        python_sha256=telegram.sha256_file(_RUNTIME["INSTALLED"] / "bin/python"),
        installed_wheel=telegram.InstalledWheelPolicy(
            wheel_path=_RUNTIME["WHEEL"],
            wheel_sha256=_RUNTIME["WHEEL_SHA"],
            site_root=_RUNTIME["SITE"],
            distribution_version=_RUNTIME["VERSION"],
        ),
    )
    policy = telegram.Policy(
        attempt_id="existing_owner_attempt_0001",
        canary="canary:existing_owner_attempt_0001",
        candidate=candidate,
        harness=telegram.HarnessPolicy(
            manifest_path=tmp_path / "harness-manifest.json",
            manifest_sha256="b" * 64,
            source_root=ROOT,
        ),
        contour=telegram.ContourPolicy(
            contour_id="existing-owner-contour",
            isolation_root=isolation,
            friday_home=isolation / "home",
            data_dir=isolation / "home/data",
            state_dir=isolation / "home/state",
            cache_dir=isolation / "home/cache",
            log_dir=isolation / "home/log",
            env_file=env_file,
            env_file_sha256=env_sha,
            database_path=isolation / "home/data/friday.sqlite3",
            inbox_db_path=isolation / "home/state/telegram-inbox.sqlite3",
            files_dir=isolation / "home/data/files",
            backend_origin="http://127.0.0.1:18991",
            known_production_homes=(tmp_path / "production",),
            foreign_lease_paths=(),
        ),
        access=telegram.AccessPolicy(access_path, access_sha),
        observer=telegram.ObserverPolicy(
            mode="owner_manual_readback",
            result_path=tmp_path / "observer.json",
            adapter_argv=(),
            adapter_executable_sha256="",
        ),
        budgets=telegram.Budgets(
            timeout_s=600,
            backend_ready_timeout_s=30,
            poll_interval_s=0.1,
            max_getupdates_rounds=20,
            max_inbound_user_messages=1,
            max_outbound_bot_posts=2,
            max_evidence_bytes=1048576,
            process_log_bytes=65536,
            cleanup_grace_s=5,
        ),
        effect_admission=telegram.EffectAdmissionPolicy(
            manifest_path=admission_path,
            manifest_sha256=admission_sha,
        ),
    )
    _, binding = telegram._resolved_owner_private_chat(
        policy, authority_path=authority, authority_sha256=authority_sha
    )
    now = dt.datetime.now(dt.UTC)
    access_document = {
        "schema": telegram.EXISTING_BOT_ACCESS_SCHEMA,
        "contour_id": policy.contour.contour_id,
        "attempt_id": policy.attempt_id,
        "candidate_sha": policy.candidate.candidate_sha,
        "candidate_manifest_sha256": policy.candidate.manifest_sha256,
        "existing_friday_bot_one_canary": True,
        "same_bot_production_consumer_stopped": True,
        "isolated_non_production_home": True,
        "installed_wheel_origin": True,
        "clean_backlog_expected": True,
        "bot_user_id": BOT_ID,
        "observer_mode": policy.observer.mode,
        "issued_at": now.isoformat(),
        "expires_at": (now + dt.timedelta(minutes=30)).isoformat(),
        "nonce": "owner-only-nonce-0001",
        "owner_identity_source": "pinned_existing_friday_configuration",
        "owner_authority_path": str(authority),
        "owner_authority_sha256": authority_sha,
        "owner_private_chat_hmac_sha256": binding,
        "startup_commands_path": str(commands_path),
        "startup_commands_sha256": commands_sha,
    }
    access = telegram.AccessManifest.from_document(access_document, policy=policy, now=now)
    return policy, access, access_document, commands


def _ledger(tmp_path: Path):
    policy, access, document, commands = _policy_access(tmp_path)
    evidence_parent = tmp_path / "evidence-parent"
    evidence_parent.mkdir(mode=0o700)
    evidence = telegram.EvidenceStore(evidence_parent / "evidence", 1048576)
    spec_path, _driver = telegram.prepare_effect_ledger(
        evidence,
        policy=policy,
        access=access,
        token=TOKEN,
        deadline_monotonic_ns=time.monotonic_ns() + 600_000_000_000,
    )
    return policy, access, document, commands, spec_path


def test_existing_access_uses_separate_authority_and_emits_no_raw_owner_keys(tmp_path):
    _policy, access, document, _commands = _policy_access(tmp_path)
    assert access.route == "existing_friday_bot_one_canary"
    assert access.chat_id == access.user_id == OWNER_ID
    assert "chat_id" not in document and "user_id" not in document
    assert access.owner_private_chat_hmac_sha256 == document["owner_private_chat_hmac_sha256"]


def test_self_asserted_owner_allowlist_is_refused(tmp_path):
    with pytest.raises(ValueError, match="authoritative owner allowlist"):
        _policy_access(tmp_path, authority_owner=OWNER_ID + 1)


@pytest.mark.parametrize(
    "authority_owner",
    ["", f"{OWNER_ID},{OWNER_ID + 1}", "-1", "not-an-id"],
)
def test_missing_multiple_negative_or_malformed_owner_authority_is_refused(
    tmp_path,
    authority_owner,
):
    with pytest.raises(ValueError):
        _policy_access(tmp_path, authority_owner=authority_owner)


def test_established_legacy_authority_aliases_bind_the_same_owner_and_bot(tmp_path):
    _policy, access, _document, _commands = _policy_access(tmp_path, legacy_authority=True)
    assert access.chat_id == access.user_id == OWNER_ID


def test_unrelated_production_allowlist_entry_is_not_admitted_to_the_contour(tmp_path):
    _policy, access, _document, _commands = _policy_access(
        tmp_path,
        legacy_authority=True,
        extra_authority_allowed=OWNER_ID + 99,
    )
    assert access.chat_id == access.user_id == OWNER_ID


def test_substitute_bot_credential_is_refused_before_any_effect(tmp_path):
    with pytest.raises(ValueError, match="bot credential"):
        _policy_access(tmp_path, authority_token=f"{BOT_ID}:not-the-established-credential")


def test_effect_admission_drift_is_refused_before_ledger_or_http(tmp_path):
    policy, access, _document, _commands = _policy_access(tmp_path)
    policy.effect_admission.manifest_path.write_text("{}\n", encoding="utf-8")
    policy.effect_admission.manifest_path.chmod(0o600)
    evidence_parent = tmp_path / "evidence-parent"
    evidence_parent.mkdir(mode=0o700)
    evidence = telegram.EvidenceStore(evidence_parent / "evidence", 1048576)
    with pytest.raises(telegram.RoundtripError, match="effect_admission_digest_mismatch"):
        telegram.prepare_effect_ledger(
            evidence,
            policy=policy,
            access=access,
            token=TOKEN,
            deadline_monotonic_ns=time.monotonic_ns() + 600_000_000_000,
        )


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        ("sendMessage", {"chat_id": OWNER_ID + 1, "text": "no"}),
        ("sendMessage", {"chat_id": str(OWNER_ID), "text": "no"}),
        ("sendPhoto", {"chat_id": OWNER_ID + 1, "photo": "x"}),
        ("sendAudio", {"chat_id": OWNER_ID, "audio": "x"}),
        ("sendDocument", {"chat_id": OWNER_ID, "document": "x"}),
        ("sendVideo", {"chat_id": OWNER_ID, "video": "x"}),
        ("sendAnimation", {"chat_id": OWNER_ID, "animation": "x"}),
        ("sendVoice", {"chat_id": OWNER_ID, "voice": "x"}),
        ("sendVideoNote", {"chat_id": OWNER_ID, "video_note": "x"}),
        ("sendMediaGroup", {"chat_id": OWNER_ID, "media": []}),
        ("sendLocation", {"chat_id": OWNER_ID, "latitude": 0, "longitude": 0}),
        ("sendVenue", {"chat_id": OWNER_ID, "latitude": 0, "longitude": 0}),
        ("sendContact", {"chat_id": OWNER_ID, "phone_number": "x"}),
        ("sendPoll", {"chat_id": OWNER_ID, "question": "x"}),
        ("sendDice", {"chat_id": OWNER_ID}),
        ("sendSticker", {"chat_id": OWNER_ID, "sticker": "x"}),
        ("sendInvoice", {"chat_id": OWNER_ID, "title": "x"}),
        ("sendGame", {"chat_id": OWNER_ID, "game_short_name": "x"}),
        ("sendPaidMedia", {"chat_id": OWNER_ID, "star_count": 1}),
        ("forwardMessage", {"chat_id": OWNER_ID + 1, "from_chat_id": OWNER_ID}),
        ("forwardMessages", {"chat_id": OWNER_ID, "from_chat_id": OWNER_ID}),
        ("copyMessage", {"chat_id": OWNER_ID + 1, "from_chat_id": OWNER_ID}),
        ("copyMessages", {"chat_id": OWNER_ID, "from_chat_id": OWNER_ID}),
        ("editMessageCaption", {"chat_id": OWNER_ID, "message_id": 1}),
        ("editMessageMedia", {"chat_id": OWNER_ID, "message_id": 1}),
        ("editMessageLiveLocation", {"chat_id": OWNER_ID, "message_id": 1}),
        ("stopMessageLiveLocation", {"chat_id": OWNER_ID, "message_id": 1}),
        ("editMessageReplyMarkup", {"chat_id": OWNER_ID, "message_id": 1}),
        ("setMessageReaction", {"chat_id": OWNER_ID + 1, "message_id": 1}),
        ("answerCallbackQuery", {"callback_query_id": "x"}),
        ("answerInlineQuery", {"inline_query_id": "x"}),
        ("answerWebAppQuery", {"web_app_query_id": "x"}),
        ("deleteMessage", {"chat_id": OWNER_ID, "message_id": 1}),
        ("deleteMessages", {"chat_id": OWNER_ID, "message_ids": [1]}),
        ("futureOpaqueEffect", {"chat_id": OWNER_ID}),
        ("sendChatAction", {"chat_id": OWNER_ID + 1, "action": "typing"}),
        ("sendChatAction", {"chat_id": OWNER_ID, "action": "upload_photo"}),
        (
            "editMessageText",
            {
                "chat_id": OWNER_ID,
                "inline_message_id": "foreign-inline-target",
                "message_id": 1,
                "text": "no",
                "disable_web_page_preview": True,
            },
        ),
    ],
)
def test_other_recipients_media_forward_copy_reaction_and_actions_stop_before_http(tmp_path, method, payload):
    policy, _access, _document, _commands, spec_path = _ledger(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": True}, request=request)

    _guard, original = telegram.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(telegram.TelegramEffectStop):
                await client.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=payload)

    try:
        asyncio.run(scenario())
    finally:
        httpx.AsyncClient.send = original
    assert calls == 0


def test_declared_setmycommands_is_one_exact_semantic_write(tmp_path):
    policy, _access, _document, commands, spec_path = _ledger(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": True}, request=request)

    _guard, original = telegram.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            response = await client.post(f"https://api.telegram.org/bot{TOKEN}/setMyCommands", json=commands)
            assert response.status_code == 200
            with pytest.raises(telegram.TelegramEffectStop):
                await client.post(f"https://api.telegram.org/bot{TOKEN}/setMyCommands", json=commands)

    try:
        asyncio.run(scenario())
    finally:
        httpx.AsyncClient.send = original
    assert calls == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"commands": []},
        {"commands": [{"command": "help", "description": "Changed"}]},
        {
            "commands": [{"command": "help", "description": "Help"}],
            "scope": {"type": "chat", "chat_id": OWNER_ID},
        },
        {
            "commands": [{"command": "help", "description": "Help"}],
            "language_code": "ru",
        },
        {
            "commands": [{"command": "help", "description": "Help"}],
            "unexpected": True,
        },
    ],
)
def test_setmycommands_mutation_or_scope_is_refused_before_http(tmp_path, mutation):
    policy, _access, _document, _commands, spec_path = _ledger(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": True}, request=request)

    _guard, original = telegram.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(telegram.TelegramEffectStop):
                await client.post(f"https://api.telegram.org/bot{TOKEN}/setMyCommands", json=mutation)

    try:
        asyncio.run(scenario())
    finally:
        httpx.AsyncClient.send = original
    assert calls == 0


@pytest.mark.parametrize(
    "variant",
    [
        "nonowner",
        "group",
        "topic",
        "direct_topic",
        "forum",
        "sender_chat",
        "bot_sender",
        "edited_carrier",
        "callback_carrier",
        "unknown_carrier",
    ],
)
def test_nonowner_group_topic_and_bot_ingress_latch_after_one_bounded_read(tmp_path, variant):
    policy, access, _document, _commands, spec_path = _ledger(tmp_path)
    inbound = telegram.build_observer_request(
        policy, access, {"bot_user_id": BOT_ID, "bot_username": "friday"}
    )["inbound_text"]
    message = {
        "message_id": 31,
        "from": {"id": OWNER_ID, "is_bot": False},
        "chat": {"id": OWNER_ID, "type": "private"},
        "text": inbound,
    }
    if variant == "nonowner":
        message["from"]["id"] = OWNER_ID + 1
    elif variant == "group":
        message["chat"]["type"] = "group"
    elif variant == "topic":
        message["message_thread_id"] = 4
    elif variant == "direct_topic":
        message["direct_messages_topic_id"] = 4
    elif variant == "forum":
        message["chat"]["is_forum"] = True
    elif variant == "sender_chat":
        message["sender_chat"] = {"id": OWNER_ID}
    elif variant == "bot_sender":
        message["from"]["is_bot"] = True
    update = {"update_id": 21, "message": message}
    if variant == "edited_carrier":
        update = {"update_id": 21, "edited_message": message}
    elif variant == "callback_carrier":
        update = {"update_id": 21, "callback_query": {"id": "x", "message": message}}
    elif variant == "unknown_carrier":
        update = {"update_id": 21, "channel_post": message}
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"ok": True, "result": [update]},
            request=request,
        )

    guard, original = telegram.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)
    poll = {"offset": 0, "timeout": 30, "allowed_updates": telegram.GETUPDATES_ALLOWED_UPDATES}

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(telegram.TelegramEffectStop):
                await client.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates", json=poll)
            with pytest.raises(telegram.TelegramEffectStop):
                await client.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    json={"chat_id": OWNER_ID, "text": "must not reply"},
                )

    try:
        asyncio.run(scenario())
    finally:
        httpx.AsyncClient.send = original
    assert calls == 1
    assert guard.latched == "telegram_effect_update_unclassified"


def test_owner_plus_foreign_update_batch_is_withheld_as_a_whole(tmp_path):
    policy, access, _document, _commands, spec_path = _ledger(tmp_path)
    inbound = telegram.build_observer_request(
        policy, access, {"bot_user_id": BOT_ID, "bot_username": "friday"}
    )["inbound_text"]
    messages = [
        {
            "update_id": 31,
            "message": {
                "message_id": 41,
                "from": {"id": OWNER_ID, "is_bot": False},
                "chat": {"id": OWNER_ID, "type": "private"},
                "text": inbound,
            },
        },
        {
            "update_id": 32,
            "message": {
                "message_id": 42,
                "from": {"id": OWNER_ID + 1, "is_bot": False},
                "chat": {"id": OWNER_ID + 1, "type": "private"},
                "text": "foreign",
            },
        },
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": messages}, request=request)

    guard, original = telegram.install_bridge_effect_guard(
        httpx,
        spec_path,
        policy.contour.env_file,
    )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(telegram.TelegramEffectStop):
                await client.post(
                    f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                    json={
                        "offset": 0,
                        "timeout": 30,
                        "allowed_updates": telegram.GETUPDATES_ALLOWED_UPDATES,
                    },
                )

    try:
        asyncio.run(scenario())
    finally:
        httpx.AsyncClient.send = original
    assert calls == 1
    assert guard.latched == "telegram_effect_updates_shape_invalid"


def test_exact_private_canary_advances_only_to_update_id_plus_one(tmp_path):
    policy, access, _document, _commands, spec_path = _ledger(tmp_path)
    inbound = telegram.build_observer_request(
        policy, access, {"bot_user_id": BOT_ID, "bot_username": "friday"}
    )["inbound_text"]
    results = [
        [
            {
                "update_id": 44,
                "message": {
                    "message_id": 33,
                    "from": {"id": OWNER_ID, "is_bot": False},
                    "chat": {"id": OWNER_ID, "type": "private"},
                    "text": inbound,
                },
            }
        ],
        [],
    ]
    calls = 0

    def handler(request):
        nonlocal calls
        result = results[calls]
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": result}, request=request)

    _guard, original = telegram.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            await client.post(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                json={
                    "offset": 0,
                    "timeout": 30,
                    "allowed_updates": telegram.GETUPDATES_ALLOWED_UPDATES,
                },
            )
            await client.post(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                json={
                    "offset": 45,
                    "timeout": 30,
                    "allowed_updates": telegram.GETUPDATES_ALLOWED_UPDATES,
                },
            )

    try:
        asyncio.run(scenario())
    finally:
        httpx.AsyncClient.send = original
    assert calls == 2


@pytest.mark.parametrize(
    "poll",
    [
        {"offset": 1, "timeout": 30, "allowed_updates": telegram.GETUPDATES_ALLOWED_UPDATES},
        {"offset": 0, "timeout": 0, "allowed_updates": telegram.GETUPDATES_ALLOWED_UPDATES},
        {"offset": 0, "timeout": 30, "allowed_updates": ["message"]},
        {"offset": 0, "timeout": 30},
    ],
)
def test_poll_skip_or_widen_is_refused_before_http(tmp_path, poll):
    policy, _access, _document, _commands, spec_path = _ledger(tmp_path)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True, "result": []}, request=request)

    _guard, original = telegram.install_bridge_effect_guard(httpx, spec_path, policy.contour.env_file)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
            with pytest.raises(telegram.TelegramEffectStop):
                await client.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates", json=poll)

    try:
        asyncio.run(scenario())
    finally:
        httpx.AsyncClient.send = original
    assert calls == 0


def test_frozen_limits_and_matrix_membership_are_unchanged():
    assert telegram.EFFECT_DURATION_LIMIT_S == 600
    assert telegram.EFFECT_TOTAL_ATTEMPT_CAP == 433
    assert telegram.EFFECT_METHOD_CAPS["bridge"]["sendMessage"] == 2
    assert sum(sum(role.values()) for role in telegram.EFFECT_METHOD_CAPS.values()) == 433
    matrix = json.loads((ROOT / "tools/release_1_0_capability_matrix.json").read_text())
    case = next(item for item in matrix["cases"] if item["id"] == "R10-LIVE-TELEGRAM-ROUNDTRIP")
    gap = next(item for item in matrix["gaps"] if item["id"] == "GAP-TELEGRAM-LIVE-ROUNDTRIP")
    assert case["capability_id"] == gap["capability_id"] == "CAP-TELEGRAM-LIVE"
    assert case["node_ids"] == [
        "tests/test_release_1_0_telegram_receipts.py::test_synthetic_complete_reader_path_is_mechanics_only"
    ]
    assert case["timeout_s"] == 600
    assert "dedicated test bot" in case["scenario"]
    assert "owner-admitted existing Friday bot" in case["scenario"]
    assert gap["blocks_1_0"] is True


def test_installed_bootstrap_has_no_source_injection_and_records_origin(tmp_path):
    assert "sys.path.insert" not in telegram._BOOTSTRAP
    dummy = tmp_path / "guard-spec.json"
    _private_write(dummy, {})
    env_file = tmp_path / "isolated.env"
    _private_write(env_file, b"EMPTY=1\n")
    receipt = tmp_path / "origin.json"
    command = [
        str(_RUNTIME["INSTALLED"] / "bin/python"),
        "-I",
        "-B",
        "-c",
        telegram._BOOTSTRAP,
        str(_RUNTIME["SITE"]),
        str(ROOT),
        str(receipt),
        "server",
        "46747aa06f91cbbf19aa035d19015e32aa041d95",
        _RUNTIME["WHEEL_SHA"],
        str(dummy),
        str(env_file),
        "__effect-guard-probe__",
    ]
    completed = subprocess.run(
        command,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    observed = json.loads(receipt.read_text())
    assert observed["schema"] == "friday.release-1-0-installed-wheel-child-origin.v1"
    assert observed["cli_origin"] == str(_RUNTIME["SITE"] / "friday/cli.py")
    assert observed["source_checkout_imported"] is False
    assert observed["wheel_sha256"] == telegram.sha256_file(_RUNTIME["WHEEL"])


@pytest.mark.parametrize("with_canary", [False, True], ids=["startup", "one-canary"])
def test_installed_bridge_startup_polls_signed_loopback_under_owner_effect_guard(tmp_path, with_canary):
    """Real installed bridge/socket/lease/inbox; Telegram stays an offline fixture."""
    from friday.security import sign_bridge_request
    from friday.telegram_bridge import BOT_COMMANDS

    secret = "S" * 48
    received = []

    class Backend(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802 - standard library HTTP handler contract
            if not self.authorized("GET", b""):
                return
            received.append(("GET", self.path, dict(self.headers), b""))
            payload = b'{"items":[],"retired":[]}'
            self.reply(payload)

        def do_POST(self):  # noqa: N802 - standard library HTTP handler contract
            size = int(self.headers["Content-Length"])
            assert 0 < size <= 8192
            body = self.rfile.read(size)
            if not self.authorized("POST", body):
                return
            received.append(("POST", self.path, dict(self.headers), body))
            assert self.path == "/api/chat"
            assert json.loads(body)["message"] == inbound
            self.reply(
                json.dumps(
                    {
                        "message_id": "offline_backend_answer",
                        "message": policy.canary,
                        "message_format": "plain",
                    }
                ).encode()
            )

        def authorized(self, method, body):
            headers = self.headers
            expected = sign_bridge_request(
                secret,
                timestamp=headers.get("X-Friday-Timestamp", ""),
                method=method,
                path=self.path,
                external_user_id=str(OWNER_ID),
                chat_id=str(OWNER_ID),
                nonce=headers.get("X-Friday-Nonce", ""),
                body=body,
            )
            if (
                headers.get("X-Friday-User") != str(OWNER_ID)
                or headers.get("X-Friday-Chat") != str(OWNER_ID)
                or headers.get("X-Friday-Signature") != expected
            ):
                self.reply(b"{}", status=403)
                return False
            return True

        def reply(self, payload, *, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer(("127.0.0.1", 0), Backend)
    server.timeout = 5
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    worker.start()
    try:
        startup_commands = {
            "commands": [
                {"command": name, "description": description}
                for name, description in BOT_COMMANDS
                if name not in {"obsidian", "obsidian_alias", "engineer"}
            ]
        }
        policy, access, _document, _commands = _policy_access(tmp_path, startup_commands=startup_commands)
        inbound = telegram.build_observer_request(
            policy, access, {"bot_user_id": BOT_ID, "bot_username": "offline_startup"}
        )["inbound_text"]
        policy = dataclasses.replace(
            policy,
            contour=dataclasses.replace(
                policy.contour, backend_origin=f"http://127.0.0.1:{server.server_port}"
            ),
        )
        with httpx.Client(trust_env=False, timeout=2) as unauthenticated:
            assert unauthenticated.get(policy.contour.backend_origin + "/health").status_code == 403
        evidence_parent = tmp_path / "startup-evidence"
        evidence_parent.mkdir(mode=0o700)
        evidence = telegram.EvidenceStore(evidence_parent / "evidence", 1048576)
        spec_path, _driver = telegram.prepare_effect_ledger(
            evidence,
            policy=policy,
            access=access,
            token=TOKEN,
            deadline_monotonic_ns=time.monotonic_ns() + 600_000_000_000,
        )
        program = r"""
import asyncio, importlib.util, json, sys
from pathlib import Path
import httpx
from friday.telegram_bridge import TelegramBridge, TelegramConfig
import friday.telegram_bridge

source, spec_path, env_file, backend, inbox, token, owner, bot, secret, site, inbound, canary, with_canary = sys.argv[1:]
with_canary = with_canary == 'True'
assert Path(friday.telegram_bridge.__file__).is_relative_to(Path(site))
assert str(Path(source).parents[1]) not in sys.path
spec = importlib.util.spec_from_file_location('startup_guard', source)
guard_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = guard_module
spec.loader.exec_module(guard_module)
guard, original_send = guard_module.install_bridge_effect_guard(
    httpx, Path(spec_path), Path(env_file))
bridge = TelegramBridge(TelegramConfig(
    bot_token=token, bridge_secret=secret, allowed_chat_ids=[int(owner)],
    inbox_db_path=inbox, backend_url=backend,
    obsidian_enabled=False, engineer_mode_enabled=False, outbound_poll_interval_sec=2))
calls = []
posts = []
backend_seen = asyncio.Event()
reply_seen = asyncio.Event()

async def telegram_fixture(request):
    method = request.url.path.rsplit('/', 1)[-1]
    calls.append(method)
    if method == 'getMe':
        result = {'id': int(bot), 'username': 'offline_startup'}
    elif method == 'setMyCommands':
        result = True
    elif method == 'getUpdates':
        await asyncio.wait_for(backend_seen.wait(), timeout=5)
        if with_canary and calls.count('getUpdates') == 1:
            result = [{'update_id': 44, 'message': {'message_id': 33,
                'from': {'id': int(owner), 'is_bot': False, 'first_name': 'Offline'},
                'chat': {'id': int(owner), 'type': 'private'},
                'date': 1789444800, 'text': inbound}}]
        else:
            if with_canary:
                await asyncio.wait_for(reply_seen.wait(), timeout=5)
                await asyncio.wait_for(asyncio.gather(*tuple(bridge._inflight.values())), timeout=5)
            bridge._running = False
            result = []
    elif method in {'sendMessage', 'editMessageText'}:
        body = json.loads(request.content)
        identifier = body.get('message_id', 900 + len(posts))
        posts.append({'method': method, 'body': body, 'message_id': identifier})
        result = {'message_id': identifier, 'chat': {'id': int(owner), 'type': 'private'}, 'text': body['text']}
        if body['text'] == canary:
            reply_seen.set()
    elif method == 'sendChatAction':
        result = True
    else:
        raise AssertionError('unexpected Telegram fixture dispatch')
    return httpx.Response(200, json={'ok': True, 'result': result}, request=request)

async def observed_response(response):
    if str(response.request.url).startswith(backend + '/api/notifications/pending?'):
        backend_seen.set()

original_init = httpx.AsyncClient.__init__
def isolated_client(self, *args, **kwargs):
    assert not kwargs.get('trust_env', True)
    kwargs['mounts'] = {'https://api.telegram.org': httpx.MockTransport(telegram_fixture)}
    kwargs['event_hooks'] = {'response': [observed_response]}
    original_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = isolated_client
try:
    asyncio.run(asyncio.wait_for(bridge.run(), timeout=22))
finally:
    httpx.AsyncClient.__init__ = original_init
    httpx.AsyncClient.send = original_send
assert backend_seen.is_set() and guard.latched == ''
if with_canary:
    assert calls[:2] == ['getMe', 'setMyCommands'] and calls.count('getUpdates') == 2, calls
    assert 1 <= calls.count('sendMessage') <= 2 and reply_seen.is_set(), calls
    assert not bridge._inflight
else:
    assert calls == ['getMe', 'setMyCommands', 'getUpdates'], calls
# The real run() finally must release its lease and close the inbox.
bridge._lease.acquire()
bridge._lease.release()
guard_module.seal_effect_ledger(Path(spec_path).parent, Path(env_file))
print(json.dumps({'calls': calls, 'backend_seen': True,
    'installed_origin': friday.telegram_bridge.__file__, 'lease_reacquired': True,
    'posts': posts, 'inflight_clear': not bridge._inflight}))
"""
        completed = subprocess.run(
            [
                str(_RUNTIME["INSTALLED"] / "bin/python"),
                "-I",
                "-B",
                "-c",
                program,
                str(ROOT / "tools/release_1_0_telegram_roundtrip.py"),
                str(spec_path),
                str(policy.contour.env_file),
                policy.contour.backend_origin,
                str(tmp_path / "startup-inbox.sqlite3"),
                TOKEN,
                str(OWNER_ID),
                str(BOT_ID),
                secret,
                str(_RUNTIME["SITE"]),
                inbound,
                policy.canary,
                str(with_canary),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        observed = json.loads(completed.stdout)
        assert observed["lease_reacquired"] and observed["backend_seen"]
        assert len(received) == 1 + int(with_canary)
        assert received[0][1] == "/api/notifications/pending?limit=20&status_messages=1"
        for method, path, headers, body in received:
            assert headers["X-Friday-User"] == headers["X-Friday-Chat"] == str(OWNER_ID)
            assert headers["X-Friday-Signature"] == sign_bridge_request(
                secret,
                timestamp=int(headers["X-Friday-Timestamp"]),
                method=method,
                path=path,
                external_user_id=str(OWNER_ID),
                chat_id=str(OWNER_ID),
                nonce=headers["X-Friday-Nonce"],
                body=body,
            )
        ledger = telegram.inspect_effect_ledger(
            spec_path.parent, policy=policy, access=access, require_complete=False
        )
        assert ledger["unknown_count"] == 0
        if with_canary:
            assert ledger["telegram_update_ids"] == [44]
            assert ledger["telegram_message_ids"] == [33]
            assert ledger["send_message_receipt_ids"] == [
                p["message_id"] for p in observed["posts"] if p["method"] == "sendMessage"
            ]
            assert any(p["body"]["text"] == policy.canary for p in observed["posts"])
            assert observed["inflight_clear"]
        else:
            assert ledger["send_message_receipt_ids"] == []
            assert ledger["counts"]["bridge"] == {"getMe": 1, "setMyCommands": 1, "getUpdates": 1}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        assert not worker.is_alive()
