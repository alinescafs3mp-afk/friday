# Obsidian на Android без подписки

Friday синхронизирует один личный Markdown-vault с одним
Android-устройством через отдельный управляемый профиль Syncthing.
Аккаунт Obsidian, Obsidian Sync, desktop-клиент, второй экран и плагин
Friday не нужны.

> Статус: Linux-контур реализован и проходит автоматические проверки; ручная
> приёмка на физическом Android ещё не зафиксирована. Поэтому пользовательский
> сценарий остаётся beta, а Syncthing-Fork 2.1.0.0+ — кандидатом на минимальную
> Android-версию. Docker-контур в этот релиз не сертифицировался.

## Контракт конфигурации

Нативный immutable-release оператор проверяет следующий минимальный
env-контракт:

```dotenv
FRIDAY_OBSIDIAN_ENABLED=1
FRIDAY_PUBLIC_BASE_URL=https://friday.example
FRIDAY_WORKERS_ENABLED=1
```

`FRIDAY_PUBLIC_BASE_URL` — внешний HTTPS origin без credentials, path, query и
fragment. Reverse proxy должен отдавать с этого origin маршруты
`/obsidian/setup*`, `/obsidian/open*` и
`/api/public/obsidian/setup/resolve`. `FRIDAY_WORKERS_ENABLED=1` нужен для
автоматической сверки; кнопка «Проверить подключение» также запускает
сверку вручную.

Остальные операторские параметры и их фактические границы:

| Параметр | По умолчанию / контракт |
| --- | --- |
| `FRIDAY_OBSIDIAN_ROOT` | `<FRIDAY_HOME>/data/obsidian`; отдельный private root |
| `FRIDAY_SYNCTHING_BINARY` | `/usr/local/bin/syncthing`; существующий absolute executable, не symlink |
| `FRIDAY_SYNCTHING_MIN_VERSION` / `MAX_VERSION` | `2.1.3` / `2.2.0`; полуинтервал `[2.1.3, 2.2.0)` |
| `FRIDAY_OBSIDIAN_PAIRING_TTL_SEC` | `900`, от 300 до 3600 |
| `FRIDAY_OBSIDIAN_MAX_PROFILES` | `64`, от 1 до 512 |
| `FRIDAY_OBSIDIAN_TRANSPORT_MODE` | только `discovery_relay` |
| `FRIDAY_OBSIDIAN_RECONCILE_INTERVAL_SEC` | `10`, от 2 до 60 |
| `FRIDAY_OBSIDIAN_REST_TIMEOUT_SEC` | `5`, от 1 до 30 |
| `FRIDAY_OBSIDIAN_PUBLIC_SETUP_RATE_LIMIT_PER_MINUTE` | `10`, от 1 до 120 на IP |

Dockerfile закрепляет Syncthing **2.1.3** и SHA-256 для `linux/amd64` и
`linux/arm64`; direct-установка должна предоставить бинарник сама. GUI/REST
работает через owner-private Unix socket, а sync-listener заменён на relay
endpoint. Global discovery и relays включены; сервер может также сам
установить исходящее direct-соединение. Входящие Syncthing/GUI-порты
публиковать не нужно.

Root должен принадлежать пользователю процесса и иметь mode `0700`.
Friday не исправляет чужой существующий root молча: symlink, чужой owner,
широкие permissions, системный/слишком широкий каталог и пересечение с
state/files/models/cache/logs/backups останавливают startup.

## Подключение с одного Android-устройства

Непройденная ещё физическая acceptance-матрица начинается с
Syncthing-Fork **2.1.0.0+**; это кандидат, а не уже доказанный минимум.

1. Установите Obsidian и Syncthing-Fork из источников их проектов. Дайте
   Syncthing-Fork доступ к общему хранилищу Android; private storage
   Obsidian не подходит.
2. В личном чате с Friday отправьте `/obsidian`. В группе команда и
   все её кнопки намеренно не работают.
3. Нажмите первую кнопку «Скопировать Friday Device ID». Полный ID
   также остаётся выделяемым текстом. Если Telegram не поддерживает
   `copy_text`, откройте одноразовую HTTPS-инструкцию. QR в текущей
   панели нет и для happy path он не нужен.
4. В Syncthing-Fork откройте **Devices** → **Add device** (`+`), вставьте Friday
   Device ID, задайте имя `Friday` и сохраните. Android Device ID обратно
   в Telegram копировать не нужно.
5. Вернитесь в Telegram и нажмите «Проверить подключение». Friday
   выберет ровно одно pending-устройство автоматически; если их несколько,
   выберите своё по имени и суффиксу ID. Friday ничего не угадывает.
6. В Syncthing-Fork примите предложенную папку `Friday`, выберите путь
   в общем хранилище, например `Documents/Obsidian/Friday`, и оставьте
   folder type **Send & Receive**.
7. В Obsidian выберите **Open folder as vault** и эту же папку. Если имя
   vault не `Friday`, сразу сообщите Friday точное имя, сохраняя пробелы:

   ```text
   /obsidian_alias Личный Vault
   ```

   Alias нормализуется в NFC и обрезается по краям; допустимы 1–100
   символов и не более 256 UTF-8 bytes, без `/`, `\` и control characters.

8. Нажимайте «Проверить подключение», пока Friday не докажет точную
   доставку `Friday Connection Test.md`. **Только после этого** появятся
   кнопки «Открыть тестовую заметку в Obsidian» и «Тестовая заметка
   открылась».
9. Сначала нажмите кнопку открытия, убедитесь, что в Obsidian открылась
   именно тестовая заметка, затем вернитесь в Telegram и нажмите
   «Тестовая заметка открылась». Лишь после доставки и этого явного
   подтверждения onboarding переходит в `ready`.

Повторный `/obsidian` возобновляет тот же durable aggregate,
ротирует одноразовую HTTPS-capability (по умолчанию на 15 минут) и не
создаёт второй
профиль, device, folder или тестовую заметку. После успешной настройки
разрешите Syncthing-Fork работу в фоне и при необходимости исключите его из
агрессивной battery optimization.

## Статусы без ложных обещаний

Onboarding `state=ready` означает, что тестовая ревизия была доставлена и
пользователь подтвердил её открытие. Это не означает, что телефон
сейчас online: отдельный `sync_state` бывает `android_connected`,
`android_offline` или `unavailable`.

Для каждой записи факты разделены:

- `local_write_complete`: Friday атомарно записал файл на сервере;
- `server_scan_complete`: локальная версия Syncthing точной ревизии совпала
  с global-версией;
- `android_connected`: свежее, немонотонное наблюдение «устройство подключено
  и не paused»;
- `android_received`: подключённый Android объявил эту файловую версию
  available, remote state равен `valid`, completion равен 100%, а `needBytes` и
  `needItems` — нулю. Friday сверяет exact operation revision до и
  после этих Syncthing-наблюдений, чтобы не приписать доставку
  уже заменённой ревизии;
- `obsidian_opened`: только явное подтверждение открытия. Onboarding не
  доказывает, что Obsidian открыл любую последующую обычную заметку.

Журнал операций движется от `prepared`/`committed` через `scan_pending`
и `scan_complete` к `delivery_pending` или `delivered`. `delivery_pending` означает
успешную локальную запись, которая дожидается Android, а не ошибку.
`uncertain` не разрешает создавать новый effect: повторяйте тот же owner +
`operation_id` для сверки. `conflict` и `failed` останавливают автоматическое
повторение эффекта; дальше нужна явная reconciliation/cancel-процедура.

## Поверхность первого релиза

Доступны list, lexical search, read, create, idempotent append, typed
properties и daily note (`Daily/YYYY-MM-DD.md`). Каждая мутация требует
owner-scoped `operation_id`; его повтор с теми же аргументами идемпотентен, а с
другими отклоняется. Текущий HTTP-контракт:

```text
GET  /api/obsidian/status
GET  /api/obsidian/diagnostics
GET  /api/obsidian/onboarding
GET  /api/obsidian/vaults
GET  /api/obsidian/notes
GET  /api/obsidian/notes/search
GET  /api/obsidian/notes/read
GET  /api/obsidian/operations/{operation_id}
POST /api/obsidian/onboarding/start
POST /api/obsidian/onboarding/check
POST /api/obsidian/onboarding/select-device
POST /api/obsidian/onboarding/confirm-open
POST /api/obsidian/onboarding/retry
POST /api/obsidian/onboarding/cancel
POST /api/obsidian/onboarding/vault-alias
POST /api/obsidian/operations
```

Все private API требуют `obsidian.connect`, `obsidian.read` или
`obsidian.write` и всегда берут owner из authenticated `actor.own_id`. Поля
`user_id`/`owner_id` не принимаются, включая shared archive.

Conflict-копии Syncthing сохраняются, исключаются из обычных list/search и
показываются в diagnostics. Автоматического compare/merge/удаления в этой
версии нет; Syncthing-папка использует staggered versioning с предельным
возрастом 365 дней.

## Безопасность, лимиты и известные границы

- Первый срез — один owner, один Android и один logical vault на
  изолированный профиль; shared/multi-device topology не поддержана.
- REST API key остаётся в private `config.xml`, не попадает в SQLite,
  Telegram или argv. Setup capability передаётся только в URL fragment,
  удаляется из address bar, одноразова; в SQLite лежит только SHA-256.
- Профиль допускает только своё Android-устройство и свою папку;
  `introducer` и `autoAcceptFolders` запрещены, а drift конфигурации fail-closed.
- Note API принимает только relative POSIX `.md` paths до 2048 символов и
  32 каталогов; traversal, absolute/Windows paths, NUL, backslash, symlinks и
  non-regular files отклоняются.
- Верхнеуровневые `.stfolder`, `.stignore`, `.stversions`, `.trash` и
  `.obsidian` зарезервированы; обычные note API не входят ни в какой
  `.obsidian` и не принимают имена с `.sync-conflict-`.
- Пороги: 4 MiB на заметку, 20 000 просмотренных entries, 5 000
  Markdown paths, 32 MiB суммарно прочитанного Markdown, 1 000 результатов list
  и 100 search. Это бюджеты API/traversal, **не filesystem quota**:
  не-Markdown вложения и peer всё ещё могут заполнить диск. Оператор должен
  наложить квоту filesystem/container и мониторить свободное место.
- Для записи с `expected_revision` Friday атомарно захватывает и арендует
  текущий inode, сверяет SHA-256, пишет durable transaction journal рядом с
  vault и выполняет Linux `renameat2(RENAME_EXCHANGE)`. После обмена он повторно
  проверяет поколения и crash-recoverably завершает или откатывает транзакцию.
  Если peer пишет в этом окне, его каноническая ревизия не затирается, обе
  стороны сохраняются как `.sync-conflict-*.md`, а операция возвращает
  `conflict`. Это bounded fail-closed preserve-both protocol, а не обещание
  общего POSIX pathname CAS или автоматического merge.

## Backup, ручная приёмка и следующие фазы

Обычный `jericho backup` содержит onboarding, profiles/vault metadata,
operation ledger и conflict records, но не Markdown, `config.xml`, device
identity и Syncthing index. Во время immutable release cutover отдельный
оператор останавливает writers и включает SQLite, Telegram inbox и весь точный
`FRIDAY_OBSIDIAN_ROOT` в один проверяемый crash-recoverable recovery set. Для
общего disaster recovery по-прежнему нужен остановленный зашифрованный snapshot
этих данных одного поколения; процедура описана в
[BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).

До production-сертификации остаются ручные прогоны на физическом Android:
one-phone copy/paste, folder permissions, exact alias and test-note order, background/offline
delivery, backend restart, direct/relay observation и conflict preservation. Актуальный
чеклист находится в
[OBSIDIAN_IMPLEMENTATION_TRACKER.md](OBSIDIAN_IMPLEMENTATION_TRACKER.md).

P5–P9 остаются follow-up: stable note identity/link graph; semantic retrieval и
operational memory; tasks/Bases/managed regions и ingestion bindings; optional Android companion;
затем alternate transports, pooled daemon, helper/intents, desktop/MCP facade. Этого нет в
первом релизе.
