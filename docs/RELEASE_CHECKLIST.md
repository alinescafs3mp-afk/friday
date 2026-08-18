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
- final startup health имеет `status=ok`, `version=0.205.0`, configured/installed
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

- schema version = 33;
- между 0.204.2 и 0.205.0 нет migration; model-gate episode использует
  существующий `runtime_kv`, а alerts — существующую notification queue;
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
- `pip check` clean.

Wheel, построенный из чисто распакованного ZIP, должен совпасть с опубликованным wheel.

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
