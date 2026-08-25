# -*- coding: utf-8 -*-
"""Проверка того, что восстановленная база ещё и РАСШИФРОВЫВАЕТСЯ.

Зачем отдельная проверка. Учение по восстановлению доказывало, что данные
доехали из хранилища и что на них стартует приложение. Обе проверки проходят на
базе, все токены МойСклада в которой превратились в мусор: `connections
.token_enc` — это Fernet-шифртекст, ключ которого выводится из `OBOROT_SECRET`
(`app/crypto.py`), а `OBOROT_SECRET` в бэкапе не лежит и лежать не должен.
Восстановление без него даёт систему, которая поднимается, показывает старые
цифры — и не может ни синхронизироваться, ни записать заказ. То есть авария
выглядит как «восстановились», а по делу восстановления не произошло.

Поэтому секрет для проверки берётся ИЗВНЕ — из отдельной офсайт-копии
(`OBOROT_RECOVERY_SECRET_FILE`), а не из окружения приложения. Проверяется ровно
то, что понадобится в день аварии: этой копией секрета сохранённые токены
действительно расшифровываются.

Запуск (обычно из deploy/offsite_restore_drill.sh):
    OBOROT_RECOVERY_SECRET_FILE=... python deploy/check_restored_secrets.py <база.db>

Коды возврата:
    0 — все сохранённые токены расшифровались;
    2 — что-то не так: нет секрета, нет таблицы, токены не расшифровываются;
    3 — расшифровывать нечего (ни одного сохранённого токена).
"""
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def fail(msg: str) -> int:
    print(f"РАСШИФРОВАНИЕ НЕ ДОКАЗАНО: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        return fail("нужен ровно один аргумент — путь к восстановленной базе")
    db_path = Path(argv[1])
    if not db_path.is_file():
        return fail(f"нет файла базы: {db_path}")

    secret_file = os.environ.get("OBOROT_RECOVERY_SECRET_FILE", "")
    if not secret_file:
        return fail("не задан OBOROT_RECOVERY_SECRET_FILE")
    try:
        secret = Path(secret_file).read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        return fail(f"не читается файл секрета: {exc}")
    if not secret:
        return fail(f"файл секрета пуст: {secret_file}")

    # Секрет приходит ТОЛЬКО отсюда. Окружение приложения на этой машине
    # проверку бы обесценило: она доказывала бы, что сервер знает свой
    # собственный ключ, а не что офсайт-копия ключа подходит к офсайт-копии базы.
    os.environ["OBOROT_SECRET"] = secret
    # prod включает fail-fast на дев-дефолте: подсунутый образец секрета не
    # должен проходить проверку молча.
    os.environ["OBOROT_ENV"] = "prod"

    try:
        from app.crypto import decrypt_token
    except Exception as exc:  # noqa: BLE001 — нам важна любая причина
        return fail(f"не удалось импортировать app.crypto: {exc}")

    uri = f"file:{db_path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        return fail(f"база не открывается: {exc}")
    try:
        try:
            rows = con.execute(
                "SELECT id, org_id, kind, token_enc FROM connections "
                "WHERE token_enc IS NOT NULL AND token_enc != ''"
            ).fetchall()
        except sqlite3.Error as exc:
            return fail(f"не читается таблица connections: {exc}")
    finally:
        con.close()

    if not rows:
        print("   сохранённых токенов интеграции в копии нет — расшифровывать нечего")
        return 3

    ok, bad = 0, []
    for conn_id, org_id, kind, token_enc in rows:
        # Значение токена не печатается никогда и никуда: этот вывод уходит
        # в журнал systemd, который читают не только те, кому токен положен.
        try:
            decrypted = decrypt_token(token_enc)
        except RuntimeError as exc:
            # Так падает fail-fast самого приложения — например, если в копию
            # секрета попал образец из документации. Без этой ветки наружу
            # выехал бы traceback вместо внятной причины.
            return fail(f"копия секрета не годится: {exc}")
        if decrypted:
            ok += 1
        else:
            bad.append(f"connection={conn_id} org={org_id} kind={kind}")

    if bad:
        print(f"   расшифровано {ok} из {len(rows)}", file=sys.stderr)
        for item in bad:
            print(f"   НЕ расшифровано: {item}", file=sys.stderr)
        return fail(
            "офсайт-копия секрета не подходит к офсайт-копии базы. Так выглядит "
            "смена OBOROT_SECRET без обновления копии для восстановления: база "
            "восстановится, приложение поднимется, а интеграция работать не будет"
        )

    print(f"   токены расшифровываются офсайт-копией секрета: {ok} из {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
