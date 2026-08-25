# -*- coding: utf-8 -*-
"""Тест Telegram-уведомлений и планировщика (без pytest, просто python).

Сценарий:
  1) поднимаем mock-Telegram (эндпоинт /bot<TOKEN>/sendMessage, пишет
     полученные payload'ы в память) и mock-МойСклад (tests/mock_ms.py);
  2) поднимаем приложение с TG_API_BASE=mock, HISTORY_DAYS=5 (быстрый синк)
     и SCHEDULER_ENABLED=0 (планировщик не стартует сам — джоб зовём руками);
  3) ручки: GET/POST /api/notify/settings (owner-only), POST /api/notify/test
     («Оборот подключён» + человекочитаемая ошибка «chat not found»);
  4) демо-организация: send_daily_digest формирует корректный HTML-дайджест
     с красными (стокауты) и жёлтыми (неликвид) алертами демо-данных;
  5) планировщик в тестовом режиме: scheduler.run_daily_job() прогоняет
     инкрементальный синк org с mock-МойСклад без ошибок и шлёт дайджест.

Запуск из корня репозитория:  python tests/test_notify.py
"""
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_notify.db"
# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — прежние.
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8802"))
TG_PORT = int(os.environ.get("OBOROT_TG_PORT", "9801"))
TG_TOKEN = "test-tg-token-123"

# Окружение — ДО импорта приложения (db.py, ms_client, notify читают env).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["MS_BASE_URL"] = f"http://127.0.0.1:{os.environ.get('OBOROT_MOCK_PORT', '9800')}"
os.environ["HISTORY_DAYS"] = "5"
os.environ["SYNC_DAYS_BACK"] = "3"
os.environ["TG_API_BASE"] = f"http://127.0.0.1:{TG_PORT}"
os.environ["OBOROT_TG_BOT_TOKEN"] = TG_TOKEN
os.environ["OBOROT_TG_BOT_NAME"] = "oborot_test_bot"
os.environ["SCHEDULER_ENABLED"] = "0"  # джоб запускаем руками, не по крону

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

import mock_ms  # noqa: E402
from app import notify, scheduler  # noqa: E402
from app.main import app as oborot_app  # noqa: E402


# ── Mock Telegram Bot API ────────────────────────────────────────────────────

tg_app = FastAPI(title="mock-telegram")
TG_RECEIVED: list[dict] = []  # все принятые sendMessage-пейлоады


@tg_app.post("/bot{token}/sendMessage")
async def tg_send_message(token: str, request: Request):
    payload = await request.json()
    if token != TG_TOKEN:
        return JSONResponse(status_code=401, content={
            "ok": False, "error_code": 401, "description": "Unauthorized"})
    if str(payload.get("chat_id")) == "bad-chat":
        return JSONResponse(status_code=400, content={
            "ok": False, "error_code": 400,
            "description": "Bad Request: chat not found"})
    TG_RECEIVED.append(payload)
    return {"ok": True, "result": {"message_id": len(TG_RECEIVED)}}


# ── Инфраструктура (как в test_sync.py) ──────────────────────────────────────

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


def wait_sync_done(client: httpx.Client, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = client.get("/api/sync/status").json()
        if last.get("state") in ("done", "error"):
            return last
        time.sleep(0.5)
    return last


def main() -> int:
    tg_srv = ServerThread(tg_app, TG_PORT)
    ms_srv = ServerThread(mock_ms.app, mock_ms.PORT)
    app_srv = ServerThread(oborot_app, APP_PORT)
    tg_srv.start()
    ms_srv.start()
    app_srv.start()
    try:
        return run_scenario()
    finally:
        app_srv.stop()
        ms_srv.stop()
        tg_srv.stop()


def run_scenario() -> int:
    base = f"http://127.0.0.1:{APP_PORT}"

    print("== Демо-организация и настройки уведомлений ==")
    demo = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    r = demo.post("/register", data={
        "name": "Демо", "email": "demo-notify@test.io",
        "password": "secret123", "org_name": "Демо-бренд «Кавычки&Ко»",
    })
    check("регистрация владельца демо-организации", r.status_code == 303)
    r = demo.post("/api/connect/demo")
    check("демо-данные посеяны", r.status_code == 200 and r.json().get("ok"))

    r = demo.get("/api/notify/settings")
    j = r.json()
    check("GET /api/notify/settings: дефолты и имя бота",
          r.status_code == 200 and j.get("tg_chat_id") == "" and not j.get("tg_enabled")
          and j.get("bot_name") == "oborot_test_bot" and j.get("bot_configured") is True,
          f"resp={j}")

    r = demo.post("/api/notify/settings", json={"tg_enabled": True, "tg_chat_id": ""})
    check("включить уведомления без chat_id нельзя (422)", r.status_code == 422,
          f"status={r.status_code}")

    r = demo.post("/api/notify/settings", json={
        "tg_chat_id": "1001", "tg_enabled": True, "digest_enabled": True,
        "alerts_stockout": True, "alerts_overstock": True,
    })
    check("POST /api/notify/settings сохраняет chat_id и флаги",
          r.status_code == 200 and r.json().get("ok")
          and r.json().get("tg_chat_id") == "1001", f"resp={r.json()}")

    print("== Тестовое сообщение ==")
    n_before = len(TG_RECEIVED)
    r = demo.post("/api/notify/test", json={})
    check("POST /api/notify/test: ok", r.status_code == 200 and r.json().get("ok"),
          f"status={r.status_code} body={r.text[:120]}")
    sent = TG_RECEIVED[n_before:]
    check("тест ушёл в Telegram с текстом «Оборот подключён» (HTML)",
          len(sent) == 1 and "Оборот подключён" in sent[0]["text"]
          and sent[0].get("parse_mode") == "HTML"
          and str(sent[0]["chat_id"]) == "1001",
          f"sent={sent and sent[0]['text'][:80]}")
    check("имя организации в тесте экранировано для HTML",
          sent and "«Кавычки&amp;Ко»" in sent[0]["text"],
          f"text={sent and sent[0]['text'][:120]}")

    r = demo.post("/api/notify/test", json={"tg_chat_id": "bad-chat"})
    check("ошибка Telegram отдана человекочитаемо (chat not found)",
          r.status_code == 502 and "чат не найден" in r.json().get("detail", "").lower(),
          f"status={r.status_code} detail={r.json().get('detail', '')[:90]}")

    print("== Дайджест по демо-данным ==")
    # org_id демо-организации — из БД (первая созданная).
    import sqlite3
    con = sqlite3.connect(DB_PATH)
    demo_org_id = con.execute(
        "SELECT org_id FROM connections WHERE kind='demo'").fetchone()[0]
    con.close()

    n_before = len(TG_RECEIVED)
    ok = notify.send_daily_digest(demo_org_id)
    check("send_daily_digest отправил сообщение", ok and len(TG_RECEIVED) == n_before + 1)
    msg = TG_RECEIVED[-1]
    text = msg["text"]
    print("  ── текст дайджеста ──")
    for line in text.splitlines():
        print("  |", line)
    print("  ─────────────────────")
    check("дайджест: HTML parse_mode и правильный чат",
          msg.get("parse_mode") == "HTML" and str(msg["chat_id"]) == "1001")
    check("дайджест: шапка с названием организации и датой",
          "Оборот ·" in text and "Дайджест за" in text)
    check("дайджест: сток в деньгах и штуках",
          "Товара на складе" in text and "₽" in text and "шт" in text)
    check("дайджест: продано за 30 дней", "Продано за 30 дней" in text)
    check("дайджест: красные алерты (стокауты) из демо-данных",
          "🔴" in text and ("распродан, теряем" in text or "пора заказывать" in text),
          f"has_red={'🔴' in text}")
    check("дайджест: жёлтые алерты (неликвид/затоварка) из демо-данных",
          "🟡" in text and ("неликвид" in text or "затоварка" in text),
          f"has_yellow={'🟡' in text}")
    check("дайджест: длина ≤ 4096", len(text) <= 4096, f"len={len(text)}")

    # Выключенный дайджест — молчаливый скип.
    r = demo.post("/api/notify/settings", json={
        "tg_chat_id": "1001", "tg_enabled": True, "digest_enabled": False,
    })
    n_before = len(TG_RECEIVED)
    ok = notify.send_daily_digest(demo_org_id)
    check("digest_enabled=False → молчаливый скип",
          ok is False and len(TG_RECEIVED) == n_before)
    demo.post("/api/notify/settings", json={
        "tg_chat_id": "1001", "tg_enabled": True, "digest_enabled": True,
    })

    print("== Организация с МойСклад (mock) для планировщика ==")
    ms = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)
    r = ms.post("/register", data={
        "name": "МС", "email": "ms-notify@test.io",
        "password": "secret123", "org_name": "МС-бренд",
    })
    check("регистрация владельца МС-организации", r.status_code == 303)
    r = ms.post("/api/connect/moysklad", json={"token": mock_ms.TOKEN})
    check("токен mock-МойСклад принят", r.status_code == 200 and r.json().get("ok"))
    r = ms.post("/api/connect/moysklad/stores", json={"ext_ids": ["st-flag", "st-web"]})
    check("склады выбраны", r.status_code == 200 and r.json().get("active") == 2)
    r = ms.post("/api/sync/initial")
    check("первичный синк запущен (HISTORY_DAYS=5)", r.status_code == 200)
    status = wait_sync_done(ms)
    check("первичный синк done", status.get("state") == "done",
          f"state={status.get('state')} error={status.get('error', '')[:100]}")
    r = ms.post("/api/notify/settings", json={
        "tg_chat_id": "2002", "tg_enabled": True, "digest_enabled": True,
    })
    check("Telegram-настройки МС-организации сохранены", r.status_code == 200)

    print("== Планировщик: run_daily_job() руками ==")
    check("планировщик не поднялся сам (SCHEDULER_ENABLED=0)",
          scheduler._scheduler is None and not scheduler._started)
    n_before = len(TG_RECEIVED)
    results = scheduler.run_daily_job()
    check("джоб обошёл только организации с активным МойСклад",
          list(results.keys()) != [] and demo_org_id not in results,
          f"results={results}")
    check("инкрементальный синк всех org завершился без ошибок",
          all(v == "done" for v in results.values()), f"results={results}")
    digests = TG_RECEIVED[n_before:]
    check("после синка ушёл дайджест МС-организации",
          len(digests) == 1 and str(digests[0]["chat_id"]) == "2002"
          and "Дайджест за" in digests[0]["text"],
          f"n={len(digests)}")
    check("дайджест МС-организации: продано вчера из БД",
          digests and "Продано вчера" in digests[0]["text"],
          f"text={digests and digests[0]['text'][:200]}")

    print("== Алерт о падающем синке: подсказка соответствует причине ==")
    # Ревью 25.08.2026, discussion_r3849074704 (PR #10, DATA-10). Остановка
    # «выбранный тип цены исчез» — ошибка НАСТРОЙКИ: исправить её может только
    # владелец, и текст ошибки прямо зовёт его в Настройки. Пока она
    # поднималась голым RuntimeError, error_cause() относил её к `internal`, и
    # алерт дописывал «Мы уже разбираемся» — обещание работы там, где сервис
    # ничего сделать не может. Владелец, прочитав такое, ждёт починки вместо
    # того чтобы поменять настройку, и синк стоит все эти дни.
    con = sqlite3.connect(DB_PATH)
    ms_org_id = con.execute(
        "SELECT org_id FROM connections WHERE kind='moysklad'").fetchone()[0]
    con.close()

    def alert_text(cause: str, error: str) -> str:
        """Текст одного алерта серии на подставном состоянии (или '')."""
        # Право на алерт выдаётся атомарно и ровно один раз за серию
        # (ms_sync.claim_failure_alert): fail_streak >= 2 и alerted_streak == 0.
        # Поэтому перед каждым вызовом состояние возвращается в начало серии.
        con = sqlite3.connect(DB_PATH)
        con.execute("UPDATE sync_state SET fail_streak=2, alerted_streak=0 "
                    "WHERE org_id=?", (ms_org_id,))
        con.commit()
        con.close()
        seen = len(TG_RECEIVED)
        notify.send_sync_failure_alert(ms_org_id, {
            "fail_streak": 2, "error": error, "stats": {"error_cause": cause},
        })
        fresh = TG_RECEIVED[seen:]
        return str(fresh[0]["text"]) if fresh else ""

    PRICE_ERR = ("Синхронизация прервана: выбранный тип цены «Полная "
                 "себестоимость» больше не встречается в ассортименте "
                 "МойСклада — тип переименован или удалён")
    txt_settings = alert_text("settings", PRICE_ERR)
    check("алерт по ошибке настройки вообще ушёл", txt_settings != "",
          "send_sync_failure_alert вернул False — алерт не отправлен")
    check("алерт по ошибке настройки НЕ обещает «мы уже разбираемся»",
          txt_settings and "разбираемся" not in txt_settings,
          f"text={txt_settings[:240]}")
    check("алерт по ошибке настройки зовёт владельца в Настройки",
          txt_settings and "Настройк" in txt_settings, f"text={txt_settings[:240]}")
    check("алерт по ошибке настройки называет пропавший тип цены",
          txt_settings and "Полная себестоимость" in txt_settings,
          f"text={txt_settings[:240]}")

    # Контроль обратной стороны: переклассификация не поехала дальше своего
    # случая. Для настоящего внутреннего сбоя «мы уже разбираемся» — правда.
    txt_internal = alert_text("internal", "Синхронизация прервана внутренней ошибкой")
    check("при внутреннем сбое подсказка осталась прежней",
          "Мы уже разбираемся" in txt_internal, f"text={txt_internal[:240]}")
    txt_token = alert_text("token", "МойСклад не принял токен доступа")
    check("при отказе токена подсказка осталась прежней",
          "Проверьте токен в Настройках" in txt_token, f"text={txt_token[:240]}")

    ms.close()
    demo.close()

    print()
    print(f"ИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалены:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
