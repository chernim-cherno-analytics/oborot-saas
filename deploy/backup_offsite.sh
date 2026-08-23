#!/usr/bin/env bash
# Согласованная копия production SQLite -> зашифрованный off-site restic repo.
# Скрипт не инициализирует repository сам: неверный URL не должен молча создать
# «бэкап» на локальном диске production-машины.
set -euo pipefail
umask 077

CONFIG_FILE="${OBOROT_BACKUP_ENV_FILE:-/opt/oborot/backup.env}"
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # Файл принадлежит root и является частью конфигурации сервера.
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi

DATA_DIR="${OBOROT_DATA_DIR:-/opt/oborot/data}"
DB_PATH="${OBOROT_DB_PATH:-$DATA_DIR/oborot.db}"
STATE_DIR="${OBOROT_BACKUP_STATE_DIR:-/opt/oborot/backup-state}"
RESTIC_BIN="${OBOROT_RESTIC_BIN:-restic}"
RESTIC_HOST="${RESTIC_HOST:-oborot-production}"

die() { echo "BACKUP FAILED: $*" >&2; exit 1; }

command -v sqlite3 >/dev/null 2>&1 || die "sqlite3 не найден"
command -v "$RESTIC_BIN" >/dev/null 2>&1 || die "restic не найден: $RESTIC_BIN"
[ -f "$DB_PATH" ] || die "production база не найдена: $DB_PATH"
[ -n "${RESTIC_REPOSITORY:-}" ] || die "RESTIC_REPOSITORY не задан"
[ -n "${RESTIC_PASSWORD_FILE:-}" ] || die "RESTIC_PASSWORD_FILE не задан"
[ -r "$RESTIC_PASSWORD_FILE" ] || die "password file недоступен"
case "$STATE_DIR" in ""|/) die "небезопасный OBOROT_BACKUP_STATE_DIR";; esac
case "$RESTIC_REPOSITORY" in
  sftp:*|s3:*|b2:*|rest:*|azure:*|gs:*|swift:*|rclone:*) ;;
  *) die "repository не off-site: разрешены только remote restic backends";;
esac

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

# Только уже инициализированный внешний repository. Exit code restic проверяем
# целиком: любой неизвестный код — отказ, как требует его scripting contract.
"$RESTIC_BIN" cat config >/dev/null || die "restic repository недоступен или не инициализирован"

STAGE_DIR="$(mktemp -d "$STATE_DIR/backup.XXXXXX")"
SNAPSHOT_DB="$STAGE_DIR/oborot.db"

echo "== Снимаем согласованную SQLite-копию =="
case "$SNAPSHOT_DB" in *"'"*) die "недопустимый путь staging";; esac
sqlite3 "$DB_PATH" ".timeout 30000" ".backup '$SNAPSHOT_DB'"
CHECK="$(sqlite3 "$SNAPSHOT_DB" "PRAGMA integrity_check;")"
[ "$CHECK" = "ok" ] || die "SQLite integrity_check: $CHECK"

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
ROWS="$(sqlite3 "$SNAPSHOT_DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table';")"
printf 'created_at_utc=%s\ntables=%s\nsource=%s\n' "$STAMP" "$ROWS" "$DB_PATH" \
  > "$STAGE_DIR/manifest.txt"

echo "== Отправляем зашифрованную копию off-site =="
(
  cd "$STAGE_DIR"
  "$RESTIC_BIN" backup --tag oborot-db --host "$RESTIC_HOST" oborot.db manifest.txt
)

# База небольшая; retention и prune выполняются только после успешного нового
# snapshot. Так сбой загрузки не удалит последнюю хорошую копию.
"$RESTIC_BIN" forget --tag oborot-db --host "$RESTIC_HOST" \
  --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
"$RESTIC_BIN" check

printf '%s host=%s repository=%s\n' "$STAMP" "$RESTIC_HOST" "$RESTIC_REPOSITORY" \
  > "$STATE_DIR/last-backup-ok.tmp"
mv "$STATE_DIR/last-backup-ok.tmp" "$STATE_DIR/last-backup-ok"
echo "BACKUP OK: $STAMP"
