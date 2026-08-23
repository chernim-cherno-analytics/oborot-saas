# agent-bridge — локальный диспетчер «Codex ревьюит → Claude чинит»

Короткая справка по файлам этого каталога. Роли, поток событий, восстановление
и стоимость описаны в `AGENT_AUTOMATION.md` в корне репозитория — здесь только
то, что нужно, чтобы запустить и остановить.

## Файлы

| Файл | Что делает |
|---|---|
| `bridge.py` | сам диспетчер: опрос, сбор ревью, вызов `claude`, тесты, commit, push, отчёт |
| `run.sh` | обёртка для launchd: чинит PATH и вызывает `bridge.py` |
| `install.sh` | ставит per-user LaunchAgent `com.oborot.agent-bridge` |
| `uninstall.sh` | снимает LaunchAgent; `--purge` убирает и состояние |
| `config.env.example` | образец настроек, копируется в `~/.config/oborot-agent-bridge/config.env` |

## Что нужно до запуска

`gh` установлен и авторизован (`gh auth status`) и настроен как credential
helper для git (`gh auth setup-git` — иначе фоновый push попросит пароль у
некого), `claude` установлен и авторизован подпиской Claude Max, `git` и
`python3` на месте, нативный Codex Cloud Code Review подключён к репозиторию и
настроен ревьюить push (на push он отзывается не всегда — диспетчер поэтому
просит ревью явно, см. «Отчётность»).

Ключей API не нужно ни одного. Диспетчер их не принимает и не читает.

## Команды

```bash
tools/agent-bridge/run.sh health              # проверка окружения, код 1 если плохо
tools/agent-bridge/run.sh poll --once --dry-run   # показать план, ничего не менять
tools/agent-bridge/run.sh poll --once         # один настоящий цикл
tools/agent-bridge/run.sh status --remote     # состояние + что видно на GitHub
tools/agent-bridge/run.sh logs -n 100         # хвост журнала
```

Порядок первого запуска: `health` → `poll --once --dry-run` → `poll --once` →
и только потом `install.sh`.

## Установка автоматики

```bash
tools/agent-bridge/install.sh      # раз в 60 с; OBOROT_BRIDGE_INTERVAL меняет интервал
tools/agent-bridge/uninstall.sh    # снять
```

`install.sh` сначала прогоняет `health` и отказывается ставить автоматику,
которая заведомо не заработает.

## Где что лежит

Состояние — вне рабочей копии, в `~/.local/state/oborot-agent-bridge/`:

* `state.json` — счётчики попыток и обработанные ревью;
* `bridge.log` — журнал;
* `lock` — блокировка, один прогон за раз;
* `checkout/` — собственный клон репозитория, в котором диспетчер и работает;
* `launchd.out.log`, `launchd.err.log` — то, что напечатал сам launchd.

Настройки — `~/.config/oborot-agent-bridge/config.env`.

## Отчётность

Каждый ключевой исход (`PUSHED`, `TESTS_FAILED`, `PUSH_REJECTED`,
`CLAUDE_FAILED`, `NO_CHANGES`, `NEEDS_HUMAN`) уходит комментарием в PR и копией
в issue-канал координации — по умолчанию issue #2,
`OBOROT_BRIDGE_COORDINATION_ISSUE=0` отключает копию. От дублей защищает маркер
в самом issue, поэтому повторный прогон и потеря локального состояния канал не
засоряют.

После успешного `PUSHED` диспетчер отдельным комментарием просит поревьюить
новый SHA — по умолчанию `@codex review`, настройка
`OBOROT_BRIDGE_REVIEW_REQUEST_COMMAND` (пусто — не просить). Это не вежливость:
23.08.2026 push нового коммита в ветку открытого PR нового ревью не запустил, а
ручная просьба запустила. Просьба уходит один раз на SHA, защита от дублей —
маркер в самом PR.

Замечания привязываются к SHA не по полю `commit_id` inline-комментария: GitHub
переставляет комментарий на новый коммит, если строка в файле не изменилась.
Привязка идёт через `pull_request_review_id` (у самого ревью `commit_id` не
переезжает) и `original_commit_id`. Сравнение по `commit_id` выдавало старые
замечания за новые и жгло попытки; так же считает и сторож.

## Границы

Диспетчер никогда не делает merge, rebase и force-push, никогда не сливает
Pull Request, останавливается после трёх попыток на один SHA, откатывает любые
правки в `.github/workflows/**` и `tools/agent-bridge/**` и не даёт `claude`
выполнять команды: тесты, коммит и публикацию делает он сам. Тесты — той же
командой, что CI: `python3 tests/run_all.py --jobs 3`.
