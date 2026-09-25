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
