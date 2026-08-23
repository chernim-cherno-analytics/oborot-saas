#!/usr/bin/env bash
#
# Проверка восстановления — единственное, что превращает копию в бэкап.
#
# Разворачивает последнюю (или указанную) копию в ОТДЕЛЬНЫЙ файл, поднимает
# на ней приложение на свободном порту и проверяет, что оно стартовало и
# отвечает. Боевую базу и боевой сервис не трогает вообще.
#
# Запуск:  bash deploy/restore_test.sh [путь-к-копии.gz]
set -euo pipefail

DIR="${BACKUP_DIR:-/opt/oborot/data/backups}"
VENV="${OBOROT_VENV:-/opt/oborot/venv}"
# `|| true`: при пустом каталоге ls возвращает ненулевой код, pipefail делает
# ненулевым весь конвейер, и set -e убивает скрипт прямо здесь — молча, кодом
# 2, ещё до проверки «НЕТ КОПИИ», которая для этого случая и написана.
SRC="${1:-$(ls -1t "$DIR"/oborot-*.db.gz 2>/dev/null | head -1 || true)}"
PORT="${RESTORE_PORT:-8099}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

command -v sqlite3 >/dev/null || { echo "НЕТ sqlite3: apt install sqlite3"; exit 1; }
[ -n "$SRC" ] && [ -f "$SRC" ] || { echo "НЕТ КОПИИ в $DIR"; exit 1; }
echo "== 1/4 Разворачиваем $SRC =="
gunzip -c "$SRC" > "$WORK/restored.db" || { echo "АРХИВ НЕ РАСПАКОВЫВАЕТСЯ: $SRC"; exit 1; }

echo "== 2/4 Целостность и содержимое =="
# `|| true` здесь по той же причине, что и в backup.sh: на битом файле sqlite3
# возвращает ненулевой код, и без этого `set -e` убил бы скрипт на присваивании
# — то есть ровно тогда, когда проверка нужнее всего.
INTEG="$(sqlite3 "$WORK/restored.db" "PRAGMA integrity_check;" 2>&1 || true)"
[ "$INTEG" = "ok" ] || { echo "БИТАЯ КОПИЯ: ${INTEG%%$'\n'*}"; exit 1; }
ORGS="$(sqlite3 "$WORK/restored.db" "SELECT COUNT(*) FROM orgs;" 2>/dev/null || echo 0)"
SALES="$(sqlite3 "$WORK/restored.db" "SELECT COUNT(*) FROM sales;" 2>/dev/null || echo 0)"
STOCK="$(sqlite3 "$WORK/restored.db" "SELECT COUNT(*) FROM stock_days;" 2>/dev/null || echo 0)"
echo "   организаций $ORGS · продаж $SALES · дней остатков $STOCK"
[ "${ORGS:-0}" -gt 0 ] 2>/dev/null || { echo "В КОПИИ НЕТ ОРГАНИЗАЦИЙ — это не рабочая база"; exit 1; }

echo "== 3/4 Поднимаем приложение на копии (порт $PORT) =="
cd "$(dirname "$0")/.."
DATABASE_URL="sqlite:///$WORK/restored.db" SCHEDULER_ENABLED=0 \
  OBOROT_ENV=test OBOROT_TRUSTED_PROXY_HOPS=0 \
  "$VENV/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" \
  > "$WORK/app.log" 2>&1 &
APP_PID=$!
trap 'kill "$APP_PID" 2>/dev/null || true; rm -rf "$WORK"' EXIT

echo "== 4/4 Проверяем, что отвечает =="
# Ответ ЗАПОМИНАЕМ, а не спрашиваем второй раз. Раньше цикл убеждался, что
# приложение живо, а печатала ответ отдельная строка `curl` уже после цикла —
# то есть скрипт задавал один и тот же вопрос дважды. Если процесс умирал в
# промежутке (а на восстановленной базе он ровно этим и рискует), второй curl
# падал, `set -e` убивал скрипт кодом 7, и вместо «НЕ ПОДНЯЛОСЬ» с логом
# человек получал пустой вывод. Тот же класс ошибки, что с integrity_check:
# аварийная ветка есть, но управление до неё не доходит.
READY=""
for _ in $(seq 1 40); do
  BODY="$(curl -fsS "http://127.0.0.1:$PORT/health/ready" 2>/dev/null || true)"
  case "$BODY" in
    *'"db":true'*) READY="$BODY"; break ;;
  esac
  # Если процесс уже умер, ждать оставшиеся секунды незачем: ответа не будет,
  # а сорок секунд тишины выглядят как «медленно», хотя это «упало».
  kill -0 "$APP_PID" 2>/dev/null || break
  sleep 1
done
if [ -z "$READY" ]; then
  echo "НЕ ПОДНЯЛОСЬ. Лог:"; tail -30 "$WORK/app.log"; exit 1
fi
echo "$READY"
echo "ВОССТАНОВЛЕНИЕ ПРОВЕРЕНО: копия рабочая, приложение на ней стартует."
