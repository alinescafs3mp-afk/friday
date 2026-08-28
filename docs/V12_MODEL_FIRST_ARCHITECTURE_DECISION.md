# V12: model-first архитектура Friday — принятое направление

Статус: **архитектурное направление принято владельцем; release 0.205.0
сохраняет безопасный shadow и узкие opt-in FILE_READ/ARCHIVE_READ routes,
добавляя exact Qwen3.8/SGLang V12.14 attestation и owner-visible degradation;
production по умолчанию остаётся `legacy`**.

Дата решения: 2026-08-17.

Исходные материалы:

- `/home/jericho/new.txt`, SHA-256
  `8d13716546b9f6d860a49aaec8e10cf16361ba439fa6ed231c60d7bda289866b`;
- внешний материал DeepSeek `V12_GAP_ANALYSIS_V2.md` (в рабочее дерево не
  включён), SHA-256
  `507a6121fc8e93b9606572f8c790834fe759aac8afe3d832f90968ffa463e920`;
- исходный план параллельного роутера архивирован в Git;
  актуальный порядок работ ведёт
  [`outer_sol/PROJECT_BACKLOG.md`](../outer_sol/PROJECT_BACKLOG.md).

## 1. Продуктовый замысел

V12 — переключаемый контур Friday для сильных моделей от 120B и выше. V11
остаётся рабочим контуром для малых моделей, аварийным откатом и источником
проверенных компонентов.

В V12 модель должна владеть смысловой частью работы:

- понимать задачу человека;
- решать, какие данные и инструменты нужны;
- строить план;
- выбирать релевантные источники;
- формировать теги, связи, сводки и итоговый ответ;
- запрашивать дополнительные проверки или более сильную модель.

Система остаётся её руками, глазами, памятью и исполнительным механизмом.

## 2. Главный архитектурный принцип

> Модель решает, **что делать и зачем**. Система гарантирует, **кому это
> разрешено, откуда взяты данные, что именно было исполнено и что человек
> получил ровно один раз**.

Model-first не означает model-unchecked. Размер модели не превращает её в
субъект авторизации и не защищает от prompt injection, повреждённых файлов,
сетевых зависаний, ложной уверенности или повторного исполнения эффекта.

## 3. Что принадлежит модели

- Семантическая классификация запроса и файлов.
- Выбор необходимых источников и инструментов.
- Планирование чтения, сравнения, исследования и создания материалов.
- Синтез ответа по собранному evidence bundle.
- Предложение тегов, важности, связей и статуса ingestion.
- Запрос эскалации на другой model tier.
- Запрос самопроверки, перекрёстной проверки и уточнения.

Для V12 предпочтительны явные типизированные tool calls и `TurnPlan`, а не
разрастающийся набор regex-классификаторов.

## 4. Что навсегда принадлежит системе

Эти границы едины для V11 и V12 и не обходятся моделью:

- actor/tenant/person/uploader authorization и приватность;
- неизменяемая идентичность исходных файлов и provenance;
- SQLite, Raw Object, Inbox, Knowledge Object и conversation lineage;
- безопасный парсинг, OCR, лимиты архивов и защита от parser bombs;
- SSRF, sandbox, cookie isolation и защита секретов;
- типизированные схемы аргументов и результатов инструментов;
- абсолютные deadlines, отмена, backpressure и resource budgets;
- idempotency/effect fence, подтверждение опасных действий и аудит;
- финальная reauthorization перед чтением, записью и публикацией;
- обязательная проверка citation/provenance для утверждений по источникам;
- ровно один внешний или durable эффект и ровно одна пользовательская выдача.

Модель может видеть каталог возможностей и просить инструмент, но execution
kernel исполняет его только в рамках прав текущего человека и текущего scope.

## 5. Решения по предложениям DeepSeek

### Принимаются

- Переключаемый V11/V12.
- Сильная модель как planner и synthesizer.
- Explicit tool calling вместо угадывания модельного намерения regex-ами.
- Общие retrieval, graph, documents, Telegram, storage и execution kernel.
- Headless-browser как fallback для JS-страниц.
- Теги Raw Objects, файловые связи и версионирование.
- Расширение офисной генерации и конвертации.
- Инструменты самопроверки, доступные модели.
- Shadow и canary до полного включения.

### Отклоняются или изменяются

- **Не выдавать все права любой 120B-модели.** Права принадлежат actor, а не
  параметрам модели.
- **Не убирать timeouts и result validation.** Модель получает структурированную
  ошибку и может перепланировать, но зависший процесс останавливает система.
- **Не убирать финальную citation/provenance verification.** Самопроверка модели
  добавляется, а не заменяет детерминированную границу.
- **Не промоутить знания только по model confidence.** Confidence не является
  авторизацией и может быть вызвана инструкцией внутри недоверенного документа.
- **Не публиковать chain-of-thought.** Хранится action/evidence trace: выбранные
  источники, вызванные tools, эффекты и основания результата.
- **Не давать модели окончательную власть над model routing.** Она запрашивает
  tier, scheduler применяет budget, availability и policy.
- **Не ставить целью обход антибот-защиты любой ценой.** Browser fallback должен
  работать в допустимых границах, соблюдать rate limits и не обходить
  аутентификацию без явной пользовательской сессии.
- **Не копировать весь runtime в `friday/v12/`.** Новый orchestrator использует
  общий data/effect plane, иначе V11 и V12 быстро разойдутся по безопасности и
  исправлениям.

## 6. Что уже есть и должно переиспользоваться

DeepSeek завысил объём отсутствующей функциональности. В V11 уже есть:

- `make_file`: DOCX, XLSX, PDF и PNG;
- `collect_files`: сборка исходных файлов в ZIP;
- извлечение PDF, Office, архивов, изображений и OCR;
- Knowledge Object tags и предлагаемые Inbox tags;
- граф, retrieval, reranker и source search;
- provider chain: Yandex, Brave, Tavily, Serper, HTML fallbacks, Wikipedia;
- actor-aware capability checks, HITL для опасных действий и effect audit;
- явное сохранение текста без обязательного review при прямом намерении человека.

Реальные функциональные gaps:

- единые теги непосредственно для Raw Objects с provenance тегировщика;
- typed file relationships и версии файлов;
- production browser service;
- PPTX generation, конвертация и шаблоны;
- мультимодельный scheduler/router;
- компактный model-first orchestrator;
- быстрый каталог зарегистрированных файлов по датам, людям, типам и тегам;
- единый долгоживущий evidence cache, чтобы не повторять OCR и разбор файла.

## 7. Целевой поток одного хода

```text
Telegram update
  -> неизменяемая регистрация сообщения и файлов
  -> авторизованный TurnInput + FileEvidenceSet
  -> V12-модель возвращает типизированный TurnPlan
  -> retrieval/parser/browser/tools собирают EvidenceBundle
  -> execution kernel авторизует и исполняет EffectPlan
  -> модель выполняет один итоговый synthesis
  -> citation/provenance/coverage/effect verification
  -> transport-neutral rendering
  -> ровно одна публикация в Telegram
```

Файлы регистрируются до модельной работы. Без подписи выполняется подробная
сводка и тегирование. С подписью выполняется именно инструкция человека, но сам
файл всё равно сохраняется и остаётся доступен через opaque file id. Модель не
получает реальные приватные пути.

## 8. Переключение и миграция

Базовый контракт:

```text
FRIDAY_ROUTER_MODE=legacy|shadow|canary|v12
```

- `legacy`: только текущий runtime;
- `shadow`: V12 планирует, но не публикует и не исполняет mutating/high effects;
- `canary`: V12 включён только для разрешённых пользователей и route classes;
- `v12`: V12 владеет только явно разрешёнными route classes, остальные ходы
  остаются у legacy; allowlist сохраняет мгновенный rollback.

Один ход имеет ровно одного владельца эффектов. История, SQLite и форматы файлов
общие, поэтому rollback не требует миграции данных.

Рекомендуемая структура — не копия Friday, а стратегии над общими контрактами:

```text
friday/orchestration/contracts.py
friday/orchestration/router.py
friday/orchestration/planner.py
friday/orchestration/file_read.py
friday/orchestration/archive_read.py
friday/orchestration/file_read_contract.py
friday/model_profiles.py
friday/model_probe.py
friday/v12_model_runtime.py
```

## 9. Model profile вместо доверия по числу параметров

Режим V12 нельзя включать только по названию или размеру модели. Для каждого
endpoint фиксируется измеренный профиль:

- точность native tool calls и JSON schema;
- качество русского языка и следования инструкции;
- допустимый контекст и exact tokenizer;
- vision/file capabilities;
- устойчивость многошагового планирования;
- latency, throughput и cancellation semantics;
- уровень автономии и максимальное число tool steps;
- разрешённые категории effects;
- необходимость обязательного verifier pass.

Модель может предложить `switch_model(tier=...)`, но scheduler выбирает endpoint
из разрешённого профиля и не допускает циклов, превышения бюджета или тихого
изменения effect owner.

Release 0.204.0 регистрирует code-owned handlers только после live-аттестации
профиля `qwen36-27b-nvfp4-nvidia:dispatcher:v12.13`. `file_read` принимает 1–2
файла текущего хода только при доказанно полном UTF-8 представлении.
`archive_read` принимает только ранее зарегистрированные полные UTF-8 файлы
самого actor: один по уникальному точному имени, ровно 1–2 последних либо не
более двух за точный локальный день «сегодня / вчера / позавчера».

Модель не выбирает uploader, Raw id или временные границы. Неоднозначность,
другой пользователь, reply/replay, выбор более двух файлов, PDF/OCR и partial
extraction остаются у legacy. Авторизация завершается до чтения body, а точный
selector и каждый источник повторно авторизуются перед публикацией. Evidence,
conn-scoped idempotency fence и единственная публикация используют одну
атомарную SQLite commit boundary. Это узкий canary, а не заявление о полной
функциональной паритетности V12; schema остаётся 33.

Release 0.205.0 добавляет отдельный exact profile
`qwen38-27b-nvfp4-sglang:dispatcher:v12.14`. Он не наследует доверие
по имени `dispatcher`: model revision, SGLang build, graph-only launch,
per-process deployment witness и свежий behavioral probe проверяются
независимо. Authority остаётся той же: 1–2 prepared evidence,
8192-token V12 context, ноль model-owned tool steps, read-only effect и
обязательный verifier. Qwen3.6/V12.13 profile остаётся зарегистрированным
для точной совместимости, а не как неявный fallback нового profile.

## 10. Работа V12 на текущей 27B-модели

Текущий аттестованный graph-only profile для release 0.205.0:

```text
qwen38-27b-nvfp4-sglang
served model alias: dispatcher
model: a2genesis/Qwen3.8-27B-NVFP4@bfd9b31207712e0850eec9da32261e8c5ee16af7
runtime: lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124
runtime source: c4271c3fe1262fc2adbd162c33b25de5255251c5
reported version: 0.0.0.dev0+qwen38.27b.g561c8f3
launch: context/total=40960, running/mamba=6, FP8 E4M3 KV
graphs: decode=full batches 1..6, prefill=disabled; radix/speculation=disabled
```

V12 должна запускаться на ней сначала в `shadow`, затем в
ограниченном `canary`. Это полезно по двум причинам:

1. архитектура остаётся model-agnostic и не строится под воображаемый API
   будущей 120B-модели;
2. если инструмент понятен только 550B-модели, его интерфейс, скорее всего,
   недостаточно ясен и типизирован.

### Что на текущей модели должно работать хорошо

- один read-only tool call;
- поиск зарегистрированного файла;
- чтение, сводка и вопрос по одному или нескольким заранее подготовленным
  EvidenceBundle;
- создание DOCX/XLSX/PDF по готовой структуре;
- обычный web search и синтез по ограниченному числу источников;
- тегирование с закрытым словарём и provenance;
- короткие планы с одним-двумя последовательными шагами.

Новый контур может оказаться быстрее V11 даже на 27B, если он использует один
план, один переиспользуемый EvidenceBundle и один финальный synthesis вместо
повторного MAP/OCR/retrieval.

### Где текущая модель будет заметно слабее 120B+

- длинное автономное планирование с множеством инструментов;
- выбор из десятков полных tool schemas одновременно;
- разрешение неоднозначных инструкций без уточнения;
- сложное исследование на нескольких языках;
- самопроверка собственных ошибок без независимого verifier;
- уверенное принятие необратимых или высокорисковых решений;
- очень длинные документы, если доказательства не подготовлены системой.

### Безопасный профиль текущей модели

Для 27B рекомендуются:

- полный каталог названий возможностей, но динамическая загрузка подробных схем
  только для релевантной группы tools;
- максимум 2 последовательных model-owned tool steps до перепланирования;
- read-only маршруты первыми;
- обязательный verifier для web/file claims;
- запрет автономных `high` effects;
- model proposal + kernel/HITL для mutating effects;
- legacy fallback при malformed/неполном TurnPlan;
- жёсткий общий deadline, reuse parser/OCR cache и не более одного итогового
  synthesis.

При таких границах Friday будет функционально полезной на текущей модели, но не
получит полный уровень автономии, предназначенный для измеренно надёжной 120B+.
Это правильная деградация: меняется глубина автономии, а не безопасность и не
доступность данных.

## 11. Производительность

Model-first не означает model-everywhere. Модель 120B–550B обычно дороже и
медленнее, поэтому архитектура обязана сокращать, а не увеличивать число
генераций.

Обязательные свойства:

- parse/OCR один раз на content hash;
- структурный индекс и теги кешируются;
- десятки файлов проходят детерминированный scan/aggregate до synthesis;
- модель получает manifest и релевантные evidence pages, а не все бинарные
  представления повторно;
- тяжёлая модель используется для планирования и итогового смысла;
- лёгкие модели могут выполнять изолированные bounded subtasks;
- Telegram быстро подтверждает приём длительной работы и публикует один
  согласованный итог;
- timeout одного документа не открывает глобальный cooldown и не отравляет
  следующие ходы;
- любые продолжения привязаны к durable job id и не повторяют эффект.

## 12. Файловый контур V12

Минимальный единый tool surface:

- `file_catalog_search(filters)`;
- `file_open(file_id, projection)`;
- `file_compare(file_ids, question)`;
- `file_tag(file_id, tags, provenance)`;
- `file_relate(parent_id, child_id, relation_type)`;
- `make_file(format, spec, sources)`;
- `convert_file(file_id, format, options)`;
- `collect_files(file_ids | filters)`.

Фильтры каталога: дата/период, uploader/person, имя, MIME/format, tag,
conversation lineage и semantic query. Результаты всегда actor-scoped.

## 13. Web-контур V12

Поток:

```text
query decomposition
  -> multilingual provider search
  -> direct fetch
  -> sandboxed browser fallback при необходимости JS
  -> extraction + deduplication
  -> cross-source EvidenceBundle
  -> synthesis с citations и общей сводкой
```

Browser — отдельный ограниченный процесс без ambient credentials и proxy, с
новым контекстом на задачу, лимитами переходов/байтов/времени, SSRF-проверкой
каждого redirect и download только в приватный sandbox.

## 14. Тестирование без повторения всей эпопеи V11

- Shared data/effect plane сохраняет существующие доказанные тесты.
- Новый orchestrator тестируется против frozen typed contracts и fake tools.
- Архивный selector тестируется как закрытая self-only grammar: exact filename,
  exact latest 1–2 и today/yesterday/pozavchera; все неоднозначные и широкие
  формы доказывают legacy fallback до чтения body.
- Финальная exact-selector reauthorization и conn-scoped idempotency fence
  проверяются mutation/rollback/replay тестами на общей commit boundary.
- `shadow` сравнивает route, tool plan и evidence coverage без второго эффекта.
- Быстрый affected gate запускается на каждой итерации.
- Полный offline gate запускается перед canary/release, а не после каждой строки.
- Canary включается route-by-route и имеет мгновенный возврат на `legacy`.
- Реальные live batteries проверяют только границы, которые нельзя доказать
  offline: endpoint, tool-call protocol, Telegram, browser и model behaviour.

## 15. Реалистичные сроки

Для одного разработчика при доступном стабильном model endpoint:

- архитектурная спецификация и frozen contracts: 1–2 дня;
- switch/shadow skeleton: 3–5 дней;
- первый полезный file-first canary: ещё 5–8 дней;
- ingestion/effects/browser/office extensions: ещё 1–2 недели;
- широкая production-паритетность и hardening: суммарно около 3–5 недель;
- редкие legacy-контуры и расширенный браузер: до 5–7 недель.

Параметры будущей 120B/550B-модели, её inference server, quantization, context и
tool-call protocol должны быть измерены отдельно. Они не должны требовать
переписывания V12.

## 16. Первый milestone

Первый V12 milestone считается готовым, когда:

1. `legacy|shadow|canary|v12` переключаются без миграции БД;
2. V12 принимает Telegram-файл и регистрирует его до model work;
3. без подписи создаёт подробную сводку и теги;
4. с подписью исполняет именно инструкцию пользователя;
5. находит только собственные ранее зарегистрированные exact UTF-8 файлы по
   уникальному точному имени, exact latest 1–2 или today/yesterday/pozavchera;
6. отвечает по нескольким файлам из одного EvidenceBundle;
7. не исполняет второй эффект в shadow/retry;
8. текущая 27B с профилем `v12.14` проходит `file_read`/`archive_read` canary,
   а ошибки плана и неподдерживаемые selector-ы уходят в legacy;
9. rollback занимает одно изменение режима;
10. все публикации сохраняют provenance и actor boundaries.
