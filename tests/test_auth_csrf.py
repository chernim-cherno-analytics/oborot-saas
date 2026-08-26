# -*- coding: utf-8 -*-
"""CSRF-защита форм /login, /register, /logout (SEC-5).

Зачем отдельный файл. Остальные наборы шлют `X-Oborot-CSRF` глобально на
каждом `httpx.Client` — это машинный обход, допустимый ровно потому, что
кросс-доменная HTML-форма не может поставить кастомный заголовок (то же
свойство, на котором уже стоит защита `/api`, см. `test_isolation.py` §5).
Здесь проверяется ДРУГОЙ, реально работающий для форм механизм — подписанный
double-submit токен (кука + скрытое поле, `auth.get_csrf_token` /
`auth.verify_csrf_form`) — и именно его границу: скрытого поля мало, оно
обязано совпасть с кукой ЭТОГО ЖЕ браузера.

Проверяется:
  1) настоящий браузер (без заголовка-обхода) проходит /register, /logout и
     /login — токен и кука реально переносят форму, а не просто присутствуют
     в разметке;
  2) отсутствие токена целиком, только кука, только поле, несовпадение —
     отклонены ДО мутации (пользователь не создан);
  3) поле из ЧУЖОГО браузера не подходит к куке другого — своя пара
     кука+поле не переносима между сессиями;
  4) несколько вкладок / кнопка «назад»: кука не перевыпускается на каждый
     GET, поэтому скрытое поле первой загрузки остаётся рабочим;
  5) встроенный режим (iframe МойСклад, прод): CSRF-кука ставится
     SameSite=None + Secure — иначе браузер отбросил бы её в третьесторонней
     рамке, и легитимный logout сломался бы вместе с атакой;
  6) машинный обход `X-Oborot-CSRF` по-прежнему работает без какого-либо
     токена — остальные ~30 наборов не тронуты и не переписаны.

Запуск из корня репозитория:  python tests/test_auth_csrf.py
"""
import io
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_auth_csrf.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8815"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
# Не дев-дефолт: раздел 5 временно переключает OBOROT_ENV=prod, а crypto.py
# на проде требует секрет, отличный от дефолта (fail-fast). Секрет один и
# тот же на весь прогон — иначе сессия, выданная в dev-фазе, не прошла бы
# проверку подписи после переключения.
os.environ.setdefault("OBOROT_SECRET", "test-only-secret-not-the-dev-default-0123456789")

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app as oborot_app  # noqa: E402


class ServerThread:
    def __init__(self, asgi_app, port: int):
        self.config = uvicorn.Config(asgi_app, host="127.0.0.1", port=port,
                                     log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"сервер на порту {self.config.port} не поднялся")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]*)"')
COOKIE_VALUE_RE = re.compile(r'^oborot_csrf=([^;]*)')

# Реальные значения токенов/кук, сгенерированные сервером за время прогона.
# Нужны только для того, чтобы после теста доказать: ни одно из них не попало
# в захваченный stdout — см. раздел 7 ниже (SEC-5, corrective).
SECRET_VALUES = set()
MIN_LEAK_LEN = 8


def remember_secret(value: str) -> str:
    if value and len(value) >= MIN_LEAK_LEN:
        SECRET_VALUES.add(value)
    return value


def leaked_secrets(text: str, secrets) -> list:
    """Значения из secrets, чей префикс (или сами целиком) нашёлся в text."""
    return [s for s in secrets if s and len(s) >= MIN_LEAK_LEN and s[:MIN_LEAK_LEN] in text]


class _Tee:
    """Пишет одновременно в реальный stdout и в буфер для последующей проверки."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, s):
        for st in self._streams:
            st.write(s)
        return len(s)

    def flush(self):
        for st in self._streams:
            st.flush()


def extract_token(html: str) -> str:
    m = TOKEN_RE.search(html)
    return remember_secret(m.group(1) if m else "")


def cookie_attr_summary(header: str) -> str:
    """Только имена атрибутов Set-Cookie (path, samesite, secure…) — без значения куки."""
    if not header:
        return "cookie отсутствует"
    attrs = [part.strip().split("=", 1)[0].lower() for part in header.split(";")[1:]]
    return "атрибуты=" + ",".join(a for a in attrs if a)


def count_users() -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run()
    finally:
        srv.stop()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(DB_PATH) + suffix)
            if p.exists():
                p.unlink()


def run() -> int:
    base = f"http://127.0.0.1:{APP_PORT}"

    # Разделы 1–6 идут под перехватом stdout: реальный вывод не меняется
    # (пишем и в него тоже), но копия остаётся в captured — по ней ниже
    # (раздел 7) доказываем отсутствие утечки реальных значений токена/куки.
    real_stdout = sys.stdout
    captured = io.StringIO()
    sys.stdout = _Tee(real_stdout, captured)
    try:
        _run_sections_1_to_6(base)
    finally:
        sys.stdout = real_stdout
    captured_output = captured.getvalue()

    print("\n== 7. Контроль: захваченный вывод не содержит значений/префиксов реальных csrf-токенов и кук ==")
    leaks = leaked_secrets(captured_output, SECRET_VALUES)
    check("вывод разделов 1–6 не содержит фактические значения сгенерированных csrf-токенов/кук",
          not leaks, f"совпадений={len(leaks)}" if leaks else "")

    print("\n== 8. Контроль: детектор утечки действительно ловит старый формат диагностики (red control) ==")
    sample = next(iter(SECRET_VALUES), "")
    if sample:
        old_style_a_b = f"a={sample[:12]}… b={sample[:12]}…"
        check("детектор ловит префикс реального токена в формате старой диагностики (a=…/b=…, tab1=…/tab2=…)",
              bool(leaked_secrets(old_style_a_b, SECRET_VALUES)))
        old_style_cookie = f"set-cookie: oborot_csrf={sample}; Path=/; SameSite=None; Secure"
        check("детектор ловит значение куки в формате старой диагностики (полный Set-Cookie)",
              bool(leaked_secrets(old_style_cookie, SECRET_VALUES)))
    else:
        check("red control пропущен — не набралось реальных секретов для проверки детектора", False)
    check("детектор не ложно-срабатывает на нейтральный текст без токенов/кук",
          not leaked_secrets("status=403 no secret material here", SECRET_VALUES))

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


def _run_sections_1_to_6(base: str) -> None:
    print("\n== 1. Настоящий браузер (без заголовка-обхода) проходит формы ==")
    browser = httpx.Client(base_url=base, timeout=30.0)
    r = browser.get("/register")
    reg_token = extract_token(r.text)
    check("страница регистрации содержит скрытый csrf_token", bool(reg_token))
    check("кука oborot_csrf выставлена GET-ом", "oborot_csrf" in browser.cookies,
          f"cookies={sorted(browser.cookies.keys())}")

    r = browser.post("/register", data={
        "name": "Аня", "email": "browser-csrf@test.io", "password": "secret123",
        "org_name": "Бренд", "csrf_token": reg_token,
    })
    check("регистрация настоящей формой прошла", r.status_code in (302, 303),
          f"status={r.status_code} {r.text[:150]}")

    r = browser.get("/onboarding")
    check("после регистрации доступна авторизованная страница", r.status_code == 200,
          f"status={r.status_code}")
    logout_token = extract_token(r.text)
    check("на авторизованной странице тоже есть csrf_token (форма logout)", bool(logout_token))

    r = browser.post("/logout", data={"csrf_token": logout_token})
    check("logout настоящей формой прошёл", r.status_code in (302, 303), f"status={r.status_code}")
    r = browser.get("/onboarding", follow_redirects=False)
    check("сессия действительно снята — защищённая страница редиректит на /login",
          r.status_code == 302 and r.headers.get("location") == "/login", f"status={r.status_code}")

    print("\n== 1б. Настоящий браузер: вход по форме ==")
    r = browser.get("/login")
    login_token = extract_token(r.text)
    check("страница входа содержит скрытый csrf_token", bool(login_token))
    r = browser.post("/login", data={"email": "browser-csrf@test.io", "password": "secret123",
                                     "csrf_token": login_token})
    check("вход настоящей формой прошёл", r.status_code in (302, 303),
          f"status={r.status_code} {r.text[:150]}")
    r = browser.get("/onboarding")
    check("сессия после входа формой действительно установлена", r.status_code == 200,
          f"status={r.status_code}")
    browser.post("/logout", data={"csrf_token": extract_token(r.text)})
    browser.close()

    print("\n== 2. Границы: неполный/несовпадающий токен отклонён ДО мутации ==")
    before = count_users()
    attacker = httpx.Client(base_url=base, timeout=30.0)

    r = attacker.post("/register", data={
        "name": "Атака", "email": "cross-site-1@test.io", "password": "secret123", "org_name": "Чужой",
    })
    check("совсем без токена/куки (типичная кросс-сайтовая форма) — отклонено",
          r.status_code == 403, f"status={r.status_code}")
    check("пользователь не создан", count_users() == before, f"было={before} стало={count_users()}")

    ck_token = extract_token(attacker.get("/register").text)  # кука выставлена этим GET
    r = attacker.post("/register", data={
        "name": "Атака", "email": "cross-site-2@test.io", "password": "secret123", "org_name": "Чужой",
    })
    check("кука есть, поле формы отсутствует — отклонено", r.status_code == 403, f"status={r.status_code}")
    check("пользователь не создан", count_users() == before)

    del attacker.cookies["oborot_csrf"]
    r = attacker.post("/register", data={
        "name": "Атака", "email": "cross-site-3@test.io", "password": "secret123", "org_name": "Чужой",
        "csrf_token": ck_token,
    })
    check("поле есть (даже валидно подписанное), кука отсутствует — отклонено",
          r.status_code == 403, f"status={r.status_code}")
    check("пользователь не создан", count_users() == before)

    real_token = extract_token(attacker.get("/register").text)
    r = attacker.post("/register", data={
        "name": "Атака", "email": "cross-site-4@test.io", "password": "secret123", "org_name": "Чужой",
        "csrf_token": real_token + "x",
    })
    check("кука и поле не совпадают — отклонено", r.status_code == 403, f"status={r.status_code}")
    check("пользователь не создан", count_users() == before)
    attacker.close()

    print("\n== 3. Токен одного браузера не подходит к куке другого ==")
    a = httpx.Client(base_url=base, timeout=30.0)
    b = httpx.Client(base_url=base, timeout=30.0)
    token_a = extract_token(a.get("/register").text)
    token_b = extract_token(b.get("/register").text)
    check("у двух независимых браузеров разные токены", bool(token_a) and bool(token_b) and token_a != token_b)
    r = b.post("/register", data={
        "name": "Б", "email": "cross-browser@test.io", "password": "secret123", "org_name": "Б",
        "csrf_token": token_a,
    })
    check("поле ИЗ ДРУГОГО браузера с валидной кукой этого браузера — всё равно отклонено",
          r.status_code == 403, f"status={r.status_code}")
    a.close()
    b.close()

    print("\n== 4. Несколько вкладок / кнопка «назад»: токен не перевыпускается ==")
    tabs = httpx.Client(base_url=base, timeout=30.0)
    token_tab1 = extract_token(tabs.get("/register").text)
    token_tab2 = extract_token(tabs.get("/register").text)  # «вторая вкладка», тот же браузер
    check("повторный GET не меняет токен (иначе первая вкладка / back-button сломались бы)",
          bool(token_tab1) and token_tab1 == token_tab2)
    r = tabs.post("/register", data={
        "name": "Вкладка", "email": "multi-tab@test.io", "password": "secret123", "org_name": "Т",
        "csrf_token": token_tab1,  # поле «застрявшей» после back-button первой загрузки страницы
    })
    check("отправка токеном первой загрузки (имитация back-button) прошла",
          r.status_code in (302, 303), f"status={r.status_code}")
    tabs.close()

    print("\n== 5. Встроенный режим (iframe МойСклад, прод): SameSite=None + Secure ==")
    embed = httpx.Client(base_url=base, timeout=30.0)
    embed.post("/register", data={
        "name": "Встр", "email": "embed-csrf@test.io", "password": "secret123", "org_name": "В",
        "csrf_token": extract_token(embed.get("/register").text),
    })
    # dev: ?embed=1 залипает кукой oborot_embed на будущее — как настоящий вход через /ms/app.
    embed.get("/onboarding?embed=1")
    prev_env = os.environ.get("OBOROT_ENV")
    try:
        os.environ["OBOROT_ENV"] = "prod"
        r = embed.get("/onboarding")
        set_cookie_headers = [v for k, v in r.headers.raw if k.lower() == b"set-cookie"]
        set_cookie_headers = [v.decode("latin-1") for v in set_cookie_headers]
        csrf_cookie_header = next((h for h in set_cookie_headers if h.startswith("oborot_csrf=")), "")
        cookie_value_match = COOKIE_VALUE_RE.match(csrf_cookie_header)
        remember_secret(cookie_value_match.group(1) if cookie_value_match else "")
        check("во встроенном режиме на проде CSRF-кука ставится SameSite=None",
              "samesite=none" in csrf_cookie_header.lower(), cookie_attr_summary(csrf_cookie_header))
        check("во встроенном режиме на проде CSRF-кука ставится Secure",
              "secure" in csrf_cookie_header.lower(), cookie_attr_summary(csrf_cookie_header))
    finally:
        if prev_env is None:
            os.environ.pop("OBOROT_ENV", None)
        else:
            os.environ["OBOROT_ENV"] = prev_env
    embed.close()

    print("\n== 6. Машинный обход X-Oborot-CSRF по-прежнему работает (совместимость наборов) ==")
    machine = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=30.0)
    r = machine.post("/register", data={
        "name": "Машина", "email": "machine-csrf@test.io", "password": "secret123", "org_name": "М",
    })
    check("машинный клиент без какого-либо csrf-токена проходит по заголовку",
          r.status_code in (302, 303), f"status={r.status_code}")
    machine.close()


if __name__ == "__main__":
    sys.exit(main())
