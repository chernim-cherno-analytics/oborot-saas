# -*- coding: utf-8 -*-
"""Восстановление доступа: инструмент оператора на сервере (D-63).

Публичного «забыли пароль» у продукта нет и не будет без решения владельца.
Доступ возвращает оператор, и только так:

1. при сопровождаемом подключении — подтверждает и записывает телефон и
   Telegram человека (`contact-set`);
2. при просьбе о восстановлении — перезванивает ТОЛЬКО на записанный телефон,
   а не на номер, с которого пришла просьба;
3. выпускает одноразовую ссылку (`issue`) и отправляет её ТОЛЬКО в записанный
   Telegram. Ссылка живёт 30 минут; прежние ссылки этого человека гаснут.

Без обоих подтверждённых контактов `issue` отказывает. Имя пользователя,
e-mail или знание счёта не заменяют ни один из шагов.

Секретов инструмент не читает: ему нужен только путь к базе. Ссылка печатается
ОДИН раз — это и есть её доставка оператору; в базе и в журнале её нет.

Запуск на сервере (от пользователя сервиса, из каталога приложения):

    python tools/account_recovery.py --db /opt/oborot/data/oborot.db \\
        contact-set --email owner@brand.ru --kind phone --value "+7 999 123-45-67" \\
        --operator vlad --method "видеозвонок при подключении, сверено с договором"

    python tools/account_recovery.py --db … contact-list --email owner@brand.ru

    python tools/account_recovery.py --db … issue --email owner@brand.ru \\
        --operator vlad --base-url https://app.example.ru \\
        --callback-confirmed "27.09 14:05 перезвонил на записанный номер, подтвердил"

    python tools/account_recovery.py --db … events --email owner@brand.ru

Код выхода: 0 — сделано, 2 — отказ (текст объясняет почему).
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _connect(db_path: str):
    """Подключение к базе ДО импорта приложения (db.py читает URL при импорте)."""
    path = Path(db_path)
    if not path.is_file():
        raise SystemExit(f"Базы нет: {path}")
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ.setdefault("SCHEDULER_ENABLED", "0")
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import inspect

    from app.db import SessionLocal, engine

    # Схему создаёт само приложение на старте (init_db и журнал миграций).
    # Инструмент миграций не запускает: из оператора, мимо порядка старта и,
    # возможно, от другого пользователя ОС это было бы ручной самодеятельностью.
    if "password_resets" not in inspect(engine).get_table_names():
        raise SystemExit("В базе нет таблиц восстановления — сначала выпустите версию "
                         "с D-63 штатным deploy (таблицы создаются на старте приложения).")
    return SessionLocal()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Восстановление доступа (D-63)")
    p.add_argument("--db", required=True, help="путь к файлу базы SQLite")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("contact-set", help="записать подтверждённый контакт")
    s.add_argument("--email", required=True)
    s.add_argument("--kind", required=True, choices=("phone", "telegram"))
    s.add_argument("--value", required=True)
    s.add_argument("--operator", required=True)
    s.add_argument("--method", required=True, help="как именно подтверждён контакт")
    s.add_argument("--reverify", action="store_true",
                   help="заменить уже записанный контакт после новой проверки")

    s = sub.add_parser("contact-list", help="показать контакты (в маске)")
    s.add_argument("--email", required=True)

    s = sub.add_parser("issue", help="выпустить одноразовую ссылку")
    s.add_argument("--email", required=True)
    s.add_argument("--operator", required=True)
    s.add_argument("--base-url", required=True, help="адрес сервиса, например https://…")
    s.add_argument("--callback-confirmed", required=True,
                   help="когда и как перезвонили на ЗАПИСАННЫЙ телефон")

    s = sub.add_parser("events", help="журнал действий с доступом")
    s.add_argument("--email", required=True)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db = _connect(args.db)
    from app import account_recovery as ar

    try:
        if args.cmd == "contact-set":
            row = ar.set_contact(db, email=args.email, kind=args.kind, value=args.value,
                                 operator=args.operator, method=args.method,
                                 reverify=args.reverify)
            print(f"Записано: {row.kind} {ar.mask(row.kind, row.value)}, "
                  f"подтвердил {row.verified_by}")
        elif args.cmd == "contact-list":
            rows = ar.contacts(db, args.email)
            if not rows:
                print("Подтверждённых контактов нет — сброс не выдаётся.")
            for r in rows:
                print(f"{r.kind:9} {ar.mask(r.kind, r.value):18} {r.verified_at:%Y-%m-%d %H:%M} "
                      f"{r.verified_by}: {r.verified_method}")
        elif args.cmd == "issue":
            if not args.base_url.startswith("https://"):
                raise ar.RecoveryRefused("Адрес сервиса должен начинаться с https://")
            token, telegram = ar.issue_reset(db, email=args.email, operator=args.operator,
                                             callback_note=args.callback_confirmed)
            print(f"Отправьте ТОЛЬКО в записанный Telegram {telegram}. Ссылка одноразовая, "
                  f"действует {int(ar.RESET_TTL.total_seconds() // 60)} минут:")
            print(ar.reset_link(args.base_url, token))
        elif args.cmd == "events":
            for e in ar.events(db, args.email):
                print(f"{e.at:%Y-%m-%d %H:%M:%S} {e.kind:20} {e.actor:12} {e.detail_json}")
    except ar.RecoveryRefused as exc:
        print(f"Отказ: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
