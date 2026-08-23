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

# Состояние выкладки: что уже изменено на проде и куда девалось прежнее
# окружение. Нужно для отката и для аварийной сети безопасности в trap EXIT.
CODE_SWITCHED=0
ENV_SWITCHED=0
VENV_SWAPPED=0
RELEASE_LOCK_USED=0
PREV_VENV_PATH=""   # где сейчас лежит окружение, работавшее до этой выкладки
VENV_HOLD=""        # каталог, который ОБЯЗАН оказаться на месте $VENV, если его нет

# Запись OBOROT_COMMIT во временный файл и mv поверх. Прежняя версия правила
# env через `sed -i`: обрыв посреди правки оставлял бы урезанный файл
# окружения, а без него приложение не стартует вовсе. mv в пределах одной
# файловой системы атомарен.
set_commit_env() {
  local sha="$1" tmp
  tmp="$(mktemp "$STATE_DIR/env.XXXXXX")" || return 1
  if grep -q '^OBOROT_COMMIT=' "$ENV_FILE"; then
    sed "s/^OBOROT_COMMIT=.*/OBOROT_COMMIT=$sha/" "$ENV_FILE" > "$tmp" \
      || { rm -f "$tmp"; return 1; }
  else
    { cat "$ENV_FILE"; printf '\nOBOROT_COMMIT=%s\n' "$sha"; } > "$tmp" \
      || { rm -f "$tmp"; return 1; }
  fi
  chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE" || { rm -f "$tmp"; return 1; }
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
#
# Вызов pip — ТОЛЬКО через `python -m pip`, и это не вкусовщина. Каталог
# окружения здесь переезжает дважды (staging → $VENV → venv-<sha>), а в
# консольных обёртках `bin/pip`, `bin/uvicorn` и прочих зашит АБСОЛЮТНЫЙ путь
# интерпретатора в первой строке. После переименования каталога такая обёртка
# перестаёт запускаться вовсе («bad interpreter»), тогда как `bin/python` —
# символическая ссылка на системный интерпретатор, и переезда не замечает.
# С прежним кодом это ломало не выкладку, а ОТКАТ: проверка годности отложенного
# окружения дёргала `bin/pip check`, всегда получала отказ — и «переиспользуем
# готовое окружение без сети» молча превращалось в пересборку через сеть,
# то есть ровно в то, чего D-36 и должен был избежать.
release_venv_ok() {
  local dir="$1"
  local sha="$2"
  [ -d "$dir" ] || return 1
  [ -x "$dir/bin/python" ] || return 1
  [ -f "$dir/RELEASE_SHA" ] || return 1
  [ "$(cat "$dir/RELEASE_SHA" 2>/dev/null)" = "$sha" ] || return 1
  "$dir/bin/python" -m pip check >/dev/null 2>&1
}

install_into() {
  local sha="$1" venv="$2" req rc=0
  if git cat-file -e "$sha:requirements.lock" 2>/dev/null; then
    req="$(mktemp "$STATE_DIR/requirements.XXXXXX")" || return 1
    git show "$sha:requirements.lock" > "$req" || { rm -f "$req"; return 1; }
    "$venv/bin/python" -m pip install -q -r "$req" || rc=1
    [ "$rc" = 0 ] && { "$venv/bin/python" -m pip check || rc=1; }
    rm -f "$req"
    [ "$rc" = 0 ] && RELEASE_LOCK_USED=1
    return "$rc"
  fi
  if [ "${OBOROT_ALLOW_NO_LOCK:-0}" != "1" ]; then
    printf 'в коммите %s нет requirements.lock — сборка невоспроизводима.\n' "$sha" >&2
    printf 'осознанный откат на старый коммит: OBOROT_ALLOW_NO_LOCK=1 bash deploy/deploy.sh %s\n' "$sha" >&2
    return 1
  fi
  req="$(mktemp "$STATE_DIR/requirements.XXXXXX")" || return 1
  git show "$sha:requirements.txt" > "$req" || { rm -f "$req"; return 1; }
  "$venv/bin/python" -m pip install -q -r "$req" || rc=1
  rm -f "$req"
  return "$rc"
}

# Окружение проверяется НА ФИНАЛЬНОМ ПУТИ, после подмены, а не на staging.
# Проверка на staging доказывает только то, что окружение работало по прежнему
# адресу; всё, что ломается именно переездом, она пропускает по построению.
verify_live_venv() {
  local sha="$1"
  [ -x "$VENV/bin/python" ] || { echo "   нет $VENV/bin/python" >&2; return 1; }
  [ -f "$VENV/RELEASE_SHA" ] || { echo "   в $VENV нет метки RELEASE_SHA" >&2; return 1; }
  [ "$(cat "$VENV/RELEASE_SHA" 2>/dev/null)" = "$sha" ] \
    || { echo "   метка окружения не совпадает с релизом" >&2; return 1; }
  "$VENV/bin/python" -m pip --version >/dev/null 2>&1 \
    || { echo "   $VENV/bin/python -m pip не работает на финальном пути" >&2; return 1; }
  if [ "$RELEASE_LOCK_USED" = 1 ]; then
    "$VENV/bin/python" -m pip check >/dev/null 2>&1 \
      || { echo "   pip check на финальном пути не проходит" >&2; return 1; }
  fi
  return 0
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
    # Годность уже доказана `pip check` выше — на финальном пути её проверим
    # ещё раз, после подмены.
    RELEASE_LOCK_USED=1
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
#
# VENV_HOLD выставляется РОВНО на то время, пока $VENV не существует. Это не
# бухгалтерия ради бухгалтерии: если скрипт умрёт именно в этом промежутке
# (сигнал, ошибка ФС), на месте рабочего окружения останется пустота, а сервис
# без окружения не поднимется вообще — то есть неудачная выкладка превратится
# в полноценную аварию. Сеть безопасности в trap EXIT читает этот путь.
swap_release_venv() {
  local staging="$1" keep="$VENV_ROOT/venv-$CURRENT" held="$VENV_ROOT/.venv-held.$$"
  rm -rf -- "$held"
  VENV_HOLD="$held"
  if ! mv "$VENV" "$held"; then
    VENV_HOLD=""
    return 1
  fi
  if ! mv "$staging" "$VENV"; then
    if mv "$held" "$VENV"; then VENV_HOLD=""; fi
    return 1
  fi
  VENV_HOLD=""
  PREV_VENV_PATH="$held"
  # С этой секунды в $VENV лежит окружение НОВОГО релиза, и откат обязан вернуть
  # прежнее. Поэтому флаг ставится ЗДЕСЬ, а не строкой у вызывающего: ниже есть
  # ещё одно переименование, и оно умеет падать. Пока флаг выставлялся снаружи,
  # падение последнего `mv` возвращало 1 раньше, чем `VENV_SWAPPED=1` успевало
  # выполниться, — и `rollback_release` молча пропускал возврат окружения: код и
  # env откатывались на прежний релиз, а в $VENV оставались библиотеки нового.
  # Состояние, которого нет ни в одном коммите, — ровно то, ради чего написан
  # D-38. Найдено внешним ревью 23.08, проверено инъекцией сбоя на этом `mv`.
  VENV_SWAPPED=1
  rm -rf -- "$keep"
  # `[ -e "$keep" ]` — не перестраховка. Если каталог пережил `rm` (права,
  # занятый файл), `mv` не заменит его, а ВЛОЖИТ прежнее окружение внутрь:
  # PREV_VENV_PATH указывал бы на venv-<sha>, который venv не является, и откат
  # подставил бы сервису каталог без интерпретатора. Это отказ, а не успех.
  if [ -e "$keep" ] || ! mv "$held" "$keep"; then
    echo "   ВНИМАНИЕ: прежнее окружение осталось как $held" >&2
    return 1
  fi
  PREV_VENV_PATH="$keep"
  echo "   прежнее окружение отложено: $keep"
}

# Обратная операция: вернуть на место окружение, работавшее до этой выкладки.
# Порядок и здесь выбран так, чтобы $VENV отсутствовал минимальное время, а при
# ВТОРИЧНОМ сбое (не получилось поставить прежнее) на месте оказалось хоть
# что-то рабочее — пусть даже окружение неудачного релиза.
restore_previous_venv() {
  local aside="$VENV_ROOT/.venv-rollback.$$"
  [ -n "$PREV_VENV_PATH" ] && [ -d "$PREV_VENV_PATH" ] || return 1
  rm -rf -- "$aside"
  if [ -e "$VENV" ]; then
    VENV_HOLD="$aside"
    if ! mv "$VENV" "$aside"; then
      VENV_HOLD=""
      return 1
    fi
  fi
  if ! mv "$PREV_VENV_PATH" "$VENV"; then
    if [ -d "$aside" ] && mv "$aside" "$VENV"; then VENV_HOLD=""; fi
    return 1
  fi
  VENV_HOLD=""
  PREV_VENV_PATH=""
  rm -rf -- "$aside"
  return 0
}

# Откат всего, что успели изменить ДО первого перезапуска сервиса. В этом окне
# он безопасен и потому обязателен: данные ещё никто не трогал, миграции не
# выполнялись, приложение работает на прежнем коде в своей памяти. Оставить прод
# в состоянии «код нового релиза, окружение неизвестно чьё, сервис ещё старый»
# хуже любого из двух согласованных состояний.
#
# Это НЕ противоречит решению «автоматического отката нет» (D-36): там речь про
# откат ПОСЛЕ перезапуска, когда новый код уже мог тронуть базу. Здесь — про
# отмену собственных недоделанных действий.
rollback_release() {
  local reason="$1" ok=1
  trap - ERR
  echo >&2
  echo "СБОЙ ДО ПЕРЕЗАПУСКА: $reason" >&2
  echo "Сервис ещё не перезапускался, данные не менялись — возвращаю прод на $CURRENT." >&2
  if [ "$VENV_SWAPPED" = 1 ]; then
    if restore_previous_venv; then
      echo "   окружение возвращено: $VENV снова от $CURRENT" >&2
    else
      ok=0
      echo "   ОКРУЖЕНИЕ ВЕРНУТЬ НЕ УДАЛОСЬ. В $VENV осталось окружение релиза $SHA." >&2
      echo "   Руками: bash deploy/deploy.sh $CURRENT (пересоберёт окружение прежнего релиза)." >&2
    fi
  fi
  if [ "$ENV_SWITCHED" = 1 ]; then
    if set_commit_env "$CURRENT"; then
      echo "   OBOROT_COMMIT возвращён на $CURRENT" >&2
    else
      ok=0
      echo "   OBOROT_COMMIT ВЕРНУТЬ НЕ УДАЛОСЬ: проверьте $ENV_FILE руками." >&2
    fi
  fi
  if [ "$CODE_SWITCHED" = 1 ]; then
    if git checkout --detach --force "$CURRENT" >/dev/null 2>&1; then
      echo "   код возвращён на $CURRENT" >&2
    else
      ok=0
      echo "   КОД ВЕРНУТЬ НЕ УДАЛОСЬ: в $APP_DIR остался $SHA." >&2
    fi
  fi
  if [ "$ok" = 1 ]; then
    echo "Прод в прежнем состоянии, выкладка не выполнена." >&2
  else
    echo "ОТКАТ НЕПОЛНЫЙ — разберитесь руками ДО перезапуска сервиса." >&2
  fi
  exit 1
}

# Старые окружения занимают место и нужны только для отката на недавнее.
#
# Два правила, и оба появились по внешнему ревью 23.08.
#
# Первое: уборка НИКОГДА не трогает непосредственную цель отката. Сортировка идёт
# по времени изменения, а отложенное окружение переезжает переименованием и
# сохраняет СТАРЫЙ mtime — то есть оказывается в конце списка. При небольшом
# OBOROT_VENV_KEEP скрипт своими руками удалял бы каталог, на который сам же
# указывает в подсказке про откат, и лишал бы прод возврата без сети.
#
# Второе: место в квоте цель отката всё-таки занимает. Иначе `OBOROT_VENV_KEEP=2`
# тихо означало бы три каталога, и настройка врала бы про размер на диске.
prune_release_venvs() {
  local line path n=0
  local keep_a="$VENV_ROOT/venv-$CURRENT" keep_b="${PREV_VENV_PATH:-}"
  if [ -d "$keep_a" ]; then n=$((n + 1)); fi
  if [ -n "$keep_b" ] && [ "$keep_b" != "$keep_a" ] && [ -d "$keep_b" ]; then
    n=$((n + 1))
  fi
  # Неудача любого звена не должна валить скрипт уже ПОСЛЕ успешной выкладки:
  # прод жив, а деплой отчитался бы об ошибке. Уборка старых каталогов не стоит
  # ложной тревоги — отсюда `|| true` на удалении и `return 0` в конце.
  while IFS= read -r line; do
    path="${line#* }"
    [ "$path" = "$keep_a" ] && continue
    [ -n "$keep_b" ] && [ "$path" = "$keep_b" ] && continue
    n=$((n + 1))
    [ "$n" -gt "$VENV_KEEP" ] || continue
    rm -rf -- "$path" || true
  done < <(find "$VENV_ROOT" -maxdepth 1 -type d -name 'venv-*' -printf '%T@ %p\n' \
             2>/dev/null | sort -rn)
  return 0
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
[ -x "$VENV/bin/python" ] || die "нет исполняемого $VENV/bin/python"
[ ! -L "$VENV" ] || die "$VENV — символическая ссылка; подмена окружения рассчитана на обычный каталог"
[ -x "$PYTHON_BIN" ] || die "нет исполняемого python для сборки окружения: $PYTHON_BIN"
[ -f "$ENV_FILE" ] || die "нет файла окружения $ENV_FILE"
cd "$APP_DIR"
mkdir -p "$STATE_DIR" "$DATA_DIR/backups"

# Сеть безопасности. Ставится ДО первого действия с окружением: если скрипт
# умрёт в тот момент, когда $VENV переехал, а новый ещё не встал на его место,
# сервис останется без интерпретатора вообще. Это единственное состояние, из
# которого прод не поднимается сам, — поэтому оно лечится даже в аварийном
# выходе, и только оно.
on_exit() {
  local rc=$? cand
  if [ -n "${STAGING_VENV:-}" ] && [ -e "$STAGING_VENV" ]; then
    rm -rf -- "$STAGING_VENV"
  fi
  if [ ! -e "$VENV" ]; then
    for cand in "$VENV_HOLD" "$PREV_VENV_PATH" "$VENV_ROOT/venv-${CURRENT:-}"; do
      [ -n "$cand" ] && [ -d "$cand" ] || continue
      if mv "$cand" "$VENV"; then
        echo "АВАРИЙНО: рабочее окружение восстановлено из $cand" >&2
        break
      fi
    done
  fi
  [ -e "$VENV" ] || echo "КРИТИЧНО: $VENV отсутствует, сервис без окружения не поднимется.
   Соберите заново: bash deploy/deploy.sh ${CURRENT:-<sha>}" >&2
  return $rc
}
trap on_exit EXIT

# Блокировка от параллельных выкладок. flock держит дескриптор до конца
# процесса и освобождается даже после SIGKILL, поэтому «забытый» лок не
# превращается в вечный запрет — в отличие от файла-флага.
exec 9>"$LOCK_FILE"
flock -n 9 || die "другой деплой уже выполняется ($LOCK_FILE)"

git diff --quiet && git diff --cached --quiet \
  || die "в рабочей копии есть незакоммиченные изменения — сначала разберитесь с ними"

# Неотслеживаемые файлы — тот же класс опасности, что и правки: `git checkout`
# их НЕ удаляет и НЕ перезаписывает, поэтому файл, положенный в рабочую копию
# руками, переживёт любую выкладку и любой откат. `app/analytics.py.bak`,
# случайный `sitecustomize.py`, недоделанный шаблон — всё это молча продолжит
# участвовать в работе прода, а `deploy.sh <sha>` будет уверенно печатать, что
# в бою ровно этот коммит. Fail-closed: любые локальные изменения — отказ.
DIRTY="$(git status --porcelain 2>/dev/null || true)"
[ -z "$DIRTY" ] || die "в рабочей копии есть локальные изменения (в том числе неотслеживаемые файлы):
$DIRTY
   Уберите их: git clean -nd (посмотреть), git clean -fd (удалить). Игнорируемые
   файлы проверке не мешают — они перечислены в .gitignore осознанно."

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
# С этой строки и до первого перезапуска сервиса любая ошибка означает откат:
# ловим и явные отказы шагов, и всё, что упадёт мимо них.
trap 'rollback_release "непредвиденная ошибка на строке $LINENO"' ERR
CODE_SWITCHED=1
git checkout --detach "$SHA" || rollback_release "не удалось переключиться на $SHA"
git --no-pager log -1 --format='%h %ad %s' --date=iso
set_commit_env "$SHA" || rollback_release "не удалось записать OBOROT_COMMIT"
ENV_SWITCHED=1
# VENV_SWAPPED выставляет сама функция — в тот момент, когда $VENV подменён.
# Снаружи это было бы поздно: между подменой и возвратом из функции есть ещё
# один шаг, и при его падении откат не знал бы, что окружение уже чужое.
swap_release_venv "$STAGING_VENV" || rollback_release "не удалось подменить окружение"
STAGING_VENV=""
# Проверка окружения на финальном пути. Именно здесь ловится всё, что ломается
# переездом каталога, а не установкой пакетов.
verify_live_venv "$SHA" || rollback_release "окружение релиза не работает на финальном пути $VENV"
printf '%s\n' "$PREV_VENV_PATH" > "$PREVIOUS_VENV_FILE"
trap - ERR

echo "== 6/6 Перезапуск и проверка =="
# Точка невозврата: после перезапуска новый код мог тронуть базу, и решение об
# откате принимает человек (D-36, deploy/README.md «Почему откат ручной»).
if systemctl restart "$SERVICE" && wait_ready; then
  printf '%s\n' "$CURRENT" > "$PREVIOUS_FILE"
  # Уборка старых окружений — только здесь, после успешного health-check. Раньше
  # она стояла ДО перезапуска, и это была ошибка: в окне «код переключён, сервис
  # ещё старый» откат обязателен (D-38), а уборка в этот момент могла унести
  # окружение, которым он и делается. Пока прод не поднялся на новом релизе,
  # лишний каталог на диске стоит дешевле отката без окружения.
  prune_release_venvs
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
