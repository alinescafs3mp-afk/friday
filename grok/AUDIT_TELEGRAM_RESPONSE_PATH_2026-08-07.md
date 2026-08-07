# Аудит: путь «человек → Пятница в Telegram → ответ модели»

**Дата:** 2026-08-07  
**Версия кода:** 0.180.0 (HEAD, `friday` / ex codename Jericho)  
**Область:** всё, что участвует в ответе, когда пользователь пишет Пятнице, шлёт файл или просит что-то в Telegram.  
**Вне области:** Admin UI, workers/organs (кроме исходящих уведомлений), CLI, массовый импорт диска, граф-админка как таковая.

Связанные артефакты: `OPEN.md` §9 (разведка 2026-08-07), `CHANGELOG.md` 0.171–0.179, `docs/SECURITY.md`, `docs/OPERATIONS.md`.

---

## 0. Резюме для владельца

Путь ответа в Telegram — **зрелый и защищённый по умолчанию**: durable-очередь, HMAC-мост, идемпотентность, tenant/person isolation, untrusted-оболочки для retrieval/файлов, review-gate на ingestion, плотное тестовое покрытие (десятки модулей).

**Критических дыр в коде моста/auth (обход allowlist, SSRF, открытый `/api/chat` без подписи) не найдено.**

Главные риски сегодня:

| # | Уровень | Суть |
|---|---------|------|
| 1 | **CRITICAL (конфиг)** | `FRIDAY_NEW_ACCOUNT_PRESET=owner` + open registration / shared archive — чужой человек получает полный архив и админ-права. Код предупреждает владельца, но не запрещает. |
| 2 | **HIGH (надёжность)** | Провал `ack` исходящих уведомлений → повторная доставка пакета до 20 сообщений. Названо в коде. |
| 3 | **HIGH (надёжность)** | Обрыв сети посреди multi-chunk `sendMessage` → retry шлёт уже доставленные куски снова. Названо в OPEN §9 / 0.173. |
| 4 | **MEDIUM (группы)** | `know:del` / `know:delok` без binding к нажавшему (в отличие от `conv`/`ent`/`relation`). |
| 5 | **MEDIUM (остаточное)** | Prompt injection через содержимое документов — смягчено оболочками и outbound-gate, не устранено. |
| 6 | **OPEN product** | Упоминание бота по `@имени` в группе не читается (OPEN §9). |

Общая оценка: **production-ready для локального личного/семейного экземпляра** при аккуратной конфигурации. Не «открытый бот в интернет без allowlist».

---

## 1. Карта архитектуры

```
Telegram getUpdates (long poll)
        │
        ▼
┌───────────────────────────────────────┐
│  friday.telegram_bridge               │
│  • SQLite inbox (pending/dead-letter) │
│  • ProcessLease (один мост)           │
│  • FIFO per chat, ≤8 concurrent       │
│  • media download (≤20 MB Bot API)    │
│  • HMAC-signed HTTP → backend         │
└───────────────────┬───────────────────┘
                    │ POST /api/chat (+ commands/callbacks)
                    ▼
┌───────────────────────────────────────┐
│  friday.server                        │
│  • body limit → HMAC+nonce+allowlist  │
│  • ActorContext (tenant vs person)    │
│  • rate limit (own_id + global)       │
│  • idempotency(source_ref)            │
│  • ingest_text / ingest_file          │
│  • AgentRuntime.chat                  │
└───────────────────┬───────────────────┘
                    │
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   ingestion    retrieval    LLM (vLLM)
   classifier   + graph      + tools
   review-gate  hybrid       kernel caps
        │           │           │
        └───────────┼───────────┘
                    ▼
          public_chat_ingestion
                    │
                    ▼
   bridge: cache response → sendMessage (HTML/UTF-16)
           + sendVoice / sendDocument (best-effort)
```

### Ключевые модули

| Слой | Путь | Роль |
|------|------|------|
| Мост | `friday/telegram_bridge/` | long-poll, очередь, команды, кнопки, медиа, доставка |
| Auth | `friday/security.py`, `server.py:_authenticate` | HMAC, nonce, allowlist, провижн |
| Chat HTTP | `server.py:POST /api/chat`, `/api/me/regenerate` | приём, ingest, agent |
| Агент | `friday/agent_runtime/` (~9.6k LOC) | контекст, tools, grounding, ответ |
| LLM | `agent_runtime/llm.py` | vLLM router, offline fallback |
| Ingest | `ingestion/_classifier.py`, `_capture.py`, `_files.py` | transient/review/promote |
| Проекции | `api/projections.py` | что можно отдать наружу |
| Голос | `whisper.py`, `tts.py` | STT/TTS (optional extra) |

Мост собран из mixin’ов (`Transport`, `Commands`, `Callbacks`, `Media`, `Views`) — раньше был один 1670-строчный модуль; `tests/test_bridge_surface.py` стережёт диспетчеры команд/кнопок AST-ом.

---

## 2. End-to-end сценарии

### 2.1. Текст

1. `getUpdates` → store в SQLite (`update_id`), offset++.
2. Drain: одна задача на `ordering_key=chat:{id}`, до 8 параллельно.
3. Allowlist **или** open registration (только private).
4. Команда (`/search`, `/retry`, …) **или** `POST /api/chat`:
   ```json
   {
     "message": "...",
     "source_ref": "telegram-update:{update_id}",
     "telegram_message_id": ...,
     "telegram_user": {...},
     "force_knowledge": false,
     "reply_to": "цитата ≤1000?",
     "forward": {...}?
   }
   ```
5. Backend: HMAC, nonce, chat allowlist, actor, rate limit, idempotency.
6. `ingest_text` (если есть `knowledge.create`) → promote / review / transient.
7. `AgentRuntime.chat` → history + retrieval + tools + model.
8. Мост кеширует JSON-ответ в inbox, шлёт текст (HTML-чанки), voice/files.
9. Успех → delete update row; ошибка → retry / dead-letter + notice.

### 2.2. Файл / фото / voice

1. `_prepare_document`: photo (max size) / document / voice / audio / video / …
2. Потолок `min(max_document_bytes, 20 MB)` — до и во время download; `getFile` «too big» → permanent.
3. `source_ref: telegram-file:{file_id}` (стабильный file_id, не update_id).
4. Base64 в chat payload → `ingest_file` (или `inspect_file_transient` при «не запоминай»).
5. Voice ≤ 180 с: transcript = message; вложение убирается из attachments (не дублировать).
6. Same-turn: модель видит excerpt ≤ 24k даже если файл в Inbox (review).
7. `uploaded_by = actor.own_id` — автор материала в shared archive.

### 2.3. Команды (без полной LLM-генерации)

`/search`, `/history`, `/timeline`, `/browse`, `/profile`, `/tags`, `/inbox`,  
`/conflicts`, `/relations`, `/merges`, `/missions`, `/reminders`, `/export`,  
`/why`, `/status`, `/compact`, `/archive`, `/delete`, `/rename`, `/retry`, …

Каждая — подписанный backend API + форматирование во `ViewsMixin`.

### 2.4. Inline-кнопки

Формат `family:action:target` (3 части), target `^[A-Za-z0-9_.-]{1,96}$`.  
Семейства: inbox, doc, know, ent, apr, feedback, mission, merge, relation, conflict, conv, remind, …

После действия — снятие **своего** ряда клавиатуры (не всей).

### 2.5. Исходящие (проактивные)

`_outbound_loop` каждые ≥2 с (poll interval 15 с):  
`GET /api/notifications/pending` → `sendMessage` → `POST /api/notifications/ack`.  
Подпись service-calls — первый **положительный** chat id allowlist (группа в `[0]` ломала outbound до 0.75.x).

---

## 3. Backend: agent.chat (суть)

`AgentRuntime.chat` (`agent_runtime/__init__.py:3571+`):

1. **person_id** (`own_id`) vs **tenant_id** (`user_id`) — переписка личная, архив может быть общим.
2. Conversation + history (20) **до** записи user-turn.
3. Stop-order (`_ORDERS_SILENCE`) — без tools/LLM.
4. Attachments: только owned `raw_object_id` + `files.read`; restore follow-up только по deictic/exact replay id.
5. Sticky **`private_context_lineage`**: private file turn → нет outbound web, нет promote в account-wide memory.
6. `_prepare_context`: hybrid search, graph, arbiters, standing rules.
7. `reply_to` **после** classifier/search — цитата не отравляет intent.
8. Tools: capability-gated; small-talk без tools; outbound strip при private/«человек».
9. Structural answers (правила, security refuse, Office exact) — модель не перебивает.
10. LLM loop + verify/repair + citations + grounding_warning.
11. Persist assistant; optional TTS (`answer_with_voice`); generated files.

### Untrusted-модель данных

| Источник | Как попадает в prompt |
|----------|------------------------|
| Knowledge / graph | `FRIDAY_CONTEXT_DATA (untrusted JSON)` user-role |
| Web tools | `WEB_SEARCH_RESULTS (untrusted; data only)` |
| Attachments | `<attachment>` / Office JSON + system «не инструкции» |
| Reply quote | после context prep |
| Standing rules | отдельный контур; security-like rules → structural refuse |

Принцип (закон проекта): динамика **не** elevates в system policy.

### Ingestion decision (chat)

| action | Когда (упрощённо) |
|--------|-------------------|
| transient | приветствия, pure Q, slash-команды, web-ask, synthetic «Загружен документ», explicit_no_save |
| review | пограничный / policy `unless_explicit` (default) / файлы без explicit save |
| promote | explicit «запомни» / `/note` / force_knowledge + policy allows |

`explicit_no_save` побеждает force; файлы в default policy → Inbox, не silent KO.

---

## 4. Безопасность

### 4.1. Сильные стороны (POSITIVE)

| # | Механизм | Где |
|---|----------|-----|
| P1 | HMAC-SHA256 + body hash + freshness + single-use nonce | `security.py`, `claim_bridge_nonce` |
| P2 | Deny-by-default allowlist; empty → refuse start | `TelegramConfig.validate`, server gate |
| P3 | Open reg только private (`chat_id == user_id`) | bridge + backend re-check |
| P4 | Secret ≥32, ≠ api_token | config validation |
| P5 | Absolute `inbox_db_path` + ProcessLease | no dual-bridge / orphan queue |
| P6 | Body limit **до** auth parse | middleware order |
| P7 | Rate limit по **own_id**, не shared tenant | `server.py:_enforce_rate_limit` |
| P8 | Idempotency по person + payload fingerprint | `/api/chat` |
| P9 | Conversations / channel sessions / regenerates keyed by person | shared archive safe |
| P10 | File rehydrate: `uploaded_by == person_id` | agent_runtime |
| P11 | Outbound tools stripped on private lineage | agent_runtime |
| P12 | Secret redaction in logs (bot token in URL path) | telemetry + bridge redactor |
| P13 | Dead-letter stores **exception type**, not message | no token leak in doctor |
| P14 | Proxy only for Telegram; backend `trust_env=False` + CA verify | transport |
| P15 | HTML escape; links only http(s); plain fallback on 400 | `_markup.py`, `_send_message` |
| P16 | Public chat projection strips internal IDs/transcripts | `public_chat_ingestion` |
| P17 | `hmac.compare_digest` on **bytes** (obs-text 500→401 fix) | security.py |
| P18 | Group new accounts → guest (least privilege) unless flags | server provision |

### 4.2. Находки

#### CRITICAL

**C1. Конфигурационный footgun: `FRIDAY_NEW_ACCOUNT_PRESET` + open registration / shared archive**

- **Где:** `server.py` ~821–897, `config` `new_account_preset`.
- **Что:** владелец может заставить **каждую** новую Telegram-учётку (включая open-reg stranger) получить preset `owner`/`admin`.
- **Следствие:** полный доступ к shared archive, tools, missions — на бюджете LLM владельца.
- **Смягчение:** уведомление owner chats (`_notify_owners_of_self_registration`); `newcomer` по умолчанию без forced preset.
- **Статус:** by design (запрос владельца 2026-08-02). Не баг кода, **операционный CRITICAL**, если флаги включены неосознанно.
- **Рекомендация:** hard-deny `owner`/`admin` для non-allowlisted open-reg; или require interactive owner approve before elevate (кнопка 0.179 уже есть — не выдавать full preset автоматически).

#### HIGH

**H1. Outbound ack failure → mass re-delivery**

- **Где:** `_transport.py:_ack_outbound` ~512–561.
- **Что:** state «доставлено» живёт только в local list до ack; failed ack → до 20 сообщений снова каждые ~15 с.
- **Документировано** в docstring; 3 in-place retries; полное решение = `in_flight` lease (schema).
- **Риск:** user-visible spam (reminders, approvals, mission pings).

**H2. Shared archive blast radius is intentional**

- Документы/knowledge — tenant-wide; isolation = preset + `uploaded_by` + private conversations.
- `FRIDAY_TELEGRAM_GROUP_MEMBERS_FULL_ACCESS=1` выдаёт `user` (web, upload, missions) всем в группе.
- Residual: любой principal с `knowledge.read` видит корпус арендатора.

**H3. Prompt injection residual**

- Полный текст файлов/знаний в prompt. Outbound web блокируется на private lineage; **локальные** tools (search, graph, export-like) остаются, если capability позволяет.
- Mitigations strong (untrusted envelopes, structural refuse, citation strip); not a formal sandbox of tool args beyond kernel.

#### MEDIUM

**M1. `know:del` / `know:delok` без invoker binding**

- **Где:** `_callbacks.py` ~266–294.
- **Контраст:** `ent:delyes`, `conv:delete`, `relation:*` вшивают `external_user_id` в `target` и сверяют presser.
- **Сценарий:** в group chat A открыл confirm delete, B нажал «Да» — backend авторизует **B** (capability B), UX intent A потерян.
- Backend не даст удалить чужое без rights, но **социальный** binding сломан.

**M2. Multi-chunk network drop redelivers earlier chunks**

- **Где:** cache + full `_send_message` on retry; 429 per-chunk fixed (0.173).
- **OPEN §9:** 1/367 long answers on live sample — rare but real.
- Fix cost: durable `chunks_sent` on inbox row.

**M3. Sync SQLite on asyncio hot path**

- `_queue.py` блокирует event loop при concurrent drains; под нагрузкой latency spikes.

**M4. Inconsistent invoker-binding across destructive callbacks**

- inbox / merge / conflict / know — без; relation / conv / ent — с.

**M5. Secrets in uploaded files can enter model context**

- `secret_hygiene` — offline doctor, not chat pipeline. Extracted PDF/DOCX with API keys becomes model-visible data.

**M6. In-process SlidingWindowLimiter**

- Per-process memory. Fine for single-worker local; multi-worker = N× budget.

#### LOW

| # | Тема | Детали |
|---|------|--------|
| L1 | `set_offset` без commit | crash → re-fetch; `INSERT OR IGNORE` saves |
| L2 | `self._redact` assigned, never called | dead code; type-name errors already safe |
| L3 | In-memory album captions / edit targets | lose on restart (documented) |
| L4 | Large media base64 in RAM | ≤20 MB × concurrent updates |
| L5 | Health may expose llm_enabled/model | local-first acceptable |
| L6 | Dual headers `x-friday-*` / `x-jericho-*` | migration; remove later |
| L7 | PermanentUpdateError may carry backend text[:500] | queue stores class name; log path careful |

---

## 5. Надёжность

| Механизм | Оценка |
|----------|--------|
| Durable inbox + offset after store | **отлично** — process crash не теряет update |
| Retry with backoff, max 288 attempts (~сутки) | **хорошо** |
| Permanent vs retryable HTTP mapping | **хорошо** (409+Retry-After vs bare 409) |
| Model response cache before Telegram send | **отлично** — LLM не дёргается повторно |
| Per-chat FIFO + cross-chat concurrency (8) | **отлично** (0.172 fix) |
| 429 per-chunk retry | **хорошо** (0.173) |
| Outbound ack | **слабо** (H1) |
| Partial multi-chunk resume | **слабо** (M2) |
| Bridge timeout vs agent budget | **выровнено** (0.171) |
| Backend-down owner alert (direct sendMessage) | **хорошо** |
| Typing indicator during long chat | **есть** |
| Voice/files after text: best-effort | **честно** |

---

## 6. Корректность / edge cases

| Сценарий | Поведение | Оценка |
|----------|-----------|--------|
| Альбом caption | in-memory, FIFO 10 groups | OK; restart loses |
| Reply-to | separate field, post-context | OK (0.175) |
| Edited message with text | «правку не подхватываю» | честно |
| Live location edit | ignore (no text) | OK |
| Voice unrecognised | flag → user wording | OK |
| Voice + caption | caption = question, transcript attachment | OK |
| «Не запоминай» + file | transient inspect, no Raw | OK |
| Web ask | transient ingest (not Inbox spam) | OK |
| Regenerate + lost attachment | regenerate_notice | OK |
| Shared archive conversation ownership | channel session by own_id | OK (measured bug fixed) |
| UTF-16 emoji split | `split_for_telegram` | OK (0.80) |
| Tables → monospace | 0.177 | OK |
| @mention bot in group | **not handled** | OPEN gap |
| Sticker/poll/dice | explicit refusal | OK |
| Location/contact | structured note, force knowledge | OK |

---

## 7. Тестовое покрытие

**Плотность: высокая** (~40–50 модулей касаются path).

### Ядро

- `tests/test_bridge_surface.py` — AST-сторож surface + dispatch tables  
- `tests/test_telegram_and_profile.py` — главный интеграционный срез (~56 tests)  
- `tests/test_api_vertical_slice.py` — signed chat, allowlist, tenant  
- `tests/test_hardening_regressions.py` — idempotency, auth, attachments  
- `tests/test_one_slow_chat_does_not_freeze_the_others.py` — concurrency  
- `tests/test_a_rate_limit_does_not_duplicate_the_answer.py` — 429  
- `tests/test_an_album_keeps_its_caption.py`  
- `tests/test_a_reply_points_at_what_it_answers.py`  
- `tests/test_attachment_security_boundaries.py`  
- `tests/test_an_answer_is_not_replayed_to_another_person.py`  
- `tests/test_a_newcomer_reaches_the_owner.py`  
- `tests/test_outbound_ack_retry.py`  
- `tests/test_callback_ack_order.py`  

### Пробелы

1. Multi-chunk partial send → no redeliver (no test; known residual).  
2. `@mention` group (product open).  
3. Full E2E Telegram update → agent → multi-chunk under combined 429.  
4. Callback idempotency matrix **всех** namespaces (feedback covered well).  
5. Hostile document → local tool chain red-team (outbound covered better).  
6. Multi-worker rate-limit coherence.  
7. `know:delok` invoker binding (gap matches M1).  

---

## 8. Уже закрытые дефекты (контекст)

Полный список — `OPEN.md` §9 и `CHANGELOG` 0.171–0.179. Кратко:

| Версия | Что починили |
|--------|----------------|
| 0.171 | timeout моста = budget ядра |
| 0.172 | non-blocking drain; delete KO from chat |
| 0.173 | 429 chunk retry; refusal wording; mission typing |
| 0.174 | «Дальше» для длинного источника |
| 0.175 | reply_to_message |
| 0.176 | album caption |
| 0.177 | tables + markup-safe split |
| 0.178 | edit KO by reply-to invite |
| 0.179 | newcomer → owner grant button |
| earlier | UTF-16, secret hygiene, TLS CA, inbox-first files, signer group id, channel own_id, voice as question, private lineage, … |

---

## 9. Рекомендации (приоритет)

### P0 — сделать / ужесточить

1. **Конфиг-guard:** не выдавать `owner`/`admin` через `NEW_ACCOUNT_PRESET` для open-reg non-allowlisted; elevate только кнопкой 0.179.  
2. **`in_flight` lease** на outbound notifications (H1) — schema + expiry.  
3. **`know:delok` invoker binding** по образцу `ent:delyes` / `conv:delete` (M1).

### P1 — надёжность доставки

4. Durable `chunks_sent` / resume multi-chunk (M2) — если multi-chunk frequency растёт.  
5. Async queue IO или threadpool для SQLite (M3) — если concurrent chats > few.  
6. Унифицировать invoker-binding на inbox/merge/conflict (M4).

### P2 — product

7. `@mention` бота в группе (OPEN).  
8. Persist album caption across restart (optional; documented degradation OK).  
9. Hostile-doc → tool red-team suite.

### P3 — hygiene

10. Remove dead `_redact` or wire it.  
11. Drop `x-jericho-*` headers after cutover complete.  
12. Optional: scan extracted text against env secrets before model (M5).

---

## 10. Операционный чеклист (живой экземпляр)

Перед/после аудита убедиться:

- [ ] `FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS` / `OWNER_CHAT_IDS` непусты и содержат **личный** chat id владельца (положительный).  
- [ ] `FRIDAY_TELEGRAM_BRIDGE_SECRET` ≥32, ≠ `FRIDAY_API_TOKEN`.  
- [ ] `FRIDAY_TELEGRAM_OPEN_REGISTRATION` — осознанно; если 1, **не** ставить `NEW_ACCOUNT_PRESET=owner`.  
- [ ] `FRIDAY_SHARED_ARCHIVE` — осознанно; гости = guest.  
- [ ] Inbox path absolute; один bridge process (lease).  
- [ ] TLS: `FRIDAY_BACKEND_CA_FILE` если backend self-signed.  
- [ ] Proxy only for Telegram if needed.  
- [ ] `jericho doctor` без bridge_queue dead-letter explosion.  
- [ ] Live smoke: text, file PDF, voice, 👍/👎, one long answer, one callback.

---

## 11. Файловая карта (для следующего аудитора)

```
friday/telegram_bridge/
  __init__.py          TelegramBridge composition
  _base.py             config, UTF-16 split, BOT_COMMANDS, errors
  _queue.py            durable inbox, ordering_key, dead-letter
  _transport.py        poll, drain, outbound, HMAC call, sendMessage
  _commands.py         message/command router, POST /api/chat
  _callbacks.py        inline buttons
  _media.py            files/photos/voice/album/forward
  _views.py            format command responses
  _markup.py           HTML for Telegram

friday/server.py       auth, rate limit, /api/chat, regenerate
friday/security.py     sign/verify bridge
friday/agent_runtime/  chat, prompt, tools, grounding
friday/ingestion/      classifier, text/file capture
friday/api/projections.py  public_chat_ingestion
friday/whisper.py, tts.py  voice I/O
```

Happy path chat entry: `_commands.py` ~1256–1295 → `server.py` ~1893–2316 → `AgentRuntime.chat` ~3571+.

---

## 12. Метод аудита

1. Чтение кода path end-to-end (bridge + server + agent + security + ingestion).  
2. Три независимых разведочных прохода (bridge / backend+agent / tests+OPEN).  
3. Сверка с `OPEN.md` §9, `CHANGELOG` 0.171–0.179, `GROK.md` laws.  
4. Точечная проверка спорных мест (`know:del`, ack, HMAC, rate limit own_id).  

**Не делалось:** live red-team против production bot; fuzz Telegram API; multi-worker load test; formal threat model doc beyond this file.

---

## 13. Вердикт

Путь «написал / скинул файл / попросил в Telegram» — **главный интерфейс продукта** и в коде это видно: он прошёл через множество **measured** fixes, имеет явные residual'ы (не молчит о них) и слой за слоем закрывает auth, isolation, idempotency, delivery.

Работать можно. Следить за **конфигом аккаунтов**, **outbound redelivery**, **group button binding** и **содержимым документов в prompt**. Открытый product-gap — `@mention` в группе.

---

*Отчёт записан: `grok/AUDIT_TELEGRAM_RESPONSE_PATH_2026-08-07.md`.*  
*Предложения по фиксам, если пойдут в работу, — отдельно в `grok/PROPOSALS.md` в принятом формате.*
