# Резервное копирование и восстановление

## 1. Встроенный SQLite backup

```powershell
jericho backup --label before-upgrade
```

Команда создаёт в `data/backups/` согласованную пару:

```text
jericho-<UTC>-<label>.sqlite3
jericho-<UTC>-<label>.manifest.json
```

Алгоритм:

1. passive WAL checkpoint;
2. SQLite online backup API;
3. `PRAGMA integrity_check` на отдельном connection;
4. SHA-256 и размер файла;
5. manifest с именем БД, schema version и явным scope.

Проверка выполняется fail-closed:

```powershell
jericho verify-backup
jericho verify-backup jericho-20260721T010203Z-before-upgrade.sqlite3
```

Пригодной считается только пара, у которой совпадают безопасный путь, имя, размер, SHA-256 и schema version, а SQLite проходит `integrity_check` без foreign-key нарушений. Отсутствующий, повреждённый или чужой manifest делает backup непригодным для автоматического восстановления.

### Ротация

`JERICHO_BACKUP_KEEP` (по умолчанию **14**) — сколько проверенных копий оставлять локально; `0` выключает уборку. Плановый воркер удаляет лишние **после** зеркалирования, чтобы старшее поколение успело уехать наружу прежде, чем исчезнет отсюда.

Это не аккуратность, а вторая половина ежедневного бэкапа: расписание добавляет полную копию базы каждые 24 часа, а убирать её было некому. Диск заполняется — и уносит с собой живой экземпляр вместе с самими бэкапами.

Удаляются только **полные пары** «БД + manifest». База без manifest не трогается: это форма прерванной записи (в том числе зеркальной), и удалить её значит уничтожить улику, а не освободить место. Зеркало не чистится вовсе — offsite-копия остаётся под контролем оператора.

## 2. Что входит во встроенную копию

Включено только согласованное состояние:

```text
data/state/jericho.sqlite3
```

Не включены:

- `data/files/`;
- `data/memory-vault/`;
- `data/state/telegram-inbox.sqlite3*`;
- `.env`/`.env.local`;
- model weights и container images.

Manifest содержит этот scope явно. SQLite backup нельзя выдавать за полный snapshot установки.

## 3. Безопасное восстановление БД

Сначала остановите backend и Telegram bridge:

```powershell
docker compose down
# либо штатно завершите процессы `jericho server` и `jericho telegram-bridge`
```

Проверьте выбранную копию:

```powershell
jericho verify-backup jericho-20260721T010203Z-before-upgrade.sqlite3
```

Затем выполните:

```powershell
jericho restore-backup jericho-20260721T010203Z-before-upgrade.sqlite3 --yes
```

Без имени используется самая новая пара backup+manifest:

```powershell
jericho restore-backup --yes
```

Команда сама:

1. захватывает эксклюзивный OS-backed backend lease и отказывается работать при запущенном backend;
2. повторно проверяет manifest, hash, размер, schema, `integrity_check` и foreign keys;
3. сохраняет точные байтовые снимки текущих DB/WAL/SHM для автоматического rollback; если текущая БД читается, дополнительно создаёт проверенный online backup `pre-restore-*`; если она уже повреждена или имеет неподдерживаемую схему, создаёт отдельный `recovery-*` bundle с SHA-256 и явной отметкой `verified: false`;
4. закрывает SQLite handles и удаляет только sidecar-файлы активной БД;
5. копирует выбранный backup во временный файл на том же filesystem, повторно сверяет размер и SHA-256 после копирования, делает `fsync` и выполняет атомарный `os.replace`;
6. открывает восстановленную БД, применяет только поддерживаемые forward migrations и повторяет health checks;
7. при любой ошибке автоматически возвращает точные исходные DB/WAL/SHM и проверяет восстановленное состояние.

Прямое копирование `.sqlite3` поверх работающей системы не является поддерживаемым способом восстановления.

Каталоги `data/backups/recovery-*` — аварийные снимки исходных байтов, а не обычные подтверждённые backups. Они намеренно не выбираются командами `verify-backup`/`restore-backup` автоматически и нужны для forensic/recovery-разбора, когда активную БД уже нельзя корректно открыть.

После успешного restore:

```powershell
jericho doctor
jericho server
```

Проверьте Admin UI, число пользователей/знаний, несколько conversation и скачиваемых файлов.

## 4. Полный snapshot установки

Для полного disaster-recovery snapshot:

1. Создайте и проверьте встроенный DB backup.
2. Остановите backend и Telegram bridge.
3. Скопируйте:

```text
data/backups/<выбранная БД + manifest>
data/files/
data/memory-vault/
data/state/telegram-inbox.sqlite3*   если нужно сохранить pending/dead-letter updates
.env или .env.local                  только в отдельное зашифрованное хранилище
```

4. Сохраните checksum списка файлов.
5. Проверьте восстановление в отдельном `JERICHO_HOME`.

Модельные веса можно исключить только при наличии надёжного источника и зафиксированного checksum snapshot.

### Пример PowerShell

После `jericho backup` и остановки процессов:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$target = "E:\JerichoBackups\$stamp"
New-Item -ItemType Directory -Force $target | Out-Null
robocopy D:\jericho\data\files "$target\files" /MIR /COPY:DAT /R:2 /W:2
robocopy D:\jericho\data\memory-vault "$target\memory-vault" /MIR /COPY:DAT /R:2 /W:2
robocopy D:\jericho\data\backups "$target\database" /E /COPY:DAT /R:2 /W:2
robocopy D:\jericho\data\state "$target\state" telegram-inbox.sqlite3* /COPY:DAT /R:2 /W:2
Get-ChildItem $target -Recurse -File | Get-FileHash -Algorithm SHA256 |
  Export-Csv "$target\checksums.csv" -NoTypeInformation -Encoding UTF8
```

Не храните plaintext `.env` рядом с незашифрованной копией, если snapshot покидает доверенный диск.

## 5. Полное восстановление

1. Разверните ту же или совместимую версию Jericho в новом каталоге.
2. Восстановите `data/files/`, `data/memory-vault/` и при необходимости Telegram queue из одного согласованного snapshot.
3. Верните secrets из зашифрованного хранилища.
4. Поместите пару `.sqlite3 + manifest` в `data/backups/`.
5. Запустите `jericho restore-backup <filename> --yes`.
6. Выполните `jericho doctor`, затем product smoke через Admin/API/Telegram.

Не смешивайте БД из одного snapshot с content-addressed files из другого: provenance останется целым, но часть исходников может отсутствовать физически.

## 6. Регулярный recovery drill

Не реже выбранного RPO/RTO-периода:

- восстановите копию в другой `JERICHO_HOME`;
- запустите с `JERICHO_LLM_ENABLED=0` и `JERICHO_WORKERS_ENABLED=0`;
- выполните `jericho doctor`;
- проверьте API vertical slice, retrieval и экспорт пользователя;
- убедитесь, что Raw Object file paths соответствуют восстановленному каталогу;
- зафиксируйте время восстановления и обнаруженные ручные шаги.

Наличие backup без теста восстановления — лишь надежда с расширением `.sqlite3`.
