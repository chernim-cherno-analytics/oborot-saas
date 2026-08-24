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
     объяснена записью в списке не-владеющих ссылок. Семантически: по колонкам
     и внешним ключам из `Base.registry`, а не по тексту исходников;
  2) СПИСКИ НЕ РАЗЪЕХАЛИСЬ С КОДОМ — `_purge_org` и `_purge_user` выполняют
     ровно те DELETE и ровно в том порядке, что объявлены в `app.tenancy`;
  3) ПОРЯДОК БЕЗОПАСЕН ПО ВНЕШНИМ КЛЮЧАМ — проверку делает
     `app.tenancy.purge_order_violations()`, а не код внутри этого файла:
     сторож должен жить рядом с порядком и переиспользоваться, иначе тот, кто
     поменяет порядок завтра, о проверке не узнает. Отрицательный контроль
     подсовывает заведомо перевёрнутый набор и требует назвать конкретный ключ;
  4) ОТРИЦАТЕЛЬНЫЙ КОНТРОЛЬ ПОЛНОТЫ — сторож обязан ЛОВИТЬ неизвестную модель,
     а не просто зеленеть. Три подставные модели: с `org_id`, со ссылкой на
     `orgs.id` под ДРУГИМ именем колонки (доказывает, что сторож смотрит на
     внешние ключи, а не на имя) и с `user_id`. Каждая регистрируется в
     ОТДЕЛЬНОМ ПОДПРОЦЕССЕ: реестр и `Base.metadata` этого процесса умирают
     вместе с ним и соседним наборам не мешают;
  5) СЛОМАННЫЙ ИМПОРТ НЕ ДАЁТ ЗЕЛЁНОГО СТОРОЖА — подставной app-модуль, который
     падает при импорте, обязан уронить сторож наружу. Пока `import_all_models`
     глотал исключения, такой модуль просто исчезал бы из реестра вместе со
     своей моделью, и сторож зеленел бы ровно тогда, когда обязан кричать;
  6) СПИСКИ НЕ-ВЛАДЕЮЩИХ ССЫЛОК ПРОВЕРЯЮТСЯ В ОБЕ СТОРОНЫ — запись, которой
     больше не соответствует реальная колонка, тоже роняет сторож. Протухший
     список исключений опаснее отсутствующего;
  7) УДАЛЕНИЕ РЕАЛЬНО ИСПОЛНЯЕТСЯ — отдельная SQLite-база с
     `PRAGMA foreign_keys=ON` и по строке в каждой таблице набора. Структурная
     проверка порядка доказывает топологию, но не то, что порядок исполним;
     это доказывает только настоящий DELETE при включённых ключах. Там же
     воспроизведён открытый дефект SEC-8 — удаление автора заявки на счёт.

Сеть и сервер не поднимаются. База поднимается только в разделе 10 и только
своя, в памяти процесса: рабочая база приложения не трогается.

Запуск из корня репозитория:  python tests/test_tenancy.py
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# База приложения не нужна: таблицы не создаются, запросы не выполняются. Но
# `app.db` читает DATABASE_URL при импорте, поэтому значение обязано быть.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SCHEDULER_ENABLED"] = "0"

from sqlalchemy import create_engine, event, func, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session as SASession  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

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
    есть то, что ломается молча (в SQLite внешние ключи по умолчанию не
    проверяются, и ошибка вылезет только на Postgres или на осиротевших
    строках).
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

# ── Подставной app-модуль со сломанным импортом ──────────────────────────────
# Кладётся В САМ ПАКЕТ app: `import_all_models` обходит `app.__path__`, и модуль
# из другого места этот обход не увидел бы — а проверить надо ровно то, что
# случится с БУДУЩИМ app-модулем. Файл удаляется в `finally`, а отдельная
# проверка ниже требует, чтобы после набора его на диске не осталось.
BROKEN_MODULE_NAME = "_control_broken_import"
BROKEN_MODULE_PATH = ROOT / "app" / f"{BROKEN_MODULE_NAME}.py"
BROKEN_MODULE_SOURCE = '''# -*- coding: utf-8 -*-
"""Подставной модуль теста: будущий app-модуль с моделью и сломанным импортом.

Ошибка поднимается ДО объявления модели — так сломанный импорт и выглядит:
класс не выполняется, модель в реестр не попадает, реестр становится неполным
и сторож полноты обязан кричать, а не зеленеть.

Если этот файл виден в репозитории — его забыл удалить tests/test_tenancy.py.
"""
raise RuntimeError("сломанный импорт подставного модуля")

# Ниже — недостижимо, и в этом весь смысл: модель со ссылкой на организацию,
# до объявления которой импорт не доживает.
# class LostOrgOwned(Base):
#     __tablename__ = "lost_org_owned"
#     org_id: Mapped[int] = mapped_column(ForeignKey("orgs.id"), nullable=False)
'''

BROKEN_IMPORT_PROBE = '''
import os, sys
sys.path.insert(0, {root!r})
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SCHEDULER_ENABLED"] = "0"

from app import tenancy
violations = tenancy.purge_completeness_violations()
print("СТОРОЖ ВЕРНУЛ ОТВЕТ, нарушений:", len(violations))
'''

# Тот же подставной модуль, но с ПРЕЖНЕЙ реализацией обхода — той, что глотала
# любое исключение. Без этого контроля проверка выше не доказывала бы, что
# правка что-то изменила: она могла бы падать по любой другой причине.
FAIL_OPEN_PROBE = '''
import importlib, os, pkgutil, sys
sys.path.insert(0, {root!r})
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SCHEDULER_ENABLED"] = "0"

from app import tenancy

def fail_open(package="app"):
    pkg = importlib.import_module(package)
    for info in pkgutil.iter_modules(pkg.__path__):
        try:
            importlib.import_module(f"{{package}}.{{info.name}}")
        except Exception:
            continue

tenancy.import_all_models = fail_open
print("СТОРОЖ ВЕРНУЛ ОТВЕТ, нарушений:", len(tenancy.purge_completeness_violations()))
'''


def run_control(model_source: str) -> str:
    """Гоняет сторож в отдельном процессе с подставной моделью в реестре."""
    script = CONTROL_TEMPLATE.format(root=str(ROOT), model=model_source)
    p = subprocess.run([sys.executable, "-c", script], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=300)
    return p.stdout + p.stderr


def _sample_value(column):
    """Значение-заглушка для обязательной колонки при засеве базы раздела 10."""
    kind = str(column.type).upper()
    if "INT" in kind:
        return 1
    if "FLOAT" in kind or "REAL" in kind or "NUMERIC" in kind:
        return 1.0
    if "BOOL" in kind:
        return False
    return "2026-01-01" if column.name == "date" else "x"


def _fk_engine(Base):
    """Отдельная SQLite-база в памяти с ВКЛЮЧЁННОЙ проверкой внешних ключей.

    Своя, а не рабочая: набор обязан иметь право удалять всё подряд. Ключи
    включаются на каждом соединении — по умолчанию SQLite их не проверяет, и
    именно поэтому неверный порядок удаления жил в проде незамеченным.
    """
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):  # noqa: ANN001
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return engine


def _seed_org(session, org_name: str, email: str) -> tuple[int, int]:
    """По строке в КАЖДОЙ таблице набора удаления организации.

    Порядок вставки — набор удаления НАОБОРОТ: родитель раньше ребёнка. Это не
    трюк, а та же топология с другой стороны: если засев в обратном порядке
    проходит при включённых ключах, значит объявленный порядок удаления и есть
    правильный. Обязательные колонки заполняются заглушками, ссылки — теми
    строками, что уже созданы.
    """
    from app.models import Org, User

    org = Org(name=org_name)
    user = User(email=email, pw_hash="x")
    session.add_all([org, user])
    session.flush()

    made = {"orgs": org.id, "users": user.id}
    for model in reversed(tenancy.org_purge_models()):
        row = model()
        for column in model.__table__.columns:
            keys = list(column.foreign_keys)
            if keys and keys[0].column.table.name in made:
                setattr(row, column.name, made[keys[0].column.table.name])
                continue
            if column.primary_key and not keys and "INT" in str(column.type).upper():
                continue  # автоинкремент
            if (column.nullable or column.default is not None
                    or column.server_default is not None):
                continue
            setattr(row, column.name, _sample_value(column))
        session.add(row)
        session.flush()
        made.setdefault(model.__tablename__, getattr(row, "id", None))
    session.commit()
    return org.id, user.id


def _swap_order(models: tuple, first: str, second: str) -> tuple:
    """Меняет местами две модели набора — заведомо неверный порядок."""
    items = list(models)
    i = next(n for n, m in enumerate(items) if m.__tablename__ == first)
    j = next(n for n, m in enumerate(items) if m.__tablename__ == second)
    items[i], items[j] = items[j], items[i]
    return tuple(items)


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
    check("сторож порядка живёт в app.tenancy, а не в этом файле",
          callable(getattr(tenancy, "purge_order_violations", None)))
    order_problems = tenancy.purge_order_violations()
    check("объявленный порядок не нарушает ни одного внешнего ключа",
          not order_problems, "; ".join(order_problems))

    inverted = _swap_order(tenancy.org_purge_models(), "production_orders", "productions")
    bad = tenancy.purge_order_violations(org_models=inverted)
    check("перевёрнутый Production/ProductionOrder пойман",
          bool(bad), f"нарушений={len(bad)}")
    check("в сообщении названы обе таблицы и сам ключ: "
          "production_orders.production_id -> productions",
          any("production_orders.production_id -> productions" in p for p in bad),
          f"сообщения={bad}")
    check("сообщение говорит, где чинить (app.tenancy.org_purge_models())",
          any("org_purge_models()" in p for p in bad), f"сообщения={bad}")

    real_org_models = tenancy.org_purge_models
    tenancy.org_purge_models = lambda: inverted
    try:
        through_guard = tenancy.purge_completeness_violations()
    finally:
        tenancy.org_purge_models = real_org_models
    check("общий сторож полноты тоже показывает нарушение порядка "
          "(проверка не потерялась при переносе)",
          any("production_orders.production_id -> productions" in p
              for p in through_guard), f"нарушения={through_guard}")

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

    print("\n== 6. Сломанный импорт не имеет права дать зелёный сторож ==")
    # Модуль будущего: он есть в пакете, в нём модель, и он не импортируется.
    # Прежняя реализация `import_all_models` молча пропускала бы его — реестр
    # оказался бы неполным, а сторож зелёным. Проверяем ровно это.
    BROKEN_MODULE_PATH.write_text(BROKEN_MODULE_SOURCE, encoding="utf-8")
    try:
        probe = subprocess.run(
            [sys.executable, "-c", BROKEN_IMPORT_PROBE.format(root=str(ROOT))],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300)
        fail_open = subprocess.run(
            [sys.executable, "-c", FAIL_OPEN_PROBE.format(root=str(ROOT))],
            cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    finally:
        BROKEN_MODULE_PATH.unlink(missing_ok=True)
    combined = probe.stdout + probe.stderr
    check("сторож не вернул зелёный ответ на сломанном импорте",
          "СТОРОЖ ВЕРНУЛ ОТВЕТ" not in probe.stdout,
          f"stdout={probe.stdout.strip()[:200]}")
    check("процесс упал ненулевым кодом", probe.returncode != 0,
          f"код={probe.returncode}")
    check("ошибка импорта дошла наружу и названа по имени модуля",
          BROKEN_MODULE_NAME in combined
          and "сломанный импорт подставного модуля" in combined,
          f"вывод={combined.strip()[-300:]}")
    check("прежняя реализация на этом же модуле зеленела — значит контроль "
          "проверяет именно правку, а не что-то соседнее",
          "СТОРОЖ ВЕРНУЛ ОТВЕТ, нарушений: 0" in fail_open.stdout,
          f"stdout={fail_open.stdout.strip()[:200]}")
    check("подставной модуль убран с диска",
          not BROKEN_MODULE_PATH.exists(), str(BROKEN_MODULE_PATH))
    check("после контроля сторож снова чист",
          not tenancy.purge_completeness_violations())

    print("\n== 7. Список не-владеющих ссылок не имеет права протухнуть ==")
    tenancy.NON_OWNING_ORG_REFERENCES[("несуществующая_таблица", "org_id")] = "выдумка теста"
    try:
        stale = tenancy.purge_completeness_violations()
    finally:
        tenancy.NON_OWNING_ORG_REFERENCES.pop(("несуществующая_таблица", "org_id"), None)
    check("запись без реальной колонки роняет сторож",
          any("несуществующая_таблица" in p for p in stale),
          f"нарушений={len(stale)}")

    tenancy.NON_OWNING_USER_REFERENCES[("user_lessons", "выдуманная_колонка")] = "выдумка теста"
    try:
        stale = tenancy.purge_completeness_violations()
    finally:
        tenancy.NON_OWNING_USER_REFERENCES.pop(("user_lessons", "выдуманная_колонка"), None)
    check("то же для ссылок на пользователя",
          any("выдуманная_колонка" in p for p in stale), f"нарушений={len(stale)}")

    check("после контрольных правок сторож снова чист",
          not tenancy.purge_completeness_violations())

    print("\n== 8. Список удаления не может обратиться к несуществующей колонке ==")
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

    # Проверка, что список объясняет, а не отмахивается: пустая причина — это не
    # объяснение, и такую запись нельзя пропускать глазами при ревью.
    print("\n== 9. У каждого исключения есть причина ==")
    empty = [k for k, v in {**tenancy.NON_OWNING_ORG_REFERENCES,
                            **tenancy.NON_OWNING_USER_REFERENCES}.items() if not v.strip()]
    check("ни одной записи без объяснения", not empty, f"пустые={empty}")
    check("исключение по billing_requests.user_id прямо называет открытый SEC-8, "
          "а не выдаёт ссылку за безопасную",
          "SEC-8" in tenancy.NON_OWNING_USER_REFERENCES[("billing_requests", "user_id")],
          tenancy.NON_OWNING_USER_REFERENCES[("billing_requests", "user_id")][:80])

    print("\n== 10. Удаление реально исполняется при включённых внешних ключах ==")
    # Структурная проверка порядка (раздел 4) доказывает топологию, но не то,
    # что порядок ИСПОЛНИМ: в объявленном наборе может не хватать таблицы, а
    # заглушка RecordingSession ничего не выполняет. Здесь настоящая база.
    from app.models import Membership, Org as OrgModel, User as UserModel
    from app.models import UserHintSeen, UserLesson, UserPrefs
    from app.routes_extra import BillingRequest

    engine = _fk_engine(Base)
    session = SASession(engine)
    check("внешние ключи в контрольной базе действительно включены",
          session.execute(text("PRAGMA foreign_keys")).scalar() == 1)

    victim_org, _victim_user = _seed_org(session, "Уходит", "victim@example.test")
    neighbor_org, _n_user = _seed_org(session, "Остаётся", "neighbor@example.test")
    seeded = {m.__tablename__: session.execute(
        select(func.count()).select_from(m.__table__)
        .where(m.__table__.c.org_id == victim_org)).scalar()
        for m in tenancy.org_purge_models()}
    check("засеяна строка в каждой таблице набора удаления",
          all(seeded.values()), f"пустые={[k for k, v in seeded.items() if not v]}")

    executed = True
    try:
        _purge_org(session, victim_org)
        session.commit()
    except IntegrityError as exc:
        executed = False
        session.rollback()
        check("удаление организации прошло при включённых ключах", False,
              str(exc).splitlines()[0][:200])
    if executed:
        check("удаление организации прошло при включённых ключах", True,
              "ни одного нарушения внешнего ключа")
        left = {m.__tablename__: session.execute(
            select(func.count()).select_from(m.__table__)
            .where(m.__table__.c.org_id == victim_org)).scalar()
            for m in tenancy.org_purge_models()}
        check("после удаления не осталось ни одной строки организации",
              not any(left.values()), f"осталось={[k for k, v in left.items() if v]}")
        kept = {m.__tablename__: session.execute(
            select(func.count()).select_from(m.__table__)
            .where(m.__table__.c.org_id == neighbor_org)).scalar()
            for m in tenancy.org_purge_models()}
        check("данные соседней организации не задеты",
              all(kept.values()), f"пусто={[k for k, v in kept.items() if not v]}")
        check("сама строка организации удалена",
              session.execute(select(func.count()).select_from(OrgModel.__table__)
                              .where(OrgModel.id == victim_org)).scalar() == 0)
    session.close()

    # Отрицательный контроль: та же база, тот же засев, порядок перевёрнут.
    # Без него зелёная проверка выше не доказывала бы ничего: она могла быть
    # зелёной и потому, что ключи на самом деле не проверяются.
    engine = _fk_engine(Base)
    session = SASession(engine)
    control_org, _ = _seed_org(session, "Контроль", "control@example.test")
    tenancy.org_purge_models = lambda: inverted
    raised = None
    try:
        _purge_org(session, control_org)
        session.commit()
    except IntegrityError as exc:
        raised = exc
        session.rollback()
    finally:
        tenancy.org_purge_models = real_org_models
    check("перевёрнутый порядок РЕАЛЬНО падает на внешнем ключе "
          "(значит проверка ключей включена, а зелёный выше не случаен)",
          raised is not None,
          "" if raised else "DELETE прошёл — ключи не проверялись")
    session.close()

    # Удаление пользователя: что сегодня исполнимо, а что нет.
    engine = _fk_engine(Base)
    session = SASession(engine)
    org = OrgModel(name="Живая организация")
    clean = UserModel(email="clean@example.test", pw_hash="x")
    payer = UserModel(email="payer@example.test", pw_hash="x")
    session.add_all([org, clean, payer])
    session.flush()
    session.add_all([
        Membership(user_id=clean.id, org_id=org.id),
        Membership(user_id=payer.id, org_id=org.id),
        UserPrefs(user_id=clean.id),
        UserLesson(user_id=clean.id, lesson="intro"),
        UserHintSeen(user_id=clean.id, page="orders"),
        BillingRequest(org_id=org.id, user_id=payer.id, plan="pro"),
    ])
    session.commit()

    clean_ok = True
    try:
        _purge_user(session, clean.id)
        session.commit()
    except IntegrityError as exc:
        clean_ok = False
        session.rollback()
        print(f"       {str(exc).splitlines()[0][:160]}")
    check("пользователь без org-owned следов удаляется при включённых ключах",
          clean_ok and session.execute(
              select(func.count()).select_from(UserModel.__table__)
              .where(UserModel.id == clean.id)).scalar() == 0)
    check("личные следы ушедшего стёрты",
          session.execute(select(func.count()).select_from(UserPrefs.__table__)
                          .where(UserPrefs.user_id == clean.id)).scalar() == 0)

    # SEC-8, воспроизведение, а не норма. Эта проверка — ТРЕКЕР ОТКРЫТОГО ДОЛГА:
    # она фиксирует, что сегодня удаление автора заявки на счёт при живой
    # организации внешним ключом ЗАПРЕЩЕНО (в Postgres будет то же самое).
    # Когда SEC-8 закроют — заявка перестанет держать пользователя, — эта
    # проверка обязана упасть и быть удалённой вместе с записью долга.
    blocked = None
    try:
        _purge_user(session, payer.id)
        session.commit()
    except IntegrityError as exc:
        blocked = str(exc).splitlines()[0]
        session.rollback()
    check("SEC-8 ВОСПРОИЗВЁЛСЯ: автор заявки на счёт не удаляется при живой "
          "организации (billing_requests.user_id — NOT NULL FK). Это открытый "
          "дефект, а не поведение, которое мы закрепляем",
          blocked is not None,
          blocked[:120] if blocked else "удаление прошло — проверь, не закрыт ли SEC-8")
    session.close()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалены:")
        for name in FAIL:
            print(f"  - {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
