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
  8) DATA-8 (третий сценарий + lifecycle corrective): на «Настройках»
     владелец видит sticky-факт о документах продаж, пропущенных из-за
     нераспознанного/невыбранного склада, честным текстом («может быть
     неполно», а не точный текущий итог) даже на завершённом ОБЫЧНОМ
     инкременте; при авторитетном нуле (успешная полная пересборка) —
     тишина. Сама серверная сохранность факта через инкремент —
     tests/test_sync_diag_store.py (часть 2);
  9) на страницах нет ошибок в консоли;
 10) сквозной путь оператора целиком в браузере: регистрация формой (включая
     обязательное согласие, о котором API-вариант не знает вовсе), демо
     кнопкой, переход в план по ссылке меню, правка ростовки, её сохранение,
     перезагрузка и повторное открытие, выгрузка кнопкой и разбор скачанной
     книги. Плюс состояние подключения — ошибка, повтор, неполные данные —
     на 1400 и на 390.

     Проверки 1-9 намеренно стартуют с готового аккаунта: регистрация и демо
     там делаются httpx-клиентом ДО запуска браузера. Это правильно для них,
     но сквозным путём не является — пункт 10 закрывает именно этот разрыв.

Запуск из корня репозитория:  python tests/test_ui.py

Снимки экрана по умолчанию НЕ сохраняются. Чтобы получить их для разбора:
`OBOROT_UI_ARTIFACTS=/абсолютный/путь/вне/репозитория python tests/test_ui.py`
— абсолютные пути к файлам набор напечатает в конце.

Нужен Chromium под playwright: `pip install -r requirements-dev.lock` и
`python -m playwright install chromium`. Каталог браузеров раньше был зашит
здесь как `/opt/pw-browsers` — путь с машины, которой нет ни у CI, ни на
macOS: playwright молча искал браузер не там, не находил и набор объявлял
себя пропущенным. Теперь каталог не навязывается: не задан
`PLAYWRIGHT_BROWSERS_PATH` — работает штатный кэш playwright.
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
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

        print("\n== Мастер: серверный запрет управляет кнопкой создания ==")
        if row:
            if page.locator("#hint-overlay").is_visible():
                page.locator("#hint-overlay .hm-close").click()
            page.evaluate("""(qty) => {
                const input = document.querySelector('.qinp');
                input.value = String(qty);
                input.dispatchEvent(new Event('input', {bubbles:true}));
            }""", row["qty"])
            gate = {"blocked": True, "code": "share_limit"}

            def server_gate(route):
                response = route.fetch()
                data = response.json()
                data["can_create"] = not gate["blocked"]
                data["stop"] = ([{"code": gate["code"], "text": "Сервер запретил этот план"}]
                                if gate["blocked"] and gate["code"] else [])
                route.fulfill(response=response, json=data)

            page.route("**/api/order-plan/preview", server_gate)
            for code, blocked in (("share_limit", True), ("new_items_over_budget", True),
                                  ("", True), ("", False)):
                gate.update(code=code, blocked=blocked)
                with page.expect_response(lambda r: r.url == f"{base}/api/order-plan/preview"):
                    page.locator("#recalcPlan").click()
                page.locator("#mkOrder").wait_for(state="visible")
                check(f"кнопка соблюдает серверный запрет {code or 'can_create'}={blocked}",
                      page.locator("#mkOrder").is_disabled() == blocked)
                if blocked:
                    check("у запрещённой кнопки есть объяснение",
                          bool(page.locator("#mkOrder").get_attribute("title")))
            page.unroute("**/api/order-plan/preview", server_gate)

        print("\n== A01: новинки не выдают неполные итоги за полные ==")
        if row:
            check("A01 без новинок обычная подпись сохранена",
                  "Полное обязательство" in (page.text_content("#cards") or "")
                  and "не учитывают" not in (page.text_content("#budgetWarn") or ""))
            page.evaluate("() => window.addNew('A01 UI новинка', 2, 1000)")
            with page.expect_response(lambda r: r.url == f"{base}/api/order-plan/preview") as new_preview:
                page.locator("#recalcPlan").click()
            check("A01 сервер отдельно считает стоимость новинок",
                  new_preview.value.json()["new_items_cost"] == 2000)
            page.locator("#mkOrder").wait_for(state="visible")
            cards_with_new = page.text_content("#cards") or ""
            check("A01 обязательство с новинками не названо полным",
                  "Полное обязательство" not in cards_with_new
                  and "Обязательство по каталожным позициям" in cards_with_new)
            check("A01 количество ограничено каталожными позициями",
                  "Каталожных позиций / штук" in cards_with_new)
            warning_with_new = page.text_content("#budgetWarn") or ""
            check("A01 полный состав и календарь включают новинки",
                  "Полный состав заказа, включая новинки" in warning_with_new
                  and "Новинки включены в календарь" in warning_with_new)
            shown_payments = page.locator("#payflow .a").all_text_contents()
            check("A01 браузер показывает полную сумму платежей сервера",
                  sum(int(''.join(ch for ch in text if ch.isdigit())) for text in shown_payments)
                  == new_preview.value.json()["order_totals"]["cost"])
            first_qty = page.locator(".qinp").first
            previous_qty = first_qty.input_value()
            first_qty.fill(str(int(previous_qty) + 1))
            check("A01 ручная правка не оставляет устаревший полный календарь",
                  "Пересчитайте" in (page.text_content("#payflow") or "")
                  and page.locator("#completeOrderSummary").count() == 0)
            first_qty.fill(previous_qty)
            check("A01 возврат количества восстанавливает полный итог",
                  page.locator("#completeOrderSummary").count() == 1
                  and page.locator("#payflow .a").count() == len(shown_payments))
            history_body = new_preview.value.request.post_data_json
            missing_cost = new_preview.value.json()["review"]["no_cost"]
            check("история: в каталоге есть позиция без себестоимости", bool(missing_cost))
            if missing_cost:
                history_body["overrides"] = {**history_body.get("overrides", {}),
                                             missing_cost[0]["base_name"]: 2}
            history_saved = c.post("/api/order-plan", json=history_body).json()
            page.reload()
            history_row = page.locator(f".repeat[data-id='{history_saved['id']}']").locator("xpath=../..")
            history_row.wait_for(state="attached")
            check("A01 история показывает полный сохранённый состав",
                  "без новинок" not in (history_row.text_content() or ""))
            check("история: неполная себестоимость явно подписана",
                  "сумма неполная" in (history_row.text_content() or ""))

        print("\n== A01: заказ только из новинок доступен в мастере ==")
        only_prod = c.post("/api/productions", json={"name": "UI только новинки"}).json()["id"]
        c.post(f"/api/productions/{only_prod}/setup", json={"preset": "fabric_sewing"})
        page.goto(f"{base}/assistant")
        page.locator(f"#prodTiles .tile[data-id='{only_prod}']").click()
        page.evaluate("() => window.addNew('Только новинки UI', 2, 1000)")
        with page.expect_response(lambda r: r.url == f"{base}/api/order-plan/preview") as only_preview:
            page.get_by_text("Сразу показать план", exact=True).click()
        only_plan = only_preview.value.json()
        check("A01 реальный сервер разрешает план только из новинок",
              not only_plan["items"] and bool(only_plan["new_items"]) and only_plan["can_create"])
        page.wait_for_timeout(500)
        check("A01 отсутствие рекомендаций не скрывает полный заказ новинок",
              page.locator("#completeOrderSummary").count() == 1
              and page.locator("#mkOrder").count() == 1
              and not page.locator("#mkOrder").is_disabled())

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

        print("\n== A05: простая форма передаёт выбранное производство ==")
        if page.locator("#hint-overlay").is_visible():
            page.locator("#hint-overlay .hm-close").click()
        selected_production = int(page.locator(".bigtab.active").get_attribute("data-id"))
        page.locator("#btn-create-order").click()
        page.locator("#order-name").fill("A05 browser metadata")
        with page.expect_response(lambda r: r.url == f"{base}/api/orders"
                                  and r.request.method == "POST") as created_response:
            page.locator("#btn-order-submit").click()
        created_response = created_response.value
        check("A05 браузер передаёт выбранное производство",
              created_response.request.post_data_json.get("production_id") == selected_production)
        check("A05 заказ из браузера создан", created_response.status == 200)
        if created_response.status == 200:
            page.locator("#order-modal").wait_for(state="hidden")
            import sqlite3
            created_id = created_response.json()["id"]
            with sqlite3.connect(DB_PATH) as connection:
                metadata = connection.execute(
                    "SELECT production_id, created_by FROM production_orders WHERE id=?",
                    (created_id,)).fetchone()
                author = connection.execute("SELECT id FROM users WHERE email='ui@test.io'").fetchone()[0]
            check("A05 выбор и автор из браузера сохранены",
                  metadata == (selected_production, author), str(metadata))
            c.delete(f"/api/orders/{created_id}")

        print("\n== «Бюджет»: строка состояния называет окно темпа ==")
        page.goto(f"{base}/budget")
        page.wait_for_timeout(3000)
        page.evaluate("""() => {
          const b = document.getElementById('calcBtn') || document.querySelector('.go-btn');
          if (b) b.click();
        }""")
        page.wait_for_function("""() => {
            const text = document.getElementById('statusHint').textContent.trim();
            return text && text !== 'считаю…';
        }""")
        hint_year = page.text_content("#statusHint") or ""
        check("окно темпа названо в строке состояния", "темп" in hint_year, hint_year[:100])
        post_settings(c, {"rate_window": "d90"}, "смена окна темпа на d90")
        page.reload()
        page.wait_for_timeout(3000)
        page.evaluate("""() => {
          const b = document.getElementById('calcBtn') || document.querySelector('.go-btn');
          if (b) b.click();
        }""")
        page.wait_for_function("""() => {
            const text = document.getElementById('statusHint').textContent.trim();
            return text && text !== 'считаю…';
        }""")
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

        def _sync_status_with(diag_value, mode="incremental"):
            def handler(route):
                route.fulfill(json={
                    "state": "done", "mode": mode, "phase": "",
                    "progress_pct": 100, "detail": "", "error": "",
                    "started_at": None, "finished_at": None,
                    "diagnostics": {"sales_docs_skipped_store_unresolved": diag_value},
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
        # mode="incremental" — намеренно: этот sticky-факт мог пережить
        # обычный инкремент (DATA-8 corrective), а не только первичную
        # загрузку. Экран не обязан знать, каким прогоном факт появился.
        page.route("**/api/sync/status", _sync_status_with(12, mode="incremental"))
        page.goto(f"{base}/settings")
        page.wait_for_timeout(1500)
        check("владельцу с МС-подключением видна кнопка «Синхронизировать сейчас»",
              page.locator("#btn-sync-now").count() > 0)
        diag_text = diag_hint_text()
        check("положительный точный факт (12) показан понятным текстом на завершённом инкременте",
              diag_hint_visible() is True and "12" in diag_text,
              f"visible={diag_hint_visible()} text={diag_text[:200]}")
        check("текст называет причину человеческим языком (склад)",
              "склад" in diag_text.lower(), diag_text[:200])
        check("текст честно говорит «может быть неполно», а не выдаёт sticky "
              "число за точный текущий итог",
              "неполн" in diag_text.lower(), diag_text[:200])

        # DATA-8 corrective: завершённый ОБЫЧНЫЙ инкремент с диагностикой 0 —
        # это ИМЕННО тот случай, который раньше тихо гасил предупреждение
        # (BLOCKED issuecomment-5438193835), хотя инкремент не перечитывает
        # всю историю. Сервер теперь в этом случае не пришлёт 0 в diagnostics
        # (см. tests/test_sync_diag_store.py, часть 2) — здесь же убеждаемся,
        # что ЕСЛИ бы пришёл именно authoritative 0 (полная пересборка),
        # экран честно его скрывает.
        page.unroute("**/api/sync/status")
        page.route("**/api/sync/status", _sync_status_with(0, mode="initial"))
        page.reload()
        page.wait_for_timeout(1500)
        check("при авторитетном нуле (успешная полная пересборка) тревожного сообщения нет",
              diag_hint_visible() is False, f"visible={diag_hint_visible()}")

        page.unroute("**/api/sync/status")
        page.unroute("**/api/settings")

        # ── PILOT-SYNC-TRUTH-1 ────────────────────────────────────────────
        #
        # Экран говорит про состояние источника двумя местами, и оба брали
        # слова не оттуда, откуда факт.
        #
        # 1. «Настройки» подписывают подключение словом «работает», а берут
        #    его из `connection.status`. Эта запись НЕ ПОНИЖАЕТСЯ:
        #    `ms_sync._activate_connection` только поднимает её до `active`
        #    (так и написано в его докстроке), а провал синка пишется в
        #    SyncState (`_thread_main`, ветка except). Значения `error` у
        #    connection не ставит вообще никто — во всём `app/` нет ни одного
        #    присваивания. Значит после упавшей синхронизации экран продолжает
        #    говорить «работает».
        # 2. «Оборачиваемость» при неполном окне обещает фоновую догрузку и
        #    РОСТ ЦИФР, а признак неполноты считает из одного покрытия. Само
        #    состояние синка рядом уже прочитано (им же гасится авто-refresh),
        #    то есть факт под рукой, а слова его не спрашивают.
        #
        # Проверяется ТОЛЬКО правдивость слов. Данные, ручки, права и
        # существующий баннер ошибки не трогаются — за последним тут отдельная
        # проверка, потому что «убрать обещание» и «убрать сообщение об
        # ошибке» — разные вещи, и перепутать их было бы хуже дефекта.
        _pilot_sync_truth(page, base, check, errors)

        check("ни одной ошибки в консоли за весь проход", not errors, str(errors[:2]))

        # ── PILOT-BROWSER-JOURNEY-1 ───────────────────────────────────────
        # Отдельный контекст и отдельная организация: путь начинается с пустой
        # формы регистрации, а не с готовой сессии, которую тест себе выдал.
        journey_shots: list[str] = []
        try:
            _browser_journey(browser, base, journey_shots)
            for width in (1400, MOBILE_WIDTH):
                _connection_states(browser, base, width, journey_shots)
        except Exception as exc:  # noqa: BLE001 — падение пути обязано стать
            # отчётом и ненулевым кодом, а не трассировкой без отчёта (D-42).
            check("сквозной путь в браузере дошёл до конца", False,
                  f"{type(exc).__name__}: {exc}"[:300])
            traceback.print_exc()
        if journey_shots:
            print("\nСнимки экрана (абсолютные пути):")
            for shot_path in journey_shots:
                print(f"  {shot_path}")
        else:
            print("\nСнимки экрана не сохранялись: OBOROT_UI_ARTIFACTS не задан "
                  "— это поведение по умолчанию, а не сбой.")

        browser.close()

    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


# ── PILOT-SYNC-TRUTH-1: экран не обещает того, чего не знает ────────────────


def _conn_line_text(page) -> str:
    """ТОЛЬКО строка состояния, а не вся карточка подключения.

    Читать весь `#conn-box` здесь нельзя, и это не придирка: в карточке стоит
    кнопка «Подключить», и проверка «подключение названо подключённым»
    зеленела бы на ней — то есть на неизменённой вёрстке. Берём ровно ту
    строку, которую рисует `renderConnection` под состояние.
    """
    return page.evaluate(
        "() => { var b = document.getElementById('conn-box');"
        " var row = b ? b.querySelector('div') : null;"
        " return row ? row.textContent : ''; }") or ""


def _season_help(page_src: str) -> str:
    """Куски страницы, где сезонная колонка объясняется человеку.

    Это подпись «—» (`SEA_NA`) и абзац справки про Зиму/Весну/Лето/Осень. Обе
    строки статические: состояния синка они не видят и условными быть не могут,
    поэтому обещание из них просто убрано. Склеиваем оба места, чтобы проверка
    смотрела на текст ДЛЯ ЧЕЛОВЕКА, а не на весь исходник заодно с
    комментариями разработчика.
    """
    out = []
    for needle in ("Сезон ещё не загружен", "Зима/Весна/Лето/Осень"):
        i = page_src.find(needle)
        if i >= 0:
            out.append(page_src[i:i + 400])
    return " | ".join(out)


def _conn_box_text(page) -> str:
    """Вся карточка — для проверок про соседние пояснения (например, демо)."""
    return page.evaluate(
        "() => { var b = document.getElementById('conn-box');"
        " return b ? b.textContent : ''; }") or ""


def _sync_line_text(page) -> str:
    return page.evaluate(
        "() => { var s = document.getElementById('sync-status-line');"
        " return s ? s.textContent : ''; }") or ""


def _pilot_sync_truth(page, base, check, errors) -> None:
    print("\n== PILOT-SYNC-TRUTH-1: «Настройки» не выдают запись подключения "
          "за здоровье источника ==")

    def settings_with(conn):
        def handler(route):
            resp = route.fetch()
            data = resp.json()
            data["connection"] = conn
            data["role"] = "owner"
            route.fulfill(response=resp, json=data)
        return handler

    def sync_status(state, error="", mode="incremental", phase=""):
        def handler(route):
            route.fulfill(json={
                "state": state, "mode": mode, "phase": phase,
                "progress_pct": 100 if state == "done" else 0,
                "detail": "", "error": error,
                "started_at": None, "finished_at": None,
                "diagnostics": {},
            })
        return handler

    # СЛУЧАЙ, РАДИ КОТОРОГО ВСЁ И ДЕЛАЕТСЯ, и он воспроизводит РЕАЛЬНЫЙ
    # сценарий, а не абстрактный «active + error»: первичная загрузка дошла до
    # `finalize-lite`, открыла сервис на частичной истории и ПРОСТАВИЛА
    # `conn.last_sync_at` (`ms_sync.py:1556` → `_activate_connection`), а
    # затем догрузка истории упала — `state="error"`, `phase="history"`, дата
    # осталась. На сервере это состояние достижимо и закреплено:
    # `tests/test_sync.py`, случай (c).
    page.route("**/api/settings", settings_with(
        {"kind": "moysklad", "status": "active",
         "last_sync_at": "2026-09-01T10:00:00"}))
    page.route("**/api/sync/status",
               sync_status("error",
                           "История загружена за 10 дней из 60 — "
                           "продолжим автоматически",
                           mode="initial", phase="history"))
    page.goto(f"{base}/settings")
    page.wait_for_timeout(1500)
    conn_text = _conn_line_text(page)
    check("при упавшем синке подпись подключения НЕ говорит «работает»",
          "работает" not in conn_text, conn_text[:200])
    check("но подключение названо подключённым, а не пропало с экрана",
          "одключ" in conn_text, conn_text[:200])
    # Баннер ошибки — существующий механизм, и он ОБЯЗАН остаться: иначе
    # «убрали ложное обещание» превратилось бы в «убрали сообщение об ошибке».
    check("существующий баннер ошибки синхронизации на месте",
          "История загружена за 10 дней" in _sync_line_text(page),
          _sync_line_text(page)[:200])
    # ДАТА НЕ ДОКАЗЫВАЕТ УСПЕХА, и это ровно тот случай, который здесь
    # разыгран (корректив B, тред r4105185067). `conn.last_sync_at` ставит
    # `_activate_connection`, а его зовёт не только успешное завершение:
    # `_finalize_lite` (`ms_sync.py:1556`) вызывает его на ЧАСТИЧНОЙ истории,
    # когда сервис открывается на 25% прогресса, а `sync_state` остаётся
    # `running` со `stage="history"` — так и написано в его докстроке. Если
    # догрузка истории потом падает, `state` становится `error`, а дата
    # остаётся стоять. Достижимость этого состояния доказана не здесь, а на
    # сервере: `tests/test_sync.py`, случай (c) — прерванный первичный синк
    # даёт `state=error` при `connection.status == "active"`.
    #
    # Значит подпись обязана быть НЕЙТРАЛЬНОЙ. «Последняя успешная
    # синхронизация» в этот момент — неправда, и прежняя редакция этой
    # проверки её закрепляла, требуя слова «успешная».
    check("дата НЕ выдаётся за доказательство успешной синхронизации",
          "спешн" not in conn_text, conn_text[:200])
    check("и названа нейтрально — как время обновления данных",
          "обновление данных" in conn_text, conn_text[:200])
    page.unroute("**/api/sync/status")
    page.unroute("**/api/settings")

    # Остальные состояния записи не должны пострадать.
    page.route("**/api/settings", settings_with(
        {"kind": "moysklad", "status": "pending", "last_sync_at": None}))
    page.route("**/api/sync/status", sync_status("idle"))
    page.goto(f"{base}/settings")
    page.wait_for_timeout(1200)
    pending_text = _conn_line_text(page)
    check("«ожидает синхронизации» осталось как было",
          "жидает синхронизации" in pending_text, pending_text[:200])
    page.unroute("**/api/sync/status")
    page.unroute("**/api/settings")

    page.route("**/api/settings", settings_with(
        {"kind": "demo", "status": "active", "last_sync_at": None}))
    page.route("**/api/sync/status", sync_status("done"))
    page.goto(f"{base}/settings")
    page.wait_for_timeout(1200)
    check("демо тоже не объявляется «работающим» источником",
          "работает" not in _conn_line_text(page), _conn_line_text(page)[:200])
    check("и объяснение про синтетические данные осталось",
          "интетическ" in _conn_box_text(page), _conn_box_text(page)[:200])
    page.unroute("**/api/sync/status")
    page.unroute("**/api/settings")

    print("\n== PILOT-SYNC-TRUTH-1: «Оборачиваемость» не обещает фоновую "
          "догрузку и рост цифр ==")

    def freshness(coverage_days, sync_state, window=730):
        def handler(route):
            route.fulfill(json={
                "connected": True,
                "last_sale_date": "2026-09-01",
                "last_stock_date": "2026-09-01",
                "sync_state": sync_state,
                "sync_error": "",
                "sync_finished_at": None,
                "coverage_days": coverage_days,
                "coverage_start": "2026-06-01",
                "history_days": window,
                "turnover_window_days": window,
            })
        return handler

    def titles():
        return page.evaluate("""() => {
          var t = document.getElementById('th-turnover');
          var d = document.getElementById('th-dis');
          var p = document.getElementById('th-turnover-period');
          return {turn: t ? t.title : '', dis: d ? d.title : '',
                  period: p ? p.textContent : ''};
        }""")

    def load_turnover(cov, state, window=730):
        page.route("**/api/freshness", freshness(cov, state, window))
        page.goto(f"{base}/turnover")
        page.wait_for_timeout(2500)
        out = titles()
        page.unroute("**/api/freshness")
        return out

    # 1. Неполное окно и синк НЕ идёт — ни одного обещания продолжения.
    for state in ("idle", "error", "done"):
        t = load_turnover(90, state)
        both = t["turn"] + " | " + t["dis"]
        check(f"[{state}] неполное окно не обещает фоновую догрузку",
              "догружается" not in both and "догружаетс" not in both,
              both[:260])
        check(f"[{state}] и прямо говорит, что синхронизация сейчас не идёт",
              "синхронизация сейчас не идёт" in both.lower(), both[:260])
        check(f"[{state}] и по-прежнему называет реальное окно",
              "90" in t["turn"], t["turn"][:200])

    # 2. Обещания РОСТА не должно быть НИ В ОДНОМ состоянии: оборачиваемость
    #    это выручка ÷ дни в стоке, и догруженная история двигает обе части —
    #    число может и упасть.
    for state in ("running", "idle", "error", "done"):
        t = load_turnover(90, state)
        both = t["turn"] + " | " + t["dis"]
        check(f"[{state}] нет обещания, что цифры вырастут",
              "вырастут" not in both and "вырастет" not in both, both[:260])

    # 3. Пока синк ИДЁТ — называется САМ ФАКТ синхронизации и ничего сверх.
    #
    # Корректив A по треду r4104781298. Прежняя редакция говорила «история
    # сейчас догружается», то есть обещала догрузку СТАРОГО недостающего окна.
    # `state="running"` этого не означает: `start_sync` публикует его для обоих
    # режимов (`ms_sync.py:1144`), а `_run_incremental` идёт от сегодня назад на
    # разрыв и нижнюю границу истории не двигает (`ms_sync.py:1924-1948`).
    # Режима наружу нет вовсе — `/api/freshness` отдаёт только `state`
    # (`routes_extra.py:293`). Ночной и почасовой прогоны планировщика при этом
    # именно инкрементальные (`scheduler.py:181`, `:253`), то есть случай
    # обычный, а не редкий.
    #
    # Поэтому текст говорит то, что состояние действительно утверждает:
    # синхронизация идёт. Что именно она грузит — экран не знает и не выдумывает.
    t = load_turnover(90, "running")
    both = t["turn"] + " | " + t["dis"]
    check("[running] сам факт идущей синхронизации назван",
          "синхронизация сейчас идёт" in both.lower(), both[:260])
    check("[running] и НЕ обещана догрузка недостающей истории",
          "догружа" not in both, both[:260])

    # 4. Полное окно — подсказка прежняя, без приписок про неполноту.
    t = load_turnover(730, "done")
    check("полное окно: подпись периода прежняя",
          "2 года" in t["period"], t["period"][:120])
    check("полное окно: в подсказке нет речи про неполную историю",
          "догружа" not in (t["turn"] + t["dis"])
          and "не идёт" not in (t["turn"] + t["dis"]),
          (t["turn"] + " | " + t["dis"])[:260])

    # 5. Покрытие НЕИЗВЕСТНО (нулей быть не должно, но они бывают) — экран не
    #    имеет права объявить это полными двумя годами.
    t = load_turnover(0, "idle")
    check("неизвестное покрытие не выдаётся за «за 2 года» в подписи периода",
          "2 года" not in t["period"], t["period"][:120])
    # И в подсказках тоже: подпись можно было бы поправить, а title оставить —
    # тогда обещание полного окна просто переехало бы под курсор.
    check("и подсказки колонок при этом не обещают полные два года",
          "2 года" not in t["turn"] and "2 года" not in t["dis"],
          (t["turn"] + " | " + t["dis"])[:260])
    check("и сказано, что глубина истории неизвестна",
          "еизвестн" in (t["turn"] + " " + t["dis"] + " " + t["period"]),
          (t["period"] + " | " + t["turn"])[:260])

    # ── Сезонные подписи: тот же класс обещания, тот же файл ──────────────
    #
    # `SEA_NA` и абзац справки про сезоны обещали фоновую догрузку и что
    # «цифра появится сама». Обе строки СТАТИЧЕСКИЕ — состояния синка они не
    # видят вовсе, поэтому условными их сделать нечем, и обещание просто
    # убрано. Проверка здесь ИСХОДНАЯ, а не поведенческая, и названа так
    # честно: она доказывает, что страница больше не отдаёт этих слов
    # браузеру, а не то, что тултип отрисовался на конкретной ячейке —
    # для последнего нужен сезон, которого в демо-данных может не быть.
    page_src = page.evaluate("() => document.documentElement.outerHTML") or ""
    check("страница «Оборачиваемость» не обещает, что цифра сезона появится сама",
          "цифра появится сама" not in page_src,
          page_src[page_src.find("Сезон ещё не загружен"):][:200])
    # Проверяется ИМЕННО сезонное обещание, а не подстрока «догружается фоном»
    # где угодно в исходнике: она законно встречается в комментариях кода, и
    # widescale-поиск по ним ловил бы не обещание пользователю, а пояснение
    # разработчику. Условный текст про идущую загрузку выше — тоже законный,
    # и запрещать его вообще было бы неверно.
    check("и сезонная подпись не обещает фоновую догрузку",
          "догружается фоном" not in _season_help(page_src),
          _season_help(page_src)[:220])


# ── PILOT-BROWSER-JOURNEY-1: путь оператора целиком в браузере ─────────────
#
# Зачем отдельный блок, если набор и так «браузерный». Проверки выше стартуют
# с готового аккаунта: регистрация и подключение демо делаются httpx-клиентом
# (строки 179-182) ДО того, как браузер вообще существует (`sync_playwright`
# — строка 184, запуск Chromium — 186), а браузер получает только готовые
# cookie. Это законно для тех проверок — им нужен вход в состояние, а не сам
# вход, — но как доказательство сквозного пути это не годится, и мой прошлый
# отчёт выдал его за такое доказательство ошибочно.
#
# Разница не теоретическая. В форме регистрации есть обязательный чекбокс
# согласия: браузер без него submit не отправит, а httpx про него не знает
# вовсе. Точно так же демо-подключение в продукте — это кнопка, анимация на
# ~2,75 с и переход по `location.href`, а не один POST.
#
# Здесь путь проходится так, как его проходит человек: форма регистрации →
# редиректы продукта → кнопка демо → переход по ссылке в меню → правка
# ростовки в раскрытой строке → перезагрузка и повторное открытие →
# выгрузка файла кнопкой и разбор самой книги.
#
# Ничего из того, что доказывается, не подменяется: сервер, база и выгрузка
# настоящие. Синтетические фикстуры стоят только там, где речь о ВНЕШНЕМ
# источнике (состояние синхронизации) — и только в блоке про подключение.

JOURNEY_EMAIL = "journey@test.io"
JOURNEY_PASSWORD = "journey-secret-123"
JOURNEY_ORG = "Бренд Путь"
JOURNEY_USER = "Оператор Путь"
MOBILE_WIDTH = 390


def _artifact_dir() -> "Path | None":
    """Каталог для снимков экрана: по умолчанию ВЫКЛЮЧЕН.

    Снимки пишутся, только если человек сам назвал каталог в
    OBOROT_UI_ARTIFACTS. Путь обязан быть абсолютным и лежать ВНЕ репозитория:
    снимок, упавший внутрь рабочего дерева, рано или поздно уезжает в коммит.

    Неверный путь — это отказ, а не молчаливый пропуск. Молчаливый пропуск
    хуже отсутствия снимков: человек считает, что картинки у него есть, и
    делает по ним вывод, которого никто не делал.
    """
    raw = (os.environ.get("OBOROT_UI_ARTIFACTS") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError(
            f"OBOROT_UI_ARTIFACTS={raw!r}: нужен АБСОЛЮТНЫЙ путь — "
            "относительный разрешился бы от текущего каталога запуска.")
    if path.is_relative_to(ROOT):
        raise RuntimeError(
            f"OBOROT_UI_ARTIFACTS={raw!r} лежит внутри репозитория ({ROOT}). "
            "Снимки экрана туда писать нельзя — назовите каталог вне дерева.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _shot(page, adir, shots: list, name: str) -> None:
    """Снимок экрана, если каталог задан. Абсолютные пути копятся для отчёта."""
    if adir is None:
        return
    target = adir / f"{name}.png"
    page.screenshot(path=str(target), full_page=True)
    shots.append(str(target.resolve()))


def _close_hint(page) -> None:
    """Подсказка-модалка перехватывает клики — закрываем, если открыта."""
    try:
        is_open = page.evaluate(
            "() => { const o = document.getElementById('hint-overlay');"
            " return !!o && o.classList.contains('open'); }")
    except Exception:  # noqa: BLE001 — страница ещё грузится, подсказки нет
        return
    if is_open:
        page.click("#hint-close")
        page.wait_for_timeout(200)


def _click_past_hint(page, locator, what: str, timeout_ms: int = 30000) -> None:
    """Клик, закрывая подсказку первого визита, если она всплыла.

    Подсказка открывается не сразу: `templates/_hints.html` показывает её
    только после трёх запросов (`/api/hints/seen`, прогресс, уроки), и когда
    именно они ответят — от прогона к прогону разное. Разовое «закрыть перед
    кликом» поэтому ненадёжно: модалка успевает появиться ПОСЛЕ него и
    перехватить клик. Человек в этом месте закрывает подсказку и жмёт снова —
    тест делает ровно это.

    Это не обход дефекта: модалка первого визита — штатное поведение продукта,
    и падение на ней означало бы проверку таймера подсказки, а не пути.
    """
    deadline = time.time() + timeout_ms / 1000
    last_error = None
    while time.time() < deadline:
        _close_hint(page)
        try:
            locator.click(timeout=2000)
            return
        except Exception as exc:  # noqa: BLE001 — причина уйдёт в отчёт ниже
            last_error = exc
            page.wait_for_timeout(200)
    raise RuntimeError(f"клик по {what} не прошёл за {timeout_ms} мс: {last_error}")


def _click_through_hint(page, selector: str, timeout_ms: int = 30000) -> None:
    _click_past_hint(page, page.locator(selector).first, repr(selector), timeout_ms)


def _register_in_browser(page, base: str) -> None:
    """Регистрация формой, а не POST'ом: со всеми полями, которые видит человек.

    Обязательное согласие — отдельный чекбокс в форме. Без него браузер submit
    не отправит вообще, и это ровно та часть пути, которой у API-варианта
    никогда не было.
    """
    page.goto(f"{base}/register")
    page.fill("#name", JOURNEY_USER)
    page.fill("#org_name", JOURNEY_ORG)
    page.fill("#email", JOURNEY_EMAIL)
    page.fill("#password", JOURNEY_PASSWORD)
    page.check("form[action='/register'] input[type=checkbox]")
    with page.expect_navigation(wait_until="load", timeout=30000):
        page.click("form[action='/register'] button[type=submit]")


def _open_sized_row(page):
    """Раскрыть первую строку, у которой ЕСТЬ размерная сетка.

    Безразмерные позиции в демо встречаются, и у них полей ростовки нет вовсе:
    брать «просто первую строку» — значит иногда искать поле, которого продукт
    здесь и не рисует, и объявлять это дефектом. Возвращает (base_name, index)
    или (None, -1), если сеток нет ни у одной строки.
    """
    rows = page.locator("#tbody tr[data-base]")
    total = min(rows.count(), 12)
    for i in range(total):
        base_name = rows.nth(i).get_attribute("data-base")
        _expand_row(page, i)
        if page.locator(".sub-panel input.size-rec").count() > 0:
            return base_name, i
        _expand_row(page, i)  # свернуть обратно
    return None, -1


def _expand_row(page, index: int) -> None:
    _click_past_hint(page, page.locator("#tbody tr[data-base] .expander").nth(index),
                     f"раскрытию строки #{index}")
    page.wait_for_timeout(250)


def _goto_plan(page, base: str) -> None:
    """Открыть «Заказ» и дождаться отрисованной таблицы, а не поспать.

    Ждём именно строки с `data-base`: в `#tbody` изначально стоит заглушка
    «Загрузка…», и проверка «в таблице что-то есть» зеленела бы на ней.
    """
    _close_hint(page)
    page.wait_for_function(
        "() => document.querySelectorAll('#tbody tr[data-base]').length > 0",
        timeout=45000)
    _close_hint(page)


def _size_input(page, size: str):
    return page.locator(f".sub-panel input.size-rec[data-size='{size}']").first


def _parse_replenish_workbook(path: str) -> dict:
    """Книга «Что заказать» → {позиция: {размер: (кол-во, примечание)}}.

    Разбирается настоящий файл, который скачала страница, а не то, что тест
    сам себе положил: строки размеров в книге идут отступом «— S» под строкой
    позиции (app/export_xlsx.py, _replenish_sheet).
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    sheet = wb["Что заказать"] if "Что заказать" in wb.sheetnames else wb.worksheets[0]
    parsed: dict = {}
    current = None
    for row in sheet.iter_rows(min_row=3, values_only=True):
        first = row[0]
        if not isinstance(first, str) or not first.strip():
            continue
        if first.startswith("—"):
            if current is None:
                continue
            size = first.lstrip("—").strip()
            note = row[14] if len(row) > 14 else ""
            parsed[current][size] = (row[10], note or "")
        elif first.startswith("Итого"):
            current = None
        else:
            current = first
            parsed.setdefault(current, {})
    wb.close()
    return parsed


def _app_paths() -> list:
    """Все маршруты приложения, включая вложенные роутеры.

    Плоского `app.routes` тут мало: FastAPI держит подключённые роутеры
    обёртками (`_IncludedRouter`), у которых своего `path` нет, а настоящие
    маршруты лежат внутри. Обход только верхнего уровня даёт пустой список —
    и заявление «такой выгрузки нет» оказалось бы верным просто потому, что
    тест никуда не заглянул. Обходим вглубь.
    """
    def walk(routes, depth=0):
        found = []
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                found.append(path)
            if depth < 5:
                nested = getattr(route, "routes", None)
                if nested:
                    found += walk(nested, depth + 1)
                original = getattr(route, "original_router", None)
                if original is not None:
                    found += walk(getattr(original, "routes", []), depth + 1)
        return found

    return sorted(set(walk(oborot_app.routes)))


def _export_surface(paths: list) -> list:
    return sorted(p for p in paths if p.endswith(".xlsx"))


def _browser_journey(browser, base: str, shots: list) -> None:  # noqa: C901
    """Регистрация → демо → план → сохранение → перезаход → выгрузка. В браузере."""
    from playwright.sync_api import TimeoutError as PWTimeoutError

    adir = _artifact_dir()
    ctx = browser.new_context(viewport={"width": 1400, "height": 900},
                              accept_downloads=True)
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))

    print("\n== Путь в браузере: регистрация формой ==")
    _register_in_browser(page, base)
    # Продукт сам решает, куда вести: /register отвечает 303 на «/», а «/» без
    # подключения уводит на /onboarding. Проверяем, что пришли именно туда,
    # куда ведёт продукт, а не туда, куда тест сходил бы сам.
    check("после формы регистрации человек оказался на подключении данных",
          page.url.rstrip("/").endswith("/onboarding"), page.url)
    check("сессия создана самим браузером, а не подложена тестом",
          any(c["name"] == "oborot_session" for c in ctx.cookies()),
          str([c["name"] for c in ctx.cookies()]))
    _shot(page, adir, shots, "01-onboarding-1400")

    print("\n== Путь в браузере: демо-данные кнопкой ==")
    with page.expect_navigation(url=lambda u: "/onboarding" not in u, timeout=60000):
        _click_through_hint(page, "#btn-connect-demo")
    check("после демо-подключения продукт увёл на «Оборачиваемость»",
          page.url.rstrip("/").endswith("/turnover"), page.url)
    _close_hint(page)
    _shot(page, adir, shots, "02-turnover-1400")

    print("\n== Путь в браузере: переход в план по ссылке меню ==")
    with page.expect_navigation(timeout=60000):
        _click_through_hint(page, "a[href='/replenish']")
    check("ссылка меню привела на «Заказ»",
          page.url.rstrip("/").endswith("/replenish"), page.url)
    _goto_plan(page, base)

    base_name, row_i = _open_sized_row(page)
    if base_name is None:
        check("в демо-данных нашлась позиция с размерной сеткой", False,
              "ни у одной из первых строк нет полей ростовки")
        ctx.close()
        return
    check("в демо-данных нашлась позиция с размерной сеткой",
          page.locator(".sub-panel input.size-rec").count() > 0, base_name[:60])

    print("\n== Путь в браузере: правка ростовки и сохранение ==")
    inp = page.locator(".sub-panel input.size-rec").first
    size = inp.get_attribute("data-size")
    rec = int(inp.input_value() or "0")
    target = rec + 7 if rec + 7 <= 9999 else max(0, rec - 7)
    with page.expect_response(
            lambda r: "/api/replenish-draft" in r.url
            and r.request.method == "POST", timeout=30000) as saved:
        inp.fill(str(target))
        inp.press("Tab")
    check("страница сама отправила правку на сервер",
          saved.value.status == 200, f"HTTP {saved.value.status}")
    # Ждём подпись, но НЕ падаем на таймауте: молчаливое исключение здесь
    # унесло бы весь набор в трассировку вместо честного FAIL с текстом,
    # который реально стоял на экране.
    try:
        page.wait_for_function(
            "() => (document.getElementById('draft-note')||{}).textContent"
            " === 'Правки сохранены'", timeout=15000)
    except PWTimeoutError:
        pass
    draft_note = (page.text_content("#draft-note") or "").strip()
    check("человек увидел, что правка сохранена",
          draft_note == "Правки сохранены",
          f"на экране: {draft_note[:120]!r} · "
          f"{base_name[:40]} · {size} · {rec} → {target}")
    _shot(page, adir, shots, "03-plan-edited-1400")

    print("\n== Путь в браузере: правка переживает перезагрузку ==")
    page.reload()
    _goto_plan(page, base)
    _expand_row(page, row_i)
    reloaded_base = page.locator("#tbody tr[data-base]").nth(row_i).get_attribute("data-base")
    check("после перезагрузки это та же позиция", reloaded_base == base_name,
          f"было {base_name[:40]}, стало {str(reloaded_base)[:40]}")
    after_reload = _size_input(page, size).input_value()
    check("после перезагрузки в поле стоит сохранённое число",
          after_reload == str(target), f"ожидали {target}, в поле {after_reload}")
    check("строка помечена как правленная вручную",
          page.locator("#tbody tr[data-base]").nth(row_i).locator(".editmark").count() > 0)
    check("счётчик сохранённых правок виден",
          page.locator("#btn-reset-drafts").is_visible())

    print("\n== Путь в браузере: правка переживает повторное открытие ==")
    page2 = ctx.new_page()
    page2.on("pageerror", lambda e: errors.append(str(e)))
    page2.goto(f"{base}/replenish")
    _goto_plan(page2, base)
    _expand_row(page2, row_i)
    reopened = _size_input(page2, size).input_value()
    check("в новой вкладке — та же позиция и то же число",
          page2.locator("#tbody tr[data-base]").nth(row_i)
          .get_attribute("data-base") == base_name and reopened == str(target),
          f"ожидали {target}, в поле {reopened}")
    page2.close()

    print("\n== Путь в браузере: выгрузка кнопкой и разбор книги ==")
    with page.expect_download(timeout=60000) as dl:
        _click_through_hint(page, "#btn-export-xlsx")
    download = dl.value
    check("файл отдан под именем, которое человек видит в продукте",
          download.suggested_filename == "Что заказать.xlsx",
          download.suggested_filename)
    out_dir = tempfile.mkdtemp(prefix="oborot-journey-")
    saved_xlsx = os.path.join(out_dir, "replenish.xlsx")
    download.save_as(saved_xlsx)
    book = _parse_replenish_workbook(saved_xlsx)
    sizes_in_book = book.get(base_name, {})
    cell = sizes_in_book.get(size)
    check("позиция и её размер есть в скачанной книге", cell is not None,
          f"позиция {base_name[:40]}, размер {size}, "
          f"в книге размеры: {sorted(sizes_in_book)[:8]}")
    if cell is not None:
        check("в книге стоит сохранённое человеком количество, а не расчёт",
              cell[0] == target, f"ожидали {target}, в книге {cell[0]} (расчёт был {rec})")
        check("книга подписывает, что число правлено вручную",
              "правлено вручную" in str(cell[1]), str(cell[1])[:120])
    shutil.rmtree(out_dir, ignore_errors=True)

    print("\n== Какая именно выгрузка существует, а какой в продукте нет ==")
    # Скачанный артефакт — это РЕКОМЕНДАЦИИ «что заказать» в состоянии экрана
    # (app/routes_extra.py:744-754: расчёт → условия производства → ручные
    # правки ростовки). Это НЕ выгрузка сохранённого плана заказа.
    #
    # Разница здесь не словесная. Сохранённый план заказа в продукте
    # существует отдельной сущностью: POST /api/order-plan кладёт OrderPlan с
    # брифом и расчётом, есть история и превращение плана в заказ
    # (/api/order-plan/{id}/apply). Выгрузки у этой сущности нет ни одной.
    #
    # Поэтому пробел называется ровно так: план заказа в продукте есть,
    # выгрузки плана заказа — нет. Ни того, ни другого тест не придумывает и
    # не добавляет; факт берётся из таблицы маршрутов самого приложения.
    paths = _app_paths()
    surface = _export_surface(paths)
    check("таблица маршрутов вообще прочитана (иначе «ничего нет» ничего не значит)",
          len(paths) > 50 and len(surface) > 0, f"{len(paths)} маршрутов, выгрузок {len(surface)}")
    check("скачанное — выгрузка рекомендаций «Что заказать»",
          "/api/export/replenish.xlsx" in surface, str(surface))
    check("сохранённый план заказа в продукте есть",
          "/api/order-plan" in paths and "/api/order-plan/history" in paths,
          str([p for p in paths if "order-plan" in p][:4]))
    # Именно префиксы, а не поиск слова где угодно: /api/supply/planning/…
    # — это планирование МАТЕРИАЛОВ из другой части продукта, и путать его с
    # планом заказа значило бы закрыть пробел на бумаге.
    order_exports = [p for p in surface
                     if p.startswith("/api/order-plan") or p.startswith("/api/orders")]
    check("а выгрузки сохранённого плана заказа нет — существующий пробел продукта",
          not order_exports, f"выгрузки: {surface}")

    check("ни одной ошибки в консоли за весь путь", not errors, str(errors[:2]))
    ctx.close()


SYNC_ERROR_TEXT = "Синхронизация прервана: источник не ответил"


def _sync_state_routes(page, state: dict, settings_patch: dict) -> None:
    """Синтетическое состояние ВНЕШНЕГО источника — и только оно.

    Подменяются два ответа: карточка подключения (иначе блок синхронизации не
    рисуется вовсе — он только для владельца с подключённым МойСкладом) и
    состояние синка. Сам экран, его разметка и его логика — настоящие; это
    ровно тот приём, которым в этом наборе уже проверяется DATA-8.

    /api/settings именно ДОПОЛНЯЕТСЯ, а не заменяется: страница читает оттуда
    много несвязанных настроек, и выдуманный целиком ответ проверял бы
    отрисовку выдуманного продукта.
    """
    def settings_handler(route):
        resp = route.fetch()
        data = resp.json()
        data.update(settings_patch)
        route.fulfill(response=resp, json=data)

    page.route("**/api/settings", settings_handler)
    page.route("**/api/sync/status", lambda route: route.fulfill(json=state))


def _fits_viewport(page, selector: str, width: int) -> bool:
    """Элемент виден целиком в окне и не обрезан по горизонтали.

    Проверка именно про чтение: на 390 сообщение об ошибке, уехавшее за
    правый край, формально «на странице есть», а человеку недоступно.
    """
    return page.evaluate(
        "([sel, w]) => { const el = document.querySelector(sel);"
        " if (!el) return false;"
        " const r = el.getBoundingClientRect();"
        " if (r.width <= 0 || r.height <= 0) return false;"
        " return r.left >= -1 && r.right <= w + 1"
        "   && el.scrollWidth <= el.clientWidth + 1; }",
        [selector, width])


def _selfcheck_fits_viewport(page, width: int) -> None:
    """Проверка обрезки обязана уметь возвращать False — иначе она украшение.

    Ставим на страницу два заведомо разных элемента: один шире окна, второй
    нормальный, — и убеждаемся, что помощник их различает. Без этого «не
    обрезано» зеленело бы всегда, в том числе на действительно обрезанном
    экране, и проверка 390 не значила бы ничего.
    """
    page.evaluate(
        "(w) => { const bad = document.createElement('div');"
        " bad.id = '__probe_bad'; bad.style.cssText ="
        " 'position:fixed;left:0;top:0;width:' + (w * 3) + 'px;height:20px';"
        " bad.textContent = 'x';"
        " const good = document.createElement('div');"
        " good.id = '__probe_good'; good.style.cssText ="
        " 'position:fixed;left:0;top:40px;width:50px;height:20px';"
        " good.textContent = 'x';"
        " document.body.append(bad, good); }", width)
    check(f"[{width}] проверка обрезки видит вылезший за экран элемент",
          _fits_viewport(page, "#__probe_bad", width) is False)
    check(f"[{width}] и не считает обрезанным поместившийся",
          _fits_viewport(page, "#__probe_good", width) is True)
    page.evaluate(
        "() => { ['__probe_bad', '__probe_good'].forEach(id => {"
        " const el = document.getElementById(id); if (el) el.remove(); }); }")


def _connection_states(browser, base: str, width: int, shots: list) -> None:
    """Ошибка подключения, повтор и неполнота — на заданной ширине окна.

    Это НЕ ошибки обновления поставок: там другой экран, другая ручка и другая
    причина. Раньше я предъявил их как доказательство этого пути — ошибочно.
    """
    adir = _artifact_dir()
    tag = f"{width}"
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))

    # Вход настоящей формой, а не подложенной cookie: на узком окне это ещё и
    # единственная проверка того, что войти оттуда вообще можно.
    page.goto(f"{base}/login")
    page.fill("#email", JOURNEY_EMAIL)
    page.fill("#password", JOURNEY_PASSWORD)
    with page.expect_navigation(wait_until="load", timeout=30000):
        page.click("button[type=submit]")
    check(f"[{tag}] вход формой удался",
          "/login" not in page.url, page.url)

    settings_patch = {
        "connection": {"kind": "moysklad", "status": "active", "last_sync_at": None},
        "role": "owner",
    }
    failed = {"state": "error", "mode": "incremental", "phase": "",
              "progress_pct": 0, "detail": "", "error": SYNC_ERROR_TEXT,
              "started_at": None, "finished_at": None,
              "diagnostics": {"sales_docs_skipped_store_unresolved": 0}}

    print(f"\n== [{tag}] подключение: синхронизация упала ==")
    _sync_state_routes(page, failed, settings_patch)
    page.goto(f"{base}/settings")
    page.wait_for_selector("#btn-sync-now", timeout=30000)
    page.wait_for_function(
        "() => (document.getElementById('sync-status-line')||{}).textContent",
        timeout=15000)
    _selfcheck_fits_viewport(page, width)
    line = (page.text_content("#sync-status-line") or "").strip()
    check(f"[{tag}] причину назвал сервер, а не браузер",
          line == SYNC_ERROR_TEXT, line[:160])
    check(f"[{tag}] сообщение об ошибке читается целиком, не обрезано",
          _fits_viewport(page, "#sync-status-line", width), line[:80])
    check(f"[{tag}] после отказа повтор не заблокирован",
          not page.locator("#btn-sync-now").is_disabled())
    _shot(page, adir, shots, f"04-sync-error-{tag}")

    print(f"\n== [{tag}] подключение: повтор — сначала отказ, потом успех ==")
    page.route("**/api/sync/run", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body='{"detail":"Источник снова недоступен"}'))
    _click_through_hint(page, "#btn-sync-now")
    page.wait_for_function(
        "() => !document.getElementById('btn-sync-now').disabled", timeout=15000)
    check(f"[{tag}] неудачный повтор не запирает кнопку навсегда",
          not page.locator("#btn-sync-now").is_disabled())

    running = {"state": "running", "mode": "incremental", "phase": "history",
               "progress_pct": 40, "detail": "Загружаем историю", "error": "",
               "started_at": None, "finished_at": None, "diagnostics": {}}
    page.unroute("**/api/sync/run")
    page.unroute("**/api/sync/status")
    page.route("**/api/sync/status", lambda route: route.fulfill(json=running))
    page.route("**/api/sync/run", lambda route: route.fulfill(
        json={"ok": True, "started": True}))
    with page.expect_response(
            lambda r: "/api/sync/run" in r.url
            and r.request.method == "POST", timeout=30000) as retried:
        _click_through_hint(page, "#btn-sync-now")
    check(f"[{tag}] повтор действительно ушёл на сервер",
          retried.value.status == 200, f"HTTP {retried.value.status}")
    page.wait_for_function(
        "() => { const el = document.getElementById('sync-status-line');"
        " return el && el.textContent.indexOf('Загружаем историю') >= 0; }",
        timeout=20000)
    after_retry = (page.text_content("#sync-status-line") or "").strip()
    check(f"[{tag}] после удачного повтора экран вышел из ошибки",
          SYNC_ERROR_TEXT not in after_retry and "Загружаем историю" in after_retry,
          after_retry[:160])

    print(f"\n== [{tag}] подключение: данные неполные ==")
    incomplete = dict(failed)
    incomplete.update(state="done", error="", progress_pct=100,
                      diagnostics={"sales_docs_skipped_store_unresolved": 12})
    page.unroute("**/api/sync/status")
    page.unroute("**/api/sync/run")
    page.route("**/api/sync/status", lambda route: route.fulfill(json=incomplete))
    page.goto(f"{base}/settings")
    page.wait_for_selector("#btn-sync-now", timeout=30000)
    page.wait_for_function(
        "() => { const b = document.getElementById('sync-diag-hint');"
        " return b && getComputedStyle(b).display !== 'none'; }", timeout=20000)
    hint = (page.text_content("#sync-diag-hint") or "").strip()
    check(f"[{tag}] про неполные данные сказано прямо",
          "неполн" in hint.lower() and "12" in hint, hint[:200])
    check(f"[{tag}] предупреждение о неполноте читается целиком, не обрезано",
          _fits_viewport(page, "#sync-diag-hint", width), hint[:80])
    _shot(page, adir, shots, f"05-sync-incomplete-{tag}")

    check(f"[{tag}] ни одной ошибки в консоли на экране подключения",
          not errors, str(errors[:2]))
    ctx.close()


if __name__ == "__main__":
    sys.exit(main())
