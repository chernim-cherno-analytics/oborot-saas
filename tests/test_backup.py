# -*- coding: utf-8 -*-
"""Резервное копирование: проверяем не «скрипт написан», а «скрипт спасает».

Зачем это тест. deploy/backup.sh и deploy/restore_test.sh — единственное, что
стоит между «база повреждена» и «данные клиентов потеряны». Их особенность в
том, что в обычной жизни они всегда отрабатывают успешно, а нужны ровно в тот
день, когда что-то пошло не так. То есть путь, ради которого они существуют,
никогда не выполняется — пока не станет поздно.

Ручная проверка 23.08 нашла в них ошибку именно такого рода: на повреждённой
базе `sqlite3 PRAGMA integrity_check` выходит с ненулевым кодом, `set -e` убивал
скрипт прямо на присваивании, и обработчик «КОПИЯ БИТАЯ → удалить» не выполнялся
никогда. Битая копия оставалась на диске, попадала в ротацию и становилась той
самой «последней копией», которую взял бы restore_test. Ошибка не видна ни
глазами, ни `bash -n`: скрипт синтаксически безупречен и на здоровой базе
работает идеально.

Отсюда правило набора: каждый аварийный путь проверяется исполнением.

Проверяется:
  1) копия снимается с базы в режиме WAL и содержит данные, которых нет в
     основном файле (обычный cp дал бы пустой снимок);
  2) повреждённая база → выход 1, внятное сообщение, и в каталоге НЕ ОСТАЁТСЯ
     ни .db, ни -wal, ни -shm;
  3) база без организаций не попадает в ротацию (защита от «сняли копию не с
     того файла»);
  4) ротация действительно удаляет лишнее и оставляет ровно RETAIN штук;
  5) отсутствие sqlite3 объясняется словами, а не кодом 127;
  6) пустой каталог копий → «НЕТ КОПИИ», а не молчаливый выход;
  7) битый архив → внятный отказ;
  8) восстановление: копия разворачивается, приложение на ней стартует и
     отвечает "db":true;
  9) восстановление копии со СТАРОЙ схемой — реальный сценарий аварии, когда
     разворачивают бэкап трёхнедельной давности; приложение должно догнать
     схему миграциями и подняться;
 10) если приложение не поднялось, скрипт не ждёт сорок секунд впустую.

Запуск из корня репозитория:  python tests/test_backup.py
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BACKUP = ROOT / "deploy" / "backup.sh"
RESTORE = ROOT / "deploy" / "restore_test.sh"
# Каталог и порты — свои у каждого процесса. Набор поднимает приложение три
# раза подряд; на одном порту третий запуск успевал получить "db":true от ещё
# не умершего процесса предыдущего — и набор падал раз в несколько прогонов,
# причём всегда «где-то в другом месте».
WORK = ROOT / f"test_backup_work_{os.getpid()}"
PORT0 = int(os.environ.get("BACKUP_TEST_PORT", "8791"))
_port_seq = iter(range(PORT0, PORT0 + 9))


def next_port() -> str:
    return str(next(_port_seq))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def sh(script: Path, env_extra: dict, args: list[str] | None = None,
       timeout: int = 180) -> tuple[int, str]:
    env = dict(os.environ)
    env.update({k: str(v) for k, v in env_extra.items()})
    p = subprocess.run(
        ["bash", str(script), *(args or [])],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=timeout,
    )
    return p.returncode, (p.stdout + p.stderr)


def make_live_db(path: Path) -> None:
    """Боевая база: WAL, есть организация. Поднимаем приложение — так схема
    получается ровно такой, какая бывает в бою, а не выдуманной руками."""
    path.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import os\n"
        f"os.environ['DATABASE_URL']='sqlite:///{path}'\n"
        "os.environ['SCHEDULER_ENABLED']='0'\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "with TestClient(app, headers={'X-Oborot-CSRF':'1'}) as c:\n"
        "    c.post('/register', data={'name':'v','email':'bk@test.io',\n"
        "        'password':'secret123','org_name':'Бэкап-бренд'})\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), check=True,
                   capture_output=True, text=True)


def make_old_schema_db(path: Path) -> None:
    """База прежней схемы — то, чем окажется бэкап трёхнедельной давности."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE orgs (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL,
                         plan VARCHAR(32) NOT NULL DEFAULT 'trial',
                         settings_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE productions (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL,
                         name VARCHAR(120) NOT NULL,
                         is_main BOOLEAN NOT NULL DEFAULT 0);
      INSERT INTO orgs (name) VALUES ('Бренд из прошлого месяца');
      INSERT INTO productions (org_id, name, is_main) VALUES (1, 'Цех', 1);
    """)
    con.commit()
    con.close()


def corrupt(path: Path) -> None:
    with open(path, "r+b") as f:
        f.seek(8192)
        f.write(b"\xde\xad\xbe\xef" * 400)


def ls_dir(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def main() -> int:
    if shutil.which("sqlite3") is None:
        # Код 77 И причина одновременно (D-42). «ИТОГО: 0 OK, 0 FAIL» при
        # нулевом коде возврата отсюда убрано: ровно эта пара и делала
        # непроверенный бэкап зелёной строкой в CI.
        print("ПРОПУЩЕНО: в системе нет клиента sqlite3 — "
              "скрипты бэкапа проверять нечем")
        return 77

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()

    live = WORK / "live" / "oborot.db"
    live_bk = WORK / "live" / "backups"
    make_live_db(live)

    print("\n== Снимок работающей базы ==")
    # Данные лежат в -wal, основной файл почти пуст: именно здесь обычное
    # копирование файла дало бы снимок без единой организации.
    wal = live.with_name(live.name + "-wal")
    check("база в режиме WAL, данные ещё не слиты в основной файл",
          wal.exists() and wal.stat().st_size > live.stat().st_size,
          f"db={live.stat().st_size} wal={wal.stat().st_size if wal.exists() else 0}")

    rc, out = sh(BACKUP, {"OBOROT_DB": live, "BACKUP_DIR": live_bk})
    check("копия снята", rc == 0, out[-200:])
    gz = [n for n in ls_dir(live_bk) if n.endswith(".db.gz")]
    check("в каталоге ровно один архив", len(gz) == 1, str(ls_dir(live_bk)))
    check("копия видит организацию из -wal", "orgs: 1" in out,
          [l for l in out.splitlines() if "orgs" in l][:1])

    print("\n== Повреждённая база ==")
    bad = WORK / "bad" / "oborot.db"
    bad_bk = WORK / "bad" / "backups"
    bad.parent.mkdir(parents=True)
    shutil.copy(live, bad)
    subprocess.run(["sqlite3", str(bad), "VACUUM;"], capture_output=True)
    corrupt(bad)
    rc, out = sh(BACKUP, {"OBOROT_DB": bad, "BACKUP_DIR": bad_bk})
    check("выход 1, а не код sqlite3", rc == 1, f"rc={rc}")
    check("сказано, что копия битая", "БИТАЯ" in out, out[-200:])
    # Главное: обломок не должен пережить проверку. Иначе он попадёт в ротацию
    # и однажды окажется «последней копией».
    check("битая копия удалена целиком (нет ни .db, ни -wal, ни -shm)",
          ls_dir(bad_bk) == [], str(ls_dir(bad_bk)))

    print("\n== База без организаций ==")
    empty = WORK / "empty" / "oborot.db"
    empty_bk = WORK / "empty" / "backups"
    empty.parent.mkdir(parents=True)
    sqlite3.connect(empty).executescript(
        "CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT);")
    rc, out = sh(BACKUP, {"OBOROT_DB": empty, "BACKUP_DIR": empty_bk})
    check("пустая база отвергнута", rc == 1, f"rc={rc}")
    check("объяснено, почему", "НЕТ ОРГАНИЗАЦИЙ" in out, out[-160:])
    check("в ротацию ничего не легло", ls_dir(empty_bk) == [], str(ls_dir(empty_bk)))

    print("\n== Ротация ==")
    rot = WORK / "rot"
    for _ in range(4):
        rc, out = sh(BACKUP, {"OBOROT_DB": live, "BACKUP_DIR": rot, "BACKUP_RETAIN": 2})
        if rc != 0:
            break
        time.sleep(1.05)   # имя копии — с точностью до секунды
    check("после четырёх запусков осталось ровно 2", len(ls_dir(rot)) == 2,
          str(ls_dir(rot)))

    print("\n== Нет клиента sqlite3 ==")
    nosql = WORK / "nosql-bin"
    nosql.mkdir()
    for b in ("bash", "date", "mkdir", "stat", "gzip", "gunzip", "ls", "xargs",
              "rm", "cat", "tail", "head", "wc", "scp", "curl", "seq", "mktemp",
              "sleep", "dirname", "grep", "kill", "tr", "sed"):
        src = shutil.which(b)
        if src:
            (nosql / b).symlink_to(src)
    rc, out = sh(BACKUP, {"OBOROT_DB": live, "BACKUP_DIR": WORK / "x", "PATH": nosql})
    check("отсутствие sqlite3 объяснено словами", rc == 1 and "НЕТ sqlite3" in out,
          f"rc={rc} {out[-120:]}")

    print("\n== Восстановление: аварийные пути ==")
    rc, out = sh(RESTORE, {"BACKUP_DIR": WORK / "nothing-here"})
    check("пустой каталог копий → внятный отказ, а не молчание",
          rc == 1 and "НЕТ КОПИИ" in out, f"rc={rc} {out[-120:]}")

    badgz = WORK / "badgz"
    badgz.mkdir()
    (badgz / "oborot-20260101-000000.db.gz").write_text("это не архив")
    rc, out = sh(RESTORE, {"BACKUP_DIR": badgz})
    check("битый архив → внятный отказ",
          rc == 1 and "НЕ РАСПАКОВЫВАЕТСЯ" in out, f"rc={rc} {out[-120:]}")

    t0 = time.monotonic()
    rc, out = sh(RESTORE, {"BACKUP_DIR": live_bk, "OBOROT_VENV": WORK / "no-venv",
                           "RESTORE_PORT": next_port()})
    dt = time.monotonic() - t0
    check("приложение не поднялось → выход 1 с логом",
          rc == 1 and "НЕ ПОДНЯЛОСЬ" in out, f"rc={rc}")
    check("падение замечено сразу, а не через сорок секунд ожидания", dt < 15,
          f"{dt:.1f} с")

    print("\n== Восстановление: рабочий путь ==")
    venv = WORK / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    rc, out = sh(RESTORE, {"BACKUP_DIR": live_bk, "OBOROT_VENV": venv,
                           "RESTORE_PORT": next_port()})
    check("копия развёрнута и приложение на ней стартовало", rc == 0, out[-300:])
    check("проверка дошла до живой базы", '"db":true' in out, out[-160:])
    check("названо, сколько организаций восстановлено", "организаций 1" in out,
          [l for l in out.splitlines() if "организаций" in l][:1])

    print("\n== Восстановление копии со старой схемой ==")
    # Настоящая авария выглядит так: разворачивают бэкап трёхнедельной
    # давности, а схема за это время уехала. Если приложение на нём не
    # поднимется, бэкап бесполезен ровно тогда, когда нужен.
    old = WORK / "old" / "oborot.db"
    old_bk = WORK / "old" / "backups"
    make_old_schema_db(old)
    rc, out = sh(BACKUP, {"OBOROT_DB": old, "BACKUP_DIR": old_bk})
    check("копия старой базы снимается", rc == 0, out[-200:])
    rc, out = sh(RESTORE, {"BACKUP_DIR": old_bk, "OBOROT_VENV": venv,
                           "RESTORE_PORT": next_port()})
    check("приложение догоняет схему миграциями и поднимается",
          rc == 0 and '"db":true' in out, out[-300:])

    shutil.rmtree(WORK, ignore_errors=True)
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
