#!/usr/bin/env python3
"""Замер поиска в интернете: сколько запросов вообще получают выдачу.

Набор ЗАМОРОЖЕН 2026-08-01 и менять его нельзя — иначе числа перестанут быть
сравнимыми с уже снятыми. Критерий объявлен до первого замера: доля запросов, на
которые вернулась непустая выдача.

Снятые числа на этом наборе:

    DuckDuckGo html          1/20   (19 ответов — HTTP 202, анти-бот заглушка)
    DuckDuckGo lite          0/20
    Brave без ключа          6/20
    Mojeek / Bing / Startpage 0/20
    Яндекс Search API v2    20/20   медиана 0.73 с

Запуск:

    FRIDAY_ENV_FILE=~/.jericho/.env.local .venv/bin/python tools/web_search_bench.py
    …                                     … tools/web_search_bench.py --chat
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.request

#: Двадцать запросов, на которых сняты все числа выше. НЕ МЕНЯТЬ.
FROZEN_QUERIES: tuple[str, ...] = (
    "ключевая ставка ЦБ РФ",
    "погода в Москве завтра",
    "новости Су-57",
    "курс доллара сегодня",
    "что такое Model Context Protocol",
    "расписание поездов Москва Казань",
    "цена на нефть Brent",
    "население России 2026",
    "как оформить загранпаспорт",
    "последние новости Украина",
    "python 3.14 release notes",
    "лучшие фильмы 2026",
    "симптомы гриппа",
    "стоимость биткоина",
    "wildberries отзывы",
    "страйкбольный привод АК-12",
    "закон о воинской обязанности",
    "отпуск военнослужащего дни",
    "SQLite WAL mode",
    "FastAPI background tasks",
)

#: Те же темы, но так, как их спрашивает человек у ассистента. Для сквозного
#: замера: важно не «выдача непустая», а что человек получил ответ со ссылкой.
FROZEN_CHAT_QUESTIONS: tuple[str, ...] = (
    "найди в интернете, какая сейчас ключевая ставка ЦБ",
    "погугли, какая погода завтра в Москве",
    "посмотри в интернете последние новости про Су-57",
    "найди в интернете курс доллара на сегодня",
    "погугли, что такое Model Context Protocol",
    "посмотри в сети расписание поездов Москва — Казань",
    "найди в интернете цену на нефть Brent",
    "погугли население России в 2026 году",
    "найди в интернете, как оформить загранпаспорт",
    "посмотри в интернете, что нового в Python 3.14",
)


async def bench_providers() -> int:
    from friday.config import load_settings
    from friday.web_surfer import AllProvidersRefusedError, WebSurfer

    surfer = WebSurfer(load_settings())
    non_empty = 0
    refused = 0
    durations: list[float] = []
    by_source: dict[str, int] = {}
    try:
        for query in FROZEN_QUERIES:
            started = time.monotonic()
            try:
                results = await surfer.search(query, max_results=5)
            except AllProvidersRefusedError as error:
                refused += 1
                print(f"  {query[:34]:34s} ОТКАЗ: {str(error)[:60]}")
                continue
            elapsed = time.monotonic() - started
            durations.append(elapsed)
            if results:
                non_empty += 1
                source = results[0].source
                by_source[source] = by_source.get(source, 0) + 1
            print(
                f"  {query[:34]:34s} {len(results)} рез. "
                f"({results[0].source if results else '—'}, {elapsed:.2f} с)"
            )
    finally:
        await surfer.close()

    total = len(FROZEN_QUERIES)
    print(f"\nнепустых: {non_empty}/{total} ({non_empty / total:.0%})   отказов: {refused}")
    if durations:
        ordered = sorted(durations)
        print(
            f"время: медиана {statistics.median(ordered):.2f} с, "
            f"p95 {ordered[int(len(ordered) * 0.95) - 1]:.2f} с"
        )
    print(f"кто ответил: {by_source or '—'}")
    return 0 if non_empty >= total * 0.8 else 1


def bench_chat(base_url: str, token: str, user_id: str) -> int:
    """Сквозной замер: спрашиваем так, как спросит человек.

    Считается не «выдача непустая», а то, что видит человек: позвался ли поиск,
    есть ли в ответе ссылка, не отказ ли это.
    """
    called = 0
    linked = 0
    failed = 0
    durations: list[float] = []
    for question in FROZEN_CHAT_QUESTIONS:
        payload = json.dumps({"message": question, "user_id": user_id}).encode()
        request = urllib.request.Request(
            f"{base_url}/api/chat",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
                body = json.loads(response.read())
        except Exception as error:  # noqa: BLE001
            failed += 1
            print(f"  {question[:44]:44s} ОШИБКА {type(error).__name__}")
            continue
        elapsed = time.monotonic() - started
        durations.append(elapsed)
        text = " ".join(str(body.get("message") or "").split())
        tools = body.get("tools_used") or []
        used_web = any("web" in str(name) for name in tools)
        has_link = "http://" in text or "https://" in text
        called += int(used_web)
        linked += int(has_link)
        mark = "✓" if used_web and has_link else ("~" if used_web else "✗")
        print(f"  {mark} {question[:42]:42s} {elapsed:5.1f} с  {text[:44]}")

    total = len(FROZEN_CHAT_QUESTIONS)
    print(f"\nпоиск вызван: {called}/{total}   со ссылкой: {linked}/{total}   сбоев: {failed}")
    if durations:
        print(f"медиана ответа: {statistics.median(durations):.1f} с")
    return 0 if called == total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat", action="store_true", help="сквозной замер через живой чат")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--env-file", default="/home/jericho/.jericho/.env.local")
    parser.add_argument("--user-id", default="964e5f17-a4bf-5744-a5c6-b7bfbdcd7bf0")
    args = parser.parse_args()

    if not args.chat:
        return asyncio.run(bench_providers())

    token = ""
    with open(args.env_file, encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if line.startswith(("FRIDAY_API_TOKEN=", "JERICHO_API_TOKEN=")):
                token = line.split("=", 1)[1]
                break
    if not token:
        print("не найден API-токен в", args.env_file)
        return 2
    return bench_chat(args.base_url, token, args.user_id)


if __name__ == "__main__":
    sys.exit(main())
