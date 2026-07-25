# Архитектура Jericho

## 1. Границы системы

Jericho — local-first система. Единственные ожидаемые внешние соединения:

1. Telegram bridge ↔ Telegram Bot API;
2. backend ↔ локальный OpenAI-compatible vLLM endpoint;
3. `web_surfer` ↔ публичные сайты и явно настроенные search API.

Основные данные, права, история, граф, review state и очереди хранятся локально. Local-model output считается недоверенным предложением, а не источником фактов или административным решением.

## 2. Модули

| Модуль | Ответственность |
|---|---|
| `telegram_bridge` | long polling, durable inbox, команды, файлы, подпись backend-запросов |
| `ingestion` | moderate classification, Raw Object, Inbox, promotion, deterministic enrichment, advisory model refinement |
| `knowledge_graph` | сущности, связи, links к знаниям, graph context, duplicate suggestions, merge history, время событий (occurred_at) и timeline |
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
8. Retrieval собирает tenant-scoped контекст из FTS/lexical/embeddings, предметных полей, качества, lifecycle, feedback и графа.
9. Agent Runtime разделяет current conversation, personal knowledge, graph evidence и general reasoning, затем вызывает только разрешённые tools в рамках mode-specific budget. Research-результат не становится знанием, пока пользователь явно не отправит его в Inbox.
10. Ответ, сообщения и tool audit сохраняются; bridge отправляет ответ и после успеха удаляет update из durable queue. Временные ошибки получают bounded backoff, исчерпанные — retained dead-letter.

### Файл

Файл хранится в `data/files/<tenant-safe>/...`, а Raw Object содержит provenance и путь. Текст извлекается в памяти с лимитами; архивные entries не получают возможность писать в произвольный filesystem path. Для изображений и сканированных PDF формируется ограниченный набор нормализованных visual assets. Локальный vision/OCR output проходит строгую схему, считается advisory и принудительно направляется на review. Promotion применяет ту же трёхстороннюю политику к извлечённому содержимому.

Telegram-медиа (voice/audio/video/video_note/animation) скачивается мостом и проходит тот же путь. Аудио и видео не транскрибируются локально (vision-модель работает только с изображениями): они сохраняются как raw-файл с провенансом и `media_kind`, а force-pending гарантирует Inbox review. Геолокация и контакт нормализуются в текстовую заметку; неподдерживаемые типы получают ответ, а не молчаливый dead-letter. Происхождение пересланного сообщения (`forward_*`) сохраняется в `RawObject.metadata_json`.

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
- `relations` — направленная типизированная связь; `weight` — ранговый сигнал (кламп 0.1–1.0), а provenance живёт в `metadata_json`: обязательный `origin` (`api` + `created_by` для ручных рёбер, `container` для PART_OF-иерархий, `review` + `reviewed_by` + исходная `confidence` для принятых кандидатов);
- `knowledge_entity_links` — связь Knowledge Object и сущности с confidence/evidence/status;
- `entity_resolution_candidates` — reviewable предложение объединения;
- `entity_merge_history` — история принятого merge;
- `relation_candidates` — предлагаемые типизированные связи с evidence;
- `knowledge_conflicts` — потенциально несовместимые утверждения, ожидающие review;
- `feedback_state` — последняя актуальная оценка цели при сохранённой append-only истории;
- `knowledge_usage` — агрегаты retrieval/answer use без изменения содержания знания.

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

Векторы хранятся в таблице `knowledge_embeddings` (один актуальный float32-вектор на Knowledge Object) и наполняются воркером `embeddings_index`: он эмбеддит объекты без вектора, из другой модели или устаревшие по версии. Длинные объекты дополнительно режутся на перекрывающиеся пассажи, чьи векторы лежат в `knowledge_chunk_embeddings` (`JERICHO_EMBEDDINGS_CHUNK_CHARS`, `0` — выключить): целая импортированная статья иначе схлопывается в один усреднённый вектор, и один релевантный абзац в ней не находится. Пообъектный скор — `max(вектор объекта, (1−blend)·лучший пассаж + blend·среднее топ-3)`, поэтому вектор объекта остаётся **полом**: чанкинг может только добавить recall и никогда не понижает существующий результат. Он же остаётся единственным входом дедупликации — пассажи в неё не попадают. Выигравший пассаж отдаётся как evidence в ответ (его границы едут в `_embedding_chunk_span`), а `strategy.embeddings_chunked` и `components.embedding_chunk` показывают его в explain-трейсе. Запрос эмбеддится один раз и косинусно сравнивается со всеми сохранёнными векторами пользователя; сильнейшие совпадения объединяются с пулом кандидатов ДО ранжирования, поэтому смысловое совпадение без общих токенов и не из числа недавних тоже находится. Embeddings опциональны (локальный OpenAI-совместимый `/embeddings`); без них поиск работает на остальных каналах, а до первого прогона индексатора dense-сигнал мягко деградирует к эмбеддингу текущего пула.

Те же векторы используются для **дедупликации знаний** (`jericho/dedup.py`, воркер `knowledge_dedup` + `POST /api/admin/knowledge/detect-duplicates`): попарный косинус ≥ `JERICHO_DEDUP_THRESHOLD` предлагает почти-дубликат как review-gated конфликт типа `near_duplicate`. Разрешение (§9: «оставить одну») помечает вторую копию `deprecated` — автослияния нет. Скан **инкрементальный**: каждый объект ровно один раз сравнивается со **всем** корпусом (сначала свежепроиндексированные векторы, затем нисходящий backfill истории), курсор лежит в `runtime_kv` и переживает рестарт, а бюджет тика (`JERICHO_DEDUP_SCAN_BATCH`, `JERICHO_DEDUP_SCAN_MAX_SECONDS`) откладывает остаток на следующий тик вместо того, чтобы молча его потерять. Порог читается при каждом прогоне: его **понижение** запускает пересканирование (ранее отвергнутые пары становятся годными), повышение — нет. Near-duplicate — организационный сигнал: он не попадает в контекст рассуждения агента, только в review-UI.

Граф расширяется на один шаг для обычного запроса и до `JERICHO_GRAPH_MAX_DEPTH` (по умолчанию 2) затухающих шагов только для relational language (`связан`, `зависит`, `через`, `отношение` и аналоги); жёсткий потолок безопасности — 4. Дополнительно используются implicit `co_occurs_in` signals между сущностями, связанными с одним Knowledge Object. Они помечаются как implicit, не сохраняются как доказанные relations и имеют меньший вес.

Reasoning над противоречиями: каждый Knowledge Object в контексте LLM несёт `lifecycle_stage`, `updated_at` и, при наличии, `conflict` (тип + противоположная [K#]). Модель инструктирована предпочитать актуальные записи устаревшим и честно указывать на противоречие, а не выдавать одну сторону за факт. Конфликты остаются review-only предложениями (regex-предикаты uses/address/quoted_value/scheduled_date; даты нормализуются к ISO, чтобы формат не порождал ложных конфликтов).

Низкокачественные вопросы/chatter, случайно попавшие в legacy knowledge, получают штраф. Recent pool нужен для recall, но низкорелевантный объект отбрасывается: маленькая база не превращает случайную заметку в «лучший ответ» только из-за отсутствия конкурентов.

## 8. Agent context и agency

Agent Runtime строит контекст в явных слоях. Все динамические слои сериализуются как недоверенные данные пользовательского уровня и никогда не становятся system-инструкциями:

- текущая conversation window;
- найденные personal Knowledge Objects с provenance и score;
- graph entities/relations и объяснение пути;
- pending Inbox/resolution signals;
- `user_model` — производная модель пользователя (`knowledge_graph.build_user_model`: постоянные люди/проекты/интересы, ритм капчи), компактно инжектится даже в чистый диалог без хитов, чтобы ответы были личными. Правило в SYSTEM_PROMPT: это фоновый ориентир, не источник фактов — не цитируется как [K#]; сбой построения деградирует в «без модели»; выключатель `JERICHO_PROFILE_IN_CONTEXT`;
- доступные actor-у tools.

Режим ответа фиксируется как `personal_knowledge`, `mixed`, `personal_knowledge_missing` или `general_conversation`. Это помогает модели не выдавать общее знание за содержимое личной базы. Короткие follow-up реплики contextualize-ятся предыдущими turn-ами, но tenant boundary не меняется.

Атрибуция доводится до пользователя: карта `[K#]` → Knowledge Object возвращается как легенда источников (`citations`, `citation_notice`) в ответе `/api/chat`, рендерится в Telegram и в инспекторе диалога админки. Личный ответ, нашедший записи базы, но не сославшийся ни на одну, помечается `answer_grounded=false` с честной пометкой. Атрибуция консервативна: приписывается только реально процитированное (или единственный сильный хит как fallback), чтобы не искажать feedback/lifecycle-сигналы.

Высокая agency ограничена полезностью и контролем: инструмент вызывается, когда он реально улучшает ответ; proactive structuring допускает максимум одно ненавязчивое предложение, а изменение долговременной структуры не маскируется под обычный разговор.

Режимы определяют глубину работы, а не уровень доверия: `dialogue` минимизирует tool use, `knowledge_work` допускает несколько шагов над личным контекстом, `research` получает расширенный bounded budget и обязан отделять план, evidence и synthesis. Ни один режим не обходит permissions, provenance или Inbox review.

Качество ретривера измеримо: золотой набор `eval_cases` (запрос → ожидаемые Knowledge Objects) прогоняется через реальный HybridSearcher (`jericho/eval.py`, worker `retrieval_eval` + `POST /api/admin/eval/run`), считаются recall@k / precision@k / MRR, а каждый прогон сравнивается с предыдущим — падение recall@k помечается как регрессия и логируется. Набор курируется в админке через label-from-results (`GET /api/admin/eval/search`), без ручного ввода id.

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

Аутентификация HTTP тоже уважает ролевую модель: помимо owner-токена (`JERICHO_API_TOKEN`) можно выпускать scoped-токены (`api_tokens`, SHA-256), каждый из которых аутентифицирует как привязанный аккаунт с его preset/capabilities. Loopback-bypass остаётся owner для локальной машины.

SQLite работает с foreign keys, WAL и busy timeout; инициализация/migration и сложные изменения используют атомарные транзакции (`BEGIN IMMEDIATE` там, где нужно сериализовать writers). Неизвестная будущая schema отклоняется без mutation. Workers изолируют сбой одного tenant/item от остальных. Backup использует SQLite online backup API, отдельный integrity check и строгий SHA-256 manifest. Копия на том же диске — не бэкап: каждая верифицированная копия зеркалируется наружу (`JERICHO_BACKUP_MIRROR_DIR`, `jericho/backup_mirror.py`) с повторной sha256-проверкой; при заданном ключе (`JERICHO_BACKUP_ENCRYPTION_KEY_FILE`) зеркальная копия AES-256-шифруется системным `openssl` и проверяется decrypt-and-compare (локальная копия остаётся открытой для быстрого restore).

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

Модуль `organs` — плагин-фреймворк (**Jericho Organ Protocol**). Орган — самодостаточный модуль под `jericho/organs/<name>/`, подключающийся через опциональные точки расширения `capabilities()` / `workers(ctx)` / `router()`. Реестр перечислен явно (`build_registry`) — никакого dynamic discovery, набор маршрутов/воркеров/прав всегда ровно такой, как в коде. `create_app` регистрирует capabilities органа, монтирует его router и передаёт его workers в супервизор; всё аддитивно. Полный контракт — `docs/ORGANS.md`.

**Исходящий канал уведомлений** — то, что сделало проактивность возможной (раньше мост только отвечал на входящее). Backend/органы кладут сообщение в durable-очередь `outbound_notifications` (`storage.enqueue_notification`, дедуп по `(user_id, dedup_key)`); мост — единственный держатель бот-токена — тянет её подписанными `GET /api/notifications/pending` + `POST /api/notifications/ack` (гейт `source=="telegram-bridge"`) и доставляет через `sendMessage`. Backend никогда не обращается к Telegram сам и не пишет в основную БД со стороны моста. Deny-by-default проверяется дважды: allowlist при enqueue (backend) и перед отправкой (мост), потому что бот-токен технически достаёт любой чат.

**Орган `reminders`.** Worker сканирует `entity_time` (§11) на события в окне `JERICHO_REMINDERS_LEAD_DAYS`, резолвит Telegram-чат из метаданных пользователя, уважает allowlist и тихие часы, кладёт одно дедуплицированное напоминание на событие.

**Орган `reflection`** — рефлексия и синтез, полный референс (все три точки расширения). Детерминированный недельный дайджест: объекты знаний по lifecycle, «ждёт вашего решения» (входящие/связи/противоречия/устаревающие), живые темы, ближайшие события. При доступной локальной модели добавляет 2–3 предложения синтеза над темами (best-effort). Contribuтит capability `reflection.read` и on-demand `GET /api/reflection`. Тихие часы обобщены (`JERICHO_QUIET_HOURS_*`) — свойство всех проактивных органов; `ServiceContext` получил `llm`.

**Орган `profile`** — модель пользователя. Само вычисление живёт в core (`knowledge_graph.build_user_model` — его же потребляет контекст агента, см. §8); орган — тонкая capability-gated поверхность: `profile.read` + `GET /api/profile` (`?synthesize=true` — LLM-портрет). Отражение знаний, в граф не пишет; правится правкой самих знаний.

**Орган `chronicle`** — присутствие во времени / эпизодическая память. Воркер «в этот день» (знания той же календарной даты прошлых лет → resurfacing-push) + окно `GET /api/chronicle?days=7` («что было за неделю»). `chronicle.read`. Опирается на новые storage-запросы `list_entities_by_activity`/`list_recent_knowledge`/`list_knowledge_on_this_day` (без изменения схемы).

**Орган `importer`** — холодный старт. `POST /api/import` (cap `import.run`, аудит) принимает ICS-календарь, экспорт закладок (Netscape HTML), mbox-архив почты или одиночное .eml и раскладывает каждый элемент в pending Inbox через `ingest_text(force_review=True)` — bulk-материал никогда не канонизируется молча; повторный импорт идемпотентен (стабильные per-item `source_ref`: ics UID / URL закладки / Message-ID). Почта парсится из исходных байтов (stdlib `mailbox`/`email`, `policy.default`) — объявленные кодировки (cp1251/koi8-r) декодируются корректно, HTML-письма конвертируются в текст. Массовое подтверждение — существующими bulk-действиями Inbox в админке.

Ключевой инвариант органов: **инициировать общение — можно, писать знание молча — нельзя.** Напоминание и дайджест уходят в чат, граф при этом не меняется — синтез это сообщение пользователю, а не канонизированное знание.
