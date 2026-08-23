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
  6) команда тестов у диспетчера совпадает с командой CI.

Запуск из корня репозитория:  python tests/test_agent_bridge.py
"""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PY = ROOT / "tools" / "agent-bridge" / "bridge.py"
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


def inline_comment(id_, sha, path="app/x.py", line=1):
    return {"id": id_, "commit_id": sha, "path": path, "line": line,
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
# 4. Команда тестов совпадает с CI
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


def main() -> int:
    print(f"agent-bridge: {BRIDGE_PY.relative_to(ROOT)}")
    test_parse_api_output()
    test_github_api_paginated()
    test_portion_fingerprint()
    test_dropped_comments_return_next_cycle()
    test_failed_attempt_returns_portion()
    test_mirror_to_coordination_issue()
    test_mirror_can_be_disabled()
    test_test_cmd_matches_ci()
    test_watchdog_permissions()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
