# -*- coding: utf-8 -*-
"""Диспетчер agent-bridge: проверяем ровно те места, где он молча теряет работу.

Зачем этот набор. `tools/agent-bridge/bridge.py` — не продуктовый код, и на
первый взгляд его можно оставить на вычитку глазами. Но у него есть свойство,
которое делает вычитку недостаточной: почти все его ошибки НЕ падают. Ревью,
которое не разобралось, выглядит как «ревью ещё нет». Замечание, помеченное
обработанным, но не отданное модели, выглядит как «всё исправлено». Исход,
не доехавший до issue-канала, выглядит как «ничего не происходило». Всё это
читается в логе как штатная тишина, и заметить её можно только специально.

Проверяется:
  1) вывод `gh api --paginate` — это НЕСКОЛЬКО значений JSON подряд, а не одно;
     склейка страниц и то, что список из двух страниц приходит одним списком;
  2) `GitHub.api` на таком выводе целиком, вместе с вызовом `gh`;
  3) порция замечаний: отпечаток берётся только с того, что ушло в запрос;
  4) хвост inline-замечаний, не поместившийся в прогон, приезжает следующим
     циклом и ровно один раз;
  5) зеркало ключевых исходов в issue-канал: дубль не появляется ни в одном
     процессе, ни после перезапуска (защита живёт в самом issue);
  6) команда тестов у диспетчера совпадает с командой CI;
  7) привязка inline-замечания к SHA: у GitHub поле `commit_id` замечания
     переезжает на новый коммит, и по нему старое ревью выглядит как новое;
  8) после успешного push диспетчер один раз просит ревью нового SHA;
  9) интерпретатор тестов: фон, health и установщик выбирают ОДИН И ТОТ ЖЕ,
     а установщик отказывается ставить автоматику, которой нечем гонять
     тесты (настоящий launchd при этом не трогается);
 10) настройки, проверенные при установке, доезжают до launchd: каталог
     настроек уходит в plist, а значения, живущие только в переменной
     окружения, установку обрывают — фон их всё равно не увидит;
 11) модель исполнителя: основная `opus` и резервная `fable` закреплены в
     настройках по умолчанию, обе доезжают до аргументов запуска, обе видны в
     `health` и `status`, и обе переопределяются владельцем;
 12) журнал владельца `OWNER_WORK_LOG.md`: шаблон из шести пунктов соблюдён, а
     SHA и протокольные слова не подменяют собой человеческий текст;
 13) «замечаний нет» распознаётся по всему телу ревью, а не по вхождению
     похвалы: тело с оговоркой («no blocking issues, but …») остаётся работой —
     одинаково у диспетчера и у сторожа, и это сверяется исполнением;
 14) цикл, в котором упал хотя бы один PR, не выдаёт себя за успешный: код
     возврата ненулевой, `status` называет номер PR и причину;
 15) `uninstall.sh --purge` удаляет только доказанный каталог диспетчера и
     отказывается от `/`, домашнего каталога, его предков и любых широких
     путей (в опасных случаях настоящее `rm` подменяется заглушкой).

Запуск из корня репозитория:  python tests/test_agent_bridge.py
"""
import contextlib
import importlib.util
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PY = ROOT / "tools" / "agent-bridge" / "bridge.py"
INSTALL_SH = ROOT / "tools" / "agent-bridge" / "install.sh"
UNINSTALL_SH = ROOT / "tools" / "agent-bridge" / "uninstall.sh"
RUN_SH = ROOT / "tools" / "agent-bridge" / "run.sh"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
WATCHDOG_YML = ROOT / ".github" / "workflows" / "agent-watchdog.yml"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def load_bridge():
    """Импорт по пути: каталог с дефисом в имени пакетом не является."""
    spec = importlib.util.spec_from_file_location("oborot_bridge", BRIDGE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load_bridge()


# --------------------------------------------------------------------------
# Заглушки
# --------------------------------------------------------------------------

class StubLog:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(("INFO", message))

    def warn(self, message):
        self.lines.append(("WARN", message))

    def error(self, message):
        self.lines.append(("ERROR", message))


class StubGitHub:
    """GitHub без сети: отдаёт заготовленное и запоминает написанное."""

    def __init__(self, reviews=None, inline=None, notes=None, issue_comments=None):
        self._reviews = reviews or []
        self._inline = inline or []
        self._notes = notes or []
        # Комментарии по номеру issue/PR: и заготовленные, и добавленные.
        self._comments = {number: list(items) for number, items
                          in (issue_comments or {}).items()}
        self.posted = []

    def reviews(self, number):
        return list(self._reviews)

    def review_comments(self, number):
        return list(self._inline)

    def issue_comments(self, number):
        if number in self._comments:
            return list(self._comments[number])
        return list(self._notes)

    def comment(self, number, body):
        self.posted.append((number, body))
        self._comments.setdefault(number, []).append(
            {"id": 10000 + len(self.posted), "body": body,
             "user": {"login": "oborot-agent-bridge"}})


class StubCheckout:
    """Рабочая копия без git: `run_fix` проверяется целиком, кроме самого git."""

    def __init__(self, head_sha, new_sha):
        self.path = Path(".")
        self._head = head_sha
        self._new = new_sha
        self.pushed = []

    def ensure(self):
        pass

    def sync_to(self, branch, sha):
        self._head = sha

    def head_sha(self):
        return self._head

    def changed_paths(self):
        return ["app/x.py"]

    def revert_protected(self, changed):
        return []

    def commit(self, message, name, email):
        return self._new

    def push(self, branch):
        self.pushed.append(branch)
        return subprocess.CompletedProcess(["git", "push"], 0, "", "")

    def discard_all(self):
        pass


def make_bridge(cfg_values, gh, state_path, dry_run=False):
    """Диспетчер без окружения: ни git, ни gh, ни claude тут не нужны."""
    # Config собирается вручную, БЕЗ чтения ~/.config и переменных окружения:
    # тест не должен зависеть от того, как настроен диспетчер на этой машине.
    cfg = bridge.Config.__new__(bridge.Config)
    cfg.values = dict(bridge.CONFIG_KEYS)
    cfg.values.update(cfg_values)
    instance = bridge.Bridge.__new__(bridge.Bridge)
    instance.cfg = cfg
    instance.tools = None
    instance.log = StubLog()
    instance.state = bridge.State(state_path)
    instance.dry_run = dry_run
    instance.repo = cfg.get("REPO")
    instance.gh = gh
    instance.checkout = None
    instance.noop_pattern = re.compile(cfg.get("NOOP_REVIEW_PATTERN"), re.IGNORECASE)
    instance.max_attempts = cfg.get_int("MAX_ATTEMPTS", 3)
    instance.coordination_issue = cfg.get_int("COORDINATION_ISSUE", 0)
    instance._mirror_seen = None
    return instance


def review(id_, sha, body="Найдено: тут ошибка, поправьте.", state="COMMENTED"):
    return {"id": id_, "commit_id": sha, "state": state, "body": body,
            "submitted_at": "2026-08-23T10:00:00Z",
            "user": {"login": "chatgpt-codex-connector[bot]"}}


def inline_comment(id_, sha, path="app/x.py", line=1, review_id=1, moved_to=None):
    """Замечание в той форме, в какой его отдаёт GitHub.

    `sha` — коммит, на котором замечание создано (`original_commit_id`).
    `moved_to` — коммит, на который GitHub переставил замечание после нового
    push: поле `commit_id` у inline-замечания не постоянное, и именно на этом
    диспетчер обжёгся (см. `test_inline_anchored_to_review_commit`).
    """
    return {"id": id_, "commit_id": moved_to or sha, "original_commit_id": sha,
            "pull_request_review_id": review_id, "path": path, "line": line,
            "original_line": line, "side": "RIGHT", "in_reply_to_id": None,
            "subject_type": "line",
            "body": f"замечание {id_}", "diff_hunk": "@@ -1 +1 @@",
            "user": {"login": "chatgpt-codex-connector[bot]"}}


# --------------------------------------------------------------------------
# 1. Разбор вывода `gh api --paginate`
# --------------------------------------------------------------------------

def test_parse_api_output():
    print("\n== Вывод gh api --paginate ==")
    p = bridge.parse_api_output

    check("пустой вывод — None", p("") is None and p("   \n") is None)
    check("одна страница-список возвращается как есть",
          p(json.dumps([{"id": 1}, {"id": 2}])) == [{"id": 1}, {"id": 2}])
    check("одна страница-объект возвращается как есть",
          p(json.dumps({"id": 1})) == {"id": 1})

    # Ровно то, что печатает gh: два массива подряд, без запятой между ними.
    # На таком тексте json.loads падает с «Extra data» — из-за этого PR, у
    # которого набралось больше ста комментариев, переставал обрабатываться.
    two_pages = json.dumps([{"id": n} for n in range(1, 101)]) + "\n" + \
        json.dumps([{"id": n} for n in range(101, 143)])
    try:
        json.loads(two_pages)
        naive_fails = False
    except ValueError:
        naive_fails = True
    check("одиночный json.loads на двух страницах падает (это и чинится)", naive_fails)

    merged = p(two_pages)
    check("две страницы склеены в один список",
          isinstance(merged, list) and len(merged) == 142, f"len={len(merged)}")
    check("порядок страниц сохранён",
          merged[0]["id"] == 1 and merged[99]["id"] == 100 and merged[-1]["id"] == 142)

    three = " ".join(json.dumps([{"id": n}]) for n in (1, 2, 3))
    check("страницы через пробел, а не перевод строки",
          p(three) == [{"id": 1}, {"id": 2}, {"id": 3}])

    pages = p(json.dumps({"a": 1}) + json.dumps({"b": 2}))
    check("разнородные страницы не склеиваются молча",
          pages == [{"a": 1}, {"b": 2}])


def test_github_api_paginated():
    print("\n== GitHub.api на многостраничном ответе ==")
    calls = []

    def fake_run(argv, cwd=None, timeout=300, stdin_text=None, env=None):
        calls.append(argv)
        out = json.dumps([{"id": n, "commit_id": "sha"} for n in range(1, 101)]) + "\n" + \
            json.dumps([{"id": n, "commit_id": "sha"} for n in range(101, 121)])
        return subprocess.CompletedProcess(argv, 0, out, "")

    original = bridge.run
    bridge.run = fake_run
    try:
        gh = bridge.GitHub.__new__(bridge.GitHub)
        gh.gh, gh.repo, gh.log = "/bin/true", "owner/name", StubLog()
        got = gh.review_comments(7)
    finally:
        bridge.run = original

    check("замечания со всех страниц дошли до вызывающего",
          isinstance(got, list) and len(got) == 120, f"len={len(got) if got else got}")
    check("страницы запрошены с --paginate", any("--paginate" in argv for argv in calls))


# --------------------------------------------------------------------------
# 2. Порция замечаний и её отпечаток
# --------------------------------------------------------------------------

def test_portion_fingerprint():
    print("\n== Порция: отпечаток только по тому, что ушло в запрос ==")
    sha = "a" * 40
    bundle = bridge.ReviewBundle(sha)
    bundle.reviews = [review(1, sha)]
    bundle.inline = [inline_comment(n, sha) for n in (10, 11, 12, 13)]

    part = bundle.portion(2)
    check("в порцию попали первые два inline", [c["id"] for c in part.inline] == [10, 11])
    check("остаток посчитан", part.dropped == 2, f"dropped={part.dropped}")
    check("отпечаток порции не равен отпечатку всего ревью",
          part.fingerprint != bundle.fingerprint)
    check("отпечаток порции не содержит отложенных замечаний",
          "c12" not in part.ids and "c13" not in part.ids, ",".join(part.ids))
    check("порция меньше лимита возвращается целиком", bundle.portion(99) is bundle)

    rest = bundle.without(part.ids)
    check("после порции остаются ровно отложенные замечания",
          [c["id"] for c in rest.inline] == [12, 13] and not rest.reviews)
    check("выбранное целиком опустошает подборку",
          bundle.without(bundle.ids).is_empty())

    prompt = bridge.build_prompt({"number": 7, "head": {"ref": "claude/x"}}, part)
    check("в запросе честно сказано про отложенные",
          "ещё 2 inline-замечаний не поместились" in prompt)
    check("отложенные замечания в запрос не попали",
          "замечание 12" not in prompt and "замечание 10" in prompt)


def test_dropped_comments_return_next_cycle():
    print("\n== Отложенные замечания приезжают следующим циклом ==")
    sha = "b" * 40
    pr = {"number": 7, "head": {"ref": "claude/agent-bridge", "sha": sha}}
    gh = StubGitHub(reviews=[review(1, sha)],
                    inline=[inline_comment(n, sha) for n in (10, 11, 12, 13, 14)])

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        seen = []

        def run_cycle():
            # Каждый цикл — свой экземпляр диспетчера и то же состояние на
            # диске: ровно так это выглядит под LaunchAgent, который запускает
            # процесс заново каждую минуту.
            instance = make_bridge({"REPO": "owner/name", "MAX_INLINE_COMMENTS": "2",
                                    "MAX_ATTEMPTS": "9", "COORDINATION_ISSUE": "0"},
                                   gh, state_path)
            delivered = []

            def fake_run_fix(pr_, bundle_, entry_, prompt_):
                delivered.extend(comment["id"] for comment in bundle_.inline)
                return True

            instance.run_fix = fake_run_fix
            handled = instance.handle_pr(pr)
            instance.state.save()
            seen.append(delivered)
            return handled

        first, second, third = run_cycle(), run_cycle(), run_cycle()
        fourth = run_cycle()

    check("первый цикл взял два замечания", seen[0] == [10, 11], str(seen[0]))
    check("второй цикл взял следующие два", seen[1] == [12, 13], str(seen[1]))
    check("третий цикл взял последнее", seen[2] == [14], str(seen[2]))
    check("четвёртый цикл ничего не нашёл", seen[3] == [] and fourth is False, str(seen[3]))
    check("замечания не повторились и не потерялись",
          sorted(seen[0] + seen[1] + seen[2]) == [10, 11, 12, 13, 14])
    check("первые три цикла отработали", first and second and third)


def test_failed_attempt_returns_portion():
    print("\n== Упавшая попытка возвращает порцию в очередь ==")
    sha = "c" * 40
    pr = {"number": 7, "head": {"ref": "claude/agent-bridge", "sha": sha}}
    gh = StubGitHub(reviews=[review(1, sha)],
                    inline=[inline_comment(n, sha) for n in (10, 11)])

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        instance = make_bridge({"REPO": "owner/name", "MAX_INLINE_COMMENTS": "1",
                                "COORDINATION_ISSUE": "0"}, gh, state_path)

        def boom(*args):
            raise RuntimeError("сеть отвалилась")

        instance.run_fix = boom
        try:
            instance.handle_pr(pr)
            raised = False
        except RuntimeError:
            raised = True
        entry = instance.state.head(7, sha)

    check("ошибка не проглочена", raised)
    check("попытка засчитана", entry["attempts"] == 1, str(entry["attempts"]))
    check("порция снята с обработанных, замечание вернётся",
          entry["processed"] == [] and entry["processed_ids"] == [],
          str(entry.get("processed_ids")))


# --------------------------------------------------------------------------
# 3. Зеркало исходов в issue-канал
# --------------------------------------------------------------------------

def test_mirror_to_coordination_issue():
    print("\n== Зеркало ключевых исходов в issue-канал ==")
    sha = "d" * 40
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        gh = StubGitHub(issue_comments={2: []})
        cfg = {"REPO": "owner/name", "COORDINATION_ISSUE": "2"}

        instance = make_bridge(cfg, gh, state_path)
        instance.report(7, "PUSHED", sha, 1, "Замечания исправлены.", "детали")
        instance.report(7, "TESTS_FAILED", sha, 2, "Тесты не прошли.", "хвост")
        # Тот же исход второй раз: так выглядит перезапуск цикла.
        instance.report(7, "PUSHED", sha, 1, "Замечания исправлены.", "детали")

        # Новый процесс: локальная память пуста, защищать от дубля обязан
        # маркер, уже лежащий в самом issue.
        restarted = make_bridge(cfg, gh, state_path)
        restarted.report(7, "PUSHED", sha, 1, "Замечания исправлены.", "детали")

        # Промежуточное состояние в общий канал не идёт.
        restarted.report(7, "CLEAN", sha, 1, "Ревью без замечаний.", "")

        mirrored = [body for number, body in gh.posted if number == 2]
        to_pr = [body for number, body in gh.posted if number == 7]

    check("исходы продублированы в issue #2", len(mirrored) == 2, str(len(mirrored)))
    check("зеркалированы именно ключевые исходы",
          any("PUSHED" in body for body in mirrored)
          and any("TESTS_FAILED" in body for body in mirrored))
    check("повтор в том же процессе не задваивает",
          sum("PUSHED" in body for body in mirrored) == 1)
    check("повтор после перезапуска не задваивает",
          len(mirrored) == 2, str(len(mirrored)))
    check("промежуточное состояние в канал не идёт",
          not any("CLEAN" in body for body in mirrored))
    check("в зеркале есть маркер с PR, SHA и попыткой",
          all(bridge.MIRROR_PREFIX in body and f"head={sha}" in body
              and "pr=7" in body for body in mirrored))
    check("отчёт в самом PR никуда не делся", len(to_pr) == 5, str(len(to_pr)))


def test_mirror_can_be_disabled():
    print("\n== Зеркало отключается и не бьёт само себя ==")
    sha = "e" * 40
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"

        off = StubGitHub(issue_comments={2: []})
        make_bridge({"REPO": "owner/name", "COORDINATION_ISSUE": "0"}, off,
                    state_path).report(7, "PUSHED", sha, 1, "итог", "")
        check("COORDINATION_ISSUE=0 отключает зеркало",
              [n for n, _ in off.posted] == [7], str(off.posted))

        same = StubGitHub(issue_comments={2: []})
        make_bridge({"REPO": "owner/name", "COORDINATION_ISSUE": "2"}, same,
                    state_path).report(2, "PUSHED", sha, 1, "итог", "")
        check("отчёт по самому issue-каналу не дублируется в него же",
              [n for n, _ in same.posted] == [2], str(same.posted))

        broken = StubGitHub(issue_comments={2: []})

        def fail(number, body):
            raise RuntimeError("GitHub недоступен")

        broken.comment = fail
        instance = make_bridge({"REPO": "owner/name", "COORDINATION_ISSUE": "2"},
                               broken, state_path)
        try:
            instance.report(7, "PUSHED", sha, 1, "итог", "")
            survived = True
        except Exception:
            survived = False
        check("недоступный GitHub не роняет цикл: правка уже в ветке", survived)


# --------------------------------------------------------------------------
# 4. Привязка inline-замечания к SHA ревью, а не к переехавшему commit_id
# --------------------------------------------------------------------------

# Настоящий случай из PR #7, подтверждённый GitHub API 23.08.2026. Замечания
# оставлены к ревью коммита 193602bf; после push коммита 1c7d616 GitHub
# переписал им `commit_id` на новый коммит (строки в файлах не изменились),
# а `original_commit_id` и `pull_request_review_id` остались прежними. У
# самого ревью `commit_id` не переехал.
OLD_SHA = "193602bf72b0c3de1acdfa7a5f16ad6a0a7ead09"
NEW_SHA = "1c7d616cf331cc717aa5e9517384efb16bbafd07"
OLD_REVIEW_ID = 5003117263


def test_inline_anchored_to_review_commit():
    print("\n== Привязка inline-замечаний: SHA ревью, а не переехавший commit_id ==")
    logins = ["chatgpt-codex-connector[bot]"]

    # Ровно та форма, которую вернул `gh api .../pulls/comments/3839314973`.
    moved = {
        "id": 3839314973,
        "commit_id": NEW_SHA,
        "original_commit_id": OLD_SHA,
        "pull_request_review_id": OLD_REVIEW_ID,
        "path": ".github/workflows/agent-watchdog.yml",
        "line": 40, "original_line": 37, "start_line": 34,
        "side": "RIGHT", "subject_type": "line", "in_reply_to_id": None,
        "body": "замечание к старому коммиту",
        "diff_hunk": "@@ -34,4 +34,7 @@",
        "user": {"login": "chatgpt-codex-connector[bot]"},
    }
    old_review = {"id": OLD_REVIEW_ID, "commit_id": OLD_SHA, "state": "COMMENTED",
                  "body": "Нашёл несколько мест.",
                  "submitted_at": "2026-08-23T18:53:40Z",
                  "user": {"login": "chatgpt-codex-connector[bot]"}}

    gh = StubGitHub(reviews=[old_review],
                    inline=[moved,
                            inline_comment(3839314974, OLD_SHA, review_id=OLD_REVIEW_ID,
                                           moved_to=NEW_SHA)])

    for_new = bridge.collect_review(gh, 7, NEW_SHA, logins)
    check("переехавшие замечания не выдаются за ревью нового HEAD",
          for_new.is_empty(), f"inline={len(for_new.inline)}")

    for_old = bridge.collect_review(gh, 7, OLD_SHA, logins)
    check("к своему SHA те же замечания по-прежнему собираются",
          len(for_old.inline) == 2 and len(for_old.reviews) == 1,
          f"inline={len(for_old.inline)}, reviews={len(for_old.reviews)}")

    # Настоящее новое ревью к новому HEAD собраться обязано: чинить перестало
    # бы не хуже, чем чинить лишнее.
    new_review = {"id": 5003200000, "commit_id": NEW_SHA, "state": "COMMENTED",
                  "body": "Ещё одно замечание.",
                  "submitted_at": "2026-08-23T20:10:00Z",
                  "user": {"login": "chatgpt-codex-connector[bot]"}}
    fresh = inline_comment(3839400000, NEW_SHA, review_id=5003200000)
    gh2 = StubGitHub(reviews=[old_review, new_review], inline=[moved, fresh])
    bundle = bridge.collect_review(gh2, 7, NEW_SHA, logins)
    check("настоящее ревью нового HEAD собирается целиком",
          [c["id"] for c in bundle.inline] == [3839400000]
          and [r["id"] for r in bundle.reviews] == [5003200000],
          f"inline={[c['id'] for c in bundle.inline]}")

    # Одиночный комментарий вне ревью: `pull_request_review_id` пустой, решает
    # `original_commit_id`.
    standalone = dict(moved, id=3839500000, pull_request_review_id=None)
    gh3 = StubGitHub(reviews=[], inline=[standalone])
    check("одиночное замечание вне ревью привязано по original_commit_id",
          bridge.collect_review(gh3, 7, OLD_SHA, logins).inline
          and not bridge.collect_review(gh3, 7, NEW_SHA, logins).inline)

    # Payload без обоих устойчивых полей: остаётся `commit_id`, иначе замечание
    # потерялось бы совсем.
    legacy = {"id": 42, "commit_id": NEW_SHA, "path": "app/x.py", "line": 1,
              "body": "старая форма", "diff_hunk": "@@ -1 +1 @@",
              "user": {"login": "chatgpt-codex-connector[bot]"}}
    gh4 = StubGitHub(reviews=[], inline=[legacy])
    check("payload без original_commit_id не теряется",
          [c["id"] for c in bridge.collect_review(gh4, 7, NEW_SHA, logins).inline] == [42])


def test_old_review_does_not_wake_claude():
    print("\n== Старое ревью не будит Claude на новом HEAD ==")
    pr = {"number": 7, "head": {"ref": "claude/agent-bridge", "sha": NEW_SHA}}
    old_review = {"id": OLD_REVIEW_ID, "commit_id": OLD_SHA, "state": "COMMENTED",
                  "body": "Нашёл несколько мест.",
                  "submitted_at": "2026-08-23T18:53:40Z",
                  "user": {"login": "chatgpt-codex-connector[bot]"}}
    gh = StubGitHub(reviews=[old_review],
                    inline=[inline_comment(n, OLD_SHA, review_id=OLD_REVIEW_ID,
                                           moved_to=NEW_SHA)
                            for n in (10, 11, 12, 13)])

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        instance = make_bridge({"REPO": "owner/name", "COORDINATION_ISSUE": "0"},
                               gh, state_path)
        called = []
        instance.run_fix = lambda *args: called.append(args) or True
        handled = instance.handle_pr(pr)
        entry = instance.state.head(7, NEW_SHA)

    check("диспетчер не берётся за работу", handled is False and not called)
    check("попытка не потрачена", entry.get("attempts", 0) == 0, str(entry))
    check("в PR ничего не написано", gh.posted == [], str(gh.posted))


# --------------------------------------------------------------------------
# 5. Просьба поревьюить новый SHA
# --------------------------------------------------------------------------

def test_review_requested_after_push():
    print("\n== После push диспетчер один раз будит ревьюера ==")
    old_sha = "f" * 40
    new_sha = "9" * 40
    pr = {"number": 7, "head": {"ref": "claude/agent-bridge", "sha": old_sha}}
    gh = StubGitHub(issue_comments={7: []})

    bundle = bridge.ReviewBundle(old_sha)
    bundle.reviews = [review(1, old_sha)]
    bundle.inline = [inline_comment(10, old_sha)]

    original = bridge.invoke_claude
    bridge.invoke_claude = lambda tools, cfg, path, prompt, log: (True, "готово")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"

            def run_once():
                # Отдельный экземпляр на каждый прогон: так это и выглядит под
                # LaunchAgent, и так проверяется защита от дублей после
                # перезапуска.
                instance = make_bridge({"REPO": "owner/name", "COORDINATION_ISSUE": "0"},
                                       gh, state_path)
                instance.checkout = StubCheckout(old_sha, new_sha)
                instance.run_tests = lambda: (True, "", "python3 tests/run_all.py --jobs 3")
                entry = instance.state.head(7, old_sha)
                entry["attempts"] = 1
                return instance.run_fix(pr, bundle, entry, "запрос")

            first, second = run_once(), run_once()
    finally:
        bridge.invoke_claude = original

    bodies = [body for number, body in gh.posted if number == 7]
    requests = [body for body in bodies if bridge.REVIEW_REQUEST_PREFIX in body]

    check("push прошёл оба раза", first and second)
    check("ревью нового SHA запрошено ровно один раз", len(requests) == 1,
          str(len(requests)))
    check("в просьбе есть команда пробуждения",
          requests and "@codex review" in requests[0], requests[0] if requests else "")
    check("маркер просьбы называет новый SHA",
          requests and f"head={new_sha}" in requests[0])
    check("сначала отчёт, потом просьба",
          any(bridge.MARKER_PREFIX in body and "PUSHED" in body for body in bodies)
          and bodies.index(requests[0]) > 0)


def test_review_request_guards():
    print("\n== Просьба о ревью: отключение, сухой прогон, отказ GitHub ==")
    new_sha = "8" * 40

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"

        off = StubGitHub(issue_comments={7: []})
        make_bridge({"REPO": "owner/name", "REVIEW_REQUEST_COMMAND": ""},
                    off, state_path).request_review(7, new_sha)
        check("пустая REVIEW_REQUEST_COMMAND отключает просьбу",
              off.posted == [], str(off.posted))

        dry = StubGitHub(issue_comments={7: []})
        make_bridge({"REPO": "owner/name"}, dry, state_path,
                    dry_run=True).request_review(7, new_sha)
        check("сухой прогон в PR не пишет", dry.posted == [], str(dry.posted))

        broken = StubGitHub(issue_comments={7: []})

        def fail(number, body):
            raise RuntimeError("GitHub недоступен")

        broken.comment = fail
        instance = make_bridge({"REPO": "owner/name"}, broken, state_path)
        try:
            instance.request_review(7, new_sha)
            survived = True
        except Exception:
            survived = False
        check("недоступный GitHub не роняет цикл: правка уже в ветке", survived)


# --------------------------------------------------------------------------
# 6. Команда тестов совпадает с CI
# --------------------------------------------------------------------------

def test_test_cmd_matches_ci():
    print("\n== Команда тестов диспетчера и CI ==")
    ci = CI_YML.read_text(encoding="utf-8")
    match = re.search(r"run:\s*(python3?\s+tests/run_all\.py[^\n]*)", ci)
    check("в CI нашлась команда прогона тестов", bool(match))
    if not match:
        return
    ci_cmd = match.group(1).strip().replace("python ", "python3 ", 1)
    check("диспетчер гоняет тесты той же командой, что CI",
          bridge.CONFIG_KEYS["TEST_CMD"] == ci_cmd,
          f"bridge={bridge.CONFIG_KEYS['TEST_CMD']!r}, ci={ci_cmd!r}")


def test_watchdog_permissions():
    print("\n== Права сторожа ==")
    text = WATCHDOG_YML.read_text(encoding="utf-8")
    # Блок permissions обнуляет всё неперечисленное: без явного pull-requests
    # сторож падает на 403 раньше, чем успевает что-то проверить.
    check("сторожу разрешено писать комментарии", "issues: write" in text)
    check("сторожу разрешено читать PR и ревью", "pull-requests: read" in text)
    check("сторож смотрит и на inline-замечания", "listReviewComments" in text)
    check("сторож знает про пустое ревью", "NOOP_REVIEW_PATTERN" in text)
    # Та же привязка, что у диспетчера. Сторож, сравнивающий `commit_id`
    # замечания с HEAD, объявил бы тревогу по SHA, к которому ревью не было:
    # GitHub переставляет замечания на новый коммит сам.
    check("сторож привязывает замечания через ревью и original_commit_id",
          "pull_request_review_id" in text and "original_commit_id" in text)
    check("сторож не фильтрует замечания по переехавшему commit_id",
          "comment.commit_id === headSha" not in text)


# --------------------------------------------------------------------------
# 9. Интерпретатор тестов: один и тот же у фона, health и установщика
# --------------------------------------------------------------------------
#
# Разбираемый дефект. Тесты диспетчер гоняет тем интерпретатором, который
# укажут в OBOROT_BRIDGE_TEST_PYTHON. Установщик копировал config.env, где эта
# строка закомментирована, в plist переменную не клал, а health о пустом
# значении лишь предупреждал. В итоге фон брал системный python3 без
# зависимостей проекта: набор падал на первом импорте, диспетчер отчитывался
# TESTS_FAILED по КАЖДОЙ правке, и автоматика, выглядя живой, не пропускала
# ничего. Ниже проверяется ровно то, чем это закрыто: путь считает одна
# функция от каталога состояния, а установщик не ставит фон, которому нечем
# запускать тесты. Настоящий launchd при этом не трогается.


def bare_config(**values):
    """Config без чтения ~/.config и переменных окружения."""
    cfg = bridge.Config.__new__(bridge.Config)
    cfg.values = dict(bridge.CONFIG_KEYS)
    cfg.values.update(values)
    return cfg


def make_venv(root: Path) -> Path:
    """Заглушка venv: важно только то, что файл на месте и исполняемый."""
    path = bridge.venv_python(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexec python3 \"$@\"\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_test_python_resolution():
    print("\n== Откуда берётся интерпретатор тестов ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        root.mkdir()

        check("без venv и без настройки — ничего",
              bridge.resolve_test_python(bare_config(STATE_DIR=str(root))) == ("", ""),
              str(bridge.resolve_test_python(bare_config(STATE_DIR=str(root)))))

        # Файл есть, но не исполняемый — это не интерпретатор. Отдать его
        # значило бы поменять падение на импорте на падение на exec.
        blind = bridge.venv_python(root)
        blind.parent.mkdir(parents=True, exist_ok=True)
        blind.write_text("", encoding="utf-8")
        blind.chmod(0o644)
        check("неисполняемый venv не считается",
              bridge.resolve_test_python(bare_config(STATE_DIR=str(root))) == ("", ""))

        venv = make_venv(root)
        found, source = bridge.resolve_test_python(bare_config(STATE_DIR=str(root)))
        check("venv в каталоге состояния находится сам",
              (found, source) == (str(venv), "venv"), f"{found!r}, {source!r}")

        # Явная настройка важнее автопоиска: подменить указанный владельцем
        # интерпретатор на найденный самим — значит гонять тесты не тем.
        other = make_executable(Path(tmp) / "own" / "python")
        found, source = bridge.resolve_test_python(
            bare_config(STATE_DIR=str(root), TEST_PYTHON=str(other)))
        check("явно заданный путь важнее найденного venv",
              (found, source) == (str(other), "config"), f"{found!r}, {source!r}")

        # Несуществующий явный путь возвращается как есть: ругаться на него —
        # дело health и install.sh, у них для этого есть слова.
        found, source = bridge.resolve_test_python(
            bare_config(STATE_DIR=str(root), TEST_PYTHON="/nope/python"))
        check("несуществующий явный путь не подменяется на venv",
              (found, source) == ("/nope/python", "config"), f"{found!r}, {source!r}")

        found, _ = bridge.resolve_test_python(
            bare_config(STATE_DIR=str(root), TEST_PYTHON="~/python"))
        check("тильда в явном пути разворачивается",
              found == str(Path.home() / "python"), found)


def test_run_tests_uses_resolved_interpreter():
    print("\n== Чем прогон тестов запускается на самом деле ==")

    class StubTools:
        python = "/usr/bin/python3"

    class StubPath:
        def __init__(self, path):
            self.path = path

    def run_with(cfg_values, tmp):
        instance = make_bridge(cfg_values, StubGitHub(), Path(tmp) / "state.json")
        instance.tools = StubTools()
        instance.checkout = StubPath(Path(tmp))
        seen = {}

        def fake_run(argv, cwd=None, timeout=None, **kwargs):
            seen["argv"] = list(argv)
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        original, bridge.run = bridge.run, fake_run
        try:
            instance.run_tests()
        finally:
            bridge.run = original
        return seen.get("argv", []), instance.log.lines

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        venv = make_venv(root)
        argv, _ = run_with({"STATE_DIR": str(root)}, tmp)
        check("тесты идут интерпретатором venv, а не системным",
              argv[:1] == [str(venv)], str(argv))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        root.mkdir()
        argv, log_lines = run_with({"STATE_DIR": str(root)}, tmp)
        # venv удалили уже после установки: запрет остаётся у install.sh, а
        # прогон обязан хотя бы объяснить в журнале, почему всё покраснело.
        check("без venv остаётся системный python3", argv[:1] == ["/usr/bin/python3"], str(argv))
        check("отсутствие venv пишется в журнал предупреждением",
              any(level == "WARN" and "интерпретатор тестов не найден" in message
                  for level, message in log_lines), str(log_lines))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        make_venv(root)
        own = make_executable(Path(tmp) / "own" / "python")
        argv, _ = run_with({"STATE_DIR": str(root), "TEST_PYTHON": str(own)}, tmp)
        check("явная настройка побеждает и в прогоне", argv[:1] == [str(own)], str(argv))


def test_health_requires_interpreter():
    print("\n== health про интерпретатор тестов ==")

    class StubTools:
        gh = git = claude = python = ""

        def __init__(self, cfg=None):
            pass

        def missing(self):
            return ["gh", "git", "claude", "python3"]

    def health_output(cfg):
        """cmd_health без внешних вызовов: ни gh, ни launchctl, ни сети."""
        out = io.StringIO()
        tools_original, bridge.Tools = bridge.Tools, StubTools
        run_original = bridge.run
        bridge.run = lambda *a, **k: subprocess.CompletedProcess(["stub"], 1, "", "")
        try:
            with contextlib.redirect_stdout(out):
                code = bridge.cmd_health(None, cfg)
        finally:
            bridge.Tools, bridge.run = tools_original, run_original
        return code, out.getvalue()

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        root.mkdir()

        code, text = health_output(bare_config(STATE_DIR=str(root)))
        line = next((l for l in text.splitlines() if "интерпретатор тестов" in l), "")
        check("без venv health не предупреждает, а заваливается",
              line.startswith("  [!!]"), line)
        check("health называет команду сборки venv", "python3 -m venv" in text)
        check("интерпретатор попал в список проблем",
              "интерпретатор тестов" in text.split("Проблемы:")[-1] and code == 1)

        venv = make_venv(root)
        _, text = health_output(bare_config(STATE_DIR=str(root)))
        line = next((l for l in text.splitlines() if "интерпретатор тестов" in l), "")
        check("с venv health его показывает и принимает",
              line.startswith("  [ok]") and str(venv) in line, line)

        _, text = health_output(
            bare_config(STATE_DIR=str(root), TEST_PYTHON="/nope/python"))
        line = next((l for l in text.splitlines() if "интерпретатор тестов" in l), "")
        check("сломанный явный путь health заваливает",
              line.startswith("  [!!]") and "/nope/python" in line, line)

        # Выключенные тесты — осознанное решение владельца, а не поломка.
        _, text = health_output(bare_config(STATE_DIR=str(root), TEST_CMD=""))
        check("при выключенных тестах интерпретатор не требуется",
              "отключены настройкой TEST_CMD" in text
              and "интерпретатор тестов" not in text)


# --------------------------------------------------------------------------
# 11. Модель исполнителя: закреплена, с резервом, видна в health и status
# --------------------------------------------------------------------------
#
# Разбираемый дефект. `CLAUDE_MODEL` по умолчанию была пустой, то есть фон
# правил ревью «чем сейчас настроен Claude Code». Модель, переключённую в
# интерактивной сессии ради другой задачи, фон унаследовал бы молча: ни в
# журнале, ни в `status` она не печаталась, и отличить «правку сделал opus» от
# «правку сделал кто угодно» было нечем. Резерва не было вовсе — перегрузка
# основной модели приходит как обычная ошибка запуска и стоила бы попытки из
# трёх с исходом `CLAUDE_FAILED`, хотя чинить нечего.
#
# Проверяется: значения по умолчанию (opus + fable), то, что обе доезжают до
# аргументов запуска, что переопределение владельца работает в обе стороны
# (своя модель и пусто — без флага), и что обе модели видны в `health` и
# `status`. Плюс сверка с config.env.example: файл настроек, обещающий не то,
# что делает код, хуже отсутствующего.


def claude_argv(cfg):
    """Аргументы, с которыми диспетчер зовёт локальный claude. Без запуска."""

    class StubTools:
        claude = "/stub/claude"

    seen = {}

    def fake_run(argv, cwd=None, timeout=None, **kwargs):
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, "готово", "")

    original, bridge.run = bridge.run, fake_run
    try:
        bridge.invoke_claude(StubTools(), cfg, Path("."), "запрос", StubLog())
    finally:
        bridge.run = original
    return seen.get("argv", [])


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_model_defaults_and_argv():
    print("\n== Модель исполнителя: основная и резервная ==")
    check("по умолчанию основная модель — opus",
          bridge.CONFIG_KEYS["CLAUDE_MODEL"] == "opus",
          repr(bridge.CONFIG_KEYS["CLAUDE_MODEL"]))
    check("по умолчанию резервная модель — fable",
          bridge.CONFIG_KEYS["CLAUDE_FALLBACK_MODEL"] == "fable",
          repr(bridge.CONFIG_KEYS["CLAUDE_FALLBACK_MODEL"]))
    # Алиас, а не полное имя: `claude-opus-5` устареет молча, а alias всегда
    # указывает на свежую версию.
    check("модели записаны алиасами, а не полными именами",
          not bridge.CONFIG_KEYS["CLAUDE_MODEL"].startswith("claude-")
          and not bridge.CONFIG_KEYS["CLAUDE_FALLBACK_MODEL"].startswith("claude-"))

    argv = claude_argv(bare_config())
    check("в запуск уходит основная модель", flag_value(argv, "--model") == "opus", str(argv))
    check("в запуск уходит резервная модель",
          flag_value(argv, "--fallback-model") == "fable", str(argv))
    # `--fallback-model` действует только в неинтерактивном режиме: без `-p`
    # флаг молча ничего не значит, и резерва фактически нет.
    check("резерв передаётся вместе с неинтерактивным режимом", "-p" in argv, str(argv))
    check("ключей API в запуске нет",
          not any(part.startswith("sk-") or "API_KEY" in part for part in argv), str(argv))

    own = claude_argv(bare_config(CLAUDE_MODEL="sonnet", CLAUDE_FALLBACK_MODEL="haiku"))
    check("владелец может переопределить обе модели",
          flag_value(own, "--model") == "sonnet"
          and flag_value(own, "--fallback-model") == "haiku", str(own))

    # Пусто — это осознанный выбор «как настроено в самом Claude Code», а не
    # повод подставить умолчание: подстановка отняла бы у владельца этот выбор.
    empty = claude_argv(bare_config(CLAUDE_MODEL="", CLAUDE_FALLBACK_MODEL=""))
    check("пустая настройка означает «флаг не передавать»",
          "--model" not in empty and "--fallback-model" not in empty, str(empty))


def test_model_visible_in_health_and_status():
    print("\n== Обе модели видны владельцу ==")

    class StubTools:
        gh = git = claude = python = ""

        def __init__(self, cfg=None):
            pass

        def missing(self):
            return ["gh", "git", "claude", "python3"]

    def output(command, cfg, args=None):
        """`health`/`status` без внешних вызовов: ни gh, ни launchctl, ни сети."""
        out = io.StringIO()
        tools_original, bridge.Tools = bridge.Tools, StubTools
        run_original = bridge.run
        bridge.run = lambda *a, **k: subprocess.CompletedProcess(["stub"], 1, "", "")
        try:
            with contextlib.redirect_stdout(out):
                command(args, cfg)
        finally:
            bridge.Tools, bridge.run = tools_original, run_original
        return out.getvalue()

    class StubArgs:
        remote = False

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "state"
        root.mkdir()

        for name, command, args in (("health", bridge.cmd_health, None),
                                    ("status", bridge.cmd_status, StubArgs())):
            text = output(command, bare_config(STATE_DIR=str(root)), args)
            line = next((l for l in text.splitlines() if "модель исполнителя" in l), "")
            check(f"{name} показывает обе модели",
                  "opus" in line and "fable" in line, line or text)

            text = output(command,
                          bare_config(STATE_DIR=str(root), CLAUDE_MODEL="sonnet",
                                      CLAUDE_FALLBACK_MODEL=""),
                          args)
            line = next((l for l in text.splitlines() if "модель исполнителя" in l), "")
            check(f"{name} показывает переопределение владельца, а не умолчание",
                  "sonnet" in line and "opus" not in line and "fable" not in line,
                  line or text)

        # Строка одна на две команды: две отдельные формулировки разошлись бы,
        # и владелец видел бы в разных командах разные модели.
        cfg = bare_config(STATE_DIR=str(root))
        health_line = next(l for l in output(bridge.cmd_health, cfg).splitlines()
                           if "модель исполнителя" in l)
        status_line = next(l for l in output(bridge.cmd_status, cfg, StubArgs()).splitlines()
                           if "модель исполнителя" in l)
        check("health и status говорят одно и то же",
              bridge.model_line(cfg) in health_line
              and bridge.model_line(cfg) in status_line,
              f"{health_line!r} / {status_line!r}")


def test_model_documented_in_config_example():
    print("\n== Файл настроек обещает то же, что делает код ==")
    example = (ROOT / "tools" / "agent-bridge" / "config.env.example").read_text(encoding="utf-8")
    for key in ("CLAUDE_MODEL", "CLAUDE_FALLBACK_MODEL"):
        default = bridge.CONFIG_KEYS[key]
        check(f"{key} описан в config.env.example с тем же значением",
              f"OBOROT_BRIDGE_{key}={default}" in example, key)
    check("в примере настроек нет ключей API",
          "OPENAI_API_KEY" not in example and "ANTHROPIC_API_KEY" not in example)


# --------------------------------------------------------------------------
# 12. Журнал владельца: шаблон соблюдён и остаётся человеческим
# --------------------------------------------------------------------------
#
# Почему это проверяется тестом, а не вычиткой. Журнал ведут агенты, и портится
# он ровно одним способом: запись понемногу превращается в протокол — SHA,
# `PUSHED`, `DEGRADED` вместо человеческого рассказа. Такой журнал формально
# существует, а по назначению (владелец читает и понимает) не работает.
# Проверяется структура, а не стиль: шесть обязательных пунктов на месте, а
# служебные слова и хеши — только в «Технической справке» в конце записи.

OWNER_LOG = ROOT / "OWNER_WORK_LOG.md"
AGENTS_MD = ROOT / "AGENTS.md"

OWNER_LOG_SECTIONS = (
    "Какие новые возможности добавили",
    "Что исправили",
    "Что это меняет для владельца",
    "Как проверили",
    "Что осталось",
)
PROTOCOL_WORDS = ("PUSHED", "TESTS_FAILED", "PUSH_REJECTED", "CLAUDE_FAILED",
                  "NO_CHANGES", "NEEDS_HUMAN", "READY_FOR_REVIEW",
                  "REVIEW_ACCEPT", "REQUEST_CHANGES", "DEGRADED", "HEALTHY")


def test_owner_log_follows_template():
    print("\n== Журнал владельца ==")
    check("OWNER_WORK_LOG.md есть в корне репозитория", OWNER_LOG.is_file())
    if not OWNER_LOG.is_file():
        return
    text = OWNER_LOG.read_text(encoding="utf-8")

    # Запись начинается с даты и задачи в самом заголовке: без даты журнал не
    # читается как история, без задачи — не ищется.
    entries = re.findall(r"^## (\d{2}\.\d{2}\.\d{4}) — (.+)$", text, re.MULTILINE)
    check("есть хотя бы одна запись с датой и задачей в заголовке",
          bool(entries), str(entries))

    bodies = re.split(r"^## \d{2}\.\d{2}\.\d{4} — .+$", text, flags=re.MULTILINE)[1:]
    for (date, _), body in zip(entries, bodies):
        missing = [name for name in OWNER_LOG_SECTIONS if name not in body]
        check(f"запись {date}: все шесть пунктов шаблона на месте",
              not missing, ", ".join(missing))

        # Служебные слова и хеши допустимы только в справке в конце записи.
        human = body.split("**Техническая справка**")[0]
        leaked = [word for word in PROTOCOL_WORDS if word in human]
        check(f"запись {date}: протокольные слова не в основном тексте",
              not leaked, ", ".join(leaked))
        hashes = re.findall(r"\b[0-9a-f]{7,40}\b", human)
        check(f"запись {date}: голых SHA в основном тексте нет", not hashes, str(hashes))

    check("шаблон записи описан в самом журнале",
          all(name in text.split("---")[0] for name in OWNER_LOG_SECTIONS))


def test_agents_md_requires_both_logs():
    print("\n== Правило про журналы в AGENTS.md ==")
    text = AGENTS_MD.read_text(encoding="utf-8")
    check("правило про журнал владельца обязательное, а не пожелание",
          "OWNER_WORK_LOG.md" in text and "обязатель" in text.lower())
    check("названы оба журнала: владельца и параллельный у Codex",
          "OWNER_WORK_LOG.md" in text and "PROJECT_WORK_LOG.md" in text)
    check("сказано, что журнал обновляется после каждого законченного блока",
          "законченного блока" in text)
    # Issue координации остаётся машиночитаемым: его формат под человеческое
    # чтение не переделывается, иначе ломается защита от дублей и сторож.
    check("Issue координации остаётся техническим зеркалом",
          "техническим зеркалом" in text)
    check("журнал владельца попал в карту документов",
          "OWNER_WORK_LOG.md" in (ROOT / "PROJECT_STATE.md").read_text(encoding="utf-8"))


def bridge_env(home: Path, **extra):
    """Окружение для запуска скриптов: без наследования настроек этой машины."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "LANG": "ru_RU.UTF-8",
    }
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def write_config(home: Path, *lines: str, config_home: Path | None = None) -> Path:
    """Файл настроек — единственное место, где значение переживает launchd.

    Переменная окружения существует только в той оболочке, где её задали;
    фоновая задача её не увидит, и установщик такие значения отвергает.
    """
    base = config_home if config_home is not None else home / ".config"
    path = base / "oborot-agent-bridge" / "config.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def install_check(home: Path, **extra):
    """`install.sh --check`: разбор настроек и запрет на полурабочий фон.

    Настоящей установки нет: до записи plist и до launchctl этот режим не
    доходит вовсе, поэтому тест безопасен и на машине владельца.
    """
    return subprocess.run(
        ["bash", str(INSTALL_SH), "--check"],
        capture_output=True, text=True, timeout=180, env=bridge_env(home, **extra))


def resolve_value(home: Path, key: str, **extra) -> str:
    result = subprocess.run(
        ["bash", str(RUN_SH), "resolve", key],
        capture_output=True, text=True, timeout=120, env=bridge_env(home, **extra))
    return result.stdout.strip()


def test_installer_refuses_without_interpreter():
    print("\n== Установщик не ставит фон, которому нечем гонять тесты ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()

        result = install_check(home)
        check("без venv установка обрывается", result.returncode == 1, result.stdout[-300:])
        check("сказано, чем это чинится",
              "python3 -m venv" in result.stderr and "requirements.lock" in result.stderr,
              result.stderr[-300:])
        check("названа причина, а не просто «ошибка»",
              "TESTS_FAILED" in result.stderr, result.stderr[-300:])
        check("настоящий LaunchAgent не появился",
              not (home / "Library" / "LaunchAgents").exists())

        # Обход health к этой проверке отношения не имеет: обходить нечего,
        # фон без интерпретатора не работает ни при каких флагах.
        skipped = install_check(home, OBOROT_BRIDGE_SKIP_HEALTH="1")
        check("OBOROT_BRIDGE_SKIP_HEALTH проверку не обходит",
              skipped.returncode == 1, skipped.stdout[-300:])

        # Тесты можно выключить сознательно — тогда интерпретатор не нужен.
        # Настройка идёт в файл, а не в переменную окружения: фон читает файл,
        # а переменную оболочки, из которой запускали установку, — нет.
        write_config(home, "OBOROT_BRIDGE_TEST_CMD=")
        off = install_check(home)
        check("с выключенными тестами установщик не возражает",
              off.returncode == 0, (off.stdout + off.stderr)[-300:])


def test_installer_accepts_venv_and_explicit_path():
    print("\n== Установщик и найденный интерпретатор ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        state = home / ".local" / "state" / "oborot-agent-bridge"
        venv = make_venv(state)

        result = install_check(home)
        check("venv в каталоге состояния установщик находит сам",
              result.returncode == 0 and str(venv) in result.stdout,
              (result.stdout + result.stderr)[-300:])
        check("установщик создал файл настроек",
              (home / ".config" / "oborot-agent-bridge" / "config.env").is_file())
        # Ради проверки настроек копировать репозиторий в LaunchAgents незачем.
        check("режим --check ничего не установил",
              not (home / "Library" / "LaunchAgents").exists())

        # Явный путь задаётся в файле настроек: только он доедет до launchd.
        own = make_executable(Path(tmp) / "own" / "python")
        write_config(home, "OBOROT_BRIDGE_TEST_PYTHON=" + str(own))
        explicit = install_check(home)
        check("явно заданный интерпретатор принимается",
              explicit.returncode == 0 and str(own) in explicit.stdout,
              (explicit.stdout + explicit.stderr)[-300:])

        write_config(home, "OBOROT_BRIDGE_TEST_PYTHON=" + str(Path(tmp) / "nope"))
        broken = install_check(home)
        check("явно заданный несуществующий путь установку обрывает",
              broken.returncode == 1 and "не исполняемый файл" in broken.stderr,
              (broken.stdout + broken.stderr)[-300:])


def test_launchagent_and_health_share_interpreter():
    print("\n== Фон и health видят один интерпретатор ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        state = home / ".local" / "state" / "oborot-agent-bridge"
        venv = make_venv(state)

        # Так спрашивает установщик, и то же самое видит ручной health.
        interactive_state = resolve_value(home, "state-dir")
        interactive = resolve_value(home, "test-python")
        check("в обычной сессии находится venv", interactive == str(venv), interactive)

        # А так выглядит окружение фоновой задачи: launchd не даёт ни XDG_*,
        # ни PATH пользователя — только то, что перечислено в plist. Раньше
        # тут получался пустой путь и молча брался системный python3.
        launchd = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(home),
            "OBOROT_BRIDGE_STATE_DIR": interactive_state,
            "LANG": "ru_RU.UTF-8",
        }
        result = subprocess.run(
            ["bash", str(RUN_SH), "resolve", "test-python"],
            capture_output=True, text=True, timeout=120, env=launchd)
        check("в окружении LaunchAgent находится тот же интерпретатор",
              result.stdout.strip() == interactive, result.stdout.strip())

        text = INSTALL_SH.read_text(encoding="utf-8")
        # Каталог состояния установщик обязан спрашивать у диспетчера: своя
        # реализация XDG в bash — это ровно тот способ разойтись молча.
        check("plist получает каталог состояния от самого диспетчера",
              'resolve state-dir' in text
              and "<key>OBOROT_BRIDGE_STATE_DIR</key>" in text
              and "XDG_STATE_HOME" not in text)
        check("файл настроек установщик тоже не вычисляет сам",
              'resolve config-file' in text and 'resolve config-home' in text
              and "XDG_CONFIG_HOME:-" not in text and "$HOME/.config" not in text)

        # `--purge` стирает каталог состояния. Считай он путь по-своему — стёр
        # бы не тот каталог и отчитался бы об успехе.
        removal = subprocess.run(
            ["bash", str(UNINSTALL_SH), "--help"],
            capture_output=True, text=True, timeout=120, env=bridge_env(home))
        check("uninstall.sh чистит тот же каталог, что видит диспетчер",
              removal.returncode == 0 and interactive_state in removal.stdout,
              (removal.stdout + removal.stderr)[-200:])
        # Проверка интерпретатора должна стоять до всего, что меняет систему.
        gate = text.index("Тесты включены")
        check("отказ случается раньше записи plist и launchctl",
              gate < text.index("launchctl bootstrap") and gate < text.index('cat >"$PLIST"'))


# --------------------------------------------------------------------------
# 10. Настройки, проверенные при установке, доезжают до launchd
# --------------------------------------------------------------------------
#
# Разбираемый дефект. Установщик проверял настройки своей оболочки — явный
# OBOROT_BRIDGE_TEST_PYTHON, нестандартный XDG_CONFIG_HOME, — а в plist клал
# только каталог состояния. LaunchAgent оболочку не наследует: фон читал
# ДРУГОЙ config.env (обычно несуществующий) и работал на значениях по
# умолчанию. «Проверено при установке» относилось не к тому, что потом
# работает, и разойтись эти двое могли молча. Закрыто с двух сторон: каталог
# настроек уходит в plist, а значения, живущие только в переменной окружения,
# установку обрывают.


def render_plist(home: Path, out_dir: Path, **where) -> dict:
    """Тот самый plist, который напишет install.sh, — но в каталог теста.

    Heredoc берётся из скрипта как есть и выполняется с теми же значениями,
    которые установщик получает от диспетчера. Сверять текст install.sh глазами
    недостаточно: разбираемый дефект в том и состоял, что в plist попадало не
    то, что проверено. Настоящий LaunchAgent не трогается — ни launchctl, ни
    ~/Library/LaunchAgents здесь нет.
    """
    body = (INSTALL_SH.read_text(encoding="utf-8")
            .split('cat >"$PLIST" <<PLIST_EOF\n', 1)[1]
            .split("\nPLIST_EOF\n")[0])
    script = (
        "set -euo pipefail\n"
        "LABEL=com.oborot.agent-bridge\n"
        "INTERVAL=60\n"
        'SCRIPT_DIR="$1"\nSTATE_DIR="$2"\nCONFIG_HOME="$3"\nPLIST="$4"\n'
        'cat >"$PLIST" <<PLIST_EOF\n' + body + "\nPLIST_EOF\n")
    path = out_dir / "com.oborot.agent-bridge.plist"
    subprocess.run(
        ["bash", "-c", script, "render", str(INSTALL_SH.parent),
         resolve_value(home, "state-dir", **where),
         resolve_value(home, "config-home", **where), str(path)],
        check=True, capture_output=True, text=True, timeout=120)
    # plistlib заодно и проверка разметки: развалившийся plist сюда не пройдёт.
    return plistlib.loads(path.read_bytes())


def launchd_env(home: Path, from_plist: dict) -> dict:
    """Ровно то, что достаётся фоновой задаче: PATH launchd плюс сам plist.

    Ни XDG_*, ни PATH пользователя, ни его rc-файлов launchd не передаёт.
    """
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home)}
    env.update(from_plist)
    return env


def outcome(result) -> str:
    """Однострочная выжимка из процесса: журнал прогона читают глазами."""
    return "код {}: {}".format(
        result.returncode, " ".join((result.stdout + result.stderr).split())[-160:])


def resolve_in(env: dict, key: str) -> str:
    result = subprocess.run(
        ["bash", str(RUN_SH), "resolve", key],
        capture_output=True, text=True, timeout=120, env=env)
    return result.stdout.strip()


def test_installer_refuses_settings_launchd_will_not_see():
    print("\n== Установщик не ставит фон на настройках, которых фон не увидит ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        make_venv(home / ".local" / "state" / "oborot-agent-bridge")
        own = make_executable(Path(tmp) / "own" / "python")

        # Раньше это проходило: установщик проверял интерпретатор из своей
        # оболочки и рапортовал об успехе, а фон о нём ничего не знал.
        env_only = install_check(home, OBOROT_BRIDGE_TEST_PYTHON=str(own))
        check("интерпретатор только из окружения — установка обрывается",
              env_only.returncode == 1, outcome(env_only))
        check("названа сама настройка, а не просто «ошибка»",
              "TEST_PYTHON" in env_only.stderr, outcome(env_only))
        check("сказано, в какой файл её переносить",
              str(home / ".config" / "oborot-agent-bridge" / "config.env") in env_only.stderr,
              outcome(env_only))
        # В сообщение попадают имена ключей, но не значения: оно уходит в
        # stderr, а оттуда — в чужие журналы и вставки в переписку.
        check("значение настройки в сообщении не печатается",
              str(own) not in env_only.stderr, outcome(env_only))
        check("настоящий LaunchAgent не появился",
              not (home / "Library" / "LaunchAgents").exists())

        # Правило общее, а не про один ключ.
        repo = install_check(home, OBOROT_BRIDGE_REPO="chuzhoy/repo")
        check("любая другая настройка из окружения — тоже отказ",
              repo.returncode == 1 and "REPO" in repo.stderr,
              outcome(repo))

        # Каталог состояния — единственное исключение: он уходит в plist
        # готовым путём, поэтому фон получит именно проверенный каталог.
        moved_state = Path(tmp) / "own-state"
        make_venv(moved_state)
        moved = install_check(home, OBOROT_BRIDGE_STATE_DIR=str(moved_state))
        check("каталог состояния из окружения установку не обрывает",
              moved.returncode == 0 and str(moved_state) in moved.stdout,
              outcome(moved))

        # Совпадающее значение расхождением не является: фон и так его получит
        # из файла настроек, терять нечего.
        write_config(home, "OBOROT_BRIDGE_TEST_PYTHON=" + str(own))
        same = install_check(home, OBOROT_BRIDGE_TEST_PYTHON=str(own))
        check("переменная, совпадающая с файлом настроек, не мешает",
              same.returncode == 0 and str(own) in same.stdout,
              outcome(same))

        text = INSTALL_SH.read_text(encoding="utf-8")
        gate = text.index("заданы только переменными окружения")
        check("отказ случается раньше записи plist и launchctl",
              gate < text.index('cat >"$PLIST"') and gate < text.index("launchctl bootstrap"))


def test_nondefault_config_home_reaches_launchd():
    print("\n== Нестандартный XDG_CONFIG_HOME доезжает до фоновой задачи ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        state = home / ".local" / "state" / "oborot-agent-bridge"
        # venv на месте по умолчанию: если каталог настроек до фона не доедет,
        # фон не упадёт, а молча возьмёт вот этот интерпретатор — потому дефект
        # и был незаметен.
        default_venv = make_venv(state)
        own = make_executable(Path(tmp) / "own" / "python")

        config_home = home / "xdg"
        write_config(home, "OBOROT_BRIDGE_TEST_PYTHON=" + str(own), config_home=config_home)
        where = {"XDG_CONFIG_HOME": str(config_home)}

        check("диспетчер отдаёт установщику каталог настроек",
              resolve_value(home, "config-home", **where) == str(config_home),
              resolve_value(home, "config-home", **where))
        check("файл настроек считается от него же",
              resolve_value(home, "config-file", **where)
              == str(config_home / "oborot-agent-bridge" / "config.env"))

        result = install_check(home, **where)
        check("установщик читает настройки из нестандартного каталога",
              result.returncode == 0 and str(own) in result.stdout,
              outcome(result))
        check("каталог настроек назван в отчёте установщика",
              str(config_home) in result.stdout, outcome(result))

        interactive = resolve_value(home, "test-python", **where)
        check("в обычной сессии берётся интерпретатор из файла настроек",
              interactive == str(own), interactive)

        # Дальше — не пересказ plist, а сам plist: тот, который напишет
        # install.sh, с теми же значениями от диспетчера.
        environment = render_plist(home, Path(tmp), **where)["EnvironmentVariables"]
        check("plist отдаёт фону каталог настроек",
              environment.get("XDG_CONFIG_HOME") == str(config_home),
              str(environment))
        check("и каталог состояния — тоже проверенный",
              environment.get("OBOROT_BRIDGE_STATE_DIR")
              == resolve_value(home, "state-dir", **where), str(environment))
        # Значения настроек в plist не уезжают: место настроек одно — файл, и
        # правка config.env должна действовать на фон сразу.
        check("в plist уходят пути, а не сами настройки",
              [key for key in environment if key.startswith("OBOROT_BRIDGE_")]
              == ["OBOROT_BRIDGE_STATE_DIR"], str(environment))

        with_plist = launchd_env(home, environment)
        check("фон с окружением из этого plist видит тот же интерпретатор",
              resolve_in(with_plist, "test-python") == interactive,
              resolve_in(with_plist, "test-python"))
        check("фон читает тот же файл настроек",
              resolve_in(with_plist, "config-file")
              == resolve_value(home, "config-file", **where))

        # Проверка мутацией: убрать эту строку из plist — и фон не упадёт, а
        # молча возьмёт интерпретатор по умолчанию, то есть НЕ проверенный.
        blind = {key: value for key, value in environment.items()
                 if key != "XDG_CONFIG_HOME"}
        check("без этой строки в plist фон брал бы другой интерпретатор",
              resolve_in(launchd_env(home, blind), "test-python")
              == str(default_venv) != interactive,
              resolve_in(launchd_env(home, blind), "test-python"))


# --------------------------------------------------------------------------
# 13. «Замечаний нет» — про всё тело, а не про первые три слова
# --------------------------------------------------------------------------
#
# Разбираемый дефект. Шаблон пустого ревью применялся подстрокой. Ревьюер же
# пишет «No blocking issues, but the retry path still loses findings» — первые
# три слова совпадают с шаблоном, а замечание настоящее. Диспетчер такое ревью
# помечал CLEAN и не будил Claude; сторож пользовался тем же признаком и тоже
# молчал. Замечание не доезжало никуда, и выглядело это как «всё чисто».

# Тела, которые ОБЯЗАНЫ считаться работой.
QUALIFIED_BODIES = [
    "No blocking issues, but the retry path still loses findings.",
    "Looks good overall; however, the migration drops the index.",
    "LGTM. One more thing: the token is written to the log in plain text.",
    "No major issues — though the lock is released before the file is closed.",
    "Замечаний нет по стилю, но откат миграции не покрыт тестами.",
    "Nothing blocking. Please rename the flag before merge.",
]

# Тела, которые ОБЯЗАНЫ считаться пустыми: иначе каждый чистый прогон ревьюера
# будит Claude впустую и жжёт попытки для этого SHA.
CLEAN_BODIES = [
    "No issues found.",
    "LGTM",
    "Looks good overall.",
    "Codex didn't find any major issues in this pull request.",
    "Замечаний нет.",
    "По этому коду замечаний нет, всё в порядке.",
    "### 💡 Codex Review\n\nLGTM\n\n"
    "<details><summary>About Codex in GitHub</summary>\n"
    "Reviews are triggered when you open a pull request.\n</details>\n\n"
    "Useful? React with 👍 / 👎.",
]


def test_clean_verdict_covers_whole_body():
    print("\n== Чистый вердикт — это всё тело целиком ==")
    noop = re.compile(bridge.CONFIG_KEYS["NOOP_REVIEW_PATTERN"], re.IGNORECASE)

    for body in QUALIFIED_BODIES:
        check(f"с оговоркой — работа: {body[:48]!r}",
              not bridge.is_clean_verdict(body, noop))
    for body in CLEAN_BODIES:
        check(f"чистое — не работа: {body[:48]!r}",
              bridge.is_clean_verdict(body, noop))

    # То же самое на уровне ревью целиком: именно здесь решение принимается.
    for body in QUALIFIED_BODIES[:2]:
        bundle = bridge.ReviewBundle("a" * 40)
        bundle.reviews = [review(1, "a" * 40, body=body)]
        check("ревью с оговоркой будит Claude", bundle.is_actionable(noop), body[:48])
    empty = bridge.ReviewBundle("a" * 40)
    empty.reviews = [review(1, "a" * 40, body="No issues found.")]
    check("чистое ревью Claude не будит", not empty.is_actionable(noop))

    # Проверка мутацией: старое поведение (шаблон подстрокой) на этом же
    # наборе обязано провалиться — иначе тест ничего не стережёт.
    old_behaviour_clean = [body for body in QUALIFIED_BODIES if noop.search(body)]
    check("старое правило «подстрокой» на этом наборе действительно ошибалось",
          len(old_behaviour_clean) >= 4, str(len(old_behaviour_clean)))


def js_word_set(text: str, name: str) -> set:
    """Список слов из JS-набора сторожа."""
    match = re.search(r"const " + name + r" = new Set\(\[(.*?)\]\);", text, re.S)
    if not match:
        return set()
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_watchdog_shares_clean_verdict():
    print("\n== Сторож понимает «замечаний нет» так же, как диспетчер ==")
    text = WATCHDOG_YML.read_text(encoding="utf-8")

    check("сторож больше не проверяет шаблон подстрокой",
          "noopPattern.test(body)" not in text)
    check("сторож считает вердикт по всему телу", "isCleanVerdict(body)" in text)
    # Списки слов у сторожа и диспетчера — копия, и копия обязана совпадать.
    # Разойдутся молча: сторож начнёт молчать там, где диспетчер работает.
    check("список слов-заполнителей совпадает",
          js_word_set(text, "VERDICT_FILLER") == bridge.VERDICT_FILLER,
          str(sorted(js_word_set(text, "VERDICT_FILLER") ^ bridge.VERDICT_FILLER))[:200])
    check("список слов-переломов совпадает",
          js_word_set(text, "VERDICT_CONTRAST") == bridge.VERDICT_CONTRAST,
          str(sorted(js_word_set(text, "VERDICT_CONTRAST") ^ bridge.VERDICT_CONTRAST))[:200])

    # Совпадения списков мало: сравнивать надо поведение. Код сторожа — это
    # JavaScript внутри workflow, и он тут же выполняется на том же наборе тел,
    # что и Python. Движок берётся любой доступный: в Linux CI это node, на
    # маке владельца — JavaScriptCore через `osascript` (node на маке может и
    # не стоять, а проверять сторожа на машине, где он чинится, важнее всего).
    block = re.search(r"// --- clean-verdict:start ---(.*?)// --- clean-verdict:end ---",
                      text, re.S)
    check("блок разбора вердикта у сторожа помечен для проверки", bool(block))
    if not block:
        return
    bodies = QUALIFIED_BODIES + CLEAN_BODIES
    # Отступы YAML снимаются: внутри workflow код лежит с отступом блока `script`.
    driver = (
        "const noopPattern = new RegExp({}, 'i');\n".format(
            json.dumps(bridge.CONFIG_KEYS["NOOP_REVIEW_PATTERN"]))
        + textwrap.dedent(block.group(1))
        + "\nconst RESULT = JSON.stringify({}.map(isCleanVerdict));\n".format(
            json.dumps(bodies, ensure_ascii=False))
        # node печатает сам; osascript печатает значение последнего выражения.
        + "if (typeof process !== 'undefined') console.log(RESULT);\nRESULT;\n"
    )
    engine = ([shutil.which("node")] if shutil.which("node")
              else ["/usr/bin/osascript", "-l", "JavaScript"]
              if Path("/usr/bin/osascript").exists() else [])
    if not engine:
        print("  SKIP движка JavaScript нет — поведение сторожа проверит Linux CI")
        return
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "verdict.js"
        script.write_text(driver, encoding="utf-8")
        result = subprocess.run(engine + [str(script)],
                                capture_output=True, text=True, timeout=120)
        check(f"код сторожа выполняется ({Path(engine[0]).name})",
              result.returncode == 0, result.stderr[-300:])
        if result.returncode != 0:
            return
        got = json.loads(result.stdout.strip())
        expected = [False] * len(QUALIFIED_BODIES) + [True] * len(CLEAN_BODIES)
        mismatch = [body[:40] for body, a, b in zip(bodies, got, expected) if a != b]
        check("сторож и диспетчер отвечают одинаково на всех телах",
              got == expected, "; ".join(mismatch))


# --------------------------------------------------------------------------
# 14. Неполный цикл не выдаёт себя за успешный
# --------------------------------------------------------------------------
#
# Разбираемый дефект. Исключение в обработке одного PR ловилось и писалось в
# журнал — это правильно, соседний PR обработать надо. Но дальше цикл
# безусловно записывал «успех»: `poll --once` возвращал ноль, `status` показывал
# успешный цикл, а PR оставался необработанным. Ошибка, которой не видно, не
# чинится: она просто повторяется каждую минуту.

class StubPRList:
    """GitHub, у которого есть только список открытых PR."""

    def __init__(self, prs):
        self._prs = prs

    def open_agent_prs(self, prefix):
        return list(self._prs)


def open_pr(number: int, sha: str) -> dict:
    return {"number": number, "head": {"ref": f"claude/x{number}", "sha": sha}}


def test_failed_pr_makes_cycle_degraded():
    print("\n== Упавший PR виден снаружи ==")
    sha = "a" * 40
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        inst = make_bridge({"REPO": "o/r"}, StubPRList([open_pr(7, sha), open_pr(9, sha)]),
                           state_path)

        def boom(pr):
            if pr["number"] == 7:
                raise RuntimeError("gh api не ответил")
            return False

        inst.handle_pr = boom
        inst.cycle()

        # Изоляция по PR сохраняется: сосед обработан, цикл не упал целиком.
        check("цикл дошёл до конца, соседний PR обработан",
              inst.state.data.get("cycles") == 1)
        check("цикл помечен неуспешным", inst.state.data.get("last_cycle_ok") is False)
        check("запомнен номер упавшего PR",
              inst.state.data.get("last_cycle_failed_prs") == [7],
              str(inst.state.data.get("last_cycle_failed_prs")))
        check("причина сохранена и в ней есть номер PR",
              "PR #7" in (inst.state.data.get("last_error") or ""),
              str(inst.state.data.get("last_error")))

        # Код возврата: launchd и человек узнают о неполном цикле только по нему.
        check("poll --once возвращает ненулевой код",
              bridge._one_cycle(inst, StubLog(), inst.state) == 1)

        # Следующий чистый цикл обязан снять и отметку, и старую причину:
        # висящая вечно ошибка врёт ровно так же, как скрытая свежая.
        inst.handle_pr = lambda pr: False
        inst.cycle()
        check("чистый цикл снова успешен", inst.state.data.get("last_cycle_ok") is True)
        check("старая причина не висит в состоянии",
              not inst.state.data.get("last_error"))
        check("чистый цикл возвращает ноль",
              bridge._one_cycle(inst, StubLog(), inst.state) == 0)

        # Упавший целиком цикл — это не «не обработан PR #7»: до PR он не
        # дошёл. Список от прошлого цикла обязан очиститься, иначе `status`
        # покажет чужую причину как свою.
        inst.state.data["last_cycle_failed_prs"] = [7]

        def cycle_dies():
            raise RuntimeError("gh недоступен")

        inst.cycle = cycle_dies
        check("упавший целиком цикл возвращает единицу",
              bridge._one_cycle(inst, StubLog(), inst.state) == 1)
        check("список упавших PR от прошлого цикла не остаётся",
              inst.state.data.get("last_cycle_failed_prs") == [],
              str(inst.state.data.get("last_cycle_failed_prs")))


def test_status_shows_degraded_cycle():
    print("\n== status показывает неполный цикл честно ==")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        root = Path(tmp) / "state"
        root.mkdir()
        (root / "state.json").write_text(json.dumps({
            "version": 1, "prs": {}, "cycles": 12,
            "last_cycle_ok": False,
            "last_cycle_failed_prs": [7],
            "last_error": "PR #7: gh api не ответил",
            "last_cycle_finished_at": "2026-08-23T10:00:00+00:00",
        }, ensure_ascii=False), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(BRIDGE_PY), "--state-dir", str(root), "status"],
            capture_output=True, text=True, timeout=120, env=bridge_env(home))
        out = result.stdout
        check("status не падает", result.returncode == 0, result.stderr[-300:])
        check("сказано, что цикл неполный", "неполный" in out, out[:400])
        check("назван PR, который не обработан", "#7" in out, out[:400])
        check("показана причина", "gh api не ответил" in out, out[:400])
        check("успехом это не называется", "(успех)" not in out, out[:400])


# --------------------------------------------------------------------------
# 15. `uninstall.sh --purge` удаляет только доказанный каталог диспетчера
# --------------------------------------------------------------------------
#
# Разбираемый дефект. `--purge` брал каталог состояния из настроек и вызывал
# `rm -rf` без единой проверки. Настройки пишет человек: `STATE_DIR=$HOME` —
# это одна опечатка, после которой домашний каталог удаляется целиком, а скрипт
# отчитывается об успехе. Ниже проверяется отказ на широких путях и удаление
# только там, где принадлежность диспетчеру доказана.
#
# Настоящее `rm` в опасных случаях подменяется заглушкой: тест обязан проверять
# РЕШЕНИЕ скрипта, а не проверять его ценой чужих данных. Если проверка когда-
# нибудь сломается, заглушка запишет попытку — и тест это увидит, ничего не
# потеряв. Единственный случай, где удаление настоящее, — временный каталог,
# созданный этим же тестом.

def purge_sandbox(tmp: Path) -> tuple:
    """Домашний каталог-однодневка и стойка с заглушками для PATH."""
    home = tmp / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Documents" / "договор.txt").write_text("ценное", encoding="utf-8")
    (home / "важное.txt").write_text("ценное", encoding="utf-8")

    stubs = tmp / "bin"
    stubs.mkdir()
    # launchctl: настоящий трогать нельзя — на машине владельца в нём живёт
    # его собственный, ни в чём не виноватый LaunchAgent.
    (stubs / "launchctl").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (stubs / "launchctl").chmod(0o755)
    return home, stubs


def run_purge(home: Path, stubs: Path, rm_log: Path | None = None, **extra):
    env = bridge_env(home, **extra)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    if rm_log is not None:
        env["OBOROT_TEST_RM_LOG"] = str(rm_log)
    return subprocess.run(["bash", str(UNINSTALL_SH), "--purge"],
                          capture_output=True, text=True, timeout=180, env=env)


def test_purge_refuses_unsafe_targets():
    print("\n== --purge отказывается от широких каталогов ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home, stubs = purge_sandbox(root)

        # Заглушка вместо rm: ничего не удаляет, но записывает, что просили.
        rm_log = root / "rm.log"
        (stubs / "rm").write_text(
            '#!/bin/sh\nprintf \'%s\\n\' "$@" >> "$OBOROT_TEST_RM_LOG"\nexit 0\n',
            encoding="utf-8")
        (stubs / "rm").chmod(0o755)

        (root / "просто-каталог").mkdir()
        dangerous = {
            "домашний каталог": str(home),
            "предок домашнего каталога": str(home.parent),
            "корень файловой системы": "/",
            "Documents": str(home / "Documents"),
            "каталог без признаков диспетчера": str(root / "просто-каталог"),
            "несуществующий путь": str(root / "нет-такого"),
        }
        for name, target in dangerous.items():
            result = run_purge(home, stubs, rm_log,
                               OBOROT_BRIDGE_STATE_DIR=target)
            if name == "несуществующий путь":
                # Нечего удалять — это не отказ, а просто пустая работа.
                check("несуществующий каталог ошибкой не считается",
                      result.returncode == 0, result.stderr[-200:])
                continue
            check(f"отказ: {name}",
                  result.returncode == 1 and "Отказ" in result.stderr,
                  (result.stdout + result.stderr)[-250:])
            check(f"причина названа словами: {name}",
                  "причина:" in result.stderr, result.stderr[-250:])

        attempts = rm_log.read_text(encoding="utf-8") if rm_log.exists() else ""
        check("ни одного удаления при отказе не запрошено",
              not any(line.strip() in {"/", str(home), str(home.parent),
                                       str(home / "Documents"),
                                       str(root / "просто-каталог")}
                      for line in attempts.splitlines()),
              attempts[:300])
        check("ценные файлы на месте",
              (home / "важное.txt").is_file()
              and (home / "Documents" / "договор.txt").is_file())


def test_purge_removes_only_proven_bridge_dirs():
    print("\n== --purge удаляет свой каталог и не трогает соседей ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home, stubs = purge_sandbox(root)

        # Здесь rm настоящий: удаляется каталог, созданный этим же тестом.
        state = home / ".local" / "state" / "oborot-agent-bridge"
        (state / "checkout").mkdir(parents=True)
        (state / "state.json").write_text("{}", encoding="utf-8")
        config = home / ".config" / "oborot-agent-bridge"
        config.mkdir(parents=True)
        (config / "config.env").write_text("# пусто\n", encoding="utf-8")

        result = run_purge(home, stubs)
        check("зачистка прошла без отказов", result.returncode == 0,
              (result.stdout + result.stderr)[-250:])
        check("каталог состояния удалён", not state.exists())
        check("каталог настроек удалён", not config.exists())
        check("ничего вокруг не пострадало",
              (home / "важное.txt").is_file()
              and (home / "Documents" / "договор.txt").is_file())

        # Свой каталог с непривычным именем: удаляется, только если в нём
        # лежат файлы диспетчера. Пустой такой каталог — отказ (см. выше).
        custom = root / "своё-состояние"
        custom.mkdir()
        (custom / "bridge.log").write_text("", encoding="utf-8")
        named = run_purge(home, stubs, OBOROT_BRIDGE_STATE_DIR=str(custom))
        check("каталог с файлами диспетчера удаляется и под своим именем",
              named.returncode == 0 and not custom.exists(),
              (named.stdout + named.stderr)[-250:])


def main() -> int:
    print(f"agent-bridge: {BRIDGE_PY.relative_to(ROOT)}")
    test_parse_api_output()
    test_github_api_paginated()
    test_portion_fingerprint()
    test_dropped_comments_return_next_cycle()
    test_failed_attempt_returns_portion()
    test_mirror_to_coordination_issue()
    test_mirror_can_be_disabled()
    test_inline_anchored_to_review_commit()
    test_old_review_does_not_wake_claude()
    test_review_requested_after_push()
    test_review_request_guards()
    test_test_cmd_matches_ci()
    test_watchdog_permissions()
    test_test_python_resolution()
    test_run_tests_uses_resolved_interpreter()
    test_health_requires_interpreter()
    test_installer_refuses_without_interpreter()
    test_installer_accepts_venv_and_explicit_path()
    test_launchagent_and_health_share_interpreter()
    test_installer_refuses_settings_launchd_will_not_see()
    test_nondefault_config_home_reaches_launchd()
    test_model_defaults_and_argv()
    test_model_visible_in_health_and_status()
    test_model_documented_in_config_example()
    test_owner_log_follows_template()
    test_agents_md_requires_both_logs()
    test_clean_verdict_covers_whole_body()
    test_watchdog_shares_clean_verdict()
    test_failed_pr_makes_cycle_degraded()
    test_status_shows_degraded_cycle()
    test_purge_refuses_unsafe_targets()
    test_purge_removes_only_proven_bridge_dirs()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
