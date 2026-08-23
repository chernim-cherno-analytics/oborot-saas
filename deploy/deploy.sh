#!/usr/bin/env bash
# Развёртывание «Оборота» на боевом сервере. Запускать НА СЕРВЕРЕ от root.
#
#   bash deploy/deploy.sh <commit-sha>   выкатить конкретный коммит
#   bash deploy/deploy.sh                выкатить текущую вершину main
#
# Почему именно так. Прод — обычный VPS с systemd-юнитом `oborot`, который
# запускает один процесс из /opt/oborot/app-src. До этого скрипта «деплой»
# означал «код как-то оказался на сервере», и ответить на вопрос «какая версия
# сейчас в бою» было нечем.
#
# Часть проверок ниже перенесена из PR #3 (ветка codex/ops-3-deploy-rollback):
# блокировка от параллельных выкладок, установка зависимостей ДО переключения
# кода, атомарная запись env, отказ на грязной рабочей копии и на коммите вне
# истории origin/main. Идеи оттуда, реализация и тесты — свои; отличия от
# оригинала отмечены по месту.
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

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "не найдена команда: $1"; }

# Запись OBOROT_COMMIT во временный файл и mv поверх. Прежняя версия правила
# env через `sed -i`: обрыв посреди правки оставлял бы урезанный файл
# окружения, а без него приложение не стартует вовсе. mv в пределах одной
# файловой системы атомарен.
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

# Зависимости ставятся ИЗ ЦЕЛЕВОГО КОММИТА и ДО переключения кода. Порядок тут
# не косметика: работающий процесс уже загрузил свои модули, поэтому неудачная
# установка не роняет живой сервис и не оставляет прод с новым кодом и старым
# набором пакетов. Отличие от PR #3: там ставится requirements.txt с
# диапазонами — здесь только lock, потому что иначе фиксация версий не значит
# ничего (решение владельца 23.08.2026, fail-closed).
install_requirements() {
  local sha="$1" req rc=0
  if git cat-file -e "$sha:requirements.lock" 2>/dev/null; then
    req="$(mktemp "$STATE_DIR/requirements.XXXXXX")"
    git show "$sha:requirements.lock" > "$req"
    "$VENV/bin/pip" install -q -r "$req" || rc=1
    [ "$rc" = 0 ] && { "$VENV/bin/pip" check || rc=1; }
    rm -f "$req"
    return "$rc"
  fi
  if [ "${OBOROT_ALLOW_NO_LOCK:-0}" != "1" ]; then
    printf 'в коммите %s нет requirements.lock — сборка невоспроизводима.\n' "$sha" >&2
    printf 'осознанный откат на старый коммит: OBOROT_ALLOW_NO_LOCK=1 bash deploy/deploy.sh %s\n' "$sha" >&2
    return 1
  fi
  req="$(mktemp "$STATE_DIR/requirements.XXXXXX")"
  git show "$sha:requirements.txt" > "$req"
  "$VENV/bin/pip" install -q -r "$req" || rc=1
  rm -f "$req"
  return "$rc"
}

wait_ready() {
  local i body
  for i in $(seq 1 "$HEALTH_ATTEMPTS"); do
    if body="$(curl -fsS "$HEALTH_URL" 2>/dev/null)" \
       && printf '%s' "$body" | grep -q '"status":"ok"'; then
      printf '%s\n' "$body"
      return 0
    fi
    sleep "$HEALTH_DELAY"
  done
  return 1
}

echo "== 1/6 Предварительная проверка =="
for c in git curl grep sed sqlite3 systemctl journalctl flock mktemp seq \
         find sort awk xargs date chmod mv sleep; do need "$c"; done
[ -d "$APP_DIR/.git" ] || die "$APP_DIR не является git-клоном"
[ -x "$VENV/bin/pip" ] || die "нет исполняемого $VENV/bin/pip"
[ -f "$ENV_FILE" ] || die "нет файла окружения $ENV_FILE"
cd "$APP_DIR"
mkdir -p "$STATE_DIR" "$DATA_DIR/backups"

# Блокировка от параллельных выкладок. flock держит дескриптор до конца
# процесса и освобождается даже после SIGKILL, поэтому «забытый» лок не
# превращается в вечный запрет — в отличие от файла-флага.
exec 9>"$LOCK_FILE"
flock -n 9 || die "другой деплой уже выполняется ($LOCK_FILE)"

git diff --quiet && git diff --cached --quiet \
  || die "в рабочей копии есть незакоммиченные изменения — сначала разберитесь с ними"

echo "== 2/6 Проверяем целевой коммит =="
git fetch --all --tags --prune
TARGET="${1:-origin/main}"
SHA="$(git rev-parse --verify "$TARGET^{commit}")" || die "не найден коммит: $TARGET"
CURRENT="$(git rev-parse --verify 'HEAD^{commit}')"
# Выкатывать можно только то, что прошло через main: случайный SHA из чужой
# ветки не должен оказаться в бою по опечатке.
git merge-base --is-ancestor "$SHA" origin/main \
  || die "коммит $SHA не принадлежит истории origin/main"
echo "сейчас: $CURRENT"
echo "цель:   $SHA"

echo "== 3/6 Зависимости целевого релиза =="
install_requirements "$SHA" || die "не удалось поставить зависимости для $SHA"

echo "== 4/6 Копия базы =="
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -f "$DATA_DIR/oborot.db" ]; then
  BACKUP="$DATA_DIR/backups/oborot-$STAMP.db"
  sqlite3 "$DATA_DIR/oborot.db" ".backup '$BACKUP'"
  # Непроверенная копия — не копия. Дешёвый quick_check здесь уместнее полного
  # integrity_check: он ловит структурные повреждения, а выкладку не тормозит.
  sqlite3 "$BACKUP" 'PRAGMA quick_check;' | grep -qx 'ok' \
    || die "копия базы не прошла quick_check: $BACKUP"
  echo "копия: $BACKUP"
  # find/sort/awk вместо `ls | tail`: устойчиво к необычным именам файлов.
  find "$DATA_DIR/backups" -maxdepth 1 -type f -name 'oborot-*.db' \
    -printf '%T@ %p\n' | sort -rn | awk 'NR > 14 {sub(/^[^ ]+ /, ""); print}' \
    | xargs -r rm --
else
  echo "файла базы нет — первый запуск?"
fi

echo "== 5/6 Переключаемся на коммит =="
git checkout --detach "$SHA"
git --no-pager log -1 --format='%h %ad %s' --date=iso
set_commit_env "$SHA"

echo "== 6/6 Перезапуск и проверка =="
if systemctl restart "$SERVICE" && wait_ready; then
  printf '%s\n' "$CURRENT" > "$PREVIOUS_FILE"
  echo "прод = $SHA · откат: bash deploy/deploy.sh $CURRENT"
  exit 0
fi

# Автоматический откат сознательно НЕ делается — см. deploy/README.md,
# раздел «Почему откат ручной». Коротко: откат кода без отката данных
# безопасен только пока миграции аддитивные, и решать это должен человек,
# глядя на логи, а не скрипт в момент, когда уже что-то пошло не так.
echo "ПРИЛОЖЕНИЕ НЕ ПОДНЯЛОСЬ. Логи:" >&2
journalctl -u "$SERVICE" -n 60 --no-pager >&2 || true
echo >&2
echo "ОТКАТ ОДНОЙ КОМАНДОЙ: bash deploy/deploy.sh $CURRENT" >&2
echo "копия базы перед этой выкладкой: ${BACKUP:-(базы не было)}" >&2
exit 1
