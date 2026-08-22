# -*- coding: utf-8 -*-
"""Логи: они вообще пишутся, и по ним видно, у КАКОГО клиента что произошло.

Зачем этот набор существует. Логгеры в проекте были аккуратные, а конфигурации
логирования не было ни одной строчки. Прод стартует через `uvicorn.run(...)`,
а uvicorn настраивает ТОЛЬКО свои логгеры и не трогает корневой: у корневого
нет обработчиков, уровень по умолчанию WARNING. Из этого следовало ровно две
вещи, и обе проверяемы:

  • каждый `log.info(...)` в приложении уходил в никуда — «планировщик
    запущен», «ежедневный синк: N организаций», «продолжение первичной
    загрузки запущено» не существовали для эксплуатации;
  • `log.warning`/`log.exception` печатались аварийным `logging.lastResort` —
    голой строкой без времени, уровня и имени модуля.

Инцидент 03–21.08 (синк восемнадцать дней падал на протухшем токене) и не мог
быть замечен по логам. Поэтому здесь проверяется не «красиво ли отформатировано»,
а три вещи, каждая из которых стоила бы отдельного инцидента:

  1) НАСТРОЙКА ПРИМЕНЕНА — у корневого логгера есть обработчик, уровень
     позволяет INFO, а запись содержит время, уровень, имя логгера и организацию.
  2) МЕТКА ОРГАНИЗАЦИИ — записи веб-запроса помечены организацией того, кто
     этот запрос сделал; у неавторизованных запросов стоит «-».
  3) МЕТКА НЕ ПРОТЕКАЕТ МЕЖДУ АРЕНДАТОРАМИ — ни между запросами, ни между
     потоками. Это то же свойство, что и изоляция данных, только для логов:
     разбор инцидента по чужой метке хуже, чем отсутствие метки.

Запуск из корня репозитория:  python tests/test_logging.py
"""
import logging
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_logging.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.pop("OBOROT_LOG_LEVEL", None)

if DB_PATH.exists():
    DB_PATH.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from app import logging_conf  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


class Capture(logging.Handler):
    """Ловит записи так же, как их увидит боевой обработчик: уже отформатированными."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.lines: list[str] = []
        self.addFilter(logging_conf.OrgFilter())
        self.setFormatter(logging.Formatter(logging_conf.FORMAT, logging_conf.DATEFMT))

    def emit(self, record):
        self.records.append(record)
        self.lines.append(self.format(record))

    def reset(self):
        self.records.clear()
        self.lines.clear()


cap = Capture()
logging.getLogger().addHandler(cap)

# Тестовый маршрут: нужен код, который пишет в лог ВНУТРИ обработки запроса.
# Своих таких маршрутов у приложения немного, и все они завязаны на внешние
# системы; проще добавить пустой — он живёт только в этом процессе.
_route_log = logging.getLogger("oborot.testroute")


@oborot_app.get("/__test_log", include_in_schema=False)
def _test_log_route():
    _route_log.info("запись из обработчика запроса")
    return {"ok": True}


client = TestClient(oborot_app)
# Вход в контекст запускает события startup приложения (создание таблиц и
# аддитивные миграции) — без этого первый же /register упал бы на
# «no such table: users».
client.__enter__()


def register(email: str, org_name: str):
    return client.post("/register", data={
        "name": email.split("@")[0], "email": email,
        "password": "secret123", "org_name": org_name,
    }, follow_redirects=False)


def login(email: str):
    return client.post("/login", data={"email": email, "password": "secret123"},
                       follow_redirects=False)


def org_of(line: str) -> str:
    """Достаёт значение org= из отформатированной строки."""
    for part in line.split():
        if part.startswith("org="):
            return part[4:]
    return ""


# ─────────────────────────────────────────────────────────────────────────────
print("\n1. Конфигурация логирования применена")

root = logging.getLogger()
check("у корневого логгера есть обработчик",
      any(not isinstance(h, Capture) for h in root.handlers),
      f"обработчиков: {len(root.handlers)}")
check("уровень корневого логгера пропускает INFO",
      root.getEffectiveLevel() <= logging.INFO,
      logging.getLevelName(root.getEffectiveLevel()))

cap.reset()
logging.getLogger("oborot.scheduler").info("ежедневный синк: %d организаций", 3)
check("INFO из модуля приложения доходит до обработчика (регрессия: раньше — нет)",
      len(cap.records) == 1)
line = cap.lines[0] if cap.lines else ""
check("в строке есть уровень", "INFO" in line, line)
check("в строке есть имя логгера", "oborot.scheduler" in line, line)
check("в строке есть время", line[:4].isdigit() and line[4] == "-", line[:20])
check("в строке есть метка организации", "org=" in line, line)
check("вне запроса метка пустая («-»), а не чужая", org_of(line) == "-", line)

cap.reset()
logging.getLogger("oborot.auth").warning("слишком много попыток входа")
check("WARNING форматируется так же, а не через lastResort",
      len(cap.records) == 1 and "WARNING" in cap.lines[0], cap.lines[:1])

check("повторный вызов setup_logging ничего не ломает",
      (logging_conf.setup_logging() or True) and root.getEffectiveLevel() <= logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
print("\n2. Метка организации в веб-запросе")

r = register("a@example.com", "Организация A")
check("регистрация A прошла", r.status_code in (200, 302, 303), str(r.status_code))
r = register("b@example.com", "Организация B")
check("регистрация B прошла", r.status_code in (200, 302, 303), str(r.status_code))

import sqlite3  # noqa: E402
con = sqlite3.connect(DB_PATH)
orgs = dict(con.execute("SELECT name, id FROM orgs").fetchall())
con.close()
org_a, org_b = orgs.get("Организация A"), orgs.get("Организация B")
check("обе организации заведены", bool(org_a and org_b and org_a != org_b), str(orgs))

login("a@example.com")
cap.reset()
r = client.get("/__test_log")
check("запрос выполнен", r.status_code == 200, str(r.status_code))
mine = [ln for ln in cap.lines if "oborot.testroute" in ln]
check("запись из обработчика поймана", len(mine) == 1, str(cap.lines))
check("запись помечена организацией автора запроса",
      bool(mine) and org_of(mine[0]) == str(org_a),
      f"ожидали org={org_a}, получили «{org_of(mine[0]) if mine else ''}»")

login("b@example.com")
cap.reset()
client.get("/__test_log")
mine = [ln for ln in cap.lines if "oborot.testroute" in ln]
check("следующий запрос помечен ДРУГОЙ организацией (метка не залипает)",
      bool(mine) and org_of(mine[0]) == str(org_b),
      f"ожидали org={org_b}, получили «{org_of(mine[0]) if mine else ''}»")

client.post("/logout", follow_redirects=False)
cap.reset()
client.get("/__test_log")
mine = [ln for ln in cap.lines if "oborot.testroute" in ln]
check("после выхода метка снова пустая, а не прежней организации",
      bool(mine) and org_of(mine[0]) == "-",
      f"получили «{org_of(mine[0]) if mine else ''}»")

cap.reset()
logging.getLogger("oborot.scheduler").info("служебная запись после запросов")
check("метка запроса не протекла в контекст процесса",
      org_of(cap.lines[0]) == "-", cap.lines[:1])

# ─────────────────────────────────────────────────────────────────────────────
print("\n3. Метка не протекает между потоками и восстанавливается")

logging_conf.set_org(41)
cap.reset()
logging.getLogger("oborot.ms_sync").info("синк идёт")
check("set_org помечает последующие записи", org_of(cap.lines[0]) == "41", cap.lines[:1])

with logging_conf.use_org(42):
    cap.reset()
    logging.getLogger("oborot.ms_sync").info("вложенная работа")
    check("use_org подменяет метку", org_of(cap.lines[0]) == "42", cap.lines[:1])
cap.reset()
logging.getLogger("oborot.ms_sync").info("после блока")
check("use_org возвращает прежнюю метку", org_of(cap.lines[0]) == "41", cap.lines[:1])

seen = []


def _in_thread():
    # Новый поток НЕ наследует контекст: чужая метка сюда попасть не должна.
    cap.reset()
    logging.getLogger("oborot.ms_sync").info("фоновый поток без метки")
    seen.append(org_of(cap.lines[0]) if cap.lines else "?")
    logging_conf.set_org(77)
    cap.reset()
    logging.getLogger("oborot.ms_sync").info("фоновый поток со своей меткой")
    seen.append(org_of(cap.lines[0]) if cap.lines else "?")


t = threading.Thread(target=_in_thread)
t.start()
t.join(timeout=10)
check("новый поток стартует без чужой метки", seen[:1] == ["-"], str(seen))
check("поток ставит свою метку", seen[1:2] == ["77"], str(seen))

cap.reset()
logging.getLogger("oborot.ms_sync").info("в главном контексте")
check("метка потока не протекла обратно", org_of(cap.lines[0]) == "41", cap.lines[:1])
logging_conf.set_org(None)

# ─────────────────────────────────────────────────────────────────────────────
print("\n4. Шумные библиотеки приглушены")

for noisy in ("httpx", "sqlalchemy.engine", "apscheduler"):
    check(f"{noisy}: INFO не попадает в лог",
          logging.getLogger(noisy).getEffectiveLevel() > logging.INFO,
          logging.getLevelName(logging.getLogger(noisy).getEffectiveLevel()))

print(f"\n{'=' * 60}\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
for f in FAIL:
    print("  FAIL:", f)
if DB_PATH.exists():
    try:
        DB_PATH.unlink()
    except OSError:
        pass
sys.exit(1 if FAIL else 0)
