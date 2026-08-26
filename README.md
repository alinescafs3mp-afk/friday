# Friday

**Friday** (по-русски — **Пятница**; ex codename Jericho) — локальная многопользовательская Knowledge Operating System: она принимает текст и документы, сохраняет первоисточник, строит граф знаний, ищет по личной базе и отвечает через Telegram или HTTP API. Веб-панель предназначена для администрирования, разбора Inbox, работы с сущностями, правами, резервными копиями и диагностикой.

Текущая версия: **0.207.46**. Авторизованный read-only `archive_search`
объединяет личные документы, знания, сообщения и Obsidian с точными
источниками, покрытием и финальной повторной проверкой прав. Schema 43
добавляет durable immutable Host Action jobs и append-only lifecycle events для
выключенного по умолчанию Host Capability Plane. Schema 42
обслуживает активный restart-safe путь сравнения: точный результат выбранных
сообщений продолжается через durable Q1/Q2 выбора документа, затем два
закрытых model-вызова синтезируют и независимо проверяют ответ перед
повторной авторизацией источников и атомарной публикацией. Schema 41
использует rebuildable body-free DocumentCatalog с bounded durable
обогащением, точно привязанный к версии
авторитетного Raw Object и состоянию извлечения; schema 40 добавила immutable
body-free набор archive-кандидатов и durable ordinal question. Добавлен
выключенный по умолчанию контур
опционального GPT-OSS secondary brain: отдельный private-CA endpoint,
строго привязанный MXFP4 profile, bounded дедлайны и fail-soft fallback.
Первичная модель остаётся финальной; secondary не имеет доступа к
инструментам, эффектам и публикации. Obsidian core остаётся opt-in beta,
а companion plugin не требуется. Opt-in V12 routes `file_read` и `archive_read`
аттестуются как `qwen38-27b-nvfp4-sglang:dispatcher:v12.15` на точном
graph-only SGLang deployment; всё неподдержанное остаётся в legacy без
урезания контекста, concurrency или CUDA graphs. После точного выбора
архивного источника закрытый follow-up на объяснение использует этот
аттестованный endpoint в двух проходах (синтез и независимая проверка), сохраняет
точные passage-citations и безопасно откатывается к дословным фрагментам при
любой недоступности ноутбука или drift. Иерархический MAP больших и byte-safe MAP
обычных текущих документов могут использовать валидированный GPT-OSS assist, но
final synthesis по-прежнему принадлежит primary, а любой сбой даёт тот же primary-only путь.
Assist открывается только точным v2 policy и одноразовым live-shadow receipt;
secondary не получает tools, effects или права прямой публикации.

```text
Telegram → подписанный durable bridge → Conversation + mode
                                      dialogue / knowledge_work / research
                                                   ↓
                                       Ingestion decision
                                 transient / review / promote
                                           ↓          ↓
                                    Raw Object      Inbox proposal
                                           └──────┬──────┘
                                                  ↓ explicit review
                                           Knowledge Object
                                                  ↓
                     Feedback ↔ Retrieval ↔ Knowledge Graph ↔ review queues
                                                  ↓
                              Agent Runtime + bounded tools → ответ
```

## Что уже реализовано

### Качество ingestion и promotion

- Каждое сообщение получает объяснимое решение `transient`, `review` или `promote` с версией политики, promotion/quality score, положительными сигналами и штрафами.
- Приветствия, подтверждения, чистые вопросы и команды почти всегда остаются только в разговоре. Явное «запомни/сохрани» считается намерением пользователя сохранить материал.
- Пограничный контент не превращается в долгосрочное знание молча: создаются Raw Object и pending Inbox item с готовым предложением для ревью.
- Enrichment формирует предметные title, summary, knowledge kind, importance, tags, URL/date/action-item metadata и консервативные сущности вместо декоративных пустых полей.
- Pending Inbox можно безопасно уточнить локальной моделью. Её ответ advisory-only: он не меняет статус, не создаёт Knowledge Object или сущности и не выполняет merge.
- Повторный отрицательный feedback на похожие автоматические promotions может только понизить будущий материал до `review`; он никогда не повышает сомнительный контент и не перебивает явные «запомни»/«не запоминай».
- Изображения и сканированные PDF проходят ограниченный локальный vision/OCR-контур. Evidence привязывается к конкретным страницам/изображениям и цитируемым фрагментам; незаземлённый или низкокачественный output получает общий confidence cap, принудительно остаётся на review и не создаёт уверенные graph links сам по себе.
- Native DOCX/XLSX получают отдельный content-free `OfficeStructureIndex v1`: прежний извлечённый текст не меняется, а точные spans, порядок блоков, row/cell IDs и однозначные person-record sets позволяют коду считать и перечислять полный состав без доверия генеративной модели. Формулы без cache, скрытый legacy-parser-ом текст, nested/merged ambiguity и любой бюджет снимают полноту fail-closed.
- Неизменяемый первоисточник (`Raw Object`) обязателен для каждого `Knowledge Object`; provenance, version snapshots и soft deletion сохраняются при исправлениях.

### Knowledge Graph, retrieval и агент

- Граф сущностей, типизированных отношений и knowledge links является рабочим источником контекста, а не только визуализацией.
- Entity extraction распознаёт явно названные проекты, инфраструктуру, технологии и версии, организации, события, локации, документы, людей и точные коды (`BRK.A`, `BRNQ26`, ISIN) с confidence/evidence. Пунктуация идентификаторов сохраняется, fuzzy/prefix merge для них запрещён.
- Entity Resolution учитывает точные алиасы, сходство имён, аббревиатуры, общие knowledge links и соседей. Сомнительные сущности только предлагаются к объединению; canonical target выбирает человек, а история merge сохраняется.
- Гибридный поиск объединяет SQLite FTS, lexical similarity, optional embeddings, точные идентификаторы, предметные поля, graph evidence, importance, lifecycle, feedback, quality и promotion confidence.
- FTS-индексы используют SQLite `unicode61`; русская нормализация, Snowball-основы и
  группы `ё/е` строятся в query layer. Английский `porter` не применяется;
  недоступный в pinned SQLite `stemmer language='ru'` и глобальный `trigram`
  не включаются вместо доказанного scoped-поиска.
- Явный `source_search` ищет по исходному тексту загруженных файлов, включая pending Inbox до promotion, но не подмешивает Raw-корпус в обычный контекст. Выдача tenant-scoped, ограничена query-aware выдержками и исключает ignored/deleted/private-dependent материал; pending никогда не называется уже сохранённым знанием.
- Длинные продвинутые **Knowledge Objects** индексируются ещё и по пассажам:
  один релевантный абзац целой статьи находится, а вектор всего Knowledge Object
  остаётся полом скора (чанкинг может только добавить recall). Выигравший
  пассаж и цитируется в ответе; pending Raw Objects и переписка этой гарантией пока
  не охвачены.
- Холодный passage-скан выбирает SQL-план по состоянию индекса: плотный актуальный корпус читается сразу в порядке объектов без сортировки всех BLOB-строк, а sparse/rolling корпус сохраняет дешёвый chunk-first путь. Окно newest-N имеет полный tie-break по ID; обе стороны денормализованного tenant ключа проверяются fail-closed.
- Для relational-запросов используется аккуратно затухающее двухшаговое расширение графа; обычный поиск остаётся одношаговым, чтобы не тащить шум. Явно отброшенная реляционная оговорка не включает дорогую дорогу, но вводная часть перед настоящим вопросом и второе неподавленное совпадение его не скрывают.
- Ранжирование, Agent Runtime и `memory_search` используют один и тот же ограниченный `graph_context`, без повторного обхода графа. Две временные границы независимы: `as_of` означает valid-time («когда связь была верна»), а offset-aware RFC3339 `known_at` — transaction-time («что Friday уже знала к этому моменту»). Обе применяются на каждом шаге; явный исторический снимок не подмешивает сегодняшние implicit `co_occurs_in`.
- Transaction-time отношений хранится append-only в `relation_revisions`, тогда как `relations` остаётся быстрой текущей проекцией. История полна только начиная с неизменяемого migration floor схемы 31; даже при откате системных часов разные commit получают строго возрастающие границы, а одно время делят лишь события одного atomic batch (`event_seq` задаёт их порядок). Принятый явный `known_at` сначала сохраняется в локальном persistent logical clock, поэтому даже пустой уже выданный срез не меняется после clock rewind; historical read осознанно делает эту маленькую запись. Более ранний `known_at` и снимок, пересекающий последующий identity/topology change сущности (merge/unmerge, soft-delete/undelete, смену canonical/merged target), отклоняются, а не подменяются текущим графом. Изменение только имени допустимо: исторические имена пока не обещаются, и публичный ответ явно сообщает `identity_basis=current_names`.
- Снимок содержит не более 10 устойчиво упорядоченных путей глубиной до 4 с направлением обхода и утверждения, временем и allowlisted provenance. В prompt попадает не более 6 путей; `grounded=true` возможен только через доверенное доказательство, связанное с уже переданным модели Knowledge Object `[K#]`, без отдельной непроверяемой системы `[G#]`.
- `/timeline` и одноимённая команда Telegram сводят в одну хронологию события и известные valid-time границы отношений: подтверждение по `valid_from` и завершение по `valid_to`. Общий лимит применяется после смешивания, полный размер периода сообщается отдельно, а неизвестное начало не выдумывается из transaction-time `created_at`.
- Низкокачественный legacy chatter и плохо классифицированные объекты получают noise penalty и не должны вытеснять хорошие знания.
- Agent Runtime собирает контекст отдельно из текущего разговора, личных знаний, графа, pending review-сигналов и разрешённых tools; различает ответ из базы, смешанный ответ и общий разговор.
- Режимы `dialogue`, `knowledge_work` и `research` дают разные tool/step budgets. `knowledge_work` объединяет несколько Knowledge Objects и графовый контекст в структурированный work product с маркерами источников `[K1]`, `[K2]`; `research` выполняет bounded synthesis. Любой такой результат сохраняется только как Inbox candidate для явной проверки.
- Текущее состояние feedback и статистика фактического использования Knowledge Objects участвуют в ranking; история feedback остаётся append-only, последняя оценка заменяет отменённую старую, а attribution привязывается к действительно процитированным/использованным знаниям.
- Предлагаемые отношения и потенциальные противоречия попадают в отдельные review-очереди с одиночными и массовыми действиями. Принятые/отклонённые решения терминальны и не переоткрываются фоновым обнаружением; ни связь, ни устаревание, ни конфликт не применяются молча.
- На пустой или маленькой базе агент не придумывает личные факты и прямо объясняет, чего в знаниях пока нет. Proactive structuring ограничена одним уместным предложением.

### Администрирование, безопасность и эксплуатация

- Строгая tenant isolation действует на SQL, graph, conversations, files, feedback, Admin API и tools.
- Opt-in V12 routes `file_read` и `archive_read` используют одни и те же fail-closed evidence-границы: архивный selector принадлежит коду и ограничен собственными файлами actor, авторизация завершается до чтения body, а перед публикацией точный selector и каждый источник повторно проверяются в той же SQLite-транзакции. Idempotency fence ставится через уже удерживаемое соединение атомарно с единственной публикацией. Узкие V12-полномочия не расширены; общая schema теперь **35**.
- В shared archive личное напоминание остаётся данными конкретного человека: durable owner marker создаётся атомарно с событием, а generic retrieval/model/graph/organs/admin исключают полное dependency closure по ID, current и authenticated historical именам/алиасам. Alias containers рекурсивно декодируются в bounded budget, а сравнение NFC → casefold → NFC закрывает иной регистр и NFD. Только точный person-scoped reminder path может вернуть и доставить запись владельцу; person export из одной snapshot отдельно разрешает его непротиворечивые marker/time/source и производные только от них, не открывая чужой или неоднозначный material.
- Capability-based permissions используют default deny, preset-ы `owner`, `admin`, `moderator`, `user`, `guest`, custom presets и явные allow/deny overrides без обходного повышения прав.
- В общем архиве надзор за поступлениями одного человека отделяет tenant от точного `uploaded_by`: лента, сводка, ритм, объём, темы и сравнение двух периодов считают один и тот же авторский срез. Материалы без достоверной отметки никому не приписываются и показываются отдельным числом.
- Поиск материалов, загруженных выбранным человеком в общий архив, применяет точное `uploaded_by` на исходном Raw Object до каждого FTS/LIKE, recent/date, whole-document и passage-vector `LIMIT`; reranker видит только это множество. Shared-дорога помечена `scoped_hybrid`, обходит tenant-wide resident cache и не читает общий graph/entity-контекст, пока у графа нет достоверного авторского provenance. Без HybridSearcher сохраняется честный `scoped_lexical` fallback.
- Admin UI/API поддерживают массовый triage Inbox, ручное promotion/correction, model advice, inspection provenance/versions/entity links, Entity Resolution с выбором canonical target, очереди связей/конфликтов, quality dashboard, explain-трейс ретривера (почему запись нашлась/отброшена/так ранжирована) и безопасную ревизию legacy-мусора.
- Вкладка «Граф» — инструмент исследования, а не картинка: общий вид и окрестность узла с глубиной 1–4, фильтры по типу сущности, виду связи, уверенности и двум независимым временным границам (`as_of` — «когда было верно», `known_at` — «что уже было известно»), перетаскивание узлов с запоминанием раскладки, легенда только встреченных видов связи. Поиск подсвечивает не только найденные узлы, но и **путь** от узла-фокуса до них — поиском в ширину по НАРИСОВАННЫМ рёбрам, потому что путь через отсеянное фильтром ребро показывал бы связь, которой на этой картине нет. На общей картине точки отсчёта нет, и вид говорит об этом прямо: молчание читалось бы как «пути нет».
- Веб-страницы сохраняются по URL (`POST /api/ingest/url`, только публичные адреса, очистка и review-gate) прямо из панели или в один клик через букмарклет «Сохранить в Friday» — он открывает панель с адресом текущей страницы, не храня токен в закладке.
- Правило хозяина сайта читается: перед загрузкой страницы спрашивается `robots.txt` (один раз на сайт), запрет отвечает названной причиной, а не пустой страницей, и `Crawl-delay` сайта заменяет наше умолчание, если он больше. Недоступные правила означают «разрешено» — иначе сбой сети стал бы запретом на весь интернет.
- Выход в интернет ограничен по числу и вежлив по темпу: не больше 400 обращений на человека за сутки (`FRIDAY_WEB_DAILY_QUOTA`, ноль — без ограничения) и не чаще раза в секунду к одному сайту (`FRIDAY_WEB_HOST_PAUSE_SEC`). Потолок взят замером — пик на живом архиве 135 вызовов на человека за сутки, — потому что защита нужна не от работающего человека, а от зациклившегося исследования, которое тратит платный ключ и портит репутацию адреса. Исчерпанная квота отвечает причиной и числом, а не пустой выдачей: молчание модель пересказала бы как факт об интернете.
- Выдача из интернета — это разные источники, а не один сайт восемь раз: адреса склеиваются канонически (схема, `www.`, `utm_*`, якорь, хвостовой слэш), и одному сайту достаётся не больше двух мест. Замерено на десяти живых запросах: в девяти из десяти домен занимал два места и больше, 21 «лишний» результат из 80 — а канонизация ловила из них лишь 2, потому что зеркала оказались разными страницами одного сайта. Отброшенные по потолку возвращаются в конец, если мест осталось больше: узкий вопрос, на который отвечает один сайт, не должен схлопывать выдачу до двух строк.
- `web_search` поддерживает строгие `site`/`include_domains`/`exclude_domains`, окно `freshness` и ISO-коды `lang`/`region` для локализации выдачи. Allow/deny проверяются повторно по hostname уже внутри Friday; deny-list не отправляется поисковикам, а недобор не заполняется запрещёнными строками. Локаль никогда не теряется молча при fallback: неспособный адаптер отказывается до сети, а язык/рынок честно остаются предпочтением ранжирования, не обещанием происхождения каждой страницы.
- Legacy quality и lifecycle scan только показывают кандидатов. По выбранным объектам администратор может вернуть материал в Inbox, переобогатить, явно подтвердить, снизить importance, архивировать или выполнить soft delete; worker никогда не применяет эти действия автоматически.
- Telegram bridge использует устойчивую SQLite-очередь, persistent offset, OS-backed singleton lease, idempotency, bounded retry/dead-letter и HMAC-подпись backend-запросов. Временная ошибка inline-действия сохраняет кнопку для безопасного повтора.
- Backend захватывает durable idempotency lease до любых побочных эффектов: точный retry воспроизводит сохранённый результат, активный конкурент получает временный `409`, а повтор того же `source_ref` с иным payload — постоянный conflict без потери новых данных.
- Явное «не запоминай» имеет абсолютный приоритет даже над `force_knowledge`: сообщение и вложение остаются transient и не создают Raw Object, Inbox, Knowledge Object, сущности или файл на диске.
- Найденные знания, graph evidence, имена вложений и tool/web output передаются модели только как недоверенные данные пользовательского уровня; динамический контент не повышается до system-инструкций.
- Admin UI также управляет пользователями, правами, знаниями, графом, разговорами, файлами, аудитом, экспортом, backups и diagnostics.
- Документы и Telegram-вложения ограничиваются ещё во время чтения: CSV/TAR/PDF/Office и сжатые форматы разбираются с byte/entry/page/row/output budget, без предварительного безграничного буферизования; web fetch защищён от SSRF, redirects и DNS rebinding закреплением уже проверенного IP.
- HTTP body limit действует на фактически полученные байты, включая chunked transfer, до аутентификации и JSON/multipart parsing; proxy headers принимаются только от явно доверенного непосредственного proxy-hop.
- Online backup SQLite включает `integrity_check`, SHA-256 manifest и повторную верификацию; вместе с БД он сохраняет append-only историю отношений и её completeness floor. Tenant export включает только принадлежащие этому пользователю `relation_revisions`. `restore-backup` требует остановленного backend через эксклюзивный lease, повторно сверяет staged copy, заменяет БД атомарно и возвращает точные DB/WAL/SHM при сбое; для уже повреждённой активной БД сохраняется отдельный явно непроверенный recovery bundle. Опциональная Markdown-проекция пишется только в явном `full_owner`; безопасное умолчание `disabled` не создаёт plaintext-копий.
- Workers обслуживают всех активных tenants: lifecycle, entity-resolution candidates, ежедневный backup, SQLite optimize, read-only quality report и bounded advisory Inbox refinement. Vault-projector добавляется только в `FRIDAY_MEMORY_VAULT_MODE=full_owner`. Каждая задача публикует состояние, длительность, следующий запуск, timeout и consecutive failures для `status`, `doctor` и Admin UI.
- Канонический multimodal profile `qwen38-27b-nvfp4-sglang` закрепляет точные model/runtime identities, graph-only 40K/6 launch contract и fail-closed V12 live attestation; прежний Qwen3.6/vLLM profile сохранён для совместимости.

### Engineer Mode и Host Capability Plane (opt-in)

Engineer Mode — owner-only defensive workbench с code-pinned единственной
сетевой целью, bounded probes только по явному запросу текущего человека и
no-network bubblewrap-разбором артефактов. Простое упоминание host/URL не
запускает DNS или probes; адрес должен пройти общий exact CIDR policy, а public
scope без operator flag и отдельного action approval закрыт. Режим
выключен по умолчанию и требует Linux acceptance из
[`docs/ENGINEER_MODE.md`](docs/ENGINEER_MODE.md).

Отдельный Host Capability Plane позволяет использовать reviewed Ubuntu CLI как
функцию, а не просто запускать приложение. Первый вертикальный срез обнаруживает
или через точный human-approved APT plan устанавливает `nmap`, аттестует
`/usr/bin/nmap`, автоматически продолжает исходную bounded local-network задачу,
парсит XML и сохраняет evidence/coverage. Backend, непривилегированный user
agent и узкий root package broker разделены; общего shell/sudo/Docker socket
нет. Отдельный reviewed `jq` action извлекает только явно названные поля из
принадлежащей владельцу Raw JSON-копии; произвольный jq-код и host path модели
не принимаются, исходный файл не изменяется. Все `FRIDAY_HOST_*` flags
default-off. Установка на Ubuntu, preflight,
Compose override и rollback: [`deploy/host-control/README.md`](deploy/host-control/README.md).

### Obsidian на Android (beta)

Опциональный first-party Organ ведёт один owner-scoped vault через
изолированный Syncthing и Syncthing-Fork. Есть private-Telegram onboarding
`/obsidian`, точный copy/paste Device ID, `/obsidian_alias <имя vault>`,
list/lexical search/read/create/append, typed properties, daily notes, журнал
операций и раздельные факты local write / scan / Android receipt / open.
Аккаунт Obsidian, подписка, desktop, QR и companion plugin не нужны.

Opt-in env-контракт `FRIDAY_OBSIDIAN_ENABLED=1` +
`FRIDAY_PUBLIC_BASE_URL=https://...` и единый immutable cutover для SQLite,
Telegram inbox и exact vault root прошли автоматическую release-проверку.
Ручная acceptance-матрица на физическом Android ещё не завершена, поэтому это не production-
сертифицированный релиз. Точные шаги, конфигурация, статусы и
ограничения: [docs/OBSIDIAN_ANDROID.md](docs/OBSIDIAN_ANDROID.md).

## Быстрый запуск на Windows

Рекомендуется Python 3.12, Docker Desktop с NVIDIA Container Toolkit и PowerShell 7.

### 1. Подготовка проекта

Распакуйте архив в `D:\jericho`, затем:

```powershell
cd D:\jericho
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
jericho init --home D:\jericho
```

Команда `jericho init` создаёт каталоги runtime и `.env.local` с двумя независимыми случайными секретами. Не публикуйте этот файл.

### 2. Модель

Канонический внешне управляемый SGLang profile ожидает exact snapshot:

```text
D:\jericho\models\qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5\
```

Включённый ниже reference Compose recipe для совместимости по-прежнему
ожидает Qwen3.6/vLLM snapshot:

```text
D:\jericho\models\qwen3.6-27b-nvfp4-nvidia\
```

Веса в архив намеренно не включены. Каждый каталог должен содержать полный
snapshot и конфигурацию для соответствующего runtime.

### 3. Запуск без LLM для проверки системы

В `.env.local` временно задайте:

```dotenv
FRIDAY_LLM_ENABLED=0
FRIDAY_WORKERS_ENABLED=0
```

Затем:

```powershell
jericho doctor
jericho server
```

Admin UI: `http://127.0.0.1:8000/admin/`. Нажмите **API-ключ** и вставьте значение `FRIDAY_API_TOKEN` из `.env.local`.

Без LLM ingestion, граф, поиск, права, Admin UI и резервирование работают; ответы агента переходят в честный локальный fallback.

### Native TLS для Admin UI и Telegram bridge

Если API доступен не только через loopback, задайте абсолютные пути к паре
сертификата и ключа. Сертификат обязан содержать SAN для каждого реального адреса
браузера, а также `localhost`/`127.0.0.1` (или `::1` для IPv6 wildcard): системный
bridge и diagnostics обращаются к wildcard-bind через проверяемый loopback URL.

```dotenv
FRIDAY_API_HOST=0.0.0.0
FRIDAY_SSL_CERTFILE=/absolute/path/server.crt
FRIDAY_SSL_KEYFILE=/absolute/path/server.key
FRIDAY_BACKEND_CA_FILE=/absolute/path/ca-or-self-signed-server.crt
FRIDAY_CORS_ORIGINS=https://127.0.0.1:8000,https://localhost:8000,https://LAN-NAME-OR-IP:8000
```

`FRIDAY_BACKEND_CA_FILE` — только публичный CA/certificate, не private key.
Оставленный пустым `FRIDAY_BACKEND_URL` автоматически становится
`https://127.0.0.1:8000` при native TLS. Не используйте `verify=False`, `curl -k`
или браузерное исключение как рабочую схему: импортируйте публичный CA в trust
store клиента. Полная systemd-процедура и rollback находятся в
[docs/OPERATIONS.md](docs/OPERATIONS.md).

Эти native TLS variables относятся к direct/systemd запуску. Base Compose
намеренно игнорирует их, держит backend↔bridge HTTP только в private Docker
network и требует отдельный TLS reverse proxy для внешнего доступа; private key
не передаётся Telegram-контейнеру.

## Полный запуск через Docker Compose

1. Скопируйте пример конфигурации:

```powershell
Copy-Item .env.example .env
```

2. В `.env` обязательно задайте:

```dotenv
FRIDAY_API_TOKEN=<случайная строка минимум 32 символа>
FRIDAY_TELEGRAM_BRIDGE_SECRET=<другая случайная строка минимум 32 символа>
FRIDAY_TELEGRAM_BOT_TOKEN=<токен BotFather>
FRIDAY_HOST_HOME=D:/jericho
FRIDAY_MODEL_ROOT=D:/friday/models
# Внутри Compose backend обращается к vLLM по имени сервиса:
FRIDAY_DOCKER_LLM_BASE_URL=http://dispatcher:8001/v1
```

Случайные значения можно получить так:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48)); print(secrets.token_urlsafe(48))"
```

3. Запустите backend, vLLM и Telegram bridge:

```powershell
docker compose --profile llm --profile telegram up -d --build
```

Только backend, без модели и Telegram:

```powershell
# Сначала задайте FRIDAY_LLM_ENABLED=0 в .env
docker compose up -d --build backend
```

Состояние:

```powershell
docker compose ps
docker compose logs -f backend
docker compose logs -f dispatcher
docker compose logs -f telegram
```

Compose запускает закреплённый по digest vLLM nightly с поддержкой ModelOpt
Qwen3.5/3.6 VLM. Сборка локального image не нужна. Dispatcher запускается без
`--language-model-only`; MM profiling намеренно не пропускается, чтобы дефицит
VRAM обнаружился при старте, а не на первом пользовательском изображении.

## Обновление существующей установки

Эти шаги выполняет только `immutable_release_operator.py` по контракту из
[Operations](docs/OPERATIONS.md). Ручная остановка writers, миграция,
замена unit-файлов или переключение anchor не являются release path.

1. Соберите wheel дважды из одного commit и убедитесь, что SHA-256 совпадает.
2. Создайте новый sibling release только из wheel; установленный venv не правьте пофайлово.
3. Дайте оператору остановить backend и Telegram bridge и сохранить проверенный согласованный снимок SQLite, WAL, Telegram inbox и exact Obsidian root.
4. Выполните offline migration, переключите общий release anchor атомарно, примите backend и только затем запускайте bridge. При ошибке используйте exact rollback, а не повреждённый прежний каталог.
5. Схема SQLite — **43**. Schema 31 один раз фиксирует `relation_history_complete_from`; schema 32 добавляет monotonic observed boundary и REPLACE/context guards; schema 33 — неизменяемые transport-id повторной загрузки; schema 34 — проверяемое имя, данное пользователем в конкретном сообщении; schema 35 — изолированные профили, vault, onboarding, журнал операций и конфликты Obsidian; schema 36 — стабильные note bindings, revision-aware index/link graph и expiring candidate/Active Frame state; schema 37 — ограниченное person-owned хранилище структурных ошибок до фиксации assistant-сообщения; schema 38 — короткоживущие owner-scoped `RecallConversation` Work Item и закрытый Active Frame, привязанные к точному принятому результату окна переписки; schema 39 — закрытые labels `RecallSelectedArchiveEvidence` и один body-free sidecar выбранного источника; schema 40 — immutable body-free набор archive-кандидатов и durable ordinal question; schema 41 — rebuildable body-free `document_catalog` с exact Raw revision/extraction binding и закрытыми explicit-incomplete состояниями; schema 42 — body-free Work Item/Active Frame и receipts активного restart-safe сравнения выбранных сообщений с точным документом через durable Q1/Q2; schema 43 — immutable host action plans, person-scoped idempotency, restart-safe unknown/reconcile state и append-only host events. Миграция schema 35→36 сначала побайтно проверяет выпущенную Obsidian-схему и только затем атомарно расширяет operation contract; schema 37–43 отдельно проверяют свои точные DDL-проекции. Остальные авторитетные знания, Inbox и разговоры не переписываются; более новая неизвестная схема отклоняется без изменений.

Перед обновлением можно дополнительно выполнить
`jericho backup --label before-upgrade` и `jericho verify-backup`. Это SQLite-only
копия, а не exact DB/WAL/Telegram-inbox recovery set атомарного cutover;
она не заменяет backup и release journal оператора.

## Канонический SGLang-профиль

Профиль `qwen38-27b-nvfp4-sglang` закрепляет следующие значения:

| Параметр | Значение |
|---|---:|
| model | `/models/qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5` |
| served model name | `dispatcher` |
| repository / revision | `Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4` / `43aa7ff5eef05ab50a3bfa6aca581085312c7a04` |
| quantization | `W4A4_NVFP4_FP8_KV` |
| runtime image | `lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124` |
| runtime source / reported version | `c4271c3fe1262fc2adbd162c33b25de5255251c5` / `0.0.0.dev0+qwen38.27b.g561c8f3` |
| max model length | `40960` |
| static memory / total tokens | `0.90` / `40960` |
| KV cache dtype | `fp8_e4m3` |
| max running requests / Mamba cache | `6` / `6` |
| chunked prefill | `2048` |
| SSM dtype | `bfloat16` |
| Radix / speculation | выключены |
| CUDA graph decode | `full`, batches `1,2,3,4,5,6` |
| CUDA graph prefill | выключён |
| attention backend | `flashinfer` |
| multimodal transport / limits | `cpu` / `image=4`, `video=0`, `audio=0` |
| reasoning parser | `qwen3` |
| tool-call parser | `qwen3_coder` |

Шесть scheduler sequences описывают пропускную способность общего endpoint, а не
fan-out одной задачи. Иерархическое чтение документа имеет отдельный явный
потолок `document_map_max_concurrency=1`: синхронизация профиля не превращает
одну загрузку в три параллельные длинные генерации.

Этот profile описывает внешне управляемый endpoint, а не свободный набор
переменных. До регистрации V12 routes startup probe сверяет exact
`/v1/models`, bounded `/metrics`, `/server_info` и per-process deployment
witness с code-owned identities и launch graph. Любой drift, неполный
witness или незамкнутый same-origin proxy оставляют routes в `legacy`.
Успешный canary startup должен показать в `/api/health` версию `0.207.46`,
точный profile id, `canary_ready`, `live_attestation_clear` и оба
зарегистрированных route; простого HTTP `status=ok` недостаточно.

## Telegram

Backend и bridge используют общий секрет только для подписи межсервисных запросов. Токен бота backend не получает.

Bridge допускает только один активный процесс для одной durable queue. Временные сбои повторяются с ограниченным backoff; исчерпавшие бюджет или заведомо некорректные update остаются в `dead_letter` для диагностики, а не исчезают молча.

Команды:

- `/start` — знакомство;
- `/help` — справка;
- `/status` — статистика личной базы и review-очередей;
- `/new` — новый разговор без очистки знаний;
- `/chat` — обычный режим диалога;
- `/work` — режим многошаговой работы с личными знаниями;
- `/research` — режим исследования с расширенным, но ограниченным бюджетом tools;
- `/inbox` — показать ближайшие предложения и принять/игнорировать их inline;
- `/conflicts` — разобрать ближайшие конфликты знаний inline;
- `/relations` — принять или отклонить ближайшие предложенные связи inline;
- `/note текст` — явно сохранить заметку;
- `/search запрос` — прямой поиск по подтверждённым знаниям списком, без ответа модели.
- `/obsidian` — в личном чате начать, возобновить или проверить Android Obsidian;
- `/obsidian_alias точное имя` — задать имя Android-vault для open-ссылки.

Ответы получают inline-оценки 👍/👎. В `knowledge_work` и `research` итог можно отправить кнопкой в Inbox; это предложение на review, а не скрытая запись в граф. `/status` показывает сохранённый режим текущего Telegram-канала.

Принимаются вложения: текст, изображения и документы (с извлечением текста/OCR), а также голосовые, аудио, видео, видео-кружки и анимации. Telegram-альбом из нескольких изображений или документов собирается durable-очередью в один ход: общая подпись относится ко всему упорядоченному набору, backend вызывает агента один раз, а человек получает один общий ответ. Локальная модель зрения распознаёт изображения; голосовые и аудио при включённом `FRIDAY_WHISPER_ENABLED` расшифровываются локально (опциональный пакет `jericho[voice]`, faster-whisper, полностью офлайн) и попадают в Inbox уже текстом — иначе, как и видео, сохраняются как есть с провенансом и метаданными и ждут вашего решения в Inbox, без расшифровки. Геолокация и контакт превращаются в заметку. Неподдерживаемые типы (стикеры, опросы) получают понятный ответ, а не молча теряются. Происхождение пересланных сообщений (кто и когда переслал) сохраняется в провенанс.

Доступ к боту работает по принципу deny-by-default: бот отвечает только чатам из эффективного allowlist (объединение `FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS` и `FRIDAY_TELEGRAM_OWNER_CHAT_IDS`). Пустой список означает, что не допущен никто. Прошедший allowlist пользователь регистрируется автоматически с preset-ом `user` и получает отдельный tenant ID вида `telegram:<realm>:<telegram_id>`.

Чтобы разрешить конкретные чаты (или задать чат владельца для первичной настройки):

```dotenv
FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS=123456789,-1001234567890
FRIDAY_TELEGRAM_OWNER_CHAT_IDS=123456789
```

Если задан bridge secret, но эффективный allowlist пуст, backend в production не стартует (в loopback-разработке выводится предупреждение). Запросы моста подписываются HMAC с одноразовым nonce, что закрывает окно повторного воспроизведения.

## CLI

```text
jericho init [--home PATH] [--force]
jericho server
jericho telegram-bridge
jericho status [--json] [--check-llm]
jericho doctor [--check-llm]
jericho backup [--label NAME]
jericho verify-backup [FILENAME]
jericho restore-backup [FILENAME] --yes
jericho export-user USER_ID
jericho import PATH [--dry-run] [--user U] [--uploaded-by U] [--suffix .md] [--limit N]
jericho events [--type TYPE] [--limit N] [--json]
jericho model-check [--json] [--timeout SEC]
jericho eval-bootstrap [--limit N] [--save]
jericho up
jericho tui
jericho install-services
jericho search-source ФРАЗА [--limit N] [--json]
jericho reindex-embeddings [--user U] --yes
jericho backfill-document-dates [--user U] [--batch N] [--limit N]
jericho backfill-entities --method M [--user U] [--batch N] [--limit N] [--apply]
jericho prune-entities [--user U] [--batch N] [--limit N] [--apply]
jericho backfill-relations [--user U] [--batch N] [--limit N] [--apply]
jericho extract-structure-relations [--user U] [--batch N] [--limit N] [--apply]
jericho review-relation-candidates --user U [--limit N] [--votes N] [--apply] [--report FILE]
jericho backfill-relation-dates [--user U] [--apply]
jericho dismiss-series-conflicts --user U [--apply]
jericho data-source {list,add,forget,describe,query} --user U [--name N] [--kind sqlite|postgres|mysql]
                    [--dsn-env VAR] [--description TEXT] [--query SQL]
jericho retag-documents [--user U] [--batch N] [--limit N] [--arbiter] [--apply] [--report FILE]
jericho resolve-exact-duplicates [--apply]
jericho backup-keygen
jericho decrypt-backup FILE
jericho purge [--id ID] [--older-than-days N] --yes
jericho mint-token --user U --preset P [--ttl 90d]
jericho revoke-token TOKEN_ID
```

- **`search-source`** — дословный поиск по ИСХОДНОМУ тексту загруженного материала, мимо ранжирования. Нужен, когда помнишь точную фразу из бумаги: 93% загруженных знаков живут только в первоисточнике, а Knowledge Object несёт сокращённую версию. Отклонённое во входящих сюда не входит — это решение, а не фильтр.
- **`up`** — запуск бэкенда и моста под супервизором; **`tui`** — интерактивный лаунчер, самая уместная точка входа, если не хочется помнить команды; **`install-services`** — systemd-юниты, чтобы всё поднималось само.
- **`reindex-embeddings`** — пометить вектора устаревшими, чтобы фоновый индексатор пересчитал их. Понадобится после смены модели эмбеддингов или правки разбиения на пассажи. Поиск при этом продолжает работать на прежних векторах: они не удаляются, а заменяются по мере пересчёта.
- **`backfill-document-dates`** — достаёт СОБСТВЕННУЮ дату документа (docProps/core.xml у Office, /CreationDate у PDF) у уже загруженных файлов. Нужна потому, что дата загрузки у импортированного корпуса одна на всё: 1531 объект из 1537 «создан» в день импорта, и хронологии архива не было. Замерено на настоящих файлах: 84% дают дату, разброс 2006-2026. Версий не создаёт — это дозапись утраченного провенанса, а не правка знания; повторный запуск продолжает прерванный проход.
- **`backfill-entities`** — проводит одно ОБЪЯВЛЯЮЩЕЕ правило извлечения по архиву, загруженному ДО появления этого правила. Нужна потому, что извлечение работает только при приёме: правило, добавленное позже, на лежащие в базе документы не действует вовсе. Метод называется явно и только один за прогон; недекларирующий метод (догадка по форме слова) отвергается — проход не лазейка мимо порога автосоздания. По умолчанию только считает, запись включает `--apply`; связи, по которым человек уже высказался, обходятся.
  Замеры перед применением: `explicit_person_patronymic` — 20 644 упоминания, 4349 узлов, 71% имён более чем в одном документе, перестановочных двойников 4 из 4349; `explicit_military_unit` — 240 документов, 151 часть, 65 из них более чем в одном документе. До правила войсковые части не извлекались НИКАК.
- **`backfill-relations`** — то же самое для СВЯЗЕЙ. В графе владельца было 4610 сущностей и ноль связей — не потому, что механизм сломан, а потому, что его никто не звал: извлечение работает при приёме, а корпус загружен раньше. Результат прохода — очередь кандидатов на подтверждение человеком, а не готовые связи; по умолчанию только считает, запись включает `--apply`.
  Замер на копии базы, проход по всем 1533 документам: без родственных слов — 0 кандидатов; со словами и без проверок — 509, среди них «Изобильный → Москва | Брат», два города в родстве; с требованием, чтобы обе стороны были людьми, — 243, и половина сцепляла людей из РАЗНЫХ анкет заголовком поля бланка; с требованием, чтобы слово стояло вплотную к имени, — 98, из них 4 от слова «внук», и все четыре оказались позывными из одного списка личного состава. Итог — 94 срабатывания на 56 документах, из них 64 различных пары в очереди (одна и та же пара, названная в трёх документах, — три срабатывания и одна связь), около 85% верных. Служебные отношения в этом корпусе словами почти не объявляются: «назначить на должность» встречается в 8 документах из 1533, «состоит в должности» — ни в одном, а родственные слова — в 186.
- **`extract-structure-relations`** — связи, объявленные не фразой, а ФОРМОЙ документа: полем анкеты, строкой ведомости, адресатом рапорта, подписью. Фразовый извлекатель связывает объявляющее слово с СОСЕДНИМ именем, а форма служебного документа объявляет отношения СУБЪЕКТА — того, чья это анкета и чей это пункт списка. Замерено на рапорте из архива владельца: из восьми фразовых пар верны три, и те случайно — субъект оказался ближайшим слева («Брат: Макаров» в пункте про Кублика превращался в «Макаров брат Варламовой»). Форму читает арбитр: шаблоном её не покрыть, анкет в архиве 167, рапортов 5, а прочего 1360, и внутри него больше десятка видов — ведомости, списки, книги, планы, выписки из приказов, листы Excel.
  За арбитром проверяется каждое слово: обе стороны берутся по номеру из переданного ему списка, тип — из закрытого перечня (`related_to` не входит: «как-то связаны» не несёт сведений сверх того, что оба названы в одном документе), а выдержка сверяется с текстом буквально И обязана называть ту сторону, о которой связь. Последнее — не формальность: без него проходит заголовок графы бланка («22. Родители (ФИО, дата рождения, где проживает…)»), который стоит между любыми двумя именами анкеты и потому сверку проходит безупречно, ничего не подтверждая. На живом архиве такими заголовками были обоснованы 11 связей из первых 64, там же нашлись выдержки про третьего человека («Комогоров Дмитрий → Комогорова Екатерина» при выдержке «Отец Комогоров Виктор Леонидович») и прямо опровергающие связь («Воинская часть: | Не указано»).
  Результат — очередь кандидатов на подтверждение человеком. По умолчанию показ, и показ настоящий: арбитр вызывается без записи. Требуется включённая модель.
- **`review-relation-candidates`** — обратный проход к предыдущему: тот предлагает связи, этот спрашивает у документа, объявляет ли он предложенное. Нужен потому, что очередь растёт быстрее, чем человек её разбирает — на архиве владельца она дошла до 597 строк. Проверок две, и они разной природы. Структурная: тип связи требует своих концов — «человек состоит в человеке» (42 кандидата живой очереди), «человек — часть войсковой части» (это `member_of`), «человек занят Графиком» (а «График» — слово из шапки бланка) отвергаются без обращения к модели, потому что ответ документа тут ничего не решает. Содержательная: арбитр читает ОКНО документа и отвечает, объявляет ли эта форма именно эту связь — строка ведомости объявляет должность того, кто в ней назван, а не его подчинённость соседу по списку.
  Почему не правилом: правило «выдержка обязана называть начало связи ИЛИ начало обязано быть субъектом документа» отсекает в очереди 344 строки, но на контроле из уже принятых связей убивает 14 из 64, включая настоящую родню из анкеты («Мама Джумаева Уланбике Эсманбетовна»). Форма документа не читается признаком одной строки.
  Воздержание арбитра статус НЕ меняет: решение по кандидату терминально, и «не уверен» оставляет строку человеку. Одного голоса мало, и это замерено: два полных прогона по одной очереди из 522 кандидатов при temperature=0 разошлись на 97 вердиктах — согласие 81.4%. Поэтому `--votes` (по умолчанию 2): решение принимается только при единогласии, разошедшийся сам с собой арбитр не решает ничего. По умолчанию показ; `--report FILE` кладёт каждый вердикт с обоснованием в JSON-строках, чтобы решения можно было прочитать глазами, а не принимать на веру.
- **`data-source`** — внешняя СУБД как ИСТОЧНИК, в который Пятница ходит читать. Не переезд: свой архив остаётся на месте, а чужая база становится ещё одним источником, как интернет или присланный файл. Поддержаны `sqlite` (без зависимостей), `postgres` и `mysql` (необязательный extra `friday[sql]`).
  Три свойства, без которых это опасная игрушка. **Строка подключения не хранится в базе** — объявляется ИМЯ переменной окружения, потому что резервные копии архива переживают всё, а экспорт аккаунта отдаётся человеку целиком. **Только чтение, и проверяется текст запроса, а не намерение**: ровно один `SELECT` или `WITH`, без второго оператора через точку с запятой, без DDL и DML; комментарии срезаются ДО проверки, иначе `SELECT 1 -- ; DROP TABLE` выглядит одним оператором, а на сервере им не является. **Потолки названы вслух**: не больше 200 строк и 15 секунд, а если обрезано — это стоит в ответе, потому что «первые двести строк» и «всего двести» разные факты.
  Модели даны три инструмента: `data_sources` (что подключено), `data_schema` (таблицы и столбцы — без него имя таблицы приходится угадывать, а угаданное даёт не пустой ответ, а ошибку) и `data_query` (одно чтение; ответ содержит и строки, и сам запрос, чтобы человек видел, ЧТО спросили в чужой системе). Право отдельное — `data.read`, только владельцу и админу.
  То же самое доступно из панели — вкладка **«Источники»**: объявить, посмотреть схему, забыть. Панель показывает и то, чего не видно из списка объявлений: задана ли переменная окружения. Объявить источник заранее законно, но молчащий об этом экран показывал бы рабочий источник, а первый же запрос падал бы непонятной ошибкой.
  Ключ источника — ПАРА «владелец + имя» (схема 29). Одним именем он быть не может: читается источник всегда парой, а писался по имени — и второй человек, объявив свой «hr», делал UPDATE чужой строки, оставляя в ней прежнего владельца. Чужой источник после этого читал бы базу соседа.
- **`dismiss-series-conflicts`** — снимает с очереди «почти-дубликаты», которые сегодняшнее правило и не завело бы. Детектор ловил бланк, а не копию: на архиве владельца висели 144 пары со сходством 0.99–1.00, и среди них «13 день.docx» ⟷ «12 день.docx» (разные дни курса), «ЖП.pdf» ⟷ «ЖП1.pdf» (журнал за два разных дня), «строевка 05.03» ⟷ «строевка 07.03» — слить их значило бы стереть данные. Порогом это не лечится, и так было записано в шапке модуля с самого начала: серия текстуально почти идентична по построению. Различающий признак не в тексте: у соседей по серии различается собственная дата документа или числа в имени. Замер: 69 пар из 144; на 55 парах, уже закрытых человеком, правило не возражает ни разу. Порядок важен — проверка идёт ПОСЛЕ точного совпадения текста, иначе она забраковала бы 18 верных решений.
- **`backfill-relation-dates`** — проставляет уже принятым связям дату документа, который их объявил. Правило «связь несёт дату своей бумаги» действует только вперёд, а связи, принятые раньше, остаются без начала — и вопрос «как было тогда» на них отвечается сегодняшней картиной. Замер на живом графе: 192 связи, у ВСЕХ 192 начало пустое; проход нашёл дату для 191, у одной документ своей даты не имеет. Пустая дата остаётся пустой: «неизвестно» — это не «с начала времён», и обход по дате такие связи не отбрасывает.
- **`retag-documents`** — проставляет каждому документу ВИД, которым он объявляет себя сам, и снимает теги прежней редакции правил. Зачем: `knowledge_kind` у 1532 объектов из 1536 равен `document` — это вид НОСИТЕЛЯ, а не документа; а теги собираются частотным счётом слов, и на живом архиве 786 объектов из 1536 несут набор тегов, побайтно совпадающий с набором другого объекта (47 карточек РАЗНЫХ людей размечены одинаково: `где|дата|нет|номер|проживает|рождения|телефона|фио`). Отбирать по такому тегу нечего.
  Сам архив свой вид объявляет — заголовком, шапкой издателя, обязательной формулой: на 1536 живых объектах вид находится у 87.9%, девятнадцатью значениями (рапорт 288, список личного состава 192, ведомость 171, досье на человека 167, инструктивная записка 103, нормативный документ 85, приказ 65…). Три ловушки, каждая молча обнуляет правило: заголовки набраны вразрядку («В Е Д О М О С Т Ь»), в архиве живут устойчивые опечатки («ИНСРУКТИВНАЯ» на 15 объектах), а `upper()`/`LIKE` в SQLite не работают с кириллицей — `where upper(content) like '%РАПОРТ%'` находит 2 документа там, где их 286.
  Как документ себя НАЗВАЛ, сильнее того, КАК ОН УСТРОЕН: шапка таблицы («№ п/п | в/зв») смотрится только тогда, когда объявления нет нигде — ни в тексте, ни в имени файла. Без этого порядка «Анкета Селиверстов.docx» размечалась списком личного состава, потому что слово «Позывной» стояло в тексте раньше, чем «АНКЕТА». `--arbiter` спрашивает модель о тех, кто вид не объявил; арбитру разрешено выбрать только из известного списка или предложить новый вид ОТДЕЛЬНО — вид, придуманный на ходу, не отберёт ни одного соседнего документа. Тег ставится с приставкой (`вид:рапорт`), поэтому виден сразу везде: чипы админки, `/tags`, `list_tags` у модели, полнотекстовый индекс.
- **`resolve-exact-duplicates`** — снимает с очереди разбора те конфликты «почти-дубликат», где извлечённый текст совпал знак в знак: там решать нечего, это один документ в нескольких экземплярах. Победителем остаётся более ранняя запись, проигравшая помечается устаревшей, а не удаляется. По умолчанию только показывает состав очереди.
  Замер на живом архиве: из 200 конфликтов 56 — точные копии, 54 — версии одного имени, 90 — прочие (медиана похожести 0.99). Ни одна из 56 не ловилась дедупликацией по хешу файла: тот же документ, пересохранённый из Word, даёт другие байты. Все 200 пришли одним импортом папки. Порог похожести здесь НЕ применяется — `friday/dedup.py` замерил, что «дубликат» и «следующая заметка в серии» им не разделяются, поэтому всё, кроме точного совпадения, остаётся человеку.
- **`import`** — обходит каталог и прогоняет каждый файл через приём. Всё уходит в Inbox: указание на папку не является решением о каждом файле внутри. Возобновляем — `source_ref` выводится из хеша содержимого, поэтому повторный запуск не грузит ничего заново, а прерванный продолжается той же командой.
- **`events`** — операционный журнал: что сломалось и починилось, пока никто не смотрел. Пишутся переходы, а не тики, поэтому воркер, сломанный всю ночь, даёт две записи, а не сотни.
- **`model-check`** — проверяет эндпоинт **генерацией**, а не соединением: отдаётся ли модель, отвечает ли она в реальном бюджете токенов, не протекает ли цепочка рассуждений в ответ, парсится ли JSON, принимают ли эмбеддинги пакет того размера, каким ходит индексатор.
- **`eval-bootstrap`** — черновики золотого набора для оценки поиска, с аудитом: вопрос, пересказывающий документ его же словами, отклоняется. Без `--save` ничего не сохраняется.

Примеры:

```powershell
jericho status --json
jericho doctor --check-llm
jericho backup --label before-upgrade
jericho verify-backup
# backend должен быть остановлен; команда сама проверит эксклюзивность
jericho restore-backup jericho-YYYYMMDDTHHMMSSZ-before-upgrade.sqlite3 --yes
jericho export-user telegram:telegram:123456789
jericho import ~/Документы --dry-run     # состав, ничего не записывая
jericho events --type worker.failed
jericho model-check
```

## Где лежат данные

При `FRIDAY_HOME=D:\jericho`:

```text
data/state/friday.sqlite3       основная БД новой установки
data/state/telegram-inbox.sqlite3 очередь Telegram bridge
data/files/                      исходные загруженные файлы
data/memory-vault/               optional full_owner/legacy Markdown-проекция
data/obsidian/                   private Syncthing-профили, индексы и Markdown-vault
data/backups/                    SQLite-копии и SHA-256-манифесты
data/exports/                    JSON-экспорты пользователей
cache/                           временные кэши
logs/                            локальные логи
models/                          веса моделей
```

Runtime-каталоги и секреты исключены из Git и из дистрибутивного архива.
Существующая установка может продолжать использовать `jericho.sqlite3` через
явный `FRIDAY_DATABASE_PATH`; две разные непустые базы рядом не выбираются
автоматически. `FRIDAY_DATABASE_MUST_EXIST=1` запрещает боевому процессу создать
пустую замену, если назначенная база пропала.

## Резервирование

`jericho backup` создаёт транзакционно согласованную копию **только SQLite-БД**. Полная резервная копия установки должна дополнительно включать:

- `data/files/`;
- весь `FRIDAY_OBSIDIAN_ROOT` (`data/obsidian/` по умолчанию), если
  тестируется Obsidian beta; coherent DB-plus-root procedure пока ручная и
  не release-сертифицирована;
- `data/memory-vault/` — только если `full_owner` явно включён или legacy-артефакты осознанно сохраняются; это plaintext, шифруйте копию;
- `.env.local` или `.env` — хранить отдельно и зашифрованно;
- модельные веса — можно не копировать, если есть проверяемый источник повторного получения.

Подробная процедура проверки, атомарного восстановления и полного файлового snapshot: [docs/BACKUP_AND_RESTORE.md](docs/BACKUP_AND_RESTORE.md).

## Безопасность

- Admin UI и все API, кроме health-check, требуют bearer token либо валидную подпись Telegram bridge.
- Для сетевого bind токен обязателен; wildcard CORS запрещён валидатором конфигурации.
- Capability-проверка выполняется перед каждым HTTP-действием и вызовом инструмента агента.
- Делегированный администратор не может повысить себя до владельца или создать обходной preset с недоступными ему правами.
- Секреты редактируются из обычных логов и credential-bearing Telegram URL.
- `code_run` по умолчанию выключен и не считается контейнерной/VM-песочницей. Включать его на хосте с чувствительными данными не рекомендуется.
- Веб-загрузчик по умолчанию не имеет доступа к частным сетям.
- Admin API имеет полный доступ к данным и должен оставаться на loopback или за отдельным TLS reverse proxy с дополнительной аутентификацией.

Полная модель угроз и правила публикации: [docs/SECURITY.md](docs/SECURITY.md).

## Проверки

```bash
.venv/bin/python -m pip install --upgrade --constraint requirements-dev.lock pip setuptools wheel
.venv/bin/python -m pip install --no-build-isolation --constraint requirements.lock --constraint requirements-dev.lock -e ".[dev,vectors]"
.venv/bin/python -m playwright install chromium
.venv/bin/python tools/quality_gate.py
jericho doctor
```

`tools/quality_gate.py` — единая проверка репозитория: обязательный preflight
Python 3.14.4, Node 22.23.2, NumPy 2.5.1, Playwright 1.61.0, установленного
Chromium revision 1228 и официального UnRAR 7.20, затем static checks,
не-браузерный pytest и отдельная UI-фаза. Любой skipped-тест в обеих pytest-фазах
считается ошибкой; точные nodeid в JUnit должны совпасть с полной коллекцией.
UI по умолчанию использует 12 workers — по одному на каждый UI-модуль; `-n 0`
включается через `--ui-workers 1`. Для локальной итерации есть `--phase static`,
`--phase tests` и `--phase ui`; toolchain preflight обязателен и для частичной
команды, а перед пушем выполняется полная команда без `--phase`.

Рекурсивная синтетическая live-приёмка имеет отдельный воспроизводимый контракт:
tracked pre-release runner одним снимком проверяет P06 A+B **40/40** и focused
P01/P02/P04/P08/P09/P10 **120/120**, после выпуска официальный `--both` запускает
две десятипроходные батареи и не начинает B до полностью чистой A. Команды,
приватные артефакты и точные критерии описаны в
[docs/LIVE_BATTERY_RUNBOOK.md](docs/LIVE_BATTERY_RUNBOOK.md).

Тесты покрывают provenance, tenant isolation, versions, soft delete, review-only lifecycle, backup verification/restore/rollback, entity resolution, терминальные relation decisions и монотонные conflict decisions, три исхода ingestion, feedback replacement и точную attribution, usage-aware retrieval, agent modes, knowledge-work/research-to-Inbox, grounded bounded vision/OCR, bulk Admin workflows, worker timeout/partial failure health, backend singleton lease, capability default-deny и безопасное делегирование, инструментальное ядро и завершение дерева процессов, архивные лимиты, SSRF, подписанный Telegram/API vertical slice, inline callbacks, миграцию/повторы/dead-letter очереди Telegram, redaction логов, tool-call protocol и exact vLLM/SGLang runtime profiles.

## Ограничения текущей версии

- Веса модели и собранные Docker layers не входят в архив; воспроизводимый рецепт специализированного vLLM image включён.
- Backend-образ собирается на том же Python 3.14, на котором прогоняется набор тестов, а зависимости фиксируются `requirements.lock`; сборка образа в этом окружении не проверялась (Docker не установлен) — проверено лишь то, что все бинарные зависимости имеют готовые колёса cp314.
- Полнотекстовый поиск работает локально; dense embeddings включаются только при настройке отдельного OpenAI-compatible embeddings endpoint.
- Deterministic entity extraction намеренно консервативно: оно лучше пропустит слабую сущность, чем испортит граф. Local-model advisor расширяет только предложения Inbox; сомнительные links и merge подтверждаются человеком.
- Неявные co-occurrence edges используются только как ранжирующий контекст и явно помечаются как implicit; они не сохраняются в граф как доказанные отношения.
- Vision/OCR для изображений и сканов требует включённого локального мультимодального vLLM и остаётся advisory-only: качество зависит от модели и сложные документы всё равно требуют review.
- RAR/7z поддержка зависит от установленных Python-библиотек и формата архива; опасное содержимое не исполняется и не распаковывается в произвольные пути.
- `code_run` — ограниченный subprocess executor, а не security boundary; он выключен по умолчанию и не выдан ни одному стандартному preset-у.
- Встроенный backup/restore охватывает только SQLite. Полный disaster-recovery snapshot файлового хранилища, Telegram queue и секретов выполняется внешней файловой процедурой; optional vault включайте только при осознанном `full_owner`/хранении legacy plaintext. БД резервируется автоматически раз в сутки при включённых workers.
- Obsidian Android остаётся beta без завершённой физической acceptance-матрицы.
  Expected-revision writes используют fail-closed conditional publication:
  гонка с peer сохраняет обе ревизии и даёт `conflict`, но автоматического
  merge нет. Лимиты Markdown — не filesystem quota. До P5–P9 нет graph/semantic/tasks/Bases,
  companion и alternate transports.

## Документация

- [Архитектура](docs/ARCHITECTURE.md)
- [V12 model-first: принятое направление и границы phase-1](docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md)
- [Миссии (executive)](docs/EXECUTIVE.md)
- [Безопасность](docs/SECURITY.md)
- [Жизненный цикл данных](docs/DATA_LIFECYCLE.md)
- [Backup и восстановление](docs/BACKUP_AND_RESTORE.md)
- [Эксплуатация и диагностика](docs/OPERATIONS.md)
- [Obsidian на Android без подписки](docs/OBSIDIAN_ANDROID.md)
- [Obsidian implementation tracker](docs/OBSIDIAN_IMPLEMENTATION_TRACKER.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [История изменений](CHANGELOG.md)

## Лицензия и участие

MIT — см. [LICENSE](LICENSE).

Проект персональный: основную работу ведёт владелец вместе с двумя моделями-помощниками
(их инструкции и предложения — в `sol/` и `grok/`). Issue приветствуются; PR принимаются,
если проходят полный гейт из «Как проверяется работа» и не двигают замеренные пороги без
нового замера с заранее объявленным критерием.
