# Release checklist

> Проект переименован: **Friday** (по-русски — **Пятница**), ex codename Jericho.

Этот gate предназначен для технического выпуска Friday. Он отделяет проверенный source release от deployment-проверок, которые требуют конкретного Windows/GPU/Telegram окружения.

## 1. Source tree

Подготовить dev-окружение и Chromium, затем запустить единый репозиторный гейт:

```bash
.venv/bin/python -m pip install --upgrade --constraint requirements-dev.lock pip setuptools wheel
.venv/bin/python -m pip install --no-build-isolation --constraint requirements.lock --constraint requirements-dev.lock -e ".[dev,vectors]"
.venv/bin/python -m playwright install chromium
.venv/bin/python tools/quality_gate.py
```

Перед любой выбранной фазой гейт обязательно аттестует Python 3.14.4,
Node 22.23.2, NumPy 2.5.1, Playwright 1.61.0, установленный Chromium revision
1228 и официальный бинарник UnRAR 7.20. Затем полный запуск идёт тремя фазами:
`static` (Ruff, mypy, Bandit HIGH и `node --check`), `tests` (не-браузерный
pytest) и `ui` (Playwright). UI-модули отделены от общего pytest, чтобы их
process-wide серверы не пересекались; по умолчанию им выделяется 12 workers —
по одному на модуль (`--ui-workers 1` задаёт настоящий serial `-n 0`). JUnit
обеих pytest-фаз сверяется по точным nodeid с полной коллекцией, и любой
failed/error/skipped-тест в любой фазе делает гейт красным. Параметры
`--phase static`, `--phase tests` и `--phase ui` предназначены только для локальной
итерации и не заменяют полный запуск перед выпуском.

Обязательные условия:

- канонический гейт завершился с кодом 0;
- нет failed/error/skipped-тестов ни в non-UI, ни в UI-фазе;
- Ruff/mypy clean;
- Bandit HIGH = 0; каждый MEDIUM/LOW отдельно рассмотрен и объяснён в release evidence;
- `git diff --check` clean;
- Admin JavaScript проходит встроенный в гейт `node --check`;
- Markdown links/fences и Compose YAML разбираются без ошибок.

## 2. Product regressions

Проверить минимум:

- `transient / review / promote` и абсолютный `do not remember`;
- `knowledge_work → structured result → Inbox`, без прямой записи;
- точные идентификаторы `BRK.A / BRK.B` без смешения;
- feedback replacement и attribution к реально использованным `[K#]`;
- terminal relation decisions and monotonic conflict decisions и bulk review;
- grounded vision с confidence cap для ответа без evidence;
- Telegram callback idempotency и retryable partial failure;
- worker timeout/partial batch health;
- backend/bridge singleton leases;
- backup verification, restore, safety backup и rollback.
- история: local-time half-open окна, автопагинация, current-message boundary,
  thematic query и hostile historical text как untrusted data;
- файлы: filename/body union, durable upload aliases, `ё/е`, active-result selection
  и exact uploader/privacy reauthorization;
- large XLSX: a 291-row complete sheet with a section column before its dense
  ordinal yields a complete deterministic all-row profile without model MAP;
  ambiguous ordinals fall back and positional rows are not comparison identities;
- real legacy Office/StarOffice conversion succeeds with an isolated ambient
  `TMPDIR`; the converter root remains on the exact sandbox-wrapper path;
- PDF/JPEG/OCR: mandatory ordered `pages[]`, singleton fallback и unreadable boundary
  без ложного `empty_text` success;
- Telegram: per-sibling album receipts, partial replay, crash/timeout uncertainty fence,
  reply edges и request-id correlation;
- weather без явного города не идёт в web; diagnostics требует exact
  capability, а MCP status строится из code-owned projection.

Для autonomous Engineer Mode дополнительно:

- контур default-off и доступен только installation owner в свежем личном
  Telegram-чате; unsigned API, guest, quote, attachment и model output не могут
  выдать command authority;
- модель получает `engineer_command_run/status/cancel` без лексического
  allowlist и `/approvals`, запускает произвольный Bash и установленное ПО от
  пользователя Friday backend с его реальными PATH, filesystem и network;
- exact current uploads и staged albums, включая archive/audio/unknown binary,
  проходят opaque byte admission без parser/OCR/transcription; quoted, ambient,
  foreign, duplicate и drifted carriers fail-closed;
- два зависимых шага с повторяющимся native tool-call ID получают разные
  code-owned identities; persistent workdir, output sealing, автоматическая
  Telegram delivery, restart/status/cancel и sparse progress остаются рабочими;
- backend shutdown закрывает admission, завершает или убивает live cgroups и
  только затем освобождает command store/SQLite; acceptance выполняется native,
  без Docker и без companion plugin.

Для отдельного Host Capability Plane дополнительно:

- disabled/disconnected host agent не публикует tools и не мешает обычному Friday;
- host-control доступен только installation owner, actor/own_id перепроверяются
  перед execution, а package capability отделена от action/network rights;
- `nmap` использует общий reviewed argv/parser contract, code-owned target
  normalization, не принимает raw flags/NSE и по умолчанию не допускает public scope;
- preinstalled `jq` проходит owner Raw-file reauthorization → exact private
  workspace grant → code-generated field-only program → receipt-bound output →
  durable attachment с точными download/history/replay bytes; pending upload
  поддерживается в том же turn, исходный файл не меняется, raw jq expression и
  host path отсутствуют в schema;
- полный fake-agent vertical slice проходит: missing `nmap` → exact APT plan →
  payload-bound approval claim → install receipt/postcondition → executable
  attestation → отдельный deterministic action job → scan evidence/coverage;
- plan/dependency/origin/target/executable drift, replay и malformed receipt
  fail-closed; потеря ответа после admission даёт durable `unknown`, status
  reconciles exact transaction/unit без повторного execute;
- broker crash после APT effect сверяется только read-only с exact pre/desired/
  mixed package snapshot: desired возобновляет continuation через отдельный
  подписанный reconciliation receipt без второго commit, pre-state допускает
  только safe re-plan, mixed/unavailable остаются durable `unknown`;
- APT stdout/stderr сохраняются только как bounded private content-addressed
  evidence (`0600`, не более 1 MiB на stream); receipt честно различает retained
  и total bytes/completeness/truncation, а raw bytes не попадают в SQLite,
  journal, prompt или публичную projection;
- cancel-before-commit и systemd-cgroup cancellation доказаны отдельными
  receipts/status, а approval получает `failed`, `uncertain` или `done` по
  фактической границе эффекта;
- `tests/test_host_control_*`, `tests/test_friday_host_agent_*`,
  `tests/test_package_broker_*` и deployment contracts зелёные;
- Ubuntu acceptance выполняется только после source gate по инструкции
  [`deploy/host-control/README.md`](../deploy/host-control/README.md), сначала с
  flags `0`, затем на owner-controlled private target. Реальный APT/systemd
  smoke является deployment evidence и не подменяется unit-тестом.
- host installer получает только canonical release wheel, собранный release
  toolchain с `setuptools>=77`/`wheel>=0.45`, и SHA-256 из отдельного release
  manifest; Ubuntu target не строит source и ставит wheel с `--no-index`.
- host installer сначала полностью проверяет permanent versioned venv и все
  staged unit/config файлы, затем атомарно переключает root-owned `current`;
  failure или handled signal восстанавливает прежнюю activation, точные файлы
  и изменённые installer-ом enable/linger/start states. Отложенное включение
  user unit выполняется только через exact selected-user `runuser` +
  HOME/XDG/DBus contour, а не через user manager оператора.
- `tools/build_host_control_release_bundle.py` на чистом exact release commit
  доказывает byte-for-byte соответствие wheel Git `HEAD`, закрытый набор и modes
  deploy-файлов, deterministic archive и strict manifest. Перед распаковкой
  trusted verifier обязан принять SHA-256, полученный по независимому каналу;
  mismatch, extra/missing member, symlink/special Git entry или modified wheel
  являются блокирующими.
- rootful Compose build/run использует exact UID/GID выбранного desktop-user и
  `userns_mode: host`; UID/GID внутри backend совпадают с host, успешный
  authenticated handshake доказывает одновременно доступ к owner-only
  directory/socket/key и exact `SO_PEERCRED`, без group/mode fallback.
- stop-agent acceptance сохраняет tmpfiles-created socket parent `0700`, удаляет
  только socket, допускает обычный backend restart с честным `disconnected`, а
  последующий start-agent возвращает authenticated healthy handshake.
- если public-network когда-либо включается, `jericho doctor` и backend
  handshake оба обязаны сверить exact Ed25519 public-key digest реального backend
  signer с agent health. Missing/malformed/mismatch signer identity не публикует
  capabilities и не может считаться `ready`.
- public action, ожидающий action-concurrency semaphore, получает short-lived
  Ed25519 proof только после fresh owner/capability/target-policy и durable
  claimed-approval recheck на send seam; queue time не старит готовый proof, а
  revoke в очереди обязан завершиться до `request_sent`. До захвата bounded
  slot durable job остаётся `planned`/`awaiting_approval`; saturation/cancel
  закрывается как доказанный pre-effect failure. Exact retry уже claimed
  approval после backend restart допустим только для совпадающего immutable
  job в `awaiting_approval`/`approved`/`admitted`, без `started_at`, evidence и
  reconciliation marker; `running`/`unknown` никогда не переисполняются.

Для Obsidian Android beta дополнительно:

- без `FRIDAY_OBSIDIAN_ENABLED=1` Organ, managed Syncthing и его workers не
  запускаются;
- pinned Syncthing 2.1.3 проходит version/config/Unix-socket attestation, а
  identity drift останавливает профиль fail-closed;
- приватный `/obsidian` возобновляет один owner-scoped onboarding, различает
  pending candidates и не выдаёт open-confirmation до exact Android receipt;
- note mutations идемпотентны по `operation_id`, expected revision не допускает
  lost update, а racing peer generation сохраняет обе стороны как conflict;
- delivery отдельно доказывает local write, scan, live connection, exact remote
  availability и user-confirmed open; offline остаётся `delivery_pending`;
- persisted Syncthing Unix endpoint не зависит от ambient backend `TMPDIR`, а
  отсутствующий private runtime directory восстанавливается с mode `0700`;
- reconcile выполняется сразу при backend startup, запускает ready profile и
  после падения child восстанавливает новый PID; `failed > 0` публикует worker
  error/degraded и только чистый следующий sweep возвращает health в `ok`;
- immutable activation сохраняет SQLite/WAL, Telegram inbox и exact Obsidian
  root одним recovery set, а staged ENV1 публикуется только после verified
  backup и удаляется durable после успеха.

Для optional GPT-OSS secondary brain начиная с 0.207.11 дополнительно:

- release принимается и первый раз запускается без `FRIDAY_SECONDARY_LLM_*`:
  health имеет `status=ok`, `version=0.207.77`, `secondary.mode=disabled`,
  `secondary.state=disabled` и `secondary.available=false`;
- `ACCEPTED_SECONDARY_RUNTIME_PROFILES` содержит ровно finalist
  `gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`
  с accepted-manifest SHA-256
  `93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`;
  code-owned provisional-реестр содержит ровно abliterated successor
  `gptoss20b-d4c2207151c7507f9d71a1d3d5d387d6ae98bb89b04f3171ba667098c2ad2d25`
  и допускает его только в discarded `shadow/extract`, никогда не в `assist`;
- finalist связан с `https://192.168.1.35:8443/v1`, context/total `4096`,
  output `512`, concurrency `1`, chunked prefill `256`, native MXFP4, BF16 KV,
  `mem_fraction_static=0.96` и full decode CUDA graph только для batch 1;
- capacity acceptance принимает только schema v2: семь non-streaming repeats
  с точными 512 completion tokens и `finish=length`; warm/cold имеют один
  transport/generation/repeat protocol, разные runtime epochs, а soak связан с
  warm epoch. Любой legacy streaming-v1 receipt отклоняется;
- первый accepted-registry release сохраняет `mode=shadow`, workload `extract`,
  `ALLOW_PRIVATE_TEXT=0`: output выбрасывается, tools/effects/publication
  остаются за primary; private text и `assist` открываются только отдельными
  staged transitions, а любой manifest lookalike отклоняется;
- обязательны regressions laptop-off, TLS/profile drift, timeout, cooldown и
  primary-once fallback: optional endpoint не может сломать startup или primary answer;
- provisional public shadow ENV0→ENV1 и его плановое выключение
  идут только отдельными distinct-candidate activation через
  `secondary_shadow_enable` / `secondary_shadow_disable`;
- после отдельного accepted-registry public-shadow release реальный private product shadow
  меняет только `ALLOW_PRIVATE_TEXT=0→1` через
  `secondary_shadow_to_private_shadow` и требует свежий owner-private
  `product-stage --stage public-shadow` receipt с exact SHA-256; только после
  его evidence `secondary_shadow_to_assist` меняет только
  `MODE=shadow→assist` и требует свежий `private-shadow` receipt;
- candidate 0.207.24 должен реализовать только первый из этих
  переходов: `ALLOW_PRIVATE_TEXT=0→1` при неизменных
  `mode=shadow`/`workload=extract`; typed output выбрасывается, а
  tools/effects/publication остаются только у primary;
- candidate 0.207.26 должен менять только
  `MODE=shadow→assist` из exact private-shadow predecessor по свежему
  `product-stage --stage private-shadow` receipt; workload, private admission,
  endpoint и accepted profile не меняются;
- assist допускает только валидированную typed Inbox extraction:
  secondary не имеет tools/effects/publication или knowledge-write
  authority, а любой отказ обязан дать exact primary-only fallback;
- owner diagnostics разделяет `endpoint_admission_total` и физические
  `endpoint_request_total`/`endpoint_success_total`. Cold recovery обязан дать
  exact HTTP sequence profile → models → generation canary → product, то есть
  endpoint delta `4/4` при трёх admission; это связывают только
  `secondary-product-diagnostics.v2` и `secondary-product-stage-evidence.v3`,
  legacy v1/v2 evidence не переинтерпретируется и отклоняется fail-closed;
- в 0.207.27 Inbox advice использует exact code-owned strict JSON
  Schema и downstream validation; malformed/truncated response уходит в
  тот же primary fallback, а document review и web search не допущены;
- document-map shadow expansion не меняет accepted gateway manifest/profile
  или Windows runtime: code-owned product policy
  `7d57947d7ecda675e8a4da3f56332baf32484c08c0504afd7fa420b9c6323cd9`
  связывает exact profile и допускает только text/read-only MAP/REDUCE с strict
  JSON summary, concurrency `1`, output `512`, primary-once fallback и primary
  final synthesis;
- document-map проходит отдельно: `secondary_assist_enable_document_map_shadow`
  атомарно добавляет workload и `DOCUMENT_MAP_MODE=shadow`; v1 не допускает
  assist, пока отдельный shadow checkpoint не будет связан с новой policy и
  новым operator evidence gate;
- candidate 0.207.30 не меняет live ENV и оставляет
  `DOCUMENT_MAP_MODE=shadow`; owner-token-only empty-body one-shot должен
  доказать ровно один реальный `DOCUMENT_MAP`, exact scheduler deltas и
  неизменный primary sentinel без создания product data;
- promotion-grade receipt не хранит body, model output, их digests
  или cumulative counters; он связан с PID/epoch, accepted profile/CA,
  v1 policy и exact sealed predecessor commit/tree/metadata/wheel/ENV;
- candidate 0.207.31 содержит canonical v2 assist policy SHA-256
  `d2ab9b67ff24a54727fec9592dcd0db1c35036e1b5ee91ac6a5daf4d3694e92e`
  и accepted manifest SHA-256
  `933c671759724e36fe686185aa8ad03fa09f90e26e3095900796707cfef36855`,
  привязанные к exact live receipt без raw receipt/lookup token в source;
- только `secondary_document_map_shadow_to_assist` может одноразово consume-ить
  этот fresh receipt и изменить `DOCUMENT_MAP_MODE=shadow→assist`; v2 сохраняет
  primary fallback/final synthesis и запрещает secondary tools/effects/publication;
- на Windows-узле установлен и в release включён at-logon gateway
  publication recovery: exact healthy gateway перезапускается не более
  одного раза только после двух совпавших доказательств отсутствия
  publication/listener; inconsistent evidence и любой model restart запрещены;
- оба promotion activate передают `--secondary-rollout-receipt` и
  `--secondary-rollout-receipt-sha256`; attestation живёт не более 570 секунд,
  одноразово consume-ится до мутации и после потери ответа/неудачи не
  переиспользуется — нужен новый witness и новый candidate;
  прямой public-shadow→assist отклоняется;
- из public/private shadow плановое выключение меняет только
  `ENABLED=1→0` через `secondary_shadow_disable` и сохраняет privacy
  bit; из assist используется `secondary_assist_to_disabled`;
  ручная правка live env запрещена, а unfinished activation продолжается
  только через `recover-activation`.

Для P1 GPT-OSS semantic supervisor shadow дополнительно:

- без semantic ENV candidate стартует с
  `FRIDAY_SEMANTIC_SUPERVISOR_MODE=off`, пустым task allowlist и не оборачивает
  существующий runtime; неизвестный mode, пустой/невалидный allowlist или
  невалидные bounds fail closed к unchanged primary path;
- scheduler допускает `plan_candidate` только поверх exact accepted profile
  `gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`,
  accepted manifest SHA-256
  `93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`
  и `FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=1`; mismatch, public-text mode,
  disabled/misconfigured secondary и product-policy drift закрывают workload;
- code-owned policy `gptoss20b-semantic-supervisor-v1` имеет SHA-256
  `edea7fce6ae8d9bfcbe461a3f90d98bd9aab897ebe7712cdb23a2d77e8de780c`,
  не recertify-ит runtime и допускает ровно task classes
  `compare_current_file_with_current_web` и
  `compare_archive_with_current_web`; `plan_candidate` не добавляется в общий
  `FRIDAY_SECONDARY_LLM_WORKLOADS`;
- `semantic_supervisor_shadow_enable` меняет только exact semantic block:
  `MODE=shadow`, обе task classes в canonical order, `MAX_STEPS=6`,
  `MAX_REVIEW_ROUNDS=0`, `TIMEOUT_SEC=12`; predecessor — exact current
  accepted private secondary production state (`ENABLED=1`,
  `ALLOW_PRIVATE_TEXT=1`, `MODE=assist`, `WORKLOADS=document_map,extract`,
  `DOCUMENT_MAP_MODE=assist`, exact finalist model/profile/timeouts, API-key
  shape и CA digest), все secondary и остальные ENV bytes неизменны;
  semantic predecessor при этом либо exact canonical off, либо legacy exact
  absence всех пяти keys поверх того же prerequisite. Partial/unknown block
  запрещён; absent predecessor source обязан давать uninstalled/effective-off
  health, а pre-backup rollback/recovery сохраняет отсутствие побайтно;
- `semantic_supervisor_shadow_disable` отдельным candidate меняет только эти
  пять ключей к `MODE=off`, пустому `TASKS`, `MAX_STEPS=6`,
  `MAX_REVIEW_ROUNDS=1`, `TIMEOUT_SEC=12`; тот же exact secondary state
  сохраняется побайтно, ручная live ENV правка запрещена;
- staged file лежит непосредственно в `state_dir`, имеет mode `0600` и exact
  `--next-env-file-sha256`; accepted layout — unchanged unrelated bytes, пять
  semantic keys в lexical sort order, затем прежний canonical sorted secondary
  block. Semantic append после `FRIDAY_SECONDARY_LLM_*` отклоняется;
- `.env.example` и output `jericho init` заканчиваются contiguous lexical
  default-off semantic block. Placeholder example не является accepted ENV:
  initial secondary transition удаляет его placeholder secondary keys и
  append-ит exact accepted canonical secondary block после semantic EOF;
- scheduler policy допускает только exact P1 bounds `MAX_STEPS=6` и
  `MAX_REVIEW_ROUNDS=0`; ручные steps `1`/`2` и review `1` дают
  `closed_reason=invalid_bounds`, effective off и не устанавливают
  `plan_candidate`;
- requested `assist`/`canary` не повышают authority: их effective mode остаётся
  `shadow`, `promotion_admitted=false`; proposal только парсится/валидируется и
  никогда не выбирает route, не исполняется, не публикуется, не меняет primary
  prompt/Work Item и не создаёт tool/effect/knowledge-write operation;
- admitted assist/canary policy `gptoss20b-semantic-supervisor-v2` имеет exact
  SHA-256 `95dc4ae7e246e7104b1e1cd036ea9706fdb014de6889d69789fca66cec9fd98b`;
  shadow evidence с иной policy identity не может выдать это право само по себе;
- sidecar возвращает exact primary result и вызывает primary ровно один раз;
  secondary attempt стартует только после успешного primary, bounded четырьмя
  pending tasks и исходным deadline; каждый созданный request до adapter
  проверен против exact 3 328-byte input budget 4K profile, а ASCII/Cyrillic
  boundary fixtures доказывают максимальный посимвольный UTF-8 prefix и
  fail-closed следующий символ; full-source secret/path guard сохраняется до
  этой проекции. Pending owned/uncertain, cancel/ordinal,
  reply/replay/body-explicit mode/voice/synthetic и прочие special/exact
  surfaces не семплируются (session-restored mode при этом только передаётся
  primary); из ingestion results допускаются только exact closed transient
  `web_request`, `archive_search_request` и synthetic `system_notice`, все
  остальные формы fail-closed обходятся;
- батарея laptop-off/startup, endpoint unavailable, profile drift, private-text
  rejection, timeout, malformed/duplicate-key proposal, policy rejection и
  saturation подтверждает fail-closed shadow и unchanged primary path без
  duplicate tool/effect/publication;
- concurrent foreground workload вытесняет lowest-priority `plan_candidate`,
  semantic permit освобождается cancellation-safe и не создаёт чужой
  `admission_busy`; semantic protocol/product failures не меняют shared circuit
  или epoch; late pre-dispatch guard закрывает stale same-conversation/pending
  attempt до HTTP POST, учитывает его как `invoked=false`/`not_called`, отделяет
  `endpoint_admission_total` от фактического `endpoint_request_total`, а
  superseded/close cancellation не теряется из body-free observations;
- health/metrics проекции body-free: response `/api/health` содержит
  `secondary.semantic_supervisor` и top-level `semantic_supervisor`; второй
  показывает `installed`, discarded-shadow role, requested/effective mode,
  `promotion_admitted=false`, unchanged runtime/primary publication ownership,
  запреты tools/effects/execution и, только когда установлен, policy/profile
  identity, pending bounds и bounded counters. Owner diagnostics
  `secondary.workloads.plan_candidate` содержит только closed routing,
  availability/reason и scheduler counters; raw message/prompt,
  document/query/proposal/model body, path, actor/conversation ID и endpoint
  error text отсутствуют;
- при admitted scheduler top-level `semantic_supervisor.installed=true`, а
  `orchestration.installed_mode` по-прежнему совпадает с underlying router;
  shutdown закрывает sidecar, затем underlying `OrchestrationRouter`, без
  double-close; optional evaluator не может удерживать этот shutdown больше
  одной секунды. Иное состояние означает incomplete production wiring и красный
  release gate;
- immutable enable health gate принимает только installed/effective shadow с
  exact policy/profile и `workload_available=true` (при этом laptop-off и
  `runtime_available=false` допустимы); disable принимает только
  uninstalled/effective off;
- exact offline command
  `.venv/bin/python -I -B tools/evaluate_semantic_supervisor_offline.py --fixtures tests/fixtures/semantic_supervisor_offline_v1.json`
  возвращает deterministic canonical schema
  `friday.semantic-supervisor-offline-evaluation.v1`: 10/10 fixture
  conformance, 10/10 runtime invariant conformance и фактически измеренную
  primary-once/response-identity-and-value parity; instrumented tripwire counters дают
  zero execution/publication/effect, `network_used=false`,
  `promotion_evidence=false`,
  `acceptance_authority=none`;
- offline report всегда помечен
  `synthetic_offline_only_not_live_shadow_or_canary_acceptance`: он не заменяет
  live shadow/canary evidence и не является promotion receipt;
- P2 web execution не admitted. Две task-class метки в P1 разрешают только
  discarded semantic proposals; нынешний `web_research` имеет `risk=mutate` и
  persistence/publication metadata. Нужны отдельная P2 mutation/persistence
  граница и новое representative live evidence, прежде чем proposal сможет
  влиять на web work selection.

Для P5 semantic-supervisor effect shadow дополнительно:

- canonical config содержит contiguous lexical block из
  `EFFECT_EVIDENCE_FILE=`, `EFFECT_EVIDENCE_SHA256=` и `EFFECT_MODE=off`;
  unknown/partial/duplicate block закрывается, а invalid raw mode не
  отображается как валидный `off`;
- `effect_planning` — самостоятельная lowest-priority scheduler lane с exact
  policy/profile и 128 output tokens; она не заимствует admission у
  `plan_candidate`, требует `ALLOW_PRIVATE_TEXT=1`, имеет отдельные counters и
  корректно диагностируется даже без обычных secondary workloads;
- maturity artifact создаётся только pure producer-ом `effect-maturity` из
  exact accepted production baseline, CANARY promotion bundle и CANARY latency
  budget. Loader повторно требует complete==observed, full trace join,
  `none:none` для каждого promoted outcome, один promotion evidence, latency в
  budget, proven primary/laptop fallback, primary publication owner и нули по
  hidden owner, duplicate и regression counters;
- evidence — owner-private canonical regular file mode `0400|0600`, no-link,
  no-overwrite, с exact raw SHA-256, installed source revision, current
  read-registry binding зрелого CANARY evidence и отдельным current
  effect-registry binding. Последний заранее выводится body-free командой
  `effect-registry-binding`, а при boot независимо сверяется с enabled Obsidian,
  exact AuthorizationService/ExecutionKernel и зарегистрированными
  `obsidian_create_note|obsidian_append_note`. Любой drift, malformed JSON или stale
  process-witness оставляет effect wrapper uninstalled;
- wrapper устанавливается только после закрытия first-party Obsidian organ
  capability/tool contour и его risk declarations (поздние workspace MCP tools
  намеренно не входят в effect-registry identity),
  вызывает primary ровно один раз и возвращает тот же объект. Model request
  возможен только после reread exact user-scoped durable assistant row и
  accepted effect receipt; pre-dispatch и post-response admission проверяются
  повторно;
- model-visible grammar допускает только `none:none`, Obsidian `create` и
  Obsidian `append`; arguments, note body/path, IDs, permissions,
  confirmations, idempotency/effect/tool/publication handles, host application,
  install, network, shell, delete и unknown symbols отклоняются;
- persisted observation содержит только keyed/domain-separated identities и
  closed symbols/counters. Raw projection/selection digests, prompt, response,
  path, actor/conversation/message IDs и primary body отсутствуют;
  process-lifetime dedupe не допускает повторный dispatch того же accepted
  effect/outcome из другого message scope либо изменённого outcome того же effect:
  atomically проверяются оба accepted `effect_id_sha256` и `outcome_sha256`.
  Non-rotating 512 KiB Bloom не вытесняет старые identities; false positive
  может только пропустить optional observation;
- top-level `semantic_supervisor_effect` exact health связывает active shadow с
  configured evidence SHA, maturity facts SHA, installed source revision,
  read registry binding, effect registry binding и exact policy;
  execution/publication всегда false. Off,
  loader/binding/composition exception и laptop-off не являются boot gate;
- immutable `semantic_supervisor_effect_shadow_enable|disable` меняет только
  exact effect block и сохраняет unrelated, primary, secondary и P1–P4 bytes.
  Enable валидирует полный maturity artifact до service stop; rollback/recovery
  к legacy predecessor проверяет прежний capability-aware health contract и не
  ждёт отсутствующее P5 поле;
- ASSIST/CANARY activation и representative-window consumers принимают только
  exact independent `effect_shadow` scheduler projection; добавление поля не
  ломает существующий promoted rollout, неизвестные nested keys/state закрывают
  его;
- P6 release review обязан сначала получить current
  `NO_ELIGIBLE_CANDIDATE`. Source-only evidence/preimage witness никогда не
  выдаёт deletion authority; invariant/mixed surfaces, rename/move/copy
  прежнего normalized AST и registry/commit drift должны отклоняться. Exact Git
  capture ограничивается во время чтения, blob size проверяется до body load,
  а полный `friday/**/*.py` scan обязан уложиться в 1 024 файла, 32 MiB и
  4 194 304 AST nodes, парся по одному модулю без tree-wide AST cache; его
  receipt остаётся aggregate-only и body-free.

Для V12 file/archive slice дополнительно:

- без явных переменных configured/installed mode остаются `legacy`, routes пусты;
- canary стартует только с `model_gate.status=canary_ready`, reason
  `live_attestation_clear`, профилем
  `qwen38-27b-nvfp4-sglang:dispatcher:v12.15` и явно разрешёнными routes
  `file_read`, `archive_read`;
- SGLang startup сверяет exact model revision
  `43aa7ff5eef05ab50a3bfa6aca581085312c7a04`, pinned runtime image/source,
  served alias `dispatcher`, bounded `/metrics`, `/server_info` и secret-free
  per-process deployment witness; build-time witness hashes берутся только
  из code-owned profile, не подставляются вручную;
- deployment witness подтверждает graph-only launch: context/total tokens
  `40960`, running/Mamba cache `6`, `mem_fraction_static=0.90`, FP8 E4M3 KV,
  Radix/speculation off, full decode CUDA graph batches `1..6`, prefill graph off,
  FlashInfer attention, CPU MM transport и limits `image=4,video=0,audio=0`;
- post-context load допускает bounded convergence не более 20 секунд с шагом
  50 мс только для valid same-epoch busy; invalid/epoch/deadline fail-closed, а
  initial idle и post-cancellation quiet остаются строгими;
- final startup health имеет `status=ok`, `version=0.207.77`, configured/installed
  `canary`, routes `[archive_read, file_read]`, точный `profile_id`,
  `verified_context_tokens=40960` и непустой public `attestation_sha256`;
- q38 выбирает только минимально достаточный closed tier из
  `8192/16384/24576/32768/40960`, тогда как legacy/Qwen3.6 сохраняет 8192;
  small-input smoke получает 8192, а вход больше baseline либо worst-case
  verifier reserve получает ровно первый достаточный attested tier;
- acquire каждого exact requirements выполняется один раз: reject, timeout,
  stale lease, process-epoch drift и capacity loss не вызывают reacquire/retry;
  tier и requirements digest должны совпадать в lease, plan/binding и принятом
  process-owned result, а swap отклоняется до публикации;
- current-file/web smoke на q38 сохраняет полную проекцию, если она помещается
  в attested tier; тот же вход на 8192 остаётся честным
  `LOCAL_CONTEXT_TRUNCATED` без скрытого расширения authority;
- синтетические 1- и 2-файловые UTF-8 smokes дают одну публикацию с точными
  citations, без повторного legacy-вызова после выбора V12;
- `archive_read` допускает только self-owned prior exact UTF-8: уникальное точное
  имя, ровно 1–2 последних файла либо не более двух файлов за локальные
  «сегодня / вчера / позавчера»;
- ambiguity, другой пользователь, reply/replay, запрос более двух файлов,
  PDF/OCR/partial extraction уходят в legacy до чтения исторического body;
- после подготовки архива изменение selector/source/permission отклоняет
  публикацию при финальной exact reauthorization, а conn-scoped idempotency
  fence и сообщение либо commit-ятся вместе, либо вместе rollback-ятся;
- неуспешная аттестация оставляет installed mode `legacy` и пустой список routes;
- Sentinel в `canary`/`v12` даёт owner-only alert ровно один раз на
  непрерывный `revoked`/`not_installed`/observer-unavailable эпизод,
  после recovery перевооружается, уважает quiet hours, не утекает private
  reason и молчит при ordinary route fallback и configured `legacy`;
- rehearsal возврата `FRIDAY_ROUTER_MODE=legacy` не меняет и не восстанавливает
  SQLite, а bridge запускается только после зелёного backend health.

## 3. Compatibility

Создать БД предыдущей опубликованной версией, наполнить всеми основными типами данных и открыть release candidate.

Проверить:

- schema version = 48;
- первый cutover 46→47 требует проверенный pre-migration recovery set и две
  distinct sealed schema-47 identities: stable `0.207.73` активируется с
  `0.207.72/schema46` как previous и неактивированным `0.207.73rc0/schema47`
  как fallback; rc0 и stable различаются только version identity. После clear
  sealed stable `0.207.73` становится schema-47-capable fallback для отдельного
  bounded passage writer release;
- cutover 47→48 аналогично требует `0.207.76rc0/schema48` как
  неактивированный schema-capable fallback; stable `0.207.76` отличается
  только version identity, а previous остаётся `0.207.75/schema47`;
- `0.207.77/schema48` не меняет DDL и принимает exact stable
  `0.207.76/schema48` одновременно как previous и immutable fallback;
- schema 43 → 44 атомарно добавляет exact dormant body-free
  `compare_current_file_with_current_web` WorkGraph: ровно два independent
  read-шага и один dependent primary-synthesis, immutable identities,
  bounded CAS/restart state, один review-admitted retry только для failed
  current-web read и раздельные exact full/terminal publication receipts;
- schema 44 → 45 сначала побайтно проверяет exact canonical DDL WorkGraph из
  unreleased feature predecessor `c287468` и
  только затем добавляет immutable `anchor_request_binding_sha256`; старые
  graphs получают explicit unbound sentinel, новые принимают только code-owned
  body-free ingress binding, а request body/source ref не восстанавливаются и
  не сохраняются; fixture correction не переопределяет production DB — live
  predecessor остаётся schema 43;
- archive разговора атомарно добавляет один code-owned assistant receipt
  `conversation_archived` и закрывает graph; bounded expiry seam делает то же
  с `expired`, без model/tool calls, evidence claims и completion claim;
- explicit user cancellation использует отдельную причину `cancelled`, один
  code-owned anchor reply и атомарный CAS; stale/replay/ошибка не оставляют
  assistant без terminal graph;
- сама schema migration не активирует expiry scheduler, web/model execution или
  production route: source-wired assist остаётся default-off и допускается
  только отдельным evidence-bound runtime/lifecycle gate;
- schema 42 → 43 атомарно добавляет exact durable Host Action jobs,
  person-scoped idempotency, restart-safe `unknown`/reconciliation и append-only
  lifecycle events; Host Capability Plane остаётся выключенным по умолчанию;
- текущий опубликованный baseline 0.207.34 имеет schema 42; exact upgrade
  0.207.34 → 0.207.35 обязан дать schema 43 и
  сохранить все schema-42 данные до Host Control acceptance;
- historical release 0.207.26 имеет schema 41; переход
  0.207.26 → 0.207.27 атомарно добавляет exact body-free dormant
  conversation/document Work Item projection; writer, admission и runtime route
  в этом release не активированы;
- переход 0.207.28 → 0.207.29 не меняет schema 42 и активирует полный
  selected-message → durable Q1/Q2 → exact comparison vertical с повторной
  авторизацией источников, независимой проверкой и WorkTrace;
- переход 0.207.29 → 0.207.30 не меняет schema 42, secondary
  ENV или полномочия; он только устанавливает fail-closed live-evidence
  gate, а document-map assist остаётся закрыт до отдельного
  evidence-bound candidate;
- переход 0.207.30 → 0.207.31 не меняет schema 42, endpoint, accepted runtime
  profile или extract assist; он меняет только document-map shadow→assist через
  exact code-owned v2 policy и одноразовый accepted live receipt;
- live release 0.207.24 уже имеет schema 41; переход 0.207.24 → 0.207.26
  не меняет schema и готовит только distinct bounded assist из
  уже принятого private discarded shadow;
- live release 0.207.23 уже имеет schema 41; переход 0.207.23 → 0.207.24
  не меняет schema и готовит только distinct private discarded
  shadow с `ALLOW_PRIVATE_TEXT=0→1`, fresh public product receipt и
  неизменной primary-only властью над ответами/эффектами;
- предыдущий release 0.207.22 уже имеет schema 41; переход 0.207.22 → 0.207.23
  меняет только code-owned admission exact GPT-OSS finalist с provisional на
  accepted и сохраняет public discarded `shadow/extract` без private text;
- предыдущий release 0.207.20 уже имеет schema 41 и reader нового receipt;
  переход 0.207.21 → 0.207.22 не меняет schema и активирует natural selected-source questions
  проверенного объяснения только для заново подтверждённого выбранного
  архивного evidence;
- предыдущий release 0.207.19 уже имеет schema 41; переход 0.207.19 → 0.207.20
  не меняет schema и заранее устанавливает rollback-safe reader для durable
  receipt проверенного объяснения выбранного архивного источника; runtime-writer
  в этом релизе ещё не активирован;
- предыдущий release 0.207.18 уже имеет schema 41; переход 0.207.18 → 0.207.19
  не меняет schema и добавляет body-free effect outcome с observe-only
  reconciliation для Obsidian create/append;
- предыдущий release 0.207.17 уже имеет schema 41; переход 0.207.17 → 0.207.18
  не меняет schema и перераспределяет только фактически неиспользованный
  bounded budget DocumentCatalog worker;
- исторический переход 0.207.16 → 0.207.17 активирует bounded
  worker/archive consumer без изменения schema;
- исторический переход 0.207.15 → 0.207.16
  атомарно добавляет exact body-free DocumentCatalog и явный bounded backfill;
- предыдущий release 0.207.13 имеет schema 39; переход 0.207.13 → 0.207.14
  атомарно добавляет schema-40 durable archive candidate projection;
- исторический переход 38 → 39 атомарно перестраивает/copy-переносит
  `work_items`, добавляя закрытые labels `RecallSelectedArchiveEvidence` и
  body-free sidecar выбранного archive source;
- counts старых строк не изменились без предусмотренной миграции;
- `integrity_check=ok`, foreign-key violations = 0;
- FTS/retrieval работают;
- durable idempotency replay сохранён;
- повторное открытие идемпотентно.

## 4. Concurrency and endurance

- не менее 20 dual-process first-init запусков одной новой SQLite DB;
- второй backend и второй bridge fail-closed до side effects;
- bounded ingestion/retrieval/feedback smoke с тысячами объектов;
- после теста: integrity/FK/FTS clean, нет orphan/staging/runtime мусора;
- записать median/p95, не превращая локальный smoke в универсальный benchmark.

## 5. Clean package

ZIP:

- без лишнего верхнего каталога;
- фиксированные timestamps/permissions/order;
- CRC clean;
- нет absolute/path traversal/symlink entries;
- нет `.git`, caches, `.env*`, runtime DB/WAL/SHM, logs, user files, exports или model weights;
- model placeholder присутствует.

Wheel:

- построен дважды с одинаковым `SOURCE_DATE_EPOCH` и совпал byte-for-byte;
- установлен в новый venv;
- импорт идёт из `site-packages`;
- package/runtime/CLI version совпадают;
- Admin UI package data присутствует;
- `pip check` clean;
- metadata содержит `venv_relocation_contract=absolute-final-v1`;
- после удаления `__pycache__` exhaustive byte-scan всего дерева и symlink targets
  не находит ни одной ссылки на staging root;
- каждый установленный console script имеет exact final
  `<RELEASE>/venv/bin/python` shebang и ровно одну совпадающую hash/size строку
  `*.dist-info/RECORD`; `activate`, `activate.csh`, `activate.fish` и
  `pyvenv.cfg` связаны с тем же final venv;
- прямые `<RELEASE>/venv/bin/friday --help`, `jericho --help` и
  `pip --version` проходят до и после атомарной публикации;
- hermetic Bash source-smoke до публикации подтверждает effective
  `VIRTUAL_ENV`/первый элемент `PATH`, а после публикации — также exact
  `command -v python`, `sys.prefix` и `sys.executable`.

Wheel, построенный из чисто распакованного ZIP, должен совпасть с опубликованным wheel.

Единственный release path:

```text
<SOURCE_PYTHON> -I -B tools/immutable_release_operator.py build ... \
  --secondary-product-runner \
  <EXACT_CLEAN_CANDIDATE_CHECKOUT>/deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py \
  --secondary-product-runner-sha256 <RUNNER_SHA256>
<CANDIDATE>/venv/bin/python -I -B \
  <CANDIDATE>/artifacts/immutable_release_operator.py install-units ...
<CANDIDATE>/venv/bin/python -I -B \
  <CANDIDATE>/artifacts/immutable_release_operator.py activate ...
<EXECUTOR>/venv/bin/python -I -B \
  <EXECUTOR>/artifacts/immutable_release_operator.py recover-activation ...
<CANDIDATE>/venv/bin/python -I -B \
  <CANDIDATE>/artifacts/immutable_release_operator.py recover-historical-album ...
```

Runner и digest берутся из чистого checkout exact candidate commit. Sealed
release-копия mode `0400` является trust anchor, а product-stage запускается из
чистого checkout активного predecessor; standalone запуск release-копии не
является допустимым evidence path.

`install-units`, `activate` и recovery не запускаются из source tree: только
sealed interpreter и копия оператора, связанные с exact release tree. Оператор
управляет только `friday-backend.service` и `friday-bridge.service`.
Ошибка проверки после атомарной публикации не удаляет уже видимое immutable-имя
commit и не разрешает retry того же commit: sibling остаётся quarantined без
clear receipt, а исправление получает новый commit.

Phase-A retry без уже потреблённых alias claims допустим после terminal
`rolled_back` или `recovered` только при неизменных persistent/retry scopes,
доказанных DB mutation + network uncertainty + `writer_target=fallback`, exact
том же candidate и lineage, где прежний fallback становится одновременно
previous и fallback. Любое отсутствие evidence либо смена env/path/health/unit
identity остаётся release-blocking.

`recover-historical-album` не считается успешным после CAS и restart. CAS receipt
имеет только `status=pending`; final `status=clear` требует, чтобы exact final
bridge фактически завершил единый десятистрочный album turn, а оператор дважды
прочитал inbox в `mode=ro/query_only`, увидел отсутствие всех десяти exact rows и
между наблюдениями повторно принял identity bridge. Gate ограничен 600 секундами
и crash-resumable из `bridge_accepted`; timeout, частичное исчезновение или новый
`dead_letter` не создают completion receipt. Повтор terminal-команды заново
проверяет durable отсутствие строк, а не доверяет одному journal-флагу.

Перед переключением оба writer-процесса останавливаются, а SQLite, WAL и Telegram
inbox копируются и проверяются как один recovery set. Candidate устанавливается
только из wheel в новый sealed release root; backend проходит exact process и
TLS `/api/health` gate до запуска bridge. Release anchor меняется атомарно.
До любой попытки запуска candidate backend можно вернуть previous anchor и exact
schema-33 DB/inbox snapshot. Начиная с `backend_start_attempted`, даже до health и
запуска bridge, старый snapshot не восстанавливается и schema-33 binary не
запускается; rollback идёт на заранее собранный sealed schema-34 fallback.
Поштучная замена файлов внутри установленного venv запрещена.

## 6. Installed-package smoke

Из нового venv:

- `/api/health` = 200;
- `/admin/` = 200;
- protected endpoint без token = 401, с owner token = 200;
- `jericho status`, `doctor`, `backup`, `verify-backup`, `restore-backup`, `export-user`;
- основные ingestion/retrieval/agent/Admin сценарии из раздела 2.

## 7. Distribution ownership

Перед публичной публикацией владелец проекта отдельно выбирает лицензию и политику disclosure. Отсутствие файла `LICENSE` означает, что публичная open-source лицензия не предоставлена. Для внешнего релиза также зафиксируйте SBOM/список зависимостей, результаты online CVE audit и канал для security reports. Эти решения нельзя придумывать автоматически от имени владельца.

## 8. Deployment checks — не заменять предположениями

Отдельно на целевой машине проверить и записать результаты:

- Windows native path/permissions/service lifecycle;
- Python 3.11 и 3.12;
- Docker Compose startup/shutdown/upgrade — только для отдельно заявленного
  Docker-контура; native primary Friday этим пунктом не сертифицируется;
- NVIDIA driver/runtime, реальная загрузка Qwen и длительный inference;
- OCR/vision quality на репрезентативных scans;
- живой Telegram Bot API, callbacks, attachments, outage recovery;
- online dependency/CVE audit.

Если среда не позволяет выполнить пункт, validation report должен прямо сказать «не проверено», а не подменять его структурной проверкой.
