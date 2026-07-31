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

## Вотчер задач (2026-07-31)

Скрипт `grok/scan_open_tasks.py` — единственный источник «open/done» для
периодического вотчера (не LLM-разбор заголовков).

```
python grok/scan_open_tasks.py --watch-status --json
```

- open = заголовок `## G\d+` без маркера `**сделано**` / `**закрыто**` / DONE / CLOSED
- истина назначения = `origin/main`; локальный «сделано» при открытом origin и
  грязном дереве → `finish_and_push` (не idle)
- state: `grok/.task_watch_state.json` (в `.gitignore`, не коммитить)

## Минимальный порядок внедрения (если владелец возьмёт)

1 → 2 → 7 (качество контекста research без сети-архитектуры)
6 (дешёвый audit)
3 (PDF, с parse budget)
4 → 5 (контроль поиска и исходящих)

Параллельно можно принимать Sol #1–#3: они на других осях.

## Доказательная база «до»

Числа в PROPOSALS — синтетические воспроизведения по коду (маркеры, stub delay),
не замер на живом корпусе владельца. Живой `~/.jericho` не читался.
