# -*- coding: utf-8 -*-
"""PILOT-RECEIPTS-UI-1: экран приёмки заказа в НАСТОЯЩЕМ браузере.

Зачем отдельный набор. Ручки приёмки (`GET`/`POST /api/orders/{id}/receipts`)
существуют с D-25 и проверены на уровне API в `tests/test_execution.py`. Чего
не было — интерфейса: частичный приход, довоз и исправление минусом человек
мог записать только запросом к API, а принятый заказ с экрана пропадал вовсе
(`load()` вырезал `status === "received"`).

Здесь проверяется ровно то, что человек видит и нажимает, и прежде всего —
ЧЕСТНОСТЬ ЭКРАНА, потому что цена ошибки здесь не «криво нарисовано»:

  1) `null` и `0` — РАЗНЫЕ утверждения. `null` это «не знаем» (факта нет либо
     источники спорят), `0` это «не приехало ничего». Показать `null` нулём
     значило бы выдать незнание за недостачу;
  2) спор источников не прячется и не решается приоритетом на экране;
  3) строки `precision=whole_order` из прежних версий — ДОПУЩЕНИЕ, а не
     подтверждённое количество (`_confirmed_rows` в `app/api.py`). Старую
     прозу D-25 экран не оживляет;
  4) пустое поле — это «не говорю ничего», а явный ноль — факт. Первое не
     отправляется, второе отправляется;
  5) повтор неизменного намерения после НЕИЗВЕСТНОГО исхода сети приходит с
     тем же ключом и тем же телом, поэтому сервер узнаёт его как повтор и
     ничего не дописывает. Изменённое намерение — другой факт;
  6) история только пополняется: правки и удаления фактов в интерфейсе нет,
     ошибка гасится компенсирующей строкой, и обе остаются видны;
  7) обрезка длинной истории названа числом, а не сделана молча.

ЧЕГО ЗДЕСЬ НЕТ. Ни одной состязательной проверки (F-22/A09 в пакет не входят):
повторы последовательные. Ни одной записи в МойСклад и ни одной боевой записи —
сервер локальный, данные синтетические, каталог из демо-сида проекта.

Запуск из корня репозитория:  python tests/test_receipts_ui.py

Нужен Chromium под playwright: `pip install -r requirements-dev.lock` и
`python -m playwright install chromium`.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_receipts_ui.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8841"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
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
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("сервер не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=120.0)


def make_order(c, name: str, items: list[dict], status: str = "sent") -> int:
    """Заказ через настоящую ручку, а не подстановкой в базу.

    Фикстура намеренно ходит тем же путём, что и человек: иначе проверка
    опиралась бы на состояние, которого продукт создать не умеет.
    """
    r = c.post("/api/orders", json={"name": name, "items": items,
                                    "allow_duplicate": True})
    if r.status_code != 200:
        raise RuntimeError(f"заказ не создан: {r.status_code} {r.text[:200]}")
    oid = r.json()["id"]
    if status != "draft":
        s = c.post(f"/api/orders/{oid}/status", json={"status": "sent"})
        if s.status_code != 200:
            raise RuntimeError(f"статус sent не выставлен: {s.text[:200]}")
    if status == "received":
        s = c.post(f"/api/orders/{oid}/status", json={"status": "received"})
        if s.status_code != 200:
            raise RuntimeError(f"статус received не выставлен: {s.text[:200]}")
    return oid


def catalog_names(c, n: int) -> list[str]:
    """Имена из демо-каталога: заказ принимает только известные позиции."""
    rows = c.get("/api/replenish").json().get("items", [])
    names = []
    for row in rows:
        base = row.get("base_name")
        if base and base not in names:
            names.append(base)
        if len(names) >= n:
            break
    if len(names) < n:
        raise RuntimeError(f"в каталоге меньше {n} позиций: {len(names)}")
    return names


# ── Доступ к экрану ──────────────────────────────────────────────────────────


def open_replenish(page) -> None:
    """Открыть раздел и ДОЖДАТЬСЯ списка заказов, а не поспать фиксированно.

    Фиксированная пауза здесь уже подвела: локально 1,8 с хватало, а на
    холодном CI первые шаги не успевали — `load()` делает четыре запроса, и
    кнопки приёмки к моменту клика ещё не существовало. Набор падал не на
    продукте, а на собственном таймере; ждём условие.
    """
    page.goto(f"{BASE}/replenish")
    close_hint(page)
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#orders-tb tr,"
            " #orders-done-tb tr').length > 0", timeout=30000)
    except Exception:  # noqa: BLE001 — отсутствие строк проверит сам шаг
        pass
    close_hint(page)


def close_hint(page) -> None:
    if page.evaluate("() => { const o = document.getElementById('hint-overlay');"
                     " return !!o && o.classList.contains('open'); }"):
        page.click("#hint-close")
        page.wait_for_timeout(200)


def open_receipts(page, order_id: int) -> bool:
    """Открыть панель и дождаться, пока строки ДЕЙСТВИТЕЛЬНО отрисованы.

    Ждём не время, а два условия подряд: появилась кнопка (список заказов уже
    отрисован) и в панели больше нет «Загрузка…» (ответ ручки пришёл и разобран).
    """
    try:
        page.wait_for_selector('.ord-receipts[data-id="%s"]' % order_id,
                               timeout=30000)
    except Exception:  # noqa: BLE001 — отсутствие кнопки и есть ответ шага
        return False
    page.evaluate("""(id) => {
      const b = document.querySelector('.ord-receipts[data-id="' + id + '"]');
      if (b) b.click();
    }""", str(order_id))
    try:
        page.wait_for_function("""() => {
          const p = document.getElementById('rc-panel');
          if (!p || p.style.display === 'none') return false;
          const l = document.getElementById('rc-lines');
          return !!l && !l.querySelector('.loading');
        }""", timeout=30000)
    except Exception:  # noqa: BLE001
        return False
    return True


def panel_text(page) -> str:
    return page.evaluate(
        "() => { const p = document.getElementById('rc-panel');"
        " return (p && p.style.display !== 'none') ? p.innerText : ''; }") or ""


def line_cells(page) -> list:
    return page.evaluate("""() => {
      return [...document.querySelectorAll('#rc-lines [data-rc-line]')].map(function(tr){
        return {
          base: tr.getAttribute('data-rc-line'),
          ordered: (tr.querySelector('[data-rc-ordered]') || {}).textContent || '',
          received: (tr.querySelector('[data-rc-received]') || {}).textContent || '',
          diff: (tr.querySelector('[data-rc-diff]') || {}).textContent || ''
        };
      });
    }""")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("ПРОПУЩЕНО: playwright не установлен — поставьте "
              "requirements-dev.lock и выполните `python -m playwright "
              "install chromium`")
        return 77
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
        check("сценарий дошёл до конца без исключения", False,
              f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}")
        print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
        for name in FAIL:
            print(f"  FAIL {name}")
        return 1
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:  # noqa: C901 — сценарный набор: шагов много, ветвлений мало
    from playwright.sync_api import sync_playwright

    c = client()
    c.post("/register", data={"name": "Владелец", "email": "receipts-ui@test.io",
                              "password": "secret123", "org_name": "Бренд-Приёмка"})
    check("демо-данные загружены", c.post("/api/connect/demo").status_code == 200)
    names = catalog_names(c, 3)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 — важно имя причины, а не тип
            check("Chromium запускается", False,
                  str(exc).strip().splitlines()[0][:200])
            print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
            return 1
        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                         for k, v in c.cookies.items()])
        errors: list[str] = []
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))

        steps = (
            ("черновик не принимает", lambda: step_draft(page, c, names)),
            ("null и ноль — разные", lambda: step_null_vs_zero(page, c, names)),
            ("спор источников", lambda: step_conflict(page, c, names)),
            ("допущение не выдаётся за приёмку",
             lambda: step_assumption(page, c, names)),
            ("принятый заказ достижим", lambda: step_received_reachable(page, c, names)),
            ("запись прихода: пусто, ноль, минус",
             lambda: step_record(page, c, names)),
            ("повтор после неизвестного исхода",
             lambda: step_retry(page, c, names)),
            ("обрезка истории названа", lambda: step_truncation(page, c, names)),
            # Корректив A по независимому ревью PR #61: три подтверждённых P1.
            ("неполная позиция вне заказа не теряется",
             lambda: step_extra_incomplete(page, c, names)),
            ("дробное количество не округляется",
             lambda: step_fractions(page, c, names)),
            ("ответ прошлой панели не портит новую",
             lambda: step_stale_callbacks(page, c, names)),
            # Корректив B: регресс, внесённый коррективом A.
            ("новое открытие получает рабочую кнопку",
             lambda: step_save_button_fresh(page, c, names)),
            # Контрольная точка 10: жизненный цикл настоящими нажатиями.
            ("жизненный цикл заказа кликами и повтор после потери ответа",
             lambda: step_lifecycle_clicks(page, c, names)),
            # PILOT-UX-ORDERS-MOBILE-1.
            ("удаление называет приёмку", lambda: step_delete_warning(page, c, names)),
            ("источники словами", lambda: step_source_labels(page, c, names)),
        )
        for label, run_step in steps:
            try:
                run_step()
            except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
                check(f"{label}: шаг дошёл до конца без исключения", False,
                      f"{type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:200]}")

        check("ни одной ошибки в консоли за весь проход", not errors,
              str(errors[:2])[:300])
        ctx.close()
        step_mobile(browser, c, names)
        try:
            step_mobile_lifecycle(browser, c, names)
        except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
            check("390 px: жизненный цикл дошёл до конца без исключения", False,
                  f"{type(exc).__name__}: {str(exc).strip().splitlines()[0][:200]}")
        for label, run_step in (
                ("390 px: действия заказа и поле прихода",
                 lambda: step_mobile_orders(browser, c, names)),
                ("создание заказа: отзыв и прокрутка",
                 lambda: step_create_feedback(browser, c, names))):
            try:
                run_step()
            except Exception as exc:  # noqa: BLE001 — важен отчёт, а не тип
                check(f"{label}: шаг дошёл до конца без исключения", False,
                      f"{type(exc).__name__}: "
                      f"{str(exc).strip().splitlines()[0][:200]}")
        browser.close()

    c.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


def step_draft(page, c, names) -> None:
    """Черновик принимать нечего — и кнопки у него быть не должно."""
    print("\n== Черновик: приёмки нет, потому что сервер её и не примет ==")
    oid = make_order(c, "Черновик-приёмка", [{"base_name": names[0], "qty": 10}],
                     status="draft")
    # Сервер отвечает на такой POST 422 — проверка опирается на факт, а не на
    # предположение о том, как ручка себя ведёт.
    r = c.post(f"/api/orders/{oid}/receipts",
               json={"lines": [{"base_name": names[0], "qty": 1}]})
    check("сервер отказывает черновику в приёмке", r.status_code == 422,
          f"{r.status_code} {r.text[:120]}")
    open_replenish(page)
    state = page.evaluate("""(id) => ({
      row: !!document.querySelector('#orders-tb tr[data-order="' + id + '"]'),
      btn: !!document.querySelector('.ord-receipts[data-id="' + id + '"]')
    })""", str(oid))
    # Строка заказа проверяется ОТДЕЛЬНО и первой: без неё «кнопки нет»
    # зеленело бы и на неотрисованной странице — то есть проверка доказывала
    # бы отсутствие разметки вместо отсутствия действия.
    check("черновик виден в списке заказов", state["row"] is True, str(state))
    check("у черновика кнопки приёмки нет", state["btn"] is False, str(state))


def step_null_vs_zero(page, c, names) -> None:
    """Главное различие всего экрана: «не знаем» против «не приехало»."""
    print("\n== null — это «не знаем», 0 — это «не приехало» ==")
    oid = make_order(c, "Частичный приход",
                     [{"base_name": names[0], "qty": 10},
                      {"base_name": names[1], "qty": 5}])
    # По первой позиции человек сказал явный НОЛЬ, по второй не сказал ничего.
    c.post(f"/api/orders/{oid}/receipts",
           json={"lines": [{"base_name": names[0], "qty": 0}],
                 "idempotency_key": "rc-zero"})
    api_lines = {ln["base_name"]: ln
                 for ln in c.get(f"/api/orders/{oid}/receipts").json()["lines"]}
    check("контракт: явный ноль пришёл нулём, а несказанное — null",
          api_lines[names[0]]["received_qty"] == 0
          and api_lines[names[1]]["received_qty"] is None,
          f"{api_lines[names[0]]['received_qty']} / {api_lines[names[1]]['received_qty']}")

    open_replenish(page)
    check("панель приёмки открывается", open_receipts(page, oid) is True)
    cells = {row["base"]: row for row in line_cells(page)}
    check("подтверждённый ноль показан нулём",
          names[0] in cells and cells[names[0]]["received"].strip() == "0",
          str(cells.get(names[0])))
    check("а несказанное показано словом «неизвестно», а не нулём",
          names[1] in cells
          and "еизвестно" in cells[names[1]]["received"]
          and "0" not in cells[names[1]]["received"],
          str(cells.get(names[1])))
    check("итог по заказу тоже «неизвестно», пока есть незакрытая позиция",
          "еизвестно" in panel_text(page), panel_text(page)[:200])


def step_conflict(page, c, names) -> None:
    """Спор источников не решается приоритетом на экране."""
    print("\n== Источники спорят — экран говорит «спор», а не победителя ==")
    oid = make_order(c, "Спор источников", [{"base_name": names[0], "qty": 10}])
    _seed_receipt(c, oid, names[0], 8.0, "ms_supply")
    _seed_receipt(c, oid, names[0], 3.0, "manual")
    body = c.get(f"/api/orders/{oid}/receipts").json()
    check("контракт: спор снял число и признал итог неизвестным",
          body["lines"][0]["received_qty"] is None
          and body["received_total"] is None and bool(body["source_conflicts"]),
          str(body["lines"][0]))

    open_replenish(page)
    open_receipts(page, oid)
    text = panel_text(page)
    check("экран называет расхождение источников вслух",
          "асхожд" in text or "спор" in text.lower(), text[:240])
    check("и не показывает ни 8, ни 3 как принятое количество",
          (line_cells(page)[0]["received"].strip() in ("неизвестно", "— неизвестно")
           or "еизвестно" in line_cells(page)[0]["received"]),
          str(line_cells(page)[0]))
    check("но сами показания источников видны человеку для разбора",
          "8" in text and "3" in text, text[:240])


def step_assumption(page, c, names) -> None:
    """`whole_order` — допущение прежних версий, а не подтверждение."""
    print("\n== Старое допущение whole_order не выдаётся за приёмку ==")
    oid = make_order(c, "Старое допущение", [{"base_name": names[0], "qty": 10}])
    _seed_receipt(c, oid, names[0], 10.0, "manual", precision="whole_order")
    body = c.get(f"/api/orders/{oid}/receipts").json()
    check("контракт: допущение количеством не распоряжается",
          body["lines"][0]["received_qty"] is None
          and "whole_order" in body["precisions"], str(body["lines"][0]))
    open_replenish(page)
    open_receipts(page, oid)
    text = panel_text(page)
    check("экран называет такую строку допущением",
          "опущен" in text, text[:240])
    check("и не показывает её как подтверждённое количество",
          "еизвестно" in line_cells(page)[0]["received"],
          str(line_cells(page)[0]))


def step_received_reachable(page, c, names) -> None:
    """Принятый заказ достижим и НЕ выдаётся за едущий."""
    print("\n== Принятый заказ виден, но не среди едущих ==")
    oid = make_order(c, "Уже принят", [{"base_name": names[0], "qty": 4}],
                     status="received")
    open_replenish(page)
    state = page.evaluate("""(id) => {
      const done = document.querySelector('#orders-done-tb [data-order="' + id + '"]');
      const active = document.querySelector('#orders-tb [data-order="' + id + '"]');
      const title = document.getElementById('orders-done-title');
      return {
        inDone: !!done,
        inActive: !!active,
        doneText: done ? done.innerText : '',
        titleText: title && title.style.display !== 'none' ? title.innerText : ''
      };
    }""", str(oid))
    check("принятый заказ достижим с экрана", state["inDone"] is True, str(state))
    check("и НЕ стоит среди заказов в производстве",
          state["inActive"] is False, str(state))
    check("подписан принятым, а не черновиком",
          "ринят" in state["doneText"] and "ерновик" not in state["doneText"],
          state["doneText"][:160])
    # Заголовок раздела рисуется с `text-transform: uppercase`, поэтому
    # `innerText` возвращает его ПРОПИСНЫМИ — сравнение регистрозависимой
    # подстрокой падало бы не на продукте, а на оформлении.
    check("и сказано, что эти заказы в «Едет» не считаются",
          "едет" in state["titleText"].lower()
          and "не считаются" in state["titleText"].lower(),
          state["titleText"][:160])
    check("приёмка у принятого заказа доступна",
          open_receipts(page, oid) is True)


def step_record(page, c, names) -> None:
    """Пустое поле молчит, явный ноль записывается, минус исправляет."""
    print("\n== Запись прихода: пусто ≠ ноль, минус — исправление ==")
    oid = make_order(c, "Запись прихода",
                     [{"base_name": names[0], "qty": 10},
                      {"base_name": names[1], "qty": 6}])
    open_replenish(page)
    open_receipts(page, oid)
    # По первой позиции 7, вторую НЕ трогаем вовсе.
    _fill_line(page, names[0], "7")
    _submit(page)
    page.wait_for_timeout(1500)
    body = c.get(f"/api/orders/{oid}/receipts").json()
    lines = {ln["base_name"]: ln for ln in body["lines"]}
    check("записано ровно то, что человек назвал",
          lines[names[0]]["received_qty"] == 7, str(lines[names[0]]))
    check("а нетронутая позиция осталась НЕИЗВЕСТНОЙ, а не нулём",
          lines[names[1]]["received_qty"] is None, str(lines[names[1]]))
    check("в истории ровно одна строка — пустое поле не отправлено",
          body["receipts_total"] == 1, str(body["receipts_total"]))

    # Довоз по второй позиции: явный ноль — это факт, а не молчание.
    open_receipts(page, oid)
    _fill_line(page, names[1], "0")
    _submit(page)
    page.wait_for_timeout(1500)
    body = c.get(f"/api/orders/{oid}/receipts").json()
    lines = {ln["base_name"]: ln for ln in body["lines"]}
    check("явный ноль записан как подтверждённый ноль",
          lines[names[1]]["received_qty"] == 0, str(lines[names[1]]))

    # Исправление минусом: история только пополняется.
    open_receipts(page, oid)
    _fill_line(page, names[0], "-2")
    _submit(page)
    page.wait_for_timeout(1500)
    body = c.get(f"/api/orders/{oid}/receipts").json()
    lines = {ln["base_name"]: ln for ln in body["lines"]}
    check("компенсирующая строка исправила итог до 5",
          lines[names[0]]["received_qty"] == 5, str(lines[names[0]]))
    check("и обе строки остались в истории — ничего не переписано",
          body["receipts_total"] == 3, str(body["receipts_total"]))
    open_receipts(page, oid)
    hist = page.evaluate(
        "() => { const h = document.getElementById('rc-history');"
        " return h ? h.innerText : ''; }") or ""
    check("история видна человеку и показывает исправление",
          "-2" in hist or "−2" in hist, hist[:240])
    check("в интерфейсе нет кнопок правки и удаления фактов",
        page.evaluate("""() => !document.querySelector(
            '#rc-history button, #rc-history [data-rc-edit], #rc-history [data-rc-del]')""")
        is True)


def step_retry(page, c, names) -> None:
    """Повтор НЕИЗМЕНЁННОГО намерения после обрыва не двоит факт."""
    print("\n== Неизвестный исход сети: повтор не дописывает вторую строку ==")
    oid = make_order(c, "Повтор после обрыва", [{"base_name": names[0], "qty": 9}])
    open_replenish(page)
    open_receipts(page, oid)
    _fill_line(page, names[0], "4")

    seen: list = []
    route = "**/api/orders/*/receipts"

    def record_and_fail(r):
        try:
            seen.append(json.loads(r.request.post_data or "{}"))
        except Exception:  # noqa: BLE001 — тело нам важно, а разбор не критичен
            seen.append({})
        r.abort()

    page.route(route, record_and_fail)
    _submit(page)
    page.wait_for_timeout(1500)
    page.unroute(route)
    check("после обрыва экран НЕ объявил сохранение удавшимся",
          _err_text(page) != "", _err_text(page)[:160])
    check("и панель осталась открытой с набранным",
          _line_value(page, names[0]) == "4", _line_value(page, names[0]))

    first_key = seen[0].get("idempotency_key") if seen else None
    check("ключ повтора у первой попытки был", bool(first_key), str(first_key))

    # ПОВТОР ТОГО ЖЕ НАМЕРЕНИЯ — та же открытая панель, ничего не правили.
    # Ключ и тело обязаны совпасть с первой попыткой, иначе сервер запишет
    # факт второй раз: его замок — это пара «ключ + содержимое».
    seen.clear()
    page.route(route, lambda r: (seen.append(
        json.loads(r.request.post_data or "{}")), r.continue_())[-1])
    _submit(page)
    page.wait_for_timeout(1800)
    page.unroute(route)
    check("повтор ушёл с ТЕМ ЖЕ ключом, что и оборвавшаяся попытка",
          bool(seen) and seen[0].get("idempotency_key") == first_key,
          f"{first_key} → {seen[0].get('idempotency_key') if seen else None}")
    body = c.get(f"/api/orders/{oid}/receipts").json()
    check("и факт записан ровно один раз",
          body["receipts_total"] == 1 and body["lines"][0]["received_qty"] == 4,
          f"total={body['receipts_total']} qty={body['lines'][0]['received_qty']}")

    # ИЗМЕНЁННОЕ намерение — другой факт, и он обязан записаться.
    open_receipts(page, oid)
    _fill_line(page, names[0], "3")
    _submit(page)
    page.wait_for_timeout(1800)
    body = c.get(f"/api/orders/{oid}/receipts").json()
    check("изменённое намерение записано как новый факт (4 + 3 = 7)",
          body["receipts_total"] == 2 and body["lines"][0]["received_qty"] == 7,
          f"total={body['receipts_total']} qty={body['lines'][0]['received_qty']}")

    # А ВОТ ЭТО — НЕ ПОВТОР, И ЗАПРЕЩАТЬ ЕГО НЕЛЬЗЯ. Человек заново открыл
    # панель и записал ещё четыре штуки: это ДОВОЗ, второй приход того же
    # количества, а не второе нажатие той же кнопки. Ключ намерения потому и
    # живёт от открытия панели до успеха: внутри одного намерения повтор
    # безопасен, а новое намерение обязано пройти. Проверка стоит здесь
    # намеренно — без неё «защита от дубля» легко превратилась бы в потерю
    # настоящего довоза, и набор бы этого не заметил.
    open_receipts(page, oid)
    _fill_line(page, names[0], "4")
    _submit(page)
    page.wait_for_timeout(1800)
    body = c.get(f"/api/orders/{oid}/receipts").json()
    check("новое намерение с тем же числом — это довоз, и он записан (7 + 4 = 11)",
          body["receipts_total"] == 3 and body["lines"][0]["received_qty"] == 11,
          f"total={body['receipts_total']} qty={body['lines'][0]['received_qty']}")


def step_truncation(page, c, names) -> None:
    """Обрезка длинной истории объявляется числом, а не молчанием."""
    print("\n== Длинная история: сколько строк скрыто, сказано вслух ==")
    oid = make_order(c, "Длинная история", [{"base_name": names[0], "qty": 700}])
    _seed_many(c, oid, names[0], 520)
    body = c.get(f"/api/orders/{oid}/receipts").json()
    check("контракт: сервер сообщил, что часть истории скрыта",
          body["receipts_hidden"] > 0 and body["receipts_total"] > len(body["receipts"]),
          f"hidden={body['receipts_hidden']} total={body['receipts_total']}")
    open_replenish(page)
    open_receipts(page, oid)
    text = panel_text(page)
    check("экран называет число скрытых строк, а не выдаёт часть за всё",
          str(body["receipts_hidden"]) in text and "скрыт" in text.lower(),
          text[:240])


def step_mobile(browser, c, names) -> None:
    """390×844 — эмуляция вьюпорта, не телефон в руках."""
    print("\n== 390×844: панель приёмки помещается, прокрутки вбок нет ==")
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        oid = make_order(c, "Телефон-приёмка",
                         [{"base_name": names[0], "qty": 12}])
        open_replenish(page)
        check("на 390 px панель приёмки открывается",
              open_receipts(page, oid) is True)
        check("на 390 px раздел не требует горизонтальной прокрутки",
              page.evaluate("() => document.documentElement.scrollWidth"
                            " <= window.innerWidth + 1") is True,
              str(page.evaluate("() => document.documentElement.scrollWidth")))
        # Клавиатура: поле количества достижимо фокусом и принимает ввод.
        focused = page.evaluate("""() => {
          const i = document.querySelector('#rc-lines input[data-rc-qty]');
          if (!i) return null;
          i.focus();
          return document.activeElement === i;
        }""")
        check("поле количества получает фокус с клавиатуры", focused is True,
              str(focused))
        check("и ошибок в консоли телефонного окна не было", not errors,
              str(errors[:2])[:200])
    finally:
        ctx.close()


# ── Корректив A по ревью PR #61: три подтверждённых P1 ──────────────────────


def _extra(page, name: str, qty: str) -> None:
    page.evaluate("""(a) => {
      const n = document.getElementById('rc-extra-name');
      const q = document.getElementById('rc-extra-qty');
      n.value = a.name; n.dispatchEvent(new Event('input', {bubbles: true}));
      q.value = a.qty;  q.dispatchEvent(new Event('input', {bubbles: true}));
    }""", {"name": name, "qty": qty})


def _extra_values(page) -> dict:
    return page.evaluate("""() => ({
      name: (document.getElementById('rc-extra-name') || {}).value,
      qty: (document.getElementById('rc-extra-qty') || {}).value
    })""")


def step_extra_incomplete(page, c, names) -> None:
    """Половина утверждения — не утверждение, и молча её терять нельзя.

    Случай ревью: по заказанной позиции названо верное число, а у позиции вне
    заказа заполнено ТОЛЬКО количество. Прежняя редакция такую пару молча
    выбрасывала, POST уходил с одной строкой и УДАВАЛСЯ, а успех очищал все
    поля — пять названных человеком штук исчезали без единого слова.
    """
    print("\n== Неполная позиция вне заказа: отказ до отправки, ввод цел ==")
    oid = make_order(c, "Неполная позиция", [{"base_name": names[0], "qty": 10}])
    open_replenish(page)
    open_receipts(page, oid)

    posts = []
    route = "**/api/orders/*/receipts"
    page.route(route, lambda r: (posts.append(r.request.method), r.continue_())[-1]
               if r.request.method == "POST" else r.continue_())
    try:
        _fill_line(page, names[0], "6")
        _extra(page, "", "5")
        _submit(page)
        check("ни одного POST не ушло — отправка отклонена целиком",
              len(posts) == 0, str(posts))
        check("человеку сказано, чего не хватает",
              "назван" in _err_text(page).lower()
              or "название" in _err_text(page).lower(), _err_text(page)[:200])
        check("введённое по заказанной позиции на месте",
              _line_value(page, names[0]) == "6", _line_value(page, names[0]))
        vals = _extra_values(page)
        check("и количество вне заказа не стёрто", vals["qty"] == "5", str(vals))
        body = c.get(f"/api/orders/{oid}/receipts").json()
        check("на сервере не записано ничего", body["receipts_total"] == 0,
              str(body["receipts_total"]))

        # Обратная неполная пара: имя есть, количества нет.
        _extra(page, "Коробка без счёта", "")
        _submit(page)
        check("обратная неполная пара тоже отклонена до отправки",
              len(posts) == 0, str(posts))
        check("и ввод по-прежнему цел",
              _line_value(page, names[0]) == "6"
              and _extra_values(page)["name"] == "Коробка без счёта",
              str(_extra_values(page)))

        # Полная пара проходит — отказ не должен запрещать законный случай.
        _extra(page, "Коробка без счёта", "5")
        _submit(page)
        body = c.get(f"/api/orders/{oid}/receipts").json()
        got = {ln["base_name"]: ln["received_qty"] for ln in body["lines"]}
        check("полная пара записывается: и заказанное, и позиция вне заказа",
              got.get(names[0]) == 6 and got.get("Коробка без счёта") == 5,
              str(got))
    finally:
        page.unroute(route)


def step_fractions(page, c, names) -> None:
    """API принимает и отдаёт дроби — экран обязан их показывать.

    `fmt` в шаблоне округляет (`Math.round`), и 0,4 превращалось в 0, а -0,4 в
    -0. Это подмена факта клиентом: поле ввода объявлено `step="any"`, сервер
    хранит три знака, а человек видел ноль там, где приехало 0,4.
    """
    print("\n== Дробное количество показывается, а не округляется ==")
    oid = make_order(c, "Дробные приходы",
                     [{"base_name": names[0], "qty": 10},
                      {"base_name": names[1], "qty": 4}])
    # Положительная дробь, отрицательная дробь и ПОДТВЕРЖДЁННЫЙ ноль рядом.
    c.post(f"/api/orders/{oid}/receipts",
           json={"lines": [{"base_name": names[0], "qty": 1.5}],
                 "idempotency_key": "fr-1"})
    c.post(f"/api/orders/{oid}/receipts",
           json={"lines": [{"base_name": names[0], "qty": -0.4}],
                 "idempotency_key": "fr-2"})
    api_lines = {ln["base_name"]: ln
                 for ln in c.get(f"/api/orders/{oid}/receipts").json()["lines"]}
    check("контракт: сервер хранит дробь, а не целое",
          abs(api_lines[names[0]]["received_qty"] - 1.1) < 1e-9,
          str(api_lines[names[0]]["received_qty"]))

    open_replenish(page)
    open_receipts(page, oid)
    cells = {row["base"]: row for row in line_cells(page)}
    got = cells[names[0]]["received"].strip()
    check("в сверке видна дробь 1,1, а не округлённая единица",
          got.replace(" ", "").replace(" ", "") in ("1,1",), got)
    check("а позиция без факта по-прежнему «неизвестно», а не ноль",
          "еизвестно" in cells[names[1]]["received"], str(cells[names[1]]))

    hist = page.evaluate(
        "() => { const h=document.getElementById('rc-history');"
        " return h ? h.innerText : ''; }") or ""
    check("в истории видна положительная дробь 1,5", "1,5" in hist, hist[:200])
    check("и отрицательная дробь -0,4, а не -0",
          ("-0,4" in hist or "−0,4" in hist), hist[:200])

    # Подтверждённый ноль обязан остаться нулём, а не стать «неизвестно».
    c.post(f"/api/orders/{oid}/receipts",
           json={"lines": [{"base_name": names[1], "qty": 0}],
                 "idempotency_key": "fr-0"})
    open_receipts(page, oid)
    cells = {row["base"]: row for row in line_cells(page)}
    check("подтверждённый ноль показан нулём и после правки формата",
          cells[names[1]]["received"].strip() == "0", str(cells[names[1]]))


def step_stale_callbacks(page, c, names) -> None:
    """Ответ ПРОШЛОГО открытия панели не имеет права трогать текущее.

    Проверка полностью клиентская и детерминированная: запрос задерживается
    маршрутом браузера и отпускается тогда, когда решит набор. Никаких
    параллельных запросов к серверу и никаких состязательных проб.
    """
    print("\n== Ответ прошлой панели не портит текущую ==")
    a_id = make_order(c, "Заказ А", [{"base_name": names[0], "qty": 10}])
    b_id = make_order(c, "Заказ Б", [{"base_name": names[1], "qty": 7}])
    open_replenish(page)

    held = []
    route = "**/api/orders/*/receipts"

    def hold_first(r):
        # Задерживаем ТОЛЬКО первый GET (он от заказа А) — остальное пропускаем.
        if r.request.method == "GET" and not held:
            held.append(r)
            return
        r.continue_()

    page.route(route, hold_first)
    try:
        page.evaluate("""(id) => {
          const b = document.querySelector('.ord-receipts[data-id="' + id + '"]');
          if (b) b.click();
        }""", str(a_id))
        page.wait_for_timeout(600)
        check("запрос заказа А задержан", len(held) == 1, str(len(held)))

        # Человек закрывает панель А и открывает Б, которая грузится нормально.
        # Маршрут НЕ снимаем: `unroute` сам доигрывает задержанный запрос, и
        # отпустить его по своей воле уже не получится («Route is already
        # handled»). Обработчик и так пропускает всё, кроме первого GET.
        page.evaluate("() => document.getElementById('rc-close').click()")
        page.wait_for_timeout(200)
        check("панель Б открылась", open_receipts(page, b_id) is True)
        _fill_line(page, names[1], "3")

        # ...и только теперь падает задержанный запрос А.
        held[0].abort()
        page.wait_for_timeout(1200)
        cells = {row["base"]: row for row in line_cells(page)}
        check("строки панели Б на месте, а не стёрты отказом А",
              names[1] in cells, str(list(cells)))
        check("набранное в Б не потеряно",
              _line_value(page, names[1]) == "3", _line_value(page, names[1]))
        check("и чужая ошибка на экране Б не показана",
              _err_text(page) == "", _err_text(page)[:200])
    finally:
        try:
            page.unroute(route)
        except Exception:  # noqa: BLE001 — маршрут мог быть уже снят
            pass

    # ТОТ ЖЕ ЗАКАЗ, закрытие и повторное открытие: одной сверки номера заказа
    # тут мало — он совпадает. Отличать обязано САМО ОТКРЫТИЕ.
    print("\n== Тот же заказ: закрыли и открыли заново ==")
    held2 = []

    def hold_first_again(r):
        if r.request.method == "GET" and not held2:
            held2.append(r)
            return
        r.continue_()

    open_replenish(page)
    page.route(route, hold_first_again)
    try:
        page.evaluate("""(id) => {
          const b = document.querySelector('.ord-receipts[data-id="' + id + '"]');
          if (b) b.click();
        }""", str(a_id))
        page.wait_for_timeout(600)
        page.evaluate("() => document.getElementById('rc-close').click()")
        page.wait_for_timeout(200)
        check("та же панель открыта заново", open_receipts(page, a_id) is True)
        _fill_line(page, names[0], "2")
        held2[0].abort()
        page.wait_for_timeout(1200)
        check("строки текущего открытия целы",
              bool(line_cells(page)), str(line_cells(page))[:160])
        check("и набранное во втором открытии не потеряно",
              _line_value(page, names[0]) == "2", _line_value(page, names[0]))
        check("чужой ошибки на экране нет", _err_text(page) == "",
              _err_text(page)[:200])
    finally:
        try:
            page.unroute(route)
        except Exception:  # noqa: BLE001
            pass


def _save_disabled(page):
    return page.evaluate(
        "() => { const b = document.getElementById('rc-save');"
        " return b ? b.disabled : null; }")


def _click_save(page) -> None:
    """Нажать и НЕ ждать завершения: запрос в этом шаге держится нарочно."""
    page.evaluate("() => { const b = document.getElementById('rc-save');"
                  " if (b) b.click(); }")
    page.wait_for_timeout(300)


def step_save_button_fresh(page, c, names) -> None:
    """Регресс корректива A: кнопка «Записать приход» одна на всю страницу.

    Отправка выключает её, а завершающий шаг включает обратно — но ТОЛЬКО
    своему открытию (охрана по поколению, и она верна). Беда в том, что новое
    открытие панели состояние кнопки не задавало вовсе: если предыдущая
    отправка ещё не завершилась, человек открывал следующую панель с уже
    выключённой кнопкой, а опоздавший ответ её включить отказывался — по делу.
    Главное действие экрана оставалось мёртвым до перезагрузки страницы.

    Проверка целиком клиентская: POST задерживается маршрутом браузера и
    отпускается набором. Параллельных запросов к серверу нет.
    """
    print("\n== Новое открытие панели получает рабочую кнопку ==")
    a_id = make_order(c, "Кнопка А", [{"base_name": names[0], "qty": 10}])
    b_id = make_order(c, "Кнопка Б", [{"base_name": names[1], "qty": 8}])
    open_replenish(page)

    held = []
    route = "**/api/orders/*/receipts"

    def hold_posts(r):
        if r.request.method == "POST":
            held.append(r)
            return
        r.continue_()

    page.route(route, hold_posts)
    try:
        # 1. Отправка по заказу А зависает.
        open_receipts(page, a_id)
        _fill_line(page, names[0], "4")
        _click_save(page)
        check("во время отправки кнопка выключена", _save_disabled(page) is True,
              str(_save_disabled(page)))
        check("запрос А задержан", len(held) == 1, str(len(held)))

        # 2. Человек закрывает А и открывает ДРУГОЙ заказ.
        page.evaluate("() => document.getElementById('rc-close').click()")
        page.wait_for_timeout(200)
        check("панель Б открылась", open_receipts(page, b_id) is True)
        check("у нового открытия кнопка РАБОЧАЯ, а не унаследованно выключенная",
              _save_disabled(page) is False, str(_save_disabled(page)))

        # 3. Опоздавший ответ А ничего не чинит и не ломает.
        held[0].abort()
        page.wait_for_timeout(1000)
        check("после завершения старой отправки кнопка Б по-прежнему рабочая",
              _save_disabled(page) is False, str(_save_disabled(page)))
        check("и чужой ошибки на экране Б нет", _err_text(page) == "",
              _err_text(page)[:160])

        # 4. ТОТ ЖЕ ЗАКАЗ: закрыли во время отправки и открыли заново.
        held.clear()
        open_receipts(page, a_id)
        _fill_line(page, names[0], "2")
        _click_save(page)
        check("отправка по А снова задержана и кнопка выключена",
              len(held) == 1 and _save_disabled(page) is True,
              f"held={len(held)} disabled={_save_disabled(page)}")
        page.evaluate("() => document.getElementById('rc-close').click()")
        page.wait_for_timeout(200)
        check("тот же заказ открыт заново", open_receipts(page, a_id) is True)
        check("и у него кнопка тоже рабочая",
              _save_disabled(page) is False, str(_save_disabled(page)))
        held[0].abort()
        page.wait_for_timeout(1000)
        check("опоздавший ответ того же заказа кнопку не выключил",
              _save_disabled(page) is False, str(_save_disabled(page)))

        # 5. И ОБРАТНОЕ: старое завершение НЕ включает кнопку панели, которая
        #    сохраняет ПРЯМО СЕЙЧАС. Иначе человек нажал бы второй раз посреди
        #    собственной отправки.
        held.clear()
        open_receipts(page, a_id)
        _fill_line(page, names[0], "1")
        _click_save(page)                      # отправка №1 висит
        page.evaluate("() => document.getElementById('rc-close').click()")
        page.wait_for_timeout(200)
        open_receipts(page, b_id)
        _fill_line(page, names[1], "3")
        _click_save(page)                      # отправка №2 висит, кнопка off
        check("обе отправки задержаны, кнопка выключена своей же отправкой",
              len(held) == 2 and _save_disabled(page) is True,
              f"held={len(held)} disabled={_save_disabled(page)}")
        held[0].abort()                        # завершается СТАРАЯ
        page.wait_for_timeout(1000)
        check("старое завершение не включило кнопку идущей отправки",
              _save_disabled(page) is True, str(_save_disabled(page)))
        held[1].abort()                        # завершается своя
        page.wait_for_timeout(1000)
        check("а своё завершение кнопку вернуло",
              _save_disabled(page) is False, str(_save_disabled(page)))
    finally:
        for r in held:
            try:
                r.abort()
            except Exception:  # noqa: BLE001 — маршрут мог быть уже отпущен
                pass
        try:
            page.unroute(route)
        except Exception:  # noqa: BLE001
            pass


# ── Контрольная точка 10: жизненный цикл заказа НАСТОЯЩИМИ нажатиями ────────
# Здесь нет ни одного `el.click()` через evaluate и ни одного `force`: кнопку,
# до которой не дотянется палец на телефоне, Playwright честно не нажмёт, и
# шаг упадёт — это и есть находка, а не повод обойти элемент.


def _text_is(locator, text: str) -> bool:
    """Дождаться точного текста (textContent, не зависит от CSS-регистра)."""
    from playwright.sync_api import expect
    try:
        expect(locator).to_have_text(text, timeout=30000)
        return True
    except AssertionError:
        return False


def _order_api(c, oid: int) -> tuple:
    rc = c.get(f"/api/orders/{oid}/receipts").json()
    return c.get(f"/api/orders/{oid}").json().get("status"), rc


def _lifecycle(page, c, oid: int, tag: str) -> None:
    """Черновик → «▶ В производство» → «✓ Принят на склад», как делает человек."""
    row = page.locator('#orders-tb tr[data-order="%s"]' % oid)
    check(f"{tag}: черновик подписан «черновик»",
          _text_is(row.locator(".stbadge"), "черновик"),
          str(row.locator(".stbadge").all_text_contents()))
    with page.expect_response(lambda r: r.request.method == "POST"
                              and r.url.endswith(f"/api/orders/{oid}/status")) as sent:
        row.get_by_role("button", name="▶ В производство").click()
    check(f"{tag}: сервер принял перевод в производство", sent.value.status == 200,
          str(sent.value.status))
    check(f"{tag}: на экране статус «в производстве»",
          _text_is(row.locator(".stbadge"), "в производстве"),
          str(row.locator(".stbadge").all_text_contents()))
    check(f"{tag}: сервер хранит status=sent", _order_api(c, oid)[0] == "sent",
          str(_order_api(c, oid)[0]))

    # «✓ Принят на склад» спрашивает confirm() и шлёт status=received БЕЗ
    # количеств (replenish.html:857-862): заказ закрывается, а сколько
    # приехало, остаётся неизвестным — выдумывать число сервер не должен.
    asked: list = []
    page.once("dialog", lambda d: (asked.append(d.message), d.accept()))
    with page.expect_response(lambda r: r.request.method == "POST"
                              and r.url.endswith(f"/api/orders/{oid}/status")) as recv:
        row.get_by_role("button", name="✓ Принят на склад").click()
    check(f"{tag}: перед приёмкой спрошено подтверждение",
          bool(asked) and "Заказ принят на склад?" in asked[0], str(asked))
    check(f"{tag}: сервер принял перевод на склад", recv.value.status == 200,
          str(recv.value.status))
    done = page.locator('#orders-done-tb tr[data-order="%s"]' % oid)
    check(f"{tag}: заказ переехал в «Принятые на склад» со статусом «принят на склад»",
          _text_is(done.locator(".stbadge"), "принят на склад"),
          str(done.locator(".stbadge").all_text_contents()))
    check(f"{tag}: и пропал из заказов в производстве", row.count() == 0,
          str(row.count()))
    status, rc = _order_api(c, oid)
    check(f"{tag}: сервер: status=received, строк приёмки 0, принято — неизвестно",
          status == "received" and rc["receipts_total"] == 0
          and rc["received_total"] is None,
          f"{status} total={rc['receipts_total']} got={rc['received_total']}")


def _open_rc_click(page, oid: int) -> None:
    page.locator('#orders-done-tb .ord-receipts[data-id="%s"]' % oid).click()
    page.wait_for_function("""() => {
      const l = document.getElementById('rc-lines');
      return !!l && !!l.querySelector('[data-rc-line]');
    }""", timeout=30000)


def _rc_input(page, base: str):
    return page.get_by_label("Принято в этот приход: " + base, exact=True)


def _rc_received_cell(page, base: str):
    return _rc_input(page, base).locator("xpath=ancestor::tr[1]").locator(
        "[data-rc-received]")


def _save_click(page, oid: int):
    with page.expect_response(lambda r: r.request.method == "POST"
                              and r.url.endswith(f"/api/orders/{oid}/receipts")) as resp:
        page.locator("#rc-save").click()
    return resp.value


def step_lifecycle_clicks(page, c, names) -> None:
    """Десктоп: цикл кликами + повтор, когда сервер ОБРАБОТАЛ, а ответ потерян.

    Частичный приход, довоз и минус на десктопе уже проверены в `step_record` —
    здесь их не дублируем. Отличие от `step_retry`: там первый запрос до
    сервера не доходит; здесь доходит и записывается, а теряется только ответ.
    """
    print("\n== Цикл заказа кликами; повтор после потерянного ответа ==")
    # Подготовка, а не предмет проверки: черновик заводится ручкой.
    oid = make_order(c, "Цикл-десктоп", [{"base_name": names[0], "qty": 9}],
                     status="draft")
    open_replenish(page)
    _lifecycle(page, c, oid, "десктоп")

    _open_rc_click(page, oid)
    _rc_input(page, names[0]).fill("4")
    lost: list = []
    route = "**/api/orders/*/receipts"

    def process_then_lose(r):
        if r.request.method != "POST" or lost:
            r.continue_()
            return
        lost.append(r.fetch().status)   # сервер запрос ВЫПОЛНИЛ…
        r.abort()                       # …а браузер ответа не получил

    page.route(route, process_then_lose)
    page.locator("#rc-save").click()
    page.wait_for_function("() => document.getElementById('rc-err')"
                           ".textContent.trim() !== ''", timeout=30000)
    page.unroute(route)
    rc = _order_api(c, oid)[1]
    check("первая попытка дошла до сервера и записана (4 шт, одна строка)",
          lost == [200] and rc["receipts_total"] == 1
          and rc["lines"][0]["received_qty"] == 4,
          f"lost={lost} total={rc['receipts_total']} qty={rc['lines'][0]['received_qty']}")
    check("экран честно говорит «Не сохранено» и хранит набранное",
          _err_text(page).startswith("Не сохранено")
          and _rc_input(page, names[0]).input_value() == "4",
          _err_text(page)[:120])

    # Человек нажимает ту же кнопку ещё раз — тот же ключ и то же тело.
    resp = _save_click(page, oid)
    body = resp.json()
    check("повтор узнан сервером как повтор: added=0, repeat=true",
          resp.status == 200 and body.get("added") == 0 and body.get("repeat") is True,
          f"{resp.status} added={body.get('added')} repeat={body.get('repeat')}")
    rc = _order_api(c, oid)[1]
    check("на сервере ровно одна строка и ровно 4 шт — двойного счёта нет",
          rc["receipts_total"] == 1 and rc["lines"][0]["received_qty"] == 4,
          f"total={rc['receipts_total']} qty={rc['lines'][0]['received_qty']}")
    check("и экран показывает принятыми 4",
          _text_is(_rc_received_cell(page, names[0]), "4")
          and _err_text(page) == "", _err_text(page)[:120])
    page.locator("#rc-close").click()


def step_mobile_lifecycle(browser, c, names) -> None:
    """390×844: цикл, частичный приход и довоз — всё настоящими нажатиями."""
    print("\n== 390×844: цикл заказа, частичный приход и довоз кликами ==")
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        oid = make_order(c, "Цикл-телефон", [{"base_name": names[1], "qty": 12}],
                         status="draft")                     # подготовка
        open_replenish(page)
        _lifecycle(page, c, oid, "390 px")

        _open_rc_click(page, oid)
        _rc_input(page, names[1]).fill("5")
        resp = _save_click(page, oid)
        rc = _order_api(c, oid)[1]
        check("390 px: частичный приход записан — 5 из 12, одна строка",
              resp.status == 200 and rc["receipts_total"] == 1
              and rc["lines"][0]["received_qty"] == 5
              and rc["lines"][0]["diff"] == -7,
              f"{resp.status} total={rc['receipts_total']} line={rc['lines'][0]}")
        check("390 px: экран показывает принятыми 5",
              _text_is(_rc_received_cell(page, names[1]), "5"))

        # Довоз — новое открытие панели, новое намерение.
        page.locator("#rc-close").click()
        _open_rc_click(page, oid)
        _rc_input(page, names[1]).fill("7")
        resp = _save_click(page, oid)
        rc = _order_api(c, oid)[1]
        check("390 px: довоз записан — 5 + 7 = 12, две строки, расхождения нет",
              resp.status == 200 and rc["receipts_total"] == 2
              and rc["lines"][0]["received_qty"] == 12
              and rc["lines"][0]["diff"] == 0 and rc["received_total"] == 12,
              f"{resp.status} total={rc['receipts_total']} line={rc['lines'][0]}")
        check("390 px: экран показывает принятыми 12",
              _text_is(_rc_received_cell(page, names[1]), "12"))
        check("390 px: ошибок в консоли не было", not errors, str(errors[:2])[:200])
    finally:
        ctx.close()


# ── PILOT-UX-ORDERS-MOBILE-1: заказы и приёмка — видимо, честно, с отзывом ──
#
# Прежние проверки 390 px мерили только `scrollWidth` страницы, а он и при
# дефекте был в норме: вбок прокручивалась не страница, а таблица заказов
# внутри себя. Кнопки «Приёмка» / «Принят на склад» / «Удалить» стояли за
# правым краем этой таблицы (x≈545–658 при окне 390), и Playwright честно
# докручивал до них сам — человек же их не видел. Поэтому здесь меряется
# ГЕОМЕТРИЯ: кнопка целиком внутри окна И внутри видимой части своей
# прокручиваемой обёртки, без единой прокрутки со стороны набора.


def _rc_post(c, oid: int, base: str, qty: float, key: str) -> None:
    r = c.post(f"/api/orders/{oid}/receipts",
               json={"lines": [{"base_name": base, "qty": qty}],
                     "idempotency_key": key})
    if r.status_code != 200:
        raise RuntimeError(f"приёмка не записана: {r.status_code} {r.text[:200]}")


def _geometry(page, selector: str):
    """Прямоугольник элемента, окно и видимая часть ближайшей прокрутки."""
    return page.evaluate("""(sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      let sc = el.parentElement;
      while (sc && sc !== document.body) {
        const s = getComputedStyle(sc);
        if (/(auto|scroll|hidden)/.test(s.overflowX)) break;
        sc = sc.parentElement;
      }
      const clip = (sc && sc !== document.body) ? sc.getBoundingClientRect()
                                                 : {left: 0, right: window.innerWidth};
      return {left: r.left, right: r.right, top: r.top, bottom: r.bottom,
              width: r.width, height: r.height, vw: window.innerWidth,
              vh: window.innerHeight, clipLeft: clip.left, clipRight: clip.right,
              fontSize: parseFloat(getComputedStyle(el).fontSize),
              lineHeight: parseFloat(getComputedStyle(el).lineHeight) || 0};
    }""", selector)


def _visible_across(g) -> bool:
    """Целиком по горизонтали: в окне и в видимой части своей прокрутки."""
    return (g is not None and g["width"] > 0 and g["left"] >= -0.5
            and g["right"] <= g["vw"] + 0.5 and g["left"] >= g["clipLeft"] - 0.5
            and g["right"] <= g["clipRight"] + 0.5)


def _confirm_text(page, oid: int, accept: bool) -> str:
    """Нажать «Удалить» у заказа и вернуть текст confirm()."""
    asked: list = []

    def answer(d):
        asked.append(d.message)
        if accept:
            d.accept()
        else:
            d.dismiss()

    page.once("dialog", answer)
    page.locator('#orders-tb .ord-del[data-id="%s"]' % oid).click()
    for _ in range(100):
        if asked:
            break
        page.wait_for_timeout(100)
    return asked[0] if asked else ""


def step_delete_warning(page, c, names) -> None:
    """«Удалить» говорит правду: история приёмки заказа уходит вместе с ним.

    Сервер удаляет строки приёмки вместе с заказом НАМЕРЕННО (`api_order_delete`
    — rowid в SQLite переиспользуется). Дефект был только в словах: confirm
    говорил лишь про «Едет», и человек стирал факты приёмки, не зная об этом.
    Число берётся существующей ручкой GET /receipts; нового API нет.
    """
    print("\n== Удаление заказа: предупреждение называет приёмку ==")
    with_rc = make_order(c, "Удаление-с-приёмкой", [{"base_name": names[0], "qty": 6}])
    _rc_post(c, with_rc, names[0], 2, "del-a")
    _rc_post(c, with_rc, names[0], 1, "del-b")
    no_rc = make_order(c, "Удаление-без-приёмки", [{"base_name": names[1], "qty": 4}])
    open_replenish(page)

    msg = _confirm_text(page, with_rc, accept=False)
    check("confirm удаления называет приёмку и число её строк (2)",
          "приёмк" in msg and "2" in msg, msg[:200])
    check("отказ в confirm ничего не удалил: заказ и 2 строки приёмки на месте",
          c.get(f"/api/orders/{with_rc}").status_code == 200
          and _order_api(c, with_rc)[1]["receipts_total"] == 2)

    msg = _confirm_text(page, no_rc, accept=False)
    check("у заказа без приёмки confirm так и говорит: приёмок нет",
          "приёмок по нему нет" in msg, msg[:200])

    # Ручка приёмки не ответила — число неизвестно, но правда остаётся:
    # история уходит вместе с заказом, просто без числа.
    route = f"**/api/orders/{with_rc}/receipts"
    page.route(route, lambda r: r.abort())
    msg = _confirm_text(page, with_rc, accept=False)
    page.unroute(route)
    check("без ответа ручки confirm всё равно предупреждает о приёмке",
          "приёмк" in msg and "удален" in msg, msg[:200])

    msg = _confirm_text(page, with_rc, accept=True)
    row = page.locator('#orders-tb tr[data-order="%s"]' % with_rc)
    gone = False
    for _ in range(100):
        if row.count() == 0:
            gone = True
            break
        page.wait_for_timeout(100)
    check("согласие удаляет заказ, как и прежде (строка ушла, сервер 404)",
          gone and c.get(f"/api/orders/{with_rc}").status_code == 404,
          f"gone={gone}")
    c.delete(f"/api/orders/{no_rc}")


def step_source_labels(page, c, names) -> None:
    """Источники приёмки названы по-человечески; правда о споре сохранена."""
    print("\n== Приёмка: источники словами, спор и «не записано» различены ==")
    fresh = make_order(c, "Источники-пусто", [{"base_name": names[0], "qty": 5}])
    open_replenish(page)
    check("панель пустого заказа открылась", open_receipts(page, fresh) is True)
    text = panel_text(page)
    check("без записанного прихода сказано «ещё не записан», а не «спорят»",
          "не записан" in text and "спорят" not in text, text[:240])
    page.locator("#rc-close").click()

    oid = make_order(c, "Источники-спор", [{"base_name": names[1], "qty": 10}])
    _rc_post(c, oid, names[1], 4, "src-a")
    _seed_receipt(c, oid, names[1], 9, "ms_order_shipped")
    open_replenish(page)
    check("панель заказа со спором открылась", open_receipts(page, oid) is True)
    text = panel_text(page)
    check("служебных кодов источников на экране нет",
          "manual" not in text and "ms_order_shipped" not in text, text[:300])
    check("ручной источник назван «вручную», МойСклад — по имени",
          "вручную" in text and "МойСклад" in text, text[:300])
    check("спор источников по-прежнему назван и итог неизвестен",
          "Расхождение источников" in text and "неизвестно" in text, text[:300])
    check("итог по-прежнему не складывает источники",
          "не складывает" in text.lower(), text[:300])
    page.locator("#rc-close").click()


def _pick_one_row(page) -> int:
    """Снять все галочки и поставить одну — как человек, кликами."""
    page.wait_for_selector("#tbody .row-check", timeout=30000)
    close_hint(page)
    if page.locator("#check-all").is_checked():
        page.locator("#check-all").click()
    page.locator("#tbody .row-check").first.click()
    return page.locator("#tbody .row-check:checked").count()


def _create_order(page, name: str, tap: bool) -> tuple:
    act = (lambda loc: loc.tap()) if tap else (lambda loc: loc.click())
    act(page.locator("#btn-create-order"))
    page.locator("#order-modal.open").wait_for(timeout=10000)
    summary = page.locator("#order-modal-summary").text_content() or ""
    page.locator("#order-name").fill(name)
    with page.expect_response(lambda r: r.request.method == "POST"
                              and r.url.endswith("/api/orders")) as created:
        act(page.locator("#btn-order-submit"))
    return created.value, summary


def _check_created(page, c, resp, name: str, summary: str, tag: str) -> None:
    oid = resp.json()["id"]
    note = page.locator("#order-created")
    try:
        note.wait_for(state="visible", timeout=30000)
        shown = note.text_content() or ""
    except Exception:  # noqa: BLE001 — отсутствие сообщения и есть ответ
        shown = ""
    check(f"{tag}: после создания видно сообщение с названием заказа",
          name in shown and "создан" in shown, shown[:200])
    row_sel = '#orders-tb tr[data-order="%s"]' % oid
    try:
        page.wait_for_function("""(sel) => {
          const r = document.querySelector(sel);
          if (!r) return false;
          const b = r.getBoundingClientRect();
          return b.top >= 0 && b.bottom <= window.innerHeight;
        }""", arg=row_sel, timeout=10000)
        in_view = True
    except Exception:  # noqa: BLE001
        in_view = False
    check(f"{tag}: новый заказ прокручен в окно", in_view,
          str(_geometry(page, row_sel)))
    check(f"{tag}: новый заказ подсвечен",
          page.locator(row_sel + ".ord-new").count() == 1)
    order = c.get(f"/api/orders/{oid}").json()
    check(f"{tag}: заказ на сервере в производстве, позиций — как в окне (1)",
          order.get("status") == "sent" and order.get("positions") == 1
          and "1 позиция" in summary,
          f"{order.get('status')} positions={order.get('positions')} «{summary[:80]}»")


def step_create_feedback(browser, c, names) -> None:
    """После «Зафиксировать размещение» человек видит, что заказ создан и где он."""
    print("\n== Создание заказа: сообщение и прокрутка к новому заказу ==")
    for tag, viewport, tap in (("десктоп", {"width": 1400, "height": 900}, False),
                               ("390 px", {"width": 390, "height": 844}, True)):
        ctx = browser.new_context(viewport=viewport, has_touch=tap)
        ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                         for k, v in c.cookies.items()])
        errors: list[str] = []
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{BASE}/replenish")
            close_hint(page)
            picked = _pick_one_row(page)
            check(f"{tag}: выбрана одна позиция", picked == 1, str(picked))
            name = f"UX-создан-{tag}"
            resp, summary = _create_order(page, name, tap)
            check(f"{tag}: сервер создал заказ", resp.status == 200, str(resp.status))
            if resp.status == 200:
                _check_created(page, c, resp, name, summary, tag)
            check(f"{tag}: ошибок в консоли не было", not errors, str(errors[:2])[:200])
        finally:
            ctx.close()


def step_mobile_orders(browser, c, names) -> None:
    """390×844, касания: действия заказа видны, приёмка вводится без прокрутки вбок."""
    print("\n== 390×844: действия заказа и поле прихода видны без прокрутки вбок ==")
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
    ctx.add_cookies([{"name": k, "value": v, "domain": "127.0.0.1", "path": "/"}
                     for k, v in c.cookies.items()])
    errors: list[str] = []
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        oid = make_order(c, "Телефон-действия", [{"base_name": names[2], "qty": 8}])
        open_replenish(page)
        row = '#orders-tb tr[data-order="%s"] ' % oid
        for cls, label in ((".ord-recv", "«Принят на склад»"),
                           (".ord-receipts", "«Приёмка»"), (".ord-del", "«Удалить»")):
            g = _geometry(page, row + cls)
            check(f"390 px: {label} целиком виден без прокрутки вбок",
                  _visible_across(g), str(g))
            check(f"390 px: {label} не ниже 40 px", g is not None and g["height"] >= 40,
                  str(g and g["height"]))
        g = _geometry(page, row + ".ord-copy")
        check("390 px: «Копировать» виден и не ниже 40 px",
              _visible_across(g) and g["height"] >= 40, str(g))
        g = _geometry(page, row + ".batchid code")
        # line-height «normal» отдаётся строкой и парсится в 0 — тогда меряем
        # по обычной для моноширинного шрифта высоте строки ≈ 1,25 кегля.
        lh = (g["lineHeight"] or g["fontSize"] * 1.25) if g else 0
        lines = (g["height"] / lh) if lh else 99
        check("390 px: идентификатор партии читается: шрифт ≥ 12 px, не больше 2 строк",
              g is not None and g["fontSize"] >= 12 and lines <= 2.2,
              f"font={g and g['fontSize']} lines={lines:.1f}")
        check("390 px: страница не прокручивается вбок",
              page.evaluate("() => document.documentElement.scrollWidth"
                            " <= window.innerWidth + 1") is True)

        page.locator(row + ".ord-receipts").tap()
        page.wait_for_function("""() => {
          const l = document.getElementById('rc-lines');
          return !!l && !!l.querySelector('[data-rc-line]');
        }""", timeout=30000)
        inp = '#rc-lines [data-rc-line] input[data-rc-qty]'
        g = _geometry(page, inp)
        check("390 px: поле «Этот приход» целиком видно без прокрутки вбок",
              _visible_across(g), str(g))
        wide = page.evaluate("""() => {
          const t = document.querySelector('#rc-panel .table-outer');
          return t ? t.scrollWidth - t.clientWidth : -1;
        }""")
        check("390 px: таблица приёмки не прокручивается вбок", 0 <= wide <= 1, str(wide))
        for sel, label in (("#rc-save", "«Записать приход»"), ("#rc-close", "«Закрыть»")):
            g = _geometry(page, sel)
            check(f"390 px: {label} виден и не ниже 40 px",
                  _visible_across(g) and g["height"] >= 40, str(g))

        page.locator(inp).tap()
        page.locator(inp).fill("3")
        with page.expect_response(lambda r: r.request.method == "POST"
                                  and r.url.endswith(f"/api/orders/{oid}/receipts")) as resp:
            page.locator("#rc-save").tap()
        check("390 px: приход записан касанием", resp.value.status == 200,
              str(resp.value.status))
        page.locator("#rc-close").tap()
        page.locator(row + ".ord-receipts").tap()
        check("390 px: после повторного открытия принято 3",
              _text_is(_rc_received_cell(page, names[2]), "3"))
        page.locator("#rc-close").tap()
        check("390 px: ошибок в консоли не было", not errors, str(errors[:2])[:200])
    finally:
        ctx.close()


# ── Мелкие помощники ────────────────────────────────────────────────────────


def _seed_receipt(c, oid: int, base: str, qty: float, source: str,
                  precision: str = "by_position") -> None:
    """Строка приёмки от ИМЕНИ ИСТОЧНИКА, которого у ручки нет.

    Ручной источник и `by_position` заводятся обычным POST; но спор источников
    и старое допущение `whole_order` иначе не воспроизвести — таких значений
    публичная ручка не принимает и принимать не должна. Пишем напрямую в базу
    набора: это фикстура прошлого состояния, а не обход контракта продукта.
    """
    import sqlite3
    from datetime import datetime
    con = sqlite3.connect(DB_PATH)
    try:
        org = con.execute("SELECT org_id FROM production_orders WHERE id=?",
                          (oid,)).fetchone()[0]
        now = datetime.utcnow().isoformat(" ", "seconds")
        con.execute(
            "INSERT INTO order_receipts (org_id, order_id, base_name, qty, at,"
            " source, precision, source_ref, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,'',NULL,?)",
            (org, oid, base, qty, now, source, precision, now))
        con.commit()
    finally:
        con.close()


def _seed_many(c, oid: int, base: str, count: int) -> None:
    import sqlite3
    from datetime import datetime
    con = sqlite3.connect(DB_PATH)
    try:
        org = con.execute("SELECT org_id FROM production_orders WHERE id=?",
                          (oid,)).fetchone()[0]
        now = datetime.utcnow().isoformat(" ", "seconds")
        con.executemany(
            "INSERT INTO order_receipts (org_id, order_id, base_name, qty, at,"
            " source, precision, source_ref, created_by, created_at)"
            " VALUES (?,?,?,?,?,'manual','by_position','',NULL,?)",
            [(org, oid, base, 1.0, now, now) for _ in range(count)])
        con.commit()
    finally:
        con.close()


def _fill_line(page, base: str, value: str) -> None:
    page.evaluate("""(arg) => {
      const tr = document.querySelector('#rc-lines [data-rc-line="' + arg.base + '"]');
      if (!tr) return;
      const i = tr.querySelector('input[data-rc-qty]');
      if (!i) return;
      i.value = arg.value;
      i.dispatchEvent(new Event('input', {bubbles: true}));
    }""", {"base": base, "value": value})


def _line_value(page, base: str) -> str:
    return page.evaluate("""(base) => {
      const tr = document.querySelector('#rc-lines [data-rc-line="' + base + '"]');
      const i = tr ? tr.querySelector('input[data-rc-qty]') : null;
      return i ? i.value : '';
    }""", base) or ""


def _submit(page) -> None:
    """Нажать «Записать приход» и дождаться, пока запрос ОТРАБОТАЛ.

    Кнопка выключается синхронно в начале отправки и включается в самом конце
    цепочки — и на успехе, и на отказе. Поэтому «кнопка снова включена» это
    точный признак завершения, в отличие от фиксированной паузы, которая на
    медленной машине истекает раньше ответа.
    """
    page.evaluate("""() => {
      const b = document.getElementById('rc-save');
      if (b) b.click();
    }""")
    try:
        page.wait_for_function(
            "() => { const b = document.getElementById('rc-save');"
            " return !!b && !b.disabled; }", timeout=30000)
    except Exception:  # noqa: BLE001 — итог всё равно проверяется по данным
        pass
    page.wait_for_timeout(250)


def _err_text(page) -> str:
    return page.evaluate(
        "() => { const e = document.getElementById('rc-err');"
        " return e ? e.textContent.trim() : ''; }") or ""


if __name__ == "__main__":
    sys.exit(main())
