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
            _shell_journey(browser, base, c)
            _actions_clarity(browser, base, c)
            _focus_urgent(browser, base)
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


# ── PILOT-UX-SHELL-1: каркас — меню, выход, демо ────────────────────────────
#
# Меряется то, что видит и нажимает человек, а не `scrollWidth` страницы:
# прежняя лента меню на 390 px не расширяла страницу, но прятала 8–10 из 11
# разделов за краем своей прокрутки без намёка, что их можно докрутить.

SHELL_LINKS = ("/turnover", "/stocks", "/assistant", "/replenish", "/sizes", "/supply",
               "/budget", "/forecast", "/revenue", "/lessons", "/settings")


def _shell_btn(page):
    return page.locator('[data-shell-toggle][aria-controls="app-nav"],'
                        ' [data-shell-toggle][aria-controls="side-nav"]').first


def _shell_nav_boxes(page) -> list:
    """Прямоугольники ссылок меню текущего каркаса (без встроенных табов)."""
    return page.evaluate("""() => {
      const nav = document.getElementById('app-nav') || document.getElementById('side-nav');
      if (!nav) return [];
      return [...nav.querySelectorAll('a[href]')].map(a => {
        const r = a.getBoundingClientRect(); const s = getComputedStyle(a);
        return {href: a.getAttribute('href'), left: r.left, right: r.right, top: r.top,
                bottom: r.bottom, h: r.height, w: r.width, vw: innerWidth, vh: innerHeight,
                shown: r.width > 0 && r.height > 0 && s.visibility !== 'hidden',
                current: a.getAttribute('aria-current')};
      });
    }""")


def _tap_past_hint(page, locator, what: str, timeout_ms: int = 30000) -> None:
    """Касание с той же оговоркой, что и `_click_past_hint`: подсказка первого
    визита всплывает не сразу и может перехватить касание — человек её
    закрывает и касается снова."""
    deadline = time.time() + timeout_ms / 1000
    last_error = None
    while time.time() < deadline:
        _close_hint(page)
        try:
            locator.tap(timeout=2000)
            return
        except Exception as exc:  # noqa: BLE001 — причина уйдёт в отчёт ниже
            last_error = exc
            page.wait_for_timeout(200)
    raise RuntimeError(f"касание {what} не прошло за {timeout_ms} мс: {last_error}")


def _wait_shell(page) -> None:
    """Дождаться УСПЕШНОЙ инициализации shell.js: класс `shell-js` ставит
    сам скрипт последним шагом (корректив REVIEW_REJECT r1)."""
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_function("() => document.documentElement.classList.contains('shell-js')",
                           timeout=30000)
    _close_hint(page)


def _shell_script_failure(browser, base: str, cookies: list) -> None:
    """Корректив REVIEW_REJECT r1 (issuecomment-5862497540): shell.js не дошёл.

    Прежде класс `shell-js` ставила строка в <head> ДО загрузки скрипта, и при
    его сбое меню на 390 px было свёрнуто, а кнопка мертва — ни одной видимой
    ссылки. Здесь браузер сам обрывает запрос /static/shell.js, и проверяется,
    что все 11 разделов видны и переход по ним работает. Контроль — та же
    страница с доставленным скриптом: меню свёрнуто и раскрывается касанием.
    """
    print("\n== PILOT-UX-SHELL-1: shell.js не загрузился — навигация жива ==")
    for path, target in (("/replenish", "/budget"), ("/settings", "/turnover")):
        for failed in (True, False):
            tag = f"390 {path} ({'shell.js оборван' if failed else 'контроль: shell.js есть'})"
            ctx = browser.new_context(viewport={"width": MOBILE_WIDTH, "height": 844},
                                      has_touch=True, is_mobile=True)
            ctx.add_cookies(cookies)
            aborted: list[str] = []
            if failed:
                def _abort(route):
                    aborted.append(route.request.url)
                    route.abort()
                ctx.route("**/static/shell.js", _abort)
            page = ctx.new_page()
            try:
                page.goto(base + path)
                page.wait_for_load_state("load")
                if not failed:
                    _wait_shell(page)
                _close_hint(page)
                has_cls = page.evaluate(
                    "() => document.documentElement.classList.contains('shell-js')")
                btn = _shell_btn(page)
                boxes = {b["href"]: b for b in _shell_nav_boxes(page)}
                if failed:
                    check(f"{tag}: запрос shell.js действительно оборван",
                          any(u.endswith("/static/shell.js") for u in aborted), str(aborted))
                    check(f"{tag}: класса shell-js нет, кнопки меню нет",
                          not has_cls and not btn.is_visible(), f"cls={has_cls}")
                    bad = [h for h in SHELL_LINKS if not (
                        h in boxes and boxes[h]["shown"] and boxes[h]["h"] >= 40
                        and boxes[h]["left"] >= -0.5 and boxes[h]["right"] <= boxes[h]["vw"] + 0.5)]
                    check(f"{tag}: все 11 разделов видны целиком по ширине и ≥ 40 px",
                          not bad, str(bad))
                    nav_id = "app-nav" if path != "/settings" else "side-nav"
                    if bad:
                        # Касаться нечего — это и есть дефект; отчёт, а не
                        # исключение, чтобы проверка base.html тоже прошла.
                        check(f"{tag}: касание пункта без shell.js открывает {target}",
                              False, "пункт не виден — касание невозможно")
                    else:
                        with page.expect_navigation():
                            _tap_past_hint(page, page.locator(f'#{nav_id} a[href="{target}"]'),
                                           target)
                        check(f"{tag}: касание пункта без shell.js открывает {target}",
                              page.url.endswith(target), page.url)
                else:
                    shown = [h for h, b in boxes.items() if b["shown"]]
                    check(f"{tag}: класс shell-js стоит, меню свёрнуто, кнопка видна",
                          has_cls and not shown and btn.is_visible(), f"shown={shown}")
                    _tap_past_hint(page, btn, "кнопка меню")
                    page.wait_for_function(
                        "() => { const n = document.getElementById('app-nav') ||"
                        " document.getElementById('side-nav');"
                        " return !!n && n.classList.contains('open'); }", timeout=10000)
                    opened = [h for h, b in _shell_nav_boxes_map(page).items() if b["shown"]]
                    check(f"{tag}: касание раскрывает все 11 разделов",
                          btn.get_attribute("aria-expanded") == "true"
                          and all(h in opened for h in SHELL_LINKS), str(len(opened)))
            finally:
                ctx.close()


def _shell_nav_boxes_map(page) -> dict:
    return {b["href"]: b for b in _shell_nav_boxes(page)}


def _shell_mobile(browser, base: str, cookies: list) -> None:
    print("\n== PILOT-UX-SHELL-1: меню на 390 px — касания, Escape, переход ==")
    ctx = browser.new_context(viewport={"width": MOBILE_WIDTH, "height": 844},
                              has_touch=True, is_mobile=True)
    ctx.add_cookies(cookies)
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        for path, label, target, target_label in (
                ("/replenish", "Заказ", "/budget", "Бюджет"),
                ("/settings", "Настройки", "/turnover", "Оборачиваемость")):
            tag = f"390 {path}"
            page.goto(base + path)
            _wait_shell(page)
            btn = _shell_btn(page)
            box = btn.bounding_box() or {}
            check(f"{tag}: кнопка меню видна, не ниже 40 px и называет раздел «{label}»",
                  btn.is_visible() and box.get("height", 0) >= 40
                  and label in (btn.text_content() or ""),
                  f"h={box.get('height')} text={btn.text_content()!r}")
            closed = [b for b in _shell_nav_boxes(page) if b["shown"]]
            check(f"{tag}: пока меню закрыто, список разделов свёрнут", not closed,
                  str([b["href"] for b in closed]))
            _tap_past_hint(page, btn, "кнопка меню")
            page.wait_for_function(
                "() => { const n = document.getElementById('app-nav') ||"
                " document.getElementById('side-nav'); return !!n && n.classList.contains('open'); }",
                timeout=10000)
            check(f"{tag}: aria-expanded=true после касания",
                  btn.get_attribute("aria-expanded") == "true")
            boxes = {b["href"]: b for b in _shell_nav_boxes(page)}
            missing = [h for h in SHELL_LINKS if h not in boxes]
            check(f"{tag}: в открытом меню все 11 разделов", not missing, str(missing))
            bad = [h for h, b in boxes.items()
                   if not (b["shown"] and b["h"] >= 40 and b["left"] >= -0.5
                           and b["right"] <= b["vw"] + 0.5)]
            check(f"{tag}: каждый раздел виден целиком по ширине и не ниже 40 px",
                  not bad, str([(h, boxes[h]) for h in bad][:2]))
            current = [h for h, b in boxes.items() if b["current"] == "page"]
            check(f"{tag}: текущий раздел отмечен aria-current (ровно один: {path})",
                  current == [path], str(current))
            page.keyboard.press("Escape")
            focused = page.evaluate("() => document.activeElement &&"
                                    " document.activeElement.hasAttribute('data-shell-toggle')"
                                    " && document.activeElement.getAttribute('aria-controls')")
            check(f"{tag}: Escape закрывает меню и возвращает фокус на кнопку",
                  btn.get_attribute("aria-expanded") == "false"
                  and focused in ("app-nav", "side-nav"), f"focus={focused}")
            # Tab за последний пункт: меню обязано закрыться, а не остаться
            # поверх страницы, пряча элемент, на который ушёл фокус.
            _tap_past_hint(page, btn, "кнопка меню")
            nav_id = btn.get_attribute("aria-controls")
            left = False
            for _ in range(20):
                page.keyboard.press("Tab")
                left = page.evaluate("""(id) => { const a = document.activeElement;
                  const n = document.getElementById(id);
                  return !!a && a !== document.body && !n.contains(a)
                         && !a.hasAttribute('data-shell-toggle'); }""", nav_id)
                if left:
                    break
            check(f"{tag}: фокус ушёл из меню клавишей Tab — меню закрылось",
                  left and btn.get_attribute("aria-expanded") == "false",
                  f"left={left} expanded={btn.get_attribute('aria-expanded')}")
            _tap_past_hint(page, btn, "кнопка меню")
            with page.expect_navigation():
                _tap_past_hint(page, page.locator(f'#{nav_id} a[href="{target}"]'), target)
            _wait_shell(page)
            nbtn = _shell_btn(page)
            check(f"{tag}: касание пункта открыло {target}, меню на новой странице"
                  f" закрыто и называет «{target_label}»",
                  page.url.endswith(target) and nbtn.get_attribute("aria-expanded") == "false"
                  and target_label in (nbtn.text_content() or ""),
                  f"url={page.url} text={nbtn.text_content()!r}")
        page.goto(base + "/replenish")
        _wait_shell(page)
        check("390 /replenish: каркас не расширяет страницу",
              page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1"),
              str(page.evaluate("() => document.documentElement.scrollWidth")))
        ubtn = page.locator('[data-shell-toggle][aria-controls="app-user-pop"]')
        _tap_past_hint(page, ubtn, "меню пользователя")
        out = page.locator('#app-user-pop button[type="submit"]')
        obox = out.bounding_box() or {}
        check("390: меню пользователя открывается касанием, «Выйти» в окне и ≥ 40 px",
              out.is_visible() and obox.get("height", 0) >= 40
              and obox.get("x", -1) >= 0 and obox.get("x", 0) + obox.get("width", 0) <= MOBILE_WIDTH + 0.5,
              str(obox))
        check("390: ошибок в консоли не было", not errors, str(errors[:2])[:200])
    finally:
        ctx.close()


def _shell_desktop_and_logout(browser, base: str, cookies: list) -> None:
    print("\n== PILOT-UX-SHELL-1: десктоп — все разделы видны; выход POST с CSRF ==")
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies(cookies)
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        for path in ("/turnover", "/settings"):
            page.goto(base + path)
            _wait_shell(page)
            boxes = {b["href"]: b for b in _shell_nav_boxes(page)}
            hidden = [h for h in SHELL_LINKS if not (h in boxes and boxes[h]["shown"]
                      and boxes[h]["right"] <= boxes[h]["vw"] + 0.5 and boxes[h]["top"] >= 0
                      and boxes[h]["bottom"] <= boxes[h]["vh"])]
            check(f"1440 {path}: все 11 разделов видны без раскрытия меню", not hidden,
                  str(hidden))
            check(f"1440 {path}: кнопка мобильного меню скрыта",
                  not _shell_btn(page).is_visible())
        page.goto(base + "/turnover")
        _wait_shell(page)
        ubtn = page.locator('[data-shell-toggle][aria-controls="app-user-pop"]')
        _click_past_hint(page, ubtn, "меню пользователя")
        form = page.locator('#app-user-pop form[action="/logout"]')
        token = form.locator('input[name="csrf_token"]').get_attribute("value") or ""
        check("1440 /turnover: в меню пользователя форма POST /logout с csrf_token",
              form.count() == 1 and (form.get_attribute("method") or "").lower() == "post"
              and len(token) > 8, f"token_len={len(token)}")
        page.keyboard.press("Escape")
        check("1440 /turnover: Escape закрывает меню пользователя",
              ubtn.get_attribute("aria-expanded") == "false"
              and not page.locator("#app-user-pop").is_visible())
        _click_past_hint(page, ubtn, "меню пользователя")
        with page.expect_navigation():
            _click_past_hint(page, page.locator('#app-user-pop button[type="submit"]'), "«Выйти»")
        check("«Выйти» со самостоятельной страницы ведёт на /login", page.url.endswith("/login"),
              page.url)
        page.goto(base + "/turnover")
        check("после выхода /turnover снова требует входа", page.url.endswith("/login"),
              page.url)
        check("1440: ошибок в консоли не было", not errors, str(errors[:2])[:200])
    finally:
        ctx.close()


def _shell_demo_truth(base: str) -> None:
    """«Демо-режим» — правда о данных: по `org.demo`, а не по тарифу."""
    import sqlite3
    print("\n== PILOT-UX-SHELL-1: «Демо-режим» по данным, а не по тарифу ==")
    for demo in (True, False):
        cl = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
        email = f"shell-{'demo' if demo else 'real'}@test.io"
        cl.post("/register", data={"name": "Каркас", "email": email,
                                   "password": "secret123", "org_name": "Каркас-" + email})
        if demo:
            cl.post("/api/connect/demo")
        kind = "демо" if demo else "без демо"
        for plan in ("trial", "start"):
            with sqlite3.connect(DB_PATH) as con:
                # Фикстура тарифа — в базе набора, как и в других наборах:
                # публичной ручки «сменить тариф без оплаты» нет и быть не должно.
                con.execute("UPDATE orgs SET plan=? WHERE id IN (SELECT m.org_id FROM"
                            " memberships m JOIN users u ON u.id=m.user_id WHERE u.email=?)",
                            (plan, email))
            for path in ("/turnover", "/replenish", "/settings"):
                html = cl.get(path).text
                check(f"{kind}, тариф {plan}, {path}: «Демо-режим» "
                      f"{'есть' if demo else 'нет'}",
                      ("Демо-режим" in html) is demo)
            settings = cl.get("/settings").text
            check(f"{kind}, тариф {plan}: триал на /settings назван только у триала",
                  ("Триал до" in settings) is (plan == "trial"))
            check(f"{kind}, тариф {plan}: на самостоятельной странице про триал ни слова",
                  "Триал до" not in cl.get("/turnover").text)
        cl.close()


def _shell_journey(browser, base: str, c) -> None:
    cookies = [{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
               for k, v in c.cookies.items()]
    _shell_mobile(browser, base, cookies)
    _shell_script_failure(browser, base, cookies)
    _shell_desktop_and_logout(browser, base, cookies)
    _shell_demo_truth(base)


# ── PILOT-UX-CLARITY-1-ACTIONS: подписи действий и плашка свежести ──────────
#
# Hit-test по ПЯТИ точкам кнопки (центр и четыре угла с отступом 3 px), а не по
# центру: на /assistant (1440×900) фиксированная плашка свежести закрывала ВЕРХ
# кнопки «Дальше →» (плашка 847..882, кнопка 867..903), а центр кнопки (885)
# оставался открытым — проверка по центру была бы зелёной и на дефекте.

HIT5 = """(sel) => [...document.querySelectorAll(sel)].map(b => {
  const r = b.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  const pts = [[r.left + r.width / 2, r.top + r.height / 2], [r.left + 3, r.top + 3],
               [r.right - 3, r.top + 3], [r.left + 3, r.bottom - 3], [r.right - 3, r.bottom - 3]];
  const res = pts.map(([x, y]) => {
    if (y < 0 || y > innerHeight || x < 0 || x > innerWidth) return 'out';
    const h = document.elementFromPoint(x, y);
    if (!h) return 'null';
    if (h === b || b.contains(h)) return 'self';
    const chip = document.getElementById('fresh-chip');
    return (chip && (h === chip || chip.contains(h))) ? 'fresh-chip' : (h.id || h.className || h.tagName);
  });
  return {text: b.textContent.trim().slice(0, 24), res: res};
}).filter(Boolean)"""


def _chip_state(page) -> dict:
    return page.evaluate("""() => { const c = document.getElementById('fresh-chip');
      if (!c) return null; const r = c.getBoundingClientRect();
      return {pos: getComputedStyle(c).position, shown: c.style.display === 'flex',
              top: r.top, bottom: r.bottom, vh: innerHeight, cls: c.className,
              href: c.getAttribute('href'), text: c.textContent}; }""")


def _wait_chip(page) -> None:
    page.wait_for_function("() => { const c = document.getElementById('fresh-chip');"
                           " return !!c && c.style.display === 'flex'; }", timeout=30000)


def _actions_clarity(browser, base: str, c) -> None:
    import sqlite3
    print("\n== PILOT-UX-CLARITY-1-ACTIONS: «Заказ позиции», «Едет», плашка свежести ==")
    cookies = [{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
               for k, v in c.cookies.items()]
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies(cookies)
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        # 1. «Заказ позиции»: честная подпись, та же ручная отметка, заказа нет.
        prods = c.get("/api/sizes/products").json()["products"]
        name = prods[0]["base_name"] if isinstance(prods[0], dict) else prods[0]
        page.goto(f"{base}/sizes")
        _close_hint(page)
        page.fill("#prod-search", name)
        page.dispatch_event("#prod-search", "input")
        page.wait_for_timeout(400)
        page.evaluate("() => { const el=document.querySelector('#dd [data-i]'); if(el)"
                      " el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true})); }")
        btn = page.locator("#send-btn")
        btn.wait_for(state="visible", timeout=30000)
        label = (btn.text_content() or "").strip()
        check("«Заказ позиции»: кнопка называет действие «Добавить в «Заказано»», без «отправлен»",
              label == "Добавить в «Заказано»" and "отправ" not in label.lower(), label)
        check("«Заказ позиции»: кнопка нейтральная, а не зелёная «готово»",
              "neutral" in (btn.get_attribute("class") or ""))
        with sqlite3.connect(DB_PATH) as con:
            org = con.execute("SELECT m.org_id FROM memberships m JOIN users u ON u.id=m.user_id"
                              " WHERE u.email='ui@test.io'").fetchone()[0]
            row = con.execute("SELECT qty FROM ordered_qty WHERE org_id=? AND base_name=?",
                              (org, name)).fetchone()
        before = float(row[0]) if row else 0.0
        orders_before = len(c.get("/api/orders").json().get("orders", []))
        held: list = []
        page.route("**/api/ordered/add", lambda r: held.append(r))
        asked: list = []
        page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
        _click_past_hint(page, btn, "«Добавить в «Заказано»»")
        try:
            page.wait_for_function("() => document.getElementById('send-btn').disabled",
                                   timeout=5000)
        except Exception:  # noqa: BLE001 — отсутствие состояния ожидания проверяется ниже
            pass
        pending = (btn.text_content() or "").strip()
        check("«Заказ позиции»: пока запрос идёт, кнопка выключена и пишет «Добавляю…»",
              btn.is_disabled() and pending.startswith("Добавляю"), pending)
        check("«Заказ позиции»: подтверждение говорит, что заказ на производство не создаётся",
              bool(asked) and "не создаётся" in asked[0] and "«Заказано»" in asked[0], str(asked)[:200])
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(100)
        sent = held[0].request.post_data_json if held else {}
        with page.expect_response(lambda r: r.url.endswith("/api/ordered/add")) as resp:
            held[0].continue_()
        page.unroute("**/api/ordered/add")
        try:
            page.wait_for_function("() => /Добавлено в «Заказано»/.test(document.getElementById("
                                   "'send-btn').textContent)", timeout=15000)
        except Exception:  # noqa: BLE001 — подпись успеха проверяется ниже
            page.wait_for_timeout(500)
        with sqlite3.connect(DB_PATH) as con:
            after = float(con.execute("SELECT qty FROM ordered_qty WHERE org_id=? AND base_name=?",
                                      (org, name)).fetchone()[0])
        qty = float(sent.get("qty") or 0)
        check("«Заказ позиции»: та же ручка и то же тело — «Заказано» выросло ровно на сумму",
              resp.value.status == 200 and sent.get("base_name") == name and qty > 0
              and abs(after - before - qty) < 1e-9, f"before={before} after={after} qty={qty}")
        check("«Заказ позиции»: заказ на производство НЕ создан",
              len(c.get("/api/orders").json().get("orders", [])) == orders_before)
        check("«Заказ позиции»: успех назван «Добавлено в «Заказано»»",
              "Добавлено в «Заказано»" in (btn.text_content() or ""), btn.text_content())

        # 2. «Активный сток»: «Едет к нам» не выдаёт ручные отметки за заказы.
        page.goto(f"{base}/stocks")
        _close_hint(page)
        page.wait_for_function("() => [...document.querySelectorAll('.ms-card')]"
                               ".some(e => /Едет к нам/.test(e.textContent))", timeout=30000)
        card = page.evaluate("() => [...document.querySelectorAll('.ms-card')]"
                             ".find(e => /Едет к нам/.test(e.textContent)).innerText")
        check("«Активный сток»: «Едет к нам» — вся графа «Заказано», не «в заказах на производстве»",
              "в заказах на производстве" not in card and "«Заказано»" in card
              and "ручные отметки" in card, card[:200])

        # 3. /assistant 1440×900: плашка в потоке, «Дальше →» открыт целиком, клик доходит.
        page.goto(f"{base}/assistant")
        _wait_chip(page)
        _close_hint(page)
        page.wait_for_timeout(300)
        chip = _chip_state(page)
        check("плашка свежести на месте: видна, ведёт в настройки, текст прежний",
              chip and chip["shown"] and chip["href"] == "/settings"
              and chip["text"].startswith("Данные:") and 0 <= chip["top"] < chip["vh"], str(chip)[:200])
        check("плашка свежести стоит в потоке страницы, а не поверх неё",
              chip and chip["pos"] != "fixed", str(chip and chip["pos"]))
        # Кнопку — к НИЖНЕМУ краю окна: именно там лежала фиксированная плашка.
        # Без прокрутки кнопка может оказаться ниже окна, и все пять точек
        # дали бы «out» — проверка прошла бы, ничего не проверив.
        page.evaluate("""() => { const b = [...document.querySelectorAll('#s1 .go-btn')]
          .find(x => /Дальше/.test(x.textContent));
          const y = b.getBoundingClientRect().bottom + scrollY - innerHeight + 10;
          window.scrollTo(0, Math.max(0, y)); }""")
        page.wait_for_timeout(300)
        hits = page.evaluate(HIT5, "#s1 .go-btn")
        covered = [h for h in hits if "fresh-chip" in h["res"]]
        check("/assistant 1440: кнопки шага 1 у нижнего края окна — ни одна точка не под плашкой",
              len(hits) == 2 and all("self" in h["res"] for h in hits) and not covered,
              str(hits)[:300])
        nxt = page.locator("#s1 .go-btn", has_text="Дальше")
        page.evaluate("() => { window.__nextClicks = 0; const b = [...document.querySelectorAll("
                      "'#s1 .go-btn')].find(x => /Дальше/.test(x.textContent));"
                      " b.addEventListener('click', () => window.__nextClicks++, true); }")
        box = nxt.bounding_box()
        page.mouse.click(box["x"] + 12, box["y"] + 4)   # верхний край: там раньше была плашка
        check("/assistant 1440: обычный клик в верхний край «Дальше →» доходит до кнопки",
              page.evaluate("() => window.__nextClicks") == 1)

        # 4. Предупреждение плашки сохранилось: отстающие данные — жёлтая, в потоке.
        page.route("**/api/freshness", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body='{"connected": true, "last_sale_date": "2020-01-01", "last_stock_date": '
                 '"2020-01-01", "sync_state": "done"}'))
        page.goto(f"{base}/turnover")
        _wait_chip(page)
        chip = _chip_state(page)
        page.unroute("**/api/freshness")
        check("плашка свежести сохранила предупреждение: отставание — жёлтая, в потоке",
              chip and "warn" in chip["cls"] and "Данные отстают" in chip["text"]
              and chip["pos"] != "fixed", str(chip)[:200])
        check("ошибок в консоли не было", not errors, str(errors[:2])[:200])
    finally:
        ctx.close()


# ── PILOT-UX-CLARITY-2-FOCUS: фильтр «Срочно» из карточки риска ────────────
#
# Своя организация (регистрация + демо): шаг двигает позиции во второе
# производство и не должен влиять на соседние шаги набора.

_ROWS_JS = """() => [...document.querySelectorAll('#tbody tr[data-base]')].map(r => {
  const b = r.querySelector('.stbadge');
  return {base: r.getAttribute('data-base'), st: b ? b.textContent.trim() : '',
          gap: /⚠/.test(r.textContent)};
})"""
_STATE_JS = """() => ({
  qty: document.getElementById('s-qty').textContent, pos: document.getElementById('s-pos').textContent,
  cost: document.getElementById('s-cost').textContent, risk: document.getElementById('k-risk').textContent,
  checked: document.querySelectorAll('#tbody .row-check:checked').length,
  search: document.getElementById('search').value,
  cat: (document.querySelector('#cat-bar .active') || {}).textContent || '',
  tab: (document.querySelector('#bigtabs .bigtab.active') || {getAttribute: () => null}).getAttribute('data-id'),
  segR: document.getElementById('seg-r').textContent, segAll: document.getElementById('seg-all').textContent,
  band: (document.querySelector('#band-seg button.on') || {}).getAttribute ?
        document.querySelector('#band-seg button.on').getAttribute('data-band') : null})"""
_FOCUS_JS = """() => { const a = document.activeElement; const hdr = document.querySelector('header.app-top');
  const r = a.getBoundingClientRect(); const hb = hdr ? hdr.getBoundingClientRect().bottom : 0;
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {band: a.getAttribute('data-band'), inSeg: !!a.closest('#band-seg'), top: r.top, bottom: r.bottom,
          headerBottom: hb, vh: innerHeight, hitSelf: !!hit && (hit === a || a.contains(hit)),
          ring: getComputedStyle(a).outlineStyle !== 'none'}; }"""


def _wait_rows(page) -> None:
    page.wait_for_function("() => document.querySelectorAll('#tbody tr[data-base]').length > 0",
                           timeout=45000)
    _close_hint(page)
    page.wait_for_timeout(300)


def _check_urgent_view(page, tag: str, before: dict, posts: list, how: str) -> None:
    after = page.evaluate(_STATE_JS)
    rows = page.evaluate(_ROWS_JS)
    check(f"{tag} [{how}]: включён именно существующий фильтр «Срочно» (aria-pressed)",
          after["band"] == "r" and page.locator('#band-seg button[data-band="r"]')
          .get_attribute("aria-pressed") == "true", str(after["band"]))
    check(f"{tag} [{how}]: все видимые строки — «Срочно», их столько же, сколько в счётчике",
          rows and all(r["st"] == "Срочно" for r in rows) and str(len(rows)) == after["segR"],
          f"rows={len(rows)} seg-r={after['segR']} st={sorted({r['st'] for r in rows})}")
    same = {k: before[k] for k in ("qty", "pos", "cost", "risk", "search", "cat", "tab")}
    now = {k: after[k] for k in same}
    check(f"{tag} [{how}]: производство, поиск, категория, итоги заказа и KPI не тронуты",
          same == now, f"{same} -> {now}")
    check(f"{tag} [{how}]: фильтр ничего не записал (0 POST)", not posts, str(posts[:3]))
    f = page.evaluate(_FOCUS_JS)
    check(f"{tag} [{how}]: фокус на «Срочно», ниже липкой шапки, виден и не перекрыт",
          f["inSeg"] and f["band"] == "r" and f["top"] >= f["headerBottom"] - 0.5
          and f["bottom"] <= f["vh"] and f["hitSelf"], str(f))


def _focus_urgent(browser, base: str) -> None:
    print("\n== PILOT-UX-CLARITY-2-FOCUS: «Срочно» из карточки риска — мышь, клавиатура, 390 ==")
    cl = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    cl.post("/register", data={"name": "Фокус", "email": "focus@test.io",
                               "password": "secret123", "org_name": "Бренд-Фокус"})
    check("фокус: демо-данные загружены", cl.post("/api/connect/demo").status_code == 200)
    items = cl.get("/api/replenish").json()["items"]
    pid = cl.post("/api/productions", json={"name": "Второй цех"}).json().get("id")
    # Во второе производство — и срочные (wos < 2, как у wosBand), и прочие,
    # чтобы там проверялась включённая кнопка, а не только выключенная.
    urgent = [it["base_name"] for it in items if it.get("wos") is not None and it["wos"] < 2]
    other = [it["base_name"] for it in items if not (it.get("wos") is not None and it["wos"] < 2)]
    moved = urgent[:2] + other[:4]
    for b in moved:
        cl.post("/api/productions/assign", json={"base_name": b, "production_id": pid})
    drafts0 = cl.get("/api/replenish-draft").json()
    orders0 = len(cl.get("/api/orders").json().get("orders", []))
    cookies = [{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"} for k, v in cl.cookies.items()]

    for tag, vp, touch in (("1440", {"width": 1440, "height": 900}, False),
                           ("390", {"width": MOBILE_WIDTH, "height": 844}, True)):
        ctx = browser.new_context(viewport=vp, has_touch=touch, is_mobile=touch)
        ctx.add_cookies(cookies)
        errors: list[str] = []
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        posts: list[str] = []
        page.on("request", lambda r: posts.append(r.url) if r.method != "GET" else None)
        try:
            page.goto(f"{base}/replenish")
            _wait_rows(page)
            legend = page.locator("#tbl-legend")
            check(f"{tag}: смысл ⚠ виден без наведения и не приравнен к «Срочно»",
                  legend.is_visible() and "не то же, что статус «Срочно»" in (legend.text_content() or ""))
            # Нетривиальная категория: есть и срочные, и прочие.
            chips = page.locator('#cat-bar [data-cat]:not([data-cat=""])')
            chosen = None
            for i in range(chips.count()):
                _click_past_hint(page, chips.nth(i), "категория")
                st = page.evaluate(_STATE_JS)
                if int(st["segR"]) > 0 and int(st["segAll"]) > int(st["segR"]):
                    chosen = chips.nth(i).get_attribute("data-cat")
                    break
            check(f"{tag}: найдена категория и со срочными, и с прочими позициями", chosen is not None)
            rg = page.locator("#risk-go")
            has_rg = rg.count() == 1
            label = (rg.text_content() or "") if has_rg else ""
            st = page.evaluate(_STATE_JS)
            check(f"{tag}: кнопка в карточке риска называет фильтр и его счётчик с учётом категории",
                  has_rg and rg.is_visible() and "Показать «Срочно» в таблице" in label
                  and st["segR"] in label and "с учётом поиска и категории" in label, label)
            if touch:
                g = page.evaluate("""() => [...document.querySelectorAll('#band-seg button, #rate-seg button,'
                  + ' #risk-go')].map(b => { const r = b.getBoundingClientRect();
                  return {t: b.textContent.trim().slice(0, 12), h: r.height, l: r.left, r: r.right}; })""")
                small = [x for x in g if x["h"] < 44]
                check("390: кнопки статуса, темпа и «Показать «Срочно»» — не ниже 44 px", not small, str(small))
                boxes = page.evaluate("""() => ['band-seg', 'rate-seg', 'search'].map(id => {
                  const r = document.getElementById(id).getBoundingClientRect();
                  return [r.left, r.top, r.right, r.bottom]; })""")
                inter = [(i, j) for i in range(3) for j in range(i + 1, 3)
                         if min(boxes[i][2], boxes[j][2]) > max(boxes[i][0], boxes[j][0]) + 0.5
                         and min(boxes[i][3], boxes[j][3]) > max(boxes[i][1], boxes[j][1]) + 0.5]
                check("390: переключатели и поиск не наезжают друг на друга", not inter, str(boxes))
                check("390: страница не прокручивается вбок",
                      page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1"),
                      str(page.evaluate("() => document.documentElement.scrollWidth")))
            if not has_rg:
                # Кнопки нет (базовое дерево): мышь и клавиатура проверять
                # нечем — это уже красная строка выше, дальше не идём.
                continue
            # 1) Указатель: настоящий клик / касание.
            before = page.evaluate(_STATE_JS)
            posts.clear()
            (rg.tap if touch else rg.click)()
            page.wait_for_timeout(400)
            _check_urgent_view(page, tag, before, posts, "касание" if touch else "мышь")
            # «Все» возвращает строки.
            (page.locator('#band-seg button[data-band=""]').tap if touch
             else page.locator('#band-seg button[data-band=""]').click)()
            page.wait_for_timeout(300)
            st = page.evaluate(_STATE_JS)
            check(f"{tag}: «Все» снимает только статус и возвращает все строки категории",
                  st["band"] == "" and len(page.evaluate(_ROWS_JS)) == int(st["segAll"])
                  and st["cat"] == before["cat"], str(st))
            # 2) Клавиатура: Tab до кнопки, затем Enter (1440) / пробел (390).
            page.evaluate("() => window.scrollTo(0, 0)")
            page.locator("#k-risk-sub").click() if not touch else None
            for _ in range(40):
                if page.evaluate("() => document.activeElement && document.activeElement.id") == "risk-go":
                    break
                page.keyboard.press("Tab")
            reached = page.evaluate("() => document.activeElement && document.activeElement.id") == "risk-go"
            check(f"{tag}: до кнопки доходит Tab, фокус виден",
                  reached and page.evaluate("() => getComputedStyle(document.activeElement)"
                                            ".outlineStyle !== 'none'"))
            before = page.evaluate(_STATE_JS)
            posts.clear()
            page.keyboard.press("Enter" if not touch else " ")
            page.wait_for_timeout(400)
            _check_urgent_view(page, tag, before, posts, "Enter" if not touch else "пробел")
            page.locator('#band-seg button[data-band=""]').click()
            page.wait_for_timeout(200)
            # 3) Поиск по одной НЕсрочной позиции с ⚠: честное «нет» вместо «всё хорошо».
            # Ищем по всем категориям (в выбранной такой строки может не быть).
            _click_past_hint(page, page.locator('#cat-bar [data-cat=""]'), "все категории")
            page.wait_for_timeout(300)
            gap_rows = [r for r in page.evaluate(_ROWS_JS) if r["st"] != "Срочно" and r["gap"]]
            if gap_rows:
                page.fill("#search", gap_rows[0]["base"])
                page.wait_for_timeout(500)
                label = rg.text_content() or ""
                check(f"{tag}: без срочных в фильтре кнопка выключена и говорит «нет», не «всё хорошо»",
                      rg.is_disabled() and "Срочных в таблице нет" in label
                      and "с учётом поиска и категории" in label, label)
                page.locator('#band-seg button[data-band="r"]').click()
                page.wait_for_timeout(300)
                empty = page.locator("#tbody .empty-note").text_content() or ""
                check(f"{tag}: пустой «Срочно» объясняет, что ⚠ есть в других статусах",
                      "Срочных позиций среди найденных" in empty and "дыра поставки есть у" in empty, empty)
                page.locator('#band-seg button[data-band=""]').click()
                page.fill("#search", "")
                page.wait_for_timeout(400)
            else:
                check(f"{tag}: в демо есть несрочная позиция с ⚠ для проверки пустого фильтра", False)
            # 4) Второе производство: вкладка остаётся той же, KPI — по вкладке.
            page.locator('#bigtabs .bigtab[data-id="%s"]' % pid).click()
            page.wait_for_timeout(500)
            st2 = page.evaluate(_STATE_JS)
            check(f"{tag}: во втором производстве есть срочные — кнопка включена",
                  not rg.is_disabled(), rg.text_content())
            before = page.evaluate(_STATE_JS)
            posts.clear()
            (rg.tap if touch else rg.click)()
            page.wait_for_timeout(400)
            _check_urgent_view(page, tag + " второе производство", before, posts,
                               "касание" if touch else "мышь")
            page.locator('#band-seg button[data-band=""]').click()
            check(f"{tag}: вкладка второго производства не сброшена фильтром",
                  page.evaluate(_STATE_JS)["tab"] == str(pid) == st2["tab"], str(st2["tab"]))
            check(f"{tag}: ошибок в консоли не было", not errors, str(errors[:2])[:200])
        finally:
            ctx.close()
    check("фокус: ручные правки ростовки и число заказов не изменились",
          cl.get("/api/replenish-draft").json() == drafts0
          and len(cl.get("/api/orders").json().get("orders", [])) == orders0)
    cl.close()


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
