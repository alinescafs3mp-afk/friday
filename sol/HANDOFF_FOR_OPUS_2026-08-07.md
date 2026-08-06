# Handoff для Opus — 0.167.0

## Неподвижные правила

- **ПРИВАТНОСТЬ ГЛАВНЕЕ ЛЮБОЙ ФУНКЦИИ.** Живой контент допустимо читать только
  локально для диагностики. Не копировать его в модельные пробы, внешние сервисы,
  Git, логи или отчёты; наружу — только структурные статусы, числа и хеши.
- `start.txt` — чужой untracked-файл владельца: не читать, не добавлять, не удалять.
- Работа существует только после полного gate, русского commit и push прямо в
  `main`; без force. Перед новым push сверять `origin/main` и не затирать чужой diff.
- Модели проверять только synthetic input. Живой SQLite/WAL не открывать вторым
  процессом при active backend; использовать доверенный host-local API/lease.

## Что завершено этой рукой

Релиз 0.167.0 реализует `uploader_scoped_hybrid_v1` из `sol/PROPOSALS.md` #39.
Один fail-closed Raw provenance predicate протянут через storage и
`HybridSearcher`: FTS/LIKE, recent/date, whole vectors, chunks, span lookup,
финальный reread и corpus count фильтруются exact `uploaded_by` до caps. В shared
`ExecutionKernel` tenant остаётся `actor.user_id`, author — выбранная учётка;
usage не записывается. Без поисковика остаётся `scoped_lexical`.

Границы намеренны: scoped вызов обходит resident tenant cache и не читает graph,
entity links/names или relation history — у них нет авторского provenance.
Query repair также выключен, пока нет author-aware vocabulary. Reranker получает
deepcopy и после него восстанавливаются canonical rows, поэтому callback не может
подменить ID/тело. Scoped storage и CPU dense/chunk work вынесены с event loop.

Synthetic регрессии покрывают whole-only и chunk-only targets за foreign caps,
date/recent/FTS limits, malformed/oversized/duplicate/BLOB/root-array metadata,
tenant mismatch, cache poison, graph/entity spies, off-loop execution, passage
excerpt и hostile in-place reranker. Обязательные мутации перечислены в #39.

Последнее performance-ревью нашло и уже закрыло ошибку adaptive chunk plan:
author-count занижал цену parent-first tenant scan. На synthetic 6000 KO/600
chunks было `28.6 ms` против `1.1 ms` forced sparse. Gate выбора снова использует
tenant live count; exact author membership не вынесена из SQL. Structural test:
`test_scoped_chunk_plan_prices_the_tenant_index_it_physically_walks`.

## Проверки и точка продолжения

После последней code-правки выполнено:

- uploader/tool/storage: `25 passed`;
- Ruff lint + format по девяти затронутым Python-файлам: PASS;
- mypy по шести source-файлам: PASS.

До performance-only правки расширенный focused-набор давал `285 passed`, а
независимый adversarial retrieval-набор — `150 passed`. Финальный canonical gate
на release tree завершён: non-UI `4962 passed`, один штатный skip real-backup
fixture, UI `23 passed`; Ruff/format, mypy, compileall, Bandit HIGH, JavaScript
syntax и Playwright preflight — PASS. Команда воспроизведения:
`.venv/bin/python tools/quality_gate.py --workers 12 --ui-workers 9`.
Release commit hash указан в сообщении `[handoff:sol:s13-uploader-hybrid]`.

## Следующие задачи по полезности

1. Исправить расхождение публичного `/api/search` и admin evaluation с уже
   замеренной agent policy. В `friday/server.py` обычный search передаёт `kg`, но
   не задаёт `graph_expansion`; default включает граф даже для нереляционных
   запросов. Замер прежней постановки: ordinary recall@10 `0.35 -> 0.15` и около
   `+556 ms`; реляционный класс получает лишь небольшой отдельный выигрыш.
   Протянуть тот же classifier/policy, что использует агент, и закрепить ordinary,
   relational и temporal wiring tests.
2. `friday/admin_api/_evaluation.py::eval_search` имеет ту же graph omission и не
   передаёт `record_usage=False`. Диагностический eval не должен менять usage,
   который затем участвует в ranking. Соседний retrieval-explain уже показывает
   правильную форму вызова. Acceptance: usage snapshot byte/count identical после
   eval; production `/api/search` при этом продолжает записывать usage.
3. После этого брать `OPEN.md` сверху. Из известных кандидатов: event-loop и
   redaction поверхности user activity; затем только с отдельной постановкой —
   pathological privacy dependency writer. Graph provenance для author search —
   самостоятельная задача, не включать graph поздним Python-фильтром.

Для следующих изменений сначала заморозить candidate/acceptance/mutations в
`sol/PROPOSALS.md`, использовать только synthetic corpus, затем полный gate,
commit и push `main`. После deploy сначала backend, затем bridge; проверить
authenticated health/admin и synthetic LLM/embedding/reranker probes без вывода
payload. Секреты и адреса брать из локальной конфигурации и не цитировать.
