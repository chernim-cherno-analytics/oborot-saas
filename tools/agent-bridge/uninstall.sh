#!/bin/bash
# Снять LaunchAgent диспетчера.
#
# По умолчанию убирается только автоматика: журнал, состояние и счётчики
# попыток остаются на месте. Это осознанный выбор — чаще всего снимают именно
# для того, чтобы разобраться, почему автоматика повела себя не так, и стереть
# улики вместе с ней было бы худшим поведением из возможных.
#
# `--purge` дополнительно удаляет каталог состояния (вместе с рабочей копией и
# журналом) и файл настроек.
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

PURGE=0
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h|--help)
            echo "Использование: $0 [--purge]"
            echo "  --purge  вдобавок удалить $STATE_DIR и $CONFIG_DIR"
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
    for dir in "$STATE_DIR" "$CONFIG_DIR"; do
        if [ -d "$dir" ]; then
            rm -rf "$dir"
            echo "Удалён $dir"
        fi
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
