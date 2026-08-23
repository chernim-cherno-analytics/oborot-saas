#!/bin/bash
# Установка LaunchAgent, который будит диспетчер примерно раз в минуту.
#
# Почему LaunchAgent, а не cron и не «оставить терминал открытым». Терминал
# закрывают, ноутбук усыпляют, сессию завершают — и автоматика тихо умирает,
# причём узнать об этом можно только по тому, что PR неделю висит без ответа.
# LaunchAgent переживает закрытие терминала и logout/login, стартует при входе
# в систему и, в отличие от cron, штатно доживает до следующего пробуждения
# машины.
#
# Per-user (`~/Library/LaunchAgents`), а не системный демон: диспетчеру нужны
# ключи авторизации `gh` и `claude` из связки ключей ЭТОГО пользователя. Демон
# в /Library/LaunchDaemons работает от root и до пользовательской связки не
# дотянется — он бы просто падал на каждом запуске.
#
# Этот скрипт НЕ запускается автоматически. Ставит его владелец руками, когда
# готов к тому, что машина начнёт сама править ветки.
set -euo pipefail

LABEL="com.oborot.agent-bridge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
INTERVAL="${OBOROT_BRIDGE_INTERVAL:-60}"

STATE_DIR="${OBOROT_BRIDGE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/oborot-agent-bridge}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/oborot-agent-bridge"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Этот установщик только для macOS (launchd). Текущая система: $(uname -s)" >&2
    exit 1
fi

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [ "$INTERVAL" -lt 30 ]; then
    echo "Интервал — целое число секунд, не меньше 30. Получено: $INTERVAL" >&2
    exit 1
fi

chmod +x "$SCRIPT_DIR/run.sh" "$SCRIPT_DIR/bridge.py" "$SCRIPT_DIR/uninstall.sh" 2>/dev/null || true

echo "== Проверка окружения =="
# Ставить автоматику, которая не найдёт `claude` или не авторизована в GitHub,
# бессмысленно: она будет молча писать в лог и ничего не делать. Поэтому
# health — обязательное условие установки, а не рекомендация.
if ! "$SCRIPT_DIR/run.sh" health; then
    echo
    echo "Проверка не прошла. Устраните проблемы выше и повторите." >&2
    echo "Обойти проверку: OBOROT_BRIDGE_SKIP_HEALTH=1 $0" >&2
    if [ "${OBOROT_BRIDGE_SKIP_HEALTH:-0}" != "1" ]; then
        exit 1
    fi
    echo "OBOROT_BRIDGE_SKIP_HEALTH=1 — продолжаю вопреки проверке."
fi

mkdir -p "$PLIST_DIR" "$STATE_DIR" "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/config.env" ] && [ -f "$SCRIPT_DIR/config.env.example" ]; then
    cp "$SCRIPT_DIR/config.env.example" "$CONFIG_DIR/config.env"
    chmod 600 "$CONFIG_DIR/config.env"
    echo "Создан файл настроек: $CONFIG_DIR/config.env"
fi

echo "== Пишу $PLIST =="
# Абсолютный путь до run.sh подставляется при установке: launchd не понимает ни
# ~, ни относительных путей, а искать репозиторий сам этот скрипт не должен.
# Если репозиторий переедет — install.sh надо просто запустить заново.
cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/run.sh</string>
        <string>poll</string>
        <string>--once</string>
    </array>
    <key>StartInterval</key>
    <integer>$INTERVAL</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>$STATE_DIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$STATE_DIR/launchd.err.log</string>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OBOROT_BRIDGE_STATE_DIR</key>
        <string>$STATE_DIR</string>
        <key>LANG</key>
        <string>ru_RU.UTF-8</string>
    </dict>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST" >/dev/null

TARGET="gui/$(id -u)"

echo "== Загружаю в launchd =="
# bootout перед bootstrap: повторная установка поверх уже загруженной задачи
# иначе падает с «service already loaded», и обновлённый plist не применяется.
launchctl bootout "$TARGET/$LABEL" 2>/dev/null || true
launchctl bootstrap "$TARGET" "$PLIST"
launchctl enable "$TARGET/$LABEL" 2>/dev/null || true

echo
echo "Готово."
echo "  метка          : $LABEL"
echo "  запуск         : $SCRIPT_DIR/run.sh poll --once"
echo "  интервал       : каждые $INTERVAL с, плюс при входе в систему"
echo "  состояние      : $STATE_DIR"
echo "  настройки      : $CONFIG_DIR/config.env"
echo
echo "Дальше:"
echo "  $SCRIPT_DIR/run.sh status --remote   что видно на GitHub"
echo "  $SCRIPT_DIR/run.sh logs -n 100       хвост журнала"
echo "  $SCRIPT_DIR/uninstall.sh             снять автоматику"
