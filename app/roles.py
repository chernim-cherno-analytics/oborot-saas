# -*- coding: utf-8 -*-
"""Роли и права сотрудников организации (решение владельца 15.11.2026).

«Нужно, чтобы были роли и владелец мог их раздавать. Условно как в МойСкладе.»

Как устроено:
  * ПРАВА фиксированы в коде (PERMS): ключ, русское название, группа. Каждая
    пишущая ручка API требует ровно одно право (auth.require_perm). Просмотр
    и выгрузки (GET, xlsx) права не требуют: они доступны любой роли.
  * РОЛЬ организации (таблица org_roles) это имя и набор прав. Три роли
    встроенные и создаются лениво при первом обращении к разделу
    «Сотрудники и роли»:
      - «Владелец»: все права; не редактируется и не удаляется;
      - «Сотрудник»: РОВНО права прежнего участника (D-69, вариант A);
      - «Наблюдатель»: только просмотр и выгрузки.
    Права встроенных ролей берутся из кода, а не из базы: у старой записи
    в базе не может «разъехаться» смысл встроенной роли.
    Свои роли владелец собирает галочками.
  * ВЛАДЕНИЕ по-прежнему определяется колонкой memberships.role ("owner"):
    на ней держится удаление аккаунта, передача организации и всё, что было
    написано до ролей. memberships.role_id нужен только не-владельцам. Код
    назначения ролей держит обе колонки согласованными: роль «Владелец»
    = role "owner", любая другая = role "member".
  * СТАРЫЕ УЧАСТНИКИ (role "member", role_id пустой) получают права
    «Сотрудника», то есть ровно то, что у них было. Миграция данных для
    этого не нужна.

Откат кода: старый код читает только memberships.role. Владельцы остаются
владельцами, все остальные (в том числе «Наблюдатель» и свои роли) снова
получают права прежнего участника. Это ограничение записано в TECH_DEBT.md.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# (ключ, название, группа). Порядок здесь = порядок в матрице на странице.
PERMS: tuple[tuple[str, str, str], ...] = (
    ("plan", "Считать и сохранять планы заказа", "Заказы"),
    ("orders_create", "Создавать заказы", "Заказы"),
    ("orders_manage", "Вести заказы: статусы и приёмка", "Заказы"),
    ("orders_delete", "Удалять заказы", "Заказы"),
    ("orders_push_ms", "Отправлять заказы в МойСклад", "Заказы"),
    ("ordered_edit", "Править «Едет» и «Заказано» вручную", "Заказы"),
    ("drafts_edit", "Править черновик ростовки по позиции", "Заказы"),
    ("drafts_reset_all", "Сбрасывать всю таблицу ростовки", "Заказы"),
    ("catalog_edit", "Каталог: архив, категории, скидки, исключения", "Каталог"),
    ("supply_edit", "Править раздел «Поставки»", "Поставки"),
    ("settings", "Настройки, подключения, синхронизация, производства и уведомления", "Организация"),
    ("billing", "Тариф и оплата", "Организация"),
    ("users_manage", "Сотрудники и роли", "Организация"),
)
PERM_KEYS: tuple[str, ...] = tuple(k for k, _t, _g in PERMS)
PERM_TITLES: dict[str, str] = {k: t for k, t, _g in PERMS}

OWNER = "owner"
EMPLOYEE = "employee"
VIEWER = "viewer"

# Права «Сотрудника» = права участника до ролей (docstring прежнего
# auth.require_owner_api, D-69 вариант A): считает планы, создаёт заказы,
# переводит их по статусам, вносит приёмку, правит и сбрасывает общий
# черновик ростовки (в том числе целиком).
EMPLOYEE_PERMS: frozenset[str] = frozenset({
    "plan", "orders_create", "orders_manage", "drafts_edit", "drafts_reset_all",
})

BUILTIN: dict[str, dict] = {
    OWNER: {"name": "Владелец", "perms": frozenset(PERM_KEYS)},
    EMPLOYEE: {"name": "Сотрудник", "perms": EMPLOYEE_PERMS},
    VIEWER: {"name": "Наблюдатель", "perms": frozenset()},
}
BUILTIN_ORDER = (OWNER, EMPLOYEE, VIEWER)

ROLE_NAME_MAX = 64


def perm_title(key: str) -> str:
    return PERM_TITLES.get(key, key)


def denied_detail(key: str) -> str:
    """Текст отказа 403: какого права не хватает и к кому идти."""
    title = perm_title(key)
    title = title[:1].lower() + title[1:]
    return (f"Недостаточно прав: {title}. "
            "Попросите владельца выдать роль с этим правом")


def parse_perms(raw: str | None) -> frozenset[str]:
    """Права из perms_json. Неизвестные ключи отбрасываются (fail-closed)."""
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(k for k in data if isinstance(k, str) and k in PERM_KEYS)


def dump_perms(perms) -> str:
    return json.dumps([k for k in PERM_KEYS if k in set(perms)], ensure_ascii=False)


def role_perms(role) -> frozenset[str]:
    """Права роли: у встроенной из кода, у своей из базы."""
    if role is None:
        return frozenset()
    if role.builtin in BUILTIN:
        return BUILTIN[role.builtin]["perms"]
    return parse_perms(role.perms_json)


def member_perms(db: Session, member) -> tuple[frozenset[str], str]:
    """(права, название роли) участника организации.

    Владелец (memberships.role == "owner") получает все права независимо от
    role_id. Не-владелец с пустым role_id (участник до ролей, участник из
    каталога МойСклада, созданный старым кодом) получает права «Сотрудника».
    Ссылка на чужую или несуществующую роль даёт ПУСТОЙ набор (fail-closed):
    такого состояния код назначения не создаёт, и угадывать права нельзя.
    """
    from app.models import OrgRole

    if (member.role or "member") == "owner":
        return BUILTIN[OWNER]["perms"], BUILTIN[OWNER]["name"]
    if member.role_id is None:
        return BUILTIN[EMPLOYEE]["perms"], BUILTIN[EMPLOYEE]["name"]
    role = db.get(OrgRole, member.role_id)
    if role is None or role.org_id != member.org_id or role.builtin == OWNER:
        # Роль «Владелец» без role="owner" владельцем не делает: владение
        # решает только memberships.role (см. докстринг модуля).
        return frozenset(), "Без роли"
    return role_perms(role), role.name


def ensure_builtin_roles(db: Session, org_id: int) -> dict[str, object]:
    """Создаёт недостающие встроенные роли организации; возвращает {код: роль}.

    Лениво и идемпотентно. Гонку двух запросов закрывает частичный
    уникальный индекс (org_id, builtin): проигравший откатывает свою вставку
    и перечитывает.
    """
    from app.models import OrgRole

    def _load() -> dict[str, object]:
        rows = db.execute(
            select(OrgRole).where(OrgRole.org_id == org_id, OrgRole.builtin != "")
        ).scalars().all()
        return {r.builtin: r for r in rows}

    have = _load()
    missing = [code for code in BUILTIN_ORDER if code not in have]
    if not missing:
        return have
    for code in missing:
        spec = BUILTIN[code]
        db.add(OrgRole(org_id=org_id, name=spec["name"], builtin=code,
                       perms_json=dump_perms(spec["perms"]),
                       created_at=datetime.utcnow()))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return _load()


def owner_count(db: Session, org_id: int) -> int:
    from app.models import Membership

    return int(db.execute(
        select(func.count()).select_from(Membership).where(
            Membership.org_id == org_id, Membership.role == "owner")
    ).scalar() or 0)


def apply_role(member, role) -> None:
    """Назначает роль, держа memberships.role и role_id согласованными."""
    member.role_id = role.id
    member.role = "owner" if role.builtin == OWNER else "member"


def ui_role(ctx, perm: str) -> str:
    """"owner", если у роли есть право `perm`, иначе "member".

    Для старых слоёв, которые получают на вход строку роли и решают по ней,
    что показать и что разрешить (доска «Поставок», предпросмотр таблиц).
    Их внутренние сравнения с "owner" после ролей значат «есть право
    записи в этот раздел», и переписывать их ради этого незачем.
    """
    return "owner" if perm in ctx.perms else "member"
