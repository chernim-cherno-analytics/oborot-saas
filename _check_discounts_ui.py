"""Страница /discounts в headless chromium: JS-ошибки, скриншоты (экран + печать)."""
import asyncio
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8001"
errors = []

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 900})
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))

        await pg.goto(BASE + "/login")
        await pg.fill('input[name="email"]', "disc@example.com")
        await pg.fill('input[name="password"]', "demo12345")
        await pg.click('button[type="submit"]')
        await pg.wait_for_load_state("networkidle")

        await pg.goto(BASE + "/discounts")
        await pg.wait_for_load_state("networkidle")
        await pg.wait_for_timeout(600)
        rows = await pg.locator("#tbody tr").count()
        cards = await pg.locator("#m-frozen").inner_text()
        print("rows:", rows, "| frozen card:", cards)
        await pg.screenshot(path="_e2e_shots/discounts_page.png", full_page=True)

        # фильтр категории + поиск
        chips = pg.locator("#cat-chips .chip")
        print("chips:", await chips.count())
        if await chips.count() > 1:
            await chips.nth(1).click()
            await pg.wait_for_timeout(200)
            print("rows after cat filter:", await pg.locator("#tbody tr").count())
            await chips.nth(0).click()
        await pg.fill("#search", "худи")
        await pg.wait_for_timeout(400)
        print("rows after search 'худи':", await pg.locator("#tbody tr").count())
        await pg.fill("#search", "")
        await pg.wait_for_timeout(400)

        # печатная версия
        await pg.emulate_media(media="print")
        await pg.wait_for_timeout(200)
        await pg.screenshot(path="_e2e_shots/discounts_print.png", full_page=True)
        await pg.emulate_media(media="screen")

        await b.close()
    print("JS errors:", errors if errors else "none")

asyncio.run(main())
