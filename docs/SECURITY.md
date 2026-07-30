# Безопасность

## 1. Основные границы доверия

1. **Admin bearer token** (`JERICHO_API_TOKEN`) — полный доступ владельца. Утечка токена равна компрометации базы. Дополнительно можно выпускать **scoped-токены** для отдельных аккаунтов: такой токен аутентифицирует ровно с preset/capabilities своего аккаунта (не владельца), хранится как SHA-256 и отзывается.
2. **Telegram bridge secret** — право представлять Telegram identities backend-у. Он не должен попадать пользователям бота.
3. **Telegram bot token** — хранится только в процессе bridge.
4. **Backend** — имеет доступ к SQLite, raw files, vault и локальному LLM.
5. **vLLM** — получает prompt/context; держите endpoint на loopback/private Docker network.
6. **Admin UI** — статическое приложение, хранит bearer token только в `sessionStorage` текущей вкладки.

## 2. Аутентификация

### Admin/API

`Authorization: Bearer <JERICHO_API_TOKEN>` сравнивается через constant-time comparison и даёт владельца. Минимум — 32 символа; `jericho init` генерирует 48-byte URL-safe token.

**Scoped-токены аккаунтов.** Любой другой bearer резолвится по SHA-256 в таблице `api_tokens` и аутентифицирует как привязанный аккаунт с его preset/capabilities — так ролевая модель применяется и к HTTP-акторам, а не только к Telegram-пользователям. Плейнтекст показывается один раз при выпуске и никогда не хранится. Выпуск/список/отзыв — capability `admin.tokens.manage` (owner+admin) через `POST/GET/DELETE /api/admin/tokens`; для bootstrap офлайн — `jericho mint-token --user <id> --preset <preset>` и `jericho revoke-token <id>`. Delegated-админ не может выпустить токен для owner-аккаунта (защита от эскалации), и не-owner не может назначить preset `owner`. Отозванный или неизвестный токен → 401.

Если `JERICHO_API_REQUIRE_TOKEN_ON_LOOPBACK=0`, loopback может работать как owner без токена; этот путь дополнительно закрыт CSRF/rebinding-guard-ом (loopback-`Host` обязателен, мутации проверяют `Origin`/`Sec-Fetch-Site` — см. §5) и уважает статус аккаунта: отключённый владелец получает 401, беспарольный путь не реактивирует его молча. Для нормальной эксплуатации оставляйте значение `1`.

**Rate-limit неудачных попыток.** Неудачная аутентификация (неверный bearer, невалидная подпись моста, битые креденшалы) расходует бюджет `JERICHO_API_AUTH_FAILURE_LIMIT_PER_MINUTE` (по умолчанию 10) на IP клиента; после исчерпания запросы получают 429 с `Retry-After` ещё до оценки креденшалов — brute-force токена ограничен, timing-оракул закрыт. Успешная аутентификация бюджет не расходует.

### Telegram bridge

Подпись включает:

```text
timestamp
HTTP method
path (включая query string)
external user id
chat id
nonce
SHA-256(body)
```

Backend проверяет HMAC-SHA256, допустимый возраст timestamp, целочисленные identity и одноразовость nonce. Nonce (32-символьный hex) генерируется мостом на каждый запрос; backend хранит короткоживущий кэш увиденных nonce и отклоняет повтор внутри окна свежести — это закрывает воспроизведение перехваченного подписанного запроса. Durable idempotency lease захватывается до побочных эффектов и привязывает `source_ref` к SHA-256 фактического payload: точный retry воспроизводится, активный конкурент повторяется позже, а изменённое содержимое получает постоянный conflict.

**Deny-by-default доступ к чатам.** Бот принимает только чаты из эффективного allowlist = `JERICHO_TELEGRAM_ALLOWED_CHAT_IDS` ∪ `JERICHO_TELEGRAM_OWNER_CHAT_IDS`; пустой список не допускает никого. Проверка дублируется на мосту (сообщения и callback) и на backend (403, чтобы мост сразу отбрасывал неавторизованный чат). Если bridge secret задан, а эффективный allowlist пуст, `validate_settings` даёт жёсткую ошибку в production и предупреждение в loopback; мост отказывается стартовать открытым.

## 3. Авторизация

Политика — default deny.

- Каждое HTTP-действие требует capability.
- Каждый agent tool имеет `security_id` и повторно проверяется перед invocation.
- Tool list для модели фильтруется по actor, поэтому запрещённые инструменты не только блокируются, но и не показываются модели.
- Explicit deny сильнее preset; explicit allow сильнее preset deny-by-absence.
- Cross-tenant Admin API использует отдельные `admin.all_data.read/manage`.
- Стандартные preset-ы не получают `code.run`.
- Только owner может назначать preset `owner` и изменять owner-аккаунты.
- Не-owner может делегировать только capability, которой обладает сам; это правило действует для preset-ов, allow override и снятия deny. Инвариант применяется **на уровне `AuthorizationService`** (`grant_permission`/`set_user_preset`/`create_custom_preset` с `acting_actor`), а не только в HTTP-роутах — не-HTTP вызов сервиса подчиняется той же проверке. `deny`/`revoke` проверки не требуют: сужать доступ можно всегда.
- **Execution kernel — fail-closed**: kernel без authorization-сервиса не показывает и не исполняет ни одного инструмента (никогда «всё разрешено по умолчанию»).
- Доверенные исключения без `acting_actor`: bootstrap владельца при старте backend и офлайн-команды CLI (`jericho mint-token` может назначить preset, включая owner) — оператор с доступом к файловой системе и так контролирует базу; обе поверхности локальные и не сетевые.

## 4. Tenant isolation

Доступ к knowledge, raw objects, entities, relations, conversations, files и feedback ограничивается `user_id`. Knowledge Object обязан ссылаться на Raw Object того же tenant. Entity merge и relation creation проверяют владение обеих сторон.

Новые методы storage должны принимать `user_id`; запрос без tenant filter допустим только внутри явно административного endpoint после `admin.all_data.*`.

## 5. Admin UI и HTTP

- Admin UI обслуживается backend-ом и защищается строгой Content Security Policy **без `unsafe-inline`** (`script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`), `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` и запретом framing. UI разделён на внешние `app.js`/`app.css`; обработчики событий — только делегированные (`data-call` с JSON-payload в явный реестр действий), поэтому XSS в отрендеренном контенте не может исполнить скрипт даже при ошибке экранирования.
- Беспарольный loopback-доступ (`JERICHO_API_REQUIRE_TOKEN_ON_LOOPBACK=0`) защищён от браузерных атак: `Host` обязан быть loopback-именем (`localhost`/`127.0.0.1`/`::1` — блокирует DNS rebinding), а мутирующие методы дополнительно проходят `Origin`/`Sec-Fetch-Site`-проверку (разрешены loopback-origins и `JERICHO_CORS_ORIGINS`). Cross-site страница не может выполнить мутацию от имени владельца через его браузер. Non-browser клиенты (curl, CLI) этих заголовков не шлют и работают как раньше; bearer-токены и HMAC моста guard не затрагивает — это не-CSRF-абельные пути с явными креденшалами.
- Wildcard CORS запрещён конфигурационным validator-ом.
- `JERICHO_TRUST_PROXY_HEADERS=0` по умолчанию. При включении задайте `JERICHO_TRUSTED_PROXY_NETWORKS`: forwarded headers учитываются только от непосредственного TCP peer из этого списка, а цепочка разбирается справа налево. Сам заголовок не может выдать удалённого клиента за loopback.
- При публикации наружу используйте TLS, дополнительную authentication layer и firewall allowlist.
- Не выставляйте vLLM port в публичную сеть.
- Pure-ASGI body limiter считает реально принятые байты, включая chunked transfer, и отклоняет oversized JSON/base64/multipart до аутентификации и парсинга.
- Public JSON endpoints строго проверяют booleans и конечные числа: строки вроде `"false"`, `NaN`, infinity и значения вне разрешённого диапазона получают `400`, а не неявное преобразование.
- Telegram callback target ограничен безопасным идентификатором; произвольный путь нельзя превратить в backend endpoint.

Рекомендуемая схема:

```text
Internet → VPN / mTLS / SSO reverse proxy → 127.0.0.1:8000
Docker private network → vLLM:8001
```

## 6. Контекст модели и enrichment как недоверенная граница

Knowledge Objects, graph evidence, conversation excerpts, имена вложений, web/tool output и Raw Object — недоверенные данные, а не инструкции. Agent Runtime передаёт динамический контекст как сериализованный пользовательский data block; system-role содержит только статическую policy и явный запрет исполнять инструкции из данных. Производная модель пользователя (`user_model`: имена людей/проектов/тегов из его же базы) едет в том же недоверенном конверте — имена сущностей могут содержать враждебный текст, поэтому они никогда не поднимаются до system-инструкций, а SYSTEM_PROMPT явно понижает модель до фонового ориентира без права быть источником фактов.

Raw Object и deterministic proposal являются источниками review; ответ vLLM — недоверенные данные. Inbox advisor:

- требует pending item того же tenant;
- передаёт модели bounded source и JSON schema, а не административные инструкции;
- принимает только один bounded JSON object без trailing prose;
- проверяет типы, enum-значения и диапазоны;
- отбрасывает model-only entity без буквального mention в Raw Object;
- ограничивает confidence таких сущностей ниже порогов автоматического graph create/link;
- не меняет Inbox status, reviewer, promotion/quality score;
- не создаёт Knowledge Object/entity/relation и не выполняет entity merge;
- пишет в audit только безопасную сводку model/recommendation/confidence, а не исходное личное содержимое.

Background worker использует тот же ingestion boundary и обрабатывает ограниченное число items за цикл. Model output никогда не подмешивается обратно как доверенный deterministic baseline.

Research synthesis также является недоверенным output. Даже явная кнопка сохранения создаёт только Raw Object + pending Inbox candidate. Relation candidates, potential conflicts и lifecycle candidates остаются review-only и capability-gated.

## 7. Web surfer и SSRF

По умолчанию запрещены:

- схемы кроме HTTP/HTTPS;
- URL с username/password;
- localhost и loopback;
- private, link-local, multicast, reserved и unspecified IP;
- hostname, резолвящийся хотя бы в один запрещённый адрес;
- DNS rebinding между проверкой и соединением: HTTP transport подключается к уже проверенному IP, сохраняя исходные Host и TLS SNI;
- редирект на запрещённый адрес (каждый hop проверяется заново);
- ответ больше установленного лимита.

`JERICHO_WEB_ALLOW_PRIVATE_NETWORKS=1` отключает часть защиты и должен использоваться только в изолированной сети при явной необходимости.

## 8. Документы и архивы

Extractor:

- не исполняет вложения;
- нормализует имена;
- не распаковывает ZIP/RAR/7z в произвольный filesystem path;
- ограничивает входной byte size до запуска format parser;
- ограничивает число entries, суммарный uncompressed size, размер member и compression ratio;
- TAR проходит streaming iteration, CSV — bounded rows/output, PDF — bounded pages, Office ZIP — preflight до библиотечного parser;
- ограничивает итоговый текст и не накапливает безграничные промежуточные строки;
- не доверяет расширению как единственному источнику MIME.

Изображения и страницы сканированных PDF перед vision/OCR ограничиваются по количеству, размеру, пикселям и encoded bytes, нормализуются в безопасный JPEG и передаются только локальному endpoint. Строгий JSON output ограничивает длины и confidence; model-only entities остаются Inbox suggestions и не становятся уверенными graph links.

Несмотря на это, парсеры сторонних форматов являются сложной attack surface. Для недоверенных массовых загрузок запускайте backend в контейнере с read-only root, dropped capabilities и отдельным volume.

## 9. Code execution

`JERICHO_CODE_EXECUTION_ENABLED=0` — обязательный безопасный default.

Встроенный executor ограничивает timeout, output и часть environment, но остаётся обычным subprocess на том же OS/container. Он **не** является надёжной sandbox boundary против злонамеренного Python-кода.

При timeout или превышении `JERICHO_CODE_EXECUTION_MAX_OUTPUT_BYTES` Jericho завершает всё созданное executor-ом process tree (POSIX process group / Windows `taskkill /T`), чтобы дочерний процесс не продолжил работу после уже отклонённого tool call. Stdout/stderr читаются потоково в общий budget. Это защита отказоустойчивости, а не замена песочницы.

Для реального code execution нужен отдельный disposable sandbox service: rootless container/VM, seccomp, no network, read-only image, tmpfs quota, PID/memory/CPU limits и одноразовый workspace. До этого момента не выдавайте `code.run` пользователям.

## 10. Секреты

- `.env`, `.env.local`, токены и bridge secret исключены из Git/архива.
- Не выводите их в screenshots. Стандартный logging formatter дополнительно редактирует известные credentials, Authorization values и Telegram token внутри URL/traceback, но это defense in depth, а не разрешение логировать секреты намеренно.
- **Приватность запросов в логах**: access-log uvicorn проходит через фильтр, срезающий query string (`/api/search?[stripped]`) — поисковые запросы и browse-фильтры суть персональные данные, которые редактор секретов распознать не может. `web_surfer` логирует только hostname и класс исключения — `str()` httpx-ошибок содержит полный URL с параметрами поисковых провайдеров.
- Храните backup secrets отдельно и зашифрованно.
- После подозрения на утечку одновременно смените API token, bridge secret и Telegram bot token.
- Перезапустите backend/bridge после ротации.

## 11. Audit

Audit log фиксирует actor, action, target, before/after, request ID и IP для административных изменений и tool invocations, а также для событий выгрузки данных и чтений чужих данных:

- **Egress логируется всегда**: скачивание файлов (`admin.file.download`, user-scoped `file.download`), резервных копий (`admin.backup.download` — копия содержит данные всех tenant'ов), экспортов (`admin.export.download` — именно скачивание, не только создание) и чтение самого аудит-лога (`admin.audit.read`).
- **Cross-tenant чтения** (админ читает контент чужого аккаунта: знания, inbox, сущности, диалоги, сообщения, файлы) логируются с целевым пользователем в `target_id`. Чтение собственных данных не логируется — иначе владелец, листающий свою же админку, заполнил бы журнал шумом без privacy-сигнала.

**Отказ на границе — не одно событие, а два.** `auth.failed` означает «не пустили»: неверные или неразобранные учётные данные, отказ по возможностям, исчерпанный бюджет неудачных попыток. `request.throttled` означает «своего придержали»: ограничитель частоты сработал ПОСЛЕ успешной аутентификации, актор действителен и назван по имени. Ни то ни другое не хранит предъявленный секрет — только метод, путь, причину, статус, адрес и request id.

Разделение появилось по замеру на живой установке: из 1302 записей `auth.failed` **1188** были собственной массовой работой владельца (разбор Inbox пачкой с верным токеном с 127.0.0.1), ещё 89 — обращениями к `/health`, пути, которого не существовало. Диагностика при пороге 60 за сутки кричала «возможен брутфорс» постоянно, а **три** настоящих обращения с чужого адреса лежали под этой лавиной невидимыми. Сигнал безопасности, который горит всегда, перестаёт быть сигналом; поэтому `/health` теперь публичный синоним `/api/health` (он и так был публичен), а троттлинг вошедшего пользователя считается отдельно и в порог брутфорса не входит.

**Append-only обеспечивается на уровне БД**: триггеры `audit_log_no_update`/`audit_log_no_delete` (`RAISE(ABORT)`) отклоняют любую попытку изменить или удалить строку журнала — включая будущие баги кода. Purge, удаление диалогов и экспорт журнал не трогают. Это по-прежнему не криптографически неизменяемый ledger: администратор с прямым доступом к файлу SQLite может удалить триггеры; для tamper evidence можно позже добавить hash chaining и периодическую подпись checkpoint внешним ключом.

## 12. Контрольный список перед публикацией

- [ ] `jericho doctor` без критических ошибок.
- [ ] API token и bridge secret уникальны и длиннее 32 символов.
- [ ] `JERICHO_API_REQUIRE_TOKEN_ON_LOOPBACK=1`.
- [ ] `JERICHO_TRUST_PROXY_HEADERS=0`, если нет доверенного proxy.
- [ ] При доверенных proxy перечислены только непосредственные hop-ы в `JERICHO_TRUSTED_PROXY_NETWORKS`.
- [ ] CORS содержит только реальные origin.
- [ ] `JERICHO_WEB_ALLOW_PRIVATE_NETWORKS=0`.
- [ ] `JERICHO_CODE_EXECUTION_ENABLED=0`.
- [ ] Backend bind только loopback/VPN/private interface.
- [ ] vLLM не опубликован наружу.
- [ ] Есть проверенная резервная копия БД и отдельная копия raw files.
