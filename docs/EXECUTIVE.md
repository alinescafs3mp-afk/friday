# Миссии (executive)

Модуль `executive` координирует высокоуровневые цели («миссии») поверх тех же bounded, review-gated примитивов Jericho. Миссия ничего не пишет в знания напрямую: её итоги проходят через Inbox review, а инструменты вызываются через capability-gated Execution Kernel с actor владельца.

## 1. Модель данных

- **Mission** — цель пользователя с планом, статусом, происхождением и провенансом. Таблица `missions`.
- **MissionTask** — один шаг плана: `seq`, `kind`, инструкция, зависимости (`depends_on` на предыдущие шаги), статус, результат и ссылка на созданный Inbox item. Таблица `mission_tasks`.

SQLite schema — версия **9**: добавлены только эти две таблицы (`CREATE TABLE IF NOT EXISTS`), обновление с schema 8 неразрушающее и идемпотентное.

## 2. Жизненный цикл миссии

```
proposed ─▶ ready ─▶ running ─▶ completed | failed
             │           │
          blocked     cancelled
```

- `proposed` — предложена агентом или worker-ом, ждёт запуска пользователем.
- `ready` — принята к выполнению, ждёт runner.
- `running` — выполняется хотя бы один шаг.
- `blocked` — автономия выключена; выполнение не начнётся.
- `completed` / `failed` / `cancelled` — терминальные.

Статусы задач: `pending → running → done | failed | skipped`. Задача с проваленной зависимостью помечается `skipped`.

## 3. Планирование

Планировщик просит локальную модель вернуть строгий JSON с шагами. Валидация: зависимости — только на предыдущие `seq` (ацикличность), число шагов ограничено `JERICHO_EXECUTIVE_MAX_TASKS_PER_MISSION`, гарантирован хотя бы один `produce`-шаг. При недоступной модели или непригодном ответе — детерминированный fallback из одного `produce`-шага.

## 4. Выполнение

Фоновый worker `mission_runner` продвигает готовые задачи короткими тактами (интервал `JERICHO_EXECUTIVE_TICK_INTERVAL_SEC`), не перекрываясь и не удерживая транзакцию во время работы модели. Шаг выполняется ограниченным tool-loop’ом (бюджет `JERICHO_EXECUTIVE_TASK_TOOL_BUDGET`) над read/gather-инструментами (`memory_search`, `entity_lookup`, `kg_stats`, `inbox_list`, `web_search`, `web_fetch`, `web_research`). Итог `produce`-шага направляется в Inbox как `knowledge_work` candidate — Knowledge Object напрямую не создаётся.

## 5. Управляемая автономия

- `JERICHO_AUTONOMY_ENABLED` (по умолчанию `1`) — общий выключатель выполнения миссий. При `0` миссии создаются, но остаются `blocked`.
- `JERICHO_OPERATOR_FULL_AUTONOMY` (по умолчанию `0`) — авто-запуск миссий, предложенных агентом или worker-ом. При `0` такие миссии ждут запуска пользователем.

Ни один флаг не обходит Inbox review: `produce`-итоги всегда остаются кандидатами на подтверждение.

## 6. Источники миссий

- **Пользователь** — Telegram `/mission цель` или `POST /api/missions`.
- **Агент** — инструмент `mission_propose` (миссия создаётся в `proposed`).
- **Проактивный worker** — `mission_proposer` предлагает одну worker-миссию при большом backlog Inbox, с дедупликацией (не более одной незавершённой worker-миссии на пользователя).

## 7. Интерфейсы

Telegram: `/mission цель`, `/missions` (список с кнопками запуска/остановки).

API (`missions.read` / `missions.create` / `missions.control`):

- `POST /api/missions` — создать и запланировать;
- `GET /api/missions` — список;
- `GET /api/missions/{id}` — миссия с задачами;
- `POST /api/missions/{id}/start` — запустить `proposed`/`blocked`;
- `POST /api/missions/{id}/stop` — отменить.

Admin-инспекция (`admin.missions.read` / `admin.missions.manage`): `GET /api/admin/missions`, `GET /api/admin/missions/{id}`, `POST /api/admin/missions/{id}/cancel`.

## 8. Безопасность

Миссии полностью tenant-scoped: план, задачи и результаты доступны только владельцу. Кросс-тенантный доступ ограничен router-ом `/api/admin/missions`. Вывод планировщика — недоверенные данные: он не может создать знание, слияние или связь в обход permissions, provenance и Inbox review. Каждое решение (create/start/cancel/finish) фиксируется в audit.
