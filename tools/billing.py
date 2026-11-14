# -*- coding: utf-8 -*-
"""Оплата клиента: инструмент оператора вместо ручного UPDATE в базе (BILLING-OPS-1).

ЗАЧЕМ. Механизм подписки утверждён и написан (D-24, D-31..D-33): доступ даёт
только `orgs.paid_until`, грейс 5 дней считается от отметки «счёт выставлен».
Но единственным способом всё это проставить был SQL руками на сервере
(`UPDATE orgs SET paid_until=…`, `UPDATE billing_requests SET status=…`), и
поэтому гейт с 22.08 так и не включён: включать его, не умея аккуратно
продлить оплатившего, значит закрыть своих же клиентов. Этот инструмент
закрывает именно эту дыру. Цены и тарифы он не решает.

Команды (запуск на сервере, от пользователя сервиса, из каталога приложения):

    python tools/billing.py --db /opt/oborot/data/oborot.db list
        заявки на счёт и состояние подписки каждой организации

    python tools/billing.py --db … gate-preview
        кого закроет гейт, если включить OBOROT_SUBSCRIPTION_GATE=1

    python tools/billing.py --db … invoiced --request 12 --operator vlad
        счёт по заявке выставлен: начинается грейс (5 календарных дней)

    python tools/billing.py --db … paid --org 7 --until 2026-12-31 --operator vlad [--request 12]
        деньги пришли: оплачено до даты включительно; заявка, если указана,
        закрывается как оплаченная

    python tools/billing.py --db … trial --org 7 --until 2026-11-30 --operator vlad
        продлить пробный период (пилот, переговоры): только вперёд

Правила:
  * дата «оплачено до» не может быть в прошлом и дальше чем через 400 дней:
    опечатка в годе (2062 вместо 2026) иначе дала бы вечный доступ молча;
  * сократить уже оплаченный срок этой командой нельзя (только вперёд):
    возврат денег это отдельное решение, а не опечатка;
  * организации из каталога МойСклада платят в МойСкладе, команды оплаты
    для них отказывают;
  * каждое изменение печатает «было → стало» и дописывает строку в журнал
    оплат `billing_audit.log` рядом с файлом базы (время UTC, оператор, что
    изменено); журнал только пополняется. Имя оператора обязательно.

Код выхода: 0 сделано, 2 отказ (текст объясняет почему).
"""
import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_AHEAD_DAYS = 400

log = logging.getLogger("oborot.billing")


class Refused(Exception):
    pass


AUDIT_PATH: Path | None = None


def _audit(line: str, operator: str) -> None:
    """Строка в журнал оплат рядом с базой. CLI живёт отдельным процессом, и
    журнал приложения (journalctl) его логгер не видит: след «кто и когда
    продлил» должен лежать на диске (ревью 14.11, P2-2)."""
    if AUDIT_PATH is None:
        return
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"{stamp} UTC\t{operator}\t{line}\n")


def _connect(db_path: str):
    global AUDIT_PATH
    path = Path(db_path)
    if not path.is_file():
        print(f"Базы нет: {path}", file=sys.stderr)
        raise SystemExit(2)
    AUDIT_PATH = path.parent / "billing_audit.log"
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ.setdefault("SCHEDULER_ENABLED", "0")
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import inspect

    from app.db import SessionLocal, engine

    cols = {c["name"] for c in inspect(engine).get_columns("orgs")}
    if "paid_until" not in cols:
        raise SystemExit("В базе нет orgs.paid_until: сначала выпустите версию с D-24 штатным deploy "
                         "(колонка создаётся на старте приложения).")
    return SessionLocal()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise Refused(f"Дата «{value}» не в формате ГГГГ-ММ-ДД")


def _check_forward(new: date, current, today: date, what: str) -> None:
    if new < today:
        raise Refused(f"{what}: дата {new} в прошлом")
    if new > today + timedelta(days=MAX_AHEAD_DAYS):
        raise Refused(f"{what}: дата {new} дальше чем через {MAX_AHEAD_DAYS} дней; "
                      "проверьте год")
    if current is not None and new < current:
        raise Refused(f"{what}: новая дата {new} раньше нынешней {current}; "
                      "сокращать срок этой командой нельзя")


def _org(db, org_id: int):
    from app.models import Org

    org = db.get(Org, org_id)
    if org is None:
        raise Refused(f"Организации {org_id} нет")
    if (getattr(org, "source", "saas") or "saas") == "ms_app":
        raise Refused(f"Организация {org_id} подключена из каталога МойСклада: "
                      "оплата и срок там, а не здесь")
    return org


def _stamp_invoiced(db, request_id: int) -> datetime:
    """Отметка «счёт выставлен». Колонка invoiced_at добавлена мини-миграцией
    (app/subscription.ensure_schema) и в модели не объявлена, поэтому SQL.
    Уже стоящая отметка не сдвигается: грейс считается от первого счёта."""
    from sqlalchemy import text

    current = db.execute(text("SELECT invoiced_at FROM billing_requests WHERE id = :id"),
                         {"id": request_id}).scalar()
    if current:
        if isinstance(current, datetime):
            return current
        try:
            return datetime.fromisoformat(str(current)[:19])
        except ValueError:
            raise Refused(f"У заявки {request_id} в отметке счёта не дата: «{current}». "
                          "Исправьте её до продолжения")
    now = datetime.utcnow().replace(microsecond=0)
    db.execute(text("UPDATE billing_requests SET invoiced_at = :ts WHERE id = :id"),
               {"ts": now, "id": request_id})
    return now


def _request(db, request_id: int):
    from app.routes_extra import BillingRequest

    row = db.get(BillingRequest, request_id)
    if row is None:
        raise Refused(f"Заявки {request_id} нет")
    return row


def cmd_list(db) -> int:
    from sqlalchemy import select

    from app import subscription
    from app.models import Org
    from app.routes_extra import BillingRequest

    print("Организации:")
    for org in db.execute(select(Org).order_by(Org.id)).scalars():
        info = subscription.state_info(org, db)
        until = info["paid_until"] or (f"триал до {info['trial_ends_at']}"
                                        if info["trial_ends_at"] else "—")
        grace = f", грейс до {info['grace_until']}" if info["grace_until"] else ""
        print(f"  {org.id:>4}  {org.name[:32]:<32}  {info['state']:<8}  {until}{grace}"
              f"  [{info['source']}]")
    rows = db.execute(select(BillingRequest).order_by(BillingRequest.id.desc())).scalars().all()
    print("\nЗаявки на счёт (новые сверху):")
    if not rows:
        print("  нет")
    for r in rows:
        print(f"  #{r.id:<4} орг {r.org_id:<4} {r.status:<9} {r.plan}/{r.period} "
              f"{r.amount} ₽/мес  {r.company[:30]}  ИНН {r.inn}  {r.email}  "
              f"{r.created_at:%Y-%m-%d}")
    return 0


def cmd_gate_preview(db) -> int:
    from app import subscription

    info = subscription.preview(db)
    c = info["counts"]
    print(f"Гейт сейчас {'ВКЛЮЧЁН' if info['gate_enabled'] else 'выключен'}.")
    print(f"Если включить: active={c.get('active', 0)} grace={c.get('grace', 0)} "
          f"readonly={c.get('readonly', 0)}")
    if info["readonly_org_ids"]:
        print("Закрылись бы организации: " + ", ".join(map(str, info["readonly_org_ids"])))
    if info["broken_org_ids"]:
        print("Состояние не вычислилось (проверьте даты): "
              + ", ".join(map(str, info["broken_org_ids"])))
    return 0


def cmd_invoiced(db, request_id: int, operator: str) -> int:
    from app import subscription

    row = _request(db, request_id)
    org = _org(db, row.org_id)
    status = (row.status or "").strip().lower()
    if status == "paid":
        raise Refused(f"Заявка {request_id} уже оплачена")
    before = row.status
    row.status = "invoiced"
    stamped = _stamp_invoiced(db, row.id)
    db.commit()
    log.warning("billing: заявка %s (орг %s) счёт выставлен оператором %s",
                request_id, org.id, operator)
    _audit(f"invoiced\trequest={request_id}\torg={org.id}\tstatus {before}→invoiced", operator)
    print(f"Заявка #{request_id}: {before} → invoiced; отметка счёта "
          f"{stamped:%Y-%m-%d %H:%M} UTC.")
    print(f"Состояние организации {org.id}: {subscription.subscription_state(org, db)} "
          f"(грейс {subscription.GRACE_DAYS} календарных дней от отметки счёта).")
    return 0


def _raw_column(db, org_id: int, column: str):
    """Значение колонки orgs как есть в базе, до нормализации типов ORM."""
    from sqlalchemy import text

    assert column in ("paid_until", "trial_ends_at")
    return db.execute(text(f"SELECT {column} FROM orgs WHERE id = :i"), {"i": org_id}).scalar()


def cmd_paid(db, org_id: int, until: str, operator: str, request_id: int | None) -> int:
    from app import subscription

    org = _org(db, org_id)
    new = _parse_date(until)
    # Сырое значение колонки, а не атрибут ORM: TolerantDate молча превращает
    # мусор в None, и отказ ниже никогда бы не сработал (ревью Codex, PR #82).
    raw = _raw_column(db, org.id, "paid_until")
    current = subscription._as_date(raw)
    if raw not in (None, "") and current is None:
        raise Refused(f"У организации {org.id} в «оплачено до» не дата: «{raw}». "
                      "Исправьте вручную, проверив, что было оплачено")
    _check_forward(new, current, subscription._today(), "Оплачено до")
    row = None
    if request_id is not None:
        row = _request(db, request_id)
        if row.org_id != org.id:
            raise Refused(f"Заявка {request_id} принадлежит другой организации ({row.org_id})")
    org.paid_until = new
    if row is not None:
        row.status = "paid"
        # Название тарифа доказательством оплаты не является (D-24), но
        # показывать «Триал» оплатившему клиенту неправда: берём тариф заявки.
        org.plan = row.plan
        _stamp_invoiced(db, row.id)
    db.commit()
    log.warning("billing: орг %s оплачено до %s (было %s), оператор %s, заявка %s",
                org.id, new, current, operator, request_id)
    _audit(f"paid\torg={org.id}\tpaid_until {current or '-'}→{new}\trequest={request_id or '-'}", operator)
    print(f"Организация {org.id} «{org.name}»: оплачено до {current or '—'} → {new}.")
    if row is not None:
        print(f"Заявка #{row.id}: → paid.")
    print(f"Состояние: {subscription.subscription_state(org, db)}.")
    return 0


def cmd_trial(db, org_id: int, until: str, operator: str) -> int:
    from app import subscription

    org = _org(db, org_id)
    new = _parse_date(until)
    raw = _raw_column(db, org.id, "trial_ends_at")
    current = subscription._as_date(raw)
    if raw not in (None, "") and current is None:
        raise Refused(f"У организации {org.id} в «пробный период до» не дата: «{raw}». "
                      "Исправьте вручную")
    _check_forward(new, current, subscription._today(), "Пробный период до")
    org.trial_ends_at = datetime.combine(new, datetime.max.time().replace(microsecond=0))
    db.commit()
    log.warning("billing: орг %s пробный период до %s (было %s), оператор %s",
                org.id, new, current, operator)
    _audit(f"trial\torg={org.id}\ttrial_ends {current or '-'}→{new}", operator)
    print(f"Организация {org.id} «{org.name}»: пробный период {current or '—'} → {new}.")
    print(f"Состояние: {subscription.subscription_state(org, db)}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Оплата клиента (оператор)")
    p.add_argument("--db", required=True, help="путь к файлу базы SQLite")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="заявки и состояние подписки")
    sub.add_parser("gate-preview", help="кого закроет гейт")
    s = sub.add_parser("invoiced", help="счёт по заявке выставлен")
    s.add_argument("--request", type=int, required=True)
    s.add_argument("--operator", required=True)
    s = sub.add_parser("paid", help="оплачено до даты")
    s.add_argument("--org", type=int, required=True)
    s.add_argument("--until", required=True, help="ГГГГ-ММ-ДД, включительно")
    s.add_argument("--operator", required=True)
    s.add_argument("--request", type=int, default=None)
    s = sub.add_parser("trial", help="продлить пробный период")
    s.add_argument("--org", type=int, required=True)
    s.add_argument("--until", required=True, help="ГГГГ-ММ-ДД, включительно")
    s.add_argument("--operator", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if hasattr(args, "operator") and not (args.operator or "").strip():
        print("Отказ: укажите оператора (--operator), без имени изменение не делается",
              file=sys.stderr)
        return 2
    db = _connect(args.db)
    try:
        if args.cmd == "list":
            return cmd_list(db)
        if args.cmd == "gate-preview":
            return cmd_gate_preview(db)
        if args.cmd == "invoiced":
            return cmd_invoiced(db, args.request, args.operator)
        if args.cmd == "paid":
            return cmd_paid(db, args.org, args.until, args.operator, args.request)
        if args.cmd == "trial":
            return cmd_trial(db, args.org, args.until, args.operator)
    except Refused as exc:
        db.rollback()
        print(f"Отказ: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
