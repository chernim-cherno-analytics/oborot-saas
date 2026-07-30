# -*- coding: utf-8 -*-
"""UI-проверка: переключатель окна темпа + предупреждение «дыра» на /replenish,
lead time на /settings. Сервер уже поднят на 8802 с посеянной демо-организацией."""
import asyncio
import os

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8802"
OUT = "_season_shots"
os.makedirs(OUT, exist_ok=True)
errors = []


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1560, "height": 950})
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))

        # логин под уже созданной демо-организацией
        await pg.goto(BASE + "/login")
        await pg.fill('input[name="email"]', "season@test.io")
        await pg.fill('input[name="password"]', "secret123")
        await pg.click('button[type="submit"]')
        await pg.wait_for_load_state("networkidle")

        await pg.goto(BASE + "/replenish")
        await pg.wait_for_selector("#tbody tr td .need, #tbody .need", timeout=15000)
        await pg.wait_for_timeout(400)

        seg = pg.locator("#rate-seg button.on")
        on_txt = await seg.first.inner_text()
        print("активная кнопка окна:", on_txt.strip())
        gap_cells = await pg.locator("#tbody .txt-red:has-text('⚠')").count()
        print("ячеек с ⚠ дырой:", gap_cells)
        note = await pg.locator("#horizon-note").inner_text()
        print("note:", note[:120])
        sub = await pg.locator("#risk-tile .m-sub").inner_text()
        print("kpi sub:", sub)
        await pg.screenshot(path=f"{OUT}/1_replenish_year.png", full_page=False)

        # раскрыть первую строку — формула с тремя темпами и дырой
        await pg.locator("#tbody .expander").first.click()
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/2_replenish_expanded.png")

        # переключить на «Сезон» — POST настройки + перезагрузка данных
        need_before = await pg.locator("#f-need").inner_text()
        await pg.click("#rw-season")
        await pg.wait_for_timeout(1500)
        await pg.wait_for_selector("#tbody .need", timeout=15000)
        on_txt2 = await pg.locator("#rate-seg button.on").first.inner_text()
        need_after = await pg.locator("#f-need").inner_text()
        print("после клика активная:", on_txt2.strip(), "| итого need:", need_before.strip(), "→", need_after.strip())
        await pg.screenshot(path=f"{OUT}/3_replenish_season.png")

        # модалка заказа: ETA = today + lead_time (45)
        await pg.click("#btn-create-order")
        await pg.wait_for_timeout(300)
        eta = await pg.input_value("#order-eta")
        print("ETA в модалке:", eta)
        await pg.screenshot(path=f"{OUT}/4_order_modal_eta.png")
        await pg.click("#btn-order-cancel")

        # settings: lead time поле + сохранение
        await pg.goto(BASE + "/settings")
        await pg.wait_for_selector("#lead-time", timeout=10000)
        await pg.wait_for_timeout(500)
        lead_val = await pg.input_value("#lead-time")
        print("lead-time в настройках:", lead_val)
        await pg.fill("#lead-time", "60")
        await pg.click("#btn-save-horizon")
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=f"{OUT}/5_settings_leadtime.png")
        await pg.reload()
        await pg.wait_for_selector("#lead-time", timeout=10000)
        await pg.wait_for_timeout(500)
        lead_val2 = await pg.input_value("#lead-time")
        print("lead-time после перезагрузки:", lead_val2)

        # вернуть 45 и окно year для чистоты
        await pg.fill("#lead-time", "45")
        await pg.click("#btn-save-horizon")
        await pg.wait_for_timeout(500)

        await b.close()

    print("JS-ошибки:", errors if errors else "нет")
    return 1 if errors or lead_val2 != "60" else 0


raise SystemExit(asyncio.run(main()))
