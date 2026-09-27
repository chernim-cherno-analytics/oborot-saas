# -*- coding: utf-8 -*-
"""Восстановление доступа по одноразовой ссылке (PILOT-MANUAL-RECOVERY-1, D-63).

Что доказывается и почему именно так:

  1) политика владельца не шире, чем решено: без ОБОИХ подтверждённых
     контактов (телефон и Telegram) и без описания обратного звонка ссылка не
     выпускается; замена контакта — только явной перепроверкой;
  2) токен нигде не лежит: в базе только SHA-256, ни в одной строке ни одной
     таблицы и ни в одной записи журнала (включая журнал ДОСТУПА uvicorn —
     именно он пишет адрес запроса) его нет; в браузере он не уходит ни в
     адресе запроса, ни в Referer, а из адресной строки стирается;
  3) одноразовость атомарна: повтор, истёкшая, отозванная и неизвестная ссылка
     дают один и тот же ответ; из пяти одновременных запросов проходит ровно
     один; сбой посреди смены пароля откатывает и отметку об использовании;
  4) все сессии отзываются, новая не выдаётся, соседний пользователь не
     задет; сброс работает и для организации в readonly;
  5) без новых таблиц отозванные сессии остаются отозванными: отзыв живёт
     только в users.session_version. Проверяется удалением новых таблиц — это
     НЕ запуск кода предыдущей версии. Откат опирается ещё и на то, что сама
     проверка версии в auth.resolve_auth этим пакетом не менялась;
  6) путь человека в настоящем браузере на 1400 и на 390.

Данные только синтетические, сервер локальный, наружу набор не ходит.
Запуск из корня репозитория:  python tests/test_account_recovery.py
"""
import hashlib
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_account_recovery.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8847"))
BASE = f"http://127.0.0.1:{APP_PORT}"

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.pop("OBOROT_SUBSCRIPTION_GATE", None)
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import account_recovery as ar  # noqa: E402
from app import auth  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

PASS, FAIL = [], []
TOKENS: list[str] = []          # каждый выпущенный токен — для проверки журналов
LOG_LINES: list[str] = []
PW0 = "Start-pass-111"


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


class Capture(logging.Handler):
    """Собирает ВСЁ, что пишут журналы приложения и uvicorn, включая доступ."""

    def emit(self, record):
        try:
            LOG_LINES.append(record.getMessage())
        except Exception:  # noqa: BLE001
            LOG_LINES.append(str(record.msg))


class ServerThread:
    def __init__(self, asgi_app, port: int):
        # log_level=info — иначе журнал доступа не пишется вовсе, и проверка
        # «токена нет в адресе запроса» была бы проверкой пустоты.
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="info", access_log=True)
        cap = Capture(level=logging.DEBUG)
        for name in ("uvicorn.access", "uvicorn.error"):
            lg = logging.getLogger(name)
            lg.handlers = [cap]          # в набор, а не в stdout раннера
            lg.propagate = False
        root = logging.getLogger()
        root.addHandler(cap)
        root.setLevel(logging.DEBUG)
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("сервер не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"},
                        follow_redirects=False, timeout=60.0)


def register(email: str, org: str) -> httpx.Client:
    c = client()
    r = c.post("/register", data={"name": "Синтетика", "email": email,
                                  "password": PW0, "org_name": org})
    assert r.status_code == 303, r.text[:200]
    return c


def login(email: str, password: str) -> httpx.Client | None:
    c = client()
    r = c.post("/login", data={"email": email, "password": password})
    return c if r.status_code == 303 else None


def alive(c: httpx.Client) -> bool:
    return c.get("/api/account").status_code == 200


def sql(query: str, args=()):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(query, args)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def user_row(email: str):
    return sql("SELECT id, pw_hash, session_version FROM users WHERE email=?", (email,))[0]


def issue(email: str, now=None) -> str:
    db = SessionLocal()
    try:
        token, _ = ar.issue_reset(db, email=email, operator="тест",
                                  callback_note="перезвонил на записанный номер", now=now)
    finally:
        db.close()
    TOKENS.append(token)
    return token


def reset(c: httpx.Client, token: str, pw: str, confirm: str | None = None) -> httpx.Response:
    return c.post("/api/account/reset", json={"token": token, "new_password": pw,
                                              "confirm_password": pw if confirm is None else confirm})


def refused(fn) -> str:
    db = SessionLocal()
    try:
        fn(db)
    except ar.RecoveryRefused as exc:
        return str(exc)
    finally:
        db.close()
    return ""


def set_contact(email, kind, value, reverify=False):
    db = SessionLocal()
    try:
        return ar.set_contact(db, email=email, kind=kind, value=value, operator="тест",
                              method="видеозвонок при подключении", reverify=reverify)
    finally:
        db.close()


def clear_limit():
    auth.reset_ip_limiter.reset("ip:127.0.0.1")


# ── сценарии ─────────────────────────────────────────────────────────────────

A, B, M = "owner-a@test.io", "owner-b@test.io", "ms-user@test.io"
STATE = {"pw": PW0}


def policy():
    print("\n== Политика D-63: без обоих контактов и звонка ссылки нет ==")
    note = "перезвонил на записанный номер"
    msg = refused(lambda db: ar.issue_reset(db, email=A, operator="т", callback_note=note))
    check("без контактов — отказ", "phone" in msg and "telegram" in msg, msg)
    set_contact(A, "phone", "+7 (999) 111-22-33")
    msg = refused(lambda db: ar.issue_reset(db, email=A, operator="т", callback_note=note))
    check("с одним телефоном — всё ещё отказ", "telegram" in msg, msg)
    set_contact(A, "telegram", "Owner_A_tg")
    msg = refused(lambda db: ar.issue_reset(db, email=A, operator="т", callback_note="да"))
    check("без описания обратного звонка — отказ", "звонок" in msg.lower(), msg)
    msg = refused(lambda db: ar.issue_reset(db, email=A, operator="", callback_note=note))
    check("без имени оператора — отказ", "оператор" in msg.lower(), msg)
    msg = refused(lambda db: ar.issue_reset(db, email="nobody@test.io", operator="т",
                                            callback_note=note))
    check("неизвестный e-mail — отказ (только оператору)", "нет" in msg, msg)
    msg = refused(lambda db: ar.issue_reset(db, email=M, operator="т", callback_note=note))
    check("вход через МойСклад — сбрасывать нечего", "МойСклад" in msg, msg)
    msg = refused(lambda db: ar.set_contact(db, email=A, kind="phone", value="+79990000000",
                                            operator="т", method="запрос в чате поддержки"))
    check("замена записанного контакта без перепроверки — отказ",
          "--reverify" in msg, msg)
    set_contact(A, "phone", "+7 999 444-55-66", reverify=True)
    check("замена с перепроверкой записана",
          sql("SELECT value FROM recovery_contacts rc JOIN users u ON u.id=rc.user_id "
              "WHERE u.email=? AND kind='phone'", (A,))[0][0] == "+79994445566")
    for kind, bad in (("phone", "12-34"), ("telegram", "@ab"), ("fax", "123")):
        msg = refused(lambda db, k=kind, v=bad: ar.set_contact(
            db, email=A, kind=k, value=v, operator="т", method="видеозвонок при подключении"))
        check(f"непохожее на контакт ({kind}) — отказ", bool(msg), msg)


def token_storage():
    print("\n== Токен не хранится: только хеш ==")
    token = issue(A)
    rows = sql("SELECT token_hash, created_at, expires_at FROM password_resets "
               "ORDER BY id DESC LIMIT 1")
    th, created, expires = rows[0]
    check("в базе SHA-256 токена", th == hashlib.sha256(token.encode()).hexdigest())
    ttl = datetime.fromisoformat(expires) - datetime.fromisoformat(created)
    check("срок жизни ровно 30 минут", ttl == timedelta(minutes=30), str(ttl))
    con = sqlite3.connect(DB_PATH)
    dump = "\n".join(con.iterdump())
    con.close()
    check("открытого токена нет ни в одной строке базы", token not in dump)
    link = ar.reset_link("https://app.example.ru/", token)
    check("токен в ссылке — во фрагменте, не в пути и не в запросе",
          link == f"https://app.example.ru/reset#t={token}")
    return token


def consume_http(token: str):
    print("\n== Использование ссылки: пароль, отзыв сессий, без входа ==")
    old = login(A, STATE["pw"])
    check("до сброса старая сессия жива", old is not None and alive(old))
    neighbour_before = user_row(B)
    _, _, v0 = user_row(A)
    c = client()
    r = reset(c, token, "New-pass-222")
    check("сброс принят", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    cookie_hdr = " ".join(r.headers.get_list("set-cookie"))
    check("сессия НЕ выдана (кука только стирается)",
          "oborot_session=" not in cookie_hdr or 'oborot_session="";' in cookie_hdr
          or "Max-Age=0" in cookie_hdr, cookie_hdr[:160])
    check("ответ не кешируется", r.headers.get("cache-control") == "no-store")
    check("старая сессия отозвана", not alive(old))
    check("старый пароль не входит", login(A, STATE["pw"]) is None)
    check("новый пароль входит", login(A, "New-pass-222") is not None)
    check("версия сессии выросла ровно на 1", user_row(A)[2] == v0 + 1,
          f"{v0} → {user_row(A)[2]}")
    check("сосед не задет", user_row(B) == neighbour_before)
    STATE["pw"] = "New-pass-222"
    return r


def reuse_expiry_reissue(token_used: str):
    print("\n== Одноразовость, срок, перевыпуск: ответ один и тот же ==")
    c = client()
    r_reuse = reset(c, token_used, "Other-pass-333")
    r_unknown = reset(c, "x" * 43, "Other-pass-333")
    old = issue(A, now=datetime.utcnow() - timedelta(minutes=31))
    r_exp = reset(c, old, "Other-pass-333")
    t1 = issue(A)
    t2 = issue(A)
    r_revoked = reset(c, t1, "Other-pass-333")
    bodies = {(r.status_code, r.text) for r in (r_reuse, r_unknown, r_exp, r_revoked)}
    check("повтор, неизвестная, истёкшая, отозванная — один ответ 400",
          len(bodies) == 1 and next(iter(bodies))[0] == 400, str(bodies)[:200])
    check("и пароль ни одна из них не сменила", login(A, STATE["pw"]) is not None)
    clear_limit()
    r = reset(c, t2, "Fresh-pass-444")
    check("новейшая ссылка после перевыпуска работает", r.status_code == 200, r.text[:120])
    STATE["pw"] = "Fresh-pass-444"


def password_rules_and_csrf():
    print("\n== Правила пароля и CSRF: ссылка при этом не сгорает ==")
    clear_limit()
    t = issue(A)
    c = client()
    cases = (("Short-7", None, "7 символов"), ("Ы" * 37, None, "73+ байта"),
             ("Good-pass-555", "Other-pass-555", "не совпадает"))
    for pw, conf, label in cases:
        r = reset(c, t, pw, conf)
        check(f"правило пароля: {label} → 422", r.status_code == 422, r.text[:100])
    bare = httpx.Client(base_url=BASE, timeout=30.0)
    r = bare.post("/api/account/reset", json={"token": t, "new_password": "Good-pass-555",
                                              "confirm_password": "Good-pass-555"})
    check("без заголовка CSRF — отказ 403", r.status_code == 403, str(r.status_code))
    r = reset(c, t, "Good-pass-555")
    check("после всех отказов ссылка всё ещё рабочая", r.status_code == 200, r.text[:100])
    STATE["pw"] = "Good-pass-555"


def readonly_gate():
    print("\n== Readonly: сброс не упирается в 402 ==")
    clear_limit()
    t = issue(A)
    c = login(A, STATE["pw"])
    org_id = sql("SELECT m.org_id FROM memberships m JOIN users u ON u.id=m.user_id "
                 "WHERE u.email=?", (A,))[0][0]
    saved = sql("SELECT trial_ends_at, plan, paid_until FROM orgs WHERE id=?", (org_id,))[0]
    sql("UPDATE orgs SET trial_ends_at='2020-01-01 00:00:00', plan='trial', paid_until=NULL "
        "WHERE id=?", (org_id,))
    os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    try:
        w = c.post("/api/settings", json={})
        check("контроль: гейт действительно закрывает запись (402)", w.status_code == 402,
              str(w.status_code))
        r = reset(c, t, "Gate-pass-666")
        check("сброс с кукой readonly-организации проходит (200)", r.status_code == 200,
              f"{r.status_code} {r.text[:100]}")
    finally:
        os.environ.pop("OBOROT_SUBSCRIPTION_GATE", None)
        sql("UPDATE orgs SET trial_ends_at=?, plan=?, paid_until=? WHERE id=?", (*saved, org_id))
    STATE["pw"] = "Gate-pass-666"


def concurrency():
    print("\n== Одновременные попытки: проходит ровно одна ==")
    clear_limit()
    t = issue(A)
    v0 = user_row(A)[2]
    barrier = threading.Barrier(5)
    codes: list[int] = []

    def one(i):
        c = client()
        barrier.wait()
        codes.append(reset(c, t, f"Race-pass-{i}77").status_code)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    check("пять одновременных: ровно один 200, четыре 400",
          sorted(codes) == [200, 400, 400, 400, 400], str(sorted(codes)))
    check("версия сессии выросла ровно на 1, а не на 5", user_row(A)[2] == v0 + 1,
          f"{v0} → {user_row(A)[2]}")
    winner = [f"Race-pass-{i}77" for i in range(5) if login(A, f"Race-pass-{i}77")]
    check("в силе ровно один из пяти паролей", len(winner) == 1, str(winner))
    STATE["pw"] = winner[0] if winner else STATE["pw"]


def atomic_rollback():
    print("\n== Сбой посреди смены пароля откатывает всё ==")
    clear_limit()
    t = issue(A)
    before = user_row(A)
    real = ar._apply_password

    def boom(*_a, **_k):
        raise RuntimeError("синтетический сбой записи пароля")

    ar._apply_password = boom
    db = SessionLocal()
    try:
        try:
            ar.consume_reset(db, t, auth.hash_password("Never-pass-888"))
            raised = False
        except RuntimeError:
            raised = True
    finally:
        ar._apply_password = real
        db.close()
    check("сбой всплыл, а не проглочен", raised)
    used = sql("SELECT used_at FROM password_resets WHERE token_hash=?",
               (hashlib.sha256(t.encode()).hexdigest(),))[0][0]
    check("отметка об использовании откатилась", used is None, str(used))
    check("пароль и версия сессии не тронуты", user_row(A) == before)
    r = reset(client(), t, "After-pass-999")
    check("та же ссылка после отката работает", r.status_code == 200, r.text[:100])
    STATE["pw"] = "After-pass-999"


def rate_limit():
    print("\n== Лимит по адресу ==")
    clear_limit()
    good = issue(A)
    c = client()
    codes = [reset(c, f"bad-{i}", "Limit-pass-101").status_code for i in range(10)]
    r = reset(c, good, "Limit-pass-101")
    check("десять плохих ссылок — ответ 400", codes == [400] * 10, str(codes))
    check("дальше адрес заперт: 429, ссылка даже не проверяется", r.status_code == 429,
          str(r.status_code))
    clear_limit()
    r = reset(c, good, "Limit-pass-101")
    check("после снятия блокировки та же ссылка рабочая", r.status_code == 200)
    STATE["pw"] = "Limit-pass-101"


def audit():
    print("\n== Журнал: кто, когда, исход — без секретов ==")
    rows = sql("SELECT e.kind, e.actor, e.detail_json FROM account_events e "
               "JOIN users u ON u.id=e.user_id WHERE u.email=? ORDER BY e.id", (A,))
    kinds = {k for k, _, _ in rows}
    check("есть контакт, перепроверка, выпуск, использование и отказ",
          {"contact_set", "contact_reverified", "reset_issued", "reset_consumed",
           "reset_rejected"} <= kinds, str(sorted(kinds)))
    blob = " ".join(d for _, _, d in rows)
    check("полного телефона в журнале нет", "+79994445566" not in blob
          and "79991112233" not in blob)
    check("ни токена, ни его хеша в журнале нет",
          not any(t in blob or hashlib.sha256(t.encode()).hexdigest() in blob for t in TOKENS))


def cli():
    print("\n== Инструмент оператора ==")
    tool = [sys.executable, str(ROOT / "tools" / "account_recovery.py"), "--db", str(DB_PATH)]
    run = lambda *a: subprocess.run(tool + list(a), capture_output=True, text=True,  # noqa: E731
                                    timeout=120)
    # Путь оператора по регламенту, шаг за шагом и тем же инструментом:
    # список (маска) → записанный номер для звонка → звонок → выпуск ссылки.
    r = run("contact-list", "--email", A)
    check("список контактов — только в маске", r.returncode == 0 and "+79994445566"
          not in r.stdout and "5566" in r.stdout, r.stdout.strip()[:160])
    r = run("callback-phone", "--email", A, "--operator", "т")
    check("номер для звонка без причины просмотра не выдаётся", r.returncode == 2)
    r = run("callback-phone", "--email", A, "--operator", "т", "--reason", "надо")
    check("и с отпиской вместо причины — тоже", r.returncode == 2 and "Причина" in r.stderr)
    r = run("callback-phone", "--email", B, "--operator", "т",
            "--reason", "просьба о сбросе из чата поддержки")
    check("без записанного телефона звонить некуда — отказ", r.returncode == 2)
    viewed_before = sql("SELECT COUNT(*) FROM account_events e JOIN users u ON u.id=e.user_id "
                        "WHERE u.email=? AND e.kind='callback_phone_viewed'", (A,))[0][0]
    r = run("callback-phone", "--email", A, "--operator", "т",
            "--reason", "просьба о сбросе из чата поддержки")
    lines = r.stdout.splitlines()
    check("оператор получает ТОЧНО записанный номер для звонка",
          r.returncode == 0 and "+79994445566" in lines, r.stdout.strip()[:160])
    check("и предупреждение звонить на него, а не на номер просьбы",
          "не на тот, с которого пришла просьба" in r.stdout)
    viewed = sql("SELECT e.actor, e.detail_json FROM account_events e JOIN users u "
                 "ON u.id=e.user_id WHERE u.email=? AND e.kind='callback_phone_viewed' "
                 "ORDER BY e.id", (A,))
    check("просмотр номера записан в журнал с оператором и причиной",
          len(viewed) == viewed_before + 1 and viewed[-1][0] == "т"
          and "просьба о сбросе" in viewed[-1][1], str(viewed[-1:])[:160])
    check("а самого номера в журнале просмотра нет — даже в маске",
          not any(d in viewed[-1][1] for d in ("79994445566", "5566", "+7")), viewed[-1][1][:160])
    r = run("issue", "--email", A, "--operator", "т", "--base-url", "https://app.example.ru")
    check("без --callback-confirmed не выпускает", r.returncode == 2)
    r = run("issue", "--email", A, "--operator", "т", "--base-url", "http://app.example.ru",
            "--callback-confirmed", "перезвонил на записанный номер")
    check("небезопасный адрес сервиса — отказ", r.returncode == 2 and "https" in r.stderr)
    r = run("issue", "--email", B, "--operator", "т", "--base-url", "https://app.example.ru",
            "--callback-confirmed", "перезвонил на записанный номер")
    check("без контактов — отказ с кодом 2", r.returncode == 2 and "Отказ" in r.stderr)
    r = run("issue", "--email", A, "--operator", "т", "--base-url", "https://app.example.ru",
            "--callback-confirmed", "перезвонил на записанный номер")
    link = [ln for ln in r.stdout.splitlines() if "/reset#t=" in ln]
    check("выпуск: ссылка напечатана один раз и назван Telegram-адресат",
          r.returncode == 0 and len(link) == 1 and "@owner_a_tg" in r.stdout, r.stderr[:160])
    if link:
        token = link[0].split("#t=", 1)[1].strip()
        TOKENS.append(token)
        clear_limit()
        rr = reset(client(), token, "Cli-pass-202")
        check("ссылка из инструмента работает", rr.status_code == 200, rr.text[:100])
        STATE["pw"] = "Cli-pass-202"


def browser_journey() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ПРОПУЩЕНО: playwright не установлен — браузерный путь не проверен")
        return False
    print("\n== Путь человека в браузере: 1400 и 390 ==")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            check("Chromium запускается", False, str(exc).splitlines()[0][:160])
            return True
        for width in (1400, 390):
            clear_limit()
            token = issue(A)
            new_pw = f"Browser-pass-{width}"
            ctx = browser.new_context(viewport={"width": width, "height": 900})
            page = ctx.new_page()
            reqs, errors = [], []
            page.on("request", lambda rq: reqs.append(
                (rq.url, rq.method, rq.post_data or "", rq.headers.get("referer", ""))))
            page.on("pageerror", lambda e: errors.append(str(e)))
            resp = page.goto(f"{BASE}/reset#t={token}")
            check(f"[{width}] страница отдаёт no-referrer и no-store",
                  resp.headers.get("referrer-policy") == "no-referrer"
                  and resp.headers.get("cache-control") == "no-store")
            page.wait_for_function("() => location.hash === ''", timeout=10000)
            check(f"[{width}] токен стёрт из адресной строки",
                  token not in page.url and "#" not in page.url, page.url)
            page.fill("#rs-new", new_pw)
            page.fill("#rs-confirm", new_pw)
            with page.expect_response(lambda r: r.url.endswith("/api/account/reset")) as done:
                page.click("#rs-submit")
            check(f"[{width}] сервер принял новый пароль", done.value.status == 200)
            page.wait_for_selector("#rs-ok", state="visible", timeout=10000)
            check(f"[{width}] человек видит, что пароль изменён",
                  "Пароль изменён" in (page.text_content("#rs-ok") or ""))
            check(f"[{width}] форма убрана, повторно отправить нечего",
                  not page.is_visible("#rs-form"))
            leaked = [u for u, _, _, ref in reqs if token in u or token in ref]
            check(f"[{width}] токена нет ни в одном адресе запроса и Referer", not leaked,
                  str(leaked)[:120])
            posted = [d for u, m, d, _ in reqs if m == "POST" and u.endswith("/api/account/reset")]
            check(f"[{width}] токен ушёл только в теле POST", len(posted) == 1
                  and token in posted[0])
            foreign = {u.split("/")[2] for u, _, _, _ in reqs} - {f"127.0.0.1:{APP_PORT}"}
            check(f"[{width}] сторонних ресурсов нет", not foreign, str(foreign))
            if width == 390:
                sw = page.evaluate("() => document.documentElement.scrollWidth")
                check("[390] без горизонтальной прокрутки", sw <= 391, str(sw))
            page.goto(f"{BASE}/login")
            page.fill("#email", A)
            page.fill("#password", new_pw)
            with page.expect_navigation():
                page.click("button[type=submit]")
            check(f"[{width}] вход новым паролем через форму", "/login" not in page.url,
                  page.url)
            check(f"[{width}] ошибок в консоли нет", not errors, str(errors[:2]))
            STATE["pw"] = new_pw
            ctx.close()
        browser.close()
    return True


def purge_and_rollback():
    print("\n== Удаление аккаунта стирает контакты; откат кода не воскрешает сессии ==")
    set_contact(B, "phone", "+7 999 777-88-99")
    set_contact(B, "telegram", "owner_b_tg")
    issue(B)
    b_id = user_row(B)[0]
    from app.main import _purge_user
    db = SessionLocal()
    try:
        _purge_user(db, b_id)
        db.commit()
    finally:
        db.close()
    left = sum(sql(f"SELECT COUNT(*) FROM {t} WHERE user_id=?", (b_id,))[0][0]
               for t in ("recovery_contacts", "password_resets", "account_events"))
    check("после удаления аккаунта его контакты, ссылки и журнал стёрты", left == 0, str(left))

    # Откат: предыдущая версия кода новых таблиц не знает. Снимаем их вовсе и
    # проверяем, что отзыв сессий и новый пароль держатся без них. Это НЕ прогон
    # старого кода: доказано лишь, что отзыв не зависит от новых таблиц; то, что
    # старый код его соблюдает, держится на неизменённой проверке версии в
    # auth.resolve_auth (этот пакет её не трогал).
    old = login(A, STATE["pw"])
    clear_limit()
    t = issue(A)
    reset(client(), t, "Rollback-pass-303")
    for table in ("recovery_contacts", "password_resets", "account_events"):
        sql(f"DROP TABLE {table}")
    check("без новых таблиц отозванная сессия остаётся отозванной", not alive(old))
    check("без новых таблиц новый пароль входит", login(A, "Rollback-pass-303") is not None)
    check("и старый — нет", login(A, STATE["pw"]) is None)


def no_token_in_logs():
    print("\n== Журналы приложения и доступа ==")
    access = [ln for ln in LOG_LINES if '"GET /reset' in ln or "/api/account/reset" in ln]
    check("журнал доступа действительно пишется (иначе проверка пустая)", bool(access),
          str(len(LOG_LINES)))
    hits = [ln for ln in LOG_LINES for t in TOKENS if t in ln]
    check(f"ни один из {len(TOKENS)} токенов не попал ни в одну запись журнала",
          not hits, str(hits[:1])[:160])


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    browser_ran = True
    try:
        register(A, "Бренд А")
        register(B, "Бренд Б")
        register(M, "Бренд М")
        sql("UPDATE users SET ms_uid='ms-synthetic-uid' WHERE email=?", (M,))
        policy()
        consume_http(token_storage())
        reuse_expiry_reissue(TOKENS[0])
        password_rules_and_csrf()
        readonly_gate()
        concurrency()
        atomic_rollback()
        rate_limit()
        cli()
        browser_ran = browser_journey()
        audit()
        purge_and_rollback()
        no_token_in_logs()
    except Exception as exc:  # noqa: BLE001 — падение обязано стать отчётом
        import traceback
        traceback.print_exc()
        check("сценарий дошёл до конца", False, f"{type(exc).__name__}: {exc}"[:200])
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        return 1
    return 0 if browser_ran else 77


if __name__ == "__main__":
    sys.exit(main())
