# -*- coding: utf-8 -*-
"""MAINT-4: у _stub_templates/ не должно быть пути обратно в приложение.

Раньше оба Jinja2Templates (app.main.templates, app.routes_extra._templates)
искали шаблон сначала в templates/, потом в _stub_templates/: удаление или
переименование настоящего шаблона не роняло страницу, а тихо подменяло её
заглушкой — тот же класс дефекта, что «расхождение интерфейса и расчёта» из
AGENTS.md §3, только на уровне вёрстки. Теперь `_stub_templates/` удалён
целиком (`git rm`), а оба loader'а ищут только в templates/: отсутствующий
шаблон обязан упасть `jinja2.TemplateNotFound`, а не подменяться.

Проверки:
  1) путь поиска обоих Jinja2Templates — ровно templates/, без второй
     директории;
  2) `_stub_templates/` не существует на диске и не числится в `git ls-files`;
  3) временно спрятанный настоящий шаблон даёт TemplateNotFound у обоих
     loader'ов (fail-fast), а не успешный рендер чем-то другим;
  4) все текущие *.html из templates/ по-прежнему грузятся обоими loader'ами
     (ничего не сломано изъятием второй директории);
  5) живые публичные роуты, использующие оба loader'а (/login — app.main,
     /legal/offer и /legal/privacy — app.routes_extra), отвечают 200 и не
     превращаются в 500 из-за конфигурации шаблонов.

Запуск из корня репозитория: python tests/test_template_stubs.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_template_stubs.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
if DB_PATH.exists():
    DB_PATH.unlink()

import jinja2  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_mod  # noqa: E402
import app.routes_extra as routes_extra_mod  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


TEMPLATES_DIR = ROOT / "templates"
STUB_DIR = ROOT / "_stub_templates"

# Прежние имена заглушек — MAINT-4 удалил их все; список фиксирует, что
# именно закрыто, а не отрастает заново молча.
FORMER_STUB_NAMES = [
    "base.html", "dashboard.html", "login.html", "onboarding.html",
    "orders.html", "register.html", "replenish.html", "settings.html",
    "stocks.html", "turnover.html",
]


def check_search_paths() -> None:
    for label, templates_obj in (
        ("app.main.templates", main_mod.templates),
        ("app.routes_extra._templates", routes_extra_mod._templates),
    ):
        search_path = [str(Path(p).resolve()) for p in templates_obj.env.loader.searchpath]
        check(f"{label}: путь поиска — ровно templates/",
              search_path == [str(TEMPLATES_DIR.resolve())],
              f"searchpath={search_path}")


def check_stub_dir_gone() -> None:
    check("_stub_templates/ отсутствует на диске", not STUB_DIR.exists(),
          f"exists={STUB_DIR.exists()}")
    tracked = subprocess.run(
        ["git", "ls-files", "_stub_templates"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    ).stdout.strip()
    check("_stub_templates/ не числится в git ls-files", tracked == "", f"tracked={tracked!r}")


def _hide_and_check_not_found(label: str, templates_obj, name: str) -> None:
    real = TEMPLATES_DIR / name
    backup = TEMPLATES_DIR / f"{name}.maint4-redproof-bak"
    shutil.move(str(real), str(backup))
    try:
        try:
            templates_obj.env.loader.get_source(templates_obj.env, name)
            check(f"{label}: спрятанный {name} — TemplateNotFound", False,
                  "loader нашёл шаблон, хотя настоящий файл спрятан")
        except jinja2.TemplateNotFound:
            check(f"{label}: спрятанный {name} — TemplateNotFound", True)
    finally:
        shutil.move(str(backup), str(real))


def check_missing_real_template_fails_fast() -> None:
    _hide_and_check_not_found("app.main.templates", main_mod.templates, "dashboard.html")
    _hide_and_check_not_found("app.routes_extra._templates", routes_extra_mod._templates, "settings.html")


def check_all_real_templates_still_load() -> None:
    html_files = sorted(p.name for p in TEMPLATES_DIR.glob("*.html"))
    check("templates/ не пуст", len(html_files) > 0, f"count={len(html_files)}")
    for label, templates_obj in (
        ("app.main.templates", main_mod.templates),
        ("app.routes_extra._templates", routes_extra_mod._templates),
    ):
        failed = []
        for name in html_files:
            try:
                templates_obj.env.loader.get_source(templates_obj.env, name)
            except Exception as exc:  # noqa: BLE001 — фиксируем ЛЮБОЙ сбой загрузки
                failed.append(f"{name}: {exc}")
        check(f"{label}: все {len(html_files)} шаблонов из templates/ загружаются",
              not failed, "; ".join(failed[:5]))
    for name in FORMER_STUB_NAMES:
        check(f"бывший stub-файл {name} по-прежнему покрыт настоящим templates/{name}",
              (TEMPLATES_DIR / name).is_file())


def check_live_routes_render() -> None:
    with TestClient(main_mod.app) as c:
        r = c.get("/login")
        check("GET /login (app.main.templates) — 200", r.status_code == 200, f"status={r.status_code}")
        r = c.get("/legal/offer")
        check("GET /legal/offer (app.routes_extra._templates) — 200",
              r.status_code == 200, f"status={r.status_code}")
        r = c.get("/legal/privacy")
        check("GET /legal/privacy (app.routes_extra._templates) — 200",
              r.status_code == 200, f"status={r.status_code}")


def main() -> int:
    print("\n== Путь поиска шаблонов — только templates/ ==")
    check_search_paths()

    print("\n== _stub_templates/ удалён (диск + git) ==")
    check_stub_dir_gone()

    print("\n== Спрятанный настоящий шаблон падает TemplateNotFound (fail-fast) ==")
    check_missing_real_template_fails_fast()

    print("\n== Все текущие реальные шаблоны по-прежнему грузятся ==")
    check_all_real_templates_still_load()

    print("\n== Живые публичные роуты обоих loader'ов рендерятся ==")
    check_live_routes_render()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if DB_PATH.exists():
        DB_PATH.unlink()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
