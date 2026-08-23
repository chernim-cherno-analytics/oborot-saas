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
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           shell=isinstance(cmd, str), timeout=timeout)
    except OSError as exc:
        # Запуск скрипта с несуществующим интерпретатором в первой строке ядро
        # отвергает так же, как отсутствующий файл (ENOENT). Для проверок ниже
        # это не исключение, а результат: «обёртка после переезда не работает».
        return 127, f"не запускается: {exc}"
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
    # Как на сервере: рядом с кодом живут файлы, которые в git не входят
    # осознанно. Проверка на неотслеживаемое обязана их пропускать.
    (app / ".gitignore").write_text("*.log\n", encoding="utf-8")
    git(["add", "-A"], cwd=app)
    git(["commit", "-qm", "v1"], cwd=app)
    git(["remote", "add", "origin", str(origin)], cwd=app)
    git(["push", "-q", "-u", "origin", "main"], cwd=app)
    return app


def add_commit(app: Path, text: str, lock: bool = True) -> str:
    (app / "app.py").write_text(text + "\n", encoding="utf-8")
    if lock:
        # Метка релиза в lock-файле — способ различить окружения. Подставной pip
        # складывает установленный lock внутрь venv, и по этой строке видно, ДЛЯ
        # КАКОГО коммита собрано окружение, которое сейчас в бою.
        (app / "requirements.lock").write_text(
            f"httpx==0.28.1\n# release {text}\n", encoding="utf-8")
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
    # systemctl ведёт журнал вызовов: по нему проверяется, что при сбое ДО
    # перезапуска сервис не трогали вообще. Без журнала «откат безопасен,
    # потому что перезапуска не было» остаётся утверждением, а не проверкой.
    (bindir / "systemctl").write_text(
        '#!/bin/sh\necho "$*" >> "${SYSTEMCTL_LOG:-/dev/null}"\nexit 0\n',
        encoding="utf-8")
    (bindir / "journalctl").write_text("#!/bin/sh\necho '(логи сервиса)'\n",
                                       encoding="utf-8")
    body = '{"status":"ok","db":true}' if health_ok else '{"status":"not ready"}'
    code = "0" if health_ok else "1"
    (bindir / "curl").write_text(
        f"#!/bin/sh\nprintf '%s' '{body}'\nexit {code}\n", encoding="utf-8")
    for f in ("systemctl", "journalctl", "curl"):
        (bindir / f).chmod(0o755)
    # Подставные python и pip. Настоящая сборка venv занимала бы секунды на
    # каждую выкладку и лезла бы в сеть за пакетами; здесь нужно проверить
    # поведение скрипта, а не работу pip.
    #
    # Подставной pip складывает файл требований, который ему дали, внутрь venv
    # (INSTALLED) и дописывает строку в журнал. По INSTALLED видно, для какого
    # релиза собрано окружение; по журналу — лазил ли откат в сеть.
    #
    # ГЛАВНОЕ в этой площадке — первая строка `bin/pip`. У настоящего venv в
    # консольных обёртках зашит АБСОЛЮТНЫЙ путь интерпретатора того каталога, в
    # котором venv создавали; после переименования каталога обёртка перестаёт
    # запускаться («bad interpreter»). Площадка обязана это воспроизводить,
    # иначе она проверяет не тот venv, который бывает на сервере: скрипт,
    # дёргающий `bin/pip` после переезда, проходил бы тест и ломался в бою.
    # `bin/python` переезд переживает — у настоящего venv это ссылка на
    # системный интерпретатор.
    fail = "" if pip_ok else "echo 'pip упал' >&2\nexit 1\n"
    pip_body = (
        f"{fail}"
        # Управляемый сбой окружения ИМЕННО на финальном пути: до подмены тот же
        # самый venv лежит по адресу staging и отвечает нормально.
        'LIVE="$(cd "${OBOROT_VENV:-/нет}" 2>/dev/null && pwd || echo /нет)"\n'
        'HERE="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || echo /тоже-нет)"\n'
        'if [ "${FAIL_VENV_VERIFY:-0}" != "0" ] && [ "$HERE" = "$LIVE" ]; then\n'
        '  if [ "$FAIL_VENV_VERIFY" = "2" ] && [ -n "${SABOTAGE_DIR:-}" ]; then\n'
        '    rm -rf "$SABOTAGE_DIR"\n'
        '  fi\n'
        '  echo "подставной pip: окружение на финальном пути неисправно" >&2\n'
        '  exit 1\n'
        'fi\n'
        'echo "$*" >> "$PIP_LOG"\n'
        'if [ "$1" = "install" ]; then\n'
        '  for a in "$@"; do req="$a"; done\n'
        '  cp "$req" "$(dirname "$0")/../INSTALLED" 2>/dev/null || true\n'
        'fi\n'
        "exit 0\n")
    pip_body_file = bindir / "pip-body"
    pip_body_file.write_text(pip_body, encoding="utf-8")

    py_tpl = bindir / "python-template"
    py_tpl.write_text(
        "#!/bin/sh\n"
        '# умеет ровно два вызова: python -m venv <каталог> и python -m pip ...\n'
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        '  mkdir -p "$3/bin" || exit 1\n'
        # Обёртка pip с шебангом на СВОЙ каталог — как у настоящего venv.
        '  printf "#!%s/bin/python\\n" "$3" > "$3/bin/pip" || exit 1\n'
        f'  cat "{pip_body_file}" >> "$3/bin/pip" || exit 1\n'
        f'  cp "{py_tpl}" "$3/bin/python" || exit 1\n'
        '  chmod 755 "$3/bin/pip" "$3/bin/python"\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then\n'
        '  shift 2\n'
        # Через `sh <файл>`, а не запуском обёртки: настоящий `python -m pip`
        # импортирует пакет, а не исполняет консольный скрипт, и потому
        # переезду каталога не подвержен.
        '  exec sh "$(dirname "$0")/pip" "$@"\n'
        'fi\n'
        "exit 0\n", encoding="utf-8")
    py_tpl.chmod(0o755)

    venv_bin = bindir.parent / "venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    shutil.copy(py_tpl, venv_bin / "python")
    (venv_bin / "pip").write_text(
        f"#!{venv_bin}/python\n" + pip_body, encoding="utf-8")
    for name in ("python", "pip"):
        (venv_bin / name).chmod(0o755)


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
        "PIP_LOG": str(WORK / "pip.log"),
        "SYSTEMCTL_LOG": str(WORK / "systemctl.log"),
    })
    env.update(extra or {})
    return env


def head_of(app: Path) -> str:
    return git(["rev-parse", "HEAD"], cwd=app)[1].strip()


def main() -> int:
    if shutil.which("flock") is None or shutil.which("sqlite3") is None:
        print("ПРОПУСК: нет flock или sqlite3 — проверять нечего.")
        print("ИТОГО: 0 OK, 0 FAIL")
        return 0
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
    installed = (WORK / "venv" / "INSTALLED")
    check("живое окружение собрано ИЗ ЦЕЛЕВОГО коммита",
          installed.exists() and "# release v2" in installed.read_text(encoding="utf-8"),
          installed.read_text(encoding="utf-8").strip() if installed.exists() else "нет файла")
    kept = list((WORK).glob("venv-*"))
    check("прежнее окружение отложено, а не затёрто", len(kept) == 1,
          str([k.name for k in kept]))
    check("запись о прежнем окружении сделана",
          (WORK / "state" / "PREVIOUS_VENV").exists())

    print("\n== Окружение живо на ФИНАЛЬНОМ пути, а не только на staging ==")
    # Сначала убеждаемся, что площадка честная: обёртка bin/pip после переезда
    # каталога действительно не запускается. Если это перестанет быть правдой,
    # все проверки ниже станут ничего не значащими, и знать об этом надо сразу.
    rc_pip, out_pip = run([str(WORK / "venv" / "bin" / "pip"), "--version"])
    check("площадка воспроизводит настоящую поломку: bin/pip после переезда не запускается",
          rc_pip != 0, f"rc={rc_pip} {out_pip.strip()[:80]}")
    rc_py, out_py = run([str(WORK / "venv" / "bin" / "python"), "-m", "pip", "--version"],
                        env=deploy_env(app))
    check("а `python -m pip` переезд переживает", rc_py == 0, out_py.strip()[:80])
    check("метка релиза лежит в живом окружении",
          (WORK / "venv" / "RELEASE_SHA").read_text(encoding="utf-8").strip() == v2)
    src = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    check("скрипт нигде не зовёт консольную обёртку bin/pip напрямую",
          not any("bin/pip" in ln for ln in code_lines),
          " | ".join(ln.strip()[:60] for ln in code_lines if "bin/pip" in ln))

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

    print("\n== Неотслеживаемый файл в рабочей копии ==")
    # `git checkout` неотслеживаемые файлы не трогает: положенный руками
    # `sitecustomize.py` или `analytics.py.bak` переживёт и выкладку, и откат,
    # а скрипт при этом уверенно напечатает, что в бою ровно этот коммит.
    stray = app / "sitecustomize.py"
    stray.write_text("# положено руками мимо git\n", encoding="utf-8")
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("выкладка с неотслеживаемым файлом отклонена", rc != 0, f"rc={rc}")
    check("файл назван в отказе", "sitecustomize.py" in out, out[-300:])
    check("подсказано, как убрать", "git clean" in out, out[-300:])
    check("файл не удалён скриптом самовольно", stray.exists())
    stray.unlink()
    # Игнорируемое проверке мешать не должно, иначе на сервере, где рядом с
    # кодом лежат база и логи, деплой не пройдёт никогда (.gitignore заведён
    # в make_repo вместе с первым коммитом).
    (app / "server.log").write_text("шум\n", encoding="utf-8")
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("игнорируемый файл выкладке не мешает", rc == 0, out[-250:])
    (app / "server.log").unlink()

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
    env_before = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    make_stubs(WORK / "stub-bin", pip_ok=False)
    rc, out = run(["bash", script, v3], env=deploy_env(app))
    check("выкладка отклонена", rc != 0, f"rc={rc}")
    check("КОД НЕ ПЕРЕКЛЮЧЁН — сервис остался на рабочем коммите",
          head_of(app) == before, f"{head_of(app)[:12]} vs {before[:12]}")
    live_now = (WORK / "venv" / "INSTALLED")
    check("ЖИВОЕ ОКРУЖЕНИЕ НЕ ТРОНУТО — собирали в стороне",
          live_now.read_text(encoding="utf-8") == env_before,
          live_now.read_text(encoding="utf-8").strip())
    staging = list(WORK.glob(".venv-staging.*")) + list(WORK.glob(".venv-held.*"))
    check("недособранное окружение убрано за собой", not staging, str(staging))
    make_stubs(WORK / "stub-bin")

    print("\n== Откат возвращает и код, и окружение ==")
    # Ради этого раздела и переделано устройство venv. Прежняя схема ставила
    # зависимости нового релиза в ЖИВОЕ окружение: `deploy.sh <прежний-sha>`
    # возвращал код, а библиотеки оставлял новые. Прод оказывался в состоянии,
    # которого не было ни в одном коммите.
    git(["checkout", "-q", "main"], cwd=app)
    v4 = add_commit(app, "v4")
    # Сначала честно выкатываем v2 через сам скрипт: код и окружение должны
    # совпадать ДО того, как проверять откат. Подмена HEAD руками оставила бы
    # площадку в состоянии «код от одного релиза, библиотеки от другого» —
    # именно в том, которого этот раздел и не должен допускать.
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("подготовка: выкатили v2", rc == 0 and head_of(app) == v2, out[-200:])
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("подготовка: в бою окружение v2", "# release v2" in live, live.strip())
    rc, out = run(["bash", script, v4], env=deploy_env(app))
    check("выкатили v4", rc == 0 and head_of(app) == v4, out[-200:])
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("в бою окружение v4", "# release v4" in live, live.strip())
    kept_v2 = WORK / f"venv-{v2}"
    check("окружение прежнего релиза отложено под его именем", kept_v2.is_dir(),
          str([p.name for p in WORK.glob('venv-*')]))

    # Откат делается в аварии, а в аварии сеть — не то, на что можно
    # рассчитывать. Ломаем pip: если откат зависит от установки пакетов, он
    # провалится ровно тогда, когда нужнее всего. Прежняя схема (ставить
    # зависимости в живой venv) в этом месте оставляла прод на плохом релизе.
    (WORK / "pip.log").write_text("", encoding="utf-8")
    make_stubs(WORK / "stub-bin", pip_ok=False)
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    make_stubs(WORK / "stub-bin")
    check("откат выполнен ДАЖЕ БЕЗ РАБОТАЮЩЕГО pip", rc == 0, out[-250:])
    check("код вернулся на прежний коммит", head_of(app) == v2, head_of(app)[:12])
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("ОКРУЖЕНИЕ ТОЖЕ ВЕРНУЛОСЬ, а не осталось от неудачного релиза",
          "# release v2" in live, live.strip())
    piplog = (WORK / "pip.log").read_text(encoding="utf-8")
    check("откат не ставил пакеты: готовое окружение переиспользовано",
          "install" not in piplog, piplog.strip().replace("\n", " | ") or "пусто")
    check("окружение неудачного релиза отложено на случай возврата",
          (WORK / f"venv-{v4}").is_dir())

    print("\n== Отложенное окружение с чужой меткой не переиспользуется ==")
    # Каталог назван именем релиза, но собран для другого. Снаружи это не видно
    # ничем — и без метки внутри откат подставил бы релизу чужие библиотеки.
    git(["checkout", "-q", "main"], cwd=app)
    vx = add_commit(app, "vx")
    fake = WORK / f"venv-{vx}"
    shutil.copytree(WORK / "venv", fake)
    (fake / "RELEASE_SHA").write_text("0" * 40 + "\n", encoding="utf-8")
    (fake / "INSTALLED").write_text("подложенное окружение\n", encoding="utf-8")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vx], env=deploy_env(app))
    check("выкладка прошла", rc == 0, out[-200:])
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("чужое окружение не подставлено, а пересобрано",
          "# release vx" in live, live.strip())
    check("пересборка действительно была", "install" in (WORK / "pip.log").read_text(encoding="utf-8"))

    print("\n== Старые окружения не копятся бесконечно ==")
    for n in ("v5", "v6", "v7"):
        # Возвращаемся на main перед каждым коммитом: предыдущая выкладка
        # оставила рабочую копию в detached HEAD, и коммит ушёл бы мимо ветки.
        git(["checkout", "-q", "main"], cwd=app)
        sha = add_commit(app, n)
        rc, out = run(["bash", script, sha], env=deploy_env(app, {"OBOROT_VENV_KEEP": "2"}))
        check(f"выкладка {n}", rc == 0, out[-200:])
    kept = sorted(p.name for p in WORK.glob("venv-*"))
    check("хранится ровно столько прежних окружений, сколько велено",
          len(kept) == 2, str(kept))

    print("\n== Сбой ПОСЛЕ подмены окружения откатывает всё ==")
    # Самый неприятный класс сбоя: скрипт успел переключить код, переписать env
    # и подменить окружение — и упал до перезапуска. Раньше в этом месте прод
    # оставался в состоянии, которого нет ни в одном релизе: код нового,
    # окружение нового, сервис ещё старый, и никто об этом не знает.
    git(["checkout", "-q", "main"], cwd=app)
    vf = add_commit(app, "vf")
    git(["checkout", "-q", "--detach", v2], cwd=app)
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("подготовка: в бою v2", rc == 0 and head_of(app) == v2, out[-200:])
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    before_env = (WORK / "env").read_text(encoding="utf-8")
    rc, out = run(["bash", script, vf],
                  env=deploy_env(app, {"FAIL_VENV_VERIFY": "1"}))
    check("выкладка отклонена", rc != 0, f"rc={rc}")
    check("сказано, что сбой до перезапуска", "СБОЙ ДО ПЕРЕЗАПУСКА" in out, out[-400:])
    check("КОД возвращён на прежний коммит", head_of(app) == v2, head_of(app)[:12])
    check("OBOROT_COMMIT возвращён", (WORK / "env").read_text(encoding="utf-8") == before_env,
          (WORK / "env").read_text(encoding="utf-8").strip())
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("ОКРУЖЕНИЕ возвращено на прежний релиз", "# release v2" in live, live.strip())
    check("метка живого окружения тоже прежняя",
          (WORK / "venv" / "RELEASE_SHA").read_text(encoding="utf-8").strip() == v2)
    syslog = (WORK / "systemctl.log").read_text(encoding="utf-8")
    check("СЕРВИС НЕ ПЕРЕЗАПУСКАЛСЯ — потому откат и безопасен",
          "restart" not in syslog, syslog.strip().replace("\n", " | ") or "пусто")
    leftovers = list(WORK.glob(".venv-staging.*")) + list(WORK.glob(".venv-held.*")) \
        + list(WORK.glob(".venv-rollback.*"))
    check("временных каталогов окружения не осталось", not leftovers, str(leftovers))

    print("\n== Вторичный сбой: окружение не вернуть, но VENV не исчезает ==")
    # Откат тоже может не получиться — на то он и авария. Требование здесь одно
    # и жёсткое: после выхода скрипта $VENV существует и работоспособен. Пустое
    # место на его месте означает, что сервис не поднимется вообще ни на каком
    # коммите, — то есть неудачная выкладка превратилась бы в полноценную аварию.
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vf], env=deploy_env(app, {
        "FAIL_VENV_VERIFY": "2",          # проверка падает и уносит с собой
        "SABOTAGE_DIR": str(WORK / f"venv-{v2}"),   # отложенное окружение v2
    }))
    check("выкладка отклонена", rc != 0, f"rc={rc}")
    check("о неполном откате сказано прямо", "ОТКАТ НЕПОЛНЫЙ" in out, out[-500:])
    check("названо, что именно осталось в $VENV",
          "ОКРУЖЕНИЕ ВЕРНУТЬ НЕ УДАЛОСЬ" in out, out[-500:])
    check("$VENV НА МЕСТЕ и работоспособен", (WORK / "venv" / "bin" / "python").exists(),
          str(sorted(p.name for p in WORK.glob("venv*"))))
    rc_py, _ = run([str(WORK / "venv" / "bin" / "python"), "-m", "pip", "--version"],
                   env=deploy_env(app))
    check("окружение в $VENV запускается", rc_py == 0, f"rc={rc_py}")
    check("код всё равно возвращён на прежний коммит", head_of(app) == v2, head_of(app)[:12])
    syslog = (WORK / "systemctl.log").read_text(encoding="utf-8")
    check("сервис не перезапускался и здесь", "restart" not in syslog,
          syslog.strip().replace("\n", " | ") or "пусто")
    # Площадку надо вернуть в согласованное состояние: окружение v2 уничтожено
    # диверсией, в $VENV лежит окружение неудачного релиза. Выкатываем v2 честно.
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("после аварии обычная выкладка чинит состояние", rc == 0 and head_of(app) == v2,
          out[-250:])

    print("\n== Сбой на записи окружения: откатывается код ==")
    # Точка сбоя раньше: env не записался. Тогда откатывать нужно ровно одно —
    # код; окружение при этом трогать нельзя, оно ещё прежнее.
    #
    # Сбой создаётся правами на каталог, а root их не замечает — под root эта
    # проверка не доказывала бы ничего и потому пропускается вслух.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  ПРОПУСК: запущено под root, права каталога сбой не создадут")
    else:
        envdir = WORK / "envdir"
        envdir.mkdir(exist_ok=True)
        envfile = envdir / "env"
        shutil.copy(WORK / "env", envfile)
        live_before = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
        (WORK / "systemctl.log").write_text("", encoding="utf-8")
        envdir.chmod(0o500)
        try:
            rc, out = run(["bash", script, vf],
                          env=deploy_env(app, {"OBOROT_ENV_FILE": str(envfile)}))
        finally:
            envdir.chmod(0o700)
        check("выкладка отклонена", rc != 0, f"rc={rc}")
        check("названа причина", "OBOROT_COMMIT" in out, out[-400:])
        check("код возвращён на прежний коммит", head_of(app) == v2, head_of(app)[:12])
        check("окружение не трогали — оно и не менялось",
              (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8") == live_before)
        syslog = (WORK / "systemctl.log").read_text(encoding="utf-8")
        check("сервис не перезапускался", "restart" not in syslog,
              syslog.strip().replace("\n", " | ") or "пусто")

    print("\n== Приложение не поднялось ==")
    make_stubs(WORK / "stub-bin", health_ok=False)
    rc, out = run(["bash", script, v3], env=deploy_env(app))
    check("выход 1", rc == 1, f"rc={rc}")
    check("показаны логи сервиса", "логи сервиса" in out, out[-200:])
    check("названа команда отката", "bash deploy/deploy.sh" in out, out[-300:])
    check("названа копия базы, снятая перед выкладкой",
          "копия базы перед этой выкладкой" in out, out[-300:])
    check("сказано, что прежнее окружение цело и откат не требует сети",
          "откат обойдётся без сети" in out, out[-300:])
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
