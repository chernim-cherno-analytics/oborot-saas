# -*- coding: utf-8 -*-
"""Отчёт оператора: все организации, их синк и подписка одной таблицей.

ЗАЧЕМ (OPS-WATCH-1, 14.11.2026). Служебное оповещение называет только число
организаций с падающим синком: имён в Telegram нет по правилу канала. Кто
именно, оператор смотрит здесь, на сервере. Отчёт заодно отвечает на
ежедневные вопросы пилота: у кого кончается триал, кто ждёт счёта, у кого
данные устарели.

Только чтение. Секретов не читает и не печатает: токены интеграций не
расшифровываются, в выводе нет ни токенов, ни паролей.

Запуск на сервере (от пользователя сервиса, из каталога приложения):

    python tools/ops_report.py --db /opt/oborot/data/oborot.db
    python tools/ops_report.py --db … --failing     # только падающие синки
    python tools/ops_report.py --db … --json        # для скриптов

Код выхода: 0 всегда, если база открылась; 2, если базы нет.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _connect(db_path: str):
    """Подключение к базе ДО импорта приложения (db.py читает URL при импорте)."""
    path = Path(db_path)
    if not path.is_file():
        print(f"Базы нет: {path}", file=sys.stderr)
        raise SystemExit(2)
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ.setdefault("SCHEDULER_ENABLED", "0")
    sys.path.insert(0, str(ROOT))
    from app.db import SessionLocal

    return SessionLocal()


def _fmt_dt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value[:16].replace("T", " ")
    return value.strftime("%Y-%m-%d %H:%M")


def collect(db, now: datetime | None = None) -> list[dict]:
    from sqlalchemy import func, select

    from app import ops_watch, subscription
    from app.models import Connection, Membership, Org, SyncState, User

    now = now or datetime.utcnow()
    failing = {f["org_id"]: f for f in ops_watch.failing_syncs(db, now)}
    rows = []
    for org in db.execute(select(Org).order_by(Org.id)).scalars():
        # Подключение МойСклада важнее демо: синк и его время — про него.
        conn = (db.execute(select(Connection).where(Connection.org_id == org.id,
                                                    Connection.kind == "moysklad")
                           .order_by(Connection.id.desc())).scalars().first()
                or db.execute(select(Connection).where(Connection.org_id == org.id)
                              .order_by(Connection.id.desc())).scalars().first())
        state = db.get(SyncState, org.id)
        owner = db.execute(
            select(User.email).join(Membership, Membership.user_id == User.id)
            .where(Membership.org_id == org.id, Membership.role == "owner")
        ).scalars().first()
        members = db.execute(select(func.count()).select_from(Membership)
                             .where(Membership.org_id == org.id)).scalar() or 0
        try:
            sub = subscription.state_info(org, db)
        except Exception as exc:  # noqa: BLE001 — отчёт не падает из-за одной строки
            sub = {"state": f"ошибка: {type(exc).__name__}"}
        f = failing.get(org.id)
        rows.append({
            "id": org.id,
            "name": org.name,
            "owner": owner or "",
            "members": int(members),
            "source": getattr(org, "source", "saas"),
            "org_status": getattr(org, "status", "active"),
            "subscription": sub.get("state"),
            "paid_until": sub.get("paid_until"),
            "trial_ends_at": sub.get("trial_ends_at"),
            "connection": f"{conn.kind}/{conn.status}" if conn else "нет",
            "last_sync_ok": _fmt_dt(conn.last_sync_at) if conn else "—",
            "sync_state": getattr(state, "state", "") if state else "",
            "fail_streak": int(getattr(state, "fail_streak", 0) or 0) if state else 0,
            "sync_failing": bool(f),
            "sync_error": (getattr(state, "error", "") or "")[:120] if (state and f) else "",
            "created_at": _fmt_dt(org.created_at),
        })
    return rows


def render(rows: list[dict]) -> str:
    if not rows:
        return "Организаций нет."
    head = ("id", "организация", "владелец", "подписка", "до", "подключение",
            "синк ок", "провалов", "")
    lines = []
    for r in rows:
        until = r["paid_until"] or (f"триал {r['trial_ends_at']}" if r["trial_ends_at"] else "—")
        flag = "СИНК НЕ ПРОХОДИТ: " + (r["sync_error"] or "нет свежих данных") if r["sync_failing"] else ""
        lines.append((str(r["id"]), r["name"][:28], r["owner"][:30], str(r["subscription"]),
                      until, r["connection"], r["last_sync_ok"], str(r["fail_streak"]), flag))
    widths = [max(len(h), *(len(line[i]) for line in lines)) for i, h in enumerate(head)]
    out = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(head)).rstrip()]
    out += ["  ".join(c.ljust(widths[i]) for i, c in enumerate(line)).rstrip() for line in lines]
    failing = sum(1 for r in rows if r["sync_failing"])
    out.append("")
    out.append(f"Всего организаций: {len(rows)}; синк не проходит: {failing}.")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Отчёт оператора: организации, синк, подписка")
    p.add_argument("--db", required=True, help="путь к файлу базы SQLite")
    p.add_argument("--failing", action="store_true", help="только организации с падающим синком")
    p.add_argument("--json", action="store_true", help="вывод в JSON")
    args = p.parse_args(argv)
    db = _connect(args.db)
    try:
        rows = collect(db)
    finally:
        db.close()
    if args.failing:
        rows = [r for r in rows if r["sync_failing"]]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
