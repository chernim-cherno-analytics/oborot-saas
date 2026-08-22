# -*- coding: utf-8 -*-
"""Тест обучения: каталог уроков, прогресс, подсказки, пример для тура.

Сценарий (без pytest, просто python; mock-МойСклад не нужен — данные для
примера даёт демо-сид):
  1) поднимаем приложение на 127.0.0.1:8805 с чистой БД;
  2) каталог app.lessons: шесть уроков, у каждого шага непустые sel/title/
     text/fallback, суммарно 13 минут («Пройти все уроки подряд · 13 мин»);
  3) аноним → 401 на всех ручках, /lessons → редирект на /login;
  4) GET /api/lessons: форма ответа, done=false у всех, hints_enabled=true;
  5) done/reset: идемпотентность, арифметика done_count, сброс всего,
     неизвестный ключ → 404 и мусор в БД не попадает;
  6) тумблер подсказок: сохраняется и переживает новый вход;
  7) GET /api/lessons/sample: пусто у свежей организации (sample=null),
     после демо-сида — бестселлер с наименьшим запасом в днях;
  8) изоляция: пользователь другой организации видит свой прогресс;
  9) страница /lessons: свой пункт меню активен, блок поддержки без кнопки,
     когда контакт не задан, и с кнопкой, когда задан.

Запуск из корня репозитория:  python tests/test_lessons.py
"""
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

DB_PATH = ROOT / "test_lessons.db"
APP_PORT = 8805

# Окружение — ДО импорта приложения (db.py читает DATABASE_URL).
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
# Контакт поддержки не задан: блок «Не нашли ответ» обязан рисоваться без кнопки.
os.environ.pop("OBOROT_SUPPORT_URL", None)
os.environ.pop("OBOROT_SUPPORT_EMAIL", None)

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import lessons  # noqa: E402
from app.main import app as oborot_app  # noqa: E402


# ── Инфраструктура (как в tests/test_sync.py) ────────────────────────────────

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


def lesson_rows() -> list[tuple]:
    """Строки user_lessons напрямую из БД (для проверки, что мусор не пишется)."""
    con = sqlite3.connect(DB_PATH)
    rows = list(con.execute("SELECT user_id, lesson FROM user_lessons ORDER BY user_id, lesson"))
    con.close()
    return rows


def new_client() -> httpx.Client:
    return httpx.Client(headers={"X-Oborot-CSRF": "1"},
                        base_url=f"http://127.0.0.1:{APP_PORT}", timeout=60.0)


def supply_days(it: dict) -> float:
    """Запас в днях по строке /api/turnover (как считает app.lessons.pick_sample)."""
    return round(int(it["cs"]) / float(it["rate"]))


def main() -> int:
    app_srv = ServerThread(oborot_app, APP_PORT)
    app_srv.start()
    try:
        return run_scenario()
    finally:
        app_srv.stop()


def run_scenario() -> int:
    # ── 1. Каталог как контракт (его же читает рендерер тура) ────────────────
    print("== Каталог уроков ==")
    cat = lessons.CATALOGUE
    check("в каталоге ровно 6 уроков", len(cat) == 6, f"n={len(cat)}")
    check("порядок ключей зафиксирован",
          [l["key"] for l in cat] == ["turnover", "settings", "replenish", "budget",
                                      "forecast", "trust"],
          f"keys={[l['key'] for l in cat]}")
    check("ключи уникальны", len({l["key"] for l in cat}) == 6)
    check("lessons.KEYS/TOTAL согласованы с каталогом",
          lessons.KEYS == [l["key"] for l in cat] and lessons.TOTAL == 6)
    check("«все уроки подряд» = 13 минут", lessons.TOTAL_MINUTES == 13,
          f"minutes={lessons.TOTAL_MINUTES}")

    bad_meta = [l["key"] for l in cat
                if not (l.get("title") and l.get("page") and l.get("desc")
                        and str(l.get("url", "")).startswith("/")
                        and isinstance(l.get("minutes"), int) and l["minutes"] > 0)]
    check("у каждого урока заполнены title/page/url/desc/minutes", not bad_meta,
          f"плохие={bad_meta}")

    steps = [(l["key"], s) for l in cat for s in l["steps"]]
    check("у каждого урока есть шаги", all(len(l["steps"]) >= 2 for l in cat),
          f"n={[len(l['steps']) for l in cat]}")
    check("у каждого шага непустой селектор",
          all(isinstance(s.get("sel"), str) and s["sel"].strip() for _, s in steps))
    check("у каждого шага есть title, text и fallback",
          all(s.get("title") and s.get("text") and s.get("fallback") for _, s in steps))
    check("fallback без плейсхолдеров (данных примера может не быть вовсе)",
          all("{" not in s["fallback"] for _, s in steps),
          f"с плейсхолдером={[k for k, s in steps if '{' in s['fallback']]}")
    known_ph = {"name", "category", "rate", "stock", "supply"}
    import re as _re
    unknown = sorted({p for _, s in steps for p in _re.findall(r"\{(\w+)\}", s["text"])} - known_ph)
    check("в текстах только известные плейсхолдеры", not unknown, f"чужие={unknown}")

    # Селекторы обязаны существовать на своих страницах (иначе тур подсветит пустоту).
    tpl = {"/turnover": "turnover.html", "/replenish": "replenish.html",
           "/budget": "budget.html", "/forecast": "forecast.html",
           "/settings": "settings.html"}
    missing = []
    for l in cat:
        html = (ROOT / "templates" / tpl[l["url"]]).read_text(encoding="utf-8")
        for s in l["steps"]:
            root_id = s["sel"].split()[0].lstrip("#")
            if f'id="{root_id}"' not in html:
                missing.append((l["key"], s["sel"]))
    check("селекторы шагов есть в шаблонах своих страниц", not missing, f"нет={missing}")

    # ── 2. Аноним ────────────────────────────────────────────────────────────
    print("== Доступ без сессии ==")
    anon = new_client()
    r = anon.get("/api/lessons")
    check("GET /api/lessons без сессии → 401", r.status_code == 401, f"status={r.status_code}")
    r = anon.post("/api/lessons/turnover/done")
    check("POST done без сессии → 401", r.status_code == 401, f"status={r.status_code}")
    r = anon.post("/api/lessons/reset")
    check("POST reset без сессии → 401", r.status_code == 401, f"status={r.status_code}")
    r = anon.get("/api/lessons/sample")
    check("GET sample без сессии → 401", r.status_code == 401, f"status={r.status_code}")
    r = anon.post("/api/prefs/hints", json={"enabled": False})
    check("POST /api/prefs/hints без сессии → 401", r.status_code == 401, f"status={r.status_code}")
    r = anon.get("/lessons")
    check("страница /lessons без сессии → редирект на /login",
          r.status_code == 302 and r.headers.get("location") == "/login",
          f"status={r.status_code} loc={r.headers.get('location')}")
    anon.close()

    # ── 3. Владелец: стартовое состояние ─────────────────────────────────────
    print("== Стартовое состояние ==")
    a = new_client()
    r = a.post("/register", data={"name": "Ученица", "email": "owner@lessons.io",
                                  "password": "secret123", "org_name": "Учебный бренд"})
    check("регистрация владельца", r.status_code == 303, f"status={r.status_code}")

    d = a.get("/api/lessons").json()
    check("GET /api/lessons: total = 6 и столько же уроков",
          d["total"] == 6 and len(d["lessons"]) == 6,
          f"total={d['total']} n={len(d['lessons'])}")
    check("done_count = 0 у новичка", d["done_count"] == 0, f"done_count={d['done_count']}")
    check("все уроки помечены непройденными", all(l["done"] is False for l in d["lessons"]))
    check("hints_enabled по умолчанию включён", d["hints_enabled"] is True)
    keys_api = [l["key"] for l in d["lessons"]]
    check("порядок уроков в API = порядок каталога", keys_api == lessons.KEYS, f"keys={keys_api}")
    first = d["lessons"][0]
    check("урок отдаёт шаги для тура",
          isinstance(first["steps"], list) and first["steps"]
          and set(first["steps"][0]) >= {"sel", "title", "text", "fallback"},
          f"step={first['steps'][0] if first['steps'] else None}")
    check("в ответе нет лишних ключей верхнего уровня",
          set(d) == {"lessons", "done_count", "total", "hints_enabled"}, f"keys={sorted(d)}")
    check("в уроке нет лишних ключей",
          set(first) == {"key", "title", "page", "url", "desc", "minutes", "steps", "done"},
          f"keys={sorted(first)}")

    # ── 4. Пример для тура до данных ─────────────────────────────────────────
    print("== Пример для тура: данных ещё нет ==")
    r = a.get("/api/lessons/sample")
    check("организация без данных → sample = null",
          r.status_code == 200 and r.json() == {"sample": None}, f"body={r.text[:120]}")

    # ── 5. Пройдено / пройти заново ──────────────────────────────────────────
    print("== Прогресс уроков ==")
    r = a.post("/api/lessons/turnover/done")
    check("отметка урока пройденным", r.status_code == 200 and r.json() == {"ok": True, "done_count": 1},
          f"body={r.text[:120]}")
    r = a.post("/api/lessons/turnover/done")
    check("повторная отметка идемпотентна", r.json()["done_count"] == 1, f"body={r.text[:120]}")
    check("в БД одна строка на урок", len(lesson_rows()) == 1, f"rows={lesson_rows()}")

    for k in ("replenish", "budget"):
        a.post(f"/api/lessons/{k}/done")
    d = a.get("/api/lessons").json()
    check("done_count = 3 после трёх уроков", d["done_count"] == 3, f"done_count={d['done_count']}")
    check("пройденными помечены именно эти три",
          {l["key"] for l in d["lessons"] if l["done"]} == {"turnover", "replenish", "budget"},
          f"done={[l['key'] for l in d['lessons'] if l['done']]}")

    r = a.post("/api/lessons/replenish/reset")
    check("«Пройти заново» уменьшает счётчик",
          r.status_code == 200 and r.json() == {"ok": True, "done_count": 2}, f"body={r.text[:120]}")
    r = a.post("/api/lessons/replenish/reset")
    check("повторный сброс идемпотентен", r.json()["done_count"] == 2, f"body={r.text[:120]}")
    check("урок снова непройден",
          all(not l["done"] for l in a.get("/api/lessons").json()["lessons"] if l["key"] == "replenish"))
    r = a.post("/api/lessons/replenish/done")
    check("урок можно пройти повторно", r.json()["done_count"] == 3, f"body={r.text[:120]}")

    r = a.post("/api/lessons/reset")
    check("сброс всего прогресса", r.status_code == 200 and r.json() == {"ok": True, "done_count": 0},
          f"body={r.text[:120]}")
    check("после общего сброса строк в БД не осталось", lesson_rows() == [], f"rows={lesson_rows()}")

    print("== Неизвестный урок ==")
    r = a.post("/api/lessons/nosuchlesson/done")
    check("done с неизвестным ключом → 404", r.status_code == 404, f"status={r.status_code}")
    r = a.post("/api/lessons/nosuchlesson/reset")
    check("reset с неизвестным ключом → 404", r.status_code == 404, f"status={r.status_code}")
    check("неизвестный ключ в БД не попал", lesson_rows() == [], f"rows={lesson_rows()}")

    # ── 6. Тумблер подсказок ─────────────────────────────────────────────────
    print("== Тумблер подсказок ==")
    r = a.post("/api/prefs/hints", json={"enabled": False})
    check("выключение подсказок", r.status_code == 200 and r.json() == {"ok": True, "hints_enabled": False},
          f"body={r.text[:120]}")
    check("состояние видно в /api/lessons", a.get("/api/lessons").json()["hints_enabled"] is False)
    r = a.post("/api/prefs/hints", json={"enabled": False})
    check("повторное выключение идемпотентно", r.json()["hints_enabled"] is False)

    fresh = new_client()
    fresh.post("/login", data={"email": "owner@lessons.io", "password": "secret123"})
    check("настройка пережила новый вход",
          fresh.get("/api/lessons").json()["hints_enabled"] is False)
    r = fresh.post("/api/prefs/hints", json={"enabled": True})
    check("включение обратно", r.json()["hints_enabled"] is True)
    check("в /api/lessons снова включено", a.get("/api/lessons").json()["hints_enabled"] is True)
    r = a.post("/api/prefs/hints", json={"enabled": "да"})
    check("нечисловое/непонятное значение → 422", r.status_code == 422, f"status={r.status_code}")
    fresh.close()

    # ── 7. Пример для тура на реальных данных ────────────────────────────────
    print("== Пример для тура: демо-данные ==")
    r = a.post("/api/connect/demo")
    check("демо-данные засеяны", r.status_code == 200 and r.json().get("ok"), f"body={r.text[:120]}")
    d = a.get("/api/lessons/sample").json()
    sm = d.get("sample")
    check("sample не пуст, когда данные есть", isinstance(sm, dict), f"sample={sm}")
    if isinstance(sm, dict):
        check("в примере все нужные поля",
              set(sm) == {"name", "category", "rate", "stock", "supply_days", "cls"},
              f"keys={sorted(sm)}")
        check("пример правдоподобен: есть имя, остаток и заработок",
              bool(sm["name"]) and sm["stock"] > 0 and sm["rate"] > 0 and sm["supply_days"] >= 0,
              f"sample={sm}")

        items = a.get("/api/turnover").json()["items"]
        pool = [it for it in items
                if it.get("group") == "rank" and not it.get("archived") and not it.get("hidden")
                and int(it["cs"] or 0) > 0 and float(it["rate"] or 0) > 0]
        order = ["best", "good", "dull", "weak"]
        best_cls = next((c for c in order if any(it["cls"] == c for it in pool)), None)
        check("выбран лучший присутствующий класс", sm["cls"] == best_cls,
              f"sample={sm['cls']} лучший={best_cls}")
        same_cls = [it for it in pool if it["cls"] == best_cls]
        min_supply = min(supply_days(it) for it in same_cls)
        check("внутри класса выбран наименьший запас в днях",
              sm["supply_days"] == min_supply,
              f"sample={sm['supply_days']} минимум={min_supply} из {len(same_cls)} позиций")
        row = next((it for it in same_cls if it["base_name"] == sm["name"]), None)
        check("пример — реальная строка таблицы «Оборачиваемость»", row is not None,
              f"name={sm['name']}")
        if row is not None:
            check("остаток и категория примера совпадают с таблицей",
                  sm["stock"] == int(row["cs"]) and sm["category"] == row["category"],
                  f"sample={sm['stock']}/{sm['category']} строка={row['cs']}/{row['category']}")
            check("rate примера — оборачиваемость в ₽/день, а не штуки",
                  sm["rate"] == int(row["turnover"]),
                  f"sample={sm['rate']} turnover={row['turnover']}")

    # ── 8. Изоляция: обучение личное ─────────────────────────────────────────
    print("== Изоляция пользователей ==")
    a.post("/api/lessons/turnover/done")
    a.post("/api/lessons/budget/done")
    b = new_client()
    r = b.post("/register", data={"name": "Сосед", "email": "other@lessons.io",
                                  "password": "secret123", "org_name": "Чужой бренд"})
    check("регистрация второго владельца", r.status_code == 303, f"status={r.status_code}")
    db_ = b.get("/api/lessons").json()
    check("у чужой организации свой (нулевой) прогресс", db_["done_count"] == 0,
          f"done_count={db_['done_count']}")
    check("у чужой организации свой тумблер подсказок", db_["hints_enabled"] is True)
    b.post("/api/lessons/settings/done")
    da = a.get("/api/lessons").json()
    check("прогресс первой организации не изменился", da["done_count"] == 2,
          f"done_count={da['done_count']}")
    check("первая организация не видит чужой урок",
          {l["key"] for l in da["lessons"] if l["done"]} == {"turnover", "budget"},
          f"done={[l['key'] for l in da['lessons'] if l['done']]}")
    check("вторая организация видит только свой урок",
          {l["key"] for l in b.get("/api/lessons").json()["lessons"] if l["done"]} == {"settings"})
    r = b.get("/api/lessons/sample")
    check("у организации без данных sample по-прежнему null", r.json() == {"sample": None},
          f"body={r.text[:120]}")
    b.close()

    # ── 9. Страница ──────────────────────────────────────────────────────────
    print("== Страница /lessons ==")
    r = a.get("/lessons")
    html = r.text
    check("страница отдаётся", r.status_code == 200, f"status={r.status_code}")
    check("заголовок страницы — «Обучение»", "<h1>Обучение</h1>" in html)
    check("пункт меню «Обучение» активен",
          '<a href="/lessons" class="active">Обучение</a>' in html)
    check("меню содержит соседние разделы",
          '<a href="/turnover">Оборачиваемость</a>' in html and '<a href="/settings">Настройки</a>' in html)
    check("страница тянет каталог из API", '"/api/lessons"' in html)
    check("тумблер подсказок ходит в /api/prefs/hints", '"/api/prefs/hints"' in html)
    check("блок FAQ на месте", "Частые вопросы" in html and html.count("<summary>") >= 8,
          f"вопросов={html.count('<summary>')}")
    check("блок «Не нашли ответ» на месте", "Не нашли ответ" in html)
    check("без настроенного контакта кнопки поддержки нет",
          "Написать в поддержку" not in html)
    tpl_src = (ROOT / "templates" / "lessons.html").read_text(encoding="utf-8")
    check("в шаблоне есть include подсказок", '{% include "_hints.html" %}' in tpl_src)
    check("подсказки реально подставились (include отработал)",
          "{% include" not in html and len(html) > len(tpl_src),
          f"html={len(html)} tpl={len(tpl_src)}")

    os.environ["OBOROT_SUPPORT_EMAIL"] = "help@example.org"
    html = a.get("/lessons").text
    check("с OBOROT_SUPPORT_EMAIL появляется кнопка",
          "Написать в поддержку" in html and "mailto:help@example.org" in html)
    os.environ["OBOROT_SUPPORT_URL"] = "https://t.me/oborot_support"
    html = a.get("/lessons").text
    check("ссылка поддержки важнее почты",
          'href="https://t.me/oborot_support"' in html and "mailto:" not in html)
    os.environ.pop("OBOROT_SUPPORT_URL", None)
    os.environ.pop("OBOROT_SUPPORT_EMAIL", None)
    a.close()

    print()
    print(f"ИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        print("Провалены:", *FAIL, sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
