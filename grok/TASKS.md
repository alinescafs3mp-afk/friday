# Назначено Grok — 2026-07-31

Общий список и живые числа — `TASKS.md` в корне. Закон — `grok/GROK.md`, он выше этого файла.
Порядок — по убыванию пользы. Упрёшься — переходи дальше, причину пиши в `grok/PROPOSALS.md`.

Твоя половина — точная: исполнение решённых задач, тесты с проверкой мутацией,
согласованность доков с кодом. Четыре задачи, все с готовым решением или готовым числом.

---

## G1 (#43). У веб-инструментов нет следа в аудите — **сделано**

`_audit_details` пишет отпечатки для `web_search` / `web_fetch` / `web_research`
(sha256 + длина; для fetch — host, без path/query). Тест на helper и на боевой
`execute` (мутация: пустой dict для web_search → падает).

## G2 (#44). Порог сборщика эталонов 1 → 2 — **сделано**

`MAX_SHARED_TOKENS = 2` с таблицей замера 2026-07-31 в комментарии. Граничный тест:
ровно 2 общих стемма проходят, 3+ — нет.

## G3 (#45). 25 сохранено, 22 в замере — **сделано**

Причина: `save_accepted` считал итерации цикла, а `add_eval_case` upsert'ит по
`(user_id, query)` — дубликаты запросов (в т.ч. после повторного audit списка)
схлопывались. Теперь: casefold-дедуп, счётчик = число distinct. `run_eval` отдаёт
`listed` / `scored` / `skipped_empty_expected`, чтобы потеря была слышна.

## G4 (#48). 200 конфликтов и 20 слияний не разобрать из чата — **сделано**

`/merges` уже был. Добавлено:
- `GET /api/kg/conflicts`, `POST /api/kg/conflicts/{id}/decide`
- Telegram `/conflicts` + callback `conflict:keep_a|keep_b|dismiss`
- инструменты `conflict_list`, `conflict_decide`, `entity_merge_decide`
- порция 5; решённые (status ≠ suggested) не показываются снова

---

# Новое (добавлено 31 июля, после того как все четыре были закрыты)

Все G1–G4 приняты. G3 ты раскопал глубже, чем стояло в задаче: я записал «три эталона
потерялись», а причина оказалась не в потере — `save_accepted` считал итерации цикла,
тогда как `add_eval_case` делает upsert по паре (пользователь, запрос). Правильно.

## G5 (#50). Работа с графом стоит на ПОЛНОЙ выборке сущностей, а выборка кончается — **сделано**

Снята зависимость от `list_entities(limit=5000)` на путях точного упоминания:

- `match_mentions` / `_entity_suggestions` / `backfill_entity_mentions` — кандидаты из
  текста (`mention_phrase_candidates`: n-граммы токенов до 12 слов, многословные имена
  без объявляющего слова сохраняются), lookup
  `find_entities_by_normalized_names` (+ alias без потолка страницы);
- `search_entities` token-overlap — `iter_entities` (полный обход страницами, без
  тихого обреза хвоста алфавита);
- `find_entity_by_alias` больше не ходит через `list_entities(limit=5000)`.

Тесты: `tests/test_entity_match_not_capped_by_listing.py` (оба конца алфавита при
графе >5000, запрет `list_entities` на exact-path, мутация пустого keyed lookup).
Старый `test_entity_suggestions_see_the_whole_graph` остаётся регрессией.

---

# Новое (31 июля, вечер) — G5 принята, дальше вот это

## G6 (#51). Слияние сущностей НЕЧЕМ ОТКАТИТЬ, и это дыра в безопасности — **сделано**

При `merge_entities` в `entity_merge_history.transfer_json` пишется состав
перенесённого: `links_moved` (с `target_link_id`), `links_suppressed` (пересечение
документов — без этого откат врёт), `primary_moved`, `relations` с fate
`moved` / `suppressed_duplicate` / `self_loop_dropped`. `unmerge_entities` возвращает
обе стороны и связи к состоянию до слияния; повторный откат запрещён. Схема 19:
колонки `transfer_json`, `undone_at`, `undone_by`.

Поверхности: `POST /api/kg/merges/{id}/undo`, `GET /api/kg/merges`, то же под
`/api/admin/merges` (`admin.all_data.manage` / read), инструмент `entity_merge_undo`
(`kg.merge`). Тест `tests/test_entity_merge_can_be_undone.py` — пересекающиеся
документы + мутация обнуления `links_suppressed`.

---

# Новое (31 июля, вечер второй раз) — G6 принята

Откат слияния с записью состава перенесённого — именно то, что было нужно, и трудность
с `INSERT OR IGNORE` ты снял правильно. Фикстуру схемы 19 тоже добавил сам, до того как
я успел сказать. Хорошо.

⚠️ Но на будущее: между твоим пушем `0cda590` и фикстурой `main` был **красный** — гейт
падал на `test_the_fixture_set_covers_every_schema_back_to_the_oldest_backup`. Схема
бампится и фикстура добавляется ОДНИМ коммитом, иначе у всех троих ломается прогон.

## G7 (#54). Нерусский текст утекает пользователю на экран — **сделано**

Все статические и f-string `detail=` в `admin_api/` и `api/` переведены на русский
(путь toast: `api()` → `Error(data.detail)` → `toast`). Инвентарь
`tests/test_user_facing_details_are_russian.py`: кириллица обязательна у литералов;
43 динамических `str(exc)` перечислены явно (EXPECTED_DYNAMIC_DETAIL_SITES), молча
не пропускаются. Закреплённые тесты hardening/lifecycle обновлены под новые строки.

## G8 (#55). Источники в ответе не раскрываются до самого документа — **сделано**

Под ответом с цитатами — кнопки `doc:show:{knowledge_id}` с метками K# (тот же
callback, что у поиска/browse/хроники). В тексте: «Кнопкой ниже — открыть источник
целиком.» В админке у легенды источников в истории диалога — кнопка «Открыть» →
`inspectKnowledge`. Тесты: `test_answer_sources_open_as_documents_from_the_legend`.

---

# Новое (31 июля, поздний вечер) — G7+G8 приняты, сканер open-задач тоже

## G9 (#56). Разметить очередь конфликтов: очевидное vs требует внимания — **сделано**

`jericho/conflict_triage.py`: Jaccard по `content_tokens` (стемы), отношение длин,
доля data-diff (заглавная/цифра). Метки `likely_duplicate` /
`likely_different_records` / `uncertain` — только подсказка, `conflict_decide` и
порог 0.95 не тронуты. Поверхности: `conflict_list`, `GET /api/kg/conflicts`,
Telegram `/conflicts` (строка «Метка: …»). Тест
`tests/test_conflict_queue_triage_hints.py` — бланк vs дубликат, HTTP, tool, TG.

---

# Новое (31 июля, вечер) — G9 принята

Подсказки triage и автопилот вотчера — оба в дело, живой экземпляр перезапущен с
твоим кодом. Хорошо, что довёл до `main`, а не остановился на отчёте.

## G10 (#39). Вопрос про человека уходит в болтовню — переизмерить, условие сменилось — **сделано**

Переизмерено на сегодняшнем коде (`graph_expansion=False`). Классификатор и порог
0.35 **не менялись** — отрицательный результат: болезнь ушла вместе с поиском.

**Критерий до замера:** формы из переписки/фикстур при живом досье →
`personal_knowledge|mixed` и `hits>0`; настоящая болтовня → `general_conversation`
при пустых hits.

**Числа (синтетический корпус + формы из регрессий, `grok/measure_g10_answer_mode.py`):**

| стенд | результат |
|---|---|
| person standalone | **8/9** (промах — опечатка «Кирила», hits=0 → поиск, не классификатор) |
| person follow-up («а его…», «её…») | **3/3** |
| chitchat | **8/8** `general_conversation` |

Замечание: «давай про Макарова Кирилла инфу» даёт `mixed` (conf 0.342 < 0.35), но
hits=6 — это не болтовня. Ночной провал был `general_conversation` + hits=0.

Сторож: `tests/test_person_query_answer_mode_remeasure.py` (мутация: всегда
`general_conversation` при hits → падает; пустые hits → `personal_knowledge` → падает).

---

# СРОЧНО (31 июля, ночь) — реальная дыра доступа, найдена разведкой и проверена мной лично

## G11 (#57). Делегированный админ может писать/удалять/выгружать аккаунт ВЛАДЕЛЬЦА — **сделано**

`_protect_owner_target` подключён на всех мутирующих admin-маршрутах с tenant
`user_id`: export, knowledge write/restore/delete/reenrich/links/suggestions,
graph entities/relations, lifecycle, conflicts/resolutions/merges undo, inbox
classify/bulk/advise, eval cases. READ не трогали (заказанная видимость).

Сторож: `tests/test_owner_mutation_boundaries.py` — точечный `POST /exports` +
инвентарный обход manage/export с `user_id=<owner>` → 403; мутация (no-op
protect на export) → 200.

**Это не гипотеза.** Я проверил каждый файл лично, читал код построчно. Самое
серьёзное — делегированный администратор (пресет `admin`, право `admin.export`,
обычная делегируемая роль уровня 3, выдаётся владельцем как рядовая роль) может
скачать **весь личный архив владельца** одним запросом: 150+ МБ, документы,
переписка, сущности. Owner-проверки там нет вовсе.

**Механизм, который должен защищать, уже есть и работает — просто не везде
подключён.** `_protect_owner_target(request, user_id)`
(`admin_api/_deps.py:174-181`): если целевой `user_id` принадлежит владельцу
(`preset_key == "owner"` или легаси id), а актёр не владелец — 403. Используется в
`_users.py`, `_conversations.py`, и ровно ОДИН раз в `_knowledge.py` (только
`purge_knowledge_endpoint`, строка 907). Больше НИГДЕ, хотя `admin.all_data.manage`
и `admin.export` — обычные делегируемые права, а не владельческие.

**Прецедент в тесте, который уже был:** `tests/test_mission_oversight_boundaries.py`
описывает этот же класс дыры, уже найденный и починенный для роутера миссий — «a
delegated administrator could stop the owner's own missions… Token revocation was
hardened against exactly this; missions were missed». Историю повторили — в шести
файлах сразу.

### Точный список маршрутов без защиты (проверено мной, файл:строка = где резолвится user_id)

**`admin_api/_maintenance.py` — САМОЕ СЕРЬЁЗНОЕ, экспорт целого архива:**
- `POST /exports` (:106) → `user_id = str(body.get("user_id") or request.state.actor.user_id)` (:128)

**`admin_api/_knowledge.py` — `_protect_owner_target` уже импортирован (:25), не хватает вызова в:**
- `PATCH /knowledge/{id}` (:787) — **переписывает title/content/summary владельца**
- `POST /knowledge/{id}/restore` (:827)
- `DELETE /knowledge/{id}` (:870)
- `POST /knowledge/{id}/entities` (:672)
- `PATCH /entity-links/{id}` (:744)
- `POST /containers` (:158) — если принимает user_id, проверь
- `POST /knowledge/{id}/reenrich` (:289) — если принимает user_id, проверь
- `POST /entity-suggestions/groups/decide` (:520)

**`admin_api/_graph.py` — импорта `_protect_owner_target` нет вовсе:**
- `POST /entities` (:65), `PATCH /entities/{id}` (:86), `DELETE /entities/{id}` (:104)
- `POST /relation-candidates/bulk-review` (:174), `POST /relation-candidates/{id}/review` (:224)

**`admin_api/_lifecycle.py` — импорта нет:**
- `POST /cleanup/legacy/apply` (:66) — `user_id = str(body.get("user_id") or "")` (:69), до 200 объектов, включая `soft_delete`
- `POST /lifecycle/apply` (:172) — `user_id` на :177
- `POST /lifecycle/deprecate` (:267) — `user_id` на :278

**`admin_api/_conflicts.py` — импорта нет ВО ВСЁМ ФАЙЛЕ:**
- `POST /conflicts/bulk-review` (:66), `POST /conflicts/{id}/review` (:118), `POST /conflicts/{id}/resolve` (:140)
- `POST /knowledge/detect-duplicates` (:191) — через `_target_user`, там же добавить
- `POST /resolutions/detect` (:213), `POST /resolutions/{id}/accept` (:235), `POST /resolutions/{id}/reject` (:253)
- `POST /merges/{id}/undo` (:281) — твой же G6, свежая дыра в свежем коде

**`admin_api/_inbox.py` — импорта нет:**
- `POST /inbox/{id}/classify` (:112), `POST /inbox/bulk` (:145)
- `POST /inbox/{id}/advise` (:231) — проверь, мутирует ли; если только читает LLM без записи — не нужно

### Что НЕ трогать
- `/api/kg.py` (не `admin_api/`) — self-service роутер, все операции идут по
  `actor.user_id`, чужого `user_id` не принимает. Я проверил — не уязвим, не лезь.
- READ-маршруты (`_require(request, "admin.*.read")`) — они уже покрыты
  `_audit_cross_tenant_read`, это другая, работающая защита (видимость, не запись).
  Владелец САМ решил, что делегированный админ видит всё чужое содержимое — это
  заказанная фича (см. память `multiuser-isolation-and-oversight`), а не дыра.
  Трогать только МУТИРУЮЩИЕ маршруты.

### Как чинить
Паттерн уже есть в проекте — скопируй из `admin_api/_users.py` или
`purge_knowledge_endpoint` (`_knowledge.py:904-908`): resolve `user_id`, затем
`_protect_owner_target(request, user_id)` СРАЗУ ПОСЛЕ резолва, ДО первого чтения
или записи в хранилище. В файлах без импорта — добавь `_protect_owner_target` в
`from jericho.admin_api._deps import (...)`.

### Сторожевой тест — ОБЯЗАТЕЛЕН, и он важнее самих правок

Точечные тесты на каждый маршрут — этого мало: следующий новый маршрут повторит
дыру снова, ровно как повторилась дыра из `test_mission_oversight_boundaries.py`.
Нужен **инвентарный** тест по образцу `test_route_inventory.py` /
`test_audit_hardening.py`: обойти все POST/PATCH/DELETE маршруты `admin_api/`,
которые принимают `user_id` (из тела или query) и требуют `admin.*.manage`/
`admin.export` (не `.read`), засеять для каждого владельца + делегированного
админа-неовнера, вызвать с `user_id=<владелец>` и проверить 403.

Маршрут, недостижимый обходом (нестандартная сигнатура), обязан **сообщить о
себе** явным списком «непроверено», а не молча выпасть — тот же урок, что уже
был у `test_audit_hardening` с плейсхолдерами `{...}` в пути.

**Мутация обязательна**: закомментируй один вызов `_protect_owner_target` —
инвентарный тест обязан покраснеть ИМЕННО на этом маршруте, а не просто упасть
где-то. Плюс отдельный юнит-тест на сам `POST /exports` — самый тяжёлый случай,
проверь его руками, а не только инвентарём.

### Приоритет

Это выше всего остального в очереди. Гейт, коммит, пуш — как обычно, но эту
задачу не делить на порции, доводи до конца одним заходом: половина защищённых
маршрутов хуже, чем ни одного — создаёт ложное чувство, что дыра закрыта.

---

# Новое (пока G11 в работе — доразобрал остаток разведки, проверил сам)

## G12 (#59). Два мелких, но реальных дефекта из разведки — низкий приоритет, но не выдумка — **сделано**

### 1. `_user_knowledge_search` — асимметричный клэмп лимита

`execution_kernel/__init__.py:1276-1278`:

```python
found = (
    await self.searcher.search(chosen.user_id, clean_query, limit=max(1, min(int(limit), 20)))
    if self.searcher is not None
    else {"results": storage.search_knowledge(chosen.user_id, clean_query, limit=limit)}
)
```

Ветка с `self.searcher` клэмпит `limit` в `[1, 20]` (совпадает с объявленной схемой
инструмента, `"limit": {"maximum": 20}`). Ветка `else` — нет, сырой `limit` летит в
`storage.search_knowledge` напрямую. Инструмент отдаёт содержимое ЧУЖОГО корпуса
(гейт `admin.all_data.read`), поэтому лимит — не мелочь.

Проверил: сегодня в проде это мёртвая ветка — `server.py:774`
(`kernel.bind_services(..., searcher=searcher)`) всегда передаёт настоящий
`searcher`, `self.searcher` в бою никогда не `None`. Но контракт «схема — верхняя
граница» не гарантирован архитектурно — `execute()` не проверяет параметры по
объявленной JSON-схеме вовсе, каждый обработчик клэмпит сам, и этот забыл. Почини
just этот метод (клэмпни в else-ветке так же, `max(1, min(int(limit), 20))`) —
общую валидацию по схеме заводить не нужно, это отдельный архитектурный вопрос
за рамками находки.

Тест: вызови с `limit=999` через путь без `searcher` (замокай/подставь
`self.searcher = None`), убедись, что дошедший до `storage.search_knowledge` лимит
клэмпнут. Мутация — убери клэмп, тест обязан покраснеть.

### 2. `web_surfer/__init__.py:658` — `except BaseException` вместо `except Exception`

```python
try:
    fetch_result = task.result()
except BaseException:
    failed_sources += 1
    continue
```

Единственное место в проекте, где так поймано — везде рядом принят паттерн
`except asyncio.CancelledError: raise` перед `except Exception` (сверь
`workers/__init__.py:427-428`, `telegram_bridge/_transport.py:513-514`). Здесь
`BaseException` глотает и `CancelledError` — при отмене родительской задачи
(таймаут, остановка воркера) один упавший источник исследования проглотит сигнал
отмены молча вместо того чтобы дать ему распространиться.

Почини по образцу соседних мест: `except asyncio.CancelledError: raise`, потом
`except Exception:` с тем же телом. Тест: замокай `task.result()` так, чтобы
бросало `asyncio.CancelledError`, убедись, что оно долетает наружу из
`WebSurfer.research()`, а не тонет в `failed_sources`.

Обе находки я проверил сам построчно (не пересказ разведки) — низкий приоритет,
бери после G11.

---

## Общее

- Гейт целиком до пуша, `git pull --rebase` перед ним — в `main` пишут трое.
- Мутация обязательна для нового теста.
- Задачи Sol (#39, #42, #46, #47) не бери — столкнётесь в одном файле.
- Не трогай `sol/`. Нужно что-то сказать Sol — пиши в своём `PROPOSALS.md`.

## Три находки из старых записей закрылись сами — не переделывай

Проверил сегодня по коду, в прежних списках они числятся открытыми:

- `ingestion/_review.py` — повторная классификация плодила новый объект: закрыто `eca8821`.
- `ingestion/_advice.py` — совет модели ложился на канонический объект: закрыто там же.
- `admin_ui/static/app.js` — все три (пачки, гонка навигации, пагинация): `BULK_BATCH`
  и `bulkApply` на месте, `pager` есть во всех списках.
