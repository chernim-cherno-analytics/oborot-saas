#!/usr/bin/env bash
#
# Учение по восстановлению из УДАЛЁННОГО хранилища.
#
# Копия становится бэкапом только в тот момент, когда её однажды восстановили.
# До этого она — файл, о котором мы верим, что он подойдёт.
#
# Этот скрипт скачивает снимок из офсайт-хранилища (не из локального каталога),
# проверяет данные и — если не отключено — поднимает на восстановленной базе
# приложение через deploy/restore_test.sh. Боевую базу и боевой сервис не
# трогает: восстановленная копия кладётся только в новый файл и только если
# путь указан явно вторым аргументом.
#
# Источник идеи — ветка Codex codex/offsite-backup-restore: `restic check
# --read-data` (проверять не только метаданные, но и сами блоки), отказ писать
# поверх боевой базы, отдельный снимок по имени. Реализация своя.
#
# Что исправлено относительно источника: там учение проверяло, что четыре
# ключевые таблицы СУЩЕСТВУЮТ. Пустая, но структурно целая база это условие
# выполняет — и объявляется годной копией. Здесь проверяются строки.
#
# Запуск:   bash deploy/offsite_restore_drill.sh [snapshot] [куда-положить.db]
# Отключить подъём приложения:  OBOROT_DRILL_BOOT_APP=0
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "$0")" && pwd)"

CONFIG_FILE="${OBOROT_BACKUP_ENV_FILE:-/opt/oborot/backup.env}"
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

DB="${OBOROT_DB:-/opt/oborot/data/oborot.db}"
STATE_DIR="${OBOROT_BACKUP_STATE_DIR:-/opt/oborot/backup-state}"
RESTIC="${OBOROT_RESTIC_BIN:-restic}"
HOST_TAG="${RESTIC_HOST:-oborot-production}"
SNAPSHOT="${1:-latest}"
OUTPUT="${2:-}"
BOOT_APP="${OBOROT_DRILL_BOOT_APP:-1}"
FULL_CHECK="${OBOROT_DRILL_FULL_CHECK:-1}"

die() { echo "УЧЕНИЕ ПРОВАЛЕНО: $*" >&2; exit 1; }

for c in sqlite3 flock mktemp gzip; do
  command -v "$c" >/dev/null 2>&1 || die "нет команды $c"
done
command -v "$RESTIC" >/dev/null 2>&1 || die "нет restic ($RESTIC)"
[ -n "${RESTIC_REPOSITORY:-}" ] || die "не задан RESTIC_REPOSITORY"
[ -n "${RESTIC_PASSWORD_FILE:-}" ] || die "не задан RESTIC_PASSWORD_FILE"
[ -r "${RESTIC_PASSWORD_FILE}" ] || die "файл с паролем недоступен: $RESTIC_PASSWORD_FILE"
case "$STATE_DIR" in ""|"/") die "небезопасный OBOROT_BACKUP_STATE_DIR: '$STATE_DIR'";; esac
case "$RESTIC_REPOSITORY" in
  sftp:*|s3:*|b2:*|rest:*|azure:*|gs:*|swift:*|rclone:*) ;;
  *) die "RESTIC_REPOSITORY не удалённый: '$RESTIC_REPOSITORY'.
   Учение на локальном каталоге доказывает только то, что диск ещё жив." ;;
esac

# Восстановление никогда не заменяет боевую базу само. Замену делает человек,
# осознанно, глядя на результат учения.
if [ -n "$OUTPUT" ]; then
  [ "$OUTPUT" != "$DB" ] || die "боевая база этим скриптом не заменяется: $DB"
  [ ! -e "$OUTPUT" ] && [ ! -L "$OUTPUT" ] || die "файл уже существует: $OUTPUT"
fi

mkdir -p "$STATE_DIR"
LOCK_FILE="$STATE_DIR/offsite.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || die "офсайт-бэкап или учение уже выполняется ($LOCK_FILE)"

STAGE=""
cleanup() { [ -n "$STAGE" ] && [ -d "$STAGE" ] && rm -rf -- "$STAGE"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

STAGE="$(mktemp -d "$STATE_DIR/drill.XXXXXX")"

echo "== 1/5 Проверяем хранилище =="
"$RESTIC" cat config >/dev/null 2>&1 || die "хранилище недоступно или не создано"
if [ "$FULL_CHECK" = "1" ]; then
  # --read-data читает сами блоки, а не только метаданные: без него «check ok»
  # означает лишь «оглавление на месте».
  "$RESTIC" check --read-data || die "restic check --read-data нашёл повреждение"
else
  echo "   БЫСТРАЯ ПРОВЕРКА: блоки данных не читаются (OBOROT_DRILL_FULL_CHECK=0)"
  "$RESTIC" check || die "restic check нашёл повреждение"
fi

echo "== 2/5 Скачиваем снимок $SNAPSHOT =="
"$RESTIC" restore "$SNAPSHOT" --tag oborot-db --host "$HOST_TAG" --target "$STAGE" \
  || die "restic restore не отработал"

RESTORED="$(find "$STAGE" -type f -name oborot.db -print 2>/dev/null | head -1 || true)"
[ -n "$RESTORED" ] || die "в снимке нет oborot.db"
EXTRA="$(find "$STAGE" -type f -name oborot.db -print 2>/dev/null | sed -n '2p' || true)"
[ -z "$EXTRA" ] || die "в снимке несколько oborot.db — непонятно, какой из них база"

echo "== 3/5 Целостность и содержимое =="
INTEG="$(sqlite3 "$RESTORED" "PRAGMA integrity_check;" 2>&1 || true)"
[ "$INTEG" = "ok" ] || die "восстановленная база битая: ${INTEG%%$'\n'*}"

ORGS="$(sqlite3 "$RESTORED" "SELECT COUNT(*) FROM orgs;" 2>/dev/null || echo 0)"
SALES="$(sqlite3 "$RESTORED" "SELECT COUNT(*) FROM sales;" 2>/dev/null || echo 0)"
STOCK="$(sqlite3 "$RESTORED" "SELECT COUNT(*) FROM stock_days;" 2>/dev/null || echo 0)"
echo "   организаций $ORGS · продаж $SALES · дней остатков $STOCK"
# Проверка строк, а не наличия таблиц. Пустая база — исправная база без данных;
# именно так «успешное учение» и превращается в ложное спокойствие.
[ "${ORGS:-0}" -gt 0 ] 2>/dev/null \
  || die "в восстановленной базе нет организаций.
   Схема цела, данных нет. Такая копия проходит любую структурную проверку и
   не годится ни для чего."

echo "== 4/5 Поднимаем приложение на восстановленной базе =="
if [ "$BOOT_APP" = "1" ]; then
  # Переиспользуем уже проверенный restore_test.sh: он принимает .gz, поднимает
  # приложение на отдельном порту и ждёт "db":true. Разделение труда: этот
  # скрипт отвечает за «данные доехали из хранилища», тот — за «на них
  # запускается приложение».
  gzip -c "$RESTORED" > "$STAGE/restored.db.gz" || die "не удалось сжать копию для проверки запуском"
  bash "$HERE/restore_test.sh" "$STAGE/restored.db.gz" \
    || die "приложение на восстановленной базе не поднялось"
else
  echo "   ПРОПУЩЕНО: OBOROT_DRILL_BOOT_APP=0."
  echo "   Учение доказало, что данные доехали, но не что на них работает приложение."
fi

echo "== 5/5 Итог =="
if [ -n "$OUTPUT" ]; then
  mkdir -p "$(dirname "$OUTPUT")"
  install -m 600 "$RESTORED" "$OUTPUT" || die "не удалось положить копию в $OUTPUT"
  echo "   проверенная копия: $OUTPUT"
fi

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s snapshot=%s host=%s orgs=%s boot_app=%s\n' \
  "$STAMP" "$SNAPSHOT" "$HOST_TAG" "$ORGS" "$BOOT_APP" \
  > "$STATE_DIR/last-offsite-drill.tmp"
mv "$STATE_DIR/last-offsite-drill.tmp" "$STATE_DIR/last-offsite-drill"
echo "УЧЕНИЕ ПРОЙДЕНО: $STAMP, организаций $ORGS"
