# -*- coding: utf-8 -*-
"""Эксплуатационные гарантии: health-ручки и запрет многопроцессного запуска.

Почему это тест, а не «и так видно». Обе гарантии проверяются только в момент
старта процесса или внешним мониторингом — то есть там, где обычные тесты не
ходят, а человек замечает поломку последним. Инцидент 03–21.08 (синк молча
падал на протухшем токене, данные протухали восемнадцать дней) случился ровно
потому, что у сервиса не было ни одного способа сказать наружу «мне плохо».

Проверяется:
  1) lock содержит только точные версии, покрывает прямые зависимости, а CI и
     production действительно ставят именно его и запускают pip check;
  2) /health/live отвечает всегда и НЕ трогает базу (liveness не должен падать
     каскадом: перезапуск процесса рвёт фоновую догрузку истории);
  3) /health/ready отвечает 200, когда старт завершён и база отвечает;
  4) обе ручки не требуют авторизации и не раскрывают данные организаций;
  5) приложение отказывается стартовать при нескольких воркерах, потому что
     кэш аналитики, лимит входа и планировщик живут в памяти процесса;
  6) осознанный многопроцессный запуск разрешается явным флагом.

Запуск из корня репозитория:  python tests/test_ops.py
"""
import os
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_ops.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.pop("WEB_CONCURRENCY", None)
os.environ.pop("OBOROT_ALLOW_MULTIPROC", None)

if DB_PATH.exists():
    DB_PATH.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def _child(env_extra: dict) -> tuple[int, str]:
    """Поднимает приложение в отдельном процессе с заданным окружением."""
    env = dict(os.environ)
    env.update(env_extra)
    env["DATABASE_URL"] = f"sqlite:///{ROOT / 'test_ops_child.db'}"
    code = (
        "from fastapi.testclient import TestClient\n"
        "import app.main as m\n"
        "with TestClient(m.app) as c:\n"
        "    print('STARTED', c.get('/health/live').status_code)\n"
    )
    p = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=120)
    return p.returncode, (p.stdout + p.stderr)


def _active_requirements(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _package_name(requirement: str) -> str:
    raw = re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0]
    return raw.strip().lower().replace("_", "-")


def main() -> int:
    print("\n== Точный lock зависимостей ==")
    source = _active_requirements(ROOT / "requirements.txt")
    locked = _active_requirements(ROOT / "requirements.lock")
    bad = [line for line in locked
           if not re.fullmatch(r"[A-Za-z0-9_.-]+==[^;\s]+", line)]
    check("каждая устанавливаемая зависимость имеет ровно одну версию",
          bool(locked) and not bad, str(bad[:5]))

    source_names = {_package_name(line) for line in source}
    locked_names = {_package_name(line) for line in locked}
    missing = sorted(source_names - locked_names)
    check("все прямые зависимости присутствуют в lock", not missing, str(missing))

    drift = []
    for line in locked:
        name, wanted = line.split("==", 1)
        try:
            actual = version(name)
        except PackageNotFoundError:
            actual = "MISSING"
        if actual != wanted:
            drift.append(f"{name}: {actual} != {wanted}")
    check("тесты идут на тех же версиях, которые фиксирует lock",
          not drift, str(drift[:5]))

    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/deploy.sh").read_text(encoding="utf-8")
    check("CI ставит lock и проверяет целостность окружения",
          "pip install -r requirements.lock" in ci and "pip check" in ci)
    check("production deploy ставит lock и проверяет целостность окружения",
          "pip\" install -q -r requirements.lock" in deploy and "pip\" check" in deploy)

    print("\n== Health-эндпоинты ==")
    with TestClient(oborot_app) as c:
        r = c.get("/health/live")
        check("/health/live отвечает 200", r.status_code == 200, f"status={r.status_code}")
        check("/health/live отдаёт статус", r.json().get("status") == "ok", r.text[:80])

        r = c.get("/health/ready")
        body = r.json()
        check("/health/ready отвечает 200 на живой базе", r.status_code == 200,
              f"status={r.status_code} {r.text[:120]}")
        check("/health/ready подтверждает, что старт завершён",
              body.get("startup") is True, r.text[:120])
        check("/health/ready подтверждает, что база отвечает",
              body.get("db") is True, r.text[:120])
        check("/health/ready сообщает состояние планировщика",
              "scheduler" in body, r.text[:120])
        check("/health/ready не раскрывает данные организаций",
              not any(k in r.text.lower() for k in ("email", "token", "org_name", "@")),
              r.text[:120])

        # Ручки доступны без авторизации: мониторинг ходит без куки.
        anon = TestClient(oborot_app)
        check("/health/live доступен без авторизации",
              anon.get("/health/live").status_code == 200)
        check("/health/ready доступен без авторизации",
              anon.get("/health/ready").status_code in (200, 503))

    print("\n== Запрет многопроцессного запуска ==")
    rc, out = _child({"WEB_CONCURRENCY": "4"})
    check("приложение не стартует при WEB_CONCURRENCY=4", rc != 0 and "STARTED" not in out,
          f"rc={rc}")
    check("в тексте отказа объяснено, почему один воркер",
          "воркер" in out and ("кэш" in out or "планировщ" in out),
          out.strip().splitlines()[-1][:120] if out.strip() else "")

    rc, out = _child({"WEB_CONCURRENCY": "4", "OBOROT_ALLOW_MULTIPROC": "1"})
    check("осознанный многопроцессный запуск разрешается флагом",
          rc == 0 and "STARTED 200" in out, f"rc={rc} {out[-120:]}")

    rc, out = _child({"WEB_CONCURRENCY": "1"})
    check("один воркер стартует как обычно", rc == 0 and "STARTED 200" in out,
          f"rc={rc} {out[-120:]}")

    for p in (ROOT / "test_ops_child.db", DB_PATH):
        if p.exists():
            p.unlink()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
