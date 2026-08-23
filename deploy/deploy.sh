#!/usr/bin/env bash
# Развёртывание «Оборота» на боевом сервере. Запускать НА СЕРВЕРЕ от root.
#
#   bash deploy/deploy.sh <commit-sha>   выкатить конкретный коммит
#   bash deploy/deploy.sh               выкатить текущую вершину main
#
# Почему именно так. Прод — обычный VPS с systemd-юнитом `oborot`, который
# запускает один процесс из /opt/oborot/app-src. До этого скрипта «деплой»
# означал «код как-то оказался на сервере», и ответить на вопрос «какая версия
# сейчас в бою» было нечем: ветка едет, коммит неизвестен. Отсюда три вещи,
# которые скрипт обеспечивает:
#
#   1) прод соответствует КОНКРЕТНОМУ коммиту (detached checkout по SHA,
#      а не «pull ветки»);
#   2) перед каждым обновлением делается копия базы — она одна и она файл;
#   3) откат возможен и стоит одну команду: bash deploy/deploy.sh <предыдущий-SHA>.
#      Предыдущий SHA пишется в /opt/oborot/PREVIOUS_SHA автоматически.
#
# Скрипт намеренно не делает миграций отдельным шагом: приложение выполняет
# аддитивные миграции само при старте, и они переживают одновременный запуск.
set -euo pipefail

APP_DIR="${OBOROT_APP_DIR:-/opt/oborot/app-src}"
VENV="${OBOROT_VENV:-/opt/oborot/venv}"
DATA_DIR="${OBOROT_DATA_DIR:-/opt/oborot/data}"
ENV_FILE="${OBOROT_ENV_FILE:-/opt/oborot/env}"
SERVICE="${OBOROT_SERVICE:-oborot}"
HEALTH_URL="${OBOROT_HEALTH_URL:-http://127.0.0.1:8000/health/ready}"

cd "$APP_DIR"

echo "== 1/6 Забираем изменения =="
git fetch --all --tags --prune
TARGET="${1:-origin/main}"
SHA="$(git rev-parse --short "$TARGET")"
CURRENT="$(git rev-parse --short HEAD)"
echo "сейчас: $CURRENT → выкатываем: $SHA"
if [ "$SHA" = "$CURRENT" ]; then
  echo "уже на этом коммите; продолжаем (перезапуск и проверка не помешают)"
fi

echo "== 2/6 Копия базы =="
mkdir -p "$DATA_DIR/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -f "$DATA_DIR/oborot.db" ]; then
  # .backup корректно снимает копию работающей базы, в отличие от cp
  sqlite3 "$DATA_DIR/oborot.db" ".backup '$DATA_DIR/backups/oborot-$STAMP.db'"
  echo "копия: $DATA_DIR/backups/oborot-$STAMP.db"
  ls -1t "$DATA_DIR/backups"/oborot-*.db | tail -n +15 | xargs -r rm --
else
  echo "файла базы нет — первый запуск?"
fi

echo "== 3/6 Запоминаем текущий коммит для отката =="
echo "$CURRENT" > /opt/oborot/PREVIOUS_SHA

echo "== 4/6 Переключаемся на коммит =="
git checkout --detach "$SHA"
git --no-pager log -1 --format='%h %ad %s' --date=iso

echo "== 5/6 Зависимости и перезапуск =="
"$VENV/bin/pip" install -q -r requirements.lock
"$VENV/bin/pip" check
# Версия сборки попадает в записи решений (см. app/version.py): по ней потом
# можно поднять код, который дал конкретную рекомендацию.
grep -q '^OBOROT_COMMIT=' "$ENV_FILE" \
  && sed -i "s/^OBOROT_COMMIT=.*/OBOROT_COMMIT=$SHA/" "$ENV_FILE" \
  || echo "OBOROT_COMMIT=$SHA" >> "$ENV_FILE"
systemctl restart "$SERVICE"

echo "== 6/6 Проверка =="
for i in $(seq 1 30); do
  sleep 2
  if curl -fsS "$HEALTH_URL" | grep -q '"status":"ok"'; then
    echo "готово: $(curl -fsS "$HEALTH_URL")"
    echo "прод = $SHA · откат: bash deploy/deploy.sh $CURRENT"
    exit 0
  fi
done

echo "ПРИЛОЖЕНИЕ НЕ ПОДНЯЛОСЬ за 60 с. Логи:"
journalctl -u "$SERVICE" -n 40 --no-pager || true
echo
echo "Откат: bash deploy/deploy.sh $CURRENT"
exit 1
