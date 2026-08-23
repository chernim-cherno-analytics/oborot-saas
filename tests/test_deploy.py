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
        self.env_file.write_text("OBOROT_ENV=prod\nOBOROT_COMMIT=old\n", encoding="utf-8")
        executable(self.venv / "bin" / "pip", """
grep -q 'release=fail' "$4" && exit 42
printf '%s\n' "$4" >> "$PIP_LOG"
""")
        executable(self.fake / "flock", '[ "$FLOCK_FAIL" = 1 ] && exit 1; exit 0\n')
        executable(self.fake / "systemctl", 'printf "restart\\n" >> "$RESTART_LOG"\n')
        executable(self.fake / "journalctl", 'printf "fake journal\\n"\n')
        executable(self.fake / "sqlite3", 'echo ok\n')
        executable(self.fake / "curl", """
head=$(git -C "$OBOROT_APP_DIR" rev-parse HEAD)
[ "$head" = "$BAD_SHA" ] && exit 22
printf '{"status":"ok"}\n'
""")

        self.restart_log = self.state / "restarts"
        self.pip_log = self.state / "pip"

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
            "RESTART_LOG": str(self.restart_log),
            "PIP_LOG": str(self.pip_log),
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
        check("успешный релиз возвращает 0", result.returncode == 0, result.stderr)
        check("HEAD переключён на точный target", box.head() == box.new)
        check("PREVIOUS_SHA хранит полный предыдущий SHA",
              (box.state / "PREVIOUS_SHA").read_text().strip() == box.old)
        check("OBOROT_COMMIT обновлён полным SHA", f"OBOROT_COMMIT={box.new}" in
              box.env_file.read_text())
        check("сервис перезапущен один раз", box.restart_log.read_text().count("restart") == 1)
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
              box.env_file.read_text())
        check("были restart релиза и restart отката",
              box.restart_log.read_text().count("restart") == 2)
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


def main() -> int:
    for scenario in (scenario_success, scenario_health_failure_rolls_back,
                     scenario_dependency_failure_does_not_switch,
                     scenario_parallel_deploy_is_rejected):
        print(f"\n== {scenario.__name__} ==")
        scenario()
    print("\nИТОГО: deploy-контур OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
