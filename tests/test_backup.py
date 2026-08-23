# -*- coding: utf-8 -*-
"""Off-site backup/restore: проверяется результат, а не наличие shell-файлов.

Fake restic играет роль внешнего repository, но SQLite-копию и
``integrity_check`` выполняет настоящий sqlite3. Так тест ловит самые опасные
ошибки: копирование живого WAL через cp, ложный success после неполной загрузки,
retention до нового snapshot, «restore», который ничего не скачивает, и
случайную перезапись production-файла.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "deploy/backup_offsite.sh"
RESTORE = ROOT / "deploy/restore_offsite.sh"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def run(script: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=60)


def db_value(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return str(con.execute("SELECT name FROM orgs WHERE id=1").fetchone()[0])
    finally:
        con.close()


def make_db(path: Path):
    con = sqlite3.connect(path)
    try:
        con.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE users (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL);
            CREATE TABLE products (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL);
            CREATE TABLE sales (id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL);
            INSERT INTO orgs VALUES (1, 'original');
            INSERT INTO users VALUES (1, 1);
            INSERT INTO products VALUES (1, 1);
            INSERT INTO sales VALUES (1, 1);
        """)
        con.commit()
    finally:
        con.close()


FAKE_RESTIC = r'''#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_RESTIC_LOG"
cmd="$1"
shift
case "$cmd" in
  cat)
    exit 0
    ;;
  backup)
    if [ "${FAKE_RESTIC_FAIL:-}" = "backup" ]; then exit 3; fi
    mkdir -p "$FAKE_RESTIC_STORE/latest"
    for value in "$@"; do
      if [ -f "$value" ]; then cp "$value" "$FAKE_RESTIC_STORE/latest/"; fi
    done
    ;;
  forget)
    if [ "${FAKE_RESTIC_FAIL:-}" = "forget" ]; then exit 1; fi
    ;;
  check)
    if [ "${FAKE_RESTIC_FAIL:-}" = "check" ]; then exit 1; fi
    ;;
  restore)
    if [ "${FAKE_RESTIC_FAIL:-}" = "restore" ]; then exit 1; fi
    target=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--target" ]; then target="$2"; shift 2; else shift; fi
    done
    [ -n "$target" ]
    mkdir -p "$target"
    cp "$FAKE_RESTIC_STORE/latest/oborot.db" "$target/oborot.db"
    cp "$FAKE_RESTIC_STORE/latest/manifest.txt" "$target/manifest.txt"
    ;;
  *)
    echo "unexpected command: $cmd" >&2
    exit 2
    ;;
esac
'''


def main() -> int:
    print("\n== Расписание и защитные границы ==")
    backup_text = BACKUP.read_text(encoding="utf-8")
    restore_text = RESTORE.read_text(encoding="utf-8")
    backup_timer = (ROOT / "deploy/systemd/oborot-backup.timer").read_text(encoding="utf-8")
    restore_timer = (ROOT / "deploy/systemd/oborot-restore-drill.timer").read_text(encoding="utf-8")
    backup_service = (ROOT / "deploy/systemd/oborot-backup.service").read_text(encoding="utf-8")
    restore_service = (ROOT / "deploy/systemd/oborot-restore-drill.service").read_text(encoding="utf-8")
    check("repository никогда не инициализируется автоматически",
          " init" not in backup_text and " init" not in restore_text)
    check("ежедневный backup timer переживает выключенную машину",
          "OnCalendar=*-*-*" in backup_timer and "Persistent=true" in backup_timer)
    check("ежемесячный restore drill тоже persistent",
          "OnCalendar=monthly" in restore_timer and "Persistent=true" in restore_timer)
    check("systemd services вызывают разные безопасные сценарии",
          "backup_offsite.sh" in backup_service
          and "restore_offsite.sh" in restore_service)

    with tempfile.TemporaryDirectory(prefix="oborot-backup-test-") as raw:
        temp = Path(raw)
        data = temp / "data"
        state = temp / "state"
        store = temp / "remote-repository"
        data.mkdir()
        state.mkdir()
        store.mkdir()
        live = data / "oborot.db"
        make_db(live)

        password = temp / "restic-password"
        password.write_text("test-only-password\n", encoding="utf-8")
        password.chmod(0o600)
        fake = temp / "restic"
        fake.write_text(FAKE_RESTIC, encoding="utf-8")
        fake.chmod(0o700)
        log = temp / "restic.log"
        log.write_text("", encoding="utf-8")

        env = dict(os.environ)
        env.update({
            "OBOROT_BACKUP_ENV_FILE": str(temp / "absent.env"),
            "OBOROT_DATA_DIR": str(data),
            "OBOROT_DB_PATH": str(live),
            "OBOROT_BACKUP_STATE_DIR": str(state),
            "OBOROT_RESTIC_BIN": str(fake),
            "RESTIC_REPOSITORY": "sftp:other-host:/oborot",
            "RESTIC_PASSWORD_FILE": str(password),
            "RESTIC_HOST": "test-production",
            "FAKE_RESTIC_STORE": str(store),
            "FAKE_RESTIC_LOG": str(log),
        })

        print("\n== Настоящая согласованная SQLite-копия и off-site upload ==")
        result = run(BACKUP, env)
        check("backup завершился успешно", result.returncode == 0,
              (result.stdout + result.stderr)[-300:])
        remote_db = store / "latest/oborot.db"
        check("во внешний repository ушёл снимок базы", remote_db.is_file())
        check("snapshot читается и содержит исходные данные",
              remote_db.is_file() and db_value(remote_db) == "original")
        check("success marker записан только после всех проверок",
              (state / "last-backup-ok").is_file())
        calls = log.read_text(encoding="utf-8")
        check("retention выполняется после upload",
              calls.index("backup ") < calls.index("forget "), calls)
        check("retention хранит daily/weekly/monthly и освобождает место",
              "--keep-daily 14" in calls and "--keep-weekly 8" in calls
              and "--keep-monthly 12" in calls and "--prune" in calls, calls)
        check("repository проверен после prune", calls.rstrip().endswith("check"), calls)
        check("временная копия и lock удалены",
              not list(state.glob("backup.*")) and not (state / "repository.lock").exists())

        print("\n== Реальный restore drill не трогает production ==")
        con = sqlite3.connect(live)
        con.execute("UPDATE orgs SET name='live-after-backup' WHERE id=1")
        con.commit()
        con.close()
        result = run(RESTORE, env)
        check("drill скачал и проверил snapshot", result.returncode == 0,
              (result.stdout + result.stderr)[-300:])
        check("drill сделал полный read-data check",
              "check --read-data" in log.read_text(encoding="utf-8"))
        check("production база осталась нетронутой", db_value(live) == "live-after-backup")
        check("restore marker появился только после проверки",
              (state / "last-restore-ok").is_file())
        check("restore staging и общий lock удалены",
              not list(state.glob("restore.*")) and not (state / "repository.lock").exists())

        print("\n== Аварийное восстановление только в новый файл ==")
        output = temp / "verified/oborot.db"
        result = run(RESTORE, env, "latest", str(output))
        check("проверенная копия выгружена в заданный новый файл",
              result.returncode == 0 and output.is_file(), result.stderr[-200:])
        check("выгружена версия из backup, а не текущая production",
              output.is_file() and db_value(output) == "original")
        result = run(RESTORE, env, "latest", str(output))
        check("существующий output не перезаписывается", result.returncode != 0)
        result = run(RESTORE, env, "latest", str(live))
        check("production DB нельзя указать как output", result.returncode != 0)

        print("\n== Частичная загрузка не становится успешным backup ==")
        old_marker = (state / "last-backup-ok").read_text(encoding="utf-8")
        log.write_text("", encoding="utf-8")
        failed_env = dict(env)
        failed_env["FAKE_RESTIC_FAIL"] = "backup"
        result = run(BACKUP, failed_env)
        calls = log.read_text(encoding="utf-8")
        check("ошибка upload возвращает ненулевой код", result.returncode != 0)
        check("после плохого upload retention не запускается",
              "backup " in calls and "forget " not in calls and "check" not in calls,
              calls)
        check("старый success marker не переписан",
              (state / "last-backup-ok").read_text(encoding="utf-8") == old_marker)
        check("ошибка тоже освобождает staging и lock",
              not list(state.glob("backup.*")) and not (state / "repository.lock").exists())

        print("\n== Локальный путь не может притвориться off-site repository ==")
        local_env = dict(env)
        local_env["RESTIC_REPOSITORY"] = str(temp / "same-vps-repository")
        result = run(BACKUP, local_env)
        check("backup отвергает local backend", result.returncode != 0
              and "не off-site" in result.stderr, result.stderr[-200:])

        print("\n== Повреждённая копия не проходит restore drill ==")
        old_restore = (state / "last-restore-ok").read_text(encoding="utf-8")
        remote_db.write_bytes(b"not a sqlite database")
        result = run(RESTORE, env)
        check("повреждённый SQLite отвергнут", result.returncode != 0,
              (result.stdout + result.stderr)[-200:])
        check("restore success marker не переписан",
              (state / "last-restore-ok").read_text(encoding="utf-8") == old_restore)

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
