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
- PDF/JPEG/OCR: mandatory ordered `pages[]`, singleton fallback и unreadable boundary
  без ложного `empty_text` success;
- Telegram: per-sibling album receipts, partial replay, crash/timeout uncertainty fence,
  reply edges и request-id correlation;
- weather без явного города не идёт в web; diagnostics требует exact
  capability, а MCP status строится из code-owned projection.

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
- immutable activation сохраняет SQLite/WAL, Telegram inbox и exact Obsidian
  root одним recovery set, а staged ENV1 публикуется только после verified
  backup и удаляется durable после успеха.

Для optional GPT-OSS secondary brain начиная с 0.207.11 дополнительно:

- release принимается и первый раз запускается без `FRIDAY_SECONDARY_LLM_*`:
  health имеет `status=ok`, `version=0.207.25`, `secondary.mode=disabled`,
  `secondary.state=disabled` и `secondary.available=false`;
- `ACCEPTED_SECONDARY_RUNTIME_PROFILES` содержит ровно finalist
  `gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f`
  с accepted-manifest SHA-256
  `93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3`;
  code-owned provisional-реестр пуст;
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
- candidate 0.207.25 должен менять только
  `MODE=shadow→assist` из exact private-shadow predecessor по свежему
  `product-stage --stage private-shadow` receipt; workload, private admission,
  endpoint и accepted profile не меняются;
- assist допускает только валидированную typed Inbox extraction:
  secondary не имеет tools/effects/publication или knowledge-write
  authority, а любой отказ обязан дать exact primary-only fallback;
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

Для V12 file/archive slice дополнительно:

- без явных переменных configured/installed mode остаются `legacy`, routes пусты;
- canary стартует только с `model_gate.status=canary_ready`, reason
  `live_attestation_clear`, профилем
  `qwen38-27b-nvfp4-sglang:dispatcher:v12.14` и явно разрешёнными routes
  `file_read`, `archive_read`;
- SGLang startup сверяет exact model revision
  `bfd9b31207712e0850eec9da32261e8c5ee16af7`, pinned runtime image/source,
  served alias `dispatcher`, bounded `/metrics`, `/server_info` и secret-free
  per-process deployment witness; build-time witness hashes берутся только
  из code-owned profile, не подставляются вручную;
- deployment witness подтверждает graph-only launch: context/total tokens
  `40960`, running/Mamba cache `6`, `mem_fraction_static=0.90`, FP8 E4M3 KV,
  Radix/speculation off, full decode CUDA graph batches `1..6`, prefill graph off,
  FlashInfer attention, CPU MM transport и limits `image=4,video=0,audio=0`;
- post-context load допускает bounded convergence не более 2 секунд с шагом
  50 мс только для valid same-epoch busy; invalid/epoch/deadline fail-closed, а
  initial idle и post-cancellation quiet остаются строгими;
- final startup health имеет `status=ok`, `version=0.207.25`, configured/installed
  `canary`, routes `[archive_read, file_read]`, точный `profile_id`,
  `verified_context_tokens=8192` и непустой public `attestation_sha256`;
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

- schema version = 41;
- live release 0.207.24 уже имеет schema 41; переход 0.207.24 → 0.207.25
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
- Docker Compose startup/shutdown/upgrade;
- NVIDIA driver/runtime, реальная загрузка Qwen и длительный inference;
- OCR/vision quality на репрезентативных scans;
- живой Telegram Bot API, callbacks, attachments, outage recovery;
- online dependency/CVE audit.

Если среда не позволяет выполнить пункт, validation report должен прямо сказать «не проверено», а не подменять его структурной проверкой.
