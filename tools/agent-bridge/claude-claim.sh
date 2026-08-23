#!/bin/bash
# Единственный разрешённый способ позвать `claude` для правки кода в этом
# проекте вручную.
#
# Зачем это существует. 23.08.2026 фоновая задача увидела замечания Codex к
# PR #8 и запустила `claude`, хотя тот же PR в этот момент правила ручная
# сессия из другого чата. Дубль остановили до push — руками, потому что за
# экраном сидел человек. Двое исполнителей на одной ветке дают встречные
# правки одних строк, отклонённый push и коммит поверх чужой незаконченной
# работы; заметить это можно только глазами и только вовремя.
#
# Правило «одна бронь на Pull Request» есть в AGENTS.md с самого начала, но
# оно жило текстом: его соблюдал тот, кто прочитал. Эта обёртка делает его
# исполняемым. Она берёт ту же самую бронь, что и фоновый диспетчер, в том же
# каталоге, той же неделимой операцией — и только потом запускает `claude`
# как дочерний процесс. Занято — `claude` не стартует вовсе.
#
# Освобождение не требует дисциплины: бронь снимается, когда команда
# завершилась любым способом, а если процесс убили насовсем (`kill -9`,
# закрытое окно терминала), блокировку снимает ядро — и следующий, кто
# спросит, увидит, что владельца больше нет.
#
# Использование:
#
#   tools/agent-bridge/claude-claim.sh --pr 8 [--sha <полный SHA>] [-- <аргументы claude>]
#
# Примеры:
#
#   # интерактивная сессия для работы над PR #8
#   tools/agent-bridge/claude-claim.sh --pr 8
#
#   # разовая правка без интерактива
#   tools/agent-bridge/claude-claim.sh --pr 8 --sha 70e11b71... -- -p "почини замечание"
#
# Код возврата: код самой команды, 75 — Pull Request занят кем-то другим
# (ничего не запускалось), 2 — ошибка в аргументах.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

usage() {
    cat >&2 <<'TEXT'
Запуск claude под бронью на Pull Request.

  claude-claim.sh --pr <номер> [--sha <SHA>] [--owner <кто>] [--ttl <секунды>]
                  [--note <что делаем>] [-- <аргументы claude>]

Обязателен только --pr: бронь берётся на Pull Request, потому что двое,
правящих одну ветку на разных коммитах, мешают друг другу так же, как на одном.

Посмотреть, кто занял:   tools/agent-bridge/run.sh claim list
Снять зависшую бронь:    tools/agent-bridge/run.sh claim release --pr <номер> --force
                         (сначала остановите сам процесс — снятие брони его не убивает)
TEXT
}

PR=""
SHA=""
OWNER="${OBOROT_CLAIM_OWNER:-}"
TTL=""
NOTE="ручная сессия claude"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --pr) PR="${2:-}"; shift 2 ;;
        --pr=*) PR="${1#*=}"; shift ;;
        --sha) SHA="${2:-}"; shift 2 ;;
        --sha=*) SHA="${1#*=}"; shift ;;
        --owner) OWNER="${2:-}"; shift 2 ;;
        --owner=*) OWNER="${1#*=}"; shift ;;
        --ttl) TTL="${2:-}"; shift 2 ;;
        --ttl=*) TTL="${1#*=}"; shift ;;
        --note) NOTE="${2:-}"; shift 2 ;;
        --note=*) NOTE="${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        *)
            echo "claude-claim.sh: непонятный аргумент «$1»." >&2
            echo "Аргументы для самого claude пишутся после --." >&2
            usage
            exit 2
            ;;
    esac
done

if [ -z "$PR" ]; then
    echo "claude-claim.sh: не указан --pr. Бронь берётся на конкретный Pull Request." >&2
    usage
    exit 2
fi
case "$PR" in
    ''|*[!0-9]*)
        echo "claude-claim.sh: --pr должен быть числом, а не «$PR»." >&2
        exit 2
        ;;
esac

# PATH и python — теми же правилами, что у фоновой задачи (см. run.sh). Обёртку
# зовут из разных мест: терминал владельца, чужой чат, скрипт. Полагаться на то,
# что в каждом из них настроен один и тот же PATH, нельзя.
PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for extra in "$HOME/.local/bin" "$HOME/.claude/local" "$HOME/.bun/bin" "$HOME/.npm-global/bin"; do
    if [ -d "$extra" ]; then
        PATH="$PATH:$extra"
    fi
done
export PATH

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
    echo "claude-claim.sh: python3 не найден" >&2
    exit 1
fi

ARGS=(claim run --pr "$PR" --kind manual --note "$NOTE")
if [ -n "$SHA" ]; then
    ARGS+=(--sha "$SHA")
fi
if [ -n "$OWNER" ]; then
    ARGS+=(--owner "$OWNER")
fi
if [ -n "$TTL" ]; then
    ARGS+=(--ttl "$TTL")
fi

# `claude` не ищется здесь, а разрешается диспетчером: у него для этого уже есть
# и настройка CLAUDE_BIN, и закрытый список кандидатов. Две копии поиска рано или
# поздно разошлись бы, и обёртка запускала бы не тот claude, что фон.
exec "$PYTHON" "$SCRIPT_DIR/bridge.py" "${ARGS[@]}" -- claude "$@"
