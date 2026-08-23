#!/usr/bin/env bash
#
# Копия боевой базы ВНЕ этой машины — зашифрованная, дедуплицированная, restic.
#
# Зачем отдельно от backup.sh. deploy/backup.sh делает согласованную копию,
# проверяет её и кладёт рядом с базой. Это защита от «сломали данные», но не
# от «умер диск», «потеряли VPS», «хостер закрыл аккаунт». Копия на том же
# диске, что и оригинал, — это не бэкап, а вторая жизнь одного и того же файла.
#
# Идея решения и три его ключевые детали взяты из ветки Codex
# codex/offsite-backup-restore (см. PR-описание блока claude/offsite-backup):
#   1) отказ работать с локальным путём вместо удалённого хранилища. Именно
#      этой ошибкой «бэкап» превращается обратно в копию на том же диске;
#   2) `restic cat config` как предварительная проверка доступа — репозиторий
#      должен быть уже создан; скрипт не инициализирует его сам, иначе опечатка
#      в адресе молча создаст пустой репозиторий и отчитается об успехе;
#   3) `forget --prune` только ПОСЛЕ успешного нового снимка. Обратный порядок
#      удаляет старые копии в тот самый день, когда новая не загрузилась.
# Реализация, тесты и всё, что ниже, — свои.
#
# Что добавлено сверх источника:
#   - блокировка через flock вместо каталога-замка: flock освобождается ядром
#     при любой смерти процесса, а забытый каталог-замок останавливает бэкап
#     навсегда и об этом никто не узнает, пока копия не понадобится;
#   - отказ выгружать базу без организаций. Пустая, но структурно целая база
#     проходит integrity_check и любую проверку схемы. Загрузить её — значит
#     через RETAIN дней получить хранилище, полное исправных пустых копий;
#   - манифест хранит количество строк, а не количество таблиц: по нему видно,
#     что копия «похудела», ещё до того, как она понадобится.
#
# Запуск вручную:  bash deploy/offsite_backup.sh
# По расписанию:   deploy/systemd/oborot-offsite-backup.timer
# Настройка:       deploy/backup.env.example → /opt/oborot/backup.env (chmod 600)
set -euo pipefail
umask 077

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
KEEP_DAILY="${OBOROT_KEEP_DAILY:-14}"
KEEP_WEEKLY="${OBOROT_KEEP_WEEKLY:-8}"
KEEP_MONTHLY="${OBOROT_KEEP_MONTHLY:-12}"

die() { echo "ОФСАЙТ-БЭКАП НЕ СДЕЛАН: $*" >&2; exit 1; }

for c in sqlite3 flock mktemp; do
  command -v "$c" >/dev/null 2>&1 || die "нет команды $c"
done
command -v "$RESTIC" >/dev/null 2>&1 || die "нет restic ($RESTIC). apt install restic"
[ -f "$DB" ] || die "нет базы: $DB"
[ -n "${RESTIC_REPOSITORY:-}" ] || die "не задан RESTIC_REPOSITORY (см. $CONFIG_FILE)"
[ -n "${RESTIC_PASSWORD_FILE:-}" ] || die "не задан RESTIC_PASSWORD_FILE"
[ -r "${RESTIC_PASSWORD_FILE}" ] || die "файл с паролем недоступен: $RESTIC_PASSWORD_FILE"
case "$STATE_DIR" in ""|"/") die "небезопасный OBOROT_BACKUP_STATE_DIR: '$STATE_DIR'";; esac

# Главная проверка этого скрипта. Локальный путь — это не офсайт, а та самая
# ошибка, ради которой OPS-4 открыт: «копия есть», и лежит она на умирающем
# диске рядом с оригиналом.
case "$RESTIC_REPOSITORY" in
  sftp:*|s3:*|b2:*|rest:*|azure:*|gs:*|swift:*|rclone:*) ;;
  *) die "RESTIC_REPOSITORY не удалённый: '$RESTIC_REPOSITORY'.
   Разрешены только sftp: s3: b2: rest: azure: gs: swift: rclone:
   Локальный путь копией ВНЕ машины не является." ;;
esac

mkdir -p "$STATE_DIR"
LOCK_FILE="$STATE_DIR/offsite.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || die "офсайт-бэкап или учение уже выполняется ($LOCK_FILE)"

STAGE=""
cleanup() { [ -n "$STAGE" ] && [ -d "$STAGE" ] && rm -rf -- "$STAGE"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

STAGE="$(mktemp -d "$STATE_DIR/offsite.XXXXXX")"
SNAP="$STAGE/oborot.db"

echo "== 1/5 Проверяем доступ к хранилищу =="
# Репозиторий должен существовать заранее. `restic init` здесь намеренно нет:
# опечатка в адресе создала бы пустой репозиторий, и скрипт отчитался бы об
# успехе, ничего на самом деле не сохранив.
"$RESTIC" cat config >/dev/null 2>&1 || die "хранилище недоступно или не создано: restic cat config вернул ошибку.
   Один раз, руками, на сервере: restic init"

echo "== 2/5 Снимаем согласованную копию =="
case "$SNAP" in *"'"*) die "недопустимый путь: $SNAP";; esac
sqlite3 "$DB" ".timeout 30000" ".backup '$SNAP'" || die "sqlite3 .backup не отработал"

echo "== 3/5 Проверяем копию ДО отправки =="
# `|| true` обязателен: на повреждённой базе sqlite3 выходит с ненулевым кодом,
# и без него set -e убил бы скрипт прямо на присваивании — то есть проверка,
# ради которой всё написано, не выполнилась бы никогда. Эта ошибка уже была
# найдена исполнением в backup.sh 23.08, здесь она не повторяется.
INTEG="$(sqlite3 "$SNAP" "PRAGMA integrity_check;" 2>&1 || true)"
[ "$INTEG" = "ok" ] || die "копия битая, не отправляю: ${INTEG%%$'\n'*}"

ORGS="$(sqlite3 "$SNAP" "SELECT COUNT(*) FROM orgs;" 2>/dev/null || echo 0)"
[ "${ORGS:-0}" -gt 0 ] 2>/dev/null || die "в копии нет организаций.
   Пустая база проходит integrity_check и любую проверку схемы. Если её
   загрузить, через $KEEP_DAILY дней в хранилище останутся только пустые копии."

COUNTS=""
for T in orgs users products sales stock_days order_plans production_orders; do
  N="$(sqlite3 "$SNAP" "SELECT COUNT(*) FROM $T;" 2>/dev/null || echo "-")"
  COUNTS="$COUNTS$T=$N"$'\n'
  echo "   $T: $N"
done

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
{
  printf 'created_at_utc=%s\nsource=%s\n' "$STAMP" "$DB"
  printf '%s' "$COUNTS"
} > "$STAGE/manifest.txt"

echo "== 4/5 Отправляем в хранилище =="
( cd "$STAGE" && "$RESTIC" backup --tag oborot-db --host "$HOST_TAG" oborot.db manifest.txt ) \
  || die "restic backup не отработал. Старые копии НЕ тронуты."

echo "== 5/5 Ротация и проверка хранилища =="
# Порядок принципиален: срезаем старое только после того, как новое доехало.
"$RESTIC" forget --tag oborot-db --host "$HOST_TAG" \
  --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" \
  --prune || die "restic forget/prune не отработал (новая копия при этом уже загружена)"
"$RESTIC" check || die "restic check не отработал"

printf '%s host=%s orgs=%s\n' "$STAMP" "$HOST_TAG" "$ORGS" > "$STATE_DIR/last-offsite-backup.tmp"
mv "$STATE_DIR/last-offsite-backup.tmp" "$STATE_DIR/last-offsite-backup"
echo "ОФСАЙТ-БЭКАП ГОТОВ: $STAMP, организаций $ORGS"
echo "Копия существует, но бэкапом станет только после учения:"
echo "  bash deploy/offsite_restore_drill.sh"
