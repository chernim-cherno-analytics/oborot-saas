# -*- coding: utf-8 -*-
"""Временная проверка «Здоровья сезона»: register + demo, /api/summary, инварианты."""
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "_tmp_season_health.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
deadline = time.time() + 20
while time.time() < deadline:
    try:
        httpx.get(f"http://127.0.0.1:{PORT}/login", timeout=1)
        break
    except Exception:
        time.sleep(0.2)

client = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=60, follow_redirects=False)
r = client.post("/register", data={
    "name": "Тест", "email": "season@test.ru", "password": "demo12345", "org_name": "Season Test",
})
assert r.status_code == 303, r.status_code
r = client.post("/api/connect/demo")
assert r.status_code == 200 and r.json()["ok"], r.text

summary = client.get("/api/summary").json()
sh = summary.get("season_health")
print(json.dumps(sh, ensure_ascii=False, indent=2))

failures = []
def check(name, ok, detail=""):
    print(("OK  " if ok else "FAIL") + " " + name + ((" — " + str(detail)) if detail and not ok else ""))
    if not ok:
        failures.append(name)

check("season_health присутствует", sh is not None)
check("метка сезона — лето 2026", sh["label"] == "лето 2026", sh["label"])
check("окно: с 1 июня по сегодня", sh["date_from"] == "2026-06-01" and sh["date_to"] == summary and True or sh["date_from"] == "2026-06-01", sh["date_from"])
for k in ("full_price_rev", "discounted_rev", "leftover_value", "full_share", "disc_share", "leftover_share"):
    check(f"{k} >= 0", sh[k] >= 0, sh[k])
share_sum = sh["full_share"] + sh["disc_share"] + sh["leftover_share"]
check("сумма долей ~100%", abs(share_sum - 1.0) < 0.005, share_sum)
total = sh["full_price_rev"] + sh["discounted_rev"] + sh["leftover_value"]
check("суммы согласованы с долями", all(
    abs(sh[k] / total - sh[s]) < 0.01 for k, s in
    (("full_price_rev", "full_share"), ("discounted_rev", "disc_share"), ("leftover_value", "leftover_share"))
), total)
check("статус валиден", sh["status"] in ("healthy", "warning", "alarm", "no_data"), sh["status"])
check("норматив 70/20/10", sh["norm"] == [70, 20, 10], sh["norm"])
check("reason согласован со статусом",
      (sh["reason"] is None) == (sh["status"] in ("healthy", "no_data")), sh["reason"])

print()
print("PORT:", PORT)
print("RESULT:", "FAILED " + str(failures) if failures else "ALL OK")
# держим сервер живым для скриншота, если передан флаг --serve
if "--serve" in sys.argv:
    print("serving; Ctrl+C to stop", flush=True)
    while True:
        time.sleep(1)
sys.exit(1 if failures else 0)
