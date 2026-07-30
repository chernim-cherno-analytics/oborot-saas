"""Проверка /api/discounts: register → connect demo → инварианты."""
import httpx

BASE = "http://127.0.0.1:8001"
c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=120)

r = c.post("/register", data={
    "name": "Влад", "email": "disc@example.com",
    "password": "demo12345", "org_name": "Chernim Cherno",
})
assert r.status_code == 200, r.status_code
if c.get("/api/discounts").status_code == 401:  # аккаунт уже был — логин
    c.post("/login", data={"email": "disc@example.com", "password": "demo12345"})

r = c.post("/api/connect/demo")
assert r.status_code == 200 and r.json().get("ok"), r.text

r = c.get("/api/discounts")
assert r.status_code == 200, r.text
d = r.json()
items = d["items"]
cards = d["cards"]

def psych(raw):
    p = int(raw // 10) * 10
    if p >= 100 and p % 100 == 0:
        p -= 10
    return max(p, 0)

errs = []
for it in items:
    if not it["discount_pct"] > 0:
        errs.append(f"pct<=0: {it['base_name']}")
    if it["cs"] <= 0:
        errs.append(f"cs<=0: {it['base_name']}")
    expect_np = psych(it["avg_price"] * (1 - it["discount_pct"] / 100))
    # avg_price в ответе округлён до рубля — допускаем расхождение на шаг 10 ₽
    if abs(it["new_price"] - expect_np) > 10:
        errs.append(f"new_price: {it['base_name']} {it['new_price']} vs {expect_np}")
    if it["new_price"] >= 100 and it["new_price"] % 100 == 0:
        errs.append(f"не психологическая цена: {it['base_name']} {it['new_price']}")
    if it["new_price"] % 10 != 0:
        errs.append(f"не кратно 10: {it['base_name']} {it['new_price']}")
    if abs(it["expected_recovery"] - it["cs"] * it["new_price"]) > 1:
        errs.append(f"expected_recovery: {it['base_name']}")

if cards["frozen_total"] != sum(x["frozen"] for x in items):
    errs.append("frozen_total != sum(frozen)")
if cards["expected_recovery"] != sum(x["expected_recovery"] for x in items):
    errs.append("expected_recovery card != sum")
if cards["positions"] != len(items):
    errs.append("positions != len(items)")
if sorted((x["frozen"] for x in items), reverse=True) != [x["frozen"] for x in items]:
    errs.append("не отсортировано по frozen desc")

pcts = sorted({x["discount_pct"] for x in items})
print("items:", len(items), "| fresh_excluded:", d["fresh_excluded_count"])
print("cards:", cards)
print("pct values:", pcts)
print("no_cost items:", sum(1 for x in items if x["no_cost"]))
print("null days_since_sale:", sum(1 for x in items if x["days_since_sale"] is None))
print("sample top-3:")
for it in items[:3]:
    print("  ", {k: it[k] for k in ("base_name", "cls", "discount_pct", "frozen",
                                    "stock_days_left", "days_since_sale",
                                    "avg_price", "new_price", "expected_recovery")})
    print("     reason:", it["reason"])
print("ERRORS:" if errs else "ALL INVARIANTS OK", *errs[:20], sep="\n" if errs else " ")

# Изоляция: второй аккаунт без данных видит пустой ответ
c2 = httpx.Client(base_url=BASE, follow_redirects=True, timeout=60)
c2.post("/register", data={"name": "X", "email": "disc2@example.com",
                           "password": "demo12345", "org_name": "Other"})
if c2.get("/api/discounts").status_code == 401:
    c2.post("/login", data={"email": "disc2@example.com", "password": "demo12345"})
d2 = c2.get("/api/discounts").json()
print("tenant2 items:", len(d2["items"]), "(ожидаем 0)")
assert len(d2["items"]) == 0
assert not errs
print("DONE")
