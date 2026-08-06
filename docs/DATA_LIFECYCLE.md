# Жизненный цикл данных

> Проект переименован: **Friday** (по-русски — **Пятница**), ex codename Jericho.

## 1. Разделение разговора и знаний

Conversation message и долговременное знание — разные сущности. Telegram/HTTP-реплика всегда может быть сохранена в разговоре, но это не означает автоматическое создание Knowledge Object.

Ingestion выбирает:

- `transient` — обычный диалог, приветствие, подтверждение, чистый вопрос или команда; долгосрочная запись не создаётся;
- `review` — материал потенциально полезен, но его долговечность/структура неочевидна; создаются Raw Object и pending Inbox suggestion;
- `promote` — материал достаточно содержателен либо пользователь явно попросил сохранить; создаётся Knowledge Object с provenance.

По умолчанию (`FRIDAY_INGESTION_REVIEW_POLICY=assessed`) `promote` канонизируется сразу — решает классификатор. `unless_explicit` восстанавливает инвариант «Inbox before canonical»: прямое продвижение остаётся только у явного намерения (`/note`/`force_knowledge`, «запомни»/«сохрани»), всё остальное ждёт человека. `always` не продвигает ничего.

**Загрузка файла больше не считается высказыванием о содержимом.** Она остаётся явным ДЕЙСТВИЕМ, но стостраничный docx человек не читал так же, как не читал вставленный абзац, — и раньше именно файл шёл мимо ревью, а абзац в него попадал. Замер на стенде: 342 документа из 344 стали знаниями, не будучи просмотренными. При `unless_explicit` файлы идут в Inbox наравне с остальным.

`force_review` у отдельного вызова — **пол**: массовый импорт, `/api/ingest/url` и импортёр ждут решения при любой политике, потому что «указать на папку» — одно действие, а файлов в ней сотни.

Такой порядок предотвращает накопление вопросов и болтовни, не заставляя систему агрессивно классифицировать пограничные записи.

У самих диалогов есть жизненный цикл (capability `conversations.manage` для своих, `admin.all_data.manage` для чужих): архивация/разархивация (`is_archived`, выпадает из списка по умолчанию) и удаление. Удаление немедленное и каскадное (сообщения + их feedback + привязка channel session) — диалоги хранят транзиентную историю, а не provenance знаний, поэтому здесь не действует правило «soft delete до purge», как для Knowledge Objects.

## 2. Raw Object

Raw Object — неизменяемый первоисточник. Для текста хранится оригинальное содержимое и content hash; для файла — source metadata и путь к локальной копии. Soft delete не разрывает provenance автоматически.

Raw Object создаётся для `review` и `promote`. Чистый transient dialogue остаётся в conversation, чтобы не дублировать каждую реплику в knowledge storage.

## 3. Inbox review

Pending Inbox содержит deterministic proposal: title, summary, kind, importance, tags, metadata, entities, promotion/quality score и объяснение решения.

Reviewer может:

1. принять promotion без правок;
2. исправить поля и затем promote;
3. оставить pending/postponed;
4. отклонить как transient/low-value;
5. запросить advisory-only уточнение локальной моделью;
6. связать или исправить entity link.

Model advice не является решением: он не меняет status/reviewer fields, не создаёт Knowledge Object/graph nodes и не выполняет merge.

**Vision/OCR, расшифровка голоса и неизвлекаемые медиа — inbox-first.** Файл, чей текст получен моделью зрения (или не извлечён вовсе — аудио/видео без расшифровки), не создаёт Knowledge Object при ингестии: он ждёт в pending Inbox без KO, а все vision-предложения (заголовок, summary, сущности с advisory-капом уверенности ≤0.79) хранятся в suggestions. Подтверждение строит KO из этих suggestions (deferred promotion — тот же механизм, что и strict-review); модельный контент не попадает в поиск до решения человека.

Голосовые/аудио-заметки при включённом `FRIDAY_WHISPER_ENABLED` расшифровываются локально (faster-whisper, полностью офлайн; опциональный extra `jericho[voice]`) *до* классификации — иначе они остаются un-extractable media, как раньше. Транскрипт — тоже модельный контент, поэтому идёт по тому же inbox-first advisory-пути: `extraction_succeeded` не выставляется, сущности из транскрипта капаются на ≤0.79 (`voice_transcript_advisory`), а провенанс (`metadata.transcription`: модель, язык, уверенность, длительность) отличает расшифровку от проверенного текста. KO появляется только после подтверждения человеком.

**Исходный текст ищется, отвергнутый — нет.** `GET /api/knowledge/sources` и `jericho search-source` ищут по `raw_objects` (см. ARCHITECTURE §7). Материал с вердиктом IGNORED в выдачу не попадает никогда; вернуть его в поиск можно единственным способом — вернув элемент в Inbox, то есть новым решением человека.

**«Игнорировать» — вердикт, а не откладывание.** Если у inbox-элемента есть привязанный KO (legacy-строки, авто-промоут, возвращённые на review), IGNORED мягко удаляет его (с пометкой `ignored_from_inbox`/`ignored_by` в metadata) и очищает ссылку — материал уходит из retrieval, Raw Object и история версий сохраняются. ARCHIVED — только уборка Inbox, KO не трогает.

## 4. Knowledge Object и версии

Knowledge Object хранит нормализованное содержимое, summary/title, kind, metadata, tags, importance, quality/promotion score, lifecycle stage и ссылку на Raw Object.

**Markdown-vault — проекция, а не архив.** Синхронизация не только пишет живые объекты, но и удаляет заметки тех, кто живым быть перестал. Раньше она только добавляла: `list_knowledge_objects` фильтрует `deleted_at IS NULL`, а `MemoryVault.delete_object` вызывался единственным местом — путём жёсткой очистки. Мягко удалённый объект (и объект, отклонённый как IGNORED) сохранял **plaintext-копию своего полного содержимого на диске навсегда**, хотя пользователю сообщили об удалении и поиск с этим соглашался; бэкапы уносили копию дальше. Уборка выполняется только после того, как страничный обход прошёл целиком, — по частичному списку удалять нельзя.

Перед изменением storage сохраняет snapshot предыдущей версии. **Чтение, слияние и запись идут одной транзакцией** — иначе это гонка read-modify-write: два редактора читают версию 1, оба вычисляют 2, и второй UPDATE затирает первый. Снимок исчезает вместе с правкой, потому что запись версии — `INSERT OR IGNORE` по паре «объект, версия», и дубликат отбрасывается молча. Воспроизведено: шесть одновременных правок давали версию **3 вместо 7** и три снимка вместо семи, без единой ошибки. То же касается `update_entity` и `merge_entities`. Human correction, re-enrichment, lifecycle transition и soft delete увеличивают version. Markdown-vault синхронизируется асинхронно, но SQLite остаётся источником истины. Любые две версии сравнимы: `GET /api/admin/knowledge/{id}/diff` (кнопка «Изменения версий» в инспекторе) отдаёт структурный diff — скаляры before→after, длинный текст unified line-diff, теги added/removed, metadata по ключам (`friday/versions.py`).

## 5. Lifecycle stages

- `active` — участвует в поиске с полным весом;
- `archived` — доступен, но ranking снижен;
- `deprecated` — явно устаревший объект с ещё меньшим весом;
- `deleted` — скрыт из обычного доступа, история и provenance сохранены.

Lifecycle worker ничего не архивирует автоматически. Он формирует read-only список кандидатов с причинами и защищает недавно использованные, положительно оценённые, вручную созданные и file-derived знания. Изменение importance/lifecycle применяется только к явно выбранным объектам через capability-gated Admin action.

## 6. Устаревание, качество и legacy cleanup

Пользователь/администратор может изменить importance/lifecycle, создать исправленную версию, мягко удалить или оставить feedback.

Для исторически накопленного мусора действует двухфазный процесс:

1. read-only scan повторно оценивает объекты и показывает кандидатов с reasons/signals;
2. администратор явно выбирает IDs и действие.

Безопасные действия:

- вернуть Knowledge Object в pending Inbox, исключив его из обычного retrieval до решения;
- re-enrich/reclassify с новой version snapshot;
- явно подтвердить объект как проверенный (`keep`);
- перевести в `archived`;
- выполнить soft delete с сохранением Raw Object, snapshot и audit trail;
- ничего не менять после preview.

Ни worker, ни scan не применяют изменения автоматически. `POST /api/admin/lifecycle/deprecate` **требует явного списка `ids`** и отвечает теми же защитами, что и read-only-скан кандидатов: файловое происхождение, явное сохранение или human review, положительный feedback, недавнее использование. Выбранный, но защищённый объект возвращается в `skipped` с причиной — он был выбран человеком, и тишина здесь неприемлема. Прежде маршрут архивировал **каждый** активный объект с `importance < 0.3` старше порога, без выбора и без единой из этих защит. Hard delete, hidden deprecation, массовая переклассификация и silent merge отсутствуют.

## 7. Entity resolution

Варианты имени и потенциальные дубликаты не объединяются автоматически при неопределённости. Candidate хранит пару, confidence, method и evidence.

После решения:

- `merged` — links/relations/aliases перенесены, source остаётся в merge history;
- `rejected` — та же пара не предлагается заново как новая;
- `suggested` — ожидает review.

Reviewer явно выбирает canonical target. Exact identifiers объединяются только по exact identity, без morphology/prefix matching.

## 8. Feedback

Feedback не переписывает историческое знание напрямую. Append-only события сохраняют аудит, а отдельное текущее состояние даёт ranking последнюю оценку вместо среднего по уже отменённым реакциям. Оценка ответа связывается только с Knowledge Objects, реально использованными при его формировании; usage хранится отдельно.

Повторные отрицательные решения review/promotion могут консервативно понизить похожий будущий материал с автоматического `promote` до `review`. Контур не выполняет повышение, merge, relation creation, archive или delete и не имеет приоритета над явными «запомни»/«не запоминай».

### Жизненный цикл личного напоминания

Личное напоминание может физически жить в SQLite общего архива, но не становится
общим знанием. При создании event, расписание и
`private_entity_owners(entity_id, person_id, 'reminder')` фиксируются атомарно.
Startup-миграция переносит marker только по точному reminder provenance и
сохранённой merge lineage; неоднозначная legacy-строка карантинится, а не
объявляется public. Под одним `BEGIN IMMEDIATE` startup сначала проверяет shape
cache/work/state и allowlist уже установленных persistent UDF-free guards,
выполняет marker backfill и безопасный owner move, затем после регистрации UDF
создаёт connection-local TEMP views/triggers, ordered rebuild entity → Raw →
Knowledge/hidden → Inbox через две пары work/cache и требует их точного равенства
live authority. Persistent guards инвалидируют нужный state даже при raw SQLite
write; обычный managed insert/non-flipping update возвращает valid локальной
ID-правкой, privacy flip — общим rebuild в той же транзакции, иначе exact heal
выполняет новое соединение. До возврата соединения приложению ставится
per-connection authorizer; неизвестный artifact, shape или trigger прерывает
startup.

Карантин применяется ретроактивно. После появления marker уже существующие копии
ID, current или authenticated historical имени/алиаса напоминания в текущих
Raw/Knowledge Objects, historical snapshots, Inbox suggestions, entity cards,
relation/resolution evidence, feedback/eval, notifications и кэшах перестают быть
читаемыми generic-путями. Bounded alias containers разворачиваются рекурсивно, а
NFC → casefold → NFC не оставляет обхода через регистр или NFD. Зависимость
проверяется до модели, API и административной агрегации; неизвестный, повреждённый
или слишком большой provenance означает «не доказано public». Merge/unmerge,
review, restore version, relation/candidate decisions, feedback mutation и purge
повторяют проверку в write transaction, поэтому старый ID или alias нельзя
использовать как ссылку для изменения скрытой строки.

Authenticated history участвует в классификации: очистка только current-карточки
не делает public сущность, чья старая версия всё ещё несёт private ID, имя или
alias. Обычный mutation path не может сам снять такой карантин — он видит цель как
отсутствующую. Добавить ещё один sanitized snapshot недостаточно, пока прежняя
authenticated version остаётся authority. Санкционированная редакция/retirement
history потребует отдельной owner-only destructive capability, code-owned rebuild
cache/work/state и политики для backup/export; скрытого автоматического стирания
provenance нет.

Владелец напоминания продолжает видеть его через person-scoped reminder/timeline
путь только при точном совпадении marker и `entity_time.source`, валидном
current/history material и отсутствии копии другой cached private identity. Этот
read идёт по готовому cache; public carriers собственного reminder остаются
скрытыми консервативно. Delayed derived
notification после quarantine не отправляется; допустима лишь исходная reminder
delivery тому же человеку. Vault prune после записи повторно берёт финальный
privacy-filtered live set, а response/idempotency caches, которые могли хранить
старый payload, одноразово очищаются с `secure_delete` и WAL checkpoint.

Экспорт человека собирается в одной SQLite snapshot и строит отдельный fixed point
от всех запрещённых direct-private seeds. Из seed-set исключается только точное
непротиворечивое marker/time/source этого человека: поэтому его private rows и
dependencies только от них сохраняются, а foreign, ambiguous, malformed и
транзитивно производный material исключается. Entity/time/marker, ссылки, версии,
merge histories, current и historical relations, candidates/resolutions, evidence,
feedback и monitor provenance пересматриваются до неподвижной точки. Самостоятельные
старые backup/export не меняются автоматически; их удаление или ротация остаётся
отдельным подтверждённым retention-действием владельца.

## 9. Предлагаемые связи и противоречия

Фразовые и graph-сигналы могут создать `relation_candidate` или `knowledge_conflict`, но не меняют граф и lifecycle автоматически. Reviewer видит evidence, confidence и обе стороны, после чего явно принимает/отклоняет связь либо подтверждает/отклоняет/**разрешает** конфликт. Источники, версии и provenance сохраняются при любом решении.

Разрешение конфликта — это действие, а не только статус: reviewer выбирает актуальную запись (`POST /api/admin/conflicts/{id}/resolve`, кнопки «Оставить A/B»), и проигравшая становится `deprecated` со ссылкой на победителя (`superseded_by_id` + metadata `deprecated_by_conflict`), а конфликт помечается `resolved`. Проигравший версионируется, а не удаляется: остаётся в поиске, но retrieval и агент перестают выдавать его за текущий факт; решение обратимо правкой записи.

### Transaction-time подтверждённых отношений

`relations` — текущая проекция, а каждое её содержательное состояние сохраняется
append-only в `relation_revisions`. INSERT, UPDATE и DELETE фиксируются DB triggers;
DELETE оставляет `present=0` tombstone. Одна внешняя storage-транзакция даёт всем
её изменениям общий `recorded_at` и `batch_id`, поэтому массовое решение,
merge/unmerge и rollback не показывают половинчатое состояние.
Если системные часы идут назад, следующая managed или прямая SQL-мутация получает
строго большую transaction-time границу. Одинаковый timestamp допустим только
событиям одного outer batch; внутри него их упорядочивает append-only `event_seq`.
Допустимый явный `known_at` до ответа атомарно сохраняется в singleton
`relation_revision_context.observed_at`. Этот durable logical clock переживает
restart, не уменьшается и входит в authority следующей записи, поэтому пустой срез
между последним событием и wall time также остаётся неизменным после clock rewind.
Следствие осознанное: historical read локально пишет только этот watermark.

`as_of` отвечает «когда связь была верна» по `valid_from`/`valid_to`; `known_at`
отвечает «что Friday уже знала» по revision `recorded_at`. Схема 31 начинает
гарантированно полную историю с неизменяемого `relation_history_complete_from`:
миграционный baseline записан на настоящий момент миграции, а не задним числом.
Floor канонизирован, связан со своим immutable provenance stamp и baseline; startup
отклоняет рассогласование, captured revision не позже floor, убывающую хронологию
и повтор одной границы в разных batch, а также history evidence позже сохранённого
`observed_at`. Сам singleton и его non-decreasing boundary защищены от
UPDATE/DELETE/REPLACE отдельными triggers. Запрос до
этой границы отклоняется. Исторические имена сущностей, entity topology и
полная история knowledge links не восстанавливаются: допустимый снимок использует
текущие имена (`identity_basis=current_names`), а пересечение более позднего
merge/unmerge, soft-delete/undelete либо смены canonical/merged target отклоняется
fail-closed. Name-only edit границу не ломает именно потому, что basis объявлен явно.

Schema 32 добавляет эти singleton/REPLACE guards как отдельную проверяемую миграцию,
а не молча меняет schema 31 под прежним номером. Ранняя schema 31 сначала проходит
точную structural и semantic проверку; затем один schema transaction сохраняет все
revisions/floor/event sequence, строит `observed_at` из максимума уже существующей
relation/identity time authority, устанавливает v32 DDL и ещё раз валидирует полный
контракт до смены marker. В полный контракт входит и UNIQUE-index surface всех
guarded tables: дополнительный конфликтный индекс не может изменить семантику
`REPLACE` и вытеснить evidence/floor мимо DELETE-trigger. Ошибка на любом шаге
оставляет прежнюю schema 31 целиком.

Tenant export включает его `relation_revisions`; verified SQLite backup сохраняет
их и completeness floor вместе с остальной БД. Это provenance подтверждённого
решения, поэтому metadata snapshots остаются tenant-scoped и не публикуются
целиком в graph/API.

### Privacy-редакция старого audit trail

Обычные audit events append-only и не переписываются lifecycle-операциями. Есть
одна узкая системная миграция v3: прежние JSON и все scalar columns, которые могли
содержать пользовательский payload, один раз заменяются content-free проекцией.
Это не удаление события: actor, action, время и безопасная расследовательская
структура сохраняются; точные IP/generated IDs требуют доказанного provenance, а
private labels/request IDs/unproven IP/ID и content fingerprints коррелируются
installation-local domain-separated HMAC refs. Миграция переводит и v2 plain SHA
fingerprints, атомарно возвращает append-only triggers, требует `secure_delete` и
очищает WAL до отметки о завершении.

Миграция действует только на активный SQLite-файл и его WAL/SHM. Она не обходит
retention и не удаляет автономные резервные копии: старый backup может всё ещё
содержать исходный payload, пока не истечёт его отдельно утверждённый срок хранения.
После восстановления такой копии startup снова применит privacy-редакцию до работы
приложения.

## 10. Hard delete policy

Hard delete не предоставляется обычному пользователю и не запускается worker-ом.

Рекомендуемая процедура:

1. soft delete;
2. retention window;
3. проверенный export/backup;
4. отдельное подтверждаемое administrative purge;
5. согласованная очистка raw file, vault copy, versions, knowledge graph links и backups.

Purge намеренно не автоматизирован: без общей backup/retention policy hard delete создаёт либо ложное обещание приватности, либо риск случайной потери данных.

Реализация (шаги 4–5): purge доступен только через capability `admin.data.purge` (owner+admin) и всегда пишется в аудит. `storage.purge_knowledge_object` в одной транзакции удаляет строки во всех связанных таблицах (versions, entity links, usage, embeddings — и пообъектные, и пассажные `knowledge_chunk_embeddings`, inbox, conflicts, feedback/feedback_state) в FK-порядке, зануляет висячие `superseded_by_id` и удаляет сам объект — его `AFTER DELETE`-триггер убирает запись из FTS. `friday.purge.purge_knowledge` затем удаляет raw-файл (только если объект осиротел, файл не разделяется по content-hash и путь внутри `files_dir`) и vault-копию. Объект должен быть **сначала мягко удалён** (`deleted_at`); `list_purgeable_knowledge`/`FRIDAY_PURGE_RETENTION_DAYS` (по умолчанию 30 дней) задают retention-окно. Интерфейсы: `GET /api/admin/data/purgeable`, `POST /api/admin/knowledge/{id}/purge`, и offline-команда `jericho purge --yes` (берёт эксклюзивный backend-lease). Шаг 3 (проверенный export/backup перед purge) остаётся ручным.

Эта capability удаляет **Knowledge Object**, но не притворяется стиранием отдельно
подтверждённого отношения. Его append-only revision может содержать собственное
relation metadata/evidence и переживает purge документа-основания; иначе прошлый
ответ системы перестал бы быть воспроизводимым. Отдельной capability для
санкционированного стирания relation history сейчас нет. Такое стирание должно
быть самостоятельной destructive operation с явной политикой и не может скрыто
обходить append-only triggers.
