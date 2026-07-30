"""Прогон всех страниц headless chromium: скриншоты + JS-ошибки."""
import os, sys, json
from playwright.sync_api import sync_playwright

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
BASE = "http://127.0.0.1:8000"
OUT = os.path.join(os.path.dirname(__file__), "_redesign_shots")
os.makedirs(OUT, exist_ok=True)

PAGES = [
    ("dashboard", "/"),
    ("replenish", "/replenish"),
    ("turnover", "/turnover"),
    ("budget", "/budget"),
    ("forecast", "/forecast"),
    ("sizes", "/sizes"),
    ("discounts", "/discounts"),
    ("stocks", "/stocks"),
    ("orders", "/orders"),
    ("settings", "/settings"),
]

errors = []

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: errors.append((page.url, m.text)) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append((page.url, str(e))))

        # login
        page.goto(BASE + "/login")
        page.screenshot(path=f"{OUT}/login.png", full_page=True)
        page.fill("#email", "vlad@demo.ru")
        page.fill("#password", "demo12345")
        page.click("button[type=submit]")
        page.wait_for_load_state("networkidle")

        for name, url in PAGES:
            page.goto(BASE + url)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(500)
            page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
            print("shot", name)

        # register page (fresh context, no session needed — just GET)
        page.goto(BASE + "/register")
        page.wait_for_timeout(200)
        page.screenshot(path=f"{OUT}/register.png", full_page=True)

        browser.close()

    print("\nJS errors:", len(errors))
    for u, t in errors:
        print(" -", u, "::", t[:200])

run()
