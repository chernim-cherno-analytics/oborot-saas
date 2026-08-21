# -*- coding: utf-8 -*-
"""Интеграционный тест приложения маркетплейса МойСклад (без pytest).

Сценарий (Vendor API 1.0, контракт из исследования):
  1) mock-МойСклад (tests/mock_ms.py: JSON API 1.2 + vendor-эндпоинт
     POST /context/{key}) на 127.0.0.1:9800; приложение на 127.0.0.1:8803
     с MS_VENDOR_API_BASE=mock и ключами приложения из mock_ms;
  2) PUT lifecycle (Install, валидный JWT HS256) → 200 {"status":"Activated"}:
     орг создана (source='ms_app', план из тарифа), токен зашифрован,
     склады выбраны ВСЕ автоматически, первичный синк стартовал сам и дошёл
     до done против mock-МС;
  3) повторный PUT (TariffChanged) → та же орг (без дублей), план обновлён,
     синк не перезапущен;
  4) GET /ms/app?contextKey → сессионная кука + redirect /, пользователь по
     ms_uid создан ровно один раз (второй вход не плодит), роль owner;
  5) PUT с битой подписью → 401; чужой appId → 404; левый contextKey → не 500;
  6) DELETE (Uninstall) → org.status='suspended', планировщик её пропускает;
     PUT (Resume) возвращает в строй;
  7) изоляция: обычная SaaS-регистрация + демо-данные работают как раньше.

Запуск из корня репозитория:  python tests/test_vendor.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_vendor.db"
APP_PORT = 8803

import mock_ms  # noqa: E402 — констант ниже требует env

# Окружение — ДО импорта приложения (db.py, ms_client и ms_vendor читают env).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = "http://127.0.0.1:9800"
os.environ["MS_VENDOR_API_BASE"] = "http://127.0.0.1:9800"
os.environ["MS_APP_ID"] = mock_ms.VENDOR_APP_ID
os.environ["MS_APP_UID"] = mock_ms.VENDOR_APP_UID
os.environ["MS_APP_SECRET"] = mock_ms.VENDOR_SECRET
os.environ["HISTORY_DAYS"] = "60"
os.environ["SYNC_DAYS_BACK"] = "3"
os.environ["SCHEDULER_ENABLED"] = "0"  # джоб не нужен; _orgs_… зовём напрямую

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import jwt as pyjwt  # noqa: E402
import uvicorn  # noqa: E402

from app import ms_sync, scheduler  # noqa: E402 — тот же процесс, что и сервер
from app.main import app as oborot_app  # noqa: E402

LIFECYCLE_URL = (f"/ms/vendor/api/moysklad/vendor/1.0/apps/"
                 f"{mock_ms.VENDOR_APP_ID}/{mock_ms.VENDOR_ACCOUNT_ID}")


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def ms_jwt(secret: str = mock_ms.VENDOR_SECRET) -> str:
    """JWT «от МойСклад»: HS256 общим секретом приложения."""
    now = int(time.time())
    return pyjwt.encode({"sub": "moysklad-vendor-api", "iat": now, "exp": now + 300},
                        secret, algorithm="HS256")


def install_body(cause: str, tariff: str, trial: bool) -> dict:
    return {
        "appUid": mock_ms.VENDOR_APP_UID,
        "accountName": mock_ms.VENDOR_ACCOUNT_NAME,
        "cause": cause,
        "access": [{
            "access_token": mock_ms.TOKEN,
            "resource": "https://api.moysklad.ru/api/remap/1.2",
            "scope": ["admin"],
        }],
        "subscription": {
            "tariffId": "tariff-0001", "tariffName": tariff,
            "trial": trial, "expiryMoment": "2026-08-13 00:00:00",
        },
    }


def wait_sync_done(org_id: int, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = ms_sync.get_status(org_id)
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(1.0)
    return last


def q1(sql: str, *args):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def main() -> int:
    mock_srv = ServerThread(mock_ms.app, mock_ms.PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    mock_srv.start()
    app_srv.start()
    try:
        return run_scenario()
    finally:
        app_srv.stop()
        mock_srv.stop()


def run_scenario() -> int:
    base = f"http://127.0.0.1:{APP_PORT}"
    client = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=60.0)
    auth_hdr = {"Authorization": f"Bearer {ms_jwt()}"}

    print("== Установка из каталога МС (PUT Install) ==")
    r = client.put(LIFECYCLE_URL, json=install_body("Install", "Оборот Бренд", True),
                   headers=auth_hdr)
    check("PUT Install → 200 Activated",
          r.status_code == 200 and r.json().get("status") == "Activated",
          f"status={r.status_code} body={r.text[:100]}")

    row = q1("SELECT id, name, plan, source, status, ms_account_id, ms_tariff_name,"
             " trial_ends_at FROM orgs WHERE ms_account_id=?",
             mock_ms.VENDOR_ACCOUNT_ID)
    check("организация создана сама (source='ms_app')",
          row is not None and row["source"] == "ms_app"
          and row["name"] == mock_ms.VENDOR_ACCOUNT_NAME
          and row["status"] == "active",
          f"got={dict(row) if row else None}")
    if row is None:
        print("  … без организации дальше нечего проверять")
        return 1
    org_id = row["id"]
    check("триал МС → план trial + trial_ends_at из expiryMoment",
          row["plan"] == "trial" and (row["trial_ends_at"] or "").startswith("2026-08-13"),
          f"plan={row['plan']} trial_ends_at={row['trial_ends_at']}")

    crow = q1("SELECT token_enc, status FROM connections WHERE org_id=? "
              "AND kind='moysklad'", org_id)
    check("подключение создано, токен зашифрован (не plaintext)",
          crow is not None and crow["token_enc"]
          and mock_ms.TOKEN not in crow["token_enc"])

    print("== Автоматика: склады + первичный синк ==")
    wrow = q1("SELECT COUNT(*) c, SUM(active) a FROM warehouses WHERE org_id=?", org_id)
    check("выбраны ВСЕ склады аккаунта (3), все активны",
          wrow["c"] == 3 and wrow["a"] == 3, f"count={wrow['c']} active={wrow['a']}")

    t0 = time.time()
    status = wait_sync_done(org_id)
    check("первичный синк стартовал сам и дошёл до done",
          status.get("state") == "done",
          f"state={status.get('state')} error={status.get('error', '')[:120]}")
    print(f"  … синк занял {time.time() - t0:.1f} c, stats={status.get('stats', {})}")

    crow = q1("SELECT status, last_sync_at FROM connections WHERE org_id=? "
              "AND kind='moysklad'", org_id)
    check("после синка подключение active", crow["status"] == "active")

    prow = q1("SELECT COUNT(*) c FROM products WHERE org_id=?", org_id)
    expected_products = len(mock_ms.SKUS) + len(mock_ms.SIZED)
    check("товары засинканы полностью", prow["c"] == expected_products,
          f"got={prow['c']} expected={expected_products}")

    all_stores = tuple(s for s, _ in mock_ms.STORES)
    exp_units = round(sum(
        mock_ms.expected_stock_today(stores=all_stores, include_service=True).values()
    ))
    srow = q1("SELECT ROUND(SUM(qty)) q FROM warehouse_stock WHERE org_id=?", org_id)
    check("текущий остаток = эталон mock-мира по ВСЕМ складам",
          srow["q"] == exp_units, f"got={srow['q']} expected={exp_units}")

    print("== Идемпотентность (PUT TariffChanged) ==")
    r = client.put(LIFECYCLE_URL, json=install_body("TariffChanged", "Оборот Про", False),
                   headers={"Authorization": f"Bearer {ms_jwt()}"})
    nrow = q1("SELECT COUNT(*) c FROM orgs WHERE ms_account_id=?",
              mock_ms.VENDOR_ACCOUNT_ID)
    check("повторный PUT → 200, организация НЕ задублирована",
          r.status_code == 200 and nrow["c"] == 1, f"orgs={nrow['c']}")
    row = q1("SELECT plan, ms_tariff_name FROM orgs WHERE id=?", org_id)
    check("план обновлён из нового тарифа (Про → pro)",
          row["plan"] == "pro" and row["ms_tariff_name"] == "Оборот Про",
          f"plan={row['plan']}")
    check("первичный синк НЕ перезапущен (state остался done)",
          ms_sync.get_status(org_id).get("state") == "done"
          and not ms_sync.is_running(org_id))

    print("== Вход из iframe (GET /ms/app) ==")
    mock_ms.VENDOR_CONTEXTS["ck-good"] = {
        "accountId": mock_ms.VENDOR_ACCOUNT_ID,
        "uid": "admin@testbrand",
        "fullName": "Иван Тестов",
    }
    r = client.get("/ms/app", params={"contextKey": "ck-good"},
                   follow_redirects=False)
    set_cookie = r.headers.get("set-cookie", "")
    check("contextKey → сессионная кука + redirect на /",
          r.status_code == 302 and r.headers.get("location") == "/"
          and "oborot_session=" in set_cookie,
          f"status={r.status_code} cookie={set_cookie[:60]}")
    check("в dev кука SameSite=Lax (None+Secure — только на проде)",
          "samesite=lax" in set_cookie.lower(), set_cookie[-80:])

    urow = q1("SELECT id, email, name FROM users WHERE ms_uid=?", "admin@testbrand")
    check("пользователь создан по ms_uid (email uid@ms.local, имя из контекста)",
          urow is not None and urow["email"] == "admin@testbrand@ms.local"
          and urow["name"] == "Иван Тестов",
          f"got={dict(urow) if urow else None}")
    mrow = q1("SELECT role FROM memberships WHERE user_id=? AND org_id=?",
              urow["id"], org_id)
    check("первый пользователь организации — owner",
          mrow is not None and mrow["role"] == "owner")

    r2 = client.get("/", follow_redirects=False)  # кука уже в клиенте
    check("по куке из iframe «/» ведёт в Оборачиваемость (дашборд скрыт)",
          r2.status_code == 302 and (r2.headers.get("location") or "") == "/turnover",
          f"status={r2.status_code} loc={r2.headers.get('location')}")

    client.get("/ms/app", params={"contextKey": "ck-good"}, follow_redirects=False)
    cnt = q1("SELECT COUNT(*) c FROM users WHERE ms_uid IS NOT NULL")
    check("повторный вход не плодит пользователей", cnt["c"] == 1,
          f"users={cnt['c']}")

    check("CSP разрешает фрейм только себе и online.moysklad.ru",
          "frame-ancestors 'self' https://online.moysklad.ru"
          in r2.headers.get("content-security-policy", ""),
          r2.headers.get("content-security-policy", "нет заголовка"))

    print("== Отказы ==")
    r = client.put(LIFECYCLE_URL, json=install_body("Install", "Оборот Про", False),
                   headers={"Authorization": f"Bearer {ms_jwt('wrong-secret')}"})
    check("PUT с битой подписью JWT → 401", r.status_code == 401,
          f"status={r.status_code}")
    r = client.request("DELETE", LIFECYCLE_URL,
                       headers={"Authorization": "Bearer not-a-jwt"})
    check("DELETE без валидного JWT → 401", r.status_code == 401,
          f"status={r.status_code}")
    r = client.put(
        f"/ms/vendor/api/moysklad/vendor/1.0/apps/other-app/{mock_ms.VENDOR_ACCOUNT_ID}",
        json=install_body("Install", "Оборот Про", False), headers=auth_hdr)
    check("PUT с чужим appId → 404", r.status_code == 404, f"status={r.status_code}")
    # Аудит 18.08 (#11): отражение нашего же исходящего токена (тот же секрет)
    from app import ms_vendor as _msv
    r = client.put(LIFECYCLE_URL, json=install_body("Install", "Оборот Про", False),
                   headers={"Authorization": f"Bearer {_msv.make_jwt()}"})
    check("отражение нашего исходящего JWT (dir=out) → 401", r.status_code == 401,
          f"status={r.status_code}")
    # accountId в claims не совпадает с accountId пути → 401
    _now = int(time.time())
    _foreign = pyjwt.encode(
        {"sub": "moysklad-vendor-api", "iat": _now, "exp": _now + 300,
         "accountId": "acc-CHUZHOY-0001"},
        mock_ms.VENDOR_SECRET, algorithm="HS256")
    r = client.put(LIFECYCLE_URL, json=install_body("Install", "Оборот Про", False),
                   headers={"Authorization": f"Bearer {_foreign}"})
    check("JWT с чужим accountId в claims → 401", r.status_code == 401,
          f"status={r.status_code}")
    # МС-подобный токен (без dir/jti/accountId) по-прежнему проходит
    r = client.put(LIFECYCLE_URL, json=install_body("TariffChanged", "Оборот Про", False),
                   headers={"Authorization": f"Bearer {ms_jwt()}"})
    check("МС-токен без dir/jti/accountId проходит (200)", r.status_code == 200,
          f"status={r.status_code}")
    anon = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=f"http://127.0.0.1:{APP_PORT}", timeout=30.0)
    r = anon.get("/ms/app", params={"contextKey": "ck-unknown"})
    check("левый contextKey → человеческая ошибка, не 500",
          r.status_code in (401, 404) and "МойСклад" in r.text,
          f"status={r.status_code}")
    r = anon.get("/ms/app")
    check("вход без contextKey → 400 с подсказкой", r.status_code == 400,
          f"status={r.status_code}")

    print("== Uninstall / Resume и планировщик ==")
    ids = scheduler._orgs_with_active_moysklad()
    check("до Uninstall планировщик видит организацию", org_id in ids, f"ids={ids}")
    r = client.request("DELETE", LIFECYCLE_URL, headers=auth_hdr,
                       json={"appUid": mock_ms.VENDOR_APP_UID, "cause": "Uninstall"})
    row = q1("SELECT status FROM orgs WHERE id=?", org_id)
    check("DELETE Uninstall → 200, org.status='suspended'",
          r.status_code == 200 and row["status"] == "suspended",
          f"status={r.status_code} org_status={row['status']}")
    ids = scheduler._orgs_with_active_moysklad()
    check("suspended-организацию планировщик пропускает", org_id not in ids,
          f"ids={ids}")
    r = client.request("DELETE",
                       f"/ms/vendor/api/moysklad/vendor/1.0/apps/"
                       f"{mock_ms.VENDOR_APP_ID}/acc-nonexistent",
                       headers=auth_hdr)
    check("DELETE неизвестного аккаунта идемпотентен (200)", r.status_code == 200)

    r = client.put(LIFECYCLE_URL, json=install_body("Resume", "Оборот Про", False),
                   headers=auth_hdr)
    row = q1("SELECT status FROM orgs WHERE id=?", org_id)
    ids = scheduler._orgs_with_active_moysklad()
    check("PUT Resume возвращает организацию в строй",
          r.status_code == 200 and row["status"] == "active" and org_id in ids)

    print("== Обычный SaaS не сломан ==")
    saas = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=f"http://127.0.0.1:{APP_PORT}", timeout=120.0)
    r = saas.post("/register", data={
        "name": "Сааc", "email": "saas@test.io",
        "password": "secret123", "org_name": "Обычный бренд",
    })
    check("обычная регистрация работает", r.status_code == 303,
          f"status={r.status_code}")
    orow = q1("SELECT source, status, ms_account_id FROM orgs WHERE name='Обычный бренд'")
    check("SaaS-организация: source='saas', status='active', без ms_account_id",
          orow is not None and orow["source"] == "saas"
          and orow["status"] == "active" and orow["ms_account_id"] is None,
          f"got={dict(orow) if orow else None}")
    r = saas.post("/api/connect/demo")
    check("демо-подключение работает", r.status_code == 200 and r.json().get("ok"))
    dsum = saas.get("/api/summary").json()
    check("демо-данные посеялись (позиций ~55)", dsum["positions"] >= 45,
          f"positions={dsum['positions']}")
    ms_sum = client.get("/api/summary").json()
    check("изоляция тенантов: числа МС-организации ≠ демо",
          ms_sum["stock_units"] != dsum["stock_units"],
          f"{ms_sum['stock_units']} vs {dsum['stock_units']}")
    saas.close()
    anon.close()
    client.close()

    print()
    print(f"ИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалены:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
