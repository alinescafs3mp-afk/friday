# Friday: где применять MCP и где сохранять нативное ядро

> Статус: внешнее архитектурное наблюдение  
> Срез репозитория: `main`, Friday `0.205.0`, 20 августа 2026 года  
> Цель: уменьшить объём собственной разработки, не размывая local-first, provenance, безопасность, tenant isolation и индивидуальность Friday.

## Исходное наблюдение

Friday уже не нужно переделывать под MCP с нуля. В проекте существуют:

- собственный MCP runtime в [`friday/mcp_runtime/`](../friday/mcp_runtime/);
- постоянные stdio-соединения с ограниченными таймаутами;
- code-owned определения серверов;
- жёсткие allowlist инструментов;
- проверка наличия обязательных инструментов при старте;
- требование structured result;
- отдельный безопасный workspace MCP server;
- зависимость `mcp>=2,<3` в [`pyproject.toml`](../pyproject.toml).

Важнейшее уже принятое архитектурное решение видно в [`friday/mcp_runtime/client.py`](../friday/mcp_runtime/client.py) и [`friday/mcp_runtime/tools.py`](../friday/mcp_runtime/tools.py): удалённые описания, schemas и annotations не публикуются модели напрямую. Friday выставляет свои узкие инструменты и сама владеет их контрактами.

Это правильная основа. Её следует расширять, а не заменять универсальным динамическим MCP-клиентом.

## Главный вывод

**Не заменять MCP-серверами ядро Friday. Заменять ими края системы.**

Friday должна владеть:

- памятью и смыслом;
- provenance и evidence;
- правами и приватностью;
- маршрутизацией и бюджетами;
- approvals и idempotency;
- проверкой результатов;
- долговременным состоянием пользователя.

MCP разумно поручить чужую API-инфраструктуру:

- OAuth;
- пагинацию;
- изменения внешних API;
- SaaS-коннекторы;
- браузерные сессии;
- облачные хранилища;
- внешние системы разработки и наблюдения;
- новые виды внешних баз данных.

Краткая формула:

> Мозг, память и правила принадлежат Friday. Чужие дверные ручки подключаются через MCP.

## Рекомендуемый разрез

| Подсистема Friday | Решение | Практическое действие |
|---|---|---|
| GitHub, Jira, Linear, Notion, Drive, Slack, почта, календари, CRM | MCP-first | Не писать полные нативные клиенты без доказанной необходимости |
| Провайдеры интернет-поиска | MCP primary + нативный fallback | Вынести provider-specific API, сохранить свою приватность, provenance и обработку источников |
| Полноценный браузер и JS-сайты | MCP | Подключить браузерный MCP только для страниц, где обычного HTTP fetch недостаточно |
| Внешние СУБД | Гибрид | SQLite/Postgres/MySQL оставить; новые движки подключать через MCP |
| Локальный workspace | Оставить как есть | Собственный узкий MCP server безопаснее универсального filesystem server |
| Telegram bridge | Нативно | Это frontend transport, очередь доставки и UX, а не простой tool connector |
| Локальный разбор файлов, OCR, DOCX/XLSX/PDF | Нативно | MCP применять для импорта из облака и публикации результатов |
| Whisper, Piper и локальные модели | Нативно | Они поддерживают local-first и контролируемую приватность |
| Memory, retrieval, graph, ingestion, provenance | Только нативно | Это продуктовая сущность Friday |
| Permissions, approvals, idempotency, tenant isolation | Только нативно | Никогда не делегировать внешнему MCP server |
| Sentry, Grafana, CI и внешняя диагностика | MCP-first | Sentinel остаётся своим, внешние сигналы поступают через адаптеры |
| LLM provider/runtime layer | Нативно | Здесь находятся attestation, model profiles, budgets и fail-closed маршрутизация |

## Первый крупный кандидат: `web_surfer`

Самая заметная область, где Friday сейчас обслуживает много чужой инфраструктуры, находится в [`friday/web_surfer/`](../friday/web_surfer/).

Этот контур самостоятельно решает:

- DNS и SSRF-защиту;
- защиту от DNS rebinding;
- redirect policy;
- `robots.txt`;
- provider-specific поисковые запросы;
- антибот-ответы и отказ провайдера;
- фильтры свежести, языка, страны и доменов;
- глобальные и поэтапные таймауты;
- лимиты тела ответа;
- HTML parsing;
- PDF extraction;
- классификацию полных и неполных результатов;
- fallback между провайдерами.

Это полезная и качественная работа, но значительная её часть не создаёт уникальность Friday. Особенно показателен зафиксированный в коде случай, когда DuckDuckGo отдавал HTTP 202 со страницей-заглушкой и выглядел как честный пустой результат. Подобные provider quirks способны бесконечно поедать время.

### Что оставить внутри Friday

- решение, разрешено ли отправлять конкретный запрос наружу;
- защиту от утечки имён, внутренних документов и личных данных;
- предотвращение отправки приватной сущности во внешний поиск;
- лимиты числа источников и размера текста;
- нормализацию результата;
- provenance и citations;
- проверку evidence;
- классификацию полного, частичного и непроверяемого ответа;
- простой безопасный HTTP fetch публичной статической страницы;
- собственный bounded research workflow.

### Что передать MCP

- обращения к конкретным search API;
- provider authentication;
- пагинацию;
- provider rate limits;
- retry policy конкретного API;
- постоянную браузерную сессию;
- JavaScript rendering;
- авторизованные сайты;
- клики, формы и навигацию;
- работу с cookie и session state.

### Предлагаемый маршрут

```text
Обычный поиск
    -> search MCP
    -> Friday валидирует и нормализует выдачу
    -> native safe fetch статических страниц
    -> собственные provenance, evidence и citations

Сложная JS-страница
    -> browser MCP
    -> Friday получает bounded structured result
    -> проверяет источник, полноту и ограничения
    -> допускает результат в synthesis
```

Для браузерного слоя разумным кандидатом является официальный Playwright MCP. Его не следует запускать на каждую обычную статью. Иначе вместо упрощения получится тяжёлый браузерный процесс, который приносит одну страницу вместе с большим accessibility tree.

## Внешние базы данных: сохранить текущий native fallback

[`friday/data_sources.py`](../friday/data_sources.py) уже представляет внешнюю базу как источник, а не как внутреннее хранилище Friday.

Полезные свойства текущей реализации:

- DSN не хранится в базе Friday;
- хранится только имя переменной окружения;
- запрос ограничен чтением;
- разрешён один `SELECT` или `WITH ... SELECT`;
- SQLite открывается в `mode=ro`;
- Postgres получает read-only connection;
- есть row limit;
- есть timeout;
- обрез результата обозначается явно;
- имеются schema-description операции;
- поддерживаются SQLite, Postgres и MySQL.

Этот код сравнительно узкий, уже написан и является хорошим fallback. Выбрасывать его нет смысла.

Рекомендуемое новое правило:

> После SQLite, Postgres и MySQL не добавлять в core новые нативные драйверы без веской причины.

Snowflake, Oracle, ClickHouse, Microsoft SQL Server, BigQuery, MongoDB и корпоративные специфические источники лучше подключать через MCP.

Friday всё равно должна сохранить собственную оболочку:

```text
Model
    -> friday_query_external_source
    -> actor / tenant / permissions
    -> read-only policy
    -> row and time budgets
    -> MCP database server
    -> structured result validation
    -> provenance and audit
```

Не следует публиковать модели сырой `execute_sql` от случайного MCP server. Предпочтительнее стабильные code-owned инструменты Friday:

```text
external_source_list
external_source_describe
external_source_query
external_source_sample
```

Так модель видит единый контракт Friday, а нижний транспорт можно менять независимо.

## Workspace MCP уже сделан почти образцово

Текущий [`friday/mcp_runtime/workspace_fs.py`](../friday/mcp_runtime/workspace_fs.py) не следует заменять универсальным filesystem MCP server.

Его поверхность намеренно узкая:

- inbox предназначен для чтения;
- outbox допускает только создание новых безопасных текстовых файлов;
- нет overwrite;
- нет append;
- нет rename;
- нет move;
- нет delete;
- нет shell;
- нет SQLite access;
- нет network access;
- пути проверяются;
- symlink traversal блокируется;
- выбранный файл повторно открывается и сверяется;
- проверяются descriptor identity, inode, device, размер и временные поля;
- результат ограничивается бюджетом;
- extracted source получает digest и признаки полноты.

Большинство универсальных filesystem MCP servers имеют более широкую поверхность. Замена здесь означала бы не упрощение, а отказ от уже построенной защитной границы.

Этот workspace server стоит оставить эталоном для будущих интеграций:

> Узкий MCP transport снаружи, полная policy и повторная валидация внутри Friday.

## Telegram bridge оставить нативным

[`friday/telegram_bridge/`](../friday/telegram_bridge/) выполняет существенно больше, чем `send_message`:

- принимает updates;
- обрабатывает команды;
- ведёт callback lifecycle;
- формирует markup;
- принимает и отправляет media;
- управляет очередью;
- обрабатывает retries;
- связывает frontend события с backend operations;
- доставляет proactive notifications;
- обеспечивает deny-by-default chat allowlist;
- участвует в idempotency и durable delivery.

MCP хорошо подходит для действий вида:

```text
send_slack_message
create_calendar_event
create_github_issue
upload_to_drive
```

Но MCP плохо заменяет frontend transport вида:

```text
receive Telegram updates
maintain callback lifecycle
render buttons
process commands
operate a durable delivery queue
bind update identity to idempotency fences
```

Поэтому:

- Telegram остаётся native frontend;
- дополнительные исходящие каналы можно подключать через MCP;
- полноценные входящие интерфейсы Slack, Discord или Matrix лучше оформлять как отдельные transport adapters, а не как tools модели.

## Файлы, OCR, voice и локальные модели оставить своими

Нативные DOCX/XLSX/PDF, OCR, Whisper и Piper поддерживают ключевые свойства Friday:

- local-first;
- воспроизводимое извлечение;
- собственные признаки полноты;
- собственные evidence spans;
- controlled privacy;
- review-gated ingestion;
- fail-closed семантику.

У Friday существуют важные понятия, которых обычный внешний file tool часто не предоставляет:

```text
source_complete
verification_eligible
advisory_only
parse_deadline_reached
parse_pages_truncated
archive_truncated
source_truncated_for_parse
```

Если отдать parsing pipeline внешнему серверу, он может вернуть просто строку текста и потерять семантику достоверности.

MCP здесь полезен вокруг native processing:

```text
Drive MCP импортирует файл
    -> Friday нативно сохраняет original bytes
    -> Friday нативно извлекает текст и evidence
    -> ingestion/review остаются своими

Friday нативно создаёт DOCX
    -> Drive MCP публикует документ

Friday нативно строит таблицу
    -> Sheets MCP создаёт облачную копию
```

[`friday/whisper.py`](../friday/whisper.py), [`friday/tts.py`](../friday/tts.py), [`friday/ingestion/`](../friday/ingestion/) и [`friday/generated_files.py`](../friday/generated_files.py) лучше оставить внутри продукта.

## Что точно нельзя заменять MCP

Следующие подсистемы составляют ДНК Friday:

- `ingestion`;
- `retrieval`;
- `knowledge_graph`;
- `memory`;
- `storage`;
- `permissions`;
- `execution_kernel`;
- `orchestration`;
- `evidence_bundle`;
- `citation_check`;
- `secret_hygiene`;
- `audit_privacy`;
- `source_identity`;
- idempotency fences;
- approvals;
- model attestation;
- V12 runtime;
- review queues;
- transaction-time и valid-time semantics.

MCP стандартизирует доступ к tools, resources и prompts. Он не предоставляет готовую модель:

- персональной памяти;
- provenance;
- tenant isolation;
- graph identity;
- review workflow;
- temporal semantics;
- доказательности ответа;
- безопасного повторения побочных эффектов.

MCP может принести Friday документ. Решать, что документ означает, кому принадлежит и можно ли на него опереться, должна сама Friday.

## Новые SaaS-коннекторы: MCP-first по умолчанию

Для будущих внешних интеграций стоит принять правило:

```text
Внешний API не принадлежит Friday
    -> сначала ищется официальный или vendor-maintained MCP server
    -> затем пишется узкий Friday adapter
    -> полноценный native connector создаётся только при доказанной необходимости
```

Приоритет доверия:

1. официальный сервер самого сервиса;
2. vendor-maintained сервер;
3. хорошо проверенный open-source сервер;
4. собственный узкий server;
5. случайный community server только после отдельного security review.

MCP Registry является каталогом, но не знаком качества и не автоматическим доверием. Remote schemas, descriptions и annotations нужно считать недоверенными данными.

### Пример: GitHub

Официальный GitHub MCP server уже покрывает repositories, issues, pull requests, Actions и другие GitHub API.

Начальная интеграция должна быть минимальной:

```text
режим: read-only
toolsets: repositories, issues, pull requests
```

Friday не обязана выставлять модели десятки сырых GitHub tools. Достаточно нескольких собственных стабильных контрактов:

```text
github_read_file
github_search_code
github_read_issue
github_list_pull_requests
```

Позднее mutating tools можно подключить отдельно:

```text
github_create_issue
github_comment_issue
github_create_branch
github_open_pull_request
```

Для них должны работать approvals, idempotency и reconciliation Friday.

## Fallback не должен дублировать полную реализацию

Полноценный native fallback для каждого MCP server уничтожит экономию. Получатся:

- две реализации;
- два набора тестов;
- две поверхности ошибок;
- постоянная проблема расхождения поведения.

Fallback должен быть тонким и деградированным, а не функционально равным primary implementation.

| Возможность | MCP primary | Разумный fallback |
|---|---|---|
| Web search | Search MCP | Текущий простой native provider |
| JS browser | Playwright MCP | Попытка обычного fetch и честный отказ от интерактивной части |
| GitHub | Official GitHub MCP | Локальный checkout или явная недоступность |
| Внешняя БД | MCP для новых engines | Текущие SQLite/Postgres/MySQL |
| Cloud files | Drive/Dropbox MCP | Локальный inbox/outbox |
| Уведомления | Slack/email MCP | Durable queue, но не молчаливая отправка через другой канал |
| Calendar | Calendar MCP | Локальное reminder предложение только после согласия пользователя |

### Когда автоматический fallback допустим

Только для безопасного чтения и только при ясной категории сбоя:

- server unavailable;
- startup timeout;
- transport failure до начала исполнения;
- отсутствующий allowlisted tool;
- protocol violation с последующим отключением подозрительного server;
- временная недоступность read-only операции.

### Когда fallback запрещён

- `PermissionError`;
- policy denial;
- некорректные аргументы;
- отказ пользователя;
- domain error, например «issue не существует»;
- unknown outcome изменяющей операции;
- ситуация, когда внешний эффект мог уже произойти.

Особенно опасен сценарий:

```text
Calendar MCP получил create_event
    -> соединение оборвалось
    -> Friday вызывает native fallback
    -> создаются два события
```

Правильная реакция:

```text
status = uncertain
    -> проверить postcondition
    -> определить, произошёл ли эффект
    -> только после этого решать, повторять ли операцию
```

Эта философия уже присутствует в Friday для побочных эффектов, idempotency fences и mission execution. Её следует распространить на MCP integrations.

## Разделить классы MCP-ошибок

Сейчас [`MCPUnavailableError`](../friday/mcp_runtime/client.py) охватывает несколько разных случаев:

- server unavailable;
- missing tool;
- tool returned `is_error`;
- invalid structured result;
- transport failure;
- retired connection;
- timeout.

Для ручного использования это приемлемо. Для автоматического fallback такой категории недостаточно.

Предлагаемая таксономия:

```python
class MCPError(RuntimeError):
    pass


class MCPTransportUnavailable(MCPError):
    """Для read-only операции допустим доверенный fallback."""


class MCPProtocolViolation(MCPError):
    """Сервер нужно отключить; read-only fallback может быть разрешён policy."""


class MCPRemoteRejected(MCPError):
    """Удалённый tool обработал запрос и отказал. Не повторять автоматически."""


class MCPPolicyDenied(MCPError):
    """Локальная policy Friday запретила действие. Fallback запрещён."""


class MCPUncertainEffect(MCPError):
    """Изменение могло произойти. Требуются reconciliation и postcondition."""
```

## Сделать capability routing декларативным

Модель не должна знать, каким transport реализован инструмент.

Предлагаемый уровень абстракции:

```python
@dataclass(frozen=True)
class CapabilityRoute:
    name: str
    primary: ToolProvider
    fallback: ToolProvider | None
    risk: Literal["observe", "mutate", "high"]
    fallback_on: frozenset[type[MCPError]]
    postcondition: Callable[..., Awaitable[bool]] | None = None
```

Общая архитектура:

```text
Model
    -> Friday ToolSpec
    -> ExecutionKernel
    -> permissions / budgets / approvals
    -> code-owned capability adapter
        -> MCP primary
        -> native degraded fallback
    -> result validation
    -> provenance / audit
```

Модели должно быть безразлично, находится ли под Friday ToolSpec:

- MCP server;
- Python function;
- локальный subprocess;
- HTTP adapter;
- queued human approval.

Контракт принадлежит Friday.

## Friday стоит сделать MCP-сервером

Это отдельный стратегически выгодный ход.

Сейчас Friday строит собственные frontend и agent interfaces. Если Friday сама предоставит MCP server, её память смогут использовать другие среды и агенты без отдельных интеграций.

### Возможные resources

```text
friday://documents/{document_id}
friday://entities/{entity_id}
friday://conversations/{conversation_id}
friday://timeline/{local_date}
friday://evidence/{bundle_id}
```

### Read-only tools

```text
friday_search
friday_find_person
friday_graph_neighbors
friday_get_timeline
friday_explain_evidence
```

### Mutating tools

```text
friday_remember
friday_ingest
friday_create_reminder
friday_resolve_conflict
```

Resources подходят для адресуемых данных и контекста. Tools подходят для вычислений и действий.

Начальная версия должна быть максимально узкой:

- local stdio;
- owner-only;
- read-only;
- fixed allowlist;
- без remote dynamic discovery;
- с теми же tenant, permission и evidence boundaries;
- без mutating tools.

Первый безопасный набор:

```text
friday_search
friday_explain_evidence
friday://documents/{id}
friday://entities/{id}
```

После стабилизации можно добавить `remember` и reminders через approvals.

В перспективе Friday станет не только самостоятельным ассистентом, но и персональным memory backend для других агентов.

## Поэтапный план

### Этап 1: обобщить существующий MCP runtime

Без изменения текущего поведения:

- разделить классы ошибок;
- добавить `CapabilityRoute`;
- описать fallback policy;
- добавить health state;
- добавить circuit breaker;
- добавить метрики latency, failure class и unavailable duration;
- сохранить fixed wrappers;
- сохранить запрет на публикацию remote schemas модели;
- сохранить code-owned server definitions.

### Этап 2: подключить первый официальный внешний MCP

Подходящий кандидат: GitHub MCP в read-only режиме.

Ограничения первой версии:

- только repositories, issues и pull requests;
- несколько собственных Friday ToolSpec;
- отдельные tenant-scoped credentials;
- pinned server version или image digest;
- bounded result projection;
- никаких write tools;
- полный audit вызова;
- native fallback только к локальному checkout.

Это проверит общую архитектуру на зрелом сервере без риска для памяти и приватных документов.

### Этап 3: разделить web path

- static safe fetch оставить native;
- search provider разрешить через MCP;
- browser MCP использовать как escalation path;
- browser write actions сначала полностью запретить;
- результаты прогонять через существующие provenance и citation механизмы;
- сохранить privacy classifier перед отправкой внешнего запроса;
- сохранить domain/freshness policy внутри Friday.

### Этап 4: добавить Friday MCP server

Первая версия:

```text
friday_search
friday_explain_evidence
friday://documents/{id}
friday://entities/{id}
```

Режим:

- local;
- owner-only;
- read-only;
- bounded;
- audited.

### После этого

- новые SaaS подключать через MCP;
- новые СУБД подключать через MCP;
- новые cloud storage подключать через MCP;
- не писать новые native OAuth clients без очень веской причины;
- каждый mutating connector вводить отдельно после read-only эксплуатации;
- каждый внешний server рассматривать как недоверенный процесс.

## Что убрать из собственной разработки

- полные API-клиенты SaaS;
- OAuth для каждого отдельного сервиса;
- provider-specific пагинацию;
- полноценный браузерный оркестратор;
- поддержку новых СУБД;
- отдельные GitHub/Jira/Linear клиенты;
- cloud publishing implementation;
- низкоуровневые клиенты внешней диагностики.

## Что сохранить своим

- ingestion;
- memory;
- retrieval;
- graph;
- provenance;
- evidence;
- permissions;
- privacy;
- approvals;
- idempotency;
- temporal semantics;
- local file storage;
- Telegram transport;
- local voice;
- model runtime;
- verifier;
- review workflow;
- audit.

## Итоговая оценка масштаба

Friday уже является не просто Telegram-ботом. По текущему дереву проекта это локальная knowledge-платформа, включающая:

- ingestion pipeline;
- immutable raw objects;
- knowledge graph;
- hybrid retrieval;
- temporal graph semantics;
- agent runtime;
- execution kernel;
- missions;
- permissions;
- privacy plane;
- admin UI;
- Telegram frontend;
- backup and restore;
- diagnostics;
- voice;
- files and OCR;
- MCP runtime;
- operational supervision.

Объективно тяжело одновременно строить продукт такого масштаба и собственную реализацию каждого внешнего API.

Задачу не обязательно уменьшать до банального бота. Нужно уменьшить территорию, которую Friday обязана обслуживать собственными руками.

**Итоговая формула:** сохранить собственными мозг, память, доказательства и правила. Стандартизировать внешние интеграции через MCP.

## Файлы репозитория, на которых основано наблюдение

- [`README.md`](../README.md)
- [`pyproject.toml`](../pyproject.toml)
- [`.env.example`](../.env.example)
- [`friday/mcp_runtime/client.py`](../friday/mcp_runtime/client.py)
- [`friday/mcp_runtime/tools.py`](../friday/mcp_runtime/tools.py)
- [`friday/mcp_runtime/workspace_fs.py`](../friday/mcp_runtime/workspace_fs.py)
- [`friday/web_surfer/__init__.py`](../friday/web_surfer/__init__.py)
- [`friday/data_sources.py`](../friday/data_sources.py)
- [`friday/telegram_bridge/`](../friday/telegram_bridge/)
- [`friday/ingestion/`](../friday/ingestion/)
- [`friday/generated_files.py`](../friday/generated_files.py)
- [`friday/whisper.py`](../friday/whisper.py)
- [`friday/tts.py`](../friday/tts.py)
- [`friday/execution_kernel/`](../friday/execution_kernel/)
- [`docs/ORGANS.md`](../docs/ORGANS.md)
- [`docs/EXECUTIVE.md`](../docs/EXECUTIVE.md)

## Внешние ориентиры

- MCP Specification: <https://modelcontextprotocol.io/specification/>
- MCP Python SDK: <https://github.com/modelcontextprotocol/python-sdk>
- MCP Registry: <https://github.com/modelcontextprotocol/registry>
- GitHub MCP Server: <https://github.com/github/github-mcp-server>
- Playwright MCP: <https://github.com/microsoft/playwright-mcp>
