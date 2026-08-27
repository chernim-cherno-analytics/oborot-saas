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
  8) копия базы снимается и проверяется до переключения кода;
  9) консольные обёртки (`bin/pip` и прочие) работают ПОСЛЕ переезда каталога
     окружения — и у свежесобранного, и у переиспользованного кэшированного;
 10) сбой починки или сбой запуска обёртки на финальном пути откатывают выкладку
     ДО перезапуска службы;
 11) выкладку исполняет драйвер из защищённого `origin/main`, а не тот, что
     лежит на диске: старый скрипт не доигрывает выкладку сам, откат на старый
     коммит драйвер не понижает, подстановка не зацикливается, временный файл
     убирается, отказ `git fetch` и сигнал не оставляют прод в промежутке;
 12) повторная выкладка ТОГО ЖЕ коммита не затирает различающиеся
     `PREVIOUS_SHA`/`PREVIOUS_VENV`, не печатает откат на самого себя и не
     позволяет уборке удалить настоящее окружение отката;
 13) такая повторная выкладка при исправном окружении сводится к починке и
     проверке: без пересборки venv, без копии базы и без перезапуска службы.

Про пункты 11–13 отдельно (OPS-8). Оба дефекта доказаны выпуском 27.08.2026, а
не выведены из рассуждений. `bash` читает скрипт из открытого дескриптора, и
`git checkout` в середине выкладки заменяет файл новым inode — прежний
дескриптор остаётся на старом содержимом, поэтому весь прогон доигрывает
ПРЕЖНЯЯ реализация. На проде это дало зелёный первый заход, на котором починка
консольных обёрток не исполнялась вовсе. Второй, вынужденный, заход тем же SHA
записал `PREVIOUS_SHA` равным выкладываемому коммиту: подсказка стала предлагать
откат на самого себя, а настоящая цель отката осталась в живых только потому,
что в квоте `OBOROT_VENV_KEEP` случайно было место.

Площадка это воспроизводит честно и потому обязана быть устроена как сервер:
подставной репозиторий несёт СВОЙ `deploy/deploy.sh`, выкладка запускается из
него, а «старый драйвер» — это тот же самый скрипт с наблюдаемой пометкой,
закоммиченный в более ранний коммит. Проверить, что пометка вообще попала в
файл, обязан отдельный раздел: иначе «старого драйвера не видно в выводе» стало
бы правдой по причине, к предмету проверки отношения не имеющей.

Про пункты 9–10 отдельно (OPS-6). Поломка настоящая и проверена на НАСТОЯЩЕМ
venv, а не только здесь: `python -m venv /tmp/x/.venv-staging.12345`, затем
`mv .venv-staging.12345 venv` — и `venv/bin/pip --version` даёт
`bad interpreter: /tmp/x/.venv-staging.12345/bin/python3.14: no such file or
directory`, код 127, тогда как `venv/bin/python -m pip --version` в том же
каталоге отвечает нормально. Ровно это и было на проде:
`/opt/oborot/venv/bin/pip` начинался с `#!/opt/oborot/.venv-staging.588121/...`.
Площадка обязана воспроизводить это сама — иначе проверки ниже ничего не стоят,
поэтому её честность доказывается отдельным разделом, независимым от deploy.sh.

Запуск из корня репозитория:  python tests/test_deploy.py
"""
import os
import re
import shutil
import signal
import subprocess
import sys
import time
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
    except subprocess.TimeoutExpired as exc:
        # Зависшая выкладка обязана выглядеть как ПАДЕНИЕ проверки, а не как
        # молчащий набор. Раньше такой случай улетал исключением наружу и
        # прогон вставал без единой строки о причине — то есть выглядел ровно
        # так же, как зависший CI. Возвращаем распознаваемый код и текст.
        out = ""
        for part in (exc.stdout, exc.stderr):
            if part:
                out += part if isinstance(part, str) else part.decode("utf-8", "replace")
        return 124, f"ЗАВИСЛО: не уложилось в {timeout} с — {cmd}\n{out}"
    return p.returncode, p.stdout + p.stderr


def wait_for(path: Path, limit: float = 60.0) -> bool:
    """Дождаться появления файла-отметки, но НИКОГДА не ждать вечно.

    Ожидание условия точнее паузы «на глазок»: сцена ниже требует, чтобы вторая
    выкладка стартовала, пока первая заведомо держит лок. Верхняя граница нужна
    ровно за тем, чтобы поломка превращалась в падение проверки, а не в
    зависший прогон.
    """
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


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
    # Как на сервере: драйвер выкладки лежит В САМОМ репозитории и едет вместе с
    # кодом. Без этого проверить bootstrap нечем — а именно на этом «скрипт
    # обновляется тем же checkout'ом, который он же и выполняет» и построен
    # первый дефект OPS-8.
    (app / "deploy").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "deploy" / "deploy.sh", app / "deploy" / "deploy.sh")
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


# «Старый драйвер» — не выдуманный скрипт, а РОВНО тот же самый, с одной
# наблюдаемой пометкой. Так и бывает на сервере: на диске лежит предыдущая
# редакция того же файла. Пометка вставляется после разбора аргументов —
# то есть заведомо ПОЗЖЕ того места, где актуальный драйвер обязан подставить
# себя, и заведомо РАНЬШЕ любой мутации: если она напечаталась, значит выкладку
# доигрывал старый скрипт.
OLD_DRIVER_ANCHOR = 'TARGET="${1:-origin/main}"'
OLD_DRIVER_MARK = "СТАРЫЙ ДРАЙВЕР ВЫПОЛНЯЕТСЯ"


def old_driver(text: str, *, sabotage: bool) -> str:
    """Тот же драйвер плюс пометка; при sabotage — ещё и отказ сразу после неё."""
    inject = f'echo "{OLD_DRIVER_MARK}"\n'
    if sabotage:
        # Отказ ровно здесь превращает проверку в однозначную: выкладка проходит
        # ТОЛЬКО если тело старого скрипта не исполнялось вовсе.
        inject += "exit 3\n"
    return text.replace(OLD_DRIVER_ANCHOR, OLD_DRIVER_ANCHOR + "\n" + inject, 1)


def commit_driver(app: Path, text: str, body: str) -> str:
    """Коммит, который меняет сам драйвер выкладки, и push в origin/main."""
    (app / "deploy" / "deploy.sh").write_text(body, encoding="utf-8")
    return add_commit(app, text)


def make_stubs(bindir: Path, *, health_ok=True, pip_ok=True) -> None:
    """Подставные системные команды. Настоящий сервер не нужен."""
    bindir.mkdir(parents=True, exist_ok=True)
    # systemctl ведёт журнал вызовов: по нему проверяется, что при сбое ДО
    # перезапуска сервис не трогали вообще. Без журнала «откат безопасен,
    # потому что перезапуска не было» остаётся утверждением, а не проверкой.
    #
    # Подставной systemctl вдобавок ВЕДЁТ СЕБЯ как systemd в одном, решающем
    # отношении: перезапуск переносит `OBOROT_COMMIT` из файла окружения в
    # «коммит живого процесса». Пока этого не было, площадка не умела отличить
    # «служба отвечает» от «служба отвечает ПРЕЖНИМ релизом» — а вся разница
    # между исправной повторной выкладкой и молчаливым враньём именно здесь.
    #
    # FAIL_RESTART — перезапуск не удался: код, env и окружение уже целевые, а в
    # бою остался прежний процесс. SIGNAL_ON_RESTART — сигнал приходит ВНУТРИ
    # самого перезапуска, то есть ровно в тот шов, где отката уже быть не может,
    # а бросать выкладку недоделанной нельзя.
    (bindir / "systemctl").write_text(
        '#!/bin/sh\n'
        'echo "$*" >> "${SYSTEMCTL_LOG:-/dev/null}"\n'
        'if [ "$1" != "restart" ]; then exit 0; fi\n'
        'if [ "${FAIL_RESTART:-0}" = "1" ]; then exit 1; fi\n'
        'if [ -n "${SIGNAL_ON_RESTART:-}" ] && [ ! -e "$SIGNAL_ON_RESTART" ]; then\n'
        '  : > "$SIGNAL_ON_RESTART"\n'
        '  kill -TERM "$PPID" 2>/dev/null || true\n'
        'fi\n'
        'if [ -n "${RUNNING_COMMIT_FILE:-}" ] && [ -f "${OBOROT_ENV_FILE:-}" ]; then\n'
        '  sed -n "s/^OBOROT_COMMIT=//p" "$OBOROT_ENV_FILE" > "$RUNNING_COMMIT_FILE"\n'
        'fi\n'
        'exit 0\n',
        encoding="utf-8")
    (bindir / "journalctl").write_text("#!/bin/sh\necho '(логи сервиса)'\n",
                                       encoding="utf-8")
    # Подставной mv. В обычном прогоне прозрачен, при FAIL_MV_KEEP=1 отказывает
    # РОВНО на одном переименовании — том, которым прежнее окружение
    # откладывается под именем venv-<sha>. Точечность здесь и есть смысл: общее
    # «сломай mv» свалило бы выкладку раньше, на другом шаге, и про интересующее
    # окно («$VENV уже подменён, а функция ещё не вернулась») не сказало бы
    # ничего. Целевые пути подмены — venv-<sha>; сам $VENV называется venv,
    # под шаблон не попадает, и возврат окружения при откате работает.
    #
    # FAIL_MV_SHEBANG отказывает на другом переименовании — том, которым
    # починка консольной обёртки ставит исправленный файл на место `bin/pip`.
    # Это инъекция «починка не удалась»: скрипт обязан не чинить дальше, не
    # перезапускать службу и откатить всё до прежнего релиза.
    (bindir / "mv").write_text(
        '#!/bin/sh\n'
        'for a in "$@"; do dest="$a"; done\n'
        'if [ "${FAIL_MV_KEEP:-0}" = "1" ]; then\n'
        '  case "${dest##*/}" in\n'
        '    venv-*) echo "подставной mv: отказ на $dest" >&2; exit 1;;\n'
        '  esac\n'
        'fi\n'
        'if [ "${FAIL_MV_SHEBANG:-0}" = "1" ]; then\n'
        '  case "${dest##*/}" in\n'
        '    pip) echo "подставной mv: отказ на починке $dest" >&2; exit 1;;\n'
        '  esac\n'
        'fi\n'
        # FAIL_MV_SILENT — сбой хуже явного отказа: переименование ТИХО не
        # происходит, а код возврата 0. Починка отчитывается об успехе, файл
        # остаётся прежним. Это единственный способ проверить, что проверка
        # обёрток действительно независима от починки, а не пересказывает её
        # отчёт: поймать такое обязана именно она.
        'if [ "${FAIL_MV_SILENT:-0}" = "1" ]; then\n'
        '  case "${dest##*/}" in\n'
        '    pip) rm -f "$1"; exit 0;;\n'
        '  esac\n'
        'fi\n'
        # FAIL_MV_SIGNAL — не сбой, а СИГНАЛ, и точка его доставки выбрана не
        # случайно. Он приходит выкладке в тот момент, когда код уже переключён,
        # env переписан, окружение подменено, отложенное окружение уже лежит под
        # своим именем — и служба ещё не перезапускалась. Это ровно то окно, в
        # котором D-46 требует полного отката. Раньше (на подмене окружения)
        # сигнал застал бы скрипт с недосведёнными указателями и проверял бы не
        # то. Сторож-файл нужен, чтобы сигнал пришёл РОВНО ОДИН раз и не мешал
        # самому откату.
        'if [ "${FAIL_MV_SIGNAL:-0}" = "1" ]; then\n'
        '  case "${dest##*/}" in\n'
        '    pip) if [ ! -e "${FAIL_MV_SIGNAL_ONCE:-/несуществующий}" ]; then\n'
        '           : > "$FAIL_MV_SIGNAL_ONCE"\n'
        '           kill -TERM "$PPID" 2>/dev/null || true\n'
        '         fi;;\n'
        '  esac\n'
        'fi\n'
        'exec /bin/mv "$@"\n', encoding="utf-8")
    # Готовность отдаёт КОММИТ ЖИВОГО ПРОЦЕССА — как настоящая `/health/ready`
    # (`app/main.py`, поле `commit` из `app.version.BUILD_COMMIT`). Это не
    # украшение: без коммита в ответе «служба отвечает» и «служба отвечает тем
    # релизом, который мы выкатываем» неразличимы, а разница между ними и есть
    # предмет проверки ниже.
    if health_ok:
        (bindir / "curl").write_text(
            '#!/bin/sh\n'
            'c=""\n'
            'if [ -n "${RUNNING_COMMIT_FILE:-}" ] && [ -f "$RUNNING_COMMIT_FILE" ]; then\n'
            '  c=$(cat "$RUNNING_COMMIT_FILE")\n'
            'fi\n'
            'printf \'{"status":"ok","db":true,"commit":"%s"}\' "$c"\n'
            'exit 0\n', encoding="utf-8")
    else:
        (bindir / "curl").write_text(
            "#!/bin/sh\nprintf '%s' '{\"status\":\"not ready\"}'\nexit 1\n",
            encoding="utf-8")
    for f in ("systemctl", "journalctl", "curl", "mv"):
        (bindir / f).chmod(0o755)
    # Подставные python и pip. Настоящая сборка venv занимала бы секунды на
    # каждую выкладку и лезла бы в сеть за пакетами; здесь нужно проверить
    # поведение скрипта, а не работу pip.
    #
    # Устроено это так же, как настоящий venv, и по одной причине: иначе
    # площадка проверяет не тот venv, который бывает на сервере.
    #
    #   * `bin/python` — СИМВОЛИЧЕСКАЯ ССЫЛКА на настоящий интерпретатор. Не
    #     копия и не скрипт: ядро не умеет исполнять шебанг, указывающий на
    #     другой скрипт (ENOEXEC), а у настоящего venv там ссылка на бинарник.
    #     Подставным скриптом здесь площадка молча меняла бы предмет проверки:
    #     «обёртка не запустилась» значило бы «интерпретатор не бинарник»,
    #     а не «каталог переехал».
    #   * `pyvenv.cfg` — чтобы интерпретатор считал каталог окружением и
    #     выставлял `sys.prefix` на него. По `sys.prefix` подставной pip и
    #     понимает, в КАКОМ окружении его позвали.
    #   * `bin/pip` — обычный текстовый файл, в первой строке которого зашит
    #     АБСОЛЮТНЫЙ путь `bin/python` того каталога, где venv «собирали».
    #     После переименования каталога такая обёртка не запускается вовсе
    #     («bad interpreter», код 127) — ровно то, что было на проде.
    #
    # Сам pip подменён модулями `pip` и `venv`, которые лежат на PYTHONPATH и
    # потому заслоняют настоящие: сборка окружения мгновенна, установка пакетов
    # никуда не ходит. Подставной pip складывает поданный ему файл требований
    # внутрь venv (INSTALLED) и дописывает строку в журнал: по INSTALLED видно,
    # для какого релиза собрано окружение, по журналу — лазил ли откат в сеть.
    #
    # Модульный вызов (`python -m pip`) и запуск консольной обёртки различаются
    # честно, а не по метке в окружении: у них разные точки входа. На этом
    # держится инъекция FAIL_CONSOLE_PIP — настоящая форма дефекта OPS-6, где
    # `python -m pip` работает, а обёртка нет.
    shim = bindir / "pyshim"
    (shim / "pip").mkdir(parents=True, exist_ok=True)
    (shim / "venv").mkdir(parents=True, exist_ok=True)
    (shim / "pip" / "MODE").write_text("ok" if pip_ok else "broken", encoding="utf-8")
    (shim / "pip" / "__init__.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "import os, shutil, sys, time\n"
        "\n"
        "\n"
        "def _refuse(msg):\n"
        "    print(msg, file=sys.stderr)\n"
        "    return 1\n"
        "\n"
        "\n"
        "def run(console):\n"
        "    args = sys.argv[1:]\n"
        "    venv = os.path.realpath(sys.prefix)\n"
        "    # Режим «pip сломан» баковался в окружение в момент его сборки —\n"
        "    # как и раньше, когда тело pip копировалось внутрь venv. Иначе\n"
        "    # проверка «откат переиспользует готовое окружение без сети»\n"
        "    # ломалась бы: у отложенного окружения pip обязан остаться рабочим.\n"
        "    if os.path.exists(os.path.join(venv, 'STUB_PIP_BROKEN')):\n"
        "        return _refuse('pip упал')\n"
        "    # Управляемый сбой ИМЕННО на финальном пути: до подмены тот же самый\n"
        "    # venv лежит по адресу staging и отвечает нормально.\n"
        "    live = os.path.realpath(os.environ.get('OBOROT_VENV', '/нет'))\n"
        "    on_final_path = venv == live\n"
        "    verify = os.environ.get('FAIL_VENV_VERIFY', '0')\n"
        "    if verify != '0' and on_final_path:\n"
        "        if verify == '2' and os.environ.get('SABOTAGE_DIR'):\n"
        "            shutil.rmtree(os.environ['SABOTAGE_DIR'], ignore_errors=True)\n"
        "        return _refuse('подставной pip: окружение на финальном пути неисправно')\n"
        "    if os.environ.get('FAIL_CONSOLE_PIP', '0') != '0' and console and on_final_path:\n"
        "        return _refuse('подставной pip: консольная обёртка на финальном пути неисправна')\n"
        "    log = os.environ.get('PIP_LOG')\n"
        "    if log:\n"
        "        with open(log, 'a', encoding='utf-8') as fh:\n"
        "            fh.write(' '.join(args) + '\\n')\n"
        # PIP_GATE останавливает выкладку на сборке окружения — то есть ПОСЛЕ
        # взятия лока и ДО `git checkout`. Точка выбрана не для красоты: в этот
        # момент лок заведомо занят, а на диске всё ещё лежит СТАРЫЙ драйвер,
        # поэтому вторая выкладка обязана сначала подставить себе актуальный и
        # только потом упереться в лок. Ворота убирают из сцены время: «обычно
        # успевает» проверкой не является.
        "    gate = os.environ.get('PIP_GATE')\n"
        "    if gate and args[:1] == ['install']:\n"
        "        open(gate + '.reached', 'w', encoding='utf-8').close()\n"
        "        while not os.path.exists(gate):\n"
        "            time.sleep(0.05)\n"
        "    if args and args[0] == 'install':\n"
        "        try:\n"
        "            shutil.copyfile(args[-1], os.path.join(venv, 'INSTALLED'))\n"
        "        except OSError:\n"
        "            pass\n"
        "    if args[:1] == ['--version']:\n"
        "        # Печатаем окружение, в котором нас исполнили. Снаружи это\n"
        "        # единственный способ увидеть, ЧЕРЕЗ КАКОЙ интерпретатор\n"
        "        # запустилась обёртка: шебанг на чужой, но живой python\n"
        "        # выглядит совершенно рабочим и об ошибке не говорит ничем.\n"
        "        print(venv)\n"
        "    return 0\n", encoding="utf-8")
    (shim / "pip" / "__main__.py").write_text(
        "import sys\nfrom pip import run\nsys.exit(run(False))\n", encoding="utf-8")
    (shim / "venv" / "__init__.py").write_text(
        "# -*- coding: utf-8 -*-\n"
        "import os, sys\n"
        "\n"
        "\n"
        "def create(target):\n"
        "    \"\"\"Собрать подставной venv так, как его собирает настоящий `-m venv`.\"\"\"\n"
        "    target = os.path.abspath(target)\n"
        "    bindir = os.path.join(target, 'bin')\n"
        "    os.makedirs(bindir, exist_ok=True)\n"
        "    real = os.path.realpath(sys.executable)\n"
        "    link = os.path.join(bindir, 'python')\n"
        "    if os.path.islink(link) or os.path.exists(link):\n"
        "        os.remove(link)\n"
        "    os.symlink(real, link)\n"
        "    with open(os.path.join(target, 'pyvenv.cfg'), 'w', encoding='utf-8') as fh:\n"
        "        fh.write('home = %s\\n' % os.path.dirname(real))\n"
        "        fh.write('include-system-site-packages = false\\n')\n"
        "    mode = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),\n"
        "                        'pip', 'MODE')\n"
        "    broken = os.path.join(target, 'STUB_PIP_BROKEN')\n"
        "    if open(mode, encoding='utf-8').read().strip() == 'broken':\n"
        "        open(broken, 'w', encoding='utf-8').write('1\\n')\n"
        "    elif os.path.exists(broken):\n"
        "        os.remove(broken)\n"
        "    # Первая строка — абсолютный путь ЭТОГО каталога. Именно она и не\n"
        "    # переживает переезда, как у настоящих консольных обёрток.\n"
        "    pip = os.path.join(bindir, 'pip')\n"
        "    with open(pip, 'w', encoding='utf-8') as fh:\n"
        "        fh.write('#!%s\\n' % link)\n"
        "        fh.write('import sys\\n')\n"
        "        fh.write('from pip import run\\n')\n"
        "        fh.write('sys.exit(run(True))\\n')\n"
        "    os.chmod(pip, 0o755)\n"
        "    return target\n", encoding="utf-8")
    (shim / "venv" / "__main__.py").write_text(
        "import sys\nfrom venv import create\ncreate(sys.argv[1])\n", encoding="utf-8")

    # Живое окружение площадки собирается тем же подставным `-m venv`, что и
    # окружения релизов: двух разных способов собрать venv в тесте быть не должно.
    live = bindir.parent / "venv"
    shim_env = dict(os.environ)
    shim_env["PYTHONPATH"] = str(shim)
    subprocess.run([sys.executable, "-m", "venv", str(live)], env=shim_env, check=True,
                   capture_output=True)


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
        # Коммит, которым отвечает ЖИВОЙ процесс. Двигает его только перезапуск,
        # а не запись в файл окружения — как на сервере.
        "RUNNING_COMMIT_FILE": str(WORK / "running-commit"),
        # Подставные модули `pip` и `venv` заслоняют настоящие. Заслоняют именно
        # для процессов выкладки: сам тест их не импортирует.
        "PYTHONPATH": str(WORK / "stub-bin" / "pyshim"),
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
    installed = (WORK / "venv" / "INSTALLED")
    check("живое окружение собрано ИЗ ЦЕЛЕВОГО коммита",
          installed.exists() and "# release v2" in installed.read_text(encoding="utf-8"),
          installed.read_text(encoding="utf-8").strip() if installed.exists() else "нет файла")
    kept = list((WORK).glob("venv-*"))
    check("прежнее окружение отложено, а не затёрто", len(kept) == 1,
          str([k.name for k in kept]))
    check("запись о прежнем окружении сделана",
          (WORK / "state" / "PREVIOUS_VENV").exists())

    print("\n== Площадка честна: непереносимый шебанг воспроизводится ==")
    # Сначала — доказательство, что площадка ломает обёртку так же, как ломает
    # её настоящий venv. Оно СОЗНАТЕЛЬНО не зависит от deploy.sh: собираем venv
    # подставным python по одному адресу, переносим каталог и смотрим на
    # обёртку. Перестанет площадка воспроизводить поломку — всё, что проверяется
    # ниже, станет ничего не значащим, и знать об этом надо сразу и отдельно.
    probe_src = WORK / "probe-staging.111"
    probe_dst = WORK / "probe-moved"
    for d in (probe_src, probe_dst):
        shutil.rmtree(d, ignore_errors=True)
    rc, out = run([str(WORK / "venv" / "bin" / "python"), "-m", "venv", str(probe_src)],
                  env=deploy_env(app))
    check("подставной venv собран", rc == 0 and (probe_src / "bin" / "pip").exists(),
          out[-200:])
    rc_probe, out_probe = run([str(probe_src / "bin" / "pip"), "--version"],
                              env=deploy_env(app))
    check("ДО переезда консольная обёртка запускается", rc_probe == 0,
          f"rc={rc_probe} {out_probe.strip()[:80]}")
    os.rename(probe_src, probe_dst)
    rc_probe, out_probe = run([str(probe_dst / "bin" / "pip"), "--version"],
                              env=deploy_env(app))
    check("ПОСЛЕ переезда — не запускается (как настоящий venv: bad interpreter, 127)",
          rc_probe != 0, f"rc={rc_probe} {out_probe.strip()[:100]}")
    rc_probe, _ = run([str(probe_dst / "bin" / "python"), "-m", "pip", "--version"],
                      env=deploy_env(app))
    check("а `python -m pip` переезд переживает — потому дефект и был не виден",
          rc_probe == 0, f"rc={rc_probe}")
    shutil.rmtree(probe_dst, ignore_errors=True)

    print("\n== Окружение живо на ФИНАЛЬНОМ пути, а не только на staging ==")
    rc_py, out_py = run([str(WORK / "venv" / "bin" / "python"), "-m", "pip", "--version"],
                        env=deploy_env(app))
    check("`python -m pip` работает в живом окружении", rc_py == 0, out_py.strip()[:80])
    # OPS-6. Ровно то, что до сих пор не проверялось и потому доехало до прода:
    # выкладка зелёная, `python -m pip` работает, а прямой вызов обёртки — 127.
    rc_pip, out_pip = run([str(WORK / "venv" / "bin" / "pip"), "--version"],
                          env=deploy_env(app))
    check("ПРЯМОЙ bin/pip работает после выкладки — обёртка починена на финальном пути",
          rc_pip == 0, f"rc={rc_pip} {out_pip.strip()[:120]}")
    first_line = (WORK / "venv" / "bin" / "pip").read_text(encoding="utf-8").splitlines()[0]
    check("и её шебанг указывает на ФИНАЛЬНЫЙ путь, а не на staging",
          first_line == f"#!{WORK / 'venv'}/bin/python", first_line)
    check("временных файлов починки за собой не осталось",
          not list((WORK / "venv" / "bin").glob("*.oborot-shebang.*")),
          str([p.name for p in (WORK / "venv" / "bin").glob("*.oborot-shebang.*")]))
    check("метка релиза лежит в живом окружении",
          (WORK / "venv" / "RELEASE_SHA").read_text(encoding="utf-8").strip() == v2)
    src = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    # Сторож D-44 не снят, а сужен. Запрет был содержательный: зависимости не
    # ставятся и не проверяются консольной обёрткой, потому что она не переживает
    # переезда. Он остаётся. Появился ровно один прямой вызов обёртки — и он
    # проверочный: `--version` ничего не ставит и ничего не меняет.
    pip_wrapper_lines = [ln for ln in code_lines if re.search(r"bin/pip\b", ln)]
    check("зависимости по-прежнему ставятся и проверяются ТОЛЬКО через `python -m pip`",
          not any(re.search(r"bin/pip\"?\s+(install|check)\b", ln)
                  for ln in pip_wrapper_lines),
          " | ".join(ln.strip()[:60] for ln in pip_wrapper_lines))
    exec_lines = [ln for ln in code_lines if re.search(r'^\s*"\$VENV/bin/pip"', ln)]
    check("прямой вызов обёртки в скрипте ровно один", len(exec_lines) == 1,
          " | ".join(ln.strip()[:60] for ln in exec_lines))
    check("и он проверочный (--version)",
          bool(exec_lines) and "--version" in exec_lines[0],
          exec_lines[0].strip()[:80] if exec_lines else "нет строки")
    check("проверка `python -m pip` не ослаблена: остались и --version, и check",
          any("-m pip --version" in ln for ln in code_lines)
          and any("-m pip check" in ln for ln in code_lines))

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
    shutil.copytree(WORK / "venv", fake, symlinks=True)
    (fake / "RELEASE_SHA").write_text("0" * 40 + "\n", encoding="utf-8")
    (fake / "INSTALLED").write_text("подложенное окружение\n", encoding="utf-8")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vx], env=deploy_env(app))
    check("выкладка прошла", rc == 0, out[-200:])
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("чужое окружение не подставлено, а пересобрано",
          "# release vx" in live, live.strip())
    check("пересборка действительно была", "install" in (WORK / "pip.log").read_text(encoding="utf-8"))

    print("\n== Переиспользованное кэшированное окружение: без сети и с живым entrypoint ==")
    # Второй путь переезда, и он не теоретический. Отложенное окружение едет
    # venv-<sha> → staging → $VENV, а на проде такие каталоги остались от
    # ПРЕЖНЕЙ версии скрипта — со ссылкой на давно исчезнувший staging внутри.
    # Требование двойное и оба половины одинаково важны: сеть не нужна (иначе
    # рушится D-44, ради которого кэш и заведён) И обёртка после переезда
    # работает.
    git(["checkout", "-q", "main"], cwd=app)
    vcache = add_commit(app, "vcache")
    git(["checkout", "-q", "--detach", vx], cwd=app)
    cached = WORK / f"venv-{vcache}"
    shutil.rmtree(cached, ignore_errors=True)
    shutil.copytree(WORK / "venv", cached, symlinks=True)
    (cached / "RELEASE_SHA").write_text(vcache + "\n", encoding="utf-8")
    (cached / "INSTALLED").write_text("httpx==0.28.1\n# release vcache\n", encoding="utf-8")
    # Обёртка с шебангом на несуществующий staging — ровно то, что оставляла
    # прежняя версия скрипта.
    stale = WORK / ".venv-staging.000000"
    cached_pip = cached / "bin" / "pip"
    cached_pip.write_text(
        f"#!{stale}/bin/python\n"
        + cached_pip.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8")
    cached_pip.chmod(0o755)
    # Соседи, которых починка трогать НЕ имеет права: рабочий чужой шебанг,
    # сломанный НЕ-python шебанг и симлинк.
    (cached / "bin" / "helper.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    (cached / "bin" / "helper.sh").chmod(0o755)
    (cached / "bin" / "чужой").write_text("#!/нет-такого/bin/notpython\n", encoding="utf-8")
    (cached / "bin" / "чужой").chmod(0o755)
    (cached / "bin" / "python3").symlink_to("python")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vcache], env=deploy_env(app))
    check("выкладка на кэшированном окружении прошла", rc == 0 and head_of(app) == vcache,
          out[-300:])
    check("кэш действительно переиспользован, а не пересобран",
          "уже собрано" in out, out[-300:])
    piplog = (WORK / "pip.log").read_text(encoding="utf-8")
    check("СЕТИ НЕ ПОНАДОБИЛОСЬ: пакеты не ставились",
          "install" not in piplog, piplog.strip().replace("\n", " | ") or "пусто")
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("в бою именно кэшированное окружение", "# release vcache" in live, live.strip())
    rc_pip, out_pip = run([str(WORK / "venv" / "bin" / "pip"), "--version"],
                          env=deploy_env(app))
    check("ПРЯМОЙ bin/pip работает и после переезда КЭШИРОВАННОГО окружения",
          rc_pip == 0, f"rc={rc_pip} {out_pip.strip()[:120]}")
    first_line = (WORK / "venv" / "bin" / "pip").read_text(encoding="utf-8").splitlines()[0]
    check("шебанг переписан на финальный путь", first_line == f"#!{WORK / 'venv'}/bin/python",
          first_line)
    check("рабочий чужой шебанг починка не тронула",
          (WORK / "venv" / "bin" / "helper.sh").read_text(encoding="utf-8").splitlines()[0]
          == "#!/bin/sh")
    check("сломанный НЕ-python шебанг починка тоже не тронула — это чужая ответственность",
          (WORK / "venv" / "bin" / "чужой").read_text(encoding="utf-8").splitlines()[0]
          == "#!/нет-такого/bin/notpython")
    check("симлинк остался симлинком, а не стал обычным файлом",
          (WORK / "venv" / "bin" / "python3").is_symlink())
    check("временных файлов починки не осталось",
          not list((WORK / "venv" / "bin").glob("*.oborot-shebang.*")),
          str([p.name for p in (WORK / "venv" / "bin").glob("*.oborot-shebang.*")]))

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

    print("\n== Сбой на ПОСЛЕДНЕМ переименовании подмены окружения ==")
    # Самый тихий сбой из известных, найден повторным внешним ревью 23.08.
    # `mv $VENV -> держатель` и `mv staging -> $VENV` прошли: в бою уже лежит
    # окружение НОВОГО релиза. Падает только третье переименование — то, которым
    # прежнее окружение откладывается под именем venv-<sha>.
    #
    # Пока отметку «окружение подменено» ставила строка у вызывающего, функция
    # успевала вернуть 1 раньше неё, и откат молча пропускал возврат окружения:
    # код и OBOROT_COMMIT возвращались на прежний релиз, а в $VENV оставались
    # библиотеки нового. Снаружи это выглядит как честный отказ выкладки.
    git(["checkout", "-q", "main"], cwd=app)
    vm = add_commit(app, "vm")
    git(["checkout", "-q", "--detach", v2], cwd=app)
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("подготовка: в бою v2", rc == 0 and head_of(app) == v2, out[-200:])
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    before_env = (WORK / "env").read_text(encoding="utf-8")
    prev_venv_note = (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8")
    rc, out = run(["bash", script, vm], env=deploy_env(app, {"FAIL_MV_KEEP": "1"}))
    check("выкладка отклонена", rc != 0, f"rc={rc}")
    check("сказано, что сбой до перезапуска", "СБОЙ ДО ПЕРЕЗАПУСКА" in out, out[-500:])
    check("КОД возвращён на прежний коммит", head_of(app) == v2, head_of(app)[:12])
    check("OBOROT_COMMIT возвращён",
          (WORK / "env").read_text(encoding="utf-8") == before_env,
          (WORK / "env").read_text(encoding="utf-8").strip())
    live = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    check("ОКРУЖЕНИЕ ВЕРНУЛОСЬ, а не осталось от неудачного релиза",
          "# release v2" in live, live.strip())
    check("метка живого окружения тоже прежняя",
          (WORK / "venv" / "RELEASE_SHA").read_text(encoding="utf-8").strip() == v2)
    rc_py, _ = run([str(WORK / "venv" / "bin" / "python"), "-m", "pip", "--version"],
                   env=deploy_env(app))
    check("и окружение в $VENV работоспособно", rc_py == 0, f"rc={rc_py}")
    syslog = (WORK / "systemctl.log").read_text(encoding="utf-8")
    check("СЕРВИС НЕ ПЕРЕЗАПУСКАЛСЯ — потому откат и безопасен",
          "restart" not in syslog, syslog.strip().replace("\n", " | ") or "пусто")
    check("запись о прежнем окружении не переписана неудачной выкладкой",
          (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8") == prev_venv_note)
    leftovers = list(WORK.glob(".venv-staging.*")) + list(WORK.glob(".venv-held.*")) \
        + list(WORK.glob(".venv-rollback.*"))
    check("временных каталогов окружения не осталось", not leftovers, str(leftovers))

    # Общая часть двух инъекций ниже. Требование к обеим одно и то же и взято не
    # из вкуса, а из D-46: сбой в окне «код переключён, служба ещё старая»
    # обязан вернуть прод целиком и не трогать службу. Разница только в том, что
    # именно сломано — сама починка или её результат.
    def check_rolled_back_to(sha, out, rc, before_env, marker):
        check("выкладка отклонена", rc != 0, f"rc={rc}")
        check("сказано, что сбой ДО перезапуска", "СБОЙ ДО ПЕРЕЗАПУСКА" in out, out[-500:])
        check("КОД возвращён на прежний коммит", head_of(app) == sha, head_of(app)[:12])
        check("OBOROT_COMMIT возвращён",
              (WORK / "env").read_text(encoding="utf-8") == before_env,
              (WORK / "env").read_text(encoding="utf-8").strip())
        live_now = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
        check("ОКРУЖЕНИЕ возвращено, а не осталось от неудачного релиза",
              marker in live_now, live_now.strip())
        check("метка живого окружения тоже прежняя",
              (WORK / "venv" / "RELEASE_SHA").read_text(encoding="utf-8").strip() == sha)
        syslog_now = (WORK / "systemctl.log").read_text(encoding="utf-8")
        check("СЛУЖБА НЕ ПЕРЕЗАПУСКАЛАСЬ — потому откат и безопасен",
              "restart" not in syslog_now,
              syslog_now.strip().replace("\n", " | ") or "пусто")
        rc_ok, _ = run([str(WORK / "venv" / "bin" / "python"), "-m", "pip", "--version"],
                       env=deploy_env(app))
        check("вернувшееся окружение работоспособно", rc_ok == 0, f"rc={rc_ok}")
        check("временных файлов починки в $VENV не осталось",
              not list((WORK / "venv" / "bin").glob("*.oborot-shebang.*")),
              str([p.name for p in (WORK / "venv" / "bin").glob("*.oborot-shebang.*")]))
        junk = list(WORK.glob(".venv-staging.*")) + list(WORK.glob(".venv-held.*")) \
            + list(WORK.glob(".venv-rollback.*"))
        check("временных каталогов окружения не осталось", not junk, str(junk))

    print("\n== Инъекция: ПОЧИНКА обёрток не удалась — fail-closed до перезапуска ==")
    # Починка правит файлы внутри живого окружения, значит умеет падать сама.
    # Отказ ровно на том переименовании, которым исправленная обёртка ставится
    # на место: скрипт обязан не идти дальше и вернуть прод на прежний релиз.
    git(["checkout", "-q", "main"], cwd=app)
    vs1 = add_commit(app, "vs1")
    git(["checkout", "-q", "--detach", v2], cwd=app)
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("подготовка: в бою v2", rc == 0 and head_of(app) == v2, out[-250:])
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    before_env = (WORK / "env").read_text(encoding="utf-8")
    rc, out = run(["bash", script, vs1], env=deploy_env(app, {"FAIL_MV_SHEBANG": "1"}))
    check("названа причина — консольные обёртки", "консольные обёртки" in out, out[-500:])
    check_rolled_back_to(v2, out, rc, before_env, "# release v2")

    print("\n== Инъекция: обёртка не запускается на финальном пути — fail-closed ==")
    # Тоньше предыдущей и ближе к настоящему дефекту: починка отработала, шебанг
    # переписан, `python -m pip` в том же окружении отвечает нормально — и
    # только прямой запуск обёртки не работает. Прежний код такое пропускал
    # молча и выкатывал на прод; теперь это отказ до перезапуска.
    git(["checkout", "-q", "main"], cwd=app)
    vs2 = add_commit(app, "vs2")
    git(["checkout", "-q", "--detach", v2], cwd=app)
    rc, out = run(["bash", script, v2], env=deploy_env(app))
    check("подготовка: в бою v2", rc == 0 and head_of(app) == v2, out[-250:])
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    before_env = (WORK / "env").read_text(encoding="utf-8")
    rc, out = run(["bash", script, vs2], env=deploy_env(app, {"FAIL_CONSOLE_PIP": "1"}))
    check("названо, что не запускается именно консольная обёртка",
          "bin/pip не запускается на финальном пути" in out, out[-500:])
    check("а модульная проверка прошла — поймала ИМЕННО новая, а не старая",
          "-m pip не работает" not in out, out[-500:])
    check_rolled_back_to(v2, out, rc, before_env, "# release v2")

    print("\n== Уборка не трогает окружение, которым делается откат ==")
    # Отложенное окружение переезжает переименованием и сохраняет СТАРЫЙ mtime:
    # при сортировке по времени оно оказывается в конце списка. Уборка стояла до
    # перезапуска и при небольшом keep удаляла ровно тот каталог, на который сам
    # скрипт указывает в подсказке про откат.
    git(["checkout", "-q", "main"], cwd=app)
    vp = add_commit(app, "vp")
    # Возвращаем рабочую копию на v2: цель отката — окружение ТОГО релиза,
    # который сейчас в бою, а add_commit оставляет копию на вершине main.
    git(["checkout", "-q", "--detach", v2], cwd=app)
    old = time.time() - 90 * 24 * 3600
    os.utime(WORK / "venv", (old, old))       # живое окружение «старое»
    decoys = []
    for i in (1, 2, 3):
        d = WORK / ("venv-" + f"{i:040x}")     # имена как у настоящих: venv-<sha>
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(WORK / "venv", d, symlinks=True)
        os.utime(d, None)                      # ...а эти — свежие
        decoys.append(d)
    rc, out = run(["bash", script, vp], env=deploy_env(app, {"OBOROT_VENV_KEEP": "1"}))
    check("выкладка прошла", rc == 0 and head_of(app) == vp, out[-250:])
    target = WORK / f"venv-{v2}"
    check("ЦЕЛЬ ОТКАТА ПЕРЕЖИЛА УБОРКУ, хотя её mtime самый старый",
          target.is_dir(), str(sorted(p.name[:16] for p in WORK.glob("venv-*"))))
    rc_py, _ = run([str(target / "bin" / "python"), "-m", "pip", "--version"],
                   env=deploy_env(app))
    check("и это работоспособное окружение, а не пустой каталог", rc_py == 0,
          f"rc={rc_py}")
    check("окружение прежнего релиза — именно прежнего",
          (target / "RELEASE_SHA").exists()
          and (target / "RELEASE_SHA").read_text(encoding="utf-8").strip() == v2)
    check("уборка при этом всё-таки была: свежие каталоги удалены",
          not any(d.exists() for d in decoys),
          str([d.name[:16] for d in decoys if d.exists()]))
    check("хранится ровно столько окружений, сколько велено",
          len(list(WORK.glob("venv-*"))) == 1,
          str(sorted(p.name[:16] for p in WORK.glob("venv-*"))))

    print("\n== Пока прод не поднялся, уборки нет вовсе ==")
    # Граница та же, что у отката: до успешного health-check прод может
    # вернуться на прежний релиз, и окружение для этого возврата обязано быть на
    # месте. Лишний каталог на диске дешевле отката без окружения.
    git(["checkout", "-q", "main"], cwd=app)
    vq = add_commit(app, "vq")
    git(["checkout", "-q", "--detach", vp], cwd=app)
    decoys = []
    for i in (4, 5, 6):
        d = WORK / ("venv-" + f"{i:040x}")
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(WORK / "venv", d, symlinks=True)
        os.utime(d, None)
        decoys.append(d)
    make_stubs(WORK / "stub-bin", health_ok=False)
    rc, out = run(["bash", script, vq], env=deploy_env(app, {"OBOROT_VENV_KEEP": "1"}))
    make_stubs(WORK / "stub-bin")
    check("выход 1", rc == 1, f"rc={rc}")
    check("СТАРЫЕ ОКРУЖЕНИЯ НЕ УБРАНЫ: до успешного health-check уборки нет",
          all(d.exists() for d in decoys),
          str([d.name[:16] for d in decoys if not d.exists()]))
    check("окружение прежнего релиза цело — ровно как обещает вывод",
          (WORK / f"venv-{vp}").is_dir(),
          str(sorted(p.name[:16] for p in WORK.glob("venv-*"))))
    check("и в подсказке назван именно этот каталог", f"venv-{vp}" in out, out[-300:])
    for d in decoys:
        shutil.rmtree(d, ignore_errors=True)
    # Площадку возвращаем в согласованное состояние: прод остался на vq с его
    # окружением, но сервис так и не поднялся. Выкатываем vq заново с рабочим
    # health-check, чтобы дальше проверять другое, а не последствия этого.
    rc, out = run(["bash", script, vq], env=deploy_env(app))
    check("повторная выкладка на исправном сервисе проходит", rc == 0, out[-250:])

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

    # Два раздела ниже добавлены correctivе-циклом по внешнему ревью и стоят
    # последними намеренно: они оставляют площадку в другом релизе, и разделы
    # выше пришлось бы приводить в согласованное состояние ради порядка чтения.
    # После них площадка сразу разбирается, поэтому убирать за собой нечего.
    print("\n== Чужой, но ЖИВОЙ интерпретатор в шебанге ==")
    # Худший случай, и первая версия правила его пропускала: шебанг указывает не
    # в никуда, а на НАСТОЯЩИЙ, запускаемый интерпретатор соседнего релиза.
    # Обёртка при этом работает — и потому не выглядит сломанной ничем, — но
    # исполняется чужим окружением: чужие библиотеки, чужие версии.
    #
    # Сосед здесь не выдуман: `venv-<CURRENT>` создаёт сама подмена окружения,
    # он же цель отката, и уборка его не трогает. На сервере рядом живут ещё и
    # отложенные окружения прежних релизов — `OBOROT_VENV_KEEP` их и держит.
    git(["checkout", "-q", "main"], cwd=app)
    vlive = add_commit(app, "vlive")
    git(["checkout", "-q", "--detach", v3], cwd=app)
    foreign = WORK / f"venv-{v3}"
    cached_live = WORK / f"venv-{vlive}"
    shutil.rmtree(cached_live, ignore_errors=True)
    shutil.copytree(WORK / "venv", cached_live, symlinks=True)
    (cached_live / "RELEASE_SHA").write_text(vlive + "\n", encoding="utf-8")
    (cached_live / "INSTALLED").write_text("httpx==0.28.1\n# release vlive\n",
                                           encoding="utf-8")
    live_pip = cached_live / "bin" / "pip"
    live_pip.write_text(
        f"#!{foreign}/bin/python\n"
        + live_pip.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8")
    live_pip.chmod(0o755)
    (WORK / "pip.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vlive], env=deploy_env(app))
    check("выкладка прошла", rc == 0 and head_of(app) == vlive, out[-300:])
    check("сеть не понадобилась: кэшированное окружение переиспользовано",
          "install" not in (WORK / "pip.log").read_text(encoding="utf-8"))
    # Без этой проверки весь раздел ничего не значит: если сосед не пережил
    # выкладку, случай выродился бы в прежний «пути больше нет».
    check("ЧУЖОЙ интерпретатор при этом жив и запускается",
          (foreign / "bin" / "python").exists()
          and run([str(foreign / "bin" / "python"), "-m", "pip", "--version"],
                  env=deploy_env(app))[0] == 0,
          str(foreign.name[:16]))
    first_line = (WORK / "venv" / "bin" / "pip").read_text(encoding="utf-8").splitlines()[0]
    check("шебанг приведён к интерпретатору ЭТОГО окружения, а не оставлен чужим",
          first_line == f"#!{WORK / 'venv'}/bin/python", first_line)
    rc_pip, out_pip = run([str(WORK / "venv" / "bin" / "pip"), "--version"],
                          env=deploy_env(app))
    check("и обёртка ИСПОЛНЯЕТСЯ окружением этого релиза, а не соседнего",
          rc_pip == 0
          and os.path.realpath(out_pip.strip()) == os.path.realpath(WORK / "venv"),
          f"rc={rc_pip} {out_pip.strip()[:120]}")

    print("\n== Починка отчиталась об успехе, а шебанг остался чужим ==")
    # Проверка обёрток обязана быть независимой от починки, а не пересказывать
    # её отчёт. Подставной mv тихо не выполняет переименование и возвращает 0:
    # починка считает, что справилась, файл при этом прежний. Поймать это может
    # только проверка — и обязана поймать ДО перезапуска службы.
    git(["checkout", "-q", "main"], cwd=app)
    vsil = add_commit(app, "vsil")
    git(["checkout", "-q", "--detach", vlive], cwd=app)
    foreign2 = WORK / f"venv-{vlive}"
    cached_sil = WORK / f"venv-{vsil}"
    shutil.rmtree(cached_sil, ignore_errors=True)
    shutil.copytree(WORK / "venv", cached_sil, symlinks=True)
    (cached_sil / "RELEASE_SHA").write_text(vsil + "\n", encoding="utf-8")
    (cached_sil / "INSTALLED").write_text("httpx==0.28.1\n# release vsil\n",
                                          encoding="utf-8")
    sil_pip = cached_sil / "bin" / "pip"
    sil_pip.write_text(
        f"#!{foreign2}/bin/python\n"
        + sil_pip.read_text(encoding="utf-8").split("\n", 1)[1], encoding="utf-8")
    sil_pip.chmod(0o755)
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    before_env = (WORK / "env").read_text(encoding="utf-8")
    rc, out = run(["bash", script, vsil], env=deploy_env(app, {"FAIL_MV_SILENT": "1"}))
    check("названо, что обёртка указывает не на интерпретатор этого окружения",
          "указывают не на" in out, out[-500:])
    check_rolled_back_to(vlive, out, rc, before_env, "# release vlive")

    # ------------------------------------------------------------------
    # OPS-8: драйвер выкладки и повторная выкладка того же коммита.
    # ------------------------------------------------------------------
    print("\n== Площадка честна: подставной репозиторий несёт свой драйвер ==")
    # Без этого раздела всё, что ниже, ничего не стоит: «старого драйвера не
    # видно в выводе» стало бы правдой просто потому, что пометка никуда не
    # попала, а «выкладка прошла» — потому что старый драйвер ничем не отличался.
    real_driver = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    check("драйвер лежит в самом репозитории, как на сервере",
          (app / "deploy" / "deploy.sh").read_text(encoding="utf-8") == real_driver)
    check("якорь для пометки в драйвере есть", OLD_DRIVER_ANCHOR in real_driver)
    marked = old_driver(real_driver, sabotage=True)
    check("пометка попала в текст старого драйвера, и он отличается от актуального",
          OLD_DRIVER_MARK in marked and marked != real_driver)
    check("пометка стоит ПОСЛЕ начала работы скрипта, а не в первой строке файла",
          marked.index(OLD_DRIVER_MARK) > marked.index("== 1/"))

    print("\n== Драйвер: на диске старый, в origin/main актуальный ==")
    # Настоящая форма первого дефекта OPS-8. Скрипт обновляется тем же
    # checkout'ом, который сам же и выполняет, поэтому первая выкладка после
    # правки драйвера доигрывалась ПРЕЖНЕЙ реализацией — со всеми проверками,
    # которых в ней ещё нет. Здесь старый драйвер вдобавок отказывает сразу
    # после пометки: выкладка проходит только если его тело не исполнялось.
    git(["checkout", "-q", "main"], cwd=app)
    vold = commit_driver(app, "vold", old_driver(real_driver, sabotage=True))
    git(["checkout", "-q", "main"], cwd=app)
    vnew = commit_driver(app, "vnew", real_driver)
    git(["checkout", "-q", "--detach", vold], cwd=app)
    check("на диске действительно старый драйвер",
          OLD_DRIVER_MARK in (app / "deploy" / "deploy.sh").read_text(encoding="utf-8"))
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", str(app / "deploy" / "deploy.sh"), vnew], env=deploy_env(app))
    check("ВЫКЛАДКА ВЫПОЛНЕНА, хотя на диске лежал старый драйвер", rc == 0,
          f"rc={rc} " + out[-400:])
    check("старый драйвер не исполнялся вовсе", OLD_DRIVER_MARK not in out, out[-400:])
    check("код переключён на целевой коммит", head_of(app) == vnew, head_of(app)[:12])
    syslog = (WORK / "systemctl.log").read_text(encoding="utf-8")
    check("служба перезапущена РОВНО ОДИН раз — второй полосы деплоя не появилось",
          syslog.count("restart") == 1, syslog.strip().replace("\n", " | ") or "пусто")
    piplog = (WORK / "pip.log").read_text(encoding="utf-8")
    check("окружение собрано один раз, а не дважды",
          len([ln for ln in piplog.splitlines() if ln.startswith("install")]) == 1,
          piplog.strip().replace("\n", " | ") or "пусто")
    check("временный файл драйвера убран за собой",
          not list((WORK / "state").glob(".deploy-driver.*")),
          str([p.name for p in (WORK / "state").glob(".deploy-driver.*")]))
    rc_pip, out_pip = run([str(WORK / "venv" / "bin" / "pip"), "--version"],
                          env=deploy_env(app))
    check("проверки актуального драйвера отработали: обёртка запускается",
          rc_pip == 0, f"rc={rc_pip} {out_pip.strip()[:120]}")

    print("\n== Откат на коммит со старым драйвером драйвер не понижает ==")
    # После отката на диске снова окажется старый драйвер — это неизбежно и
    # нормально. Требование в другом: сам откат обязан исполняться актуальным
    # драйвером, а следующая выкладка — снова подняться до актуального.
    rc, out = run(["bash", str(app / "deploy" / "deploy.sh"), vold], env=deploy_env(app))
    check("откат выполнен", rc == 0 and head_of(app) == vold, f"rc={rc} " + out[-300:])
    check("исполнялся актуальный драйвер, а не тот, что лежит в целевом коммите",
          OLD_DRIVER_MARK not in out, out[-400:])
    check("на диске после отката действительно старый драйвер — иначе проверка ниже пуста",
          OLD_DRIVER_MARK in (app / "deploy" / "deploy.sh").read_text(encoding="utf-8"))
    rc, out = run(["bash", str(app / "deploy" / "deploy.sh"), vnew], env=deploy_env(app))
    check("СЛЕДУЮЩАЯ выкладка с понижённого диска снова идёт актуальным драйвером",
          rc == 0 and OLD_DRIVER_MARK not in out and head_of(app) == vnew,
          f"rc={rc} " + out[-400:])

    print("\n== Подстановка драйвера не зацикливается ==")
    # Счётчик глубины уже израсходован, а драйвер всё ещё расходится. Единственно
    # правильный исход — отказ до любых изменений: новый круг подстановки в этом
    # состоянии означал бы бесконечный цикл на боевом сервере.
    git(["checkout", "-q", "main"], cwd=app)
    vquiet = commit_driver(app, "vquiet", old_driver(real_driver, sabotage=False))
    git(["checkout", "-q", "main"], cwd=app)
    vnew2 = commit_driver(app, "vnew2", real_driver)
    git(["checkout", "-q", "--detach", vquiet], cwd=app)
    head_before = head_of(app)
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", str(app / "deploy" / "deploy.sh"), vnew2],
                  env=deploy_env(app, {"OBOROT_DEPLOY_BOOTSTRAP_DEPTH": "1"}))
    check("вторая несходимость драйвера — отказ, а не новый круг", rc != 0, f"rc={rc}")
    check("названа причина", "драйвер не сошёлся" in out, out[-400:])
    check("до мутаций дело не дошло: код не переключён", head_of(app) == head_before,
          head_of(app)[:12])
    check("и служба не тронута",
          "restart" not in (WORK / "systemctl.log").read_text(encoding="utf-8"))
    check("временный файл драйвера убран и в этом случае",
          not list((WORK / "state").glob(".deploy-driver.*")),
          str([p.name for p in (WORK / "state").glob(".deploy-driver.*")]))

    print("\n== Две одновременные выкладки после подстановки сходятся на одном локе ==")
    # Требование внешнего ревью к этому пакету: подстановка драйвера стоит ДО
    # блокировки, значит надо доказать, что от неё не появляется второй полосы
    # деплоя. `exec` заменяет процесс (PID тот же), поэтому лок берёт ровно
    # один — а проигравшая выкладка обязана не тронуть ни код, ни env, ни
    # окружение, ни базу, ни службу.
    #
    # Времени в этой сцене нет вовсе: первая выкладка останавливается воротами
    # на сборке окружения — она уже ДЕРЖИТ лок, но ещё НЕ делала `git checkout`,
    # то есть на диске по-прежнему старый драйвер. Только тогда запускается
    # вторая, и подставить себе актуальный драйвер ей придётся по-настоящему.
    git(["checkout", "-q", "--detach", vquiet], cwd=app)
    check("подготовка: на диске старый драйвер",
          OLD_DRIVER_MARK in (app / "deploy" / "deploy.sh").read_text(encoding="utf-8"))
    # Окружение цели убирается намеренно: с готовым кэшем сборки не будет, а
    # значит не будет и ворот — сцена молча выродилась бы в последовательную.
    shutil.rmtree(WORK / f"venv-{vnew2}", ignore_errors=True)
    gate = WORK / "build-gate"
    gate_reached = WORK / "build-gate.reached"
    for g in (gate, gate_reached):
        if g.exists():
            g.unlink()
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    gate_env = deploy_env(app, {"PIP_GATE": str(gate)})
    first = subprocess.Popen(["bash", str(app / "deploy" / "deploy.sh"), vnew2],
                             env=gate_env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    out1, rc1 = "", None
    try:
        check("первая выкладка взяла лок и остановлена ДО git checkout",
              wait_for(gate_reached, 90))
        check("а на диске всё ещё старый драйвер — второй придётся подставлять",
              OLD_DRIVER_MARK in (app / "deploy" / "deploy.sh").read_text(encoding="utf-8"))
        rc2, out2 = run(["bash", str(app / "deploy" / "deploy.sh"), vnew2],
                        env=gate_env, timeout=90)
    finally:
        gate.write_text("иди\n", encoding="utf-8")
        try:
            out1 = first.communicate(timeout=120)[0] or ""
        except subprocess.TimeoutExpired:
            first.kill()
            out1 = first.communicate()[0] or ""
        rc1 = first.returncode
    check("первая выкладка завершилась успешно", rc1 == 0, f"rc={rc1} " + out1[-300:])
    check("первая шла актуальным драйвером, а не тем, что на диске",
          OLD_DRIVER_MARK not in out1, out1[-300:])
    check("ВТОРАЯ отклонена именно локом", rc2 != 0 and "другой деплой" in out2,
          f"rc={rc2} " + out2[-300:])
    check("и до лока она дошла УЖЕ подставленным драйвером",
          "перезапускаюсь на нём" in out2, out2[-400:])
    syslog = (WORK / "systemctl.log").read_text(encoding="utf-8")
    check("служба перезапущена РОВНО ОДИН раз", syslog.count("restart") == 1,
          syslog.strip().replace("\n", " | ") or "пусто")
    # Считается по выводу каждого процесса, а не по именам файлов в каталоге.
    # Имя копии — `oborot-<дата>-<время>.db` с точностью до секунды, и в CI весь
    # этот набор укладывается в десяток секунд: две копии подряд попадают в одну
    # секунду и вторая перезаписывает первую. Сравнение множества имён на таком
    # прогоне доказывало бы не «копия одна», а «часы медленнее набора».
    check("копию базы сняла только победившая, и ровно одну",
          out1.count("копия: ") == 1 and "копия: " not in out2,
          f"первая={out1.count('копия: ')} вторая={out2.count('копия: ')}")
    piplog = (WORK / "pip.log").read_text(encoding="utf-8")
    check("окружение собрано ровно один раз",
          len([ln for ln in piplog.splitlines() if ln.startswith("install")]) == 1,
          piplog.strip().replace("\n", " | ") or "пусто")
    check("проигравшая ничего не тронула: код и OBOROT_COMMIT — от победившей",
          head_of(app) == vnew2
          and f"OBOROT_COMMIT={vnew2}" in (WORK / "env").read_text(encoding="utf-8"),
          head_of(app)[:12])
    check("временных файлов драйвера не осталось ни от одной из двух",
          not list((WORK / "state").glob(".deploy-driver.*")),
          str([p.name for p in (WORK / "state").glob(".deploy-driver.*")]))
    for g in (gate, gate_reached):
        if g.exists():
            g.unlink()

    print("\n== Отказ git fetch останавливает выкладку до любых мутаций ==")
    # Подстановка драйвера добавила выкладке новую внешнюю причину падать, и
    # цена этого решения проверяется здесь: худший исход обязан быть «выкладка
    # не начата», а не «прод в промежуточном состоянии».
    git(["checkout", "-q", "--detach", vquiet], cwd=app)
    rc, out = run(["bash", script, vnew2], env=deploy_env(app))
    check("подготовка: в бою vnew2", rc == 0 and head_of(app) == vnew2, out[-250:])
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    head_before = head_of(app)
    env_before = (WORK / "env").read_text(encoding="utf-8")
    live_before = (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8")
    os.rename(WORK / "origin.git", WORK / "origin.git.hidden")
    try:
        rc, out = run(["bash", script, vquiet], env=deploy_env(app))
    finally:
        os.rename(WORK / "origin.git.hidden", WORK / "origin.git")
    check("без origin выкладка не начинается", rc != 0, f"rc={rc}")
    check("код не переключён", head_of(app) == head_before, head_of(app)[:12])
    check("OBOROT_COMMIT не тронут",
          (WORK / "env").read_text(encoding="utf-8") == env_before)
    check("живое окружение не тронуто",
          (WORK / "venv" / "INSTALLED").read_text(encoding="utf-8") == live_before)
    check("служба не перезапускалась",
          "restart" not in (WORK / "systemctl.log").read_text(encoding="utf-8"))
    check("пакеты не ставились",
          "install" not in (WORK / "pip.log").read_text(encoding="utf-8"))
    check("временный файл драйвера не остался",
          not list((WORK / "state").glob(".deploy-driver.*")),
          str([p.name for p in (WORK / "state").glob(".deploy-driver.*")]))

    print("\n== Сигнал в окне до перезапуска: полный откат, а не брошенный прод ==")
    # SIGTERM (обрыв ssh, `systemctl stop` соседнего юнита, Ctrl-C) убивал bash
    # мимо всех трапов: откат не делался, и прод оставался в состоянии «код
    # новый, окружение новое, служба старая и живая» — ровно в том, против
    # которого написан D-46, и незаметном снаружи.
    git(["checkout", "-q", "main"], cwd=app)
    vsig = add_commit(app, "vsig")
    git(["checkout", "-q", "--detach", vnew2], cwd=app)
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    before_env = (WORK / "env").read_text(encoding="utf-8")
    sentinel = WORK / "signal-once"
    if sentinel.exists():
        sentinel.unlink()
    rc, out = run(["bash", script, vsig], env=deploy_env(app, {
        "FAIL_MV_SIGNAL": "1", "FAIL_MV_SIGNAL_ONCE": str(sentinel)}))
    check("сигнал действительно доставлен — иначе раздел не проверяет ничего",
          sentinel.exists())
    check("сказано, что это сигнал", "сигнал" in out, out[-500:])
    check_rolled_back_to(vnew2, out, rc, before_env, "# release vnew2")

    print("\n== Повторная выкладка того же коммита: цель отката не затирается ==")
    # Второй дефект OPS-8 целиком. Повторный заход тем же SHA штатен — им
    # доигрывают выкладку, начатую старым драйвером, — и прежний код на нём
    # записывал цель отката равной выкладываемому релизу.
    git(["checkout", "-q", "main"], cwd=app)
    vprev = add_commit(app, "vprev")
    git(["checkout", "-q", "main"], cwd=app)
    vcur = add_commit(app, "vcur")
    git(["checkout", "-q", "--detach", vnew2], cwd=app)
    rc, out = run(["bash", script, vprev], env=deploy_env(app))
    check("подготовка: выкатили vprev", rc == 0 and head_of(app) == vprev, out[-250:])
    rc, out = run(["bash", script, vcur], env=deploy_env(app))
    check("подготовка: выкатили vcur", rc == 0 and head_of(app) == vcur, out[-250:])
    prev_sha = (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip()
    prev_venv = (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip()
    check("подготовка: цель отката — vprev", prev_sha == vprev, prev_sha[:12])
    check("подготовка: окружение отката рядом и работоспособно",
          Path(prev_venv).is_dir()
          and run([str(Path(prev_venv) / "bin" / "python"), "-m", "pip", "--version"],
                  env=deploy_env(app))[0] == 0, prev_venv)

    # Ровно то состояние, ради которого повторный заход и делают: обёртка в
    # живом окружении испорчена, всё остальное на месте.
    live_pip = WORK / "venv" / "bin" / "pip"
    live_pip.write_text(f"#!{WORK / 'нет-такого'}/bin/python\n"
                        + live_pip.read_text(encoding="utf-8").split("\n", 1)[1],
                        encoding="utf-8")
    live_pip.chmod(0o755)
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    backups_before = sorted(p.name for p in (WORK / "data" / "backups").glob("oborot-*.db"))
    rc, out = run(["bash", script, vcur], env=deploy_env(app))
    check("повторная выкладка того же коммита прошла", rc == 0, f"rc={rc} " + out[-400:])
    check("PREVIOUS_SHA НЕ ЗАТЁРТ выкладываемым коммитом",
          (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip() == vprev,
          (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip()[:12])
    check("PREVIOUS_VENV по-прежнему указывает на окружение прежнего релиза",
          (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip() == prev_venv,
          (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip())
    check("подсказка называет РЕАЛЬНЫЙ прежний релиз",
          f"deploy/deploy.sh {vprev}" in out, out[-300:])
    check("и не предлагает откат на самого себя",
          f"deploy/deploy.sh {vcur}" not in out, out[-300:])
    check("СЛУЖБА НЕ ПЕРЕЗАПУСКАЛАСЬ: менять было нечего",
          "restart" not in (WORK / "systemctl.log").read_text(encoding="utf-8"),
          (WORK / "systemctl.log").read_text(encoding="utf-8").strip() or "пусто")
    check("КОПИЯ БАЗЫ НЕ СНИМАЛАСЬ: ни код, ни env, ни библиотеки не менялись",
          "копия: " not in out
          and sorted(p.name for p in (WORK / "data" / "backups").glob("oborot-*.db"))
          == backups_before, out[-300:])
    check("ОКРУЖЕНИЕ НЕ ПЕРЕСОБИРАЛОСЬ",
          "install" not in (WORK / "pip.log").read_text(encoding="utf-8"),
          (WORK / "pip.log").read_text(encoding="utf-8").strip().replace("\n", " | ")
          or "пусто")
    check("а испорченная обёртка при этом ПОЧИНЕНА и запускается",
          run([str(live_pip), "--version"], env=deploy_env(app))[0] == 0)
    check("шебанг приведён к интерпретатору живого окружения",
          live_pip.read_text(encoding="utf-8").splitlines()[0]
          == f"#!{WORK / 'venv'}/bin/python",
          live_pip.read_text(encoding="utf-8").splitlines()[0])
    check("окружение отката цело", Path(prev_venv).is_dir(), prev_venv)

    print("\n== Уборка не может удалить цель отката и при повторной выкладке ==")
    # Тот же дефект, что уборка ловила раньше, но в его новой форме: при
    # повторной выкладке `venv-$CURRENT` и «отложенное этой выкладкой» указывают
    # на один и тот же каталог текущего релиза, а настоящая цель отката остаётся
    # без защиты — и уезжает первой, потому что её mtime самый старый.
    decoys = []
    for i in (11, 12, 13):
        d = WORK / ("venv-" + f"{i:040x}")
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(WORK / "venv", d, symlinks=True)
        os.utime(d, None)
        decoys.append(d)
    old = time.time() - 90 * 24 * 3600
    os.utime(Path(prev_venv), (old, old))
    rc, out = run(["bash", script, vcur], env=deploy_env(app, {"OBOROT_VENV_KEEP": "1"}))
    check("повторная выкладка прошла", rc == 0, f"rc={rc} " + out[-300:])
    check("ЦЕЛЬ ОТКАТА ПЕРЕЖИЛА УБОРКУ, хотя её mtime самый старый",
          Path(prev_venv).is_dir(),
          str(sorted(p.name[:16] for p in WORK.glob("venv-*"))))
    check("и это работоспособное окружение, а не пустой каталог",
          Path(prev_venv).is_dir()
          and run([str(Path(prev_venv) / "bin" / "python"), "-m", "pip", "--version"],
                  env=deploy_env(app))[0] == 0)
    check("уборка при этом всё-таки была: свежие каталоги удалены",
          not any(d.exists() for d in decoys),
          str([d.name[:16] for d in decoys if d.exists()]))

    print("\n== Повторная выкладка при негодном окружении: полный путь, маркеры целы ==")
    # Метка окружения врёт про релиз — починкой обёрток такое не лечится, и
    # маршрут обязан стать обычным: пересборка, подмена, перезапуск. Правило про
    # маркеры отката при этом остаётся ровно тем же.
    (WORK / "venv" / "RELEASE_SHA").write_text("0" * 40 + "\n", encoding="utf-8")
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    (WORK / "pip.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vcur], env=deploy_env(app))
    check("выкладка прошла", rc == 0, f"rc={rc} " + out[-400:])
    # Проверяется результат, а не способ: годное окружение могло и найтись в
    # кэше. Важно, что в $VENV снова лежит окружение с ВЕРНОЙ меткой релиза, —
    # то есть маршрут был полным, с подменой, а не «починили строку и ушли».
    check("окружение подменено заново — метка снова верна",
          (WORK / "venv" / "RELEASE_SHA").read_text(encoding="utf-8").strip() == vcur,
          (WORK / "venv" / "RELEASE_SHA").read_text(encoding="utf-8").strip()[:12])
    check("служба перезапущена: окружение действительно менялось",
          "restart" in (WORK / "systemctl.log").read_text(encoding="utf-8"))
    check("PREVIOUS_SHA всё равно не затёрт",
          (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip() == vprev,
          (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip()[:12])
    check("PREVIOUS_VENV всё равно не затёрт",
          (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip() == prev_venv,
          (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip())
    check("и подсказка про откат по-прежнему честна",
          f"deploy/deploy.sh {vprev}" in out, out[-300:])
    check("окружение отката цело и здесь", Path(prev_venv).is_dir(), prev_venv)

    print("\n== Повторная выкладка: сбой проверки обёрток — отказ без перезапуска ==")
    # Маршрут «только починка и проверка» обязан быть таким же fail-closed, как
    # обычный: проверка не прошла — служба не трогается, маркеры отката не
    # трогаются, и об этом сказано вслух.
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    before_env = (WORK / "env").read_text(encoding="utf-8")
    rc, out = run(["bash", script, vcur], env=deploy_env(app, {"FAIL_CONSOLE_PIP": "1"}))
    check("выкладка отклонена", rc != 0, f"rc={rc}")
    check("названо, что не запускается именно консольная обёртка",
          "bin/pip не запускается на финальном пути" in out, out[-500:])
    check("СЛУЖБА НЕ ПЕРЕЗАПУСКАЛАСЬ",
          "restart" not in (WORK / "systemctl.log").read_text(encoding="utf-8"),
          (WORK / "systemctl.log").read_text(encoding="utf-8").strip() or "пусто")
    check("OBOROT_COMMIT не тронут",
          (WORK / "env").read_text(encoding="utf-8") == before_env)
    check("маркеры отката не тронуты",
          (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip() == vprev
          and (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip()
          == prev_venv)
    check("код остался на том же коммите", head_of(app) == vcur, head_of(app)[:12])

    # ------------------------------------------------------------------
    # OPS-8 corrective #1: то, что нашло внешнее ревью на HEAD e17002e.
    # ------------------------------------------------------------------
    running_commit = WORK / "running-commit"

    print("\n== Неудачная готовность не разводит PREVIOUS_SHA и PREVIOUS_VENV ==")
    # Последовательность Z → A → неудавшийся B. Прежний код записывал
    # PREVIOUS_VENV=venv-A ДО перезапуска, а PREVIOUS_SHA — только ПОСЛЕ
    # успешной готовности. Значит после провала готовности на диске оставалась
    # пара «откатывайся на Z, окружение вот от A»: два маркера про разные
    # релизы. Повтор той же цели читал эту пару целиком и печатал Z, теряя
    # настоящего предшественника A.
    git(["checkout", "-q", "main"], cwd=app)
    vz = add_commit(app, "vz")
    git(["checkout", "-q", "main"], cwd=app)
    va = add_commit(app, "va")
    git(["checkout", "-q", "main"], cwd=app)
    vb = add_commit(app, "vb")
    git(["checkout", "-q", "--detach", vcur], cwd=app)
    rc, out = run(["bash", script, vz], env=deploy_env(app))
    check("подготовка: выкатили Z", rc == 0 and head_of(app) == vz, out[-250:])
    rc, out = run(["bash", script, va], env=deploy_env(app))
    check("подготовка: выкатили A, цель отката — Z", rc == 0
          and (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip() == vz,
          out[-250:])
    make_stubs(WORK / "stub-bin", health_ok=False)
    rc, out = run(["bash", script, vb], env=deploy_env(app))
    make_stubs(WORK / "stub-bin")
    check("выкладка B отклонена по готовности", rc == 1, f"rc={rc}")
    check("но служба уже перезапущена — откат назад делает человек",
          running_commit.read_text(encoding="utf-8").strip() == vb,
          running_commit.read_text(encoding="utf-8").strip()[:12])
    pair_sha = (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip()
    pair_venv = (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip()
    check("PREVIOUS_SHA после провала готовности — НАСТОЯЩИЙ предшественник A",
          pair_sha == va, f"{pair_sha[:12]} (ожидали {va[:12]})")
    check("PREVIOUS_VENV — окружение того же самого A", pair_venv == str(WORK / f"venv-{va}"),
          pair_venv)
    check("ПАРА СОГЛАСОВАНА: метка внутри каталога совпадает с записанным SHA",
          Path(pair_venv, "RELEASE_SHA").exists()
          and Path(pair_venv, "RELEASE_SHA").read_text(encoding="utf-8").strip() == pair_sha,
          f"{pair_sha[:12]} vs {pair_venv}")
    # Повтор той же цели: подсказка обязана назвать A, а не Z.
    rc, out = run(["bash", script, vb], env=deploy_env(app))
    check("повтор той же цели прошёл", rc == 0, f"rc={rc} " + out[-300:])
    check("откат назван на A — настоящего предшественника не потеряли",
          f"deploy/deploy.sh {va}" in out, out[-300:])
    check("и НЕ на Z, который к этому релизу отношения уже не имеет",
          f"deploy/deploy.sh {vz}" not in out, out[-300:])
    check("пара маркеров повтором не тронута",
          (WORK / "state" / "PREVIOUS_SHA").read_text(encoding="utf-8").strip() == va
          and (WORK / "state" / "PREVIOUS_VENV").read_text(encoding="utf-8").strip()
          == str(WORK / f"venv-{va}"))

    print("\n== Обрыв между двумя записями маркеров не склеивает чужую пару ==")
    # Два файла атомарно не записать: обрыв между ними оставляет SHA от одного
    # релиза и путь от другого. Склеивать их нельзя — иначе скрипт сам сочинит
    # несуществующий релиз. Годность определяется меткой ВНУТРИ каталога, ровно
    # как в D-44, и по ней же пара восстанавливается.
    (WORK / "state" / "PREVIOUS_SHA").write_text(va + "\n", encoding="utf-8")
    (WORK / "state" / "PREVIOUS_VENV").write_text(str(WORK / f"venv-{vz}") + "\n",
                                                  encoding="utf-8")
    decoys = []
    for i in (21, 22, 23):
        d = WORK / ("venv-" + f"{i:040x}")
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(WORK / "venv", d, symlinks=True)
        os.utime(d, None)
        decoys.append(d)
    old = time.time() - 90 * 24 * 3600
    os.utime(WORK / f"venv-{va}", (old, old))
    rc, out = run(["bash", script, vb], env=deploy_env(app, {"OBOROT_VENV_KEEP": "1"}))
    check("выкладка прошла", rc == 0, f"rc={rc} " + out[-300:])
    check("откат по-прежнему назван на A", f"deploy/deploy.sh {va}" in out, out[-300:])
    check("сказано, что запись не сошлась и окружение найдено по метке",
          "по метке" in out, out[-400:])
    check("ОКРУЖЕНИЕ A ПЕРЕЖИЛО УБОРКУ — защита досталась ему, а не чужому каталогу",
          (WORK / f"venv-{va}").is_dir(),
          str(sorted(p.name[:16] for p in WORK.glob("venv-*"))))
    check("и оно работоспособно",
          run([str(WORK / f"venv-{va}" / "bin" / "python"), "-m", "pip", "--version"],
              env=deploy_env(app))[0] == 0)
    check("уборка при этом была: приманки удалены",
          not any(d.exists() for d in decoys),
          str([d.name[:16] for d in decoys if d.exists()]))

    print("\n== Сигнал ВНУТРИ самого перезапуска не бросает выкладку недоделанной ==")
    # Шов, найденный ревью: отметка «перезапуск начат» становится видимой
    # обработчику раньше, чем bash входит в `systemctl restart`. Сигнал в этом
    # промежутке пропускал обязательный откат, а сигнал во время самого
    # перезапуска бросал выкладку на полпути — служба уже перезапущена, а
    # скрипт отчитался отказом. Шов закрыт не отметкой, а тем, что на время
    # перехода сигналы ИГНОРИРУЮТСЯ: обработчику неоткуда увидеть отметку рано,
    # потому что он в этом окне не запускается вовсе.
    git(["checkout", "-q", "main"], cwd=app)
    vsg = add_commit(app, "vsg")
    git(["checkout", "-q", "--detach", vb], cwd=app)
    rc, out = run(["bash", script, vb], env=deploy_env(app))
    check("подготовка: в бою B", rc == 0 and head_of(app) == vb, out[-250:])
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    once = WORK / "restart-signal-once"
    if once.exists():
        once.unlink()
    rc, out = run(["bash", script, vsg],
                  env=deploy_env(app, {"SIGNAL_ON_RESTART": str(once)}))
    check("сигнал действительно доставлен внутри перезапуска — иначе раздел пуст",
          once.exists())
    check("ВЫКЛАДКА ДОВЕДЕНА ДО КОНЦА, а не брошена на полпути", rc == 0,
          f"rc={rc} " + out[-400:])
    check("код на цели", head_of(app) == vsg, head_of(app)[:12])
    check("и в бою именно цель",
          running_commit.read_text(encoding="utf-8").strip() == vsg,
          running_commit.read_text(encoding="utf-8").strip()[:12])
    check("служба перезапущена ровно один раз",
          (WORK / "systemctl.log").read_text(encoding="utf-8").count("restart") == 1)
    # Окно игнорирования обязано быть ровно вокруг перезапуска, а не шире:
    # иначе Ctrl-C переставал бы работать на всё ожидание готовности.
    src = (ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8").splitlines()
    off = [i for i, ln in enumerate(src) if ln.strip() == "trap '' INT TERM HUP"]
    back = [i for i, ln in enumerate(src) if ln.strip() == "trap 'on_signal INT' INT"]
    window = []
    if off and back:
        after = [i for i in back if i > off[0]]
        if after:
            window = src[off[0] + 1:after[0]]
    check("сигналы глушатся ровно на переход, и в окне — только перезапуск",
          bool(window)
          and len([ln for ln in window if "systemctl restart" in ln]) == 1
          and not any("wait_ready" in ln for ln in window),
          " | ".join(ln.strip()[:40] for ln in window) or "окно не найдено")

    print("\n== Повтор не засчитывает ЧУЖОЙ живой процесс за перезапущенный ==")
    # Самый тихий из трёх. `systemctl restart` не удался: код, env и окружение
    # уже целевые, а в бою по-прежнему прежний процесс. Повтор той же цели
    # видел «status: ok» и объявлял релиз развёрнутым, ни разу его не запустив.
    # Готовность отдаёт коммит живого процесса — его и надо спрашивать.
    git(["checkout", "-q", "main"], cwd=app)
    vr = add_commit(app, "vr")
    git(["checkout", "-q", "--detach", vsg], cwd=app)
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vr], env=deploy_env(app, {"FAIL_RESTART": "1"}))
    check("выкладка с неудавшимся перезапуском отклонена", rc == 1, f"rc={rc}")
    check("подготовка: код уже на цели", head_of(app) == vr, head_of(app)[:12])
    check("подготовка: а в бою по-прежнему ПРЕЖНИЙ процесс",
          running_commit.read_text(encoding="utf-8").strip() == vsg,
          running_commit.read_text(encoding="utf-8").strip()[:12])
    (WORK / "systemctl.log").write_text("", encoding="utf-8")
    rc, out = run(["bash", script, vr], env=deploy_env(app))
    check("повтор НЕ объявил цель развёрнутой без перезапуска",
          "перезапуск не нужен" not in out, out[-400:])
    check("служба перезапущена", "restart" in (WORK / "systemctl.log").read_text(encoding="utf-8"),
          (WORK / "systemctl.log").read_text(encoding="utf-8").strip() or "пусто")
    check("и в бою теперь именно цель, а не прежний релиз",
          running_commit.read_text(encoding="utf-8").strip() == vr,
          running_commit.read_text(encoding="utf-8").strip()[:12])
    check("повтор завершился успешно", rc == 0, f"rc={rc} " + out[-300:])

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
