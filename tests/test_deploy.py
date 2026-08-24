# -*- coding: utf-8 -*-
"""Скрипт выкладки: проверяем аварийные ветки, а не «скрипт написан».

Зачем это тест. `deploy/deploy.sh` — единственное, что стоит между «код в
репозитории» и «код в бою». Его особенность та же, что у скриптов бэкапа: в
обычной жизни он отрабатывает успешно, а нужен правильным ровно в тот день,
когда что-то не так. Проверка чтением тут уже подводила: в скриптах бэкапа
`bash -n` и вычитка глазами пропустили три экземпляра одной ошибки.

Часть проверяемого поведения перенесена из PR #3 (codex/ops-3-deploy-rollback):
блокировка параллельных выкладок, установка зависимостей до переключения кода,
атомарная запись env, отказ на грязной копии и на коммите вне истории main.
Реализация и эти тесты — свои.

Проверяется на НАСТОЯЩЕМ git-репозитории с подставными `systemctl`, `curl`,
`pip` и `journalctl` (боевой сервер не нужен и не трогается):
  1) успешная выкладка: код переключён, OBOROT_COMMIT записан, PREVIOUS_SHA
     сохранён;
  2) вторая выкладка при занятом локе не начинается;
  3) грязная рабочая копия — отказ до любых действий;
  4) коммит вне истории origin/main — отказ;
  5) коммит без requirements.lock — отказ (fail-closed), и он снимается явным
     OBOROT_ALLOW_NO_LOCK=1;
  6) падение pip НЕ переключает код: прод остаётся на прежнем коммите;
  7) приложение не поднялось — выход 1, логи, команда отката, имя копии базы;
  8) копия базы снимается и проверяется до переключения кода.

Запуск из корня репозитория:  python tests/test_deploy.py
"""
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / f"test_deploy_work_{os.getpid()}"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def run(cmd, cwd=None, env=None, timeout=120):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                       shell=isinstance(cmd, str), timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def git(args, cwd, **kw):
    return run(["git", *args], cwd=cwd, **kw)


def make_repo() -> Path:
    """Настоящий репозиторий: origin (bare) + рабочая копия, как на сервере."""
    origin = WORK / "origin.git"
    app = WORK / "app-src"
    origin.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "--bare", "-b", "main", str(origin)])
    run(["git", "init", "-b", "main", str(app)])
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git(["config", k, v], cwd=app)
    (app / "requirements.lock").write_text("httpx==0.28.1\n", encoding="utf-8")
    (app / "app.py").write_text("v1\n", encoding="utf-8")
    git(["add", "-A"], cwd=app)
    git(["commit", "-qm", "v1"], cwd=app)
    git(["remote", "add", "origin", str(origin)], cwd=app)
    git(["push", "-q", "-u", "origin", "main"], cwd=app)
    return app


def add_commit(app: Path, text: str, lock: bool = True) -> str:
    (app / "app.py").write_text(text + "\n", encoding="utf-8")
    if lock:
        (app / "requirements.lock").write_text("httpx==0.28.1\n", encoding="utf-8")
    elif (app / "requirements.lock").exists():
        (app / "requirements.lock").unlink()
        (app / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    git(["add", "-A"], cwd=app)
    git(["commit", "-qm", text], cwd=app)
    git(["push", "-q", "origin", "main"], cwd=app)
    return git(["rev-parse", "HEAD"], cwd=app)[1].strip()


def make_stubs(bindir: Path, *, health_ok=True, pip_ok=True) -> None:
    """Подставные системные команды. Настоящий сервер не нужен."""
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bindir / "journalctl").write_text("#!/bin/sh\necho '(логи сервиса)'\n",
                                       encoding="utf-8")
    body = '{"status":"ok","db":true}' if health_ok else '{"status":"not ready"}'
    code = "0" if health_ok else "1"
    (bindir / "curl").write_text(
        f"#!/bin/sh\nprintf '%s' '{body}'\nexit {code}\n", encoding="utf-8")
    for f in ("systemctl", "journalctl", "curl"):
        (bindir / f).chmod(0o755)
    venv_bin = bindir.parent / "venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    (venv_bin / "pip").write_text(
        "#!/bin/sh\n" + ("exit 0\n" if pip_ok else "echo 'pip упал' >&2\nexit 1\n"),
        encoding="utf-8")
    (venv_bin / "pip").chmod(0o755)


def deploy_env(app: Path, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    stub = WORK / "stub-bin"
    env["PATH"] = f"{stub}:{env['PATH']}"
    env.update({
        "OBOROT_APP_DIR": str(app),
        "OBOROT_VENV": str(WORK / "venv"),
        "OBOROT_DATA_DIR": str(WORK / "data"),
        "OBOROT_ENV_FILE": str(WORK / "env"),
        "OBOROT_STATE_DIR": str(WORK / "state"),
        "OBOROT_HEALTH_ATTEMPTS": "2",
        "OBOROT_HEALTH_DELAY": "0",
    })
    env.update(extra or {})
    return env


def head_of(app: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd=app)[1].strip()


def main() -> int:
    if shutil.which("flock") is None or shutil.which("sqlite3") is None:
        # Код 77 И причина одновременно (D-42): пропуск без причины
        # неотличим от поломки, а прежние «ИТОГО: 0 OK, 0 FAIL» с кодом 0
        # выдавали непроверенный деплой за проверенный.
        missing = [t for t in ("flock", "sqlite3") if shutil.which(t) is None]
        print(f"ПРОПУЩЕНО: в системе нет {' и '.join(missing)} — "
              f"deploy.sh проверять нечем (на macOS: brew install util-linux)")
        return 77
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir()
    script = str(ROOT / "deploy" / "deploy.sh")

    app = make_repo()
    (WORK / "state").mkdir(parents=True, exist_ok=True)
    (WORK / "data").mkdir(parents=True, exist_ok=True)
    (WORK / "env").write_text("OBOROT_ENV=prod\n", encoding="utf-8")
    make_stubs(WORK / "stub-bin")

    # Боевая база, чтобы проверился шаг с копией.
    import sqlite3 as _sq
    con = _sq.connect(WORK / "data" / "oborot.db")
    con.execute("CREATE TABLE orgs (id INTEGER PRIMARY KEY, name TEXT)")
    con.execute("INSERT INTO orgs (name) VALUES ('Бренд')")
    con.commit(); con.close()

    v2 = add_commit(app, "v2")
    git(["checkout", "-q", "--detach", "HEAD~1"], cwd=app)

    print("\n== Успешная выкладка ==")
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("выкладка завершилась успешно", rc == 0, out[-300:])
    check("код переключён на целевой коммит", head_of(app) == v2, head_of(app)[:12])
    envtext = (WORK / "env").read_text(encoding="utf-8")
    check("OBOROT_COMMIT записан", f"OBOROT_COMMIT={v2}" in envtext, envtext.strip())
    check("прежний OBOROT_ENV не потерян при атомарной записи",
          "OBOROT_ENV=prod" in envtext, envtext.strip())
    prev = (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip()
    check("предыдущий коммит сохранён для отката", len(prev) == 40, prev[:12])
    backups = list((WORK / "data" / "backups").glob("oborot-*.db"))
    check("копия базы снята и прошла quick_check", len(backups) == 1,
          str([b.name for b in backups]))

    print("\n== Занятый лок ==")
    lock = WORK / "state" / "deploy.lock"
    lock.touch()
    # start_new_session: flock запускает `sleep` потомком, и дескриптор держит
    # именно он. Убийство одного flock оставляло бы лок занятым — все
    # последующие проверки падали бы с «другой деплой уже выполняется», причём
    # в местах, к локу отношения не имеющих. Поймано первым же прогоном.
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"],
                              start_new_session=True)
    try:
        rc, out = run(["bash", script, v2], env=deploy_env(app))
        check("вторая выкладка не начинается", rc != 0, f"rc={rc}")
        check("и объясняет, почему", "другой деплой" in out, out[-200:])
    finally:
        os.killpg(os.getpgid(holder.pid), signal.SIGKILL)
        holder.wait()

    print("\n== Грязная рабочая копия ==")
    (app / "app.py").write_text("грязь\n", encoding="utf-8")
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("выкладка на грязной копии отклонена", rc != 0 and "незакоммич" in out,
          out[-200:])
    git(["checkout", "-q", "--", "."], cwd=app)

    print("\n== Коммит вне истории main ==")
    git(["checkout", "-q", "-B", "side", v2], cwd=app)
    side = add_commit_local(app, "чужая ветка")
    git(["checkout", "-q", "--detach", v2], cwd=app)
    rc, out = run(["bash", script, side], env=deploy_env(app))
    check("случайный коммит из чужой ветки в бой не едет",
          rc != 0 and "не принадлежит истории" in out, out[-200:])

    print("\n== Fail-closed: коммит без requirements.lock ==")
    git(["checkout", "-q", "main"], cwd=app)
    nolock = add_commit(app, "без lock", lock=False)
    git(["checkout", "-q", "--detach", v2], cwd=app)
    rc, out = run(["bash", script, nolock], env=deploy_env(app))
    check("без lock-файла выкладка отклонена", rc != 0 and "requirements.lock" in out,
          out[-250:])
    check("прод при этом остался на прежнем коммите", head_of(app) == v2,
          head_of(app)[:12])
    rc, out = run(["bash", script, nolock],
                  env=deploy_env(app, {"OBOROT_ALLOW_NO_LOCK": "1"}))
    check("явное разрешение снимает запрет (осознанный откат)", rc == 0, out[-250:])

    print("\n== Падение pip не переключает код ==")
    git(["checkout", "-q", "main"], cwd=app)
    v3 = add_commit(app, "v3")
    git(["checkout", "-q", "--detach", nolock], cwd=app)
    before = head_of(app)
    make_stubs(WORK / "stub-bin", pip_ok=False)
    rc, out = run(["bash", script, v3], env=deploy_env(app))
    check("выкладка отклонена", rc != 0, f"rc={rc}")
    check("КОД НЕ ПЕРЕКЛЮЧЁН — сервис остался на рабочем коммите",
          head_of(app) == before, f"{head_of(app)[:12]} vs {before[:12]}")
    make_stubs(WORK / "stub-bin")

    print("\n== Приложение не поднялось ==")
    make_stubs(WORK / "stub-bin", health_ok=False)
    rc, out = run(["bash", script, v3], env=deploy_env(app))
    check("выход 1", rc == 1, f"rc={rc}")
    check("показаны логи сервиса", "логи сервиса" in out, out[-200:])
    check("названа команда отката", "bash deploy/deploy.sh" in out, out[-300:])
    check("названа копия базы, снятая перед выкладкой",
          "копия базы перед этой выкладкой" in out, out[-300:])
    make_stubs(WORK / "stub-bin")

    shutil.rmtree(WORK, ignore_errors=True)
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


def add_commit_local(app: Path, text: str) -> str:
    """Коммит БЕЗ push: остаётся вне истории origin/main."""
    (app / "app.py").write_text(text + "\n", encoding="utf-8")
    git(["add", "-A"], cwd=app)
    git(["commit", "-qm", text], cwd=app)
    return git(["rev-parse", "HEAD"], cwd=app)[1].strip()


if __name__ == "__main__":
    sys.exit(main())
