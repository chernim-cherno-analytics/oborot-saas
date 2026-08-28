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
     необязательные, но если даны оба, то `net_rev` обязан равняться их
     разности: разрез, противоречащий сам себе, — не разрез. Необязательное
     поле `returns_coverage: "full"` объявляет, что охват возвратов у эталона
     совпадает с «Оборотом» (по умолчанию это НЕ предполагается).
  C. Штатный публичный ответ `GET /api/sales-monthly?month=YYYY-MM`
     (`legacy/main.py::get_sales_monthly`):
     `{"months": [...], "month": "YYYY-MM",
       "summary": {"sales": .., "returns": .., "net": .., "sales_qty": ..,
                   "returns_qty": ..},
       "items": [{"base": .., "sale_rev": .., "sale_qty": .., "ret_rev": ..,
                  "ret_qty": .., "net": .., "sizes": [...]}]}`
     Это ЕДИНСТВЕННЫЙ формат, который отдаёт разрез «валовые/возвраты» сам,
     без ручной перекладки данных оператором. Ради этого он и поддержан
     напрямую: перекладка боевых цифр руками — лишний повод их где-нибудь
     сохранить, а сохранять их нельзя.

Чего эталон не сообщил — то остаётся `null`, а не нулём (D-30/D-34:
отсутствие факта не выдаётся за факт). Формат A физически не может
подтвердить, что сходятся именно возвраты, — в отчёте это так и написано.

ДВЕ ЛОВУШКИ ФОРМАТА C, из-за которых он проверяется строже прочих.

* **Ручка молча подменяет месяц.** `get_sales_monthly` при неизвестном ей
  `month` возвращает САМЫЙ СВЕЖИЙ месяц и тот же HTTP 200. Оператор спросил
  май, получил июль и не узнал бы об этом ни из кода ответа, ни из чисел.
  Поэтому `month` ответа сверяется с запрошенным, и несовпадение — отказ
  закрытым, а не тихая сверка не того месяца.
* **Ответ может противоречить сам себе.** `summary` считается отдельно от
  `items`, поэтому он проверяется суммой позиций: расхождение — отказ
  закрытым. Допуск при этом узкий и существует ровно ради шума сложения
  float (см. `MONEY_TOLERANCE`), а не ради того, чтобы «почти сошлось»
  считалось «сошлось». Он АБСОЛЮТНЫЙ и жёстко ограничен сверху: копейка —
  это потолок на любом обороте, а не доля от него. Прежняя относительная
  добавка росла вместе с суммой и на 25 млн прощала две копейки — ровно то
  ложное согласие, ради предотвращения которого проверка и написана.

ДВЕ ВЕЩИ, КОТОРЫЕ ИНСТРУМЕНТ НАЗЫВАЕТ, А НЕ СГЛАЖИВАЕТ.

* **Округление публикации формата A.** `sales_by_month` отдаёт выручку
  позиции округлённой до рубля, «Оборот» держит копейки. На ОДНИХ И ТЕХ ЖЕ
  исходных строках это даёт до полурубля на позицию — и это разрешение
  публикации, а не расхождение данных. Величина считается заранее
  (`FORMAT_A_REV_ROUNDING`), печатается отдельной строкой и вычитается только
  при решении «сработал ли `--fail-on-delta`». Само расхождение при этом
  остаётся в отчёте числом: прятать его нечем и незачем. У форматов B и C
  неопределённость нулевая, то есть их поведение прежнее.
* **Разный охват возвратов.** «Оборот» грузит `salesreturn` И
  `retailsalesreturn`, зеркало первой таблицы — только `salesreturn`. На
  месяце с розничными возвратами строки «валовые» и «возвраты» у двух сторон
  считают РАЗНЫЕ множества документов, поэтому классификация «возвраты
  сошлись, разница в валовых» по ним не доказывается. Отчёт помечает обе
  строки справочными (`returns_comparable: false`), пока охват не выровнен
  или не объявлен полным явным полем `returns_coverage`.

ОТКАЗ ЗАКРЫТЫМ. Недоступная или битая база, отсутствующая организация,
месяц, за который у организации нет ни одной строки продаж, строки продаж
без товара этой организации (их деньги выпадают из всех итогов, поэтому
сверка не выполняется вовсе), недоступный или непонятный эталон, месяц,
которого нет в эталоне, самопротиворечивый разрез `net`/`gross`/`return`,
база, открывшаяся на запись, нечисловое или НЕ КОНЕЧНОЕ значение в эталоне
(`NaN`, `Infinity`, целое вне диапазона float), негодные `--fail-on-delta`
и `--timeout` и просто неверный вызов — всё это ошибка с кодом 1, а не
«ноль» и не «сошлось».

КОДЫ ВОЗВРАТА.
  0 — сверка выполнена (расхождение может быть любым, если не задан порог);
  1 — отказ закрытым: сверка НЕ выполнена. Сюда же относится ЛЮБАЯ ошибка
      вызова — опечатка в ключе, `--org не-число`, забытый обязательный
      аргумент. Штатный `argparse` вышел бы на них кодом 2, но код 2 здесь
      занят и значит совсем другое, поэтому он переопределён (см. `_Parser`);
  2 — сверка выполнена, и модуль расхождения по выбранной базе сравнения
      строго больше порога `--fail-on-delta`. Порога по умолчанию НЕТ:
      без явного `--fail-on-delta` ненулевое расхождение кодом возврата не
      наказывается. Так сделано намеренно — «расхождение» и «поломка» это
      разные события, и решать, какое расхождение считать поломкой, должен
      оператор, а не автор инструмента. Сам порог обязан быть конечным и
      неотрицательным: `nan` молча выключал бы гейт, отрицательный —
      выворачивал бы его наизнанку.

ЗАПУСК (примеры со синтетическими значениями; настоящие итоги в публичные
артефакты не попадают):

    python tools/reconcile_sales.py --db /path/to/copy.db --org 1 \\
        --month 2026-05 --reference-file /path/to/reference.json

    python tools/reconcile_sales.py --db /path/to/copy.db --org 1 \\
        --month 2026-05 --json --top 20 --fail-on-delta 0 \\
        --reference-url 'https://example.invalid/api/sales-monthly?month=2026-05'

Копию базы оператор делает сам; инструмент работает и на живом файле, но
копия — правильная привычка: она снимает вопрос о блокировках и о том, что
чтение как-то повлияло на прод.
"""
from __future__ import annotations

import argparse
import json
import math
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

# Допуск сверки `summary` с суммой `items` в формате C. Он существует ровно
# ради одного: обе стороны — суммы float, и порядок слагаемых у эталона свой,
# поэтому последний бит результата у двух правильных сложений может не
# совпасть. Он НЕ существует ради того, чтобы «почти сошлось» считалось
# «сошлось».
#
# ЗДЕСЬ БЫЛ ДЕФЕКТ, найденный независимым ревью (discussion_r3877349893 и
# r3877357949). Прежняя добавка была ОТНОСИТЕЛЬНОЙ — 1e-9 от суммы — и потому
# росла вместе с оборотом: на 25 млн допуск становился 2,5 копейки и разница в
# две копейки считалась совпадением, на миллиарде — целым рублём. Документация
# при этом обещала «не больше копейки», то есть контракт и код разошлись, и
# разошлись в сторону ложного согласия. Это худшая из возможных сторон: сверка,
# которая молчит о расхождении, хуже отсутствующей сверки.
#
# Теперь допуск АБСОЛЮТНЫЙ и жёстко ограничен документированным максимумом:
# он равен оценке шума сложения, но никогда не больше MONEY_TOLERANCE /
# QTY_TOLERANCE, сколько бы ни был велик оборот. Оценка шума — верхняя граница
# ошибки суммирования положительных слагаемых: N × eps × |сумма|, что в шагах
# float64 равно SUMMATION_STEPS × ulp(|сумма|). N взято с большим запасом:
# миллион позиций в месяце — заведомо больше любого реального каталога.
MONEY_TOLERANCE = 0.01      # ₽, копейка — ПОТОЛОК, а не типичное значение
QTY_TOLERANCE = 1e-6        # шт — тот же потолок для количеств
SUMMATION_STEPS = 1_000_000  # верхняя оценка числа слагаемых в сумме эталона

# Неопределённость округления формата A. `legacy/rebuild_history.py`
# (`sales_by_month`) публикует выручку позиции уже округлённой до РУБЛЯ
# (`round(v[1])`), а количество — до одного знака (`round(v[0], 1)`). «Оборот»
# держит копейки, поэтому даже на идентичных исходных строках сравнение
# «в лоб» даёт до полурубля расхождения на позицию — и это не расхождение
# данных, а разрешение публикации (найдено ревью, discussion_r3877357940).
#
# Величина хранится рядом с числами и вычитается из расхождения при решении
# «сработал ли порог», но НЕ прячется: и текстовый отчёт, и JSON называют её
# отдельно. Спрятать её было бы ровно тем же классом ошибки, что и прощать
# две копейки на 25 млн, только с другой стороны.
FORMAT_A_REV_ROUNDING = 0.5   # ₽ на позицию: round() до рубля
FORMAT_A_QTY_ROUNDING = 0.05  # шт на позицию: round(..., 1)

# Охват возвратов у эталона. «Оборот» грузит и `salesreturn`, и
# `retailsalesreturn` (`app/ms_sync.py::_collect_sales`), а зеркало первой
# таблицы — только `salesreturn` (`legacy/sync.py::sync_sales`, SCHEMA).
# Значит у месяца с розничными возвратами разница по возвратам ОЖИДАЕМА и
# ничего не говорит о качестве загрузки (найдено ревью,
# discussion_r3877357928). Инструмент обязан сказать это структурно, а не
# молча выдать известное расхождение охвата за расхождение данных.
COVERAGE_FULL = "full"                # обе стороны считают одни и те же документы
COVERAGE_LEGACY_PARTIAL = "legacy_partial"  # у эталона нет розничных возвратов
COVERAGE_UNKNOWN = "unknown"          # происхождение эталона не объявлено

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

    # Строки продаж, у которых не нашлось товара ЭТОЙ организации, — отказ
    # закрытым, а не примечание в конце отчёта (найдено ревью,
    # discussion_r3877357936).
    #
    # Внутреннее соединение таких строк не возвращает, поэтому их деньги
    # выпадают из ВСЕХ итогов, включая «сырое нетто». Прежняя версия честно
    # печатала счётчик — и этого мало: «сырое» переставало означать «все
    # продажи организации», а `--fail-on-delta` считался по неполной сумме,
    # то есть прогон мог с одинаковым успехом ложно согласиться с эталоном и
    # ложно с ним разойтись. Ни одно из двух чисел, между которыми выбирает
    # оператор, при этом не было бы верным.
    #
    # Внешнее соединение вместо отказа не годится: у такой строки нет ни
    # базового имени, ни категории, ни признака исключения — её деньги
    # нельзя ни разложить по позициям, ни сопоставить с эталоном. Появился бы
    # итог, который не раскладывается. Отказ честнее: состояние ненормальное
    # (foreign keys в приложении выключены осознанно, `app/db.py`), и
    # разбираться с ним нужно до сверки, а не во время неё.
    orphan_rows = int(total_rows) - joined_rows
    if orphan_rows:
        raise ReconcileError(
            f"у организации id={org_id} за {month} есть {orphan_rows} строк продаж без "
            "товара этой организации: их деньги не попадают ни в один итог, поэтому "
            "сверка не выполняется — сначала разобраться с этими строками")

    return {
        "org_id": org_id,
        "month": month,
        "date_from": date_from,
        "date_to": date_to,
        "totals": totals,
        "bases": bases,
        "rows": int(total_rows),
        "orphan_rows": orphan_rows,
        "archived_bases": sorted(hidden & set(bases)),
    }


# ── эталон ───────────────────────────────────────────────────────────────────

def _tolerance(expected: float, got: float, absolute: float) -> float:
    """Предел, внутри которого две суммы одних и тех же слагаемых считаются равными.

    Никогда не превышает `absolute` — это жёсткий потолок, а не отправная
    точка. На обычных оборотах предел ЗАМЕТНО МЕНЬШЕ потолка: шум сложения
    float64 на порядки мельче копейки, и прощать больше, чем этот шум, не за
    чем. Значение монотонно по величине сравниваемых сумм и на любом масштабе
    остаётся не больше документированного максимума.
    """
    magnitude = max(abs(expected), abs(got))
    if not math.isfinite(magnitude):
        return 0.0  # с не-числом ничто не «совпадает»
    return min(absolute, SUMMATION_STEPS * math.ulp(magnitude))


def _agrees(got: float, expected: float, absolute: float) -> bool:
    if not (math.isfinite(got) and math.isfinite(expected)):
        return False
    return abs(got - expected) <= _tolerance(expected, got, absolute)


def _no_rounding() -> dict:
    """Эталон отдаёт копейки как есть — неопределённости публикации нет."""
    return {"per_base_rev": 0.0, "per_base_qty": 0.0,
            "total_rev": 0.0, "total_qty": 0.0, "source": ""}


def _number(value, where: str) -> float:
    """Конечное число из полезной нагрузки эталона. `True` числом не считается.

    Отдельно отвергаются NaN, ±Infinity и целые вне диапазона float — и это
    не педантизм (найдено ревью, discussion_r3877212862). `json.loads` по
    умолчанию принимает литералы `NaN`, `Infinity` и `-Infinity`, а `1e309`
    молча превращает в бесконечность: дальше все разности становятся `nan`
    или `inf`, отчёт печатает бессмыслицу, а `--json` выдаёт `Infinity`,
    которого в стандартном JSON нет вовсе. Огромное же ЦЕЛОЕ (`10**400`)
    роняло `float()` с `OverflowError` мимо обработчика `ReconcileError` —
    то есть трассировкой вместо обещанного кода возврата 1.

    Всё это один класс ошибки: эталон непригоден. Значит — отказ закрытым.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReconcileError(f"эталон: {where} — ожидалось число, получено {value!r}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:  # int вне диапазона float
        raise ReconcileError(
            f"эталон: {where} — число вне диапазона float ({exc})") from exc
    if not math.isfinite(number):
        raise ReconcileError(
            f"эталон: {where} — ожидалось конечное число, получено {value!r}")
    return number


def parse_reference(payload, month: str) -> dict:
    """Разобрать полезную нагрузку эталона в итоги запрошенного месяца.

    Любое отклонение формата — отказ закрытым. Молча пропустить непонятную
    запись здесь нельзя: пропущенная позиция выглядит как расхождение и
    отправляет расследование не туда.
    """
    month_bounds(month)  # проверка формата запрошенного месяца
    if not isinstance(payload, dict):
        raise ReconcileError("эталон: ожидался JSON-объект верхнего уровня")

    # Каждый служебный формат опознаётся по ДВУМ полям сразу. Одного поля
    # мало: формат A — это словарь имён позиций, и позиция с таким именем
    # хоть и невероятна, но опознание не должно зависеть от её отсутствия.
    # Порядок важен: формат C проверяется первым, потому что он единственный
    # штатный ответ живой ручки, и ошибиться в нём дороже всего.
    if "items" in payload and "summary" in payload:
        return _parse_reference_sales_monthly(payload, month)
    if "bases" in payload and "month" in payload:
        if not isinstance(payload["bases"], dict):
            raise ReconcileError("эталон: поле 'bases' должно быть объектом")
        return _parse_reference_explicit(payload, month)
    return _parse_reference_by_month(payload, month)


# Поля позиции формата C. `net` требуется наравне с остальными: он приходит
# всегда, и если однажды перестанет — это изменение контракта источника, и
# узнать о нём лучше отказом, чем молча посчитанной по-своему выручкой.
_SALES_MONTHLY_ITEM_FIELDS = ("sale_rev", "sale_qty", "ret_rev", "ret_qty", "net")


def _parse_reference_sales_monthly(payload: dict, month: str) -> dict:
    """Штатный ответ `GET /api/sales-monthly?month=YYYY-MM` первой таблицы.

    Единственный формат, который сам отдаёт разрез «валовые/возвраты», —
    ради него он и поддержан напрямую: иначе оператору пришлось бы руками
    перекладывать боевые цифры в промежуточный файл, а это лишний повод их
    где-нибудь сохранить.

    Проверяется строже прочих по двум причинам, названным в докстринге
    модуля: ручка молча подменяет неизвестный ей месяц самым свежим, а
    `summary` считается отдельно от `items` и потому может им противоречить.
    Итоги берутся из СВЁРНУТЫХ позиций, а не из `summary`: отчёт обязан
    раскладываться по позициям, иначе крупнейшие расхождения не сойдутся с
    общим числом. `summary` при этом не игнорируется — он служит контролем.
    """
    ref_month = payload.get("month")
    if not isinstance(ref_month, str) or not MONTH_RE.match(ref_month):
        raise ReconcileError(
            f"эталон: поле 'month' обязано быть YYYY-MM, получено {ref_month!r}")
    if ref_month != month:
        raise ReconcileError(
            f"эталон ответил за {ref_month}, а запрошен {month}: ручка отдаёт самый "
            "свежий месяц, когда запрошенного у неё нет, и делает это тем же HTTP 200")

    months = payload.get("months")
    if months is not None:
        if not isinstance(months, list) or not all(
                isinstance(m, str) and MONTH_RE.match(m) for m in months):
            raise ReconcileError("эталон: поле 'months' должно быть списком месяцев YYYY-MM")
        if month not in months:
            raise ReconcileError(f"эталон: месяца {month} нет среди доступных ему месяцев")

    items = payload.get("items")
    if not isinstance(items, list):
        raise ReconcileError(
            f"эталон: поле 'items' должно быть списком, получено {type(items).__name__}")
    if not items:
        raise ReconcileError(f"эталон: за {month} нет ни одной позиции — сверять не с чем")

    bases: dict[str, dict] = {}
    agg = {"sale_rev": 0.0, "ret_rev": 0.0, "sale_qty": 0.0, "ret_qty": 0.0}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ReconcileError(
                f"эталон: items[{index}] — ожидался объект, получено {type(item).__name__}")
        raw_base = item.get("base")
        if not isinstance(raw_base, str) or not canon_base(raw_base):
            raise ReconcileError(
                f"эталон: items[{index}] — поле 'base' должно быть непустой строкой")
        values = {}
        for field in _SALES_MONTHLY_ITEM_FIELDS:
            if field not in item:
                raise ReconcileError(
                    f"эталон: items[{index}] ({raw_base!r}) — нет поля {field!r}")
            values[field] = _number(item[field], f"items[{index}] ({raw_base!r}) {field}")
        if not _agrees(values["net"], values["sale_rev"] - values["ret_rev"], MONEY_TOLERANCE):
            raise ReconcileError(
                f"эталон противоречит сам себе: у items[{index}] ({raw_base!r}) "
                "net не равен sale_rev − ret_rev")

        # Свёртка по каноническому имени. Ручка и так отдаёт канонические
        # базы, но складывать одноимённые позиции обязан сам инструмент:
        # положиться на то, что дублей не будет, значит однажды посчитать
        # позицию дважды и не заметить.
        cur = bases.setdefault(canon_base(raw_base), {
            "net_qty": 0.0, "net_rev": 0.0, "gross_rev": 0.0, "return_rev": 0.0})
        cur["gross_rev"] += values["sale_rev"]
        cur["return_rev"] += values["ret_rev"]
        cur["net_rev"] += values["sale_rev"] - values["ret_rev"]
        cur["net_qty"] += values["sale_qty"] - values["ret_qty"]
        for field in ("sale_rev", "ret_rev", "sale_qty", "ret_qty"):
            agg[field] += values[field]

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ReconcileError(
            f"эталон: поле 'summary' должно быть объектом, получено {type(summary).__name__}")
    expected = (
        ("sales", agg["sale_rev"], MONEY_TOLERANCE),
        ("returns", agg["ret_rev"], MONEY_TOLERANCE),
        ("net", agg["sale_rev"] - agg["ret_rev"], MONEY_TOLERANCE),
        ("sales_qty", agg["sale_qty"], QTY_TOLERANCE),
        ("returns_qty", agg["ret_qty"], QTY_TOLERANCE),
    )
    for field, value, absolute in expected:
        if field not in summary:
            raise ReconcileError(f"эталон: в summary нет поля {field!r}")
        got = _number(summary[field], f"summary.{field}")
        if not _agrees(got, value, absolute):
            raise ReconcileError(
                f"эталон противоречит сам себе: summary.{field} не сходится с суммой items — "
                f"расхождение {abs(got - value):.6g} при допуске "
                f"{_tolerance(value, got, absolute):.6g}")

    totals = {
        "net_qty": sum(v["net_qty"] for v in bases.values()),
        "net_rev": sum(v["net_rev"] for v in bases.values()),
        "gross_rev": sum(v["gross_rev"] for v in bases.values()),
        "return_rev": sum(v["return_rev"] for v in bases.values()),
    }
    return {
        "month": month, "shape": "sales_monthly", "bases": bases, "totals": totals,
        # Копейки эталон здесь не режет — округления публикации нет.
        "rounding": _no_rounding(),
        # Но источник тот же: у зеркала первой таблицы розничных возвратов нет.
        "returns_coverage": COVERAGE_LEGACY_PARTIAL,
    }


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
    # Формат A публикует выручку позиции округлённой до рубля — неопределённость
    # известна заранее и переносится в отчёт вместе с числами.
    return {
        "month": month, "shape": "sales_by_month", "bases": bases, "totals": totals,
        "rounding": {"per_base_rev": FORMAT_A_REV_ROUNDING,
                     "per_base_qty": FORMAT_A_QTY_ROUNDING,
                     "total_rev": FORMAT_A_REV_ROUNDING * len(bases),
                     "total_qty": FORMAT_A_QTY_ROUNDING * len(bases),
                     "source": "формат A округляет выручку позиции до рубля"},
        # Тот же источник, что и у формата C: зеркало первой таблицы, где
        # розничных возвратов нет вовсе.
        "returns_coverage": COVERAGE_LEGACY_PARTIAL,
    }


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
        net_rev = _number(item["net_rev"], f"{raw_base!r} net_rev")
        cur["net_qty"] += _number(item["net_qty"], f"{raw_base!r} net_qty")
        cur["net_rev"] += net_rev
        gross_rev = return_rev = None
        if "gross_rev" in item:
            gross_rev = _number(item["gross_rev"], f"{raw_base!r} gross_rev")
            cur["gross_rev"] += gross_rev
        else:
            has_gross = False
        if "return_rev" in item:
            return_rev = _number(item["return_rev"], f"{raw_base!r} return_rev")
            cur["return_rev"] += return_rev
        else:
            has_return = False
        # Разрез, который сам себе противоречит, — не разрез (найдено ревью,
        # discussion_r3877357945). Ровно ради классификации «возвраты сошлись,
        # разница в валовых» этот формат и существует; если net не равен
        # gross − return, отчёт печатает нетто-расхождение, противоречащее
        # собственному разрезу, — то есть отвечает не на тот вопрос, ради
        # которого его читают. Допуск тот же ограниченный, что и у живой ручки.
        if gross_rev is not None and return_rev is not None:
            if not _agrees(net_rev, gross_rev - return_rev, MONEY_TOLERANCE):
                raise ReconcileError(
                    f"эталон противоречит сам себе: у позиции {raw_base!r} "
                    "net_rev не равен gross_rev − return_rev")

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
    # Происхождение этого формата инструменту неизвестно: его готовит человек.
    # Поэтому охват возвратов по умолчанию НЕ считается доказанным, но может
    # быть объявлен явно — тогда классификация возвратов сравнима.
    declared = payload.get("returns_coverage", COVERAGE_UNKNOWN)
    if declared not in (COVERAGE_FULL, COVERAGE_LEGACY_PARTIAL, COVERAGE_UNKNOWN):
        raise ReconcileError(
            f"эталон: неизвестное значение 'returns_coverage': {declared!r}")
    return {
        "month": month, "shape": "explicit", "bases": bases, "totals": totals,
        "rounding": _no_rounding(),
        "returns_coverage": declared,
    }


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
    # Таймаут проверяется ЗДЕСЬ, а не только в `run`: функция публичная, и
    # вызвать её мимо CLI ничто не мешает — та же причина, что у `exceeds`.
    timeout = check_timeout(timeout)
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
    # OverflowError здесь не теоретический: на негодном таймауте его бросает
    # сам слой сокетов. Таймаут уже проверен выше, но перечень оставлен
    # полным — отказ закрытым не должен зависеть от того, что проверка выше
    # осталась на месте.
    except (urllib.error.URLError, OSError, ValueError, OverflowError) as exc:
        raise ReconcileError(f"эталон: {url} недоступен: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReconcileError(f"эталон: {url} вернул не JSON: {exc}") from exc
    return parse_reference(payload, month)


# ── сравнение ────────────────────────────────────────────────────────────────

def _excess(delta, allowance: float) -> float:
    """Часть расхождения, которую НЕЛЬЗЯ объяснить заявленной неопределённостью.

    Ноль означает «целиком объяснимо», а не «расхождения нет»: само
    расхождение остаётся в отчёте отдельным числом. Скидка применяется только
    к решению «сработал ли порог» — и только на ту величину, которая честно
    вытекает из округления публикации эталона.
    """
    if delta is None:
        return 0.0
    return max(0.0, abs(delta) - max(0.0, float(allowance)))


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

    # Неопределённость публикации эталона и та часть расхождения, которую ею
    # объяснить НЕЛЬЗЯ. Порог `--fail-on-delta` смотрит именно на вторую: у
    # формата A обе стороны могут иметь одни и те же исходные строки и всё
    # равно разойтись на полурубль по позиции, и объявлять это расхождением
    # значит поднимать ложную тревогу. Прятать её при этом нечем: и число, и
    # его происхождение печатаются рядом.
    rounding = reference.get("rounding") or _no_rounding()
    deltas["net_rev_excess"] = _excess(deltas["net_rev"], rounding["total_rev"])
    deltas["net_qty_excess"] = _excess(deltas["net_qty"], rounding["total_qty"])

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
            # Часть расхождения, объяснимая округлением публикации эталона:
            # у позиции, которой у эталона нет, объяснять нечего.
            "rounding_rev": rounding["per_base_rev"] if right else 0.0,
            "rounding_qty": rounding["per_base_qty"] if right else 0.0,
        })
    for item in per_base:
        item["delta_rev_excess"] = _excess(item["delta_rev"], item["rounding_rev"])
        item["delta_qty_excess"] = _excess(item["delta_qty"], item["rounding_qty"])
        item["within_reference_rounding"] = item["delta_rev_excess"] == 0.0

    # Детерминированный порядок: по НЕОБЪЯСНЁННОЙ части расхождения, затем по
    # модулю расхождения, затем по имени. Ранжировать по сырому расхождению
    # значило бы поднимать наверх позиции, вся разница которых — округление
    # публикации эталона (найдено ревью, discussion_r3877357940).
    ranked = sorted(per_base, key=lambda it: (-it["delta_rev_excess"],
                                              -abs(it["delta_rev"]), it["base_name"]))
    top_n = max(0, int(top))

    # Охват возвратов. «Оборот» грузит и обычные, и РОЗНИЧНЫЕ возвраты, а
    # зеркало первой таблицы — только обычные. Значит на месяце с розничными
    # возвратами строки «валовые» и «возвраты» у двух сторон считают разные
    # множества документов, и разность по ним — известная разница охвата, а не
    # вывод о качестве загрузки. Инструмент говорит это структурно и не
    # выдаёт классификацию за доказанную (discussion_r3877357928).
    coverage = reference.get("returns_coverage", COVERAGE_UNKNOWN)
    comparable = coverage == COVERAGE_FULL
    notes = []
    if not comparable:
        notes.append(
            "Возвраты сторон посчитаны по РАЗНЫМ множествам документов: «Оборот» "
            "берёт salesreturn и retailsalesreturn, зеркало первой таблицы — только "
            "salesreturn. "
            + ("Охват эталона не объявлен, поэтому равным он не считается. "
               if coverage == COVERAGE_UNKNOWN else "")
            + "Строки «валовые» и «возвраты» поэтому справочные: классифицировать "
              "расхождение как «возвраты сошлись, разница в валовых» по ним нельзя, "
              "пока охват не выровнен или не доказан.")
    if rounding["total_rev"]:
        notes.append(
            f"{rounding['source']}: до {rounding['per_base_rev']:g} ₽ на позицию и до "
            f"{rounding['total_rev']:g} ₽ на итог — это неопределённость публикации "
            "эталона, а не расхождение данных. Порог --fail-on-delta смотрит на "
            "необъяснённую ею часть.")

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
        "reference_rounding": dict(rounding),
        "returns_coverage": coverage,
        # Сравнимы ли строки «валовые» и «возвраты» между сторонами. Ложь
        # означает не «числа неверны», а «их разность включает известную
        # разницу охвата документов», то есть классифицировать по ней нельзя.
        "returns_comparable": comparable,
        "scope_notes": list(SCOPE_NOTES) + notes,
    }


def check_threshold(threshold):
    """Проверить порог `--fail-on-delta` или отказать закрытым.

    Найдено ревью (discussion_r3877212859): `argparse` с `type=float`
    принимает `nan` и отрицательные числа, а дальше единственный явный
    предохранитель инструмента ломается в обе стороны сразу. С `nan` любое
    сравнение ложно — гейт молча ВЫКЛЮЧАЕТСЯ и любое расхождение отдаёт 0.
    С отрицательным порогом сравнение истинно всегда — гейт ИНВЕРТИРУЕТСЯ и
    даже нулевое расхождение отдаёт 2. Порог, который сам себя отключает или
    выворачивает, хуже отсутствующего: на него полагаются.
    """
    if threshold is None:
        return None
    value = float(threshold)
    if not math.isfinite(value):
        raise ReconcileError(
            f"--fail-on-delta должен быть конечным числом, получено {threshold!r}")
    if value < 0:
        raise ReconcileError(
            f"--fail-on-delta не может быть отрицательным, получено {threshold!r}")
    return value


def check_timeout(timeout):
    """Таймаут запроса эталона: конечный и строго положительный, иначе отказ.

    Найдено ревью (discussion_r3877540672). `argparse` с `type=float`
    принимает `inf`, а `urlopen` на нём падает `OverflowError: timestamp out
    of range for platform time_t` — и этот класс исключения не ловился
    обработчиком, то есть вместо обещанного «ОТКАЗ» и кода 1 инструмент
    выдавал трассировку. Тот же дефект, что у `--fail-on-delta nan`, только с
    другой стороны: негодное число доезжает до места, где его уже некому
    проверить.

    Ноль и отрицательные тоже отвергаются: «ждать не дольше нуля секунд» —
    это не таймаут, а другое поведение (неблокирующий сокет), и просить его
    ключом с таким именем нельзя.
    """
    value = float(timeout)
    if not math.isfinite(value):
        raise ReconcileError(f"--timeout должен быть конечным числом, получено {timeout!r}")
    if value <= 0:
        raise ReconcileError(f"--timeout должен быть больше нуля, получено {timeout!r}")
    return value


def exceeds(report: dict, threshold) -> bool:
    """Расхождение по выбранной базе строго больше порога? Порога нет — нет.

    Порог здесь уже проверенный (`check_threshold`), но проверка повторена и
    тут: функция публичная, и вызвать её мимо CLI ничто не мешает.
    """
    value = check_threshold(threshold)
    if value is None:
        return False
    # Сравнивается НЕОБЪЯСНЁННАЯ часть расхождения: та, которую не покрывает
    # округление публикации эталона. У форматов B и C неопределённость нулевая,
    # и это ровно прежнее поведение; у формата A иначе `--fail-on-delta 0`
    # срабатывал бы на идентичных исходных строках (discussion_r3877357940).
    delta = report["deltas"].get("net_rev_excess", report["deltas"]["net_rev"])
    if not math.isfinite(delta):
        raise ReconcileError("расхождение не является конечным числом")
    return abs(delta) > value


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
        f"   эталон {_money(t['reference_gross_rev'])}   Δ {_money(d['gross_rev_raw'])}"
        f"{'' if report['returns_comparable'] else '   [охват возвратов разный — справочно]'}",
        f"  возвраты             «Оборот» {_money(t['saas_return_rev'])}"
        f"   эталон {_money(t['reference_return_rev'])}   Δ {_money(d['return_rev_raw'])}"
        f"{'' if report['returns_comparable'] else '   [охват возвратов разный — справочно]'}",
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
    if report["reference_rounding"]["total_rev"]:
        lines.append(
            f"  неопределённость публикации эталона: до "
            f"{_money(report['reference_rounding']['total_rev'])} на итог; "
            f"необъяснённая ею часть расхождения: {_money(d['net_rev_excess'])}")
    if report["top_base_deltas"]:
        lines.append("")
        lines.append("  Крупнейшие расхождения по позициям:")
        for item in report["top_base_deltas"]:
            where = "обе" if item["in_saas"] and item["in_reference"] else (
                "только «Оборот»" if item["in_saas"] else "только эталон")
            mark = "  [в пределах округления эталона]" if (
                item["within_reference_rounding"] and item["delta_rev"]) else ""
            lines.append(
                f"    {_money(item['delta_rev']):>18}   {item['base_name']}   "
                f"(«Оборот» {_money(item['saas_net_rev'])}, эталон {_money(item['reference_net_rev'])}, {where}){mark}")
    lines.append("")
    for note in report["scope_notes"]:
        lines.append(f"  ! {note}")
    return "\n".join(lines)


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)


# ── командная строка ─────────────────────────────────────────────────────────

class _Parser(argparse.ArgumentParser):
    """Парсер, у которого ошибка вызова — это отказ, а не «расхождение».

    Найдено ревью (discussion_r3877212853). `argparse.error()` завершает
    процесс кодом 2, а код 2 у этого инструмента ЗАНЯТ и означает совсем
    другое: «сверка выполнена, и расхождение больше порога». Автоматика,
    которая по коду 2 заводит расследование, на опечатке в вызове
    (`--org не-число`, забытый обязательный ключ) заводила бы его на пустом
    месте — и наоборот, приняла бы сломанный вызов за содержательный ответ.
    Ошибка разбора превращается в `ReconcileError`, то есть в код 1.

    `--help` этим не затронут: он идёт через `exit()`, а не через `error()`,
    и по-прежнему печатает справку и завершается нулём.
    """

    def error(self, message):  # noqa: A003 — имя задано базовым классом
        raise ReconcileError(f"неверный вызов: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
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
    conn = None
    try:
        # Разбор аргументов ВНУТРИ try: ошибка вызова обязана стать кодом 1,
        # а не кодом 2, который занят подтверждённым расхождением.
        args = build_parser().parse_args(argv)
        if args.top < 0:
            raise ReconcileError("--top не может быть отрицательным")
        threshold = check_threshold(args.fail_on_delta)
        # Таймаут проверяется всегда, а не только при `--reference-url`:
        # бессмысленное значение — это сломанный вызов независимо от того,
        # дойдёт ли дело до сети.
        check_timeout(args.timeout)
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
    return EXIT_DELTA if exceeds(report, threshold) else EXIT_OK


if __name__ == "__main__":
    sys.exit(run())
