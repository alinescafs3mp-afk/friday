# Jericho

**Jericho** — локальная многопользовательская Knowledge Operating System: она принимает текст и документы, сохраняет первоисточник, строит граф знаний, ищет по личной базе и отвечает через Telegram или HTTP API. Веб-панель предназначена для администрирования, разбора Inbox, работы с сущностями, правами, резервными копиями и диагностикой.

Текущая версия: **0.145.0**. Это release-candidate / 1.0-ready сборка: умеренная классификация, активный граф знаний, управляемая многошаговая работа, миссии с управляемой автономией, замкнутый feedback loop и полноценные эксплуатационные контуры без скрытой автоматической записи.

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
- Неизменяемый первоисточник (`Raw Object`) обязателен для каждого `Knowledge Object`; provenance, version snapshots и soft deletion сохраняются при исправлениях.

### Knowledge Graph, retrieval и агент

- Граф сущностей, типизированных отношений и knowledge links является рабочим источником контекста, а не только визуализацией.
- Entity extraction распознаёт явно названные проекты, инфраструктуру, технологии и версии, организации, события, локации, документы, людей и точные коды (`BRK.A`, `BRNQ26`, ISIN) с confidence/evidence. Пунктуация идентификаторов сохраняется, fuzzy/prefix merge для них запрещён.
- Entity Resolution учитывает точные алиасы, сходство имён, аббревиатуры, общие knowledge links и соседей. Сомнительные сущности только предлагаются к объединению; canonical target выбирает человек, а история merge сохраняется.
- Гибридный поиск объединяет SQLite FTS, lexical similarity, optional embeddings, точные идентификаторы, предметные поля, graph evidence, importance, lifecycle, feedback, quality и promotion confidence.
- Длинные объекты индексируются ещё и по пассажам: один релевантный абзац целой статьи находится, а вектор всего объекта остаётся полом скора (чанкинг может только добавить recall). Выигравший пассаж и цитируется в ответе.
- Для relational-запросов используется аккуратно затухающее двухшаговое расширение графа; обычный поиск остаётся одношаговым, чтобы не тащить шум.
- Низкокачественный legacy chatter и плохо классифицированные объекты получают noise penalty и не должны вытеснять хорошие знания.
- Agent Runtime собирает контекст отдельно из текущего разговора, личных знаний, графа, pending review-сигналов и разрешённых tools; различает ответ из базы, смешанный ответ и общий разговор.
- Режимы `dialogue`, `knowledge_work` и `research` дают разные tool/step budgets. `knowledge_work` объединяет несколько Knowledge Objects и графовый контекст в структурированный work product с маркерами источников `[K1]`, `[K2]`; `research` выполняет bounded synthesis. Любой такой результат сохраняется только как Inbox candidate для явной проверки.
- Текущее состояние feedback и статистика фактического использования Knowledge Objects участвуют в ranking; история feedback остаётся append-only, последняя оценка заменяет отменённую старую, а attribution привязывается к действительно процитированным/использованным знаниям.
- Предлагаемые отношения и потенциальные противоречия попадают в отдельные review-очереди с одиночными и массовыми действиями. Принятые/отклонённые решения терминальны и не переоткрываются фоновым обнаружением; ни связь, ни устаревание, ни конфликт не применяются молча.
- На пустой или маленькой базе агент не придумывает личные факты и прямо объясняет, чего в знаниях пока нет. Proactive structuring ограничена одним уместным предложением.

### Администрирование, безопасность и эксплуатация

- Строгая tenant isolation действует на SQL, graph, conversations, files, feedback, Admin API и tools.
- Capability-based permissions используют default deny, preset-ы `owner`, `admin`, `moderator`, `user`, `guest`, custom presets и явные allow/deny overrides без обходного повышения прав.
- Admin UI/API поддерживают массовый triage Inbox, ручное promotion/correction, model advice, inspection provenance/versions/entity links, Entity Resolution с выбором canonical target, очереди связей/конфликтов, quality dashboard, explain-трейс ретривера (почему запись нашлась/отброшена/так ранжирована) и безопасную ревизию legacy-мусора.
- Веб-страницы сохраняются по URL (`POST /api/ingest/url`, только публичные адреса, очистка и review-gate) прямо из панели или в один клик через букмарклет «Сохранить в Jericho» — он открывает панель с адресом текущей страницы, не храня токен в закладке.
- Legacy quality и lifecycle scan только показывают кандидатов. По выбранным объектам администратор может вернуть материал в Inbox, переобогатить, явно подтвердить, снизить importance, архивировать или выполнить soft delete; worker никогда не применяет эти действия автоматически.
- Telegram bridge использует устойчивую SQLite-очередь, persistent offset, OS-backed singleton lease, idempotency, bounded retry/dead-letter и HMAC-подпись backend-запросов. Временная ошибка inline-действия сохраняет кнопку для безопасного повтора.
- Backend захватывает durable idempotency lease до любых побочных эффектов: точный retry воспроизводит сохранённый результат, активный конкурент получает временный `409`, а повтор того же `source_ref` с иным payload — постоянный conflict без потери новых данных.
- Явное «не запоминай» имеет абсолютный приоритет даже над `force_knowledge`: сообщение и вложение остаются transient и не создают Raw Object, Inbox, Knowledge Object, сущности или файл на диске.
- Найденные знания, graph evidence, имена вложений и tool/web output передаются модели только как недоверенные данные пользовательского уровня; динамический контент не повышается до system-инструкций.
- Admin UI также управляет пользователями, правами, знаниями, графом, разговорами, файлами, аудитом, экспортом, backups и diagnostics.
- Документы и Telegram-вложения ограничиваются ещё во время чтения: CSV/TAR/PDF/Office и сжатые форматы разбираются с byte/entry/page/row/output budget, без предварительного безграничного буферизования; web fetch защищён от SSRF, redirects и DNS rebinding закреплением уже проверенного IP.
- HTTP body limit действует на фактически полученные байты, включая chunked transfer, до аутентификации и JSON/multipart parsing; proxy headers принимаются только от явно доверенного непосредственного proxy-hop.
- Online backup SQLite включает `integrity_check`, SHA-256 manifest и повторную верификацию. `restore-backup` требует остановленного backend через эксклюзивный lease, повторно сверяет staged copy, заменяет БД атомарно и возвращает точные DB/WAL/SHM при сбое; для уже повреждённой активной БД сохраняется отдельный явно непроверенный recovery bundle. Markdown-vault пишет атомарно и использует Windows-safe пути.
- Workers обслуживают всех активных tenants: lifecycle, entity-resolution candidates, vault, ежедневный backup, SQLite optimize, read-only quality report и bounded advisory Inbox refinement. Каждая задача публикует состояние, длительность, следующий запуск, timeout и consecutive failures для `status`, `doctor` и Admin UI.
- Сохранён локальный vLLM-профиль `qwen3.6-35b-a3b-nvfp4`, fail-closed tool-call protocol и редактирование секретов в логах.

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

Полный snapshot модели должен находиться здесь:

```text
D:\jericho\models\qwen3.6-35b-a3b-nvfp4\
```

Веса в архив намеренно не включены. Каталог должен содержать модельные файлы и конфигурацию, пригодные для загрузки vLLM.

### 3. Запуск без LLM для проверки системы

В `.env.local` временно задайте:

```dotenv
JERICHO_LLM_ENABLED=0
JERICHO_WORKERS_ENABLED=0
```

Затем:

```powershell
jericho doctor
jericho server
```

Admin UI: `http://127.0.0.1:8000/admin/`. Нажмите **API-ключ** и вставьте значение `JERICHO_API_TOKEN` из `.env.local`.

Без LLM ingestion, граф, поиск, права, Admin UI и резервирование работают; ответы агента переходят в честный локальный fallback.

## Полный запуск через Docker Compose

1. Скопируйте пример конфигурации:

```powershell
Copy-Item .env.example .env
```

2. В `.env` обязательно задайте:

```dotenv
JERICHO_API_TOKEN=<случайная строка минимум 32 символа>
JERICHO_TELEGRAM_BRIDGE_SECRET=<другая случайная строка минимум 32 символа>
JERICHO_TELEGRAM_BOT_TOKEN=<токен BotFather>
JERICHO_HOST_HOME=D:/jericho
JERICHO_MODEL_ROOT=D:/jericho/models
# Внутри Compose backend обращается к vLLM по имени сервиса:
JERICHO_DOCKER_LLM_BASE_URL=http://dispatcher:8001/v1
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
# Сначала задайте JERICHO_LLM_ENABLED=0 в .env
docker compose up -d --build backend
```

Состояние:

```powershell
docker compose ps
docker compose logs -f backend
docker compose logs -f dispatcher
docker compose logs -f telegram
```

Compose сам собирает закреплённый image `jericho/vllm-openai:v0.25.1-asyncio-e4f88a8` из `docker/vllm-asyncio/Dockerfile`. Базовый vLLM-образ закреплён по digest, а небольшая fail-closed правка применяется только при полном совпадении SHA-256 исходного файла. Отдельная ручная сборка при первом запуске не нужна; её можно выполнить командой `docker compose --profile llm build dispatcher`.

## Обновление существующей установки

1. Остановите backend и Telegram bridge.
2. Сделайте копию каталога `data/` и файлов конфигурации.
3. Замените только исходники проекта; не переносите из архива runtime-каталоги поверх своих данных.
4. Повторите `pip install -e ".[dev]"` и запустите `jericho doctor`.
5. Схема SQLite — **18**; обновление её не переписывает: авторитетные знания, граф, Inbox и разговоры не переписываются. При открытии могут идемпотентно достраиваться отсутствующие производные projections вроде usage state. Более новая неизвестная схема отклоняется без изменений.

Перед обновлением выполните `jericho backup --label before-upgrade` и `jericho verify-backup`: совместимость схемы не заменяет проверенную резервную копию.

## Параметры vLLM

Профиль `qwen36-vl` закрепляет следующие значения:

| Параметр | Значение |
|---|---:|
| model | `models/qwen3.6-35b-a3b-nvfp4` |
| served model name | `dispatcher` |
| max model length | `32768` |
| GPU memory utilization | `0.90` |
| KV cache dtype | `fp8` |
| max sequences | `16` |
| max batched tokens | `4096` |
| tokenizer mode | `auto` |
| multimodal limits | `image=4`, `video=1` |
| prefix caching | включён |
| MM profiling | пропускается |
| MM processor cache | `4.0 GiB` |

Поля `JERICHO_QWEN_QUANT_ARGS`, `JERICHO_QWEN_ENFORCE_EAGER` и `JERICHO_QWEN_EXTRA_ARGS` оставлены для конкретной сборки vLLM/GPU, поскольку точные quantization-флаги зависят от формата локального snapshot.

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
- `/note текст` — явно сохранить заметку;
- `/search запрос` — прямой поиск по подтверждённым знаниям списком, без ответа модели.

Ответы получают inline-оценки 👍/👎. В `knowledge_work` и `research` итог можно отправить кнопкой в Inbox; это предложение на review, а не скрытая запись в граф. `/status` показывает сохранённый режим текущего Telegram-канала.

Принимаются вложения: текст, изображения и документы (с извлечением текста/OCR), а также голосовые, аудио, видео, видео-кружки и анимации. Локальная модель зрения распознаёт изображения; голосовые и аудио при включённом `JERICHO_WHISPER_ENABLED` расшифровываются локально (опциональный пакет `jericho[voice]`, faster-whisper, полностью офлайн) и попадают в Inbox уже текстом — иначе, как и видео, сохраняются как есть с провенансом и метаданными и ждут вашего решения в Inbox, без расшифровки. Геолокация и контакт превращаются в заметку. Неподдерживаемые типы (стикеры, опросы) получают понятный ответ, а не молча теряются. Происхождение пересланных сообщений (кто и когда переслал) сохраняется в провенанс.

Доступ к боту работает по принципу deny-by-default: бот отвечает только чатам из эффективного allowlist (объединение `JERICHO_TELEGRAM_ALLOWED_CHAT_IDS` и `JERICHO_TELEGRAM_OWNER_CHAT_IDS`). Пустой список означает, что не допущен никто. Прошедший allowlist пользователь регистрируется автоматически с preset-ом `user` и получает отдельный tenant ID вида `telegram:<realm>:<telegram_id>`.

Чтобы разрешить конкретные чаты (или задать чат владельца для первичной настройки):

```dotenv
JERICHO_TELEGRAM_ALLOWED_CHAT_IDS=123456789,-1001234567890
JERICHO_TELEGRAM_OWNER_CHAT_IDS=123456789
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
jericho import PATH [--dry-run] [--user U] [--suffix .md] [--limit N]
jericho events [--type TYPE] [--limit N] [--json]
jericho model-check [--json] [--timeout SEC]
jericho eval-bootstrap [--limit N] [--save]
jericho up
jericho tui
jericho install-services
jericho search-source ФРАЗА [--limit N] [--json]
jericho reindex-embeddings [--user U] --yes
jericho backup-keygen
jericho decrypt-backup FILE
jericho purge [--id ID] [--older-than-days N] --yes
jericho mint-token --user U --preset P [--ttl 90d]
jericho revoke-token TOKEN_ID
```

- **`search-source`** — дословный поиск по ИСХОДНОМУ тексту загруженного материала, мимо ранжирования. Нужен, когда помнишь точную фразу из бумаги: 93% загруженных знаков живут только в первоисточнике, а Knowledge Object несёт сокращённую версию. Отклонённое во входящих сюда не входит — это решение, а не фильтр.
- **`up`** — запуск бэкенда и моста под супервизором; **`tui`** — интерактивный лаунчер, самая уместная точка входа, если не хочется помнить команды; **`install-services`** — systemd-юниты, чтобы всё поднималось само.
- **`reindex-embeddings`** — пометить вектора устаревшими, чтобы фоновый индексатор пересчитал их. Понадобится после смены модели эмбеддингов или правки разбиения на пассажи. Поиск при этом продолжает работать на прежних векторах: они не удаляются, а заменяются по мере пересчёта.
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

При `JERICHO_HOME=D:\jericho`:

```text
data/state/jericho.sqlite3       основная БД
data/state/telegram-inbox.sqlite3 очередь Telegram bridge
data/files/                      исходные загруженные файлы
data/memory-vault/               Markdown-представление знаний
data/backups/                    SQLite-копии и SHA-256-манифесты
data/exports/                    JSON-экспорты пользователей
cache/                           временные кэши
logs/                            локальные логи
models/                          веса моделей
```

Runtime-каталоги и секреты исключены из Git и из дистрибутивного архива.

## Резервирование

`jericho backup` создаёт транзакционно согласованную копию **только SQLite-БД**. Полная резервная копия установки должна дополнительно включать:

- `data/files/`;
- `data/memory-vault/`;
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

```powershell
pytest -q
python -m compileall -q jericho tests
jericho doctor
```

Тесты покрывают provenance, tenant isolation, versions, soft delete, review-only lifecycle, backup verification/restore/rollback, entity resolution, терминальные relation decisions и монотонные conflict decisions, три исхода ingestion, feedback replacement и точную attribution, usage-aware retrieval, agent modes, knowledge-work/research-to-Inbox, grounded bounded vision/OCR, bulk Admin workflows, worker timeout/partial failure health, backend singleton lease, capability default-deny и безопасное делегирование, инструментальное ядро и завершение дерева процессов, архивные лимиты, SSRF, подписанный Telegram/API vertical slice, inline callbacks, миграцию/повторы/dead-letter очереди Telegram, redaction логов, tool-call protocol и закреплённый vLLM image/profile.

## Ограничения текущей версии

- Веса модели и собранные Docker layers не входят в архив; воспроизводимый рецепт специализированного vLLM image включён.
- Backend-образ собирается на том же Python 3.14, на котором прогоняется набор тестов, а зависимости фиксируются `requirements.lock`; сборка образа в этом окружении не проверялась (Docker не установлен) — проверено лишь то, что все бинарные зависимости имеют готовые колёса cp314.
- Полнотекстовый поиск работает локально; dense embeddings включаются только при настройке отдельного OpenAI-compatible embeddings endpoint.
- Deterministic entity extraction намеренно консервативно: оно лучше пропустит слабую сущность, чем испортит граф. Local-model advisor расширяет только предложения Inbox; сомнительные links и merge подтверждаются человеком.
- Неявные co-occurrence edges используются только как ранжирующий контекст и явно помечаются как implicit; они не сохраняются в граф как доказанные отношения.
- Vision/OCR для изображений и сканов требует включённого локального мультимодального vLLM и остаётся advisory-only: качество зависит от модели и сложные документы всё равно требуют review.
- RAR/7z поддержка зависит от установленных Python-библиотек и формата архива; опасное содержимое не исполняется и не распаковывается в произвольные пути.
- `code_run` — ограниченный subprocess executor, а не security boundary; он выключен по умолчанию и не выдан ни одному стандартному preset-у.
- Встроенный backup/restore охватывает только SQLite. Полный disaster-recovery snapshot файлового хранилища, vault, Telegram queue и секретов выполняется внешней файловой процедурой; БД резервируется автоматически раз в сутки при включённых workers.

## Документация

- [Архитектура](docs/ARCHITECTURE.md)
- [Миссии (executive)](docs/EXECUTIVE.md)
- [Безопасность](docs/SECURITY.md)
- [Жизненный цикл данных](docs/DATA_LIFECYCLE.md)
- [Backup и восстановление](docs/BACKUP_AND_RESTORE.md)
- [Эксплуатация и диагностика](docs/OPERATIONS.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [История изменений](CHANGELOG.md)
