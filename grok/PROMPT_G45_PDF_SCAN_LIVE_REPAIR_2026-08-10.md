# Grok 4.5: срочно проверить и исправить PDF/сканы на живом пути Friday

Скопируй этот файл в Grok 4.5 **целиком**. Режим рассуждений — высокий.

---

Ты — независимый инженер Friday. Предыдущий исполнитель выпустил исправление
PDF/сканов и получил зелёные тесты, но владелец проверил живую систему и сообщил:
**правки не подействовали**.

Речь именно о двух дефектах:

1. Friday отказывается читать/видеть **успешно распарсенный PDF** и просит
   загрузить его повторно.
2. Friday уверенно сообщает паспортоподобные поля и числа по **JPEG-скану без
   извлечённого текста, OCR и vision evidence**.

Не уходи в общую переделку документов, multi-file resolver, OGG, web или генерацию
XLSX/DOCX/PDF. Они здесь только соседние регрессии. Эта задача заканчивается
лишь тогда, когда реальный Telegram/API путь PDF и скана доказуемо исправлен.

## 0. Закон, безопасность и отдельный worktree

1. Полностью прочитай `grok/GROK.md`, затем этот файл.
2. Работай в отдельном `git worktree`/клоне от свежего `origin/main`: основной
   worktree сейчас грязный, в нём параллельно собирается другой релиз. Не делай
   reset/checkout чужих файлов и не подмешивай незавершённый diff.
3. На момент составления задания последний выпущенный commit:
   `c51bccf0c0b63542581040f17e013ff38b66dfcc`
   (`Harden attachment truth boundaries`). Сначала установи фактические
   `origin/main`, deployed SHA и hash импортированного runtime; не предполагай,
   что systemd исполняет именно этот checkout/venv.
4. Не читай и не меняй:
   - `sol/LIVE_TEST_2026-08-08.md`;
   - `start.txt`;
   - `/home/jericho/win.txt`;
   - `/tmp/friday-key-i8oBux`;
   - live SQLite `/home/jericho/.jericho/data/state/jericho.sqlite3`.
5. Не трогай незавершённый P09:
   - `tools/synthetic_live_battery.py`;
   - `tests/test_synthetic_live_battery.py`.
6. Не выводи содержимое паспорта, ФИО, номера, даты, адреса, токены или ключи.
   Для живого аудита используй только существующий owner-authorized TLS API и
   публикуй только opaque ids, timestamps, размеры, булевы флаги, счётчики и
   хеши. Live SQLite и пользовательские файлы не открывай.
7. Все содержательные тесты — только на синтетических PDF/JPEG с искусственными
   маркерами. Никаких внешних model/network calls и записи тестовых данных в
   живой corpus.
8. Новые тесты обязательны с доказанной мутацией: тест должен краснеть на
   исходном production SHA и при удалении исправленного условия.

## 1. Уже зафиксированная безопасная фактура

Срез последних пяти полных пар был зафиксирован на cutoff
`2026-08-10T20:55:09+03:00` через owner-authenticated TLS API, без чтения live
SQLite и без вывода PII.

### PDF

- Raw id: `raw_caeecbd1bfec4dcd`.
- Принят: `2026-08-10 20:45:36 MSK`.
- MIME: `application/pdf`.
- Размер: `436488` bytes.
- `extraction_success=true`.
- `text_extraction_success=true`.
- Pages: `1/1`.
- Нет truncation/deadline loss.
- `vision=false` — для этого PDF vision не требовался, текст был извлечён.
- Два последующих ответа отказались читать/видеть PDF и предложили повторную
  загрузку.
- `citations=0`, `verified=false`, `verification_status=unknown`.

### JPEG-скан

- Raw id: `raw_18f8a0fa889447a0`.
- Принят: `2026-08-10 20:50:34 MSK`.
- MIME: `image/jpeg`.
- Размер: `770639` bytes.
- `extraction_success=false`.
- `text_extraction_success=false`.
- `vision_used=false`.
- В основном профиле нет доступного vision/OCR evidence.
- Ответ не отказался и без явной неопределённости уверенно выдал структуру
  «поле: значение» с 14 numeric tokens, похожую на паспортные реквизиты.
- `citations=0`, `verified=false`, `verification_status=unknown`.

Содержимое обоих файлов для работы не нужно и запрещено раскрывать. Эти metadata
достаточны, чтобы классифицировать PDF-ответ как ложный access refusal, а ответ по
JPEG — как unsupported confident hallucination.

## 2. Что уже пытались исправить в `c51bccf`

Не доверяй этому описанию — сверяй с кодом и production trace. Оно дано, чтобы
быстрее найти расхождение между замыслом и реально исполняемым путём.

1. Добавлен positive evidence gate: `advisory_only` или
   `verification_eligible=False` не должны входить в projection/hierarchy/map и
   не должны считаться readable.
2. Если выбранное вложение есть, но readable count равен нулю, модель и verifier
   не вызываются; публикуется code-owned UNKNOWN с явным «не буду угадывать».
3. Для полностью читаемого PDF ложный whole-file refusal должен получить ровно
   один tool-free retry с тем же authenticated evidence. Повторный отказ должен
   стать code-owned diagnostic без просьбы reupload.
4. Late file builder блокируется после attachment model failure, чтобы третий
   model call не создавал документ из неудачного ответа.
5. Для смешанного набора readable PDF + unreadable scan должен быть code-owned
   partial/UNKNOWN, а не синтез сведений о скане.
6. Синтетические focused и соседние suites были зелёными, включая current и
   restored attachment. Живой результат это опроверг.

Следовательно, главная задача — найти production path, на котором эти условия не
активируются, получают другие metadata или обходятся более поздней мутацией.

## 3. До правки: докажи, какой код реально работает

### 3.1. Deployment audit

Сними безопасные факты:

- `git rev-parse HEAD` и `git rev-parse origin/main` в deploy checkout;
- `systemctl --user show friday-backend.service friday-bridge.service`:
  `ActiveState`, `SubState`, `NRestarts`, `ExecMainStatus`;
- process start time/PID и cwd/executable без environment secrets;
- SHA-256 реально импортированного `friday/agent_runtime/__init__.py`;
- локальный TLS `GET /api/health`;
- import path/venv systemd-сервиса.

Если systemd исполняет старый checkout, старый `.pyc`, другой venv или процесс не
перезапускался — это самостоятельная корневая причина. Исправь deployment path,
но всё равно проверь кодовые рубежи ниже.

### 3.2. Новый immutable live cutoff

Владелец продолжал тестировать, поэтому прежние пары уже не «последние».
Owner-authorized API должен взять новый bounded tail и зафиксировать cutoff между
последней законченной assistant-парой и следующим user message.

Для релевантных PDF/scan пар сохрани только санитарную таблицу:

- user/assistant opaque message ids и timestamps;
- raw ids, MIME, bytes, parser/vision/advisory/verification flags;
- attachment expected/readable count и coverage complete;
- current/restored/replay route;
- `model_spoke`, tool call count, citations count, verification status;
- класс ответа: `false_pdf_refusal`, `unsupported_scan_claim`, `honest_unknown`.

Не цитируй распознанные/выдуманные поля документа.

### 3.3. Проследи production path целиком

Для PDF и JPEG отдельно пройди:

```
Telegram update / API upload
  -> bridge media classification (document vs photo)
  -> ingestion/extractor receipt
  -> persisted Raw Object descriptor
  -> server current attachment adapter
  -> conversation restore/replay adapter
  -> AgentRuntime active attachment set
  -> projection / hierarchy / prompt
  -> model answer
  -> retry / verifier / late output guards
  -> stored assistant metadata
  -> public response / idempotency cache
  -> Telegram delivery
```

Обязательные вопросы к трассировке:

1. Telegram **photo** и Telegram **document** проходят один и тот же attachment
   contract или один путь теряет `raw_object_id`, `extraction_success`,
   `verification_eligible`, `advisory_only`?
2. Ставится ли `attachment_expected_count=1` для unreadable JPEG, или descriptor
   отбрасывается раньше и runtime считает ход обычным текстом?
3. Есть ли разница между current attachment в том же update и restored attachment
   в следующем сообщении?
4. Не default-ится ли отсутствующий `verification_eligible` в `True` в одном из
   production adapters?
5. Попадает ли JPEG advisory/raw text в model messages, hierarchy/map или repair,
   хотя verifier evidence его исключает?
6. Есть ли в prompt полностью распарсенный PDF text; если нет, почему metadata
   говорит readable/complete?
7. Для ложного PDF-отказа истинны ли на последнем mutation boundary:
   - `_model_generated is True`;
   - `llm_failed is False`;
   - `attachment_coverage_complete is True`;
   - `attachment_readable_count > 0`?
8. Если detector/retry сработал, не возвращает ли поздний repair, carrier,
   structural prefix или replay старый отказ после guard?
9. Не отдаёт ли `/api/chat` или Telegram bridge ранее завершённый
   `request_idempotency.response_json`/`updates.backend_response_json` вместо
   нового runtime result? Проверь новый message id/source_ref, не ломая законную
   идемпотентность.
10. Есть ли branch, который synthetic tests monkeypatch-или и тем самым обошли
    настоящий `_prepare_context`, bridge adapter или `_generate_response`?

## 4. Обязательные красные синтетические E2E до исправления

Тестируй production functions, а не только regex/helper.

### 4.1. Полностью читаемый PDF

Создай synthetic PDF с маркером `PDF-GROUNDED-CLAUSE-417`, полный parse 1/1,
`verification_eligible=True`. Проверь оба маршрута:

- current attachment в том же ходе;
- persisted/restored attachment в следующем сообщении.

Hostile model последовательно отвечает естественными whole-file refusals:

- `Я не могу открыть PDF. Загрузите его снова.`
- `Я вижу файл, но не могу открыть его. Пришлите снова.`
- `Файл получил. Открыть его не могу.`
- `Мне недоступен загруженный PDF.`
- `Содержимое документа не отображается.`

Ожидание:

- первый отказ не выходит пользователю;
- ровно один retry, `tools=[]`;
- retry видит тот же authenticated PDF marker;
- если retry отвечает по marker — он принимается;
- если retry повторяет отказ — code-owned diagnostic, без `загрузите снова`;
- максимум два model calls;
- verifier/file builder/voice не создают третий ответ или производный файл;
- persisted metadata говорит, что ложный отказ был заменён;
- field-level uncertainty вроде `не могу разобрать одну цифру в строке` не
  классифицируется как whole-file refusal.

### 4.2. JPEG без OCR/vision

Сделай synthetic JPEG descriptor с маркером
`ADVISORY-SCAN-BODY-MUST-NOT-LEAK-288`, отдельно для:

- `extraction_success=False`;
- `verification_eligible=False` при наличии advisory text;
- `advisory_only=True`;
- оба последних флага вместе;
- Telegram `photo` adapter;
- Telegram `document` с MIME `image/jpeg`;
- current и restored route.

Hostile generator должен пытаться вернуть `MIXED-HALLUCINATION-999`.

Ожидание:

- answer generator и model verifier вообще не вызываются;
- marker отсутствует в projection, hierarchy, map, messages, response и durable
  assistant metadata;
- expected=1, readable=0, coverage/verification incomplete;
- code-owned ответ явно говорит, что содержимое не прочитано и данные не будут
  угадываться;
- verification status = UNKNOWN, verified=false, model_spoke=false;
- tools/files/voice отсутствуют.

### 4.3. Смешанный PDF + JPEG

Выбери два файла явно: readable synthetic PDF + unreadable/advisory JPEG. Hostile
model должен пытаться сочинить сведения о скане.

Ожидание:

- expected=2/readable=1;
- model не получает advisory body и не вызывается для ответа по всему набору;
- deterministic partial/UNKNOWN сообщает `1 из 2` и запрещает угадывание;
- PDF marker и JPEG marker не смешиваются;
- terminal punctuation в `Сравни report.pdf и scan.jpg.` не выбрасывает второй
  filename;
- foreign tenant/uploader scan никогда не входит в selected set.

### 4.4. Bridge и idempotency

Добавь offline bridge/backend E2E с synthetic Telegram updates:

- `sendDocument` PDF;
- `sendPhoto` JPEG;
- вопрос в caption;
- вопрос следующим update;
- повтор того же Telegram update (законный idempotent replay);
- новый вопрос после deploy с новым message id/source_ref.

Проверь exact backend payload и exact final response. Старый cached refusal может
повториться только для того же immutable request; новый вопрос обязан пройти новый
runtime. Нельзя лечить это глобальным отключением идемпотентности.

## 5. Требования к исправлению

1. Исправляй корневую передачу metadata/state или последний output boundary, а
   не только дописывай фразы в regex.
2. Source truth принадлежит parser/runtime, не модели:
   - readable PDF нельзя объявить недоступным;
   - unreadable scan нельзя превращать в источник фактов.
3. `verification_eligible=False` и `advisory_only=True` — positive admission
   boundary для **всех** synthesis paths: projection, query, hierarchy, repair,
   late file, TTS.
4. Unreadable selected attachment не должен исчезать из expected count: это один
   выбранный файл, но ноль readable evidence.
5. До появления настоящего локального OCR/vision правильный результат для JPEG —
   честный deterministic UNKNOWN. Не включай внешнее OCR и не отправляй скан
   наружу.
6. PDF retry — ровно один, tool-free и attachment-only. Повторный отказ —
   deterministic failure; никаких циклов и третьего carrier call.
7. Guard должен применяться после всех model-derived mutation paths или иметь
   последний fail-closed postcondition перед persistence/delivery.
8. Не ослабляй tenant/person authorization, public projections и secret filters.
9. Не меняй unrelated multi-file/document/generation behavior, кроме минимальных
   соседних тестов, нужных для PDF+scan selection.
10. Если корень — deployment/cached old process, исправь deployment и добавь
    автоматический post-restart SHA/import-path check, чтобы это не повторилось.

## 6. Как доказать, что это не очередной false-green

Для каждого нового regression:

1. Запусти его на исходном deployed SHA и зафиксируй ожидаемое падение.
2. После fix сломай ключевое условие мутацией и покажи повторное падение.
3. Ассерть не только ответ, но и:
   - exact raw ids/current-restored route;
   - attachment expected/readable/coverage;
   - exact messages, увиденные моделью;
   - отсутствие advisory marker;
   - model/verifier/tool/file-builder call counts;
   - stored assistant structural metadata;
   - public API и bridge response.
4. Запусти соседние frozen suites:
   - PDF/scan truth boundaries;
   - attachment security/current/restored continuity;
   - Office/full-document hierarchy (на отсутствие регрессии);
   - Telegram bridge/idempotency;
   - shared-tenant file authorization.
5. Ruff, format-check, mypy/py_compile, `git diff --check` — green.
6. Перед push — полный канонический gate:

   ```bash
   .venv/bin/python tools/quality_gate.py
   ```

## 7. Release

1. Координируйся с параллельным generated-file релизом. Перед финальным rebase
   дождись свежего `origin/main`; не перетирай его runtime diff.
2. Rebase отдельной ветки, повтори focused + полный gate на frozen bytes.
3. Получи независимый read-only review exact hashes.
4. Коммит по-русски, push `main` без force.
5. Перезапусти только:

   ```bash
   systemctl --user restart friday-backend.service friday-bridge.service
   ```

6. Проверь оба unit state/restart count/main status, локальный TLS `/api/health`,
   process start time, import path и SHA реально загруженного runtime.
7. После deploy прогони **новый** synthetic PDF и JPEG через тот же production
   API/Telegram adapter path. Не используй старый idempotency key.

## 8. Итоговый отчёт владельцу

Сначала результат, затем доказательства:

- почему `c51bccf` не изменил живое поведение;
- какой production path отличался от тестового;
- commit SHA, deployed SHA, runtime file hash/process start;
- красный baseline, mutation result, focused/full gate;
- PDF current/restored/bridge результаты;
- JPEG current/restored/photo/document результаты;
- mixed PDF+scan результат;
- подтверждение отсутствия PII/live file reads и внешнего OCR;
- подтверждение, что параллельные generated-file/P09/forbidden файлы не затронуты.

Не пиши «должно работать». Работа закончена только после доказанного нового
production run на новом request id, где readable PDF не получает ложный отказ, а
JPEG без evidence не получает ни одного выдуманного поля.
