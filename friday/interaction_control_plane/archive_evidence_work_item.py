"""Closed follow-up surface for replaying one selected archive source.

The parser recognizes the exact replay commands and bounded content questions
which explicitly refer to the already selected source.  It does not infer a
source, search again, compare corpora or admit effects; those belong to the
ordinary router or a future durable candidate-set controller.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from friday.interaction_control_plane.selected_archive_evidence import SelectedArchiveEvidence
from friday.interaction_control_plane.work_item_contract import (
    RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON,
    RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_SCHEMA,
    WORK_ITEM_MAX_REVISION,
    WORK_ITEM_TTL_HOURS,
    WorkCompletionContract,
    WorkGoal,
    WorkItemContractError,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
    canonical_work_item_instant,
)

_MAX_SURFACE_LENGTH = 96
_MAX_SURFACE_UTF8_BYTES = 512
RECALL_SELECTED_ARCHIVE_EVIDENCE_WORK_ITEM_SCHEMA = "friday.recall-selected-archive-evidence-work-item.v1"
_WORK_ITEM_ID_RE = re.compile(r"work_[0-9a-f]{16}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID_RE = re.compile(r"msg_[0-9a-f]{16}\Z")
_USER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_FOLLOWUP_RE = re.compile(
    r"^(?:"
    r"(?P<explain>"
    r"(?:а[, ]+)?что (?:в н[её]м|там) сказано|"
    r"what does it say"
    r")|"
    r"(?P<passage>"
    r"(?:а[, ]+)?(?:покажи|приведи|процитируй)(?: мне)? (?:этот )?фрагмент|"
    r"show(?: me)? (?:that |the )?passage"
    r")"
    r")[?!.]?$",
    re.IGNORECASE,
)
_NATURAL_QUESTION_START_RE = re.compile(
    r"^(?:а[, ]+)?(?:"
    r"что|кто|чем|какой|какая|какое|какие|каков|какова|каковы|чей|чья|чьё|"
    r"когда|где|куда|откуда|почему|зачем|сколько|как|"
    r"(?:есть|был(?:а|о|и)?|указан(?:а|о|ы)?|упомянут(?:а|о|ы)?|"
    r"описан(?:а|о|ы)?|содержится?|говорится|следует|верно|правда) ли|"
    r"what|who|which|when|where|why|how|whose|"
    r"does|do|did|is|are|was|were|has|have|had|can|could|would|should"
    r")\b",
    re.IGNORECASE,
)
_STRONG_SELECTED_SOURCE_REFERENCE_RE = re.compile(
    r"(?:"
    r"\b(?:в|о|об) н[ёе]м\b|\b(?:в|о|об) ней\b|"
    r"\bиз него\b|\bиз не[ёе]\b|\bпо нему\b|\bпо ней\b|"
    r"\b(?:в|из|по|о|об) (?:этом|этого|этому|выбранном|выбранного|выбранному|"
    r"ранее выбранном|ранее выбранного) "
    r"(?:документе|документа|документу|файле|файла|файлу|"
    r"сообщении|сообщения|сообщению|источнике|источника|источнику|"
    r"фрагменте|фрагмента|фрагменту)\b|"
    r"\b(?:этот|выбранный|ранее выбранный) "
    r"(?:документ|файл|источник|фрагмент)\b|"
    r"\b(?:это|выбранное|ранее выбранное) сообщение\b|"
    r"\b(?:in|from|about) it\b|"
    r"\b(?:in|from|about) (?:this|that selected|the selected|the previously selected) "
    r"(?:document|file|message|source|passage)\b|"
    r"\b(?:this|that selected|the selected|the previously selected) "
    r"(?:document|file|message|source|passage)\b|"
    r"\b(?:does|did|can) it (?:say|state|mention|contain|describe|show|indicate|specify)\b|"
    r"\b(?:is|was) it (?:stated|mentioned|described|shown|specified)\b"
    r")",
    re.IGNORECASE,
)
_WEAK_SELECTED_SOURCE_REFERENCE_RE = re.compile(r"\b(?:там|there)\b", re.IGNORECASE)
_WEAK_SOURCE_CONTENT_RE = re.compile(
    r"\b(?:"
    r"сказан\w*|написан\w*|указан\w*|упомянут\w*|описан\w*|содерж\w*|"
    r"говор\w*|предлож\w*|срок\w*|дат\w*|вывод\w*|услов\w*|пункт\w*|"
    r"автор\w*|имя|имени|имена|именем|имён|именами|решени\w*|причин\w*|"
    r"сумм\w*|номер\w*|значени\w*|"
    r"требован\w*|обязан\w*|перенос\w*|дедлайн\w*|"
    r"said|written|stated|mentioned|described|contains?|contained|says|proposed|"
    r"deadline|date|conclusion|term|clause|author|name|decision|reason|amount|"
    r"number|value|requirement|obligation|delay"
    r")\b",
    re.IGNORECASE,
)
_REFERENCE_FIRST_QUESTION_RE = re.compile(
    r"(?:"
    r"^(?:а[, ]+)?(?:в н[ёе]м|в ней|из него|из не[ёе]|по нему|по ней|"
    r"(?:о|об) н[ёе]м|(?:о|об) ней|там|"
    r"(?:в|из|по|о|об) (?:этом|выбранном|ранее выбранном) "
    r"(?:документе|файле|сообщении|источнике|фрагменте))[, ]+"
    r"(?:что|кто|какой|какая|какое|какие|когда|где|почему|зачем|сколько|как)\b|"
    r"^(?:(?:in|from|about) it|there|"
    r"(?:in|from|about) (?:this|that selected|the selected|the previously selected) "
    r"(?:document|file|message|source|passage))[, ]+"
    r"(?:what|who|which|when|where|why|how|whose|"
    r"does|do|did|is|are|was|were|has|have|had|can|could|would|should)\b"
    r")",
    re.IGNORECASE,
)
_CONTROL_META_RE = re.compile(
    r"\b(?:ignore (?:all|any|the|these|those|prior|previous) instructions?|"
    r"system prompt|developer message|chain of thought|"
    r"hidden instructions?|(?:system|developer|internal) instructions?|"
    r"системн\w* промпт\w*|скрыт\w* инструкц\w*|"
    r"служебн\w* инструкц\w*|цепочк\w* мысл\w*|игнорир\w*|забудь|"
    r"(?:служебн|системн|внутренн)\w* метаданн\w*|"
    r"internal metadata|metadata (?:of|from) (?:the )?(?:work item|receipt|trace|runtime|system))\b",
    re.IGNORECASE,
)
_RU_MIXED_ACTION_SUFFIX_RE = re.compile(
    r"(?:,|\b(?:и|а затем|а потом|а заодно|затем|потом|заодно|после этого)\b|[-—])\s*"
    r"(?:пожалуйста\s+)?(?:(?:как|где|когда)\s+|(?:можно|нужно|надо) ли\s+)?"
    r"(?:найди|найдите|поищи|поищите|ищи|ищите|отыщи|"
    r"отыщите|разыщи|разыщите|"
    r"проверь|проверьте|посмотри|посмотрите|прочитай|прочитайте|покажи|покажите|"
    r"открой|откройте|закрой|закройте|перейди|перейдите|сравни|сравните|"
    r"сопоставь|сопоставьте|создай|создайте|добавь|добавьте|допиши|допишите|"
    r"запиши|запишите|измени|измените|удали|удалите|перемести|переместите|"
    r"переименуй|переименуйте|сохрани|сохраните|отправь|отправьте|"
    r"опубликуй|опубликуйте|экспортируй|экспортируйте|загрузи|загрузите|"
    r"скачай|скачайте|напомни|напомните|запланируй|запланируйте|выполни|"
    r"выполните|запусти|запустите|позвони|позвоните|поставь|поставьте|внеси|"
    r"внесите|замени|замените|исправь|исправьте|перепиши|перепишите|скопируй|"
    r"скопируйте|сделай|сделайте|напиши|напишите|пришли|пришлите|перешли|"
    r"перешлите|скажи|скажите|дай|дайте|ответь|ответьте|верни|верните|"
    r"выведи|выведите|обнови|обновите|установи|установите|подключи|подключите|"
    r"прикрепи|прикрепите|распечатай|распечатайте|заполни|заполните|"
    r"оформи|оформите|представь|представьте|переведи|переведите|перескажи|"
    r"перескажите|суммируй|суммируйте|расскажи|расскажите|объясни|объясните|"
    r"проанализируй|проанализируйте|перефразируй|перефразируйте|погугли|"
    r"погуглите|загугли|загуглите|синхронизируй|синхронизируйте|"
    r"найти|поискать|проверить|посмотреть|прочитать|показать|открыть|закрыть|"
    r"перейти|сравнить|сопоставить|создать|добавить|дописать|записать|изменить|"
    r"удалить|переместить|переименовать|сохранить|отправить|опубликовать|"
    r"экспортировать|загрузить|скачать|напомнить|запланировать|выполнить|"
    r"запустить|позвонить|написать|прислать|переслать|обновить|установить|"
    r"подключить|прикрепить|распечатать|заполнить|перевести|пересказать|"
    r"суммировать|ответить|синхронизировать)\b",
    re.IGNORECASE,
)
_SOURCE_OBLIGATION_PROPOSITION_RE = re.compile(
    r"^(?:а[, ]+)?(?:"
    r"кто(?:\s+[^\W_]+){0,5}\s+(?:должен|обязан|может|будет|должна|обязана)|"
    r"(?:какой|какая|какое) (?:сотрудник|пользователь|сервис|процесс|задача|роль) "
    r"(?:должен|должна|должно|обязан|обязана|может)|"
    r"who(?:\s+[^\W_]+){0,5}\s+(?:should|must|can|is supposed to|has to)|"
    r"(?:which|what) (?:person|user|service|process|job|task|role) "
    r"(?:should|must|can|is supposed to|has to)"
    r")\b",
    re.IGNORECASE,
)
_SOURCE_ACTION_PROPOSITION_RE = re.compile(
    r"(?:"
    r"\b(?:предлагается|рекомендуется|требуется|нужно|надо|следует|"
    r"указан\w*|описан\w*|перечислен\w*|"
    r"сказан\w*|написан\w*|упомянут\w*)\b"
    r".{0,56}\b(?:создать|удалить|запустить|отправить|открыть|закрыть|"
    r"сохранить|переместить|переименовать|обновить|синхронизировать|"
    r"create|delete|remove|run|send|open|close|save|move|rename|update|sync|search)\b|"
    r"\b(?:say|says|said|state|states|stated|mention|mentions|mentioned|describe|"
    r"describes|described|list|lists|listed|recommend|recommends|recommended)\b"
    r".{0,56}\b(?:create|delete|remove|run|send|open|close|save|move|rename|update|"
    r"sync|search)\b|"
    r"\b(?:create|delete|remove|run|send|open|close|save|move|rename|update|sync)"
    r"(?:\s+(?:and|or|/|,)?\s*(?:create|delete|remove|run|send|open|close|save|"
    r"move|rename|update|sync))*\s+(?:operations?|commands?|steps?|actions?)\b"
    r".{0,40}\b(?:described|listed|stated|required|recommended|описан\w*|"
    r"перечислен\w*|указан\w*)\b"
    r")",
    re.IGNORECASE,
)
_SOURCE_COMPOUND_ACTION_PROPOSITION_RE = re.compile(
    r"(?:"
    r"\b(?:предлагается|рекомендуется|требуется|нужно|надо|следует)\b.{0,56}\b"
    r"(?:создать|удалить|запустить|отправить|открыть|сохранить)\b.{0,24}"
    r"\b(?:и|или)\s+(?:создать|удалить|запустить|отправить|открыть|сохранить)\b|"
    r"\b(?:create|delete|remove|run|send|open|save)"
    r"(?:\s+(?:and|or|/|,)?\s*(?:create|delete|remove|run|send|open|save))+"
    r"\s+(?:operations?|commands?|steps?|actions?)\b.{0,40}"
    r"\b(?:described|listed|stated|required|recommended|описан\w*|перечислен\w*)\b"
    r"|\b(?:про|about)\s+(?:search|create|delete|remove|run|send|open|save)\s+"
    r"(?:and|or)\s+(?:search|create|delete|remove|run|send|open|save)\b"
    r")",
    re.IGNORECASE,
)
_RU_IMPERATIVE_MIXED_ACTION_RE = re.compile(
    r"\b(?:и|а)\s+(?:пожалуйста\s+)?(?:найди|поищи|проверь|посмотри|прочитай|"
    r"покажи|открой|закрой|сравни|создай|добавь|измени|удали|перемести|"
    r"переименуй|сохрани|отправь|опубликуй|запусти|напиши|пришли|обнови|"
    r"переведи|суммируй|синхронизируй)\b",
    re.IGNORECASE,
)
_EN_MIXED_ACTION_SUFFIX_RE = re.compile(
    r"(?:,|\band(?: then)?\b|\bthen\b|[-—])\s*(?:please\s+)?"
    r"(?:check|verify|open|visit|search|find|browse|compare|contrast|create|add|"
    r"append|write|edit|change|delete|remove|move|rename|save|send|publish|"
    r"export|upload|download|remind|schedule|execute|run|call|translate|summarize|"
    r"answer|return|render|format|tell|show|read|give|explain|analyze|paraphrase|"
    r"sync|synchronize|update|install|connect|close|print|attach|fill)\b",
    re.IGNORECASE,
)
_DIRECT_ACTION_REQUEST_RE = re.compile(
    r"(?:"
    r"^(?:how|what) to\s+(?:check|verify|open|visit|search|find|browse|compare|"
    r"create|add|append|write|edit|change|delete|remove|move|rename|save|send|"
    r"publish|export|upload|download|remind|schedule|execute|run|call|translate|"
    r"summarize|sync|synchronize|update|install|connect|close|print|attach|fill)\b|"
    r"^(?:can|could|would|will) you\b.{0,48}\b"
    r"(?:check|verify|open|visit|search|find|browse|compare|create|add|append|write|"
    r"edit|change|delete|remove|move|rename|save|send|publish|export|upload|download|"
    r"remind|schedule|execute|run|call|translate|summarize|answer|return|render|format|"
    r"tell|show|read|give|sync|synchronize|update|install|connect|close|print|attach|fill)\b|"
    r"^(?:should|can|could|would) (?:i|we)\b.{0,48}\b"
    r"(?:check|verify|open|visit|search|find|browse|compare|create|add|append|write|"
    r"edit|change|delete|remove|move|rename|save|send|publish|export|upload|download|"
    r"remind|schedule|execute|run|call|translate|summarize|answer|return|render|format|"
    r"tell|show|read|give|sync|synchronize|update|install|connect|close|print|attach|fill)\b|"
    r"^(?:what|which|how|where|when)(?: [^\W_]+){0,4} "
    r"(?:do|can|could|should|would) (?:i|we|you)\b.{0,48}\b"
    r"(?:check|verify|open|visit|search|find|browse|compare|create|add|append|write|"
    r"edit|change|delete|remove|move|rename|save|send|publish|export|upload|download|"
    r"remind|schedule|execute|run|call|translate|summarize|answer|return|render|format|"
    r"tell|show|read|give|sync|synchronize|update|install|connect|close|print|attach|fill)\b|"
    r"^(?:а[, ]+)?(?:как|что|какой|какая|какое)(?: [^\W_]+){0,4} "
    r"(?:мне|нам|тебе|вам)\b.{0,48}\b"
    r"(?:найти|поискать|проверить|открыть|перейти|сравнить|сопоставить|создать|"
    r"добавить|дописать|записать|изменить|удалить|переместить|переименовать|"
    r"сохранить|отправить|опубликовать|экспортировать|загрузить|скачать|напомнить|"
    r"запланировать|выполнить|запустить|позвонить|перевести|пересказать|"
    r"суммировать|ответить|вернуть|вывести|оформить)\b"
    r"|^(?:а[, ]+)?(?:что|как|какой|какая|какое)\s+"
    r"(?:мне\s+|нам\s+|тебе\s+|вам\s+)?(?:нужно|надо|следует|можно)\s+"
    r"(?:найти|поискать|проверить|открыть|перейти|сравнить|сопоставить|создать|"
    r"добавить|дописать|записать|изменить|удалить|переместить|переименовать|"
    r"сохранить|отправить|опубликовать|экспортировать|загрузить|скачать|напомнить|"
    r"запланировать|выполнить|запустить|позвонить|перевести|пересказать|"
    r"суммировать|ответить|вернуть|вывести|оформить)\b"
    r"|^(?:а[, ]+)?(?:как|что|какой|какая|какое)\s+"
    r"(?:найти|поискать|проверить|посмотреть|прочитать|показать|открыть|закрыть|"
    r"перейти|сравнить|сопоставить|создать|добавить|дописать|записать|изменить|"
    r"удалить|переместить|переименовать|сохранить|отправить|опубликовать|"
    r"экспортировать|загрузить|скачать|напомнить|запланировать|выполнить|"
    r"запустить|позвонить|написать|прислать|переслать|обновить|установить|"
    r"подключить|прикрепить|распечатать|заполнить|перевести|пересказать|"
    r"суммировать|ответить|синхронизировать)\b"
    r")",
    re.IGNORECASE,
)
_DIRECT_MUTATION_QUESTION_RE = re.compile(
    r"(?:"
    r"^(?:а[, ]+)?(?:что|кто|какой|какая|какое|какие|когда|где|куда|откуда|как)"
    r"(?:\s+[^\W_]+){0,7}\s+(?:найти|поискать|проверить|открыть|закрыть|"
    r"перейти|сравнить|создать|добавить|дописать|записать|изменить|удалить|"
    r"переместить|переименовать|сохранить|отправить|опубликовать|экспортировать|"
    r"загрузить|скачать|напомнить|запланировать|выполнить|запустить|позвонить|"
    r"обновить|установить|подключить|прикрепить|распечатать|заполнить|"
    r"синхронизировать)\b|"
    r"^(?:what|who|which|when|where|why|how|whose)"
    r"(?:\s+[^\W_]+){0,7}\s+(?:to\s+)?(?:check|verify|open|close|visit|search|"
    r"find|browse|compare|create|add|append|write|edit|change|delete|remove|move|"
    r"rename|save|send|publish|export|upload|download|remind|schedule|execute|run|"
    r"call|sync|synchronize|update|install|connect|print|attach|fill)\b"
    r")",
    re.IGNORECASE,
)
_UNAMBIGUOUS_MIXED_ACTION_RE = re.compile(
    r"(?:"
    r"(?:,|\b(?:а затем|а потом|а заодно|затем|потом|заодно|после этого)\b|[-—])\s*"
    r"(?:пожалуйста\s+)?(?:можешь(?: ли)?\s+|можно\s+|надо\s+|нужно\s+|"
    r"следует\s+|давай\s+)?(?:найди|найдите|поищи|поищите|проверь|проверьте|"
    r"посмотри|посмотрите|прочитай|прочитайте|покажи|покажите|открой|откройте|"
    r"сравни|сравните|создай|создайте|добавь|добавьте|измени|измените|удали|"
    r"удалите|перемести|переместите|сохрани|сохраните|отправь|отправьте|"
    r"опубликуй|опубликуйте|запусти|запустите|напиши|напишите|пришли|пришлите|"
    r"обнови|обновите|переведи|переведите|суммируй|суммируйте|"
    r"синхронизируй|синхронизируйте|найти|поискать|проверить|посмотреть|"
    r"прочитать|показать|открыть|сравнить|создать|добавить|изменить|удалить|"
    r"переместить|сохранить|отправить|опубликовать|запустить|написать|обновить|"
    r"перевести|суммировать|синхронизировать)\b|"
    r"(?:,\s*(?:and\s+)?|\band then\b|\bthen\b|[-—]\s*)\s*(?:please\s+)?"
    r"(?:(?:can|could|would|will|should) you\s+)?(?:check|verify|open|visit|search|"
    r"find|browse|compare|create|add|append|write|edit|change|delete|remove|move|"
    r"rename|save|send|publish|export|upload|download|remind|schedule|execute|run|"
    r"call|translate|summarize|sync|synchronize|update|install|connect|close|"
    r"print|attach|fill)\b"
    r")",
    re.IGNORECASE,
)
_DIRECT_COMPARISON_REQUEST_RE = re.compile(
    r"(?:"
    r"^(?:а[, ]+)?(?:как|чем)\b.{0,72}\b(?:сравн\w*|отлич\w*|сопостав\w*)\b|"
    r"^how\b.{0,72}\b(?:compare|compares|differ|differs|contrast|contrasts)\b"
    r")",
    re.IGNORECASE,
)
_OUTPUT_TRANSFORM_SUFFIX_RE = re.compile(
    r"(?:"
    r"(?:,|[-—])\s*(?:таблицей|списком|кратко|"
    r"в (?:формате )?(?:json|yaml|xml|csv))|"
    r"(?:,|[-—])\s*(?:as (?:a )?table|as (?:a )?list|briefly|"
    r"in (?:json|yaml|xml|csv)(?: format)?)|"
    r"\b(?:таблицей|списком|кратко|в виде (?:таблицы|списка)|"
    r"(?:только )?(?:краткий|подробный) ответ|ответ в (?:markdown|json|yaml|xml|csv)|"
    r"(?:(?:в )?(?:одном|двух|тр[ёе]х) (?:предложени(?:и|ях)|абзац(?:е|ах))|"
    r"одним (?:абзацем|предложением)|двумя предложениями|тезисами)|"
    r"(?:json|yaml|xml|csv)(?: only)?)\b|"
    r"\b(?:as (?:a )?table|as (?:a )?list|briefly|"
    r"(?:short|detailed) answer|answer in (?:markdown|json|yaml|xml|csv)|"
    r"in (?:one|two|three) (?:sentences?|paragraphs?)|"
    r"(?:json|yaml|xml|csv)(?: only)?)\b"
    r")\s*[?!.]?$",
    re.IGNORECASE,
)
_SOURCE_FORMAT_PROPOSITION_RE = re.compile(
    r"(?:"
    r"\b(?:представлен\w*|записан\w*|хранится|дан\w*)\s+"
    r"(?:таблицей|списком|в (?:формате )?(?:json|yaml|xml|csv))\b|"
    r"\b(?:presented|stored|written|encoded|shown)\s+"
    r"(?:as (?:a )?table|as (?:a )?list|in (?:json|yaml|xml|csv)(?: format)?)\b|"
    r"\b(?:про|о) (?:режим(?:е)?\s+)?(?:json|yaml|xml|csv)(?: only)?\b|"
    r"\babout (?:the )?(?:json|yaml|xml|csv)(?: only)? (?:mode|format)\b"
    r")",
    re.IGNORECASE,
)
_UNSUPPORTED_ANSWER_MODE_RE = re.compile(
    r"(?:"
    r"\b(?:без (?:цитат|ссылок|доказательств)|по памяти|на английском)\b|"
    r"\b(?:without (?:citations|sources|evidence)|from memory|in english)\b"
    r")\s*[?!.]?$",
    re.IGNORECASE,
)
_SOURCE_LANGUAGE_PROPOSITION_RE = re.compile(
    r"(?:"
    r"\b(?:про|о) (?:документаци\w*|текст\w*|раздел\w*|описани\w*) "
    r"на английском\b|"
    r"\babout (?:the )?(?:documentation|text|section|description) in english\b"
    r")",
    re.IGNORECASE,
)
_ADDITIONAL_SOURCE_CLAUSE_RE = re.compile(
    r"(?:,|\b(?:и|а|and|but)\b)\s+(?:[^.!?]{0,48}\s+)?(?:"
    r"(?:подтверждает ли это|подтверждает это|что об этом (?:говорит|пишет)) сайт|"
    r"(?:совпадает ли (?:он|она|оно|это) с|сверить (?:его|её|это) с) сайтом|"
    r"на сайте|в интернете|в вебе|онлайн|в архиве|в базе|в (?:другом|других) "
    r"(?:документе|документах|файле|файлах|сообщении|сообщениях)|"
    r"по другим (?:документам|файлам|сообщениям)|в (?:моей|нашей) переписке|"
    r"(?:does (?:the )?(?:site|website) confirm it)|"
    r"on (?:the )?(?:site|website|web)|on the internet|in (?:the )?archive|"
    r"in (?:another|other) (?:document|documents|file|files|message|messages)|"
    r"from (?:another|other) (?:document|documents|file|files|message|messages)|"
    r"in (?:my|our) (?:conversation|messages))\b",
    re.IGNORECASE,
)
_CONTENT_TOKEN_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
_NON_CONTENT_TOKENS = frozenset(
    {
        "а",
        "в",
        "из",
        "по",
        "об",
        "про",
        "и",
        "ли",
        "что",
        "кто",
        "как",
        "какой",
        "какая",
        "какое",
        "какие",
        "когда",
        "где",
        "почему",
        "зачем",
        "сколько",
        "есть",
        "там",
        "нем",
        "нём",
        "ней",
        "этом",
        "этого",
        "этот",
        "это",
        "выбранном",
        "выбранного",
        "выбранный",
        "выбранное",
        "ранее",
        "документ",
        "документе",
        "документа",
        "файл",
        "файле",
        "файла",
        "сообщение",
        "сообщении",
        "сообщения",
        "источник",
        "источнике",
        "фрагмент",
        "фрагменте",
        "what",
        "who",
        "which",
        "when",
        "where",
        "why",
        "how",
        "does",
        "do",
        "did",
        "is",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "can",
        "could",
        "would",
        "should",
        "the",
        "a",
        "an",
        "in",
        "from",
        "about",
        "it",
        "there",
        "this",
        "that",
        "selected",
        "previously",
        "document",
        "file",
        "message",
        "source",
        "passage",
    }
)


class ArchiveEvidenceFollowupKind(StrEnum):
    EXPLAIN = "explain"
    SHOW_PASSAGES = "show_passages"


@dataclass(frozen=True, slots=True)
class RecallSelectedArchiveEvidenceActiveFrame:
    """Closed body-free marker; exact evidence lives in the immutable sidecar."""

    @classmethod
    def parse(cls, value: object) -> RecallSelectedArchiveEvidenceActiveFrame:
        if value != RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON:
            raise WorkItemContractError("selected archive active frame is invalid")
        return cls()

    def to_payload(self) -> dict[str, str]:
        return {"schema": RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_SCHEMA}

    def to_json(self) -> str:
        return RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON


def _identifier(value: object, pattern: re.Pattern[str], *, label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} is not a valid identifier")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise WorkItemContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class RecallSelectedArchiveEvidenceWorkItem:
    """Typed joined projection of one archive Work Item and its exact sidecar."""

    id: str
    user_id: str
    conversation_id: str
    state: WorkState
    active_frame: RecallSelectedArchiveEvidenceActiveFrame
    anchor_user_message_id: str
    anchor_assistant_message_id: str
    accepted_plan_sha256: str
    accepted_outcome_sha256: str
    revision: int
    transition: WorkTransition
    created_at: str
    updated_at: str
    expires_at: str
    closed_at: str | None
    selected_evidence: SelectedArchiveEvidence

    def __post_init__(self) -> None:
        _identifier(self.id, _WORK_ITEM_ID_RE, label="work_item_id")
        _identifier(self.user_id, _USER_ID_RE, label="user_id")
        _identifier(self.conversation_id, _CONVERSATION_ID_RE, label="conversation_id")
        _identifier(self.anchor_user_message_id, _MESSAGE_ID_RE, label="anchor_user_message_id")
        _identifier(
            self.anchor_assistant_message_id,
            _MESSAGE_ID_RE,
            label="anchor_assistant_message_id",
        )
        if self.anchor_user_message_id == self.anchor_assistant_message_id:
            raise WorkItemContractError("work item anchors must differ")
        _digest(self.accepted_plan_sha256, label="accepted_plan_sha256")
        _digest(self.accepted_outcome_sha256, label="accepted_outcome_sha256")
        if type(self.state) is not WorkState or type(self.transition) is not WorkTransition:
            raise WorkItemContractError("selected archive work state is invalid")
        if type(self.active_frame) is not RecallSelectedArchiveEvidenceActiveFrame:
            raise WorkItemContractError("selected archive active frame is invalid")
        if (
            type(self.selected_evidence) is not SelectedArchiveEvidence
            or self.selected_evidence.work_item_id != self.id
        ):
            raise WorkItemContractError("selected archive evidence does not belong to the work item")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or not 1 <= self.revision <= WORK_ITEM_MAX_REVISION
        ):
            raise WorkItemContractError("revision is outside the closed limit")
        created = canonical_work_item_instant(self.created_at, label="created_at")
        updated = canonical_work_item_instant(self.updated_at, label="updated_at")
        expires = canonical_work_item_instant(self.expires_at, label="expires_at")
        if (created, updated, expires) != (self.created_at, self.updated_at, self.expires_at):
            raise WorkItemContractError("work item timestamps must already be canonical")
        if updated < created or datetime.fromisoformat(expires) > datetime.fromisoformat(updated) + timedelta(
            hours=WORK_ITEM_TTL_HOURS
        ):
            raise WorkItemContractError("selected archive work timestamps are invalid")
        if self.state in {WorkState.ACTIVE, WorkState.SUSPENDED}:
            if self.closed_at is not None or expires <= updated:
                raise WorkItemContractError("open selected archive work lifecycle is invalid")
        else:
            if self.closed_at is None:
                raise WorkItemContractError("closed selected archive work requires closed_at")
            closed = canonical_work_item_instant(self.closed_at, label="closed_at")
            if closed != self.closed_at or closed != updated:
                raise WorkItemContractError("selected archive closed_at is invalid")
            if self.state is WorkState.EXPIRED and expires > updated:
                raise WorkItemContractError("selected archive expiry is invalid")
        expected_transitions = {
            WorkState.ACTIVE: {WorkTransition.CREATED, WorkTransition.EVIDENCE_REPLAYED},
            WorkState.SUSPENDED: {WorkTransition.SUSPENDED},
            WorkState.CANCELLED: {WorkTransition.CANCELLED},
            WorkState.EXPIRED: {WorkTransition.EXPIRED},
        }
        admitted_transitions = expected_transitions.get(self.state)
        if admitted_transitions is None or self.transition not in admitted_transitions:
            raise WorkItemContractError("selected archive transition does not match state")
        if self.transition is WorkTransition.CREATED:
            if self.revision != 1 or (
                self.selected_evidence.origin_boundary_user_message_id != self.anchor_user_message_id
            ):
                raise WorkItemContractError("created selected archive work is inconsistent")
        elif self.revision < 2:
            raise WorkItemContractError("post-create selected archive work requires revision 2")

    @classmethod
    def from_storage_rows(
        cls,
        work: Mapping[str, object],
        selected_evidence: SelectedArchiveEvidence,
    ) -> RecallSelectedArchiveEvidenceWorkItem:
        expected = frozenset(
            {
                "id",
                "user_id",
                "conversation_id",
                "kind",
                "goal",
                "state",
                "playbook",
                "completion_contract",
                "active_frame_json",
                "anchor_user_message_id",
                "anchor_assistant_message_id",
                "accepted_plan_sha256",
                "accepted_outcome_sha256",
                "revision",
                "transition",
                "created_at",
                "updated_at",
                "expires_at",
                "closed_at",
            }
        )
        if not isinstance(work, Mapping) or frozenset(work) != expected:
            raise WorkItemContractError("selected archive work storage row is invalid")
        if (
            work["kind"] != WorkKind.RECALL_SELECTED_ARCHIVE_EVIDENCE.value
            or work["goal"] != WorkGoal.EXACT_SELECTED_ARCHIVE_EVIDENCE_RECALL.value
            or work["playbook"] != WorkPlaybook.RECALL_SELECTED_ARCHIVE_EVIDENCE.value
            or work["completion_contract"]
            != WorkCompletionContract.ACCEPTED_EXACT_SELECTED_ARCHIVE_EVIDENCE.value
        ):
            raise WorkItemContractError("selected archive workflow identity is invalid")
        text_names = (
            "id",
            "user_id",
            "conversation_id",
            "state",
            "transition",
            "active_frame_json",
            "anchor_user_message_id",
            "anchor_assistant_message_id",
            "accepted_plan_sha256",
            "accepted_outcome_sha256",
            "created_at",
            "updated_at",
            "expires_at",
        )
        if any(not isinstance(work[name], str) for name in text_names):
            raise WorkItemContractError("selected archive work text columns are invalid")
        revision = work["revision"]
        closed = work["closed_at"]
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or (closed is not None and not isinstance(closed, str))
        ):
            raise WorkItemContractError("selected archive work scalar columns are invalid")
        try:
            state = WorkState(str(work["state"]))
            transition = WorkTransition(str(work["transition"]))
        except (TypeError, ValueError) as exc:
            raise WorkItemContractError("selected archive work enum columns are invalid") from exc
        return cls(
            id=str(work["id"]),
            user_id=str(work["user_id"]),
            conversation_id=str(work["conversation_id"]),
            state=state,
            active_frame=RecallSelectedArchiveEvidenceActiveFrame.parse(work["active_frame_json"]),
            anchor_user_message_id=str(work["anchor_user_message_id"]),
            anchor_assistant_message_id=str(work["anchor_assistant_message_id"]),
            accepted_plan_sha256=str(work["accepted_plan_sha256"]),
            accepted_outcome_sha256=str(work["accepted_outcome_sha256"]),
            revision=revision,
            transition=transition,
            created_at=str(work["created_at"]),
            updated_at=str(work["updated_at"]),
            expires_at=str(work["expires_at"]),
            closed_at=closed,
            selected_evidence=selected_evidence,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": RECALL_SELECTED_ARCHIVE_EVIDENCE_WORK_ITEM_SCHEMA,
            "id": self.id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "kind": WorkKind.RECALL_SELECTED_ARCHIVE_EVIDENCE.value,
            "goal": WorkGoal.EXACT_SELECTED_ARCHIVE_EVIDENCE_RECALL.value,
            "state": self.state.value,
            "playbook": WorkPlaybook.RECALL_SELECTED_ARCHIVE_EVIDENCE.value,
            "completion_contract": (WorkCompletionContract.ACCEPTED_EXACT_SELECTED_ARCHIVE_EVIDENCE.value),
            "active_frame": self.active_frame.to_payload(),
            "anchor_user_message_id": self.anchor_user_message_id,
            "anchor_assistant_message_id": self.anchor_assistant_message_id,
            "accepted_plan_sha256": self.accepted_plan_sha256,
            "accepted_outcome_sha256": self.accepted_outcome_sha256,
            "revision": self.revision,
            "transition": self.transition.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "closed_at": self.closed_at,
            "selected_evidence": self.selected_evidence.to_payload(),
        }


def parse_archive_evidence_followup(message: object) -> ArchiveEvidenceFollowupKind | None:
    """Return one closed replay intent encoded by ``message``.

    Natural questions are admitted only when they name the retained source by
    an explicit deictic reference and contain useful question content. Search,
    comparison, effects and control-plane/meta requests remain ordinary turns.
    """

    if not isinstance(message, str) or not message or len(message) > _MAX_SURFACE_LENGTH:
        return None
    if any(unicodedata.category(character).startswith("C") for character in message):
        return None
    try:
        if len(message.encode("utf-8", errors="strict")) > _MAX_SURFACE_UTF8_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    surface = " ".join(unicodedata.normalize("NFKC", message).split()).casefold()
    if (
        not surface
        or len(surface) > _MAX_SURFACE_LENGTH
        or len(surface.encode("utf-8")) > _MAX_SURFACE_UTF8_BYTES
    ):
        return None
    match = _FOLLOWUP_RE.fullmatch(surface)
    if match is not None:
        return (
            ArchiveEvidenceFollowupKind.EXPLAIN
            if match.group("explain") is not None
            else ArchiveEvidenceFollowupKind.SHOW_PASSAGES
        )
    question_shaped = bool(
        _NATURAL_QUESTION_START_RE.search(surface) or _REFERENCE_FIRST_QUESTION_RE.search(surface)
    )
    source_bound = bool(
        _STRONG_SELECTED_SOURCE_REFERENCE_RE.search(surface)
        or (_WEAK_SELECTED_SOURCE_REFERENCE_RE.search(surface) and _WEAK_SOURCE_CONTENT_RE.search(surface))
    )
    obligation_proposition = _SOURCE_OBLIGATION_PROPOSITION_RE.search(surface) is not None
    source_action_proposition = bool(obligation_proposition or _SOURCE_ACTION_PROPOSITION_RE.search(surface))
    source_compound_action_proposition = bool(
        obligation_proposition or _SOURCE_COMPOUND_ACTION_PROPOSITION_RE.search(surface)
    )
    mixed_action = bool(
        _UNAMBIGUOUS_MIXED_ACTION_RE.search(surface)
        or _RU_IMPERATIVE_MIXED_ACTION_RE.search(surface)
        or (
            (_RU_MIXED_ACTION_SUFFIX_RE.search(surface) or _EN_MIXED_ACTION_SUFFIX_RE.search(surface))
            and not source_compound_action_proposition
        )
    )
    output_transform = bool(
        _OUTPUT_TRANSFORM_SUFFIX_RE.search(surface) and _SOURCE_FORMAT_PROPOSITION_RE.search(surface) is None
    )
    unsupported_answer_mode = bool(
        _UNSUPPORTED_ANSWER_MODE_RE.search(surface)
        and _SOURCE_LANGUAGE_PROPOSITION_RE.search(surface) is None
    )
    requests_capability = bool(
        _CONTROL_META_RE.search(surface)
        or _DIRECT_ACTION_REQUEST_RE.search(surface)
        or (_DIRECT_MUTATION_QUESTION_RE.search(surface) and not source_action_proposition)
        or _DIRECT_COMPARISON_REQUEST_RE.search(surface)
        or mixed_action
        or output_transform
        or unsupported_answer_mode
        or _ADDITIONAL_SOURCE_CLAUSE_RE.search(surface)
    )
    natural_body = surface[:-1] if surface[-1:] in {".", "?", "!"} else surface
    has_unsafe_punctuation = bool(
        any(character in natural_body for character in ".?!;:`'\"")
        or "…" in natural_body
        or any(unicodedata.category(character) in {"Ps", "Pe", "Pi", "Pf"} for character in natural_body)
    )
    if not question_shaped or not source_bound or requests_capability or has_unsafe_punctuation:
        return None
    content_tokens = {
        token for token in _CONTENT_TOKEN_RE.findall(surface) if token not in _NON_CONTENT_TOKENS
    }
    return ArchiveEvidenceFollowupKind.EXPLAIN if content_tokens else None


def is_archive_evidence_followup_syntax(message: object) -> bool:
    return parse_archive_evidence_followup(message) is not None


__all__ = [
    "ArchiveEvidenceFollowupKind",
    "RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_SCHEMA",
    "RECALL_SELECTED_ARCHIVE_EVIDENCE_WORK_ITEM_SCHEMA",
    "RecallSelectedArchiveEvidenceActiveFrame",
    "RecallSelectedArchiveEvidenceWorkItem",
    "is_archive_evidence_followup_syntax",
    "parse_archive_evidence_followup",
]
