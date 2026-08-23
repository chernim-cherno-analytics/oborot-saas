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
  3) ротация после неудачной загрузки ИЛИ до проверки хранилища. Если
     `forget --prune` выполнится, когда новый снимок не доехал или когда
     `check` ещё не сказал, что хранилище цело, скрипт своими руками удалит
     последнюю хорошую копию — ровно в тот день, когда что-то пошло не так;
  4) учение на базе, все токены интеграции в которой нечитаемы. Данные
     доехали, приложение стартовало, отчёт зелёный — а синхронизация и запись
     в МойСклад не работают, потому что OBOROT_SECRET в копии нет и не должно
     быть. Учение обязано расшифровать настоящий токен ОТДЕЛЬНОЙ офсайт-копией
     секрета, и без неё оно не начинается.

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
#
# Два отдельных вида вранья, которые настоящим хранилищем по заказу не
# воспроизвести:
#   FAKE_BACKUP_SILENT=1              — `backup` возвращает 0 и НИЧЕГО не создаёт;
#   FAKE_SNAPSHOTS_FAIL_AFTER_BACKUP=1 — `snapshots` отказывает только после того,
#                                        как снимок появился (то есть на втором,
#                                        послезагрузочном опросе).
#
# Идентификаторы снимков подставной restic выдаёт как настоящий: каждый новый
# снимок получает НОВЫЙ id (у restic в хеш входит время), даже если данные не
# изменились. Без этого проверка «появился ли новый снимок» была бы непроверяема.
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
    # Ложный успех: команда отчиталась, а в хранилище ничего не появилось.
    if [ "${FAKE_BACKUP_SILENT:-0}" = "1" ]; then
      echo "snapshot saved"
      exit 0
    fi
    mkdir -p "$FAKE_REPO/snapshot"
    for f in oborot.db manifest.txt; do
      [ -f "$f" ] && cp "$f" "$FAKE_REPO/snapshot/$f"
    done
    N=$(( $(cat "$FAKE_REPO/counter" 2>/dev/null || echo 0) + 1 ))
    printf '%s' "$N" > "$FAKE_REPO/counter"
    printf '%040x' "$N" > "$FAKE_REPO/snapshot-id"
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
  snapshots)
    if [ "${FAKE_SNAPSHOTS_FAIL_AFTER_BACKUP:-0}" = "1" ] && [ -f "$FAKE_REPO/snapshot-id" ]; then
      echo "подставной restic: список снимков недоступен" >&2
      exit 1
    fi
    # Как настоящий restic: снимков нет — это УСПЕХ с пустым списком, а не
    # ошибка. Скрипт обязан отличать одно от другого сам.
    if [ -f "$FAKE_REPO/snapshot-id" ]; then
      ID="$(cat "$FAKE_REPO/snapshot-id")"
      printf '[{"time":"2026-08-23T03:00:00Z","tree":"0000","id":"%s","short_id":"%s","tags":["oborot-db"]}]\n' \
        "$ID" "${ID:0:8}"
    else
      echo '[]'
    fi
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


# Секрет приложения, которым зашифрован токен в подставной боевой базе. В день
# аварии именно его копию берут с собой — и именно она проверяется учением.
LIVE_SECRET = "офсайтовый-секрет-приложения-9f3a"
LIVE_TOKEN = "ms-token-проверочный-0001"


def make_live_db(path: Path) -> None:
    """Боевая база: схему делает само приложение, а не выдуманный SQL.

    Вместе с организацией кладём НАСТОЯЩИЙ зашифрованный токен интеграции:
    без него учение проверяло бы расшифрование на пустом множестве и проходило
    бы на базе, из которой доступ к МойСкладу не восстанавливается.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    code = (
        "import os, sqlite3\n"
        f"os.environ['DATABASE_URL']='sqlite:///{path}'\n"
        "os.environ['SCHEDULER_ENABLED']='0'\n"
        f"os.environ['OBOROT_SECRET']={LIVE_SECRET!r}\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "from app.crypto import encrypt_token\n"
        "with TestClient(app, headers={'X-Oborot-CSRF':'1'}) as c:\n"
        "    c.post('/register', data={'name':'v','email':'off@test.io',\n"
        "        'password':'secret123','org_name':'Офсайт-бренд'})\n"
        f"con = sqlite3.connect({str(path)!r})\n"
        "con.execute(\"INSERT INTO connections (org_id, kind, token_enc, status,"
        " config_json) VALUES (1,'moysklad',?,'active','{}')\","
        f" (encrypt_token({LIVE_TOKEN!r}),))\n"
        "con.commit(); con.close()\n"
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
        # Копия секрета приложения — та, что в жизни лежит вне сервера.
        self.recovery = self.dir / "recovery-secret"
        self.recovery.write_text(LIVE_SECRET + "\n", encoding="utf-8")
        # Учение проверяет расшифрование и подъём приложения интерпретатором из
        # venv приложения: ему нужны и cryptography, и uvicorn, и сам пакет app.
        #
        # Здесь не символическая ссылка, а обёртка: ссылка на интерпретатор
        # ЧУЖОГО venv уводит Python на базовый префикс — свои site-packages он
        # ищет по pyvenv.cfg рядом с собой, а его в подставном каталоге нет.
        # Внешне такой venv выглядит рабочим и падает на первом же импорте.
        self.venv = self.dir / "venv"
        (self.venv / "bin").mkdir(parents=True, exist_ok=True)
        shim = self.venv / "bin" / "python"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)

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
            "OBOROT_RECOVERY_SECRET_FILE": str(self.recovery),
            "OBOROT_VENV": str(self.venv),
            # Площадка не может смонтировать файловую систему (нужен root), а
            # учение по умолчанию требует, чтобы копия секрета лежала НЕ на том
            # же диске, что боевая база. Поэтому здесь стоит осознанное
            # послабление — то самое, которое пишет в отметку
            # domain=не-проверен. Само правило проверяется отдельно (13е), и
            # там же — что без послабления местный файл отвергается.
            "OBOROT_DRILL_ALLOW_LOCAL_SECRET": "1",
        }
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run(self, script: Path, args=None, timeout: int = 180, **extra):
        env = dict(os.environ)
        # Секрет приложения не должен просачиваться из окружения прогона:
        # учение обязано брать его только из офсайт-копии.
        env.pop("OBOROT_SECRET", None)
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
    # Порядок и есть проверяемое поведение: сначала запоминаем последний снимок
    # ДО загрузки, потом убеждаемся, что появился НОВЫЙ и что хранилище цело, —
    # и только потом срезаем старое.
    check("порядок: cat → snapshots → backup → snapshots → check → forget",
          order == ["cat", "snapshots", "backup", "snapshots", "check", "forget"],
          " → ".join(order))
    stamp_text = (e.state / "last-offsite-backup").read_text(encoding="utf-8")
    snap_id = (e.repo_dir / "snapshot-id").read_text(encoding="utf-8").strip()
    check("в отметке записан id снимка, который появился в хранилище",
          f"snapshot={snap_id}" in stamp_text, stamp_text.strip())
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

    # ---------------------------------------------------------------- 6а
    print("\n== 6а. Хранилище не прошло check — ротации НЕ БУДЕТ ==")
    # Прежний порядок был backup → forget --prune → check. Повреждение
    # обнаруживалось уже ПОСЛЕ того, как старые снимки срезаны, — то есть в
    # единственном случае, ради которого проверка существует, копий, которыми
    # можно пережить повреждение, к этому моменту уже не было.
    e = Env("check-failed")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP, FAKE_RESTIC_FAIL="check")
    check("скрипт упал на проверке хранилища", rc != 0, f"код {rc}")
    calls = e.calls()
    check("новая копия при этом загружена", any(c.startswith("backup") for c in calls),
          " | ".join(calls))
    check("РОТАЦИИ НЕ БЫЛО: forget/prune не вызывался",
          not any(c.startswith("forget") for c in calls), " | ".join(calls))
    check("сказано, что старые копии целы", "старые копии целы" in out,
          out.strip().splitlines()[-1] if out.strip() else "")
    check("отметки об успехе нет", not (e.state / "last-offsite-backup").exists())

    # ---------------------------------------------------------------- 6б
    print("\n== 6б. Список снимков недоступен — ни загрузки, ни ротации ==")
    # Хранилище отвечает на `cat config`, но список снимков не отдаёт. Тогда
    # доказать появление новой копии будет нечем, а решение на этом
    # доказательстве принимается разрушительное — отказ до отправки.
    e = Env("snapshot-missing")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP, FAKE_RESTIC_FAIL="snapshots")
    check("скрипт упал", rc != 0, f"код {rc}")
    check("выгрузки не было",
          not any(c.startswith("backup") for c in e.calls()), " | ".join(e.calls()))
    check("ротации не было",
          not any(c.startswith("forget") for c in e.calls()), " | ".join(e.calls()))
    check("проверки хранилища тоже не было — незачем",
          not any(c.startswith("check") for c in e.calls()), " | ".join(e.calls()))
    check("названа причина", "список снимков" in out,
          out.strip().splitlines()[-1] if out.strip() else "")

    # То же самое, но опрос ломается ПОСЛЕ загрузки: копия уже уехала, а
    # проверить её появление нечем. Ротация в этом состоянии запрещена.
    e = Env("snapshot-list-after")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP, FAKE_SNAPSHOTS_FAIL_AFTER_BACKUP="1")
    check("скрипт упал уже после загрузки", rc != 0, f"код {rc}")
    check("копия при этом загружена",
          any(c.startswith("backup") for c in e.calls()), " | ".join(e.calls()))
    check("РОТАЦИИ НЕ БЫЛО",
          not any(c.startswith("forget") for c in e.calls()), " | ".join(e.calls()))
    check("сказано, что старые копии целы", "старые копии целы" in out,
          out.strip().splitlines()[-1] if out.strip() else "")

    # Отдельный случай, и он опаснее: `restic snapshots` при отсутствии снимков
    # завершается УСПЕШНО с пустым списком. Скрипт, проверяющий только код
    # возврата, здесь спокойно пошёл бы удалять старые копии.
    e = Env("snapshot-empty")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    # Хранилище «принимает» загрузку, но снимка после неё не появляется.
    rc, out = e.run(BACKUP, FAKE_BACKUP_SILENT="1")
    check("пустой список снимков — это отказ, а не успех", rc != 0, f"код {rc}")
    check("РОТАЦИИ НЕ БЫЛО",
          not any(c.startswith("forget") for c in e.calls()), " | ".join(e.calls()))
    check("сказано, что старые копии целы", "старые копии целы" in out,
          out.strip().splitlines()[-1] if out.strip() else "")

    # --------------------------------------------------------------- 6г
    print("\n== 6г. СТАРЫЙ снимок есть, нового не появилось — ротации НЕ БУДЕТ ==")
    # Найдено повторным внешним ревью 23.08 и опаснее всех предыдущих случаев,
    # потому что снаружи выглядит как обычная успешная ночь: `restic backup`
    # вернул 0 и ничего не создал, а в хранилище лежит вчерашний снимок.
    # Проверка «список не пуст» на нём проходит — и ротация срезает копии
    # позавчерашние. День за днём остаётся один устаревающий снимок при зелёном
    # отчёте. Проверяется появление ИМЕННО НОВОГО снимка: id до и после.
    e = Env("stale-snapshot")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP)
    check("подготовка: вчерашний снимок в хранилище есть", rc == 0, f"код {rc}")
    old_id = (e.repo_dir / "snapshot-id").read_text(encoding="utf-8").strip()
    check("подготовка: у него есть идентификатор", len(old_id) == 40, old_id)
    e.log.write_text("", encoding="utf-8")
    (e.state / "last-offsite-backup").unlink()
    rc, out = e.run(BACKUP, FAKE_BACKUP_SILENT="1")
    check("ЛОЖНЫЙ УСПЕХ ЗАГРУЗКИ ПОЙМАН: скрипт упал", rc != 0, f"код {rc}")
    check("названо, что именно не сошлось", "НОВОГО снимка" in out,
          " / ".join(out.strip().splitlines()[-4:]))
    check("в отказе назван тот самый старый id", old_id in out,
          " / ".join(out.strip().splitlines()[-4:]))
    calls = e.calls()
    check("загрузку всё-таки пробовали", any(c.startswith("backup") for c in calls),
          " | ".join(calls))
    check("РОТАЦИИ НЕ БЫЛО: forget/prune не вызывался",
          not any(c.startswith("forget") for c in calls), " | ".join(calls))
    check("проверки хранилища не было — до неё не дошло",
          not any(c.startswith("check") for c in calls), " | ".join(calls))
    check("старый снимок на месте, его никто не тронул",
          (e.repo_dir / "snapshot" / "oborot.db").exists()
          and (e.repo_dir / "snapshot-id").read_text(encoding="utf-8").strip() == old_id)
    check("отметки об успехе нет", not (e.state / "last-offsite-backup").exists())

    # ---------------------------------------------------------------- 6в
    print("\n== 6в. В хранилище уезжают только база и манифест ==")
    e = Env("no-secrets")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    rc, out = e.run(BACKUP)
    check("копия загружена", rc == 0, f"код {rc}")
    uploaded = sorted(p.name for p in (e.repo_dir / "snapshot").iterdir())
    check("в снимке ровно два файла", uploaded == ["manifest.txt", "oborot.db"], str(uploaded))
    blob = b""
    for p in e.repo_dir.rglob("*"):
        if p.is_file():
            blob += p.read_bytes()
    check("СЕКРЕТ ПРИЛОЖЕНИЯ В ХРАНИЛИЩЕ НЕ УЕХАЛ",
          LIVE_SECRET.encode() not in blob)
    check("пароль restic в хранилище не уехал",
          "не настоящий пароль".encode() not in blob)

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
    check("токен интеграции расшифрован офсайт-копией секрета",
          "токены расшифровываются" in out,
          out.strip().splitlines()[-1] if out.strip() else "")
    check("сам токен нигде не напечатан", LIVE_TOKEN not in out)
    check("сам секрет нигде не напечатан", LIVE_SECRET not in out)
    drill_stamp = e.state / "last-offsite-drill"
    check("отметка об учении записана", drill_stamp.exists())
    check("в отметке видно, что токены проверены",
          "tokens=да" in drill_stamp.read_text(encoding="utf-8"),
          drill_stamp.read_text(encoding="utf-8").strip() if drill_stamp.exists() else "")

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
    rc, out = e.run(DRILL, timeout=300, OBOROT_DRILL_BOOT_APP="1",
                    RESTORE_PORT=next(_ports))
    check("приложение поднялось на восстановленной базе", rc == 0,
          out.strip().splitlines()[-1] if out.strip() else f"код {rc}")
    check("проверка запуском действительно выполнялась",
          "ВОССТАНОВЛЕНИЕ ПРОВЕРЕНО" in out)

    # --------------------------------------------------------------- 13а
    print("\n== 13а. Учение без копии секрета не начинается ==")
    # Fail-closed. Мягкий вариант («секрета нет — просто не проверяем») дал бы
    # зелёное учение, которое молчит ровно о том, ради чего проводится.
    e = Env("drill-no-secret")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.run(BACKUP)
    e.log.write_text("", encoding="utf-8")
    rc, out = e.run(DRILL, OBOROT_RECOVERY_SECRET_FILE="")
    check("без OBOROT_RECOVERY_SECRET_FILE учение провалено", rc != 0, f"код {rc}")
    check("сказано, чего не хватает и почему", "OBOROT_RECOVERY_SECRET_FILE" in out
          and "расшифров" in out, out.strip().splitlines()[0] if out.strip() else "")
    check("отказ ДО обращения к хранилищу", e.calls() == [], " | ".join(e.calls()))

    rc, out = e.run(DRILL, OBOROT_RECOVERY_SECRET_FILE=str(e.dir / "нет-такого-файла"))
    check("несуществующая копия секрета — отказ", rc != 0, f"код {rc}")
    empty = e.dir / "пустой-секрет"
    empty.write_text("", encoding="utf-8")
    rc, out = e.run(DRILL, OBOROT_RECOVERY_SECRET_FILE=str(empty))
    check("пустая копия секрета — отказ", rc != 0, f"код {rc}")
    check("названа причина", "пуста" in out, out.strip().splitlines()[-1] if out.strip() else "")

    rc, out = e.run(DRILL, OBOROT_RECOVERY_SECRET_FILE=str(e.password))
    check("копия секрета и пароль restic не могут быть одним файлом", rc != 0, f"код {rc}")

    # --------------------------------------------------------------- 13б
    print("\n== 13б. Секрет из окружения проверку не заменяет ==")
    # С OBOROT_SECRET в окружении учение доказывало бы, что сервер знает свой
    # собственный ключ. Это не то утверждение, которое нужно в день аварии.
    rc, out = e.run(DRILL, OBOROT_SECRET=LIVE_SECRET)
    check("учение с секретом в окружении отказывается идти", rc != 0, f"код {rc}")
    check("сказано, почему это не проверка", "офсайт-копию секрета" in out,
          out.strip().splitlines()[-1] if out.strip() else "")

    # --------------------------------------------------------------- 13в
    print("\n== 13в. Чужая копия секрета — учение провалено ==")
    # Самый вероятный сценарий из реальных: OBOROT_SECRET на сервере сменили,
    # а копию для восстановления не обновили. Данные при этом восстанавливаются
    # полностью, приложение стартует — и интеграция мертва.
    e = Env("drill-wrong-secret")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.run(BACKUP)
    wrong = e.dir / "чужой-секрет"
    wrong.write_text("совсем-другой-секрет\n", encoding="utf-8")
    rc, out = e.run(DRILL, OBOROT_RECOVERY_SECRET_FILE=str(wrong))
    check("учение провалено", rc != 0, f"код {rc}")
    check("названо, что именно не сходится", "не расшифровывает" in out,
          out.strip().splitlines()[-1] if out.strip() else "")
    check("отметки об успешном учении нет",
          not (e.state / "last-offsite-drill").exists())

    # --------------------------------------------------------------- 13г
    print("\n== 13г. Токенов в копии нет — учение это говорит вслух ==")
    # Не отказ (у организации может не быть подключения), но и не молчаливый
    # успех: в отметке остаётся tokens=нечего, а в выводе — прямая оговорка.
    e = Env("drill-no-tokens")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.run(BACKUP)
    con = sqlite3.connect(e.repo_dir / "snapshot" / "oborot.db")
    con.execute("DELETE FROM connections")
    con.commit()
    con.close()
    rc, out = e.run(DRILL)
    check("учение пройдено", rc == 0, out.strip().splitlines()[-1] if out.strip() else f"код {rc}")
    check("сказано, что доступ к МойСкладу НЕ проверен",
          "не сохранность доступа" in out,
          " / ".join(out.strip().splitlines()[-3:]))
    check("в отметке это видно",
          "tokens=нечего" in (e.state / "last-offsite-drill").read_text(encoding="utf-8"))

    # --------------------------------------------------------------- 13е
    print("\n== 13е. Копия секрета на диске сервера — учение не начинается ==")
    # Найдено повторным внешним ревью 23.08. Схема сама себе противоречила:
    # секрет объявлялся офсайт-копией, а лежал файлом в /opt/oborot — на том же
    # диске, что и база. Такой файл умирает вместе с машиной ровно так же, как
    # база, и еженедельное учение с ним доказывает только то, что сервер знает
    # свой собственный ключ.
    e = Env("drill-local-secret")
    shutil.copy(seed, _mk(e.db))
    e.repo_initialized()
    e.run(BACKUP)
    e.log.write_text("", encoding="utf-8")
    rc, out = e.run(DRILL, OBOROT_DRILL_ALLOW_LOCAL_SECRET="0")
    check("копия секрета на диске с базой — учение провалено", rc != 0, f"код {rc}")
    check("названа причина", "той же файловой системе" in out,
          " / ".join(out.strip().splitlines()[-3:]))
    check("сказано, чем подать секрет вместо этого", "примонтированный" in out,
          " / ".join(out.strip().splitlines()[-4:]))
    check("отказ ДО обращения к хранилищу", e.calls() == [], " | ".join(e.calls()))
    check("отметки об учении нет", not (e.state / "last-offsite-drill").exists())

    # Осознанное послабление для стенда: не отказ, но и не молчаливый успех.
    rc, out = e.run(DRILL, OBOROT_DRILL_ALLOW_LOCAL_SECRET="1")
    check("с явным послаблением учение идёт", rc == 0,
          out.strip().splitlines()[-1] if out.strip() else f"код {rc}")
    stamp_text = (e.state / "last-offsite-drill").read_text(encoding="utf-8")
    check("но в отметке видно, что происхождение ключа НЕ проверено",
          "domain=не-проверен" in stamp_text, stamp_text.strip())
    check("и сказано об этом вслух", "НЕ то, что ключ хранится вне этой машины" in out,
          " / ".join(out.strip().splitlines()[-3:]))

    print("\n== 13ж. Копия секрета, поданная извне, принимается ==")
    alt = alt_fs_dir()
    if alt is None:
        print("  ПРОПУСК: второй файловой системы в этой машине не нашлось.")
        print("  Смонтировать её тест не может (нужен root), а объявлять проверку")
        print("  пройденной без проверки — то же самое враньё, против которого весь набор.")
    else:
        outside = alt / f"oborot-drill-secret-{os.getpid()}"
        outside.write_text(LIVE_SECRET + "\n", encoding="utf-8")
        os.chmod(outside, 0o600)
        try:
            rc, out = e.run(DRILL, OBOROT_DRILL_ALLOW_LOCAL_SECRET="0",
                            OBOROT_RECOVERY_SECRET_FILE=str(outside))
            check(f"секрет с другой файловой системы ({alt}) принят", rc == 0,
                  out.strip().splitlines()[-1] if out.strip() else f"код {rc}")
            stamp_text = (e.state / "last-offsite-drill").read_text(encoding="utf-8")
            check("в отметке domain=отдельный", "domain=отдельный" in stamp_text,
                  stamp_text.strip())
            check("токены при этом расшифрованы", "tokens=да" in stamp_text,
                  stamp_text.strip())
            # Честность формулировки: «другая файловая система» — это не
            # доказательство отдельного домена отказа, и учение так и говорит.
            check("и прямо сказано, чего это НЕ доказывает",
                  "физическое размещение доказать не может" in out,
                  " / ".join(out.strip().splitlines()[-3:]))
        finally:
            outside.unlink(missing_ok=True)

    print("\n== 13з. Схема хранения ключа не противоречит сама себе ==")
    # Документ и юнит — часть той же схемы, и разъехаться им нельзя: образец
    # конфигурации, юнит по расписанию и текст инструкции должны говорить одно.
    unit = (ROOT / "deploy" / "systemd" / "oborot-offsite-drill.service").read_text(
        encoding="utf-8")
    example = (ROOT / "deploy" / "backup.env.example").read_text(encoding="utf-8")
    readme = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    secret_line = [ln for ln in example.splitlines()
                   if ln.startswith("OBOROT_RECOVERY_SECRET_FILE=")]
    check("в образце ровно одна строка с путём к копии секрета", len(secret_line) == 1,
          str(secret_line))
    secret_path = secret_line[0].split("=", 1)[1].strip() if secret_line else ""
    check("образец НЕ предлагает файл на диске сервера",
          not secret_path.startswith("/opt/oborot"), secret_path)
    mount_lines = [ln for ln in unit.splitlines()
                   if ln.startswith(("RequiresMountsFor=", "AssertPathIsMountPoint="))]
    check("юнит учения требует смонтированного каталога", len(mount_lines) == 2,
          str(mount_lines))
    check("и это тот самый каталог, что в образце",
          bool(secret_path) and all(
              ln.split("=", 1)[1].strip() == os.path.dirname(secret_path)
              for ln in mount_lines),
          f"{secret_path} vs {mount_lines}")
    # Assert*, а не Condition*: пропущенное учение выглядит как успешное.
    check("отсутствие монтирования РОНЯЕТ учение, а не пропускает его",
          "AssertPathIsMountPoint=" in unit and "ConditionPathIsMountPoint=" not in unit)
    # Упомянуть локальный путь можно — ровно затем, чтобы сказать, что он не
    # годится. Нельзя другое: предлагать его как рецепт или как значение
    # настройки. Поэтому проверяются готовые команды и присваивания, а не само
    # наличие строки: иначе тест запрещал бы объяснять, в чём была ошибка.
    blocks = readme.split("```")[1::2]
    check("README не предлагает завести файл секрета на диске сервера",
          not any("/opt/oborot/recovery-secret" in b for b in blocks),
          "рецепт остался в примере команд")
    bad_assign = [ln for ln in (readme + "\n" + example).splitlines()
                  if "OBOROT_RECOVERY_SECRET_FILE" in ln and "/opt/oborot" in ln]
    check("нигде нет настройки, указывающей на диск сервера", not bad_assign,
          str(bad_assign))

    # --------------------------------------------------------------- 13д
    print("\n== 13д. Таймауты у юнитов systemd заданы ==")
    # Type=oneshot живёт с DefaultTimeoutStartSec (обычно 90 с), если не сказано
    # иначе: systemd убил бы копию посреди загрузки, а в журнале осталось бы
    # «timeout» — и никто не узнал бы, что копий нет, до дня аварии.
    for unit in ("oborot-offsite-backup.service", "oborot-offsite-drill.service"):
        text = (ROOT / "deploy" / "systemd" / unit).read_text(encoding="utf-8")
        line = [ln for ln in text.splitlines() if ln.startswith("TimeoutStartSec=")]
        check(f"{unit}: TimeoutStartSec задан", len(line) == 1, str(line))
        check(f"{unit}: таймаут не меньше часа",
              bool(line) and line[0].split("=")[1].strip() in ("1h", "2h", "3h", "4h", "6h")
              or bool(line) and line[0].split("=")[1].strip().isdigit()
              and int(line[0].split("=")[1]) >= 3600,
              line[0] if line else "нет строки")

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


def alt_fs_dir():
    """Каталог на ДРУГОЙ файловой системе, чем площадка теста, или None.

    Нужен ровно для одной проверки: учение с копией секрета, поданной извне,
    проходит и пишет `domain=отдельный`. Смонтировать файловую систему тест не
    может — для этого нужен root, — поэтому берётся то, что уже есть в машине:
    на Linux это обычно tmpfs `/dev/shm`. Не нашлось ничего — проверка
    пропускается ВСЛУХ. Объявить её пройденной без проверки значило бы то же
    самое враньё, против которого написан весь этот набор.
    """
    try:
        base = os.stat(WORK).st_dev
    except OSError:
        return None
    seen = []
    for cand in ("/dev/shm", f"/run/user/{os.getuid()}",
                 os.environ.get("TMPDIR", ""), "/tmp", "/var/tmp"):
        if not cand or cand in seen:
            continue
        seen.append(cand)
        p = Path(cand)
        try:
            if p.is_dir() and os.access(p, os.W_OK) and os.stat(p).st_dev != base:
                return p
        except OSError:
            continue
    return None


if __name__ == "__main__":
    sys.exit(main())
