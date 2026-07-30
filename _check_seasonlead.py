# -*- coding: utf-8 -*-
"""Проверка фичи: окна темпа (год/90/сезон) + lead time. Запуск на живом сервере 8802."""
import httpx

BASE = "http://127.0.0.1:8802"
c = httpx.Client(base_url=BASE, timeout=120)

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))

r = c.post("/register", data={"name": "Тест", "email": "season@test.io",
                              "password": "secret123", "org_name": "Сезон-бренд"})
check("регистрация", r.status_code == 303, f"status={r.status_code}")
r = c.post("/api/connect/demo")
check("демо посеяно", r.status_code == 200 and r.json().get("ok"))

# ── настройки: дефолты ──
s = c.get("/api/settings").json()
check("settings: rate_window дефолт year", s.get("rate_window") == "year", str(s.get("rate_window")))
check("settings: lead_time_days дефолт 45", s.get("lead_time_days") == 45, str(s.get("lead_time_days")))

# ── replenish на дефолте (year) ──
d = c.get("/api/replenish").json()
check("replenish: rate_window=year, lead_time_days=45",
      d["rate_window"] == "year" and d["lead_time_days"] == 45)
check("replenish: сезонное окно отдано", bool(d.get("season_from") and d.get("season_to")),
      f"{d.get('season_from')}..{d.get('season_to')}")
items = {i["base_name"]: i for i in d["items"]}
all_items = list(d["items"])
check("replenish: у items есть rate_year/rate_90/rate_season/gap_days/season_fallback",
      all(k in all_items[0] for k in ("rate_year", "rate_90", "rate_season", "gap_days", "season_fallback")))

# сезонные позиции: rate_90 != rate_year
diff = [i for i in all_items if i["rate_year"] > 0 and abs(i["rate_90"] - i["rate_year"]) / i["rate_year"] > 0.15]
check(f"rate_90 отличается от rate_year (>15%) у {len(diff)} позиций", len(diff) >= 5,
      ", ".join(x["base_name"] for x in diff[:4]))

puh = items.get('Пуховик «Норд»')
if puh is None:
    # мог попасть в excluded — достанем метрики через все item'ы turnover? проверим excluded
    excl = {e["base_name"] for e in d["excluded"]}
    print("  … Пуховик в excluded:", 'Пуховик «Норд»' in excl)
else:
    print(f"  … Пуховик «Норд»: year={puh['rate_year']} d90={puh['rate_90']} season={puh['rate_season']} fallback={puh['season_fallback']}")
    check("пуховик (зима): rate_90 (лето) < rate_year", puh["rate_90"] < puh["rate_year"],
          f"{puh['rate_90']} vs {puh['rate_year']}")
    check("пуховик: rate_season (осень-зима прошлого года) > rate_year",
          puh["rate_season"] > puh["rate_year"], f"{puh['rate_season']} vs {puh['rate_year']}")

dress = next((i for i in all_items if i["category"] == "Платья"), None)
if dress:
    print(f"  … {dress['base_name']}: year={dress['rate_year']} d90={dress['rate_90']} season={dress['rate_season']}")
    check("платье (лето): rate_90 (летний пик) > rate_year", dress["rate_90"] > dress["rate_year"],
          f"{dress['rate_90']} vs {dress['rate_year']}")

# gap_days при lead 45
gaps = [i for i in all_items if i["gap_days"] > 0]
check(f"gap_days>0 у позиций со скорым стокаутом ({len(gaps)} шт)", len(gaps) > 0,
      ", ".join(f"{x['base_name']}:{x['gap_days']}" for x in gaps[:3]))
gap_ok = all(
    (i["stockout_date"] is None and i["gap_days"] == 0) or i["gap_days"] >= 0
    for i in all_items)
sellout = next((i for i in all_items if i["cs"] == 0 and i["rate"] > 0), None)
if sellout:
    check("распроданная в ноль позиция: gap_days = lead_time (45)",
          sellout["gap_days"] == 45, f"{sellout['base_name']}: {sellout['gap_days']}")

need_year = {i["base_name"]: i["need"] for i in all_items}
rate_year_snapshot = {i["base_name"]: (i["rate_year"], i["rate_90"], i["rate_season"]) for i in all_items}

# ── переключение rate_window ── (сохранение + смена ответа)
r = c.post("/api/settings", json={"rate_window": "d90"})
check("POST settings rate_window=d90", r.status_code == 200 and r.json()["settings"]["rate_window"] == "d90")
d90 = c.get("/api/replenish").json()
check("replenish отражает d90", d90["rate_window"] == "d90")
need_d90 = {i["base_name"]: i["need"] for i in d90["items"]}
changed = [b for b in need_year if b in need_d90 and need_d90[b] != need_year[b]]
check(f"need изменился при окне d90 у {len(changed)} позиций", len(changed) >= 3,
      ", ".join(f"{b}: {need_year[b]}→{need_d90[b]}" for b in changed[:3]))
it90 = next((i for i in d90["items"] if i["base_name"] in rate_year_snapshot), None)
check("активный rate = rate_90 при окне d90",
      all(i["rate"] == i["rate_90"] for i in d90["items"]))
check("настройка сохранилась (GET /api/settings)", c.get("/api/settings").json()["rate_window"] == "d90")

r = c.post("/api/settings", json={"rate_window": "season"})
check("POST settings rate_window=season", r.status_code == 200)
ds = c.get("/api/replenish").json()
check("replenish отражает season", ds["rate_window"] == "season")
need_season = {i["base_name"]: i["need"] for i in ds["items"]}
changed_s = [b for b in need_year if b in need_season and need_season[b] != need_year[b]]
check(f"season-окно меняет need у {len(changed_s)} позиций", len(changed_s) >= 3,
      ", ".join(f"{b}: {need_year[b]}→{need_season[b]}" for b in changed_s[:3]))
check("активный rate = rate_season", all(i["rate"] == i["rate_season"] for i in ds["items"]))
fb = [i for i in ds["items"] if i["season_fallback"]]
print(f"  … season_fallback у {len(fb)} позиций в списке заказа")

r = c.post("/api/settings", json={"rate_window": "bogus"})
check("невалидный rate_window отклонён 422", r.status_code == 422, f"status={r.status_code}")

# ── lead_time: сохранение и влияние ──
r = c.post("/api/settings", json={"rate_window": "year", "lead_time_days": 120})
check("lead_time_days=120 сохранён", r.status_code == 200 and r.json()["settings"]["lead_time_days"] == 120)
s2 = c.get("/api/settings").json()
check("GET settings: lead_time_days=120", s2["lead_time_days"] == 120)
d120 = c.get("/api/replenish").json()
gaps120 = [i for i in d120["items"] if i["gap_days"] > 0]
check(f"при lead 120 позиций с дырой больше ({len(gaps120)} vs {len(gaps)})", len(gaps120) > len(gaps))
check("season_from сдвинулось при lead 120",
      d120["season_from"] != d["season_from"], f"{d['season_from']} → {d120['season_from']}")

# сохранение из /settings-формы (полный body как шлёт страница) не затирает rate_window
r = c.post("/api/settings", json={"thresholds": {"weak": 1000, "dull": 2000, "good": 5000},
                                  "min_stock_days": 3, "horizon_days": 90, "lead_time_days": 45})
check("полный POST /settings вернул lead 45 и не затёр rate_window",
      r.status_code == 200 and r.json()["settings"]["lead_time_days"] == 45
      and r.json()["settings"]["rate_window"] == "year")

# порог min_stock_days применяется во всех окнах: поднимем и убедимся, что rate_90 меняется
base_d = c.get("/api/replenish").json()
r = c.post("/api/settings", json={"min_stock_days": 15})
hi_d = c.get("/api/replenish").json()
b90 = {i["base_name"]: i["rate_90"] for i in base_d["items"]}
h90 = {i["base_name"]: i["rate_90"] for i in hi_d["items"]}
moved = [b for b in b90 if b in h90 and h90[b] != b90[b]]
check(f"min_stock_days влияет на rate_90 ({len(moved)} позиций изменились)", len(moved) > 0)
c.post("/api/settings", json={"min_stock_days": 3})

print()
print(f"ИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
if FAIL:
    print("Провалены:", *FAIL, sep="\n  - ")
raise SystemExit(1 if FAIL else 0)
