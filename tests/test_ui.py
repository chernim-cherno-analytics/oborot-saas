# -*- coding: utf-8 -*-
"""Поведение страниц в НАСТОЯЩЕМ браузере.

Зачем этот набор существует. Дважды за сутки фича проходила зелёный регресс,
будучи мёртвой в браузере: тесты ходили прямо в API, а между API и человеком
лежит слой на ванильном JS, которого они не касались. Поле ручного количества
не отправлялось на сервер; браузер считал маржу «цена − себестоимость» и при
нулевой себестоимости обещал прибылью всю выручку. Проверка «строка есть в
HTML» такое не ловит — ловит только запуск страницы.

Здесь проверяется ровно то, что человек видит и нажимает:
  1) неудачный расчёт на «Заказе позиции» гасит карточки и кнопку отправки.
     Раньше пустой .catch() оставлял на экране ростовку ПРЕДЫДУЩЕГО товара
     при уже переключённом названии, и кнопка отправляла её под новым именем;
  2) причину отказа называет сервер, а не браузер: было «проверьте интернет»
     и «нужны права владельца» на любую ошибку, включая 402 «подписка»;
  3) не сохранившаяся скидка откатывается на экране. Раньше значение и
     подсветка ставились ДО ответа сервера и оставались после отказа —
     скидка выглядела сохранённой, не будучи сохранённой;
  4) подписи порогов классов приходят из настроек организации, а не зашиты
     в вёрстку: после смены порогов класс менялся у большинства позиций,
     а надписи на карточках не двигались;
  5) ошибка периода на «Обороте» гасит карточки, а не оставляет прошлые
     цифры под новой подписью;
  6) «Валовая маржа» в мастере совпадает с сервером до рубля, включая
     правку количества выше потребности;
  7) сумма заказа на «Что заказать» не выдаёт позиции без себестоимости
     за бесплатные;
  8) на страницах нет ошибок в консоли.

Запуск из корня репозитория:  python tests/test_ui.py
(нужен Chromium: PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers)
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_ui.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8816"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("  SKIP  playwright не установлен — набор пропущен")
        return 0
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:  # noqa: C901 — сценарный тест: шагов много, ветвлений мало
    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    c.post("/register", data={"name": "Владелец", "email": "ui@test.io",
                              "password": "secret123", "org_name": "Бренд-UI"})
    check("демо-данные загружены", c.post("/api/connect/demo").status_code == 200)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                         for k, v in c.cookies.items()])
        errors: list[str] = []
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("\n== «Заказ позиции»: отказ не оставляет чужой товар на экране ==")
        prods = c.get("/api/sizes/products").json()["products"][:2]
        names = [p["base_name"] if isinstance(p, dict) else p for p in prods]
        page.goto(f"{base}/sizes")
        page.wait_for_timeout(1500)

        def pick(name: str) -> None:
            page.fill("#prod-search", name)
            page.dispatch_event("#prod-search", "input")
            page.wait_for_timeout(400)
            # Выпадающий список слушает mousedown, а не click.
            page.evaluate(
                "() => { const el=document.querySelector('#dd [data-i]');"
                " if(el) el.dispatchEvent(new MouseEvent('mousedown',"
                "{bubbles:true,cancelable:true})); }")

        pick(names[0])
        page.wait_for_timeout(2000)
        check("расчёт первой позиции показан",
              page.evaluate("() => document.getElementById('cards').style.display") != "none")

        page.route("**/api/sizes/calc*", lambda route: route.fulfill(
            status=402, content_type="application/json",
            body='{"detail":"Подписка не оплачена"}'))
        pick(names[1])
        page.wait_for_timeout(1500)
        check("карточки предыдущего товара погашены",
              page.evaluate("() => document.getElementById('cards').style.display") == "none")
        check("кнопка отправки заказа скрыта",
              page.evaluate("() => document.getElementById('send-btn').style.display") == "none")
        body = page.text_content("#tbody") or ""
        check("причину назвал сервер, а не браузер", "одписк" in body, body[:80])
        page.unroute("**/api/sizes/calc*")

        print("\n== «Оборачиваемость»: не сохранившаяся скидка откатывается ==")
        page.goto(f"{base}/turnover")
        page.wait_for_timeout(3000)
        page.route("**/api/discount-overrides", lambda route: route.fulfill(
            status=402, content_type="application/json",
            body='{"detail":"Подписка не оплачена"}')
            if route.request.method == "POST" else route.continue_())
        page.once("dialog", lambda d: d.accept())
        applied = page.evaluate("""() => {
          const inp = document.querySelector('.disc-in');
          if(!inp) return null;
          const base = inp.getAttribute('data-base');
          inp.value = '25';
          inp.dispatchEvent(new Event('change', {bubbles:true}));
          return base;
        }""")
        page.wait_for_timeout(1200)
        check("поле скидки на странице есть", applied is not None, str(applied))
        state = page.evaluate("""() => {
          const inp = document.querySelector('.disc-in');
          return {value: inp.value, has: inp.classList.contains('has'),
                  mem: (window.DISC||{})[inp.getAttribute('data-base')]};
        }""")
        check("значение откатилось к сохранённому", not state["value"],
              str(state))
        check("подсветка «задана вручную» снята", state["has"] is False, str(state))
        page.unroute("**/api/discount-overrides")

        print("\n== «Активный сток»: подписи порогов — из настроек ==")
        page.goto(f"{base}/stocks")
        page.wait_for_timeout(2500)
        before = [page.text_content(f"#cond-{k}") for k in ("best", "good", "mid", "bad")]
        check("подписи отрисованы", all(before), str(before))
        c.post("/api/settings", json={"thresholds": {"weak": 500, "dull": 1500, "good": 3000}})
        page.reload()
        page.wait_for_timeout(2500)
        after = [page.text_content(f"#cond-{k}") for k in ("best", "good", "mid", "bad")]
        check("после смены порогов подписи изменились", before != after,
              f"{before[0]} -> {after[0]}")
        check("и называют новые числа", "3 000" in (after[0] or ""), str(after[0]))
        c.post("/api/settings", json={"thresholds": {"weak": 1000, "dull": 2000, "good": 5000}})

        print("\n== «Оборот»: ошибка гасит устаревшие карточки ==")
        page.goto(f"{base}/revenue")
        page.wait_for_timeout(2500)
        rev_before = page.text_content("#c-rev")
        check("выручка показана", rev_before not in (None, "", "—"), str(rev_before))
        page.evaluate("""() => {
          document.getElementById('d-from').value='2026-08-01';
          document.getElementById('d-to').value='2026-01-01';
          document.getElementById('applyCustom').click();
        }""")
        page.wait_for_timeout(1500)
        check("карточка выручки погашена, а не осталась прошлой",
              page.text_content("#c-rev") == "—", f'{rev_before} -> {page.text_content("#c-rev")}')
        check("причина названа словами сервера",
              "позже" in (page.text_content("#catbars") or ""),
              (page.text_content("#catbars") or "")[:70])

        print("\n== «Мастер заказа»: маржа на экране совпадает с сервером ==")
        # Ведём страницу как человек: анкета → «Показать план» → правка
        # количества в таблице. Никаких тестовых крючков в коде страницы:
        # проверяем ровно то, что видит пользователь.
        page.goto(f"{base}/assistant")
        page.wait_for_timeout(1800)
        page.fill("#budget", "300 000")
        # Экраны анкеты переключаются функцией go(n), кнопок с одинаковым
        # текстом на странице несколько — зовём напрямую то же, что и кнопка.
        page.evaluate("() => window.go(2)")
        page.wait_for_timeout(500)
        page.evaluate("() => window.preview(3)")
        page.wait_for_timeout(7000)
        row = page.evaluate("""() => {
          const el = document.querySelector('.qinp');
          return el ? {base: el.dataset.b, qty: parseInt(el.value,10)||0} : null;
        }""")
        check("план построен и таблица отрисована", row is not None, str(row))
        if row:
            bumped = row["qty"] + 60
            page.evaluate("""(v) => {
              const el = document.querySelector('.qinp');
              el.value = String(v);
              el.dispatchEvent(new Event('input', {bubbles:true}));
            }""", bumped)
            page.wait_for_timeout(1200)
            shown = page.evaluate("""() => {
              const cards = document.querySelectorAll('#cards .card');
              for (const c of cards) {
                const lbl = c.querySelector('.l');
                if (lbl && /Валовая маржа/i.test(lbl.textContent))
                  return c.querySelector('.v').textContent;
              }
              return null;
            }""")
            check("карточка «Валовая маржа» на экране есть", shown is not None,
                  str(shown))
            plan_body = {"budget": 300000, "budget_scope": "now",
                         "cadence_days": 30, "safety_days": 14,
                         "overrides": {row["base"]: bumped}}
            srv = c.post("/api/order-plan/preview", json=plan_body).json()
            srv = srv.get("plan") or srv
            srv_profit = srv["totals"]["expected_profit"]
            digits = "".join(ch for ch in (shown or "") if ch.isdigit())
            ui_profit = int(digits or 0)
            check("маржа на экране совпадает с серверной до рубля",
                  abs(ui_profit - srv_profit) <= 1,
                  f"экран {ui_profit} vs сервер {srv_profit}")
            check("излишек сверх потребности назван отдельно, а не влит в маржу",
                  "сверх потребности" in (page.text_content("#s3") or ""),
                  "строки нет")

        print("\n== «Что заказать»: позиции без себестоимости не бесплатны ==")
        page.goto(f"{base}/replenish")
        page.wait_for_timeout(3500)
        sub = page.text_content("#s-cost-sub") or ""
        check("подпись суммы заказа отрисована", bool(sub.strip()), sub[:60])
        # Сколько позиций без себестоимости на самом деле — спрашиваем сервер,
        # а не страницу: сверяем экран с источником, а не сам с собой.
        no_cost = sum(1 for it in c.get("/api/replenish").json()["items"]
                      if not (it.get("cost_price") or 0) > 0)
        if no_cost:
            check("сумма помечена неполной", "НЕПОЛНАЯ" in sub, sub[:80])
            check("названо, сколько именно позиций без себестоимости",
                  str(no_cost) in sub, f"ожидалось {no_cost}, на экране: {sub[:80]}")
        else:
            check("без таких позиций подпись обычная", "НЕПОЛНАЯ" not in sub, sub[:80])

        check("ни одной ошибки в консоли за весь проход", not errors, str(errors[:2]))
        browser.close()

    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
