# -*- coding: utf-8 -*-
"""Encrypted off-site backup and a restore drill that boots the application."""
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


def check(name: str, condition: bool, detail: str = "") -> None:
    target = PASS if condition else FAIL
    target.append(name)
    print(f"  {'OK  ' if condition else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ""))


def make_app_db(path: Path) -> None:
    code = f"""
import os
os.environ['DATABASE_URL'] = 'sqlite:///{path}'
os.environ['SCHEDULER_ENABLED'] = '0'
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as client:
    response = client.post('/register', data={{
        'name': 'backup', 'email': 'offsite@test.io',
        'password': 'secret123', 'org_name': 'Offsite test'}})
    assert response.status_code in (200, 303), response.text
"""
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True,
                   capture_output=True, text=True)


FAKE_RESTIC = r'''#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE_RESTIC_LOG"
cmd="$1"; shift
case "$cmd" in
  cat) exit 0 ;;
  backup)
    [ "${FAKE_RESTIC_FAIL:-}" != backup ] || exit 3
    mkdir -p "$FAKE_RESTIC_STORE/latest"
    for arg in "$@"; do
      [ ! -f "$arg" ] || cp "$arg" "$FAKE_RESTIC_STORE/latest/"
    done
    ;;
  forget) ;;
  check) [ "${FAKE_RESTIC_FAIL:-}" != check ] || exit 4 ;;
  restore)
    [ "${FAKE_RESTIC_FAIL:-}" != restore ] || exit 5
    target=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --target ]; then target="$2"; shift 2; else shift; fi
    done
    mkdir -p "$target"
    cp "$FAKE_RESTIC_STORE/latest/oborot.db" "$target/oborot.db"
    cp "$FAKE_RESTIC_STORE/latest/manifest.txt" "$target/manifest.txt"
    ;;
  *) exit 9 ;;
esac
'''


def run(script: Path, env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def value(path: Path) -> str:
    with sqlite3.connect(path) as db:
        return str(db.execute("SELECT name FROM orgs ORDER BY id LIMIT 1").fetchone()[0])


def main() -> int:
    print("\n== Конфигурация расписания ==")
    backup_text = BACKUP.read_text(encoding="utf-8")
    restore_text = RESTORE.read_text(encoding="utf-8")
    check("локальный restic backend запрещён", "repository не off-site" in backup_text)
    check("repository не инициализируется скриптами",
          "restic init" not in backup_text and "restic init" not in restore_text)
    check("restore по умолчанию проверяет все remote data", "check --read-data" in restore_text)
    check("restore по умолчанию запускает приложение", "OBOROT_RESTORE_APP_CHECK:-1" in restore_text)
    timers = [(ROOT / "deploy/systemd/oborot-backup.timer").read_text(),
              (ROOT / "deploy/systemd/oborot-restore-drill.timer").read_text()]
    check("daily и monthly timers переживают выключение машины",
          all("Persistent=true" in item for item in timers))

    with tempfile.TemporaryDirectory(prefix="oborot-offsite-") as raw:
        temp = Path(raw)
        live = temp / "data/oborot.db"
        live.parent.mkdir()
        make_app_db(live)
        state, store = temp / "state", temp / "remote"
        state.mkdir(); store.mkdir()
        password = temp / "password"
        password.write_text("test-password\n", encoding="utf-8")
        fake = temp / "restic"
        fake.write_text(FAKE_RESTIC, encoding="utf-8")
        fake.chmod(0o700)
        log = temp / "restic.log"
        log.write_text("", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "OBOROT_BACKUP_ENV_FILE": str(temp / "missing.env"),
            "OBOROT_DB_PATH": str(live),
            "OBOROT_BACKUP_STATE_DIR": str(state),
            "OBOROT_RESTIC_BIN": str(fake),
            "OBOROT_RESTORE_PYTHON": sys.executable,
            "OBOROT_APP_DIR": str(ROOT),
            "RESTIC_REPOSITORY": "sftp:offsite:/oborot",
            "RESTIC_PASSWORD_FILE": str(password),
            "RESTIC_HOST": "test-production",
            "FAKE_RESTIC_STORE": str(store),
            "FAKE_RESTIC_LOG": str(log),
        })

        print("\n== Согласованный upload и retention ==")
        result = run(BACKUP, env)
        remote = store / "latest/oborot.db"
        check("backup успешно ушёл во внешний repository", result.returncode == 0,
              (result.stdout + result.stderr)[-180:])
        check("remote snapshot читается", remote.is_file() and value(remote) == "Offsite test")
        calls = log.read_text(encoding="utf-8")
        check("retention идёт только после нового snapshot",
              calls.index("backup ") < calls.index("forget "), calls)
        check("success marker создан после проверок", (state / "last-backup-ok").is_file())

        print("\n== Настоящий remote restore и запуск приложения ==")
        with sqlite3.connect(live) as db:
            db.execute("UPDATE orgs SET name='production changed'")
            db.commit()
        result = run(RESTORE, env)
        check("restore drill скачал снимок и поднял приложение", result.returncode == 0,
              (result.stdout + result.stderr)[-300:])
        check("production база не затронута", value(live) == "production changed")
        check("выполнен полный read-data check", "check --read-data" in log.read_text())
        check("restore marker создан только после smoke", (state / "last-restore-ok").is_file())

        output = temp / "verified/oborot.db"
        result = run(RESTORE, env, "latest", str(output))
        check("аварийный restore создаёт отдельный проверенный файл",
              result.returncode == 0 and output.is_file())
        check("существующий output не перезаписывается",
              run(RESTORE, env, "latest", str(output)).returncode != 0)
        check("production нельзя указать как output",
              run(RESTORE, env, "latest", str(live)).returncode != 0)

        print("\n== Ошибки fail closed ==")
        failed = dict(env); failed["FAKE_RESTIC_FAIL"] = "backup"
        log.write_text("", encoding="utf-8")
        check("ошибка upload возвращает failure", run(BACKUP, failed).returncode != 0)
        check("после плохого upload retention не запускается", "forget " not in log.read_text())
        local = dict(env); local["RESTIC_REPOSITORY"] = str(temp / "same-vps")
        check("локальный repository отвергнут", run(BACKUP, local).returncode != 0)
        remote.write_bytes(b"not sqlite")
        old_marker = (state / "last-restore-ok").read_text()
        check("повреждённый remote snapshot отвергнут", run(RESTORE, env).returncode != 0)
        check("ошибка не переписывает restore marker",
              (state / "last-restore-ok").read_text() == old_marker)

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
