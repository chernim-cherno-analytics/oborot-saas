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
  8) DATA-8 (третий сценарий): на «Настройках» владелец видит положительный
     счётчик документов продаж, пропущенных из-за нераспознанного/невыбранного
     склада, понятным текстом; при нуле тревожного сообщения нет;
  9) на страницах нет ошибок в консоли.

Запуск из корня репозитория:  python tests/test_ui.py

Нужен Chromium под playwright: `pip install -r requirements-dev.lock` и
`python -m playwright install chromium`. Каталог браузеров раньше был зашит
здесь как `/opt/pw-browsers` — путь с машины, которой нет ни у CI, ни на
macOS: playwright молча искал браузер не там, не находил и набор объявлял
себя пропущенным. Теперь каталог не навязывается: не задан
`PLAYWRIGHT_BROWSERS_PATH` — работает штатный кэш playwright.
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


SETTINGS_BODY_LIMIT = 300


def _require_2xx(status_code: int, body_text: str, context: str) -> None:
    """Fail-closed guard для ответов POST /api/settings.

    Ниже по сценарию UI-проверки читают состояние, которое обязан был
    создать этот POST (новые пороги, окно темпа). Молчаливое 4xx/5xx здесь
    раньше означало, что сценарий проверяет несуществующую предпосылку и
    списывает разницу на флак браузера, а не на реальный отказ настроек.
    Диагностика режется по длине, чтобы не разлить тело ответа целиком.
    """
    if 200 <= status_code < 300:
        return
    detail = (body_text or "")[:SETTINGS_BODY_LIMIT]
    raise RuntimeError(
        f"{context}: POST /api/settings -> HTTP {status_code}: {detail}")


def post_settings(client: "httpx.Client", payload: dict, context: str):
    resp = client.post("/api/settings", json=payload)
    _require_2xx(resp.status_code, resp.text, context)
    return resp


def _selfcheck_settings_guard() -> None:
    """Узкий self-check: synthetic non-2xx действительно ловится.

    Тест-локальный вызов на выдуманных status/body — без сети, без браузера
    и без прод-хуков, — доказывает, что `_require_2xx` реально fail-closed,
    а не просто выглядит так по чтению кода.
    """
    secret_like = "token=SHOULD-NOT-LEAK-" + ("x" * 500)
    try:
        _require_2xx(500, secret_like, "synthetic-guard")
    except RuntimeError as exc:
        msg = str(exc)
        check("guard: synthetic non-2xx POST /api/settings отклонён",
              "500" in msg, msg[:120])
        check("guard: диагностика ограничена по длине и не льёт тело целиком",
              len(msg) < len(secret_like), f"len={len(msg)}")
    else:
        check("guard: synthetic non-2xx POST /api/settings отклонён", False,
              "исключение не брошено")
        check("guard: диагностика ограничена по длине и не льёт тело целиком",
              False, "исключение не брошено")


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
        # Код 77 И причина — оба сигнала сразу, иначе раннер засчитает это
        # падением (D-42). Раньше здесь стоял return 0, и набор, не открывший
        # ни одной страницы, выглядел в CI зелёным.
        print("ПРОПУЩЕНО: playwright не установлен — поставьте "
              "requirements-dev.lock и выполните `python -m playwright "
              "install chromium`")
        return 77
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
    from playwright.sync_api import sync_playwright, expect, TimeoutError as PWTimeoutError

    _selfcheck_settings_guard()

    base = f"http://127.0.0.1:{APP_PORT}"
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    c.post("/register", data={"name": "Владелец", "email": "ui@test.io",
                              "password": "secret123", "org_name": "Бренд-UI"})
    check("демо-данные загружены", c.post("/api/connect/demo").status_code == 200)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — важно имя причины, а не тип
            # Playwright есть, браузера нет. Это НЕ пропуск: набор обязателен,
            # а окружение не готово — и сказать об этом надо отчётом, а не
            # трассировкой, которую раннер прочитает как «нет отчёта».
            check("Chromium запускается", False,
                  str(exc).strip().splitlines()[0][:200])
            print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
            return 1
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
        post_settings(c, {"thresholds": {"weak": 500, "dull": 1500, "good": 3000}},
                      "смена порогов на 3 000 перед проверкой подписей")
        page.reload()
        try:
            # Ждём КОНКРЕТНУЮ новую подпись, а не фиксированную паузу: JS
            # красит #cond-best новым порогом асинхронно после reload, и
            # только это ожидание — доказательство готовности (TECH_DEBT
            # OPS-7, «test_ui.py ждёт фиксированными паузами»). Таймаут
            # ограничен: если условие не наступит, ниже это честно упадёт
            # через check(), а не зависнет.
            expect(page.locator("#cond-best")).to_contain_text("3 000", timeout=5000)
        except PWTimeoutError:
            pass
        after = [page.text_content(f"#cond-{k}") for k in ("best", "good", "mid", "bad")]
        check("после смены порогов подписи изменились", before != after,
              f"{before[0]} -> {after[0]}")
        check("и называют новые числа", "3 000" in (after[0] or ""), str(after[0]))
        post_settings(c, {"thresholds": {"weak": 1000, "dull": 2000, "good": 5000}},
                      "возврат порогов к дефолту после сценария")

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
            # Сценарий обязан сам создать свою предпосылку. Излишек считается
            # как «введено минус потребность» — и сервером, и страницей, — а
            # прежнее qty + 60 брало число с потолка. 26.08 первая строка
            # демо-плана дала qty=15 при need=75: qty + 60 — это ровно 75,
            # то есть вровень с потребностью, а не сверх неё. Излишка нет,
            # строки на экране закономерно нет, и обязательный набор ui падал
            # не на регрессе продукта, а на своём допущении (post-merge CI
            # 32917949907, оба прогона 2594 OK / 1 FAIL).
            #
            # Потребность спрашиваем у сервера, а не считываем с экрана:
            # сверяем экран с источником, а не сам с собой — тем же приёмом,
            # что и «Что заказать» ниже.
            plan_params = {"budget": 300000, "budget_scope": "now",
                           "cadence_days": 30, "safety_days": 14}
            plan_src = c.post("/api/order-plan/preview", json=plan_params).json()
            plan_src = plan_src.get("plan") or plan_src
            item = next((i for i in plan_src.get("items", [])
                         if i.get("base_name") == row["base"]), None)
            check("правим ту же строку, что построил сервер",
                  item is not None and item.get("qty") == row["qty"],
                  f"экран {row}, сервер "
                  + str(None if item is None
                        else {"qty": item.get("qty"), "need": item.get("need")}))
            # need может не прийти вовсе (строка, вписанная руками, — см.
            # _manual_item) или прийти null. Страница в этом случае считает
            # потребность равной введённому количеству (liveTotals в
            # assistant.html), то есть излишка не бывает ни при каком вводе.
            # Молчать об этом нельзя: иначе сценарий снова проверяет не то,
            # что обещает названием.
            need = item.get("need") if item else None
            need = int(need) if isinstance(need, (int, float)) else None
            margin = max(0, (item.get("avg_price") or 0)
                         - (item.get("cost_price") or 0)) if item else 0
            check("потребность строки известна числом", need is not None,
                  f'{row["base"]}: need={None if item is None else item.get("need")!r}')
            # Строго больше потребности — не «на 60 больше». Прежнее qty + 60
            # остаётся нижней границей: правка руками должна быть заметной,
            # иначе она не проверяет и расчёт маржи.
            bumped = max(row["qty"] + 60, need + 1) if need is not None \
                else row["qty"] + 60
            check("ручной ввод строго больше потребности и излишку есть маржа",
                  need is not None and bumped > need and margin > 0,
                  f'{row["base"]}: ввод {bumped} шт против потребности {need} шт, '
                  f"маржа {margin} ₽/шт")
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
            plan_body = dict(plan_params, overrides={row["base"]: bumped})
            srv = c.post("/api/order-plan/preview", json=plan_body).json()
            srv = srv.get("plan") or srv
            srv_profit = srv["totals"]["expected_profit"]
            digits = "".join(ch for ch in (shown or "") if ch.isdigit())
            ui_profit = int(digits or 0)
            check("маржа на экране совпадает с серверной до рубля",
                  abs(ui_profit - srv_profit) <= 1,
                  f"экран {ui_profit} vs сервер {srv_profit}")
            # Спрашиваем подпись самой карточки маржи, а не весь экран: слова
            # «сверх потребности» есть на странице и по другим поводам —
            # в карточке «Позиций / штук» и в пояснении строки, — и проверка
            # по всему #s3 могла бы пройти, ни разу не увидев, назван ли
            # излишек ОТДЕЛЬНО ОТ МАРЖИ. Именно это обещает название проверки.
            over_note = page.evaluate("""() => {
              const cards = document.querySelectorAll('#cards .card');
              for (const c of cards) {
                const lbl = c.querySelector('.l');
                if (lbl && /Валовая маржа/i.test(lbl.textContent))
                  return (c.querySelector('.s') || {}).textContent || "";
              }
              return null;
            }""")
            check("излишек сверх потребности назван отдельно, а не влит в маржу",
                  "сверх потребности" in (over_note or ""),
                  "карточки маржи нет" if over_note is None
                  else f"подпись карточки: {over_note[:120]}")

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

        print("\n== «Бюджет»: строка состояния называет окно темпа ==")
        page.goto(f"{base}/budget")
        page.wait_for_timeout(3000)
        page.evaluate("""() => {
          const b = document.getElementById('calcBtn') || document.querySelector('.go-btn');
          if (b) b.click();
        }""")
        page.wait_for_timeout(3000)
        hint_year = page.text_content("#statusHint") or ""
        check("окно темпа названо в строке состояния", "темп" in hint_year, hint_year[:100])
        post_settings(c, {"rate_window": "d90"}, "смена окна темпа на d90")
        page.reload()
        page.wait_for_timeout(3000)
        page.evaluate("""() => {
          const b = document.getElementById('calcBtn') || document.querySelector('.go-btn');
          if (b) b.click();
        }""")
        page.wait_for_timeout(3000)
        hint_90 = page.text_content("#statusHint") or ""
        check("после смены окна строка изменилась",
              hint_90 != hint_year, f"{hint_year[:60]} -> {hint_90[:60]}")
        post_settings(c, {"rate_window": "year"},
                      "возврат окна темпа к дефолту после сценария")

        print("\n== «Настройки»: диагностика пропущенных документов продаж (DATA-8) ==")
        # /api/settings подмешивается (fetch реального ответа + подмена только
        # connection/role), а не заменяется целиком: страница читает из него
        # много несвязанных настроек, ломать их не нужно и незачем — нас
        # интересует ровно ветка owner+moysklad, которая рисует кнопки синка
        # и рядом с ними диагностику.
        def _mock_settings_moysklad(route):
            resp = route.fetch()
            data = resp.json()
            data["connection"] = {"kind": "moysklad", "status": "active", "last_sync_at": None}
            data["role"] = "owner"
            route.fulfill(response=resp, json=data)

        def _sync_status_with(diag_value):
            def handler(route):
                route.fulfill(json={
                    "state": "done", "mode": "incremental", "phase": "",
                    "progress_pct": 100, "detail": "", "error": "",
                    "started_at": None, "finished_at": None,
                    "diagnostics": {"sales_docs_skipped_store": diag_value},
                })
            return handler

        def diag_hint_visible():
            # evaluate, а не locator/text_content: элемент до правки НЕ существует
            # вовсе, а не просто скрыт — text_content на отсутствующем селекторе
            # ждёт полный таймаут вместо честного FAIL здесь и сейчас.
            return page.evaluate(
                "() => { var b = document.getElementById('sync-diag-hint'); "
                "return b ? getComputedStyle(b).display !== 'none' : null; }")

        def diag_hint_text():
            return page.evaluate(
                "() => { var b = document.getElementById('sync-diag-hint'); "
                "return b ? b.textContent : null; }") or ""

        page.route("**/api/settings", _mock_settings_moysklad)
        page.route("**/api/sync/status", _sync_status_with(12))
        page.goto(f"{base}/settings")
        page.wait_for_timeout(1500)
        check("владельцу с МС-подключением видна кнопка «Синхронизировать сейчас»",
              page.locator("#btn-sync-now").count() > 0)
        diag_text = diag_hint_text()
        check("положительный точный счётчик (12) показан понятным текстом",
              diag_hint_visible() is True and "12" in diag_text,
              f"visible={diag_hint_visible()} text={diag_text[:160]}")
        check("текст называет причину человеческим языком (склад)",
              "склад" in diag_text.lower(), diag_text[:160])

        page.unroute("**/api/sync/status")
        page.route("**/api/sync/status", _sync_status_with(0))
        page.reload()
        page.wait_for_timeout(1500)
        check("при нуле тревожного сообщения нет",
              diag_hint_visible() is False, f"visible={diag_hint_visible()}")

        page.unroute("**/api/sync/status")
        page.unroute("**/api/settings")

        check("ни одной ошибки в консоли за весь проход", not errors, str(errors[:2]))
        browser.close()

    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
