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
# Уточнение к пункту 3 по внешнему ревью 23.08: «после успешного снимка» — это
# необходимое условие, но не достаточное. Ротация выполняется только после того,
# как новый снимок ВИДЕН в хранилище и хранилище прошло `restic check`. Прежний
# порядок (backup → forget --prune → check) удалял старые снимки до того, как
# повреждение обнаружено, — то есть ровно в том случае, ради которого проверка
# и делается, старые хорошие копии уже были бы срезаны.
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

echo "== 1/6 Проверяем доступ к хранилищу =="
# Репозиторий должен существовать заранее. `restic init` здесь намеренно нет:
# опечатка в адресе создала бы пустой репозиторий, и скрипт отчитался бы об
# успехе, ничего на самом деле не сохранив.
"$RESTIC" cat config >/dev/null 2>&1 || die "хранилище недоступно или не создано: restic cat config вернул ошибку.
   Один раз, руками, на сервере: restic init"

echo "== 2/6 Снимаем согласованную копию =="
case "$SNAP" in *"'"*) die "недопустимый путь: $SNAP";; esac
sqlite3 "$DB" ".timeout 30000" ".backup '$SNAP'" || die "sqlite3 .backup не отработал"

echo "== 3/6 Проверяем копию ДО отправки =="
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

# Наши же проверки выше открывали копию, а открытие базы в режиме WAL создаёт
# рядом с ней -wal и -shm. Данных в них нет (`.backup` отдаёт самодостаточный
# файл, а мы только читали), но в каталоге отправки лишних файлов быть не
# должно: ниже это проверяется, и проверка не должна спотыкаться о собственный
# след. Контрольная точка перед удалением — на случай, если что-то всё же
# осталось в журнале.
sqlite3 "$SNAP" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
rm -f -- "$SNAP-wal" "$SNAP-shm"

STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
{
  printf 'created_at_utc=%s\nsource=%s\n' "$STAMP" "$DB"
  printf '%s' "$COUNTS"
} > "$STAGE/manifest.txt"

echo "== 4/6 Отправляем в хранилище =="
# В хранилище уезжает ровно два файла — база и манифест. Проверка выглядит
# избыточной ровно до того дня, когда в каталог отправки попадёт что-то ещё:
# ключ, дамп окружения, случайный файл соседнего скрипта. Секретам в бэкапе
# места нет по построению — копия базы и ключ к ней не должны лежать вместе,
# иначе шифрование хранилища перестаёт что-либо значить.
EXTRA_STAGED="$(find "$STAGE" -mindepth 1 ! -name oborot.db ! -name manifest.txt 2>/dev/null || true)"
[ -z "$EXTRA_STAGED" ] || die "в каталоге отправки лишние файлы:
$EXTRA_STAGED
   В хранилище уезжает только oborot.db и manifest.txt."
case "${OBOROT_RECOVERY_SECRET_FILE:-}" in
  "$STAGE"/*|"$STATE_DIR"/*) die "копия секрета лежит внутри рабочего каталога бэкапа
   (${OBOROT_RECOVERY_SECRET_FILE}). Ключ к копиям обязан храниться отдельно от копий." ;;
esac
if [ -n "${OBOROT_SECRET:-}" ]; then
  # Не отказ: бэкап важнее чистоты конфигурации, и останавливать ночную копию
  # из-за лишней строки в backup.env было бы хуже самой строки. Но учение с
  # таким окружением не пройдёт — и это правильно.
  echo "   ВНИМАНИЕ: OBOROT_SECRET виден в окружении бэкапа (вероятно, из $CONFIG_FILE)." >&2
  echo "   Ключ к копиям рядом с копиями — уберите его; учение этого не примет." >&2
fi
( cd "$STAGE" && "$RESTIC" backup --tag oborot-db --host "$HOST_TAG" oborot.db manifest.txt ) \
  || die "restic backup не отработал. Старые копии НЕ тронуты."

echo "== 5/6 Проверяем хранилище ДО ротации =="
# Порядок здесь и есть суть шага. Сначала убеждаемся, что новый снимок в
# хранилище виден и хранилище цело, — и только потом срезаем старое. Обратный
# порядок означает: повреждение нашли уже после того, как своими руками удалили
# копии, которыми его можно было бы пережить.
# --json и проверка на пустой список — не украшение: `restic snapshots` при
# отсутствии подходящих снимков завершается УСПЕШНО и просто ничего не находит.
# Проверка «команда отработала» здесь ничего бы не значила, а решение на её
# основании принимается разрушительное — удаление старых копий.
SNAP_LIST="$("$RESTIC" snapshots --json --latest 1 --tag oborot-db --host "$HOST_TAG" 2>/dev/null)" \
  || die "не удалось получить список снимков. Ротация НЕ выполнялась, старые копии целы."
case "$(printf '%s' "$SNAP_LIST" | tr -d ' \n\r\t')" in
  ""|"[]"|"null")
    die "новый снимок в хранилище не виден: restic snapshots вернул пустой список.
   Загрузка отчиталась об успехе, а снимка нет. Ротация НЕ выполнялась,
   старые копии целы." ;;
esac
"$RESTIC" check \
  || die "restic check нашёл проблему в хранилище. Ротация НЕ выполнялась, старые копии целы."

echo "== 6/6 Ротация =="
"$RESTIC" forget --tag oborot-db --host "$HOST_TAG" \
  --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" \
  --prune || die "restic forget/prune не отработал (новая копия при этом уже загружена)"

printf '%s host=%s orgs=%s\n' "$STAMP" "$HOST_TAG" "$ORGS" > "$STATE_DIR/last-offsite-backup.tmp"
mv "$STATE_DIR/last-offsite-backup.tmp" "$STATE_DIR/last-offsite-backup"
echo "ОФСАЙТ-БЭКАП ГОТОВ: $STAMP, организаций $ORGS"
echo "Копия существует, но бэкапом станет только после учения:"
echo "  bash deploy/offsite_restore_drill.sh"
