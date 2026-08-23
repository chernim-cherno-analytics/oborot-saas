# Автоматическая связь Claude → Codex → Claude

## Роли

Claude — единственный исполнитель. Codex — reviewer без права менять файлы.
Автоматика никогда не сливает Pull Request самостоятельно.

## Что запускает что

1. Claude создаёт или обновляет PR из ветки `claude/*`.
2. `agent-codex-review.yml` проверяет точный HEAD SHA в режиме `:read-only`.
3. `APPROVE` завершает цикл. `REQUEST_CHANGES` немедленно запускает
   `agent-claude-fix.yml`.
4. Claude исправляет замечания, тестирует, commit/push делает в ту же ветку.
5. Новый SHA снова передаётся Codex.
6. `agent-watchdog.yml` каждые 10 минут ищет потерянные передачи. Зависший
   `WORKING` перезапускается; после трёх попыток ставится `NEEDS_HUMAN`.

## Однократная настройка владельцем repository

В `Settings → Secrets and variables → Actions` создать два repository secret:

* `OPENAI_API_KEY` — отдельный ключ OpenAI только для автоматического review;
* `ANTHROPIC_API_KEY` — отдельный ключ Anthropic для Claude Code Action.

Ключи нельзя присылать в чат, записывать в Issue, PR, файл или commit.

В `Settings → Actions → General → Workflow permissions` разрешить GitHub Actions
создавать commits и comments (`Read and write permissions`). Workflow всё равно
запрашивает минимальные права отдельно для каждого job.

## Ограничения безопасности

Автоматика принимает только открытые PR из этого же repository и только из
веток `claude/*`. Fork PR не получает секреты. Codex работает без права записи.
Claude не имеет права менять `.github/workflows/agent-*.yml`, делать merge,
rebase, force-push или менять бизнес-логику без решения владельца.

Официальные actions закреплены полными commit SHA, а не плавающим тегом.
Обновление их версий — отдельное проверяемое изменение.

## Первый сквозной тест

После merge автоматики:

1. Claude создаёт тестовую ветку `claude/agent-loop-smoke` и маленький PR.
2. Codex обязан оставить маркер `APPROVE` или `REQUEST_CHANGES` для точного SHA.
3. Для проверки обратного пути Codex возвращает безопасное тестовое замечание.
4. Claude публикует новый SHA; Codex автоматически проверяет его повторно.
5. Удаляется тестовая ветка/PR после проверки владельцем.

Канал называется `HEALTHY` только после этого цикла. Наличие файлов workflow и
секретов само по себе не доказывает работоспособность.
