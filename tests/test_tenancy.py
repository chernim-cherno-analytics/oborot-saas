# -*- coding: utf-8 -*-
"""Сторож полноты удаления арендатора: реестр моделей против списков удаления.

Зачем отдельный набор. Обещание «данные стёрты» держится на перечислении
моделей в `app.tenancy`. Соседние наборы проверяют, что удаление РАБОТАЕТ на
сегодняшних таблицах (`test_account.py`, `test_decision_record.py`), — и это
правильные тесты, но у них общая слепая зона: они видят только те таблицы, где
в сценарии ЕСТЬ строки. Модель, добавленная завтра и забытая в списке удаления,
не уронит ни один из них. Уронит этот.

Проверяется:
  1) СВЕРКА С РЕЕСТРОМ — каждая колонка каждой mapped-модели, ссылающаяся на
     организацию или пользователя, либо покрыта списком удаления, либо
     объяснена записью в allowlist. Семантически: по колонкам и внешним ключам
     из `Base.registry`, а не по тексту исходников;
  2) СПИСКИ НЕ РАЗЪЕХАЛИСЬ С КОДОМ — `_purge_org` и `_purge_user` выполняют
     ровно те DELETE и ровно в том порядке, что объявлены в `app.tenancy`;
  3) ПОРЯДОК БЕЗОПАСЕН ПО ВНЕШНИМ КЛЮЧАМ — зависимая таблица удаляется раньше
     той, на которую ссылается (в Postgres иначе упадёт);
  4) ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ — сторож обязан ЛОВИТЬ неизвестную модель, а не
     просто зеленеть. Три подставные модели: с `org_id`, со ссылкой на
     `orgs.id` под ДРУГИМ именем колонки (доказывает, что сторож смотрит на
     внешние ключи, а не на имя) и с `user_id`. Каждая регистрируется в
     ОТДЕЛЬНОМ ПОДПРОЦЕССЕ: реестр и `Base.metadata` этого процесса умирают
     вместе с ним и соседним наборам не мешают;
  5) ALLOWLIST ПРОВЕРЯЕТСЯ В ОБЕ СТОРОНЫ — запись, которой больше не
     соответствует реальная колонка, тоже роняет сторож. Протухший список
     исключений опаснее отсутствующего.

Ни сервера, ни сети, ни базы этот набор не поднимает: он работает с реестром
моделей.

Запуск из корня репозитория:  python tests/test_tenancy.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# База не нужна: таблицы не создаются, запросы не выполняются. Но `app.db`
# читает DATABASE_URL при импорте, поэтому значение обязано быть.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SCHEDULER_ENABLED"] = "0"

from sqlalchemy import delete  # noqa: E402

from app import tenancy  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


class RecordingSession:
    """Сессия, которая ничего не выполняет, а записывает целевые таблицы.

    Нужна, чтобы проверить ПОРЯДОК DELETE, не создавая базу: порядок — это и
    есть то, что ломается молча (в SQLite внешние ключи не проверяются, и
    ошибка вылезет только на Postgres или на осиротевших строках).
    """

    def __init__(self):
        self.tables: list[str] = []

    def execute(self, statement):
        self.tables.append(statement.table.name)
        return None


CONTROL_TEMPLATE = '''
import os, sys
sys.path.insert(0, {root!r})
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SCHEDULER_ENABLED"] = "0"

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app import tenancy
tenancy.import_all_models()
from app.db import Base

{model}

for line in tenancy.purge_completeness_violations():
    print(line)
'''

CONTROL_ORG_BY_NAME = '''
class RogueOrgOwned(Base):
    """Модель, которую забыли внести в список удаления организации."""
    __tablename__ = "rogue_org_owned"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer, nullable=False)
'''

CONTROL_ORG_BY_FK = '''
class RogueRenamedOrgOwned(Base):
    """Владение организацией через колонку С ДРУГИМ ИМЕНЕМ, но с внешним ключом.

    Сторож, который искал бы подстроку org_id, эту модель бы пропустил.
    """
    __tablename__ = "rogue_renamed_org_owned"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False)
'''

CONTROL_USER = '''
class RogueUserOwned(Base):
    """Личные данные пользователя, забытые в списке удаления аккаунта."""
    __tablename__ = "rogue_user_owned"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
'''


def run_control(model_source: str) -> str:
    """Гоняет сторож в отдельном процессе с подставной моделью в реестре."""
    script = CONTROL_TEMPLATE.format(root=str(ROOT), model=model_source)
    p = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=300)
    return p.stdout + p.stderr


def main() -> int:
    print("== 1. Реестр моделей прочитан целиком ==")
    tenancy.import_all_models()
    from app.db import Base

    tables = sorted(m.local_table.name for m in Base.registry.mappers
                    if m.local_table is not None)
    check("реестр непустой (иначе сторож зеленел бы впустую)", len(tables) >= 20,
          f"моделей={len(tables)}")
    check("модель вне app.models тоже в реестре (BillingRequest из routes_extra)",
          "billing_requests" in tables)
    check("обход пакета не пропустил app.models",
          {"orgs", "users", "memberships", "order_plans"} <= set(tables),
          f"всего таблиц={len(tables)}")

    org_refs, user_refs = [], []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        for column in sorted(tenancy._tenant_refs(
                table, tenancy.ORG_ROOT_TABLE, tenancy.ORG_REF_COLUMN_NAMES)):
            org_refs.append(f"{table.name}.{column}")
        for column in sorted(tenancy._tenant_refs(
                table, tenancy.USER_ROOT_TABLE, tenancy.USER_REF_COLUMN_NAMES)):
            user_refs.append(f"{table.name}.{column}")
    print(f"  ссылок на организацию: {len(org_refs)} · {', '.join(sorted(org_refs))}")
    print(f"  ссылок на пользователя: {len(user_refs)} · {', '.join(sorted(user_refs))}")

    print("\n== 2. Каждая ссылка на арендатора объяснена ==")
    problems = tenancy.purge_completeness_violations()
    check("сторож не нашёл нарушений полноты удаления", not problems,
          "; ".join(problems) if problems else "")

    print("\n== 3. Код удаления делает ровно то, что объявлено ==")
    from app.main import _purge_org, _purge_user

    fake = RecordingSession()
    _purge_org(fake, 424242)
    expected_org = [m.__tablename__ for m in tenancy.org_purge_models()] + ["orgs"]
    check("_purge_org удаляет объявленные таблицы в объявленном порядке",
          fake.tables == expected_org,
          f"факт={fake.tables}\n        ожидание={expected_org}")

    fake = RecordingSession()
    _purge_user(fake, 424242)
    expected_user = [m.__tablename__ for m in tenancy.user_purge_models()] + ["users"]
    check("_purge_user удаляет объявленные таблицы в объявленном порядке",
          fake.tables == expected_user,
          f"факт={fake.tables}\n        ожидание={expected_user}")
    check("строка организации удаляется последней, после всех зависимых",
          expected_org[-1] == "orgs")
    check("membership удаляется в ОБОИХ потоках (у неё есть и org_id, и user_id)",
          "memberships" in expected_org and "memberships" in expected_user)

    print("\n== 4. Порядок удаления безопасен по внешним ключам ==")
    order = {m.__tablename__: i for i, m in enumerate(tenancy.org_purge_models())}
    order["orgs"] = len(order)
    broken = []
    for model in tenancy.org_purge_models():
        table = model.__table__
        for column in table.columns:
            for fk in column.foreign_keys:
                target = fk.column.table.name
                if target == table.name or target not in order:
                    continue
                if order[table.name] > order[target]:
                    broken.append(f"{table.name}.{column.name} -> {target}")
    check("зависимая таблица удаляется раньше той, на которую ссылается",
          not broken, "; ".join(sorted(set(broken))))

    print("\n== 5. Отрицательный контроль: сторож ловит неизвестную модель ==")
    out = run_control(CONTROL_ORG_BY_NAME)
    check("подставная модель с org_id названа по имени таблицы",
          "rogue_org_owned" in out and "org_purge_models" in out,
          f"вывод={out.strip()[:300]}")
    out = run_control(CONTROL_ORG_BY_FK)
    check("владение через organization_id -> orgs.id тоже поймано "
          "(сторож смотрит на внешний ключ, а не на имя колонки)",
          "rogue_renamed_org_owned" in out and "organization_id" in out,
          f"вывод={out.strip()[:300]}")
    out = run_control(CONTROL_USER)
    check("подставная модель с user_id названа по имени таблицы",
          "rogue_user_owned" in out and "user_purge_models" in out,
          f"вывод={out.strip()[:300]}")
    check("подставные модели остались в своих процессах: здесь реестр прежний",
          not any(t.startswith("rogue_") for t in
                  (m.local_table.name for m in Base.registry.mappers
                   if m.local_table is not None)))

    print("\n== 6. Allowlist не имеет права протухнуть ==")
    tenancy.ORG_REF_ALLOWLIST[("несуществующая_таблица", "org_id")] = "выдумка теста"
    try:
        stale = tenancy.purge_completeness_violations()
    finally:
        tenancy.ORG_REF_ALLOWLIST.pop(("несуществующая_таблица", "org_id"), None)
    check("запись allowlist без реальной колонки роняет сторож",
          any("несуществующая_таблица" in p for p in stale),
          f"нарушений={len(stale)}")

    tenancy.USER_REF_ALLOWLIST[("user_lessons", "выдуманная_колонка")] = "выдумка теста"
    try:
        stale = tenancy.purge_completeness_violations()
    finally:
        tenancy.USER_REF_ALLOWLIST.pop(("user_lessons", "выдуманная_колонка"), None)
    check("то же для user-allowlist",
          any("выдуманная_колонка" in p for p in stale), f"нарушений={len(stale)}")

    check("после контрольных правок сторож снова чист",
          not tenancy.purge_completeness_violations())

    print("\n== 7. Список удаления не может обратиться к несуществующей колонке ==")
    from app.models import Org

    real = tenancy.org_purge_models
    tenancy.org_purge_models = lambda: real() + (Org,)
    try:
        wrong = tenancy.purge_completeness_violations()
    finally:
        tenancy.org_purge_models = real
    check("модель без колонки org_id в org-наборе роняет сторож "
          "(цикл удаления обращается к model.org_id)",
          any("org_id" in p and "orgs" in p for p in wrong),
          f"нарушения={wrong}")

    # Проверка, что allowlist объясняет, а не отмахивается: пустая причина —
    # это не объяснение, и такую запись нельзя пропускать глазами при ревью.
    print("\n== 8. У каждого исключения есть причина ==")
    empty = [k for k, v in {**tenancy.ORG_REF_ALLOWLIST,
                            **tenancy.USER_REF_ALLOWLIST}.items() if not v.strip()]
    check("ни одной записи allowlist без объяснения", not empty, f"пустые={empty}")

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
