# Git-handoff для Sol

Этот каталог — единственный вход для артефактов, которые другой исполнитель передаёт
Sol через Git. Репозиторий и его история публично и бессрочно хранят каждую версию,
поэтому сюда разрешены только обезличенные машинные данные.

## S7: фазовый след retrieval

Opus публикует файл строго по пути:

`sol/handoff/s7/phase_trace_deidentified.json`

Коммит должен содержать маркер `[handoff:sol:s7]` в сообщении. Файл не должен превышать
256 КиБ и обязан содержать:

- ровно 20 обезличенных кейсов;
- только `sha256[:16]` вместо исходных case/object id;
- состояния `missing_from_pool`, `in_pool_below_top10`,
  `in_top10_after_rerank` или `dropped_by_rerank`;
- числовые `rerank_score`, `components`, агрегаты и `channel_medians`.

Запрещены запросы, заголовки, выдержки и тексты документов, имена людей, имена файлов,
локальные пути и URL, исходные id, user/account id, токены, ключи, cookies и любые
учётные данные. Если для интерпретации числа нужен текст, такой артефакт через Git не
передаётся: Opus публикует только агрегированный вердикт в задаче.

Sol перед использованием проверяет размер, валидность JSON, число кейсов, формат всех
хешей, допустимые состояния и отсутствие текстовых/идентифицирующих полей. Любое
отклонение закрывает вход fail-closed: файл не читается как измерение и продуктовый код
по нему не меняется.

## S7: сравнение глубины reranker 20 против 40

После одобрения следующей руки Opus публикует новый файл строго по пути:

`sol/handoff/s7/rerank_depth_comparison_deidentified.json`

Коммит должен содержать маркер `[handoff:sol:s7-depth]`. Старый фазовый след не
перезаписывается. Новый файл не должен превышать 256 КиБ и обязан описывать те же 20
кейсов и те же хеши ожидаемых объектов, что уже приняты из
`phase_trace_deidentified.json`.

Обе руки запускаются на одной read-only scratch-копии с `record_usage=false`. Различаться
может только `rerank_top`: 20 или 40. Для обеих рук фиксированы `k=10`, `pool_max=400`,
`graph_expansion=false`, модель, порог и веса. Порядок запуска рук чередуется по кейсам,
чтобы прогрев сервисов не был систематически приписан одной глубине.

Для каждого кейса и каждого ожидаемого объекта нужны только обезличенные поля:

- верхний уровень: `cases=20`, объект `settings`, массив `per_case` и объект `summary`;
- `settings` содержит ровно `k`, `pool_max`, `graph_expansion`,
  `rerank_confident_min` и `same_scratch_snapshot=true`; имя модели, имя, путь и хеш
  снимка запрещены;
- каждая запись `per_case` содержит `case` и массив `expected`; `case` и `object` в
  `expected` — `sha256[:16]`, совпадающие с прежним следом;
- каждый ожидаемый объект содержит объект `arms` с ключами `20` и `40`; у каждой руки
  обязательны `pre_rerank_rank` (`integer|null`, 0-based), `rerank_applied` (`boolean`),
  `reranked_count` (`integer`), `post_rerank_rank` (`integer|null`, 0-based),
  `rerank_score` (`number|null`), `found_in_top10` (`boolean`) и `latency_ms`
  (`number`);
- итог пары: `win`, `loss`, `tie_hit` или `tie_miss` с точки зрения руки 40;
- `summary` содержит только проверяемые числовые агрегаты, перечисленные ниже.

В агрегате обязательны hits каждой руки, wins, losses, `net_gain=wins-losses`, p50
времени каждой руки и число неуспешных обращений к reranker. Артефакт принимается как
замер только при нуле таких сбоев. Глубина 40 проходит заранее объявленный критерий
только при `net_gain >= 2`; задержка публикуется как цена, но не подменяет качество.

Действуют все запреты предыдущего раздела. Дополнительно запрещены строки запросов к
модели и ответы модели. Любое несовпадение множества хешей, настроек, scratch-снимка или
числовых агрегатов закрывает вход fail-closed.

`pre_rerank_rank` снимается отдельно внутри каждой настоящей руки с порядка элементов,
переданного её callback reranker, до перестановки и порога. Копировать rank из третьей
руки с `reranker=None` запрещено: в `HybridSearcher.search` сам `rerank_top` задаёт
`depth`, а тот меняет ширину FTS, recent pool и графового пула ещё до reranker. Поэтому
третья рука видит другой набор кандидатов и не может служить до-rerank trace для рук 20
и 40.

## S8 (#63): graph expansion на реляционных вопросах

Исполнитель с доступом к живому экземпляру публикует файл строго по пути:

`sol/handoff/s8/relational_graph_comparison_deidentified.json`

Коммит должен содержать маркер `[handoff:sol:s8-relational]`. Файл не должен превышать
256 КиБ. До запуска любой руки исполнитель вручную фиксирует ровно 12 реляционных
кейсов на одной read-only scratch-копии живой базы. Выборка содержит не менее трёх
кейсов каждого класса:

- `single_entity_neighbours` — кто связан или работал с одной названной сущностью;
- `pair_bridge` — через что две названные сущности пересекаются;
- `collaborator_lookup` — с кем названная сущность работала.

Для каждого кейса до запуска рук вручную отмечается от одного до пяти Knowledge Object,
которые действительно служат доказательством запрошенной связи. Выбирать эталоны из
результатов любой руки запрещено. Кейс считается найденным, если хотя бы один заранее
отмеченный объект попал в top-10. Это единственный критерий выигрыша кейса.

Обе руки запускаются на одном commit и одной scratch-копии с `record_usage=false`,
`k=10`, `pool_max=400`, рабочими embeddings и reranker, `rerank_top=40`, тем же порогом,
весами и `kg=state.kg`. Различаться может только `graph_expansion`: `false` в руке
`off`, `true` в руке `on`. Порядок рук чередуется по кейсам. Любой сбой graph traversal
или reranker аннулирует измерение, потому что молчаливый fallback подменяет сравниваемую
руку.

JSON содержит ровно верхние ключи `cases`, `settings`, `dataset`, `per_case`, `summary`:

- `cases=12`;
- `settings` содержит ровно `k=10`, `pool_max=400`, `rerank_top=40`,
  `rerank_confident_min`, `record_usage=false` и `same_scratch_snapshot=true`;
- `dataset` содержит ровно `selection_frozen_before_arms=true`,
  `expected_labels_frozen_before_arms=true`, `direct_entity_relations` и числовые
  `entities`, `knowledge_entity_links`; для этой постановки `direct_entity_relations`
  обязан быть 0;
- каждая запись `per_case` содержит `case` (`sha256[:16]`), `class` из трёх enum выше,
  `relational_regex_match` (результат текущего `_RELATIONAL_QUERY_RE`), `first_arm`
  (`off`/`on`), массив `expected` из уникальных `sha256[:16]`, объект `arms` и `outcome`;
- каждая из рук `off`/`on` содержит `top10` из не более чем десяти уникальных
  `sha256[:16]`, `found`, `expected_top10_count`, `best_expected_rank` (0-based либо
  `null`), `latency_ms`, `graph_candidate_count`, `query_root_count`,
  `implicit_relation_count`, `explicit_relation_count`, `graph_failed` и
  `reranker_failed`;
- `found`, `expected_top10_count` и `best_expected_rank` обязаны пересчитываться из
  пересечения `expected` и `top10`; `outcome` — `win`, `loss`, `tie_hit` или `tie_miss`
  с точки зрения руки `on`;
- `summary` содержит ровно `hits_off`, `hits_on`, `wins`, `losses`, `tie_hit`,
  `tie_miss`, `net_gain=wins-losses`, `p50_latency_ms_off`, `p50_latency_ms_on`,
  `graph_failures`, `reranker_failures`, `regex_matched_cases` и объекты
  `hits_off_by_class`, `hits_on_by_class` с тремя enum-ключами.

Graph expansion проходит заранее объявленный критерий только при `net_gain >= 2` из
12 и нуле сбоев. Задержка публикуется как цена, но не заменяет критерий качества.
Покрытие `_RELATIONAL_QUERY_RE` оценивается отдельно: даже положительный общий результат
не разрешает включать граф по regex, если выигрыши приходятся на кейсы, которые regex
не распознаёт.

В Git запрещены исходные запросы, имена сущностей и людей, названия или тексты
документов, исходные object/entity/case/user/account id, пути, URL, имена моделей,
токены, ключи, cookies и любые учётные данные. Разрешены только перечисленные enum,
boolean, числа и `sha256[:16]`. Любое лишнее поле, несовпадение агрегатов, настроек,
порядка рук или следов scratch-копии закрывает вход fail-closed; продуктовый код по
такому файлу не меняется.
