# Рабочие заметки Grok по переносу (2026-07-31)

Не канон. Канонические предложения — в `PROPOSALS.md`.

## Прочитано

- `grok/GROK.md`, `grok/BRIEF_PORTING.md`
- `sol/SOL.md`, `sol/BRIEF_PORTING.md`, `sol/PROPOSALS.md` (#1–#3)
- `README.md`, `docs/STATE_2026-07-31.md` (обзор)
- Код: `web_surfer`, `execution_kernel`, `documents`, `retrieval.best_snippet`,
  `permissions` (пресеты web.*)

## Карта: дефект → предложение

| Бриф | Дефект в коде | № в PROPOSALS | Sol? |
|---|---|---|---|
| A | `FetchResult.to_dict` = голова; память уже на `best_snippet` | 1 | нет |
| B | research 8×20k → `to_llm_message` 11.9k с головы | 2 | стык с #3, не дубль |
| C | allowlist без pdf; extractor есть | 3 | нет |
| D | schema query+max_results; dedup strip-only | 4 | нет |
| E | user имеет web.search; нет per-host/actor cap | 5 | нет (не HITL) |
| F | `_audit_details` только code_run | 6 | стык с #3 |
| G | gather + kernel 30s vs fetch 60s | 7 | нет |

## Сознательно не трогал

1. `Organ.tools()` / второй реестр — Sol #1
2. HITL / `default_requires_hitl` — Sol #2
3. `WebEvidence` / `[W#]` легенда — Sol #3
4. Итеративный multi-hop research как сущность — брифинг Sol §D
5. Запрещённый перенос: browser, packages, arbitrary FS, process mgmt, code.run

## Вотчер задач / автопилот Grok (канон на 2026-07-31 вечер)

Скрипт `grok/scan_open_tasks.py` — единственный источник «open/done» для
периодического вотчера (не LLM-разбор заголовков). При action ≠ idle —
довести до gate + push в `main` (без force).

```
python grok/scan_open_tasks.py --watch-status --json
# или просто:
python grok/scan_open_tasks.py
```

| action | смысл |
|---|---|
| `idle` | open 0 на origin и worktree |
| `implement` | есть open G, дерево чистое — делать с нуля |
| `continue_wip` | open G + dirty — дожать WIP, не переписывать |
| `finish_and_push` | origin open, local уже «сделано», dirty — push |

- open = заголовок `## G\d+` / `# … G\d+` без `**сделано**` / `**закрыто**` / DONE / CLOSED
- state: `grok/.task_watch_state.json` (gitignore)
- репо: `D:\jericho-src`, ветка `main`
- закон: `grok/GROK.md` + `grok/TASKS.md`
- gate до push: ruff / ruff format / mypy / bandit / node --check / pytest
- мутация обязательна для новых тестов

### Конфиг Grok Build scheduler (восстановить при «подними автопилот»)

| поле | значение |
|---|---|
| interval | **2m** (было 3m; владелец: «давай каждые 2 минуты») |
| durable | true |
| foreground | false (background subagent) |
| cwd / workdir | `D:\jericho-src` |
| last known id | `019fb97cc529` (сессия 2026-07-31; id не переживает restart — создать заново) |

Промпт (кратко, полный — в сессии scheduler):

```
Ты — Grok-исполнитель Jericho (D:\jericho-src). Закон: grok/GROK.md + grok/TASKS.md.
1) cd D:\jericho-src; git pull --ff-only (или rebase если нужно)
2) python grok/scan_open_tasks.py  → action + open_ids
3) idle → ответь «idle» и выйди
4) иначе: implement/continue/finish по action_ids из TASKS.md
5) полный gate (§4 GROK.md), commit по-русски, push main (без --force)
6) короткий отчёт: id, action, commit SHA или idle
```

На ночь 2026-07-31 watcher **выключен** (владелец: баиньки после G23).
Утро: создать scheduler с interval `2m`, durable, промптом выше.

### За сессию 2026-07-31 (Grok)

| id | результат |
|---|---|
| G21 | `dismiss_notification` + kind=reminder; `f648010` |
| G22 | PDF в web_fetch (замер 9/10); `0d8b39b` |
| G23 | состязательный обзор TTS/speak/Telegram; HIGH=0; `dae85ea` |

## Минимальный порядок внедрения (если владелец возьмёт)

1 → 2 → 7 (качество контекста research без сети-архитектуры)
6 (дешёвый audit)
3 (PDF, с parse budget)
4 → 5 (контроль поиска и исходящих)

Параллельно можно принимать Sol #1–#3: они на других осях.

## Доказательная база «до»

Числа в PROPOSALS — синтетические воспроизведения по коду (маркеры, stub delay),
не замер на живом корпусе владельца. Живой `~/.jericho` не читался.
