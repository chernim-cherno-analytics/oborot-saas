#!/usr/bin/env bash
# Атомарная выкладка «Оборота» на один VPS с systemd.
# ERR не наследуется внутрь функций: иначе один failure сначала вызывал trap
# внутри wait_ready, а затем второй раз на уровне вызова функции. Верхний
# уровень всё равно видит ненулевой return и выполняет единый rollback ровно
# один раз.
set -euo pipefail

APP_DIR="${OBOROT_APP_DIR:-/opt/oborot/app-src}"
VENV="${OBOROT_VENV:-/opt/oborot/venv}"
DATA_DIR="${OBOROT_DATA_DIR:-/opt/oborot/data}"
ENV_FILE="${OBOROT_ENV_FILE:-/opt/oborot/env}"
SERVICE="${OBOROT_SERVICE:-oborot}"
HEALTH_URL="${OBOROT_HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
STATE_DIR="${OBOROT_STATE_DIR:-/opt/oborot}"
LOCK_FILE="${OBOROT_LOCK_FILE:-$STATE_DIR/deploy.lock}"
PREVIOUS_FILE="${OBOROT_PREVIOUS_FILE:-$STATE_DIR/PREVIOUS_SHA}"
PREVIOUS_VENV_FILE="${OBOROT_PREVIOUS_VENV_FILE:-$STATE_DIR/PREVIOUS_VENV}"
HEALTH_ATTEMPTS="${OBOROT_HEALTH_ATTEMPTS:-30}"
HEALTH_DELAY="${OBOROT_HEALTH_DELAY:-2}"
PYTHON_BIN="${OBOROT_PYTHON:-$VENV/bin/python}"

TRANSACTION_STARTED=0
VENV_SWAPPED=0
HANDLING_ERROR=0
CURRENT=""
NEW_VENV=""
OLD_VENV=""
FAILED_VENV=""
ENV_BACKUP=""
STALE_VENV=""

say() { printf '%s\n' "$*"; }
die() { say "ОШИБКА: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "не найдена команда: $1"; }

set_commit_env() {
  local sha="$1" tmp
  tmp="$(mktemp "$STATE_DIR/env.XXXXXX")"
  if grep -q '^OBOROT_COMMIT=' "$ENV_FILE"; then
    sed "s/^OBOROT_COMMIT=.*/OBOROT_COMMIT=$sha/" "$ENV_FILE" > "$tmp"
  else
    { cat "$ENV_FILE"; printf '\nOBOROT_COMMIT=%s\n' "$sha"; } > "$tmp"
  fi
  chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

wait_ready() {
  local i body
  for i in $(seq 1 "$HEALTH_ATTEMPTS"); do
    if body="$(curl -fsS "$HEALTH_URL" 2>/dev/null)" \
      && printf '%s' "$body" | grep -q '"status":"ok"'; then
      say "$body"
      return 0
    fi
    sleep "$HEALTH_DELAY"
  done
  return 1
}

prepare_venv() {
  local sha="$1" destination="$2" req source_file
  req="$(mktemp "$STATE_DIR/requirements.XXXXXX")"
  if git cat-file -e "$sha:requirements.lock" 2>/dev/null; then
    source_file="requirements.lock"
  else
    source_file="requirements.txt"
  fi
  git show "$sha:$source_file" > "$req" || {
    rm -f "$req"
    return 1
  }
  "$PYTHON_BIN" -m venv "$destination" || {
    rm -f "$req"
    return 1
  }
  "$destination/bin/pip" install -q -r "$req" \
    && "$destination/bin/pip" check || {
      rm -f "$req"
      return 1
    }
  rm -f "$req"
}

cleanup() {
  if [ -n "$NEW_VENV" ] && [ -d "$NEW_VENV" ]; then
    rm -rf -- "$NEW_VENV"
  fi
  if [ -n "$FAILED_VENV" ] && [ -d "$FAILED_VENV" ]; then
    rm -rf -- "$FAILED_VENV"
  fi
  [ -z "$ENV_BACKUP" ] || rm -f -- "$ENV_BACKUP"
}

rollback_transaction() {
  say "== Автоматический откат на $CURRENT ==" >&2
  if [ "$VENV_SWAPPED" = 1 ]; then
    FAILED_VENV="$VENV.failed.$$"
    if [ -d "$VENV" ]; then
      mv "$VENV" "$FAILED_VENV" || return 1
    fi
    mv "$OLD_VENV" "$VENV" || return 1
    VENV_SWAPPED=0
  fi
  git checkout --detach "$CURRENT" || return 1
  cp -p "$ENV_BACKUP" "$ENV_FILE" || return 1
  systemctl restart "$SERVICE" || return 1
  wait_ready
}

on_error() {
  local rc="$?"
  if [ "$HANDLING_ERROR" = 1 ]; then
    exit 2
  fi
  HANDLING_ERROR=1
  set +e
  trap - ERR
  if [ "$TRANSACTION_STARTED" = 1 ]; then
    journalctl -u "$SERVICE" -n 60 --no-pager >&2 || true
    if rollback_transaction; then
      TRANSACTION_STARTED=0
      say "ОТКАТ ВЫПОЛНЕН: сервис снова работает на $CURRENT" >&2
      cleanup
      exit 1
    fi
    say "КРИТИЧЕСКАЯ ОШИБКА: релиз и откат не поднялись" >&2
    journalctl -u "$SERVICE" -n 100 --no-pager >&2 || true
    cleanup
    exit 2
  fi
  cleanup
  exit "$rc"
}

trap on_error ERR
trap cleanup EXIT

say "== 1/7 Предварительная проверка =="
for command_name in git curl grep sed sqlite3 systemctl journalctl flock mktemp \
  seq find sort awk xargs date chmod mv sleep rm cp dirname basename; do
  need "$command_name"
done
[ -d "$APP_DIR/.git" ] || die "$APP_DIR не является git-клоном"
[ -x "$PYTHON_BIN" ] || die "нет исполняемого Python: $PYTHON_BIN"
[ -f "$ENV_FILE" ] || die "нет файла окружения $ENV_FILE"
mkdir -p "$STATE_DIR" "$DATA_DIR/backups"
if [ -f "$PREVIOUS_VENV_FILE" ]; then
  IFS= read -r STALE_VENV < "$PREVIOUS_VENV_FILE" || true
  if [ -n "$STALE_VENV" ]; then
    VENV_PARENT="$(dirname "$VENV")"
    VENV_NAME="$(basename "$VENV")"
    STALE_PARENT="$(dirname "$STALE_VENV")"
    STALE_NAME="$(basename "$STALE_VENV")"
    [ "$STALE_PARENT" = "$VENV_PARENT" ] \
      || die "небезопасный каталог прошлого venv в $PREVIOUS_VENV_FILE"
    case "$STALE_NAME" in
      "$VENV_NAME".rollback.*) ;;
      *) die "небезопасное имя прошлого venv в $PREVIOUS_VENV_FILE";;
    esac
  fi
fi

# flock держит файловый дескриптор до завершения процесса и освобождается даже
# после SIGKILL. Второй релиз не ждёт и не вмешивается в первый.
exec 9>"$LOCK_FILE"
flock -n 9 || die "другой деплой уже выполняется ($LOCK_FILE)"

cd "$APP_DIR"
STATUS="$(git status --porcelain --untracked-files=all)"
[ -z "$STATUS" ] || die "рабочая копия не чиста (включая untracked): $STATUS"

say "== 2/7 Получаем и проверяем целевой коммит =="
git fetch origin --tags --prune
TARGET="${1:-origin/main}"
SHA="$(git rev-parse --verify "$TARGET^{commit}")" \
  || die "не найден коммит: $TARGET"
CURRENT="$(git rev-parse --verify 'HEAD^{commit}')"
git merge-base --is-ancestor "$SHA" origin/main \
  || die "целевой коммит $SHA не принадлежит истории origin/main"
say "сейчас: $CURRENT"
say "цель:   $SHA"

say "== 3/7 Готовим изолированное окружение целевого релиза =="
# Никогда не устанавливаем пакеты в live venv: даже частично упавший pip иначе
# оставлял старый код с наполовину новыми зависимостями.
NEW_VENV="$VENV.prepare.$SHA.$$"
[ ! -e "$NEW_VENV" ] || die "staging venv уже существует: $NEW_VENV"
prepare_venv "$SHA" "$NEW_VENV" \
  || die "не удалось подготовить изолированные зависимости $SHA"

say "== 4/7 Снимаем согласованную копию SQLite =="
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -f "$DATA_DIR/oborot.db" ]; then
  BACKUP="$DATA_DIR/backups/oborot-$STAMP.db"
  sqlite3 "$DATA_DIR/oborot.db" ".backup '$BACKUP'"
  sqlite3 "$BACKUP" 'PRAGMA quick_check;' | grep -qx 'ok' \
    || die "копия базы не прошла PRAGMA quick_check: $BACKUP"
  say "копия: $BACKUP"
  find "$DATA_DIR/backups" -maxdepth 1 -type f -name 'oborot-*.db' \
    -printf '%T@ %p\n' | sort -rn | awk 'NR > 14 {sub(/^[^ ]+ /, ""); print}' \
    | xargs -r rm --
else
  say "базы ещё нет — первый запуск"
fi

say "== 5/7 Переключаем код =="
ENV_BACKUP="$(mktemp "$STATE_DIR/env-backup.XXXXXX")"
cp -p "$ENV_FILE" "$ENV_BACKUP"
OLD_VENV="$VENV.rollback.$CURRENT.$$"
[ ! -e "$OLD_VENV" ] || die "rollback venv уже существует: $OLD_VENV"
TRANSACTION_STARTED=1
git checkout --detach "$SHA"
set_commit_env "$SHA"
mv "$VENV" "$OLD_VENV"
VENV_SWAPPED=1
mv "$NEW_VENV" "$VENV"
NEW_VENV=""

say "== 6/7 Перезапускаем и проверяем =="
systemctl restart "$SERVICE"
wait_ready

printf '%s\n' "$CURRENT" > "$PREVIOUS_FILE"
printf '%s\n' "$OLD_VENV" > "$PREVIOUS_VENV_FILE"
TRANSACTION_STARTED=0
trap - ERR
if [ -n "$STALE_VENV" ] && [ "$STALE_VENV" != "$OLD_VENV" ] && [ -d "$STALE_VENV" ]; then
  rm -rf -- "$STALE_VENV" || say "ПРЕДУПРЕЖДЕНИЕ: не удалён старый venv $STALE_VENV" >&2
fi
say "готово: прод = $SHA"
say "предыдущий успешный коммит: $CURRENT"
say "предыдущее окружение: $OLD_VENV"
exit 0
