#!/bin/bash
# Точка входа для LaunchAgent: один цикл диспетчера и выход.
#
# Отдельный файл, а не прямой вызов python из plist, нужен ради PATH. launchd
# запускает процесс с PATH="/usr/bin:/bin:/usr/sbin:/sbin" — в нём нет ни `gh`
# из Homebrew, ни `claude` из ~/.local/bin. Диспетчер умеет искать бинарники
# сам, но дешевле отдать ему готовый PATH, чем каждый раз перебирать кандидатов.
#
# Пользовательские rc-файлы (.zshrc, .bash_profile) сознательно НЕ читаются:
# фоновой задаче не нужны алиасы, автозапуски и `nvm use` из чужой сессии, а
# ошибка в rc-файле иначе роняла бы автоматику молча.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# Порядок: сначала то, куда ставят Homebrew и npm, потом системные каталоги.
# Ничего из /tmp и других каталогов с общей записью здесь быть не должно.
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for extra in "$HOME/.local/bin" "$HOME/.claude/local" "$HOME/.bun/bin" "$HOME/.npm-global/bin"; do
    if [ -d "$extra" ]; then
        PATH="$PATH:$extra"
    fi
done
export PATH

# Домашний каталог берём из окружения, а не из захардкоженного /Users/<кто-то>:
# LaunchAgent ставится на любого пользователя, и путь у каждого свой.
if [ -z "${HOME:-}" ]; then
    HOME="$(cd ~ && pwd -P)"
    export HOME
fi

PYTHON="${OBOROT_BRIDGE_PYTHON_BIN:-}"
if [ -z "$PYTHON" ]; then
    for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
        if [ -x "$candidate" ]; then
            PYTHON="$candidate"
            break
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ]; then
    echo "agent-bridge: python3 не найден" >&2
    exit 1
fi

# Без аргументов — ровно один цикл: launchd будит нас сам раз в минуту, свой
# вечный цикл здесь был бы вторым планировщиком поверх первого.
if [ "$#" -eq 0 ]; then
    set -- poll --once
fi

exec "$PYTHON" "$SCRIPT_DIR/bridge.py" "$@"
