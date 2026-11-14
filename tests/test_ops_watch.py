# -*- coding: utf-8 -*-
"""Наблюдение оператора (OPS-WATCH-1, 14.11.2026).

Что доказывается:
  1) падающим синком считаются: серия провалов, давно не было успеха, давно
     подключённая организация без единого успеха; НЕ считаются: здоровые,
     приостановленные, демо, свежеподключённые;
  2) служебное оповещение уходит один раз на набор, сразу при новом
     участнике, повторно через сутки, и забывает прошлое, когда всё починено;
  3) текст оповещения фиксированный: число и время, без имён организаций и
     строк извне; неверное число отвергается;
  4) необработанная ошибка: клиент видит общий текст без типа и трассировки,
     счётчик растёт, оператор получает число один раз;
  5) отчёт оператора видит падающую организацию, не печатает токены, работает
     в JSON;
  6) воспроизведение инцидента 28.09–13.11: 1 146 провалов при пустом чате
     организации раньше не давали ни одного оповещения, теперь дают.

Синтетическая база, наружу набор не ходит. Запуск: python tests/test_ops_watch.py
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_ops_watch.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.pop("OBOROT_OPS_CHAT_ID", None)
os.environ.pop("OBOROT_TG_BOT_TOKEN", None)

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Connection, Org, SyncState  # noqa: E402
from app import notify, ops_watch  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  OK   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


init_db()
NOW = datetime(2026, 11, 14, 3, 0)
SECRET = "gAAAA-SYNTHETIC-ENCRYPTED-TOKEN-DO-NOT-PRINT"


def mk(db, name, *, kind="moysklad", status="active", conn_status="active",
       last_ok=None, streak=0, created=None, error=""):
    org = Org(name=name, plan="trial", status=status, trial_ends_at=NOW + timedelta(days=30),
              created_at=created or NOW - timedelta(days=60))
    db.add(org)
    db.flush()
    db.add(Connection(org_id=org.id, kind=kind, status=conn_status,
                      token_enc=SECRET, last_sync_at=last_ok))
    db.add(SyncState(org_id=org.id, state="error" if streak else "done",
                     fail_streak=streak, error=error))
    db.flush()
    return org.id


db = SessionLocal()
ids = {
    "healthy": mk(db, "Здоровый Бренд", last_ok=NOW - timedelta(hours=3)),
    "streak": mk(db, "Бренд Серия Провалов", last_ok=NOW - timedelta(hours=5), streak=4,
                 error="МойСклад не принял токен доступа."),
    "stale": mk(db, "Бренд Без Свежих Данных", last_ok=NOW - timedelta(days=2)),
    "suspended": mk(db, "Приостановленный Бренд", status="suspended",
                    last_ok=NOW - timedelta(days=20), streak=9),
    "demo": mk(db, "Демо Бренд", kind="demo", last_ok=None),
    "fresh_pending": mk(db, "Только Подключился", conn_status="pending", last_ok=None,
                        created=NOW - timedelta(hours=2)),
    "old_pending": mk(db, "Брошенный Онбординг", conn_status="pending", last_ok=None,
                      created=NOW - timedelta(days=3)),
    "never_ok": mk(db, "Активно Без Единого Успеха", last_ok=None,
                   created=NOW - timedelta(days=3)),
    "initial_running": mk(db, "Старый Аккаунт Грузит Историю", last_ok=None,
                          created=NOW - timedelta(days=90)),
    "readonly": mk(db, "Просроченный Триал", last_ok=NOW - timedelta(days=5)),
    "pending_failing": mk(db, "Новый Клиент Упала Первичная", conn_status="pending",
                          last_ok=None, streak=40, created=NOW - timedelta(days=3),
                          error="МойСклад не принял токен доступа."),
}
st_run = db.get(SyncState, ids["initial_running"])
st_run.state, st_run.mode = "running", "initial"
ro = db.get(Org, ids["readonly"])
ro.trial_ends_at = NOW - timedelta(days=10)
db.commit()

print("\n1. Кто считается падающим")
failing = {f["org_id"] for f in ops_watch.failing_syncs(db, NOW)}
check("гейт выключен: серия провалов, давний успех, активное без успеха, просроченный триал и упавшая первичная (pending с провалами) — падающие",
      failing == {ids["streak"], ids["stale"], ids["never_ok"], ids["readonly"],
                  ids["pending_failing"]}, str(failing))
check("здоровый, приостановленный, демо, незавершённое подключение и идущая первичная загрузка — нет",
      not failing & {ids["healthy"], ids["suspended"], ids["demo"], ids["fresh_pending"],
                     ids["old_pending"], ids["initial_running"]})
os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
failing_gate = {f["org_id"] for f in ops_watch.failing_syncs(db, NOW)}
os.environ.pop("OBOROT_SUBSCRIPTION_GATE")
check("гейт включён: организацию в readonly планировщик не синкает, она не «падает»",
      ids["readonly"] not in failing_gate and ids["streak"] in failing_gate, str(failing_gate))
db.close()

print("\n2. Когда уходит служебное оповещение")
SENT = []


def fake_send(kind, unit=None):
    notify.ops_text(kind, unit)  # тот же контракт текста, что у настоящей отправки
    SENT.append((kind, unit))
    return True, ""


ops_watch.reset_for_tests()
r1 = ops_watch.run_once(NOW, send=fake_send)
check("первый проход: одно оповещение с числом 5", SENT == [("sync-failing", "5")], str(SENT))
r2 = ops_watch.run_once(NOW + timedelta(hours=1), send=fake_send)
check("тот же набор через час: тишина", len(SENT) == 1 and not r2["sync_sent"], str(SENT))
r3 = ops_watch.run_once(NOW + timedelta(hours=25), send=fake_send)
check("тот же набор через сутки: напоминание", len(SENT) == 2 and r3["sync_sent"], str(SENT))

db = SessionLocal()
new_bad = mk(db, "Новый Падающий", last_ok=NOW - timedelta(hours=1), streak=3)
db.commit()
db.close()
ops_watch.run_once(NOW + timedelta(hours=26), send=fake_send)
check("новая падающая организация: сразу, с новым числом",
      SENT[-1] == ("sync-failing", "6") and len(SENT) == 3, str(SENT))

db = SessionLocal()
for o in db.query(Connection).all():
    o.last_sync_at = NOW + timedelta(hours=26)
for s in db.query(SyncState).all():
    s.fail_streak = 0
db.commit()
db.close()
r4 = ops_watch.run_once(NOW + timedelta(hours=27), send=fake_send)
check("всё починено: тишина", len(SENT) == 3 and r4["failing"] == 0, str(r4))
db = SessionLocal()
st = db.get(SyncState, ids["streak"])
st.fail_streak = 5
db.commit()
db.close()
ops_watch.run_once(NOW + timedelta(hours=28), send=fake_send)
check("после починки новая серия приходит сразу, а не через сутки",
      SENT[-1] == ("sync-failing", "1"), str(SENT))

FAILED_SEND = []
ops_watch.reset_for_tests()
ops_watch.run_once(NOW + timedelta(hours=28), send=lambda k, u=None: (FAILED_SEND.append(k) or (False, "нет канала")))
ops_watch.run_once(NOW + timedelta(hours=29), send=lambda k, u=None: (FAILED_SEND.append(k) or (False, "нет канала")))
check("неудачная отправка не считается отправленной: попытка повторяется",
      FAILED_SEND == ["sync-failing", "sync-failing"], str(FAILED_SEND))

print("\n3. Текст оповещения")
txt = notify.ops_text("sync-failing", "3", now=NOW)
check("текст содержит число и подсказку оператору",
      "3" in txt and "tools/ops_report.py" in txt, txt)
names = ["Здоровый", "Серия", "Свежих", "токен"]
check("в тексте нет имён организаций и текста ошибок",
      not any(n in txt for n in names), txt)
for bad in ("0", "-1", "abc", None, "3; rm -rf /"):
    try:
        notify.ops_text("sync-failing", bad)
        check(f"неверное число {bad!r} отвергнуто", False)
    except ValueError:
        check(f"неверное число {bad!r} отвергнуто", True)
err_txt = notify.ops_text("server-errors", "2", now=NOW)
check("текст об ошибках сервера называет число и журнал",
      "2" in err_txt and "journalctl" in err_txt, err_txt)

print("\n4. Необработанная ошибка сервера")
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def _boom():
    raise RuntimeError("секретная деталь /opt/oborot/data")


app.add_api_route("/api/__ops_watch_boom", _boom, methods=["GET"])
app.add_api_route("/__ops_watch_boom_page", _boom, methods=["GET"])
ops_watch.reset_for_tests()
client = TestClient(app, raise_server_exceptions=False)
resp = client.get("/api/__ops_watch_boom")
check("API: 500 с общим текстом", resp.status_code == 500
      and "на нашей стороне" in resp.json().get("detail", ""), resp.text[:200])
check("API: в ответе нет типа ошибки, пути и детали",
      "RuntimeError" not in resp.text and "/opt/oborot" not in resp.text
      and "секретная" not in resp.text, resp.text[:200])
page = client.get("/__ops_watch_boom_page")
check("страница: 500 человеческой страницей без детали",
      page.status_code == 500 and "Ошибка на нашей стороне" in page.text
      and "секретная" not in page.text, page.text[:200])
check("счётчик ошибок: 2", ops_watch.recent_server_errors() == 2,
      str(ops_watch.recent_server_errors()))
SENT.clear()
now = datetime.utcnow()
ops_watch.run_once(now, send=fake_send)
check("оператор получил число ошибок один раз", ("server-errors", "2") in SENT, str(SENT))
SENT.clear()
ops_watch.run_once(now + timedelta(minutes=5), send=fake_send)
check("те же ошибки второй раз не шлются", not any(k == "server-errors" for k, _ in SENT), str(SENT))
# Ревью Codex: база недоступна, а ошибки сервера всё равно доходят до оператора.
ops_watch.record_server_error(now + timedelta(minutes=6))
_orig_failing = ops_watch.failing_syncs
ops_watch.failing_syncs = lambda db, n: (_ for _ in ()).throw(RuntimeError("database is locked"))
SENT.clear()
try:
    res = ops_watch.run_once(now + timedelta(minutes=7), send=fake_send)
finally:
    ops_watch.failing_syncs = _orig_failing
check("база недоступна: оповещение об ошибках всё равно уходит",
      ("server-errors", "1") in SENT and res["errors"] == 1, f"{SENT} {res}")
ok_resp = client.get("/health/live")
check("обычные запросы не задеты", ok_resp.status_code == 200, str(ok_resp.status_code))

print("\n5. Отчёт оператора")
sys.path.insert(0, str(ROOT / "tools"))
import ops_report  # noqa: E402

buf = io.StringIO()
with redirect_stdout(buf):
    code = ops_report.main(["--db", str(DB_PATH)])
out = buf.getvalue()
check("отчёт выполнился", code == 0, out[-300:])
check("падающая организация помечена с причиной",
      "Бренд Серия Провалов" in out and "СИНК НЕ ПРОХОДИТ" in out
      and "не принял токен" in out, out[-600:])
check("токены в отчёт не попадают", SECRET not in out)
buf = io.StringIO()
with redirect_stdout(buf):
    ops_report.main(["--db", str(DB_PATH), "--json", "--failing"])
rows = json.loads(buf.getvalue())
check("JSON --failing содержит только падающие",
      rows and all(r["sync_failing"] for r in rows), str([r["name"] for r in rows]))
check("в JSON нет токенов", SECRET not in buf.getvalue())

print("\n6. Воспроизведение инцидента 28.09–13.11")
db = SessionLocal()
incident = mk(db, "Бренд Владельца", last_ok=datetime(2026, 9, 28, 3, 0), streak=1146,
              error="МойСклад не принял токен доступа.")
db.commit()
db.close()
ops_watch.reset_for_tests()
SENT.clear()
ops_watch.run_once(datetime(2026, 11, 13, 23, 50), send=fake_send)
check("1 146 провалов без чата организации теперь дают служебное оповещение",
      any(k == "sync-failing" for k, _ in SENT), str(SENT))
with redirect_stdout(io.StringIO()):
    sent, reason = notify.send_ops("sync-failing", "1")
check("без настроенного служебного канала отправка честно отказывает, наружу не ходит",
      sent is False and "OBOROT_OPS_CHAT_ID" in reason, reason)

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()
print(f"\nИТОГО: {OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
