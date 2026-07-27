# Жизненный цикл данных

## 1. Разделение разговора и знаний

Conversation message и долговременное знание — разные сущности. Telegram/HTTP-реплика всегда может быть сохранена в разговоре, но это не означает автоматическое создание Knowledge Object.

Ingestion выбирает:

- `transient` — обычный диалог, приветствие, подтверждение, чистый вопрос или команда; долгосрочная запись не создаётся;
- `review` — материал потенциально полезен, но его долговечность/структура неочевидна; создаются Raw Object и pending Inbox suggestion;
- `promote` — материал достаточно содержателен либо пользователь явно попросил сохранить; создаётся Knowledge Object с provenance.

По умолчанию `promote` канонизируется сразу (авто-продвижение содержательного материала). Флаг `JERICHO_INGESTION_STRICT_REVIEW=1` восстанавливает инвариант «Inbox before canonical»: **не явное** авто-продвижение понижается до pending Inbox-предложения (KO не создаётся до подтверждения человеком). Явные сохранения (`/note`/`force_knowledge`, «запомни»/«сохрани») продвигаются напрямую в любом режиме — решение уже принял пользователь. Файловые загрузки трактуются как явное намерение.

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

Голосовые/аудио-заметки при включённом `JERICHO_WHISPER_ENABLED` расшифровываются локально (faster-whisper, полностью офлайн; опциональный extra `jericho[voice]`) *до* классификации — иначе они остаются un-extractable media, как раньше. Транскрипт — тоже модельный контент, поэтому идёт по тому же inbox-first advisory-пути: `extraction_succeeded` не выставляется, сущности из транскрипта капаются на ≤0.79 (`voice_transcript_advisory`), а провенанс (`metadata.transcription`: модель, язык, уверенность, длительность) отличает расшифровку от проверенного текста. KO появляется только после подтверждения человеком.

**«Игнорировать» — вердикт, а не откладывание.** Если у inbox-элемента есть привязанный KO (legacy-строки, авто-промоут, возвращённые на review), IGNORED мягко удаляет его (с пометкой `ignored_from_inbox`/`ignored_by` в metadata) и очищает ссылку — материал уходит из retrieval, Raw Object и история версий сохраняются. ARCHIVED — только уборка Inbox, KO не трогает.

## 4. Knowledge Object и версии

Knowledge Object хранит нормализованное содержимое, summary/title, kind, metadata, tags, importance, quality/promotion score, lifecycle stage и ссылку на Raw Object.

**Markdown-vault — проекция, а не архив.** Синхронизация не только пишет живые объекты, но и удаляет заметки тех, кто живым быть перестал. Раньше она только добавляла: `list_knowledge_objects` фильтрует `deleted_at IS NULL`, а `MemoryVault.delete_object` вызывался единственным местом — путём жёсткой очистки. Мягко удалённый объект (и объект, отклонённый как IGNORED) сохранял **plaintext-копию своего полного содержимого на диске навсегда**, хотя пользователю сообщили об удалении и поиск с этим соглашался; бэкапы уносили копию дальше. Уборка выполняется только после того, как страничный обход прошёл целиком, — по частичному списку удалять нельзя.

Перед изменением storage сохраняет snapshot предыдущей версии. **Чтение, слияние и запись идут одной транзакцией** — иначе это гонка read-modify-write: два редактора читают версию 1, оба вычисляют 2, и второй UPDATE затирает первый. Снимок исчезает вместе с правкой, потому что запись версии — `INSERT OR IGNORE` по паре «объект, версия», и дубликат отбрасывается молча. Воспроизведено: шесть одновременных правок давали версию **3 вместо 7** и три снимка вместо семи, без единой ошибки. То же касается `update_entity` и `merge_entities`. Human correction, re-enrichment, lifecycle transition и soft delete увеличивают version. Markdown-vault синхронизируется асинхронно, но SQLite остаётся источником истины. Любые две версии сравнимы: `GET /api/admin/knowledge/{id}/diff` (кнопка «Изменения версий» в инспекторе) отдаёт структурный diff — скаляры before→after, длинный текст unified line-diff, теги added/removed, metadata по ключам (`jericho/versions.py`).

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

## 9. Предлагаемые связи и противоречия

Фразовые и graph-сигналы могут создать `relation_candidate` или `knowledge_conflict`, но не меняют граф и lifecycle автоматически. Reviewer видит evidence, confidence и обе стороны, после чего явно принимает/отклоняет связь либо подтверждает/отклоняет/**разрешает** конфликт. Источники, версии и provenance сохраняются при любом решении.

Разрешение конфликта — это действие, а не только статус: reviewer выбирает актуальную запись (`POST /api/admin/conflicts/{id}/resolve`, кнопки «Оставить A/B»), и проигравшая становится `deprecated` со ссылкой на победителя (`superseded_by_id` + metadata `deprecated_by_conflict`), а конфликт помечается `resolved`. Проигравший версионируется, а не удаляется: остаётся в поиске, но retrieval и агент перестают выдавать его за текущий факт; решение обратимо правкой записи.

## 10. Hard delete policy

Hard delete не предоставляется обычному пользователю и не запускается worker-ом.

Рекомендуемая процедура:

1. soft delete;
2. retention window;
3. проверенный export/backup;
4. отдельное подтверждаемое administrative purge;
5. согласованная очистка raw file, vault copy, versions, graph links и backups.

Purge намеренно не автоматизирован: без общей backup/retention policy hard delete создаёт либо ложное обещание приватности, либо риск случайной потери данных.

Реализация (шаги 4–5): purge доступен только через capability `admin.data.purge` (owner+admin) и всегда пишется в аудит. `storage.purge_knowledge_object` в одной транзакции удаляет строки во всех связанных таблицах (versions, entity links, usage, embeddings — и пообъектные, и пассажные `knowledge_chunk_embeddings`, inbox, conflicts, feedback/feedback_state) в FK-порядке, зануляет висячие `superseded_by_id` и удаляет сам объект — его `AFTER DELETE`-триггер убирает запись из FTS. `jericho.purge.purge_knowledge` затем удаляет raw-файл (только если объект осиротел, файл не разделяется по content-hash и путь внутри `files_dir`) и vault-копию. Объект должен быть **сначала мягко удалён** (`deleted_at`); `list_purgeable_knowledge`/`JERICHO_PURGE_RETENTION_DAYS` (по умолчанию 30 дней) задают retention-окно. Интерфейсы: `GET /api/admin/data/purgeable`, `POST /api/admin/knowledge/{id}/purge`, и offline-команда `jericho purge --yes` (берёт эксклюзивный backend-lease). Шаг 3 (проверенный export/backup перед purge) остаётся ручным.
