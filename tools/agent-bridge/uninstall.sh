#!/bin/bash
# Снять LaunchAgent диспетчера.
#
# По умолчанию убирается только автоматика: журнал, состояние и счётчики
# попыток остаются на месте. Это осознанный выбор — чаще всего снимают именно
# для того, чтобы разобраться, почему автоматика повела себя не так, и стереть
# улики вместе с ней было бы худшим поведением из возможных.
#
# `--purge` дополнительно удаляет каталог состояния (вместе с рабочей копией и
# журналом) и файл настроек — но только те каталоги, про которые доказано, что
# они принадлежат диспетчеру. Подробности и список отказов — ниже, у
# `purge_target`.
set -euo pipefail

LABEL="com.oborot.agent-bridge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# Пути спрашиваем у диспетчера, а не считаем заново: своя реализация XDG в bash
# не видела бы STATE_DIR из config.env, и `--purge` чистил бы не тот каталог,
# бодро отчитываясь об успехе.
STATE_DIR="$("$SCRIPT_DIR/run.sh" resolve state-dir)"
CONFIG_FILE="$("$SCRIPT_DIR/run.sh" resolve config-file)"
CONFIG_DIR="$(dirname "$CONFIG_FILE")"

# --------------------------------------------------------------------------
# Что можно удалять
# --------------------------------------------------------------------------
#
# `--purge` — единственное место во всей автоматике, которое удаляет данные
# рекурсивно, и путь оно берёт из настроек, то есть из рук человека. Значит,
# путь надо не «проверить на всякий случай», а доказать: удаляется только тот
# каталог, про который видно, что он принадлежит диспетчеру. Всё остальное —
# отказ. Правило одностороннее сознательно: цена лишнего отказа — минута
# ручной работы, цена лишнего удаления — чужие данные.

# Домашний каталог в разрешённом виде: сравнивать надо физические пути, иначе
# симлинк (/tmp → /private/tmp на macOS) обходит любую проверку.
HOME_REAL="$(cd "$HOME" 2>/dev/null && pwd -P || echo "$HOME")"
# Репозиторий, из которого запущен сам скрипт: удалять каталог, внутри которого
# лежит выполняющийся файл, нельзя ни при каких настройках.
REPO_REAL="$SCRIPT_DIR"

# Каталоги, которые не являются каталогом диспетчера никогда, чем бы их ни
# назначили в настройках. Проверка по точному совпадению — «внутри» ловится
# отдельными правилами ниже.
BROAD_DIRS=(
    / /Users /home /tmp /private /var /etc /usr /opt /bin /sbin
    /Library /System /Applications /Volumes /Network /Users/Shared
    "$HOME_REAL"
    "$HOME_REAL/Library" "$HOME_REAL/Library/LaunchAgents"
    "$HOME_REAL/Desktop" "$HOME_REAL/Documents" "$HOME_REAL/Downloads"
    "$HOME_REAL/Movies" "$HOME_REAL/Music" "$HOME_REAL/Pictures"
    "$HOME_REAL/Public" "$HOME_REAL/Applications" "$HOME_REAL/Projects"
    "$HOME_REAL/.config" "$HOME_REAL/.local" "$HOME_REAL/.local/state"
    "$HOME_REAL/.local/share" "$HOME_REAL/.local/bin" "$HOME_REAL/.cache"
    "$HOME_REAL/.ssh" "$HOME_REAL/.gnupg" "$HOME_REAL/.claude" "$HOME_REAL/.codex"
    "${XDG_CONFIG_HOME:-}" "${XDG_STATE_HOME:-}" "${XDG_DATA_HOME:-}"
    "${XDG_CACHE_HOME:-}"
)

# $1 лежит внутри $2 или совпадает с ним.
inside() {
    case "$1" in
        "$2"|"$2"/*) return 0 ;;
    esac
    return 1
}

# Признак каталога диспетчера: либо каноническое имя, либо файлы, которые
# кладёт туда сам диспетчер. Пустой каталог с произвольным именем признаком не
# обладает — и удалён не будет, даже если он действительно наш: доказать это
# нечем, а угадывать тут нельзя.
looks_like_bridge_dir() {
    local dir="$1"
    if [ "$(basename "$dir")" = "oborot-agent-bridge" ]; then
        return 0
    fi
    local marker
    for marker in state.json bridge.log lock config.env checkout venv; do
        if [ -e "$dir/$marker" ]; then
            return 0
        fi
    done
    return 1
}

refuse() {
    echo "Отказ: не удаляю $1" >&2
    echo "  причина: $2" >&2
}

# Печатает разрешённый физический путь и возвращает 0 — либо объясняет отказ и
# возвращает 1.
purge_target() {
    local raw="$1"
    local dir
    dir="$(cd "$raw" 2>/dev/null && pwd -P || true)"
    if [ -z "$dir" ]; then
        refuse "$raw" "путь не открывается как каталог"
        return 1
    fi

    if [ "$dir" = "/" ]; then
        refuse "$dir" "это корень файловой системы"
        return 1
    fi
    # Меньше двух уровней — это всегда что-то системное вроде /data.
    case "${dir#/}" in
        */*) : ;;
        *)
            refuse "$dir" "каталог верхнего уровня, каталогом диспетчера быть не может"
            return 1
            ;;
    esac
    if inside "$HOME_REAL" "$dir"; then
        refuse "$dir" "это домашний каталог или его предок"
        return 1
    fi
    if inside "$REPO_REAL" "$dir"; then
        refuse "$dir" "внутри лежит сам репозиторий с этим скриптом"
        return 1
    fi
    local broad
    for broad in ${BROAD_DIRS+"${BROAD_DIRS[@]}"}; do
        [ -n "$broad" ] || continue
        if [ "$dir" = "$(cd "$broad" 2>/dev/null && pwd -P || echo "$broad")" ]; then
            refuse "$dir" "общий каталог, а не каталог диспетчера"
            return 1
        fi
    done
    if [ ! -O "$dir" ]; then
        refuse "$dir" "каталог принадлежит другому пользователю"
        return 1
    fi
    if ! looks_like_bridge_dir "$dir"; then
        refuse "$dir" "ничем не подтверждается, что это каталог agent-bridge:
           ни имя oborot-agent-bridge, ни файлы диспетчера (state.json,
           bridge.log, config.env, checkout, venv)"
        return 1
    fi
    printf '%s\n' "$dir"
    return 0
}

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help)
            echo "Использование: $0 [--purge]"
            echo "  --purge  вдобавок удалить $STATE_DIR и $CONFIG_DIR"
            echo "           (каталог удаляется, только если это доказуемо"
            echo "            каталог диспетчера; иначе отказ и выход с ошибкой)"
            exit 0
            ;;
        *)
            echo "Неизвестный аргумент: $arg" >&2
            exit 1
            ;;
    esac
done

TARGET="gui/$(id -u)"

if launchctl print "$TARGET/$LABEL" >/dev/null 2>&1; then
    echo "Выгружаю $LABEL"
    launchctl bootout "$TARGET/$LABEL" || true
else
    echo "$LABEL в launchd не загружен"
fi

if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Удалён $PLIST"
fi

if [ "$PURGE" = "1" ]; then
    # Рабочая копия внутри STATE_DIR — обычный клон: всё, что в нём было
    # ценного, уже отправлено в ветку. Неотправленного там не остаётся:
    # диспетчер сбрасывает копию после каждого неудачного прогона. Вместе с
    # каталогом уезжает и venv для тестов — перед следующей установкой его
    # придётся собрать заново, install.sh об этом напомнит.
    #
    # Но каталог сюда приходит из настроек, а настройки пишет человек.
    # `OBOROT_BRIDGE_STATE_DIR=$HOME` — это одна опечатка, после которой
    # `rm -rf` уносит домашний каталог целиком, бодро отчитавшись об успехе.
    # Поэтому сначала доказательство, потом удаление: проверяются ОБА каталога,
    # и если хоть один не прошёл — не удаляется НИЧЕГО. Частичная зачистка тут
    # хуже отказа: она оставляет систему в состоянии, которого никто не
    # планировал, и делает это молча.
    REFUSED=0
    APPROVED=()
    for dir in "$STATE_DIR" "$CONFIG_DIR"; do
        if [ ! -d "$dir" ]; then
            continue
        fi
        if resolved="$(purge_target "$dir")"; then
            APPROVED+=("$resolved")
        else
            REFUSED=1
        fi
    done

    if [ "$REFUSED" = "1" ]; then
        echo >&2
        echo "Ничего не удалено. Разберитесь с настройкой каталогов и повторите," >&2
        echo "либо удалите нужный каталог руками — тогда решение принимает человек," >&2
        echo "а не скрипт по подозрительному пути." >&2
        exit 1
    fi

    for dir in ${APPROVED+"${APPROVED[@]}"}; do
        rm -rf "$dir"
        echo "Удалён $dir"
    done
else
    echo
    echo "Состояние и журнал оставлены:"
    echo "  $STATE_DIR"
    echo "  $CONFIG_DIR"
    echo "Удалить вместе с ними: $0 --purge"
fi

echo
echo "Готово. Диспетчер больше не запускается сам."
echo "Разовый прогон руками по-прежнему доступен: tools/agent-bridge/run.sh poll --once"
