# Архитектура Friday

> Проект переименован: **Friday** (по-русски — **Пятница**), ex codename Jericho.

## 1. Границы системы

Friday — local-first система. Единственные ожидаемые внешние соединения:

1. Telegram bridge ↔ Telegram Bot API;
2. backend ↔ локальный OpenAI-compatible vLLM endpoint;
3. `web_surfer` ↔ публичные сайты, явно настроенные search API **и — при их
   отсутствии или пустой выдаче — HTML-страница DuckDuckGo**. Последнее не
   требует настройки и включено по умолчанию: инструменту `web_search`
   достаточно способности `web.search`. Для local-first это заявление о
   границе доверия, а не деталь реализации — запрос пользователя уходит
   третьей стороне. Отключается снятием `web.search` у пресета.

Обычный поиск может пользоваться бесплатными HTML-резервами, но окно
`freshness=day|week|month|year` не ослабляется при fallback. Его доказанно
соблюдают Yandex (`date:>YYYYMMDD`), Brave API, Tavily и Serper. Brave HTML,
DuckDuckGo HTML и Wikipedia отказываются от такого запроса локально, до сети;
если способного адаптера нет, `web_surfer` поднимает структурный
`freshness_unavailable`, а не возвращает потенциально старую выдачу.

`site` и bounded `include_domains`/`exclude_domains` образуют строгую локальную
границу результата: provider query/native-параметр — только hint, после него URL
снова проверяется по HTTP(S) hostname и exact/subdomain-семантике. Deny-list
никогда не сериализуется во внешний request. `lang` (ISO-639-1) и `region`
(ISO-3166-1 alpha-2) — locale/market hints: они меняют язык, сниппеты и
ранжирование там, где это документировано, но не доказывают язык каждого
документа или страну владельца сайта. Адаптер либо применяет все запрошенные
hints, либо отказывается до socket; комбинированный отказ возвращает структурный
список blockers. Пустой узкий индекс Wikipedia считается пустотой открытого
интернета только при явном ограничении поиска этим корпусом.

Основные данные, права, история, граф, review state и очереди хранятся локально. Local-model output считается недоверенным предложением, а не источником фактов или административным решением.

### Внутренний TLS-контур

Native TLS — единое состояние backend-а, а не настройка только uvicorn. При полной
паре `FRIDAY_SSL_CERTFILE`/`FRIDAY_SSL_KEYFILE` сервер, выведенные loopback CORS
origins, Telegram bridge и live diagnostics используют HTTPS. Оба локальных
клиента сохраняют hostname verification: bridge начинает со стандартного public
root bundle httpx/certifi, diagnostics — с OS default roots, затем каждый при
необходимости добавляет только публичный `FRIDAY_BACKEND_CA_FILE`. Если
отдельный CA не задан, клиент того же native TLS процесса доверяет публичному
server certificate. Private key читает только uvicorn.

```text
browser/LAN ───── HTTPS + bearer ──→ uvicorn backend
Telegram bridge ─ HTTPS + HMAC ────→ uvicorn backend
live diagnostics ─ HTTPS + bearer ─→ uvicorn backend
Telegram bridge ─ HTTPS ───────────→ Telegram API
```

При IPv4 wildcard bind systemd bridge выбирает `https://127.0.0.1:<port>`, при
IPv6 wildcard — `https://[::1]:<port>`; соответствующий IP SAN обязателен. Base
Compose намеренно оставляет backend↔bridge на HTTP внутри private Docker network и
обнуляет server TLS paths в общей среде, чтобы Telegram-контейнер никогда не видел
private key. Внешний HTTPS для Compose завершается reverse proxy. Явный
`FRIDAY_BACKEND_URL` оставлен для такой и другой нестандартной топологии, но
CA/hostname contract остаётся тем же. Telegram proxy никогда не наследуется
backend-клиентом.

## 2. Модули

| Модуль | Ответственность |
|---|---|
| `telegram_bridge` | long polling, durable inbox, команды, файлы, подпись backend-запросов |
| `ingestion` | moderate classification, Raw Object, Inbox, promotion, deterministic enrichment, advisory model refinement |
| `knowledge_graph` | сущности, связи, links к знаниям, graph context, duplicate suggestions, merge history и единая timeline событий/valid-time границ отношений |
| `retrieval` | FTS, lexical similarity, persistent dense embeddings (corpus-wide recall), field/quality/graph/feedback/lifecycle ranking |
| `agent_runtime` | диалог, сборка контекста, режим ответа, planning/tool calls, fail-closed автопроверка ответа (`passed`/`failed`/`unknown`/`skipped` + предупреждение), легенда источников `[K#]` и пометка неподкреплённых ответов, и ответ |
| `executive` | миссии: планирование цели в ациклический план задач, фоновое пошаговое выполнение, управляемая автономия |
| `memory` | Markdown-vault как локальное переносимое представление знаний |
| `documents` | безопасное извлечение текста/метаданных и bounded visual assets; нераспознаваемые медиа (аудио/видео) хранятся как raw с провенансом |
| `web_surfer` | публичный поиск/fetch/research с SSRF-защитой |
| `execution_kernel` | реестр инструментов, capability gate, tenant context, аудит |
| `permissions` | actor context, capabilities, presets, overrides, default deny |
| `storage` | SQLite schema, транзакции, provenance, versions, soft delete, capability-gated hard-delete/purge, backup/export |
| `admin_api` | административные cross-tenant операции с отдельными capabilities |
| `admin_ui` | review и data-management workflows, а не основной разговорный интерфейс; статические `index.html`+`app.js`+`app.css` под строгий CSP без inline-кода |
| `workers` | безопасные периодические задачи для всех активных tenants |
| `organs` | плагин-фреймворк (JOP): органы добавляют capabilities/workers/routers; первый орган — `reminders`; см. `docs/ORGANS.md` |
| `supervisor` | generic-супервизор для `jericho up`: рестарты с backoff, crash-loop guard, штатная остановка; systemd-юниты через `jericho install-services` |
| `config` | env-конфигурация и закреплённый runtime profile модели |
| `telemetry` | локальные счётчики/тайминги без внешней отправки |
| `diagnostics` | конфигурация, БД, каталоги, права записи, endpoint checks |

## 3. Вертикальный поток

### Текст из Telegram

1. Bridge получает OS-backed singleton lease и не допускает второй процесс для той же очереди.
2. Update до обработки записывается в `telegram-inbox.sqlite3`; Telegram offset повышается только после durable insert.
3. Bridge подписывает method/path/user/chat/body через HMAC-SHA256.
4. Backend проверяет timestamp, подпись, allowlist и rate limits, затем до любых side effects захватывает durable idempotency lease, связанный с SHA-256 payload.
5. Telegram identity преобразуется в стабильный tenant ID, сообщение сохраняется в текущий channel conversation; режим `dialogue`, `knowledge_work` или `research` хранится вместе с conversation/channel session.
6. `ingestion` оценивает сообщение до долговременного сохранения и выбирает один из трёх исходов:
   - `transient` — приветствие, подтверждение, чистый вопрос/команда или слабый chatter остаются только в conversation;
   - `review` — создаются Raw Object и pending Inbox item с объяснением и предложенной структурой, но без Knowledge Object;
   - `promote` — создаются Raw Object, Knowledge Object и links к достаточно уверенным сущностям; неоднозначные links всё равно остаются reviewable.
7. Явное намерение «запомни/сохрани» повышает решение до promotion, но не отменяет provenance и validation.
8. Retrieval собирает tenant-scoped контекст из FTS/lexical/embeddings, предметных полей, качества, lifecycle, feedback и графа. В shared person-search это две независимые координаты: tenant задаёт место хранения, exact Raw `uploaded_by` — автора. Авторский предикат проверяется до caps FTS/LIKE, recent/date, whole- и chunk-vector SQL; tenant resident cache обходится. Общие graph/entities не имеют такого provenance и на этом пути не читаются. Reranker получает detached author-only rows и может менять только порядок/числовой score; canonical тело восстанавливается после него. Все uncached SQL и dense aggregation выполняются через blocking boundary, а passage span переносится в пользовательскую выдержку.
9. Agent Runtime разделяет current conversation, personal knowledge, graph evidence и general reasoning, затем вызывает только разрешённые tools в рамках mode-specific budget. Research-результат не становится знанием, пока пользователь явно не отправит его в Inbox.
10. Ответ, сообщения и tool audit сохраняются; bridge отправляет ответ и после успеха удаляет update из durable queue. Временные ошибки получают bounded backoff, исчерпанные — retained dead-letter.

### Файл

Файл хранится в `data/files/<tenant-safe>/...`, а Raw Object содержит provenance и путь. Текст извлекается в памяти с лимитами; архивные entries не получают возможность писать в произвольный filesystem path. Для изображений и сканированных PDF формируется ограниченный набор нормализованных visual assets. Локальный vision/OCR output проходит строгую схему, считается advisory и принудительно направляется на review. Promotion применяет ту же трёхстороннюю политику к извлечённому содержимому.

Telegram-медиа (voice/audio/video/video_note/animation) скачивается мостом и проходит тот же путь. Геолокация и контакт нормализуются в текстовую заметку; неподдерживаемые типы получают ответ, а не молчаливый dead-letter. Происхождение пересланного сообщения (`forward_*`) сохраняется в `RawObject.metadata_json`.

При `FRIDAY_WHISPER_ENABLED=1` поддерживаемые voice/audio и контейнеры со звуком транскрибируются локально. Транскрипт остаётся advisory и Inbox-first: synthesis может помочь ответить в текущем ходе, но verifier не принимает распознавание за источник истины. При выключенном Whisper, превышении лимита или ошибке остаётся raw-файл без текста. Короткое голосовое без подписи становится ровно одной user-репликой; оно не дублируется как attachment evidence, а реальное ограничение транскрипта отмечается структурно и видимым предупреждением.

Извлечённый текст текущего файла доступен только точному uploader при действующем `files.read`. Persisted follow-up восстанавливает не более трёх файлов и 24 000 знаков только по явной ссылке либо deictic-продолжению сразу после attachment-grounded ответа; закрытый класс продолжений включает повторный подсчёт/перечисление и проверку «это всё?». Новый attachment-bearing ход целиком заменяет прежний active set. На каждом ходе заново проверяются tenant, точный `uploaded_by` и capability, а regenerate привязан к immutable source message ID, не к совпадению подписи. Message metadata хранит только структурные counts и ограниченные opaque Raw IDs; API не возвращает эти IDs или excerpts. В общем архиве source/content/text dedup также разделён по точному uploader, поэтому безопасный conversational pointer никогда не заимствуется у другого участника. После использования файла разговор получает sticky private-lineage: до начала нового conversation внешние инструменты и перенос правил/поправок в account-wide память запрещены, даже когда сам файл на очередном ходе уже не восстанавливается.

Native DOCX/XLSX дополнительно несёт `OfficeStructureIndex v1`, не меняя ни одного
байта `DocumentResult.text`. Индекс связан с exact UTF-8 SHA-256 этого текста и
содержит только bounded IDs, spans, ordinals, closed roles и coverage counts/reasons;
literal values восстанавливаются из Raw content после строгой повторной валидации.
Durable индекс подписан installation-local HMAC одновременно по canonical index и
SHA-256 исходных байтов из tenant-scoped Raw row; current, restored и replay не
получают process-private trust marker, пока эта подпись не проверена. У no-save
подписи нет: parser-built объект помечается не сериализуемым через JSON Python-типом
и существует только внутри текущего вызова.
DOCX source order может поэтому отличаться от исторического paragraphs-first
порядка spans, не меняя corpus. Authoritative `person_rows` появляется только при
однозначной шапке и непрерывной области записей; candidate inventory имеет ровно
одну ячейку declared person-column на row и не проходит общий graph cap.

В runtime все валидные Office-вложения сводятся в один canonical
`FRIDAY_ATTACHMENT_DATA` user-data JSON. Бюджет принимает целый paragraph/row либо
не принимает его вовсе; totals, emitted counts, authority, completeness и omission
reasons остаются видимы. Exact count/list рендерится кодом в source order, минуя
модель, verifier и repair. Любая неполнота даёт deterministic UNKNOWN, а
исчерпывающий model-only ответ на составном ходе удаляется до persistence вместе с
производными carriers. В durable storage индекс существует только у Raw Object;
no-save source/index остаются эфемерными. Text-dedup Office требует равенства двух
полных валидных индексов, поэтому одинаковый flat text с иной layout не переиспользует
структурную authority первого файла.

Review-действия вынесены прямо в Telegram: `/inbox` разбирает входящие предложения, `/merges` показывает кандидатов на объединение дубликатов сущностей с inline-кнопками accept/reject. Обе команды вызывают user-scoped, capability-gated и аудируемые эндпоинты (`inbox.review`, `kg.merge`); слияния остаются исключительно ручными.

### Веб-страница

`POST /api/ingest/url` (cap `web.fetch`+`knowledge.create`) загружает публичную страницу через SSRF-защищённый `web_surfer.fetch` (DNS-пиннинг на каждом hop, content-type allowlist, лимит размера) и направляет очищенный текст в тот же ingestion pipeline с `source="web"` и `source_ref=<url>`: Raw Object → Inbox → Knowledge Object только после review. Заблокированный/пустой fetch отклоняется (422), повтор того же URL идемпотентен. Веб-контент становится извлекаемым знанием, а не только синтезом в ответе агента.

## 4. Moderate classification и enrichment

Promotion assessment хранит:

- action/category и confidence;
- promotion score и quality score;
- положительные signals и penalties;
- knowledge kind;
- reason и policy version.

Основной принцип: высокая precision важнее агрессивного recall. Система не обязана сохранять каждую реплику. Пограничный материал направляется в Inbox, где пользователь видит title, summary, tags, importance, metadata, сущности и proposed action до принятия решения.

Deterministic enrichment распознаёт типы `fact`, `decision`, `preference`, `task`, `event`, `project`, `procedure`, `contact`, `reference`, `idea`, `technical_note`, `document` и нейтральный `note`. Metadata может содержать URLs, даты, action items, структуру текста, extraction evidence и версию политики.

Local-model advisor работает поверх pending Inbox:

- получает только Raw Object и deterministic baseline;
- использует background priority и bounded JSON schema;
- не меняет `status`, `reviewed_at`, promotion/quality score;
- не создаёт Knowledge Object, сущности или relation;
- не выполняет merge;
- принимает model-only entity только при буквальном mention в источнике и ограничивает confidence ниже порогов graph auto-link;
- сохраняет ответ отдельно как `advisory_only`, чтобы reviewer видел происхождение предложения.

## 5. Модель данных

### Raw Object

Первичный вход содержит tenant, source/source_ref, оригинальный текст либо file provenance, content hash, metadata и timestamps. Raw Object служит доказательством происхождения. Knowledge Object без существующего Raw Object того же tenant не создаётся.

### Knowledge Object

Нормализованное долговременное представление содержит:

- title, summary, content, content type;
- tags, importance, knowledge kind и structured metadata;
- promotion/quality score;
- lifecycle stage;
- ссылку на Raw Object;
- version, timestamps и soft delete.

Перед изменением snapshot записывается в `knowledge_object_versions`. Re-enrichment, возврат в Inbox, lifecycle transition и soft delete не уничтожают provenance.

### Inbox

Inbox item связывает Raw Object и, при наличии, Knowledge Object. Он хранит review status, suggested action, promotion/quality score, tags, подробный suggestions payload, notes и reviewer metadata. Человек может принять, исправить, отклонить, отложить или вернуть legacy-объект на повторный разбор.

### Knowledge Graph

- `entities` — canonical entity, aliases, type, description, metadata;
- `relations` — направленная типизированная связь и быстрая mutable current projection; `weight` — ранговый сигнал (кламп 0.1–1.0), а provenance живёт в `metadata_json`: обязательный `origin` (`api` + `created_by` для ручных рёбер, `container` для PART_OF-иерархий, `review` + `reviewed_by` + исходная `confidence` для принятых кандидатов);
- `relation_revisions` — каноническая append-only transaction-time история полных typed snapshots отношений: `present`-tombstone, operation, revision, глобальный `event_seq`, UTC `recorded_at`, transaction `batch_id` и `history_quality`; capture/guard triggers охватывают INSERT, содержательный UPDATE, DELETE и конфликтующий `OR REPLACE`, а отдельные triggers запрещают UPDATE/DELETE/replace истории;
- `relation_revision_context` — singleton transaction context и durable logical clock `observed_at`: максимальная уже выданная историческая граница либо более поздний graph/identity commit; значение канонично, сохраняется между restart и никогда не уменьшается;
- `knowledge_entity_links` — связь Knowledge Object и сущности с confidence/evidence/status;
- `entity_resolution_candidates` — reviewable предложение объединения;
- `entity_merge_history` — история принятого merge;
- `relation_candidates` — предлагаемые типизированные связи с evidence;
- `knowledge_conflicts` — потенциально несовместимые утверждения, ожидающие review;
- `feedback_state` — последняя актуальная оценка цели при сохранённой append-only истории;
- `knowledge_usage` — агрегаты retrieval/answer use без изменения содержания знания.

`GET /api/kg/timeline` объединяет два вида строк: EVENT с `at=occurred_at` и
изменение relation с `boundary=confirmed` на непустом `valid_from` либо
`boundary=ended` на непустом `valid_to`. После единой стабильной сортировки
применяется один общий `limit`; `total` и `truncated` считают весь период, а не
страницу. В положение строки входят только valid-time поля. `created_at` и
`invalidated_at` возвращаются отдельно как transaction evidence, но не заменяют
неизвестную valid-time границу; relation metadata наружу не выходит. Invalidated
relation остаётся частью истории, soft-deleted relation исключается.

`as_of` и `known_at` отвечают на разные вопросы. Первый фильтрует valid-time
выбранной версии (`valid_from <= as_of < valid_to`), второй выбирает последнюю
revision, уже записанную к offset-aware RFC3339 transaction-time. Без `as_of`
исторический снимок показывает связи, которые тогда считались действующими, а не
связи на календарную дату `known_at`. Все relation mutations внутри внешней
`FridayStorage.transaction()` получают один UTC timestamp и `batch_id`, поэтому
candidate accept, merge и unmerge наблюдаются целиком до или после границы;
rollback откатывает projection и revisions вместе. Прямой SQL также ловится
trigger fallback. И managed context, и fallback учитывают системное время,
последние relation/identity события, completeness floor и durable `observed_at`:
откат wall clock не делает более позднюю транзакцию видимой в уже выданном snapshot.
Явный допустимый `known_at` сначала атомарно поднимает `observed_at`, и только потом
читает mutable projections; поэтому historical read — маленькая локальная запись,
а не чистый SELECT. Новый outer batch
получает строго большую границу; равное время делят только атомарные события одного
batch, где порядок детерминирован `event_seq`.

Fallback гарантирует capture одной relation-строки, а не transaction semantics
произвольного multi-row SQL: у SQLite нет statement-level trigger, способного
выдать строковым triggers общий batch и очистить его после statement. Все product
bulk mutations поэтому обязаны идти через `FridayStorage.transaction()`; прямой
multi-row DML — неподдерживаемый repair-path, а не скрытый API.

Схема 31 не выдаёт нынешнюю projection за старую историю: миграция атомарно
записывает неизменяемый `relation_history_complete_from` и baseline с
`history_quality=migration_baseline`. `known_at` раньше floor отклоняется
fail-closed. Revisions восстанавливают historical endpoint IDs и link state, но
не исторические имена или entity topology. Поэтому снимок, после которого случился
merge/unmerge, soft-delete/undelete либо смена canonical/merged target, отклоняется;
name-only edit допустим, а ответ честно маркируется `identity_basis=current_names`.

Schema-32 authority проверяется до любого idempotent DDL и после получения
межпроцессного `BEGIN IMMEDIATE`: marker/floor, bidirectional current↔latest lineage,
точные `sqlite_master.sql` двух owned tables, допустимой historical/canonical формы
projection `relations`, всех 15 capture/protection triggers и полной UNIQUE-index
conflict surface четырёх guarded tables. Exact `uq_active_relation` и штатные
implicit indexes обязательны; любой дополнительный UNIQUE отклоняется по счётчику
без публикации его недоверенного имени или выражения.
Развёрнутая ранняя schema 31 является отдельным известным predecessor-контрактом:
startup принимает только точные fingerprints её трёх tables и десяти guards,
проверяет всю semantic lineage, затем атомарно заменяет context/guards и повторно
доказывает уже v32 перед сменой marker. Revisions, `event_seq`, snapshots и floor
при этом не переписываются; `observed_at` засевается максимумом существующей
relation/identity time authority. Так одноимённая пустышка, гибридный DDL,
потерянный marker и конкурентный первый старт не могут ни скрыть gap, ни сдвинуть
completeness floor. Floor обязан быть каноническим RFC3339 и совпадать со своим
immutable `updated_at`; baseline — иметь тот же `recorded_at`, фиксированные
migration batch/quality, revision 1 и `present=1`; только baseline может лежать
ровно на floor, captured revision обязана быть позже, а вся линия дополнительно
проверяется на неубывание времени по `event_seq`, запрет одинаковой границы у
разных batch и отсутствие evidence позже `observed_at`.

## 6. Entity extraction и resolution

Entity extraction остаётся консервативным, но покрывает явные проекты, инфраструктуру, технологии/версии, организации, события, локации, документы, identifiers и имена людей. Слабые mention становятся предложениями, а не подтверждёнными graph links.

Duplicate detection использует:

- точное нормализованное имя и aliases;
- token/name similarity;
- acronym/abbreviation evidence;
- общие Knowledge Objects и графовых соседей;
- совместимость типа.

Точные identifiers и contract-like codes не сравниваются morphology/prefix/stemming: для них требуется exact normalized identity.

Правила merge:

- uncertain candidate имеет статус `suggested`;
- reject сохраняется и не создаётся заново как новая пара;
- reviewer явно выбирает canonical target;
- merge переносит knowledge links и relations, объединяет aliases и не создаёт self-relations;
- source остаётся non-canonical, указывает `merged_into_id`, а snapshot решения сохраняется в merge history;
- cross-tenant merge запрещён.

Сущность, созданная после старого документа, догоняется worker-ом
`entity_mention_backfill`. Это кооперативная state machine, а не один большой
`asyncio.to_thread`: один tick ограничен документами, временем и числом links, а
внешний supervisor timeout остаётся только последним предохранителем. Содержимое
SQLite читается через `blobopen` bounded UTF-8 страницами; character+byte cursors
возобновляют phrase, exact и inflected/token фазы без перечитывания префикса.

Durable candidate/present/winner/validation spool содержит только числовые позиции и
authority — никогда текст, имя, alias или snippet. Spool и primary checkpoint пишутся
атомарно и очищаются по bounded страницам. Перед link повторно проверяются tenant,
document version и entity ID+version; validation progress дополнительно подписан
process-local keyed authority, поэтому persisted состояние не может само себя
авторизовать после рестарта. Быстрый 12-token phrase path имеет bounded literal
fallback для полного canonical-domain до 240 символов, а inflected matching принимает
решение longest-first над полным текущим token-window. Терминальные accepted/rejected
links остаются терминальными.

### Контейнеры и browse-слой

Организация знаний живёт внутри графа, а не в параллельной иерархии таблиц: контейнеры — это сущности типов `project` и `collection` (`CONTAINER_ENTITY_TYPES`), членство — обычные `knowledge_entity_links` (в browse участвуют только `accepted`), иерархия — `PART_OF`-отношения между контейнерами. `KnowledgeGraph.create_container` создаёт (или возвращает существующий) контейнер и, при явном `parent_id`, сразу материализует `PART_OF` — review-gate относится к системным предложениям, а не к явным действиям пользователя. `list_containers` отдаёт плоский список с `knowledge_count` и `parent_id` для рендера дерева.

Browse-поверхности: `GET /api/knowledge/tags` (агрегация тегов через `json_each`; сравнение и группировка регистронезависимы и для кириллицы — зарегистрирована SQL-функция `jericho_casefold`, потому что `lower()`/NOCASE в SQLite сворачивают только ASCII), фильтры `tag`/`entity_id` в `GET /api/knowledge` и `GET /api/admin/knowledge`, `GET/POST /api/kg/containers`, поиск сущностей по имени `GET /api/kg/entities?q=`. Telegram: `/tags` и `/browse тег|контейнер|сущность`; админка: чипы тегов в «Знаниях» и дерево контейнеров в «Графе».

## 7. Graph-aware retrieval

Поиск формирует несколько evidence channels:

1. SQLite FTS;
2. lexical tokens/trigrams с сохранением точных identifiers;
3. persistent dense embeddings (corpus-wide dense recall);
4. matches по title, summary, tags, kind и entity links;
5. graph expansion от сущностей запроса;
6. importance, lifecycle, feedback, promotion/quality score и noise penalty.

Векторы хранятся в таблице `knowledge_embeddings` (один актуальный float32-вектор на Knowledge Object) и наполняются воркером `embeddings_index`: он эмбеддит объекты без вектора, из другой модели или устаревшие по версии. Длинные объекты дополнительно режутся на перекрывающиеся пассажи, чьи векторы лежат в `knowledge_chunk_embeddings` (`FRIDAY_EMBEDDINGS_CHUNK_CHARS`, `0` — выключить): целая импортированная статья иначе схлопывается в один усреднённый вектор, и один релевантный абзац в ней не находится. Пообъектный скор — `max(вектор объекта, (1−blend)·лучший пассаж + blend·среднее топ-3)`, поэтому вектор объекта остаётся **полом**: чанкинг может только добавить recall и никогда не понижает существующий результат. Он же остаётся единственным входом дедупликации — пассажи в неё не попадают. Выигравший пассаж отдаётся как evidence в ответ (его границы едут в `_embedding_chunk_span`), а `strategy.embeddings_chunked` и `components.embedding_chunk` показывают его в explain-трейсе. Запрос эмбеддится один раз и косинусно сравнивается со всеми сохранёнными векторами пользователя; сильнейшие совпадения объединяются с пулом кандидатов ДО ранжирования, поэтому смысловое совпадение без общих токенов и не из числа недавних тоже находится. Embeddings опциональны (локальный OpenAI-совместимый `/embeddings`); без них поиск работает на остальных каналах, а до первого прогона индексатора dense-сигнал мягко деградирует к эмбеддингу текущего пула.

Загрузка passage-векторов адаптивна только по цене, не по семантике. При плотном
актуальном индексе SQLite идёт от `knowledge_objects` по
`(user_id, created_at DESC, id ASC)` и точечно читает chunks через их составной PK;
при sparse/rolling model/dim остаётся chunk-first scan с небольшой сортировкой.
Переключение разрешено лишь при `active_chunks > 2*object_window` и
`4*active_chunks > 3*total_chunks`. Обе ветки применяют одинаковые tenant,
soft-delete и private-dependency predicates и возвращают один total order; профиль
содержит только счётчики и не читает vector BLOB.

Те же векторы используются для **дедупликации знаний** (`friday/dedup.py`, воркер `knowledge_dedup` + `POST /api/admin/knowledge/detect-duplicates`): попарный косинус ≥ `FRIDAY_DEDUP_THRESHOLD` предлагает почти-дубликат как review-gated конфликт типа `near_duplicate`. Разрешение (§9: «оставить одну») помечает вторую копию `deprecated` — автослияния нет. Скан **инкрементальный**: каждый объект ровно один раз сравнивается со **всем** корпусом (сначала свежепроиндексированные векторы, затем нисходящий backfill истории), курсор лежит в `runtime_kv` и переживает рестарт, а бюджет тика (`FRIDAY_DEDUP_SCAN_BATCH`, `FRIDAY_DEDUP_SCAN_MAX_SECONDS`) откладывает остаток на следующий тик вместо того, чтобы молча его потерять. Порог читается при каждом прогоне: его **понижение** запускает пересканирование (ранее отвергнутые пары становятся годными), повышение — нет. Near-duplicate — организационный сигнал: он не попадает в контекст рассуждения агента, только в review-UI.

Граф расширяется на один шаг для обычного запроса и до `FRIDAY_GRAPH_MAX_DEPTH` (по умолчанию 2) затухающих шагов только для relational language (`связан`, `зависит`, `через`, `отношение` и аналоги); жёсткий потолок безопасности — 4. Дополнительно используются implicit `co_occurs_in` signals между сущностями, связанными с одним Knowledge Object. Они помечаются как implicit, не сохраняются как доказанные relations и имеют меньший вес.

Reasoning над противоречиями: каждый Knowledge Object в контексте LLM несёт `lifecycle_stage`, `updated_at` и, при наличии, `conflict` (тип + противоположная [K#]). Модель инструктирована предпочитать актуальные записи устаревшим и честно указывать на противоречие, а не выдавать одну сторону за факт. Конфликты остаются review-only предложениями (regex-предикаты uses/address/quoted_value/scheduled_date; даты нормализуются к ISO, чтобы формат не порождал ложных конфликтов).

Три границы этого контура **сообщают о себе**, а не срабатывают молча:

- **Термы FTS.** Бюджет — **24** терма на запрос (`_FTS_TERM_BUDGET`; поднят с 12, замер 9/60 → 12/60 попаданий в топ-10). Пока запрос в него укладывается, берутся **все** его токены, включая стоп-слова: на этой стадии FTS работает на recall, и для парафраза общие слова — единственный лексический мост к документу (безусловный отброс стоп-слов стоил стенду 0.583 → 0.458). Стоп-слова отбрасываются **только** у запроса, превысившего бюджет: раньше он терял хвост, а русский вопрос ставит в начало «как», «почему», «пожалуйста», и идентификатор, который и называет ответ, до индекса не доезжал вовсе.
- **Покрытие документа.** `chunk_spans` расширяет окно, пока пассажи не уложатся в `max_chunks`, вместо одного прохода с последующим `[:limit]`. При штатных значениях (1200/200/63) документ в 490 КБ индексировался **на 59%**, и потерянный 41% был доступен только через объектный вектор, который сам ограничен. Крупный документ получает более грубые пассажи — и это осознанная цена: объектный вектор остаётся полом, поэтому чанкинг может только добавить recall.
- **Плотное доказательство.** `FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN` (по умолчанию **0.35**; история калибровки — 0.16 → 0.35 на синтетике → 0.40 на обрезанном индексе → снова 0.35 на честном, см. §15) — косинус, ниже которого плотный скор не считается доказательством в фильтре `insufficient_evidence`. Число принадлежит **модели**, а не Friday, поэтому оно настраиваемое, а значение по умолчанию **измерено**, а не выбрано. На `qwen3-embedding-0.6b` в рабочей точке «короткий запрос против тела документа»: 56 пар «запрос × чужой документ» дали min 0.1032 / p50 0.2361 / p90 0.3255 / max 0.3878, а 8 пар «запрос × свой документ» — min 0.4188 / p50 0.5197 / max 0.6196. Прежняя константа 0.16 стояла **ниже медианы шума** и пропускала 48 из 56 чужих документов (85.7%) как доказательство. Меняя модель, перемеряйте: у другой модели масштаб будет другим.
- **Пул кандидатов.** `FRIDAY_RETRIEVAL_POOL_MAX` (по умолчанию 400).

**Поиск по исходному тексту — отдельный контур, не шестой канал.** `raw_objects` хранит принятые символы как есть, Knowledge Object — нормализованную, часто сокращённую версию; замерено на этой установке, **93% принятых символов** жили только в первом и не покрывались ни одним индексом. Индекс `raw_fts` (FTS5, external content) отвечает на вопрос «в каком документе я это читал» — это провенанс, а не recall.

Он **намеренно не подключён** к `HybridSearcher`, к контексту агента и к реестру инструментов, и это закреплено тестом: место, где воскрешение отвергнутого материала навредило бы сильнее всего, — агент, цитирующий его как факт.

**Достижимость определяет вердикт Inbox.** `pending`, `classified`, `archived` и материал без inbox-строки — достижимы; **`ignored` — нет**. DATA_LIFECYCLE §3 делает «игнорировать» вердиктом: Raw Object сохраняется ради провенанса, а не ради выдачи. Проверка — `NOT EXISTS ... status='ignored'`, а не соединение по текущему статусу: у одного Raw Object может быть несколько inbox-строк, и соединение пропускало объект, если хоть одна из них не была отказом. Если пул вернулся полным и корпус больше него, ответ несёт `strategy.lexical_pool_capped`, `lexical_pool_scanned` и `corpus_size`. Пустой результат на 8000 объектах и пустой результат на 40 — разные вещи, а печатались одинаково.

Низкокачественные вопросы/chatter, случайно попавшие в legacy knowledge, получают штраф. Recent pool нужен для recall, но низкорелевантный объект отбрасывается: маленькая база не превращает случайную заметку в «лучший ответ» только из-за отсутствия конкурентов.

### Как читается сам вопрос

Перед тем как искать, запрос проходит два преобразования — оба **симметричные** (обе стороны сравнения считает одна и та же функция) и оба видимые в ответе.

**Морфология.** Слово-признак в `lexical_vector` — это **основа** (`friday/morphology.py`, алгоритм Snowball для русского), а не поверхностная форма. Русский склоняется: «Казань», «в Казани» и «под Казанью» — одно слово для читателя и три строки для ранжирования по токенам. Замерено на корпусе владельца: документ, содержащий все слова запроса в косвенном падеже, набирал лексически **0.0597** — ниже порога доказательства — и доходил до ответа только потому, что засевал граф, а его собственные сущности ручались за него обратно. Эту круговую поруку и должна была снимать морфология.

Границы намеренно узкие: стеммятся только целиком кириллические токены длиной от пяти символов (идентификаторы вроде `BRK.A` и `PK-04-04` обязаны выживать дословно — `identifier_coverage` отбрасывает кандидата, не содержащего их буквально), триграммы остаются на поверхностной форме (они уже терпели морфологию приблизительно, и «список»/«списка» расходятся беглой гласной, до которой не дотягивается ни одно суффиксное правило), а **индекс FTS не трогается** — свёртка там означает миграцию схемы со своим перестроением. Функция мемоизирована: `lexical_vector` считается по полному телу каждого кандидата, и без кэша стемминг стоил ×3.3 (34 мс против 10 мс на теле в 51 КБ); с кэшем — 10.4 мс.

**Починка запроса** (`retrieval/_repair.py`). Три ввода неотличимы для сопоставления токенов и означают совершенно разное: `uhfabr jngecrjd` — не переключённая раскладка (это вопрос), «график дужурста» — соскользнувший палец (это вопрос), `asdkjhqwe zxcmn` — телефон в кармане (это не вопрос). Первые два получали ту же тишину, что и третий.

Правило: правка обязана **заслужить** право на применение. Вариант используется, только когда его находит индекс **для этого пользователя**, а исходный запрос не находит ничего. Ничего не угадывается по форме текста — цена ошибки в том, что ответят на другой вопрос, а это хуже, чем не ответить.

- Раскладка (`retrieval/_keyboard.py`) — точное и обратимое соответствие по позициям клавиш ЙЦУКЕН/QWERTY, угадывать нечего.
- Опечатки — расстояние Левенштейна до слов **самого корпуса** (`knowledge_vocab`, представление `fts5vocab` над индексом: второй копии текста нет). Терм заменяется, только если ближайшее слово ровно одно: ничья означает, что архив сам не знает, что имелось в виду. Допуск — одна правка до восьми символов, две от восьми; термы короче пяти символов не правятся вовсе.
- Словарь корпусный, а не пользовательский (у индекса нет колонки пользователя) — поэтому починенный запрос принимается только по признаку «нашёл результаты **спрашивающему**»: слово, заимствованное из чужого документа, просто ничего не вернёт.

Починка сообщает о себе всегда: `strategy.query_repaired` (`keyboard_layout` / `spelling`), `query_as_typed` и `query_repair_detail`. Читатель, которому не сказали, что вопрос переписан, не сможет заметить неверную догадку.

### Переранжирование: единственный сигнал, различающий кандидатов между собой

Отбор кандидатов и упорядочивание внутри отобранного — разные задачи, и Friday решал только первую. Замерено на корпусе владельца (64 вопроса, судья с известной базой 12.2% «да» на случайной паре, деление на выводящую и отложенную половины):

| сигнал | AUC внутри пула | AUC общий | точность@5 | охват@5 |
|---|---|---|---|---|
| выдача как есть | 0.512 | 0.495 | 41.9% | 68.8% |
| плотный канал | 0.488 | 0.738 | 42.5% | 65.6% |
| cross-encoder | **0.754** | **0.877** | **53.1%** | 68.8% |

Читать надо первый столбец. **Внутри пула прежний порядок был подбрасыванием монеты** (0.512), и плотный канал тоже (0.488) — при том что общий AUC у него 0.738. Расхождение не противоречие: косинус говорит, есть ли в архиве похожее вообще, но не какой из уже отобранных документов отвечает. Все внутренние ручки на этом и застряли — веса каналов, потолок отбора, порог доказательств, переранжирование чат-моделью замерены и исчерпаны, каждая уткнулась в отсутствие сигнала, а не в способ его смешивания.

Cross-encoder (`FRIDAY_RERANK_BASE_URL`, `FRIDAY_RERANK_TOP`; по умолчанию выключен) — первый различитель, который эту задачу решает: он читает вопрос и документ **вместе**, а не сравнивает два независимо посчитанных вектора. В пятёрке теперь отвечают 2.7 документа вместо 2.1.

Границы измерены и записаны в код:

- **Перестановка фиксированного пула сама по себе не растит охват**: в исходном замере
  он был 68.8% до и после cross-encoder. Но рабочий `rerank_top` одновременно задаёт
  ширину отбора кандидатов (`depth`), поэтому увеличение 20 → 40 расширяет и пул до
  reranker. На 20 трудных живых эталонах это дало recall@10 0.60 → 0.70: три выигрыша,
  одна потеря, чистый +2 — ровно объявленный критерий. Все три новых попадания
  отсутствовали в пуле руки 20 и пришли в руку 40 на до-rerank местах 24, 25 и 36.
  Цена: p50 полного поиска 1857 → 2523 мс (+35.9%). Поэтому для явно включённого
  reranker измеренная глубина — 40; значение 0 по-прежнему означает «выключено».
- **4000 знаков на документ** — подобрано на выводящей половине: 1000 и 2000 знаков заметно хуже (AUC 0.734 и 0.736 против 0.832), 8000 не лучше, нарезка на куски с максимумом выигрывает 0.017 при шестикратной цене, что на 32 вопросах шум.
- **Запрос делится на части.** Служба считает токены по всем парам сразу, и вопрос входит в каждую: двадцать документов по 4000 знаков — около 36 тысяч токенов при пределе 16384. Без деления настроенное переранжирование не сработало бы ни разу и выглядело бы работающим.
- **Отказ службы не роняет поиск** — выдача остаётся в прежнем порядке и без отсева. Именно поэтому отказ проверяется диагностикой (`start_rerank_runtime`): молчаливый откат к AUC 0.512 не меняет ни одного признака, и заметить его по виду выдачи нельзя.

#### Порог: система научилась не отвечать

Скор cross-encoder **откалиброван** — это не относительная величина внутри выдачи, как слитый скор, а близкая к вероятности оценка «этот документ отвечает». На живом архиве «график отпусков» даёт пять штук по 0.999, а вопрос, ответа на который в архиве нет, уводит **весь пул ниже 0.01**. Это первый сигнал, по которому Friday может отличить «не нашёл» от «нашёл не то».

`FRIDAY_RERANK_CONFIDENT_MIN` (по умолчанию **0.10**) отсекает. Замерено на отложенной половине (32 вопроса: у 25 ответ в пуле есть, у 7 нет):

| порог | точность показанного | показано | верное молчание | потеряно вопросов |
|---|---|---|---|---|
| нет | 43.5% | 19.5 | 0 из 7 | 0 |
| **0.10** | **78.6%** | **8.2** | **6 из 7** | **4 из 25** |
| 0.90 | 82.8% | 5.6 | 6 из 7 | 9 из 25 |

Размен принят владельцем осознанно: 16% вопросов, у которых ответ был, теперь остаются без ответа — ради того, чтобы на остальных не выдавать похожее за нужное. `0` выключает отсев, не выключая переранжирование.

Три следствия, каждое замерено или выведено:

- **Соблазн показать «хотя бы лучшего из отсеянных» отвергнут.** Среди вопросов, у которых порог срезал всё, лучший срезанный отвечает **1 раз из 8** (на отложенной половине — 0 из 4). Утешительный документ почти всегда не о том и выглядит как ответ, то есть делает ровно то, ради предотвращения чего порог стоит.
- **Режется до обрезки по `limit`, а не после.** Иначе отсев съедал бы места в странице, и человек получал бы два документа там, где порог прошли восемь.
- **Отсев теперь работает и когда нашлось мало.** Условие «кандидатов больше запрошенного» стояло, пока шаг только переставлял (переставлять тройку внутри тройки бессмысленно). С порогом у шага вторая работа, и нужна она как раз тогда, когда нашлось три правдоподобных документа, ни один из которых не о том.

Отсев **называет себя**: `strategy.rerank_dropped` — сколько снято, `rerank_below_threshold` в explain-трейсе рядом с самим скором. Пустая выдача после отсева и пустая выдача на пустом архиве — разные ответы, и Telegram говорит их по-разному: совет «загляните в Inbox» верен только для второго, потому что в первом материал давно разобран.

## 8. Agent context и agency

Agent Runtime строит контекст в явных слоях. Все динамические слои сериализуются как недоверенные данные пользовательского уровня и никогда не становятся system-инструкциями:

- текущая conversation window;
- найденные personal Knowledge Objects с provenance и score;
- graph entities/relations и объяснение пути;
- pending Inbox/resolution signals;
- `user_model` — производная модель пользователя (`knowledge_graph.build_user_model`: постоянные люди/проекты/интересы, ритм капчи), компактно инжектится даже в чистый диалог без хитов, чтобы ответы были личными. Правило в SYSTEM_PROMPT: это фоновый ориентир, не источник фактов — не цитируется как [K#]; сбой построения деградирует в «без модели»; выключатель `FRIDAY_PROFILE_IN_CONTEXT`;
- доступные actor-у tools.

Режим ответа фиксируется как `personal_knowledge`, `mixed`, `personal_knowledge_missing` или `general_conversation`. Это помогает модели не выдавать общее знание за содержимое личной базы. Короткие follow-up реплики contextualize-ятся предыдущими turn-ами, но tenant boundary не меняется.

Атрибуция доводится до пользователя: карта `[K#]` → Knowledge Object возвращается как легенда источников (`citations`, `citation_notice`) в ответе `/api/chat`, рендерится в Telegram и в инспекторе диалога админки. Личный ответ, нашедший записи базы, но не сославшийся ни на одну, помечается `answer_grounded=false` с честной пометкой. Атрибуция консервативна: приписывается только реально процитированное (или единственный сильный хит как fallback), чтобы не искажать feedback/lifecycle-сигналы. Вид `человек` сначала различает системную учётку и названного человека из архива: неизвестное среди учёток имя сохраняет обычные Knowledge hits. Если после этого нет ни записи, ни допустимого person-tool/attachment/graph evidence, выведенный `user_model` не считается основанием для досье: модельное тело и его file/voice/attribution carriers отбрасываются, а ответ формирует структура как честную недостаточность данных.

Высокая agency ограничена полезностью и контролем: инструмент вызывается, когда он реально улучшает ответ; proactive structuring допускает максимум одно ненавязчивое предложение, а изменение долговременной структуры не маскируется под обычный разговор.

Режимы определяют глубину работы, а не уровень доверия: `dialogue` минимизирует tool use, `knowledge_work` допускает несколько шагов над личным контекстом, `research` получает расширенный bounded budget и обязан отделять план, evidence и synthesis. Ни один режим не обходит permissions, provenance или Inbox review.

Результат `web_research` сохраняет множественность источников до самой модели:
общий контекстный бюджет делится между страницами, а не отдаётся первой длинной
странице. Исследование имеет общий внутренний бюджет 27 секунд внутри 30-секундного
дедлайна ядра; после поиска параллельные загрузки получают не более 12 оставшихся
секунд. Уже завершённые источники возвращаются в исходном порядке, а число
недожданных называется явно. Это ограничение контекста, не провенанс `[K#]`:
веб-страница не становится Knowledge Object без обычного review-gate.

Качество ретривера измеримо: золотой набор `eval_cases` (запрос → ожидаемые Knowledge Objects) прогоняется через реальный HybridSearcher (`friday/eval.py`, worker `retrieval_eval` + `POST /api/admin/eval/run`), считаются recall@k / precision@k / MRR, а каждый прогон сравнивается с предыдущим — падение recall@k помечается как регрессия и логируется. Набор курируется в админке через label-from-results (`GET /api/admin/eval/search`), без ручного ввода id.

## 9. Feedback loops и малая база

Feedback хранит type, target, score, comment/context, tenant и timestamp. История остаётся append-only, а `feedback_state` указывает на последнюю актуальную оценку, поэтому исправленная реакция заменяет старый ranking-сигнал, не стирая аудит. Answer feedback атрибутируется Knowledge Objects, реально переданным в ответ; retrieval/answer usage агрегируется отдельно.

Повторные отрицательные решения по похожему автоматически promoted материалу могут только понизить будущую promotion до `review`. Контур не повышает сомнительный материал и не перебивает явные save/no-save команды.

При пустой базе поиск возвращает пустой массив и понятную стратегию, агент сообщает об отсутствии личного контекста и предлагает прислать заметку/документ. При малой базе используется тот же quality threshold, а не ослабленный режим с выдуманной уверенностью.

## 10. Безопасная ревизия legacy-данных

`scan_legacy_quality` повторно оценивает существующие Knowledge Objects без mutation и формирует reasons/signals. Администратор применяет действие только к выбранным IDs:

- `return_to_inbox` — объект перестаёт участвовать в обычном поиске и появляется в pending Inbox для решения;
- `reenrich` / `reclassify` — создаётся новая версия с улучшенными title/summary/kind/metadata/scores;
- `keep` — объект явно подтверждается после review без потери истории;
- `archive` — lifecycle переводится в `archived`, объект остаётся доступным с меньшим ranking weight;
- `soft_delete` — объект скрывается из обычного доступа, но Raw Object, snapshot и audit сохраняются;
- preview — никаких изменений.

Worker и scan не применяют действия автоматически. Массового silent promotion/merge/hard delete нет; каждое изменение tenant-scoped, требует выбранных IDs и сохраняет Raw Object, version history и audit trail.

## 11. Многопользовательская модель и отказоустойчивость

`user_id` — tenant key во всех основных сущностях. Storage, retrieval, graph, conversations и tools принимают tenant явно или через `ActorContext`. Execution Kernel передаёт actor на каждый invocation, поэтому параллельные запросы не смешивают пользователей.

Аутентификация HTTP тоже уважает ролевую модель: помимо owner-токена (`FRIDAY_API_TOKEN`) можно выпускать scoped-токены (`api_tokens`, SHA-256), каждый из которых аутентифицирует как привязанный аккаунт с его preset/capabilities. Loopback-bypass остаётся owner для локальной машины.

SQLite работает с foreign keys, WAL и busy timeout; инициализация/migration и сложные изменения используют атомарные транзакции (`BEGIN IMMEDIATE` там, где нужно сериализовать writers). Неизвестная будущая schema отклоняется без mutation. Workers изолируют сбой одного tenant/item от остальных. Backup использует SQLite online backup API, отдельный integrity check и строгий SHA-256 manifest; поскольку `relation_revisions` и immutable completeness floor находятся в той же БД, verified backup сохраняет их вместе с current projection. Tenant export отдельно включает только relation revisions этого tenant. Копия на том же диске — не бэкап: каждая верифицированная копия зеркалируется наружу (`FRIDAY_BACKUP_MIRROR_DIR`, `friday/backup_mirror.py`) с повторной sha256-проверкой; при заданном ключе (`FRIDAY_BACKUP_ENCRYPTION_KEY_FILE`) зеркальная копия AES-256-шифруется системным `openssl` и проверяется decrypt-and-compare (локальная копия остаётся открытой для быстрого restore).

Process lease является частью SQLite read authority, а не предварительной подсказкой. Живой backend сам строит diagnostics на уже открытом connection и отдаёт snapshot только через authenticated host-local API; внешний CLI при active lease не открывает main DB/WAL. Offline diagnostics удерживает тот же backend lease на всём окне integrity/worker/auth reads; bridge queue аналогично читается только под bridge lease. Проигрыш гонки всегда означает API либо fail-closed report. Даже filesystem secret scanner исключает main/queue SQLite, WAL/SHM/journal/lease и hardlink текущего inode, поэтому не создаёт второй raw reader обходным путём. Admin overview/settings/diagnostics вместе с authorization выполняются через blocking boundary, а production bridge впервые открывает queue только после захвата lease.

## 12. Развитие без ломки границ

Расширения должны использовать существующие контракты:

- embeddings/vector store — через retrieval adapter;
- OCR — отдельный document extractor с лимитами;
- calendar/external APIs — новый `security_id`, ToolSpec и audit;
- более глубокий model enrichment — только как evidence/suggestion, без uncertain merge или скрытого promotion;
- full backup orchestration — отдельный manifest для DB/files/vault/secrets policy.

## 13. Миссии и управляемая автономия (executive)

Модуль `executive` добавляет слой миссий поверх тех же bounded, review-gated примитивов. Миссия — это цель пользователя, которую планировщик раскладывает в ациклический план задач: каждый шаг имеет `kind` (`gather` — собрать сведения, `produce` — подготовить итог) и зависимости только на предыдущие шаги, поэтому граф ацикличен по построению. Если локальная модель недоступна или вернула непригодный ответ, используется детерминированный fallback-план из одного `produce`-шага.

Выполнение ведёт фоновый worker `mission_runner` короткими тактами: он берёт готовую (все зависимости `done`) задачу, выполняет её через тот же capability-gated Execution Kernel с actor владельца и ограниченным набором read/gather-инструментов, и продвигается дальше. Такт не перекрывается и не держит транзакцию во время работы модели. Результат `produce`-шага отправляется в Inbox как `knowledge_work` candidate — Knowledge Object напрямую не создаётся, поэтому финальный контроль остаётся за пользователем.

Автономия управляема двумя флагами. `autonomy_enabled` — общий выключатель: при выключенной автономии миссии создаются и планируются, но остаются `blocked`, а runner ничего не продвигает. `operator_full_autonomy` определяет, запускаются ли миссии, предложенные агентом (инструмент `mission_propose`) или проактивным worker-ом (`mission_proposer`), автоматически или ждут явного запуска пользователем в состоянии `proposed`. Ни один флаг не обходит permissions, provenance или Inbox review: план модели — недоверенные данные, а не полномочие.

Миссии полностью tenant-scoped. Пользователь управляет ими из Telegram (`/mission`, `/missions`) и через `/api/missions`; кросс-тенантная инспекция вынесена в `/api/admin/missions` за `admin.missions.*`. Каждое решение (create/start/cancel/finish) фиксируется в audit.

## 14. Органы (JOP) и проактивность

Модуль `organs` — плагин-фреймворк (**Friday Organ Protocol**). Орган — самодостаточный модуль под `friday/organs/<name>/`, подключающийся через опциональные точки расширения `capabilities()` / `workers(ctx)` / `router()`. Реестр перечислен явно (`build_registry`) — никакого dynamic discovery, набор маршрутов/воркеров/прав всегда ровно такой, как в коде. `create_app` регистрирует capabilities органа, монтирует его router и передаёт его workers в супервизор; всё аддитивно. Полный контракт — `docs/ORGANS.md`.

**Исходящий канал уведомлений** — то, что сделало проактивность возможной (раньше мост только отвечал на входящее). Backend/органы кладут сообщение в durable-очередь `outbound_notifications` (`storage.enqueue_notification`, дедуп по `(user_id, dedup_key)`); мост — единственный держатель бот-токена — тянет её подписанными `GET /api/notifications/pending` + `POST /api/notifications/ack` (гейт `source=="telegram-bridge"`) и доставляет через `sendMessage`. Backend никогда не обращается к Telegram сам и не пишет в основную БД со стороны моста. Deny-by-default проверяется дважды: allowlist при enqueue (backend) и перед отправкой (мост), потому что бот-токен технически достаёт любой чат.

**Орган `reminders`.** Worker сканирует `entity_time` (§11) на события в окне `FRIDAY_REMINDERS_LEAD_DAYS`, резолвит Telegram-чат из метаданных пользователя, уважает allowlist и тихие часы, кладёт одно дедуплицированное напоминание на событие. В shared archive событие одновременно получает durable `private_entity_owners` marker конкретного человека: `entity_time.source` хранит расписание, но не заменяет privacy authority. Generic graph/retrieval/model/organs/admin paths используют единое fail-closed замыкание зависимостей и не видят ни напоминание, ни копию его ID, current или authenticated historical имени/алиаса. Legacy alias containers разворачиваются рекурсивно в bounded byte/node budget, а comparison после NFC → casefold → NFC закрывает иной регистр и NFD. Invalid current/history material сам становится seed карантина.

Recursive closure материализует только entity IDs: `private_entity_material_work` — staging, `private_entity_material_cache` — опубликованное множество. Второй cache/work хранит только `(material_kind, object_id, user_id)` для видимых Raw/Knowledge/Inbox и sparse `knowledge_hidden`, чтобы горячие readers делали PK lookup, а Inbox rebuild не сканировал все Knowledge для каждой строки. У обеих пар свой singleton state, valid только при точном равенстве; authority не копирует identity/content, inspection идёт из canonical rows через connection-local TEMP. Persistent UDF-free guards инвалидируют entity authority при entity/owner/reminder-time/version write и derivative authority при Raw/Knowledge/link/Inbox write даже из raw SQLite. Managed TEMP AFTER triggers обычную вставку или non-flipping update исправляют по одному ID; смена privacy-класса запускает общий ordered rebuild Raw → Knowledge/hidden → Inbox в той же transaction. Внешний writer оставляет state invalid до exact heal новым соединением. Все generic material views требуют оба valid state. Per-connection authorizer разрешает derivative DML только code-owned triggers, запрещает caller-у прямой DML над cache/work/state, owned DDL и `writable_schema`; startup под одним write-lock сверяет свежий live result как `work == cache`, а любой tier, который этот opener не пересобрал, независимо как `cache == live`. Persistent schema поэтому можно менять обычным SQLite без application UDF.

Только person-scoped reminder path допускает точного владельца при совпавших marker/time/source, валидных current/history states и отсутствии зависимости от другой cached identity. Он использует готовый ID-cache (`35.7 ms` median на synthetic 4 500-chain), а не строит fixed point на каждого читателя; public carriers собственного reminder остаются консервативно скрыты. Person export не вычитает собственный ID из global cache: в одной SQLite snapshot он строит fresh fixed point от всех запрещённых direct-private seeds, исключив только точную authority экспортирующего человека. Поэтому dependencies только от его напоминания сохраняются, а foreign/ambiguous/malformed closure исключается. На sparse synthetic 4 500 entities / 1 500 Raw+KO+Inbox / 15 private новая object-ID authority дала Raw point `0.074 ms`, search20 `151 ms`, Inbox list/count `0.96/0.41 ms` и cold exact rebuild `1.52–1.57 s`; обычный ingest — `27 ms`, без identity UDF в горячих планах. Startup под `BEGIN IMMEDIATE` сначала валидирует cache artifacts и allowlist persistent guards, выполняет reminder backfill/owner move, ставит connection-local runtime authority, rebuild и exact validation; authorizer устанавливается до первого application query.

**Орган `reflection`** — рефлексия и синтез, полный референс (все три точки расширения). Детерминированный недельный дайджест: объекты знаний по lifecycle, «ждёт вашего решения» (входящие/связи/противоречия/устаревающие), живые темы, ближайшие события. При доступной локальной модели добавляет 2–3 предложения синтеза над темами (best-effort). Contribuтит capability `reflection.read` и on-demand `GET /api/reflection`. Тихие часы обобщены (`FRIDAY_QUIET_HOURS_*`) — свойство всех проактивных органов; `ServiceContext` получил `llm`.

**Орган `profile`** — модель пользователя. Само вычисление живёт в core (`knowledge_graph.build_user_model` — его же потребляет контекст агента, см. §8); орган — тонкая capability-gated поверхность: `profile.read` + `GET /api/profile` (`?synthesize=true` — LLM-портрет). Отражение знаний, в граф не пишет; правится правкой самих знаний.

**Орган `chronicle`** — присутствие во времени / эпизодическая память. Воркер «в этот день» (знания той же календарной даты прошлых лет → resurfacing-push) + окно `GET /api/chronicle?days=7` («что было за неделю»). `chronicle.read`. Опирается на новые storage-запросы `list_entities_by_activity`/`list_recent_knowledge`/`list_knowledge_on_this_day` (без изменения схемы).

**Орган `importer`** — холодный старт. `POST /api/import` (cap `import.run`, аудит) принимает ICS-календарь, экспорт закладок (Netscape HTML), mbox-архив почты или одиночное .eml и раскладывает каждый элемент в pending Inbox через `ingest_text(force_review=True)` — bulk-материал никогда не канонизируется молча; повторный импорт идемпотентен (стабильные per-item `source_ref`: ics UID / URL закладки / Message-ID). Почта парсится из исходных байтов (stdlib `mailbox`/`email`, `policy.default`) — объявленные кодировки (cp1251/koi8-r) декодируются корректно, HTML-письма конвертируются в текст. Массовое подтверждение — существующими bulk-действиями Inbox в админке.

**Орган `compactor`** — ночная сводка о поведении СИСТЕМЫ (не о содержании переписки). Собирается из структурных признаков хода, которые система и так пишет; корпус модели не показывается вовсе, а в самой сводке нет поля, куда текст мог бы попасть: коды инцидентов и целые числа, человеческая формулировка живёт в коде программы. Права разведены по цене, а не по секретности: `compact.read` (владелец, надзор, участник — своё) отдаёт готовую строку, `compact.run` (админ) перечитывает все ходы суток и пишет в базу. Читается через `GET /api/compacts`, вкладку «Сводки» и команду `/compact` — наблюдение за системой не должно существовать только для того, кто откроет браузер. Объявленный код инцидента обязан либо выставляться детектором, либо стоять в `_WITHOUT_A_DETECTOR` с записанной причиной: объявление без механизма — обещание, на которое ссылаются.

Ключевой инвариант органов: **инициировать общение — можно, писать знание молча — нельзя.** Напоминание и дайджест уходят в чат, граф при этом не меняется — синтез это сообщение пользователю, а не канонизированное знание.

## 14.5. Отрицательный результат: падежный фильтр для `location_preposition`

**Не повторять без выборки другого размера.**

Метод `location_preposition` ловит «в/на» плюс слово с заглавной и объявляет это
местом. Замер на настоящем корпусе владельца, разметка независимая (локальная
модель, которой не сообщали ни метод, ни уверенность), 300 документов:
**8 верных из 31 — 26%**.

Разбор размеченного показал структуру, а не шум: настоящие места были из 1–2 слов,
а 18 из 23 ложных — из ТРЁХ, причём 15 с окончанием «-но» (заголовки колонок таблиц
вроде «Период Начислено Оплачено», попавшие в жадный захват). Отсюда правило:
предлог «в/на» управляет **предложным падежом**, и если пойманное слово не в нём —
захвачено соседнее. На выборке вывода правило давало **89% и не теряло ничего**.

**Проверка на 824 ДРУГИХ документах его опровергла:**

| | штук | настоящих | точность |
|---|---:|---:|---:|
| без правила | 75 | 25 | 33% |
| правило оставляет | 24 | 10 | **42%** |
| правило **выбрасывает** | 51 | **15** | 29% |

Шестьдесят процентов настоящих мест в обмен на девять пунктов точности. Правило
откачено целиком.

Почему выборка вывода польстила: 31 кандидат при 8 положительных — слишком мало,
чтобы отличить структуру от совпадения, а правило подгонялось под те же данные, на
которых потом и оценивалось.

**Две методические ошибки, сделанные по дороге, стоят упоминания отдельно.**
Первая: правило с обрезкой оценивалось по меткам, поставленным на НЕобрезанных
строках — «Министерстве финансов Российской» размечено ложным, а после обрезки это
«Министерстве финансов», размеченное настоящим; сравнение со старой меткой ничего не
значит. Вторая: переразметка обрезанных форм шла БЕЗ контекста, тогда как исходная
шла с 220 символами вокруг — инструмент поменялся между замерами, и числа снова
оказались несопоставимы.

**Что делать вместо этого.** Точность `location_preposition` — 33%, но объём мал:
80 кандидатов на 824 документа. Настоящая проблема ревью не в нём: извлечение даёт
**12 481 кандидата на 300 документов, то есть 42 на документ**, при 100% точности у
двух доминирующих методов. Очередь подтверждения нечитаема из-за ОБЪЁМА, а не из-за
шума, и следующая работа здесь — про группировку и пакетное подтверждение, а не про
пороги.

## 15. Чего здесь нет намеренно

### Семантический поиск противоречий

Предлагалось: два Knowledge Object, которые **противоречат** друг другу, должны попадать в среднюю полосу косинуса (~0.78–0.92) — достаточно близко, чтобы быть об одном и том же, достаточно далеко, чтобы не быть перефразировкой. Полоса становится дешёвым префильтром, а модель судит только то, что внутрь попало.

Посылка измерена на установленной модели (`tools/contradiction_probe.py`, `qwen3-embedding-0.6b`, продакшн-путь `EmbeddingBackend`), на четырёх отношениях: противоречие, перефразировка, обновление (более позднее состояние того же факта), несвязанное.

| отношение | n | min | медиана | max | внутри 0.78–0.92 |
|---|---:|---:|---:|---:|---:|
| противоречие | 8 | 0.665 | 0.867 | 0.941 | **37.5%** |
| перефразировка | 6 | 0.737 | 0.832 | 0.936 | **66.7%** |
| обновление | 5 | 0.545 | 0.634 | 0.723 | 0.0% |
| несвязанное | 6 | 0.153 | 0.173 | 0.186 | 0.0% |

Предложенная полоса ловит перефразировки **лучше**, чем противоречия — ровно наоборот тому, зачем строилась. Диапазоны противоречия [0.665, 0.941] и перефразировки [0.737, 0.936] перекрываются почти полностью, и это не дефект модели: эмбеддинг кодирует *тему*, а «45 тысяч» и «60 тысяч» — одна тема. Единственное, что косинус здесь разделяет надёжно, — «об одном» (≥0.5) против «о разном» (~0.17), то есть тематический фильтр, который у продукта уже есть в дедупликации.

Самый щадящий порог — `≥0.64`: ловит 100% противоречий и тащит с собой 8 из 17 не-противоречий. И это на наборе, где противоречия составляют треть всех пар; в реальном хранилище пар «перефразировка/обновление» на порядки больше, поэтому вход модели оказался бы почти целиком шумом. Хуже того, «обновления» (0.545–0.723) лежат **ниже** противоречий — порог 0.64 пометил бы противоречиями 2 из 5 обновлений, то есть именно ту законную supersession, ради которой Friday и существует.

Цена ошибки несимметрична: ложное «ваши заметки противоречат друг другу» заставляет владельца проверять то, что верно, и обесценивает все последующие сигналы. Функция не строится. Проба оставлена в `tools/` — ответ есть свойство модели, а не идеи, и при смене модели его надо перемерить, а не пересказывать.

### Порог near-duplicate: чего детектор не может

`FRIDAY_DEDUP_THRESHOLD` — косинус между векторами документов; пара выше порога становится review-gated конфликтом. Значение 0.92 никогда не измерялось. Измерено (`tools/dedup_threshold_probe.py`, 20 пар русских заметок той длины, что здесь хранится, продовый путь `knowledge_search_text` → `EmbeddingBackend`):

| отношение | n | min | max | ловится при 0.92 |
|---|---:|---:|---:|---:|
| копия | 2 | 1.000 | 1.000 | 2/2 |
| правленая копия | 2 | 0.997 | 0.999 | 2/2 |
| переформатированная | 1 | 0.888 | 0.888 | 0/1 |
| пересказанная | 2 | 0.683 | 0.888 | 0/2 |
| **та же тема, разные заметки** | 11 | 0.360 | **0.928** | **1/11** |
| несвязанное | 2 | 0.134 | 0.194 | 0/2 |

Два вывода, и оба против исходной интуиции.

**Классы перекрываются.** Настоящие дубли идут от 0.683, не-дубли доходят до 0.928 — ни один порог их не разделяет. Потолок не-дублей дают две недельные планёрки, написанные по одному шаблону: тот же проект, те же участники, те же заголовки, отличается одна строка. Две записи про одну квартиру — 0.917 и 0.914. Понедельничный и вторничный отчёт — 0.779. Лексическое перекрытие не спасает: у шаблонной пары 0.845 против 0.632 у настоящего переформатирования. Сходство текста не отличает «та же заметка ещё раз» от «следующая заметка в серии», потому что серия текстуально почти идентична по построению.

**0.92 стояло внутри распределения шума.** Не с запасом, а между 0.917 и 0.928 — попадёт ли серия протоколов в предложения на слияние, решал третий знак после запятой. Тот же дефект, что был у порога плотных доказательств в §7 (0.16 ниже медианы шума), только здесь цена ошибки — предложение слить две разные записи владельца.

Дефолт — **0.95**. Он ловит ровно столько же настоящих дублей: классы 0.888 и ниже недостижимы ни при каком безопасном значении, и попытка до них дотянуться первым делом предложит слить недельные планёрки. Детектор осознанно работает на точность: он ловит пересохранение почти дословной копии — то, чего `content_hash` не поймал, потому что текст всё же тронули, — и не претендует на большее.

Перемерить при смене модели эмбеддингов; константа `_MEASURED_NON_DUPLICATE_CEILING` в `friday/dedup.py` и тест `test_the_default_threshold_clears_the_measured_non_duplicate_ceiling` держат связь между числом и замером.

### Падежи в именах сущностей: проверено, отдельный сигнал не нужен

Русское существительное меняется в конце: `Москва / Москвы / Москве / Москвой` — одно место в четырёх формах, и каждая приходит из извлекателя отдельным узлом. Напрашивается признак «общее начало слова», которого у симметричного `SequenceMatcher` нет.

Замерено на выписанном вручную стенде склонений (37 пар форм одного имени, 276 пар разных мест; разметка из языка, а не из проверяемого признака):

- сырой `SequenceMatcher`: формы от 0.67, разные места до 0.75 — **перекрываются**;
- общее начало по словам: 33 из 37 форм при пороге 0.7 и **ноль** ложных из 276.

Признак выглядел выигрышным — и не даёт ничего. В боевой формуле уверенности стоит не сырой `SequenceMatcher`, а максимум из нескольких взвешенных сигналов, и `sorted_similarity * 0.78` с `token_jaccard * 0.90` уже закрывают многословные имена, а односложные падежные формы делят почти все символы. Замер: добавленный признак поднимает уверенность у **4 пар из 37** и **не переводит через порог ни одной** — при 0.5 старый скорер ловит 37 из 37, при 0.6 — 30 из 37.

Правка откачена. Записано, чтобы её не написали заново: ошибка была в том, что сравнивался сырой `SequenceMatcher`, а не то, что реально считает `find_duplicate_candidates`.

Отдельно: на настоящем корпусе (342 документа, 47 сущностей) 12 групп имён с общим началом оказались именами **разной длины в словах** — 1 против 2, 1 против 3 — то есть не падежными формами. Утверждать по ним, что граф полон дублей, нельзя; вопрос остаётся открытым и требует разметки, а она требует чтения имён.

### Графовый засев: документ, который сам себе доказательство

Аудит стадий поиска на настоящем корпусе (342 документа) нашёл: четыре запроса из несуществующих слов дают **ноль результатов при выключенном графе и десять при включённом**, причём у всех десяти lexical 0.0000, field 0.0000, embedding 0.0000 и graph 0.677. Причина в двух местах сразу:

- `_lexical_rank` возвращает **все** кандидаты, отсортированные по косинусу, без нижнего порога, поэтому `lexical_ranking[:8]` всегда даёт восемь «сидов» — даже когда лучший из них 0.0002;
- графового вклада 0.677 достаточно, чтобы в одиночку снять гейт `insufficient_evidence` (`grp < 0.20`).

Живой путь всегда передаёт `kg`, так что пользователь и модель в `agent_runtime` получают десять чужих личных документов вместо честного «ничего не найдено».

**Правка написана, измерена и откачена.** Фильтр сидов по `_LEXICAL_EVIDENCE_MIN` ломает ровно тот случай, ради которого граф и существует: запрос «Казань» против документа «в Казани» набирает лексически **0.0597** — ниже порога — и попадает в ответ только потому, что сам засевает граф, а его собственные сущности затем за него ручаются. Круг замкнут, и он делает полезную работу: русскую морфологию здесь больше нечем перекрыть.

То есть настоящий дефект глубже засева: **лексическое совпадение не умеет в русскую морфологию**, а графовый круг это молча компенсирует. Чинить надо в этом порядке — сначала морфология в лексике, потом честный засев. Порог трогать бессмысленно: `_prefix_similarity`-подход уже проверен и отвергнут замером (см. выше), нужен настоящий стеммер, а он меняет `normalized_name` — это миграция схемы.

### Порог плотных доказательств принадлежит корпусу, а не только модели

`FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN` поднимался с 0.16 до 0.35 по замеру на **синтетическом** корпусе: чужие пары «запрос × тело документа» давали p50 0.2361, p90 0.3255, max 0.3878.

Перемерено на **настоящем** архиве владельца — 77 проиндексированных рабочих документов, длинных, формальных, однородных:

| | p50 | p90 | p99 | max |
|---|---:|---:|---:|---:|
| чужие пары (n=770) | 0.308 | **0.427** | 0.495 | 0.568 |
| свои пары (n=122) | 0.500 | — | — | (min 0.314) |

Шум выше синтетического почти на десятую: два русских служебных документа похожи друг на друга независимо от того, о чём они. Порог 0.35 при этом пропускает **28.8% чужих пар**.

| порог | чужих проходит | своих проходит |
|---:|---:|---:|
| 0.35 | 28.8% | 97.5% |
| **0.40** | **13.9%** | **83.6%** |
| 0.45 | 5.3% | 69.7% |

Дефолт был 0.40: ложных доказательств вдвое меньше при сохранении пяти шестых настоящих. Выше брать дорого: плотный отбор существует ровно ради совпадения без общих слов, а гейт — конъюнкция, и документ с лексическим или FTS-свидетельством проходит независимо от него.

Классы **перекрывались** (свои от 0.314, чужие до 0.568) — разделяющего порога нет и он не заявляется. Число зависит от однородности корпуса не меньше, чем от модели: **перемерять, а не наследовать.**

#### Перемерено на честном индексе: 0.35

Замер выше сделан на индексе, чьи вектора считались **по обрезкам текста**: отказ сервиса эмбеддингов по длине лечился укорачиванием входов вдвое, и вектор описывал начало документа. У формальных русских документов начала похожи — шапки, реквизиты, преамбулы — независимо от содержания. Отсюда и высокий шум.

После починки (деление запроса вместо резки текста) индекс пересчитан, и совпадение вектора с полным текстом проверено: **50 из 50** против 18 из 50 до пересчёта. Перемерено на 1537 документах той же методикой — чужие пары суть вопросы о том, чего в архиве нет, причём отсутствие каждой темы **проверено поиском по телам** (3 темы из 12 отброшены, потому что нашлись):

| | p50 | p90 | p99 | max |
|---|---:|---:|---:|---:|
| чужие пары на обрезанном индексе (n=770) | 0.308 | 0.427 | 0.495 | 0.568 |
| **чужие пары на честном индексе (n=540)** | **0.2304** | **0.2981** | **0.3487** | **0.3747** |

| порог | чужих проходит | своих проходит (n=60) |
|---:|---:|---:|
| 0.30 | 9.3% | 81.7% |
| **0.35** | **0.9%** | **66.7%** |
| 0.38 | 0.0% | 60.0% |
| 0.40 | 0.0% | 53.3% |

Шум упал почти на полторы десятых, и 0.40 перестал что-либо покупать — чужие пары кончаются на 0.3747. Стоить он при этом продолжает: через 0.40 проходит половина настоящих совпадений, через 0.35 — две трети.

0.35 стоит над p99 чужих пар — то же правило «выше шума», по которому выбирались и прежние значения. Ниже нельзя: 0.30 впускает 9.3% чужих, а на полутора тысячах документов это полторы сотни кандидатов в «доказательства» для бессмысленного вопроса.

⚠️ Своя сторона с таблицей выше **несравнима**: там вопросы строились из слов самого документа, здесь их писала модель как человеческие. Сопоставлять 66.7% с 83.6% нельзя — сравнима только чужая сторона. И 60 своих пар это мало: бутстрап даёт 56.7%..76.7% при 0.35.

Оговорка о выборке: 10 запросов о предметах, которых в архиве нет, против 122 запросов, собранных из слов самих документов; проиндексировано 77 объектов из 342 (индексация остановлена, см. ниже). Направление вывода на такой выборке устойчиво, точное число — нет.

**Заодно опровергнута находка аудита №3** («длинный документ проходит гейт просто потому, что у него много чанков»). Замер на тех же векторах: на запросах о заведомо отсутствующих предметах порог проходят 22–38 документов из 77 по **вектору документа** и лишь 1–16 из 56 по агрегату чанков. Агрегат оказался строже, а не мягче; беда была в абсолютном пороге.

### Расширение по графу включается только для измеренного реляционного режима

`_prepare_context` — сбор контекста на КАЖДОЕ сообщение агента, единственный путь, которым идёт вопрос владельца в Telegram — до 2026-07-31 всегда передавал `kg` в поиск и расширялся по графу. Инструмент явного поиска `memory_search`, напротив, граф не передавал; это прежнее различие годами маскировало, что боевой путь и путь инструмента вели себя по-разному.

Проход правилом ФИО (§6, `explicit_person_patronymic`) довёл граф владельца со 110 сущностей до 4458, и на золотом наборе (20 эталонов, живой архив) расширение обнажило то, что раньше пряталось в шуме:

| | recall@10 | MRR |
|---|---:|---:|
| граф + расширение (было в бою) | 0.1500 | 0.0813 |
| граф без расширения | 0.3500 | 0.1530 |
| без графа вовсе | 0.3500 | 0.1530 |

Расширение уполовинивало recall, а не добавляло к нему. Причина — со-встречаемость через документ-концентратор (`co_occurs_in` из §7): штатное расписание на полсотни имён делает «связанными» все пары этих людей, и они вытесняют настоящий ответ из top-10. Контроль на копии без 4349 узлов, заведённых проходом ФИО, показал, что это не следствие прохода: без них канал вредил **сильнее** (recall 0.1000) — граф уже был перегружен со-встречаемостью на 110 сущностях, проход лишь сделал эффект заметным.

**Решение — отдельный параметр, не общее выключение графа.** `HybridSearcher.search(..., graph_expansion=True)`: при `False` расширение по связям и implicit `co_occurs_in` не строится, но `kg` по-прежнему используется для `entity_matches` в ответе (сущности, упомянутые запросом, для контекста агента). Замер трёх рук на одном коде показал, что вторая и третья строки таблицы выше совпадают кейс в кейс — упомянутые сущности достаются бесплатно, отключать их незачем.

`_prepare_context` теперь вычисляет `graph_expansion` через единый `is_relational_query`: обычный запрос получает `False`, а распознанный запрос об отношениях — `True`. `/api/search` принимает необязательные `as_of` и `known_at`; `memory_search` передаёт их вместе с `kg`, включая расширение для явно исторического запроса.

**Умолчание параметра — `False`, и это сторож, а не осторожность.** До 0.168.0 умолчание было `True`, то есть политику принимал тот, кто про неё промолчал. Из семи дорог, зовущих `HybridSearcher.search`, четыре объявляли режим явно, а три молчали и получали граф: публичный `GET /api/search`, админский `eval_search` и синтетический прибор `tools/retrieval_bench.py`. Практический смысл расхождения — человек в панели искал одним поиском, Пятница в чате отвечала другим, а прибор мерил третий и завышал числа за счёт канала, которого в бою нет. Теперь молчание означает измеренное обычное поведение, а расширение требуется назвать вслух; `tests/test_graph_policy_is_declared.py` держит и умолчание, и каждую из трёх дорог. Неверная дата, timestamp до completeness floor или snapshot через более поздний identity/topology change сущности отклоняются до best-effort графовой ветки и не превращаются молча в текущий graphless-ответ; default без `known_at` не менялся.

**Один снимок от ranking до ответа.** `HybridSearcher` возвращает ограниченный `graph_context`, который участвовал в ранжировании, с фактически использованным запросом (включая keyboard/query repair), `as_of`, нормализованным `known_at`, `known_at_floor`, `history_complete`, `identity_basis` и `temporal_basis` (`valid_time` либо `bitemporal`). `_prepare_context` переиспользует этот снимок без второго traversal; отдельный обход сохранён только как совместимый fallback для legacy/test searcher, который вообще не вернул новый ключ. Тот же контракт проходит через Agent Runtime/prompt и инструменты `memory_search`/`entity_lookup`; durable assistant metadata хранит эффективную transaction-time границу. `/api/search`, публичные KG и Admin graph endpoints возвращают те же provenance-поля и не проглатывают отказ снимка. В tool payload снимок дополнительно ограничен шестью путями и 3200 символами.

`as_of` фильтрует КАЖДЫЙ explicit hop по valid-time, а один нормализованный `known_at` выбирает revision для КАЖДОГО hop; явный `known_at`, как и historical `as_of`, исключает нынешние implicit `co_occurs_in`. Публичная проекция ограничена 10 стабильно отсортированными путями глубиной до 4, а каждый путь несёт согласованные score/path ID, ordered entity IDs, собственные безопасные подписи узлов и ordered edges с отдельными canonical endpoints и направлением обхода, временем и allowlisted provenance. Повреждённый, оборванный или более длинный путь отвергается целиком, а не чинится усечением. В prompt входит не более 6 целых путей. Путь получает `grounded=true` только когда доверенная provenance каждого его ребра ссылается на Knowledge Object, уже присутствующий в том же контексте как `[K#]`; ручное или неполное доказательство остаётся незаземлённым, отдельные `[G#]` не вводятся. Единственный temporal-only ranking candidate реализован и product-accepted. Его sealed-прибор manifest v2 связывает exact candidate diff, frozen gold и evaluator/helper HEAD blobs; evaluator запускается из приватной capability-bound изолированной проекции, а atomic durable one-shot latch расходуется до любой holdout-arm. После commit `8c4c334` seal check и committed calibration прошли; единственный paired holdout дал `16` wins, `0` losses, побайтно идентичный non-temporal control и ноль infrastructure failures. Latch закрывает повторный запуск.

**Картина графа живёт кадрами, а не одним прогоном (0.169.0).** Раскладка вынесена в отдельный поставляемый файл `friday/admin_ui/static/graph-layout.js` — без DOM и без глобалей, поэтому её меряют в Node, а не через браузер вместе с разметкой и перерисовкой. Попарное отталкивание заменено квадродеревом Барнса-Хата (θ=0.6, значение подобрано под заранее объявленные пороги качества, а не на глаз), и вместо одного синхронного прогона на 260 шагов симуляция делает шаг за кадр через `requestAnimationFrame`. Замер боевой функции до правки: 150 узлов — 22 мс, 1000 — 499 мс, 4500 — 15 114 мс, и всё это ДО первой отрисовки. После: полная укладка 4500 узлов — 553 мс, худший кадр 5.24 мс при бюджете 16.7 мс. Перетаскивание стало вводом в симуляцию — соседи откликаются; закрепляется РОВНО сдвинутый узел (прежде сохранялись координаты всего вида, и одно перетаскивание молча замораживало картину целиком); камера переживает смену фильтра; постоянные подписи оставлены шестидесяти самым связанным, остальные показываются по наведению. Потолок картины поднят со 150 до 1000 узлов — выше него один кадр SVG стоит 13.4 мс при 2000 и 27.2 мс при 4500, то есть нужен canvas, и это отдельная работа.

**Реляционный канал измерен отдельно.** На 12 заранее размеченных кейсах трёх классов рука `graph_expansion=true` дала 12 попаданий против 10, без потерь и без graph/reranker failures: `net_gain=2`, ровно объявленный порог. Оба выигрыша пришлись на класс сотрудничества, который прежний regex не распознавал; классификатор расширен только этой измеренной формой. Цена существенная: p50 выросла примерно с 2.48 до 4.35 с, поэтому глобальное включение по-прежнему запрещено.

**Явный отказ от реляционной оговорки также измерен отдельно.** На замороженных до обеих рук 12 русских human-authored synthetic сообщениях bare regex дал TP/FP/TN/FN `6/6/0/0`, а единственный заранее разрешённый `dismiss_explicit_prefix_v1` — `6/0/6/0`. Он подавляет только совпадение, непосредственно следующее за ограниченной формулой отказа; слова между ними либо второе живое совпадение сохраняют relational-режим. Линейное bounded-окно эквивалентно буквальному prefix-кандидату и не пересканирует растущее сообщение квадратично.

**Официальная метрика качества повторяет боевое поведение.** `_score_cases` использует тот же `is_relational_query`, поэтому `jericho doctor`, абляции и путь агента не расходятся ни для обычного, ни для реляционного режима.

С боевой конфигурацией целиком — реальные эмбеддинги и реальный переранжировщик, а не только лексика — recall@10 составил **0.60**: 12 из 20 эталонов найдены и показаны, 6 найдены, но не попали в топ-10 (задача ранжирования, не отбора), 1 не найден вовсе, 1 отсеян переранжировщиком. Прежние 0.35/0.15 в таблице выше измерены на облегчённом стенде без плотного канала — они показывают эффект от графа, а не итоговое качество поиска.
