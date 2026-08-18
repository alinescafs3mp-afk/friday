# План параллельного нового роутера для следующей 27B-модели

Статус: **исторический план заменён решением
`docs/V12_MODEL_FIRST_ARCHITECTURE_DECISION.md`; phase-1 shadow и ограниченный
FILE_READ canary реализованы, а этот файл сохраняет исходные ограничения
миграции и следующие этапы**.

Начинать новый роутер, переключатель или model-specific интеграцию можно только
после отдельного явного сигнала владельца. Текущий ремонт файлового контура не
должен ждать этого проекта или незаметно превращаться в него.

Название будущей модели в разговоре — Qwen 3.8 27B. До публикации официальных
весов, model card, chat template и независимых тестов это рабочее обозначение,
а не подтверждённый runtime-контракт. Переход начинается только после появления
пригодного квантования и замеров на фактическом железе Пятницы.

## Цель

Рядом должны сосуществовать две внутренние логики:

- `legacy` — текущая проверенная логика и мгновенный аварийный откат;
- `v12` — существенно меньший, типизированный и model-aware оркестратор;
- `shadow` — безопасное сравнение решения нового контура с legacy без второй
  пользовательской выдачи и без повторного внешнего или необратимого эффекта;
- `canary` — ограниченное включение `v12` по разрешённым маршрутам/пользователям.

Переход не является переписыванием хранилища или безопасности. Новый роутер
переиспользует проверенные органы и меняет только оркестрацию.

## Неподвижный общий фундамент

Эти части остаются единственными и общими для `legacy` и `v12`; копировать или
реализовывать их второй раз запрещено:

- авторизация, tenant/person/uploader boundaries и privacy lifecycle;
- SQLite storage, Raw/Inbox, immutable file bytes и conversation lineage;
- idempotency/effect fence и execution kernel;
- ToolSpec, risk classification и audited tool execution;
- ingestion, parser/OCR/Whisper и file-delivery;
- публичная projection/redaction, секреты и Telegram transport;
- абсолютный deadline и правила «не начинать новый эффект после дедлайна».

## Блоки, которые готовятся к повторному использованию уже сейчас

Подготовка допустима только внутри доказанного текущего исправления и только
если legacy-поведение до/после закреплено тестами. Большой механический
рефакторинг ради будущей модели до сигнала запрещён.

По мере ремонта логика оформляется небольшими model-independent блоками:

1. Нормализованный `TurnInput` и закрытая authority-классификация.
2. Авторизованный `FileEvidenceSet` с неизменяемой идентичностью источников:
   excerpt и parser output привязаны к process-private снимку той же строки и
   тех же байтов, а не к повторному чтению «похожего» актуального объекта.
3. Детерминированный план чтения: direct/full-fit/query projection/hierarchy.
   Полная hierarchy может заменить метрику обрезанной prompt-проекции только
   при совпавших source identity, cardinality и финальной reauthorization.
4. `EvidenceBundle`, одинаковый для synthesis, verifier и output carriers.
5. Политика модельных стадий: zero/one/two-pass и причины обязательной проверки.
   Direct full-document admission доказывается точным tokenizer конкретного
   model profile; эвристика «символов на токен» не является границей
   безопасности и не может сама отключать hierarchy.
6. Отдельный effect plan: read-only до mutating/high, без повторного исполнения.
7. Финальная reauthorization и output projection перед сохранением/выдачей.
   Один publication contract охватывает текст, производный файл и голосовой
   carrier; поздний TTS не может жить за пределами снимка, защитившего текст.
8. Transport-neutral Markdown/Telegram rendering contract.

Legacy сначала вызывает эти блоки как адаптер. Новый `v12` забирает их целиком,
не копируя ветви огромного `AgentRuntime`.

## Предполагаемый контракт переключения

После сигнала и отдельного implementation review:

```text
FRIDAY_ROUTER_MODE=legacy|shadow|canary|v12
```

Режим должен дополнительно поддерживать маршрутный allowlist. Это позволит,
например, сначала включить `v12` для small-talk и локального файлового чтения,
оставив редкие эффекты на `legacy`.

Требования:

- один запрос выбирает ровно один effect-owning runtime;
- `shadow` не исполняет mutating/high tools, не пишет второй ответ и не создаёт
  второй файл/напоминание/уведомление;
- сравнение сохраняет только технические счётчики, причины маршрута и хэши, без
  приватных prompt/answer/file bodies;
- откат на `legacy` не требует миграции БД, рестарта model dispatcher или
  преобразования conversation history;
- неизвестный/невалидный режим fail-closed выбирает `legacy`, а не `v12`.

## Что нужно узнать после выхода модели

До написания model-specific adapter необходимо проверить на реальных весах:

- официальный chat template и special tokens;
- tool-call JSON/streaming contract и поведение при malformed tool output;
- thinking/non-thinking режимы и сохранение reasoning state между tool calls;
- настоящий context limit при выбранном квантовании;
- exact tokenizer/chat-template counter для admission direct file prompts;
- качество русского языка, файлового анализа и следования закрытым отказам;
- latency/throughput при канонической конкуренции и длинных документах;
- cancellation/abort-on-disconnect на фактическом inference server;
- FP8/AWQ/GPTQ/GGUF (что реально доступно) и потеря качества относительно
  исходных весов;
- независимые тесты и собственная blinded differential battery.

Не следует подгонять архитектуру под слухи о model API до этих измерений.

## Этапы после отдельного сигнала владельца

1. **Model bring-up (около 1 дня):** immutable profile, quantization comparison,
   chat/tool/reasoning probes, memory and concurrency limits.
2. **Каркас (2–3 дня):** typed contracts, `legacy|shadow|canary|v12`, telemetry,
   fail-closed dispatch и rollback.
3. **Основные маршруты (4–7 дней):** small-talk, ordinary dialogue, local files,
   archive/web read routes, затем effects.
4. **Дифференциальная проверка (3–5 дней):** frozen offline suites, shadow
   comparison, deletion/privacy/idempotency mutations, live batteries.
5. **Canary и выпуск:** route-by-route promotion; любой RED возвращает маршрут на
   `legacy`, исправление получает новый immutable SHA и полный повтор доказательств.

Оценка: первый содержательно рабочий `v12` — 4–6 дней после доступности модели;
production-ready — 10–15 рабочих дней; полная паритетность редких legacy-контуров
может потребовать 3–4 недели.

## Критерии готовности `v12`

- Нет ухудшения file/privacy/deletion/idempotency contracts относительно legacy.
- Простые ходы не запускают retrieval/model без необходимости.
- Обычный полный файловый обзор не содержит quicklook и не делает лишний MAP.
- Один evidence bundle используется всеми модельными стадиями и carriers.
- Число модельных стадий ограничено и наблюдаемо для каждого route class.
- Shadow не создаёт внешних или durable эффектов.
- Canary имеет технический и операторский мгновенный rollback.
- Один и тот же immutable SHA проходит offline, synthetic/live и document contour.
- После deploy backend, bridge, MCP, queues и TLS аттестованы на том же SHA.

## Текущая директива

После полученного сигнала владельца:

- создавать V12 рядом с legacy, не копируя data/effect plane;
- добавлять переключатель только с безопасным `legacy`-умолчанием;
- не менять production model profile ради будущей модели;
- сначала доказывать typed contracts и shadow, затем включать read-only canary;
- сохранять legacy как мгновенный откат и единственного владельца эффекта до
  явного route promotion;
- обновлять этот документ, если во время текущей работы найден новый общий
  архитектурный инвариант.
