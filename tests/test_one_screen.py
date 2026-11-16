# -*- coding: utf-8 -*-
"""D-89 «один экран заказа» (решение владельца 15.11.2026 23:28).

1) Меню: один пункт «Заказ», без «Мастера заказа», «Заказа позиции» и
   «Бюджета»; на их страницах подсвечен «Заказ».
2) «Заказ» открывается сразу по всем категориям (оформление доступно).
3) Полоса «Под бюджет»: бюджет задан = план мастера тем же движком, итог
   карточки «Себестоимость» = итог плана, «Не вошло в план» отдельным
   фильтром; «Вся потребность» возвращает расчёт.
4) Шаги мастера заканчиваются на таблице: «Открыть в «Заказе»» переносит
   бюджет и стратегию, план считается сам.
5) Первый заказ: карточка «Собрать по шагам», закрывается.

Запуск: PLAYWRIGHT_BROWSERS_PATH=/tmp/pwb python tests/test_one_screen.py
"""
import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "test_one_screen.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8853"))
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
OK = FAIL = 0
PW = "secret123"


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


cfg = uvicorn.Config(oborot_app, host="127.0.0.1", port=APP_PORT, log_level="warning")
server = uvicorn.Server(cfg)
th = threading.Thread(target=server.run, daemon=True)
th.start()
for _ in range(200):
    if server.started:
        break
    time.sleep(0.05)


def cookies(cl):
    return [{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"} for k, v in cl.cookies.items()]


def money(txt):
    """«299 тыс ₽», «1,99 млн ₽», «300 000» → рубли."""
    t = (txt or "").replace("\xa0", " ").replace(" ", "")
    m = re.search(r"([0-9]+(?:[.,][0-9]+)?)", t)
    if not m:
        return 0
    v = float(m.group(1).replace(",", "."))
    if "млн" in t:
        v *= 1_000_000
    elif "тыс" in t:
        v *= 1000
    return int(round(v))


try:
    own = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=120)
    own.post("/register", data={"name": "Владелец", "email": "own@one.io", "password": PW, "org_name": "Один экран"})
    own.post("/api/connect/demo")
    for pg in ("assistant", "replenish"):
        own.post("/api/hints/seen", json={"page": pg})

    print("\n1. Меню: один пункт «Заказ»")
    for path in ("/replenish", "/turnover", "/assistant", "/sizes", "/budget"):
        html = own.get(path).text
        navs = re.findall(r"<nav[^>]*>(.*?)</nav>", html, re.S)
        nav = [h for block in navs for h in re.findall(r'<a href="(/[a-z]+)"', block)]
        check(f"{path}: в меню нет «Мастера», «Заказа позиции», «Бюджета»",
              not any(h in nav for h in ("/assistant", "/sizes", "/budget")), str(nav))
        if path in ("/assistant", "/sizes", "/budget"):
            check(f"{path}: подсвечен «Заказ»",
                  re.search(r'<a href="/replenish"[^>]*class="active"', html) is not None)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.add_cookies(cookies(own))
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(BASE + "/replenish")
        page.wait_for_selector("#tbody tr[data-base]", timeout=60000)

        print("\n2. Сразу по всем категориям")
        note = page.inner_text("#scope-note")
        check("категории: «все категории» без выбора", "все категории" in note, note)
        check("«Оформить размещение» доступно сразу", not page.is_disabled("#btn-create-order"))

        print("\n5. Первый заказ: карточка «Собрать по шагам»")
        check("карточка видна, пока заказов нет", page.is_visible("#first-card"))
        page.click("#first-card-x")
        check("«Сразу к таблице» её закрывает", not page.is_visible("#first-card"))
        page.reload()
        page.wait_for_selector("#tbody tr[data-base]", timeout=60000)
        check("и после перезагрузки она не возвращается", not page.is_visible("#first-card"))

        print("\n3. Полоса «Под бюджет»")
        pos_full = page.inner_text("#s-pos")
        page.fill("#pb-budget", "300000")
        page.click("#pb-apply")
        page.wait_for_function("document.getElementById('pb-note').textContent.indexOf('План под бюджет')>=0",
                               timeout=60000)
        note = page.inner_text("#pb-note")
        plan = own.post("/api/order-plan/preview", json={
            "budget": 300000, "budget_scope": "full", "strategy": "balance", "max_share_pct": 30,
            "production_id": own.get("/api/productions").json()["productions"][0]["id"],
            "exclude_categories": []}).json()
        t = plan["totals"]
        check("подпись плана называет позиции и штуки движка",
              f"{t['positions']} поз." in note.replace("\xa0", " ") and f"{t['units']} шт" in note.replace("\xa0", " "),
              note)
        cost_card = money(page.inner_text("#s-cost"))
        check("карточка «Себестоимость» = себестоимость плана (до тысячи из-за краткой записи)",
              abs(cost_card - t["cost"]) <= max(1000, t["cost"] * 0.01), f"{cost_card} vs {t['cost']}")
        check("«Не вошло в план» видно отдельным фильтром", page.is_visible("#seg-out-btn"))
        outn = int(page.inner_text("#seg-out") or 0)
        check("и в нём есть позиции", outn > 0, str(outn))
        page.click("#seg-out-btn")
        rows = page.locator("#tbody tr[data-base]")
        check("фильтр показывает только не вошедшие", rows.count() == outn
              and page.locator("#tbody tr.plan-out").count() == outn, f"{rows.count()} / {outn}")
        check("и они сняты с галочки",
              page.locator("#tbody tr.plan-out input.row-check:checked").count() == 0)
        tags = page.eval_on_selector_all("#tbody tr.plan-out .out-tag", "els => els.map(e => e.textContent)")
        budget_names = {x["base_name"] for x in plan.get("not_included") or []}
        check("у каждой не вошедшей названа причина", tags and all(t.strip() for t in tags), str(tags[:5]))
        check("не хватившие бюджета так и подписаны",
              not budget_names or any("не вошло в бюджет" in t for t in tags), str(tags[:5]))
        page.click('#band-seg button[data-band=""]')
        print("\n3б. Excel под бюджетом: количества плана, а не полная потребность (ревью #136)")
        bodies = []
        page.on("request", lambda r: bodies.append(r.post_data_json) if r.method == "POST"
                and r.url.endswith("/api/export/replenish.xlsx") else None)
        page.click("#btn-export-xlsx")
        page.wait_for_timeout(2500)
        body = bodies[-1] if bodies else {}
        q = (body or {}).get("quantities") or {}
        top = next(iter(sorted(plan["items"], key=lambda i: -i["need"])), None)
        planned = {i["base_name"]: i["qty"] for i in plan["items"] if i["qty"] > 0}
        check("запрос выгрузки несёт количества плана",
              q and all(q[b]["qty"] == planned[b] for b in planned if b in q), str(list(q.items())[:2]))
        import io
        from openpyxl import load_workbook
        r = own.post("/api/export/replenish.xlsx", json={
            "base_names": list(planned), "quantities": {b: {"qty": v, "sizes": {}} for b, v in planned.items()}})
        wb = load_workbook(io.BytesIO(r.content))
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        hdr_i = next(i for i, row in enumerate(rows) if row and "Заказать, шт" in [str(x) for x in row])
        col = [str(x) for x in rows[hdr_i]].index("Заказать, шт")
        name_col = 0
        got = {row[name_col]: row[col] for row in rows[hdr_i + 1:] if row and row[name_col] in planned}
        bad = {b: (got.get(b), v) for b, v in planned.items() if b in got and got[b] != v}
        check("в файле количества плана (строки позиций)", got and not bad, str(list(bad.items())[:3]))
        check("шапка файла говорит «по плану под бюджет»",
              any("по плану под бюджет" in str(c) for row in rows[:hdr_i] for c in row if c))
        check("«Вся потребность» видна", page.is_visible("#pb-clear"))
        page.click("#pb-clear")
        page.wait_for_timeout(300)
        check("«Вся потребность» возвращает расчёт", page.inner_text("#s-pos") == pos_full
              and not page.is_visible("#seg-out-btn"), f"{page.inner_text('#s-pos')} vs {pos_full}")

        print("\n4. Шаги мастера заканчиваются на таблице")
        page.goto(BASE + "/assistant")
        page.wait_for_load_state("networkidle")
        page.evaluate("""() => localStorage.setItem('oborot.planbar', JSON.stringify(
            {budget: 200000, scope: 'full', strategy: 'grow', cap: 60}))""")
        page.goto(BASE + "/replenish")
        page.wait_for_function("document.getElementById('pb-note').textContent.indexOf('План под бюджет')>=0",
                               timeout=60000)
        check("условия из шагов перенесены и план посчитан сам",
              "лимит 60 %" in page.inner_text("#pb-note").replace("\xa0", " ")
              and page.locator('#pb-strat button.on[data-v="grow"]').count() == 1, page.inner_text("#pb-note"))
        check("бюджет в поле", money(page.input_value("#pb-budget")) == 200000, page.input_value("#pb-budget"))
        cats = sorted({(i.get("category") or "Без категории") for i in own.get("/api/replenish").json()["items"]})
        keep = cats[0]
        page.evaluate("""(ex) => localStorage.setItem('oborot.planbar', JSON.stringify(
            {budget: 200000, scope: 'full', strategy: 'balance', cap: 30, excl: ex}))""",
                      [c for c in cats if c != keep])
        page.goto(BASE + "/replenish")
        page.wait_for_function("document.getElementById('pb-note').textContent.indexOf('План под бюджет')>=0",
                               timeout=60000)
        note2 = page.inner_text("#scope-note")
        in_plan = page.eval_on_selector_all(
            "#tbody tr[data-base]:not(.plan-out):not(.out-scope) input.row-check:checked",
            "els => els.map(e => e.closest('tr').querySelector('.csub').textContent)")
        check("категории из шагов перенесены: выбрана только одна",
              keep in note2 and all(c not in note2 for c in cats if c != keep), note2)
        check("в плане только позиции этой категории",
              in_plan and all(t.startswith(keep) for t in in_plan), str(in_plan[:3]))

        print("\n6. Заказ по плану: бюджет потрачен, полоса не предлагает второй заказ на те же деньги")
        planned_n = page.locator("#tbody tr[data-base]:not(.plan-out):not(.out-scope) input.row-check:checked").count()
        page.click("#btn-create-order")
        page.wait_for_selector("#order-modal.open", timeout=10000)
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/orders")) as cr:
            page.click("#btn-order-draft")
        oid = cr.value.json().get("id")
        page.wait_for_function("document.getElementById('pb-note').textContent.indexOf('Заказ оформлен')>=0",
                               timeout=30000)
        try:
            page.wait_for_function("document.getElementById('seg-out-btn').style.display==='none'", timeout=30000)
        except Exception:  # noqa: BLE001 — итог скажет проверка ниже
            pass
        check("после оформления бюджет снят, полоса говорит об этом",
              page.input_value("#pb-budget") == "" and not page.is_visible("#seg-out-btn"),
              f"budget={page.input_value('#pb-budget')!r} out={page.is_visible('#seg-out-btn')}")
        saved = next(o for o in own.get("/api/orders").json()["orders"] if o["id"] == oid)
        check("в черновике столько позиций, сколько было в плане", len(saved["items"]) == planned_n,
              f"{len(saved['items'])} vs {planned_n}")
        own.delete(f"/api/orders/{oid}")
        src = own.get("/assistant").text
        check("в мастере есть кнопка «Открыть в «Заказе»»", 'id="toTable"' in src)
        check("ошибок JS нет", not errs, str(errs[:3]))
        b.close()
finally:
    server.should_exit = True
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
