"""Shared SQL privacy predicates for graph-derived storage rows.

These builders deliberately have no storage-mixin imports.  Graph, Knowledge,
Inbox and vector readers all need the exact same dependency closure, and keeping
it here avoids both import cycles and subtly different definitions of "public".
Aliases are code-owned SQL identifiers, never request values.
"""

from __future__ import annotations

from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES

# Предел на JSON предложений Inbox стоял в 128 раз ниже, чем такой же предел у
# Raw/Knowledge (`_CURRENT_PUBLIC_JSON_MAX_BYTES`), хотя сторожит то же самое —
# цену обхода `json_tree`. Замер на архиве владельца: медиана 2 436 байт, p99
# 10 537, максимум 18 217; прежние 8 КиБ отсекали 85 строк из 2071, и вместе с
# ними становились невидимы 85 Raw Objects. Размер блоба не говорит ничего о том,
# копирует ли он приватную личность: за пределом честного ответа нет, есть только
# молчаливая пропажа. Цена выравнивания замерена: полный обход 979.7 → 1097.2 мс.
_INBOX_PUBLIC_JSON_MAX_BYTES = 1_048_576
_INBOX_PUBLIC_NOTES_MAX_CHARS = 4_000
_GRAPH_PUBLIC_JSON_MAX_BYTES = 8_192
_RELATION_PUBLIC_JSON_MAX_BYTES = 1_048_576
_RESOLUTION_PUBLIC_JSON_MAX_BYTES = 1_048_576
_ENTITY_PUBLIC_MATERIAL_MAX_BYTES = 1_048_576
_PUBLIC_NOTIFICATION_BODY_MAX_BYTES = 65_536
_PUBLIC_NOTIFICATION_KEY_MAX_BYTES = 2_048
_CURRENT_PUBLIC_BODY_MAX_BYTES = 64 * 1_048_576
_CURRENT_PUBLIC_FIELD_MAX_BYTES = 1_048_576
_CURRENT_PUBLIC_JSON_MAX_BYTES = 1_048_576


def _not_audio_document(alias: str = "r") -> str:
    """SQL predicate excluding voice/audio carriers from document inventories.

    Telegram stores a voice note as a Raw Object with ``content_type='file'``;
    that transport fact does not turn it into a document.  Prefer the explicit
    media kind and MIME type, then retain a suffix fallback for legacy rows
    which predate those metadata fields.
    """

    raw_metadata = f"{alias}.metadata_json"
    bounded_metadata = (
        f"typeof({raw_metadata})='text' "
        f"AND length(CAST({raw_metadata} AS BLOB))<={RAW_FILE_METADATA_MAX_BYTES}"
    )
    # SQLite may reorder WHERE predicates, so a neighbouring ``json_valid``
    # guard cannot make direct ``json_extract(raw_metadata, ...)`` safe.  Use a
    # locally total JSON expression: caller-specific provenance/privacy gates
    # still decide whether malformed metadata is admissible, while this media
    # classifier can never abort the whole catalog on one legacy row.
    metadata = (
        f"(CASE WHEN {bounded_metadata} AND json_valid({raw_metadata}) THEN {raw_metadata} ELSE '{{}}' END)"
    )
    filename = f"lower(COALESCE(json_extract({metadata},'$.filename'),''))"
    mime = (
        "lower(COALESCE("
        f"NULLIF(json_extract({metadata},'$.mime_type'),''),"
        f"NULLIF(json_extract({metadata},'$.mime'),''),"
        f"NULLIF(json_extract({metadata},'$.content_type'),''),''))"
    )
    media_kind = f"lower(COALESCE(json_extract({metadata},'$.media_kind'),''))"
    raw_content_type = f"lower(COALESCE({alias}.content_type,''))"
    audio_suffixes = (
        ".ogg",
        ".oga",
        ".opus",
        ".mp3",
        ".m4a",
        ".aac",
        ".wav",
        ".flac",
        ".wma",
        ".aif",
        ".aiff",
        ".amr",
    )
    suffix_guard = " AND ".join(f"{filename} NOT LIKE '%{suffix}'" for suffix in audio_suffixes)
    return (
        f"(({bounded_metadata}) "
        f"AND {raw_content_type} NOT IN ('voice','audio') "
        f"AND {raw_content_type} NOT LIKE 'audio/%' "
        f"AND {raw_content_type} NOT LIKE 'voice/%' "
        f"AND {media_kind} NOT IN ('voice','audio') "
        f"AND {mime} NOT LIKE 'audio/%' AND {suffix_guard})"
    )


def _private_material_cache_valid() -> str:
    """Cheap authority-state guard shared by every generic material surface."""

    return """EXISTS (
        SELECT 1 FROM private_entity_material_cache_state material_cache_state
         WHERE material_cache_state.singleton=1
           AND material_cache_state.valid=1
    )"""


def _private_derivative_cache_valid() -> str:
    """Global validity guard for the ID-only Raw/Knowledge/Inbox authority."""

    return f"""(
        {_private_material_cache_valid()}
        AND EXISTS (
            SELECT 1
              FROM private_entity_material_derivative_state derivative_cache_state
             WHERE derivative_cache_state.singleton=1
               AND derivative_cache_state.valid=1
        )
    )"""


def _private_derivative_id_dependency(
    alias: str,
    material_kind: str,
    *,
    work: bool = False,
) -> str:
    """Exact ID lookup in the durable allowlist or its rebuild staging set.

    The kind and table name are selected only by code.  Object/user identifiers
    stay as columns, so a hot public read never has to rescan body or JSON text.
    Rebuild expressions deliberately read ``work`` while global validity is zero;
    application predicates always read ``cache`` behind the validity guard.
    """

    if material_kind not in {"raw", "knowledge", "knowledge_hidden", "inbox"}:
        raise ValueError("unknown private derivative material kind")
    table = "private_entity_material_derivative_work" if work else "private_entity_material_derivative_cache"
    authority = "derivative_work" if work else "derivative_cache"
    valid = "1" if work else _private_derivative_cache_valid()
    return f"""(
        {valid}
        AND EXISTS (
            SELECT 1 FROM {table} {authority}
             WHERE {authority}.material_kind='{material_kind}'
               AND {authority}.object_id={alias}.id
               AND {authority}.user_id={alias}.user_id
        )
    )"""


def _not_private_reminder_entity(alias: str = "e") -> str:
    """SQL predicate keeping personal reminder entities out of generic reads."""

    return (
        "NOT EXISTS (SELECT 1 FROM private_entity_owners private_owner "
        f"WHERE private_owner.entity_id={alias}.id) "
        "AND NOT EXISTS (SELECT 1 FROM entity_time private_time "
        f"WHERE private_time.entity_id={alias}.id AND private_time.source LIKE 'reminder:%')"
    )


def _safe_entity_material_json(alias: str, field: str, expected_type: str) -> str:
    """JSON expression that never raises while the closure scans legacy rows."""

    fallback = "[]" if expected_type == "array" else "{}"
    expression = f"{alias}.{field}"
    return f"""CASE
        WHEN length(CAST(COALESCE({expression},'') AS BLOB))
                 <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
         AND json_valid({expression})
         AND json_type({expression})='{expected_type}'
        THEN {expression} ELSE '{fallback}' END"""


def _entity_material_shape(alias: str) -> str:
    """Bounded, parseable entity fields before dependency propagation."""

    aliases = _safe_entity_material_json(alias, "aliases_json", "array")
    metadata = _safe_entity_material_json(alias, "metadata_json", "object")
    return f"""(
        length(CAST(COALESCE({alias}.name,'') AS BLOB))
              <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
        AND length(CAST(COALESCE({alias}.description,'') AS BLOB))
              <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
        AND length(CAST(COALESCE({alias}.aliases_json,'') AS BLOB))
              <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
        AND json_valid({alias}.aliases_json)
        AND json_type({alias}.aliases_json)='array'
        AND length(CAST(COALESCE({alias}.metadata_json,'') AS BLOB))
              <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
        AND json_valid({alias}.metadata_json)
        AND json_type({alias}.metadata_json)='object'
        AND NOT EXISTS (
            SELECT 1 FROM json_tree({aliases}) entity_alias_nested_json
             WHERE (entity_alias_nested_json.type='text'
                    AND substr(ltrim(CAST(entity_alias_nested_json.value AS TEXT)),1,1)
                          IN ('{{','[','"'))
                OR substr(ltrim(CAST(entity_alias_nested_json.key AS TEXT)),1,1)
                     IN ('{{','[','"')
        )
        AND NOT EXISTS (
            SELECT 1 FROM json_tree({metadata}) entity_metadata_nested_json
             WHERE (entity_metadata_nested_json.type='text'
                    AND substr(ltrim(CAST(entity_metadata_nested_json.value AS TEXT)),1,1)
                          IN ('{{','[','"'))
                OR substr(ltrim(CAST(entity_metadata_nested_json.key AS TEXT)),1,1)
                     IN ('{{','[','"')
        )
    )"""


def _entity_material_copy_condition(carrier: str, dependency: str) -> str:
    """Whether one valid entity copies another hidden entity's id or name."""

    aliases = _safe_entity_material_json(carrier, "aliases_json", "array")
    metadata = _safe_entity_material_json(carrier, "metadata_json", "object")
    return f"""(
        instr(COALESCE({carrier}.name,''), {dependency}.id)>0
        OR instr(COALESCE({carrier}.description,''), {dependency}.id)>0
        OR ({dependency}.name<>'' AND jericho_private_identity_match(
            COALESCE({carrier}.name,''), {dependency}.name)=1)
        OR ({dependency}.name<>'' AND jericho_private_identity_match(
            COALESCE({carrier}.description,''), {dependency}.name)=1)
        OR EXISTS (
            SELECT 1 FROM json_tree({aliases}) carrier_alias_value
             WHERE (
                   carrier_alias_value.type='text'
                   AND (
                   instr(CAST(carrier_alias_value.value AS TEXT), {dependency}.id)>0
                   OR ({dependency}.name<>'' AND jericho_private_identity_match(
                       CAST(carrier_alias_value.value AS TEXT), {dependency}.name)=1)
                   )
               )
                OR instr(CAST(carrier_alias_value.key AS TEXT), {dependency}.id)>0
                OR ({dependency}.name<>'' AND jericho_private_identity_match(
                    CAST(carrier_alias_value.key AS TEXT), {dependency}.name)=1)
        )
        OR EXISTS (
            SELECT 1 FROM json_tree({metadata}) carrier_metadata_value
             WHERE (
                   carrier_metadata_value.type='text'
                   AND (
                   instr(CAST(carrier_metadata_value.value AS TEXT), {dependency}.id)>0
                   OR ({dependency}.name<>'' AND jericho_private_identity_match(
                       CAST(carrier_metadata_value.value AS TEXT), {dependency}.name)=1)
                   )
               )
                OR instr(CAST(carrier_metadata_value.key AS TEXT), {dependency}.id)>0
                OR ({dependency}.name<>'' AND jericho_private_identity_match(
                    CAST(carrier_metadata_value.key AS TEXT), {dependency}.name)=1)
        )
    )"""


def _prepared_entity_material_copy_condition(carrier: str, dependency: str) -> str:
    """Copy predicate for the live fixed point's pre-folded material rows.

    Recursive closure compares every reachable identity with candidate carrier
    states.  Calling the Python Unicode UDF for both sides of every pair makes a
    long 4,500-node chain take tens of seconds.  The live CTE materializes each
    folded scalar once; JSON is still decoded exactly, but default empty arrays
    and objects avoid constructing a virtual ``json_tree`` for every pair.
    """

    aliases = _safe_entity_material_json(carrier, "aliases_json", "array")
    metadata = _safe_entity_material_json(carrier, "metadata_json", "object")
    return f"""(
        instr(COALESCE({carrier}.name,''), {dependency}.id)>0
        OR instr(COALESCE({carrier}.description,''), {dependency}.id)>0
        OR ({dependency}.name<>''
            AND instr({carrier}.folded_name, {dependency}.folded_name)>0
            AND jericho_private_identity_match(
                {carrier}.folded_name, {dependency}.folded_name)=1)
        OR ({dependency}.name<>''
            AND instr({carrier}.folded_description, {dependency}.folded_name)>0
            AND jericho_private_identity_match(
                {carrier}.folded_description, {dependency}.folded_name)=1)
        OR ({aliases}<>'[]' AND EXISTS (
            SELECT 1 FROM json_tree({aliases}) carrier_alias_value
             WHERE (
                   carrier_alias_value.type='text'
                   AND (
                   instr(CAST(carrier_alias_value.value AS TEXT), {dependency}.id)>0
                   OR ({dependency}.name<>'' AND jericho_private_identity_match(
                       CAST(carrier_alias_value.value AS TEXT), {dependency}.folded_name)=1)
                   )
               )
                OR instr(CAST(carrier_alias_value.key AS TEXT), {dependency}.id)>0
                OR ({dependency}.name<>'' AND jericho_private_identity_match(
                    CAST(carrier_alias_value.key AS TEXT), {dependency}.folded_name)=1)
        ))
        OR ({metadata}<>'{{}}' AND EXISTS (
            SELECT 1 FROM json_tree({metadata}) carrier_metadata_value
             WHERE (
                   carrier_metadata_value.type='text'
                   AND (
                   instr(CAST(carrier_metadata_value.value AS TEXT), {dependency}.id)>0
                   OR ({dependency}.name<>'' AND jericho_private_identity_match(
                       CAST(carrier_metadata_value.value AS TEXT), {dependency}.folded_name)=1)
                   )
               )
                OR instr(CAST(carrier_metadata_value.key AS TEXT), {dependency}.id)>0
                OR ({dependency}.name<>'' AND jericho_private_identity_match(
                    CAST(carrier_metadata_value.key AS TEXT), {dependency}.folded_name)=1)
        ))
    )"""


def _private_entity_material_seeded_cte(
    direct_seed_predicate: str,
    cte_name: str = "private_material_id",
) -> str:
    """Build a privacy closure from a code-owned direct-seed predicate.

    ``direct_seed_predicate`` is SQL over ``closure_seed_entity`` and may contain
    bound ``?`` parameters.  It must never contain request-provided identifiers or
    values.  Invalid current/authenticated-history material is always seeded here,
    outside the caller's predicate, so a person-aware export can exempt only its
    exact valid reminder provenance without exempting malformed evidence.
    """

    copied = _prepared_entity_material_copy_condition(
        "closure_carrier_state",
        "closure_dependency_token",
    )
    own_reminder = _own_tenant_reminder_identity(
        "closure_dependency_token.id",
        "closure_carrier_entity.user_id",
    )
    return f"""WITH RECURSIVE
    closure_material_states AS MATERIALIZED (
        SELECT material_state.*,
               jericho_casefold(COALESCE(material_state.name,'')) AS folded_name,
               jericho_casefold(COALESCE(material_state.description,'')) AS folded_description
          FROM private_entity_material_states material_state
    ),
    closure_identity_tokens AS MATERIALIZED (
        SELECT identity_token.id, identity_token.name,
               jericho_casefold(COALESCE(identity_token.name,'')) AS folded_name
          FROM private_entity_identity_tokens identity_token
    ),
    {cte_name}(id) AS (
        SELECT closure_seed_entity.id
          FROM entities closure_seed_entity
         WHERE ({direct_seed_predicate})
            OR EXISTS (
                SELECT 1 FROM closure_material_states invalid_material_state
                 WHERE invalid_material_state.id=closure_seed_entity.id
                   AND invalid_material_state.material_valid=0
            )
        UNION
        SELECT closure_carrier_state.id
          FROM {cte_name} closure_dependency
          JOIN closure_identity_tokens closure_dependency_token
            ON closure_dependency_token.id=closure_dependency.id
          JOIN closure_material_states closure_carrier_state
            ON closure_carrier_state.id<>closure_dependency.id
           AND closure_carrier_state.material_valid=1
           AND {copied}
           -- §76: чужое напоминание красит носителя приватным, СВОЁ — нет.
           -- Без этой строки инструмент напоминаний не мог поставить второе
           -- напоминание про совещание: новая сущность повторяла имя старой,
           -- сразу становилась приватной, `create_entity` отказывал, а человеку
           -- при этом отвечалось «записано». Прямой посев не трогается — само
           -- напоминание остаётся скрытым.
           AND NOT EXISTS (
               SELECT 1 FROM entities closure_carrier_entity
                WHERE closure_carrier_entity.id=closure_carrier_state.id
                  AND {own_reminder}
           )
    )"""


def _private_entity_material_seeded_query(
    direct_seed_predicate: str,
    cte_name: str = "private_material_id",
) -> str:
    """Return seeded closure rows as stable ``(id, name)`` identity tokens."""

    closure = _private_entity_material_seeded_cte(direct_seed_predicate, cte_name)
    return f"""{closure}
    SELECT hidden_material.id AS id,
           COALESCE(hidden_identity.name, '') AS name
      FROM {cte_name} hidden_material
      LEFT JOIN closure_identity_tokens hidden_identity
        ON hidden_identity.id=hidden_material.id"""


def _private_entity_material_live_cte(cte_name: str = "private_material_id") -> str:
    """Compute direct and transitively copied private entity ids."""

    seed_private = _not_private_reminder_entity("closure_seed_entity")
    return _private_entity_material_seeded_cte(f"NOT ({seed_private})", cte_name)


def _not_private_entity_material_dependency(alias: str = "e") -> str:
    """Keep copies of personal entity identity out of ordinary entity reads.

    A public entity can carry another entity's name/id in its description,
    aliases or metadata.  If the referenced entity later becomes a personal
    reminder, the copy is private derived material too.  JSON-in-string values
    are rejected because SQLite's JSON walker cannot safely prove what an opaque
    second encoding contains.
    """

    return f"""(
        {_private_material_cache_valid()}
        AND
        NOT EXISTS (
            SELECT 1 FROM private_entity_material_cache private_material_entity
             WHERE private_material_entity.entity_id={alias}.id
        )
    )"""


def _not_disallowed_private_material_for_person(
    alias: str = "e",
    person_expression: str | None = None,
) -> str:
    """Allow only a person's exact valid reminder through the global cache.

    Generic rows take the ID-only cache fast path.  A cached row may be exposed
    only when its tenant is the exact reminder owner/time provenance and none of
    its authenticated current/history states is malformed or copies any *other*
    cached identity.  Carriers remain hidden conservatively, including carriers
    of the person's own reminder; this narrow exception exists only for the
    reminder entity itself on person-scoped surfaces. ``person_expression`` is a
    code-owned SQL value expression, never a request-provided identifier. Passing
    ``?`` consumes exactly one bind even though provenance compares it repeatedly.
    """

    requested_person = person_expression or f"{alias}.user_id"
    copied_other = _entity_material_copy_condition(
        "person_material_state",
        "person_cached_dependency",
    )
    return f"""(
        {_private_material_cache_valid()}
        AND (
            NOT EXISTS (
                SELECT 1 FROM private_entity_material_cache person_cached_row
                 WHERE person_cached_row.entity_id={alias}.id
            )
            OR EXISTS (
                SELECT 1 FROM (SELECT {requested_person} AS person_id) requested_person
                 WHERE
                EXISTS (
                    SELECT 1 FROM private_entity_owners person_exact_owner
                     WHERE person_exact_owner.entity_id={alias}.id
                       AND person_exact_owner.person_id=requested_person.person_id
                       AND person_exact_owner.privacy_kind='reminder'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM private_entity_owners person_other_owner
                     WHERE person_other_owner.entity_id={alias}.id
                       AND (
                            person_other_owner.person_id<>requested_person.person_id
                            OR person_other_owner.privacy_kind<>'reminder'
                       )
                )
                AND EXISTS (
                    SELECT 1 FROM entity_time person_exact_time
                     WHERE person_exact_time.entity_id={alias}.id
                       AND person_exact_time.user_id={alias}.user_id
                       AND person_exact_time.source=
                              'reminder:' || requested_person.person_id
                )
                AND NOT EXISTS (
                    SELECT 1 FROM entity_time person_other_time
                     WHERE person_other_time.entity_id={alias}.id
                       AND person_other_time.source LIKE 'reminder:%'
                       AND (
                            person_other_time.user_id<>{alias}.user_id
                            OR person_other_time.source<>
                                   'reminder:' || requested_person.person_id
                       )
                )
                AND NOT EXISTS (
                    SELECT 1 FROM private_entity_material_states person_invalid_state
                     WHERE person_invalid_state.id={alias}.id
                       AND person_invalid_state.material_valid=0
                )
                AND NOT EXISTS (
                    SELECT 1
                      FROM private_entity_material_states person_material_state
                      JOIN private_entity_material_cached_closure person_cached_dependency
                        ON person_cached_dependency.id<>person_material_state.id
                       AND {copied_other}
                     WHERE person_material_state.id={alias}.id
                       AND person_material_state.material_valid=1
                )
            )
        )
    )"""


def _own_tenant_reminder_identity(
    dependency_id_expression: str,
    tenant_expression: str,
) -> str:
    """Носитель повторяет имя СВОЕГО личного напоминания своего же арендатора.

    §30 прячет личное напоминание от ДРУГОГО человека: в общем архиве одного
    арендатора читают многие. Внутри арендатора самого владельца напоминания
    другого человека нет — кто вообще может читать этого арендатора, уже решено
    изоляцией, — и заметка, просто повторившая слова напоминания, не спрятана ни
    от кого. Замерено на копии живого архива: `108` из `3352` Raw Objects
    владельца были заперты словами «совещание», «отчёт в пятницу» и «позвонить в
    автосервис»; новая заметка с любым из них отвергалась, и новое напоминание про
    совещание тоже — инструмент не мог создать событие с именем события, которое
    уже знал.

    Условие целиком лежит в `private_entity_own_tenant_reminder`: та же
    долговечная опора, что у персонального исключения по сущности — ровно один
    маркер владения, ровно один провенанс расписания, исправное состояние
    материала. Чужой арендатор, неоднозначное владение и испорченное состояние
    держат карантин.

    Вид намеренно не смотрит в кэш: это выражение считается в том числе ПОКА
    строится живое замыкание, когда опубликованного кэша ещё нет, а окружающие
    предикаты чтения и так закрываются при невалидном кэше. Условие «копирует
    ЧУЖУЮ приватную личность» здесь тоже не нужно: носитель, копирующий чужое
    имя, всё равно достаётся замыканием от ТОГО посева, а послабление снимает
    ровно одно ребро.

    ``tenant_expression`` — code-owned выражение-столбец, не bind-маркер и никогда
    не данные запроса.
    """

    return f"""EXISTS (
        SELECT 1 FROM private_entity_own_tenant_reminder own_tenant_reminder
         WHERE own_tenant_reminder.entity_id={dependency_id_expression}
           AND own_tenant_reminder.tenant_id={tenant_expression}
    )"""


def _not_private_text_entity_dependency(
    *expressions: str,
    max_bytes: int,
    tenant_expression: str = "",
) -> str:
    """Bound text and reject identities of any entity hidden from public graph reads.

    ``tenant_expression`` names the carrier's tenant. Passing it lets a carrier
    keep the words of its OWN personal reminder (see
    ``_own_tenant_reminder_identity``); omitting it keeps the older, fully
    conservative behaviour for callers with no tenant column at hand.
    """

    if not expressions:
        return "1"
    bounds = " AND ".join(
        f"length(CAST(COALESCE({expression},'') AS BLOB))<={max(1, int(max_bytes))}"
        for expression in expressions
    )
    copies = " OR ".join(
        f"""instr(COALESCE({expression},''), copied_text_entity.id)>0
             OR (copied_text_entity.name<>'' AND jericho_private_identity_match(
                 COALESCE({expression},''), copied_text_entity.name)=1)"""
        for expression in expressions
    )
    own_reminder = (
        f"AND NOT {_own_tenant_reminder_identity('copied_text_entity.id', tenant_expression)}"
        if tenant_expression
        else ""
    )
    return f"""(
        {_private_material_cache_valid()}
        AND {bounds}
        AND NOT EXISTS (
            SELECT 1 FROM private_entity_material_closure copied_text_entity
             WHERE ({copies})
             {own_reminder}
        )
    )"""


def _raw_material_expression(alias: str = "r") -> str:
    """Current Raw text/metadata cannot remain public after its source turns private."""

    content = _not_private_text_entity_dependency(
        f"{alias}.raw_content",
        max_bytes=_CURRENT_PUBLIC_BODY_MAX_BYTES,
        tenant_expression=f"{alias}.user_id",
    )
    source_ref = _not_private_text_entity_dependency(
        f"{alias}.source_ref",
        max_bytes=_CURRENT_PUBLIC_FIELD_MAX_BYTES,
        tenant_expression=f"{alias}.user_id",
    )
    metadata = _not_private_bounded_json_dependency(
        f"{alias}.metadata_json",
        f"{alias}.user_id",
        max_bytes=_CURRENT_PUBLIC_JSON_MAX_BYTES,
        reject_nested_json=True,
        scan_knowledge=False,
    )
    return f"""(
        {content}
        AND {source_ref}
        AND length(CAST(COALESCE({alias}.source,'') AS BLOB))
              <={_CURRENT_PUBLIC_FIELD_MAX_BYTES}
        AND length(CAST(COALESCE({alias}.content_type,'') AS BLOB))
              <={_CURRENT_PUBLIC_FIELD_MAX_BYTES}
        AND {metadata}
    )"""


def _not_private_raw_material_dependency(
    alias: str = "r",
    *,
    _work: bool = False,
) -> str:
    """Cheap lookup into the startup-defined current Raw material authority."""

    return _private_derivative_id_dependency(alias, "raw", work=_work)


def _knowledge_material_expression(alias: str = "k") -> str:
    """Current KO bodies/cards cannot retain copied private entity identity."""

    content = _not_private_text_entity_dependency(
        f"{alias}.content",
        max_bytes=_CURRENT_PUBLIC_BODY_MAX_BYTES,
        tenant_expression=f"{alias}.user_id",
    )
    card_text = _not_private_text_entity_dependency(
        f"{alias}.title",
        f"{alias}.summary",
        max_bytes=_CURRENT_PUBLIC_FIELD_MAX_BYTES,
        tenant_expression=f"{alias}.user_id",
    )
    tags = _not_private_bounded_json_dependency(
        f"{alias}.tags_json",
        f"{alias}.user_id",
        max_bytes=_CURRENT_PUBLIC_JSON_MAX_BYTES,
        reject_nested_json=True,
        expected_json_type="array",
        scan_knowledge=False,
    )
    metadata = _not_private_bounded_json_dependency(
        f"{alias}.metadata_json",
        f"{alias}.user_id",
        max_bytes=_CURRENT_PUBLIC_JSON_MAX_BYTES,
        reject_nested_json=True,
        scan_knowledge=False,
    )
    return f"""(
        {content}
        AND {card_text}
        AND length(CAST(COALESCE({alias}.content_type,'') AS BLOB))
              <={_CURRENT_PUBLIC_FIELD_MAX_BYTES}
        AND length(CAST(COALESCE({alias}.knowledge_kind,'') AS BLOB))
              <={_CURRENT_PUBLIC_FIELD_MAX_BYTES}
        AND {tags}
        AND {metadata}
    )"""


def _not_private_knowledge_entity_dependency(alias: str = "k") -> str:
    """SQL predicate for direct primary/link entity dependencies."""

    primary = _not_private_entity_material_dependency("primary_dependency_entity")
    linked = _not_private_entity_material_dependency("linked_dependency_entity")
    return f"""(
        ({alias}.entity_id IS NULL OR EXISTS (
            SELECT 1 FROM entities primary_dependency_entity
             WHERE primary_dependency_entity.id={alias}.entity_id
               AND primary_dependency_entity.user_id={alias}.user_id
               AND {primary}
        ))
        AND NOT EXISTS (
            SELECT 1 FROM knowledge_entity_links dependency_link
            LEFT JOIN entities linked_dependency_entity
              ON linked_dependency_entity.id=dependency_link.entity_id
             AND linked_dependency_entity.user_id=dependency_link.user_id
             WHERE dependency_link.knowledge_object_id={alias}.id
               AND dependency_link.user_id={alias}.user_id
               AND (linked_dependency_entity.id IS NULL OR NOT ({linked}))
        )
    )"""


def _not_private_knowledge_structure_dependency(
    alias: str = "k",
    *,
    _work: bool = False,
) -> str:
    """Structural KO dependencies, excluding only its remediable current fields."""

    direct = _not_private_knowledge_entity_dependency(alias)
    superseding = _not_private_knowledge_entity_dependency("superseding_dependency")
    superseding_material = _knowledge_material_expression("superseding_dependency")
    raw_material = _not_private_raw_material_dependency(
        "knowledge_dependency_raw",
        _work=_work,
    )
    superseding_raw_material = _not_private_raw_material_dependency(
        "superseding_dependency_raw",
        _work=_work,
    )
    return f"""(
        {direct}
        AND ({alias}.raw_object_id IS NULL OR EXISTS (
            SELECT 1 FROM raw_objects knowledge_dependency_raw
             WHERE knowledge_dependency_raw.id={alias}.raw_object_id
               AND knowledge_dependency_raw.user_id={alias}.user_id
               AND {raw_material}
        ))
        AND ({alias}.superseded_by_id IS NULL OR EXISTS (
            SELECT 1 FROM knowledge_objects superseding_dependency
             WHERE superseding_dependency.id={alias}.superseded_by_id
               AND superseding_dependency.user_id={alias}.user_id
               AND superseding_dependency.deleted_at IS NULL
               AND superseding_dependency.superseded_by_id IS NULL
               AND {superseding}
               AND {superseding_material}
               AND (superseding_dependency.raw_object_id IS NULL OR EXISTS (
                   SELECT 1 FROM raw_objects superseding_dependency_raw
                    WHERE superseding_dependency_raw.id=
                              superseding_dependency.raw_object_id
                      AND superseding_dependency_raw.user_id=
                              superseding_dependency.user_id
                      AND {superseding_raw_material}
               ))
        ))
    )"""


def _not_private_knowledge_dependency(
    alias: str = "k",
    *,
    _work: bool = False,
) -> str:
    """SQL predicate keeping a Knowledge Object's full dependency closure public."""

    return _private_derivative_id_dependency(alias, "knowledge", work=_work)


def _inbox_dependency_expression(
    alias: str = "i",
    *,
    _work: bool = False,
) -> str:
    """SQL predicate keeping private entity copies out of generic Inbox reads."""

    suggested = _not_private_entity_material_dependency("suggested_dependency_entity")
    knowledge = _not_private_knowledge_dependency(
        "inbox_dependency_knowledge",
        _work=_work,
    )
    return f"""(
        length(CAST(COALESCE({alias}.suggestions_json,'') AS BLOB))
            <= {_INBOX_PUBLIC_JSON_MAX_BYTES}
        AND json_valid({alias}.suggestions_json)
        AND json_type({alias}.suggestions_json)='object'
        AND length(CAST(COALESCE({alias}.suggested_tags_json,'') AS BLOB))
            <= {_INBOX_PUBLIC_JSON_MAX_BYTES}
        AND json_valid({alias}.suggested_tags_json)
        AND json_type({alias}.suggested_tags_json)='array'
        AND length(COALESCE({alias}.classification_notes,''))
            <= {_INBOX_PUBLIC_NOTES_MAX_CHARS}
        AND NOT EXISTS (
            SELECT 1 FROM json_tree({alias}.suggestions_json) nested_suggestion_json
             WHERE (nested_suggestion_json.type='text'
                    AND substr(ltrim(CAST(nested_suggestion_json.value AS TEXT)),1,1)
                          IN ('{{','[','"')
                    AND json_valid(CAST(nested_suggestion_json.value AS TEXT)))
                OR (substr(ltrim(CAST(nested_suggestion_json.key AS TEXT)),1,1)
                     IN ('{{','[','"')
                    AND json_valid(CAST(nested_suggestion_json.key AS TEXT)))
        )
        AND ({alias}.suggested_entity_id IS NULL OR EXISTS (
            SELECT 1 FROM entities suggested_dependency_entity
             WHERE suggested_dependency_entity.id={alias}.suggested_entity_id
               AND suggested_dependency_entity.user_id={alias}.user_id
               AND {suggested}
        ))
        AND ({alias}.knowledge_object_id IS NULL OR EXISTS (
            SELECT 1 FROM knowledge_objects inbox_dependency_knowledge
             WHERE inbox_dependency_knowledge.id={alias}.knowledge_object_id
               AND inbox_dependency_knowledge.user_id={alias}.user_id
               AND {knowledge}
        ))
        AND NOT EXISTS (
            SELECT 1 FROM private_entity_material_closure embedded_dependency_entity
             WHERE (
                   instr(COALESCE({alias}.classification_notes,''),
                         embedded_dependency_entity.id)>0
                   OR (embedded_dependency_entity.name<>'' AND
                       jericho_private_identity_match(
                           COALESCE({alias}.classification_notes,''),
                           embedded_dependency_entity.name)=1)
                   OR EXISTS (
                       SELECT 1 FROM json_tree({alias}.suggestions_json) embedded_suggestion
                        WHERE (
                              embedded_suggestion.type='text'
                              AND (
                              instr(CAST(embedded_suggestion.value AS TEXT),
                                    embedded_dependency_entity.id)>0
                              OR (embedded_dependency_entity.name<>'' AND
                                  jericho_private_identity_match(
                                      CAST(embedded_suggestion.value AS TEXT),
                                      embedded_dependency_entity.name)=1)
                              )
                          )
                           OR instr(CAST(embedded_suggestion.key AS TEXT),
                                    embedded_dependency_entity.id)>0
                           OR (embedded_dependency_entity.name<>'' AND
                               jericho_private_identity_match(
                                   CAST(embedded_suggestion.key AS TEXT),
                                   embedded_dependency_entity.name)=1)
                   )
                   OR EXISTS (
                       SELECT 1 FROM json_each({alias}.suggested_tags_json) embedded_tag
                        WHERE embedded_tag.type='text'
                          AND (
                              instr(CAST(embedded_tag.value AS TEXT),
                                    embedded_dependency_entity.id)>0
                              OR (embedded_dependency_entity.name<>'' AND
                                  jericho_private_identity_match(
                                      CAST(embedded_tag.value AS TEXT),
                                      embedded_dependency_entity.name)=1)
                          )
                   )
               )
               AND NOT {_own_tenant_reminder_identity("embedded_dependency_entity.id", f"{alias}.user_id")}
        )
        AND NOT EXISTS (
            SELECT 1
              FROM private_entity_material_derivative_work embedded_dependency_knowledge
             WHERE embedded_dependency_knowledge.material_kind='knowledge_hidden'
               AND (
                   instr(COALESCE({alias}.classification_notes,''),
                         embedded_dependency_knowledge.object_id)>0
                   OR EXISTS (
                       SELECT 1 FROM json_tree({alias}.suggestions_json)
                                     embedded_knowledge_suggestion
                        WHERE (embedded_knowledge_suggestion.type='text'
                               AND
                              instr(CAST(embedded_knowledge_suggestion.value AS TEXT),
                                    embedded_dependency_knowledge.object_id)>0
                              )
                           OR instr(CAST(embedded_knowledge_suggestion.key AS TEXT),
                                    embedded_dependency_knowledge.object_id)>0
                   )
                   OR EXISTS (
                       SELECT 1 FROM json_each({alias}.suggested_tags_json)
                                     embedded_knowledge_tag
                        WHERE embedded_knowledge_tag.type='text'
                          AND instr(CAST(embedded_knowledge_tag.value AS TEXT),
                                    embedded_dependency_knowledge.object_id)>0
                   )
               )
        )
    )"""


def _not_private_inbox_dependency(
    alias: str = "i",
    *,
    _work: bool = False,
) -> str:
    """Cheap lookup into the startup-defined public Inbox dependency authority."""

    return _private_derivative_id_dependency(alias, "inbox", work=_work)


def _not_private_raw_dependency(alias: str = "r") -> str:
    """SQL predicate excluding sources with quarantined derived copies."""

    knowledge = _not_private_knowledge_dependency("raw_dependency_knowledge")
    inbox = _not_private_inbox_dependency("raw_dependency_inbox")
    return f"""(
        {_not_private_raw_material_dependency(alias)}
        AND NOT EXISTS (
            SELECT 1 FROM knowledge_objects raw_dependency_knowledge
             WHERE raw_dependency_knowledge.raw_object_id={alias}.id
               AND raw_dependency_knowledge.user_id={alias}.user_id
               AND NOT ({knowledge})
        )
        AND NOT EXISTS (
            SELECT 1 FROM inbox raw_dependency_inbox
             WHERE raw_dependency_inbox.raw_object_id={alias}.id
               AND raw_dependency_inbox.user_id={alias}.user_id
               AND NOT ({inbox})
        )
    )"""


def _exact_uploader_raw_dependency(alias: str = "r") -> str:
    """One exact, fail-closed uploader check over bounded Raw metadata.

    The expression contains one bound ``?``.  Duplicate JSON keys, malformed or
    oversized metadata and a non-text/missing ``uploaded_by`` all fail closed;
    filename navigation and semantic source recall must interpret provenance by
    the same rule as uploader-scoped Knowledge retrieval.
    """

    return f"""CASE
        WHEN length(CAST(COALESCE({alias}.metadata_json,'') AS BLOB))
                 <={RAW_FILE_METADATA_MAX_BYTES}
         AND typeof({alias}.metadata_json)='text'
         AND json_valid({alias}.metadata_json)
        THEN CASE
          WHEN json_type({alias}.metadata_json)='object'
           AND NOT EXISTS (
                 SELECT 1 FROM json_tree({alias}.metadata_json) uploader_json_member
                  WHERE uploader_json_member.key IS NOT NULL
                  GROUP BY uploader_json_member.parent,
                           CAST(uploader_json_member.key AS TEXT)
                 HAVING COUNT(*) > 1
               )
           AND json_type({alias}.metadata_json,'$.uploaded_by')='text'
          THEN json_extract({alias}.metadata_json,'$.uploaded_by')=?
          ELSE 0
        END
        ELSE 0
      END"""


def _exact_uploader_knowledge_dependency(
    knowledge_alias: str = "k",
    raw_alias: str = "uploader_raw",
) -> str:
    """One exact, fail-closed author boundary for a Knowledge Object.

    The author is provenance on the source Raw Object, not the shared tenant on
    the Knowledge Object.  The returned expression contains one bound ``?`` for
    the exact uploader id.  Aliases are code-owned SQL identifiers; request data
    never enters the SQL text.

    Keep this as a correlated ``EXISTS`` rather than a caller-specific JOIN so
    FTS, date/recent pools and both vector readers can apply the identical rule
    before their own LIMIT without changing column resolution or join order.
    """

    public_raw = _not_private_raw_dependency(raw_alias)
    exact_uploader = _exact_uploader_raw_dependency(raw_alias)
    return f"""EXISTS (
        SELECT 1 FROM raw_objects {raw_alias}
         WHERE {raw_alias}.id={knowledge_alias}.raw_object_id
           AND {raw_alias}.user_id={knowledge_alias}.user_id
           AND {raw_alias}.deleted_at IS NULL
           AND {public_raw}
           AND {exact_uploader}
           AND NOT EXISTS (
               SELECT 1 FROM inbox uploader_inbox_verdict
                WHERE uploader_inbox_verdict.raw_object_id={raw_alias}.id
                  AND uploader_inbox_verdict.user_id={raw_alias}.user_id
                  AND uploader_inbox_verdict.status='ignored'
           )
    )"""


def _not_private_bounded_json_dependency(
    json_expression: str,
    user_expression: str,
    *,
    knowledge_paths: tuple[str, ...] = (),
    max_bytes: int = _GRAPH_PUBLIC_JSON_MAX_BYTES,
    reject_nested_json: bool = False,
    expected_json_type: str = "object",
    scan_knowledge: bool = True,
) -> str:
    """Validate one public JSON object and every durable private dependency in it.

    Expressions and paths are code-owned SQL fragments.  The nested ``CASE`` is
    intentional: SQLite JSON functions raise on malformed input, so validity and
    size have to be established before either ``json_tree`` or ``json_extract``
    is evaluated.  Hidden entity names are treated as dependencies too; an
    evidence excerpt is another copy of the private fact, not harmless metadata.
    """

    if expected_json_type not in {"array", "object"}:
        raise ValueError("expected_json_type must be array or object")
    anchor_checks: list[str] = []
    if knowledge_paths:
        visible_anchor = _not_private_knowledge_dependency("anchored_public_knowledge")
        for path in knowledge_paths:
            json_type = f"json_type({json_expression},'{path}')"
            json_value = f"trim(CAST(json_extract({json_expression},'{path}') AS TEXT))"
            anchor_checks.append(
                f"""(
                    {json_type} IS NULL OR (
                        {json_type}='text'
                        AND length({json_value})<=160
                        AND ({json_value}='' OR EXISTS (
                            SELECT 1 FROM knowledge_objects anchored_public_knowledge
                             WHERE anchored_public_knowledge.id={json_value}
                               AND anchored_public_knowledge.user_id={user_expression}
                               AND anchored_public_knowledge.deleted_at IS NULL
                               AND {visible_anchor}
                        ))
                    )
                )"""
            )
    anchors = " AND ".join(anchor_checks) if anchor_checks else "1"
    nested_json_guard = "1"
    if reject_nested_json:
        nested_json_guard = f"""NOT EXISTS (
            SELECT 1 FROM json_tree({json_expression}) nested_json_value
             WHERE (nested_json_value.type='text'
                    AND substr(ltrim(CAST(nested_json_value.value AS TEXT)),1,1)
                          IN ('{{','[','"'))
                OR substr(ltrim(CAST(nested_json_value.key AS TEXT)),1,1)
                     IN ('{{','[','"')
        )"""
    embedded_knowledge_guard = "1"
    if scan_knowledge:
        visible_embedded_knowledge = _not_private_knowledge_dependency("embedded_private_knowledge")
        embedded_knowledge_guard = f"""NOT EXISTS (
            SELECT 1 FROM knowledge_objects embedded_private_knowledge
             WHERE NOT ({visible_embedded_knowledge})
               AND EXISTS (
                   SELECT 1 FROM json_tree({json_expression}) embedded_knowledge_value
                    WHERE (embedded_knowledge_value.type='text'
                           AND
                          instr(CAST(embedded_knowledge_value.value AS TEXT),
                                embedded_private_knowledge.id)>0
                          )
                       OR instr(CAST(embedded_knowledge_value.key AS TEXT),
                                embedded_private_knowledge.id)>0
               )
        )"""
    return f"""(
        CASE
          WHEN length(CAST(COALESCE({json_expression},'') AS BLOB))<={max(1, int(max_bytes))}
          THEN CASE WHEN json_valid({json_expression})
            THEN CASE WHEN json_type({json_expression})='{expected_json_type}'
              THEN CASE WHEN
                {anchors}
                AND {nested_json_guard}
                AND NOT EXISTS (
                    SELECT 1 FROM private_entity_material_closure embedded_private_entity
                     WHERE EXISTS (
                           SELECT 1 FROM json_tree({json_expression}) embedded_json_value
                            WHERE (
                                  embedded_json_value.type='text'
                                  AND (
                                  instr(CAST(embedded_json_value.value AS TEXT),
                                        embedded_private_entity.id)>0
                                  OR (embedded_private_entity.name<>'' AND
                                      jericho_private_identity_match(
                                          CAST(embedded_json_value.value AS TEXT),
                                          embedded_private_entity.name)=1)
                                  )
                              )
                               OR instr(CAST(embedded_json_value.key AS TEXT),
                                        embedded_private_entity.id)>0
                               OR (embedded_private_entity.name<>'' AND
                                   jericho_private_identity_match(
                                       CAST(embedded_json_value.key AS TEXT),
                                       embedded_private_entity.name)=1)
                       )
                       AND NOT {_own_tenant_reminder_identity("embedded_private_entity.id", user_expression)}
                )
                AND {embedded_knowledge_guard}
              THEN 1 ELSE 0 END
            ELSE 0 END
          ELSE 0 END
        ELSE 0 END
    )"""


def _not_private_relation_candidate_dependency(alias: str = "c") -> str:
    """Public relation candidate evidence and its Knowledge Object anchors."""

    return _not_private_bounded_json_dependency(
        f"{alias}.evidence_json",
        f"{alias}.user_id",
        knowledge_paths=("$.knowledge_object_id", "$.evidence.knowledge_object_id"),
        reject_nested_json=True,
    )


def _not_private_resolution_candidate_dependency(alias: str = "c") -> str:
    """Public duplicate evidence and every graph object used to score the pair.

    Duplicate detection persists the complete KO/neighbour input sets in the
    evidence object.  Scanning every text leaf means a later reminder quarantine
    invalidates the old score even when the candidate only stored a derived
    scalar such as ``shared_knowledge`` next to those durable dependency IDs.
    Legacy/custom evidence remains supported, but malformed, nested-encoded or
    unbounded JSON cannot authorize a merge.
    """

    return _not_private_bounded_json_dependency(
        f"{alias}.evidence_json",
        f"{alias}.user_id",
        max_bytes=_RESOLUTION_PUBLIC_JSON_MAX_BYTES,
        reject_nested_json=True,
    )


def _not_private_relation_dependency(alias: str = "r") -> str:
    """Public relation metadata and the source facts which ground the edge."""

    metadata = f"{alias}.metadata_json"
    strict_review = _not_private_bounded_json_dependency(
        metadata,
        f"{alias}.user_id",
        knowledge_paths=("$.knowledge_object_id", "$.evidence.knowledge_object_id"),
        max_bytes=_RELATION_PUBLIC_JSON_MAX_BYTES,
        reject_nested_json=True,
    )
    # An explicit relation is a user-authored fact in its own right; its arbitrary
    # metadata is projected away at the public boundary.  Only the canonical
    # review signature says the relation fact itself was derived from evidence and
    # must disappear when that evidence becomes private.  The outer 1 MiB bound
    # still makes malformed/hostile legacy rows fail closed without rejecting the
    # established large-metadata projection contract.
    trusted_review = f"""(
        json_type({metadata},'$.origin')='text'
        AND json_extract({metadata},'$.origin')='review'
        AND json_type({metadata},'$.source')='text'
        AND json_extract({metadata},'$.source')='reviewed_relation_candidate'
        AND json_type({metadata},'$.candidate_id')='text'
        AND trim(CAST(json_extract({metadata},'$.candidate_id') AS TEXT))<>''
        AND json_type({metadata},'$.reviewed_by')='text'
        AND trim(CAST(json_extract({metadata},'$.reviewed_by') AS TEXT))<>''
        AND EXISTS (
            SELECT 1 FROM relation_candidates grounding_candidate
             WHERE grounding_candidate.id=
                       CAST(json_extract({metadata},'$.candidate_id') AS TEXT)
               AND grounding_candidate.user_id={alias}.user_id
               AND grounding_candidate.source_entity_id={alias}.source_entity_id
               AND grounding_candidate.target_entity_id={alias}.target_entity_id
               AND grounding_candidate.relation_type={alias}.relation_type
               AND grounding_candidate.status='accepted'
               AND grounding_candidate.reviewed_by=
                       CAST(json_extract({metadata},'$.reviewed_by') AS TEXT)
               AND {_not_private_relation_candidate_dependency("grounding_candidate")}
        )
    )"""
    review_intent = f"""(
        (json_type({metadata},'$.origin')='text'
         AND json_extract({metadata},'$.origin')='review')
        OR (json_type({metadata},'$.source')='text'
            AND json_extract({metadata},'$.source')='reviewed_relation_candidate')
    )"""
    return f"""(
        CASE
          WHEN length(CAST(COALESCE({metadata},'') AS BLOB))
                   <={_RELATION_PUBLIC_JSON_MAX_BYTES}
          THEN CASE WHEN json_valid({metadata})
            THEN CASE WHEN json_type({metadata})='object'
              THEN CASE WHEN {review_intent}
                        THEN CASE WHEN {trusted_review} THEN {strict_review} ELSE 0 END
                        ELSE 1 END
            ELSE 0 END
          ELSE 0 END
        ELSE 0 END
    )"""


def _not_private_notification_dependency(alias: str = "n") -> str:
    """Keep delayed outbound text private after an entity is quarantined.

    A personal reminder is the sole exception, and only for the exact durable
    owner.  Chronicle/reflection/monitor text remains derived shared material even
    when it happens to target the same chat, so its kind cannot opt into this
    exception.
    """

    return f"""(
        {_private_material_cache_valid()}
        AND length(CAST(COALESCE({alias}.body,'') AS BLOB))
            <={_PUBLIC_NOTIFICATION_BODY_MAX_BYTES}
        AND length(CAST(COALESCE({alias}.dedup_key,'') AS BLOB))
            <={_PUBLIC_NOTIFICATION_KEY_MAX_BYTES}
        AND NOT EXISTS (
            SELECT 1 FROM private_entity_material_closure queued_private_entity
             WHERE (
                   instr(COALESCE({alias}.body,''), queued_private_entity.id)>0
                   OR instr(COALESCE({alias}.dedup_key,''), queued_private_entity.id)>0
                   OR (queued_private_entity.name<>'' AND jericho_private_identity_match(
                       COALESCE({alias}.body,''), queued_private_entity.name)=1)
                   OR (queued_private_entity.name<>'' AND jericho_private_identity_match(
                       COALESCE({alias}.dedup_key,''), queued_private_entity.name)=1)
               )
               AND NOT (
                   {alias}.kind='reminder'
                   AND EXISTS (
                       SELECT 1 FROM private_entity_owners queued_exact_owner
                        WHERE queued_exact_owner.entity_id=queued_private_entity.id
                          AND queued_exact_owner.person_id={alias}.user_id
                          AND queued_exact_owner.privacy_kind='reminder'
                   )
                   AND EXISTS (
                       SELECT 1 FROM entity_time queued_exact_time
                        WHERE queued_exact_time.entity_id=queued_private_entity.id
                          AND queued_exact_time.source='reminder:' || {alias}.user_id
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM entity_time queued_conflicting_time
                        WHERE queued_conflicting_time.entity_id=queued_private_entity.id
                          AND queued_conflicting_time.source LIKE 'reminder:%'
                          AND queued_conflicting_time.source<>
                                  'reminder:' || {alias}.user_id
                   )
               )
        )
    )"""


_ENTITY_HISTORY_SAFE_SNAPSHOT = f"""CASE
    WHEN length(CAST(COALESCE(v.snapshot_json,'') AS BLOB))
             <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
    THEN CASE WHEN json_valid(v.snapshot_json)
         THEN CASE WHEN json_type(v.snapshot_json)='object'
              THEN v.snapshot_json ELSE '{{}}' END
         ELSE '{{}}' END
    ELSE '{{}}' END"""
_ENTITY_HISTORY_AUTHENTICATED = f"""(
    json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.id')=v.entity_id
    AND json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.user_id')=v.user_id
    AND json_type({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.version')='integer'
    AND json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.version')=v.version
    AND json_type({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.name')='text'
    AND json_type({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.description')='text'
    AND json_type({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.aliases_json')='text'
    AND json_type({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.metadata_json')='text'
)"""
_ENTITY_HISTORY_IDENTITY_TOKENS = f"""CASE
    WHEN {_ENTITY_HISTORY_AUTHENTICATED}
    THEN jericho_private_identity_tokens(
        COALESCE(json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.name'), ''),
        COALESCE(json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.aliases_json'), '')
    )
    ELSE '[]'
END"""


def _raw_derivative_live(alias: str) -> str:
    return f"({_private_material_cache_valid()} AND {_raw_material_expression(alias)})"


def _knowledge_derivative_live(alias: str) -> str:
    """Lookup the exact live predicate compiled once in the TEMP view.

    Inlining the predicate into each managed trigger repeated its guarded JSON
    ``CASE`` tree several times.  SQLite builds with a fixed Lemon parser stack
    overflowed while compiling ``privacy_material_knowledge_au_refresh`` even
    though the same predicate had already compiled successfully in the live
    view.  The view's ``knowledge`` branch is the former expression verbatim;
    this ID/tenant lookup changes only where SQLite parses it, not what is public.
    """

    return f"""EXISTS (
        SELECT 1 FROM private_entity_material_derivative_live live_knowledge
         WHERE live_knowledge.material_kind='knowledge'
           AND live_knowledge.object_id={alias}.id
           AND live_knowledge.user_id={alias}.user_id
    )"""


def _inbox_derivative_live(alias: str) -> str:
    return f"({_private_material_cache_valid()} AND {_inbox_dependency_expression(alias, _work=True)})"


def _derivative_decision_sql(local_condition: str = "1") -> str:
    """Capture pre-trigger validity before staging writes overwrite prior_valid."""

    return f"""
DELETE FROM private_entity_material_derivative_decision;
INSERT INTO private_entity_material_derivative_decision(prior_valid, local_ok)
SELECT derivative_state.prior_valid,
       CASE WHEN derivative_state.prior_valid=1 AND ({local_condition})
            THEN 1 ELSE 0 END
  FROM private_entity_material_derivative_state derivative_state
 WHERE derivative_state.singleton=1;
"""


_DERIVATIVE_DECISION_FINISH_SQL = """
UPDATE private_entity_material_derivative_state
   SET valid=1, prior_valid=1
 WHERE singleton=1
   AND EXISTS (
       SELECT 1 FROM private_entity_material_derivative_decision
        WHERE local_ok=1
   );
INSERT INTO private_entity_material_derivative_refresh(requested)
SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=0;
DELETE FROM private_entity_material_derivative_decision;
"""


def _derivative_replace_raw_sql(alias: str = "NEW") -> str:
    visible = _raw_derivative_live(alias)
    return f"""
DELETE FROM private_entity_material_derivative_work
 WHERE material_kind='raw' AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
DELETE FROM private_entity_material_derivative_cache
 WHERE material_kind='raw' AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
INSERT OR IGNORE INTO private_entity_material_derivative_work(
    material_kind, object_id, user_id
)
SELECT 'raw', {alias}.id, {alias}.user_id
 WHERE EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1)
   AND {visible};
INSERT OR IGNORE INTO private_entity_material_derivative_cache(
    material_kind, object_id, user_id
)
SELECT material_kind, object_id, user_id
  FROM private_entity_material_derivative_work
 WHERE material_kind='raw' AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
"""


def _derivative_replace_knowledge_sql(alias: str = "NEW") -> str:
    visible = _knowledge_derivative_live(alias)
    return f"""
DELETE FROM private_entity_material_derivative_work
 WHERE material_kind IN ('knowledge','knowledge_hidden') AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
DELETE FROM private_entity_material_derivative_cache
 WHERE material_kind IN ('knowledge','knowledge_hidden') AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
INSERT OR IGNORE INTO private_entity_material_derivative_work(
    material_kind, object_id, user_id
)
SELECT CASE WHEN {visible} THEN 'knowledge' ELSE 'knowledge_hidden' END,
       {alias}.id, {alias}.user_id
 WHERE EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
INSERT OR IGNORE INTO private_entity_material_derivative_cache(
    material_kind, object_id, user_id
)
SELECT material_kind, object_id, user_id
  FROM private_entity_material_derivative_work
 WHERE material_kind IN ('knowledge','knowledge_hidden') AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
"""


def _derivative_replace_inbox_sql(alias: str = "NEW") -> str:
    visible = _inbox_derivative_live(alias)
    return f"""
DELETE FROM private_entity_material_derivative_work
 WHERE material_kind='inbox' AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
DELETE FROM private_entity_material_derivative_cache
 WHERE material_kind='inbox' AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
INSERT OR IGNORE INTO private_entity_material_derivative_work(
    material_kind, object_id, user_id
)
SELECT 'inbox', {alias}.id, {alias}.user_id
 WHERE EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1)
   AND {visible};
INSERT OR IGNORE INTO private_entity_material_derivative_cache(
    material_kind, object_id, user_id
)
SELECT material_kind, object_id, user_id
  FROM private_entity_material_derivative_work
 WHERE material_kind='inbox' AND object_id={alias}.id
   AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
"""


_DERIVATIVE_CACHE_REBUILD_SQL = f"""
UPDATE private_entity_material_derivative_state
   SET valid=0, prior_valid=0 WHERE singleton=1;
DELETE FROM private_entity_material_derivative_work;
INSERT OR IGNORE INTO private_entity_material_derivative_work(
    material_kind, object_id, user_id
)
SELECT 'raw', r.id, r.user_id
  FROM raw_objects r
 WHERE {_private_material_cache_valid()}
   AND {_raw_material_expression("r")};
INSERT OR IGNORE INTO private_entity_material_derivative_work(
    material_kind, object_id, user_id
)
SELECT 'knowledge', k.id, k.user_id
  FROM knowledge_objects k
 WHERE {_private_material_cache_valid()}
   AND {_not_private_knowledge_structure_dependency("k", _work=True)}
   AND {_knowledge_material_expression("k")};
INSERT OR IGNORE INTO private_entity_material_derivative_work(
    material_kind, object_id, user_id
)
SELECT 'knowledge_hidden', k.id, k.user_id
  FROM knowledge_objects k
 WHERE NOT EXISTS (
       SELECT 1 FROM private_entity_material_derivative_work visible_knowledge
        WHERE visible_knowledge.material_kind='knowledge'
          AND visible_knowledge.object_id=k.id
          AND visible_knowledge.user_id=k.user_id
   );
INSERT OR IGNORE INTO private_entity_material_derivative_work(
    material_kind, object_id, user_id
)
SELECT 'inbox', i.id, i.user_id
  FROM inbox i
 WHERE {_private_material_cache_valid()}
   AND {_inbox_dependency_expression("i", _work=True)};
DELETE FROM private_entity_material_derivative_cache;
INSERT OR IGNORE INTO private_entity_material_derivative_cache(
    material_kind, object_id, user_id
)
SELECT material_kind, object_id, user_id
  FROM private_entity_material_derivative_work;
UPDATE private_entity_material_derivative_state
   SET valid=1, prior_valid=1 WHERE singleton=1;
"""

_MATERIAL_CACHE_REBUILD_SQL = f"""
UPDATE private_entity_material_cache_state
   SET valid=0, prior_valid=0 WHERE singleton=1;
DELETE FROM private_entity_material_work;
INSERT OR IGNORE INTO private_entity_material_work(entity_id)
SELECT id FROM private_entity_material_live;
DELETE FROM private_entity_material_cache;
INSERT OR IGNORE INTO private_entity_material_cache(entity_id)
SELECT entity_id FROM private_entity_material_work;
UPDATE private_entity_material_cache_state
   SET valid=1, prior_valid=1 WHERE singleton=1;
{_DERIVATIVE_CACHE_REBUILD_SQL}
"""
_MATERIAL_CACHE_INVALID = f"NOT ({_private_material_cache_valid()})"
_MATERIAL_CACHE_PRIOR_INVALID = """EXISTS (
    SELECT 1 FROM private_entity_material_cache_state prior_cache_state
     WHERE prior_cache_state.singleton=1 AND prior_cache_state.prior_valid=0
)"""
_NEW_ENTITY_MATERIAL_REBUILD = f"""(
    {_MATERIAL_CACHE_PRIOR_INVALID}
    OR NOT ({_not_private_reminder_entity("NEW")})
    OR NOT ({_entity_material_shape("NEW")})
    OR EXISTS (
        SELECT 1 FROM private_entity_material_cached_closure cached_dependency
         WHERE {_entity_material_copy_condition("NEW", "cached_dependency")}
    )
)"""
_NEW_VERSION_MATERIAL_REBUILD = f"""EXISTS (
    SELECT 1 FROM private_entity_material_states new_version_state
     WHERE new_version_state.version_id=NEW.id
       AND (
           new_version_state.material_valid=0
           OR EXISTS (
               SELECT 1 FROM private_entity_material_cached_closure cached_version_dependency
                WHERE {
    _entity_material_copy_condition(
        "new_version_state",
        "cached_version_dependency",
    )
}
           )
       )
) OR {_MATERIAL_CACHE_PRIOR_INVALID}"""


PRIVATE_MATERIAL_PERSISTENT_SCHEMA = """
-- Persistent schema is deliberately UDF-free.  Offline SQLite migrations and
-- repair tools must be able to reparse it without importing the application.
DROP VIEW IF EXISTS public_inbox_dependencies;
DROP VIEW IF EXISTS public_knowledge_dependencies;
DROP VIEW IF EXISTS public_raw_material;
DROP VIEW IF EXISTS private_entity_material_closure;
DROP VIEW IF EXISTS private_entity_material_live;
DROP VIEW IF EXISTS private_entity_identity_tokens;
DROP VIEW IF EXISTS private_entity_material_states;

-- All three tables are disposable ID/validity derivatives.  Recreate them
-- after the pre-schema integrity audit; no user material lives here.
DROP TABLE IF EXISTS private_entity_material_work;
DROP TABLE IF EXISTS private_entity_material_cache;
DROP TABLE IF EXISTS private_entity_material_cache_state;
DROP TABLE IF EXISTS private_entity_material_derivative_work;
DROP TABLE IF EXISTS private_entity_material_derivative_cache;
DROP TABLE IF EXISTS private_entity_material_derivative_state;
CREATE TABLE private_entity_material_cache (entity_id TEXT PRIMARY KEY);
CREATE TABLE private_entity_material_work (entity_id TEXT PRIMARY KEY);
CREATE TABLE private_entity_material_cache_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    valid INTEGER NOT NULL CHECK(valid IN (0,1)),
    prior_valid INTEGER NOT NULL CHECK(prior_valid IN (0,1))
);
INSERT INTO private_entity_material_cache_state(singleton, valid, prior_valid)
VALUES(1, 0, 0);

-- Current Raw material, Knowledge and Inbox visibility are a dense derivative
-- of the sparse private-entity closure.  Persist only opaque object/tenant IDs;
-- body, JSON and identity tokens remain connection-local in TEMP views.  The
-- duplicate work set lets a managed rebuild publish all three dependency tiers
-- atomically and gives the state guard a UDF-free equality proof.
CREATE TABLE private_entity_material_derivative_cache (
    material_kind TEXT NOT NULL
        CHECK(material_kind IN ('raw','knowledge','knowledge_hidden','inbox')),
    object_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY(material_kind, object_id, user_id)
) WITHOUT ROWID;
CREATE TABLE private_entity_material_derivative_work (
    material_kind TEXT NOT NULL
        CHECK(material_kind IN ('raw','knowledge','knowledge_hidden','inbox')),
    object_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    PRIMARY KEY(material_kind, object_id, user_id)
) WITHOUT ROWID;
CREATE TABLE private_entity_material_derivative_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    valid INTEGER NOT NULL CHECK(valid IN (0,1)),
    prior_valid INTEGER NOT NULL CHECK(prior_valid IN (0,1))
);
INSERT INTO private_entity_material_derivative_state(singleton, valid, prior_valid)
VALUES(1, 0, 0);

CREATE TRIGGER privacy_material_cache_ai
AFTER INSERT ON private_entity_material_cache
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_cache_au
AFTER UPDATE ON private_entity_material_cache
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_cache_ad
AFTER DELETE ON private_entity_material_cache
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;

CREATE TRIGGER privacy_material_work_ai
AFTER INSERT ON private_entity_material_work
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_work_au
AFTER UPDATE ON private_entity_material_work
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_work_ad
AFTER DELETE ON private_entity_material_work
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;

CREATE TRIGGER privacy_material_derivative_cache_ai
AFTER INSERT ON private_entity_material_derivative_cache
BEGIN
    UPDATE private_entity_material_derivative_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_derivative_cache_au
AFTER UPDATE ON private_entity_material_derivative_cache
BEGIN
    UPDATE private_entity_material_derivative_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_derivative_cache_ad
AFTER DELETE ON private_entity_material_derivative_cache
BEGIN
    UPDATE private_entity_material_derivative_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_derivative_work_ai
AFTER INSERT ON private_entity_material_derivative_work
BEGIN
    UPDATE private_entity_material_derivative_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_derivative_work_au
AFTER UPDATE ON private_entity_material_derivative_work
BEGIN
    UPDATE private_entity_material_derivative_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_derivative_work_ad
AFTER DELETE ON private_entity_material_derivative_work
BEGIN
    UPDATE private_entity_material_derivative_state
       SET valid=0, prior_valid=0 WHERE singleton=1;
END;

CREATE TRIGGER privacy_material_cache_state_bi
BEFORE INSERT ON private_entity_material_cache_state
WHEN NEW.valid=1 AND EXISTS (
    SELECT 1 FROM (
        SELECT work.entity_id FROM private_entity_material_work work
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_cache cached
              WHERE cached.entity_id=work.entity_id
         )
        UNION ALL
        SELECT cached.entity_id FROM private_entity_material_cache cached
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_work work
              WHERE work.entity_id=cached.entity_id
         )
    ) cache_difference
)
BEGIN
    SELECT RAISE(ABORT, 'private material cache does not match live closure');
END;
CREATE TRIGGER privacy_material_cache_state_bu
BEFORE UPDATE OF valid ON private_entity_material_cache_state
WHEN NEW.valid=1 AND EXISTS (
    SELECT 1 FROM (
        SELECT work.entity_id FROM private_entity_material_work work
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_cache cached
              WHERE cached.entity_id=work.entity_id
         )
        UNION ALL
        SELECT cached.entity_id FROM private_entity_material_cache cached
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_work work
              WHERE work.entity_id=cached.entity_id
         )
    ) cache_difference
)
BEGIN
    SELECT RAISE(ABORT, 'private material cache does not match live closure');
END;

CREATE TRIGGER privacy_material_derivative_state_bi
BEFORE INSERT ON private_entity_material_derivative_state
WHEN NEW.valid=1 AND EXISTS (
    SELECT 1 FROM (
        SELECT work.material_kind, work.object_id, work.user_id
          FROM private_entity_material_derivative_work work
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_derivative_cache cached
              WHERE cached.material_kind=work.material_kind
                AND cached.object_id=work.object_id
                AND cached.user_id=work.user_id
         )
        UNION ALL
        SELECT cached.material_kind, cached.object_id, cached.user_id
          FROM private_entity_material_derivative_cache cached
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_derivative_work work
              WHERE work.material_kind=cached.material_kind
                AND work.object_id=cached.object_id
                AND work.user_id=cached.user_id
         )
    ) derivative_difference
)
BEGIN
    SELECT RAISE(ABORT, 'private derivative cache does not match rebuild work');
END;
CREATE TRIGGER privacy_material_derivative_state_bu
BEFORE UPDATE OF valid ON private_entity_material_derivative_state
WHEN NEW.valid=1 AND EXISTS (
    SELECT 1 FROM (
        SELECT work.material_kind, work.object_id, work.user_id
          FROM private_entity_material_derivative_work work
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_derivative_cache cached
              WHERE cached.material_kind=work.material_kind
                AND cached.object_id=work.object_id
                AND cached.user_id=work.user_id
         )
        UNION ALL
        SELECT cached.material_kind, cached.object_id, cached.user_id
          FROM private_entity_material_derivative_cache cached
         WHERE NOT EXISTS (
             SELECT 1 FROM private_entity_material_derivative_work work
              WHERE work.material_kind=cached.material_kind
                AND work.object_id=cached.object_id
                AND work.user_id=cached.user_id
         )
    ) derivative_difference
)
BEGIN
    SELECT RAISE(ABORT, 'private derivative cache does not match rebuild work');
END;

-- A raw/out-of-process source write cannot rebuild Unicode/JSON closure safely.
-- It can always invalidate authority, however.  Managed connections add TEMP
-- AFTER triggers which either prove the cache unchanged or rebuild it exactly.
CREATE TRIGGER privacy_material_entities_bi_invalidate BEFORE INSERT ON entities
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_entities_bu_invalidate BEFORE UPDATE OF
    id, user_id, name, aliases_json, description, metadata_json ON entities
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_entities_bd_invalidate BEFORE DELETE ON entities
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_owners_bi_invalidate BEFORE INSERT ON private_entity_owners
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_owners_bu_invalidate BEFORE UPDATE ON private_entity_owners
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_owners_bd_invalidate BEFORE DELETE ON private_entity_owners
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_time_bi_invalidate BEFORE INSERT ON entity_time
WHEN NEW.source LIKE 'reminder:%'
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_time_bu_invalidate BEFORE UPDATE OF
    source, entity_id, user_id ON entity_time
WHEN OLD.source LIKE 'reminder:%' OR NEW.source LIKE 'reminder:%'
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_time_bd_invalidate BEFORE DELETE ON entity_time
WHEN OLD.source LIKE 'reminder:%'
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_versions_bi_invalidate BEFORE INSERT ON entity_versions
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_versions_bu_invalidate BEFORE UPDATE OF
    id, entity_id, user_id, version, snapshot_json ON entity_versions
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_versions_bd_invalidate BEFORE DELETE ON entity_versions
BEGIN
    UPDATE private_entity_material_cache_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;

-- Out-of-process source writers cannot evaluate the Unicode/JSON predicates,
-- but they can atomically revoke the dense allowlist before changing a source.
-- FridayStorage.transaction() rebuilds once at its outer commit; a connection-
-- local AFTER fallback below covers a direct managed SQL statement.
CREATE TRIGGER privacy_material_raw_bi_invalidate BEFORE INSERT ON raw_objects
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_raw_bu_invalidate BEFORE UPDATE OF
    id, user_id, raw_content, source_ref, source, content_type, metadata_json
    ON raw_objects
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_raw_bd_invalidate BEFORE DELETE ON raw_objects
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;

CREATE TRIGGER privacy_material_knowledge_bi_invalidate BEFORE INSERT ON knowledge_objects
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_knowledge_bu_invalidate BEFORE UPDATE OF
    id, user_id, raw_object_id, entity_id, content, content_type, title,
    summary, tags_json, metadata_json, knowledge_kind, superseded_by_id, deleted_at
    ON knowledge_objects
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_knowledge_bd_invalidate BEFORE DELETE ON knowledge_objects
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;

CREATE TRIGGER privacy_material_links_bi_invalidate BEFORE INSERT ON knowledge_entity_links
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_links_bu_invalidate BEFORE UPDATE OF
    user_id, knowledge_object_id, entity_id ON knowledge_entity_links
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_links_bd_invalidate BEFORE DELETE ON knowledge_entity_links
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;

CREATE TRIGGER privacy_material_inbox_bi_invalidate BEFORE INSERT ON inbox
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_inbox_bu_invalidate BEFORE UPDATE OF
    id, user_id, raw_object_id, knowledge_object_id, suggested_entity_id,
    suggested_tags_json, suggestions_json, classification_notes ON inbox
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;
CREATE TRIGGER privacy_material_inbox_bd_invalidate BEFORE DELETE ON inbox
BEGIN
    UPDATE private_entity_material_derivative_state
       SET prior_valid=valid, valid=0 WHERE singleton=1;
END;

-- Cached identity/history remains immutable until quarantine is deliberately
-- removed and a managed TEMP trigger has rebuilt the authority.
CREATE TRIGGER privacy_material_entities_private_bu
BEFORE UPDATE OF id, user_id, name, aliases_json, description, metadata_json ON entities
WHEN EXISTS (
    SELECT 1 FROM private_entity_material_cache cached_private
     WHERE cached_private.entity_id=OLD.id
) AND (
       NEW.id IS NOT OLD.id
    OR NEW.user_id IS NOT OLD.user_id
    OR NEW.name IS NOT OLD.name
    OR NEW.aliases_json IS NOT OLD.aliases_json
    OR NEW.description IS NOT OLD.description
    OR NEW.metadata_json IS NOT OLD.metadata_json
)
BEGIN
    SELECT RAISE(ABORT, 'private entity material is immutable while quarantined');
END;
CREATE TRIGGER privacy_material_entities_private_bd
BEFORE DELETE ON entities
WHEN EXISTS (
    SELECT 1 FROM private_entity_material_cache cached_private
     WHERE cached_private.entity_id=OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'private entity material cannot be deleted while quarantined');
END;
CREATE TRIGGER privacy_material_versions_private_bu
BEFORE UPDATE OF id, entity_id, user_id, version, snapshot_json ON entity_versions
WHEN EXISTS (
    SELECT 1 FROM private_entity_material_cache cached_private
     WHERE cached_private.entity_id=OLD.entity_id
)
BEGIN
    SELECT RAISE(ABORT, 'private entity history is immutable while quarantined');
END;
CREATE TRIGGER privacy_material_versions_private_bd
BEFORE DELETE ON entity_versions
WHEN EXISTS (
    SELECT 1 FROM private_entity_material_cache cached_private
     WHERE cached_private.entity_id=OLD.entity_id
)
BEGIN
    SELECT RAISE(ABORT, 'private entity history cannot be deleted while quarantined');
END;
"""


PRIVATE_MATERIAL_RUNTIME_SCHEMA = f"""
DROP TRIGGER IF EXISTS privacy_material_entities_ai;
DROP TRIGGER IF EXISTS privacy_material_entities_ai_restore;
DROP TRIGGER IF EXISTS privacy_material_entities_au;
DROP TRIGGER IF EXISTS privacy_material_entities_au_restore;
DROP TRIGGER IF EXISTS privacy_material_entities_ad;
DROP TRIGGER IF EXISTS privacy_material_owners_ai;
DROP TRIGGER IF EXISTS privacy_material_owners_au;
DROP TRIGGER IF EXISTS privacy_material_owners_ad;
DROP TRIGGER IF EXISTS privacy_material_time_ai;
DROP TRIGGER IF EXISTS privacy_material_time_au;
DROP TRIGGER IF EXISTS privacy_material_time_ad;
DROP TRIGGER IF EXISTS privacy_material_versions_ai;
DROP TRIGGER IF EXISTS privacy_material_versions_ai_restore;
DROP TRIGGER IF EXISTS privacy_material_versions_au;
DROP TRIGGER IF EXISTS privacy_material_versions_au_restore;
DROP TRIGGER IF EXISTS privacy_material_versions_ad;
DROP TRIGGER IF EXISTS privacy_material_raw_ai_refresh;
DROP TRIGGER IF EXISTS privacy_material_raw_au_refresh;
DROP TRIGGER IF EXISTS privacy_material_raw_ad_refresh;
DROP TRIGGER IF EXISTS privacy_material_knowledge_ai_refresh;
DROP TRIGGER IF EXISTS privacy_material_knowledge_au_refresh;
DROP TRIGGER IF EXISTS privacy_material_knowledge_ad_refresh;
DROP TRIGGER IF EXISTS privacy_material_links_ai_refresh;
DROP TRIGGER IF EXISTS privacy_material_links_au_refresh;
DROP TRIGGER IF EXISTS privacy_material_links_ad_refresh;
DROP TRIGGER IF EXISTS privacy_material_inbox_ai_refresh;
DROP TRIGGER IF EXISTS privacy_material_inbox_au_refresh;
DROP TRIGGER IF EXISTS privacy_material_inbox_ad_refresh;
DROP TRIGGER IF EXISTS privacy_material_derivative_refresh_ai;

DROP TABLE IF EXISTS temp.private_entity_material_derivative_refresh;
DROP TABLE IF EXISTS temp.private_entity_material_derivative_decision;

DROP VIEW IF EXISTS temp.public_inbox_dependencies;
DROP VIEW IF EXISTS temp.public_knowledge_dependencies;
DROP VIEW IF EXISTS temp.public_raw_material;
DROP VIEW IF EXISTS temp.private_entity_material_derivative_live;
DROP VIEW IF EXISTS temp.private_entity_material_closure;
DROP VIEW IF EXISTS temp.private_entity_material_cached_closure;
DROP VIEW IF EXISTS temp.private_entity_own_tenant_reminder;
DROP VIEW IF EXISTS temp.private_entity_material_live;
DROP VIEW IF EXISTS temp.private_entity_identity_tokens;
DROP VIEW IF EXISTS temp.private_entity_material_states;

CREATE TEMP TABLE private_entity_material_derivative_refresh (
    requested INTEGER NOT NULL CHECK(requested=1)
);
CREATE TEMP TABLE private_entity_material_derivative_decision (
    prior_valid INTEGER NOT NULL CHECK(prior_valid IN (0,1)),
    local_ok INTEGER NOT NULL CHECK(local_ok IN (0,1))
);

CREATE TEMP VIEW private_entity_material_states AS
SELECT e.id AS id, '' AS version_id, 1 AS identity_authenticated,
       e.name AS name, e.description AS description,
       e.aliases_json AS aliases_json, e.metadata_json AS metadata_json,
       CASE WHEN {_entity_material_shape("e")} THEN 1 ELSE 0 END AS material_valid
  FROM entities e
UNION ALL
SELECT history_state.id, history_state.version_id, history_state.snapshot_valid,
       history_state.name, history_state.description,
       history_state.aliases_json, history_state.metadata_json,
       CASE WHEN history_state.snapshot_valid=1
                  AND {_entity_material_shape("history_state")}
            THEN 1 ELSE 0 END AS material_valid
  FROM (
      SELECT v.entity_id AS id, v.id AS version_id,
             COALESCE(json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.name'), '') AS name,
             COALESCE(json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.description'), '')
                 AS description,
             COALESCE(json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.aliases_json'), '')
                 AS aliases_json,
             COALESCE(json_extract({_ENTITY_HISTORY_SAFE_SNAPSHOT}, '$.metadata_json'), '')
                 AS metadata_json,
             CASE WHEN {_ENTITY_HISTORY_AUTHENTICATED}
                  THEN 1 ELSE 0 END AS snapshot_valid
        FROM entity_versions v
  ) history_state;

-- §76: плоский ответ на вопрос «это СВОЁ напоминание своего же арендатора?».
--
-- Условие могло бы стоять прямо в предикатах, и первая редакция так и стояла —
-- но пять вложенных `EXISTS` внутри и без того глубокого выражения пробили
-- предел разбора: на сборке SQLite с консервативными лимитами (`maximum depth
-- 30`) представление переставало компилироваться вовсе. Отдельный вид даёт ту же
-- проверку глубиной в один `EXISTS`.
--
-- Арендатор сущности, человек-владелец и провенанс расписания должны совпадать
-- между собой, поэтому ключом достаточно одного `tenant_id`: потребитель
-- сравнивает его с арендатором СВОЕГО носителя.
CREATE TEMP VIEW private_entity_own_tenant_reminder AS
SELECT own_reminder_entity.id AS entity_id,
       own_reminder_entity.user_id AS tenant_id
  FROM entities own_reminder_entity
  JOIN private_entity_owners own_reminder_owner
    ON own_reminder_owner.entity_id=own_reminder_entity.id
   AND own_reminder_owner.person_id=own_reminder_entity.user_id
   AND own_reminder_owner.privacy_kind='reminder'
  JOIN entity_time own_reminder_time
    ON own_reminder_time.entity_id=own_reminder_entity.id
   AND own_reminder_time.user_id=own_reminder_entity.user_id
   AND own_reminder_time.source='reminder:' || own_reminder_entity.user_id
 WHERE NOT EXISTS (
       SELECT 1 FROM private_entity_owners other_reminder_owner
        WHERE other_reminder_owner.entity_id=own_reminder_entity.id
          AND (
               other_reminder_owner.person_id<>own_reminder_entity.user_id
               OR other_reminder_owner.privacy_kind<>'reminder'
          )
   )
   AND NOT EXISTS (
       SELECT 1 FROM entity_time other_reminder_time
        WHERE other_reminder_time.entity_id=own_reminder_entity.id
          AND other_reminder_time.source LIKE 'reminder:%'
          AND (
               other_reminder_time.user_id<>own_reminder_entity.user_id
               OR other_reminder_time.source<>'reminder:' || own_reminder_entity.user_id
          )
   )
   AND NOT EXISTS (
       SELECT 1 FROM private_entity_material_states own_reminder_state
        WHERE own_reminder_state.id=own_reminder_entity.id
          AND own_reminder_state.material_valid=0
   );

CREATE TEMP VIEW private_entity_identity_tokens AS
SELECT DISTINCT material_state.id AS id, CAST(identity_token.value AS TEXT) AS name
  FROM private_entity_material_states material_state,
       json_each(CASE WHEN material_state.identity_authenticated=1
         THEN jericho_private_identity_tokens(
             material_state.name,
             material_state.aliases_json
         ) ELSE '[]' END) identity_token
 WHERE material_state.identity_authenticated=1
   AND identity_token.type='text'
   AND identity_token.value<>''
   AND length(CAST(COALESCE(identity_token.value,'') AS BLOB))
           <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES};

CREATE TEMP VIEW private_entity_material_live AS
{_private_entity_material_live_cte()}
SELECT id FROM private_material_id;

CREATE TEMP VIEW private_entity_material_cached_closure AS
-- Anchor every hot identity lookup on the sparse persistent ID authority before
-- deriving text.  Joining the cache to the global DISTINCT token view makes
-- SQLite materialize current + every historical token for the whole graph at
-- each nested Raw/Knowledge/Inbox predicate, even when only a handful of IDs are
-- private.  CROSS JOIN is intentional: it prevents the planner from reversing
-- the history join into a full entity_versions scan.  Identity text remains
-- connection-local and live; nothing new is copied into the main DB/WAL.
SELECT material.entity_id AS id, '' AS name
  FROM private_entity_material_cache material
UNION
SELECT material.entity_id AS id, CAST(identity_token.value AS TEXT) AS name
  FROM private_entity_material_cache material
  CROSS JOIN entities current_entity
    ON current_entity.id=material.entity_id
  CROSS JOIN json_each(jericho_private_identity_tokens(
      current_entity.name,
      current_entity.aliases_json
  )) identity_token
 WHERE identity_token.type='text'
   AND identity_token.value<>''
   AND length(CAST(COALESCE(identity_token.value,'') AS BLOB))
           <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
UNION
SELECT material.entity_id AS id, CAST(identity_token.value AS TEXT) AS name
  FROM private_entity_material_cache material
  CROSS JOIN entity_versions v
    ON v.entity_id=material.entity_id
  CROSS JOIN json_each({_ENTITY_HISTORY_IDENTITY_TOKENS}) identity_token
 WHERE {_ENTITY_HISTORY_AUTHENTICATED}
   AND identity_token.type='text'
   AND identity_token.value<>''
   AND length(CAST(COALESCE(identity_token.value,'') AS BLOB))
           <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES};

CREATE TEMP VIEW private_entity_material_closure AS
SELECT cached_material.id, cached_material.name
  FROM private_entity_material_cache_state cache_state
  CROSS JOIN private_entity_material_cached_closure cached_material
 WHERE cache_state.singleton=1 AND cache_state.valid=1
UNION ALL
-- A raw/offline writer invalidates the persistent authority before changing a
-- privacy source.  Keep the old conservative all-entity fallback, but put the
-- singleton state first in every loop: while valid, SQLite must not derive the
-- global identity set speculatively.  The unconditional empty-name row retains
-- ID matching even for malformed/no-token identities; extra token rows can only
-- make the invalid state more restrictive.
SELECT entity.id AS id, '' AS name
  FROM private_entity_material_cache_state cache_state
  CROSS JOIN entities entity
 WHERE cache_state.singleton=1 AND cache_state.valid=0
UNION ALL
SELECT entity.id AS id, CAST(identity_token.value AS TEXT) AS name
  FROM private_entity_material_cache_state cache_state
  CROSS JOIN entities entity
  CROSS JOIN json_each(jericho_private_identity_tokens(
      entity.name,
      entity.aliases_json
  )) identity_token
 WHERE cache_state.singleton=1 AND cache_state.valid=0
   AND identity_token.type='text'
   AND identity_token.value<>''
   AND length(CAST(COALESCE(identity_token.value,'') AS BLOB))
           <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES}
UNION ALL
SELECT v.entity_id AS id, CAST(identity_token.value AS TEXT) AS name
  FROM private_entity_material_cache_state cache_state
  CROSS JOIN entity_versions v
  CROSS JOIN json_each({_ENTITY_HISTORY_IDENTITY_TOKENS}) identity_token
 WHERE cache_state.singleton=1 AND cache_state.valid=0
   AND {_ENTITY_HISTORY_AUTHENTICATED}
   AND identity_token.type='text'
   AND identity_token.value<>''
   AND length(CAST(COALESCE(identity_token.value,'') AS BLOB))
           <={_ENTITY_PUBLIC_MATERIAL_MAX_BYTES};

CREATE TEMP VIEW public_raw_material AS
SELECT derivative_cache.object_id AS id, derivative_cache.user_id
  FROM private_entity_material_derivative_state derivative_state
  CROSS JOIN private_entity_material_derivative_cache derivative_cache
 WHERE derivative_state.singleton=1
   AND derivative_state.valid=1
   AND {_private_material_cache_valid()}
   AND derivative_cache.material_kind='raw';

CREATE TEMP VIEW public_knowledge_dependencies AS
SELECT derivative_cache.object_id AS id, derivative_cache.user_id
  FROM private_entity_material_derivative_state derivative_state
  CROSS JOIN private_entity_material_derivative_cache derivative_cache
 WHERE derivative_state.singleton=1
   AND derivative_state.valid=1
   AND {_private_material_cache_valid()}
   AND derivative_cache.material_kind='knowledge';

CREATE TEMP VIEW public_inbox_dependencies AS
SELECT derivative_cache.object_id AS id, derivative_cache.user_id
  FROM private_entity_material_derivative_state derivative_state
  CROSS JOIN private_entity_material_derivative_cache derivative_cache
 WHERE derivative_state.singleton=1
   AND derivative_state.valid=1
   AND {_private_material_cache_valid()}
   AND derivative_cache.material_kind='inbox';

CREATE TEMP VIEW private_entity_material_derivative_live AS
SELECT 'raw' AS material_kind, r.id AS object_id, r.user_id AS user_id
  FROM raw_objects r
 WHERE {_private_material_cache_valid()}
   AND {_raw_material_expression("r")}
UNION ALL
SELECT 'knowledge', k.id, k.user_id
  FROM knowledge_objects k
 WHERE {_private_material_cache_valid()}
   AND {_not_private_knowledge_structure_dependency("k", _work=True)}
   AND {_knowledge_material_expression("k")}
UNION ALL
SELECT 'knowledge_hidden', k.id, k.user_id
  FROM knowledge_objects k
 WHERE NOT EXISTS (
       SELECT 1 FROM private_entity_material_derivative_work visible_knowledge
        WHERE visible_knowledge.material_kind='knowledge'
          AND visible_knowledge.object_id=k.id
          AND visible_knowledge.user_id=k.user_id
   )
UNION ALL
SELECT 'inbox', i.id, i.user_id
  FROM inbox i
 WHERE {_private_material_cache_valid()}
   AND {_inbox_dependency_expression("i", _work=True)};

CREATE TEMP TRIGGER privacy_material_derivative_refresh_ai
AFTER INSERT ON private_entity_material_derivative_refresh
BEGIN
    {_DERIVATIVE_CACHE_REBUILD_SQL}
    DELETE FROM private_entity_material_derivative_refresh;
END;

CREATE TEMP TRIGGER privacy_material_entities_ai AFTER INSERT ON main.entities
WHEN {_NEW_ENTITY_MATERIAL_REBUILD}
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_entities_ai_restore AFTER INSERT ON main.entities
WHEN NOT ({_NEW_ENTITY_MATERIAL_REBUILD})
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=1, prior_valid=1 WHERE singleton=1;
END;

CREATE TEMP TRIGGER privacy_material_entities_au AFTER UPDATE OF
    id, user_id, name, aliases_json, description, metadata_json ON main.entities
WHEN EXISTS (
         SELECT 1 FROM private_entity_material_cache
          WHERE entity_id=OLD.id
     ) OR {_NEW_ENTITY_MATERIAL_REBUILD}
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_entities_au_restore AFTER UPDATE OF
    id, user_id, name, aliases_json, description, metadata_json ON main.entities
WHEN NOT (
    EXISTS (
        SELECT 1 FROM private_entity_material_cache WHERE entity_id=OLD.id
    ) OR {_NEW_ENTITY_MATERIAL_REBUILD}
)
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=1, prior_valid=1 WHERE singleton=1;
END;

CREATE TEMP TRIGGER privacy_material_entities_ad AFTER DELETE ON main.entities
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;

CREATE TEMP TRIGGER privacy_material_owners_ai AFTER INSERT ON main.private_entity_owners
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_owners_au AFTER UPDATE ON main.private_entity_owners
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_owners_ad AFTER DELETE ON main.private_entity_owners
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;

CREATE TEMP TRIGGER privacy_material_time_ai AFTER INSERT ON main.entity_time
WHEN NEW.source LIKE 'reminder:%'
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_time_au AFTER UPDATE OF
    source, entity_id, user_id ON main.entity_time
WHEN OLD.source LIKE 'reminder:%' OR NEW.source LIKE 'reminder:%'
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_time_ad AFTER DELETE ON main.entity_time
WHEN OLD.source LIKE 'reminder:%'
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;

CREATE TEMP TRIGGER privacy_material_versions_ai AFTER INSERT ON main.entity_versions
WHEN EXISTS (
    SELECT 1 FROM private_entity_material_cache WHERE entity_id=NEW.entity_id
) OR {_NEW_VERSION_MATERIAL_REBUILD}
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_versions_ai_restore AFTER INSERT ON main.entity_versions
WHEN NOT (
    EXISTS (
        SELECT 1 FROM private_entity_material_cache WHERE entity_id=NEW.entity_id
    ) OR {_NEW_VERSION_MATERIAL_REBUILD}
)
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=1, prior_valid=1 WHERE singleton=1;
END;
CREATE TEMP TRIGGER privacy_material_versions_au AFTER UPDATE OF
    id, entity_id, user_id, version, snapshot_json ON main.entity_versions
WHEN EXISTS (
    SELECT 1 FROM private_entity_material_cache WHERE entity_id=NEW.entity_id
) OR EXISTS (
    SELECT 1 FROM private_entity_material_cache WHERE entity_id=OLD.entity_id
) OR {_NEW_VERSION_MATERIAL_REBUILD}
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;
CREATE TEMP TRIGGER privacy_material_versions_au_restore AFTER UPDATE OF
    id, entity_id, user_id, version, snapshot_json ON main.entity_versions
WHEN NOT (
    EXISTS (
        SELECT 1 FROM private_entity_material_cache WHERE entity_id=NEW.entity_id
    ) OR EXISTS (
        SELECT 1 FROM private_entity_material_cache WHERE entity_id=OLD.entity_id
    ) OR {_NEW_VERSION_MATERIAL_REBUILD}
)
BEGIN
    UPDATE private_entity_material_cache_state
       SET valid=1, prior_valid=1 WHERE singleton=1;
END;
CREATE TEMP TRIGGER privacy_material_versions_ad AFTER DELETE ON main.entity_versions
BEGIN
    {_MATERIAL_CACHE_REBUILD_SQL}
END;

-- Common ingest mutations update one ID in both authority copies.  The decision
-- row captures ``prior_valid`` before cache/work guards overwrite it.  If an
-- external writer had already invalidated the authority, or a dependency can
-- propagate beyond this row, the refresh-control trigger performs one exact
-- global rebuild.  External sqlite connections have neither TEMP table and stay
-- fail-closed.  Reads after INSERT inside the same managed transaction therefore
-- see an exact authority without an O(corpus) rebuild per imported row.
CREATE TEMP TRIGGER privacy_material_raw_ai_refresh AFTER INSERT ON main.raw_objects
BEGIN
    {_derivative_decision_sql()}
    {_derivative_replace_raw_sql()}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_raw_au_refresh AFTER UPDATE OF
    id, user_id, raw_content, source_ref, source, content_type, metadata_json
    ON main.raw_objects
BEGIN
    {
    _derivative_decision_sql(f'''NEW.id=OLD.id
        AND NEW.user_id=OLD.user_id
        AND (EXISTS (
            SELECT 1 FROM private_entity_material_derivative_work prior_raw
             WHERE prior_raw.material_kind='raw'
               AND prior_raw.object_id=OLD.id
               AND prior_raw.user_id=OLD.user_id
        ))=({_raw_derivative_live("NEW")})''')
}
    {_derivative_replace_raw_sql()}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_raw_ad_refresh AFTER DELETE ON main.raw_objects
BEGIN
    {_derivative_decision_sql()}
    DELETE FROM private_entity_material_derivative_work
     WHERE material_kind='raw' AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    DELETE FROM private_entity_material_derivative_cache
     WHERE material_kind='raw' AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;

CREATE TEMP TRIGGER privacy_material_knowledge_ai_refresh
AFTER INSERT ON main.knowledge_objects
BEGIN
    {
    _derivative_decision_sql('''
        NOT EXISTS (
            SELECT 1 FROM knowledge_objects dependent_knowledge
             WHERE dependent_knowledge.id<>NEW.id
               AND dependent_knowledge.superseded_by_id=NEW.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM inbox prior_inbox
             WHERE instr(COALESCE(prior_inbox.classification_notes,''), NEW.id)>0
                OR instr(COALESCE(prior_inbox.suggestions_json,''), NEW.id)>0
                OR instr(COALESCE(prior_inbox.suggested_tags_json,''), NEW.id)>0
        )
    ''')
}
    {_derivative_replace_knowledge_sql()}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_knowledge_au_refresh AFTER UPDATE OF
    id, user_id, raw_object_id, entity_id, content, content_type, title,
    summary, tags_json, metadata_json, knowledge_kind, superseded_by_id, deleted_at
    ON main.knowledge_objects
BEGIN
    {
    _derivative_decision_sql(f'''NEW.id=OLD.id
        AND NEW.user_id=OLD.user_id
        AND NOT EXISTS (
            SELECT 1 FROM knowledge_objects dependent_knowledge
             WHERE dependent_knowledge.id<>NEW.id
               AND dependent_knowledge.superseded_by_id=NEW.id
        )
        AND (EXISTS (
            SELECT 1 FROM private_entity_material_derivative_work prior_knowledge
             WHERE prior_knowledge.material_kind='knowledge'
               AND prior_knowledge.object_id=OLD.id
               AND prior_knowledge.user_id=OLD.user_id
        ))=({_knowledge_derivative_live("NEW")})''')
}
    {_derivative_replace_knowledge_sql()}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_knowledge_ad_refresh
AFTER DELETE ON main.knowledge_objects
BEGIN
    {
    _derivative_decision_sql('''
        NOT EXISTS (
            SELECT 1 FROM knowledge_objects dependent_knowledge
             WHERE dependent_knowledge.superseded_by_id=OLD.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM inbox prior_inbox
             WHERE instr(COALESCE(prior_inbox.classification_notes,''), OLD.id)>0
                OR instr(COALESCE(prior_inbox.suggestions_json,''), OLD.id)>0
                OR instr(COALESCE(prior_inbox.suggested_tags_json,''), OLD.id)>0
        )
    ''')
}
    DELETE FROM private_entity_material_derivative_work
     WHERE material_kind IN ('knowledge','knowledge_hidden') AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    DELETE FROM private_entity_material_derivative_cache
     WHERE material_kind IN ('knowledge','knowledge_hidden') AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;

CREATE TEMP TRIGGER privacy_material_links_ai_refresh
AFTER INSERT ON main.knowledge_entity_links
BEGIN
    {
    _derivative_decision_sql(f'''EXISTS (
        SELECT 1 FROM knowledge_objects linked_knowledge
         WHERE linked_knowledge.id=NEW.knowledge_object_id
           AND linked_knowledge.user_id=NEW.user_id
           AND (EXISTS (
               SELECT 1 FROM private_entity_material_derivative_work prior_knowledge
                WHERE prior_knowledge.material_kind='knowledge'
                  AND prior_knowledge.object_id=linked_knowledge.id
                  AND prior_knowledge.user_id=linked_knowledge.user_id
           ))=({_knowledge_derivative_live("linked_knowledge")})
    )''')
}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_links_au_refresh AFTER UPDATE OF
    user_id, knowledge_object_id, entity_id ON main.knowledge_entity_links
BEGIN
    {
    _derivative_decision_sql(f'''(
        SELECT COUNT(DISTINCT linked_knowledge.id)
          FROM knowledge_objects linked_knowledge
         WHERE linked_knowledge.id IN (OLD.knowledge_object_id, NEW.knowledge_object_id)
    )=(CASE WHEN OLD.knowledge_object_id=NEW.knowledge_object_id THEN 1 ELSE 2 END)
    AND NOT EXISTS (
        SELECT 1 FROM knowledge_objects linked_knowledge
         WHERE linked_knowledge.id IN (OLD.knowledge_object_id, NEW.knowledge_object_id)
           AND (EXISTS (
               SELECT 1 FROM private_entity_material_derivative_work prior_knowledge
                WHERE prior_knowledge.material_kind='knowledge'
                  AND prior_knowledge.object_id=linked_knowledge.id
                  AND prior_knowledge.user_id=linked_knowledge.user_id
           ))<>({_knowledge_derivative_live("linked_knowledge")})
    )''')
}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_links_ad_refresh
AFTER DELETE ON main.knowledge_entity_links
BEGIN
    {
    _derivative_decision_sql(f'''EXISTS (
        SELECT 1 FROM knowledge_objects linked_knowledge
         WHERE linked_knowledge.id=OLD.knowledge_object_id
           AND linked_knowledge.user_id=OLD.user_id
           AND (EXISTS (
               SELECT 1 FROM private_entity_material_derivative_work prior_knowledge
                WHERE prior_knowledge.material_kind='knowledge'
                  AND prior_knowledge.object_id=linked_knowledge.id
                  AND prior_knowledge.user_id=linked_knowledge.user_id
           ))=({_knowledge_derivative_live("linked_knowledge")})
    )''')
}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;

CREATE TEMP TRIGGER privacy_material_inbox_ai_refresh AFTER INSERT ON main.inbox
BEGIN
    {_derivative_decision_sql()}
    {_derivative_replace_inbox_sql()}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_inbox_au_refresh AFTER UPDATE OF
    id, user_id, raw_object_id, knowledge_object_id, suggested_entity_id,
    suggested_tags_json, suggestions_json, classification_notes ON main.inbox
BEGIN
    {_derivative_decision_sql()}
    DELETE FROM private_entity_material_derivative_work
     WHERE material_kind='inbox' AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    DELETE FROM private_entity_material_derivative_cache
     WHERE material_kind='inbox' AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    {_derivative_replace_inbox_sql()}
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
CREATE TEMP TRIGGER privacy_material_inbox_ad_refresh AFTER DELETE ON main.inbox
BEGIN
    {_derivative_decision_sql()}
    DELETE FROM private_entity_material_derivative_work
     WHERE material_kind='inbox' AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    DELETE FROM private_entity_material_derivative_cache
     WHERE material_kind='inbox' AND object_id=OLD.id
       AND EXISTS (SELECT 1 FROM private_entity_material_derivative_decision WHERE local_ok=1);
    {_DERIVATIVE_DECISION_FINISH_SQL}
END;
"""

PRIVATE_MATERIAL_CACHE_REBUILD_SQL = _MATERIAL_CACHE_REBUILD_SQL
PRIVATE_DERIVATIVE_CACHE_REBUILD_SQL = _DERIVATIVE_CACHE_REBUILD_SQL


__all__ = [
    "PRIVATE_MATERIAL_CACHE_REBUILD_SQL",
    "PRIVATE_DERIVATIVE_CACHE_REBUILD_SQL",
    "PRIVATE_MATERIAL_PERSISTENT_SCHEMA",
    "PRIVATE_MATERIAL_RUNTIME_SCHEMA",
    "_private_entity_material_seeded_cte",
    "_private_entity_material_seeded_query",
    "_not_private_bounded_json_dependency",
    "_not_private_inbox_dependency",
    "_not_disallowed_private_material_for_person",
    "_not_private_entity_material_dependency",
    "_not_private_knowledge_dependency",
    "_not_private_knowledge_entity_dependency",
    "_not_private_knowledge_structure_dependency",
    "_not_private_raw_material_dependency",
    "_not_private_notification_dependency",
    "_not_private_raw_dependency",
    "_not_private_relation_candidate_dependency",
    "_not_private_relation_dependency",
    "_not_private_resolution_candidate_dependency",
    "_not_private_reminder_entity",
]
