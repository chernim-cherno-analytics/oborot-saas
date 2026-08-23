#!/usr/bin/env python3
"""Изолированные проверки deploy/deploy.sh без systemd, сети и продовой БД."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy.sh"


def run(*args: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True):
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            capture_output=True, check=False)
    if check and result.returncode:
        raise AssertionError(f"{args} -> {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result


def executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Sandbox:
    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="oborot-deploy-test-"))
        self.origin = self.tmp / "origin.git"
        self.app = self.tmp / "app"
        self.state = self.tmp / "state"
        self.data = self.tmp / "data"
        self.venv = self.tmp / "venv"
        self.fake = self.tmp / "bin"
        for directory in (self.state, self.data, self.venv / "bin", self.fake):
            directory.mkdir(parents=True)

        run("git", "init", "--bare", str(self.origin), cwd=self.tmp)
        run("git", "clone", str(self.origin), str(self.app), cwd=self.tmp)
        run("git", "config", "user.email", "deploy-test@example.invalid", cwd=self.app)
        run("git", "config", "user.name", "Deploy test", cwd=self.app)
        (self.app / "requirements.txt").write_text("release=old\n", encoding="utf-8")
        run("git", "add", "requirements.txt", cwd=self.app)
        run("git", "commit", "-m", "old", cwd=self.app)
        run("git", "branch", "-M", "main", cwd=self.app)
        run("git", "push", "-u", "origin", "main", cwd=self.app)
        self.old = run("git", "rev-parse", "HEAD", cwd=self.app).stdout.strip()

        (self.app / "requirements.txt").write_text("release=new\n", encoding="utf-8")
        run("git", "commit", "-am", "new", cwd=self.app)
        run("git", "push", "origin", "main", cwd=self.app)
        self.new = run("git", "rev-parse", "HEAD", cwd=self.app).stdout.strip()
        run("git", "checkout", "--detach", self.old, cwd=self.app)

        self.env_file = self.state / "env"
        self.env_file.write_text(
            f"OBOROT_ENV=prod\nOBOROT_COMMIT={self.old}\n", encoding="utf-8")
        (self.venv / "live-marker").write_text("old live venv", encoding="utf-8")
        self.pip_template = self.state / "fake-pip"
        executable(self.pip_template, """
case "$1" in
  install)
    grep -q 'release=fail' "$4" && exit 42
    printf '%s\n' "$4" >> "$PIP_LOG"
    ;;
  check) [ "${PIP_CHECK_FAIL:-0}" != 1 ] || exit 43; exit 0 ;;
  *) exit 44 ;;
esac
""")
        executable(self.fake / "python", """
[ "$1" = -m ] && [ "$2" = venv ] || exit 45
mkdir -p "$3/bin"
cp "$FAKE_PIP_TEMPLATE" "$3/bin/pip"
chmod +x "$3/bin/pip"
printf 'prepared\n' > "$3/prepared-marker"
""")
        executable(self.fake / "flock", '[ "$FLOCK_FAIL" = 1 ] && exit 1; exit 0\n')
        executable(self.fake / "systemctl", """
printf 'restart\n' >> "$RESTART_LOG"
if [ "${SYSTEMCTL_FAIL_ONCE:-0}" = 1 ] && [ ! -f "$RESTART_FAILED_MARKER" ]; then
  : > "$RESTART_FAILED_MARKER"
  exit 46
fi
""")
        executable(self.fake / "journalctl", 'printf "fake journal\\n"\n')
        executable(self.fake / "sqlite3", """
case "$2" in
  .backup*)
    target=$(printf '%s' "$2" | sed -n "s/^\\.backup '\\(.*\\)'$/\\1/p")
    cp "$1" "$target"
    ;;
  *) echo ok ;;
esac
""")
        # macOS find не поддерживает GNU -printf; ротация не относится к
        # проверяемой транзакции и здесь имитируется пустым списком удаления.
        executable(self.fake / "find", "exit 0\n")
        executable(self.fake / "curl", """
head=$(git -C "$OBOROT_APP_DIR" rev-parse HEAD)
[ "$head" = "$BAD_SHA" ] && exit 22
printf '{"status":"ok"}\n'
""")

        self.restart_log = self.state / "restarts"
        self.pip_log = self.state / "pip"
        (self.data / "oborot.db").write_text("fake sqlite source", encoding="utf-8")

    def close(self) -> None:
        shutil.rmtree(self.tmp)

    def environment(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "PATH": f"{self.fake}:{env['PATH']}",
            "OBOROT_APP_DIR": str(self.app),
            "OBOROT_VENV": str(self.venv),
            "OBOROT_DATA_DIR": str(self.data),
            "OBOROT_ENV_FILE": str(self.env_file),
            "OBOROT_STATE_DIR": str(self.state),
            "OBOROT_PREVIOUS_FILE": str(self.state / "PREVIOUS_SHA"),
            "OBOROT_LOCK_FILE": str(self.state / "deploy.lock"),
            "OBOROT_HEALTH_ATTEMPTS": "1",
            "OBOROT_HEALTH_DELAY": "0",
            "OBOROT_PYTHON": str(self.fake / "python"),
            "RESTART_LOG": str(self.restart_log),
            "RESTART_FAILED_MARKER": str(self.state / "restart-failed"),
            "PIP_LOG": str(self.pip_log),
            "FAKE_PIP_TEMPLATE": str(self.pip_template),
            "PIP_CHECK_FAIL": "0",
            "SYSTEMCTL_FAIL_ONCE": "0",
            "FLOCK_FAIL": "0",
            "BAD_SHA": "none",
        })
        env.update(extra)
        return env

    def deploy(self, **extra: str):
        return run("bash", str(DEPLOY), cwd=self.app,
                   env=self.environment(**extra), check=False)

    def head(self) -> str:
        return run("git", "rev-parse", "HEAD", cwd=self.app).stdout.strip()


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  OK  {name}")


def scenario_success() -> None:
    box = Sandbox()
    try:
        result = box.deploy()
        check("успешный релиз возвращает 0", result.returncode == 0,
              result.stdout + result.stderr)
        check("HEAD переключён на точный target", box.head() == box.new)
        check("PREVIOUS_SHA хранит полный предыдущий SHA",
              (box.state / "PREVIOUS_SHA").read_text().strip() == box.old)
        check("OBOROT_COMMIT обновлён полным SHA", f"OBOROT_COMMIT={box.new}" in
              box.env_file.read_text())
        check("сервис перезапущен один раз", box.restart_log.read_text().count("restart") == 1)
        check("live venv заменён только после полной подготовки",
              (box.venv / "prepared-marker").is_file())
        previous_venv = Path((box.state / "PREVIOUS_VENV").read_text().strip())
        check("предыдущее окружение сохранено для отката",
              (previous_venv / "live-marker").is_file())
        backups = list((box.data / "backups").glob("oborot-*.db"))
        check("перед релизом создана копия базы", len(backups) == 1)
        check("копия снята с ожидаемого файла",
              backups[0].read_text(encoding="utf-8") == "fake sqlite source")
    finally:
        box.close()


def scenario_health_failure_rolls_back() -> None:
    box = Sandbox()
    try:
        result = box.deploy(BAD_SHA=box.new)
        check("неудачный релиз возвращает 1 после успешного отката", result.returncode == 1,
              result.stdout + result.stderr)
        check("код автоматически возвращён", box.head() == box.old)
        check("переменная версии автоматически возвращена", f"OBOROT_COMMIT={box.old}" in
              box.env_file.read_text(), box.env_file.read_text())
        check("были restart релиза и restart отката",
              box.restart_log.read_text().count("restart") == 2)
        check("откат вернул исходное live venv", (box.venv / "live-marker").is_file())
        check("откат явно записан в отчёт", "ОТКАТ ВЫПОЛНЕН" in result.stderr)
    finally:
        box.close()


def scenario_dependency_failure_does_not_switch() -> None:
    box = Sandbox()
    try:
        run("git", "checkout", "--detach", box.new, cwd=box.app)
        (box.app / "requirements.txt").write_text("release=fail\n", encoding="utf-8")
        run("git", "add", "requirements.txt", cwd=box.app)
        run("git", "commit", "-m", "bad deps", cwd=box.app)
        run("git", "push", "origin", "HEAD:main", cwd=box.app)
        bad = run("git", "rev-parse", "HEAD", cwd=box.app).stdout.strip()
        run("git", "checkout", "--detach", box.old, cwd=box.app)
        result = box.deploy(BAD_SHA=bad)
        check("ошибка pip останавливает релиз", result.returncode != 0)
        check("при ошибке pip код не переключён", box.head() == box.old)
        check("при ошибке pip сервис не перезапущен", not box.restart_log.exists())
        check("ошибка pip не изменила live venv", (box.venv / "live-marker").is_file())
        check("при ошибке pip база ещё не копировалась",
              not list((box.data / "backups").glob("oborot-*.db")))
    finally:
        box.close()


def scenario_parallel_deploy_is_rejected() -> None:
    box = Sandbox()
    try:
        result = box.deploy(FLOCK_FAIL="1")
        check("занятый lock отклоняет второй деплой", result.returncode != 0)
        check("сообщение объясняет причину", "другой деплой уже выполняется" in result.stderr)
        check("второй деплой ничего не переключает", box.head() == box.old)
    finally:
        box.close()


def scenario_restart_failure_uses_global_rollback() -> None:
    box = Sandbox()
    try:
        result = box.deploy(SYSTEMCTL_FAIL_ONCE="1")
        check("ошибка restart после переключения возвращает failure",
              result.returncode == 1, result.stdout + result.stderr)
        check("глобальный rollback вернул код", box.head() == box.old)
        check("глобальный rollback вернул env",
              f"OBOROT_COMMIT={box.old}" in box.env_file.read_text())
        check("глобальный rollback вернул прежний venv",
              (box.venv / "live-marker").is_file())
        check("после первого сбоя выполнен restart отката",
              box.restart_log.read_text().count("restart") == 2)
    finally:
        box.close()


def scenario_untracked_file_blocks_deploy() -> None:
    box = Sandbox()
    try:
        (box.app / "accidental-secret.txt").write_text("not committed", encoding="utf-8")
        result = box.deploy()
        check("untracked файл останавливает деплой", result.returncode != 0)
        check("сообщение называет untracked проверку", "untracked" in result.stderr)
        check("до подготовки venv и restart дело не дошло",
              not box.pip_log.exists() and not box.restart_log.exists())
    finally:
        box.close()


def scenario_unsafe_previous_venv_path_is_rejected() -> None:
    box = Sandbox()
    try:
        victim = box.tmp / "must-survive"
        victim.mkdir()
        (victim / "marker").write_text("keep", encoding="utf-8")
        (box.state / "PREVIOUS_VENV").write_text(
            f"{box.venv}.rollback.fake/../../must-survive\n", encoding="utf-8")
        result = box.deploy()
        check("путь с traversal в PREVIOUS_VENV отклонён", result.returncode != 0)
        check("чужой каталог не удалён", (victim / "marker").is_file())
    finally:
        box.close()


def main() -> int:
    for scenario in (scenario_success, scenario_health_failure_rolls_back,
                     scenario_dependency_failure_does_not_switch,
                     scenario_parallel_deploy_is_rejected,
                     scenario_restart_failure_uses_global_rollback,
                     scenario_untracked_file_blocks_deploy,
                     scenario_unsafe_previous_venv_path_is_rejected):
        print(f"\n== {scenario.__name__} ==")
        scenario()
    print("\nИТОГО: deploy-контур OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
