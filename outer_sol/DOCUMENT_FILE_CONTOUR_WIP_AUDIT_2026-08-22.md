# Document / file contour: инвентаризация незавершённой работы

Дата среза: 2026-08-22, Europe/Moscow.

Это инвентаризационная записка, а не команда немедленно удалять файлы. Проверка
была read-only: без `fetch`, `pull`, переключения веток, тестов и изменений в
рабочих деревьях. Git-выводы относятся к локальному `origin/main` на момент
проверки: `9cc53d759ab1dde5d6d716b70f7a73862082d8f3` (Friday 0.207.4).

## Короткий вывод

Основной выпущенный document/file contour не висит незавершённым. Старые
document-ветки, document gates и исправления detailed attachment review уже
вошли в `origin/main`. То, что сейчас выглядит как незавершённая работа, делится
на три группы:

1. активная параллельная работа второго Sol — не трогать;
2. старые рабочие деревья и большой superseded overlay — убрать после безопасной
   архивации и повторной проверки, когда второй Sol закончит;
3. несколько идей и тестов, которые могут быть полезны, но их следует заново
   реализовывать от свежего `origin/main`, а не переносить старые blobs целиком.

## Не трогать: активная работа

### Obsidian acceptance / structured operations

- Worktree:
  `/home/jericho/.jericho/runtime/friday-obsidian-acceptance.worktree`
- Ветка: `hotfix/obsidian-acceptance-battery`
- База на момент среза совпадала с локальным `origin/main` (`9cc53d7`).
- Файлы менялись прямо во время аудита. Последний срез показывал 13 изменённых
  tracked-файлов и 7 новых файлов, около `+1896/-28`; эти числа быстро устаревают.
- Реализуются структурные секции Markdown, задачи, шаблоны, Obsidian Bases,
  preserve-both conflict preview, vault mutations и их маршрутизация.

Основные новые файлы:

- `friday/organs/obsidian/structured_notes.py`
- `friday/organs/obsidian/task_index.py`
- `friday/organs/obsidian/templates.py`
- `friday/organs/obsidian/base_spec.py`
- `friday/organs/obsidian/note_merge.py`
- `tests/test_obsidian_structured_acceptance_core.py`
- `tests/test_obsidian_vault_mutations.py`

Это живая работа второго Sol. Не форматировать, не тестировать из другого
процесса, не делать reset/rebase/checkout и не удалять worktree.

### Interaction Control Plane

- Worktree:
  `/home/jericho/.jericho/runtime/friday-interaction-control-plane.worktree`
- Ветка: `feature/interaction-control-plane`
- Коммит: `f38a0d301546ef2c9b052c81b00c9868d33cc417`, также присутствует в
  `origin/feature/interaction-control-plane`.
- Дерево чистое; это намеренно сохранённый checkpoint, а не deployment candidate.

Уже сделано: privacy-safe TurnTrace v1, HMAC-идентификаторы, учёт вызовов и
токенов, legacy/V12 file/archive publication traces, restart/continuation и
privacy-контракты.

Оставшаяся работа по записанному плану:

1. failure traces для turns, которые не доходят до assistant publication;
2. episode-level metrics и baseline reports;
3. typed `CapabilityOutcome` adapters для document/message/web reads;
4. schema 36 Work Items — только после стабилизации P0/P1.

Источник статуса:
`outer_sol/INTERACTION_CONTROL_PLANE_IMPLEMENTATION_STATUS.md` в этом worktree.

## Уже слито: не переносить повторно

- Detailed-review кандидат `caa01c23c3342724234f3eb1be3718b278787365`
  уже является предком `origin/main`.
- Последующие исправления attachment review уже в `origin/main`:
  - `0e74a1b` — восстановление публикации attachment review, исправление bounded
    projection/cardinality и transport truncation;
  - `279dcb4` — сохранение read-only описаний DOCX/XLSX;
  - `63e2568` — quarantine same-model judge / более надёжная deterministic review
    проверка.
- Чистые worktree `friday-document-turn-v2`,
  `friday-document-orchestration-v3`, `friday-local-files-format`,
  `friday-detailed-review-hotfix-02064` уже являются предками `origin/main`.
- Gate-кандидаты `7952677`, `22df5e0`, `0c61fe0` и остальные gate31–36 уже
  являются предками `origin/main`.
- Для `0c61fe0` сохранён operator report со `status=passed`, двумя чистыми
  итерациями и пустым списком failure codes. Старые надписи `pending live gate`
  в lock-файлах устарели.
- Смысл ветки `g45-pdf-scan-live-repair` / `a697c48b` — честно отличать scan и
  provenance stub от текстового PDF — уже поглощён более широким коммитом
  `43a365a4`, присутствующим в `origin/main`.

Не cherry-pick'ать эти изменения повторно и не считать старые lock-тексты
источником актуального статуса.

## Убрать после безопасной проверки

Ничего из этого не удалять, пока второй Sol активен. Перед удалением необходимо:

1. убедиться, что соответствующий путь не открыт процессом Codex/тестами;
2. сохранить список refs и `git status`;
3. для dirty/untracked содержимого сделать отдельный архив, bundle или patch;
4. повторно проверить ancestry относительно уже обновлённого `origin/main`.

После этого кандидатами на уборку являются:

### Исторические clean worktrees

- `friday-document-turn-v2.worktree`
- `friday-document-orchestration-v3.worktree`
- `friday-local-files-format.worktree`
- `friday-detailed-review-hotfix-02064.worktree`
- старые document gate31–36 worktrees

Причина: чистые, их HEAD уже являются ancestors `origin/main`; полезный код из
них не потеряется. Сначала сохранить release/operator evidence отдельно.

### Старые lock/status записи

Lock-тексты `pending document contour` / `pending live gate`, относящиеся к уже
пройденным gates, следует удалить либо пометить superseded и дать ссылку на
passed operator report. Сейчас они создают ложное впечатление незакрытого gate.

### G45 backup

- `/home/jericho/backups/friday-g45-pdf-scan`
- Ветка `g45-pdf-scan-live-repair`, HEAD `04e79d39`.
- Содержит untracked `.venv` и `tests/test_g45_probe.py`; сам probe отмечает, что
  superseded тестом `tests/test_g45_natural_refusal_and_scan.py`.

После сохранения ref/bundle можно удалить локальную `.venv`, superseded probe и
сам backup worktree, если он больше не нужен как forensic evidence.

### Старый dirty release snapshot

- `/home/jericho/.jericho/runtime/releases/de37c64`
- Detached `de37c64`, изменения датированы примерно 11 августа.
- Незакоммичены старые версии `agent_runtime`, `ingestion/_files.py` и
  `server.py`.

Сначала архивировать diff и сопоставить его с актуальными исправлениями; затем
удалить как исторический live-residue, если уникального поведения не останется.

### Старый основной overlay

- `/home/jericho/jericho`
- Ветка `main`, HEAD `7f4551a6`, на момент проверки отставала от локального
  `origin/main` на 121 коммит.
- Около 100 dirty tracked-файлов, включая 34 staged, и около 834 untracked.

Это нельзя чистить или ребейзить, пока второй Sol использует каталог. После его
завершения следует сделать полный snapshot индекса, working tree и untracked,
сравнить уникальные blobs с актуальным `origin/main`, вынести только
подтверждённо полезное в новые чистые ветки и лишь затем удалить superseded
overlay. Массовый merge этого дерева в `main` опасен и не нужен.

## Возможно полезно сохранить и переосмыслить

Это кандидаты на отдельные маленькие задачи от свежего `origin/main`, а не на
слепой перенос старых файлов.

### `embeddings_pending` observability

Старый `friday/retrieval/__init__.py` добавляет в retrieval strategy число
knowledge objects без embeddings (`embeddings_pending`). В актуальном
`origin/main` этот сигнал отсутствует. Полезно для честной диагностики неполного
dense index; проверить стоимость запроса и необходимость метрики.

### Поиск документов по имени файла

Старый overlay пытался добавить `metadata_json` в `raw_fts` и поднять схему до
34, чтобы искать filenames. Актуальный `origin/main` уже имеет schema 35, а
`raw_fts` по-прежнему индексирует только `raw_content`.

Потребность может быть реальной, но старую миграцию переносить нельзя. Нужно
спроектировать свежую миграцию поверх текущей схемы либо использовать
`file_source_aliases`/отдельный нормализованный индекс, с migration fixtures и
проверкой external-content FTS rebuild.

### Search recall guarantee

Untracked `tests/test_search_recall_guarantee.py` в старом основном дереве может
содержать полезную продуктовую гарантию. Перед уборкой overlay проверить тест на
дублирование актуальным suite; если он уникален и корректен — перенести как
отдельный тест вместе с минимальной реализацией.

### Synthetic live battery hardening

В старом `tools/synthetic_live_battery.py` и
`tests/test_synthetic_live_battery.py` есть дополнительная проверка
parenthetical/unowned adverse predicate. Это не центральный document contour,
но может закрывать редкий ложный speech-act. Сначала сравнить с актуальными P09
проверками.

### Документальная трассировка release gate

`DOCUMENT_CONTOUR_RELEASE_CRITERIA` остаётся декларативным и не ссылается на
сохранённый passed operator report. Стоит добавить ссылку/идентификатор evidence,
чтобы старые `pending` lock-записи больше не воспринимались как реальный долг.

## Реальная плановая незавершённость продукта

Это не потерянные коммиты и не причина восстанавливать старые деревья:

- единый `DocumentCatalog`;
- semantic titles и passages для pending Raw;
- актуализация embeddings и typed date facts;
- conversation passages;
- единый `archive_search`;
- расширение V12 дальше canary `FILE_READ` / `ARCHIVE_READ` к document, message,
  web и effect handlers;
- document search/details/review HTTP API поверх нынешних upload/list/download;
- Obsidian P5–P9: stable note identity/graph, semantic retrieval, durable tasks,
  typed Bases, managed regions и explicit Inbox ingestion.

Obsidian physical Android/Syncthing acceptance также остаётся незакрытой, но это
ручная проверка окружения, а не незакоммиченный код.

## Рекомендуемый порядок после завершения второго Sol

1. Зафиксировать новые HEAD/status всех worktrees и обновить refs безопасным
   способом.
2. Сохранить dirty основной overlay и старые release leftovers в отдельный
   recoverable архив.
3. Удалить clean merged worktrees и исправить устаревшие lock/status записи.
4. Провести отдельный salvage-review четырёх кандидатов: `embeddings_pending`,
   filename search, search recall guarantee, synthetic P09 hardening.
5. Любую принятую идею реализовывать маленьким коммитом от свежего `origin/main`.
6. Не смешивать cleanup со всё ещё активными Obsidian и Interaction Control
   Plane ветками.
