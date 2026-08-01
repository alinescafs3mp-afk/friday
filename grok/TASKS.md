# Назначено Grok — 2026-07-31

Общий список и живые числа — `TASKS.md` в корне. Закон — `grok/GROK.md`, он выше этого файла.
Порядок — по убыванию пользы. Упрёшься — переходи дальше, причину пиши в `grok/PROPOSALS.md`.

---

# Последнее на сегодня — G23: состязательный обзор голоса Friday (свежий взгляд) — **сделано**

Последняя задача на сегодня — можешь взять сейчас и отдыхать после неё, новых не будет.

Владелец за вечер попросил голосовой ответ (Friday говорит на просьбу внутри
разговора, не команда, не режим-переключатель) — я реализовала и включила на
бою. Три коммита: `af620c3` (движок `friday/tts.py` + инструмент агента
`speak`), `faab13a` (доставка в Telegram — `_send_voice`/
`_deliver_voice_reply`), `11a35b2` (девять тестов, каждый проверен мутацией).
Гейт зелёный, `FRIDAY_TTS_ENABLED=1` уже в бою.

**Твоя задача — состязательный обзор этих трёх коммитов, свежим взглядом
(ты этот код не писал).** Смотри как в прошлых проходах ревью (G17/G18/G21
нашли реальные HIGH). Наводки, откуда начать смотреть, не готовые находки:

1. **`tts.use` — граница прав.** Выдан admin/moderator/user/guest, как
   `chat.use` (`friday/permissions/__init__.py`). Верно ли это по риску —
   `speak` синтезирует локально, не читает и не пишет чужие данные, но ест
   CPU/GPU время наравне с LLM-вызовом. Стоит ли это ограничивать так же, как
   `web.search`/`web.fetch` (risk_level 1), а не risk_level 0?
2. **`ToolResult.attachment` — новый канал мимо модели.** `execution_kernel/__init__.py:execute()`
   вынимает `_attachment` из `data` ДО того, как он попадёт в
   `to_llm_message()`/`to_dict()` — специально, чтобы аудио не текло в контекст
   модели. Это единственный тул сегодня, что так делает. Проверь: не течёт ли
   `_attachment`/`audio_base64` куда-то ещё — аудит-лог (`_audit_details`),
   verification-путь (`_verify_response`), citation-логика — везде, где
   `tool_result.data`/`tool_evidence` читается по всему `agent_runtime`.
3. **`_speak` не проверяет `actor` вообще** (`del actor` в начале — синтез
   без пользовательского состояния). Это верно СЕЙЧАС (piper не трогает
   storage/kg), но подумай, есть ли путь, которым чужой `text` синтезируется
   не для того пользователя — например, кэш модели (`_MODEL_CACHE` в
   `friday/tts.py`) ключуется на `(voice, download_root)`, ОБЩИЙ на все
   тенанты. Это утечка? (Он общий у STT в `whisper.py` тоже — тот же вопрос
   применим и туда, если решишь, что это находка.)
4. **`sanitize_text`/`max_chars=2000` — единственная защита от долгого
   синтеза.** Нет отдельного rate-limit на `speak` сверх обычного
   tool-call-бюджета агентского цикла. Реалистичен ли сценарий отказа —
   пользователь просит озвучить очень длинный ответ (модель сама решает
   текст для `speak`, не пользователь напрямую) достаточно часто, чтобы
   забить CPU?
5. **`_deliver_voice_reply` глотает ЛЮБОЕ исключение** best-effort
   (`friday/telegram_bridge/_callbacks.py`) — сделано намеренно (сбой
   голоса не должен портить успешный текстовый ответ), но проверь: не
   прячет ли это то, что стоило бы видеть оператору (например,
   систематический сбой `sendVoice` из-за неверного формата — сейчас это
   только `LOGGER.warning`, в метрики/`doctor` не попадает).
6. **OGG/Opus кодирование через PyAV** (`friday/tts.py:_encode_opus_ogg`) —
   ресурс: как ведёт себя на пограничных/повреждённых входах (пустой PCM,
   очень короткий текст)? Есть ли тест на это или дыра.

Не переписывай регэкспы/веса/пороги без замера — тот же закон, что везде.
Если находка требует замера (маловероятно для security-обзора, но мало ли) —
объяви критерий до, как обычно. Мутация обязательна для новых тестов. Если
находок нет — так и напиши в `grok/PROPOSALS.md`, это тоже результат.

**Итог G23:** HIGH нет (af620c3/faab13a/11a35b2). Attachment-канал в LLM/audit/
verification не течёт; tts.use@guest как chat.use — осознанный контракт; общий
кэш Piper — публичные веса, как whisper. Soft: metrics для swallow sendVoice,
guard empty PCM. Полный разбор — `grok/PROPOSALS.md` §G23. Код не менял.

---

# СРОЧНОЕ (после G20) — G21: находка состязательного ревью в твоём же G19 — **сделано**

Второй проход ревью (после G17) по всему, что влилось с прошлой проверки —
G18, G19, G20, S8, мой фикс очереди семафора. HIGH-находку в G18 (`/delete`
путал того, кто нажал кнопку, с тем, кто вызвал команду) я уже закрыл сама
(`_commands.py`/`_callbacks.py`, коммит скоро придёт). Эта — твоя, в G19.

## G21. `dismiss_notification` не проверяет `kind='reminder'`, хотя `list_pending_reminders` проверяет — **сделано**

`list_pending_reminders` (`storage/_runtime.py:56-72`) фильтрует `WHERE
user_id=? AND kind='reminder' AND status='pending'`. Парный
`dismiss_notification` (`:74-95`) — только `WHERE id=? AND user_id=? AND
status='pending'`, без `kind`. Маршрут `POST
/api/me/reminders/{id}/dismiss` называет себя reminder-only в докстринге, но
сама SQL пропустит ЛЮБУЮ pending-строку этого же пользователя вне
зависимости от `kind` — `chronicle`, `sentinel`, `reflection`, `onboarding`.

Сегодня это НЕ эксплуатируется (проверено ревью): ни один self-service или
admin маршрут не отдаёт id чужого-kind уведомления обратно арендатору,
`list_pending_reminders` сама уже kind-scoped, `id` — uuid4[:16]. Но это
латентное расхождение контракта с исполнением, и `dismiss` НЕ чистит
`dedup_key` намеренно (для reminder это правильно — блокирует повторный
scan) — если через это когда-нибудь пройдёт id ДРУГОГО kind (лог, будущая
админ-ручка, ручной скрипт), его `dedup_key` будет заблокирован НАВСЕГДА
без возможности переиграть.

**Чем закрыть:** добавить `AND kind='reminder'` в SQL `dismiss_notification`
— тот же охват, что уже есть в `list_pending_reminders`, ничего больше не
менять (сигнатура, self-service контур — не трогай).

Тест: строка `kind='chronicle'` (или любой другой не-reminder) в статусе
pending у того же `user_id` — `dismiss_notification` должна вернуть `False`
(404 на уровне маршрута), не снимать её. Мутация: убери `AND
kind='reminder'` из SQL — тест обязан покраснеть.

Реализовано: `dismiss_notification` SQL + `AND kind='reminder'` (как у
`list_pending_reminders`); тест
`test_dismiss_notification_rejects_non_reminder_kind` (storage False + HTTP
404, reminder всё ещё dismissable). Мутация: без kind-фильтра тест красный.

---

# Новое (после G21) — G22: твои же предложения из `grok/PROPOSALS.md`, решение принято, в очередь не попало — **сделано**

Разбор твоих семи предложений (`grok/PROPOSALS.md`, раздел «Решения по этим
предложениям») дал вердикты, но я не перенесла принятые пункты в этот файл —
отсюда простой. Пункты ниже, в порядке, который я сама же назначила:

1. **№6 первым, если берёшь по одному** — `_audit_details`
   (`execution_kernel/__init__.py`, сейчас около строки 582) отдаёт непустой
   словарь только для `code_run`; у веб-веток (`web_search`/`web_fetch`/
   `web_research`) фингерпринта нет. **Прежде чем чинить — перепроверь на
   HEAD**: возможно уже сделано (в разборе PROPOSALS я сама цитирую эти же
   строки как уже содержащие web-ветки — цитата могла относиться к твоему
   предложению, а не к текущему коду; проверь код, не мой пересказ).
2. **№2 и №7 — одним заходом, не порознь** (моё решение, причина внутри
   PROPOSALS.md): срез `to_llm_message`/`to_dict` с ГОЛОВЫ на большом
   `web_research`-результате, и `_FETCH_TOTAL_BUDGET` веб-фетча (сейчас 60с в
   `friday/web_surfer/__init__.py`) больше потолка ядра в 30с
   (`ExecutionKernel.execute`, `timeout = ... else 30`). **Тоже перепроверь
   сперва**: `_web_research_for_llm` в текущем коде уже бюджетирует по
   источникам (похоже на правку G12) — возможно это уже закрывает №2
   целиком, и открыт только №7 (`_FETCH_TOTAL_BUDGET=60.0` всё ещё выше
   потолка ядра — это я проверила сама, по коду, прямо сейчас).
3. **№3 — PDF в `_ALLOWED_CONTENT_TYPES`**: принято, но нужен твой замер
   первым (порог объяви ДО измерения, десяток настоящих PDF из сети — сколько
   разбирается в осмысленный текст, а не в мусор из лигатур/колонтитулов).
4. №4, №5 — очередь, не бери сейчас (обоснование в PROPOSALS.md: веб-путь
   почти не используется).

Не переписывай сами предложения — они уже подробно расписаны тобой же в
`grok/PROPOSALS.md`, там же файл:строка и сценарий отказа для каждого.
Мутация и полный гейт — как обычно.

---


Реализовано/проверено на HEAD:
1. №6 — уже было: `_audit_details` для web_search/web_research/web_fetch + тесты fingerprint.
2. №2 — уже было: `_web_research_for_llm` делит бюджет по источникам; `test_web_research_context_budget.py`.
3. №7 — уже было: `_RESEARCH_TOTAL_BUDGET`/`_RESEARCH_FETCH_BUDGET`, partial sources.
4. №3 — замер 9/10 (порог ≥7/10 до fetch) → `application/pdf` в allowlist, `DocumentExtractor` in-memory, `parse_timeout` 8 с; `tests/test_web_fetch_pdf.py` (мутация allowlist — красный).
5. №4/№5 — не брались (очередь по решению).

# G20: скачать разговор текстом из Telegram — **сделано**

Из того же исследования: у мейнстримных ассистентов можно выгрузить переписку
файлом. У моста СЕГОДНЯ нет вообще никакой отправки файлов ОТ бота человеку —
только `_send_message` (`telegram_bridge/_transport.py:653`, `sendMessage`
JSON). G18/G19 переиспользовали готовый self-service backend; здесь backend
для получения сообщений уже есть (`get_conversation_messages`,
`storage/_conversations.py`), а вот отправка ФАЙЛА в мост — новая инфра,
маленькая, но настоящая новая.

**Как добавить:** `sendDocument` — не JSON, `multipart/form-data`. httpx это
умеет через `files=`:

```python
async def _send_document(self, client, chat_id, filename, content_bytes, *, caption=""):
    response = await client.post(
        f"{self._api_url}/sendDocument",
        data={"chat_id": chat_id, "caption": caption[:1024]},
        files={"document": (filename, content_bytes, "text/plain")},
    )
    response.raise_for_status()
```

Рядом с `_send_message` (`_transport.py`), добавь в `BridgeShared`
(`_base.py`), как остальные кросс-миксин методы.

**Формат текста:** простой транскрипт — по строке на сообщение, роль +
время + текст, без HTML/markdown-разметки Telegram (это отдельный файл, не
чат-сообщение). `get_conversation_messages(conversation_id, user_id=...,
limit=...)` уже отдаёт всё нужное; для целого разговора подними `limit`
разумно (не «до бесконечности» — реши сама потолок и скажи явно в тексте
файла, если обрезала: «показаны последние N сообщений», как это уже принято
в проекте — молчаливая обрезка запрещена везде, где её ловили).

**Self-service HTTP не обязателен** — эта фича по природе про Telegram
(получить файл), не про JSON-ответ. Но если хочешь единообразия с
G16/G17/G18 — `GET /api/conversations/{id}/export` (гейт
`conversations.read`, тот же паттерн `_resolve_conversation_ref`/`current`
из G18), отдающий `text/plain` напрямую, а команда в Telegram скачивает его
и пересылает через `_send_document`. Выбор дизайна — твой, обоснуй коротко в
коммите.

Telegram: `/export` (текущий разговор, sentinel `current` как у G18).
`BOT_COMMANDS`, `/help`, `tests/test_bridge_surface.py`
(`EXPECTED_COMMANDS`/`EXPECTED_BRIDGE_COUNT`, и `_send_document` — новый
кросс-миксин метод, значит новая строка в `BridgeShared`).

Тест: self-service (чужой разговор — 404); файл содержит все ожидаемые
реплики в порядке created_at; мутация — убрать вызов `_send_document` из
обработчика команды, тест обязан покраснеть, поймав отсутствие вызова (не
проверяй байты файла через реальный HTTP к Telegram — мокай клиента, как
везде в этом файле).

Реализовано: ``GET /api/conversations/{id}/export`` (text/plain, sentinel current,
потолок 500 с явной пометкой обрезки), ``_send_document`` + ``_backend_text``,
Telegram ``/export``. Тесты: ``tests/test_conversation_export.py``.


---

# G19: посмотреть и снять предстоящее напоминание — **сделано**

Из того же исследования: у мейнстримных ассистентов можно посмотреть список
предстоящих напоминаний и снять одно. У Friday «напоминания» — не то, что
человек создаёт руками, а автосканирование `entity_time` (даты, найденные в
документах) органом `reminders` (`friday/organs/reminders/__init__.py`):
`scan_reminders` кладёт push в `outbound_notifications` с `kind="reminder"`
и `dedup_key=f"reminder:{entity_id}:{occurred_at}"`. Это важно для формы
задачи — «отменить» здесь значит не удалить строку, а официально её снять,
и это НЕ одно и то же.

**Грабля, которую я уже нашёл, не наступай сама:** `dedup_key` уникален только
частичным индексом `uq_outbound_dedup ON outbound_notifications(user_id,
dedup_key) WHERE dedup_key <> ''` (`storage/_base.py:744`). Если строку
просто DELETE — уникальность снимается вместе со строкой, и следующий скан
`scan_reminders` (раз в `reminders_poll_interval_sec`) заведёт то же
напоминание заново, потому что `enqueue_notification`'s `INSERT OR IGNORE`
больше не на что натыкаться. Смотри, как уже решена симметричная задача
(`storage/_runtime.py:71-83`, `release_undeliverable_notifications` или
похожий метод рядом) — там `status='failed', dedup_key=''` СНИМАЕТ дедуп
специально, чтобы дать органу поднять вопрос снова. Тебе нужно ОБРАТНОЕ:
`status='dismissed'` (новое значение, не 'sent'/'failed'/'pending') БЕЗ
очистки `dedup_key` — тогда партиальный индекс продолжает блокировать
повторную вставку того же `dedup_key` навсегда, как уже происходит для
`status='sent'` (`storage/_runtime.py:95`, dedup_key там тоже не чистится).

**Вторая грабля:** `list_pending_notifications` (`storage/_runtime.py:48`) не
принимает `user_id` вовсе — она для внутреннего drain-цикла моста и отдаёт
ВСЕХ пользователей. Для self-service нужен НОВЫЙ метод с обязательным
`user_id`, иначе одна ручка утечёт чужие напоминания.

**Что сделать:**
- `list_pending_reminders(user_id, *, limit=...)` в `storage/_runtime.py`
  (или `_conversations.py`, где логичнее) — `WHERE user_id=? AND kind='reminder'
  AND status='pending'`.
- `dismiss_notification(user_id, notification_id)` — `UPDATE ... SET
  status='dismissed' WHERE id=? AND user_id=? AND status='pending'` (tenant
  через `user_id` в WHERE, не только через приложение).
- Self-service HTTP: `GET /api/me/reminders`, `POST
  /api/me/reminders/{id}/dismiss` (гейт — подбери существующий capability
  для собственных уведомлений, если такого нет, `chat.use` сгодится как и у
  `/api/me/instructions`).
- Telegram: команда со списком (текст напоминания уже человекочитаемый,
  `_format_reminder`) и inline-кнопкой «Снять» на каждую строку — паттерн
  списка с кнопками уже есть у `/conflicts`/`/merges`, повтори.
- `status='dismissed'` нигде раньше не встречался — проверь, что
  `list_pending_notifications` (внутренний drain) и любой другой код,
  фильтрующий `status='pending'` жёстко, не подхватит снятые по ошибке (не
  должен, раз фильтр именно `='pending'`, но убедись явно тестом).

Тест: снятое напоминание не появляется повторно после ИМИТАЦИИ следующего
скана (`scan_reminders` с тем же `dedup_key` — `enqueue_notification` должен
вернуть `False`); self-service (чужой `notification_id` — 404, не 403);
мутация на то, что `dismiss_notification` НЕ чистит `dedup_key` (если
случайно скопируешь паттерн `release_undeliverable_notifications` и
очистишь его — тест обязан покраснеть, поймав возврат напоминания).

Реализовано: `list_pending_reminders` / `dismiss_notification` (status=dismissed,
dedup_key сохраняется), `GET/POST /api/me/reminders[...]`, Telegram `/reminders` +
кнопка «Снять» (`remind:dismiss`). Тесты: `tests/test_reminders_self_service.py`.


---

# G18: три готовых self-service ручки без команды в Telegram — **сделано**

Отдельное исследование (не аудит кода на баги — сравнение с тем, что есть у
любого мейнстримного ассистента) нашло: backend для управления разговором
почти весь уже готов и self-service (гейт `conversations.manage`, уже выдан
`user`-пресету), просто ни одна ручка не вызывается из Telegram — а это
главный интерфейс проекта. Три штуки, каждая маленькая, независимая от других
— бери в любом порядке, но не сразу все в один коммит, чтобы гейт между ними
оставался зелёным для Sol.

**Общий кусок для всех трёх:** резолв «текущего разговора этого чата» уже есть
как паттерн — `GET /api/conversations/channel/why`
(`friday/api/conversations.py:32-50`) резолвит `channel`/`channel_id` в
`conversation_id` через `state.storage.get_channel_session(actor.user_id,
"telegram", channel_id)`. Команды ниже читают ЭТОТ conversation_id (текущий
чат = текущий разговор), не принимают чужой id аргументом.

## G18a. `/archive` — архивировать текущий разговор

`POST /api/conversations/{id}/archive` уже существует (`conversations.py:149`,
гейт `conversations.manage`, self-service). Команда без аргумента, обычный
паттерн `_backend_json` + `_send_message` с подтверждением.

## G18b. `/delete` — удалить текущий разговор (с подтверждением)

`DELETE /api/conversations/{id}` уже существует (`conversations.py:160`, тот
же гейт) — это HARD delete (сообщения, feedback, сама запись, каскадом в одной
транзакции, `storage/_conversations.py:67-106`), не soft. Нужен шаг
подтверждения — в кодовой базе уже есть паттерн Да/Нет inline-кнопок для
`/merges` и разбора Inbox (`_commands.py`/`_views.py`, найди и повтори), не
удаляй по одной команде без подтверждения.

## G18c. `/rename текст` — переименовать текущий разговор

Тут ручки ЕЩЁ НЕТ вообще, не только в Telegram — ни метода в хранилище, ни
HTTP-маршрута. Добавь `ConversationsMixin.set_conversation_title(conversation_id,
user_id, title)` в `storage/_conversations.py` (короткий, по образцу
`set_conversation_mode`, тот же файл), `PATCH /api/conversations/{id}` с телом
`{title}` в `friday/api/conversations.py` (тот же гейт `conversations.manage`,
образец — маршрут archive рядом), затем команду в Telegram.

**Для всех трёх:** `BOT_COMMANDS`, `/help`, `tests/test_bridge_surface.py`
(`EXPECTED_COMMANDS`/`EXPECTED_BRIDGE_COUNT`) и `tests/test_route_inventory.py`
(`EXPECTED_OPERATIONS` — только у G18c новый HTTP-маршрут, у a/b маршруты уже
есть). Тест на каждую: self-service (только свой `user_id`, чужой разговор не
трогается — 404, не 403, тем же способом, что уже работает у archive/delete),
мутация на сам факт вызова нужного storage-метода.

---

# G17: две находки состязательного ревью в /regenerate — **сделано**

Я прогнал воркфлоу состязательного ревью по всему, что влилось в main сегодня —
шесть независимых рецензентов, каждая находка отдельно проверена на опровержение
(default: считать опровергнутой, если механизм не подтверждён чтением кода живьём).
Пять находок выжили опровержение, две — в твоём `POST /api/me/regenerate`. Обе
подтверждены двумя разными агентами независимо чтением текущего кода, не диффа.

## G17a. Нет защиты от гонки — два одновременных `/regenerate` дублируют ход

`regenerate_last_turn` (`server.py`, вокруг строки 1203) зовёт `state.agent.chat(...)`
напрямую, без `state.storage.idempotency_claim` — тогда как `/api/chat` именно для
этого класса гонки его использует (`server.py`, блок «Claim the key atomically
before any side effect», ~1331-1367; сигнатура — `storage/_runtime.py:142`,
`idempotency_claim(user_id, request_key, *, request_hash="", lease_seconds=300)`,
статусы `replay`/`conflict`/`in_progress`/`claimed`). `AgentRuntime.chat` не имеет
своей блокировки: читает `prior_history`, затем безусловно `store_message(...)`
(`agent_runtime/__init__.py:466`) — обычный INSERT без CAS. `/retry` в Telegram шлёт
пустое тело без `source_ref`, так что ключ взять неоткуда естественным путём —
телеграм-очередь маскирует гонку сериализацией апдейтов одного чата, но
`/api/me/regenerate` — обычная `chat.use`-ручка, любой клиент с bearer-токеном
зовёт её напрямую (доказано твоим же `test_regenerate_accepts_explicit_conversation_id`).

**Сценарий отказа:** двойной тап по кнопке «Ещё раз» до того, как интерфейс её
задизейблил, или retry-on-timeout у HTTP-клиента — оба запроса проходят проверку
владения, оба читают пересекающуюся историю, оба дописывают user+assistant пару;
если агент по пути вызовет инструмент с побочным эффектом (напоминание, правка
графа) — эффект сработает дважды.

**Чем закрыть:** ключ идемпотентности внутри самого `regenerate_last_turn`, без
участия клиента — `request_key = f"regenerate:{conversation_id}"`, короткий lease
(секунд 60-90, одного хода агенту должно хватить с запасом); `request_hash` можно
не считать (пустая строка допустима по сигнатуре) — тело запроса не варьируется
содержательно, дедуп нужен именно по факту «этот разговор уже регенерируется».
При `status in ("in_progress", "replay")` — вернуть осмысленный ответ (409 или
закэшированный `response`), не звать `agent.chat` второй раз. Не забудь
`idempotency_complete` после успешного ответа — иначе lease никогда не освобождается.

## G17b. Регенерация хода с вложением молча теряет обоснование ответа, без единого слова об этом

`regenerate_last_turn` жёстко ставит `attachments=[]` (осознанно, как написано в
твоём же комментарии над функцией) и переигрывает только `message.content` из БД.
Но `AgentRuntime.chat` вообще не сохраняет метаданные вложений на строке
сообщения — `store_message(conversation_id, user_id, "user", clean_message)`
(`agent_runtime/__init__.py:466`) зовётся без `metadata`, хотя `attachments`
в этот момент уже в области видимости (параметр функции). Значит `/regenerate`
СТРУКТУРНО не может узнать, было ли у оригинального хода вложение — не то что
переслать его, а хотя бы предупредить.

Два конкретных случая, почему это хуже обычного «другой ответ»:
- `explicit_no_save`-путь (`ingestion/_files.py:729`, `inspect_file_transient`):
  байты файла НИКОГДА не попадают в Raw Objects/Inbox/граф — только временная
  выжимка на один ответ. Материал, на котором строился первый ответ, физически
  невосстановим НИКАКИМ способом. Регенерация такого хода — не «другой взгляд»,
  а гадание с нуля под видом переответа.
- Документ без подписи получает синтетический текст `"Загружен документ:
  {filename}"` (`/api/chat`) — именно это и переиграется дословно, без единого
  байта самого файла и без `attachment_names` в контексте (тот заполняется
  только `if attachments:`, `agent_runtime/__init__.py:1303-1308`). Модель отвечает
  на «загружен документ» вообще не видя документа.

Ни HTTP-ответ, ни текст в Telegram (`_format_response_message`) сегодня не говорят
об этом ни слова.

**Чем закрыть:** 1) в `AgentRuntime.chat` передать в `store_message` метаданные
о наличии вложений на исходном ходе (например `metadata={"had_attachments": True,
"attachment_count": len(attachments)}` если `attachments` непусты — минимум,
без содержимого файлов, только факт); 2) в `regenerate_last_turn` прочитать эту
метку у найденного `last_user`-сообщения и, если она стоит, добавить в ответ явную
пометку («ответ восстановлен без исходного вложения — ...» или похожим текстом,
по образцу существующих `grounding_warning`/`citation_notice` в
`_format_response_message`, `telegram_bridge/_callbacks.py:378-409`).

Тест на обе: гонка — два конкурентных вызова `regenerate_last_turn` на один
`conversation_id`, второй должен НЕ вызвать `agent.chat` второй раз (мутация:
убери `idempotency_claim` → тест обязан покраснеть, поймав двойной вызов).
Вложение — сообщение с `attachments` непустыми, затем `/regenerate` того же
хода, ответ должен нести пометку об утраченном обосновании; без вложений —
пометки нет (мутация: убери метку из `store_message` → тест на присутствие
предупреждения обязан покраснеть).

---

Твоя половина — точная: исполнение решённых задач, тесты с проверкой мутацией,
согласованность доков с кодом. Четыре задачи, все с готовым решением или готовым числом.

---

## G1 (#43). У веб-инструментов нет следа в аудите — **сделано**

`_audit_details` пишет отпечатки для `web_search` / `web_fetch` / `web_research`
(sha256 + длина; для fetch — host, без path/query). Тест на helper и на боевой
`execute` (мутация: пустой dict для web_search → падает).

## G2 (#44). Порог сборщика эталонов 1 → 2 — **сделано**

`MAX_SHARED_TOKENS = 2` с таблицей замера 2026-07-31 в комментарии. Граничный тест:
ровно 2 общих стемма проходят, 3+ — нет.

## G3 (#45). 25 сохранено, 22 в замере — **сделано**

Причина: `save_accepted` считал итерации цикла, а `add_eval_case` upsert'ит по
`(user_id, query)` — дубликаты запросов (в т.ч. после повторного audit списка)
схлопывались. Теперь: casefold-дедуп, счётчик = число distinct. `run_eval` отдаёт
`listed` / `scored` / `skipped_empty_expected`, чтобы потеря была слышна.

## G4 (#48). 200 конфликтов и 20 слияний не разобрать из чата — **сделано**

`/merges` уже был. Добавлено:
- `GET /api/kg/conflicts`, `POST /api/kg/conflicts/{id}/decide`
- Telegram `/conflicts` + callback `conflict:keep_a|keep_b|dismiss`
- инструменты `conflict_list`, `conflict_decide`, `entity_merge_decide`
- порция 5; решённые (status ≠ suggested) не показываются снова

---

# Новое (добавлено 31 июля, после того как все четыре были закрыты)

Все G1–G4 приняты. G3 ты раскопал глубже, чем стояло в задаче: я записал «три эталона
потерялись», а причина оказалась не в потере — `save_accepted` считал итерации цикла,
тогда как `add_eval_case` делает upsert по паре (пользователь, запрос). Правильно.

## G5 (#50). Работа с графом стоит на ПОЛНОЙ выборке сущностей, а выборка кончается — **сделано**

Снята зависимость от `list_entities(limit=5000)` на путях точного упоминания:

- `match_mentions` / `_entity_suggestions` / `backfill_entity_mentions` — кандидаты из
  текста (`mention_phrase_candidates`: n-граммы токенов до 12 слов, многословные имена
  без объявляющего слова сохраняются), lookup
  `find_entities_by_normalized_names` (+ alias без потолка страницы);
- `search_entities` token-overlap — `iter_entities` (полный обход страницами, без
  тихого обреза хвоста алфавита);
- `find_entity_by_alias` больше не ходит через `list_entities(limit=5000)`.

Тесты: `tests/test_entity_match_not_capped_by_listing.py` (оба конца алфавита при
графе >5000, запрет `list_entities` на exact-path, мутация пустого keyed lookup).
Старый `test_entity_suggestions_see_the_whole_graph` остаётся регрессией.

---

# Новое (31 июля, вечер) — G5 принята, дальше вот это

## G6 (#51). Слияние сущностей НЕЧЕМ ОТКАТИТЬ, и это дыра в безопасности — **сделано**

При `merge_entities` в `entity_merge_history.transfer_json` пишется состав
перенесённого: `links_moved` (с `target_link_id`), `links_suppressed` (пересечение
документов — без этого откат врёт), `primary_moved`, `relations` с fate
`moved` / `suppressed_duplicate` / `self_loop_dropped`. `unmerge_entities` возвращает
обе стороны и связи к состоянию до слияния; повторный откат запрещён. Схема 19:
колонки `transfer_json`, `undone_at`, `undone_by`.

Поверхности: `POST /api/kg/merges/{id}/undo`, `GET /api/kg/merges`, то же под
`/api/admin/merges` (`admin.all_data.manage` / read), инструмент `entity_merge_undo`
(`kg.merge`). Тест `tests/test_entity_merge_can_be_undone.py` — пересекающиеся
документы + мутация обнуления `links_suppressed`.

---

# Новое (31 июля, вечер второй раз) — G6 принята

Откат слияния с записью состава перенесённого — именно то, что было нужно, и трудность
с `INSERT OR IGNORE` ты снял правильно. Фикстуру схемы 19 тоже добавил сам, до того как
я успел сказать. Хорошо.

⚠️ Но на будущее: между твоим пушем `0cda590` и фикстурой `main` был **красный** — гейт
падал на `test_the_fixture_set_covers_every_schema_back_to_the_oldest_backup`. Схема
бампится и фикстура добавляется ОДНИМ коммитом, иначе у всех троих ломается прогон.

## G7 (#54). Нерусский текст утекает пользователю на экран — **сделано**

Все статические и f-string `detail=` в `admin_api/` и `api/` переведены на русский
(путь toast: `api()` → `Error(data.detail)` → `toast`). Инвентарь
`tests/test_user_facing_details_are_russian.py`: кириллица обязательна у литералов;
43 динамических `str(exc)` перечислены явно (EXPECTED_DYNAMIC_DETAIL_SITES), молча
не пропускаются. Закреплённые тесты hardening/lifecycle обновлены под новые строки.

## G8 (#55). Источники в ответе не раскрываются до самого документа — **сделано**

Под ответом с цитатами — кнопки `doc:show:{knowledge_id}` с метками K# (тот же
callback, что у поиска/browse/хроники). В тексте: «Кнопкой ниже — открыть источник
целиком.» В админке у легенды источников в истории диалога — кнопка «Открыть» →
`inspectKnowledge`. Тесты: `test_answer_sources_open_as_documents_from_the_legend`.

---

# Новое (31 июля, поздний вечер) — G7+G8 приняты, сканер open-задач тоже

## G9 (#56). Разметить очередь конфликтов: очевидное vs требует внимания — **сделано**

`friday/conflict_triage.py`: Jaccard по `content_tokens` (стемы), отношение длин,
доля data-diff (заглавная/цифра). Метки `likely_duplicate` /
`likely_different_records` / `uncertain` — только подсказка, `conflict_decide` и
порог 0.95 не тронуты. Поверхности: `conflict_list`, `GET /api/kg/conflicts`,
Telegram `/conflicts` (строка «Метка: …»). Тест
`tests/test_conflict_queue_triage_hints.py` — бланк vs дубликат, HTTP, tool, TG.

---

# Новое (31 июля, вечер) — G9 принята

Подсказки triage и автопилот вотчера — оба в дело, живой экземпляр перезапущен с
твоим кодом. Хорошо, что довёл до `main`, а не остановился на отчёте.

## G10 (#39). Вопрос про человека уходит в болтовню — переизмерить, условие сменилось — **сделано**

Переизмерено на сегодняшнем коде (`graph_expansion=False`). Классификатор и порог
0.35 **не менялись** — отрицательный результат: болезнь ушла вместе с поиском.

**Критерий до замера:** формы из переписки/фикстур при живом досье →
`personal_knowledge|mixed` и `hits>0`; настоящая болтовня → `general_conversation`
при пустых hits.

**Числа (синтетический корпус + формы из регрессий, `grok/measure_g10_answer_mode.py`):**

| стенд | результат |
|---|---|
| person standalone | **8/9** (промах — опечатка «Кирила», hits=0 → поиск, не классификатор) |
| person follow-up («а его…», «её…») | **3/3** |
| chitchat | **8/8** `general_conversation` |

Замечание: «давай про Макарова Кирилла инфу» даёт `mixed` (conf 0.342 < 0.35), но
hits=6 — это не болтовня. Ночной провал был `general_conversation` + hits=0.

Сторож: `tests/test_person_query_answer_mode_remeasure.py` (мутация: всегда
`general_conversation` при hits → падает; пустые hits → `personal_knowledge` → падает).

---

# СРОЧНО (31 июля, ночь) — реальная дыра доступа, найдена разведкой и проверена мной лично

## G11 (#57). Делегированный админ может писать/удалять/выгружать аккаунт ВЛАДЕЛЬЦА — **сделано**

`_protect_owner_target` подключён на всех мутирующих admin-маршрутах с tenant
`user_id`: export, knowledge write/restore/delete/reenrich/links/suggestions,
graph entities/relations, lifecycle, conflicts/resolutions/merges undo, inbox
classify/bulk/advise, eval cases. READ не трогали (заказанная видимость).

Сторож: `tests/test_owner_mutation_boundaries.py` — точечный `POST /exports` +
инвентарный обход manage/export с `user_id=<owner>` → 403; мутация (no-op
protect на export) → 200.

**Это не гипотеза.** Я проверил каждый файл лично, читал код построчно. Самое
серьёзное — делегированный администратор (пресет `admin`, право `admin.export`,
обычная делегируемая роль уровня 3, выдаётся владельцем как рядовая роль) может
скачать **весь личный архив владельца** одним запросом: 150+ МБ, документы,
переписка, сущности. Owner-проверки там нет вовсе.

**Механизм, который должен защищать, уже есть и работает — просто не везде
подключён.** `_protect_owner_target(request, user_id)`
(`admin_api/_deps.py:174-181`): если целевой `user_id` принадлежит владельцу
(`preset_key == "owner"` или легаси id), а актёр не владелец — 403. Используется в
`_users.py`, `_conversations.py`, и ровно ОДИН раз в `_knowledge.py` (только
`purge_knowledge_endpoint`, строка 907). Больше НИГДЕ, хотя `admin.all_data.manage`
и `admin.export` — обычные делегируемые права, а не владельческие.

**Прецедент в тесте, который уже был:** `tests/test_mission_oversight_boundaries.py`
описывает этот же класс дыры, уже найденный и починенный для роутера миссий — «a
delegated administrator could stop the owner's own missions… Token revocation was
hardened against exactly this; missions were missed». Историю повторили — в шести
файлах сразу.

### Точный список маршрутов без защиты (проверено мной, файл:строка = где резолвится user_id)

**`admin_api/_maintenance.py` — САМОЕ СЕРЬЁЗНОЕ, экспорт целого архива:**
- `POST /exports` (:106) → `user_id = str(body.get("user_id") or request.state.actor.user_id)` (:128)

**`admin_api/_knowledge.py` — `_protect_owner_target` уже импортирован (:25), не хватает вызова в:**
- `PATCH /knowledge/{id}` (:787) — **переписывает title/content/summary владельца**
- `POST /knowledge/{id}/restore` (:827)
- `DELETE /knowledge/{id}` (:870)
- `POST /knowledge/{id}/entities` (:672)
- `PATCH /entity-links/{id}` (:744)
- `POST /containers` (:158) — если принимает user_id, проверь
- `POST /knowledge/{id}/reenrich` (:289) — если принимает user_id, проверь
- `POST /entity-suggestions/groups/decide` (:520)

**`admin_api/_graph.py` — импорта `_protect_owner_target` нет вовсе:**
- `POST /entities` (:65), `PATCH /entities/{id}` (:86), `DELETE /entities/{id}` (:104)
- `POST /relation-candidates/bulk-review` (:174), `POST /relation-candidates/{id}/review` (:224)

**`admin_api/_lifecycle.py` — импорта нет:**
- `POST /cleanup/legacy/apply` (:66) — `user_id = str(body.get("user_id") or "")` (:69), до 200 объектов, включая `soft_delete`
- `POST /lifecycle/apply` (:172) — `user_id` на :177
- `POST /lifecycle/deprecate` (:267) — `user_id` на :278

**`admin_api/_conflicts.py` — импорта нет ВО ВСЁМ ФАЙЛЕ:**
- `POST /conflicts/bulk-review` (:66), `POST /conflicts/{id}/review` (:118), `POST /conflicts/{id}/resolve` (:140)
- `POST /knowledge/detect-duplicates` (:191) — через `_target_user`, там же добавить
- `POST /resolutions/detect` (:213), `POST /resolutions/{id}/accept` (:235), `POST /resolutions/{id}/reject` (:253)
- `POST /merges/{id}/undo` (:281) — твой же G6, свежая дыра в свежем коде

**`admin_api/_inbox.py` — импорта нет:**
- `POST /inbox/{id}/classify` (:112), `POST /inbox/bulk` (:145)
- `POST /inbox/{id}/advise` (:231) — проверь, мутирует ли; если только читает LLM без записи — не нужно

### Что НЕ трогать
- `/api/kg.py` (не `admin_api/`) — self-service роутер, все операции идут по
  `actor.user_id`, чужого `user_id` не принимает. Я проверил — не уязвим, не лезь.
- READ-маршруты (`_require(request, "admin.*.read")`) — они уже покрыты
  `_audit_cross_tenant_read`, это другая, работающая защита (видимость, не запись).
  Владелец САМ решил, что делегированный админ видит всё чужое содержимое — это
  заказанная фича (см. память `multiuser-isolation-and-oversight`), а не дыра.
  Трогать только МУТИРУЮЩИЕ маршруты.

### Как чинить
Паттерн уже есть в проекте — скопируй из `admin_api/_users.py` или
`purge_knowledge_endpoint` (`_knowledge.py:904-908`): resolve `user_id`, затем
`_protect_owner_target(request, user_id)` СРАЗУ ПОСЛЕ резолва, ДО первого чтения
или записи в хранилище. В файлах без импорта — добавь `_protect_owner_target` в
`from friday.admin_api._deps import (...)`.

### Сторожевой тест — ОБЯЗАТЕЛЕН, и он важнее самих правок

Точечные тесты на каждый маршрут — этого мало: следующий новый маршрут повторит
дыру снова, ровно как повторилась дыра из `test_mission_oversight_boundaries.py`.
Нужен **инвентарный** тест по образцу `test_route_inventory.py` /
`test_audit_hardening.py`: обойти все POST/PATCH/DELETE маршруты `admin_api/`,
которые принимают `user_id` (из тела или query) и требуют `admin.*.manage`/
`admin.export` (не `.read`), засеять для каждого владельца + делегированного
админа-неовнера, вызвать с `user_id=<владелец>` и проверить 403.

Маршрут, недостижимый обходом (нестандартная сигнатура), обязан **сообщить о
себе** явным списком «непроверено», а не молча выпасть — тот же урок, что уже
был у `test_audit_hardening` с плейсхолдерами `{...}` в пути.

**Мутация обязательна**: закомментируй один вызов `_protect_owner_target` —
инвентарный тест обязан покраснеть ИМЕННО на этом маршруте, а не просто упасть
где-то. Плюс отдельный юнит-тест на сам `POST /exports` — самый тяжёлый случай,
проверь его руками, а не только инвентарём.

### Приоритет

Это выше всего остального в очереди. Гейт, коммит, пуш — как обычно, но эту
задачу не делить на порции, доводи до конца одним заходом: половина защищённых
маршрутов хуже, чем ни одного — создаёт ложное чувство, что дыра закрыта.

---

# Новое (пока G11 в работе — доразобрал остаток разведки, проверил сам)

## G12 (#59). Два мелких, но реальных дефекта из разведки — низкий приоритет, но не выдумка — **сделано**

### 1. `_user_knowledge_search` — асимметричный клэмп лимита

`execution_kernel/__init__.py:1276-1278`:

```python
found = (
    await self.searcher.search(chosen.user_id, clean_query, limit=max(1, min(int(limit), 20)))
    if self.searcher is not None
    else {"results": storage.search_knowledge(chosen.user_id, clean_query, limit=limit)}
)
```

Ветка с `self.searcher` клэмпит `limit` в `[1, 20]` (совпадает с объявленной схемой
инструмента, `"limit": {"maximum": 20}`). Ветка `else` — нет, сырой `limit` летит в
`storage.search_knowledge` напрямую. Инструмент отдаёт содержимое ЧУЖОГО корпуса
(гейт `admin.all_data.read`), поэтому лимит — не мелочь.

Проверил: сегодня в проде это мёртвая ветка — `server.py:774`
(`kernel.bind_services(..., searcher=searcher)`) всегда передаёт настоящий
`searcher`, `self.searcher` в бою никогда не `None`. Но контракт «схема — верхняя
граница» не гарантирован архитектурно — `execute()` не проверяет параметры по
объявленной JSON-схеме вовсе, каждый обработчик клэмпит сам, и этот забыл. Почини
just этот метод (клэмпни в else-ветке так же, `max(1, min(int(limit), 20))`) —
общую валидацию по схеме заводить не нужно, это отдельный архитектурный вопрос
за рамками находки.

Тест: вызови с `limit=999` через путь без `searcher` (замокай/подставь
`self.searcher = None`), убедись, что дошедший до `storage.search_knowledge` лимит
клэмпнут. Мутация — убери клэмп, тест обязан покраснеть.

### 2. `web_surfer/__init__.py:658` — `except BaseException` вместо `except Exception`

```python
try:
    fetch_result = task.result()
except BaseException:
    failed_sources += 1
    continue
```

Единственное место в проекте, где так поймано — везде рядом принят паттерн
`except asyncio.CancelledError: raise` перед `except Exception` (сверь
`workers/__init__.py:427-428`, `telegram_bridge/_transport.py:513-514`). Здесь
`BaseException` глотает и `CancelledError` — при отмене родительской задачи
(таймаут, остановка воркера) один упавший источник исследования проглотит сигнал
отмены молча вместо того чтобы дать ему распространиться.

Почини по образцу соседних мест: `except asyncio.CancelledError: raise`, потом
`except Exception:` с тем же телом. Тест: замокай `task.result()` так, чтобы
бросало `asyncio.CancelledError`, убедись, что оно долетает наружу из
`WebSurfer.research()`, а не тонет в `failed_sources`.

Обе находки я проверил сам построчно (не пересказ разведки) — низкий приоритет,
бери после G11.

---

# Новое (после G12) — два хвоста моей же сегодняшней фичи авторегистрации

Я сделал `jericho backfill-entities` -- нет, не то. Сделал авторегистрацию
(`fe1f810`, `FRIDAY_TELEGRAM_OPEN_REGISTRATION`) и по пути задумал, но не успел
дописать две вещи. Оба — прямое следствие правила проекта «максимум функционала
в Telegram», и оба про один и тот же момент: человек только что впервые написал
боту.

## G13 (#60). Владелец не узнаёт, что кто-то самозарегистрировался — **сделано**

Сейчас единственный способ узнать о новом `newcomer`-аккаунте — вручную зайти в
список пользователей. При включённой открытой регистрации бот открыт для
незнакомцев (в приватных чатах), и владелец должен видеть, кто пришёл, не
угадывая.

Место: `server.py`, там же, где выбирается `preset_for_new_account` и вызывается
`_ensure_newcomer_preset` (после моего сегодняшнего коммита `fe1f810`, ищи
`elif in_private_chat:`). Именно там известно, что аккаунт СОЗДАН впервые
(`existing` — `None`, `linked_id` — `None`, ветка `else` у `if linked_id:` чуть
ниже).

Как отправить: `storage.enqueue_notification(user_id, chat_id, body, kind=,
dedup_key=)` — образец есть в `organs/sentinel/__init__.py:144` и
`organs/reminders/__init__.py:61`. Владельца находишь через
`settings.telegram_owner_chat_ids` (список, отправь во все); `resolve_chat_id`
(`organs/__init__.py:112`) дан для чужого поиска чата по `user_id`, тут он не
нужен — chat_id владельца уже есть готовым в настройках. `dedup_key` обязателен
(`f"onboarding:{user_id}"` — на аккаунт ровно одно уведомление, не на каждый
рестарт супервизора).

Текст — display_name/username (уже есть в этом блоке `server.py`), без
содержимого переписки. Тест: создать нового `newcomer`-аккаунта через
`_bridge_get`/`/api/me` (образец — мои же тесты `test_open_registration_*` в
`tests/test_api_vertical_slice.py`), проверить строку в
`outbound_notifications` с `chat_id` владельца. Мутация: убери постановку в
очередь — тест обязан покраснеть.

## G14 (#61). `/start` не говорит новичку, что ему доступно не всё — **сделано**

`telegram_bridge/_commands.py:201-211` — один и тот же текст `/start` для всех:
владельца, старого участника, свежего `newcomer`. Новичок узнаёт про
ограничения (нет `/mission`, нет запуска кода) только когда попробует и
получит отказ — неприятный первый опыт, и не по правилу проекта (человек
должен понимать, что ему доступно, не через отказ).

`register_backend_user()` в этой же функции уже зовёт `/api/me` — используй
ответ (`preset_key` в нём есть, смотри что возвращает `server.py`'s
`/api/me`) и веди текст по ветке: `newcomer` получает то же приветствие плюс
короткую строку про режим (не сочиняй формулировку сама, она должна быть
фактической: чат, файлы, веб-поиск — без миссий и кода, что владелец может
это расширить). Владелец и уже существующие 'user'/'guest' — приветствие как
было, без изменений (мутация должна ловить именно ЭТУ границу — не тронь
существующие тесты `/start` для не-newcomer).

Тест: `/start` от нового `newcomer` содержит упоминание ограничения; `/start`
от статически разрешённого приватного чата — байт-в-байт прежний текст (сверь
с уже существующим тестом на `/start`, если такой есть — `grep` по проекту).

---

# Новое (после G13/G14)

## G15 (#62). Команда «ещё раз» для последнего ответа — **сделано**

POST /api/me/regenerate (self-service, chat.use): резолв conversation_id как у
/api/chat для Telegram, хвост get_conversation_messages(limit=4), последнее
role=user, повторный agent.chat с attachments=[] и ingestion_result=None.
Telegram /retry + BOT_COMMANDS + /help. Ветвление ответов в storage нет —
дописывается новый ход (осознанно). Тесты: tests/test_regenerate_last_turn.py
(последний user, не более ранний; вызов agent.chat; пустой разговор → 400).

---

# Контекст назначения G15 (текст раздачи)


Человеческая сторона: у любого мейнстримного чат-ассистента есть кнопка/команда
«сгенерировать ответ заново», когда первый не устроил. У Friday этого нет вовсе
— неудачный ответ можно только переспросить своими словами, теряя точную
формулировку вопроса. Я сегодня закрыл похожую по духу вещь (своё пожелание о
стиле, `/instructions`) — это соседняя, тоже маленькая и тоже self-service.

**Решение, уже продуманное, реализуй как есть:**

Новый эндпоинт `POST /api/me/regenerate`, self-service, гейт `chat.use` (тот же
паттерн, что `PATCH /api/me/instructions`, добавленный мной сегодня в
`server.py` перед `/api/chat` — смотри его как образец self-service ручки).
Тело: необязательный `conversation_id` (как у `/api/chat`); если не пришёл —
резолвится ТЕМ ЖЕ способом, что `/api/chat` резолвит его для Telegram
(`server.py:1401-1411`, через `state.storage.get_channel_session(actor.user_id,
"telegram", str(channel_chat_id))`, `channel_chat_id =
getattr(request.state, "bridge_chat_id", None)`) — вынеси этот резолв в
маленький общий helper, если сможешь сделать это без риска задеть сам
`/api/chat` (не обязательно, дублирование в этом объёме не грех).

Дальше: `state.storage.get_conversation_messages(conversation_id, user_id=...,
limit=4)` (хронологический хвост, порядок уже гарантирован — см. докстринг
метода), взять последнее сообщение с `role == "user"`. Если такого нет
(разговор пуст или последним был не человек, что не должно случаться, но
проверь) — 400 с понятным текстом. Дальше вызвать `state.agent.chat(...)` СНОВА
с тем же текстом, тем же `conversation_id`, `attachments=[]`,
`ingestion_result=None` (текст не новый — повторно ингестить его `/api/chat`
уже ингестил при первом ходе, второй раз не нужно), `kg=state.kg`,
`hybrid_searcher=state.hybrid_searcher`. Верни то же, что возвращает
`/api/chat` (результат `agent.chat` целиком).

**Осознанное упрощение, не пытайся сделать иначе:** хранилище сообщений
(`storage/_conversations.py`) не умеет ветвление/альтернативные ответы на один
ход — `store_message` внутри `agent.chat()` допишет ЕЩЁ ОДНУ запись с
`role="user"` и тем же текстом, а не заменит прежний ответ. Это осознанная
цена: правка модели ответов ради альтернативных веток — отдельная и большая
задача, не эта. Просто не выдавай за реализованное то, чего нет: если у хода
были вложения (`attachments`), они не переотправляются — ответ будет заметно
отличаться, и это ожидаемо для первой версии, а не баг.

В Telegram — команда `/retry` без аргумента (без параметров: он ретраит
ПОСЛЕДНИЙ вопрос, а не произвольный). Обработчик — `telegram_bridge/_commands.py`,
рядом с `/instructions`, тем же паттерном `register_backend_user()` +
`_backend_json`. Добавь в `BOT_COMMANDS` (`telegram_bridge/_base.py`) и в текст
`/help`.

**Гейт целиком, включая `tests/test_bridge_surface.py` и `tests/test_route_inventory.py`
(EXPECTED_COMMANDS/EXPECTED_BRIDGE_COUNT/EXPECTED_OPERATIONS ОБА файла — сегодня
я нашёл и закрыл давнюю находку: `test_bridge_surface.py` был случайно подменён
содержимым `test_route_inventory.py` чужим слиянием и полгода молчал; теперь это
два РАЗНЫХ файла с разными проверками, оба нужно двигать при смене поверхности).**
Мутация обязательна: тест должен падать, если убрать вызов `agent.chat` внутри
`/regenerate`, и должен падать, если endpoint случайно возьмёт НЕ последнее
сообщение (например, если в разговоре два вопроса подряд без ответов между ними
— редко, но `limit=4` и фильтр по роли должны это пережить корректно, проверь
тестом с такой раскладкой).

---

## Общее

- Гейт целиком до пуша, `git pull --rebase` перед ним — в `main` пишут трое.
- Мутация обязательна для нового теста.
- Задачи Sol (#39, #42, #46, #47) не бери — столкнётесь в одном файле.
- Не трогай `sol/`. Нужно что-то сказать Sol — пиши в своём `PROPOSALS.md`.

## Три находки из старых записей закрылись сами — не переделывай

Проверил сегодня по коду, в прежних списках они числятся открытыми:

- `ingestion/_review.py` — повторная классификация плодила новый объект: закрыто `eca8821`.
- `ingestion/_advice.py` — совет модели ложился на канонический объект: закрыто там же.
- `admin_ui/static/app.js` — все три (пачки, гонка навигации, пагинация): `BULK_BATCH`
  и `bulkApply` на месте, `pager` есть во всех списках.

---

# G16: поиск по ИСТОРИИ ПЕРЕПИСКИ, а не только по знаниям — **сделано**

`/search` ищет по `knowledge_objects` (то, что человек явно сохранил). Ни один
мейнстримный ассистент не заставляет помнить, СОХРАНИЛ ли ты нужную мысль как
заметку — можно просто спросить «что я спрашивал про Х на прошлой неделе», и он
ищет по ВСЕЙ переписке. У Friday этого нет: таблица `messages`
(`storage/_base.py:461`) — обычная таблица без FTS-индекса вовсе, найти старую
реплику можно только листая `/api/conversations/{id}/messages` руками.

**Решение по образцу, уже есть в коде — не выдумывай своё:** `knowledge_fts` и его
триггеры (`storage/_base.py:758-793`, `CREATE VIRTUAL TABLE ... USING fts5`,
`_ai`/`_ad`/`_au` триггеры на INSERT/DELETE/UPDATE) — тот же паттерн, применить к
`messages`. `SCHEMA_VERSION = 19` (`storage/_base.py:60`) → 20 ОДНИМ коммитом с
фикстурой (та же грабля, что тебе уже прилетала на схеме 19 — G6 разбор помнишь).

Нужно:
- `messages_fts` (fts5, content-таблица `messages`, индексировать `content`) + три
  триггера, симметричные `knowledge_objects_ai/ad/au`.
- Метод хранилища `search_messages(user_id, query, *, limit=..., conversation_id=None)`
  — по образцу `search_knowledge`, свой tenant через `user_id`, чужого разговора не
  видно НИКОГДА (даже владельцу — переписка это не документ, `_protect_owner_target`
  тут ни при чём, это про ЧУЖОГО РЯДОВОГО пользователя).
- Инструмент агента (`execution_kernel`) `message_search` — по паттерну
  `memory_search`, свой capability gate.
- HTTP `GET /api/me/messages/search?q=...` — self-service, `chat.use`, только свой
  `user_id`, без параметра для чужого (тот же контур, что `/api/me/instructions` и
  `/api/me/regenerate` — прочти оба перед тем как писать, это установленный сегодня
  паттерн self-service ручек).
- Telegram — новая команда (сама выбери короткое английское имя в духе остальных:
  `/note`, `/tags`, `/status`; НЕ занимай `/search`, он уже знаниевый). Не забудь
  `BOT_COMMANDS`, `/help`, `tests/test_bridge_surface.py`
  (`EXPECTED_COMMANDS`/`EXPECTED_BRIDGE_COUNT`) И `tests/test_route_inventory.py`
  (`EXPECTED_OPERATIONS`) — оба файла теперь РАЗНЫЕ, не дубликаты (см. коммит
  `40a9eca` сегодня: `test_bridge_surface.py` полгода был случайно подменён чужим
  содержимым чьим-то слиянием, я восстановил оригинал из истории — прочти его
  докстринг перед правкой, там объяснено, что именно он проверяет через AST).

**Осознанно НЕ в эту задачу:** индексация вложений/документов внутри сообщений
(это уже покрыто `knowledge_fts`, если файл стал знанием); ранжирование релевантности
сложнее plain FTS bm25 (переранжировщик сюда не тащи — это отдельное решение с
отдельной ценой, как S7 у Sol).

Тест: своя история находится, чужая — нет (даже с owner-actor, но чужим
`user_id` — self-service структурно не принимает foreign user_id, но проверь
явно), пустой запрос не роняет, мутация на отсутствие индекса в триггере DELETE
(осиротевшая FTS-строка после удаления `messages` — искать то, чего больше нет).

---
