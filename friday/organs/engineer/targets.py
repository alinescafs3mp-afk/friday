"""Code-owned target selection for the engineer workbench.

Only the current human speech may mint a target. Model output can refer to the
resulting :class:`PinnedTarget`, but cannot construct one from a free-form host
argument.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")
_IPV6 = re.compile(r"(?<![0-9a-f:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f:.]{1,15}(?![0-9a-f:])", re.IGNORECASE)
_IPV6_LOOSE = re.compile(
    r"(?<![0-9a-z])(?:\[[0-9a-f:.]+\](?::\d{1,5})?|[0-9a-f:.]*:[0-9a-f:.]*:[0-9a-f:.]*)(?![0-9a-z])",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_HOSTNAME = re.compile(
    r"\b(?:localhost|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})\b",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"[),.;,]+$")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_CIDR = re.compile(
    r"(?<![0-9a-f:.])(?:"
    r"(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)|"
    r"[0-9a-f:]*:[0-9a-f:.]+"
    r")/[^\s,;.!?()\[\]{}<>\"']*",
    re.IGNORECASE,
)
_ACTIVE_ASSESSMENT_VERB = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет|здравствуй(?:те)?)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|так|тогда|теперь|сейчас|уже)\s+){0,2}"
    r"(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:want|need|ask|authorize)\s+you\s+to|"
    r"пожалуйста|прошу|можешь|можете|сможешь|сможете|"
    r"(?:не\s+)?мог(?:ла|ли)?\s+бы(?:\s+(?:ты|вы))?|давай(?:те)?|"
    r"нужно|надо|хочу|разрешаю)\s*[,;:]?\s+)?"
    r"(?:(?:now|then|finally|теперь|сейчас|уже|наконец)\s+)?"
    r"(?:actively\s+|активно\s+)?(?:"
    r"scan|probe|audit|assess|inspect|enumerate|discover|check|test|"
    r"run\s+(?:an?\s+)?(?:scan|probe|audit|assessment|inspection)|"
    r"start\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"perform\s+(?:an?\s+)?(?:scan|probe|audit|assessment|inspection)|"
    r"просканиру(?:й|йте|ем)|просканировать|сканиру(?:й|йте|ем)|сканировать|"
    r"проверь(?:те)?|провер(?:ь|ьте)|проверить|"
    r"проаудиру(?:й|йте)|проаудировать|обследу(?:й|йте)|обследовать|"
    r"исследу(?:й|йте)|исследовать|"
    r"запусти(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?(?:сканирование|проверку|аудит)|"
    r"проведи(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?"
    r"(?:сканирование|проверку|аудит|обследование)"
    r")\b",
    re.IGNORECASE,
)
_ACTIVE_ASSESSMENT_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never|without)\b[^.!?\n]{0,48}\b"
    r"(?:scan|probe|audit|assess|inspect|enumerate|discover|check|test)\w*\b|"
    r"\b(?:не|никогда|без)\b[^.!?\n]{0,48}\b"
    r"(?:скан|провер|аудит|обслед|исслед|развед)\w*\b)",
    re.IGNORECASE,
)
_PASSIVE_ASSESSMENT_OBJECT = re.compile(
    r"\b(?:report|results?|log|document|file|text|article|screenshot|"
    r"configuration|settings?|status|topology|diagram|inventory|connection|connectivity|"
    r"отч[её]т\w*|результат\w*|лог\w*|документ\w*|файл\w*|текст\w*|"
    r"стать\w*|скриншот\w*|конфигурац\w*|настройк\w*|"
    r"состоян\w*|статус\w*|тополог\w*|схем\w*|инвентар\w*|"
    r"подключен\w*|соединен\w*|соединён\w*)\b",
    re.IGNORECASE,
)
_CONFIGURED_NETWORK_OBJECT = re.compile(
    r"\b(?:"
    r"my\s+(?:(?:local|home|private)\s+)?(?:subnet|network|lan)|"
    r"(?:local|home|private)\s+(?:subnet|network|lan)|"
    r"мо(?:ю|я|ей)\s+(?:(?:локальн|домашн|частн)\w*\s+)?(?:подсет\w*|сет\w*)|"
    r"(?:локальн|домашн|частн)\w*\s+(?:подсет\w*|сет\w*)"
    r")\b",
    re.IGNORECASE,
)
_CONFIGURED_NETWORK_ACTIVE_VERB = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет|здравствуй(?:те)?)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|так|тогда|теперь|сейчас|уже)\s+){0,2}"
    r"(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:want|need|ask|authorize)\s+you\s+to|"
    r"пожалуйста|прошу|можешь|можете|сможешь|сможете|"
    r"(?:не\s+)?мог(?:ла|ли)?\s+бы(?:\s+(?:ты|вы))?|давай(?:те)?|"
    r"нужно|надо|хочу|разрешаю)\s*[,;:]?\s+)?"
    r"(?:(?:now|then|finally|теперь|сейчас|уже|наконец)\s+)?"
    r"(?:actively\s+|активно\s+)?(?:"
    r"scan|probe|audit|enumerate|discover|"
    r"run\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"start\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"perform\s+(?:an?\s+)?(?:scan|probe|audit)|"
    r"просканиру(?:й|йте|ем)|просканировать|сканиру(?:й|йте|ем)|сканировать|"
    r"проаудиру(?:й|йте)|проаудировать|обследу(?:й|йте)|обследовать|"
    r"исследу(?:й|йте)|исследовать|"
    r"(?:use|run)\s+(?:nmap|a\s+(?:network|port)\s+scan)|"
    r"(?:используй|используйте|запусти|запустите)\s+(?:nmap|сканер\w*)|"
    r"запусти(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?(?:сканирование|аудит)|"
    r"проведи(?:те)?\s*(?:,\s*пожалуйста\s*,?\s*)?"
    r"(?:сканирование|аудит|обследование)"
    r")\b",
    re.IGNORECASE,
)
_NETWORK_SCAN_MECHANISM = re.compile(
    r"\b(?:nmap|scanner|network\s+scan|port\s+scan|"
    r"сканер\w*|сканирован\w*|скан\w*\s+порт\w*)\b",
    re.IGNORECASE,
)
_HOST_VULNERABILITY_CUE = re.compile(
    r"\b(?:vulnerabilit(?:y|ies)|security\s+(?:weakness(?:es)?|exposure)|"
    r"weakness(?:es)?|misconfigurations?|"
    r"уязвимост\w*|слаб(?:ое\s+место|ые\s+места|ост)\w*|"
    r"небезопасн\w*\s+(?:служб|сервис|настройк)\w*|опасн\w*\s+(?:служб|сервис)\w*)\b",
    re.IGNORECASE,
)
_HOST_VULNERABILITY_FOLLOWUP = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|так|теперь|сейчас)\s+){0,2}"
    r"(?:(?:please|pls|kindly|пожалуйста|прошу|скажи(?:те)?|покажи(?:те)?)\s*[,;:]?\s+)?"
    r"(?:"
    r"(?:what|which)\s+(?:known\s+)?(?:vulnerabilit(?:y|ies)|weakness(?:es)?)\s+"
    r"(?:does\s+)?(?:it|that\s+host|the\s+host)(?:\s+have)?|"
    r"does\s+(?:it|that\s+host|the\s+host)\s+have\s+(?:any\s+)?"
    r"(?:vulnerabilit(?:y|ies)|weakness(?:es)?)|"
    r"(?:check|scan|assess|inspect)\s+(?:it|that\s+host|the\s+host)\s+for\s+"
    r"(?:vulnerabilit(?:y|ies)|weakness(?:es)?)|"
    r"find\s+(?:vulnerabilit(?:y|ies)|weakness(?:es)?)\s+(?:on|in)\s+"
    r"(?:it|that\s+host|the\s+host)|"
    r"(?:какие|что\s+за)\s+(?:уязвимост\w*|слаб\w*\s+мест\w*)\s+"
    r"(?:есть\s+)?(?:у\s+него|у\s+этого\s+хоста|на\s+н[её]м)|"
    r"какие\s+(?:у\s+него|у\s+этого\s+хоста|на\s+н[её]м)\s+(?:есть\s+)?"
    r"(?:уязвимост\w*|слаб\w*\s+мест\w*)|"
    r"есть\s+ли\s+(?:у\s+него|у\s+этого\s+хоста|на\s+н[её]м)\s+"
    r"(?:уязвимост\w*|слаб\w*\s+мест\w*)|"
    r"(?:проверь(?:те)?|просканиру(?:й|йте)|оцени(?:те)?|исследу(?:й|йте))\s+"
    r"(?:его|этот\s+хост)\s+на\s+(?:уязвимост\w*|слаб\w*\s+мест\w*)|"
    r"найди(?:те)?\s+(?:у\s+него|на\s+н[её]м|у\s+этого\s+хоста)\s+"
    r"(?:уязвимост\w*|слаб\w*\s+мест\w*)"
    r")\b",
    re.IGNORECASE,
)
_HOST_VULNERABILITY_QUESTION = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|так|теперь|сейчас)\s+){0,2}"
    r"(?:(?:please|пожалуйста|скажи(?:те)?|покажи(?:те)?)\s*[,;:]?\s+)?(?:"
    r"какие\s+(?:есть\s+)?(?:уязвимост\w*|слаб\w*\s+мест\w*)\s+(?:у|на)|"
    r"что\s+(?:там\s+)?с\s+безопасност\w*|"
    r"есть\s+ли\s+(?:уязвимост\w*|слаб\w*\s+мест\w*)\s+(?:у|на)|"
    r"насколько\s+безопас\w*|"
    r"what\s+(?:known\s+)?(?:vulnerabilit(?:y|ies)|weakness(?:es)?)\s+(?:does|do)|"
    r"does\s+.+?\s+have\s+(?:any\s+)?(?:vulnerabilit(?:y|ies)|weakness(?:es)?)|"
    r"how\s+secure\s+(?:is|are)|is\s+.+?\s+vulnerable|"
    r"what\s+is\s+the\s+security\s+(?:state|posture)\s+of"
    r")\b",
    re.IGNORECASE,
)
_HOST_EFFECT_TARGET_TAIL = re.compile(
    r"[\s,:—–-]*(?:(?:у|на)\s+)?"
    r"(?:(?:the|this|that|этот|этого|данный|данного)\s+)?"
    r"(?:(?:host|server|target|machine|address|ip|"
    r"хост\w*|сервер\w*|узл\w*|адрес\w*)\s+)?",
    re.IGNORECASE,
)
_HOST_VULNERABILITY_QUESTION_NEGATION = re.compile(
    r"(?:\A|[.!?;]\s*)(?:мне\s+)?(?:"
    r"не\s+(?:отвечай(?:те)?|надо\s+отвечать|нужно\s+отвечать|"
    r"спрашиваю|интересн\w*|говори(?:те)?|рассказывай(?:те)?|показывай(?:те)?)|"
    r"(?:no\s+need\s+to|do\s+not|don't|dont|never)\s+"
    r"(?:answer|tell|explain|show|ask)|"
    r"i(?:'m|\s+am)?\s+not\s+(?:asking|interested)"
    r")\b",
    re.IGNORECASE,
)
_NMAP_CAPABILITY_TRUTH = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет)[!,.;:\s]+)?(?:"
    r"(?:у\s+тебя|у\s+вас)\s+(?:(?:же\s+)?(?:должен|должна|должно)\s+быть\s+)?"
    r"(?:есть\s+)?(?:доступ|доступа|возможност\w*)\s+(?:к\s+)?nmap|"
    r"(?:есть|имеется)\s+ли\s+(?:у\s+тебя|у\s+вас)\s+(?:доступ|возможност\w*)\s+(?:к\s+)?nmap|"
    r"(?:ты|вы)\s+(?:же\s+)?(?:умеешь|умеете|можешь|можете|способна|способен)\s+"
    r"(?:использовать|запускать|запустить)\s+nmap|"
    r"можешь\s+ли\s+ты\s+(?:использовать|запускать|запустить)\s+nmap|"
    r"nmap\s+(?:тебе|вам)?\s*(?:доступен|доступна|установлен|работает)|"
    r"(?:you\s+(?:should|must)\s+have|do\s+you\s+have|have\s+you\s+got)\s+"
    r"(?:access\s+to\s+)?nmap|"
    r"(?:can|could|would)\s+you\s+(?:use|run|launch)\s+nmap|"
    r"are\s+you\s+able\s+to\s+(?:use|run|launch)\s+nmap|"
    r"you\s+(?:can|are\s+able\s+to)\s+(?:use|run)\s+nmap|"
    r"is\s+nmap\s+(?:available|installed|usable)|"
    r"nmap\s+(?:же\s+)?установлен[^.!?\n]{0,80}почему\s+не\s+использу\w*|"
    r"nmap\s+is\s+installed[^.!?\n]{0,80}why\s+(?:don't|do\s+not)\s+you\s+use\s+it"
    r")\b",
    re.IGNORECASE,
)
_NMAP_CAPABILITY_NEGATION = re.compile(
    r"\b(?:нет|не|без|never|no|not|without|don't|dont|cannot|can't)\b"
    r"[^.!?\n]{0,64}\bnmap\b|\bnmap\b[^.!?\n]{0,64}"
    r"\b(?:недоступ\w*|не\s+установ\w*|not\s+available|not\s+installed|cannot|can't)\b",
    re.IGNORECASE,
)
_NETWORK_REPORT_EXPORT_VERB = (
    r"(?:пришл(?:и|ите)|отправ(?:ь|ьте)|прилож(?:и|ите)|прикреп(?:и|ите)|"
    r"сохран(?:и|ите)|выгруз(?:и|ите)|экспортируй(?:те)?|сформируй(?:те)?|"
    r"подготов(?:ь|ьте)|сделай(?:те)?|создай(?:те)?|дай(?:те)?|"
    r"send|attach|save|export|generate|create|prepare|provide|return|give)"
)
_NETWORK_REPORT_RESULT_OBJECT = r"(?:отч[её]т\w*|результат\w*|reports?|results?)"
_NETWORK_REPORT_RESULT = re.compile(rf"\b{_NETWORK_REPORT_RESULT_OBJECT}\b", re.IGNORECASE)
_NETWORK_REPORT_FILE_CARRIER = r"(?:json|markdown|маркдаун\w*|md|файл\w*|вложени\w*|files?|attachments?)"
_NETWORK_REPORT_EXPORT = re.compile(
    rf"\b{_NETWORK_REPORT_EXPORT_VERB}\b[^.!?;\n]{{0,120}}(?:"
    rf"\b{_NETWORK_REPORT_RESULT_OBJECT}\b[^.!?;\n]{{0,80}}\b{_NETWORK_REPORT_FILE_CARRIER}\b|"
    rf"\b{_NETWORK_REPORT_FILE_CARRIER}\b[^.!?;\n]{{0,80}}\b{_NETWORK_REPORT_RESULT_OBJECT}\b"
    rf")",
    re.IGNORECASE,
)
_NETWORK_REPORT_CARRIER_EXPORT = re.compile(
    rf"\b{_NETWORK_REPORT_EXPORT_VERB}\b[^.!?;\n]{{0,120}}"
    rf"\b{_NETWORK_REPORT_FILE_CARRIER}\b",
    re.IGNORECASE,
)
_NETWORK_REPORT_JSON = re.compile(r"(?<![\w.])(?:json|\.json)(?!\w)", re.IGNORECASE)
_NETWORK_REPORT_MARKDOWN = re.compile(
    r"(?<![\w.])(?:markdown|маркдаун\w*|md|\.md)(?!\w)",
    re.IGNORECASE,
)
_NETWORK_REPORT_SCAN_CONTEXT = re.compile(
    r"\b(?:nmap|scans?|scanning|скан\w*|проскан\w*)\b",
    re.IGNORECASE,
)
_NETWORK_REPORT_AUDIT_CONTEXT = re.compile(
    r"\b(?:audits?|auditing|probes?|probing|enumerat\w*|discover\w*|"
    r"аудит\w*|обслед\w*|исслед\w*)\b",
    re.IGNORECASE,
)
_NETWORK_REPORT_TARGET_CONTEXT = re.compile(
    r"\b(?:hosts?|networks?|subnets?|lan|cidr|ip|хост\w*|сет\w*|подсет\w*)\b",
    re.IGNORECASE,
)
_NETWORK_REPORT_LITERAL_TARGET_CONTEXT = re.compile(
    r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.])"
)
_NETWORK_REPORT_EXPORT_NEGATION = re.compile(
    rf"(?:\b(?:не|без)\b|\b(?:do\s+not|don't|dont|without|no)\b)"
    rf"[^.!?;\n]{{0,80}}(?:\b{_NETWORK_REPORT_EXPORT_VERB}\b|"
    rf"\b{_NETWORK_REPORT_RESULT_OBJECT}\b|\b{_NETWORK_REPORT_FILE_CARRIER}\b)",
    re.IGNORECASE,
)
_NETWORK_REPORT_EXPORT_META = re.compile(
    rf"\b(?:как|почему|зачем|можно\s+ли|умеешь\s+ли|how|why|can\s+you)\b"
    rf"[^.!?;\n]{{0,100}}\b{_NETWORK_REPORT_EXPORT_VERB}\b",
    re.IGNORECASE,
)
_REQUEST_CODE_TEXT = re.compile(
    r"(?P<request_ticks>`+)[\s\S]*?(?:(?P=request_ticks)|\Z)|"
    r"(?P<request_tildes>~{3,})[\s\S]*?(?:(?P=request_tildes)|\Z)"
)
_QUOTED_REQUEST_TEXT = re.compile(
    r"«[\s\S]*?(?:»|\Z)|“[\s\S]*?(?:”|\Z)|„[\s\S]*?(?:“|\Z)|"
    r"‘[\s\S]*?(?:’|\Z)|‚[\s\S]*?(?:‘|\Z)|‹[\s\S]*?(?:›|\Z)|"
    r"「[\s\S]*?(?:」|\Z)|『[\s\S]*?(?:』|\Z)|"
    r"(?<!\w)(?P<request_quote>[\"'])[\s\S]*?(?:(?P=request_quote)|\Z)",
)
_REQUEST_BLOCKQUOTE_START = re.compile(r"^[ \t]{0,3}>")
_REQUEST_INDENTED_CODE = re.compile(r"(?: {4}| {0,3}\t)")
_REQUEST_UNIT_BOUNDARY = re.compile(r"(?:[!?;]+(?:\s+|$)|\.(?:\s+|$)|\n+)")
_REQUEST_SOFT_BOUNDARY = re.compile(r"(?:,\s+|\s+[—–-]\s+)")
_REPORTED_REQUEST_CUE = re.compile(
    r"\b(?:сказа\w*|говор\w*|написа\w*|указа\w*|спрос\w*|попрос\w*|просил\w*|велел\w*|"
    r"предлож\w*|посовет\w*|требу\w*|цитир\w*|цитат\w*|повтор\w*|"
    r"перевед\w*|означа\w*|said|says?|wrote|told|asked|ordered|"
    r"suggested|recommended|required|quote\w*|repeat\w*|translat\w*|means?)\b",
    re.IGNORECASE,
)
_META_REQUEST_CUE = re.compile(
    r"\b(?:пример\w*|фраз\w*|цитат\w*|инструкц\w*|шаблон\w*|"
    r"команд\w*|examples?|phrases?|quot(?:e|es|ed)|instructions?|templates?|commands?)\b",
    re.IGNORECASE,
)
_CONDITIONAL_REQUEST_CUE = re.compile(r"\b(?:если|if|unless)\b", re.IGNORECASE)
_POLITE_NEGATIVE_MODAL = re.compile(
    r"\bне\s+мог(?:ла|ли)?\s+бы(?:\s+(?:ты|вы))?\b",
    re.IGNORECASE,
)
_TRAILING_REQUEST_CANCEL = re.compile(
    r"(?:\b(?:но|однако|хотя|but|however)\b[^.!?\n]{0,40})?"
    r"(?:\bне\s+(?:надо|нужно|стоит|делай(?:те)?|выполняй(?:те)?|"
    r"запускай(?:те)?|сканируй(?:те)?)\b|"
    r"\b(?:отмена|отмени(?:те)?|передумал(?:а)?)\b|"
    r"\b(?:do\s+not|don't|dont|never)\s+(?:do|scan|run|execute)\b|"
    r"\b(?:cancel(?:\s+(?:it|that))?|never\s+mind)\b)",
    re.IGNORECASE,
)
_LEADING_REQUEST_CANCEL = re.compile(
    r"\b(?:"
    r"отмена|отбой|стоп|я\s+передумал(?:а)?|не\s+(?:надо|нужно)|"
    r"cancel(?:\s+(?:it|that))?|never\s+mind|stop|no\s+need|"
    r"do\s+not|don't|dont"
    r")\b",
    re.IGNORECASE,
)
_TRAILING_REQUEST_ATTRIBUTION = re.compile(
    r"(?:[,;]\s*|\s+[—–-]\s+|(?:^|[.!?])\s*)"
    r"(?:это\s+(?:пример|цитата|фраза|команда)|"
    r"так\s+(?:сказал|написал)\w*|"
    r"this\s+is\s+(?:an?\s+)?(?:example|quote|phrase|command)|"
    r"(?:as\s+)?(?:said|written)\s+by)\b",
    re.IGNORECASE,
)
_EXPLICIT_REQUEST_CONTEXT_RESET = re.compile(
    r"\A\s*(?:(?:а|и|and|so)\s+)?(?:теперь|сейчас|наконец|now|then|finally)\b",
    re.IGNORECASE,
)
_ARTIFACT_PATCH_REQUEST = re.compile(
    r"\A\s*(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:want|need|ask|authorize)\s+you\s+to|"
    r"пожалуйста|прошу|можешь|можете|нужно|надо|хочу|разрешаю)\s*[,;:]?\s+)?"
    r"(?:patch|fix|modify|edit|rewrite|change|repair|"
    r"apply\s+(?:the\s+)?(?:patch|changes?)\s+to|"
    r"(?:produce|create|generate)\s+[^.!?\n]{0,64}\b(?:derived|patched|modified)|"
    r"исправь|исправьте|почини|почините|измени|измените|"
    r"отредактируй|отредактируйте|перепиши|перепишите|"
    r"пропатчь|пропатчьте|модифицируй|модифицируйте|"
    r"примени(?:те)?\s+(?:патч|изменения)\s+(?:к|для)|"
    r"(?:создай|создайте|сгенерируй|сгенерируйте)\s+[^.!?\n]{0,64}\b"
    r"(?:исправленн|измен[её]нн|пропатченн)\w*)\b"
    r"[^.!?\n]{0,120}\b(?:artifact|file|binary|executable|archive|attachment|"
    r"it|this|артефакт\w*|файл\w*|бинарн\w*|исполняем\w*|архив\w*|"
    r"вложени\w*|его|е[её]|это)\b",
    re.IGNORECASE,
)
_ARTIFACT_PATCH_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never|without)\b[^.!?\n,;]{0,48}\b"
    r"(?:patch|fix|modify|edit|rewrite|change|repair)\b|"
    r"\b(?:не|никогда|без)\b[^.!?\n,;]{0,48}\b"
    r"(?:исправ|почин|измен|редактир|перепис|патч|модифицир)\w*\b)",
    re.IGNORECASE,
)
_ARTIFACT_DECOMPILE_FILENAME = (
    r"(?<![\w./\\-])[A-Za-z0-9_@+(),\[\]-]+"
    r"(?:\.[A-Za-z0-9_@+(),\[\]-]+)*\.[A-Za-z0-9]{1,16}(?![\w./\\-])"
)
_ARTIFACT_DECOMPILE_REQUEST = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет|здравствуй(?:те)?)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|тогда|теперь|сейчас)\s+){0,2}"
    r"(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"пожалуйста|прошу|можешь|можете|сможешь|сможете)\s*[,;:]?\s+)?"
    r"(?:"
    r"(?:decompile|reverse[ -]engineer|"
    r"декомпилируй(?:те)?|декомпилировать|"
    r"(?:проведи(?:те)?\s+)?реверс(?:-|\s+)инжиниринг)"
    r"[^.!?\n]{0,64}?\b(?:(?:this|that)\s+(?:artifact|file|binary|executable)|"
    r"(?:этот|эту)\s+(?:артефакт\w*|файл\w*|бинарник\w*|исполняем\w*)|"
    r"it|this|that|artifact|file|binary|executable|"
    r"его|е[её]|это|этот|эту|артефакт\w*|файл\w*|бинарник\w*|"
    r"исполняем\w*(?:\s+файл\w*)?|"
    rf"{_ARTIFACT_DECOMPILE_FILENAME})\b|"
    r"(?:analy[sz]e|inspect|проанализируй(?:те)?|анализировать|разбери(?:те)?)"
    r"[^.!?\n]{0,64}?\b(?:binary|executable|binary\s+artifact|"
    r"бинарник\w*|бинарн\w*(?:\s+(?:файл|артефакт)\w*)?|"
    r"исполняем\w*(?:\s+файл\w*)?)\b"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_DECOMPILE_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never|without)\b[^.!?\n]{0,48}\b"
    r"(?:decompil\w*|reverse[ -]engineer\w*|analy[sz]e\w*|inspect\w*)\b|"
    r"\b(?:не|никогда|без)\b[^.!?\n]{0,48}\b"
    r"(?:декомпил\w*|реверс\w*|анализир\w*|проанализир\w*|разбер\w*)\b)",
    re.IGNORECASE,
)
_ARTIFACT_DECOMPILE_TARGET_EXCLUSION = re.compile(
    r"(?:"
    r"\bне\s+(?:этот|эту|данн\w*|текущ\w*)\s+"
    r"(?:файл\w*|бинарник\w*|артефакт\w*|исполняем\w*)\b|"
    r"\b(?:любой|другой)\s+(?:файл\w*|бинарник\w*|артефакт\w*)\b"
    r"[^.!?\n]{0,48}\b(?:кроме|исключая)\s+(?:этого|этот|его)\b|"
    r"\bnot\s+(?:this|that|the\s+current)\s+(?:file|binary|artifact|executable)\b|"
    r"\b(?:any|another|other)\s+(?:file|binary|artifact|executable)\b"
    r"[^.!?\n]{0,48}\b(?:except|excluding)\s+(?:this|that|it|the\s+current\s+one)\b"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_DECOMPILE_CAPABILITY = re.compile(
    r"(?:\A|[.!?]\s*)(?:"
    r"(?:ты\s+)?(?:умеешь|способна|можешь\s+ли|можете\s+ли)\b[^.!?\n]{0,64}\b"
    r"(?:декомпил\w*|реверс\w*|анализир\w*)|"
    r"(?:есть|имеется)\s+ли\b[^.!?\n]{0,64}\b(?:инструмент|возможност)\w*"
    r"[^.!?\n]{0,48}\b(?:декомпил\w*|реверс\w*|анализ\w*)|"
    r"(?:can\s+you|are\s+you\s+able\s+to|do\s+you\s+know\s+how\s+to)\s+"
    r"(?:decompil\w*|reverse[ -]engineer|analy[sz]e)\s+"
    r"(?:files?|binaries|executables|artifacts)\s*\?\s*$"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_DECOMPILE_META = re.compile(
    r"\b(?:что\s+значит|как\s+перевести|объясни\w*\s+(?:фразу|термин)|"
    r"what\s+does\b[^.!?\n]{0,48}\bmean|how\s+do\s+you\s+say)\b",
    re.IGNORECASE,
)
_ARTIFACT_DECOMPILE_REPORTED = re.compile(
    r"\b(?:сообщ\w*|ответ\w*|пересказ\w*|states?|reports?|reported|replied)\b",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_FILENAME = r"(?<![\w./\\-])[A-Za-z_][A-Za-z0-9_]{0,119}\.java(?![\w./\\-])"
_ARTIFACT_COMPILE_PROFILE = r"(?<![\w-])java21_single_source_library_jar_v1(?![\w-])"
_ARTIFACT_COMPILE_EN_SOURCE = (
    r"(?:"
    r"(?:(?:this|that|the|an?|attached|current)\s+){0,2}"
    r"(?:java(?:[- ]21)?\s+(?:source|source\s+file|file)|source\s+file\s+in\s+java)|"
    r"(?:(?:this|that|the\s+(?:attached|current)|attached|current)\s+)"
    r"(?:source(?:\s+file)?|file)|"
    r"(?:it|this|that)(?=\s*(?:[.!?…]*\Z|,?\s+(?:and|then)\b))"
    r")"
)
_ARTIFACT_COMPILE_RU_SOURCE = (
    r"(?:"
    r"(?:(?:этот|эту|данн\w*|приложенн\w*|текущ\w*)\s+){0,2}"
    r"(?:java(?:[- ]21)?[- ]файл\w*|java(?:[- ]21)?\s+исходник\w*|"
    r"исходник\w*\s+java(?:[- ]21)?)|"
    r"(?:этот|эту|данн\w*|приложенн\w*|текущ\w*)\s+"
    r"(?:файл\w*|исходник\w*)|"
    r"это(?=\s*(?:[.!?…]*\Z|,?\s+(?:и|а\s+затем)\b))"
    r")"
)
_ARTIFACT_COMPILE_EN_NAMED = (
    rf"(?:(?:(?:this|that|the|an?|attached|current)\s+){{0,2}}"
    rf"(?:file\s+)?{_ARTIFACT_COMPILE_FILENAME}|"
    rf"(?:(?:(?:using|with)\s+)?(?:the\s+)?profile\s+)?{_ARTIFACT_COMPILE_PROFILE})"
)
_ARTIFACT_COMPILE_RU_NAMED = (
    rf"(?:(?:(?:этот|эту|данн\w*|приложенн\w*|текущ\w*)\s+){{0,2}}"
    rf"(?:файл\w*\s+)?{_ARTIFACT_COMPILE_FILENAME}|"
    rf"(?:(?:(?:по|с)\s+)?профил\w*\s+)?{_ARTIFACT_COMPILE_PROFILE})"
)
_ARTIFACT_COMPILE_EN_OUTPUT = (
    r"(?:(?:the|an?)\s+)?(?:(?:compiled|resulting)\s+)?"
    r"(?:jar|binary|build\s+artifact)\b"
    r"(?:\s*/\s*(?:jar|binary|build\s+artifact)\b)?"
)
_ARTIFACT_COMPILE_RU_OUTPUT = (
    r"(?:(?:готов\w*|собранн\w*|скомпилированн\w*)\s+)?"
    r"(?:jar[- ]файл\w*|jar\b|бинарник\w*|бинарн\w*\s+артефакт\w*)"
    r"(?:\s*/\s*(?:jar[- ]файл\w*|jar\b|бинарник\w*|бинарн\w*\s+артефакт\w*))?"
)
_ARTIFACT_COMPILE_EN_DELIVERY = rf"(?:send|attach|upload|deliver)\s+(?:me\s+)?{_ARTIFACT_COMPILE_EN_OUTPUT}"
_ARTIFACT_COMPILE_RU_DELIVERY = (
    rf"(?:пришли(?:те)?|отправь(?:те)?|приложи(?:те)?|выгрузи(?:те)?)\s+"
    rf"(?:мне\s+)?{_ARTIFACT_COMPILE_RU_OUTPUT}"
)
_ARTIFACT_COMPILE_DELIVERY_SUFFIX = (
    rf"(?:\s*,?\s+(?:and|then)\s+{_ARTIFACT_COMPILE_EN_DELIVERY}|"
    rf"\s*,?\s+(?:и|а\s+затем)\s+{_ARTIFACT_COMPILE_RU_DELIVERY})"
)
_ARTIFACT_COMPILE_REQUEST = re.compile(
    r"\A\s*(?:(?:hi|hello|hey|привет|здравствуй(?:те)?)[!,.;:\s]+)?"
    r"(?:(?:and|so|then|now|а|и|ну|тогда|теперь|сейчас)\s+){0,2}"
    r"(?:(?:please|pls|kindly|can\s+you|could\s+you|would\s+you|"
    r"i\s+(?:want|need|ask|authorize)\s+you\s+to|"
    r"пожалуйста|прошу|можешь|можете|сможешь|сможете|"
    r"(?:не\s+)?мог(?:ла|ли)?\s+бы(?:\s+(?:ты|вы))?|"
    r"нужно|надо|хочу|разрешаю)\s*[,;:]?\s+)?"
    r"(?:"
    r"(?:compile|build)\s+(?:"
    rf"{_ARTIFACT_COMPILE_EN_SOURCE}|{_ARTIFACT_COMPILE_EN_NAMED}"
    r")"
    r"(?:\s+(?:into|as)\s+(?:an?\s+)?jar)?|"
    rf"(?:compile|build)\s+(?:and|then)\s+{_ARTIFACT_COMPILE_EN_DELIVERY}|"
    r"(?:скомпилируй(?:те)?|компилируй(?:те)?|собери(?:те)?|"
    r"скомпилировать|компилировать|собрать)\s+(?:"
    rf"{_ARTIFACT_COMPILE_RU_SOURCE}|{_ARTIFACT_COMPILE_RU_NAMED}"
    r")"
    r"(?:\s+в\s+jar)?|"
    rf"(?:скомпилируй(?:те)?|компилируй(?:те)?|собери(?:те)?)\s+"
    rf"(?:и|а\s+затем)\s+{_ARTIFACT_COMPILE_RU_DELIVERY}|"
    r"(?:сборка|компиляция)\s+(?:"
    rf"{_ARTIFACT_COMPILE_RU_SOURCE}|{_ARTIFACT_COMPILE_RU_NAMED}"
    r")(?:\s+в\s+jar)?|"
    r"(?:выполни(?:те)?|сделай(?:те)?|запусти(?:те)?|проведи(?:те)?)\s+"
    r"(?:сборку|компиляцию)\s+(?:"
    rf"{_ARTIFACT_COMPILE_RU_SOURCE}|{_ARTIFACT_COMPILE_RU_NAMED}"
    r")(?:\s+в\s+jar)?"
    r")"
    rf"(?:{_ARTIFACT_COMPILE_DELIVERY_SUFFIX})?",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_NEGATION = re.compile(
    r"(?:\b(?:do\s+not|don't|dont|never|without)\b[^.!?\n]{0,48}\b"
    r"(?:compil|build)\w*\b|"
    r"\b(?:не|никогда|без)\b[^.!?\n]{0,48}\b"
    r"(?:компилир|скомпилир|собир|собер|сборк|компиляц)\w*\b)",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_TARGET_EXCLUSION = re.compile(
    rf"(?:"
    rf"\b(?:but\s+)?not\s+(?:(?:this|that|the\s+(?:attached|current))\s+"
    rf"(?:java\s+)?(?:file|source)|this|that|it|the\s+(?:attached|current)\s+one|"
    rf"{_ARTIFACT_COMPILE_FILENAME})\b|"
    rf"\b(?:except|exclude|excluding)\s+"
    rf"(?:this|that|it|the\s+(?:attached|current)\s+one|"
    rf"{_ARTIFACT_COMPILE_FILENAME})\b|"
    rf"\b(?:но\s+)?(?:только\s+)?не\s+(?:(?:этот|эту|данн\w*|текущ\w*|"
    rf"приложенн\w*)\s+(?:java[- ]?)?(?:файл\w*|исходник\w*)|"
    rf"это|этого|этот|эту|его|е[её]|"
    rf"{_ARTIFACT_COMPILE_FILENAME})\b|"
    rf"\b(?:кроме|исключая|исключи(?:те)?)\s+(?:этого|этот|эту|его|е[её]|"
    rf"{_ARTIFACT_COMPILE_FILENAME})\b"
    rf")",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_MIXED_TARGETS = re.compile(
    rf"(?:"
    rf"\b(?:it|this|that|(?:this|that|the\s+(?:attached|current)|attached|current)\s+"
    rf"(?:file|source))\b\s*(?:,|and|or|plus)\s*{_ARTIFACT_COMPILE_FILENAME}|"
    rf"{_ARTIFACT_COMPILE_FILENAME}\s*(?:,|and|or|plus)\s*\b"
    rf"(?:it|this|that|(?:this|that|the\s+(?:attached|current)|attached|current)\s+"
    rf"(?:file|source))\b|"
    rf"\b(?:это|(?:этот|эту|данн\w*|приложенн\w*|текущ\w*)\s+"
    rf"(?:файл\w*|исходник\w*))\b\s*(?:,|и|или|плюс)\s*"
    rf"{_ARTIFACT_COMPILE_FILENAME}|"
    rf"{_ARTIFACT_COMPILE_FILENAME}\s*(?:,|и|или|плюс)\s*\b"
    rf"(?:это|(?:этот|эту|данн\w*|приложенн\w*|текущ\w*)\s+"
    rf"(?:файл\w*|исходник\w*))\b"
    rf")",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_DEFERRED_PREFIX = re.compile(
    r"\b(?:after|before|once|upon|when(?:ever)?|provided|assuming|pending|"
    r"tomorrow|later|next\s+(?:week|month)|at\s+\d{1,2}(?::\d{2})?|"
    r"in\s+\d+\s+(?:seconds?|minutes?|hours?|days?)|with\s+(?:my\s+)?approval|"
    r"после|до|когда|как\s+только|завтра|позже|потом|"
    r"на\s+следующ\w*\s+(?:недел\w*|месяц\w*)|"
    r"в\s+\d{1,2}(?::\d{2})?|через\s+\d+\s+(?:секунд\w*|минут\w*|час\w*|дн\w*)|"
    r"при\s+условии|при\s+подтверждении|"
    r"после\s+(?:подтверждения|одобрения|разрешения))\b",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_TRAILING_CONDITION = re.compile(
    r"\A\s*(?:[,;:()]|[—–-])?\s*(?:(?:but|and|но|и)\s+)?"
    r"(?:(?:only|just|только)\s+)?(?:"
    r"when(?:ever)?|after|before|once|upon|provided(?:\s+that)?|"
    r"assuming(?:\s+that)?|subject\s+to|pending|until|as\s+soon\s+as|"
    r"tomorrow|next\s+(?:week|month)|at\s+\d{1,2}(?::\d{2})?|"
    r"in\s+\d+\s+(?:seconds?|minutes?|hours?|days?)|with\s+(?:my\s+)?approval|"
    r"когда|после|до|как\s+только|завтра|"
    r"на\s+следующ\w*\s+(?:недел\w*|месяц\w*)|"
    r"в\s+\d{1,2}(?::\d{2})?|через\s+\d+\s+(?:секунд\w*|минут\w*|час\w*|дн\w*)|"
    r"при\s+условии|при\s+подтверждении|"
    r"с\s+(?:моего\s+)?(?:подтверждения|одобрения|разрешения)"
    r")\b",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_TRAILING_DENIAL = re.compile(
    r"\A\s*(?:[,;:()]|[—–-])?\s*(?:(?:but|however|actually|но|однако)\s+)?(?:"
    r"(?:is\s+not|isn't)\s+(?:needed|required|requested)|"
    r"(?:this|that|it)\s+(?:is\s+not|isn't|was\s+not|wasn't)\s+"
    r"(?:a\s+)?(?:request|command)|"
    r"(?:not\s+now|not\s+this\s+time|no\s+need|later(?:\s+instead)?|hold\s+off|"
    r"wait|skip\s+(?:it|that)|(?:i\s+)?changed\s+my\s+mind|scratch\s+that|"
    r"forget\s+it|"
    r"ignore\s+(?:it|that))|"
    r"не\s+(?:нужн|требу|заказан|запрошен)\w*|"
    r"это\s+не\s+(?:просьба|команда|запрос)|"
    r"(?:не\s+сейчас|позже|потом|подожди(?:те)?|отложи(?:те)?|"
    r"пропусти(?:те)?|игнорируй(?:те)?)"
    r")\b",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_DELIVERY_NEGATION = re.compile(
    r"(?:"
    r"\b(?:do\s+not|don't|dont|never|without)\b[^.!?\n]{0,48}\b"
    r"(?:send|attach|upload|deliver)\w*\b[^.!?\n]{0,48}\b"
    r"(?:jar|binary|build\s+artifact)\b|"
    r"\b(?:не|никогда|без)\b[^.!?\n]{0,48}\b"
    r"(?:присыл|пришл|отправ|прикладыв|прилож|выгруж|выгруз)\w*\b"
    r"[^.!?\n]{0,48}\b(?:jar|бинарник|бинарн\w*\s+артефакт)\w*\b"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_TRAILING_REPORT = re.compile(
    r"\A\s*(?:[,;:()]|[—–-])?\s*(?:(?:the\s+)?build\s+)?(?:"
    r"(?:(?:is|was|has|has\s+been)\s+)?(?:completed|finished|failed|successful)|"
    r"(?:сборка\s+)?(?:уже\s+)?(?:(?:была|был|было)\s+)?"
    r"(?:заверш|выполн|готов|успеш|провал|не\s+удал)\w*"
    r")\b",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_REPORTED_PREFIX = re.compile(
    r"\b(?:requested|requesting|ordered|attributed|according\s+to|"
    r"запросил\w*|поручил\w*|приписал\w*|по\s+словам)\b",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_TRAILING_ATTRIBUTION = re.compile(
    r"(?:\A|[,;.!?()]\s*|\s+[—–-]\s+)(?:"
    r"according\s+to|per\s+(?:[A-Za-z0-9_@.-]+\s+){0,3}"
    r"[A-Za-z0-9_@.-]+['’]s\s+(?:request|command)|(?:a\s+)?quote\s+from|"
    r"(?:said|wrote|asked|requested|ordered)\b|"
    r"(?:[A-Za-z0-9_@.'’-]+\s+){1,4}(?:said|wrote|asked|requested|ordered)\b|"
    r"(?:[A-Za-z0-9_@.-]+\s+){0,3}[A-Za-z0-9_@.-]+['’]s\s+"
    r"(?:request|command|quote)\b|"
    r"(?:this|that)\s+(?:is|was)\b[^.!?\n]{0,40}\b(?:request|command|quote)\b|"
    r"по\s+(?:словам|просьбе|команде|поручению)|"
    r"(?:цитата|просьба|команда)\s+(?:от|из)|"
    r"(?:сказал|сказала|сказали|написал|написала|написали|попросил|попросила|"
    r"попросили|велел|велела|велели)\b|"
    r"(?:[А-Яа-яЁёA-Za-z0-9_@.'’-]+\s+){1,4}"
    r"(?:сказал|сказала|сказали|написал|написала|написали|попросил|попросила|"
    r"попросили|велел|велела|велели)\b|"
    r"(?:просьба|команда|цитата)\s+[А-Яа-яЁёA-Za-z0-9_@.'’-]+\b|"
    r"это\s+(?:была|был|есть)?\s*(?:просьба|команда|цитата)\b"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_CAPABILITY = re.compile(
    r"(?:\A|[.!?]\s*)(?:"
    r"(?:ты\s+)?(?:умеешь|способна|можешь\s+ли|можете\s+ли)\b[^.!?\n]{0,64}\b"
    r"(?:компилир\w*|сборк\w*|собир\w*)[^.!?\n]{0,80}\?\s*$|"
    r"(?:есть|имеется)\s+ли\b[^.!?\n]{0,64}\b(?:возможност|поддержк)\w*"
    r"[^.!?\n]{0,80}\b(?:компиляц|сборк|компилир)\w*[^.!?\n]*\?\s*$|"
    r"(?:are\s+you\s+able\s+to|do\s+you\s+know\s+how\s+to|do\s+you\s+support)\s+"
    r"(?:compil\w*|build\w*)[^.!?\n]{0,100}\?\s*$"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_REMAINDER_DENIAL = re.compile(
    r"(?:"
    r"\b(?:do\s+not|don't|dont|never\s+mind|forget\s+it|cancel(?:\s+(?:it|that))?|"
    r"hold\s+off|skip\s+(?:it|that)|stop)\b|"
    r"\b(?:i(?:'d|\s+would)\s+(?:rather|prefer)|better|let(?:'s|\s+us))\s+not\b|"
    r"\b(?:could|would)\s+you\s+not\b|"
    r"\bi\s+(?:do\s+not|don't|dont)\s+want\b|"
    r"(?:\A|[,;.!?…—–-]\s*)(?:no|nope|nah|нет|неа)\b|"
    r"\b(?:давай(?:те)?|лучше)\s+не\b|"
    r"\b(?:я\s+)?(?:лучше\s+)?не\s+(?:буду|хочу)\b|"
    r"\bне\s+(?:делай(?:те)?|компилируй(?:те)?|собирай(?:те)?|"
    r"запускай(?:те)?|выполняй(?:те)?|надо|нужно)\b|"
    r"\b(?:отмена|отбой|стоп|передумал(?:а)?)\b"
    r")",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_SAFE_DELIVERY_REMAINDER = re.compile(
    r"\A\s*(?:"
    r"(?:no|without)\s+(?:explanation|commentary|details?)"
    r"(?:\s+(?:is|are))?\s*(?:needed|required)?\s*[,;:—–-]*\s*"
    r"(?:just\s+)?(?:send|attach|upload|deliver)\s+(?:me\s+)?(?:the\s+)?"
    r"(?:jar|binary|build\s+artifact)|"
    r"(?:без\s+(?:объяснен\w*|комментар\w*|подробност\w*)|"
    r"(?:объяснен\w*|комментар\w*|подробност\w*)\s+не\s+нужн\w*)"
    r"\s*[,;:—–-]*\s*(?:просто\s+)?"
    r"(?:пришли(?:те)?|отправь(?:те)?|приложи(?:те)?|выгрузи(?:те)?)\s+"
    r"(?:мне\s+)?(?:jar|бинарник|артефакт)\w*"
    r")\s*[.!?…]*\s*\Z",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_SAFE_CONTEXT_REMAINDER = re.compile(
    rf"\A\s*{_ARTIFACT_COMPILE_FILENAME}\s+(?:"
    r"is\s+(?:unrelated|only\s+(?:an?\s+)?(?:reference|example)|not\s+the\s+target)|"
    r"(?:не\s+является|не)\s+(?:целью|исходником)|"
    r"(?:лишь|только)\s+(?:ссылка|пример))\s*[.!?…]*\s*\Z",
    re.IGNORECASE,
)
_ARTIFACT_COMPILE_SAFE_COMPANION_REMAINDER = re.compile(
    r"\A\s*(?:(?:and|then|also|please|и|затем|также|пожалуйста|"
    r"а\s+потом)\s+){0,3}(?:"
    r"explain(?:\s+to\s+me)?\s+(?:"
    r"what\s+(?:this|the)\s+(?:code|source|class|method|compiler\s+error)\s+means?|"
    r"how\s+(?:it|the\s+(?:code|source|class|method))\s+works?|"
    r"(?:the\s+|this\s+)?(?:code|source|class|method|compiler(?:\s+error)?|"
    r"diagnostics?|build\s+result|jar|output|generated\s+artifact))|"
    r"(?:review|summari[sz]e|describe|analy[sz]e|inspect|check)\s+"
    r"(?:the\s+|this\s+)?(?:code|source|class|method|compiler(?:\s+error)?|"
    r"diagnostics?|build\s+result|jar|output|generated\s+artifact)|"
    r"(?:объясни(?:те)?|поясни(?:те)?)\s+(?:мне\s+)?(?:"
    r"как\s+(?:код|исходник|класс|метод)\s+работа\w*|"
    r"что\s+(?:означа\w*|дела\w*)\s+(?:код|исходник|класс|метод)|"
    r"(?:этот|данный)?\s*(?:код|исходник|класс|метод|компилятор|"
    r"диагностик\w*|ошибк\w*\s+компил\w*|результат\w*\s+сборк\w*|"
    r"jar|выход\w*|артефакт\w*))|"
    r"(?:проверь(?:те)?|разбери(?:те)?|проанализируй(?:те)?|опиши(?:те)?|"
    r"суммаризируй(?:те)?|сделай(?:те)?\s+ревью)\s+"
    r"(?:этот|данный)?\s*(?:код|исходник|класс|метод|компилятор|"
    r"диагностик\w*|ошибк\w*\s+компил\w*|результат\w*\s+сборк\w*|"
    r"jar|выход\w*|артефакт\w*)"
    r")\s*[.!?…]*\s*\Z",
    re.IGNORECASE,
)
_METADATA_V4 = ipaddress.ip_address("169.254.169.254")
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_OTHER_METADATA_V4 = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("192.0.0.192"),
    }
)


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    """Resolved, current-speech authority consumed by network stages."""

    host: str
    addresses: tuple[str, ...]
    implied_port: int | None
    source_token: str
    source_sha256: str

    @property
    def connect_address(self) -> str:
        if not self.addresses:
            raise ValueError("pinned target has no authorized address")
        return self.addresses[0]

    def public_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "addresses": list(self.addresses),
            "implied_port": self.implied_port,
            "source_sha256": self.source_sha256,
        }


_PINNED_TARGET: ContextVar[PinnedTarget | None] = ContextVar("friday_engineer_pinned_target", default=None)


@contextmanager
def bind_pinned_target(target: PinnedTarget | None) -> Iterator[PinnedTarget | None]:
    """Bind code-owned target authority across one model/tool turn."""

    token = _PINNED_TARGET.set(target)
    try:
        yield target
    finally:
        _PINNED_TARGET.reset(token)


def current_pinned_target() -> PinnedTarget | None:
    return _PINNED_TARGET.get()


def normalize_ip_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Collapse IPv4-mapped IPv6 before applying destination policy."""

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject metadata aliases and non-target network classes fail-closed."""

    normalized = normalize_ip_address(address)
    if normalized in {_METADATA_V4, _METADATA_V6, *_OTHER_METADATA_V4}:
        return True
    return bool(
        normalized.is_unspecified
        or normalized.is_multicast
        or normalized.is_reserved
        or normalized.is_link_local
    )


def _normalize_hostname(value: str) -> str:
    host = str(value or "").strip().rstrip(".").casefold()
    if not host or len(host) > 253 or "\x00" in host or any(char.isspace() for char in host):
        raise ValueError("host is empty or malformed")
    try:
        return str(normalize_ip_address(ipaddress.ip_address(host)))
    except ValueError:
        pass
    if host == "localhost":
        return host
    labels = host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("host is empty or malformed")
    return host


def _validate_port(port: int | None) -> int | None:
    if port is None:
        return None
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError("port is not in 1..65535")
    return int(port)


def parse_host_token(value: str) -> tuple[str, int | None]:
    raw = _TRAILING.sub("", str(value or "").strip())
    if not raw:
        raise ValueError("host is empty")
    if "://" in raw:
        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("URL port is invalid") from exc
        return _normalize_hostname(parsed.hostname or ""), _validate_port(port)
    if raw.count(":") == 1 and not raw.startswith("["):
        host, maybe_port = raw.rsplit(":", 1)
        if maybe_port.isdigit():
            return _normalize_hostname(host), _validate_port(int(maybe_port))
    if raw.startswith("[") and "]" in raw:
        host = raw[1 : raw.index("]")]
        rest = raw[raw.index("]") + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                raise ValueError("bracketed host has an invalid port")
            return _normalize_hostname(host), _validate_port(int(rest[1:]))
        return _normalize_hostname(host), None
    return _normalize_hostname(raw), None


def extract_targets(speech: str) -> list[dict[str, str | int | None]]:
    """Return unquoted current-speech targets in textual appearance order.

    Quoted examples, code and Markdown blockquotes are data, not destination
    authority.  Their bytes are position-preservingly blanked by the same
    projection used by the action classifier before any target regex runs.
    """

    _, authority = _request_projection(speech)
    candidates: list[tuple[int, int, int, str, str]] = []
    for priority, (kind, pattern) in enumerate(
        (
            ("url", _URL),
            ("ipv6", _IPV6_LOOSE),
            ("ipv4", _IPV4),
            ("ipv6", _IPV6),
            ("hostname", _HOSTNAME),
        )
    ):
        for match in pattern.finditer(authority):
            candidates.append((match.start(), match.end(), priority, kind, match.group()))
    candidates.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))

    accepted_ranges: list[tuple[int, int]] = []
    found: list[dict[str, str | int | None]] = []
    seen: set[tuple[str, int | None]] = set()
    for start, end, _priority, kind, token in candidates:
        if any(
            start < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_ranges
        ):
            continue
        try:
            host, port = parse_host_token(token)
        except ValueError:
            continue
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        accepted_ranges.append((start, end))
        found.append({"host": host, "port": port, "kind": kind, "token": token[:253]})
    return found


def extract_single_target(speech: str) -> dict[str, str | int | None] | None:
    """Select exactly one current-speech target or refuse the ambiguous turn."""

    targets = extract_targets(speech)
    if not targets:
        return None
    if len(targets) != 1:
        raise ValueError("engineer network turn must name exactly one target")
    return targets[0]


def extract_single_cidr(speech: str) -> str | None:
    """Return one unquoted canonical CIDR and reject mixed target authority."""

    _, authority = _request_projection(speech)
    url_ranges = [(item.start(), item.end()) for item in _URL.finditer(authority)]
    matches = [
        item
        for item in _CIDR.finditer(authority)
        if not any(item.start() < end and item.end() > start for start, end in url_ranges)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("engineer network turn must name exactly one CIDR")
    match = matches[0]
    token = match.group()
    masked = authority[: match.start()] + (" " * len(token)) + authority[match.end() :]
    if extract_targets(masked):
        raise ValueError("engineer network turn cannot mix a CIDR with another target")
    try:
        network = ipaddress.ip_network(token, strict=True)
    except ValueError as exc:
        raise ValueError("engineer network CIDR is invalid or noncanonical") from exc
    canonical = str(network)
    if token.casefold() != canonical.casefold():
        raise ValueError("engineer network CIDR is invalid or noncanonical")
    return canonical


@dataclass(frozen=True, slots=True)
class _DirectRequestSpan:
    start: int
    end: int
    unit_start: int
    unit_end: int


def _normalize_request_text(speech: str) -> str:
    """Normalize words while retaining newline authority boundaries."""

    normalized = unicodedata.normalize("NFKC", str(speech or ""))
    lines: list[str] = []
    for line in normalized.splitlines():
        compact = " ".join(line.split())
        # CommonMark indented code is data.  Detect its original indentation
        # before whitespace normalization can turn it into an imperative.
        lines.append(" " * len(compact) if _REQUEST_INDENTED_CODE.match(line) else compact)
    return "\n".join(lines)


def _mask_request_data(text: str) -> str:
    """Blank quoted/code/reported Markdown payloads without moving offsets."""

    masked = text
    for pattern in (_REQUEST_CODE_TEXT, _QUOTED_REQUEST_TEXT):
        masked = pattern.sub(lambda match: " " * len(match.group(0)), masked)
    # CommonMark permits paragraph continuation lines in a block quote to omit
    # the ``>`` marker.  Mask the complete contiguous paragraph; otherwise a
    # quoted imperative on its second line could become current authority.
    projected: list[str] = []
    in_blockquote_paragraph = False
    for line in masked.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content) :]
        if not content.strip():
            in_blockquote_paragraph = False
            projected.append(line)
            continue
        if _REQUEST_BLOCKQUOTE_START.match(content):
            in_blockquote_paragraph = True
        if in_blockquote_paragraph:
            projected.append((" " * len(content)) + ending)
        else:
            projected.append(line)
    return "".join(projected)


def _request_projection(speech: str) -> tuple[str, str]:
    text = _normalize_request_text(speech)
    return text, _mask_request_data(text)


def _network_report_export_matches(unit: str) -> tuple[re.Match[str], ...]:
    """Return bounded output clauses; quoted/reported masking happens upstream."""

    object_matches = tuple(_NETWORK_REPORT_EXPORT.finditer(unit))
    carrier_matches = tuple(
        match
        for match in _NETWORK_REPORT_CARRIER_EXPORT.finditer(unit)
        if not any(
            object_match.start() <= match.start() and match.end() <= object_match.end()
            for object_match in object_matches
        )
    )
    return tuple(sorted((*object_matches, *carrier_matches), key=lambda match: match.start()))


def _network_report_export_requested_in_unit(unit: str) -> bool:
    return bool(
        _network_report_export_matches(unit)
        and _NETWORK_REPORT_EXPORT_NEGATION.search(unit) is None
        and _NETWORK_REPORT_EXPORT_META.search(unit) is None
    )


def _network_report_format_in_unit(unit: str) -> str | None:
    matches = _network_report_export_matches(unit)
    if not _network_report_export_requested_in_unit(unit):
        return None
    surface = " ".join(match.group(0) for match in matches)
    requested: set[str] = set()
    if _NETWORK_REPORT_JSON.search(surface):
        requested.add("json")
    if _NETWORK_REPORT_MARKDOWN.search(surface):
        requested.add("markdown")
    if len(requested) > 1:
        return None
    return next(iter(requested), "markdown")


def _without_network_report_export_clause(unit: str) -> str:
    """Remove only a proven output clause before passive-report scan checks."""

    masked = unit
    for match in reversed(_network_report_export_matches(unit)):
        masked = masked[: match.start()] + (" " * (match.end() - match.start())) + masked[match.end() :]
    return masked


def _request_units(masked: str) -> Iterator[tuple[int, int]]:
    cursor = 0
    for boundary in _REQUEST_UNIT_BOUNDARY.finditer(masked):
        if cursor < boundary.start():
            yield cursor, boundary.start()
        cursor = boundary.end()
    if cursor < len(masked):
        yield cursor, len(masked)


def _request_is_negated(masked: str) -> bool:
    # Russian ``не мог бы`` is a conventional polite request, not a
    # cancellation. A second ``не`` before the action remains visible and denies it.
    negation_surface = _POLITE_NEGATIVE_MODAL.sub(
        lambda match: " " * len(match.group(0)),
        masked,
    )
    return _ACTIVE_ASSESSMENT_NEGATION.search(negation_surface) is not None


def _newline_payload_has_inert_governor(masked: str, unit_start: int) -> bool:
    """Keep a reported/example paragraph inert after a ``:`` + newline."""

    if _EXPLICIT_REQUEST_CONTEXT_RESET.match(masked[unit_start:]):
        return False
    prefix = masked[:unit_start]
    lines = prefix.split("\n")
    # The last item is the current (possibly empty) line prefix.  Only a
    # completed preceding line can introduce the following payload.  A blank
    # line does not end that authority boundary: pasted/report payloads can
    # contain arbitrary vertical whitespace.  A short ``Label:`` is also inert
    # because speaker attribution must never become packet authority.
    return any(
        line.rstrip().endswith(":")
        and (_REPORTED_REQUEST_CUE.search(line) or _META_REQUEST_CUE.search(line) or len(line.strip()) <= 96)
        for line in lines[:-1]
    )


def _prior_request_unit_has_inert_governor(masked: str, unit_start: int) -> bool:
    """Keep a prior reported/meta/negative sentence from minting a new effect."""

    prior_units = tuple(_request_units(masked[:unit_start]))
    if not prior_units or _EXPLICIT_REQUEST_CONTEXT_RESET.match(masked[unit_start:]):
        return False
    prior_start, prior_end = prior_units[-1]
    prior = masked[prior_start:prior_end]
    return bool(
        _REPORTED_REQUEST_CUE.search(prior)
        or _META_REQUEST_CUE.search(prior)
        or _HOST_VULNERABILITY_QUESTION_NEGATION.search(prior)
        or _LEADING_REQUEST_CANCEL.search(prior)
    )


def _direct_request_matches(speech: str, pattern: re.Pattern[str]) -> tuple[_DirectRequestSpan, ...]:
    """Locate direct action clauses and keep data/reported speech inert.

    A fact may precede the request (``nmap is installed, scan ...``), but the
    action itself must begin a punctuation-delimited clause.  Every governing
    prefix in the same sentence is inspected, so inserting ``please`` between a
    reporting verb and its quoted command cannot mint effect authority.
    """

    _text, masked = _request_projection(speech)
    if not masked.strip():
        return ()
    found: list[_DirectRequestSpan] = []
    for unit_start, unit_end in _request_units(masked):
        if _newline_payload_has_inert_governor(masked, unit_start):
            continue
        unit = masked[unit_start:unit_end]
        if _CONDITIONAL_REQUEST_CUE.search(unit):
            continue
        starts = [unit_start]
        starts.extend(unit_start + boundary.end() for boundary in _REQUEST_SOFT_BOUNDARY.finditer(unit))
        for start in starts:
            request = pattern.match(masked[start:unit_end])
            if request is None:
                continue
            governing_prefix = masked[unit_start:start]
            if (
                _REPORTED_REQUEST_CUE.search(governing_prefix)
                or _META_REQUEST_CUE.search(governing_prefix)
                or _LEADING_REQUEST_CANCEL.search(governing_prefix)
            ):
                continue
            request_start = start + request.start()
            request_end = start + request.end()
            trailing = masked[request_end:]
            if _TRAILING_REQUEST_CANCEL.search(trailing) or _TRAILING_REQUEST_ATTRIBUTION.search(trailing):
                return ()
            found.append(
                _DirectRequestSpan(
                    start=request_start,
                    end=request_end,
                    unit_start=unit_start,
                    unit_end=unit_end,
                )
            )
            break
    return tuple(found)


def requests_active_assessment(speech: str) -> bool:
    """Return whether the current human text explicitly asks for active probes.

    A host or URL is data, not effect authority.  This deliberately narrow,
    code-owned gate admits only direct request language and fails closed on a
    negated request.  Target extraction and policy admission remain separate
    gates; this predicate alone can never authorize a destination.
    """

    text, masked = _request_projection(speech)
    if not text or _request_is_negated(masked):
        return False
    request_spans = _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    if not request_spans:
        return False
    targets = extract_targets(text)
    if len(targets) != 1:
        # Preserve an explicit zero/multi-target request for the separate exact
        # target gate, which will return a useful refusal without doing DNS.
        return any(
            _PASSIVE_ASSESSMENT_OBJECT.search(
                _without_network_report_export_clause(masked[item.unit_start : item.unit_end])
            )
            is None
            for item in request_spans
        )
    token = str(targets[0].get("token") or "")
    target_start = text.casefold().find(token.casefold())
    if target_start < 0:
        return False
    target_end = target_start + len(token)
    for request_span in request_spans:
        between = (
            masked[request_span.end : target_start]
            if request_span.end <= target_start
            else masked[target_end : request_span.start]
        )
        # “Inspect the report about host” is a request to inspect passive
        # material, not permission to contact the host. Keep the effect phrase
        # close to the target and free of an intervening passive object.
        if len(between) <= 160 and _PASSIVE_ASSESSMENT_OBJECT.search(between) is None:
            return True
    return False


def requests_artifact_patch(speech: str) -> bool:
    """Admit artifact mutation only from a direct, current-human request.

    Static evidence is adversarial and may tell the model to call the patch
    tool.  This narrow code-owned predicate is evaluated only against the
    authenticated current user message; an uploaded file, prior conversation,
    dossier text, or model output cannot satisfy it.
    """

    text = " ".join(str(speech or "").split())
    return bool(
        text
        and _ARTIFACT_PATCH_NEGATION.search(text) is None
        and _ARTIFACT_PATCH_REQUEST.search(text) is not None
    )


def requests_artifact_decompile(speech: str) -> bool:
    """Admit a direct current-user binary analysis/decompilation request.

    The action must own a concrete deictic artifact or name a binary/executable.
    The shared authority projection keeps quotes, code and blockquotes inert;
    the shared clause parser additionally rejects reported, example,
    conditional and trailing-cancelled commands.  This predicate only classifies
    current speech and grants neither an artifact identity nor tool authority.
    """

    text, masked = _request_projection(speech)
    if (
        not masked.strip()
        or _ARTIFACT_DECOMPILE_NEGATION.search(masked)
        or _ARTIFACT_DECOMPILE_TARGET_EXCLUSION.search(masked)
        or _ARTIFACT_DECOMPILE_CAPABILITY.search(masked)
    ):
        return False
    for request in _direct_request_matches(text, _ARTIFACT_DECOMPILE_REQUEST):
        prefix = masked[request.unit_start : request.start]
        if _ARTIFACT_DECOMPILE_META.search(prefix) or _ARTIFACT_DECOMPILE_REPORTED.search(prefix):
            continue
        prior_units = tuple(_request_units(masked[: request.unit_start]))
        if prior_units and not _EXPLICIT_REQUEST_CONTEXT_RESET.match(masked[request.unit_start :]):
            prior_start, prior_end = prior_units[-1]
            prior = masked[prior_start:prior_end]
            if (
                _REPORTED_REQUEST_CUE.search(prior)
                or _META_REQUEST_CUE.search(prior)
                or _ARTIFACT_DECOMPILE_REPORTED.search(prior)
            ):
                continue
        return True
    return False


def artifact_decompile_request_is_atomic(speech: str) -> bool:
    """Whether decompilation is the sole clause in the current utterance."""

    if not requests_artifact_decompile(speech):
        return False
    text, masked = _request_projection(speech)
    for request in _direct_request_matches(text, _ARTIFACT_DECOMPILE_REQUEST):
        if request.unit_start != 0:
            continue
        trailing = masked[request.end :]
        if re.fullmatch(r"[\s.!?…]*", trailing):
            return True
    return False


def _artifact_compile_remainder_is_safe(remainder: str) -> bool:
    """Admit only closed, code-owned companion wording after javac authority."""

    if re.fullmatch(r"[\s,;.!?…—–-]*", remainder):
        return True
    surface = remainder.strip(" \t\r\n,;:.!?…—–-")
    if not surface or len(surface) > 240 or "\n" in surface:
        return False
    if _ARTIFACT_COMPILE_SAFE_DELIVERY_REMAINDER.fullmatch(surface):
        return True
    if _ARTIFACT_COMPILE_SAFE_CONTEXT_REMAINDER.fullmatch(surface):
        return True
    if _ARTIFACT_COMPILE_REMAINDER_DENIAL.search(surface):
        return False
    # A second sentence is a distinct speech act.  It cannot silently inherit
    # compile authority; the owner can issue it as a separate turn.
    if re.search(r"[.!?…]\s+\S", surface):
        return False
    return bool(_ARTIFACT_COMPILE_SAFE_COMPANION_REMAINDER.fullmatch(surface))


def _accepted_artifact_compile_requests(
    speech: str,
) -> tuple[str, str, tuple[_DirectRequestSpan, ...]]:
    """Return Java compile clauses which survive the complete intent gate."""

    text, masked = _request_projection(speech)
    negation_surface = _POLITE_NEGATIVE_MODAL.sub(
        lambda match: " " * len(match.group(0)),
        masked,
    )
    if (
        not masked.strip()
        or _ARTIFACT_COMPILE_NEGATION.search(negation_surface)
        or _ARTIFACT_COMPILE_DELIVERY_NEGATION.search(masked)
        or _ARTIFACT_COMPILE_TARGET_EXCLUSION.search(masked)
        or _ARTIFACT_COMPILE_MIXED_TARGETS.search(masked)
        or _ARTIFACT_COMPILE_CAPABILITY.search(masked)
    ):
        return text, masked, ()
    direct_requests = _direct_request_matches(text, _ARTIFACT_COMPILE_REQUEST)
    if len(direct_requests) != 1:
        return text, masked, ()
    accepted: list[_DirectRequestSpan] = []
    for request in direct_requests:
        unit = masked[request.unit_start : request.unit_end]
        named_sources = {
            match.group(0) for match in re.finditer(_ARTIFACT_COMPILE_FILENAME, unit, re.IGNORECASE)
        }
        if len(named_sources) > 1:
            continue
        trailing = masked[request.end : request.unit_end]
        full_trailing = masked[request.end :]
        if (
            _ARTIFACT_COMPILE_TRAILING_CONDITION.match(trailing)
            or _ARTIFACT_COMPILE_TRAILING_DENIAL.match(trailing)
            or _ARTIFACT_COMPILE_TRAILING_REPORT.match(trailing)
            or _ARTIFACT_COMPILE_TRAILING_ATTRIBUTION.search(full_trailing)
            or not _artifact_compile_remainder_is_safe(full_trailing)
        ):
            continue
        prefix = masked[request.unit_start : request.start]
        if (
            _META_REQUEST_CUE.search(prefix)
            or _REPORTED_REQUEST_CUE.search(prefix)
            or _ARTIFACT_COMPILE_REPORTED_PREFIX.search(prefix)
            or _ARTIFACT_COMPILE_DEFERRED_PREFIX.search(prefix)
        ):
            continue
        prior_units = tuple(_request_units(masked[: request.unit_start]))
        if prior_units and not _EXPLICIT_REQUEST_CONTEXT_RESET.match(masked[request.unit_start :]):
            prior_start, prior_end = prior_units[-1]
            prior = masked[prior_start:prior_end]
            if (
                _REPORTED_REQUEST_CUE.search(prior)
                or _ARTIFACT_COMPILE_REPORTED_PREFIX.search(prior)
                or _META_REQUEST_CUE.search(prior)
                or _CONDITIONAL_REQUEST_CUE.search(prior)
            ):
                continue
        accepted.append(request)
    return text, masked, tuple(accepted)


def requests_artifact_compile(speech: str) -> bool:
    """Admit one direct current-human request for the fixed Java profile."""

    _text, _masked, requests = _accepted_artifact_compile_requests(speech)
    return bool(requests)


def requested_artifact_compile_filename(speech: str) -> str | None:
    """Return the sole explicitly named Java source from an admitted request.

    This is target data, not authority: callers must still bind it to one
    current, owner-authorized Raw object.  Deictic/profile-only requests return
    ``None`` and therefore retain the exact-single-current-file rule.
    """

    _text, masked, requests = _accepted_artifact_compile_requests(speech)
    if not requests:
        return None
    distinct: dict[str, str] = {}
    for request in requests:
        accepted_span = masked[request.start : request.end]
        for match in re.finditer(_ARTIFACT_COMPILE_FILENAME, accepted_span, re.IGNORECASE):
            distinct.setdefault(match.group(0), match.group(0))
    return next(iter(distinct.values())) if len(distinct) == 1 else None


def artifact_compile_request_is_atomic(speech: str) -> bool:
    """Whether fixed-profile Java compilation is the sole current clause."""

    _text, masked, requests = _accepted_artifact_compile_requests(speech)
    if not requests:
        return False
    for request in requests:
        trailing = masked[request.end : request.unit_end]
        if (
            _ARTIFACT_COMPILE_TRAILING_CONDITION.match(trailing)
            or _ARTIFACT_COMPILE_TRAILING_DENIAL.match(trailing)
            or _ARTIFACT_COMPILE_TRAILING_REPORT.match(trailing)
            or _ARTIFACT_COMPILE_TRAILING_ATTRIBUTION.search(masked[request.end :])
        ):
            continue
        if request.unit_start != 0:
            continue
        if re.fullmatch(r"[\s.!?…]*", masked[request.end :]):
            return True
    return False


def requests_configured_network_assessment(speech: str) -> bool:
    """Admit the sole configured private network only from current speech.

    This is the code-owned meaning of “scan my subnet”.  Passive reports and
    configuration questions never authorize packets; an ambiguous configured
    scope is rejected later by policy rather than selected by a model.
    """

    text, masked = _request_projection(speech)
    if not requests_network_scan(text) or extract_targets(text):
        return False
    requests = _direct_request_matches(text, _CONFIGURED_NETWORK_ACTIVE_VERB)
    if not requests:
        requests = _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    return any(
        _CONFIGURED_NETWORK_OBJECT.search(masked[item.start : item.unit_end]) is not None for item in requests
    )


def requests_network_scan(speech: str) -> bool:
    """Require explicit packet-intent language for a CIDR-wide effect."""

    text, masked = _request_projection(speech)
    if not requests_active_assessment(text):
        return False
    packet_requests = _direct_request_matches(text, _CONFIGURED_NETWORK_ACTIVE_VERB)
    if any(
        _PASSIVE_ASSESSMENT_OBJECT.search(
            _without_network_report_export_clause(masked[item.unit_start : item.unit_end])
        )
        is None
        for item in packet_requests
    ):
        return True
    # A generic “check” is packet authority only when the current clause also
    # names an explicit scanner.  “Check my network/config/password” stays
    # passive and cannot reach nmap.
    return any(
        _PASSIVE_ASSESSMENT_OBJECT.search(
            _without_network_report_export_clause(masked[item.start : item.unit_end])
        )
        is None
        and _NETWORK_SCAN_MECHANISM.search(masked[item.start : item.unit_end])
        for item in _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    )


def requests_host_vulnerability_assessment(speech: str) -> bool:
    """Classify one explicit current-message vulnerability assessment.

    This is intent only. It neither chooses a destination nor authorizes
    packets; callers must separately pin exactly one policy-admitted host.
    """

    text, masked = _request_projection(speech)
    targets = extract_targets(text)
    target_ranges: list[tuple[int, int]] = []
    for target in targets:
        token = str(target.get("token") or "")
        start = text.casefold().find(token.casefold())
        if start >= 0:
            target_ranges.append((start, start + len(token)))

    def target_is_bound_to_request(item: _DirectRequestSpan) -> bool:
        for target_start, target_end in target_ranges:
            if target_start < item.unit_start or target_end > item.unit_end:
                continue
            if target_start < item.end:
                if target_end <= item.end:
                    return True
                continue
            tail = masked[item.end : target_start]
            if len(tail) <= 64 and _HOST_EFFECT_TARGET_TAIL.fullmatch(tail) is not None:
                return True
        return False

    active = requests_active_assessment(text) and any(
        (not targets or target_is_bound_to_request(item))
        and not _prior_request_unit_has_inert_governor(masked, item.unit_start)
        and _HOST_VULNERABILITY_CUE.search(masked[item.unit_start : item.unit_end]) is not None
        for item in _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    )
    question = any(
        bool(targets)
        and target_is_bound_to_request(item)
        and not _prior_request_unit_has_inert_governor(masked, item.unit_start)
        and _HOST_VULNERABILITY_QUESTION_NEGATION.search(masked[item.unit_start : item.unit_end]) is None
        and _PASSIVE_ASSESSMENT_OBJECT.search(masked[item.unit_start : item.unit_end]) is None
        and _REPORTED_REQUEST_CUE.search(masked[item.unit_start : item.unit_end]) is None
        for item in _direct_request_matches(text, _HOST_VULNERABILITY_QUESTION)
    )
    return active or question


def requests_host_vulnerability_followup(speech: str) -> bool:
    """Classify a direct deictic vulnerability continuation, without binding it."""

    text, masked = _request_projection(speech)
    if (
        not masked.strip()
        or extract_targets(text)
        or _request_is_negated(masked)
        or _HOST_VULNERABILITY_QUESTION_NEGATION.search(masked)
    ):
        return False
    return bool(_direct_request_matches(text, _HOST_VULNERABILITY_FOLLOWUP))


def requests_nmap_capability_truth(speech: str) -> bool:
    """Recognise a direct question/assertion about Friday's nmap capability."""

    text, masked = _request_projection(speech)
    if (
        not masked.strip()
        or extract_targets(text)
        or requests_active_assessment(text)
        or (
            _NMAP_CAPABILITY_NEGATION.search(masked)
            and re.search(r"\b(?:почему\s+не\s+использу\w*|why\s+(?:don't|do\s+not)\s+you\s+use)\b", masked)
            is None
        )
        or _META_REQUEST_CUE.search(masked)
    ):
        return False
    for unit_start, unit_end in _request_units(masked):
        unit = masked[unit_start:unit_end]
        if (
            _newline_payload_has_inert_governor(masked, unit_start)
            or _prior_request_unit_has_inert_governor(masked, unit_start)
            or _CONDITIONAL_REQUEST_CUE.search(unit)
            or _REPORTED_REQUEST_CUE.search(unit)
            or _META_REQUEST_CUE.search(unit)
            or _HOST_VULNERABILITY_QUESTION_NEGATION.search(unit)
            or _TRAILING_REQUEST_CANCEL.search(unit)
            or _TRAILING_REQUEST_ATTRIBUTION.search(unit)
        ):
            continue
        if _NMAP_CAPABILITY_TRUTH.match(unit):
            return True
    return False


def requested_network_report_format(speech: str) -> str | None:
    """Return one direct current-turn network report format, if unambiguous.

    This classifies an output carrier only.  It grants neither packet authority
    nor access to a historical result; callers must separately prove the exact
    current scan and final owner capability.
    """

    text, masked = _request_projection(speech)
    if not requests_network_scan(text):
        return None
    requests = _direct_request_matches(text, _CONFIGURED_NETWORK_ACTIVE_VERB)
    if not requests:
        requests = _direct_request_matches(text, _ACTIVE_ASSESSMENT_VERB)
    formats = {
        report_format
        for item in requests
        if (report_format := _network_report_format_in_unit(masked[item.unit_start : item.unit_end]))
        is not None
    }
    return next(iter(formats)) if len(formats) == 1 else None


def _requests_network_report_output_clause(
    speech: str,
    *,
    require_network_context: bool,
) -> bool:
    _text, masked = _request_projection(speech)
    if not masked.strip():
        return False
    for unit_start, unit_end in _request_units(masked):
        if _newline_payload_has_inert_governor(masked, unit_start):
            continue
        unit = masked[unit_start:unit_end]
        if (
            _CONDITIONAL_REQUEST_CUE.search(unit)
            or _REPORTED_REQUEST_CUE.search(unit)
            or _META_REQUEST_CUE.search(unit)
        ):
            continue
        has_target_context = bool(
            _NETWORK_REPORT_TARGET_CONTEXT.search(unit) or _NETWORK_REPORT_LITERAL_TARGET_CONTEXT.search(unit)
        )
        has_network_result_context = bool(
            _NETWORK_REPORT_SCAN_CONTEXT.search(unit)
            and (_NETWORK_REPORT_RESULT.search(unit) or has_target_context)
            or _NETWORK_REPORT_AUDIT_CONTEXT.search(unit)
            and has_target_context
        )
        if (
            has_network_result_context or not require_network_context
        ) and _network_report_export_requested_in_unit(unit):
            return True
    return False


def requests_network_report_export(speech: str) -> bool:
    """Recognise a direct network-result file clause without granting a scan."""

    return _requests_network_report_output_clause(speech, require_network_context=True)


def requests_network_report_output_clause(speech: str) -> bool:
    """Recognise the output clause after a network result is already settled."""

    return _requests_network_report_output_clause(speech, require_network_context=False)


def target_source_sha256(speech: str, token: str) -> str:
    body = f"{str(speech or '')}\x00{str(token or '')}".encode("utf-8", errors="replace")
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "artifact_compile_request_is_atomic",
    "artifact_decompile_request_is_atomic",
    "PinnedTarget",
    "bind_pinned_target",
    "current_pinned_target",
    "extract_single_target",
    "extract_single_cidr",
    "extract_targets",
    "is_forbidden_address",
    "normalize_ip_address",
    "parse_host_token",
    "requests_active_assessment",
    "requests_artifact_compile",
    "requested_artifact_compile_filename",
    "requests_artifact_decompile",
    "requests_artifact_patch",
    "requests_configured_network_assessment",
    "requests_host_vulnerability_assessment",
    "requests_host_vulnerability_followup",
    "requests_nmap_capability_truth",
    "requests_network_scan",
    "requested_network_report_format",
    "requests_network_report_export",
    "requests_network_report_output_clause",
    "target_source_sha256",
]
