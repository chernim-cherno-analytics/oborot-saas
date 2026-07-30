"""Интеракции: фильтр Срочно, плитка риска, сортировка, раскрытие, заказ, печать."""
import os
from playwright.sync_api import sync_playwright

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
BASE = "http://127.0.0.1:8000"
OUT = os.path.join(os.path.dirname(__file__), "_redesign_shots")
os.makedirs(OUT, exist_ok=True)
errors = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.on("console", lambda m: errors.append((page.url, m.text)) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append((page.url, str(e))))

    page.goto(BASE + "/login")
    page.fill("#email", "vlad@demo.ru")
    page.fill("#password", "demo12345")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

    # 1. Дашборд: кнопка «Показать» на красной плитке
    page.goto(BASE + "/")
    page.wait_for_load_state("networkidle")
    page.click("#btn-show-urgent")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    assert "/replenish?f=urgent" in page.url, page.url
    rows = page.locator("#tbody > tr:not(.expand-row)").count()
    badges = page.locator("#tbody .st.r").count()
    print("dash->replenish urgent rows:", rows, "badges:", badges)
    assert rows == badges and rows > 0
    page.screenshot(path=f"{OUT}/i_replenish_urgent.png")

    # 2. Сегмент «Все», затем плитка риска «Показать» на самой странице
    page.click("#band-seg button[data-band='']")
    page.wait_for_timeout(200)
    all_rows = page.locator("#tbody > tr:not(.expand-row)").count()
    print("all rows:", all_rows)
    page.click("#btn-show-urgent")
    page.wait_for_timeout(300)
    assert page.locator("#tbody > tr:not(.expand-row)").count() == rows
    page.click("#band-seg button[data-band='']")
    page.wait_for_timeout(200)

    # 3. Сортировка: клик по «Остаток» (cs)
    page.click("th[data-sort='cs']")
    page.wait_for_timeout(200)
    first = page.locator("#tbody > tr:not(.expand-row)").first.inner_text()
    print("after sort desc cs, first row:", first.split("\n")[0])
    page.click("th[data-sort='turnover']")  # вернуть сортировку
    page.wait_for_timeout(200)

    # 4. Раскрытие строки (размерная сетка + формула)
    page.locator("#tbody .expander").first.click()
    page.wait_for_timeout(300)
    assert page.locator(".sub-panel").count() == 1
    page.screenshot(path=f"{OUT}/i_replenish_expanded.png")

    # 5. Поиск пословный
    page.fill("#search", "худи скетч")
    page.wait_for_timeout(400)
    n = page.locator("#tbody > tr:not(.expand-row)").count()
    print("search 'худи скетч' rows:", n)
    assert n == 1
    page.fill("#search", "")
    page.wait_for_timeout(400)

    # 6. Создание заказа: модалка
    page.click("#btn-create-order")
    page.wait_for_timeout(300)
    assert page.locator("#order-modal.open").count() == 1
    page.screenshot(path=f"{OUT}/i_order_modal.png")
    page.click("#btn-order-submit")
    page.wait_for_url("**/orders", timeout=8000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    page.screenshot(path=f"{OUT}/orders.png", full_page=True)

    # 7. Печать заказа — эмуляция print (светлая)
    page.emulate_media(media="print")
    page.screenshot(path=f"{OUT}/i_order_print.png", full_page=True)
    page.emulate_media(media="screen")

    # 8. Скидки: печатный прайс
    page.goto(BASE + "/discounts")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    page.emulate_media(media="print")
    page.screenshot(path=f"{OUT}/i_discounts_print.png")
    page.emulate_media(media="screen")

    # 9. Онбординг (после logout нового юзера не делаем — просто скриншот с новым акком)
    browser.close()

print("\nJS errors:", len(errors))
for u, t in errors:
    print(" -", u, "::", t[:200])
