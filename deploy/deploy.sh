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
VENV_ROOT="$(dirname "$VENV")"
VENV_KEEP="${OBOROT_VENV_KEEP:-3}"
PYTHON_BIN="${OBOROT_PYTHON:-$VENV/bin/python}"
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

# ОКРУЖЕНИЕ НА РЕЛИЗ. Идея — из ветки Codex codex/ops-3-hardening, и она
# строго лучше того, что было у меня: я ставил зависимости целевого коммита в
# ЖИВОЙ venv до переключения кода. Это спасало прод от неудачной установки, но
# ломало откат: `deploy.sh <прежний-sha>` возвращал код на прежний коммит и
# оставлял ему НОВЫЕ библиотеки. Откат восстанавливал половину состояния.
#
# Теперь окружение каждого релиза собирается отдельно и подставляется целиком.
# Прежнее не удаляется, а откладывается под именем venv-<прежний-sha>.
#
# Своё сверх источника: откат переиспользует отложенное окружение, если оно
# цело. В аварии сеть — не то, на что стоит рассчитывать, а пересборка venv
# без сети невозможна. Там, где у источника откат зависел от pip, здесь он
# сводится к переименованию каталога.
#
# Отличие от PR #3 сохраняется: ставится только requirements.lock. Установка
# requirements.txt с диапазонами означала бы, что фиксация версий не значит
# ничего (решение владельца 23.08.2026, fail-closed).
# Годность отложенного окружения проверяется по метке ВНУТРИ него, а не по
# имени каталога. Имя ставится по коммиту, который был выкачен на момент
# подмены; если по какой-то причине оно соврало, переиспользование подсунуло бы
# релизу чужие библиотеки — и молча, потому что снаружи всё выглядит правильно.
# Метка внутри такого не позволяет: расхождение означает пересборку.
release_venv_ok() {
  local dir="$1"
  local sha="$2"
  [ -d "$dir" ] || return 1
  [ -x "$dir/bin/pip" ] || return 1
  [ -f "$dir/RELEASE_SHA" ] || return 1
  [ "$(cat "$dir/RELEASE_SHA" 2>/dev/null)" = "$sha" ] || return 1
  "$dir/bin/pip" check >/dev/null 2>&1
}

install_into() {
  local sha="$1" venv="$2" req rc=0
  if git cat-file -e "$sha:requirements.lock" 2>/dev/null; then
    req="$(mktemp "$STATE_DIR/requirements.XXXXXX")"
    git show "$sha:requirements.lock" > "$req"
    "$venv/bin/pip" install -q -r "$req" || rc=1
    [ "$rc" = 0 ] && { "$venv/bin/pip" check || rc=1; }
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
  "$venv/bin/pip" install -q -r "$req" || rc=1
  rm -f "$req"
  return "$rc"
}

prepare_release_venv() {
  # Раздельные строки не для красоты: bash раскрывает ВСЕ слова команды `local`
  # до её выполнения, поэтому `local sha="$1" cached="...$sha"` под `set -u`
  # падает с «unbound variable» — переменная ещё не присвоена в момент подстановки.
  local sha="$1"
  local staging="$2"
  local cached="$VENV_ROOT/venv-$sha"
  if release_venv_ok "$cached" "$sha"; then
    echo "   окружение этого релиза уже собрано — сеть не нужна"
    mv "$cached" "$staging" || return 1
    return 0
  fi
  rm -rf -- "$cached" "$staging"
  "$PYTHON_BIN" -m venv "$staging" || { rm -rf -- "$staging"; return 1; }
  install_into "$sha" "$staging" || { rm -rf -- "$staging"; return 1; }
  printf '%s\n' "$sha" > "$staging/RELEASE_SHA" || { rm -rf -- "$staging"; return 1; }
  return 0
}

# Подмена в два переименования. Живой процесс держит свои файлы по inode и
# переезда каталога не замечает — он в любом случае будет перезапущен ниже.
swap_release_venv() {
  local staging="$1" keep="$VENV_ROOT/venv-$CURRENT" held="$VENV_ROOT/.venv-held.$$"
  mv "$VENV" "$held" || return 1
  if ! mv "$staging" "$VENV"; then
    mv "$held" "$VENV" || true
    return 1
  fi
  rm -rf -- "$keep"
  mv "$held" "$keep" || return 1
  printf '%s\n' "$keep" > "$PREVIOUS_VENV_FILE"
  echo "   прежнее окружение отложено: $keep"
}

# Старые окружения занимают место и нужны только для отката на недавнее.
prune_release_venvs() {
  # `|| true` не украшение: под pipefail неудача любого звена убила бы скрипт
  # уже ПОСЛЕ успешной выкладки — прод был бы жив, а деплой отчитался бы об
  # ошибке. Уборка старых каталогов не стоит ложной тревоги.
  find "$VENV_ROOT" -maxdepth 1 -type d -name 'venv-*' -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | awk -v k="$VENV_KEEP" 'NR > k {sub(/^[^ ]+ /, ""); print}' \
    | xargs -r rm -rf -- || true
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
[ ! -L "$VENV" ] || die "$VENV — символическая ссылка; подмена окружения рассчитана на обычный каталог"
[ -x "$PYTHON_BIN" ] || die "нет исполняемого python для сборки окружения: $PYTHON_BIN"
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

echo "== 3/6 Окружение целевого релиза =="
STAGING_VENV="$VENV_ROOT/.venv-staging.$$"
cleanup_staging() {
  if [ -n "${STAGING_VENV:-}" ] && [ -e "$STAGING_VENV" ]; then
    rm -rf -- "$STAGING_VENV"
  fi
  return 0
}
trap cleanup_staging EXIT
prepare_release_venv "$SHA" "$STAGING_VENV" \
  || die "не удалось собрать окружение для $SHA (прод не тронут)"

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
swap_release_venv "$STAGING_VENV" || die "не удалось подменить окружение"
STAGING_VENV=""
prune_release_venvs

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
echo "прежнее окружение цело: $VENV_ROOT/venv-$CURRENT — откат обойдётся без сети" >&2
echo "копия базы перед этой выкладкой: ${BACKUP:-(базы не было)}" >&2
exit 1
