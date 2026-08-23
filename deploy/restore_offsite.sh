#!/usr/bin/env bash
# Реально скачивает snapshot из off-site repository и доказывает, что SQLite
# читается. Без OUTPUT это безопасный drill; с OUTPUT кладёт проверенную копию
# в НОВЫЙ файл, но никогда не заменяет production базу автоматически.
set -euo pipefail
umask 077

CONFIG_FILE="${OBOROT_BACKUP_ENV_FILE:-/opt/oborot/backup.env}"
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

DATA_DIR="${OBOROT_DATA_DIR:-/opt/oborot/data}"
DB_PATH="${OBOROT_DB_PATH:-$DATA_DIR/oborot.db}"
STATE_DIR="${OBOROT_BACKUP_STATE_DIR:-/opt/oborot/backup-state}"
RESTIC_BIN="${OBOROT_RESTIC_BIN:-restic}"
RESTIC_HOST="${RESTIC_HOST:-oborot-production}"
SNAPSHOT="${1:-latest}"
OUTPUT="${2:-}"

die() { echo "RESTORE FAILED: $*" >&2; exit 1; }

command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 не найден"
command -v "$RESTIC_BIN" >/dev/null 2>&1 || die "restic не найден: $RESTIC_BIN"
[ -n "${RESTIC_REPOSITORY:-}" ] || die "RESTIC_REPOSITORY не задан"
[ -n "${RESTIC_PASSWORD_FILE:-}" ] || die "RESTIC_PASSWORD_FILE не задан"
[ -r "$RESTIC_PASSWORD_FILE" ] || die "password file недоступен"
[ "$STATE_DIR" != "/" ] && [ -n "$STATE_DIR" ] || die "небезопасный OBOROT_BACKUP_STATE_DIR"
case "$RESTIC_REPOSITORY" in
  sftp:*|s3:*|b2:*|rest:*|azure:*|gs:*|swift:*|rclone:*) ;;
  *) die "repository не off-site: разрешены только remote restic backends";;
esac
[ -z "$OUTPUT" ] || [ "$OUTPUT" != "$DB_PATH" ] || die "production база не заменяется этим скриптом"
[ -z "$OUTPUT" ] || { [ ! -e "$OUTPUT" ] && [ ! -L "$OUTPUT" ]; } \
  || die "output уже существует: $OUTPUT"

mkdir -p "$STATE_DIR"
LOCK_DIR="$STATE_DIR/repository.lock"
mkdir "$LOCK_DIR" 2>/dev/null || die "backup/restore уже выполняется"
STAGE_DIR=""
cleanup() {
  if [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ]; then
    rm -rf -- "$STAGE_DIR"
  fi
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$RESTIC_BIN" cat config >/dev/null || die "restic repository недоступен или не инициализирован"
if [ "${OBOROT_RESTORE_FULL_CHECK:-1}" = "1" ]; then
  echo "== Полная проверка данных repository =="
  "$RESTIC_BIN" check --read-data
else
  "$RESTIC_BIN" check
fi

STAGE_DIR="$(mktemp -d "$STATE_DIR/restore.XXXXXX")"
echo "== Скачиваем snapshot $SNAPSHOT =="
"$RESTIC_BIN" restore "$SNAPSHOT" --tag oborot-db --host "$RESTIC_HOST" \
  --target "$STAGE_DIR"

RESTORED_DB="$(find "$STAGE_DIR" -type f -name oborot.db -print -quit)"
[ -n "$RESTORED_DB" ] || die "в snapshot нет oborot.db"
SECOND_DB="$(find "$STAGE_DIR" -type f -name oborot.db -print | sed -n '2p')"
[ -z "$SECOND_DB" ] || die "в snapshot несколько oborot.db"

CHECK="$(sqlite3 "$RESTORED_DB" "PRAGMA integrity_check;")"
[ "$CHECK" = "ok" ] || die "SQLite integrity_check: $CHECK"
CORE_TABLES="$(sqlite3 "$RESTORED_DB" \
  "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('orgs','users','products','sales');")"
[ "$CORE_TABLES" = "4" ] || die "неполная схема: найдено $CORE_TABLES/4 основных таблиц"

if [ -n "$OUTPUT" ]; then
  mkdir -p "$(dirname "$OUTPUT")"
  install -m 600 "$RESTORED_DB" "$OUTPUT"
  echo "проверенная копия: $OUTPUT"
fi

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s snapshot=%s host=%s repository=%s\n' \
  "$STAMP" "$SNAPSHOT" "$RESTIC_HOST" "$RESTIC_REPOSITORY" \
  > "$STATE_DIR/last-restore-ok.tmp"
mv "$STATE_DIR/last-restore-ok.tmp" "$STATE_DIR/last-restore-ok"
echo "RESTORE DRILL OK: $STAMP"
