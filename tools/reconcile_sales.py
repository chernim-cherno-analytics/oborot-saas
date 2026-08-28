#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сверка одного месяца продаж: база «Оборота» ↔ эталон первой таблицы (DATA-4/DATA-5).

ЗАЧЕМ. TECH_DEBT DATA-4: после первичной загрузки история никогда не
перечитывается, и механизма обнаружения расхождения с источником не
существует — расхождение может только накапливаться. TECH_DEBT DATA-5:
одно такое расхождение за май уже зафиксировано журналом и не расследовано,
и пока оно не объяснено, доверие ко ВСЕМ историческим цифрам ограничено.
У проекта при этом есть редкое преимущество — работающая первая таблица на
тех же данных. Этот инструмент превращает разовую ручную сверку в
воспроизводимую операцию: оператор называет организацию и месяц, получает
машиночитаемые итоги и крупнейшие расхождения по позициям.

ЧЕМ ЭТОТ ИНСТРУМЕНТ НЕ ЯВЛЯЕТСЯ.

* Он **ничего не чинит и не пересинхронизирует.** Расхождение — вход для
  расследования, а не повод переписать историю: автоматический ресинк по
  результату сверки означал бы, что цифры на экране меняет программа,
  которую никто не просил их менять.
* Он **не меняет ни одной формулы** и не трогает семантику отбора складов
  и источников (AGENTS.md §1, DECISIONS D-35). Он только читает и вычитает.
* Он **не решает продуктовый вопрос** «чей охват складов правильный»
  (см. раздел РАЗНЫЙ ОХВАТ ниже). Он делает этот вопрос видимым числом и
  на этом останавливается.
* Он **не часть продукта**: ни роута, ни страницы, ни планировщика. Это
  операторская диагностика, запускаемая руками.

СТРОГО ТОЛЬКО ЧТЕНИЕ. База открывается URI-режимом `mode=ro`, поверх него
ставится `PRAGMA query_only=1`, и оба режима проверяются пробой на захват
записи (`BEGIN IMMEDIATE`), которая обязана провалиться. Проба ничего не
пишет даже в аномальном случае: она берёт блокировку, а не строки, и при
неожиданном успехе инструмент откатывает её и отказывается работать
(fail-closed). Эталон читается GET-запросом или из локального файла. Ни
один путь исполнения не открывает файл на запись: результат печатается в
stdout и нигде не сохраняется. Это существенно: репозиторий публичный, а
итоги организации — коммерческая тайна её владельца.

РАЗНЫЙ ОХВАТ СКЛАДОВ — ЧИТАТЬ ДО ТОГО, КАК ОБЪЯСНЯТЬ РАЗНИЦУ. Две стороны
считают по разным множествам документов, и это видно прямо в исходниках:

* эталон (`legacy/sync.py::_fetch_docs` + `legacy/rebuild_history.py::sales_by_month`)
  агрегирует `demand` и `retaildemand` БЕЗ фильтра по складу вовсе, а
  возвраты (`salesreturn`) кладёт отдельным типом строки и вычитает их
  при выдаче помесячного нетто;
* «Оборот» (`app/ms_sync.py::_collect_sales`) отбрасывает документ целиком,
  если его склад не входит в множество АКТИВНЫХ складов организации.

Поэтому ненулевая разница здесь — не обязательно дефект загрузки: часть её
может быть законной разницей охвата. Инструмент печатает это предупреждение
рядом с числами и не выбирает, чей охват правильный: это продуктовое
решение владельца (AGENTS.md §3), а не вывод из вычитания.

СЫРОЕ НЕТТО И НЕТТО ИНТЕРФЕЙСА. У «Оборота» есть два разных законных ответа
на вопрос «сколько продано за месяц», и путать их нельзя:

* **сырое нетто** — все строки `sales` организации за месяц. Ближайший
  аналог эталона: у первой таблицы понятия «исключённая позиция» нет вовсе;
* **нетто интерфейса** — то, что видно на странице «Оборот»
  (`app/analytics_extra.py::build_revenue`): позиции с `products.excluded`
  и позиции в ручном архиве (`sku_hidden`) не считаются.

Разница между этими двумя величинами — не расхождение с источником, а
осознанные исключения владельца. Инструмент показывает обе, чтобы разницу
охвата каталога не приняли за потерю данных.

ЭТАЛОН. Принимаются два формата полезной нагрузки:

  A. Формат первой таблицы `GET /api/sales-by-month`:
     `{"<база>": {"YYYY-MM": [qty, rev], ...}, ...}` — нетто (продажи минус
     возвраты), валовые продажи и возвраты по отдельности там недоступны.
  B. Явный формат сверки:
     `{"month": "YYYY-MM", "bases": {"<база>": {"net_qty": .., "net_rev": ..,
       "gross_rev": .., "return_rev": ..}}}` — поля `gross_rev`/`return_rev`
     необязательные.

Чего эталон не сообщил — то остаётся `null`, а не нулём (D-30/D-34:
отсутствие факта не выдаётся за факт). Формат A физически не может
подтвердить, что сходятся именно возвраты, — в отчёте это так и написано.

ОТКАЗ ЗАКРЫТЫМ. Недоступная или битая база, отсутствующая организация,
месяц, за который у организации нет ни одной строки продаж, недоступный или
непонятный эталон, месяц, которого нет в эталоне, база, открывшаяся на
запись, — всё это ошибка с кодом 1, а не «ноль» и не «сошлось».

КОДЫ ВОЗВРАТА.
  0 — сверка выполнена (расхождение может быть любым, если не задан порог);
  1 — отказ закрытым: сверка НЕ выполнена;
  2 — сверка выполнена, и модуль расхождения по выбранной базе сравнения
      строго больше порога `--fail-on-delta`. Порога по умолчанию НЕТ:
      без явного `--fail-on-delta` ненулевое расхождение кодом возврата не
      наказывается. Так сделано намеренно — «расхождение» и «поломка» это
      разные события, и решать, какое расхождение считать поломкой, должен
      оператор, а не автор инструмента.

ЗАПУСК (примеры со синтетическими значениями; настоящие итоги в публичные
артефакты не попадают):

    python tools/reconcile_sales.py --db /path/to/copy.db --org 1 \\
        --month 2026-05 --reference-file /path/to/reference.json

    python tools/reconcile_sales.py --db /path/to/copy.db --org 1 \\
        --month 2026-05 --reference-url https://example.invalid/api/sales-by-month \\
        --json --top 20 --fail-on-delta 0

Копию базы оператор делает сам; инструмент работает и на живом файле, но
копия — правильная привычка: она снимает вопрос о блокировках и о том, что
чтение как-то повлияло на прод.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DELTA = 2

# Тот же суффикс размера, что у продукта (`app/ms_sync._SIZE_SUFFIX_RE`).
# Копия, а не импорт: инструмент обязан оставаться автономным и не поднимать
# приложение (модели, движок БД, конфигурацию) ради одной регулярки. Копия не
# расходится молча — `tests/test_reconcile_sales.py` сверяет обе реализации на
# наборе имён.
SIZE_SUFFIX_RE = re.compile(r"\s*\(([^)]*)\)\s*$")

MONTH_RE = re.compile(r"^\d{4}-\d{2}$")

# Печатается рядом с числами всегда. Формулировка сознательно не выбирает
# правильную сторону: это вопрос владельца (AGENTS.md §3).
SCOPE_NOTES = (
    "Эталон агрегирует demand+retaildemand без фильтра по складу; "
    "«Оборот» считает только документы активных складов организации. "
    "Часть разницы может быть законной разницей охвата, а не потерей данных. "
    "Какой охват правильный — решение владельца, инструмент его не принимает.",
    "«Сырое нетто» и «нетто интерфейса» различаются на исключённые позиции "
    "(products.excluded) и ручной архив (sku_hidden) — это не расхождение "
    "с источником.",
)


class ReconcileError(Exception):
    """Отказ закрытым: сверка не выполнена и результата нет."""


_WRITABLE_MSG = "база открылась с правом записи — инструмент только читает, работа прекращена"


# ── имена ────────────────────────────────────────────────────────────────────

def canon_base(name) -> str:
    """Каноническое базовое имя: без финальных скобок-размера, без краёв."""
    return SIZE_SUFFIX_RE.sub("", str(name if name is not None else "")).strip()


# ── месяц ────────────────────────────────────────────────────────────────────

def month_bounds(month: str) -> tuple[str, str]:
    """('2026-05') → ('2026-05-01', '2026-05-31'). Мусор — ReconcileError."""
    text = str(month or "")
    if not MONTH_RE.match(text):
        raise ReconcileError(f"месяц должен быть в формате YYYY-MM, получено: {text!r}")
    year, mon = int(text[:4]), int(text[5:7])
    if not 1 <= mon <= 12:
        raise ReconcileError(f"месяца {text!r} не существует")
    if year < 1970 or year > 9999:
        raise ReconcileError(f"год вне разумного диапазона: {text!r}")
    last = monthrange(year, mon)[1]
    return f"{text}-01", f"{text}-{last:02d}"


# ── база: только чтение ──────────────────────────────────────────────────────

def _assert_cannot_write(conn: sqlite3.Connection) -> None:
    """Доказать делом, что по этому соединению записать нельзя.

    Проба просит блокировку записи (`BEGIN IMMEDIATE`), а не изменяет строки,
    поэтому она безопасна даже в аномальном случае: захват немедленно
    откатывается, и содержимое базы не меняется ни на байт.

    Честная граница: `BEGIN IMMEDIATE` мог бы упасть и по занятости файла
    чужим писателем — такой отказ мы принимаем за подтверждение, то есть в
    редком случае проба доказывает меньше, чем кажется. Она третий слой, а
    не единственный, и ослабить `mode=ro` и `query_only` не может.
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        return  # ожидаемый и единственный правильный исход
    conn.execute("ROLLBACK")
    raise ReconcileError(_WRITABLE_MSG)


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Открыть базу «Оборота» строго на чтение или отказать.

    Три слоя, и каждый нужен: `mode=ro` запрещает запись на уровне SQLite,
    `query_only` — на уровне соединения (страхует от кода, который позже
    откроет транзакцию), `_assert_cannot_write` доказывает первые два делом.

    Про WAL. Прод «Оборота» живёт в режиме WAL, и SQLite, открывая такую базу
    даже только на чтение, создаёт рядом служебные `-wal`/`-shm`. Содержимое
    базы при этом не меняется, но каталог перестаёт быть нетронутым, а на
    каталоге без права записи открытие вовсе не удастся. Это ещё один довод
    сверять по КОПИИ, а не по живому файлу.
    """
    uri = "file:" + urllib.parse.quote(str(db_path)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise ReconcileError(f"база {db_path!r} не открылась только на чтение: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = 1")
        row = conn.execute("PRAGMA query_only").fetchone()
        if not row or int(row[0]) != 1:
            raise ReconcileError("PRAGMA query_only не встал — соединение не доказано read-only")
        _assert_cannot_write(conn)
    except ReconcileError:
        conn.close()
        raise
    except sqlite3.Error as exc:
        conn.close()
        raise ReconcileError(f"база {db_path!r} непригодна для чтения: {exc}") from exc
    return conn


def _query(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as exc:
        raise ReconcileError(f"запрос к базе не выполнен: {exc}") from exc


def load_saas_month(conn: sqlite3.Connection, org_id: int, month: str) -> dict:
    """Итоги месяца по базе «Оборота»: валовые, возвраты, сырое нетто, нетто интерфейса.

    Организации нет или у неё нет ни одной строки продаж за месяц — отказ
    закрытым: ноль здесь означал бы «сверили и сошлось», а сверять было нечего.
    """
    date_from, date_to = month_bounds(month)

    if not _query(conn, "SELECT 1 FROM orgs WHERE id = ?", (org_id,)):
        raise ReconcileError(f"организации id={org_id} нет в базе")

    total_rows = _query(
        conn,
        "SELECT COUNT(*) AS c FROM sales WHERE org_id = ? AND date >= ? AND date <= ?",
        (org_id, date_from, date_to),
    )[0]["c"]
    if not total_rows:
        raise ReconcileError(
            f"у организации id={org_id} нет ни одной строки продаж за {month} — сверять нечего")

    rows = _query(conn, """
        SELECT p.base_name AS base,
               SUM(CASE WHEN s.is_return = 0 THEN s.qty     ELSE 0 END) AS gross_qty,
               SUM(CASE WHEN s.is_return = 0 THEN s.revenue ELSE 0 END) AS gross_rev,
               SUM(CASE WHEN s.is_return <> 0 THEN s.qty     ELSE 0 END) AS return_qty,
               SUM(CASE WHEN s.is_return <> 0 THEN s.revenue ELSE 0 END) AS return_rev,
               SUM(CASE WHEN s.is_return = 0 AND p.excluded = 0 THEN s.qty     ELSE 0 END) AS inc_gross_qty,
               SUM(CASE WHEN s.is_return = 0 AND p.excluded = 0 THEN s.revenue ELSE 0 END) AS inc_gross_rev,
               SUM(CASE WHEN s.is_return <> 0 AND p.excluded = 0 THEN s.qty     ELSE 0 END) AS inc_return_qty,
               SUM(CASE WHEN s.is_return <> 0 AND p.excluded = 0 THEN s.revenue ELSE 0 END) AS inc_return_rev,
               COUNT(*) AS rows_n
          FROM sales s
          JOIN products p ON p.id = s.product_id AND p.org_id = s.org_id
         WHERE s.org_id = ? AND s.date >= ? AND s.date <= ?
         GROUP BY p.base_name
    """, (org_id, date_from, date_to))

    hidden = {canon_base(r["base_name"]) for r in _query(
        conn, "SELECT base_name FROM sku_hidden WHERE org_id = ?", (org_id,))}

    bases: dict[str, dict] = {}
    joined_rows = 0
    for r in rows:
        base = canon_base(r["base"])
        cur = bases.setdefault(base, {
            "gross_qty": 0.0, "gross_rev": 0.0, "return_qty": 0.0, "return_rev": 0.0,
            "included_gross_qty": 0.0, "included_gross_rev": 0.0,
            "included_return_qty": 0.0, "included_return_rev": 0.0,
        })
        joined_rows += int(r["rows_n"] or 0)
        in_archive = base in hidden
        cur["gross_qty"] += float(r["gross_qty"] or 0.0)
        cur["gross_rev"] += float(r["gross_rev"] or 0.0)
        cur["return_qty"] += float(r["return_qty"] or 0.0)
        cur["return_rev"] += float(r["return_rev"] or 0.0)
        if not in_archive:
            cur["included_gross_qty"] += float(r["inc_gross_qty"] or 0.0)
            cur["included_gross_rev"] += float(r["inc_gross_rev"] or 0.0)
            cur["included_return_qty"] += float(r["inc_return_qty"] or 0.0)
            cur["included_return_rev"] += float(r["inc_return_rev"] or 0.0)

    for base, v in bases.items():
        v["net_qty"] = v["gross_qty"] - v["return_qty"]
        v["net_rev"] = v["gross_rev"] - v["return_rev"]
        v["included_net_qty"] = v["included_gross_qty"] - v["included_return_qty"]
        v["included_net_rev"] = v["included_gross_rev"] - v["included_return_rev"]

    totals = {k: 0.0 for k in (
        "gross_qty", "gross_rev", "return_qty", "return_rev",
        "net_qty", "net_rev",
        "included_gross_qty", "included_gross_rev",
        "included_return_qty", "included_return_rev",
        "included_net_qty", "included_net_rev")}
    for v in bases.values():
        for k in totals:
            totals[k] += v[k]

    return {
        "org_id": org_id,
        "month": month,
        "date_from": date_from,
        "date_to": date_to,
        "totals": totals,
        "bases": bases,
        "rows": int(total_rows),
        # Строки продаж, у которых не нашлось товара этой же организации.
        # Это не норма и не ноль: такие деньги не попадают ни в один экран.
        "orphan_rows": int(total_rows) - joined_rows,
        "archived_bases": sorted(hidden & set(bases)),
    }


# ── эталон ───────────────────────────────────────────────────────────────────

def _number(value, where: str) -> float:
    """Число из полезной нагрузки эталона. `True` числом не считается."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReconcileError(f"эталон: {where} — ожидалось число, получено {value!r}")
    return float(value)


def parse_reference(payload, month: str) -> dict:
    """Разобрать полезную нагрузку эталона в итоги запрошенного месяца.

    Любое отклонение формата — отказ закрытым. Молча пропустить непонятную
    запись здесь нельзя: пропущенная позиция выглядит как расхождение и
    отправляет расследование не туда.
    """
    month_bounds(month)  # проверка формата запрошенного месяца
    if not isinstance(payload, dict):
        raise ReconcileError("эталон: ожидался JSON-объект верхнего уровня")

    # Явный формат опознаётся по ДВУМ служебным полям сразу. Одного `bases`
    # мало: формат A — это словарь имён позиций, и позиция с таким именем
    # хоть и невероятна, но опознание не должно зависеть от её отсутствия.
    if "bases" in payload and "month" in payload:
        if not isinstance(payload["bases"], dict):
            raise ReconcileError("эталон: поле 'bases' должно быть объектом")
        return _parse_reference_explicit(payload, month)
    return _parse_reference_by_month(payload, month)


def _parse_reference_by_month(payload: dict, month: str) -> dict:
    """Формат первой таблицы: {база: {"YYYY-MM": [qty, rev]}} — только нетто."""
    bases: dict[str, dict] = {}
    seen_month = False
    for raw_base, months in payload.items():
        if not isinstance(months, dict):
            raise ReconcileError(
                f"эталон: у позиции {raw_base!r} ожидался объект месяцев, получено {type(months).__name__}")
        for raw_month, pair in months.items():
            if not MONTH_RE.match(str(raw_month)):
                raise ReconcileError(f"эталон: {raw_base!r} — {raw_month!r} не месяц вида YYYY-MM")
            if str(raw_month) != month:
                continue
            seen_month = True
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ReconcileError(
                    f"эталон: {raw_base!r}/{raw_month} — ожидалась пара [qty, rev], получено {pair!r}")
            qty = _number(pair[0], f"{raw_base!r}/{raw_month} qty")
            rev = _number(pair[1], f"{raw_base!r}/{raw_month} rev")
            cur = bases.setdefault(canon_base(raw_base),
                                   {"net_qty": 0.0, "net_rev": 0.0,
                                    "gross_rev": None, "return_rev": None})
            cur["net_qty"] += qty
            cur["net_rev"] += rev
    if not seen_month:
        raise ReconcileError(f"эталон: месяца {month} в полезной нагрузке нет")
    totals = {
        "net_qty": sum(v["net_qty"] for v in bases.values()),
        "net_rev": sum(v["net_rev"] for v in bases.values()),
        "gross_rev": None,
        "return_rev": None,
    }
    return {"month": month, "shape": "sales_by_month", "bases": bases, "totals": totals}


def _parse_reference_explicit(payload: dict, month: str) -> dict:
    """Явный формат сверки: {"month": .., "bases": {база: {net_qty, net_rev, ...}}}."""
    ref_month = payload.get("month")
    if not isinstance(ref_month, str) or not MONTH_RE.match(ref_month):
        raise ReconcileError(f"эталон: поле 'month' обязано быть YYYY-MM, получено {ref_month!r}")
    if ref_month != month:
        raise ReconcileError(f"эталон описывает {ref_month}, а запрошен {month}")
    if not payload["bases"]:
        raise ReconcileError(f"эталон: за {month} нет ни одной позиции — сверять не с чем")

    bases: dict[str, dict] = {}
    has_gross = has_return = True
    for raw_base, item in payload["bases"].items():
        if not isinstance(item, dict):
            raise ReconcileError(
                f"эталон: у позиции {raw_base!r} ожидался объект, получено {type(item).__name__}")
        for required in ("net_qty", "net_rev"):
            if required not in item:
                raise ReconcileError(f"эталон: у позиции {raw_base!r} нет поля {required!r}")
        cur = bases.setdefault(canon_base(raw_base),
                               {"net_qty": 0.0, "net_rev": 0.0,
                                "gross_rev": 0.0, "return_rev": 0.0})
        cur["net_qty"] += _number(item["net_qty"], f"{raw_base!r} net_qty")
        cur["net_rev"] += _number(item["net_rev"], f"{raw_base!r} net_rev")
        if "gross_rev" in item:
            cur["gross_rev"] += _number(item["gross_rev"], f"{raw_base!r} gross_rev")
        else:
            has_gross = False
        if "return_rev" in item:
            cur["return_rev"] += _number(item["return_rev"], f"{raw_base!r} return_rev")
        else:
            has_return = False

    # Частично заполненный разрез — это не разрез: складывать известное с
    # неизвестным и печатать сумму значило бы выдать домысел за факт.
    for v in bases.values():
        if not has_gross:
            v["gross_rev"] = None
        if not has_return:
            v["return_rev"] = None

    totals = {
        "net_qty": sum(v["net_qty"] for v in bases.values()),
        "net_rev": sum(v["net_rev"] for v in bases.values()),
        "gross_rev": sum(v["gross_rev"] for v in bases.values()) if has_gross and bases else None,
        "return_rev": sum(v["return_rev"] for v in bases.values()) if has_return and bases else None,
    }
    return {"month": month, "shape": "explicit", "bases": bases, "totals": totals}


def load_reference_file(path: str, month: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise ReconcileError(f"эталон: файл {path!r} не прочитан: {exc}") from exc
    except ValueError as exc:
        raise ReconcileError(f"эталон: файл {path!r} — не JSON: {exc}") from exc
    return parse_reference(payload, month)


def load_reference_url(url: str, month: str, timeout: float = 30.0) -> dict:
    """Прочитать эталон обычным GET.

    Ни заголовков авторизации, ни cookie, ни тела запроса: инструмент ходит
    только туда, где данные и так открыты. Адрес с встроенными учётными
    данными отклоняется до сети — иначе секрет утёк бы в историю команд и в
    журналы сервера.
    """
    parsed = urllib.parse.urlparse(str(url))
    if parsed.scheme not in ("http", "https"):
        raise ReconcileError(f"эталон: поддерживаются только http и https, получено {parsed.scheme!r}")
    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        raise ReconcileError("эталон: учётные данные в адресе запрещены")
    if not parsed.netloc:
        raise ReconcileError(f"эталон: адрес {url!r} неполон")
    request = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — схема проверена выше
            status = getattr(response, "status", None)
            if status is not None and int(status) != 200:
                raise ReconcileError(f"эталон: {url} ответил HTTP {status}")
            raw = response.read()
    except ReconcileError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ReconcileError(f"эталон: {url} недоступен: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReconcileError(f"эталон: {url} вернул не JSON: {exc}") from exc
    return parse_reference(payload, month)


# ── сравнение ────────────────────────────────────────────────────────────────

def _delta(left, right):
    """Разность там, где известны обе стороны; иначе None (а не ноль)."""
    if left is None or right is None:
        return None
    return left - right


def compare(saas: dict, reference: dict, *, basis: str = "raw", top: int = 10) -> dict:
    """Свести две стороны в один отчёт. Ничего не округляет по дороге."""
    if basis not in ("raw", "included"):
        raise ReconcileError(f"база сравнения должна быть raw или included, получено {basis!r}")
    if saas["month"] != reference["month"]:
        raise ReconcileError(
            f"месяцы сторон не совпали: база {saas['month']}, эталон {reference['month']}")

    qty_key = "net_qty" if basis == "raw" else "included_net_qty"
    rev_key = "net_rev" if basis == "raw" else "included_net_rev"

    st, rt = saas["totals"], reference["totals"]
    totals = {
        "saas_gross_rev": st["gross_rev"],
        "saas_return_rev": st["return_rev"],
        "saas_net_rev": st["net_rev"],
        "saas_included_net_rev": st["included_net_rev"],
        "saas_gross_qty": st["gross_qty"],
        "saas_return_qty": st["return_qty"],
        "saas_net_qty": st["net_qty"],
        "saas_included_net_qty": st["included_net_qty"],
        "reference_gross_rev": rt["gross_rev"],
        "reference_return_rev": rt["return_rev"],
        "reference_net_rev": rt["net_rev"],
        "reference_net_qty": rt["net_qty"],
    }
    # Валовые и возвраты сравниваются ТОЛЬКО в сыром разрезе: у эталона
    # понятия «исключённая позиция» нет, и «нетто интерфейса» ему не с чем
    # сопоставить в этих двух строках. Ключи названы так, чтобы это было
    # видно из отчёта, а не из памяти читателя.
    deltas = {
        "gross_rev_raw": _delta(st["gross_rev"], rt["gross_rev"]),
        "return_rev_raw": _delta(st["return_rev"], rt["return_rev"]),
        "net_rev_raw": st["net_rev"] - rt["net_rev"],
        "net_rev_included": st["included_net_rev"] - rt["net_rev"],
        "net_qty_raw": st["net_qty"] - rt["net_qty"],
        "net_qty_included": st["included_net_qty"] - rt["net_qty"],
    }
    deltas["net_rev"] = deltas["net_rev_raw"] if basis == "raw" else deltas["net_rev_included"]
    deltas["net_qty"] = deltas["net_qty_raw"] if basis == "raw" else deltas["net_qty_included"]

    saas_bases, ref_bases = saas["bases"], reference["bases"]
    per_base = []
    for base in sorted(set(saas_bases) | set(ref_bases)):
        left = saas_bases.get(base)
        right = ref_bases.get(base)
        saas_rev = left[rev_key] if left else 0.0
        saas_qty = left[qty_key] if left else 0.0
        ref_rev = right["net_rev"] if right else 0.0
        ref_qty = right["net_qty"] if right else 0.0
        per_base.append({
            "base_name": base,
            "in_saas": left is not None,
            "in_reference": right is not None,
            "saas_net_rev": saas_rev,
            "reference_net_rev": ref_rev,
            "delta_rev": saas_rev - ref_rev,
            "saas_net_qty": saas_qty,
            "reference_net_qty": ref_qty,
            "delta_qty": saas_qty - ref_qty,
        })

    # Детерминированный порядок: по модулю расхождения, при равенстве — по имени.
    ranked = sorted(per_base, key=lambda it: (-abs(it["delta_rev"]), it["base_name"]))
    top_n = max(0, int(top))

    return {
        "org_id": saas["org_id"],
        "month": saas["month"],
        "basis": basis,
        "reference_shape": reference["shape"],
        "totals": totals,
        "deltas": deltas,
        "top_base_deltas": ranked[:top_n],
        "base_counts": {
            "saas": len(saas_bases),
            "reference": len(ref_bases),
            "only_in_saas": sum(1 for it in per_base if it["in_saas"] and not it["in_reference"]),
            "only_in_reference": sum(1 for it in per_base if it["in_reference"] and not it["in_saas"]),
            "matched": sum(1 for it in per_base if it["in_saas"] and it["in_reference"]),
        },
        "only_in_saas": [it["base_name"] for it in per_base if it["in_saas"] and not it["in_reference"]],
        "only_in_reference": [it["base_name"] for it in per_base if it["in_reference"] and not it["in_saas"]],
        "saas_rows": saas["rows"],
        "orphan_rows": saas["orphan_rows"],
        "archived_bases": saas["archived_bases"],
        "scope_notes": list(SCOPE_NOTES),
    }


def exceeds(report: dict, threshold) -> bool:
    """Расхождение по выбранной базе строго больше порога? Порога нет — нет."""
    if threshold is None:
        return False
    return abs(report["deltas"]["net_rev"]) > float(threshold)


# ── вывод ────────────────────────────────────────────────────────────────────

def _money(value) -> str:
    return "—" if value is None else f"{value:,.2f}".replace(",", " ")


def _qty(value) -> str:
    return "—" if value is None else f"{value:,.1f}".replace(",", " ")


def render_text(report: dict) -> str:
    t, d = report["totals"], report["deltas"]
    lines = [
        f"Сверка продаж: организация {report['org_id']}, месяц {report['month']}",
        f"База сравнения: {report['basis']}; формат эталона: {report['reference_shape']}",
        "",
        f"  валовые продажи      «Оборот» {_money(t['saas_gross_rev'])}"
        f"   эталон {_money(t['reference_gross_rev'])}   Δ {_money(d['gross_rev_raw'])}",
        f"  возвраты             «Оборот» {_money(t['saas_return_rev'])}"
        f"   эталон {_money(t['reference_return_rev'])}   Δ {_money(d['return_rev_raw'])}",
        f"  сырое нетто          «Оборот» {_money(t['saas_net_rev'])}"
        f"   эталон {_money(t['reference_net_rev'])}   Δ {_money(d['net_rev_raw'])}",
        f"  нетто интерфейса     «Оборот» {_money(t['saas_included_net_rev'])}"
        f"   эталон {_money(t['reference_net_rev'])}   Δ {_money(d['net_rev_included'])}",
        f"  штуки (нетто)        «Оборот» {_qty(t['saas_net_qty'])}"
        f"   эталон {_qty(t['reference_net_qty'])}   Δ {_qty(d['net_qty_raw'])}",
        "",
        f"  строк продаж: {report['saas_rows']}; без товара своей организации: {report['orphan_rows']}",
        f"  позиций: «Оборот» {report['base_counts']['saas']}, эталон {report['base_counts']['reference']}, "
        f"общих {report['base_counts']['matched']}, "
        f"только у «Оборота» {report['base_counts']['only_in_saas']}, "
        f"только у эталона {report['base_counts']['only_in_reference']}",
    ]
    if report["archived_bases"]:
        lines.append(f"  в ручном архиве организации: {len(report['archived_bases'])} позиц.")
    if report["top_base_deltas"]:
        lines.append("")
        lines.append("  Крупнейшие расхождения по позициям:")
        for item in report["top_base_deltas"]:
            where = "обе" if item["in_saas"] and item["in_reference"] else (
                "только «Оборот»" if item["in_saas"] else "только эталон")
            lines.append(
                f"    {_money(item['delta_rev']):>18}   {item['base_name']}   "
                f"(«Оборот» {_money(item['saas_net_rev'])}, эталон {_money(item['reference_net_rev'])}, {where})")
    lines.append("")
    for note in report["scope_notes"]:
        lines.append(f"  ! {note}")
    return "\n".join(lines)


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)


# ── командная строка ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile_sales.py",
        description="Сверка месяца продаж «Оборота» с эталоном первой таблицы. "
                    "Только чтение: ни одна из сторон не изменяется.",
    )
    parser.add_argument("--db", required=True, help="путь к файлу базы «Оборота» (открывается mode=ro)")
    parser.add_argument("--org", required=True, type=int, help="идентификатор организации")
    parser.add_argument("--month", required=True, help="месяц YYYY-MM")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reference-file", help="файл JSON с эталоном")
    source.add_argument("--reference-url", help="адрес http(s) с эталоном (GET, без учётных данных)")
    parser.add_argument("--basis", choices=("raw", "included"), default="raw",
                        help="какое нетто «Оборота» сравнивать: сырое (по умолчанию) или интерфейса")
    parser.add_argument("--top", type=int, default=10, help="сколько позиций показать (по модулю расхождения)")
    parser.add_argument("--timeout", type=float, default=30.0, help="таймаут запроса эталона, секунд")
    parser.add_argument("--json", action="store_true", help="печатать отчёт как JSON")
    parser.add_argument("--fail-on-delta", type=float, default=None, metavar="СУММА",
                        help="код возврата 2, если модуль расхождения строго больше СУММЫ; "
                             "без этого ключа расхождение кодом возврата не наказывается")
    return parser


def run(argv: list[str] | None = None, *, stdout=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    args = build_parser().parse_args(argv)
    conn = None
    try:
        if args.top < 0:
            raise ReconcileError("--top не может быть отрицательным")
        conn = open_readonly(args.db)
        saas = load_saas_month(conn, args.org, args.month)
        if args.reference_file:
            reference = load_reference_file(args.reference_file, args.month)
        else:
            reference = load_reference_url(args.reference_url, args.month, timeout=args.timeout)
        report = compare(saas, reference, basis=args.basis, top=args.top)
    except ReconcileError as exc:
        print(f"ОТКАЗ: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if conn is not None:
            conn.close()

    print(render_json(report) if args.json else render_text(report), file=out)
    return EXIT_DELTA if exceeds(report, args.fail_on_delta) else EXIT_OK


if __name__ == "__main__":
    sys.exit(run())
