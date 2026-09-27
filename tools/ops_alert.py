# -*- coding: utf-8 -*-
"""Операционное оповещение в служебный чат сервиса (не в чат организации).

    python tools/ops_alert.py test
    python tools/ops_alert.py unit-failed oborot-offsite-backup.service

Зовёт его systemd: `OnFailure=oborot-ops-alert@%n.service` у юнитов
офсайт-бэкапа и учения. Токен бота и чат сервиса приходят из окружения
(`EnvironmentFile=/opt/oborot/env`) — не аргументами командной строки.

Текст фиксированный (app/notify.py, ops_text): ни журнала, ни путей, ни имён
организаций, ни секретов, ни строк извне. Юнит — только из явного списка.

Код выхода:
  0 — Telegram принял сообщение;
  1 — не отправлено (нет настройки, Telegram отказал, нет связи);
  2 — неверный ввод (вид оповещения или юнит не из списка).
На экран — только строка статуса: ни текста сообщения, ни значений настроек.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Операционное оповещение в служебный чат")
    sub = p.add_subparsers(dest="kind", required=True)
    sub.add_parser("test", help="проверка служебного канала")
    s = sub.add_parser("unit-failed", help="сбой юнита из списка")
    s.add_argument("unit")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from app import notify

    unit = getattr(args, "unit", None)
    try:
        notify.ops_text(args.kind, unit)
    except ValueError as exc:
        print(f"Отказ: {exc}", file=sys.stderr)
        return 2
    ok, reason = notify.send_ops(args.kind, unit)
    if ok:
        print("Служебное оповещение отправлено.")
        return 0
    print(f"Служебное оповещение НЕ отправлено: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
