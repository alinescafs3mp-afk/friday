# Безопасность

> Проект переименован: **Friday** (по-русски — **Пятница**), ex codename Jericho.

## 1. Основные границы доверия

1. **Admin bearer token** (`FRIDAY_API_TOKEN`) — полный доступ владельца. Утечка токена равна компрометации базы. Дополнительно можно выпускать **scoped-токены** для отдельных аккаунтов: такой токен аутентифицирует ровно с preset/capabilities своего аккаунта (не владельца), хранится как SHA-256 и отзывается.
2. **Telegram bridge secret** — право представлять Telegram identities backend-у. Он не должен попадать пользователям бота.
3. **Telegram bot token** — хранится только в процессе bridge.
4. **Backend** — имеет доступ к SQLite, raw files, vault и локальному LLM.
5. **vLLM** — получает prompt/context; держите endpoint на loopback/private Docker network.
6. **Admin UI** — статическое приложение, хранит bearer token только в `sessionStorage` текущей вкладки.

## 2. Аутентификация

### Admin/API

`Authorization: Bearer <FRIDAY_API_TOKEN>` сравнивается через constant-time comparison и даёт владельца. Минимум — 32 символа; `jericho init` генерирует 48-byte URL-safe token.

**Scoped-токены аккаунтов.** Любой другой bearer резолвится по SHA-256 в таблице `api_tokens` и аутентифицирует как привязанный аккаунт с его preset/capabilities — так ролевая модель применяется и к HTTP-акторам, а не только к Telegram-пользователям. Плейнтекст показывается один раз при выпуске и никогда не хранится. Выпуск/список/отзыв — capability `admin.tokens.manage` (owner+admin) через `POST/GET/DELETE /api/admin/tokens`; для bootstrap офлайн — `jericho mint-token --user <id> --preset <preset>` и `jericho revoke-token <id>`. Delegated-админ не может выпустить токен для owner-аккаунта (защита от эскалации), и не-owner не может назначить preset `owner`. Отозванный или неизвестный токен → 401.

Если `FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK=0`, loopback может работать как owner без токена; этот путь дополнительно закрыт CSRF/rebinding-guard-ом (loopback-`Host` обязателен, мутации проверяют `Origin`/`Sec-Fetch-Site` — см. §5) и уважает статус аккаунта: отключённый владелец получает 401, беспарольный путь не реактивирует его молча. Для нормальной эксплуатации оставляйте значение `1`.

**Rate-limit неудачных попыток.** Неудачная аутентификация (неверный bearer, невалидная подпись моста, битые креденшалы) расходует бюджет `FRIDAY_API_AUTH_FAILURE_LIMIT_PER_MINUTE` (по умолчанию 10) на IP клиента; после исчерпания запросы получают 429 с `Retry-After` ещё до оценки креденшалов — brute-force токена ограничен, timing-оракул закрыт. Успешная аутентификация бюджет не расходует.

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

**Deny-by-default доступ к чатам.** Бот принимает только чаты из эффективного allowlist = `FRIDAY_TELEGRAM_ALLOWED_CHAT_IDS` ∪ `FRIDAY_TELEGRAM_OWNER_CHAT_IDS`; пустой список не допускает никого. Проверка дублируется на мосту (сообщения и callback) и на backend (403, чтобы мост сразу отбрасывал неавторизованный чат). Если bridge secret задан, а эффективный allowlist пуст, `validate_settings` даёт жёсткую ошибку в production и предупреждение в loopback; мост отказывается стартовать открытым.

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

### Личное напоминание внутри общего tenant

В shared-режиме `user_id` обозначает общий архив, а не конкретного человека.
Поэтому напоминание получает отдельную долговечную authority:
`private_entity_owners(entity_id, person_id, privacy_kind='reminder')`. Запись
события, точного `entity_time.source='reminder:<person_id>'` и owner marker
происходит одной транзакцией. Свободная строка `source`, Telegram chat ID и само
имя сущности не являются доказательством владения.

Generic graph/retrieval/profile/organs/model/admin readers не видят ни личную
сущность, ни производную копию её ID, current или authenticated historical
имени/алиаса в Raw/Knowledge Object, Inbox, relation/candidate/resolution/merge
evidence, версиях, feedback/eval/usage, notification queue и диагностических
агрегатах. Alias containers декодируются рекурсивно с жёстким byte/node budget;
malformed, oversized либо повторно закодированный material отклоняется fail-closed.
Сопоставление выполняется после Unicode NFC → casefold → NFC, чтобы иной регистр или
NFD не открывал копию. Mutation path повторяет ту же проверку внутри транзакции и
отвечает как на отсутствующий объект, не создавая oracle существования.

Materialized authority не создаёт дополнительных копий identity/content text. Sparse entity cache/work
содержит только quarantined entity IDs; второй cache/work — только
`(material_kind, object_id, user_id)` видимых Raw/Knowledge/Inbox и bounded
`knowledge_hidden` helper. У каждой пары свой singleton state, valid лишь при
точном равенстве; все generic material surfaces требуют оба valid state.
Persistent UDF-free guards сначала инвалидируют нужный state даже у внешнего raw
SQLite writer. После регистрации Unicode UDF managed connection создаёт только в
TEMP schema identity views и AFTER triggers: обычный insert/non-flipping update
меняет один ID, privacy flip пересобирает ordered authority в той же transaction,
а raw/offline write остаётся invalid до exact heal новым соединением. Поэтому
обычный `ALTER TABLE` не обязан знать application UDF. Per-connection SQLite
authorizer запрещает прямой caller DML над обоими cache/work/state, DDL над owned
privacy objects и `writable_schema`. Startup под тем же write-lock проверяет только
что пересобранный tier как `work == cache`: work перед этим очищен и целиком заполнен
из live authority, а cache опубликован только из work. Tier, который этот opener не
пересобирал, по-прежнему независимо проверяется как `cache == live`. Повреждённая
derivative authority поэтому закрывает generic чтение, а не открывает его.

Личный reminder-reader и доставка возвращают запись только точному `person_id`,
когда одновременно совпали durable marker, time provenance, валидные
current/history states и нет ни конфликтующего владельца, ни зависимости от иной
cached private identity. Обычные строки идут по ID-cache fast path; public carrier
даже собственного reminder консервативно остаётся скрытым. Person export —
отдельная разрешённая граница: в одной SQLite snapshot он
строит fresh recursive closure от всех запрещённых direct-private seeds, исключая
только точное непротиворечивое marker/time/source экспортирующего человека. Так его
собственная private запись и зависимости только от неё остаются в архиве, а чужая,
неоднозначная, malformed и транзитивная history исключается. Уже созданный
самостоятельный export или backup не переписывается скрыто: до отдельной
owner-approved rotation он остаётся чувствительным артефактом.

Локальные каталоги runtime/backups/exports/vault имеют режим `0700`, а SQLite,
WAL/SHM, bridge DB, manifests и data files — `0600`, включая момент до первого
открытия SQLite при обычном `umask 022`. HTTP-download не оставляет отложенный
дескриптор на повторно подменяемый путь: файл проверяется и копируется в
контролируемый private snapshot до ответа.

## 5. Admin UI и HTTP

- Admin UI обслуживается backend-ом и защищается строгой Content Security Policy **без `unsafe-inline`** (`script-src 'self'; style-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`), `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy` и запретом framing. UI разделён на внешние `app.js`/`app.css`; обработчики событий — только делегированные (`data-call` с JSON-payload в явный реестр действий), поэтому XSS в отрендеренном контенте не может исполнить скрипт даже при ошибке экранирования.
- Беспарольный loopback-доступ (`FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK=0`) защищён от браузерных атак: `Host` обязан быть loopback-именем (`localhost`/`127.0.0.1`/`::1` — блокирует DNS rebinding), а мутирующие методы дополнительно проходят `Origin`/`Sec-Fetch-Site`-проверку (разрешены loopback-origins и `FRIDAY_CORS_ORIGINS`). Cross-site страница не может выполнить мутацию от имени владельца через его браузер. Non-browser клиенты (curl, CLI) этих заголовков не шлют и работают как раньше; bearer-токены и HMAC моста guard не затрагивает — это не-CSRF-абельные пути с явными креденшалами.
- Wildcard CORS запрещён конфигурационным validator-ом.
- Native TLS включается только полной парой `FRIDAY_SSL_CERTFILE`/
  `FRIDAY_SSL_KEYFILE`. Telegram bridge и live diagnostics при этом сами
  переходят на HTTPS, добавляют публичный `FRIDAY_BACKEND_CA_FILE` к проверяемым
  default roots клиента (httpx/certifi у bridge, OS defaults у diagnostics) и
  сохраняют hostname verification. `verify=False` и
  эквивалентный retry после ошибки сертификата отсутствуют.
- Server certificate должен иметь SAN для каждого browser-facing имени/IP и для
  loopback-адреса внутреннего systemd-клиента (`127.0.0.1` при IPv4 wildcard,
  `::1` при IPv6 wildcard). Самоподписанный сертификат безопасен только после явного
  импорта его публичной части в trust store клиента; «продолжить несмотря на
  предупреждение» не устанавливает надёжную identity boundary.
- Public cert/CA и private key обязаны быть разными файлами. Validator сравнивает
  inode, а не только строки пути, и отклоняет symlink/hardlink alias ключа как
  `FRIDAY_SSL_CERTFILE` или `FRIDAY_BACKEND_CA_FILE`; локальные клиенты никогда не
  получают key под видом trust anchor.
- `FRIDAY_TRUST_PROXY_HEADERS=0` по умолчанию. При включении задайте `FRIDAY_TRUSTED_PROXY_NETWORKS`: forwarded headers учитываются только от непосредственного TCP peer из этого списка, а цепочка разбирается справа налево. Сам заголовок не может выдать удалённого клиента за loopback.
- При публикации наружу используйте TLS, дополнительную authentication layer и firewall allowlist.
- Не выставляйте vLLM port в публичную сеть.
- Pure-ASGI body limiter считает реально принятые байты, включая chunked transfer, и отклоняет oversized JSON/base64/multipart до аутентификации и парсинга.
- Public JSON endpoints строго проверяют booleans и конечные числа: строки вроде `"false"`, `NaN`, infinity и значения вне разрешённого диапазона получают `400`, а не неявное преобразование.
- Telegram callback target ограничен безопасным идентификатором; произвольный путь нельзя превратить в backend endpoint.

Рекомендуемая схема:

```text
LAN → native TLS + exact CORS → backend
Internet → VPN / mTLS / SSO reverse proxy → loopback backend
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

Ход с приватным источником закрывает outbound hard boundary, а не полагается на уговор модели. Это относится к current/restored/reply/replay/deictic файлам, `source_search`, MCP-inbox read и sticky private-lineage. После допуска такого носителя model schemas проходят закрытую классификацию: остаются только явно перечисленные process-local инструменты; `web_search`, `web_fetch`, `web_research`, семейство внешних `data_*`, не являющийся OS-песочницей `code_run`, `workspace_create` и любой последующий MCP-вызов удаляются. Неизвестный будущий connector по умолчанию запрещён. Та же проверка повторяется перед kernel, поэтому hallucinated/native call и его аргументы не достигают provider. `workspace_list/search/read` разрешены только для приобретения или повторной проверки источника до появления приватного текста; после результата MCP закрывается. Единственное web-исключение — изолированная current-message-only сводка публичных новостей: она допускается лишь без current/restored/reply/replay/deictic/MCP/source carrier и не получает историю, retrieval или личные правила. После первого file-grounded хода private-lineage переносится каждым assistant message до нового conversation: файл сам по себе не становится ambient context, но outbound и account-wide обучение standing rules/corrections остаются закрыты, чтобы пересказ приватного ответа не попал в глобальную память. Synthesis, verifier и единственный repair используют один bounded projection без повторной обрезки attachment chunks. Vision/OCR/transcript видимы только как advisory и не становятся verification evidence. Неполный parser/context/advisory projection не может подтвердить count/all/exhaustiveness ни в исходном ответе, ни после repair: финальный статус принудительно `unknown`. Сырые замечания verifier доступны лишь немедленному repair; durable metadata, API, Telegram, TTS и idempotency получают только allowlisted issue code. Данные repair передаются user-role недоверенным JSON, а system-role остаётся статическим.

Для native DOCX/XLSX эта граница усилена content-free `OfficeStructureIndex v1`.
Его schema закрыта allowlist-ом, размер ограничен 48 КиБ, а каждый span и reference
повторно связывается с exact UTF-8 hash текущего Raw text. Durable индекс не содержит
cell/paragraph literals и хранится только у Raw Object; caller metadata не может его
подменить, Knowledge/Inbox/API его не получают. Installation-local HMAC связывает
canonical индекс с SHA-256 байтов именно этого Raw-файла; current/restored/replay
проверяют оба значения из tenant-scoped строки перед выдачей process-private trust
marker. No-save получает такой marker только напрямую от parser path, не переживает
текущий вызов и не попадает в idempotency. Координированное уменьшение record set,
candidate list и declared count отвергается повторным code-owned выводом и подписью;
подписанный индекс другого файла не принимается даже при одинаковом flat text.
Это authenticity boundary для API/caller metadata и повреждения без штатного key
path, не шифрование базы: installation key живёт в защищённой SQLite вместе с
данными, поэтому полностью привилегированный читатель БД вне threat model может
прочитать ключ и пересчитать подпись. Его останавливают права ОС и защита storage,
а не этот HMAC.
Formula без cache, неиндексируемые OOXML parts/containers, nested/merged ambiguity и
любой budget снимают completeness. Valid Office, который не помещается в canonical
whole-record JSON, никогда не откатывается к legacy raw wrapper. Exact count/list
не вызывает модель; ложный исчерпывающий model output на другом пути отбрасывается
вместе с подготовленными file, voice и Knowledge attribution carriers.

`user_model` остаётся только фоновым ориентиром и не лицензирует факты о человеке. Для person-intent без Knowledge Object, допустимого person-tool, attachment или graph evidence свободный модельный текст отбрасывается до verifier/persistence; одновременно очищаются подготовленные моделью file, voice и Knowledge attribution carriers. Структура отвечает, что подтверждённых данных недостаточно. Это fail-closed граница против досье, сгенерированного из весов модели при нулевой retrieval confidence.

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

`FRIDAY_WEB_ALLOW_PRIVATE_NETWORKS=1` отключает часть защиты и должен использоваться только в изолированной сети при явной необходимости.

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

`FRIDAY_CODE_EXECUTION_ENABLED=0` — обязательный безопасный default.

Встроенный executor ограничивает timeout, output и часть environment, но остаётся обычным subprocess на том же OS/container. Он **не** является надёжной sandbox boundary против злонамеренного Python-кода.

При timeout или превышении `FRIDAY_CODE_EXECUTION_MAX_OUTPUT_BYTES` Friday завершает всё созданное executor-ом process tree (POSIX process group / Windows `taskkill /T`), чтобы дочерний процесс не продолжил работу после уже отклонённого tool call. Stdout/stderr читаются потоково в общий budget. Это защита отказоустойчивости, а не замена песочницы.

Для реального code execution нужен отдельный disposable sandbox service: rootless container/VM, seccomp, no network, read-only image, tmpfs quota, PID/memory/CPU limits и одноразовый workspace. До этого момента не выдавайте `code.run` пользователям.

## 10. Host Capability Plane

Host Control — опциональная Linux-возможность и по умолчанию отсутствует из
runtime tool surface (`FRIDAY_HOST_CONTROL_ENABLED=0`). Она разделена на три
границы доверия:

- backend в контейнере выбирает reviewed adapter, проверяет actor/policy,
  фиксирует immutable plan, approval и durable job; host shell у него нет;
- `friday-host-agent` работает непривилегированным desktop-пользователем,
  принимает только версионированные HMAC-запросы через Unix socket и запускает
  закрытые argv в отдельных ограниченных `systemd --user` cgroup;
- root-owned `friday-package-broker` принимает только expiring exact APT
  transaction. В первом релизе policy допускает лишь `nmap`; API для shell,
  записи файлов, репозиториев, ключей и произвольных apt options отсутствует.

Package mutation включается отдельным
`FRIDAY_HOST_PACKAGE_INSTALL_ENABLED=1`. Любая установка требует человеческого
approval, связанного с точными версиями, зависимостями, origin, размером,
continuation и digest плана. Broker повторно симулирует transaction перед
commit; drift прекращает исполнение. Строка approval ID не является доверием:
backend authorization boundary выдаёт короткоживущий Ed25519 proof, связанный с
точными plan/actor/own/job/idempotency/expiry, а root broker независимо проверяет
его и атомарно с execution claim фиксирует защиту от replay. Private seed доступен
backend только через отдельный memberless supplemental GID; перед чтением seed
процесс обязан подтвердить `PR_SET_DUMPABLE=0`, поэтому обычный same-UID host
process не может извлечь ключ через ptrace или `/proc`. Его receipt подписан отдельным Ed25519
ключом, а приватный signing seed не покидает root service. Потеря ответа после
admission фиксируется как `unknown`: автоматический повтор запрещён, сначала
нужен exact status/reconciliation.

Ожидание backend action slot ограничено по времени и не переводит durable job
через границу `request_sent`: до захвата slot он остаётся `planned` либо
`awaiting_approval`. Saturation/cancellation закрывает job как доказанный
pre-effect отказ. После backend crash уже claimed approval разрешено продолжить
только при exact совпадении неизменяемого job/plan/actor и состоянии до send;
`running`, `unknown` и terminal jobs автоматически не переисполняются.

Обычный network adapter принимает только code-owned профили `discover`,
`services`, `selected_ports`, точные IP/CIDR из configured policy и не принимает
raw flags/NSE. Public scope выключен отдельно. Raw XML/stdout/stderr остаются в
actor-scoped bounded evidence; модель получает только проверенную структурную
проекцию и coverage. Контейнеру не монтируются `/home`, `/etc`, host executables,
system D-Bus или Docker socket. Установка и rollback описаны в
[`deploy/host-control/README.md`](../deploy/host-control/README.md).

Локальный `jq` adapter принимает только opaque Raw file ID текущего владельца и
закрытый список field paths. Backend повторно авторизует и хеширует исходные
байты, создаёт private exact workspace grant, а adapter сам строит jq program;
модель не передаёт executable, host path или jq expression. Установка `jq` через
root broker в этом релизе не разрешена: action доступен только для уже
package-attested `/usr/bin/jq`.

Compose-интеграция Host Control использует только rootful Docker и явный
`userns_mode: host`: backend собирается и запускается с числовыми UID/GID
выбранного непривилегированного desktop-пользователя. Поэтому owner-only
socket directory `0700`, socket `0600` и HMAC key `0600` остаются закрытыми, а
agent принимает ровно тот же UID через `SO_PEERCRED`; host root, group fallback
и произвольный UID в allowlist не допускаются. Rootless/subordinate-ID mapping
для этого override не поддерживается и не является поводом расширять mode.
Parent socket-каталог создаётся systemd-tmpfiles независимо от agent unit:
остановка agent удаляет socket, но не bind-source, поэтому backend сохраняет
fail-soft startup и показывает authenticated transport как disconnected.

## 11. Секреты

- `.env`, `.env.local`, токены и bridge secret исключены из Git/архива.
- Не выводите их в screenshots. Стандартный logging formatter дополнительно редактирует известные credentials, Authorization values и Telegram token внутри URL/traceback, но это defense in depth, а не разрешение логировать секреты намеренно.
- **Приватность запросов в логах**: access-log uvicorn проходит через фильтр, срезающий query string (`/api/search?[stripped]`) — поисковые запросы и browse-фильтры суть персональные данные, которые редактор секретов распознать не может. `web_surfer` логирует только hostname и класс исключения — `str()` httpx-ошибок содержит полный URL с параметрами поисковых провайдеров.
- **Доменные фильтры поиска тоже приватны**: `include_domains` проходит тот же локальный outbound gate в исходном IDN-написании и canonical punycode, а `exclude_domains` вообще не покидает процесс. В append-only audit оба списка представлены только count/length и installation-keyed aggregate ref; сырых доменов и обратимого plain SHA там нет. Provider hints не являются границей доступа: каждый HTTP(S) hostname повторно проверяется внутри Friday, unknown/relative/opaque URL fail-closed отбрасываются.
- **Все application-логи content-free by construction.** В них разрешены фиксированный event, числовые счётчики, жёстко заданные enum, класс ошибки и обезличенный hostname. Запрещены текст/запрос/ответ, URL, имя файла и путь, user/chat/callback/entity ID, `str(exception)`, traceback, `exc_info`, `stack_info` и произвольный `extra`. AST-contract проверяет все logger aliases и inline `logging.getLogger`; synthetic caplog-тесты подставляют длинные маркеры в exception/query/backend response.
- Secret hygiene ищет только точные известные этому процессу credential values и никогда не повторяет значение в finding, включая случай, когда оно встроено в filename или имя переменной. Scan ограничен path/byte budget, честно отмечает неполный охват и не открывает live main/bridge SQLite, WAL/SHM/journal/lease либо hardlink к их текущему inode.
- Храните backup secrets отдельно и зашифрованно.
- После подозрения на утечку одновременно смените API token, bridge secret и Telegram bot token.
- Перезапустите backend/bridge после ротации.

## 12. Audit

Audit log фиксирует actor, action, target, before/after, request ID и IP для административных изменений и tool invocations, а также для событий выгрузки данных и чтений чужих данных:

- **Egress логируется всегда**: скачивание файлов (`admin.file.download`, user-scoped `file.download`), резервных копий (`admin.backup.download` — копия содержит данные всех tenant'ов), экспортов (`admin.export.download` — именно скачивание, не только создание) и чтение самого аудит-лога (`admin.audit.read`).
- **Cross-tenant чтения** (админ читает контент чужого аккаунта: знания, inbox, сущности, диалоги, сообщения, файлы) логируются с целевым пользователем в `target_id`. Чтение собственных данных не логируется — иначе владелец, листающий свою же админку, заполнил бы журнал шумом без privacy-сигнала.

**Отказ на границе — не одно событие, а два.** `auth.failed` означает «не пустили»: неверные или неразобранные учётные данные, отказ по возможностям, исчерпанный бюджет неудачных попыток. `request.throttled` означает «своего придержали»: ограничитель частоты сработал ПОСЛЕ успешной аутентификации, актор действителен и назван по имени. Ни то ни другое не хранит предъявленный секрет: остаются allowlisted причина/статус, точный canonical IP только с in-process provenance наблюдаемого ASGI peer и непрозрачная ссылка на request ID; raw path и заголовок запроса в audit не попадают. Равная по форме строка прямого caller-а становится HMAC ref, а не доказательством адреса.

Разделение появилось по замеру на живой установке: из 1302 записей `auth.failed` **1188** были собственной массовой работой владельца (разбор Inbox пачкой с верным токеном с 127.0.0.1), ещё 89 — обращениями к `/health`, пути, которого не существовало. Диагностика при пороге 60 за сутки кричала «возможен брутфорс» постоянно, а **три** настоящих обращения с чужого адреса лежали под этой лавиной невидимыми. Сигнал безопасности, который горит всегда, перестаёт быть сигналом; поэтому `/health` теперь публичный синоним `/api/health` (он и так был публичен), а троттлинг вошедшего пользователя считается отдельно и в порог брутфорса не входит.

**Append-only обеспечивается на уровне БД**: триггеры `audit_log_no_update`/`audit_log_no_delete` (`RAISE(ABORT)`) отклоняют любую попытку изменить или удалить строку журнала — включая будущие баги кода. Purge, удаление диалогов и экспорт журнал не трогают. Это по-прежнему не криптографически неизменяемый ledger: администратор с прямым доступом к файлу SQLite может удалить триггеры; для tamper evidence можно позже добавить hash chaining и периодическую подпись checkpoint внешним ключом.

Append-only не означает «разрешено навсегда сохранить payload». Перед каждой
записью storage-boundary приводит **всю строку** к ограниченной типизированной
проекции: `action`/`target_type` имеют точные allowlist; actor обязан существовать
в локальных users; audit/target IDs сохраняются точно только с in-process generated
marker либо доказанным существованием в authoritative table; IP — только с marker
ASGI peer; created-at обязан быть offset-aware. Текст, запрос, код и URL в
`before`/`after` заменяются длиной и domain-separated keyed fingerprint;
filename/path — длиной и suffix; произвольные ключи и идентификаторы не проходят.
Это единственная обязательная граница, поэтому правило действует и при прямом
вызове storage в обход HTTP-хелперов.

Request ID, private target label, unproven IP/structural ID и fingerprint любого
content/query/code/URL получают стабильную домен-разделённую ссылку через
installation-local 256-bit HMAC key. Обычный SHA для короткого prompt, PIN, URL или
имени не используется: читатель audit-log не должен иметь возможность перебрать
словарь и восстановить значение. Ключ лежит только в локальном `schema_meta`, не
входит в user export; его отсутствие или порча останавливает startup. Одинаковые
ссылки остаются сопоставимы только внутри своего домена и установки, исходная
строка — нет.

При первом открытии старой БД миграция `audit_payload_privacy=v3` переписывает
прежние JSON и scalar columns под тем же контрактом; v2 plain SHA fingerprints
переиздаются как keyed refs. Она временно снимает только два append-only trigger
внутри `BEGIN IMMEDIATE`, включает и проверяет SQLite `secure_delete`, возвращает
точные канонические trigger definitions и требует успешный
`wal_checkpoint(TRUNCATE)`. Любой сбой откатывает строки и triggers; startup
остаётся fail-closed до завершённого checkpoint; pending-v3 retry не меняет уже
выданные opaque refs. Уже созданные автономные backup
файлы эта операция не меняет: восстановленная старая копия будет очищена при
открытии, но до удаления по отдельной retention-политике сам backup следует считать
содержащим прежние audit payload.

## 13. Контрольный список перед публикацией

- [ ] `jericho doctor` без критических ошибок.
- [ ] API token и bridge secret уникальны и длиннее 32 символов.
- [ ] `FRIDAY_API_REQUIRE_TOKEN_ON_LOOPBACK=1`.
- [ ] `FRIDAY_TRUST_PROXY_HEADERS=0`, если нет доверенного proxy.
- [ ] При доверенных proxy перечислены только непосредственные hop-ы в `FRIDAY_TRUSTED_PROXY_NETWORKS`.
- [ ] CORS содержит только реальные origin.
- [ ] Для non-loopback bind включён native TLS либо отдельный TLS reverse proxy.
- [ ] SAN сертификата покрывает browser-facing адрес и внутренний hostname/IP
      bridge; публичный CA/cert установлен как trust anchor без private key.
- [ ] HTTPS health/identity проверены без `-k`/`verify=False`.
- [ ] `FRIDAY_WEB_ALLOW_PRIVATE_NETWORKS=0`.
- [ ] `FRIDAY_ENGINEER_MODE_ENABLED=0`, пока точный candidate commit не прошёл
      полный `python tools/quality_gate.py`, Engineer contract tests и реальный
      startup smoke bubblewrap на целевом хосте.
- [ ] При включённом Engineer mode `/engineer`, видимость и исполнение tools
      доступны только владельцу установки (не shared-участнику с preset
      `owner`), а простое упоминание host/URL не запускает DNS или probes.
      Каждая сетевая операция требует явного активного запроса и единственной
      цели из текущей человеческой реплики, её code-pinned адресов и допуска
      exact `FRIDAY_HOST_ALLOWED_CIDRS`; public scope без operator flag и
      отдельного per-action HITL всегда отклоняется. Явный URL-порт
      остаётся точной частью подписанного scope, а цель без явного порта может
      использовать не более 64 выбранных портов того же хоста. Потерянный
      терминальный результат после входа в сетевой action фиксируется как
      `uncertain`, а не как отсутствие probes.
- [ ] На целевом хосте подтверждены no-network sandbox артефактов, отказ для
      неоднозначной/запрещённой цели и отсутствие exploit payloads; пройден
      rollout smoke из [`docs/ENGINEER_MODE.md`](ENGINEER_MODE.md).
- [ ] Host Control и package install остаются `0`, пока exact candidate не прошёл
      host-control contract/deployment tests и Ubuntu smoke. При включении
      socket/key/job-root canonical, agent непривилегирован, broker policy
      допускает только reviewed packages, а `unknown` jobs reconciled без replay.
- [ ] `FRIDAY_CODE_EXECUTION_ENABLED=0`.
- [ ] Backend bind только loopback/VPN/private interface.
- [ ] vLLM не опубликован наружу.
- [ ] Есть проверенная резервная копия БД и отдельная копия raw files.
