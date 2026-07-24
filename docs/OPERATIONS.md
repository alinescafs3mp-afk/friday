# Эксплуатация и диагностика

## 1. Конфигурация и первый запуск

Direct Python автоматически читает `./.env.local` либо путь из `JERICHO_ENV_FILE`. Уже заданные environment variables имеют приоритет.

```powershell
jericho init --home D:\jericho
jericho --env-file D:\jericho\.env.local status
```

`init` создаёт runtime-каталоги и `.env.local` атомарно, с private permissions best-effort. Существующий symlink не перезаписывается даже с `--force`.

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

`status` печатает короткую сводку и конкретные действия. `doctor` возвращает тот же snapshot в подробном JSON, удобном для тикета или автоматической проверки.

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

## 3. Процессы и singleton-гарантии

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

## 4. API health

Неаутентифицированный endpoint:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

Проверка identity:

```powershell
$env:JERICHO_TOKEN='<token>'
Invoke-RestMethod http://127.0.0.1:8000/api/me `
  -Headers @{Authorization="Bearer $env:JERICHO_TOKEN"}
```

Admin diagnostics показывает те же schema/backup/worker/lease/actions, но raw JSON оставлен под progressive disclosure для глубокого разбора.

## 5. Обновление

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

0.8.0 использует SQLite schema 8, как и 0.7.0: обновление не меняет авторитетные Knowledge/Graph/Inbox/Conversation records. Отсутствующие производные projections могут идемпотентно достраиваться при открытии. Любая будущая поддерживаемая schema migration выполняется одной транзакцией; неизвестная более новая schema отклоняется fail-closed.

Восстановление выполняется только штатной командой при остановленном backend:

```powershell
jericho restore-backup <backup.sqlite3> --yes
```

Полный контракт описан в [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).

## 6. Ежедневная работа с знаниями

### Telegram modes

- `/chat` — обычный разговор с минимальным tool budget;
- `/work` — многошаговая работа над несколькими личными знаниями и графом;
- `/research` — планирование, web/tool gathering и bounded synthesis;
- `/inbox` — ближайшие предложения с inline promote/ignore;
- `/status` — статистика базы, review pressure и текущий сохранённый режим канала.

`knowledge_work` и `research` могут подготовить структурированный результат, но не записывают его в память напрямую. Кнопка **В Inbox на review** создаёт идемпотентный Raw Object + pending candidate. Пользователь исправляет title/summary/kind/entities и только затем выполняет promotion.

Ответы, опирающиеся на личную базу, используют внутренние маркеры `[K#]`. Feedback 👍/👎 связывается с фактически процитированными/использованными Knowledge Objects и влияет на retrieval без переписывания исходной importance.

### Admin workflow

1. **Inbox** — одиночный или массовый triage с Raw Object, score, explanation и model advice.
2. **Знания** — correction с version snapshot, provenance и entity links.
3. **Граф** — Entity Resolution, relation candidates и potential conflicts; одиночное или bulk accept/reject/confirm/dismiss.
4. **Качество** — feedback, usage, legacy noise и read-only lifecycle candidates.
5. **Диагностика** — системное состояние, recovery actions, workers и leases.

Принятые/отклонённые relation decisions терминальны: background discovery не возвращает их в очередь. Merge, archive, lowering importance и conflict resolution всегда требуют явного действия.

## 7. Background workers

При `JERICHO_WORKERS_ENABLED=1` supervisor запускает tenant-scoped задачи lifecycle, ER candidates, vault, backup, SQLite optimize, quality scan и bounded model advice.

Для каждой задачи сохраняются:

```text
scheduled / running / ok / error / timeout
started_at / finished_at / duration_ms / next_run_at
consecutive_failures / last_error
```

Timeout ограничен. Ошибка одного tenant/item не отменяет результаты остальных, но batch получает degraded health, чтобы частичный сбой не исчез молча. При намеренно остановленном backend старые timestamp не считаются зависанием.

Типичный разбор:

```powershell
jericho status
jericho doctor
```

Затем сопоставьте task name и `last_error` с локальными логами. В error text не должно быть содержимого знаний или secrets.

## 8. Типовые проблемы

### Admin UI просит ключ снова

Token хранится в `sessionStorage`, поэтому новая вкладка/сессия требует повторного ввода. Используйте `JERICHO_API_TOKEN`.

### Backend не запускается второй раз

Это защитное поведение singleton lease. Найдите и штатно завершите существующий `jericho server`/container. Не удаляйте lock-файл как «исправление»: diagnostics отличает stale metadata от активного ownership.

### Backend отклоняет конфигурацию

Проверьте длину secrets, CORS, bind address и URL:

```powershell
jericho doctor
```

### Telegram получает 401

Backend и bridge должны использовать одинаковый `JERICHO_TELEGRAM_BRIDGE_SECRET`; часы host/container не должны сильно расходиться; signature max age по умолчанию 90 секунд.

### Telegram повторяет update

Это ожидаемое at-least-once поведение при временном сбое. Exact retry воспроизводит сохранённый результат, активный конкурент получает retryable `409`, а тот же `source_ref` с другим payload — permanent conflict/dead-letter. Inline-кнопка остаётся доступной после временной ошибки и исчезает только после успешного или окончательно невалидного действия.

### Worker в timeout/error

Проверьте `doctor`, доступность SQLite/LLM, размер очереди и локальные logs. Один повреждённый Inbox item не должен блокировать остальные; после устранения причины следующий успешный цикл сбросит consecutive failures.

### Модель не стартует

Проверьте полный snapshot, совместимость vLLM image, GPU visibility, VRAM и конкретные quantization args. Не уменьшайте профиль вслепую: сначала сохраните реальную ошибку загрузки.

### Vision дал сомнительную экстракцию

Это не авария: visual output advisory-only. Проверьте asset/page evidence и цитируемые фрагменты в Inbox. Незаземлённый high-confidence ответ автоматически понижается, а сложный скан требует ручной correction.

### Backup не проходит проверку

Не используйте его для restore. Сверьте пару `.sqlite3 + .manifest.json`, исключите частичную синхронизацию носителя и создайте новую копию из исправной БД.

### Restore отказывается запускаться

Backend всё ещё владеет lease, отсутствует `--yes` либо backup не прошёл fail-closed verification. Остановите процессы, выполните `verify-backup`, затем повторите штатную команду. Не копируйте БД вручную поверх живого процесса.

## 9. Наблюдаемость и privacy

Telemetry локальная и не отправляет данные наружу. Audit хранит административные изменения и tool calls. Внешний structured-log collector допустим только opt-in и не должен включать содержимое личных знаний, prompts, documents или secrets по умолчанию.
