# Эксплуатация и диагностика

> Проект переименован: **Friday** (по-русски — **Пятница**), ex codename Jericho.

## 1. Конфигурация и первый запуск

Direct Python автоматически читает `./.env.local` либо путь из `FRIDAY_ENV_FILE`. Уже заданные environment variables имеют приоритет.

```powershell
jericho init --home D:\jericho
jericho --env-file D:\jericho\.env.local status
```

`init` создаёт runtime-каталоги и `.env.local` атомарно, с private permissions best-effort. Существующий symlink не перезаписывается даже с `--force`.

У установки, которая продолжает использовать прежний файл `jericho.sqlite3`, задайте
`FRIDAY_DATABASE_PATH` явно. Для боевого сервиса дополнительно задайте
`FRIDAY_DATABASE_MUST_EXIST=1`: отсутствующий или пустой файл тогда останавливает
запуск до открытия SQLite, а не превращается в новую пустую базу. Если рядом лежат
две разные непустые базы `friday.sqlite3` и `jericho.sqlite3`, автоматический выбор
всегда завершается ошибкой и требует явного пути.

После инициализации:

```powershell
jericho status
jericho doctor
```

Состояния:

- `ready` — основные локальные проверки пройдены;
- `attention` — система работоспособна, но есть предупреждение, например отсутствуют веса или свежий backup;
- `setup_required` — не завершён первый запуск либо отсутствует БД/обязательная конфигурация;
- `degraded` — нарушена целостность, зависли/падают workers, конфликтует runtime lease или недоступен явно проверяемый сервис.

`status` печатает короткую сводку и конкретные действия. `doctor` возвращает тот же snapshot в подробном JSON, удобном для тикета или автоматической проверки. Если backend уже владеет process lease, внешний CLI не открывает его SQLite даже `mode=ro`: snapshot приходит от локального authenticated API. Недоступность API при живом lease даёт честный `degraded` без SQLite fallback. При остановленном backend offline-проверка сама удерживает тот же lease на всём окне чтения, поэтому стартующий backend либо ждёт, либо выигрывает до первого SQLite-open.

## 2. Что проверяет doctor

```powershell
jericho doctor
jericho doctor --check-llm
```

Проверяются:

- обязательные secrets и небезопасные network/CORS настройки;
- runtime-каталоги и возможность записи;
- model directory и наличие реальных model files вместо placeholder;
- SQLite schema/integrity/foreign keys/FTS, включая read-only путь при закрытом storage;
- последняя пара backup+manifest;
- backend process lease;
- состояние и свежесть каждой background-задачи;
- TCP-доступность LLM endpoint при `--check-llm`.

`doctor` не загружает модель и не оценивает качество inference. Для этого нужен отдельный реальный запрос к `/v1/chat/completions` и продуктовый smoke на своих документах.

### Гигиена секретов

`jericho doctor` (и sentinel) ищут учётные данные самого Friday в посторонних файлах — сравнением по точному значению, а не по шаблону, поэтому ложных срабатываний нет: файл либо содержит этот токен, либо нет. Проверяются `$HOME` и `FRIDAY_HOME` на глубину 3: до 20 000 путей и 256 МиБ фактически прочитанных данных. Малые файлы идут раньше крупных независимо от порядка каталогов; крупные читаются chunks с overlap, а hardlink-inode — один раз с отдельным отчётом по каждому пути. File cap, byte cap и недоступный/неполный файл дают `secret_scan_incomplete`, а не ложное «чисто». Значение credential никогда не возвращается даже когда оно оказалось частью имени файла или переменной.

Main/bridge SQLite, WAL/SHM/journal, lease-файлы и их текущие hardlink-inodes исключены сильнее обычного protected-файла: scanner не делает над ними даже raw `open(2)`. Живой владелец SQLite остаётся единственным процессом, который читает эти артефакты.

Повод конкретный: живой токен бота двое суток пролежал в открытом файле на рабочем столе, и ничто этого не заметило. Резервные копии `.env.local` тоже считаются лишними копиями и попадают в отчёт.

## 3. Операционные команды

```bash
jericho model-check          # проверить эндпоинт модели генерацией, а не коннектом
jericho events               # журнал: что сломалось и починилось, пока никто не смотрел
jericho eval-bootstrap       # черновики золотого набора для оценки поиска
```

**`model-check`** пробует то, что реально ломает интеграцию: отдаётся ли настроенная модель, отвечает ли она в том бюджете токенов, который Friday использует, не протекает ли цепочка рассуждений в ответ, парсится ли вывод как JSON, есть ли эмбеддинги. TCP-коннект не доказывает ничего, а `/models` доказывает лишь, что сервер слышал про модель.

**`events`** — операционный журнал. Пишутся переходы, а не состояния: воркер, сломанный всю ночь, даёт две записи (`worker.failed`, `worker.recovered`), а не одну на каждый тик. Плюс `backup.created` на каждую копию. Журнал ограничен 2000 записями.

**`eval-bootstrap`** предлагает кейсы для золотого набора из имеющихся знаний, спрашивая у модели, каким вопросом человек искал бы данный объект. Каждое предложение проходит аудит на лексическое пересечение: вопрос, пересказывающий документ его же словами, отклоняется — иначе набор показывает отличный recall, проверяя только совпадение слов. Без `--save` ничего не сохраняется.

## 4. Процессы и singleton-гарантии

Direct mode обычно использует два/три терминала:

```powershell
# 1. vLLM — через Docker/другой launcher
# 2. backend
jericho server
# 3. bridge
jericho telegram-bridge
```

Backend и Telegram bridge имеют разные OS-backed singleton leases. Второй процесс той же роли fail-closed завершается до запуска workers, polling или иных side effects. Наличие `.lock`-файла само по себе не означает живой процесс; ownership определяется ОС и отображается в diagnostics.

Docker mode:

```powershell
docker compose --profile llm --profile telegram up -d --build
docker compose ps
docker compose logs --tail 200 backend
docker compose logs --tail 200 dispatcher
docker compose logs --tail 200 telegram
```

Используйте штатный SIGTERM/`docker compose down`. Принудительное завершение допустимо только как аварийная мера.

### Что происходит при штатной остановке

1. Отменяются asyncio-задачи воркеров. Это **не** останавливает уже запущенные блокирующие вызовы: `asyncio.to_thread` прекращает *ожидание*, поток живёт дальше.
2. Остановка **ждёт** завершения таких вызовов — по бюджету самих воркеров (самый длинный `timeout_sec`, сейчас 900 с, плюс запас), а не по константе. Прежние 30 с были короче, чем воркеру **разрешено** держать поток: `knowledge_dedup` сканирует до 600 с внутри такта в 900 с, и остановка бросала работу, которая законно продолжалась. Если по истечении бюджета что-то ещё выполняется — в лог уходит `WARNING` с перечнем.
3. Только после этого `storage.close(final=True)`.

`final=True` — это не косметика. Свойство `conn` прозрачно переоткрывает соединение, когда сменилось поколение (на этом держится восстановление из бэкапа), поэтому поток, переживший остановку, **молча получал новое соединение** и продолжал писать уже после того, как процесс отпустил `backend.lock` — а его к этому моменту мог занять новый backend. Теперь такой поток получает `StorageClosedError`. Обычный `close()` (без `final`) по-прежнему переоткрывается: на этом работает `restore-backup`.

### Логи детей `jericho up`

Супервизор пишет stdout/stderr каждого ребёнка в `<log_dir>/<имя>.log` и **проворачивает** файл, когда тот перерастает `FRIDAY_LOG_MAX_BYTES` (по умолчанию 16 МиБ), храня `FRIDAY_LOG_BACKUPS` поколений (`backend.log.1`, `.2`, `.3`). `0` в первой переменной выключает ротацию.

Ротация нужна не для порядка, а потому что рост реальный: мост опрашивает `GET /api/notifications/pending` каждые 15 с, и каждый опрос стоит строки в access-логе — `backend.log` набирает десятки мегабайт в сутки и без потолка однажды забьёт диск.

Проворот — **copy-truncate**, а не переименование: ребёнок держит унаследованный дескриптор на тот же inode, и после `rename` он продолжил бы писать в уведённый файл, а живой лог остался бы пустым до перезапуска. Дескриптор открыт с `O_APPEND`, поэтому усечение не оставляет дыры. Плата — несколько строк, попавших в окно между копированием и усечением; это ровно тот компромисс, что и у `logrotate copytruncate`.

### Native TLS под systemd --user

Native TLS переключает единственный uvicorn port целиком. Поэтому backend и bridge
обновляются как один контур, но с проверяемым checkpoint между рестартами. До
изменения `.env.local` сертификат должен иметь SAN как минимум для
`127.0.0.1`/`localhost` (либо `::1` для IPv6 bind) и для каждого имени либо IP,
который набирает браузер.
Проверяйте именно SAN, не только `CN`:

```bash
openssl x509 -in /absolute/path/server.crt -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in /absolute/path/server.crt -noout -checkip 127.0.0.1
openssl x509 -in /absolute/path/server.crt -noout -checkhost localhost
openssl x509 -in /absolute/path/server.crt -noout -checkhost LAN-NAME
openssl x509 -in /absolute/path/server.crt -noout -checkip LAN-IP
```

Private key должен принадлежать runtime-user и иметь режим `0600`; cert/CA —
публичные данные, но каталог TLS всё равно держите private. Cert/CA не должны быть
тем же файлом, symlink или hardlink, что key: validator отклоняет такой alias без
чтения содержимого. Затем задайте в том env-файле, который указан в
`ExecStart --env-file`:

```dotenv
FRIDAY_API_HOST=0.0.0.0
FRIDAY_SSL_CERTFILE=/absolute/path/server.crt
FRIDAY_SSL_KEYFILE=/absolute/path/server.key
FRIDAY_BACKEND_CA_FILE=/absolute/path/ca-or-self-signed-server.crt
FRIDAY_CORS_ORIGINS=https://127.0.0.1:8000,https://localhost:8000,https://LAN-NAME-OR-IP:8000
```

Для same-host systemd не задавайте `FRIDAY_BACKEND_URL`: при TLS bridge сам
выбирает `https://127.0.0.1:<port>`. Если override нужен, он обязан быть HTTPS и
его hostname обязан входить в SAN. `FRIDAY_BACKEND_CA_FILE` указывает только на
public CA/certificate — никогда на key.

Установленные CLI units обычно называются `jericho-backend.service` и
`jericho-bridge.service`; у переименованной существующей установки это могут быть
`friday-backend.service` и `friday-bridge.service`. Сначала переключите backend и
докажите HTTPS health через CA, только затем перезапустите bridge:

```bash
systemctl --user restart jericho-backend.service
curl --fail --silent --show-error --cacert /absolute/path/ca-or-self-signed-server.crt \
  https://127.0.0.1:8000/api/health
systemctl --user restart jericho-bridge.service
systemctl --user is-active jericho-backend.service jericho-bridge.service
FRIDAY_ENV_FILE=/absolute/path/.env.local jericho doctor
```

Не применяйте `curl -k`, `verify=False` или постоянное browser exception: такой
тест доказывает шифрование, но не identity сервера. Импортируйте публичный CA в
trust store устройства и проверьте Admin UI по каждому реальному LAN origin.

Rollback до рабочего HTTP выполняется тем же порядком: очистите одновременно
`FRIDAY_SSL_CERTFILE`, `FRIDAY_SSL_KEYFILE`, `FRIDAY_BACKEND_CA_FILE`, верните
`http://` origins, перезапустите backend, проверьте `/api/health`, затем
перезапустите bridge. Не оставляйте половину TLS-пары — validator её отклоняет.

Base Compose намеренно не включает native TLS: backend и bridge общаются по HTTP
только внутри private Docker network, а общая среда принудительно очищает server
cert/key/CA paths. Это не даёт Telegram-контейнеру прочитать private key. Для
внешнего HTTPS публикуйте backend только через отдельный TLS reverse proxy; не
кладите server key в общий `/runtime/data` и не пробрасывайте его в bridge.

## 5. API health

Неаутентифицированный endpoint:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

Проверка identity:

```powershell
$env:FRIDAY_TOKEN='<token>'
Invoke-RestMethod http://127.0.0.1:8000/api/me `
  -Headers @{Authorization="Bearer $env:FRIDAY_TOKEN"}
```

Admin diagnostics показывает те же schema/backup/worker/lease/actions, но raw JSON оставлен под progressive disclosure для глубокого разбора.

## 6. Обновление

Перед заменой кода:

```powershell
jericho backup --label before-upgrade
jericho verify-backup
pytest -q
```

Затем:

1. остановите backend и Telegram bridge;
2. замените только исходники, сохранив `data/`, модели и secrets;
3. активируйте venv и выполните `pip install -e ".[dev]"`;
4. запустите `jericho doctor`;
5. запустите backend и проверьте Admin/Telegram smoke.

0.205.0 использует SQLite schema 33, как и 0.204.2: Qwen3.8/SGLang
profile и owner degradation alerts не добавляют миграцию и не меняют
авторитетные Knowledge/Graph/Inbox/Conversation records. Sentinel хранит
только техническое состояние эпизода в существующем `runtime_kv`.
Отсутствующие производные projections могут идемпотентно достраиваться при
открытии. Любая будущая поддерживаемая schema migration выполняется одной
транзакцией; неизвестная более новая schema отклоняется fail-closed.

Восстановление выполняется только штатной командой при остановленном backend:

```powershell
jericho restore-backup <backup.sqlite3> --yes
```

Полный контракт описан в [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).

## 7. Ежедневная работа с знаниями

### Telegram modes

- `/chat` — обычный разговор с минимальным tool budget;
- `/work` — многошаговая работа над несколькими личными знаниями и графом;
- `/research` — планирование, web/tool gathering и bounded synthesis;
- `/inbox` — ближайшие предложения с inline promote/ignore;
- `/status` — статистика базы, review pressure и текущий сохранённый режим канала.

`knowledge_work` и `research` могут подготовить структурированный результат, но не записывают его в память напрямую. Кнопка **В Inbox на review** создаёт идемпотентный Raw Object + pending candidate. Пользователь исправляет title/summary/kind/entities и только затем выполняет promotion.

Ответы, опирающиеся на личную базу, используют внутренние маркеры `[K#]`. Feedback 👍/👎 связывается с фактически процитированными/использованными Knowledge Objects и влияет на retrieval без переписывания исходной importance.

### Массовый импорт с диска

```bash
jericho import ~/Документы --dry-run          # состав, ничего не записывая
jericho import ~/Документы                    # загрузить
jericho import ~/Документы --uploaded-by USER # кто действительно принёс файлы
jericho import ~/Архив --suffix .md --limit 500
```

Обходит каталог и прогоняет каждый файл через обычный конвейер приёма. Всё уходит в Inbox — указание на папку не является решением о каждом файле внутри неё.

CLI не аутентифицирует человека и поэтому не выводит автора из `--user`: в общем архиве этот идентификатор называет общий tenant. Передайте `--uploaded-by ACCOUNT_ID`, только если автор действительно известен. Без флага новые Raw Objects получают явное `uploaded_by: null` и учитываются как материалы без автора; повторный импорт не переписывает метаданные уже существующих объектов.

Возобновляемость даром: `source_ref` выводится из хеша содержимого, поэтому повторный запуск на том же дереве не грузит ничего заново, а прерванный прогон продолжается той же командой. Побайтово одинаковые файлы по разным путям схлопываются в один объект.

Каталоги вроде `node_modules`, `.git`, `__pycache__`, `.venv` отсекаются до входа. Обход упорядочен, поэтому `--limit` режет большое дерево на партии, а не на случайную выборку.

### Разбор Inbox группами

После импорта в очереди тысячи материалов. Панель **Группы непроверенного** в разделе Inbox режет её по типу файла, каталогу или источнику: на настоящем импорте 200 файлов это 14 групп с крупнейшей на 168.

Групповое действие **только отклоняет** (архив/игнорировать). Продвижение остаётся поштучным, через «Разобрать», где виден исходный текст: одобрение двухсот непрочитанных материалов решением не является, а продвижение вдобавок запускает обогащение, создаёт сущности графа и ставит в очередь кандидатов на связи и конфликты.

### Admin workflow

1. **Inbox** — одиночный или массовый triage с Raw Object, score, explanation и model advice.
2. **Знания** — correction с version snapshot, provenance и entity links.
3. **Граф** — Entity Resolution, relation candidates и potential conflicts; одиночное или bulk accept/reject/confirm/dismiss.
4. **Качество** — feedback, usage, legacy noise и read-only lifecycle candidates.
5. **Диагностика** — системное состояние, recovery actions, workers и leases.

Принятые/отклонённые relation decisions терминальны: background discovery не возвращает их в очередь. Merge, archive, lowering importance и conflict resolution всегда требуют явного действия.

## 8. Background workers

При `FRIDAY_WORKERS_ENABLED=1` supervisor запускает tenant-scoped задачи lifecycle, ER candidates, vault, backup, SQLite optimize, quality scan и bounded model advice.

Для каждой задачи сохраняются:

```text
scheduled / running / ok / error / timeout
started_at / finished_at / duration_ms / next_run_at
consecutive_failures / last_error
```

Supervisor timeout ограничивает ожидание и health-state, но не останавливает уже запущенный `asyncio.to_thread` blocking thread. Поэтому blocking workers обязаны иметь внутренние кооперативные budgets; `entity_mention_backfill` возвращает `budget_exhausted` / `budget_reason` / `has_more` и продолжает работу следующим tick. Его штатные пределы на tenant — `8 documents / 25 links / <=10 s`; content читается bounded UTF-8 blob-страницами, а числовые character/byte cursors и технические spool/checkpoint переживают yield и рестарт без сохранения текста, имён или snippets. Malformed/stale progress автоматически переходит в bounded cleanup/revalidation. Ошибка одного tenant/item не отменяет результаты остальных, но batch получает degraded health, чтобы частичный сбой не исчез молча. При намеренно остановленном backend старые timestamp не считаются зависанием.

Типичный разбор:

```powershell
jericho status
jericho doctor
```

Затем сопоставьте task name и `last_error` с локальными логами. В error text не должно быть содержимого знаний или secrets.

## 9. Типовые проблемы

### Admin UI просит ключ снова

Token хранится в `sessionStorage`, поэтому новая вкладка/сессия требует повторного ввода. Используйте `FRIDAY_API_TOKEN`.

### Backend не запускается второй раз

Это защитное поведение singleton lease. Найдите и штатно завершите существующий `jericho server`/container. Не удаляйте lock-файл как «исправление»: diagnostics отличает stale metadata от активного ownership.

### Backend отклоняет конфигурацию

Проверьте длину secrets, CORS, bind address и URL:

```powershell
jericho doctor
```

### Telegram получает 401

Backend и bridge должны использовать одинаковый `FRIDAY_TELEGRAM_BRIDGE_SECRET`; часы host/container не должны сильно расходиться; signature max age по умолчанию 90 секунд.

### Telegram повторяет update

Это ожидаемое at-least-once поведение при временном сбое. Exact retry воспроизводит сохранённый результат, активный конкурент получает retryable `409`, а тот же `source_ref` с другим payload — permanent conflict/dead-letter. Inline-кнопка остаётся доступной после временной ошибки и исчезает только после успешного или окончательно невалидного действия.

### Worker в timeout/error

Проверьте `doctor`, доступность SQLite/LLM, размер очереди и локальные logs. Один повреждённый Inbox item не должен блокировать остальные; после устранения причины следующий успешный цикл сбросит consecutive failures.

### Модель не стартует

Проверьте полный snapshot, совместимость vLLM image, GPU visibility, VRAM и конкретные quantization args. Не уменьшайте профиль вслепую: сначала сохраните реальную ошибку загрузки.

### Vision дал сомнительную экстракцию

Это не авария: visual output advisory-only. Проверьте asset/page evidence и цитируемые фрагменты в Inbox. Незаземлённый high-confidence ответ автоматически понижается, а сложный скан требует ручной correction.

### Office отвечает, что точный состав неизвестен

Это fail-closed результат, а не повод включать менее строгий model prompt. Exact
count/list доступен только native DOCX/XLSX с полным `OfficeStructureIndex v1` и
целым structured prompt. Формула без cached value, неоднозначная/merged шапка,
nested table, header/footer, скрытый legacy-parser-ом OOXML text или любой
text/row/index/prompt budget снимает полноту. То же происходит при отсутствующей или
неверной local attestation индекса: она связана с SHA-256 байтов конкретного Raw и
не переносится между файлами. Старые Raw Objects автоматически не
backfill-ятся: повторно пришлите исходный файл, если нужен новый code-owned
inventory. Не копируйте живое содержимое документа в диагностические команды или
внешние model probes; для воспроизведения используйте synthetic файл.

### Backup не проходит проверку

Не используйте его для restore. Сверьте пару `.sqlite3 + .manifest.json`, исключите частичную синхронизацию носителя и создайте новую копию из исправной БД.

### Restore отказывается запускаться

Backend всё ещё владеет lease, отсутствует `--yes` либо backup не прошёл fail-closed verification. Остановите процессы, выполните `verify-backup`, затем повторите штатную команду. Не копируйте БД вручную поверх живого процесса.

## 10. Наблюдаемость и privacy

Telemetry локальная и не отправляет данные наружу. Audit хранит административные изменения и tool calls. Внешний structured-log collector допустим только opt-in и не должен включать содержимое личных знаний, prompts, documents или secrets по умолчанию.

## 11. V12 shadow и opt-in file/archive canary

Без настройки работает только прежний runtime. Переключатель обратим и не
меняет схему БД:

```dotenv
FRIDAY_ROUTER_MODE=legacy
FRIDAY_ROUTER_CANARY_ROUTES=file_read
FRIDAY_ROUTER_CANARY_USER_IDS=
FRIDAY_ROUTER_PLAN_TIMEOUT_SEC=12
```

`shadow` строит технический план в фоне, но ответ и эффекты всегда принадлежат
legacy. `canary` требует непустой allowlist пользователя, хотя бы один явно
разрешённый route (`file_read` и/или `archive_read`) и успешную live-аттестацию
профиля `qwen38-27b-nvfp4-sglang:dispatcher:v12.14` при старте backend.
`file_read` обрабатывает 1–2 полных UTF-8
файла текущего хода. `archive_read` обрабатывает только прежние полные UTF-8
файлы самого actor: уникальное точное имя, ровно 1–2 последних файла или не
более двух файлов за точный локальный день «сегодня / вчера / позавчера».

Неоднозначный selector, явный другой пользователь, reply/replay, запрос более
двух файлов, PDF, изображения/OCR, partial extraction, web и effects
автоматически остаются в legacy. Авторизация выполняется до чтения body; при
публикации selector и каждый Raw Object повторно проверяются под одним SQLite
write barrier, а conn-scoped idempotency fence фиксируется атомарно с ответом.

Канонический deployment graph для этого profile: SGLang image
`lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124`,
source `c4271c3fe1262fc2adbd162c33b25de5255251c5`, reported version
`0.0.0.dev0+qwen38.27b.g561c8f3`, model revision
`bfd9b31207712e0850eec9da32261e8c5ee16af7`, alias `dispatcher`, context/total
tokens `40960`, running/Mamba cache `6`, `mem_fraction_static=0.90`, FP8 E4M3 KV,
Radix/speculation off, full decode CUDA graphs `1..6`, prefill graph off. Startup
дополнительно требует exact per-process deployment witness для
engine/base image, model snapshot, launch manifest и closed same-origin proxy.
Build-time hashes берутся из code-owned profile/witness и не вводятся
оператором вручную. Не печатайте raw witness: adapter выносит в health
только ограниченную публичную проекцию.

Минимальная owner-only конфигурация canary:

```dotenv
FRIDAY_ROUTER_MODE=canary
FRIDAY_ROUTER_CANARY_ROUTES=file_read,archive_read
FRIDAY_ROUTER_CANARY_USER_IDS=owner
FRIDAY_ROUTER_PLAN_TIMEOUT_SEC=12
```

Startup probe синхронный и может занять до 330 секунд. При контролируемом
переключении сначала штатно остановите bridge, затем запустите backend и не
возвращайте bridge до полного health-подтверждения:

```text
orchestration.configured_mode = canary
orchestration.installed_mode = canary
orchestration.registered_routes = [archive_read, file_read]
orchestration.model_gate.profile_id = qwen38-27b-nvfp4-sglang:dispatcher:v12.14
orchestration.model_gate.status = canary_ready
orchestration.model_gate.reason_code = live_attestation_clear
orchestration.model_gate.verified_context_tokens = 8192
```

Во время probe `/api/health` ещё недоступен. Ждите до 420 секунд и дополнительно
требуйте `status=ok` и `version=0.205.0`.

HTTP `status=ok` при `installed_mode=legacy` означает безопасную деградацию, но
не успешный canary. В `canary`/`v12` Sentinel не реже раза в минуту
проверяет только bounded public gate status. `revoked`, `not_installed` и
недоступность observer дают владельцу одно санитизированное
предупреждение на непрерывный эпизод; recovery перевооружает следующий.
Обычный route fallback не алертит, private reason не попадает в очередь,
а quiet hours сохраняются.

Для мгновенного отката оставьте bridge остановленным,
верните `FRIDAY_ROUTER_MODE=legacy`, перезапустите backend, подтвердите
configured/installed `legacy` и только затем снова запустите bridge. Обычный
rollback кода не требует восстановления SQLite.
