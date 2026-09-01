# Эксплуатация и диагностика

> Проект переименован: **Friday** (по-русски — **Пятница**), ex codename Jericho.

## 1. Конфигурация и первый запуск

Direct Python автоматически читает `./.env.local` либо путь из `FRIDAY_ENV_FILE`. Уже заданные environment variables имеют приоритет.

```powershell
jericho init --home D:\jericho
jericho --env-file D:\jericho\.env.local status
```

`init` создаёт runtime-каталоги и `.env.local` атомарно, с private permissions best-effort. Существующий symlink не перезаписывается даже с `--force`.

У установки, которая продолжает использовать прежний файл `jericho.sqlite3`, задайте
`FRIDAY_DATABASE_PATH` явно. Для боевого сервиса дополнительно задайте
`FRIDAY_DATABASE_MUST_EXIST=1`: отсутствующий или пустой файл тогда останавливает
запуск до открытия SQLite, а не превращается в новую пустую базу. Если рядом лежат
две разные непустые базы `friday.sqlite3` и `jericho.sqlite3`, автоматический выбор
всегда завершается ошибкой и требует явного пути.

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

`status` печатает короткую сводку и конкретные действия. `doctor` возвращает тот же snapshot в подробном JSON, удобном для тикета или автоматической проверки. Если backend уже владеет process lease, внешний CLI не открывает его SQLite даже `mode=ro`: snapshot приходит от локального authenticated API. Недоступность API при живом lease даёт честный `degraded` без SQLite fallback. При остановленном backend offline-проверка сама удерживает тот же lease на всём окне чтения, поэтому стартующий backend либо ждёт, либо выигрывает до первого SQLite-open.

### Obsidian Android beta

Ветка содержит opt-in контракт `FRIDAY_OBSIDIAN_ENABLED=1`,
`FRIDAY_PUBLIC_BASE_URL=https://...` и `FRIDAY_WORKERS_ENABLED=1`; без флага
Organ выключен. Public URL должен быть HTTPS origin без
credentials/path/query/fragment. Syncthing ожидается в полуинтервале
`[2.1.3, 2.2.0)`, а `FRIDAY_OBSIDIAN_ROOT` — owner-private каталог mode
`0700`, отдельный от state/files/models/cache/logs/backups. Unix socket GUI/REST
и relay/discovery topology не требуют публиковать Syncthing GUI/sync ports.

Immutable release operator связывает mode/root/env с activation identity и
включает SQLite, Telegram inbox и точный Obsidian root в единый проверяемый
recovery set. Обычный disaster-recovery snapshot остаётся остановленной внешней
процедурой. Физическая Android-приёмка ещё не выполнена. Полные env-границы,
Telegram/Syncthing-Fork steps, смысл статусов и acceptance-чеклист см. в
[OBSIDIAN_ANDROID.md](OBSIDIAN_ANDROID.md).

Первое включение в живой immutable-install не делается ручным изменением
канонического `.env.local`. Сначала schema-35 `rc1` принимается с Obsidian в
режиме `disabled` и с точными bytes исходного `ENV0`. Затем рядом с activation
journal в owner-private `state_dir` готовится отдельный `ENV1`: exact `ENV0`
плюс утверждённые Obsidian-параметры. Финальный `activate` получает одновременно
`--env-file-sha256 <ENV0_SHA256>`,
`--terminal-journal-env-sha256 <ENV0_SHA256>`, `--next-env-file <ENV1>` и
`--next-env-file-sha256 <ENV1_SHA256>`. Оператор проверяет оба конфига до
остановки writers, сохраняет канонический `ENV0` до полностью проверенной копии
SQLite/WAL, inbox и exact Obsidian root, после чего атомарно и durable публикует
`ENV1`. При успехе staged-файл удаляется и это отсутствие fsync-подтверждается;
при crash продолжение выполняется только `recover-activation` в identity из
journal, без ручного запуска systemd или восстановления отдельных файлов.

### Optional GPT-OSS secondary brain

Релизы 0.207.11–0.207.22 ввели default-off поддержку и provisional public
`shadow/extract`. В 0.207.23 complete physical/profile chain принят: code-owned
accepted-реестр содержит ровно exact finalist с accepted-manifest SHA-256
`93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`,
а provisional-реестр пуст. Первый accepted release сохраняет public
`shadow/extract` без private text: результат выбрасывается; ответ, tools,
effects и publication остаются за primary. Регистрация профиля сама по себе не
открывает private shadow или `assist`.

В live 0.207.24 развёрнут отдельный private shadow:
`ALLOW_PRIVATE_TEXT=1` при `mode=shadow` и `workload=extract`.
Typed secondary output валидируется и выбрасывается; ответ, tools,
effects и publication остаются только у primary.

Live 0.207.26 завершил distinct `mode=shadow→assist` cutover
через свежий `product-stage --stage private-shadow` receipt и
`secondary_shadow_to_assist`. В assist только валидированная
typed Inbox extraction может заменить primary extraction; knowledge
write всё равно требует review, а tools, effects и publication
недоступны. Любая ошибка ведёт в exact primary fallback; отсутствие
ноутбука не меняет ответственность primary.

Следующее расширение `document_map` использует отдельную code-owned product
policy `gptoss20b-document-map-v1` (SHA-256
`7d57947d7ecda675e8a4da3f56332baf32484c08c0504afd7fa420b9c6323cd9`).
Оно не меняет Windows/SGLang engine, profile ID, served-model alias или
gateway manifest `93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`;
перезапуск контейнеров ноутбука для этого rollout не нужен. Secondary получает
только bounded text-only MAP/REDUCE leaf, возвращает strict JSON `summary`, не
видит tools и не публикует ответ. Primary остаётся финальным синтезатором, а
любой отказ/таймаут/невалидный JSON запускает прежний primary map ровно один
раз.

Расширение включается distinct-candidate activation, без ручной правки live
env. Из текущего exact `assist/extract` добавьте
`FRIDAY_SECONDARY_LLM_WORKLOADS=document_map,extract` и
`FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE=shadow`, затем используйте transition
`secondary_assist_enable_document_map_shadow`. В shadow-фазе primary MAP
остаётся пользовательским результатом, а secondary результат валидируется и
выбрасывается. V1 не допускает assist.
Owner diagnostics показывает отдельный
`workloads.document_map.routing_mode` и content-free counters этой ступени.

Promotion gate принимает natural
owner document-map shadow через product seam может создать content-free
receipt, однако promotion-grade receipt выдаёт только одноразовый same-process
`POST /api/admin/secondary-document-map-witness/observe-shadow` с owner token и
пустым body. Он синхронно проводит реальный `DOCUMENT_MAP`, доказывает exact
`selected+1/success+1/shadow.valid+1`, неизменность invalid/skipped/in-flight и
сохранение primary sentinel ровно за один вызов. Product/Telegram/документы при
этом не создаются.

`$FRIDAY_STATE_DIR/secondary-document-map-shadow-receipt.v1.json` имеет mode
0600 и не содержит текст/ответ модели, их digests или cumulative counters.
Подписанная аттестация привязана к PID/epoch, profile/CA/policy и точному sealed
predecessor: commit, release-tree/metadata/wheel SHA-256, live ENV и lexical
ENV/anchor path SHA-256. Полная tree-проверка выполняется до model attempt и ещё
раз перед durable receipt. Receipt действует не более часа; identity drift,
failed one-shot, non-exact replay или несовпадение durable one-shot latch
закрывают consume. Потерянный consume-ответ можно получить повторно только тем
же exact request/candidate: сервер восстанавливает идентичный подписанный ответ
без повторной мутации. Consumed receipt/tombstone не удаляется общим prune и не
заменяется последующим natural shadow; новый PID/epoch может выпустить новый
current receipt, сохранив прежние consumed audit rows. Оператор полностью
перепроверяет sealed predecessor tree и до, и после consume.

Release `0.207.31` связывает canonical v2 policy SHA-256
`d2ab9b67ff24a54727fec9592dcd0db1c35036e1b5ee91ac6a5daf4d3694e92e`
с exact live-shadow receipt SHA-256
`a00f18f8c50a7449d1fa6a357d8d5bb1ca37b0c397c81a96c0e621231bc09e2d`.
Accepted manifest SHA-256 —
`933c671759724e36fe686185aa8ad03fa09f90e26e3095900796707cfef36855`;
raw receipt и lookup token в source отсутствуют. Только distinct commit и
atomic owner-only consume могут сменить ровно
`DOCUMENT_MAP_MODE=shadow→assist`; ENV/CLI не могут подменить эти digests.

В `0.207.33` GPU-дорога также включается для полного текста обычного текущего
документа: bare upload и естественные RU/EN запросы summary/review/analysis/compare
дают до восьми byte-safe MAP-вызовов под общим 15-секундным deadline. После них
выполняется ровно один primary final. Oversize, laptop-off, timeout или malformed ответ
дают прежний primary-only путь. Обычный диалог, web-search и final не публикуются secondary.

Source 0.207.27 не расширяет полномочия secondary: bounded Inbox
extraction запрашивает точную code-owned JSON Schema и по-прежнему
проходит downstream validation. Malformed, truncated или unavailable
secondary даёт exact primary fallback. Веб-поиск и ревью документов
в этот workload не входят.

На ноутбуке установлен и с 0.207.24 включён fail-closed at-logon
gateway publication recovery. После готовности LAN, Docker и exact healthy
`friday-secondary-gateway` он требует два последовательных совпавших
доказательства отсутствия exact `192.168.1.35:8443` publication и TCP
listener. Только тогда допустим один restart этого gateway; несогласованные
наблюдения закрывают recovery, а model container никогда не перезапускается.

В owner diagnostics `endpoint_admission_total` считает занятые client permits,
а `endpoint_request_total`/`endpoint_success_total` — созданные физические HTTP
tasks и ответы с допустимым HTTP status. Поэтому cold product recovery имеет
три admission, но exact endpoint delta `4/4`: profile manifest, `/models`,
generation canary и product request. Новый контракт выпускает только
`friday.secondary-product-diagnostics.v2` внутри
`friday.secondary-product-stage-evidence.v3`; прежние v1/v2 receipts не
считаются эквивалентными и отклоняются.

После приёмки самого default-off релиза оператор может подготовить
owner-private ENV1. Это не отдельный env-файл: скопируйте exact ENV0 вместе с
завершающим newline и добавьте в его конец следующий canonical sorted/unquoted
LF-блок. Все secondary-ключи должны встретиться ровно один раз. Кроме непустого
64-hex gateway token и absolute path к owner-private копии exact CA, блок должен
совпадать побайтно по значениям:

```dotenv
FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC=0.10
FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0
FRIDAY_SECONDARY_LLM_API_KEY=<64-hex-gateway-token>
FRIDAY_SECONDARY_LLM_BASE_URL=https://192.168.1.35:8443/v1
FRIDAY_SECONDARY_LLM_CALL_BUDGET_SEC=15.0
FRIDAY_SECONDARY_LLM_CA_FILE=/absolute/private/path/friday-secondary-ca.crt
FRIDAY_SECONDARY_LLM_CONNECT_TIMEOUT_SEC=1.0
FRIDAY_SECONDARY_LLM_COOLDOWN_SEC=60
FRIDAY_SECONDARY_LLM_ENABLED=1
FRIDAY_SECONDARY_LLM_HEALTH_INTERVAL_SEC=30
FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY=1
FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS=4096
FRIDAY_SECONDARY_LLM_MODE=shadow
FRIDAY_SECONDARY_LLM_MODEL=friday-secondary-gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f
FRIDAY_SECONDARY_LLM_PROFILE=gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f
FRIDAY_SECONDARY_LLM_READ_TIMEOUT_SEC=12.0
FRIDAY_SECONDARY_LLM_WORKLOADS=extract
```

CA должен иметь SHA-256
`392756a74fd9100635c42f4fbf7e5a5f1822d18ea898ebb7848b9fdd0bddc1fe`.
Положите ENV1 как owner-private regular file непосредственно в `state_dir` и
публикуйте его только normal distinct-candidate `install-units`/`activate`
path. Для `activate` обязательны `--env-file-sha256 <ENV0_SHA256>`,
`--terminal-journal-env-sha256 <ENV0_SHA256>`, `--next-env-file <ENV1>`,
`--next-env-file-sha256 <ENV1_SHA256>` и
`--staged-config-transition secondary_shadow_enable`. Уже развёрнутый candidate
повторно использовать нельзя; ручная правка живого `.env.local` не является
cutover path.

Проверьте `/api/health`: в default-off режиме `status=ok`,
`secondary.mode=disabled`, `secondary.state=disabled`. После отдельной
activation ENV1 и успешного exact TLS/profile/model probe админская
`/api/admin/diagnostics` должна показать exact profile,
`profile_admission=accepted`, `state=healthy` и `available=true`.
Одной доступности ноутбука недостаточно. Его отсутствие не меняет `status=ok`
и ведёт к обычному primary-only path.

ENV1 в accepted public-shadow release не получает private Inbox text и не
заменяет реальный product shadow. Для следующего distinct release постройте
ENV2 из exact ENV1, изменив только
`FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1`, и проведите новую
distinct-candidate activation с
`--staged-config-transition secondary_shadow_to_private_shadow`. В эту же
команду обязательны абсолютный путь к свежему owner-private автоматическому
`product-stage --stage public-shadow` receipt и его exact digest:
`--secondary-rollout-receipt <RECEIPT>` и
`--secondary-rollout-receipt-sha256 <SHA256>`. Это
единственная ступень для реального private Inbox shadow: typed output
валидируется и выбрасывается, а ответ по-прежнему строит primary.

Просроченная demand-admission freshness не блокирует product-stage.
В public shadow runner отдельно пробирует exact TLS/profile endpoint и
доказывает ожидаемый privacy rejection. В private shadow и assist
stale admission допустима только в pre-call snapshot: product-call должен
завершиться fresh admitted post-call state. Exact manifest, privacy и
product-counter gates при этом не смягчаются.

Только после принятого private-shadow evidence постройте ENV3 из exact
ENV2, изменив только `FRIDAY_SECONDARY_LLM_MODE=assist`, и проведите
`secondary_shadow_to_assist`, передав теми же двумя флагами свежий receipt
`product-stage --stage private-shadow`: прямой ENV1→assist отклоняется —
private-text admission и передача авторитета не могут произойти одним
переходом.

Rollout receipt живёт не более 570 секунд. Оператор сверяет predecessor,
candidate, release trees, staged ENV и sealed runner, затем одноразово сжигает
server attestation до первой мутации. Потеря consume-ответа или неудача после
consume не разрешает повтор с тем же receipt: выполните новый exact
`product-stage` из чистого checkout активного predecessor и начните activation
с новым candidate.

Для планового выключения public или private shadow после terminal
`clear` создайте disabled ENV из exact текущего, изменив только
`FRIDAY_SECONDARY_LLM_ENABLED=0`, и выполните новую distinct-candidate
activation с `secondary_shadow_disable`; privacy bit должен
сохраниться. Из assist используйте `secondary_assist_to_disabled`.
Для document-map shadow этот же assist-disable сохраняет exact workload и
`DOCUMENT_MAP_MODE`, меняя только `ENABLED=1→0`.
При unfinished activation не запускайте disable: продолжайте только через
`recover-activation` в identity существующего journal.

### GPT-OSS semantic supervisor: default-off shadow и evidence-gated assist

Semantic supervisor использует уже принятый optional GPT-OSS-20B как
недоверенный planner/reviewer. Ноутбук не становится backend, storage, tool
kernel или publication owner. Primary Qwen 27B формирует единственный итоговый
ответ, а code-owned Policy Kernel, permissions и durable graph сохраняют
authority. Текущий source содержит P1 shadow и один P2–P4 journey, но canonical
конфигурация остаётся `off`; production assist/canary нельзя считать принятым
без нового live production-joined evidence.

Закрытый P1–P4 ENV-блок состоит из 13 ключей; независимые три P5-ключа
перечислены ниже:

```dotenv
FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS=1
FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS=6
FRIDAY_SEMANTIC_SUPERVISOR_MODE=off
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS=
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED=0
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE=
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256=
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE=
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256=
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256=
FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256=
FRIDAY_SEMANTIC_SUPERVISOR_TASKS=
FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC=12
```

Unknown mode, partial/unknown key set, invalid bound, policy/profile drift,
missing evidence or scheduler failure closes the supervisor. Не добавляйте
`plan_candidate` в `FRIDAY_SECONDARY_LLM_WORKLOADS`: это независимый
code-owned overlay accepted secondary runtime.

Shadow использует policy `gptoss20b-semantic-supervisor-v1`, SHA-256
`edea7fce6ae8d9bfcbe461a3f90d98bd9aab897ebe7712cdb23a2d77e8de780c`,
`MAX_STEPS=6`, `MAX_REVIEW_ROUNDS=0` и одну либо обе task classes
`compare_current_file_with_current_web`,
`compare_archive_with_current_web`. Proposal вызывается после успешного primary
и выбрасывается; laptop-off, timeout, saturation и malformed output не меняют
primary path. Для synthetic regression используйте:

```bash
.venv/bin/python -I -B tools/evaluate_semantic_supervisor_offline.py \
  --fixtures tests/fixtures/semantic_supervisor_offline_v1.json
```

Этот отчёт не является live promotion evidence.

Assist/canary используют отдельный policy
`gptoss20b-semantic-supervisor-v2`, SHA-256
`95dc4ae7e246e7104b1e1cd036ea9706fdb014de6889d69789fca66cec9fd98b`,
ровно `MAX_STEPS=6`, `MAX_REVIEW_ROUNDS=1` и только
`compare_current_file_with_current_web`. Обязательны:

- `PROMOTION_ENABLED=1` и exact requested mode;
- owner-private mode-0600 promotion evidence
  `friday.supervisor-assist-promotion.v5` и его raw SHA-256;
- accepted latency budget
  `friday.semantic-supervisor-latency-budget-document.v1` и его SHA-256;
- exact candidate source revision и capability-registry binding;
- для canary — 1–32 sorted unique actor-binding SHA-256;
- для assist — пустой actor allowlist;
- minimum one genuine joined observation, exact trace coverage, zero hidden
  owners, duplicate capability/effect/publication и user-visible regressions;
  20 observations remain the useful operating target, not an availability
  gate, and synthetic traffic is never promotion evidence;
- baseline file/report, operator-attestation provenance; canary evidence также
  ссылается на canonical digest установленного predecessor assist evidence.

Создавайте evidence только pure producer-ом
`tools/build_semantic_supervisor_promotion_evidence.py`: он проверяет
canonical baseline, latency budget и typed operator attestation, создаёт новый
файл без overwrite/symlink и печатает body-free receipt. Ручной JSON не является
доказательством.

Immutable operator допускает только exact reversible transitions:

- `semantic_supervisor_shadow_enable` / `semantic_supervisor_shadow_disable`;
- `semantic_supervisor_shadow_to_assist` / `semantic_supervisor_assist_to_shadow`;
- `semantic_supervisor_assist_to_canary` / `semantic_supervisor_canary_to_assist`.

Staged ENV должен быть owner-private regular file mode `0600` в `state_dir`, а
прочие ENV и accepted secondary block — побайтно неизменными. Ручная правка
live `.env.local` не является rollout или rollback. Shadow→assist требует
readiness evidence; assist→canary — новый outcome evidence, exact predecessor
chain и canary allowlist. Health и installed source seam проверяются до и после
transition; выключенный ноутбук допустим, подмена source/evidence/policy — нет.

Promoted journey распознаётся только при exact current-turn attachment, явном
current-web query, dialogue mode, claimed idempotency request и разрешённых
read-only capabilities. Admission связывает request binding, sealed plan,
actor/conversation, current Raw source/content и fixed adapters:

```text
files.read -> friday.orchestration.file_read.V12FileReadHandler
web.search.current -> web.compare.transient /
                      TransientWebComparisonAdapter.research
primary.synthesis -> attested primary model
```

Graph schema v2 хранится в DB schema 45. Он допускает две parallel read ветви,
одну primary synthesis и не более одного review + одного code-admitted web
recovery. Publication повторно проверяет authority/source и атомарно создаёт
ровно один primary-owned assistant result и body-free receipts. Web evidence
остаётся transient: legacy `web.research` и его capture/mutate contour здесь не
используются.

ACTIVE overlap классифицируется до ingestion как `ROOT_REPLAY`, `NEW_TURN`,
`EXPLICIT_CANCEL` или `UNCERTAIN`. Только `NEW_TURN` разрешает ordinary
ingestion/primary; replay, cancel и uncertainty остаются side-effect free до
fresh exact graph check. Cancellation связывается с новым request-effect fence.
При accepted promoted composition startup восстанавливает exact personal
principal/current source, заново строит closed plan и CAS-rebind-ит прежний
ACTIVE graph к новому процессу; повторной ingestion, legacy fallback и второй
publication нет. Lost rebind acknowledgement сверяется с durable state.
Недоступный или stale recovery surface оставляет graph владельцем и блокирует
overlap. В `off|shadow` либо при failed promoted composition используется
authorized terminalization до начала traffic. Expiry остаётся bounded worker
path, а schema44 migration использует explicit unbound sentinel и никогда не
выдумывает replay identity.

Проверяйте body-free `/api/health.secondary.semantic_supervisor`, top-level
`/api/health.semantic_supervisor` и owner diagnostics. Для promoted mode
top-level status должен показывать exact requested/effective mode, fresh
promotion admission, policy/profile/source/registry/evidence identities,
durable graph counts и закрытые authority flags. Проекции не содержат body,
prompt, query, path, actor/conversation ID или endpoint error text.

P5 имеет отдельный default-off контур и не включается режимами P1–P4:

```dotenv
FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE=
FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256=
FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_MODE=off
```

`shadow` допускается только при `FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1`,
accepted profile/policy, exact private mode-0400|0600 maturity artifact и совпадении
его source revision + прежнего read-registry binding с установленным релизом,
а также отдельного effect-registry binding с реально enabled и зарегистрированным
Obsidian `create|append` контуром.
Artifact строится подкомандой
`tools/build_semantic_supervisor_promotion_evidence.py effect-maturity` из exact
production baseline, финального CANARY promotion bundle и CANARY latency
budget. Producer ничего не активирует и не перезаписывает существующий файл.
До сборки `effect-maturity` тот же tool с подкомандой
`effect-registry-binding` выдаёт body-free code-owned expected SHA; его передают
как `--effect-registry-binding-sha256`. Backend независимо принимает этот SHA
только после проверки фактических settings, AuthorizationService, ExecutionKernel,
ObsidianRuntime и обоих зарегистрированных write tools.

Rollout выполняется только immutable transitions
`semantic_supervisor_effect_shadow_enable` и
`semantic_supervisor_effect_shadow_disable`. Enable обязан проверить полный
artifact до остановки сервиса; unrelated, primary, secondary и P1–P4 ENV bytes
не меняются. Health gate связывает active runtime с configured evidence SHA,
maturity facts, installed source revision, read registry binding и отдельным
live effect registry binding. При rollback к
legacy predecessor оператор использует его прежний health contract, а не ждёт
несуществующую P5-проекцию.

После durable accepted Obsidian outcome wrapper возвращает exact primary result
и может только сравнить уже совершённый `create|append` с symbolic ответом
`none|create|append` независимой lowest-priority lane. В persisted observation
нет request text, raw digest, path, IDs или model body; выполнение, replay,
compensation и publication всегда запрещены. Laptop-off, evidence/runtime
ошибка или saturation только пропускают observation и не являются boot gate.
Один accepted effect/outcome dispatch-ится не более раза за process lifetime:
dedupe атомарно связывает оба digest до model call, а fixed non-rotating Bloom
может дать только безопасный пропуск optional observation.

P6 current inventory возвращает `NO_ELIGIBLE_CANDIDATE`: две поверхности —
deterministic invariants, ещё две — mixed legacy. Source-only evidence и
preimage rollback не могут выдать production deletion authority; никакой код
или config не удаляется. До появления отдельно reviewed semantic-only candidate
и trusted production+rollback evidence все четыре поверхности сохраняются.
Repository scan читает Git output с жёстким лимитом, проверяет blob size до
body load и последовательно освобождает AST каждого модуля; наружу выходит
только bounded aggregate receipt без путей и source bodies.

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

### Гигиена секретов

`jericho doctor` (и sentinel) ищут учётные данные самого Friday в посторонних файлах — сравнением по точному значению, а не по шаблону, поэтому ложных срабатываний нет: файл либо содержит этот токен, либо нет. Проверяются `$HOME` и `FRIDAY_HOME` на глубину 3: до 20 000 путей и 256 МиБ фактически прочитанных данных. Малые файлы идут раньше крупных независимо от порядка каталогов; крупные читаются chunks с overlap, а hardlink-inode — один раз с отдельным отчётом по каждому пути. File cap, byte cap и недоступный/неполный файл дают `secret_scan_incomplete`, а не ложное «чисто». Значение credential никогда не возвращается даже когда оно оказалось частью имени файла или переменной.

Main/bridge SQLite, WAL/SHM/journal, lease-файлы и их текущие hardlink-inodes исключены сильнее обычного protected-файла: scanner не делает над ними даже raw `open(2)`. Живой владелец SQLite остаётся единственным процессом, который читает эти артефакты.

Повод конкретный: живой токен бота двое суток пролежал в открытом файле на рабочем столе, и ничто этого не заметило. Резервные копии `.env.local` тоже считаются лишними копиями и попадают в отчёт.

## 3. Операционные команды

```bash
jericho model-check          # проверить эндпоинт модели генерацией, а не коннектом
jericho events               # журнал: что сломалось и починилось, пока никто не смотрел
jericho eval-bootstrap       # черновики золотого набора для оценки поиска
```

**`model-check`** пробует то, что реально ломает интеграцию: отдаётся ли настроенная модель, отвечает ли она в том бюджете токенов, который Friday использует, не протекает ли цепочка рассуждений в ответ, парсится ли вывод как JSON, есть ли эмбеддинги. TCP-коннект не доказывает ничего, а `/models` доказывает лишь, что сервер слышал про модель.

**`events`** — операционный журнал. Пишутся переходы, а не состояния: воркер, сломанный всю ночь, даёт две записи (`worker.failed`, `worker.recovered`), а не одну на каждый тик. Плюс `backup.created` на каждую копию. Журнал ограничен 2000 записями.

**`eval-bootstrap`** предлагает кейсы для золотого набора из имеющихся знаний, спрашивая у модели, каким вопросом человек искал бы данный объект. Каждое предложение проходит аудит на лексическое пересечение: вопрос, пересказывающий документ его же словами, отклоняется — иначе набор показывает отличный recall, проверяя только совпадение слов. Без `--save` ничего не сохраняется.

## 4. Процессы и singleton-гарантии

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

### Что происходит при штатной остановке

1. Отменяются asyncio-задачи воркеров. Это **не** останавливает уже запущенные блокирующие вызовы: `asyncio.to_thread` прекращает *ожидание*, поток живёт дальше.
2. Остановка **ждёт** завершения таких вызовов — по бюджету самих воркеров (самый длинный `timeout_sec`, сейчас 900 с, плюс запас), а не по константе. Прежние 30 с были короче, чем воркеру **разрешено** держать поток: `knowledge_dedup` сканирует до 600 с внутри такта в 900 с, и остановка бросала работу, которая законно продолжалась. Если по истечении бюджета что-то ещё выполняется — в лог уходит `WARNING` с перечнем.
3. Только после этого `storage.close(final=True)`.

`final=True` — это не косметика. Свойство `conn` прозрачно переоткрывает соединение, когда сменилось поколение (на этом держится восстановление из бэкапа), поэтому поток, переживший остановку, **молча получал новое соединение** и продолжал писать уже после того, как процесс отпустил `backend.lock` — а его к этому моменту мог занять новый backend. Теперь такой поток получает `StorageClosedError`. Обычный `close()` (без `final`) по-прежнему переоткрывается: на этом работает `restore-backup`.

### Логи детей `jericho up`

Супервизор пишет stdout/stderr каждого ребёнка в `<log_dir>/<имя>.log` и **проворачивает** файл, когда тот перерастает `FRIDAY_LOG_MAX_BYTES` (по умолчанию 16 МиБ), храня `FRIDAY_LOG_BACKUPS` поколений (`backend.log.1`, `.2`, `.3`). `0` в первой переменной выключает ротацию.

Ротация нужна не для порядка, а потому что рост реальный: мост опрашивает `GET /api/notifications/pending` каждые 15 с, и каждый опрос стоит строки в access-логе — `backend.log` набирает десятки мегабайт в сутки и без потолка однажды забьёт диск.

Проворот — **copy-truncate**, а не переименование: ребёнок держит унаследованный дескриптор на тот же inode, и после `rename` он продолжил бы писать в уведённый файл, а живой лог остался бы пустым до перезапуска. Дескриптор открыт с `O_APPEND`, поэтому усечение не оставляет дыры. Плата — несколько строк, попавших в окно между копированием и усечением; это ровно тот компромисс, что и у `logrotate copytruncate`.

### Native TLS под systemd --user

Native TLS переключает единственный uvicorn port целиком. Поэтому backend и bridge
обновляются как один контур, но с проверяемым checkpoint между рестартами. До
изменения `.env.local` сертификат должен иметь SAN как минимум для
`127.0.0.1`/`localhost` (либо `::1` для IPv6 bind) и для каждого имени либо IP,
который набирает браузер.
Проверяйте именно SAN, не только `CN`:

```bash
openssl x509 -in /absolute/path/server.crt -noout -subject -issuer -dates -ext subjectAltName
openssl x509 -in /absolute/path/server.crt -noout -checkip 127.0.0.1
openssl x509 -in /absolute/path/server.crt -noout -checkhost localhost
openssl x509 -in /absolute/path/server.crt -noout -checkhost LAN-NAME
openssl x509 -in /absolute/path/server.crt -noout -checkip LAN-IP
```

Private key должен принадлежать runtime-user и иметь режим `0600`; cert/CA —
публичные данные, но каталог TLS всё равно держите private. Cert/CA не должны быть
тем же файлом, symlink или hardlink, что key: validator отклоняет такой alias без
чтения содержимого. Затем задайте в том env-файле, который указан в
`ExecStart --env-file`:

```dotenv
FRIDAY_API_HOST=0.0.0.0
FRIDAY_SSL_CERTFILE=/absolute/path/server.crt
FRIDAY_SSL_KEYFILE=/absolute/path/server.key
FRIDAY_BACKEND_CA_FILE=/absolute/path/ca-or-self-signed-server.crt
FRIDAY_CORS_ORIGINS=https://127.0.0.1:8000,https://localhost:8000,https://LAN-NAME-OR-IP:8000
```

Для same-host systemd не задавайте `FRIDAY_BACKEND_URL`: при TLS bridge сам
выбирает `https://127.0.0.1:<port>`. Если override нужен, он обязан быть HTTPS и
его hostname обязан входить в SAN. `FRIDAY_BACKEND_CA_FILE` указывает только на
public CA/certificate — никогда на key.

Канонические 0.206 units называются `friday-backend.service` и
`friday-bridge.service`. Сначала переключите backend и докажите HTTPS health
через CA, только затем перезапустите bridge. Старые `jericho-*` units могут
встречаться только в pre-0.206 установках и не являются release path:

```bash
systemctl --user restart friday-backend.service
curl --fail --silent --show-error --cacert /absolute/path/ca-or-self-signed-server.crt \
  https://127.0.0.1:8000/api/health
systemctl --user restart friday-bridge.service
systemctl --user is-active friday-backend.service friday-bridge.service
FRIDAY_ENV_FILE=/absolute/path/.env.local jericho doctor
```

Не применяйте `curl -k`, `verify=False` или постоянное browser exception: такой
тест доказывает шифрование, но не identity сервера. Импортируйте публичный CA в
trust store устройства и проверьте Admin UI по каждому реальному LAN origin.

Rollback до рабочего HTTP выполняется тем же порядком: очистите одновременно
`FRIDAY_SSL_CERTFILE`, `FRIDAY_SSL_KEYFILE`, `FRIDAY_BACKEND_CA_FILE`, верните
`http://` origins, перезапустите backend, проверьте `/api/health`, затем
перезапустите bridge. Не оставляйте половину TLS-пары — validator её отклоняет.

Base Compose намеренно не включает native TLS: backend и bridge общаются по HTTP
только внутри private Docker network, а общая среда принудительно очищает server
cert/key/CA paths. Это не даёт Telegram-контейнеру прочитать private key. Для
внешнего HTTPS публикуйте backend только через отдельный TLS reverse proxy; не
кладите server key в общий `/runtime/data` и не пробрасывайте его в bridge.

## 5. API health

Неаутентифицированный endpoint:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

Проверка identity:

```powershell
$env:FRIDAY_TOKEN='<token>'
Invoke-RestMethod http://127.0.0.1:8000/api/me `
  -Headers @{Authorization="Bearer $env:FRIDAY_TOKEN"}
```

Admin diagnostics показывает те же schema/backup/worker/lease/actions, но raw JSON оставлен под progressive disclosure для глубокого разбора.

## 6. Обновление

Перед заменой кода можно создать дополнительную operator-independent копию:

```powershell
jericho backup --label before-upgrade
jericho verify-backup
pytest -q
```

Единственный release path — `tools/immutable_release_operator.py`. Снача source
Python строит sealed wheel-only siblings; все последующие команды запускаются
только из sealed release его же interpreter и копией оператора:

```text
<SOURCE_PYTHON> -I -B tools/immutable_release_operator.py build ... \
  --state-dir <STATE_DIR> \
  --secondary-product-runner \
  <EXACT_CLEAN_CANDIDATE_CHECKOUT>/deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py \
  --secondary-product-runner-sha256 <RUNNER_SHA256> \
  --production-observation-operator-sha256 <EXACT_GIT_BLOB_SHA256> \
  --release-retention-toolchain-manifest-sha256 <INDEPENDENT_TOOLCHAIN_SHA256> \
  --build-receipt-profile p0h-retention-v1
<CANDIDATE>/venv/bin/python -I -B \
  <CANDIDATE>/artifacts/immutable_release_operator.py install-units ...
<CANDIDATE>/venv/bin/python -I -B \
  <CANDIDATE>/artifacts/immutable_release_operator.py activate ...
<EXECUTOR>/venv/bin/python -I -B \
  <EXECUTOR>/artifacts/immutable_release_operator.py recover-activation ...
<CANDIDATE>/venv/bin/python -I -B \
  <CANDIDATE>/artifacts/immutable_release_operator.py recover-historical-album ...
```

После owner smoke production-наблюдение запускается только sealed interpreter
и tree-bound observer кандидата. Private artifact остаётся `0400`, а его digest
фиксируется отдельно до успешной атомарной публикации bundle:

```text
<CANDIDATE>/venv/bin/python -I -B \
  <CANDIDATE>/artifacts/production_read_only_observation_operator.py \
  --release-tree-sha256 <TREE_SHA256> \
  --output <OWNER_PRIVATE_CREATE_ONLY_ARTIFACT> ...
<SOURCE_PYTHON> -I -S -B \
  <EXACT_CLEAN_CANDIDATE_CHECKOUT>/tools/exact_release_evidence.py \
  production-bundle \
  --artifact <OWNER_PRIVATE_CREATE_ONLY_ARTIFACT> \
  --expected-artifact-sha256 <PUBLISHER_RECORDED_SHA256> \
  --expected-source-commit <COMMIT> \
  --expected-tree-sha256 <TREE_SHA256> \
  --expected-wheel-sha256 <WHEEL_SHA256> \
  --expected-database-schema <SCHEMA> \
  --output-root <EXTERNAL_PRIVATE_BUNDLE_ROOT>
```

Все пути release-controller образуют один portable layout от exact absolute
`<FRIDAY_HOME>`: `data/state`, `wheel-only-releases`, `current-release` и
`.env.local`. `build` проверяет весь layout до lock/staging и сериализует его
через canonical state lock; reader-first profile намеренно не публикует новый
lock-scope metadata pair, чтобы exact `0.207.84` оставался допустимым fallback.
Совместная подмена home+state не создаёт второй lock для прежнего release root.
`install-units` выводит home из `data/state`, а
runtime повторно связывает layout и sealed candidate units. Внешний `unit-dir`
дополнительно сериализован общим semantic lock для exact backend/bridge unit
pair, поэтому разные home или unit-dir не управляют этими units одновременно.

Runner берётся только из чистого checkout exact candidate commit; переданный
digest должен совпасть с его стабильными bytes. В release копия хранится mode
`0400` как immutable trust anchor. Самостоятельно запускать эту копию нельзя:
`product-stage` исполняется из чистого checkout уже активного predecessor, где
доступны sibling modules и Git identity, а оператор требует byte-for-byte
совпадение с sealed anchor.

`build` запускается отдельно для каждого sealed sibling. Режим vault связан с
immutable config identity и с аттестованной release-metadata capability
`memory_vault_mode_contract=v1`; совпадение semver её не заменяет. `install-units`
допускает новый candidate только с
`venv_relocation_contract=absolute-final-v1`: до seal оператор перепривязывает
все console shebangs, shell activation scripts и `pyvenv.cfg` к exact final
venv, пересчитывает соответствующие `RECORD` hash/size, удаляет `__pycache__` и
доказывает exhaustive scan, что staging root не остался ни в одном regular file
или symlink target. До и после atomic rename напрямую исполняются `friday
--help`, `jericho --help` и `pip --version`; smoke также связывает `sys.prefix`
и `sys.executable` с этим venv. Отдельный hermetic Bash smoke фактически source-ит
`activate`: до публикации проверяет effective `VIRTUAL_ENV` и начало `PATH`, а
после неё также `command -v python`, `sys.prefix` и `sys.executable`. Ошибка после
публикации оставляет immutable
commit-root quarantined без clear receipt: автоматического удаления или retry
того же имени нет.

`install-units`
crash-recoverably переводит уже подготовленную canonical Friday-пару с
аттестованного transition runtime на anchor-based unit files, не останавливая
live; `jericho-*` units он не мигрирует. `activate` сам останавливает writers,
делает exact DB/WAL/inbox
recovery set, мигрирует, переключает атомарный anchor и доказывает
process/TLS health backend до bridge. `recover-historical-album` **никогда не
запускается в Phase A**: только после accepted final Phase B исторический альбом
восстанавливается отдельной crash-recoverable командой. CAS из `dead_letter` в
`pending` выдаёт только внутренний `pending` receipt. Публичный `status=clear`
появляется лишь после запуска exact final bridge, двух read-only наблюдений, что
все десять связанных update rows атомарно исчезли из durable inbox, и повторной
проверки identity живого bridge между наблюдениями. Это означает, что bridge
завершил единый album turn и удалил все десять строк только после возврата
`_process_update`; сам CAS, health или restart доказательством публикации не
считаются. Ожидание ограничено 600 секундами: timeout оставляет journal в
`bridge_accepted` без completion receipt, а повтор команды продолжает только
monitor; частичное исчезновение либо повторный `dead_letter` fail-closed и не
маскируются как recovery. Completed album journal идемпотентен лишь при exact той
же config identity и при повторно подтверждённом отсутствии всех десяти строк;
mismatch mode/env/config или возвращённые строки не переходят автоматически и
требуют review/remediation под исходной bound-config.

Первый переход на новую schema требует двух отдельных sealed identities с
одинаковым functional tree: `rc0` уже умеет открыть новую schema и служит только
schema-capable fallback, а stable candidate отличается от него лишь version
identity. `rc0` отдельно не активируется. Для 46→47 это
`0.207.72/schema46` previous → `0.207.73/schema47` candidate с
`0.207.73rc0/schema47` fallback; использовать schema-46 release как fallback
оператор обязан отклонить до остановки writers.
Для 47→48 действует тот же контракт: `0.207.75/schema47` previous →
`0.207.76/schema48` candidate с неактивированным
`0.207.76rc0/schema48` fallback.
`0.207.77/schema48` миграции не выполняет: его stable previous и immutable
fallback — один exact `0.207.76/schema48`.
`0.207.78/schema48` также не меняет DDL: exact `0.207.77/schema48` служит и
stable previous, и immutable fallback.
Переход 48→49 снова использует две sealed schema-capable identity:
`0.207.78/schema48` previous → `0.207.79/schema49` candidate с никогда не
активированным `0.207.79rc0/schema49` fallback. Stable отличается от rc0 только
version identity; schema-48 release не допускается как post-migration fallback.
Переход 49→50 использует тот же контракт: `0.207.79/schema49`
previous → `0.207.80/schema50` candidate с никогда не активированным
`0.207.80rc0/schema50` fallback. Stable отличается от rc0 только
version identity; schema-49 release не допускается как post-migration
fallback.
`0.207.81/schema50` DDL не меняет: exact stable
`0.207.80/schema50` служит одновременно previous и immutable fallback.
`0.207.82/schema50` также не меняет DDL: exact stable
`0.207.81/schema50` служит одновременно previous и immutable fallback.
`0.207.83/schema50` также не меняет DDL: exact stable
`0.207.82/schema50` служит одновременно previous и immutable fallback.
`0.207.84/schema50` также не меняет DDL: exact stable
`0.207.83/schema50` служит одновременно previous и immutable fallback.
`0.207.85/schema50` также не меняет DDL: exact stable
`0.207.84/schema50` служит одновременно previous и immutable fallback.
`0.207.85`–`0.207.89` не активировались; исправляющий `0.207.90/schema50` идёт напрямую от
exact stable `0.207.84/schema50`, который служит одновременно previous и
immutable fallback.
Перед активацией завершается только bootstrap v1-generation для `0.207.84`.
После активации `0.207.90` вторую v1-generation не публикуйте и оставьте
`older` пустым до полного writer-контракта: v1 остаётся пригодным для защиты и
read-only классификации, но кодово не может дать destructive apply authority.
`0.207.91/schema50` DDL не меняет и использует exact `0.207.90/schema50`
одновременно как previous и immutable fallback. Перед установкой единственный
точный завершённый v1 journal `0.207.90` связывается с admission первого v2;
после успешной активации lifecycle публикует pair-bearing v2 generation и
сохраняет pre-activation `0.207.84` отдельным permanent retain-only anchor.
Activation не удаляет релизные артефакты: scope, reviewed dry-run, bounded
apply и свежий terminal-zero convergence выполняются отдельными шагами. Fresh
`review_required` admission может пропустить следующий release только как
scope-bound non-destructive defer: convergence-поля остаются пустыми, а delete
authority не возникает.
`0.207.92/schema50` DDL не меняет, принимает exact `0.207.91/schema50` как
previous и сохраняет `0.207.90/schema50` immutable fallback. Canonical gate
evidence аутентифицируется отдельным защищённым контуром вне disposable
release/backup inventory и никогда не получает delete classification.
`0.207.93/schema50` DDL не меняет, принимает exact `0.207.92/schema50` как
previous и сохраняет `0.207.90/schema50` immutable fallback.
`0.207.94/schema50` DDL не меняет, принимает exact `0.207.93/schema50` как
previous и сохраняет `0.207.90/schema50` immutable fallback.
`0.207.95/schema50` DDL не меняет, принимает exact `0.207.94/schema50` как
previous и сохраняет `0.207.90/schema50` immutable fallback.
`0.207.96/schema50` DDL не меняет, принимает exact `0.207.95/schema50` как
previous и сохраняет `0.207.90/schema50` immutable fallback.
`0.207.97/schema50` DDL не меняет, принимает exact `0.207.96/schema50` как
previous и сохраняет `0.207.90/schema50` immutable fallback.
`0.207.98/schema50` DDL не меняет, принимает exact `0.207.97/schema50` как
previous и сохраняет `0.207.90/schema50` immutable fallback.

0.206.4 использует SQLite schema 34; Obsidian-релиз 0.207.2 поднимает её до
schema 35. Новое поле имени загрузки принадлежит
точному message-bound alias и не переписывает канонический Raw Object. Миграция
и исторический backfill разрешены только при остановленных backend/bridge,
проверенной копии SQLite вместе с WAL и Telegram inbox, exact lease/identity
и post-verify публичного поиска. Cutover из 0.205 в body-free режим двухфазный:

1. **До редактирования env** сохраните SHA-256 его точных sealed bytes. **Phase A /
   bridge:** `FRIDAY_MEMORY_VAULT_MODE=full_owner`; candidate — mode-aware
   schema-34 `0.206.0rc1`, previous — legacy 0.205/schema-33, fallback — отдельный
   mode-aware schema-34 `0.206.0rc0`. В `activate` передайте сохранённый digest как
   `--terminal-journal-env-sha256 <PRE_EDIT_ENV_SHA256>`: только так exact legacy-v1
   terminal identity может быть аутентифицирована после добавления `full_owner`.
   Неверный digest, unfinished journal или попытка применить этот переход сразу к
   `disabled` отклоняются. Если rollback активировал rc0, повтор указывает rc0
   одновременно как previous и fallback. Если этот rollback случился после
   network-writer boundary, уже применённые alias claims при повторе **опускаются**:
   terminal journal разрешает это только для `rolled_back` **или** crash-recovery
   terminal `recovered` с подтверждёнными DB mutation/network uncertainty и exact
   `writer_target=fallback`, тем же rc1 candidate, прежним rc0 fallback, rc0 как
   новые previous=fallback, неизменными persistent scope и retry-scope (включая
   прежний env hash). `recovered` с previous/candidate writer, иной lineage или
   изменённой конфигурацией не может оправдать omission. При rollback до этой
   boundary точный backup восстановлен, поэтому claims повторяются.
   Одноразовые alias-claim manifests разрешены только здесь: полный config
   identity Phase A связывает их exact paths/counts/plan hashes.
2. **Phase B / body-free:** только после accepted rc1 и schema 34 перевести
   env в `disabled`; candidate — final, previous и fallback — один и тот же rc1.
   Alias-claim arguments здесь обязаны отсутствовать: persistent cutover scope
   исключает уже потреблённые одноразовые claims, но остальные runtime paths,
   health/unit identities и lineage остаются связанными.

Lineage закрыт: rc0 содержит последний functional diff, rc1 отличается от rc0
только version identity, а final от rc1 — только version/docs; новые runtime-правки
между этими siblings запрещены и требуют нового bridge cycle.

Release без mode contract считается legacy `full_owner` и допустим только в Phase A.
В `disabled` такой previous/fallback/candidate отклоняется независимо от semver,
включая stale pre-contract 0.206 siblings. Activation receipt связывает фазу и
режим. Завершённый activation journal заменяется при exact legacy-v1 migration
только в Phase A или при строго связанном переходе Phase A→B: прежняя фаза должна
быть `clear` (не `rolled_back`), config scope совпадает, прежний candidate равен
новым previous и fallback, а mode меняется ровно `full_owner`→`disabled`. Любой
unfinished/иной identity mismatch остаётся fail-closed и требует recovery в
исходной identity. Album journal mode/env transition не принимает. Неизвестная
более новая schema отклоняется fail-closed.

Обычное восстановление вне release transition выполняется только штатной
командой при остановленном backend. Незавершённая activation восстанавливается
только `recover-activation`; `jericho restore-backup` не заменяет release journal:

```powershell
jericho restore-backup <backup.sqlite3> --yes
```

Полный контракт описан в [BACKUP_AND_RESTORE.md](BACKUP_AND_RESTORE.md).

## 7. Ежедневная работа с знаниями

### Telegram modes

- `/chat` — обычный разговор с минимальным tool budget;
- `/work` — многошаговая работа над несколькими личными знаниями и графом;
- `/research` — планирование, web/tool gathering и bounded synthesis;
- `/inbox` — ближайшие предложения с inline promote/ignore;
- `/status` — статистика базы, review pressure и текущий сохранённый режим канала.

`knowledge_work` и `research` могут подготовить структурированный результат, но не записывают его в память напрямую. Кнопка **В Inbox на review** создаёт идемпотентный Raw Object + pending candidate. Пользователь исправляет title/summary/kind/entities и только затем выполняет promotion.

Ответы, опирающиеся на личную базу, используют внутренние маркеры `[K#]`. Feedback 👍/👎 связывается с фактически процитированными/использованными Knowledge Objects и влияет на retrieval без переписывания исходной importance.

### Массовый импорт с диска

```bash
jericho import ~/Документы --dry-run          # состав, ничего не записывая
jericho import ~/Документы                    # загрузить
jericho import ~/Документы --uploaded-by USER # кто действительно принёс файлы
jericho import ~/Архив --suffix .md --limit 500
```

Обходит каталог и прогоняет каждый файл через обычный конвейер приёма. Всё уходит в Inbox — указание на папку не является решением о каждом файле внутри неё.

CLI не аутентифицирует человека и поэтому не выводит автора из `--user`: в общем архиве этот идентификатор называет общий tenant. Передайте `--uploaded-by ACCOUNT_ID`, только если автор действительно известен. Без флага новые Raw Objects получают явное `uploaded_by: null` и учитываются как материалы без автора; повторный импорт не переписывает метаданные уже существующих объектов.

Возобновляемость даром: `source_ref` выводится из хеша содержимого, поэтому повторный запуск на том же дереве не грузит ничего заново, а прерванный прогон продолжается той же командой. Побайтово одинаковые файлы по разным путям схлопываются в один объект.

Каталоги вроде `node_modules`, `.git`, `__pycache__`, `.venv` отсекаются до входа. Обход упорядочен, поэтому `--limit` режет большое дерево на партии, а не на случайную выборку.

### Разбор Inbox группами

После импорта в очереди тысячи материалов. Панель **Группы непроверенного** в разделе Inbox режет её по типу файла, каталогу или источнику: на настоящем импорте 200 файлов это 14 групп с крупнейшей на 168.

Групповое действие **только отклоняет** (архив/игнорировать). Продвижение остаётся поштучным, через «Разобрать», где виден исходный текст: одобрение двухсот непрочитанных материалов решением не является, а продвижение вдобавок запускает обогащение, создаёт сущности графа и ставит в очередь кандидатов на связи и конфликты.

### Admin workflow

1. **Inbox** — одиночный или массовый triage с Raw Object, score, explanation и model advice.
2. **Знания** — correction с version snapshot, provenance и entity links.
3. **Граф** — Entity Resolution, relation candidates и potential conflicts; одиночное или bulk accept/reject/confirm/dismiss.
4. **Качество** — feedback, usage, legacy noise и read-only lifecycle candidates.
5. **Диагностика** — системное состояние, recovery actions, workers и leases.

Принятые/отклонённые relation decisions терминальны: background discovery не возвращает их в очередь. Merge, archive, lowering importance и conflict resolution всегда требуют явного действия.

## 8. Background workers

При `FRIDAY_WORKERS_ENABLED=1` supervisor запускает tenant-scoped задачи lifecycle, ER candidates, backup, SQLite optimize, quality scan и bounded model advice. Полнотекстовый Markdown-projector регистрируется только при явном `FRIDAY_MEMORY_VAULT_MODE=full_owner`; безопасное умолчание `disabled` не создаёт `MemoryVault` и не планирует эту задачу. Неизвестное значение останавливает startup; `full_owner` также fail-closed на платформе без descriptor-relative `O_NOFOLLOW` boundary (включая Windows), до публикации enabled-health. Отключение никогда не удаляет прежние заметки автоматически: `status`/`doctor` показывают presence, Markdown-count и completeness без публикации имён, путей и содержимого; crash-temp и другие regular artifacts тоже отмечают plaintext presence. Обычный offline `jericho purge --yes` удаляет лишь final/crash-temp файлы Knowledge Object, который действительно проходит purge; это не команда массовой очистки legacy-vault.

Для каждой задачи сохраняются:

```text
scheduled / running / ok / error / timeout
started_at / finished_at / duration_ms / next_run_at
consecutive_failures / last_error
```

Supervisor timeout ограничивает ожидание и health-state, но не останавливает уже запущенный `asyncio.to_thread` blocking thread. Поэтому blocking workers обязаны иметь внутренние кооперативные budgets; `entity_mention_backfill` возвращает `budget_exhausted` / `budget_reason` / `has_more` и продолжает работу следующим tick. Его штатные пределы на tenant — `8 documents / 25 links / <=10 s`; content читается bounded UTF-8 blob-страницами, а числовые character/byte cursors и технические spool/checkpoint переживают yield и рестарт без сохранения текста, имён или snippets. Malformed/stale progress автоматически переходит в bounded cleanup/revalidation. Ошибка одного tenant/item не отменяет результаты остальных, но batch получает degraded health, чтобы частичный сбой не исчез молча. При намеренно остановленном backend старые timestamp не считаются зависанием.

Типичный разбор:

```powershell
jericho status
jericho doctor
```

Затем сопоставьте task name и `last_error` с локальными логами. В error text не должно быть содержимого знаний или secrets.

## 9. Типовые проблемы

### Admin UI просит ключ снова

Token хранится в `sessionStorage`, поэтому новая вкладка/сессия требует повторного ввода. Используйте `FRIDAY_API_TOKEN`.

### Backend не запускается второй раз

Это защитное поведение singleton lease. Найдите и штатно завершите существующий `jericho server`/container. Не удаляйте lock-файл как «исправление»: diagnostics отличает stale metadata от активного ownership.

### Backend отклоняет конфигурацию

Проверьте длину secrets, CORS, bind address и URL:

```powershell
jericho doctor
```

### Telegram получает 401

Backend и bridge должны использовать одинаковый `FRIDAY_TELEGRAM_BRIDGE_SECRET`; часы host/container не должны сильно расходиться; signature max age по умолчанию 90 секунд.

### Telegram повторяет update

Это ожидаемое at-least-once поведение при временном сбое. Exact retry воспроизводит сохранённый результат, активный конкурент получает retryable `409`, а тот же `source_ref` с другим payload — permanent conflict/dead-letter. Inline-кнопка остаётся доступной после временной ошибки и исчезает только после успешного или окончательно невалидного действия.

### Worker в timeout/error

Проверьте `doctor`, доступность SQLite/LLM, размер очереди и локальные logs. Один повреждённый Inbox item не должен блокировать остальные; после устранения причины следующий успешный цикл сбросит consecutive failures.

### Модель не стартует

Проверьте полный snapshot, совместимость vLLM image, GPU visibility, VRAM и конкретные quantization args. Не уменьшайте профиль вслепую: сначала сохраните реальную ошибку загрузки.

### Vision дал сомнительную экстракцию

Это не авария: visual output advisory-only. Проверьте asset/page evidence и цитируемые фрагменты в Inbox. Незаземлённый high-confidence ответ автоматически понижается, а сложный скан требует ручной correction.

### Office отвечает, что точный состав неизвестен

Это fail-closed результат, а не повод включать менее строгий model prompt. Exact
count/list доступен только native DOCX/XLSX с полным `OfficeStructureIndex v1` и
целым structured prompt. Формула без cached value, неоднозначная/merged шапка,
nested table, header/footer, скрытый legacy-parser-ом OOXML text или любой
text/row/index/prompt budget снимает полноту. То же происходит при отсутствующей или
неверной local attestation индекса: она связана с SHA-256 байтов конкретного Raw и
не переносится между файлами. Старые Raw Objects автоматически не
backfill-ятся: повторно пришлите исходный файл, если нужен новый code-owned
inventory. Не копируйте живое содержимое документа в диагностические команды или
внешние model probes; для воспроизведения используйте synthetic файл.

### Backup не проходит проверку

Не используйте его для restore. Сверьте пару `.sqlite3 + .manifest.json`, исключите частичную синхронизацию носителя и создайте новую копию из исправной БД.

### Restore отказывается запускаться

Backend всё ещё владеет lease, отсутствует `--yes` либо backup не прошёл fail-closed verification. Остановите процессы, выполните `verify-backup`, затем повторите штатную команду. Не копируйте БД вручную поверх живого процесса.

## 10. Наблюдаемость и privacy

Telemetry локальная и не отправляет данные наружу. Audit хранит административные изменения и tool calls. Внешний structured-log collector допустим только opt-in и не должен включать содержимое личных знаний, prompts, documents или secrets по умолчанию.

## 11. V12 shadow и opt-in file/archive canary

Без настройки работает только прежний runtime. Переключатель обратим и не
меняет схему БД:

```dotenv
FRIDAY_ROUTER_MODE=legacy
FRIDAY_ROUTER_CANARY_ROUTES=file_read
FRIDAY_ROUTER_CANARY_USER_IDS=
FRIDAY_ROUTER_PLAN_TIMEOUT_SEC=12
```

`shadow` строит технический план в фоне, но ответ и эффекты всегда принадлежат
legacy. `canary` требует непустой allowlist пользователя, хотя бы один явно
разрешённый route (`file_read` и/или `archive_read`) и успешную live-аттестацию
профиля `qwen38-27b-nvfp4-sglang:dispatcher:v12.15` при старте backend.
`file_read` обрабатывает 1–2 полных UTF-8
файла текущего хода. `archive_read` обрабатывает только прежние полные UTF-8
файлы самого actor: уникальное точное имя, ровно 1–2 последних файла или не
более двух файлов за точный локальный день «сегодня / вчера / позавчера».

Неоднозначный selector, явный другой пользователь, reply/replay, запрос более
двух файлов, PDF, изображения/OCR, partial extraction, web и effects
автоматически остаются в legacy. Авторизация выполняется до чтения body; при
публикации selector и каждый Raw Object повторно проверяются под одним SQLite
write barrier, а conn-scoped idempotency fence фиксируется атомарно с ответом.

Канонический deployment graph для этого profile: SGLang image
`lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124`,
source `c4271c3fe1262fc2adbd162c33b25de5255251c5`, reported version
`0.0.0.dev0+qwen38.27b.g561c8f3`, model revision
`43aa7ff5eef05ab50a3bfa6aca581085312c7a04`, alias `dispatcher`, context/total
tokens `40960`, running/Mamba cache `6`, `mem_fraction_static=0.90`, FP8 E4M3 KV,
Radix/speculation off, full decode CUDA graphs `1..6`, prefill graph off. Startup
дополнительно требует exact per-process deployment witness для
engine/base image, model snapshot, launch manifest и closed same-origin proxy.
Build-time hashes берутся из code-owned profile/witness и не вводятся
оператором вручную. Не печатайте raw witness: adapter выносит в health
только ограниченную публичную проекцию.

Минимальная owner-only конфигурация canary:

```dotenv
FRIDAY_ROUTER_MODE=canary
FRIDAY_ROUTER_CANARY_ROUTES=file_read,archive_read
FRIDAY_ROUTER_CANARY_USER_IDS=owner
FRIDAY_ROUTER_PLAN_TIMEOUT_SEC=12
```

После принятого long-context ответа probe допускает только bounded convergence
собственной SGLang load-метрики: до 20 секунд, один same-epoch valid busy-sample
за попытку и пауза 50 мс. Invalid sample, transport failure, epoch drift и любой
исчерпанный deadline немедленно fail-closed; initial idle и post-cancellation
quiet observations не смягчаются.

Startup probe синхронный и может занять до 330 секунд. При контролируемом
переключении сначала штатно остановите bridge, затем запустите backend и не
возвращайте bridge до полного health-подтверждения:

```text
orchestration.configured_mode = canary
orchestration.installed_mode = canary
orchestration.registered_routes = [archive_read, file_read]
orchestration.model_gate.profile_id = qwen38-27b-nvfp4-sglang:dispatcher:v12.15
orchestration.model_gate.status = canary_ready
orchestration.model_gate.reason_code = live_attestation_clear
orchestration.model_gate.verified_context_tokens = 40960
```

При таком exact q38 witness runtime выбирает минимально достаточный tier из
`8192/16384/24576/32768/40960`; Qwen3.6 и объекты без нового capacity API
остаются на 8192. Размер считается отдельно для каждого model-вызова, включая
worst-case JSON expansion полного разрешённого ответа внутри verifier input.
Requirements digest и tier переносятся без подмены через plan/binding, lease и
process-owned result. Acquire одноразовый: отказ, timeout, drift либо отзыв
attestation не разрешают повторный acquire на меньшем или новом tier.

Во время probe `/api/health` ещё недоступен. Ждите до 420 секунд и дополнительно
требуйте `status=ok` и `version=0.207.98`.

HTTP `status=ok` при `installed_mode=legacy` означает безопасную деградацию, но
не успешный canary. В `canary`/`v12` Sentinel не реже раза в минуту
проверяет только bounded public gate status. `revoked`, `not_installed` и
недоступность observer дают владельцу одно санитизированное
предупреждение на непрерывный эпизод; recovery перевооружает следующий.
Обычный route fallback не алертит, private reason не попадает в очередь,
а quiet hours сохраняются.

Для мгновенного отката оставьте bridge остановленным,
верните `FRIDAY_ROUTER_MODE=legacy`, перезапустите backend, подтвердите
configured/installed `legacy` и только затем снова запустите bridge. Обычный
rollback кода не требует восстановления SQLite.
