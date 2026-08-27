# -*- coding: utf-8 -*-
"""Тест блока «доверие и деньги»: пароль, удаление аккаунта, тарифы, свежесть.

Сценарий (без pytest, как в соседних тестах):
  1) поднимаем приложение на своей БД (мок-МойСклад не нужен: данные берём
     из демо-набора, /api/connect/demo);
  2) смена пароля: неверный текущий, несовпадение, короткий, длинный
     кириллический (72 БАЙТА — предел bcrypt), успех, вход старым и новым;
  3) удаление аккаунта: подтверждение паролем и словом «УДАЛИТЬ», три случая —
     участник, владелец с передачей организации коллеге, владелец с полным
     удалением организации (включая аккаунты коллег без других организаций);
  4) после полного удаления в БД не остаётся НИ ОДНОЙ строки организации —
     в том числе подключения с зашифрованным токеном МойСклада;
  5) тарифы: /api/plans (единый источник PLANS), заявка на счёт и её проверки;
  6) /api/freshness: поле connected до и после подключения демо-данных;
  7) лимит попыток входа: ключ по аккаунту не обходится заголовком
     X-Forwarded-For, аккаунты не мешают друг другу, память лимитера не растёт,
     блокировка снимается сама и объясняет человеку, сколько ждать.

Запуск из корня репозитория:  python tests/test_account.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_account.db"
# Порты берутся из окружения: так tests/run_all.py разводит наборы и
# может гонять их параллельно. Значения по умолчанию — прежние.
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8804"))

# Окружение — ДО импорта приложения (db.py читает DATABASE_URL при импорте).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app import auth  # noqa: E402
from app.crypto import encrypt_token  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.routes_extra import PLANS  # noqa: E402


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


BASE = f"http://127.0.0.1:{APP_PORT}"


def client() -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=BASE, timeout=120.0)


def register(c: httpx.Client, email: str, org_name: str, password: str = "secret123"):
    return c.post("/register", data={
        "name": email.split("@")[0], "email": email,
        "password": password, "org_name": org_name,
    })


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def exec_sql(query: str, *args) -> int:
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(query, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def add_member(org_id: int, email: str, name: str) -> int:
    """Сотрудник организации: приглашений в UI ещё нет, заводим строкой в БД."""
    import bcrypt

    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
    uid = exec_sql(
        "INSERT INTO users (email, pw_hash, name, created_at) VALUES (?, ?, ?, datetime('now'))",
        email, pw, name,
    )
    exec_sql("INSERT INTO memberships (user_id, org_id, role) VALUES (?, ?, 'member')", uid, org_id)
    return uid


def org_of(email: str) -> int:
    return sql(
        "SELECT m.org_id FROM memberships m JOIN users u ON u.id = m.user_id WHERE u.email = ?",
        email,
    )[0][0]


def clear_limiters() -> None:
    """Сбрасывает оба лимитера login полностью: и скользящий счётчик, и
    фиксированную блокировку (auth.LoginLimiter хранит их в двух разных
    словарях — очистить только `_hits` мало, «забытая» блокировка переживёт
    сброс и заденет следующий сценарий)."""
    for limiter in (auth.login_limiter, auth.ip_login_limiter):
        limiter._hits.clear()
        limiter._locked_until.clear()


def org_rows(org_id: int) -> dict[str, int]:
    """Сколько строк осталось у организации во ВСЕХ таблицах с org_id."""
    con = sqlite3.connect(DB_PATH)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        out = {}
        for t in tables:
            cols = [c[1] for c in con.execute(f"PRAGMA table_info({t})").fetchall()]
            if "org_id" in cols:
                n = con.execute(f"SELECT count(*) FROM {t} WHERE org_id = ?", (org_id,)).fetchone()[0]
                if n:
                    out[t] = n
        n = con.execute("SELECT count(*) FROM orgs WHERE id = ?", (org_id,)).fetchone()[0]
        if n:
            out["orgs"] = n
        return out
    finally:
        con.close()


def cookie_client(payload: dict) -> httpx.Client:
    """Клиент с вручную выставленной сессионной кукой (для legacy/битых версий)."""
    raw = auth._serializer().dumps(payload)
    cl = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=BASE, timeout=30.0)
    cl.cookies.set(auth.SESSION_COOKIE, raw)
    return cl


def cookie_client_raw(raw: str) -> httpx.Client:
    """Клиент с вручную выставленной СЫРОЙ (уже подписанной) сессионной кукой."""
    cl = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=BASE, timeout=30.0)
    cl.cookies.set(auth.SESSION_COOKIE, raw)
    return cl


def cookie_version(resp: httpx.Response):
    """Версия (поле 'v') из Set-Cookie ответа; None, если куки не было."""
    raw = resp.cookies.get(auth.SESSION_COOKIE)
    if not raw:
        return None
    return auth._serializer().loads(raw).get("v", 0)


def run_concurrent_password_changes(clients_and_passwords):
    """Гоняет несколько POST /api/account/password ПОДЛИННО параллельно.

    Тестовый барьер (НЕ production sleep/hook): monkeypatch'ит auth.hash_password
    так, что все параллельные запросы гарантированно оказываются внутри
    обработчика ОДНОВРЕМЕННО, непосредственно перед записью в БД. Без барьера
    гонка была бы вероятностной — можно было бы годами не поймать её на быстрой
    машине. Патч живёт ровно на время этого вызова и снимается в finally.
    """
    n = len(clients_and_passwords)
    barrier = threading.Barrier(n)
    orig_hash = auth.hash_password

    def _barrier_hash(password):
        barrier.wait(timeout=10)
        return orig_hash(password)

    results = [None] * n

    def _worker(i, cl, new_pw):
        results[i] = cl.post("/api/account/password", json={
            "current_password": "secret123", "new_password": new_pw, "confirm_password": new_pw,
        })

    auth.hash_password = _barrier_hash
    try:
        threads = [
            threading.Thread(target=_worker, args=(i, cl, pw))
            for i, (cl, pw) in enumerate(clients_and_passwords)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
    finally:
        auth.hash_password = orig_hash
    return results


def patch_session_version_seeds(sequence):
    """Test-only: подставляет ФИКСИРОВАННУЮ последовательность вместо
    auth.new_session_version_seed, чтобы delete→register тест доказывал
    отсутствие коллизии версий ДЕТЕРМИНИРОВАННО, а не полагался на то, что
    два независимых случайных сида не совпадут (вероятность коллизии мала,
    но не ноль — «детерминированный» тест не имеет права полагаться на неё).

    Патчит, только если helper вообще существует: на BASE-коммите до SEC-3
    corrective (166c725) его ещё нет, и тогда патч не устанавливается вовсе
    — там новый пользователь получает version=0 прямо из server_default
    модели (без какой-либо случайности), и исходный delete→register дефект
    воспроизводится как и раньше, RED на том коммите не теряется.

    Возвращает (restore, patched): restore — функция отката, ОБЯЗАНА быть
    вызвана в finally; patched — True, если подмена реально произошла (это
    определяет, какого рода assert имеет смысл делать дальше — точное
    зафиксированное значение или общий факт «есть версия, не 0»).
    """
    if not hasattr(auth, "new_session_version_seed"):
        return (lambda: None), False
    orig = auth.new_session_version_seed
    it = iter(sequence)
    auth.new_session_version_seed = lambda: next(it)

    def _restore():
        auth.new_session_version_seed = orig

    return _restore, True


def main() -> int:
    srv = ServerThread(oborot_app, APP_PORT)
    srv.start()
    try:
        return run_scenario()
    finally:
        srv.stop()


def run_scenario() -> int:
    print("== Свежесть данных: connected ==")
    c = client()
    register(c, "fresh@test.io", "Свежий бренд")
    j = c.get("/api/freshness").json()
    check("без подключения connected=False и даты пустые",
          j.get("connected") is False and j.get("last_sale_date") is None
          and j.get("last_stock_date") is None, f"resp={j}")
    check("демо-данные посеяны", c.post("/api/connect/demo").status_code == 200)
    j = c.get("/api/freshness").json()
    check("после демо connected=True, даты заполнены",
          j.get("connected") is True and j.get("last_sale_date")
          and j.get("last_stock_date"), f"resp={j}")
    check("остальные поля ответа не потерялись",
          {"sync_state", "sync_error", "sync_finished_at"} <= set(j), f"keys={sorted(j)}")

    print("== Смена пароля ==")
    c = client()
    register(c, "pw@test.io", "Пароль-бренд")

    def change(cur, new, conf=None):
        r = c.post("/api/account/password", json={
            "current_password": cur, "new_password": new,
            "confirm_password": conf if conf is not None else new,
        })
        return r.status_code, (r.json().get("detail", "") if r.status_code >= 400 else "")

    st, msg = change("не-тот-пароль", "новыйпароль1")
    check("неверный текущий пароль — 403 и человеческий текст",
          st == 403 and "не подошёл" in msg, f"{st} {msg[:60]}")
    st, msg = change("secret123", "новыйпароль1", "другойпароль")
    check("новый и подтверждение не совпали — 422", st == 422 and "не совпадают" in msg,
          f"{st} {msg[:60]}")
    st, msg = change("secret123", "1234567")
    check("короткий пароль — 422", st == 422 and "8 символов" in msg, f"{st} {msg[:60]}")
    long_ru = "я" * 40  # 80 байт — bcrypt столько не хеширует
    check("длинный кириллический пароль действительно длиннее 72 байт",
          len(long_ru.encode("utf-8")) == 80)
    st, msg = change("secret123", long_ru)
    check("длинный кириллический — 422 с объяснением про байты (не 500)",
          st == 422 and "72 байта" in msg, f"{st} {msg[:80]}")
    st, msg = change("secret123", "secret123")
    check("новый пароль совпадает со старым — 422", st == 422 and "совпадает" in msg,
          f"{st} {msg[:60]}")
    st, _ = change("secret123", "новыйпароль1")
    check("успешная смена пароля — 200", st == 200, f"status={st}")
    check("текущая сессия осталась рабочей", c.get("/api/account").status_code == 200)

    c_old = client()
    r = c_old.post("/login", data={"email": "pw@test.io", "password": "secret123"})
    check("старый пароль больше не пускает", r.status_code == 200 and "Неверный" in r.text)
    c_new = client()
    r = c_new.post("/login", data={"email": "pw@test.io", "password": "новыйпароль1"},
                   follow_redirects=False)
    check("новый пароль пускает", r.status_code == 303, f"status={r.status_code}")

    r = httpx.post(f"{BASE}/api/account/password", json={}, timeout=30.0)
    check("смена пароля защищена от CSRF (нет заголовка — 403)", r.status_code == 403,
          f"status={r.status_code}")

    print("== Гашение сессий после смены пароля (SEC-3) ==")
    c1 = client()
    register(c1, "sec3@test.io", "Сек-бренд")
    c2 = client()
    r = c2.post("/login", data={"email": "sec3@test.io", "password": "secret123"})
    check("второе устройство вошло тем же паролем", r.status_code == 303, f"status={r.status_code}")
    check("оба устройства видят защищённые данные до смены пароля",
          c1.get("/api/account").status_code == 200 and c2.get("/api/account").status_code == 200)

    c3 = client()
    register(c3, "sec3-other@test.io", "Другой-бренд")
    check("сторонний пользователь тоже авторизован", c3.get("/api/account").status_code == 200)

    r = c1.post("/api/account/password", json={
        "current_password": "secret123", "new_password": "новыйпароль-sec3",
        "confirm_password": "новыйпароль-sec3",
    })
    check("смена пароля с первого устройства — 200", r.status_code == 200, f"status={r.status_code}")

    check("первое устройство (сменившее пароль) осталось авторизовано",
          c1.get("/api/account").status_code == 200)

    r2 = c2.get("/api/account")
    check("второе устройство отозвано сразу после смены пароля (401)",
          r2.status_code == 401, f"status={r2.status_code}")

    r2_page = c2.get("/account", follow_redirects=False)
    check("второе устройство на HTML-странице получает редирект на вход",
          r2_page.status_code == 302 and r2_page.headers.get("location") == "/login",
          f"status={r2_page.status_code} loc={r2_page.headers.get('location')}")

    check("сторонний пользователь не задет чужой сменой пароля",
          c3.get("/api/account").status_code == 200)

    c_old = client()
    r = c_old.post("/login", data={"email": "sec3@test.io", "password": "secret123"})
    check("старый пароль после смены больше не пускает (SEC-3)",
          r.status_code == 200 and "Неверный" in r.text)

    c_new = client()
    r = c_new.post("/login", data={"email": "sec3@test.io", "password": "новыйпароль-sec3"},
                   follow_redirects=False)
    check("новый пароль пускает", r.status_code == 303, f"status={r.status_code}")
    check("вход новым паролем даёт рабочую сессию", c_new.get("/api/account").status_code == 200)

    print("== Кука без версии (пред-миграционный формат) и битая версия (SEC-3) ==")

    c_legacy = client()
    register(c_legacy, "sec3-legacy@test.io", "Легаси-бренд")
    legacy_uid = sql("SELECT id FROM users WHERE email = ?", "sec3-legacy@test.io")[0][0]
    legacy_org = org_of("sec3-legacy@test.io")
    # Свежая регистрация теперь сама минтит случайный положительный сид
    # (SEC-3 corrective #2, см. auth.new_session_version_seed) — она больше не
    # представляет «старую» строку. Довыпущенная/мигрированная строка
    # получает version=0 из ALTER'а с DEFAULT 0 (models._ensure_users_session_version),
    # а не из этого пути создания пользователя — сводим строку к такому виду
    # руками, чтобы честно проверить именно легаси-сценарий.
    exec_sql("UPDATE users SET session_version = 0 WHERE id = ?", legacy_uid)
    # c_legacy сам получил куку со случайным сидом при регистрации — без
    # переиздания она разойдётся с только что подставленной DB version=0.
    c_legacy.cookies.set(auth.SESSION_COOKIE,
                          auth._serializer().dumps({"user_id": legacy_uid, "org_id": legacy_org, "v": 0}))

    legacy_cookie_client = cookie_client({"user_id": legacy_uid, "org_id": legacy_org})
    check("кука без поля версии (довыпущенный формат) работает при DB version 0",
          legacy_cookie_client.get("/api/account").status_code == 200)

    r = c_legacy.post("/api/account/password", json={
        "current_password": "secret123", "new_password": "легаси-новый-пароль",
        "confirm_password": "легаси-новый-пароль",
    })
    check("смена пароля у легаси-пользователя — 200", r.status_code == 200, f"status={r.status_code}")

    check("та же кука без версии отозвана после первого инкремента версии",
          legacy_cookie_client.get("/api/account").status_code == 401)

    bad_str = cookie_client({"user_id": legacy_uid, "org_id": legacy_org, "v": "1"})
    check("нечисловая (строковая) версия в куке — fail-closed 401",
          bad_str.get("/api/account").status_code == 401)

    bad_float = cookie_client({"user_id": legacy_uid, "org_id": legacy_org, "v": 1.0})
    check("дробная версия в куке — fail-closed 401",
          bad_float.get("/api/account").status_code == 401)

    bad_bool = cookie_client({"user_id": legacy_uid, "org_id": legacy_org, "v": True})
    check("булева версия в куке — fail-closed 401",
          bad_bool.get("/api/account").status_code == 401)

    print("== UI /account больше не обещает 7 дней на других устройствах (SEC-3) ==")
    acc_html = c_new.get("/account").text
    check("страница «Аккаунт» не утверждает, что чужой вход держится до 7 дней",
          "до 7 дней" not in acc_html, "текст с ложным сроком всё ещё в разметке")

    print("== Аддитивная миграция users.session_version (SEC-3) ==")
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy import inspect as _inspect
    from sqlalchemy import text as _text

    from app import models as _models

    old_db = Path(tempfile.mkdtemp()) / "old_users_schema.db"
    old_engine = _create_engine(f"sqlite:///{old_db}", future=True,
                                connect_args={"check_same_thread": False})
    with old_engine.begin() as conn:
        conn.execute(_text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL, "
            "pw_hash VARCHAR(255) NOT NULL, name VARCHAR(255) NOT NULL, created_at DATETIME)"))
        conn.execute(_text(
            "INSERT INTO users (id, email, pw_hash, name) VALUES (1, 'old@test.io', 'x', 'Старый')"))
    _models.ensure_schema(bind=old_engine)
    cols = {c["name"] for c in _inspect(old_engine).get_columns("users")}
    check("миграция добавила session_version в существующую таблицу users",
          "session_version" in cols, f"cols={sorted(cols)}")
    with old_engine.begin() as conn:
        row = conn.execute(_text(
            "SELECT email, session_version FROM users WHERE id = 1")).first()
    check("старый пользователь уцелел, session_version по умолчанию = 0",
          tuple(row) == ("old@test.io", 0), f"row={tuple(row) if row else None}")
    _models.ensure_schema(bind=old_engine)  # повторный прогон — идемпотентно
    with old_engine.begin() as conn:
        again = conn.execute(_text("SELECT session_version FROM users WHERE id = 1")).scalar()
    check("повторный прогон миграции не меняет значение", again == 0, f"session_version={again}")
    old_engine.dispose()

    # Конкурентный старт нескольких воркеров: миграция не должна падать и не
    # должна добавить колонку дважды.
    old_db2 = Path(tempfile.mkdtemp()) / "old_users_schema_concurrent.db"
    old_engine2 = _create_engine(f"sqlite:///{old_db2}", future=True,
                                 connect_args={"check_same_thread": False})
    with old_engine2.begin() as conn:
        conn.execute(_text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL, "
            "pw_hash VARCHAR(255) NOT NULL, name VARCHAR(255) NOT NULL, created_at DATETIME)"))
    errors = []

    def _run_migration():
        try:
            _models.ensure_schema(bind=old_engine2)
        except Exception as exc:  # noqa: BLE001 — конкурентная гонка не должна ронять воркер
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: _run_migration(), range(6)))
    check("конкурентный запуск миграции на нескольких воркерах не падает",
          not errors, f"errors={errors}")
    cols2 = [c["name"] for c in _inspect(old_engine2).get_columns("users")]
    check("конкурентная миграция добавила колонку ровно один раз (без дублей)",
          cols2.count("session_version") == 1, f"cols={cols2}")
    old_engine2.dispose()

    print("== Границы сида версии сессии не переполняют Postgres INTEGER (SEC-3 corrective #3) ==")
    if not hasattr(auth, "new_session_version_seed"):
        # BASE-коммит до SEC-3 corrective (166c725) — хелпера ещё нет, сама
        # проверка неприменима. Не падаем: остальной сценарий (в частности,
        # delete→register ниже) обязан продолжить выполняться и честно
        # показать RED старого дефекта, а не потеряться в трейсбеке отсюда.
        print("  SKIP auth.new_session_version_seed отсутствует на этом коммите — проверка неприменима")
    else:
        # Postgres INTEGER — знаковый 32-битный, потолок 2**31-1. Смена пароля
        # инкрементирует session_version (см. api_change_password), поэтому сид
        # обязан оставлять существенный гарантированный запас инкрементов ДО
        # этого потолка, а не только не совпадать с ним в моменте выдачи.
        _PG_INT32_MAX = 2**31 - 1
        seed_samples = [auth.new_session_version_seed() for _ in range(3000)]
        check("сид всегда положительный (никогда не 0 — легаси-версия зарезервирована за миграцией)",
              min(seed_samples) >= 1, f"min={min(seed_samples)}")
        check("сид никогда не достигает потолка Postgres INTEGER",
              max(seed_samples) < _PG_INT32_MAX, f"max={max(seed_samples)}")
        check("верхняя граница диапазона сида (auth._SESSION_VERSION_SEED_MAX) оставляет "
              "не менее 2**30 гарантированных инкрементов до потолка Postgres INTEGER",
              _PG_INT32_MAX - auth._SESSION_VERSION_SEED_MAX >= 2**30,
              f"headroom={_PG_INT32_MAX - auth._SESSION_VERSION_SEED_MAX}")
        check("ни один сэмпл не вышел за задокументированную границу диапазона",
              max(seed_samples) <= auth._SESSION_VERSION_SEED_MAX,
              f"max={max(seed_samples)} bound={auth._SESSION_VERSION_SEED_MAX}")

    print("== Гонка: два одновременных POST /api/account/password (SEC-3 corrective #1) ==")
    c_race_a = client()
    register(c_race_a, "sec3-race@test.io", "Гонка-бренд")
    c_race_b = client()
    r = c_race_b.post("/login", data={"email": "sec3-race@test.io", "password": "secret123"})
    check("второй клиент гонки вошёл тем же паролем", r.status_code == 303, f"status={r.status_code}")
    race_uid = sql("SELECT id FROM users WHERE email = ?", "sec3-race@test.io")[0][0]

    r_a, r_b = run_concurrent_password_changes([
        (c_race_a, "гонка-пароль-a1"),
        (c_race_b, "гонка-пароль-b1"),
    ])
    check("оба параллельных запроса на смену пароля успешны (200) — гонка не должна превращаться в 409",
          r_a.status_code == 200 and r_b.status_code == 200,
          f"a={r_a.status_code} b={r_b.status_code}")

    va, vb = cookie_version(r_a), cookie_version(r_b)
    check("версии в куках двух параллельных успешных ответов различны (не потерянный инкремент)",
          va is not None and vb is not None and va != vb, f"va={va} vb={vb}")

    db_version = sql("SELECT session_version FROM users WHERE id = ?", race_uid)[0][0]
    check("итоговая версия в БД равна максимуму из двух полученных версий",
          va is not None and vb is not None and db_version == max(va, vb),
          f"db={db_version} va={va} vb={vb}")

    resp_a2 = c_race_a.get("/api/account")
    resp_b2 = c_race_b.get("/api/account")
    check("не обе куки-версии остаются рабочими одновременно после гонки",
          not (resp_a2.status_code == 200 and resp_b2.status_code == 200),
          f"a2={resp_a2.status_code} b2={resp_b2.status_code}")
    if va is not None and vb is not None:
        newer_resp, older_resp = (resp_a2, resp_b2) if va > vb else (resp_b2, resp_a2)
        check("клиент с последней зафиксированной версией куки остался авторизован",
              newer_resp.status_code == 200, f"status={newer_resp.status_code}")
        check("клиент со старой (перебитой гонкой) версией куки отклонён (401)",
              older_resp.status_code == 401, f"status={older_resp.status_code}")

    print("== Delete → register: переиспользованный id не воскрешает старую куку (SEC-3 corrective #2) ==")
    # Фиксируем сиды ДЕТЕРМИНИРОВАННО (см. patch_session_version_seeds) —
    # тест не должен полагаться на то, что два независимых случайных сида
    # просто не совпадут. На BASE-коммите до 166c725 хелпера ещё нет,
    # seeds_patched будет False, и сценарий честно идёт по старому пути
    # (version=0 из server_default для обоих пользователей).
    restore_seeds, seeds_patched = patch_session_version_seeds([424242, 555555])
    try:
        c_reuse = client()
        register(c_reuse, "sec3-reuse@test.io", "Реюз-бренд")
        reuse_uid1 = sql("SELECT id FROM users WHERE email = ?", "sec3-reuse@test.io")[0][0]
        old_cookie_raw = c_reuse.cookies.get(auth.SESSION_COOKIE)
        check("кука первого владельца слота захвачена для дальнейшей проверки", bool(old_cookie_raw))

        r = c_reuse.post("/api/account/password", json={
            "current_password": "secret123", "new_password": "реюз-новый-пароль",
            "confirm_password": "реюз-новый-пароль",
        })
        check("смена пароля перед удалением — 200", r.status_code == 200, f"status={r.status_code}")

        old_cookie_client = cookie_client_raw(old_cookie_raw)
        check("старая (дореволюционная) кука отозвана сразу после смены пароля",
              old_cookie_client.get("/api/account").status_code == 401)

        r = c_reuse.post("/api/account/delete", json={"password": "реюз-новый-пароль", "confirm": "УДАЛИТЬ"})
        check("владелец-одиночка удалил аккаунт и организацию целиком — 200",
              r.status_code == 200 and r.json()["scope"] == "org", f"resp={r.text[:140]}")
        check("строка пользователя удалена из БД",
              not sql("SELECT id FROM users WHERE id = ?", reuse_uid1))

        c_reuse2 = client()
        r = register(c_reuse2, "sec3-reuse@test.io", "Реюз-бренд-2")
        check("тот же e-mail снова доступен для регистрации после удаления — 303",
              r.status_code == 303, f"status={r.status_code}")

        reuse_uid2 = sql("SELECT id FROM users WHERE email = ?", "sec3-reuse@test.io")[0][0]
        check("id действительно переиспользован (SQLite: max(id)+1 после удаления последней строки)",
              reuse_uid2 == reuse_uid1, f"old={reuse_uid1} new={reuse_uid2}")

        new_seed = sql("SELECT session_version FROM users WHERE id = ?", reuse_uid2)[0][0]
        if seeds_patched:
            check("новый пользователь на переиспользованном id получил ВТОРОЙ зафиксированный "
                  "тестом сид (детерминированно, не унаследовал версию первого)",
                  new_seed == 555555, f"session_version={new_seed}")
        else:
            check("новый пользователь на переиспользованном id получил положительную версию, не 0",
                  new_seed > 0, f"session_version={new_seed}")

        resurrect_client = cookie_client_raw(old_cookie_raw)
        resurrect_status = resurrect_client.get("/api/account").status_code
        check("старая кука (тот же user_id, ПЕРВЫЙ сид) НЕ воскрешает доступ к новому пользователю "
              "на том же id (ВТОРОЙ сид, гарантированно другой, а не просто «не совпал в этот раз»)",
              resurrect_status == 401, f"status={resurrect_status}")
        check("новый владелец слота нормально авторизован собственной свежей сессией",
              c_reuse2.get("/api/account").status_code == 200)
    finally:
        restore_seeds()

    print("== Удаление: участник организации ==")
    owner = client()
    register(owner, "boss@test.io", "Ателье Шов")
    boss_org = org_of("boss@test.io")
    add_member(boss_org, "lena@test.io", "Лена")
    lena = client()
    lena.post("/login", data={"email": "lena@test.io", "password": "secret123"})
    j = lena.get("/api/account").json()
    check("участник видит свою роль и владельца в списке",
          j["role"] == "member" and [o["email"] for o in j["others"]] == ["boss@test.io"],
          f"resp={j.get('role')} {j.get('others')}")
    r = lena.post("/api/account/delete", json={"password": "secret123", "confirm": "да"})
    check("без слова-подтверждения удаления не происходит (422)",
          r.status_code == 422 and "УДАЛИТЬ" in r.json()["detail"], f"status={r.status_code}")
    r = lena.post("/api/account/delete", json={"password": "чужой", "confirm": "УДАЛИТЬ"})
    check("с неверным паролем удаления не происходит (403)", r.status_code == 403,
          f"status={r.status_code}")
    r = lena.post("/api/account/delete", json={"password": "secret123", "confirm": " удалить "})
    check("участник удалил себя (слово принимается без учёта регистра)",
          r.status_code == 200 and r.json()["scope"] == "account", f"resp={r.text[:120]}")
    check("аккаунт участника исчез", not sql("SELECT id FROM users WHERE email = ?", "lena@test.io"))
    check("сессия удалённого участника больше не работает",
          lena.get("/api/account").status_code == 401)
    check("организация и её данные на месте",
          sql("SELECT count(*) FROM orgs WHERE id = ?", boss_org)[0][0] == 1
          and owner.get("/api/account").status_code == 200)

    print("== Удаление: владелец передаёт организацию коллеге ==")
    add_member(boss_org, "vova@test.io", "Вова")
    r = owner.post("/api/account/delete", json={"password": "secret123", "confirm": "УДАЛИТЬ"})
    check("владелец с сотрудниками обязан выбрать судьбу организации (422)",
          r.status_code == 422 and "участники" in r.json()["detail"], f"status={r.status_code}")
    r = owner.post("/api/account/delete", json={
        "password": "secret123", "confirm": "УДАЛИТЬ",
        "mode": "transfer", "transfer_to": "чужой@test.io"})
    check("передать организацию постороннему нельзя (422)", r.status_code == 422,
          f"status={r.status_code}")
    r = owner.post("/api/account/delete", json={
        "password": "secret123", "confirm": "УДАЛИТЬ",
        "mode": "transfer", "transfer_to": "vova@test.io"})
    check("владелец удалился, передав организацию", r.status_code == 200
          and r.json()["scope"] == "account", f"resp={r.text[:120]}")
    check("коллега стал владельцем",
          sql("SELECT m.role FROM memberships m JOIN users u ON u.id = m.user_id "
              "WHERE u.email = ?", "vova@test.io") == [("owner",)])
    check("данные организации не тронуты",
          sql("SELECT count(*) FROM orgs WHERE id = ?", boss_org)[0][0] == 1)

    print("== Удаление: владелец сносит организацию целиком ==")
    big = client()
    register(big, "big@test.io", "Большой бренд")
    big_org = org_of("big@test.io")
    check("демо-данные посеяны для организации", big.post("/api/connect/demo").status_code == 200)
    # Токен МойСклада: подключение через UI требует живого МС, поэтому кладём
    # шифртекст напрямую — проверяем, что удаление его действительно стирает.
    exec_sql("INSERT INTO connections (org_id, kind, token_enc, status, config_json) "
             "VALUES (?, 'moysklad', ?, 'active', '{}')", big_org, encrypt_token("MS-SECRET"))
    sonya_id = add_member(big_org, "sonya@test.io", "Соня")
    dual_id = add_member(big_org, "dual@test.io", "Двойной")
    # «Двойной» состоит ещё в одной организации — его аккаунт трогать нельзя.
    exec_sql("INSERT INTO memberships (user_id, org_id, role) VALUES (?, ?, 'member')",
             dual_id, boss_org)
    big_id = sql("SELECT id FROM users WHERE email = ?", "big@test.io")[0][0]

    # Реальные строки в таблицах, которых в этом сценарии раньше не было.
    # Проверка «в БД не осталось ни одной строки организации» смотрит только на
    # таблицы, где строки ЕСТЬ, — то есть про планы заказов и личные следы людей
    # она молчала. А это ровно те таблицы, из-за которых заводили SEC-6.
    r = big.post("/api/order-plan", json={"budget": 150000, "budget_scope": "now",
                                          "cadence_days": 30, "safety_days": 14,
                                          "strategy": "balance"})
    check("план заказа сохранён — есть что удалять в order_plans",
          r.status_code == 200 and sql("SELECT count(*) FROM order_plans WHERE org_id = ?",
                                       big_org)[0][0] > 0,
          f"status={r.status_code} {r.text[:120]}")

    def leave_traces(c: httpx.Client) -> None:
        """Личные следы человека: пройденный урок, тумблер подсказок, инструкция."""
        c.post("/api/lessons/turnover/done")
        c.post("/api/prefs/hints", json={"enabled": False})
        c.post("/api/hints/seen", json={"page": "orders"})

    leave_traces(big)
    sonya = client()
    sonya.post("/login", data={"email": "sonya@test.io", "password": "secret123"})
    leave_traces(sonya)
    dual = client()
    dual.post("/login", data={"email": "dual@test.io", "password": "secret123"})
    leave_traces(dual)
    PERSONAL = ("user_lessons", "user_prefs", "user_hints_seen")
    for tbl in PERSONAL:
        n = sql(f"SELECT count(*) FROM {tbl} WHERE user_id IN (?, ?, ?)",
                big_id, sonya_id, dual_id)[0][0]
        check(f"личные следы записались, есть что удалять: {tbl}", n == 3, f"строк={n}")

    j = big.get("/api/account").json()
    check("владельцу показано, что именно уйдёт (позиции, продажи, подключение)",
          j["counts"]["products"] > 0 and j["counts"]["sales"] > 0
          and j["connection"]["has_token"] is True, f"counts={j['counts']} conn={j['connection']}")
    before = org_rows(big_org)
    check("до удаления у организации есть данные в нескольких таблицах", len(before) >= 5,
          f"tables={sorted(before)}")
    r = big.post("/api/account/delete", json={
        "password": "secret123", "confirm": "УДАЛИТЬ", "mode": "org"})
    check("организация удалена, снят один осиротевший аккаунт",
          r.status_code == 200 and r.json()["scope"] == "org"
          and r.json()["removed_members"] == 1, f"resp={r.text[:140]}")
    after = org_rows(big_org)
    check("в БД не осталось НИ ОДНОЙ строки организации", after == {}, f"осталось={after}")
    check("подключение и зашифрованный токен стёрты",
          not sql("SELECT id FROM connections WHERE org_id = ?", big_org))
    check("аккаунт сотрудника без других организаций удалён",
          not sql("SELECT id FROM users WHERE email = ?", "sonya@test.io"))
    check("аккаунт сотрудника с другой организацией сохранён",
          sql("SELECT id FROM users WHERE email = ?", "dual@test.io")
          and sql("SELECT org_id FROM memberships WHERE user_id = ?", dual_id) == [(boss_org,)])
    # Удаление обязано быть точным в обе стороны: стереть следы ушедших и не
    # тронуть следы того, кто остался. Ошибка «удалили всё» так же ломает
    # обещание, как и «удалили не всё», просто у другого человека.
    for tbl in PERSONAL:
        gone = sql(f"SELECT count(*) FROM {tbl} WHERE user_id IN (?, ?)",
                   big_id, sonya_id)[0][0]
        check(f"личные следы удалённых людей стёрты: {tbl}", gone == 0, f"осталось={gone}")
        kept = sql(f"SELECT count(*) FROM {tbl} WHERE user_id = ?", dual_id)[0][0]
        check(f"следы оставшегося коллеги не задеты: {tbl}", kept == 1, f"строк={kept}")
    sonya.close()
    dual.close()
    again = client()
    check("после удаления можно зарегистрироваться тем же e-mail",
          register(again, "big@test.io", "Второй заход").status_code == 303)

    print("== Тарифы ==")
    c = client()
    register(c, "plan@test.io", "Тариф-бренд")
    j = c.get("/api/plans").json()
    check("тарифы приходят из единого списка PLANS",
          [p["code"] for p in j["plans"]] == [p["code"] for p in PLANS]
          and len(j["plans"]) == len(PLANS), f"plans={[p['code'] for p in j['plans']]}")
    check("цены совпадают с лендингом (4900 / 9900 / 19900)",
          [p["price_month"] for p in j["plans"]] == [4900, 9900, 19900],
          f"prices={[p['price_month'] for p in j['plans']]}")
    check("годовая цена — минус 20%",
          all(p["price_year_month"] == round(p["price_month"] * 0.8) for p in j["plans"]))
    check("виден текущий тариф и остаток триала",
          j["current"] == "trial" and j["trial_days_left"] == 14 and j["trial_ends_at"],
          f"current={j['current']} left={j['trial_days_left']}")
    check("заявки ещё нет", j["request"] is None)

    def request_plan(**kw):
        body = {"plan": "brand", "period": "month", "company": "ООО «Тариф»",
                "inn": "7701234567", "email": "buh@test.io"}
        body.update(kw)
        r = c.post("/api/plans/request", json=body)
        return r.status_code, (r.json().get("detail", "") if r.status_code >= 400 else r.json())

    st, msg = request_plan(plan="золотой")
    check("несуществующий тариф — 422", st == 422, f"{st} {msg}")
    st, msg = request_plan(company="Я")
    check("пустое название плательщика — 422 с объяснением", st == 422 and "счёте" in msg,
          f"{st} {msg[:60]}")
    st, msg = request_plan(inn="7701")
    check("кривой ИНН — 422 с объяснением", st == 422 and "ИНН" in msg, f"{st} {msg[:60]}")
    st, msg = request_plan(email="не почта")
    check("кривая почта — 422 с объяснением", st == 422 and "почт" in msg, f"{st} {msg[:60]}")
    st, res = request_plan(period="year", inn="77 01 23 45 67", email=" Buh@Test.IO ")
    check("заявка принята: годовая цена, нормализованные ИНН и почта",
          st == 200 and res["request"]["amount"] == 7920
          and res["request"]["inn"] == "7701234567"
          and res["request"]["email"] == "buh@test.io", f"{st} {res}")
    j = c.get("/api/plans").json()
    check("заявка видна на странице тарифов после перезагрузки",
          j["request"] and j["request"]["plan_name"] == "Бренд" and j["request"]["status"] == "new",
          f"request={j.get('request')}")
    check("заявка сохранена в базе",
          sql("SELECT plan, period, amount, status FROM billing_requests") == [
              ("brand", "year", 7920, "new")])

    print("== Ревью 22.08: заявки на счёт не спамятся ==")
    # QA: десять одинаковых POST подряд создавали десять заявок в очереди,
    # которую владелец сервиса разбирает руками. Открытая заявка у
    # организации — одна: повтор с теми же данными не плодит строку.
    statuses = []
    for _ in range(10):
        st, msg = request_plan(period="year", inn="77 01 23 45 67", email=" Buh@Test.IO ")
        statuses.append(st)
    check("десять одинаковых подряд: ни одна не создала вторую заявку",
          statuses == [409] * 10, f"statuses={statuses}")
    check("объяснение — человеческое, что делать написано",
          "уже отправлена" in msg and "тариф" in msg, f"msg={msg[:120]}")
    check("в базе по-прежнему одна заявка", sql(
        "SELECT COUNT(*) FROM billing_requests") == [(1,)])
    # Передумал — прислал другой тариф: должно пройти и не завести вторую строку.
    st, res = request_plan(plan="start", period="month")
    check("другой тариф после спама — принят (200), не заблокирован окном",
          st == 200 and res["request"]["plan"] == "start", f"{st} {res}")
    check("это обновило ТУ ЖЕ заявку, а не завело вторую", sql(
        "SELECT plan, period, status FROM billing_requests") == [("start", "month", "new")])

    # Организация из каталога МойСклад платит через МС — счёт от нас ей не нужен.
    exec_sql("UPDATE orgs SET source = 'ms_app', ms_tariff_name = 'Расширенный' WHERE id = ?",
             org_of("plan@test.io"))
    j = c.get("/api/plans").json()
    check("МС-организация помечена источником и своим тарифом МС",
          j["source"] == "ms_app" and j["ms_tariff_name"] == "Расширенный", f"resp={j['source']}")
    r = c.post("/api/plans/request", json={"plan": "brand", "company": "ООО «Тариф»",
                                           "inn": "7701234567", "email": "buh@test.io"})
    check("МС-организации счёт не выставляем (409 с объяснением)",
          r.status_code == 409 and "маркетплейсе" in r.json()["detail"], f"status={r.status_code}")
    exec_sql("UPDATE orgs SET source = 'saas' WHERE id = ?", org_of("plan@test.io"))

    member = client()
    add_member(org_of("plan@test.io"), "clerk@test.io", "Клерк")
    member.post("/login", data={"email": "clerk@test.io", "password": "secret123"})
    check("участник видит тарифы, но счёт запросить не может",
          member.get("/api/plans").status_code == 200
          and member.post("/api/plans/request", json={"plan": "brand"}).status_code == 403)

    print("== Права на условия производства ==")
    # Ревью 22.08: участник менял срок основного цеха (200 вместо 403) и этим
    # двигал количество в заказе всей организации. Право — как у настроек.
    owner_c = c
    prods = owner_c.get("/api/productions").json()["productions"]
    main_id = [p["id"] for p in prods if p["is_main"]][0]
    lead_before = [p["lead_time_days"] for p in prods if p["is_main"]][0]
    denied = {
        "создать производство":
            member.post("/api/productions", json={"name": "Левый цех", "lead_time_days": 90}),
        "изменить условия основного цеха":
            member.post(f"/api/productions/{main_id}",
                        json={"name": "Основное производство", "lead_time_days": 120}),
        "перенести позицию на другое производство":
            member.post("/api/productions/assign",
                        json={"base_name": "Худи", "production_id": None}),
        "удалить производство": member.delete(f"/api/productions/{main_id}"),
    }
    for title, resp in denied.items():
        check(f"участнику нельзя: {title} (403)", resp.status_code == 403,
              f"status={resp.status_code} {resp.text[:70]}")
    check("список производств участнику по-прежнему виден",
          member.get("/api/productions").status_code == 200)
    lead_now = [p["lead_time_days"] for p in owner_c.get("/api/productions").json()["productions"]
                if p["is_main"]][0]
    check("условия производства после попыток участника не изменились",
          lead_now == lead_before, f"{lead_before} → {lead_now}")
    r = owner_c.post(f"/api/productions/{main_id}",
                     json={"name": "Основное производство", "lead_time_days": 120})
    check("владельцу можно менять условия производства",
          r.status_code == 200 and r.json()["lead_time_days"] == 120,
          f"status={r.status_code} {r.text[:70]}")

    print("== Лимит попыток входа ==")
    # Лимитеры общие на процесс — начинаем с чистого листа, чтобы не зависеть
    # от неудачных входов предыдущих сценариев (и не мешать следующим).
    clear_limiters()
    c = client()
    register(c, "brute@test.io", "Бренд-жертва")
    register(client(), "calm@test.io", "Соседний бренд")

    def try_login(cl, email, password, xff=None):
        h = {"X-Forwarded-For": xff} if xff else {}
        return cl.post("/login", data={"email": email, "password": password}, headers=h)

    # 1. Обход через заголовок: 30 попыток по ОДНОМУ аккаунту, каждая с нового
    #    адреса (проверка §«Обход через X-Forwarded-For» из задания).
    attacker = client()
    blocked = 0
    for i in range(30):
        r = try_login(attacker, "brute@test.io", f"подбор{i}", xff=f"10.20.{i}.7")
        if "Слишком много" in r.text:
            blocked += 1
    check("подбор с ротацией X-Forwarded-For блокируется (ключ лимита не зависит от адреса)",
          blocked == 25, f"заблокировано {blocked} из 30 (ожидали 25: попытки 6..30 при max_attempts=5)")
    r = try_login(attacker, "brute@test.io", "ещё", xff="10.20.99.7")
    check("текст блокировки называет срок ожидания и почту поддержки",
          "через 5 минут" in r.text and "tsitsilinvlad@gmail.com" in r.text,
          f"текст={r.text[r.text.find('Слишком много'):][:160]}")

    # 1б. ЦЕЛЕВАЯ блокировка (Задача 1): пока атакующий держит аккаунт
    #     заблокированным подбором, ЖЕРТВА вводит верный пароль с другого
    #     адреса — и должна пройти сразу, а не ждать вместе с атакующим.
    victim = client()
    r = try_login(victim, "brute@test.io", "secret123", xff="8.8.8.8")
    check("верный пароль жертвы проходит, даже пока аккаунт заблокирован атакой",
          r.status_code == 303, f"status={r.status_code}")
    check("после этого блокировка снята (следующая случайная попытка — не отказ по лимиту)",
          auth.login_limiter.retry_after("acc:brute@test.io") == 0)

    # 2. Блокировка одного аккаунта не мешает другому.
    r = try_login(client(), "calm@test.io", "secret123")
    check("вход в другой аккаунт во время блокировки первого работает",
          r.status_code == 303, f"status={r.status_code}")

    # 3. Человек с двумя опечатками входит без помех и без задержки ответа.
    human = client()
    for _ in range(2):
        try_login(human, "calm@test.io", "опечатка")
    t0 = time.time()
    try_login(human, "calm@test.io", "ещё-опечатка")
    third = time.time() - t0
    check("третья неудачная попытка отвечает сразу (нет time.sleep в пуле потоков)",
          third < 0.5, f"{third:.2f}s")
    r = try_login(human, "calm@test.io", "secret123")
    check("после трёх опечаток верный пароль пускает", r.status_code == 303,
          f"status={r.status_code}")

    # 4. Подбор паролем: 100 попыток подряд на один аккаунт — считаем, сколько
    #    РЕАЛЬНО дошло до сверки пароля (считаем сами вызовы bcrypt через
    #    verify_password, а не гадаем по тексту ответа), сколько успело, за
    #    какое время (проверка §«Подбор» из задания). Пароль сверяется
    #    КАЖДЫЙ раз, даже пока ключ заперт, — это и есть цена «мягкой»
    #    блокировки (иначе верный пароль не пропустить во время локаута), но
    #    подбирателю это не помогает: единственный видимый ему сигнал успеха —
    #    сам вход, а число проверок он снаружи не наблюдает.
    clear_limiters()
    register(client(), "perebor@test.io", "Бренд для подбора")
    prober = client()
    verify_calls = 0
    _orig_verify_password = auth.verify_password

    def _counting_verify_password(password, pw_hash):
        nonlocal verify_calls
        verify_calls += 1
        return _orig_verify_password(password, pw_hash)

    auth.verify_password = _counting_verify_password
    t0 = time.time()
    succeeded = 0
    try:
        for i in range(100):
            r = try_login(prober, "perebor@test.io", f"неверно{i}")
            if r.status_code == 303:
                succeeded += 1
    finally:
        auth.verify_password = _orig_verify_password
    elapsed = time.time() - t0
    check("100 неверных попыток: ни одна не вошла (подбор бессмыслен без знания пароля)",
          succeeded == 0, f"успехов={succeeded}")
    check("пароль сверяется на каждой из 100 попыток (иначе верный пароль не прошёл бы во время локаута)",
          verify_calls == 100, f"вызовов verify_password: {verify_calls} из 100")
    check("100 попыток (100 bcrypt-сверок) укладываются в разумное время (единственная цена — bcrypt, sleep не добавлен)",
          elapsed < 90, f"{elapsed:.1f}s — {elapsed / 100 * 1000:.0f} мс/попытку")
    r = try_login(client(), "perebor@test.io", "secret123", xff="9.9.9.9")
    check("после подбора верный пароль владельца по-прежнему проходит",
          r.status_code == 303, f"status={r.status_code}")

    # 5. Блокировка снимается сама — и НЕ продлевается чужими попытками, пока
    #    активна (Задача 2). Окно на время проверки увеличено до 10 с (с
    #    запасом на bcrypt: 5 попыток блокировки + 3 «докучаю» — восемь
    #    bcrypt-сверок, на медленной машине это может быть больше 2 с).
    clear_limiters()
    window = auth.login_limiter.window_sec
    auth.login_limiter.window_sec = 10
    try:
        for i in range(5):
            try_login(client(), "brute@test.io", f"нет{i}")
        until_first = auth.login_limiter._locked_until.get("acc:brute@test.io")
        check("после 5 неудач ключ ушёл в фиксированную блокировку", until_first is not None)

        # 5а. Пока блокировка активна, ЧУЖИЕ (или свои же повторные) неудачные
        #     попытки не должны отодвигать срок разблокировки вперёд.
        for i in range(3):
            time.sleep(1)
            try_login(client(), "brute@test.io", f"докучаю{i}")
        until_after_spam = auth.login_limiter._locked_until.get("acc:brute@test.io")
        check("попытки во время блокировки не сдвигают срок разблокировки (окно фиксировано)",
              until_after_spam == until_first,
              f"{until_after_spam} vs исходный {until_first}")

        # 5б. Окно истекло по расписанию (от 5-й неудачи, а не от последней из
        #     «докучаю») — аккаунт освобождён без чьего-либо участия. Спим до
        #     заведомо истёкшего срока, а не по номиналу window_sec, — цель
        #     проверки «не продлилось», а не секундомер.
        wait_left = max(0.5, until_first - time.monotonic() + 0.5)
        time.sleep(wait_left)
        check("после исходного окна блокировка снята сама, без продления",
              auth.login_limiter.retry_after("acc:brute@test.io") == 0)
        r = try_login(client(), "brute@test.io", "secret123")
        check("после окна верный пароль снова пускает", r.status_code == 303,
              f"status={r.status_code}")
    finally:
        auth.login_limiter.window_sec = window
        clear_limiters()

    # 6. Память: ключи (и скользящий счётчик, и блокировка) живут не дольше
    #    окна, живые записи не вытесняются.
    probe = auth.LoginLimiter(max_attempts=5, window_sec=2)
    for i in range(5):
        probe.hit("acc:victim@test.io")
    for i in range(20000):
        probe.hit(f"acc:flood{i}@test.io")
    total_before = len(probe._hits) + len(probe._locked_until)
    check("поток мусорных ключей не обнуляет счётчик жертвы (нет вытеснения живых записей)",
          probe.check("acc:victim@test.io") is False, f"ключей всего={total_before}")
    check("жертва ушла в фиксированную блокировку, а не осталась в скользящем счётчике",
          "acc:victim@test.io" in probe._locked_until and "acc:victim@test.io" not in probe._hits)
    time.sleep(2.2)
    probe.hit("acc:после@test.io")
    total_after = len(probe._hits) + len(probe._locked_until)
    check("после истечения окна словари лимитера очищаются (память не растёт)",
          total_after == 1, f"ключей осталось {total_after} из {total_before}")

    # 7. Разбор адреса клиента: чему верим и когда.
    def fake_request(xff: str | None, peer: str = "203.0.113.9"):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "method": "POST", "path": "/login",
                        "headers": headers, "client": (peer, 40000),
                        "scheme": "http", "server": ("test", 80), "query_string": b""})

    os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)
    check("без прокси адрес берём как есть и доверяем",
          auth.client_ip(fake_request(None)) == ("203.0.113.9", True))
    check("заголовок без настроенного прокси адресу не верим (лимит по IP не считаем)",
          auth.client_ip(fake_request("10.1.1.1"))[1] is False)
    os.environ[auth.TRUSTED_PROXY_HOPS_ENV] = "1"
    try:
        check("один свой прокси: берём последний адрес цепочки, подделку слева игнорируем",
              auth.client_ip(fake_request("1.1.1.1, 2.2.2.2, 198.51.100.4"))
              == ("198.51.100.4", True))
        os.environ[auth.TRUSTED_PROXY_HOPS_ENV] = "2"
        check("два своих прокси: берём предпоследний адрес цепочки",
              auth.client_ip(fake_request("1.1.1.1, 198.51.100.4, 10.0.0.1"))
              == ("198.51.100.4", True))
        check("цепочка короче настроенной — адресу не верим",
              auth.client_ip(fake_request("198.51.100.4"))[1] is False)
    finally:
        os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)

    # 8. Fail-fast на старте (Задача 3): в проде переменная обязана быть
    #    задана ЯВНО — «забыли» не должно означать «лимит по IP тихо
    #    выключен». Дев/тест (OBOROT_ENV≠prod) незаданной переменной не
    #    задевается — иначе сломались бы все остальные тесты в этом файле.
    _env_backup = {k: os.environ.get(k) for k in ("OBOROT_ENV", auth.TRUSTED_PROXY_HOPS_ENV)}
    try:
        os.environ["OBOROT_ENV"] = "prod"
        os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)
        raised_detail = ""
        try:
            auth.check_proxy_config()
        except RuntimeError as e:
            raised_detail = str(e)
        check("прод без настроенной переменной не стартует (fail-fast, а не тихая дыра)",
              auth.TRUSTED_PROXY_HOPS_ENV in raised_detail, f"detail={raised_detail[:80]!r}")

        for hops_value in ("0", "1", "2"):
            os.environ[auth.TRUSTED_PROXY_HOPS_ENV] = hops_value
            try:
                auth.check_proxy_config()
                ok = True
            except RuntimeError:
                ok = False
            check(f"прод с явно заданным {auth.TRUSTED_PROXY_HOPS_ENV}={hops_value} стартует",
                  ok)

        # 8б. Д2 (ревью деплоя): проверка ловила только is None — пустая
        # строка и любой мусор проходили и тихо выключали лимит по IP.
        # Теперь значение проверяется по существу (целое в разумных
        # границах), а не по факту наличия переменной.
        for bad_value in ("", "abc", "-1", "99"):
            os.environ[auth.TRUSTED_PROXY_HOPS_ENV] = bad_value
            bad_detail = ""
            try:
                auth.check_proxy_config()
                ok = True
            except RuntimeError as e:
                ok = False
                bad_detail = str(e)
            check(f"прод с мусорным {auth.TRUSTED_PROXY_HOPS_ENV}={bad_value!r} НЕ стартует",
                  not ok, f"detail={bad_detail[:80]!r}")
            check(f"  сообщение для {bad_value!r} называет переменную и что с ней делать",
                  auth.TRUSTED_PROXY_HOPS_ENV in bad_detail and "0" in bad_detail,
                  f"detail={bad_detail!r}")

        # Сообщение одинаково понятно и для «не задано», и для «задано криво»:
        # оба случая объясняют, что поставить (не просто «неверное значение»).
        os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)
        missing_detail = ""
        try:
            auth.check_proxy_config()
        except RuntimeError as e:
            missing_detail = str(e)
        os.environ[auth.TRUSTED_PROXY_HOPS_ENV] = "abc"
        garbage_detail = ""
        try:
            auth.check_proxy_config()
        except RuntimeError as e:
            garbage_detail = str(e)
        check("оба отказа называют один и тот же диапазон допустимых значений",
              "от 0 до" in missing_detail and "от 0 до" in garbage_detail,
              f"missing={missing_detail[:60]!r} garbage={garbage_detail[:60]!r}")

        os.environ["OBOROT_ENV"] = "dev"
        os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)
        try:
            auth.check_proxy_config()
            ok = True
        except RuntimeError:
            ok = False
        check("вне прода незаданная переменная не мешает стартовать (dev/test)", ok)
    finally:
        for k, v in _env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # 9. Лимит по IP: «забыли настроить» — ДО и ПОСЛЕ (проверка §«Лимит по IP
    #    при незаданной переменной» из задания). Незаданная переменная сама
    #    по себе (вне прода, где это уже ловится п.8) по-прежнему не считает
    #    адрес надёжным — это тот же честный выбор, что и раньше (см. п.7):
    #    лучше не ограничивать по IP вовсе, чем ограничить не тот адрес.
    #    Perебор email'ов при этом всё равно упирается в лимит ПО АККАУНТУ
    #    там, где бьют по одному адресату; здесь — заведомо разные адресаты,
    #    поэтому именно IP-лимит — единственная линия защиты, и её состояние
    #    видно по числу заблокированных.
    os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)
    clear_limiters()
    unset_client = client()
    blocked_unset = sum(
        "Слишком много" in try_login(unset_client, f"noproxy{i}@test.io", "x",
                                     xff="172.16.5.9").text
        for i in range(40))
    check("ДО: переменная не задана → лимит по IP молчит (email'ы разные, по аккаунту не бьёт)",
          blocked_unset == 0, f"заблокировано {blocked_unset} из 40")

    os.environ[auth.TRUSTED_PROXY_HOPS_ENV] = "1"
    clear_limiters()
    try:
        set_client = client()
        blocked_set = sum(
            "Слишком много" in try_login(set_client, f"withproxy{i}@test.io", "x",
                                         xff="172.16.9.9").text
            for i in range(40))
        check("ПОСЛЕ: переменная задана (=1) → тот же перебор упирается в лимит по IP",
              blocked_set == 20, f"заблокировано {blocked_set} из 40 (ожидали 20 — max_attempts=20)")

        # 10. Лимит по IP при правильно настроенном прокси остаётся мягким и
        #     не задевает соседей за тем же адресом.
        clear_limiters()
        same = client()
        blocked_same = sum(
            "Слишком много" in try_login(same, f"mail{i}@test.io", "x", xff="198.51.100.4").text
            for i in range(22))
        clear_limiters()
        rotating = client()
        blocked_rot = sum(
            "Слишком много" in try_login(rotating, f"other{i}@test.io", "x",
                                         xff=f"198.51.100.{i}").text
            for i in range(22))
        check("перебор e-mail'ов с одного адреса упирается в лимит по IP",
              blocked_same == 2, f"заблокировано {blocked_same} из 22 (ожидали 2)")
        r = try_login(client(), "calm@test.io", "secret123", xff="198.51.100.4")
        check("исчерпанный лимит по IP не запирает того, кто знает пароль",
              r.status_code == 303, f"status={r.status_code}")
        check("у разных клиентов за тем же прокси свой счётчик (нет блокировки всем сразу)",
              blocked_rot == 0, f"заблокировано {blocked_rot} из 22 (ожидали 0)")

        # 10б. Д2: полная матрица значений переменной на живом лимите входа
        # (те самые числа из ревью — «0 из 40 заблокировано» на мусоре против
        # «20 из 40» на корректном значении). Мусорные значения при этом
        # ведут себя КАК незаданная переменная — trusted_proxy_hops() всегда
        # безопасно возвращает 0 на плохой ввод (лимит по IP просто не
        # считается, а не падает и не доверяет подделанному адресу). На
        # проде до этого не доходит вовсе: check_proxy_config() (см. п.8б)
        # не даст стартовать — эта проверка про рантайм-парсер отдельно.
        matrix = {}
        for raw_value, label in (
            (None, "не задана"), ("", "''"), ("abc", "'abc'"), ("-1", "'-1'"),
            ("0", "'0'"), ("1", "'1'"), ("99", "'99'"),
        ):
            if raw_value is None:
                os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)
            else:
                os.environ[auth.TRUSTED_PROXY_HOPS_ENV] = raw_value
            clear_limiters()
            mclient = client()
            blocked = sum(
                "Слишком много" in try_login(mclient, f"m{label}{i}@test.io", "x",
                                             xff="203.0.113.77").text
                for i in range(40))
            matrix[label] = blocked
        # "0" здесь тоже 0 заблокированных, но по другой (корректной) причине,
        # чем мусор: hops=0 значит «прокси нет», а запрос всё же несёт
        # X-Forwarded-For (как и остальные запросы в этом тесте) — значит
        # адресу нельзя верить (см. client_ip и check "заголовок без
        # настроенного прокси адресу не верим" выше), лимит по IP по нему
        # осознанно не считается. "99" тоже 0: цепочка (1 адрес) короче
        # настроенных 99 хопов — тоже честно «не верим».
        check("мусор/не задана/вне диапазона на рантайме не включают лимит по IP",
              matrix["не задана"] == 0 and matrix["''"] == 0 and matrix["'abc'"] == 0
              and matrix["'-1'"] == 0 and matrix["'99'"] == 0,
              f"matrix={matrix}")
        check("'0' (осознанно «прокси нет») тоже не включает лимит при пришедшем XFF — это не баг",
              matrix["'0'"] == 0, f"matrix={matrix}")
        check("единственное значение, включающее лимит по IP на этой цепочке — реальный хоп '1' (20 из 40)",
              matrix["'1'"] == 20, f"matrix={matrix}")
    finally:
        os.environ.pop(auth.TRUSTED_PROXY_HOPS_ENV, None)
        clear_limiters()

    print("== Страницы ==")
    check("страница тарифов открывается", c.get("/plans").status_code == 200)
    check("страница аккаунта открывается", c.get("/account").status_code == 200)
    for alias in ("/profile", "/security"):
        r = c.get(alias, follow_redirects=False)
        check(f"{alias} ведёт на /account (раньше был 404)",
              r.status_code == 302 and r.headers.get("location") == "/account",
              f"status={r.status_code}")
    print("== Таблицы: сортировка колонок и память вида ==")
    # Обе таблицы (и разметка «Оборачиваемости», и JS-шапка «Активного стока»)
    # объявляют сортируемые колонки атрибутом data-sk. Проверяем контракт
    # разметки: сама сортировка живёт в браузере, но если атрибуты или правила
    # шапки уедут, кликать станет не по чему.
    t1 = client()
    register(t1, "sort@test.io", "Сортировка-бренд")
    tv, stk = t1.get("/turnover").text, t1.get("/stocks").text
    check("на «Оборачиваемости» сортируются все 15 колонок с данными",
          tv.count('data-sk="') == 15, f"колонок={tv.count('data-sk=')}")
    for key in ("name", "supply", "zat", "turn", "disc"):
        check(f"«Оборачиваемость»: колонка {key} сортируемая", f'data-sk="{key}"' in tv)
    for key in ("name", "wh:", "cost", "margin", "zat", "defq", "ord"):
        check(f"«Активный сток»: колонка {key} сортируемая", f'data-sk="{key}' in stk)

    # Регрессия: если в правило сортируемых заголовков попадёт position, оно
    # перебьёт position:sticky у thead th — шапка перестанет липнуть сверху,
    # а первая колонка слева (проверено вживую: ломается именно так).
    for page, html in (("Оборачиваемость", tv), ("Активный сток", stk)):
        rule = html.split("thead th[data-sk] {", 1)[1].split("}", 1)[0] \
            if "thead th[data-sk] {" in html else ""
        check(f"{page}: правило сортируемых заголовков не отменяет липкую шапку",
              bool(rule) and "position" not in rule, f"rule={rule.strip()!r}")

    # Память вида (категория, поиск, сортировка) живёт в браузере, но ключ
    # хранилища обязан быть привязан к паре «пользователь + организация»:
    # за одним браузером работают разные люди и разные аккаунты.
    # Название организации уходит в разметку через |tojson, поэтому кириллица
    # там в виде \uXXXX — сверяем не текст названия, а форму ключа.
    for page, html in (("Оборачиваемость", tv), ("Активный сток", stk)):
        check(f"{page}: ключ памяти вида привязан к пользователю И организации",
              '"sort@test.io"+"|"+"' in html
              and '"sort@test.io"+"|"+""' not in html,
              f"есть vhash={'vhash(' in html}")
    t2 = client()
    register(t2, "sort2@test.io", "Другой-бренд")
    other = t2.get("/turnover").text
    check("у второго человека в том же браузере ключ памяти вида другой",
          '"sort2@test.io"+"|"+"' in other and '"sort@test.io"' not in other)

    guest = httpx.Client(base_url=BASE, timeout=30.0, follow_redirects=False)
    for path in ("/account", "/plans"):
        check(f"гостя с {path} отправляет на вход",
              guest.get(path).status_code == 302
              and guest.get(path).headers.get("location") == "/login")

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print("  FAIL:", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
