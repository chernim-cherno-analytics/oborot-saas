# -*- coding: utf-8 -*-
"""Временный скриншот карточки «Здоровье сезона» на дашборде."""
import asyncio
import sys

from playwright.async_api import async_playwright

BASE = f"http://127.0.0.1:{sys.argv[1]}"
errors = []


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 1440, "height": 1000})
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append(str(e)))

        await pg.goto(BASE + "/login")
        await pg.fill('input[name="email"]', "season@test.ru")
        await pg.fill('input[name="password"]', "demo12345")
        await pg.click('button[type="submit"]')
        await pg.wait_for_load_state("networkidle")
        await pg.wait_for_timeout(1200)
        await pg.screenshot(path="_season_health_dashboard.png", full_page=True)
        card = pg.locator("#season-box")
        await pg.locator("div.card", has=card).screenshot(path="_season_health_card.png")
        txt = await pg.locator("#season-title").inner_text()
        badge = await pg.locator("#season-badge").inner_text()
        legend = await pg.locator(".season-legend").inner_text()
        msg = await pg.locator(".season-msg").inner_text()
        print("title:", txt)
        print("badge:", badge)
        print("legend:", legend.replace("\n", " | "))
        print("msg:", msg)
        await b.close()
    print("console errors:", errors if errors else "none")


asyncio.run(main())
