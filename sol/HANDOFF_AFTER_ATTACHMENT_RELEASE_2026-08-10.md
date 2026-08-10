# Handoff после релиза вложений — 2026-08-10

## Выпущенная точка

- Базовый web-фикс: `ec30f3d09e5a5df2b2f45d5a0704ab934931a34c`
  (`Allow web search in private conversations`).
- Объединённая пачка документов, web-routing, self inventory и audio-фильтра:
  `f53606ea7a0e3e72f1293e3ac1075fa202ce0557`
  (`Исправить полное чтение вложений и список документов`).
- Оба commit находятся в `origin/main`.
- После push 2026-08-10 в 20:22:57 MSK перезапущены пользовательские units
  `friday-backend.service` и `friday-bridge.service`. Обе остались
  `active/running`, `NRestarts=0`, `ExecMainStatus=0`; локальный
  `https://127.0.0.1:8000/api/health` вернул HTTP 200.
- В первую секунду старта bridge была одна Telegram ERROR-запись без traceback;
  после 20:23:15 MSK новых ERROR/traceback не появилось.

## Что теперь гарантирует код

### Любое активное или восстановленное вложение

- Для tenant-authorized current/restored attachment решение о полном чтении
  принимается по состоянию источника, а не по словам «прочитай», «проанализируй»
  или «найди».
- Если обычная проекция на 24 000 символов или Office prompt не содержит весь
  источник, до ответа строится единый full-source map/reduce bundle. Тот же
  bundle доступен обычному agentic loop, поэтому web/reminder/другие разрешённые
  инструменты не теряются.
- Map-stage имеет отдельный deadline и не съедает primary answer deadline.
- Точный lexical lookup больше не отменяет full-source prepass: локальное
  совпадение можно совместить с правилом или контекстом из другой части файла.
- Restored follow-up вида `Что на 288 позиции?` привязывается к ранее реально
  использованному вложению. Для XLSX позиция берётся из parser-owned row spans;
  embedded newline в более ранней ячейке не сдвигает ordinal.
- Полный count по 300-row XLSX после prepass рендерится детерминированно. Exact
  Office `UNKNOWN` остаётся системным структурным вердиктом в metadata.

### Полнота всех поддерживаемых форматов

- Все форматы, которые успешно разбирает `DocumentExtractor`, проходят через
  один source-completeness/prepass контракт, а не только Excel.
- `rows_truncated`, generic `extraction_truncated`, parser deadlines, page caps,
  source truncation и archive loss протянуты до runtime. Неполный источник не
  называется прочитанным целиком: ответ помечается как частичный или exact-запрос
  получает `UNKNOWN`.
- ZIP/TAR/RAR больше не теряют хвост каждого member из-за скрытого 20k slice;
  nested parser loss сохраняется. Oversized TAR и listing-only 7z честно
  помечаются incomplete. Telegram receipt сообщает о частично прочитанном
  archive member даже когда member count формально совпал.
- План ограничен 128 map leaves и конечным временем. Это покрывает обычный
  extractor ceiling одного источника, но несколько предельных файлов или parser
  hard limit могут дать честный partial/UNKNOWN. Нельзя описывать эту границу как
  бесконечный размер; важная гарантия — отсутствие молчаливого head-only ответа.

### Web вместе с файлами и историей

- Same-tenant private/person/attachment context больше не включает history-free
  web isolation и не получает ранний privacy refusal.
- `web_search`, `web_research`, `web_fetch` доступны как при явной просьбе, так и
  при model-selected вызове; обычная ограниченная история сохраняется.
- Same-sentence compound вроде «обобщи документ и поищи в интернете» вызывает
  web research, а не теряет вторую часть.
- На этих маршрутах `code_run` и `data_query` не предлагаются и дополнительно
  отклоняются pre-kernel allowlist. Auth, quotas, SSRF, secret filtering и
  cross-tenant ограничения не снимались.
- Focused attachment не запускает account person/timeline prefetch, поэтому
  содержимое файла не смешивается с посторонней активностью пользователя.

### «Какие я тебе документы скидывал?» и `.ogg`

- Self-target связывается напрямую с authenticated `actor.own_id`, в том числе в
  shared tenant и при коллизии чужого alias. Fuzzy person resolver не может снова
  сделать `я` неоднозначным.
- Точный self documents-only read разрешён по `files.read`; другие пользователи
  и обычная activity остаются под прежним admin gate.
- Запрос без периода означает всё время только при полном отсутствии временного
  указания. Месяц, год, квартал и полугодие не расширяются молча до all-time.
- Voice/audio исключаются до count/limit во всех semantic document inventory,
  list и collect paths по raw content type, media kind, MIME и legacy suffix,
  включая `.ogg`. Обычная activity и управление файлами по-прежнему видят audio.

## Проверки frozen tree

- Объединённые изменённые regression-файлы: `378 passed`.
- Независимый web/history rerun: `218 passed`.
- Документы, Office, archives: `176 passed`.
- Форматы и extractor completeness: `132 passed`.
- Kernel/auth/web filters: `100 passed`.
- Telegram/bridge surfaces: `115 passed`.
- Self inventory и audio/OGG: `61 passed`.
- Репозиторный static quality gate: PASS — `git diff --check`, Ruff lint/format,
  mypy (146 source files), compileall, Bandit HIGH и JavaScript syntax.
- Независимый read-only runtime diff audit: CLEAR по пяти пользовательским
  границам; P0/P1 не найдено.
- Все проверки были offline fake/synthetic. Живая модель и live SQLite для этого
  релиза не вызывались и не открывались; владелец отдельно проверяет поведение в
  реальной Пятнице.

## Состояние после передачи

- Не использовать `git add -A`: два независимых P09-файла батареи остались
  изменёнными вне этого релиза, а owner-owned untracked artifacts сохранены без
  чтения и staging.
- После записи этого handoff и его push все агенты и фоновые работы остановлены
  по прямому указанию владельца. Не продолжать батареи, model calls, новые фиксы
  или мониторинг без нового явного запроса.
