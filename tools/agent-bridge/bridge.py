#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальный диспетчер связи «Codex ревьюит → Claude чинит».

Зачем этот файл вообще существует. Прошлая версия автоматики жила в GitHub
Actions и вызывала платные API OpenAI и Anthropic: `openai/codex-action` и
`anthropics/claude-code-action`. Владелец выбрал другой размен — обе стороны уже
оплачены подпиской: ревью делает нативный Codex Cloud Code Review, подключённый
к репозиторию, а исправления делает локальный `claude` под подпиской Claude Max.
Значит, ключей `OPENAI_API_KEY` и `ANTHROPIC_API_KEY` в проекте быть не должно:
каждый вызов по ним — это счёт за то, за что уже заплачено.

Что осталось от прежней схемы и почему. GitHub остаётся долговременной очередью
и журналом: ревью, замечания, маркеры и отчёты живут в PR, а не в памяти
процесса. Локальная машина может уснуть, перезагрузиться и потерять состояние —
GitHub от этого ничего не забудет, и следующий запуск подхватит ровно то, что
не доделано.

Поток событий:

  1. Claude пушит новый SHA в ветку `claude/*`, PR открыт.
  2. Нативный Codex Cloud Code Review сам просыпается на push и оставляет ревью:
     тело ревью и inline-замечания, привязанные к точному commit SHA.
  3. Этот диспетчер (LaunchAgent, раз в минуту) видит новое ревью **ровно для
     текущего HEAD SHA**, собирает его целиком и зовёт локальный `claude -p`.
  4. Claude правит ТОЛЬКО названные замечания. Тесты, commit и push делает не
     он, а диспетчер — так проще доказать, что в ветку уехало именно то, что
     прошло тесты.
  5. Новый SHA снова будит Codex. Круг замыкается.

Жёсткие ограничения, зашитые в код, а не только в инструкцию:

  * никогда `merge`, `rebase`, `--force`/`--force-with-lease`;
  * никогда merge Pull Request — решение о слиянии принимает владелец;
  * три попытки на один SHA, дальше `NEEDS_HUMAN` и остановка;
  * `claude` запускается без права выполнять команды: ему разрешены только
    чтение и правка файлов. Тесты и git — на стороне диспетчера;
  * правки в `.github/workflows/**` и в сам `tools/agent-bridge/**` откатываются
    до коммита: автоматика не переписывает автоматику;
  * ни один секрет не читается, не передаётся и не пишется в лог. Всё, что
    похоже на токен, вычищается из логов и комментариев.

Команды:

    bridge.py poll --once          один цикл и выход (режим LaunchAgent)
    bridge.py poll --interval 60   вечный цикл, если запускать руками
    bridge.py poll --once --dry-run   ничего не менять, показать план
    bridge.py status               что диспетчер помнит и что видит на GitHub
    bridge.py health               проверка окружения, код возврата 0/1
    bridge.py logs -n 100          хвост журнала

Код возврата: 0 — цикл прошёл, 1 — цикл или проверка упали.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1"
MARKER_PREFIX = "<!-- oborot-bridge:v1"
MIRROR_PREFIX = "<!-- oborot-bridge-mirror:v1"
# Маркер просьбы «поревьюй новый SHA». Отдельный от отчётного: отчёт и просьба
# живут разными жизнями, и защита от дублей у просьбы своя — по новому SHA.
REVIEW_REQUEST_PREFIX = "<!-- oborot-bridge-review-request:v1"

# Исходы, которые видит не только автор PR, но и второй агент: по ним он решает,
# ждать ему нового SHA, читать хвост тестов или звать человека. Промежуточные
# состояния (`WORKING`, `CLEAN`, `APPROVED`) в общий канал не идут — там нужна
# сводка, а не поток.
MIRRORED_STATUSES = (
    "PUSHED",
    "TESTS_FAILED",
    "PUSH_REJECTED",
    "CLAUDE_FAILED",
    "NO_CHANGES",
    "NEEDS_HUMAN",
)

# --------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------

# Порядок источников: переменная окружения → файл конфигурации → значение по
# умолчанию. Файл нужен затем, что LaunchAgent не наследует окружение
# интерактивной сессии: в нём нет ни PATH из ~/.zshrc, ни экспортов терминала.
CONFIG_KEYS = {
    "REPO": "",
    "BRANCH_PREFIX": "claude/",
    "REVIEWER_LOGINS": "chatgpt-codex-connector[bot]",
    "MAX_ATTEMPTS": "3",
    "CHECKOUT": "",
    # Ровно та команда, которой проверяет CI (`.github/workflows/ci.yml`).
    # `--jobs 3` здесь не украшение: без него раннер поднимает все наборы разом,
    # они мешают друг другу портами и временем, и получаются падения, которых в
    # CI нет. Диспетчер, который «краснее» CI, бесполезен — его вывод перестают
    # читать; диспетчер, который «зеленее», опаснее вдвойне.
    "TEST_CMD": "python3 tests/run_all.py --jobs 3",
    "TEST_TIMEOUT": "2700",
    "TEST_PYTHON": "",
    "CLAUDE_BIN": "",
    "CLAUDE_TIMEOUT": "1800",
    "CLAUDE_MODEL": "",
    "CLAUDE_EXTRA_ARGS": "",
    "GH_BIN": "",
    "GIT_BIN": "",
    "PYTHON_BIN": "",
    "STATE_DIR": "",
    "NOOP_REVIEW_PATTERN": (
        r"(no (major |significant |blocking )?issues|"
        r"looks good|lgtm|нет замечаний|замечаний нет|"
        r"no comments to post|didn't find any)"
    ),
    "MAX_INLINE_COMMENTS": "60",
    "LOG_KEEP_BYTES": "5242880",
    # Канонический issue-канал координации агентов. 0 или пусто — не зеркалить.
    "COORDINATION_ISSUE": "2",
    # Чем будить ревьюера после успешного push. Пусто — не будить.
    # Наблюдение 23.08.2026: push 1c7d616 в ветку открытого PR нового ревью
    # Codex НЕ запустил, а ручной комментарий `@codex review` — запустил.
    # Значит «новый SHA сам разбудит Codex» — предположение, а не факт, и на
    # нём круг замыкать нельзя.
    "REVIEW_REQUEST_COMMAND": "@codex review",
}

ENV_PREFIX = "OBOROT_BRIDGE_"

# Пути, по которым исполняемые файлы ищутся, если их нет в PATH. Список
# сознательно закрытый: диспетчер не читает пользовательские rc-файлы и не
# выполняет ничего из каталогов, доступных на запись кому угодно.
BIN_CANDIDATES = {
    "claude": [
        "~/.claude/local/claude",
        "~/.local/bin/claude",
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        "~/.bun/bin/claude",
        "~/.npm-global/bin/claude",
        "~/node_modules/.bin/claude",
    ],
    "gh": [
        "/opt/homebrew/bin/gh",
        "/usr/local/bin/gh",
        "~/.local/bin/gh",
    ],
    "git": [
        "/opt/homebrew/bin/git",
        "/usr/bin/git",
        "/usr/local/bin/git",
    ],
    "python3": [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ],
}

SAFE_PATH = ":".join([
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
])

# То, что не должно попасть ни в лог, ни в комментарий PR. Диспетчер токенов не
# читает, но `gh` и `git` могут их выплюнуть в тексте ошибки — а лог и
# комментарий переживут гораздо дольше, чем сам инцидент.
SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{16,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"x-access-token:[^@\s]+@"),
    re.compile(r"(?i)(authorization|bearer)\s*[:=]\s*\S+"),
]

# Пути, которые автоматика себе править не даёт. Причина простая: если правка
# ревью может изменить сам диспетчер или workflow, то один неудачный прогон
# ломает механизм, которым его чинят.
PROTECTED_PATHS = (
    ".github/workflows/",
    "tools/agent-bridge/",
)


def redact(text: str) -> str:
    """Убрать из текста всё, что похоже на секрет."""
    if not text:
        return ""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


class Config:
    def __init__(self, overrides: dict | None = None):
        self.values = dict(CONFIG_KEYS)
        for path in self.config_paths():
            self.values.update(self._read_file(path))
        for key in CONFIG_KEYS:
            env = os.environ.get(ENV_PREFIX + key)
            if env is not None:
                self.values[key] = env
        if overrides:
            self.values.update({k: v for k, v in overrides.items() if v})

    @staticmethod
    def config_paths() -> list[Path]:
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        return [base / "oborot-agent-bridge" / "config.env"]

    @staticmethod
    def _read_file(path: Path) -> dict:
        out: dict[str, str] = {}
        if not path.is_file():
            return out
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith(ENV_PREFIX):
                key = key[len(ENV_PREFIX):]
            value = value.strip().strip('"').strip("'")
            if key in CONFIG_KEYS:
                out[key] = value
        return out

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def get_int(self, key: str, fallback: int) -> int:
        try:
            return int(str(self.values.get(key, "")).strip())
        except (TypeError, ValueError):
            return fallback

    def reviewer_logins(self) -> list[str]:
        raw = self.get("REVIEWER_LOGINS")
        return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


# --------------------------------------------------------------------------
# Пути, журнал, состояние
# --------------------------------------------------------------------------

def state_dir(cfg: Config) -> Path:
    configured = cfg.get("STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return base / "oborot-agent-bridge"


def venv_python(root: Path) -> Path:
    """Интерпретатор venv, который диспетчер ищет сам, — <каталог состояния>/venv."""
    return root / "venv" / "bin" / "python"


def resolve_test_python(cfg: Config) -> tuple[str, str]:
    """Каким интерпретатором гонять тесты. Возвращает (путь, источник).

    Источник: `config` — задан явно (переменной окружения или файлом настроек),
    `venv` — найден автоматически в каталоге состояния, `""` — не нашлось ничего.

    Почему автопоиск, а не «пропишите путь в config.env». Прописанный путь
    существует ровно в одном месте — в файле настроек той машины, где его
    прописали руками. Фоновая задача читает окружение launchd, установщик —
    свою оболочку, health — свой процесс; любой из них может не увидеть
    настройку, и тогда тесты пойдут системным python3, у которого нет
    зависимостей проекта. Набор упадёт на первом импорте, диспетчер отчитается
    `TESTS_FAILED`, и всякая правка по ревью встанет — при том что правка
    может быть верной. Единственный способ этого избежать — чтобы путь
    вычисляла одна функция от каталога состояния, а каталог состояния и так
    известен всем троим.

    Явно заданное значение возвращается как есть, даже если файла нет: молча
    подменить его на venv значило бы гонять тесты не тем, что просил владелец.
    Проверку исполняемости делает вызывающий (health и install.sh) — им есть
    что сказать пользователю, а run_tests обязан работать и без неё.
    """
    explicit = cfg.get("TEST_PYTHON").strip()
    if explicit:
        return str(Path(explicit).expanduser()), "config"
    candidate = venv_python(state_dir(cfg))
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate), "venv"
    return "", ""


class Log:
    """Журнал в файл и на stdout.

    Один файл, усечение по размеру. Ротации по времени намеренно нет: журнал
    читают глазами после сбоя, а не собирают в систему логирования.
    """

    def __init__(self, path: Path, keep_bytes: int, echo: bool = True):
        self.path = path
        self.keep_bytes = keep_bytes
        self.echo = echo
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._truncate()

    def _truncate(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.keep_bytes:
                tail = self.path.read_bytes()[-self.keep_bytes // 2:]
                self.path.write_bytes(b"... journal truncated ...\n" + tail)
        except OSError:
            pass

    def write(self, level: str, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = "{} {:<5} {}".format(stamp, level, redact(message))
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        if self.echo:
            stream = sys.stderr if level in ("ERROR", "WARN") else sys.stdout
            print(line, file=stream, flush=True)

    def info(self, message: str) -> None:
        self.write("INFO", message)

    def warn(self, message: str) -> None:
        self.write("WARN", message)

    def error(self, message: str) -> None:
        self.write("ERROR", message)


class State:
    """Долговременное состояние вне рабочей копии.

    Вне копии — потому что рабочую копию диспетчер сам же и чистит
    (`git clean`), а счётчик попыток обязан пережить чистку: иначе цикл
    «попытка — сброс — попытка» станет бесконечным.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"version": VERSION, "prs": {}, "cycles": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            backup = self.path.with_suffix(".corrupt")
            try:
                shutil.copy2(self.path, backup)
            except OSError:
                pass
            return {"version": VERSION, "prs": {}, "cycles": 0}
        data.setdefault("prs", {})
        data.setdefault("cycles", 0)
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def head(self, pr_number: int, sha: str) -> dict:
        pr = self.data["prs"].setdefault(str(pr_number), {"heads": {}})
        pr.setdefault("heads", {})
        return pr["heads"].setdefault(sha, {
            "attempts": 0,
            "processed": [],
            # Идентификаторы отдельных замечаний, уже ушедших в работу. Нужны
            # затем, что порция может быть меньше ревью: по отпечатку порции
            # хвост замечаний не отследить.
            "processed_ids": [],
            "status": "NEW",
            "status_at": None,
            "needs_human_notified": False,
        })

    def prune(self, open_pr_numbers: set[int], keep_heads: int = 8) -> None:
        """Забыть закрытые PR и старые SHA — файл не должен расти вечно."""
        for number in list(self.data["prs"].keys()):
            if int(number) not in open_pr_numbers:
                del self.data["prs"][number]
                continue
            heads = self.data["prs"][number].get("heads", {})
            if len(heads) > keep_heads:
                ordered = sorted(heads.items(), key=lambda kv: kv[1].get("status_at") or "")
                for sha, _ in ordered[:len(heads) - keep_heads]:
                    del heads[sha]


class Lock:
    """Ровно один диспетчер за раз.

    LaunchAgent будит нас раз в минуту, а один прогон `claude` плюс тесты идут
    десятки минут. Без блокировки на одном SHA сошлись бы несколько правок
    сразу — и вся арифметика попыток потеряла бы смысл.
    """

    def __init__(self, path: Path, write_owner: bool = True):
        self.path = path
        self.write_owner = write_owner
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return None
        if self.write_owner:
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write("{} {}\n".format(os.getpid(), datetime.now(timezone.utc).isoformat()))
            self.handle.flush()
        return self

    def __exit__(self, *exc):
        if self.handle:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
        return False


# --------------------------------------------------------------------------
# Поиск исполняемых файлов и запуск процессов
# --------------------------------------------------------------------------

def find_binary(name: str, configured: str = "") -> str | None:
    """Найти исполняемый файл, не полагаясь на PATH из терминала.

    LaunchAgent стартует с урезанным PATH (`/usr/bin:/bin:/usr/sbin:/sbin`), в
    котором нет ни `gh` из Homebrew, ни `claude` из ~/.local/bin. Поэтому
    сначала явная настройка, потом PATH процесса, потом безопасный PATH, и
    только потом закрытый список кандидатов.
    """
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    found = shutil.which(name)
    if found:
        return found
    found = shutil.which(name, path=SAFE_PATH)
    if found:
        return found
    for raw in BIN_CANDIDATES.get(name, []):
        candidate = Path(raw).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def run(argv: list[str], cwd: Path | None = None, timeout: int = 300,
        stdin_text: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Запуск без оболочки. Список аргументов, никакого shell=True."""
    process_env = dict(os.environ)
    process_env.setdefault("PATH", SAFE_PATH)
    # Отключаем интерактивность на всякий случай: под LaunchAgent некому
    # отвечать на приглашение ввести пароль, и процесс просто повиснет.
    process_env["GIT_TERMINAL_PROMPT"] = "0"
    process_env["GIT_ASKPASS"] = "true"
    process_env["CLICOLOR"] = "0"
    process_env["NO_COLOR"] = "1"
    if env:
        process_env.update(env)
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        # Пустой stdin вместо унаследованного: под LaunchAgent некому ответить
        # на приглашение ввода, и процесс, который его ждёт, висит до таймаута.
        input=stdin_text if stdin_text is not None else "",
        capture_output=True,
        text=True,
        timeout=timeout,
        env=process_env,
        check=False,
    )


class Tools:
    """Разрешённые исполняемые файлы, найденные один раз за прогон."""

    def __init__(self, cfg: Config):
        self.gh = find_binary("gh", cfg.get("GH_BIN"))
        self.git = find_binary("git", cfg.get("GIT_BIN"))
        self.claude = find_binary("claude", cfg.get("CLAUDE_BIN"))
        self.python = find_binary("python3", cfg.get("PYTHON_BIN"))

    def missing(self) -> list[str]:
        return [name for name, value in
                (("gh", self.gh), ("git", self.git), ("claude", self.claude), ("python3", self.python))
                if not value]


# --------------------------------------------------------------------------
# GitHub: очередь и журнал
# --------------------------------------------------------------------------

def parse_api_output(text: str):
    """Разобрать вывод `gh api --paginate`.

    Одного `json.loads` тут мало, и это не теория. При `--paginate` gh печатает
    КАЖДУЮ страницу отдельным значением JSON, одно за другим: два массива по сто
    элементов — это не «массив из двухсот», а два массива подряд. `json.loads`
    на таком тексте падает с «Extra data», и PR, у которого набралось больше ста
    комментариев, переставал обрабатываться совсем — причём в самый неудобный
    момент, на долгоживущей ветке.

    Разбираем поток по одному значению (`raw_decode`) и, если все страницы —
    списки, склеиваем их в один список: вызывающему коду не должно быть видно,
    сколько страниц отдал GitHub. Разнородный поток (страницы-объекты, как у
    поиска) возвращаем списком страниц — такие эндпоинты диспетчер не зовёт, но
    молча склеивать их было бы враньём.
    """
    text = (text or "").strip()
    if not text:
        return None
    decoder = json.JSONDecoder()
    pages = []
    index = 0
    while index < len(text):
        value, end = decoder.raw_decode(text, index)
        pages.append(value)
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
    if len(pages) == 1:
        return pages[0]
    if all(isinstance(page, list) for page in pages):
        merged: list = []
        for page in pages:
            merged.extend(page)
        return merged
    return pages


class GitHub:
    def __init__(self, tools: Tools, repo: str, log: Log):
        self.gh = tools.gh
        self.repo = repo
        self.log = log

    def api(self, path: str, paginate: bool = True, method: str = "GET",
            body: dict | None = None, timeout: int = 120):
        argv = [self.gh, "api", "-H", "Accept: application/vnd.github+json"]
        if method != "GET":
            argv += ["--method", method]
        if paginate and method == "GET":
            argv.append("--paginate")
        argv.append(path)
        stdin_text = None
        if body is not None:
            argv += ["--input", "-"]
            stdin_text = json.dumps(body, ensure_ascii=False)
        result = run(argv, timeout=timeout, stdin_text=stdin_text)
        if result.returncode != 0:
            raise RuntimeError("gh api {} -> {}: {}".format(
                path, result.returncode, redact(result.stderr.strip())[:600]))
        return parse_api_output(result.stdout)

    def open_agent_prs(self, branch_prefix: str) -> list[dict]:
        """Открытые PR из веток `<prefix>*` этого же репозитория.

        PR из форка отсекается специально: у форка чужой владелец, а мы после
        правки пушим в head-ветку. Пуш в чужую ветку — это не наша роль.
        """
        prs = self.api("repos/{}/pulls?state=open&per_page=100".format(self.repo)) or []
        out = []
        for pr in prs:
            head = pr.get("head") or {}
            head_repo = (head.get("repo") or {}).get("full_name")
            if head_repo != self.repo:
                continue
            if not (head.get("ref") or "").startswith(branch_prefix):
                continue
            if pr.get("draft"):
                continue
            out.append(pr)
        return out

    def reviews(self, number: int) -> list[dict]:
        return self.api("repos/{}/pulls/{}/reviews?per_page=100".format(self.repo, number)) or []

    def review_comments(self, number: int) -> list[dict]:
        return self.api("repos/{}/pulls/{}/comments?per_page=100".format(self.repo, number)) or []

    def issue_comments(self, number: int) -> list[dict]:
        return self.api("repos/{}/issues/{}/comments?per_page=100".format(self.repo, number)) or []

    def comment(self, number: int, body: str) -> None:
        self.api("repos/{}/issues/{}/comments".format(self.repo, number),
                 paginate=False, method="POST", body={"body": redact(body)})


def login_matches(login: str, patterns: list[str]) -> bool:
    """Сравнение логина ревьюера.

    Точное совпадение или совпадение без суффикса `[bot]` — GitHub возвращает
    логин приложения то с суффиксом, то без, в зависимости от эндпоинта.
    """
    if not login:
        return False
    normalized = login.lower()
    bare = normalized[:-5] if normalized.endswith("[bot]") else normalized
    for pattern in patterns:
        candidate = pattern.lower()
        candidate_bare = candidate[:-5] if candidate.endswith("[bot]") else candidate
        if normalized == candidate or bare == candidate_bare:
            return True
    return False


class ReviewBundle:
    """Ревью Codex для одного точного SHA, собранное целиком."""

    def __init__(self, sha: str):
        self.sha = sha
        self.reviews: list[dict] = []
        self.inline: list[dict] = []
        self.notes: list[dict] = []
        self.approved = False
        # Сколько inline-замечаний осталось за бортом этого прогона. Считается
        # при нарезке порции и нужно только для честной строки в запросе.
        self.dropped = 0

    @property
    def ids(self) -> list[str]:
        parts = ["r{}".format(r["id"]) for r in self.reviews]
        parts += ["c{}".format(c["id"]) for c in self.inline]
        parts += ["n{}".format(n["id"]) for n in self.notes]
        return sorted(parts)

    @property
    def fingerprint(self) -> str:
        return ",".join(self.ids)

    def is_empty(self) -> bool:
        return not (self.reviews or self.inline or self.notes)

    def without(self, done_ids) -> "ReviewBundle":
        """Копия без уже обработанных замечаний.

        Обработанное отсекается поштучно, а не целым ревью: иначе замечание,
        которое не поместилось в прошлый прогон, никогда бы не доехало.
        """
        done = set(done_ids or ())
        rest = ReviewBundle(self.sha)
        rest.approved = self.approved
        rest.reviews = [r for r in self.reviews if "r{}".format(r["id"]) not in done]
        rest.inline = [c for c in self.inline if "c{}".format(c["id"]) not in done]
        rest.notes = [n for n in self.notes if "n{}".format(n["id"]) not in done]
        return rest

    def portion(self, max_inline: int) -> "ReviewBundle":
        """Порция на один прогон: не больше `max_inline` inline-замечаний.

        Отрезанные замечания НЕ считаются обработанными — ни в отпечатке, ни в
        списке идентификаторов. Раньше отпечаток брался со всего ревью целиком,
        и уже на следующем цикле оно опознавалось как «это мы уже делали», хотя
        хвост замечаний Claude в глаза не видел. Обещание «остальные придут
        следующим циклом» в запросе было ложным.
        """
        if max_inline <= 0 or len(self.inline) <= max_inline:
            return self
        part = ReviewBundle(self.sha)
        part.approved = self.approved
        part.reviews = list(self.reviews)
        part.notes = list(self.notes)
        part.inline = self.inline[:max_inline]
        part.dropped = len(self.inline) - max_inline
        return part

    def is_actionable(self, noop_pattern: re.Pattern) -> bool:
        """Есть ли что чинить.

        Codex оставляет ревью и тогда, когда замечаний нет. Будить `claude`
        ради «issues not found» — это трата подписки и лишний шум в PR, поэтому
        пустое ревью распознаётся и пропускается.
        """
        if self.inline:
            return True
        for item in self.reviews + self.notes:
            body = (item.get("body") or "").strip()
            if not body:
                continue
            if noop_pattern.search(body):
                continue
            return True
        return False


def inline_anchor_sha(comment: dict, review_commits: dict) -> str:
    """SHA, к которому inline-замечание относилось В МОМЕНТ ревью.

    Поле `commit_id` у inline-замечания для этого не годится, и это не теория.
    Замечание 3839314973 в PR #7 было оставлено к ревью коммита 193602bf, а
    после push нового коммита GitHub переписал ему `commit_id` на 1c7d616c —
    строка в файле никуда не делась, и API «переехал» комментарий на свежий
    коммит. Диспетчер, сравнивающий `commit_id` с текущим HEAD, из-за этого
    видел четыре уже отработанных замечания как четыре новых и заново будил
    Claude чинить то, что уже починено, сжигая попытки для этого SHA.

    Устойчивых полей два. `pull_request_review_id` ведёт к ревью, у которого
    `commit_id` не переезжает (проверено на том же PR: у ревью 5003117263 он
    так и остался 193602bf) — это самая надёжная привязка, потому что решение
    «к какому коммиту относится ревью» принимается один раз. `original_commit_id`
    хранит коммит, на котором замечание создано, и выручает для одиночных
    комментариев вне ревью. `commit_id` остаётся последним запасным вариантом:
    только для payload, где нет ни того, ни другого.
    """
    review_id = comment.get("pull_request_review_id")
    if review_id is not None:
        anchored = review_commits.get(review_id)
        if anchored:
            return anchored
    return comment.get("original_commit_id") or comment.get("commit_id") or ""


def collect_review(gh: GitHub, number: int, head_sha: str, logins: list[str]) -> ReviewBundle:
    """Собрать замечания ревьюера, привязанные ровно к `head_sha`.

    «Ровно к SHA» — не придирка. Комментарий к прошлому коммиту мог уже быть
    исправлен, и починка «по устаревшему замечанию» ломает свежий код и жжёт
    попытки. Поэтому сравнение точное и по полному SHA — но сравнивать надо с
    исходной привязкой замечания, а не с той, куда его переставил GitHub:
    см. `inline_anchor_sha`.
    """
    bundle = ReviewBundle(head_sha)
    short = head_sha[:7]

    # id ревью → коммит, к которому оно относится. Собирается по всем ревью
    # ревьюера, включая APPROVED: карта нужна для привязки замечаний, а не для
    # отбора самих ревью.
    review_commits: dict = {}

    for review in gh.reviews(number):
        if not login_matches((review.get("user") or {}).get("login", ""), logins):
            continue
        review_id = review.get("id")
        review_sha = review.get("commit_id") or ""
        if review_id is not None and review_sha:
            review_commits[review_id] = review_sha
        if review_sha != head_sha:
            continue
        state = (review.get("state") or "").upper()
        if state == "APPROVED":
            bundle.approved = True
            continue
        if state in ("CHANGES_REQUESTED", "COMMENTED"):
            bundle.reviews.append(review)

    for comment in gh.review_comments(number):
        if not login_matches((comment.get("user") or {}).get("login", ""), logins):
            continue
        if inline_anchor_sha(comment, review_commits) != head_sha:
            continue
        bundle.inline.append(comment)

    # Отдельные комментарии ревьюера, не оформленные как review. Берём только
    # те, где явно назван наш SHA: иначе в подборку попадёт переписка людей.
    for comment in gh.issue_comments(number):
        if not login_matches((comment.get("user") or {}).get("login", ""), logins):
            continue
        body = comment.get("body") or ""
        if head_sha not in body and short not in body:
            continue
        bundle.notes.append(comment)

    return bundle


# --------------------------------------------------------------------------
# Рабочая копия диспетчера
# --------------------------------------------------------------------------

class Checkout:
    """Отдельный клон, которым владеет только диспетчер.

    Своя копия, а не рабочий каталог человека, по двум причинам. Первая: мы её
    чистим `git clean` и переключаем `checkout --force` — делать это с копией,
    где кто-то работает, недопустимо. Вторая: правки ревью не должны трогать
    файлы самого диспетчера, который в этот момент выполняется.

    Работаем в detached HEAD. Это осознанный выбор: локальной ветки нет, значит
    нет и соблазна её «подтянуть» через merge или rebase. Публикация —
    единственной командой `git push origin HEAD:refs/heads/<branch>` без force.
    """

    def __init__(self, tools: Tools, path: Path, repo: str, log: Log):
        self.git = tools.git
        self.gh = tools.gh
        self.path = path
        self.repo = repo
        self.log = log

    def git_run(self, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
        return run([self.git] + args, cwd=self.path, timeout=timeout)

    def ensure(self) -> None:
        if (self.path / ".git").exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log.info("рабочей копии нет, клонирую {} в {}".format(self.repo, self.path))
        # Клонируем через `gh`, а не через `git clone https://…`: `gh` сам
        # подставит авторизацию. Голый git на приватном репозитории спросил бы
        # пароль, а спрашивать под LaunchAgent некого — процесс бы просто повис.
        result = run([self.gh, "repo", "clone", self.repo, str(self.path)], timeout=900)
        if result.returncode != 0:
            raise RuntimeError("gh repo clone упал: " + redact(result.stderr.strip())[:600])

    def sync_to(self, branch: str, sha: str) -> None:
        """Привести копию ровно к нужному коммиту. Без merge и без rebase."""
        fetch = self.git_run(["fetch", "--prune", "--quiet", "origin",
                              "+refs/heads/{}:refs/remotes/origin/{}".format(branch, branch)], timeout=600)
        if fetch.returncode != 0:
            raise RuntimeError("git fetch упал: " + redact(fetch.stderr.strip())[:600])
        checkout = self.git_run(["checkout", "--force", "--detach", sha])
        if checkout.returncode != 0:
            raise RuntimeError("git checkout {} упал: {}".format(sha[:8], redact(checkout.stderr.strip())[:600]))
        # `-fd` без `-x`: игнорируемые файлы (кэш pip, тестовые базы) остаются,
        # чтобы каждый прогон не начинался с нуля.
        self.git_run(["clean", "-fd"])
        self.git_run(["reset", "--quiet", "HEAD", "--", "."])
        self.git_run(["checkout", "--", "."])

    def head_sha(self) -> str:
        result = self.git_run(["rev-parse", "HEAD"])
        return result.stdout.strip() if result.returncode == 0 else ""

    def changed_paths(self) -> list[str]:
        result = self.git_run(["status", "--porcelain", "--untracked-files=all"])
        paths = []
        for line in result.stdout.splitlines():
            if len(line) < 4:
                continue
            path = line[3:].strip()
            # Переименование: `old -> new`, нас интересует новый путь.
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            paths.append(path.strip('"'))
        return paths

    def revert_protected(self, paths: list[str]) -> list[str]:
        """Откатить правки в защищённых путях. Возвращает то, что откатили."""
        reverted = []
        for path in paths:
            if not path.startswith(PROTECTED_PATHS):
                continue
            reverted.append(path)
            self.git_run(["checkout", "--", path])
            target = self.path / path
            if target.exists() and path in self.changed_paths():
                try:
                    target.unlink()
                except OSError:
                    pass
        return reverted

    def discard_all(self) -> None:
        self.git_run(["checkout", "--", "."])
        self.git_run(["clean", "-fd"])

    def commit(self, message: str, author_name: str, author_email: str) -> str | None:
        add = self.git_run(["add", "--all"])
        if add.returncode != 0:
            raise RuntimeError("git add упал: " + redact(add.stderr.strip())[:400])
        staged = self.git_run(["diff", "--cached", "--name-only"])
        if not staged.stdout.strip():
            return None
        result = self.git_run([
            "-c", "user.name=" + author_name,
            "-c", "user.email=" + author_email,
            "commit", "--no-verify", "--message", message,
        ])
        if result.returncode != 0:
            raise RuntimeError("git commit упал: " + redact(result.stderr.strip())[:600])
        return self.head_sha()

    def push(self, branch: str) -> subprocess.CompletedProcess:
        """Публикация. Никакого force и никакого force-with-lease.

        Если ветка успела уехать вперёд, push отклонят — и это правильный
        исход: значит, кто-то ещё работал с веткой, и следующий цикл начнёт с
        актуального SHA, а не затрёт чужую работу.
        """
        return self.git_run(["push", "origin", "HEAD:refs/heads/" + branch], timeout=600)


# --------------------------------------------------------------------------
# Вызов Claude Code
# --------------------------------------------------------------------------

def build_prompt(pr: dict, bundle: ReviewBundle) -> str:
    """Запрос к Claude по УЖЕ нарезанной порции замечаний.

    Нарезкой занимается `ReviewBundle.portion` до вызова: то, что попало в
    запрос, и то, что диспетчер запишет как обработанное, обязано быть одним и
    тем же набором.
    """
    number = pr["number"]
    branch = pr["head"]["ref"]
    lines = [
        "Ты — единственный исполнитель кода в проекте «Оборот» (см. AGENTS.md).",
        "Нативный Codex Cloud Code Review оставил замечания к Pull Request #{}.".format(number),
        "",
        "Ветка: {}".format(branch),
        "Проверенный коммит (HEAD): {}".format(bundle.sha),
        "",
        "ЗАДАЧА: исправить в рабочей копии ровно те замечания, что перечислены ниже.",
        "",
        "Правила, нарушать которые нельзя:",
        "1. Никаких изменений сверх названных замечаний. Ни рефакторинга «по пути»,",
        "   ни переименований, ни правки форматирования в нетронутых файлах.",
        "2. Не трогать `.github/workflows/**` и `tools/agent-bridge/**`.",
        "3. Не запускать команды, не делать commit, push, merge, rebase, не менять",
        "   ветку. Тесты, коммит и публикацию делает вызвавший тебя диспетчер.",
        "4. Не трогать секреты, `.env`, боевые данные.",
        "5. Если замечание кажется неверным или требует продуктового решения —",
        "   не выдумывай: не меняй этот файл и напиши об этом в итоговом ответе",
        "   отдельной строкой «НЕ ИСПРАВЛЕНО: <замечание> — <причина>».",
        "6. Если замечание требует правки бизнес-логики или миграции — остановись",
        "   и опиши это в ответе вместо изменения.",
        "",
        "Стиль правок должен совпадать с окружающим кодом: проект пишет комментарии",
        "по-русски и объясняет причину решения, а не пересказывает код.",
        "",
        "=== ЗАМЕЧАНИЯ ===",
    ]

    for index, review in enumerate(bundle.reviews, 1):
        body = (review.get("body") or "").strip()
        if not body:
            continue
        lines += ["", "--- Итог ревью #{} (state={}) ---".format(index, review.get("state")), body]

    for index, note in enumerate(bundle.notes, 1):
        body = (note.get("body") or "").strip()
        if body:
            lines += ["", "--- Комментарий ревьюера #{} ---".format(index), body]

    for index, comment in enumerate(bundle.inline, 1):
        path = comment.get("path", "?")
        line_no = comment.get("line") or comment.get("original_line") or comment.get("position") or "?"
        hunk = (comment.get("diff_hunk") or "").strip()
        lines += ["", "--- Замечание #{}: {}:{} ---".format(index, path, line_no)]
        if hunk:
            lines += ["Контекст диффа:", "```diff", hunk[-1500:], "```"]
        lines += [(comment.get("body") or "").strip()]

    if bundle.dropped > 0:
        lines += ["", "(ещё {} inline-замечаний не поместились в этот запуск: "
                      "исправь перечисленные, остальные придут следующим циклом)".format(
                          bundle.dropped)]

    lines += [
        "",
        "=== КОНЕЦ ЗАМЕЧАНИЙ ===",
        "",
        "В конце ответа выведи короткий список: какие файлы изменены и что именно",
        "исправлено по каждому замечанию, а также строки «НЕ ИСПРАВЛЕНО: ...», если",
        "такие есть. Этот текст попадёт в комментарий к PR — пиши для человека.",
    ]
    return "\n".join(lines)


def invoke_claude(tools: Tools, cfg: Config, cwd: Path, prompt: str, log: Log) -> tuple[bool, str]:
    """Неинтерактивный запуск локального Claude Code.

    Инструментов даём ровно столько, сколько нужно для правки файлов. Bash не
    разрешён сознательно: диспетчер сам обязан прогнать тесты и сам сделать
    commit, иначе «зелёные тесты» и «то, что уехало в ветку» перестают быть
    одним и тем же. `--permission-mode acceptEdits` нужен потому, что в режиме
    `-p` спросить разрешение не у кого — процесс просто повис бы.
    """
    argv = [
        tools.claude,
        "-p", prompt,
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Read,Edit,Write,Glob,Grep",
    ]
    model = cfg.get("CLAUDE_MODEL")
    if model:
        argv += ["--model", model]
    extra = cfg.get("CLAUDE_EXTRA_ARGS").split()
    argv += extra

    timeout = cfg.get_int("CLAUDE_TIMEOUT", 1800)
    log.info("запускаю claude ({} симв. запроса, лимит {} с)".format(len(prompt), timeout))
    try:
        result = run(argv, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "claude не уложился в {} с и был снят".format(timeout)
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        return False, "claude вернул код {}: {}".format(
            result.returncode, redact((result.stderr or output).strip())[:1500])
    return True, redact(output)


# --------------------------------------------------------------------------
# Цикл
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, cfg: Config, tools: Tools, log: Log, state: State, dry_run: bool):
        self.cfg = cfg
        self.tools = tools
        self.log = log
        self.state = state
        self.dry_run = dry_run
        self.repo = cfg.get("REPO") or detect_repo(tools)
        if not self.repo:
            raise RuntimeError("не удалось определить репозиторий: задайте "
                               + ENV_PREFIX + "REPO вида owner/name")
        self.gh = GitHub(tools, self.repo, log)
        checkout_path = cfg.get("CHECKOUT")
        self.checkout = Checkout(
            tools,
            Path(checkout_path).expanduser() if checkout_path else state_dir(cfg) / "checkout",
            self.repo,
            log,
        )
        self.noop_pattern = re.compile(cfg.get("NOOP_REVIEW_PATTERN"), re.IGNORECASE)
        self.max_attempts = cfg.get_int("MAX_ATTEMPTS", 3)
        self.coordination_issue = cfg.get_int("COORDINATION_ISSUE", 0)
        # Маркеры, уже лежащие в issue-канале. Читаются один раз за прогон и
        # только если действительно есть что зеркалить: лишний запрос к API
        # каждую минуту ради «ничего не случилось» не нужен.
        self._mirror_seen: set | None = None

    # -- один цикл -------------------------------------------------------

    def cycle(self) -> int:
        started = datetime.now(timezone.utc).isoformat()
        self.state.data["last_cycle_started_at"] = started
        prs = self.gh.open_agent_prs(self.cfg.get("BRANCH_PREFIX"))
        self.log.info("открытых PR из веток «{}»: {}".format(self.cfg.get("BRANCH_PREFIX"), len(prs)))
        handled = 0
        for pr in prs:
            try:
                if self.handle_pr(pr):
                    handled += 1
            except Exception as exc:  # цикл не должен падать из-за одного PR
                self.log.error("PR #{}: {}".format(pr.get("number"), redact(str(exc))[:800]))
        self.state.prune({pr["number"] for pr in prs})
        self.state.data["cycles"] = int(self.state.data.get("cycles", 0)) + 1
        self.state.data["last_cycle_finished_at"] = datetime.now(timezone.utc).isoformat()
        self.state.data["last_cycle_ok"] = True
        self.state.save()
        return handled

    def handle_pr(self, pr: dict) -> bool:
        number = pr["number"]
        branch = pr["head"]["ref"]
        head_sha = pr["head"]["sha"]
        entry = self.state.head(number, head_sha)
        self.state.data["prs"][str(number)]["branch"] = branch

        bundle = collect_review(self.gh, number, head_sha, self.cfg.reviewer_logins())
        if bundle.is_empty():
            if bundle.approved:
                self.mark(entry, "APPROVED")
                self.log.info("PR #{} {}: ревью одобрено, делать нечего".format(number, head_sha[:8]))
            else:
                self.log.info("PR #{} {}: ревью для этого SHA пока нет".format(number, head_sha[:8]))
            return False

        if not bundle.is_actionable(self.noop_pattern):
            self.mark(entry, "CLEAN")
            self.log.info("PR #{} {}: ревью без замечаний".format(number, head_sha[:8]))
            return False

        # Что из этого ревью ещё не отдавали Claude. Учёт поштучный: замечание,
        # не поместившееся в прошлый прогон, обязано доехать следующим, а не
        # исчезнуть вместе с отпечатком всего ревью.
        pending = bundle.without(entry.get("processed_ids", []))
        if pending.is_empty():
            self.log.info("PR #{} {}: это ревью уже обработано".format(number, head_sha[:8]))
            return False

        if not pending.is_actionable(self.noop_pattern):
            # Всё, что требовало работы, уже сделано, а сверху добавилось пустое
            # ревью. Запоминаем его как обработанное, иначе диспетчер будет
            # заново приходить к этому выводу каждую минуту.
            done = entry.setdefault("processed_ids", [])
            done += [item for item in pending.ids if item not in done]
            self.state.save()
            self.log.info("PR #{} {}: новых замечаний нет".format(number, head_sha[:8]))
            return False

        portion = pending.portion(self.cfg.get_int("MAX_INLINE_COMMENTS", 60))
        fingerprint = portion.fingerprint
        if fingerprint in entry.get("processed", []):
            self.log.info("PR #{} {}: эта порция замечаний уже обработана".format(
                number, head_sha[:8]))
            return False

        if entry.get("attempts", 0) >= self.max_attempts:
            self.stop_for_human(number, head_sha, entry)
            return False

        self.log.info(
            "PR #{} {}: новое ревью — {} итогов, {} inline, {} комментариев; "
            "в этот прогон {} inline, отложено {}".format(
                number, head_sha[:8], len(bundle.reviews), len(bundle.inline),
                len(bundle.notes), len(portion.inline), portion.dropped))

        prompt = build_prompt(pr, portion)

        if self.dry_run:
            print("\n=== DRY-RUN: PR #{}, HEAD {} ===".format(number, head_sha))
            print("попытка {} из {}".format(entry.get("attempts", 0) + 1, self.max_attempts))
            print("запрос к claude ({} симв.):".format(len(prompt)))
            print("-" * 70)
            print(redact(prompt))
            print("-" * 70)
            print("дальше было бы: тесты «{}», commit, push в {}, комментарий в PR".format(
                self.cfg.get("TEST_CMD"), branch))
            return True

        # Попытку засчитываем ДО работы, отпечаток порции — тоже: иначе падение
        # посередине оставило бы счётчик нетронутым, и цикл повторялся бы
        # бесконечно. Если упало не по вине ревью (сеть, git), отпечаток
        # снимается ниже — ревью вернётся в очередь, но уже под потолком попыток.
        # Обработанными помечаются РОВНО те замечания, что ушли в запрос.
        portion_ids = portion.ids
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry.setdefault("processed", []).append(fingerprint)
        processed_ids = entry.setdefault("processed_ids", [])
        processed_ids += [item for item in portion_ids if item not in processed_ids]
        self.mark(entry, "WORKING")
        self.state.save()

        try:
            return self.run_fix(pr, portion, entry, prompt)
        except Exception as exc:
            if fingerprint in entry.get("processed", []):
                entry["processed"].remove(fingerprint)
            entry["processed_ids"] = [item for item in entry.get("processed_ids", [])
                                      if item not in set(portion_ids)]
            self.mark(entry, "ERROR")
            self.state.save()
            try:
                self.checkout.discard_all()
            except Exception:
                pass
            raise

    def run_fix(self, pr: dict, bundle: ReviewBundle, entry: dict, prompt: str) -> bool:
        number = pr["number"]
        branch = pr["head"]["ref"]
        head_sha = bundle.sha
        attempt = entry["attempts"]

        self.checkout.ensure()
        self.checkout.sync_to(branch, head_sha)
        actual = self.checkout.head_sha()
        if actual != head_sha:
            raise RuntimeError("рабочая копия на {}, ожидался {} — правки не делаю".format(
                actual[:8], head_sha[:8]))

        ok, output = invoke_claude(self.tools, self.cfg, self.checkout.path, prompt, self.log)
        if not ok:
            self.checkout.discard_all()
            self.mark(entry, "CLAUDE_FAILED")
            self.state.save()
            self.report(number, "CLAUDE_FAILED", head_sha, attempt,
                        "Локальный Claude Code не отработал.", output)
            return False

        changed = self.checkout.changed_paths()
        reverted = self.checkout.revert_protected(changed)
        if reverted:
            self.log.warn("откатил правки в защищённых путях: {}".format(", ".join(reverted)))
            changed = [path for path in self.checkout.changed_paths()
                       if not path.startswith(PROTECTED_PATHS)]

        if not changed:
            self.checkout.discard_all()
            self.mark(entry, "NO_CHANGES")
            self.state.save()
            self.report(number, "NO_CHANGES", head_sha, attempt,
                        "Claude не изменил ни одного файла — вероятно, замечание "
                        "требует решения человека.", output)
            return False

        self.log.info("изменено файлов: {}".format(len(changed)))

        tests_ok, tests_tail, tests_cmd = self.run_tests()
        if not tests_ok:
            self.checkout.discard_all()
            self.mark(entry, "TESTS_FAILED")
            self.state.save()
            self.report(number, "TESTS_FAILED", head_sha, attempt,
                        "Правки внесены, но тесты не прошли — в ветку ничего не отправлено.\n\n"
                        "Команда: `{}`\n\nИзменённые файлы:\n{}".format(
                            tests_cmd, format_paths(changed)),
                        tests_tail)
            return False

        message = commit_message(number, head_sha, bundle)
        new_sha = self.checkout.commit(message, "oborot-agent-bridge",
                                       "agent-bridge@users.noreply.github.com")
        if not new_sha:
            self.checkout.discard_all()
            self.mark(entry, "NO_CHANGES")
            self.state.save()
            return False

        push = self.checkout.push(branch)
        if push.returncode != 0:
            stderr = redact(push.stderr.strip())[:1200]
            self.checkout.discard_all()
            self.mark(entry, "PUSH_REJECTED")
            self.state.save()
            self.report(number, "PUSH_REJECTED", head_sha, attempt,
                        "Push отклонён. Force не делаю: ветка могла уехать вперёд, "
                        "и следующий цикл начнёт с её актуального SHA.", stderr)
            return False

        self.mark(entry, "PUSHED")
        entry["result_sha"] = new_sha
        self.state.save()
        self.log.info("PR #{}: опубликован {} в {}".format(number, new_sha[:8], branch))
        self.report(number, "PUSHED", head_sha, attempt,
                    "Замечания Codex к `{}` исправлены и отправлены в `{}`.\n\n"
                    "Новый HEAD: `{}`\n\nТесты: `{}` — пройдены.\n\n"
                    "Изменённые файлы:\n{}".format(
                        head_sha, branch, new_sha, tests_cmd, format_paths(changed)),
                    output)
        self.request_review(number, new_sha)
        return True

    def request_review(self, number: int, new_sha: str) -> None:
        """Разбудить ревьюера на новый SHA — один комментарий `@codex review`.

        Круг «Codex ревьюит → Claude чинит → Codex ревьюит снова» держался на
        предположении, что push сам запускает нативное ревью. 23.08.2026 это
        предположение не подтвердилось: коммит 1c7d616 уехал в ветку открытого
        PR #7, и нового ревью не появилось; ручной комментарий `@codex review`
        его запустил сразу. Пока причина молчания на стороне облака неизвестна,
        дешевле разбудить явно, чем ждать ревью, которого не будет: без него
        цикл встаёт, а сторож через 90 минут не скажет ничего — висящего ревью
        ведь тоже нет.

        Дублей боимся так же, как в зеркале исходов, и защищаемся так же — не
        памятью процесса, а маркером в самом PR. Диспетчер просыпается раз в
        минуту и переживает перезапуски; десять одинаковых «@codex review» под
        PR — это шум, из-за которого перестают читать и остальные комментарии.
        """
        command = self.cfg.get("REVIEW_REQUEST_COMMAND").strip()
        if self.dry_run or not command:
            return
        marker = "{} head={} -->".format(REVIEW_REQUEST_PREFIX, new_sha)
        try:
            if any(marker in (comment.get("body") or "")
                   for comment in self.gh.issue_comments(number)):
                self.log.info("PR #{}: ревью для {} уже запрошено".format(
                    number, new_sha[:8]))
                return
            self.gh.comment(number, "\n".join([marker, "", command]))
            self.log.info("PR #{}: запросил ревью для {}".format(number, new_sha[:8]))
        except Exception as exc:
            # Правка уже в ветке; не сумели позвать ревьюера — это повод для
            # строки в журнале, а не для падения цикла.
            self.log.error("не смог запросить ревью в PR #{}: {}".format(
                number, redact(str(exc))[:400]))

    def run_tests(self) -> tuple[bool, str, str]:
        command = self.cfg.get("TEST_CMD").strip()
        if not command:
            return True, "", "(тесты отключены настройкой)"
        argv = command.split()
        # Тестам нужен интерпретатор С УСТАНОВЛЕННЫМИ зависимостями проекта.
        # Системный python3 их не имеет, и набор упал бы на первом импорте —
        # причём выглядело бы это как «правка сломала тесты». Откуда берётся
        # путь — см. resolve_test_python: те же правила у health и install.sh.
        interpreter, source = resolve_test_python(self.cfg)
        if not interpreter:
            # До установки такое состояние ловит install.sh и не даёт поставить
            # автоматику. Сюда мы попадаем, если venv удалили уже после —
            # значит, ближайший красный прогон объясняется этой строкой.
            self.log.warn(
                "интерпретатор тестов не найден ({} нет): пойдёт системный python3, "
                "падение на импорте зависимостей вероятно".format(
                    venv_python(state_dir(self.cfg))))
            interpreter = self.tools.python
        if argv and argv[0] in ("python", "python3") and interpreter:
            argv[0] = str(Path(interpreter).expanduser())
        timeout = self.cfg.get_int("TEST_TIMEOUT", 2700)
        self.log.info("запускаю тесты: {} (лимит {} с, интерпретатор {}{})".format(
            command, timeout, interpreter or "не найден",
            ", " + source if source else ""))
        try:
            result = run(argv, cwd=self.checkout.path, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "тесты не уложились в {} с".format(timeout), command
        tail = redact(((result.stdout or "") + "\n" + (result.stderr or "")).strip())[-4000:]
        if result.returncode != 0:
            self.log.warn("тесты упали, код {}".format(result.returncode))
            return False, tail, command
        self.log.info("тесты прошли")
        return True, tail, command

    # -- служебное -------------------------------------------------------

    def mark(self, entry: dict, status: str) -> None:
        entry["status"] = status
        entry["status_at"] = datetime.now(timezone.utc).isoformat()

    def stop_for_human(self, number: int, head_sha: str, entry: dict) -> None:
        """Потолок в три попытки на один SHA.

        Не «на всякий случай»: если три захода не закрыли замечание, четвёртый
        его тоже не закроет, а вот кредиты подписки и историю PR он испортит.
        """
        self.mark(entry, "NEEDS_HUMAN")
        if entry.get("needs_human_notified"):
            return
        entry["needs_human_notified"] = True
        self.state.save()
        self.log.warn("PR #{} {}: три попытки исчерпаны, нужен человек".format(number, head_sha[:8]))
        if self.dry_run:
            return
        self.report(number, "NEEDS_HUMAN", head_sha, entry.get("attempts", self.max_attempts),
                    "Автоматика остановлена: {} попытки для этого SHA не закрыли замечания.\n\n"
                    "Дальше — решение человека. Диспетчер к этому SHA больше не вернётся; "
                    "новый коммит в ветке снимет ограничение.".format(self.max_attempts), "")

    def report(self, number: int, status: str, head_sha: str, attempt: int,
               summary: str, detail: str) -> None:
        """Отчёт в PR. GitHub — это и очередь, и журнал: локальный лог сотрут."""
        marker = "{} status={} head={} attempt={} -->".format(
            MARKER_PREFIX, status, head_sha, attempt)
        body = [marker, "", "**agent-bridge — {}**".format(status), "", summary]
        if detail.strip():
            body += ["", "<details><summary>Подробности</summary>", "",
                     "```", detail.strip()[-8000:], "```", "", "</details>"]
        try:
            self.gh.comment(number, "\n".join(body))
        except Exception as exc:
            self.log.error("не смог оставить комментарий в PR #{}: {}".format(
                number, redact(str(exc))[:400]))
        self.mirror(number, status, head_sha, attempt, summary)

    def mirror(self, number: int, status: str, head_sha: str, attempt: int,
               summary: str) -> None:
        """Копия ключевого исхода в канонический issue-канал координации.

        Зачем дублировать то, что уже написано в PR. Комментарий в PR читает
        тот, кто в этот PR смотрит; общий канал — это единственное место, где
        видно состояние всей связки сразу, и по правилам проекта (`AGENTS.md`)
        ключевые итоги обязаны быть там. Без зеркала канал тихо устаревает, и
        второй агент делает выводы по протухшей картине.

        Дублей боимся всерьёз: диспетчер просыпается раз в минуту, а исход
        одного и того же SHA может пересчитаться после перезапуска или потери
        состояния. Поэтому защита не в памяти процесса, а в самом issue —
        маркер с номером PR, статусом, SHA и попыткой. Есть маркер — молчим.

        Ошибка зеркалирования не должна ломать цикл: правка уже уехала в ветку,
        и ронять из-за комментария нечего.
        """
        if self.dry_run or status not in MIRRORED_STATUSES:
            return
        issue = self.coordination_issue
        if issue <= 0 or issue == number:
            return
        marker = "{} pr={} status={} head={} attempt={} -->".format(
            MIRROR_PREFIX, number, status, head_sha, attempt)
        try:
            if self._mirror_seen is None:
                self._mirror_seen = {
                    comment.get("body") or "" for comment in self.gh.issue_comments(issue)
                }
            if any(marker in body for body in self._mirror_seen):
                return
            first_line = (summary or "").strip().splitlines()[0] if summary.strip() else ""
            body = [
                marker,
                "",
                "**agent-bridge — {}** · PR #{} · `{}` · попытка {}".format(
                    status, number, head_sha[:8], attempt),
                "",
                first_line,
                "",
                "Подробности — в комментарии диспетчера в самом PR #{}.".format(number),
            ]
            text = "\n".join(body)
            self.gh.comment(issue, text)
            self._mirror_seen.add(text)
            self.log.info("исход {} для PR #{} продублирован в issue #{}".format(
                status, number, issue))
        except Exception as exc:
            self.log.error("не смог продублировать исход в issue #{}: {}".format(
                issue, redact(str(exc))[:400]))


def format_paths(paths: list[str]) -> str:
    return "\n".join("- `{}`".format(path) for path in sorted(paths)[:50])


def commit_message(number: int, head_sha: str, bundle: ReviewBundle) -> str:
    return "\n".join([
        "Правки по ревью Codex для PR #{}".format(number),
        "",
        "Замечания к коммиту {} (inline: {}, итогов ревью: {}).".format(
            head_sha, len(bundle.inline), len(bundle.reviews)),
        "Изменения внесены локальным Claude Code, тесты прогнаны диспетчером",
        "перед публикацией. Merge не делается: решение о слиянии — за владельцем.",
    ])


def detect_repo(tools: Tools) -> str:
    """Определить `owner/name`, не полагаясь на имя каталога.

    Сначала спрашиваем `gh` о репозитории текущего каталога, потом читаем
    `origin`. Захардкоженного значения нет специально: файл лежит в репозитории,
    который могут форкнуть или переименовать.
    """
    here = Path(__file__).resolve().parent
    if tools.gh:
        result = run([tools.gh, "repo", "view", "--json", "nameWithOwner",
                      "--jq", ".nameWithOwner"], cwd=here, timeout=60)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    if tools.git:
        result = run([tools.git, "remote", "get-url", "origin"], cwd=here, timeout=30)
        if result.returncode == 0:
            match = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?$", result.stdout.strip())
            if match:
                return match.group(1)
    return ""


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------

def cmd_poll(args, cfg: Config) -> int:
    root = state_dir(cfg)
    log = Log(root / "bridge.log", cfg.get_int("LOG_KEEP_BYTES", 5 * 1024 * 1024))
    tools = Tools(cfg)
    missing = tools.missing()
    if missing:
        log.error("не найдены исполняемые файлы: {}. Проверьте «bridge.py health».".format(
            ", ".join(missing)))
        return 1

    with Lock(root / "lock") as lock:
        if lock is None:
            log.info("прошлый цикл ещё идёт — этот пропускаю")
            return 0
        state = State(root / "state.json")
        try:
            bridge = Bridge(cfg, tools, log, state, dry_run=args.dry_run)
        except Exception as exc:
            log.error(redact(str(exc))[:800])
            return 1

        if args.once:
            return _one_cycle(bridge, log, state)

        interval = max(15, args.interval)
        log.info("вечный цикл, интервал {} с. Ctrl-C для остановки.".format(interval))
        while True:
            _one_cycle(bridge, log, state)
            time.sleep(interval)


def _one_cycle(bridge: Bridge, log: Log, state: State) -> int:
    try:
        bridge.cycle()
        return 0
    except Exception as exc:
        message = redact(str(exc))[:800]
        log.error("цикл упал: " + message)
        state.data["last_cycle_ok"] = False
        state.data["last_error"] = message
        state.data["last_cycle_finished_at"] = datetime.now(timezone.utc).isoformat()
        state.save()
        return 1


def cmd_status(args, cfg: Config) -> int:
    root = state_dir(cfg)
    log = Log(root / "bridge.log", cfg.get_int("LOG_KEEP_BYTES", 5 * 1024 * 1024), echo=False)
    tools = Tools(cfg)
    state = State(root / "state.json")

    print("agent-bridge — состояние")
    print("  каталог состояния : {}".format(root))
    print("  циклов выполнено  : {}".format(state.data.get("cycles", 0)))
    print("  последний цикл    : {} ({})".format(
        state.data.get("last_cycle_finished_at") or "никогда",
        "успех" if state.data.get("last_cycle_ok", True) else "ошибка"))
    if state.data.get("last_error"):
        print("  последняя ошибка  : {}".format(state.data["last_error"]))

    lock_path = root / "lock"
    if lock_path.exists():
        # Пробуем взять блокировку, не переписывая её содержимое: `status` не
        # должен выглядеть в файле блокировки как владелец прогона.
        with Lock(lock_path, write_owner=False) as lock:
            busy = lock is None
        print("  прогон сейчас     : {}".format("идёт" if busy else "нет"))

    if not state.data.get("prs"):
        print("\nЗапомненных PR нет.")
    else:
        print("\nЗапомненные PR:")
        for number, pr in sorted(state.data["prs"].items(), key=lambda kv: int(kv[0])):
            print("  PR #{} ({})".format(number, pr.get("branch", "?")))
            for sha, entry in sorted(pr.get("heads", {}).items(),
                                     key=lambda kv: kv[1].get("status_at") or ""):
                print("    {}  {:<14} попыток {}/{}  {}".format(
                    sha[:8], entry.get("status", "?"), entry.get("attempts", 0),
                    cfg.get_int("MAX_ATTEMPTS", 3), entry.get("status_at") or ""))

    if args.remote and not tools.missing():
        try:
            repo = cfg.get("REPO") or detect_repo(tools)
            gh = GitHub(tools, repo, log)
            prs = gh.open_agent_prs(cfg.get("BRANCH_PREFIX"))
            print("\nGitHub, {}: открытых PR из «{}» — {}".format(
                repo, cfg.get("BRANCH_PREFIX"), len(prs)))
            for pr in prs:
                bundle = collect_review(gh, pr["number"], pr["head"]["sha"], cfg.reviewer_logins())
                noop = re.compile(cfg.get("NOOP_REVIEW_PATTERN"), re.IGNORECASE)
                verdict = ("есть замечания" if bundle.is_actionable(noop)
                           else "одобрено" if bundle.approved
                           else "ревью для этого SHA нет")
                print("  PR #{} {} {} — {}".format(
                    pr["number"], pr["head"]["ref"], pr["head"]["sha"][:8], verdict))
        except Exception as exc:
            print("\nGitHub недоступен: {}".format(redact(str(exc))[:300]))
    return 0


def cmd_health(args, cfg: Config) -> int:
    root = state_dir(cfg)
    tools = Tools(cfg)
    problems: list[str] = []
    lines: list[str] = []

    def check(name: str, ok: bool, detail: str) -> None:
        lines.append("  [{}] {:<22} {}".format("ok" if ok else "!!", name, detail))
        if not ok:
            problems.append(name)

    print("agent-bridge — проверка окружения")

    for name, path in (("gh", tools.gh), ("git", tools.git),
                       ("claude", tools.claude), ("python3", tools.python)):
        check(name, bool(path), path or "не найден (задайте {}{}_BIN)".format(
            ENV_PREFIX, name.upper().replace("PYTHON3", "PYTHON")))

    if tools.gh:
        result = run([tools.gh, "auth", "status"], timeout=60)
        # `gh auth status` печатает и токен, и логин: в отчёт кладём только логин.
        who = run([tools.gh, "api", "user", "--jq", ".login"], timeout=60)
        check("gh авторизован", result.returncode == 0,
              who.stdout.strip() if who.returncode == 0 else "нет активной сессии")

    if tools.git:
        # Без credential helper push спросит пароль — а под LaunchAgent отвечать
        # некому: с GIT_TERMINAL_PROMPT=0 это не зависание, а падение каждого
        # цикла. Дешевле поймать здесь, чем по пустому журналу через неделю.
        helpers = run([tools.git, "config", "--get-regexp", r"credential\..*helper"], timeout=30)
        output = helpers.stdout or ""
        if "gh auth git-credential" in output:
            check("git умеет пушить", True, "credential helper от gh")
        elif output.strip():
            check("git умеет пушить", True,
                  "свой credential helper: " + redact(output.split("\n")[0])[:80])
        else:
            check("git умеет пушить", False, "helper'а нет — выполните: gh auth setup-git")
    if tools.claude:
        result = run([tools.claude, "--version"], timeout=60)
        check("claude отвечает", result.returncode == 0,
              redact(result.stdout.strip() or result.stderr.strip())[:120])

    repo = cfg.get("REPO") or (detect_repo(tools) if not tools.missing() else "")
    check("репозиторий", bool(repo), repo or "не определён, задайте " + ENV_PREFIX + "REPO")

    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("каталог состояния", True, str(root))
    except OSError as exc:
        check("каталог состояния", False, "{}: {}".format(root, exc))

    checkout = Path(cfg.get("CHECKOUT")).expanduser() if cfg.get("CHECKOUT") else root / "checkout"
    check("рабочая копия", True,
          "{} ({})".format(checkout, "готова" if (checkout / ".git").exists()
                           else "будет склонирована при первом цикле"))

    # Интерпретатор для тестов проверяем отдельно: если в нём нет зависимостей
    # проекта, набор упадёт на импорте, и это выглядит как «правка сломала
    # тесты» — самая дорогая в разборе ложная тревога из возможных. Поэтому
    # здесь не предупреждение, а полноценная проверка: пока тесты включены, а
    # интерпретатора нет, health красный и install.sh автоматику не поставит.
    test_python, source = resolve_test_python(cfg)
    if not cfg.get("TEST_CMD").strip():
        lines.append("  [--] {:<22} {}".format("тесты", "отключены настройкой TEST_CMD"))
    elif source == "config":
        resolved = Path(test_python)
        check("интерпретатор тестов",
              resolved.is_file() and os.access(resolved, os.X_OK),
              "{} (задан явно{})".format(
                  resolved,
                  "" if resolved.is_file() and os.access(resolved, os.X_OK)
                  else "; файла нет или он не исполняемый"))
    elif source == "venv":
        check("интерпретатор тестов", True, "{} (venv в каталоге состояния)".format(test_python))
    else:
        check("интерпретатор тестов", False,
              "нет {}; соберите: python3 -m venv {} && {} install -r requirements.lock".format(
                  venv_python(root), root / "venv", root / "venv" / "bin" / "pip"))

    plist = Path.home() / "Library" / "LaunchAgents" / "com.oborot.agent-bridge.plist"
    loaded = False
    if shutil.which("launchctl"):
        result = run(["launchctl", "list"], timeout=30)
        loaded = "com.oborot.agent-bridge" in (result.stdout or "")
    lines.append("  [{}] {:<22} {}".format(
        "ok" if plist.exists() and loaded else "--", "LaunchAgent",
        "загружен" if loaded else ("установлен, но не загружен" if plist.exists()
                                   else "не установлен (tools/agent-bridge/install.sh)")))

    state = State(root / "state.json")
    last = state.data.get("last_cycle_finished_at")
    if not last:
        lines.append("  [--] {:<22} {}".format("свежесть цикла", "циклов ещё не было"))
    else:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(last)
            detail = "последний {} назад".format(short_delta(age))
            # Давность цикла — проблема только если циклы кто-то обязан
            # запускать. Пока LaunchAgent не загружен, редкие ручные прогоны
            # нормальны, и заваливать из-за них установку неправильно.
            if loaded:
                check("свежесть цикла", age <= timedelta(minutes=15), detail)
            else:
                lines.append("  [--] {:<22} {}".format("свежесть цикла", detail))
        except ValueError:
            check("свежесть цикла", False, "непонятная отметка времени: " + last)

    lines.append("  [--] {:<22} {}".format("платные API", "не используются: подписки Codex и Claude Max"))

    print("\n".join(lines))
    if problems:
        print("\nПроблемы: {}".format(", ".join(problems)))
        return 1
    print("\nВсё на месте.")
    return 0


def short_delta(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "{} с".format(seconds)
    if seconds < 5400:
        return "{} мин".format(seconds // 60)
    return "{} ч".format(seconds // 3600)


def cmd_logs(args, cfg: Config) -> int:
    path = state_dir(cfg) / "bridge.log"
    if not path.is_file():
        print("Журнала ещё нет: {}".format(path))
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.number:]:
        print(line)
    return 0


def resolved_settings(cfg: Config) -> dict:
    """Значения, которые вычисляет диспетчер, — в машиночитаемом виде.

    Нужны установщику. Иначе install.sh пришлось бы повторять на bash логику
    путей (каталог состояния, файл настроек, поиск venv), а любая копия логики
    рано или поздно расходится с оригиналом: launchd получил бы один каталог
    состояния, health смотрел бы в другой, и разошлись бы они молча.
    """
    root = state_dir(cfg)
    test_python, source = resolve_test_python(cfg)
    return {
        "state-dir": str(root),
        "config-file": str(Config.config_paths()[0]),
        "checkout": str(Path(cfg.get("CHECKOUT")).expanduser() if cfg.get("CHECKOUT")
                        else root / "checkout"),
        "test-cmd": cfg.get("TEST_CMD").strip(),
        "test-python": test_python,
        "test-python-source": source,
        "venv-python": str(venv_python(root)),
    }


def cmd_resolve(args, cfg: Config) -> int:
    values = resolved_settings(cfg)
    if not args.key:
        width = max(len(key) for key in values)
        for key, value in values.items():
            print("{:<{}}  {}".format(key, width, value))
        return 0
    if args.key not in values:
        print("Неизвестный ключ: {}. Известные: {}".format(
            args.key, ", ".join(values)), file=sys.stderr)
        return 2
    # Ровно значение и перевод строки: вывод читает install.sh через $(...).
    print(values[args.key])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bridge.py",
        description="Локальный диспетчер: ревью нативного Codex → правки локального Claude Code.")
    parser.add_argument("--repo", default="", help="owner/name; по умолчанию определяется сам")
    parser.add_argument("--state-dir", default="", help="каталог долговременного состояния")
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="опросить GitHub и обработать новые ревью")
    poll.add_argument("--once", action="store_true", help="один цикл и выход (режим LaunchAgent)")
    poll.add_argument("--dry-run", action="store_true", help="ничего не менять, показать план")
    poll.add_argument("--interval", type=int, default=60, help="пауза между циклами, секунды")
    poll.set_defaults(func=cmd_poll)

    status = sub.add_parser("status", help="что диспетчер помнит")
    status.add_argument("--remote", action="store_true", help="дополнительно опросить GitHub")
    status.set_defaults(func=cmd_status)

    health = sub.add_parser("health", help="проверка окружения; код 1, если что-то не так")
    health.set_defaults(func=cmd_health)

    logs = sub.add_parser("logs", help="хвост журнала")
    logs.add_argument("-n", "--number", type=int, default=80)
    logs.set_defaults(func=cmd_logs)

    resolve = sub.add_parser(
        "resolve", help="во что разворачиваются пути и настройки (для install.sh)")
    resolve.add_argument("key", nargs="?", default="",
                         help="state-dir | config-file | checkout | test-cmd | "
                              "test-python | test-python-source | venv-python; "
                              "без ключа — всё сразу")
    resolve.set_defaults(func=cmd_resolve)

    args = parser.parse_args(argv)
    cfg = Config({"REPO": args.repo, "STATE_DIR": args.state_dir})
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        print("\nОстановлено с клавиатуры.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
