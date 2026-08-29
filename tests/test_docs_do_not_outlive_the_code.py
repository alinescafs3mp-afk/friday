"""Документ, который врёт о числах, хуже отсутствующего.

`docs/ARCHITECTURE.md` — не описание замысла, а запись ЗАМЕРОВ: он существует
ровно затем, чтобы следующий человек не перемерял то, что уже мерили. Устаревшее
число здесь дороже обычного: его не проверяют, на него ссылаются.

Разошлось три числа и одна версия:

* «Бюджет — 12 термов» против `_FTS_TERM_BUDGET = 24` — причём комментарий рядом с
  константой описывает переход 12→24 и свой замер, то есть доку просто забыли.
* `DENSE_EVIDENCE_MIN` «по умолчанию 0.35» против 0.40 в коде, и §7 противоречил
  §15 того же файла.
* Морфология «живёт в `retrieval/_morphology.py`» — такого файла нет.
* README: «Текущая версия 0.9.0» при 0.141.0 и «schema 8» при 18, причём это
  инструкция по обновлению, называющая неверную схему.

Тест не пересказывает доки — он сверяет то, что можно сверить машинно.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
ARCHITECTURE = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
PROJECT_BACKLOG = (ROOT / "outer_sol" / "PROJECT_BACKLOG.md").read_text(encoding="utf-8")
BACKUP_GUIDANCE = (ROOT / "docs" / "BACKUP_AND_RESTORE.md").read_text(encoding="utf-8")
OPERATIONS = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
RELEASE_CHECKLIST = (ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_the_readme_states_the_version_the_package_has():
    from friday import __version__

    match = re.search(
        r"Текущая версия: \*\*([0-9]+(?:\.[0-9]+)*(?:rc[0-9]+)?)\*\*",
        README,
    )
    assert match, "в README больше нет строки с версией — поправьте тест вместе с ней"
    assert match.group(1) == __version__, f"README обещает {match.group(1)}, пакет — {__version__}"


def test_the_readme_states_the_schema_the_code_opens():
    from friday.storage._base import SCHEMA_VERSION

    match = re.search(r"Схема SQLite — \*\*(\d+)\*\*", README)
    assert match, "в README больше нет строки со схемой"
    assert int(match.group(1)) == SCHEMA_VERSION, (
        f"README называет схему {match.group(1)}, код открывает {SCHEMA_VERSION} — "
        "это инструкция по обновлению, ошибка в ней дороже обычной"
    )


def test_current_release_identity_matches_package_operator_docs_and_schema():
    """The release number and no-migration schema gate must move as one unit."""
    from friday import __version__
    from friday.storage._base import SCHEMA_VERSION

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__

    unreleased_header = "## Unreleased — version assigned by release integrator\n"
    is_unreleased_candidate = CHANGELOG.startswith(unreleased_header)
    if is_unreleased_candidate:
        assert "неопубликованный release candidate" in README
        changelog_version = re.search(r"^## ([0-9]+(?:\.[0-9]+)+) —", CHANGELOG, re.MULTILINE)
    else:
        changelog_version = re.match(r"## ([0-9]+(?:\.[0-9]+)+) —", CHANGELOG)
    assert changelog_version, "CHANGELOG must name the current release baseline"
    assert changelog_version.group(1) == __version__

    operations_health = re.search(r"требуйте `status=ok` и `version=([0-9.]+)`", OPERATIONS)
    checklist_health = re.search(
        r"final startup health имеет `status=ok`, `version=([0-9.]+)`",
        RELEASE_CHECKLIST,
    )
    assert operations_health and operations_health.group(1) == __version__
    if is_unreleased_candidate:
        assert "final startup health имеет `status=ok`, `version=<allocated release version>`" in (
            RELEASE_CHECKLIST
        )
    else:
        assert checklist_health and checklist_health.group(1) == __version__

    checklist_schema = re.search(r"- schema version = (\d+);", RELEASE_CHECKLIST)
    assert checklist_schema, "release checklist must state the compatibility schema"
    assert int(checklist_schema.group(1)) == SCHEMA_VERSION


def test_secondary_runbook_is_bound_to_the_only_accepted_finalist():
    """Operator copy/paste values must not drift from the code-owned admission."""
    from friday.secondary_brain.profiles import (
        ACCEPTED_SECONDARY_RUNTIME_PROFILES,
        PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES,
    )

    (profile,) = ACCEPTED_SECONDARY_RUNTIME_PROFILES.values()
    assert set(ACCEPTED_SECONDARY_RUNTIME_PROFILES).isdisjoint(PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES)
    section = OPERATIONS.split("### Optional GPT-OSS secondary brain", 1)[1].split("## 2.", 1)[0]

    exact_lines = {
        f"FRIDAY_SECONDARY_LLM_BASE_URL={profile.endpoint_base_url}",
        f"FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY={profile.max_concurrency}",
        f"FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS={profile.max_context_tokens}",
        f"FRIDAY_SECONDARY_LLM_MODEL={profile.served_model_alias}",
        f"FRIDAY_SECONDARY_LLM_PROFILE={profile.profile_id}",
        "FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT=0",
        "FRIDAY_SECONDARY_LLM_MODE=shadow",
        "FRIDAY_SECONDARY_LLM_WORKLOADS=extract",
    }
    assert all(line in section for line in exact_lines)
    assert profile.manifest_sha256 in RELEASE_CHECKLIST
    assert f"output `{profile.max_output_tokens}`" in RELEASE_CHECKLIST
    assert f"chunked prefill `{profile.chunked_prefill_size}`" in RELEASE_CHECKLIST
    assert f"`mem_fraction_static={profile.mem_fraction_static}`" in RELEASE_CHECKLIST
    assert "secondary_shadow_to_private_shadow" in section
    assert "secondary_shadow_to_assist" in section
    assert "прямой ENV1→assist отклоняется" in section


def test_semantic_supervisor_runbooks_bind_current_policy_identities():
    """A changed policy digest must invalidate copy/paste rollout guidance too."""
    from friday import semantic_supervisor_policy as policy

    for document in (OPERATIONS, RELEASE_CHECKLIST):
        assert policy.SUPERVISOR_PRODUCT_POLICY_SHA256 in document
        assert policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256 in document


def test_canonical_backlog_states_the_schema_the_code_opens():
    """The only project status register must not pin a prehistoric schema."""
    from friday.storage._base import SCHEMA_VERSION

    match = re.search(r"- Database schema: (\d+)", PROJECT_BACKLOG)
    assert match, "в едином backlog нет проверяемой версии схемы"
    assert int(match.group(1)) == SCHEMA_VERSION


def test_the_term_budget_in_the_docs_is_the_one_in_the_code():
    from friday.storage._knowledge import _FTS_TERM_BUDGET

    match = re.search(r"Бюджет — \*\*(\d+)\*\* терма", ARCHITECTURE)
    assert match, "раздел про термы FTS переписан — сверьте число заново"
    assert int(match.group(1)) == _FTS_TERM_BUDGET


def test_the_dense_evidence_default_in_the_docs_is_the_one_in_the_code(settings):
    match = re.search(r"FRIDAY_RETRIEVAL_DENSE_EVIDENCE_MIN` \(по умолчанию \*\*([0-9.]+)\*\*", ARCHITECTURE)
    assert match, "раздел про плотное доказательство переписан — сверьте число заново"
    assert float(match.group(1)) == settings.retrieval_dense_evidence_min, (
        f"доки обещают {match.group(1)}, код берёт {settings.retrieval_dense_evidence_min}"
    )


def test_every_module_path_named_in_the_architecture_exists():
    """Ссылка на несуществующий файл отправляет читателя искать то, чего нет."""
    referenced = set(re.findall(r"`([\w/]+\.py)`", ARCHITECTURE))
    missing = sorted(
        path for path in referenced if not (ROOT / path).exists() and not (ROOT / "friday" / path).exists()
    )
    assert not missing, f"ARCHITECTURE.md ссылается на несуществующие файлы: {missing}"


def test_backup_guidance_names_the_runtime_database_and_file_backup_contract():
    """The recovery runbook must not point an operator at one guessed DB name."""

    assert "FRIDAY_DATABASE_PATH" in BACKUP_GUIDANCE
    assert "data/state/jericho.sqlite3" in BACKUP_GUIDANCE
    assert "data/backups/files/" in BACKUP_GUIDANCE
    assert "не распространяет удаления" in BACKUP_GUIDANCE
    assert "FRIDAY_BACKUP_KEEP" in BACKUP_GUIDANCE


# --- утверждения интерфейса о самом себе ------------------------------------


def test_a_guessed_attribution_is_not_called_a_source():
    """«Источники» означает «ответ опирается на это». Догадка — не то же самое.

    Возврат к единственному сильному попаданию защитим как эвристика: модель
    иногда не ставит метку. Но подписать догадку так же, как явную ссылку, значит
    утверждать проверенное там, где ничего не проверяли — а та же атрибуция кормит
    feedback и lifecycle.
    """
    from friday.agent_runtime import _citation_notice

    citations = [{"label": "K1", "title": "Договор аренды"}]
    explicit = _citation_notice(citations, True, inferred=False)
    guessed = _citation_notice(citations, True, inferred=True)

    assert explicit.startswith("📎 Источники:")
    assert guessed != explicit
    assert "Вероятно" in guessed and "не сослалась" in guessed


def test_the_trust_boundary_names_duckduckgo():
    """Local-first обещание обязано называть КАЖДЫЙ канал наружу.

    `web_surfer` ходит на HTML DuckDuckGo безусловно, когда настроенных провайдеров
    нет или их выдача пуста; инструменту хватает способности `web.search`. Пока
    доки называли внешними каналами только «явно настроенные search API», это было
    неверным утверждением о границе доверия, а не о реализации.
    """
    section = ARCHITECTURE.split("## 2", 1)[0]
    assert "DuckDuckGo" in section, "раздел «Границы системы» снова не называет этот канал"


def test_the_mission_tool_does_not_promise_the_model_it_never_runs_itself():
    """Модель пересказывает описание инструмента человеку как факт."""
    source = (ROOT / "friday" / "execution_kernel" / "__init__.py").read_text(encoding="utf-8")
    assert "ждёт запуска пользователем, ничего не выполняя сама" not in source, (
        "обещание вернулось: при operator_full_autonomy миссия от агента создаётся "
        "сразу READY и подхватывается воркером mission_runner"
    )


def test_a_server_without_tool_calling_still_answers():
    """Отказ в ОДНОЙ способности не должен выглядеть как отказ модели целиком.

    vLLM, запущенный без `--enable-auto-tool-choice` и `--tool-call-parser`,
    отвергает любой запрос с `tools` четырёхсотым. Агент шлёт инструменты всегда,
    поэтому на этой установке не работал ни один вызов с самого начала, а человек
    видел «LLM сейчас недоступна» вместо ответа. Профиль, который Friday сам
    предлагает для запуска, эти флаги не выставляет.
    """
    from friday.agent_runtime.llm import _tools_unsupported

    vllm = (
        '{"error":{"message":"\\"auto\\" tool choice requires --enable-auto-tool-choice '
        'and --tool-call-parser to be set","type":"BadRequestError"}}'
    )
    assert _tools_unsupported(vllm) is True

    # Другие четырёхсотые не должны молча лишать агента инструментов: это лечение
    # симптома чужой болезни.
    assert _tools_unsupported('{"error":{"message":"context length exceeded"}}') is False
    assert _tools_unsupported('{"error":{"message":"model not found"}}') is False
    assert _tools_unsupported("") is False


def test_the_readme_lists_every_cli_command():
    """Раздел CLI в README оформлен как исчерпывающий список — значит должен им быть.

    Он разошёлся с парсером на ДЕСЯТЬ команд из 23, и молчал не о мелочах:
    `search-source` (дословный поиск по исходному тексту — то, что нужно, когда
    подводит ранжирование), `reindex-embeddings` (после смены модели эмбеддингов),
    `tui` (самая уместная точка входа для не-разработчика), `up` и
    `install-services` («как поднять это навсегда»).

    Список подкоманд берётся из САМОГО парсера, а не переписывается сюда: копия
    разошлась бы точно так же, просто позже.
    """
    import re
    from pathlib import Path

    from friday.cli import build_parser

    parser = build_parser()
    names = sorted(
        {
            name
            for action in parser._subparsers._group_actions  # noqa: SLF001
            for name in action.choices
        }
        if parser._subparsers  # noqa: SLF001
        else set()
    )
    assert names, "подкоманды не нашлись — проба сломана, а не README"

    readme = Path("README.md").read_text(encoding="utf-8")
    missing = [name for name in names if not re.search(rf"jericho {re.escape(name)}\b", readme)]
    assert not missing, (
        f"README не упоминает команды: {', '.join(missing)}. Раздел выдаёт себя за полный "
        "перечень, поэтому умолчание читается как «такой команды нет»"
    )
