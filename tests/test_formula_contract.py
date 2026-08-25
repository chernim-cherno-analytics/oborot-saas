# -*- coding: utf-8 -*-
"""Контракт формул потребности и largest-remainder (DATA-11).

ЧТО ЭТО. Замок на текущую семантику четырёх формул. Он ничего не исправляет
и ничего не улучшает: он фиксирует то, что код делает СЕГОДНЯ, — включая
расхождения, описанные в BUSINESS_LOGIC §9. Смысл замка в том, что после него
любая правка формулы перестаёт быть молчаливой: она обязана уронить этот набор
и объясниться в DECISIONS.md (D-35, AGENTS.md §1).

ПОЧЕМУ ЗАМОК, А НЕ СВЕДЕНИЕ. Потребность считается двумя формулами, а метод
наибольших остатков реализован трижды. Соблазн «убрать дубль» велик, но у
реализаций РАЗНАЯ семантика (нулевые веса, ключ тай-брейка), и любое сведение
изменило бы числа на экране. Формулы не меняются никем, кроме владельца, —
поэтому первым шагом идёт guard, а не рефактор.

ЧТО ЗАКРЕПЛЕНО (и где живёт оригинал):

  1. Аналитика, `app/analytics.py`:
         proj_stock = max(0.0, cs + ordered − rate × lead)
         need       = max(0, round(rate × horizon) − round(proj_stock))
     Проверяется НА ЖИВОМ ПУТИ: `analytics.get_snapshot` (то есть
     `_compute_snapshot`) → `analytics.build_replenish`, на настоящей БД.

  2. Мастер заказа, `app/order_planner.py`:
         proj_stock = max(0.0, cs + ordered − r_lead × days_to_eta)
         need       = max(0, int(round(r_cover × cover − proj_stock)))
     Проверяется через настоящий `order_planner.plan_order`.

  3. `analytics.size_split` — при нулевых весах делит ПОРОВНУ,
     тай-брейк по ДВУМ ключам (остаток, вес).

  4. `analytics_extra._largest_remainder` — при нулевых весах отдаёт НУЛИ,
     тай-брейк по ОДНОМУ ключу (остаток).

ПРО РАЗНИЦУ НА 1. На одном и том же входе две формулы потребности дают 14 и 15.
Это НЕ «правильное» и «неправильное» число: это известное текущее поведение,
описанное в BUSINESS_LOGIC §9.4 («сам порядок округления расходится на
2 позициях из 42 и ровно на 1 шт»). Набор закрепляет его как факт кода, чтобы
разница не выросла и не исчезла незаметно. Каким темпом мастер обязан считать
заказ — вопрос владельца, а не этого набора.

ЧЕГО ЗДЕСЬ НЕТ НАМЕРЕННО. Поведение при `None`/отсутствующем значении не
закрепляется: планировщик приводит `None` к нулю (`float(… or 0)`), а D-34
требует не выдавать отсутствие факта за число. Это смысловое противоречие,
и решать его правкой продакшн-кода — не дело test-only набора. Вынесено в
отдельный вопрос очереди. Так же вне набора: выбор темпа для мастера (§9.4),
ручная ростовка против бюджета (§9.8), единый порядок размеров (§9.7) и
разница `round()` Python против `Math.round()` JS (§9.12) — это развилки
владельца, а не то, что тест вправе решить за него.

ЧЕСТНАЯ РАМКА RED/GREEN. Набор характеризует текущее поведение, поэтому на
неизменённом коде он зелёный ПО ПОСТРОЕНИЮ — выдавать его за «сначала красный»
нельзя. Способность ловить доказывается отдельно: `tests/check_formula_mutations.py`
по одной вносит в КОПИЮ настоящего продакшн-кода ровно те правки, ради которых
замок писался, и каждая обязана уронить этот файл.

Запуск из корня репозитория:  python tests/test_formula_contract.py
"""
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_formula_contract.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

for _p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
    if _p.exists():
        _p.unlink()

from app import analytics, analytics_extra, order_planner as op  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Org, Product, Sale, StockDay  # noqa: E402

Base.metadata.create_all(engine)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond, detail: str = "") -> bool:
    """Проверка. cond — значение ИЛИ функция без аргументов.

    Функцию удобно передавать там, где мутация продакшн-кода способна не
    вернуть неверное число, а бросить исключение (снятый fallback деления
    поровну даёт ZeroDivisionError). Упавшее исключение — это провал
    проверки, а не крах набора: набор обязан дописать отчёт до конца,
    иначе раннер по D-42 не сможет отличить его от оборвавшегося.
    """
    try:
        ok = bool(cond() if callable(cond) else cond)
    except Exception as exc:  # noqa: BLE001 — исключение и есть результат
        ok, detail = False, f"{detail} исключение: {exc!r}".strip()
    print(("  OK   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    (PASSED if ok else FAILED).append(name)
    return ok


def block(title: str, fn) -> None:
    """Блок проверок. Исключение внутри — один провал, а не конец набора."""
    print(f"\n{title}")
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        check(f"{title} — блок дошёл до конца", False, f"исключение: {exc!r}")


# ── Доказательство живого пути ───────────────────────────────────────────────
#
# Тест обязан проверять формулу ТАМ, ГДЕ ОНА ЖИВЁТ, а не свою копию рядом.
# Копия сошлась бы сама с собой и осталась бы зелёной ровно в тот день, когда
# продакшн-формулу поменяли, — то есть была бы не замком, а его имитацией.
#
# Доказательства здесь два, и они независимы:
#   1) построчная трассировка: строка формулы в app/*.py реально исполнилась
#      во время вызова (ниже);
#   2) матрица мутаций: правка продакшн-файла роняет этот набор
#      (tests/check_formula_mutations.py). Если бы набор проверял копию,
#      мутация продакшн-кода оставила бы его зелёным.

def _sole_line(path: Path, needle: str) -> int | None:
    """Номер ЕДИНСТВЕННОЙ строки файла с needle; None — если не ровно одна.

    Ноль совпадений или больше одного — это не «не нашли», это изменившийся
    код: формулу переписали или продублировали. И то и другое обязано быть
    замечено, поэтому None здесь превращается в провал проверки, а не в
    молчаливый пропуск.
    """
    nums = [
        i
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if needle in line
    ]
    return nums[0] if len(nums) == 1 else None


def _trace(module_file: str, fn):
    """Выполнить fn() и вернуть (результат, множество исполненных строк файла).

    Трассируются построчно ТОЛЬКО кадры нужного файла: глобальный обработчик
    возвращает None для всех остальных, и накладные расходы остаются в пределах
    самого модуля (иначе трассировка SQLAlchemy сделала бы набор неприлично
    долгим).
    """
    hit: set[int] = set()
    target = os.path.realpath(module_file)

    def _local(frame, event, arg):
        if event == "line":
            hit.add(frame.f_lineno)
        return _local

    def _global(frame, event, arg):
        if event == "call" and os.path.realpath(frame.f_code.co_filename) == target:
            return _local
        return None

    prev = sys.gettrace()
    sys.settrace(_global)
    try:
        result = fn()
    finally:
        sys.settrace(prev)
    return result, hit


ANALYTICS_PY = Path(analytics.__file__).resolve()
PLANNER_PY = Path(op.__file__).resolve()

# Ровно те строки, которые набор и запирает. Ищутся по тексту, а не по номеру:
# номер поедет от любой правки выше по файлу, а замок обязан переживать правки,
# которые его не касаются.
NEED_LINE_ANALYTICS = 'item["need"] = max(0, round(rate * horizon) - round(proj_stock))'
PROJ_LINE_ANALYTICS = 'proj_stock = max(0.0, cs + float(item["ordered"]) - rate * lead)'
NEED_LINE_PLANNER = "need = max(0, int(round(r_cover * cover - proj_stock)))"
PROJ_LINE_PLANNER = "proj_stock = max(0.0, cs + ordered - r_lead * days_to_eta)"


# ── Сцена для аналитики: настоящая БД, настоящий снапшот ─────────────────────

TODAY = date.today()
BASE = "Рубашка Контракт"

# Горизонт 45 = ритм 30 + страховка 15 (analytics.cover_days, режим 'cadence');
# срок производства 45. Оба числа выбраны так, чтобы темп 0,5 шт/день вывел
# обе формулы ровно на границу «.5» — единственный класс входов, где порядок
# округления вообще что-то меняет.
HORIZON = 45
LEAD = 45
ORG_SETTINGS = {
    "thresholds": {"weak": 1000, "dull": 2000, "good": 5000},
    "horizon_days": 90,
    "min_stock_days": 3,
    "rate_window": "year",
    "lead_time_days": LEAD,
    "cover_mode": "cadence",
    "order_cadence_days": 30,
    "safety_days": 15,
}


def iso(days_ago: int) -> str:
    return (TODAY - timedelta(days=days_ago)).isoformat()


def make_org(db, name: str, *, cs: float, sold: int) -> Org:
    """Организация с ОДНОЙ позицией и заранее известным темпом.

    Дней в стоке ровно два: сегодня (остаток cs — он же станет item['cs'],
    потому что аналитика берёт текущий остаток на последней дате) и вчера
    (остаток 5). Порог глубины по базе — 3, поэтому оба дня засчитываются,
    а других дат у организации нет. Отсюда rate_year = sold / 2: при sold = 1
    темп ровно 0,5 шт/день, и это точное двоичное число — округление в самом
    темпе ничего не портит и в проверку не вмешивается.
    """
    org = Org(name=name, settings_json=json.dumps(ORG_SETTINGS))
    db.add(org)
    db.flush()
    prod = Product(org_id=org.id, base_name=BASE, size="M", category="Рубашки",
                   sale_price=1000, cost_price=300)
    db.add(prod)
    db.flush()
    db.add(StockDay(org_id=org.id, product_id=prod.id, date=iso(0), qty=float(cs)))
    db.add(StockDay(org_id=org.id, product_id=prod.id, date=iso(1), qty=5.0))
    for _ in range(sold):
        db.add(Sale(org_id=org.id, product_id=prod.id, date=iso(1),
                    qty=1, revenue=1000, is_return=False))
    db.commit()
    return org


# ── Сцена для мастера: чистая функция plan_order ─────────────────────────────
#
# plan_order в БД не ходит (её докстринг это и обещает), поэтому «живой путь»
# для мастера — это прямой вызов самой plan_order на синтетическом снапшоте.
# Идиома mk_snap/mk_ctx/mk_brief взята из tests/test_planner.py.

PLANNER_SETTINGS = {"order_cadence_days": 30, "safety_days": 15, "moq_units": 0,
                    "reserve_new_pct": 0, "lead_time_days": LEAD}
ONE_STAGE = op.normalize_stages(None, LEAD)


def mk_snap(*, cs: float) -> dict:
    return {
        "today": TODAY.isoformat(),
        "settings": {"min_stock_days": 3, "horizon_days": HORIZON,
                     "cover_days": HORIZON, "lead_time_days": LEAD,
                     "order_cadence_days": 30, "safety_days": 15},
        "items": {
            BASE: {
                "base_name": BASE, "category": "Рубашки", "cls": "good",
                "turnover": 5000.0, "rate_year": 0.5, "cs": cs, "ordered": 0,
                "cost_price": 300, "avg_price": 1000, "sale_price": 1000,
                "archived": False, "hidden": False, "low_data": False,
                "nq": 150, "dis": 300,
                "sizes": {"S": {"stock": 0, "sold365": 10},
                          "M": {"stock": cs, "sold365": 20}},
            }
        },
    }


def mk_ctx(*, r_lead: float, r_cover: float) -> dict:
    return {
        "cover_days": HORIZON,
        "rate_lead": {BASE: r_lead},
        "rate_cover": {BASE: r_cover},
        "fresh": {BASE},
        "assign": {},
        "main_production_id": 1,
    }


def mk_brief(*, must: bool = False) -> dict:
    raw = {
        "eta_date": (TODAY + timedelta(days=LEAD)).isoformat(),
        "budget": 300000,
        "budget_scope": "full",
        "must_have": [BASE] if must else [],
    }
    brief = op.normalize_brief(raw, PLANNER_SETTINGS, ONE_STAGE, TODAY)
    brief["production_id"] = None
    return brief


def plan_need(plan: dict):
    """Потребность позиции так, как её называет сам план.

    Смотрим в оба списка: позиция с нулевой потребностью в заказ не попадает
    и живёт в not_included. Если мутация снимет пол, она окажется там же —
    но с отрицательным числом, и именно это надо увидеть.
    """
    for key in ("items", "not_included"):
        for row in plan.get(key) or []:
            if row.get("base_name") == BASE:
                return row.get("need")
    return None


def main() -> int:
    db = SessionLocal()

    # ── 1. Аналитика: живой путь ─────────────────────────────────────────────
    #
    # Сцена: темп 0,5 шт/день, горизонт 45, срок 45, остаток 30.
    #   proj_stock = max(0, 30 − 0,5×45) = 7,5
    #   need       = max(0, round(22,5) − round(7,5)) = 22 − 8 = 14
    # Обе половины попадают ровно на «.5», где round() в Python округляет
    # к чётному: 22,5 → 22, но 7,5 → 8. Ровно поэтому порядок округления
    # здесь виден, а на «обычных» входах — нет.
    org_a = make_org(db, "contract-tie", cs=30, sold=1)

    def _snapshot():
        return analytics.get_snapshot(db, org_a)

    # Организация свежая, кэш снапшотов по ней пуст — значит вызов гарантированно
    # доходит до _compute_snapshot, а не возвращает готовое из кэша.
    snap_a, hit_a = _trace(str(ANALYTICS_PY), _snapshot)
    item_a = snap_a["items"][BASE]

    def _live_analytics():
        line_need = _sole_line(ANALYTICS_PY, NEED_LINE_ANALYTICS)
        line_proj = _sole_line(ANALYTICS_PY, PROJ_LINE_ANALYTICS)
        check("формула потребности лежит в app/analytics.py ровно в одном месте",
              line_need is not None)
        check("формула прогнозного остатка лежит в app/analytics.py ровно в одном месте",
              line_proj is not None)
        # Если трассировка не увидела НИ ОДНОЙ строки модуля — сломана она сама,
        # и об этом надо сказать отдельно, а не списать на формулу.
        check("трассировка вообще наблюдала app/analytics.py", bool(hit_a),
              "ни одной строки — доказательство живого вызова недоступно")
        check("строка потребности аналитики ИСПОЛНИЛАСЬ (живой путь, не копия)",
              line_need is not None and line_need in hit_a,
              f"строка {line_need} не в числе исполненных")
        check("строка прогнозного остатка аналитики ИСПОЛНИЛАСЬ",
              line_proj is not None and line_proj in hit_a,
              f"строка {line_proj} не в числе исполненных")

    block("1. Аналитика: формула исполняется на живом пути", _live_analytics)

    def _analytics_numbers():
        check("темп собран из данных ровно как 0,5 шт/день (nq/dis365)",
              item_a["rate_year"] == 0.5, f"rate_year={item_a['rate_year']}")
        check("прогнозный остаток = max(0, cs + ordered − темп×срок) = 8 (7,5)",
              item_a["proj_stock"] == 8, f"proj_stock={item_a['proj_stock']}")
        check("потребность = round(темп×горизонт) − round(остаток) = 22 − 8 = 14",
              item_a["need"] == 14, f"need={item_a['need']}")
        check("потребность — целое, а не float",
              type(item_a["need"]) is int, f"тип {type(item_a['need']).__name__}")

    block("2. Аналитика: порядок округления закреплён как есть", _analytics_numbers)

    def _replenish():
        rep = analytics.build_replenish(snap_a)
        row = next((r for r in rep["items"] if r["base_name"] == BASE), None)
        check("позиция дошла до ответа /api/replenish", row is not None)
        check("страница «Заказ» показывает ТУ ЖЕ потребность, что и снапшот",
              row is not None and row["need"] == item_a["need"] == 14,
              f"row={row['need'] if row else None} snap={item_a['need']}")
        check("горизонт ответа — эффективный горизонт покрытия (45)",
              rep["horizon_days"] == HORIZON, f"horizon={rep['horizon_days']}")
        # Ростовка в ответе собирается size_split'ом — тем самым, чей контракт
        # запирается ниже. Сумма по размерам обязана сойтись с потребностью.
        check("ростовка ответа складывается ровно в потребность",
              row is not None
              and sum(s["rec"] for s in row["sizes"].values()) == row["need"],
              f"sizes={row['sizes'] if row else None}")

    block("3. Аналитика: живой путь продолжается в build_replenish", _replenish)

    def _analytics_floor():
        # Нулевой темп: позиция без продаж. Потребность обязана быть нулём,
        # а не отрицательным числом (−10 на экране «Заказ» означало бы
        # «сдайте товар обратно»).
        org_zero = make_org(db, "contract-zero", cs=10, sold=0)
        snap_zero = analytics.get_snapshot(db, org_zero)
        it_zero = snap_zero["items"][BASE]
        check("нет продаж → темп 0", it_zero["rate_year"] == 0.0,
              f"rate_year={it_zero['rate_year']}")
        check("нет продаж → потребность 0, а не отрицательная",
              it_zero["need"] == 0, f"need={it_zero['need']}")

        # Затоварка: прогнозный остаток больше, чем закроет горизонт.
        #   proj = max(0, 1000 − 22,5) = 977,5 → round = 978
        #   need = max(0, 22 − 978) = 0     (без пола было бы −956)
        org_over = make_org(db, "contract-floor", cs=1000, sold=1)
        snap_over = analytics.get_snapshot(db, org_over)
        it_over = snap_over["items"][BASE]
        check("затоварка: прогнозный остаток 978 (977,5)",
              it_over["proj_stock"] == 978, f"proj_stock={it_over['proj_stock']}")
        check("затоварка: пол max(0, …) держит потребность на нуле",
              it_over["need"] == 0, f"need={it_over['need']}")

        rep_over = analytics.build_replenish(snap_over)
        check("затоваренная позиция уходит в excluded, а не в заказ",
              all(r["base_name"] != BASE for r in rep_over["items"])
              and any(r["base_name"] == BASE for r in rep_over["excluded"]))

    block("4. Аналитика: ноль и пол", _analytics_floor)

    # ── 5. Мастер: живой путь через plan_order ───────────────────────────────
    #
    # Те же числа, что у аналитики: темп 0,5, покрытие 45, до прихода 45,
    # остаток 30.
    #   proj_stock = max(0, 30 − 0,5×45) = 7,5
    #   need       = max(0, int(round(22,5 − 7,5))) = int(round(15,0)) = 15
    snap_m = mk_snap(cs=30)
    ctx_m = mk_ctx(r_lead=0.5, r_cover=0.5)
    brief_m = mk_brief()

    def _plan():
        return op.plan_order(snap_m, brief_m, ctx_m, ONE_STAGE, with_sensitivity=False)

    plan_m, hit_p = _trace(str(PLANNER_PY), _plan)

    def _live_planner():
        line_need = _sole_line(PLANNER_PY, NEED_LINE_PLANNER)
        line_proj = _sole_line(PLANNER_PY, PROJ_LINE_PLANNER)
        check("формула потребности лежит в app/order_planner.py ровно в одном месте",
              line_need is not None)
        check("формула прогнозного остатка лежит в app/order_planner.py ровно в одном месте",
              line_proj is not None)
        check("трассировка вообще наблюдала app/order_planner.py", bool(hit_p),
              "ни одной строки — доказательство живого вызова недоступно")
        check("строка потребности мастера ИСПОЛНИЛАСЬ (живой путь, не копия)",
              line_need is not None and line_need in hit_p,
              f"строка {line_need} не в числе исполненных")
        check("строка прогнозного остатка мастера ИСПОЛНИЛАСЬ",
              line_proj is not None and line_proj in hit_p,
              f"строка {line_proj} не в числе исполненных")

    block("5. Мастер: формула исполняется на живом пути", _live_planner)

    def _planner_numbers():
        need_m = plan_need(plan_m)
        check("мастер: потребность = int(round(темп×покрытие − остаток)) = 15",
              need_m == 15, f"need={need_m}")
        check("мастер: потребность — целое, а не float",
              type(need_m) is int, f"тип {type(need_m).__name__}")
        row = next((r for r in plan_m["items"] if r["base_name"] == BASE), None)
        check("мастер: прогнозный остаток = 8 (7,5), тот же вход, что у аналитики",
              row is not None and row["proj_stock"] == 8,
              f"proj_stock={row['proj_stock'] if row else None}")

    block("6. Мастер: порядок округления закреплён как есть", _planner_numbers)

    def _cover_rate():
        # Мастер считает потребность темпом ПОКРЫТИЯ (r_cover), а прогнозный
        # остаток — темпом ДО ПРИХОДА (r_lead). Это два разных числа
        # (сезонный индекс, §4), и подмена одного другим обязана быть заметна.
        #   proj = max(0, 30 − 0,5×45) = 7,5      ← r_lead
        #   need = max(0, int(round(1,0×45 − 7,5))) = int(round(37,5)) = 38
        # Если r_cover подменить на r_lead, получится 15 — сцена различает.
        plan_two = op.plan_order(mk_snap(cs=30), mk_brief(),
                                 mk_ctx(r_lead=0.5, r_cover=1.0), ONE_STAGE,
                                 with_sensitivity=False)
        need_two = plan_need(plan_two)
        check("потребность считается темпом ПОКРЫТИЯ: 38, а не 15",
              need_two == 38, f"need={need_two}")
        row = next((r for r in plan_two["items"] if r["base_name"] == BASE), None)
        check("прогнозный остаток считается темпом ДО ПРИХОДА: 8 (7,5)",
              row is not None and row["proj_stock"] == 8,
              f"proj_stock={row['proj_stock'] if row else None}")

    block("7. Мастер: r_cover и r_lead не взаимозаменяемы", _cover_rate)

    def _planner_floor():
        # Позиция помечена «Взять» — иначе мизерная потребность отсеется
        # раньше, чем её число попадёт в план, и пол проверить будет нечем.
        plan_zero = op.plan_order(mk_snap(cs=10), mk_brief(must=True),
                                  mk_ctx(r_lead=0.0, r_cover=0.0), ONE_STAGE,
                                  with_sensitivity=False)
        need_zero = plan_need(plan_zero)
        check("мастер: нулевой темп → потребность 0, а не −10",
              need_zero == 0, f"need={need_zero}")

        # Затоварка: 22,5 − 977,5 = −955 до пола.
        plan_over = op.plan_order(mk_snap(cs=1000), mk_brief(must=True),
                                  mk_ctx(r_lead=0.5, r_cover=0.5), ONE_STAGE,
                                  with_sensitivity=False)
        need_over = plan_need(plan_over)
        check("мастер: затоварка → потребность 0, а не −955",
              need_over == 0, f"need={need_over}")

    block("8. Мастер: ноль и пол", _planner_floor)

    def _known_gap():
        # BUSINESS_LOGIC §9.4: «сам порядок округления расходится на
        # 2 позициях из 42 и ровно на 1 шт». Здесь — тот самый вход, на
        # котором это происходит. Ни одно из двух чисел не объявляется
        # правильным: закрепляется факт, что их два и разница ровно 1.
        # Сведение формул изменит числа на экране и требует решения
        # владельца (D-35), а не правки этого файла.
        need_analytics = item_a["need"]
        need_master = plan_need(plan_m)
        check("на одном входе аналитика даёт 14, мастер — 15",
              (need_analytics, need_master) == (14, 15),
              f"аналитика={need_analytics} мастер={need_master}")
        check("разница ровно 1 — известное поведение (BUSINESS_LOGIC §9.4)",
              need_master - need_analytics == 1,
              f"разница={need_master - need_analytics if need_master is not None else None}")

    block("9. Известное расхождение двух формул (BUSINESS_LOGIC §9.4)", _known_gap)

    def _size_split():
        # Нулевые веса: продаж по размерам нет вовсе → делим ПОРОВНУ.
        # Это отдельное решение size_split, и оно противоположно тому, что
        # делает _largest_remainder (см. блок 11).
        even = analytics.size_split(
            {"S": {"sold365": 0}, "M": {"sold365": 0}, "L": {"sold365": 0}}, 6)
        check("нулевые веса → деление поровну", lambda: even == {"S": 2, "M": 2, "L": 2},
              f"{even}")
        check("нулевые веса: сумма сходится с заказом",
              lambda: sum(even.values()) == 6, f"{even}")

        # Тай-брейк. Веса 1 и 3, заказ 2:
        #   exact = [0,5, 1,5]; alloc = [0, 1]; остатки одинаковые — 0,5 и 0,5.
        # Решает ВТОРОЙ ключ (вес): лишняя штука уходит размеру с бо́льшими
        # продажами. С одним ключом сортировка стабильна и штука досталась бы
        # первому размеру сетки, то есть порядок словаря решал бы за товароведа.
        tie = analytics.size_split({"S": {"sold365": 1}, "L": {"sold365": 3}}, 2)
        check("тай-брейк по второму ключу (остаток, вес): {S:0, L:2}",
              tie == {"S": 0, "L": 2}, f"{tie}")

        check("пустая сетка → пустой ответ, а не деление на ноль",
              lambda: analytics.size_split({}, 10) == {})
        check("нулевой заказ → пустой ответ",
              lambda: analytics.size_split({"S": {"sold365": 5}}, 0) == {})

        # Сумма ростовки. Диапазон нарочно захватывает заказы МЕНЬШЕ числа
        # размеров: там всё решают остатки, и ошибка в распределении лишних
        # штук видна только здесь.
        grid = {"XS": {"sold365": 0}, "S": {"sold365": 7}, "M": {"sold365": 13},
                "L": {"sold365": 1}, "XL": {"sold365": 4}}
        bad_sum = [n for n in range(0, 301)
                   if sum(analytics.size_split(grid, n).values()) != n]
        check("сумма ростовки = заказу для n от 0 до 300 (включая n < числа размеров)",
              lambda: bad_sum == [], f"расходится при n={bad_sum[:5]}")
        bad_type = [n for n in range(0, 51)
                    if any(type(v) is not int for v in analytics.size_split(grid, n).values())]
        check("штуки в ростовке — целые, а не float",
              lambda: bad_type == [], f"не целые при n={bad_type[:5]}")
        check("отрицательные веса не тянут ростовку в минус",
              lambda: all(v >= 0 for v in
                          analytics.size_split({"S": {"sold365": -5},
                                                "M": {"sold365": 5}}, 4).values()))

    block("10. Контракт analytics.size_split", _size_split)

    def _largest_remainder():
        lr = analytics_extra._largest_remainder
        # Нулевые веса: НУЛИ. Не поровну — здесь это осознанно другое решение,
        # чем у size_split: функция раскладывает деньги/штуки по долям, и
        # «долей нет» означает «нечего раскладывать», а не «поделим всем поровну».
        check("нулевые веса → нули (а НЕ деление поровну)",
              lambda: lr([0.0, 0.0, 0.0], 6) == [0, 0, 0], f"{lr([0.0, 0.0, 0.0], 6)}")
        check("пустой список весов → пустой ответ", lambda: lr([], 5) == [])
        check("нулевой total → нули", lambda: lr([1.0, 2.0], 0) == [0, 0])

        # Тай-брейк по ОДНОМУ ключу (остаток). Веса 1 и 3, total 2:
        # остатки равны, сортировка стабильна → лишняя штука первому.
        # Ровно тот же вход, что у size_split выше, и ответ ДРУГОЙ.
        check("тай-брейк по одному ключу (остаток): [1, 1]",
              lambda: lr([1.0, 3.0], 2) == [1, 1], f"{lr([1.0, 3.0], 2)}")
        check("сумма = total на длинном ряду",
              lambda: all(sum(lr([0.0, 7.0, 13.0, 1.0, 4.0], n)) == n
                          for n in range(1, 201)))
        check("значения — целые",
              lambda: all(type(v) is int for v in lr([1.0, 2.0, 3.0], 10)))
        check("отрицательный вес не даёт отрицательной доли",
              lambda: all(v >= 0 for v in lr([-5.0, 5.0], 4)))

    block("11. Контракт analytics_extra._largest_remainder", _largest_remainder)

    def _not_interchangeable():
        # BUSINESS_LOGIC §9.13 честно говорит: «на реальных входах расхождений
        # нет». Но «совпадает на реальных входах» и «одна и та же функция» —
        # разные утверждения, и дедупликация опирается на второе. Здесь
        # закреплено, что второе НЕВЕРНО: две задокументированные точки
        # расхождения. Пока они есть, свести функции нельзя — а если владелец
        # решит их свести, этот набор обязан покраснеть и потребовать решения.
        lr = analytics_extra._largest_remainder
        zero_split = analytics.size_split(
            {"S": {"sold365": 0}, "M": {"sold365": 0}}, 4)
        zero_lr = lr([0.0, 0.0], 4)
        check("нулевые веса: size_split делит поровну, _largest_remainder — нули",
              lambda: list(zero_split.values()) == [2, 2] and zero_lr == [0, 0],
              f"size_split={zero_split} lr={zero_lr}")

        tie_split = analytics.size_split({"S": {"sold365": 1}, "L": {"sold365": 3}}, 2)
        tie_lr = lr([1.0, 3.0], 2)
        check("равные остатки: size_split даёт [0, 2], _largest_remainder — [1, 1]",
              lambda: list(tie_split.values()) == [0, 2] and tie_lr == [1, 1],
              f"size_split={tie_split} lr={tie_lr}")
        check("две реализации НЕ взаимозаменяемы ни на одном из двух входов",
              lambda: list(zero_split.values()) != zero_lr
              and list(tie_split.values()) != tie_lr)

    block("12. Две реализации largest-remainder семантически различны", _not_interchangeable)

    db.close()

    # Числовой отчёт канонического вида: раннер выносит приговор по нему и
    # только по нему (D-42). Набор с 0 OK пройденным не считается.
    print(f"\nИТОГО: {len(PASSED)} OK, {len(FAILED)} FAIL")
    for name in FAILED:
        print(f"  FAIL {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = main()
    for _p in (DB_PATH, Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
        if _p.exists():
            _p.unlink()
    sys.exit(code)
