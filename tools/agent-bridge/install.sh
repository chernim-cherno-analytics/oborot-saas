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
#
# `--check` — только проверить настройки и интерпретатор тестов, ничего не
# устанавливая. Работает на любой системе: проверять готовность venv можно и
# там, где launchd нет.
set -euo pipefail

LABEL="com.oborot.agent-bridge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
INTERVAL="${OBOROT_BRIDGE_INTERVAL:-60}"

CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        -h|--help)
            echo "Использование: $0 [--check]"
            echo "  --check  проверить настройки и интерпретатор тестов, не устанавливая"
            exit 0
            ;;
        *) echo "Неизвестный аргумент: $arg (есть только --check)" >&2; exit 2 ;;
    esac
done

if [ "$CHECK_ONLY" -eq 0 ] && [ "$(uname -s)" != "Darwin" ]; then
    echo "Этот установщик только для macOS (launchd). Текущая система: $(uname -s)" >&2
    exit 1
fi

if ! [[ "$INTERVAL" =~ ^[0-9]+$ ]] || [ "$INTERVAL" -lt 30 ]; then
    echo "Интервал — целое число секунд, не меньше 30. Получено: $INTERVAL" >&2
    exit 1
fi

chmod +x "$SCRIPT_DIR/run.sh" "$SCRIPT_DIR/bridge.py" "$SCRIPT_DIR/uninstall.sh" 2>/dev/null || true

# Файл настроек кладём до того, как что-то вычислять: он может задать и каталог
# состояния, и интерпретатор тестов, и всё дальнейшее должно считаться уже с
# ним. Путь спрашиваем у самого диспетчера — второй реализации XDG в bash быть
# не должно, иначе launchd и health однажды разойдутся по разным каталогам.
CONFIG_FILE="$("$SCRIPT_DIR/run.sh" resolve config-file)"
mkdir -p "$(dirname "$CONFIG_FILE")"
if [ ! -f "$CONFIG_FILE" ] && [ -f "$SCRIPT_DIR/config.env.example" ]; then
    cp "$SCRIPT_DIR/config.env.example" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    echo "Создан файл настроек: $CONFIG_FILE"
fi

STATE_DIR="$("$SCRIPT_DIR/run.sh" resolve state-dir)"
TEST_CMD="$("$SCRIPT_DIR/run.sh" resolve test-cmd)"
TEST_PYTHON="$("$SCRIPT_DIR/run.sh" resolve test-python)"
TEST_PYTHON_SOURCE="$("$SCRIPT_DIR/run.sh" resolve test-python-source)"
VENV_PYTHON="$("$SCRIPT_DIR/run.sh" resolve venv-python)"

echo "== Интерпретатор тестов =="
# Почему это отдельная проверка, а не строчка в health. Диспетчер, который
# гоняет тесты интерпретатором без зависимостей проекта, падает на первом
# импорте и отчитывается `TESTS_FAILED` по любой правке. Автоматика при этом
# выглядит живой: цикл идёт, комментарии пишутся, а не проходит НИЧЕГО. Такой
# фон хуже отсутствующего, поэтому проверка обязательная и обойти её
# OBOROT_BRIDGE_SKIP_HEALTH нельзя: пропускать нечего, чинится она одной
# командой.
if [ -z "$TEST_CMD" ]; then
    echo "  тесты отключены (TEST_CMD пуста) — интерпретатор не нужен"
elif [ -z "$TEST_PYTHON" ]; then
    cat >&2 <<MSG
Тесты включены ($TEST_CMD), а интерпретатора с зависимостями проекта нет.
Системный python3 не годится: набор упадёт на первом импорте, и каждая правка
по ревью будет отчитываться TESTS_FAILED. Установка прервана.

Соберите окружение диспетчера:

  python3 -m venv "$STATE_DIR/venv"
  "$STATE_DIR/venv/bin/pip" install -r "$REPO_ROOT/requirements.lock"

и повторите установку. Диспетчер найдёт venv по этому пути сам — прописывать
его в настройках не нужно. Свой интерпретатор задаётся явно:
OBOROT_BRIDGE_TEST_PYTHON=/путь/до/python (переменной или в $CONFIG_FILE).
MSG
    exit 1
elif [ ! -x "$TEST_PYTHON" ]; then
    echo "OBOROT_BRIDGE_TEST_PYTHON задан ($TEST_PYTHON), но это не исполняемый файл." >&2
    echo "Исправьте путь или уберите настройку — тогда возьмётся $VENV_PYTHON." >&2
    exit 1
else
    case "$TEST_PYTHON_SOURCE" in
        venv) echo "  $TEST_PYTHON (venv в каталоге состояния)" ;;
        *)    echo "  $TEST_PYTHON (задан явно)" ;;
    esac
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    echo
    echo "Проверка настроек (--check), установка не выполнялась."
    echo "  настройки : $CONFIG_FILE"
    echo "  состояние : $STATE_DIR"
    exit 0
fi

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

mkdir -p "$PLIST_DIR" "$STATE_DIR"

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
        <!-- Каталог состояния — тот же, что вернул `run.sh resolve state-dir`,
             то есть тот, который видят health и ручные запуски. От него же
             диспетчер отсчитывает venv с зависимостями проекта, поэтому фон
             и health берут один интерпретатор без второй настройки.
             Сам путь до интерпретатора сюда НЕ вписывается: переменная
             окружения перебивает файл настроек, и вписанное здесь значение
             пережило бы правку OBOROT_BRIDGE_TEST_PYTHON в config.env —
             владелец менял бы настройку, а фон продолжал бы своё. -->
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
echo "  настройки      : $CONFIG_FILE"
echo "  тесты          : ${TEST_CMD:-отключены}"
echo "  интерпретатор  : ${TEST_PYTHON:-не нужен} ${TEST_PYTHON_SOURCE:+($TEST_PYTHON_SOURCE)}"
echo
echo "Дальше:"
echo "  $SCRIPT_DIR/run.sh status --remote   что видно на GitHub"
echo "  $SCRIPT_DIR/run.sh logs -n 100       хвост журнала"
echo "  $SCRIPT_DIR/uninstall.sh             снять автоматику"
