# -*- coding: utf-8 -*-
"""Офсайт-бэкап: проверяем не «скрипт написан», а «скрипт не соврёт».

Зачем это набор. deploy/offsite_backup.sh и deploy/offsite_restore_drill.sh
существуют ради одного дня — того, когда VPS больше нет. В обычной жизни оба
всегда завершаются успешно, поэтому проверить их «наблюдением» нельзя: путь,
ради которого они написаны, в обычной жизни не выполняется.

Опаснее всего здесь не падение, а УСПЕХ, которого не должно было быть. Три
таких успеха и проверяются в первую очередь:

  1) хранилище на локальном диске. «Копия есть» — и лежит она рядом с базой,
     на том же диске, который и умрёт;
  2) выгрузка пустой базы. Пустая база проходит integrity_check и любую
     проверку схемы; через две недели ротации в хранилище останутся только
     исправные пустые копии;
  3) ротация после неудачной загрузки. Если `forget --prune` выполнится, когда
     новый снимок НЕ доехал, скрипт своими руками удалит последнюю хорошую
     копию — ровно в тот день, когда что-то пошло не так.

restic в наборе подставной: настоящий требует хранилища, а нам нужно проверить
поведение скрипта, в том числе при отказах, которые на настоящем хранилище по
заказу не воспроизвести. Подставной restic пишет журнал вызовов — по нему
проверяется ПОРЯДОК шагов, а не только их наличие.

Что доказано локально: вся логика скриптов. Чего этот набор НЕ доказывает:
что настоящий restic на настоящем sftp/s3 ведёт себя так же. Это проверяется
только прогоном на сервере, и до него OPS-4 остаётся открытым.

Запуск из корня репозитория:  python tests/test_offsite.py
"""
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BACKUP = ROOT / "deploy" / "offsite_backup.sh"
DRILL = ROOT / "deploy" / "offsite_restore_drill.sh"
WORK = ROOT / f"test_offsite_work_{os.getpid()}"
PORT0 = int(os.environ.get("OFFSITE_TEST_PORT", "8881"))
_ports = iter(range(PORT0, PORT0 + 9))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


# --------------------------------------------------------------------------
# Подставной restic.
#
# Хранилище — каталог $FAKE_REPO. Отказы задаются через FAKE_RESTIC_FAIL:
# список команд через запятую, на которых подставной restic вернёт 1.
# Каждый вызов дописывается в $FAKE_LOG, чтобы проверять порядок шагов.
# --------------------------------------------------------------------------
FAKE_RESTIC = r"""#!/usr/bin/env bash
set -u
CMD="${1:-}"
echo "$*" >> "$FAKE_LOG"
case ",${FAKE_RESTIC_FAIL:-}," in
  *",$CMD,"*) echo "подставной restic: отказ на $CMD" >&2; exit 1;;
esac
case "$CMD" in
  cat)
    [ -f "$FAKE_REPO/config" ] || { echo "repository does not exist" >&2; exit 1; }
    cat "$FAKE_REPO/config"
    ;;
  backup)
    mkdir -p "$FAKE_REPO/snapshot"
    for f in oborot.db manifest.txt; do
      [ -f "$f" ] && cp "$f" "$FAKE_REPO/snapshot/$f"
    done
    echo "snapshot saved"
    ;;
  restore)
    TARGET=""
    while [ $# -gt 0 ]; do
      [ "$1" = "--target" ] && { TARGET="$2"; break; }
      shift
    done
    [ -n "$TARGET" ] || { echo "no target" >&2; exit 1; }
    [ -d "$FAKE_REPO/snapshot" ] || { echo "no snapshot" >&2; exit 1; }
    mkdir -p "$TARGET"
    cp -r "$FAKE_REPO/snapshot/." "$TARGET/"
    echo "restored"
    ;;
  forget|check|init) echo "$CMD ok" ;;
  *) echo "подставной restic не знает команду $CMD" >&2; exit 1 ;;
esac
exit 0
"""


def write_fake_restic(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_RESTIC, encoding="utf-8")
    path.chmod(0o755)


def make_live_db(path: Path) -> None:
    """Боевая база: схему делает само приложение, а не выдуманный SQL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import os\n"
        f"os.environ['DATABASE_URL']='sqlite:///{path}'\n"
        "os.environ['SCHEDULER_ENABLED']='0'\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "with TestClient(app, headers={'X-Oborot-CSRF':'1'}) as c:\n"
        "    c.post('/register', data={'name':'v','email':'off@test.io',\n"
        "        'password':'secret123','org_name':'Офсайт-бренд'})\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), check=True,
                   capture_output=True, text=True)
    # Сводим WAL в основной файл: дальше база копируется как один файл, и без
    # этого копии уезжали бы пустыми — данные остались бы в -wal рядом.
    con = sqlite3.connect(path)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()


def make_empty_db(src: Path, dst: Path) -> None:
    """Та же схема, но без единой организации — исправная пустая база."""
    shutil.copy(src, dst)
    con = sqlite3.connect(dst)
    con.execute("DELETE FROM orgs")
    con.commit()
    con.close()


class Env:
    """Одна изолированная площадка: своя база, своё хранилище, свой журнал."""

    def __init__(self, name: str, repo: str = "sftp:backup-host:/srv/restic/oborot"):
        self.dir = WORK / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = self.dir / "data" / "oborot.db"
        self.state = self.dir / "state"
        self.repo_dir = self.dir / "repo"
        self.repo_dir.mkdir(exist_ok=True)
        self.log = self.dir / "restic.log"
        self.log.write_text("", encoding="utf-8")
        self.password = self.dir / "restic-password"
        self.password.write_text("не настоящий пароль\n", encoding="utf-8")
        self.restic = self.dir / "bin" / "restic"
        write_fake_restic(self.restic)
        self.repo = repo

    def repo_initialized(self, yes: bool = True) -> None:
        cfg = self.repo_dir / "config"
        if yes:
            cfg.write_text("fake restic config\n", encoding="utf-8")
        elif cfg.exists():
            cfg.unlink()

    def env(self, **extra) -> dict:
        e = {
            "OBOROT_BACKUP_ENV_FILE": str(self.dir / "нет-такого-файла.env"),
            "OBOROT_DB": str(self.db),
            "OBOROT_BACKUP_STATE_DIR": str(self.state),
            "OBOROT_RESTIC_BIN": str(self.restic),
            "RESTIC_REPOSITORY": self.repo,
            "RESTIC_PASSWORD_FILE": str(self.password),
            "FAKE_REPO": str(self.repo_dir),
            "FAKE_LOG": str(self.log),
            "OBOROT_DRILL_BOOT_APP": "0",
        }
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run(self, script: Path, args=None, timeout: int = 180, **extra):
        env = dict(os.environ)
        env.update(self.env(**extra))
        p = subprocess.run(["bash", str(script), *(args or [])],
                           capture_output=True, text=True, env=env,
                           cwd=str(ROOT), timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)

    def calls(self) -> list[str]:
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    print("\n== Готовим боевую базу ==")
    seed = WORK / "seed" / "oborot.db"
    make_live_db(seed)
    print(f"   {seed} ({seed.stat().st_size // 1024} КБ)")

    # ---------------------------------------------------------------- 1
    print("\n== 1. Локальный путь вместо хранилища ==")
    e = Env("local-path", repo=str(WORK / "local-path" / "repo"))
    e.db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(seed, e.db)
    e.repo_initialized()
    rc, out = e.run(BACKUP)
    check("локальный путь отвергнут", rc != 0, f"код {rc}")
    check("сказано, почему", "не удалённый" in out, out.strip().splitlines()[-1] if out.strip() else "")
    check("на локальный путь ничего не выгружено", e.calls() == [], f"вызовов: {len(e.calls())}")

    for pref in ("sftp:h:/x", "s3:s3.example.com/b", "b2:bucket:/x", "rclone:remote:x"):
        e2 = Env("scheme-" + pref.split(":")[0], repo=pref)
        shutil.copy(seed, _mk(e2.db))
        e2.repo_initialized()
        rc2, out2 = e2.run(BACKUP)
        check(f"схема {pref.split(':')[0]}: разрешена", rc2 == 0, f"код {rc2}")

    # ---------------------------------------------------------------- 2
    print("\n== 2. Хранилище недоступно ==")
    e = Env("no-repo")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized(False)
    rc, out = e.run(BACKUP)
    check("отказ, когда репозиторий не создан", rc != 0, f"код {rc}")
    check("предложено restic init", "restic init" in out)
    check("выгрузки не было", not any(c.startswith("backup") for c in e.calls()))
    check("ротации не было", not any(c.startswith("forget") for c in e.calls()))

    # ---------------------------------------------------------------- 3
    print("\n== 3. Успешная выгрузка ==")
    e = Env("happy")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP)
    check("успех", rc == 0, out.strip().splitlines()[-1] if out.strip() else f"код {rc}")
    calls = e.calls()
    order = [c.split()[0] for c in calls]
    check("порядок: cat → backup → forget → check",
          order == ["cat", "backup", "forget", "check"], " → ".join(order))
    check("снимок помечен тегом", any("--tag oborot-db" in c for c in calls))
    check("ротация с --prune", any("--prune" in c for c in calls))
    stamp = e.state / "last-offsite-backup"
    check("отметка об успехе записана", stamp.exists(),
          stamp.read_text(encoding="utf-8").strip() if stamp.exists() else "нет файла")
    check("в отметке есть число организаций", "orgs=1" in stamp.read_text(encoding="utf-8"))
    man = e.repo_dir / "snapshot" / "manifest.txt"
    check("манифест доехал", man.exists())
    check("в манифесте строки, а не только таблицы",
          "orgs=1" in man.read_text(encoding="utf-8"),
          man.read_text(encoding="utf-8").replace("\n", " ") if man.exists() else "")
    leftovers = [p for p in e.state.glob("offsite.*") if p.is_dir()]
    check("рабочий каталог убран за собой", not leftovers, str(leftovers))

    # ---------------------------------------------------------------- 4
    print("\n== 4. Пустая база не уезжает в хранилище ==")
    e = Env("empty")
    _mk(e.db)
    make_empty_db(seed, e.db)
    e.repo_initialized()
    rc, out = e.run(BACKUP)
    check("отказ на базе без организаций", rc != 0, f"код {rc}")
    check("сказано, чем это опасно", "нет организаций" in out)
    check("пустая база не выгружена", not any(c.startswith("backup") for c in e.calls()))
    check("ротация не запускалась", not any(c.startswith("forget") for c in e.calls()))

    # ---------------------------------------------------------------- 5
    print("\n== 5. Битая база не уезжает в хранилище ==")
    e = Env("corrupt")
    shutil.copy(seed, _mk(e.db))
    with open(e.db, "r+b") as f:
        f.seek(8192)
        f.write(b"\xde\xad\xbe\xef" * 400)
    e.repo_initialized()
    rc, out = e.run(BACKUP)
    check("отказ на битой базе", rc != 0, f"код {rc}")
    # Не только код возврата: на битой базе sqlite3 выходит с ненулевым кодом, и
    # без `|| true` set -e убил бы скрипт прямо на присваивании — падение было бы
    # тем же, а причина исчезла бы. Проверка «названа причина» ловит именно это.
    check("названа причина, а не молчаливая смерть", "копия битая" in out,
          out.strip().splitlines()[-1] if out.strip() else "пусто")
    check("выгрузки не было", not any(c.startswith("backup") for c in e.calls()))

    # ---------------------------------------------------------------- 6
    print("\n== 6. Загрузка не удалась — старое НЕ удаляем ==")
    e = Env("upload-failed")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP, FAKE_RESTIC_FAIL="backup")
    check("скрипт упал", rc != 0, f"код {rc}")
    check("сказано, что старое цело", "не тронуты" in out.lower() or "НЕ тронуты" in out)
    check("forget/prune НЕ выполнялся",
          not any(c.startswith("forget") for c in e.calls()),
          " | ".join(e.calls()))
    check("отметки об успехе нет", not (e.state / "last-offsite-backup").exists())

    # ---------------------------------------------------------------- 7
    print("\n== 7. Блокировка от одновременного запуска ==")
    e = Env("lock")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.state.mkdir(parents=True, exist_ok=True)
    lock = e.state / "offsite.lock"
    lock.touch()
    holder = subprocess.Popen(["flock", str(lock), "sleep", "20"],
                              start_new_session=True)
    try:
        rc, out = e.run(BACKUP, timeout=60)
        check("второй запуск отказался", rc != 0, f"код {rc}")
        check("сказано про блокировку", "уже выполняется" in out)
    finally:
        os.killpg(os.getpgid(holder.pid), 15)
        holder.wait(timeout=10)

    # ---------------------------------------------------------------- 8
    print("\n== 8. Учение: полный проход ==")
    e = Env("drill-ok")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP)
    check("подготовка: копия загружена", rc == 0, f"код {rc}")
    e.log.write_text("", encoding="utf-8")
    rc, out = e.run(DRILL)
    check("учение пройдено", rc == 0, out.strip().splitlines()[-1] if out.strip() else f"код {rc}")
    order = [c.split()[0] for c in e.calls()]
    check("порядок: cat → check → restore", order == ["cat", "check", "restore"], " → ".join(order))
    check("проверка читает сами данные", any("--read-data" in c for c in e.calls()),
          " | ".join(e.calls()))
    drill_stamp = e.state / "last-offsite-drill"
    check("отметка об учении записана", drill_stamp.exists())

    # ---------------------------------------------------------------- 9
    print("\n== 9. Учение: восстановилась пустая база ==")
    e = Env("drill-empty")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, _ = e.run(BACKUP)
    check("подготовка: копия загружена", rc == 0, f"код {rc}")
    # Подменяем содержимое хранилища: приехала исправная, но пустая база.
    make_empty_db(seed, e.repo_dir / "snapshot" / "oborot.db")
    rc, out = e.run(DRILL)
    check("учение провалено на пустой копии", rc != 0, f"код {rc}")
    check("названа настоящая причина", "нет организаций" in out,
          out.strip().splitlines()[-1] if out.strip() else "")

    # --------------------------------------------------------------- 10
    print("\n== 10. Учение: в снимке нет базы ==")
    e = Env("drill-no-db")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.run(BACKUP)
    (e.repo_dir / "snapshot" / "oborot.db").unlink()
    rc, out = e.run(DRILL)
    check("учение провалено", rc != 0, f"код {rc}")
    check("сказано, чего не хватает", "нет oborot.db" in out)

    # --------------------------------------------------------------- 11
    print("\n== 11. Учение не заменяет боевую базу ==")
    e = Env("drill-prod")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.run(BACKUP)
    before = e.db.read_bytes()
    rc, out = e.run(DRILL, args=["latest", str(e.db)])
    check("отказ писать поверх боевой базы", rc != 0, f"код {rc}")
    check("сказано прямо", "не заменяется" in out)
    check("боевая база не изменилась", e.db.read_bytes() == before)

    exists = WORK / "drill-prod" / "уже-есть.db"
    exists.write_text("занято", encoding="utf-8")
    rc, out = e.run(DRILL, args=["latest", str(exists)])
    check("отказ писать поверх существующего файла", rc != 0, f"код {rc}")
    check("файл не перезаписан", exists.read_text(encoding="utf-8") == "занято")

    out_path = WORK / "drill-prod" / "восстановленная.db"
    rc, out = e.run(DRILL, args=["latest", str(out_path)])
    check("в новый файл класть можно", rc == 0 and out_path.exists(), f"код {rc}")

    # Отдельный случай: боевой базы ещё НЕТ (свежая машина, разворачиваемся из
    # копии). Тогда проверка «файл уже существует» ничего не ловит, и от
    # создания боевой базы мимо человека спасает только запрет на её путь.
    e2 = Env("drill-prod-missing")
    shutil.copy(seed, _mk(e2.db))
    e2.repo_initialized()
    e2.run(BACKUP)
    e2.db.unlink()
    rc, out = e2.run(DRILL, args=["latest", str(e2.db)])
    check("боевой путь запрещён, даже когда файла там нет", rc != 0, f"код {rc}")
    check("боевая база не создана учением", not e2.db.exists())

    # --------------------------------------------------------------- 12
    print("\n== 12. Учение на локальном пути отвергается ==")
    e = Env("drill-local", repo=str(WORK / "drill-local" / "repo"))
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(DRILL)
    check("учение на локальном каталоге отвергнуто", rc != 0, f"код {rc}")
    check("сказано, что оно ничего не доказывает", "диск ещё жив" in out)

    # --------------------------------------------------------------- 13
    print("\n== 13. Учение поднимает приложение на восстановленной базе ==")
    e = Env("drill-boot")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.run(BACKUP)
    venv = WORK / "drill-boot" / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    rc, out = e.run(DRILL, timeout=300, OBOROT_DRILL_BOOT_APP="1",
                    OBOROT_VENV=str(venv), RESTORE_PORT=next(_ports))
    check("приложение поднялось на восстановленной базе", rc == 0,
          out.strip().splitlines()[-1] if out.strip() else f"код {rc}")
    check("проверка запуском действительно выполнялась",
          "ВОССТАНОВЛЕНИЕ ПРОВЕРЕНО" in out)

    # --------------------------------------------------------------- 14
    print("\n== 14. Нет пароля — нет работы ==")
    e = Env("no-password")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP, RESTIC_PASSWORD_FILE=str(e.dir / "нет-пароля"))
    check("отказ без файла пароля", rc != 0, f"код {rc}")
    check("пароль нигде не напечатан", "не настоящий пароль" not in out)

    print("\n" + "=" * 62)
    print(f"OK: {len(PASS)}   FAIL: {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  FAIL {f}")
    shutil.rmtree(WORK, ignore_errors=True)
    return 1 if FAIL else 0


def _mk(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


if __name__ == "__main__":
    sys.exit(main())
