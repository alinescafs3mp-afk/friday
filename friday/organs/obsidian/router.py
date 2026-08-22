"""Capability-gated HTTP and fragment-token setup surfaces for Obsidian."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from .contracts import (
    IdempotencyConflictError,
    NoteAlreadyExistsError,
    NoteNotFoundError,
    ObsidianNoteError,
    RevisionConflictError,
)
from .operations import (
    OperationCommitUncertain,
    OperationLedgerError,
    OperationTerminalError,
)
from .runtime import ObsidianCompatibilityError
from .syncthing import SyncthingError

_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_SETUP_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SMALL_BODY_BYTES = 1_024
_OPERATION_BODY_BYTES = 256 * 1_024
_PUBLIC_SETUP_BODY_BYTES = 512
_PUBLIC_SETUP_FIELDS = frozenset(
    {
        "actions",
        "android_path_hint",
        "display_name",
        "folder_id",
        "message",
        "requires_obsidian_account",
        "requires_qr",
        "server_device_id",
        "state",
        "steps",
        "vault_name",
    }
)

_SETUP_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Friday · настройка Obsidian</title>
</head>
<body>
  <main>
    <h1>Подключение Obsidian на Android</h1>
    <p id="setup-status">Проверяю ссылку настройки…</p>
    <section id="device-section" hidden>
      <label for="device-id">Friday Device ID</label>
      <input id="device-id" type="text" readonly autocomplete="off">
      <button id="copy-device-id" type="button">Скопировать Device ID</button>
    </section>
    <p id="setup-details"></p>
    <noscript>Для этой страницы нужен JavaScript. Device ID остаётся доступен в Telegram.</noscript>
  </main>
  <script src="/obsidian/setup.js" defer></script>
</body>
</html>
"""

_SETUP_JS = r""""use strict";
(() => {
  const status = document.getElementById("setup-status");
  const details = document.getElementById("setup-details");
  const section = document.getElementById("device-section");
  const deviceId = document.getElementById("device-id");
  const copy = document.getElementById("copy-device-id");
  const fragment = window.location.hash.slice(1);
  window.history.replaceState(null, "", window.location.pathname);

  let token = fragment;
  if (fragment.startsWith("token=")) {
    try {
      token = decodeURIComponent(fragment.slice(6));
    } catch (_error) {
      token = "";
    }
  }
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) {
    status.textContent = "Ссылка настройки недействительна или уже использована.";
    return;
  }

  fetch("/api/public/obsidian/setup/resolve", {
    method: "POST",
    credentials: "omit",
    cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({token}),
  }).then((response) => {
    token = "";
    if (!response.ok) {
      throw new Error("setup unavailable");
    }
    return response.json();
  }).then((payload) => {
    status.textContent = String(payload.message || "Продолжите настройку в Syncthing-Fork.");
    const value = String(payload.server_device_id || "");
    if (value) {
      deviceId.value = value;
      section.hidden = false;
    }
    const vault = String(payload.vault_name || payload.display_name || "");
    details.textContent = vault ? `Vault: ${vault}` : "";
  }).catch(() => {
    status.textContent = "Ссылка настройки недействительна или уже использована.";
  });

  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(deviceId.value);
      copy.textContent = "Device ID скопирован";
    } catch (_error) {
      deviceId.focus();
      deviceId.select();
    }
  });
})();
"""

_OPEN_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Friday · открыть в Obsidian</title>
</head>
<body>
  <main>
    <h1>Открыть заметку в Obsidian</h1>
    <p id="open-status">Проверяю ссылку…</p>
    <a id="open-note" href="#" hidden>Открыть заметку в Obsidian</a>
    <p id="open-fallback" hidden>Если приложение не открылось автоматически, нажмите ссылку выше или откройте заметку вручную.</p>
    <noscript>Для безопасного перехода в приложение нужен JavaScript.</noscript>
  </main>
  <script src="/obsidian/open.js" defer></script>
</body>
</html>
"""

_OPEN_JS = r""""use strict";
(() => {
  const status = document.getElementById("open-status");
  const link = document.getElementById("open-note");
  const fallback = document.getElementById("open-fallback");
  let fragment = window.location.hash.slice(1);
  window.history.replaceState(null, "", window.location.pathname);
  let parameters;
  try {
    parameters = new URLSearchParams(fragment);
  } catch (_error) {
    parameters = new URLSearchParams();
  }
  fragment = "";
  const vault = String(parameters.get("vault") || "").normalize("NFC").trim();
  const file = String(parameters.get("file") || "").normalize("NFC");
  const unsafeVault = /[\\/\u0000-\u001f\u007f]/u;
  const unsafeFile = /[\\\u0000-\u001f\u007f]/u;
  const fileParts = file.split("/");
  if (!vault || vault.length > 100 || new TextEncoder().encode(vault).length > 256
      || unsafeVault.test(vault) || !file || file.length > 2048
      || new TextEncoder().encode(file).length > 4096 || file.startsWith("/")
      || unsafeFile.test(file) || fileParts.some((part) => !part || part === "." || part === "..")
      || !file.toLocaleLowerCase("en-US").endsWith(".md")) {
    status.textContent = "Ссылка открытия недействительна.";
    return;
  }
  parameters = new URLSearchParams({vault, file});
  link.href = `obsidian://open?${parameters.toString()}`;
  link.textContent = `Открыть ${file} в Obsidian`;
  link.hidden = false;
  fallback.hidden = false;
  status.textContent = `Vault: ${vault}`;
  try {
    window.location.assign(link.href);
  } catch (_error) {
    status.textContent = `Vault: ${vault}. Нажмите ссылку открытия.`;
  }
})();
"""


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "obsidian_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Obsidian runtime is unavailable")
    return runtime


def _runtime_method(request: Request, name: str) -> Any:
    method = getattr(_runtime(request), name, None)
    if not callable(method):
        raise HTTPException(status_code=501, detail="Obsidian operation is unavailable")
    return method


async def _invoke(request: Request, name: str, *args: Any) -> Any:
    try:
        return await _runtime_method(request, name)(*args)
    except NoteNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Obsidian note not found") from exc
    except (
        IdempotencyConflictError,
        NoteAlreadyExistsError,
        OperationCommitUncertain,
        OperationTerminalError,
        RevisionConflictError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OperationLedgerError as exc:
        raise HTTPException(status_code=404, detail="Obsidian operation not found") from exc
    except (ObsidianCompatibilityError, SyncthingError) as exc:
        raise HTTPException(status_code=503, detail="Obsidian synchronization is unavailable") from exc
    except (ObsidianNoteError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _owner(request: Request, capability: str) -> str:
    actor = request.state.actor
    request.app.state.auth_service.require(actor, capability)
    return str(actor.own_id)


def _reject_explicit_owner(request: Request) -> None:
    if "user_id" in request.query_params or "owner_id" in request.query_params:
        raise HTTPException(status_code=400, detail="Explicit Obsidian owner is not accepted")


async def _json_object(request: Request, *, maximum: int) -> dict[str, Any]:
    raw_length = request.headers.get("content-length", "").strip()
    if raw_length:
        try:
            if int(raw_length) > maximum:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
    payload = await request.body()
    if len(payload) > maximum:
        raise HTTPException(status_code=413, detail="Request body is too large")
    if not payload:
        return {}
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="Expected application/json")
    try:
        parsed = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")
    return parsed


async def _empty_body(request: Request) -> None:
    body = await _json_object(request, maximum=_SMALL_BODY_BYTES)
    if body:
        raise HTTPException(status_code=400, detail="This action accepts no fields")


def _remove_setup_token(value: Any, token: str) -> Any:
    if isinstance(value, str):
        return value.replace(token, "")
    if isinstance(value, list):
        return [_remove_setup_token(item, token) for item in value[:32]]
    if isinstance(value, Mapping):
        return {
            str(key): _remove_setup_token(item, token)
            for key, item in list(value.items())[:32]
            if "token" not in str(key).casefold()
        }
    return value


def _public_setup_result(value: Any, token: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HTTPException(status_code=404, detail="Setup link is invalid or already used")
    projected = {
        str(key): _remove_setup_token(item, token)
        for key, item in value.items()
        if str(key) in _PUBLIC_SETUP_FIELDS
    }
    if not projected:
        raise HTTPException(status_code=404, detail="Setup link is invalid or already used")
    return projected


def build_router() -> APIRouter:
    """Build a fresh router; request state remains the only runtime authority."""

    api = APIRouter(tags=["obsidian"])

    @api.get("/api/obsidian/status")
    async def status(request: Request) -> Any:
        _reject_explicit_owner(request)
        owner_id = _owner(request, "obsidian.read")
        return await _runtime_method(request, "status")(owner_id)

    @api.get("/api/obsidian/diagnostics")
    async def diagnostics(request: Request) -> Any:
        _reject_explicit_owner(request)
        owner_id = _owner(request, "obsidian.read")
        return await _invoke(request, "diagnostics", owner_id)

    @api.post("/api/obsidian/onboarding/start")
    async def start(request: Request) -> Any:
        await _empty_body(request)
        owner_id = _owner(request, "obsidian.connect")
        return await _runtime_method(request, "start")(owner_id)

    @api.get("/api/obsidian/onboarding")
    async def onboarding(request: Request) -> Any:
        _reject_explicit_owner(request)
        owner_id = _owner(request, "obsidian.read")
        return await _runtime_method(request, "onboarding")(owner_id)

    @api.post("/api/obsidian/onboarding/check")
    async def check(request: Request) -> Any:
        await _empty_body(request)
        owner_id = _owner(request, "obsidian.connect")
        return await _runtime_method(request, "check")(owner_id)

    @api.post("/api/obsidian/onboarding/select-device")
    async def select_device(request: Request) -> Any:
        body = await _json_object(request, maximum=_SMALL_BODY_BYTES)
        if set(body) != {"candidate_id"}:
            raise HTTPException(status_code=400, detail="candidate_id is the only accepted field")
        candidate_id = body.get("candidate_id")
        if not isinstance(candidate_id, str) or _CANDIDATE_ID.fullmatch(candidate_id) is None:
            raise HTTPException(status_code=400, detail="Invalid candidate_id")
        owner_id = _owner(request, "obsidian.connect")
        try:
            return await _runtime_method(request, "select_device")(owner_id, candidate_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Obsidian device candidate not found") from exc

    @api.post("/api/obsidian/onboarding/confirm-open")
    async def confirm_open(request: Request) -> Any:
        await _empty_body(request)
        owner_id = _owner(request, "obsidian.connect")
        return await _runtime_method(request, "confirm_open")(owner_id)

    @api.post("/api/obsidian/onboarding/retry")
    async def retry(request: Request) -> Any:
        await _empty_body(request)
        owner_id = _owner(request, "obsidian.connect")
        return await _runtime_method(request, "retry")(owner_id)

    @api.post("/api/obsidian/onboarding/cancel")
    async def cancel(request: Request) -> Any:
        await _empty_body(request)
        owner_id = _owner(request, "obsidian.connect")
        return await _runtime_method(request, "cancel")(owner_id)

    @api.post("/api/obsidian/onboarding/vault-alias")
    async def vault_alias(request: Request) -> Any:
        body = await _json_object(request, maximum=_SMALL_BODY_BYTES)
        if set(body) != {"alias"} or not isinstance(body.get("alias"), str):
            raise HTTPException(status_code=400, detail="alias is the only accepted field")
        owner_id = _owner(request, "obsidian.connect")
        return await _invoke(request, "set_vault_alias", owner_id, body["alias"])

    @api.get("/api/obsidian/vaults")
    async def vaults(request: Request) -> Any:
        _reject_explicit_owner(request)
        owner_id = _owner(request, "obsidian.read")
        return await _runtime_method(request, "vaults")(owner_id)

    @api.get("/api/obsidian/notes")
    async def list_notes(request: Request) -> Any:
        _reject_explicit_owner(request)
        if request.query_params:
            raise HTTPException(status_code=400, detail="Note list accepts no query fields")
        owner_id = _owner(request, "obsidian.read")
        return await _invoke(request, "list_notes", owner_id)

    @api.get("/api/obsidian/notes/search")
    async def search_notes(request: Request) -> Any:
        _reject_explicit_owner(request)
        if set(request.query_params) - {"q", "limit"}:
            raise HTTPException(status_code=400, detail="Unsupported note search field")
        if len(request.query_params.getlist("q")) != 1:
            raise HTTPException(status_code=400, detail="q is required exactly once")
        query = request.query_params.get("q", "")
        if not query or len(query) > 1_000:
            raise HTTPException(status_code=400, detail="q must be non-empty and bounded")
        raw_limit = request.query_params.get("limit", "20")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="limit must be an integer") from exc
        if not 1 <= limit <= 100 or len(request.query_params.getlist("limit")) > 1:
            raise HTTPException(status_code=400, detail="limit must be between 1 and 100")
        owner_id = _owner(request, "obsidian.read")
        return await _invoke(request, "search_notes", owner_id, query, limit)

    @api.get("/api/obsidian/notes/read")
    async def read_note(request: Request) -> Any:
        _reject_explicit_owner(request)
        if set(request.query_params) != {"path"} or len(request.query_params.getlist("path")) != 1:
            raise HTTPException(status_code=400, detail="path is required exactly once")
        path = request.query_params.get("path", "")
        if not path or len(path) > 2_048:
            raise HTTPException(status_code=400, detail="path must be non-empty and bounded")
        owner_id = _owner(request, "obsidian.read")
        return await _invoke(request, "read_note", owner_id, path)

    @api.post("/api/obsidian/operations")
    async def execute_operation(request: Request) -> Any:
        body = await _json_object(request, maximum=_OPERATION_BODY_BYTES)
        if "user_id" in body or "owner_id" in body:
            raise HTTPException(status_code=400, detail="Explicit Obsidian owner is not accepted")
        owner_id = _owner(request, "obsidian.write")
        return await _invoke(request, "execute_operation", owner_id, body)

    @api.get("/api/obsidian/operations/{operation_id}")
    async def get_operation(request: Request, operation_id: str) -> Any:
        _reject_explicit_owner(request)
        if not operation_id or len(operation_id) > 200 or "\x00" in operation_id:
            raise HTTPException(status_code=400, detail="Invalid operation_id")
        owner_id = _owner(request, "obsidian.read")
        return await _invoke(request, "get_operation", owner_id, operation_id)

    @api.get("/obsidian/setup", include_in_schema=False, response_class=HTMLResponse)
    async def setup_page() -> HTMLResponse:
        return HTMLResponse(_SETUP_HTML)

    @api.get("/obsidian/setup.js", include_in_schema=False)
    async def setup_script() -> Response:
        return Response(_SETUP_JS, media_type="application/javascript")

    @api.get("/obsidian/open", include_in_schema=False, response_class=HTMLResponse)
    async def open_page() -> HTMLResponse:
        return HTMLResponse(
            _OPEN_HTML,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @api.get("/obsidian/open.js", include_in_schema=False)
    async def open_script() -> Response:
        return Response(
            _OPEN_JS,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @api.post("/api/public/obsidian/setup/resolve", include_in_schema=False)
    async def resolve_public_setup(request: Request) -> dict[str, Any]:
        body = await _json_object(request, maximum=_PUBLIC_SETUP_BODY_BYTES)
        if set(body) != {"token"} or not isinstance(body.get("token"), str):
            raise HTTPException(status_code=400, detail="token is the only accepted field")
        token = body["token"]
        if _SETUP_TOKEN.fullmatch(token) is None:
            raise HTTPException(status_code=404, detail="Setup link is invalid or already used")
        try:
            resolved = await _runtime_method(request, "resolve_public_setup")(token)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="Setup link is invalid or already used") from exc
        return _public_setup_result(resolved, token)

    return api


router = build_router()

__all__ = ["build_router", "router"]
