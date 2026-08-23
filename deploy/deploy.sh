#!/usr/bin/env bash
# Атомарная выкладка «Оборота» на один VPS с systemd.
set -Eeuo pipefail

APP_DIR="${OBOROT_APP_DIR:-/opt/oborot/app-src}"
VENV="${OBOROT_VENV:-/opt/oborot/venv}"
DATA_DIR="${OBOROT_DATA_DIR:-/opt/oborot/data}"
ENV_FILE="${OBOROT_ENV_FILE:-/opt/oborot/env}"
SERVICE="${OBOROT_SERVICE:-oborot}"
HEALTH_URL="${OBOROT_HEALTH_URL:-http://127.0.0.1:8000/health/ready}"
STATE_DIR="${OBOROT_STATE_DIR:-/opt/oborot}"
LOCK_FILE="${OBOROT_LOCK_FILE:-$STATE_DIR/deploy.lock}"
PREVIOUS_FILE="${OBOROT_PREVIOUS_FILE:-$STATE_DIR/PREVIOUS_SHA}"
HEALTH_ATTEMPTS="${OBOROT_HEALTH_ATTEMPTS:-30}"
HEALTH_DELAY="${OBOROT_HEALTH_DELAY:-2}"

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

install_requirements() {
  local sha="$1" req
  req="$(mktemp "$STATE_DIR/requirements.XXXXXX")"
  git show "$sha:requirements.txt" > "$req" || {
    rm -f "$req"
    return 1
  }
  if ! "$VENV/bin/pip" install -q -r "$req"; then
    rm -f "$req"
    return 1
  fi
  rm -f "$req"
}

rollback() {
  local previous="$1"
  say "== Автоматический откат на $previous =="
  git checkout --detach "$previous" || return 1
  install_requirements "$previous" || return 1
  set_commit_env "$previous" || return 1
  systemctl restart "$SERVICE" || return 1
  wait_ready
}

say "== 1/7 Предварительная проверка =="
for command_name in git curl grep sed sqlite3 systemctl journalctl flock mktemp \
  seq find sort awk xargs date chmod mv sleep; do
  need "$command_name"
done
[ -d "$APP_DIR/.git" ] || die "$APP_DIR не является git-клоном"
[ -x "$VENV/bin/pip" ] || die "нет исполняемого $VENV/bin/pip"
[ -f "$ENV_FILE" ] || die "нет файла окружения $ENV_FILE"
mkdir -p "$STATE_DIR" "$DATA_DIR/backups"

# flock держит файловый дескриптор до завершения процесса и освобождается даже
# после SIGKILL. Второй релиз не ждёт и не вмешивается в первый.
exec 9>"$LOCK_FILE"
flock -n 9 || die "другой деплой уже выполняется ($LOCK_FILE)"

cd "$APP_DIR"
git diff --quiet && git diff --cached --quiet \
  || die "в рабочей копии есть незакоммиченные изменения"

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

say "== 3/7 Проверяем зависимости целевого релиза =="
# Сначала подготавливаем зависимости. Текущий процесс уже загрузил свои модули,
# поэтому ошибка pip не роняет работающий сервис и не переключает код.
install_requirements "$SHA" || die "не удалось установить зависимости $SHA"

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
git checkout --detach "$SHA"
set_commit_env "$SHA"

say "== 6/7 Перезапускаем и проверяем =="
if systemctl restart "$SERVICE" && wait_ready; then
  printf '%s\n' "$CURRENT" > "$PREVIOUS_FILE"
  say "готово: прод = $SHA"
  say "предыдущий успешный коммит: $CURRENT"
  exit 0
fi

say "Релиз $SHA не прошёл health-check. Последние логи:" >&2
journalctl -u "$SERVICE" -n 60 --no-pager >&2 || true

say "== 7/7 Возвращаем предыдущий релиз =="
if rollback "$CURRENT"; then
  say "ОТКАТ ВЫПОЛНЕН: сервис снова работает на $CURRENT" >&2
  exit 1
fi

say "КРИТИЧЕСКАЯ ОШИБКА: не поднялись ни $SHA, ни откат $CURRENT" >&2
journalctl -u "$SERVICE" -n 100 --no-pager >&2 || true
exit 2
