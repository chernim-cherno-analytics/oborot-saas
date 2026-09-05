# -*- coding: utf-8 -*-
"""SUPPLY-3: планирование производства — границы, честность неизвестного, права.

Зачем этот набор. Слой впервые в «Поставках» ПИШЕТ то, что решил человек, а не
читает чужую таблицу. Цена ошибки здесь другая, чем у предпросмотра: не «показал
криво», а «потерял решение владельца», «выдал план за заказ», «подставил ноль
вместо незнания» или «списал материал, которого никто не списывал». Поэтому
проверяется не «работает ли форма», а что именно слой делает с неоднозначностью,
с чужими данными и с повторным нажатием.

Что доказывается:

  1) материал заводится ДО вещи и партии — это первый шаг пути, а не побочный;
  2) вещь каталога берётся по каноническому имени, а не по размерной строке;
     новинка живёт своим тождеством и несёт приватный эскиз;
  3) один материал делится на две партии, две ткани собираются в одну партию,
     у одной вещи бывает две плановые партии;
  4) 100 → 40 + 35 → 25 свободно; ни одно из чисел не выдумано;
  5) неизвестное остаётся неизвестным: `qty=None` не превращается в ноль, и
     остаток такого материала тоже неизвестен;
  6) назначение сверх известного наличия ПРЕДУПРЕЖДАЕТ числом, но не запрещает
     и не обрезает: это план, а не расход;
  7) метры не превращаются в штуки нигде: план изделий вводится отдельно;
  8) перенос метража между партиями неделим — сумма назначенного не меняется;
  9) повторный POST того же поступка не применяется дважды; правка поверх чужой
     правки отвергается 409, а не затирает её;
 10) срок бывает неизвестным, ориентировочным, точным и текстом; у него есть
     источник и автор, и он ни во что не считается;
 11) два одинаковых имени — две разные вещи;
 12) замена снимка предпросмотра (в том числе с перестановкой строк) ручные
     решения не трогает вовсе;
 13) арендаторы и роли: участник читает, владелец пишет, чужой идентификатор
     даёт 404 без различия «нет» и «чужое», readonly-подписка закрывает запись;
 14) эскиз приватен: чужой не отдаётся, не-картинка не принимается, SVG — тоже;
 15) структурно: слой не трогает `production_orders`, `CC_BATCH_ID`,
     `OrderedQty`, приёмки, формулы, МойСклад и парсер предпросмотра;
 16) миграция аддитивна: старт на «старой» базе создаёт таблицы, повторный старт
     идемпотентен, шагов старта одиннадцать и первые десять не тронуты;
 17) удаление организации уносит все строки слоя.

Живых внешних систем здесь нет ни одной: ни МойСклада, ни Google. Все данные
синтетические, PII в наборе нет.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_supply_planning.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8823"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"

for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB_PATH) + suffix)
    if p.exists():
        p.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import supply_planning as sp  # noqa: F401,E402 — граница слоя проверяется по исходникам
from app import supply_sheets as ss  # noqa: E402
from app.main import app as oborot_app  # noqa: E402

BASE = f"http://127.0.0.1:{APP_PORT}"
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


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


def client(headers=None) -> httpx.Client:
    h = {"X-Oborot-CSRF": "1"}
    h.update(headers or {})
    return httpx.Client(base_url=BASE, headers=h, timeout=60.0)


def register(c: httpx.Client, email: str, org: str, name: str = "Владелец"):
    return c.post("/register", data={"name": name, "email": email,
                                     "password": "secret123", "org_name": org})


# ── Синтетический PNG и JPEG, собранные байтами ──────────────────────────────
#
# Картинки строятся здесь, а не берутся файлом: набор обязан работать в пустом
# чекауте, а бинарник в репозитории — это ещё и вопрос «что именно на нём».

def make_png(width: int = 24, height: int = 16) -> bytes:
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def make_jpeg(width: int = 32, height: int = 20) -> bytes:
    """Минимальный JPEG: SOI, APP0 объявленной длины и SOF0 с размерами.

    Длина сегмента APP0 объявляется ЧЕСТНО (2 байта длины + содержимое), иначе
    разбор уедет мимо SOF0 — и набор проверял бы не продукт, а свою фикстуру.
    """
    import struct
    app0_body = b"JFIF\x00" + bytes([1, 1, 0, 0, 1, 0, 1, 0, 0])
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_body) + 2) + app0_body
    sof_body = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sof = b"\xff\xc0" + struct.pack(">H", len(sof_body) + 2) + sof_body
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def sizes_of(payload: dict) -> dict:
    return {m["title"]: (m["qty"], m["assigned"], m["free"]) for m in payload["materials"]}


def run() -> int:
    owner = client()
    register(owner, "sp-owner@test.io", "Бренд Один")
    owner.post("/api/connect/demo")

    # ── 1. Материал заводится ДО вещи и партии ────────────────────────────────
    #
    # Это не «удобно», а факт производства: ткань покупают партией задолго до
    # того, как решено, что из неё шьют. Слой, который требует сначала выбрать
    # вещь, заставил бы человека выдумать её ради формы.
    print("\n== Материал существует сам по себе, до дизайна ==")
    empty = owner.get("/api/supply/planning").json()
    check("пустое состояние предлагает начать с материала",
          empty["next_step"]["code"] == "add_material", empty["next_step"]["text"][:60])
    check("и не показывает ни одной выдуманной цифры",
          empty["summary"]["materials"] == 0 and empty["summary"]["batches"] == 0
          and empty["summary"]["free_known"] == 0, json.dumps(empty["summary"]))

    r = owner.post("/api/supply/planning/materials",
                   json={"title": "Ткань костюмная 100", "qty": "100", "unit": "м",
                         "source_note": "счёт от поставщика", "op_id": "m-100"})
    check("материал создан без вещи и без партии", r.status_code == 200, r.text[:160])
    board = r.json()
    mat = board["materials"][0]
    mat_id = mat["id"]
    check("количество принято как есть, свободно всё",
          mat["qty"] == 100.0 and mat["assigned"] == 0.0 and mat["free"] == 100.0,
          f"{mat['qty']} {mat['assigned']} {mat['free']}")
    check("следующий шаг ведёт дальше по пути, а не повторяет пройденный",
          board["next_step"]["code"] == "add_item", board["next_step"]["text"][:60])

    # ── 2. Вещь каталога — по каноническому имени ─────────────────────────────
    print("\n== Вещь каталога берётся по каноническому имени ==")
    cat = owner.get("/api/supply/planning/catalog").json()["options"]
    check("каталог демо-данных виден как список канонических имён",
          len(cat) > 0 and all("base_name" in o and "sizes" in o for o in cat),
          f"{len(cat)}")
    base_name = cat[0]["base_name"]
    r = owner.post("/api/supply/planning/items",
                   json={"kind": "catalog", "base_name": base_name, "op_id": "i-cat"})
    check("вещь каталога принята", r.status_code == 200, r.text[:160])
    cat_item = [i for i in r.json()["items"] if i["kind"] == "catalog"][0]
    check("хранится каноническое имя, а не идентификатор размерной строки",
          cat_item["base_name"] == base_name and str(cat_item["id"]).isdigit(),
          f"{cat_item['base_name']}")

    bad = owner.post("/api/supply/planning/items",
                     json={"kind": "catalog", "base_name": "Такой вещи нет",
                           "op_id": "i-bad"})
    check("вещь не из своего каталога отклоняется, а не создаётся молча",
          bad.status_code == 400 and "новинку" in bad.json()["detail"],
          f"{bad.status_code} {bad.text[:90]}")

    # ── 3. Полноценная новинка с приватным эскизом ────────────────────────────
    print("\n== Новинка: своё тождество и приватный эскиз ==")
    png = make_png()
    up = owner.post("/api/supply/planning/sketches",
                    files={"file": ("sketch.png", png, "image/png")})
    check("эскиз принят и разобран по самим байтам",
          up.status_code == 200 and up.json()["mime"] == "image/png"
          and up.json()["width"] == 24 and up.json()["height"] == 16,
          f"{up.status_code} {up.text[:120]}")
    sketch_id = up.json()["sketch_id"]

    jpeg = owner.post("/api/supply/planning/sketches",
                      files={"file": ("s.jpg", make_jpeg(), "image/jpeg")})
    check("JPEG тоже принят и его размеры прочитаны",
          jpeg.status_code == 200 and jpeg.json()["mime"] == "image/jpeg"
          and jpeg.json()["width"] == 32, jpeg.text[:120])

    svg = owner.post("/api/supply/planning/sketches",
                     files={"file": ("s.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                                     "image/svg+xml")})
    check("SVG не принимается: это документ, а не картинка",
          svg.status_code == 400, f"{svg.status_code} {svg.text[:90]}")
    liar = owner.post("/api/supply/planning/sketches",
                      files={"file": ("s.png", b"MZ\x90\x00 not an image", "image/png")})
    check("файл, названный PNG, но им не являющийся, тоже отклонён",
          liar.status_code == 400, f"{liar.status_code} {liar.text[:90]}")

    got = owner.get(f"/api/supply/planning/sketches/{sketch_id}")
    check("свой эскиз отдаётся и это те же байты",
          got.status_code == 200 and got.content == png, f"{got.status_code}")
    check("отдаётся с nosniff и без публичного кэша",
          got.headers.get("x-content-type-options") == "nosniff"
          and "no-store" in (got.headers.get("cache-control") or ""),
          str(dict(got.headers))[:120])

    r = owner.post("/api/supply/planning/items",
                   json={"kind": "draft", "title": "Новинка Б", "sketch_id": sketch_id,
                         "op_id": "i-new"})
    check("новинка создана вместе с эскизом", r.status_code == 200, r.text[:160])
    new_item = [i for i in r.json()["items"] if i["title"] == "Новинка Б"][0]
    check("эскиз привязан к новинке", new_item["sketch_id"] == sketch_id,
          str(new_item))

    # ── 4. Два одинаковых имени — две разные вещи ─────────────────────────────
    print("\n== Одинаковые названия не склеиваются ==")
    owner.post("/api/supply/planning/items",
               json={"kind": "draft", "title": "Платье миди", "op_id": "i-dup1"})
    dup = owner.post("/api/supply/planning/items",
                     json={"kind": "draft", "title": "Платье миди", "op_id": "i-dup2"})
    same = [i for i in dup.json()["items"] if i["title"] == "Платье миди"]
    check("две вещи с одинаковым именем существуют раздельно",
          len(same) == 2 and same[0]["id"] != same[1]["id"], str(same))

    # ── 5. Две плановые партии одной вещи; 100 → 40 + 35 → 25 свободно ────────
    print("\n== Две партии одной вещи, метраж расписан, остаток честный ==")
    r = owner.post("/api/supply/planning/batches",
                   json={"item_id": new_item["id"], "title": "Партия А",
                         "plan_qty": "30", "due_kind": "approx",
                         "due_text": "к середине ноября", "due_source": "цех",
                         "op_id": "b-a"})
    check("плановая партия создана", r.status_code == 200, r.text[:160])
    r = owner.post("/api/supply/planning/batches",
                   json={"item_id": new_item["id"], "title": "Партия Б",
                         "plan_qty": "20", "due_kind": "unknown", "op_id": "b-b"})
    board = r.json()
    bids = {b["title"]: b["id"] for b in board["batches"]}
    check("у ОДНОЙ вещи две разные плановые партии",
          len(bids) == 2 and len({b["item_id"] for b in board["batches"]}) == 1,
          str(sorted(bids)))
    check("планы изделий введены отдельно и не выведены из метража",
          sorted(b["plan_qty"] for b in board["batches"]) == [20.0, 30.0],
          str([b["plan_qty"] for b in board["batches"]]))

    owner.post("/api/supply/planning/assignments",
               json={"material_id": mat_id, "batch_id": bids["Партия А"],
                     "qty": "40", "op_id": "a-40"})
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": mat_id, "batch_id": bids["Партия Б"],
                         "qty": "35", "op_id": "a-35"})
    board = r.json()
    m = board["materials"][0]
    check("назначено 40 + 35 = 75, свободно 25",
          m["assigned"] == 75.0 and m["free"] == 25.0 and m["qty"] == 100.0,
          f"{m['assigned']} {m['free']}")
    check("предупреждения о превышении нет — его и не должно быть",
          m["over"] is False and m["warning"] == "", m["warning"][:80])
    check("следующий шаг называет неназначенный остаток числом",
          board["next_step"]["code"] == "assign" and "25" in board["next_step"]["text"],
          board["next_step"]["text"][:90])
    check("метраж и штуки в сводке НЕ смешаны",
          board["summary"]["free_known"] == 25.0
          and board["summary"]["plan_known"] == 50.0,
          json.dumps(board["summary"]))

    # ── 6. Одна ткань на две вещи; две ткани на одну партию ───────────────────
    print("\n== Ткань на две вещи и две ткани на одну партию ==")
    per_batch = {b["title"]: b["assigned_total"] for b in board["batches"]}
    check("один материал расписан по двум партиям разных вещей",
          per_batch["Партия А"] == 40.0 and per_batch["Партия Б"] == 35.0,
          str(per_batch))

    r = owner.post("/api/supply/planning/materials",
                   json={"title": "Подкладка", "qty": "18", "op_id": "m-lining"})
    lining_id = [x["id"] for x in r.json()["materials"] if x["title"] == "Подкладка"][0]
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": lining_id, "batch_id": bids["Партия А"],
                         "qty": "12", "op_id": "a-lining"})
    batch_a = [b for b in r.json()["batches"] if b["title"] == "Партия А"][0]
    check("на одной партии два разных материала",
          len(batch_a["assignments"]) == 2
          and {a["material_title"] for a in batch_a["assignments"]}
              == {"Ткань костюмная 100", "Подкладка"},
          str([a["material_title"] for a in batch_a["assignments"]]))
    check("и суммарно на партии 52 — сложены метры, а не метры со штуками",
          batch_a["assigned_total"] == 52.0, str(batch_a["assigned_total"]))

    # ── 7. Неизвестное количество остаётся неизвестным ────────────────────────
    print("\n== Неизвестное количество: остаток тоже неизвестен ==")
    r = owner.post("/api/supply/planning/materials",
                   json={"title": "Ткань без замера", "qty": "", "op_id": "m-unknown"})
    unknown = [x for x in r.json()["materials"] if x["title"] == "Ткань без замера"][0]
    check("пустое количество сохранено как НЕИЗВЕСТНО, а не как ноль",
          unknown["qty"] is None and unknown["qty_known"] is False, str(unknown["qty"]))
    check("свободный остаток такого материала тоже неизвестен",
          unknown["free"] is None and unknown["free_known"] is False,
          str(unknown["free"]))
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": unknown["id"], "batch_id": bids["Партия Б"],
                         "qty": "7", "op_id": "a-unknown"})
    unknown = [x for x in r.json()["materials"] if x["id"] == unknown["id"]][0]
    check("после назначения остаток по-прежнему неизвестен, а не «минус семь»",
          unknown["assigned"] == 7.0 and unknown["free"] is None
          and unknown["over"] is False,
          f"{unknown['assigned']} {unknown['free']} {unknown['over']}")

    zero = owner.post("/api/supply/planning/materials",
                      json={"title": "Явный ноль", "qty": "0", "op_id": "m-zero"})
    z = [x for x in zero.json()["materials"] if x["title"] == "Явный ноль"][0]
    check("явно написанный ноль остаётся нулём и неизвестным не становится",
          z["qty"] == 0.0 and z["qty_known"] is True, str(z["qty"]))

    # ── 8. Превышение предупреждает, но не запрещает ──────────────────────────
    print("\n== План сверх наличия: предупреждение, а не запрет ==")
    r = owner.post("/api/supply/planning/assignments",
                   json={"material_id": lining_id, "batch_id": bids["Партия Б"],
                         "qty": "10", "op_id": "a-over"})
    check("назначение сверх известного наличия ПРИНЯТО", r.status_code == 200,
          r.text[:160])
    over = [x for x in r.json()["materials"] if x["id"] == lining_id][0]
    check("и превышение названо числом",
          over["over"] is True and over["over_by"] == 4.0 and over["assigned"] == 22.0,
          f"{over['over_by']} {over['assigned']}")
    check("предупреждение прямо говорит, что это план, а не расход",
          "план, а не расход" in over["warning"], over["warning"][:120])
    check("количество материала при этом не тронуто и не обрезано",
          over["qty"] == 18.0, str(over["qty"]))

    # ── 9. Перенос метража неделим ────────────────────────────────────────────
    #
    # Проверяется не «кнопка работает», а СУММА: перенос — один поступок, и если
    # он разложится на два шага, метраж либо потеряется, либо удвоится. Ровно на
    # этом ломаются самодельные «сначала снять, потом добавить».
    print("\n== Перенос 10 м А→Б: ничего не потеряно и не удвоено ==")
    board = owner.get("/api/supply/planning").json()
    batch_a = [b for b in board["batches"] if b["title"] == "Партия А"][0]
    a_main = [a for a in batch_a["assignments"]
              if a["material_id"] == mat_id][0]
    before = [x for x in board["materials"] if x["id"] == mat_id][0]["assigned"]
    r = owner.post("/api/supply/planning/assignments/move",
                   json={"assignment_id": a_main["id"], "to_batch_id": bids["Партия Б"],
                         "qty": "10", "rev": a_main["rev"], "op_id": "mv-10"})
    check("перенос выполнен", r.status_code == 200, r.text[:160])
    moved = r.json()
    after = [x for x in moved["materials"] if x["id"] == mat_id][0]["assigned"]
    check("сумма назначенного по материалу не изменилась ни на грамм",
          before == after == 75.0, f"{before} -> {after}")
    tot = {b["title"]: sum(a["qty"] for a in b["assignments"]
                           if a["material_id"] == mat_id) for b in moved["batches"]}
    check("десять метров ушли из А и пришли в Б",
          tot["Партия А"] == 30.0 and tot["Партия Б"] == 45.0, str(tot))
    check("в приёмнике это ОДНА строка материала, а не две",
          len([a for a in
               [b for b in moved["batches"] if b["title"] == "Партия Б"][0]["assignments"]
               if a["material_id"] == mat_id]) == 1)

    a_main2 = [a for b in moved["batches"] if b["title"] == "Партия А"
               for a in b["assignments"] if a["material_id"] == mat_id][0]
    too_much = owner.post("/api/supply/planning/assignments/move",
                          json={"assignment_id": a_main2["id"],
                                "to_batch_id": bids["Партия Б"], "qty": "999",
                                "rev": a_main2["rev"], "op_id": "mv-too"})
    check("перенести больше, чем назначено, нельзя — и это сказано числом",
          too_much.status_code == 400 and "30" in too_much.json()["detail"],
          f"{too_much.status_code} {too_much.text[:90]}")
    still = owner.get("/api/supply/planning").json()
    check("после отказа переноса ничего не сдвинулось",
          [x for x in still["materials"] if x["id"] == mat_id][0]["assigned"] == 75.0)

    # ── 10. Исправление и снятие назначения ───────────────────────────────────
    print("\n== Правка и снятие назначения ==")
    a_now = [a for b in still["batches"] if b["title"] == "Партия А"
             for a in b["assignments"] if a["material_id"] == mat_id][0]
    r = owner.post(f"/api/supply/planning/assignments/{a_now['id']}/update",
                   json={"qty": "22", "rev": a_now["rev"], "op_id": "upd-22"})
    check("назначение исправлено",
          [x for x in r.json()["materials"] if x["id"] == mat_id][0]["assigned"] == 67.0,
          str(r.status_code))
    a_now2 = [a for b in r.json()["batches"] if b["title"] == "Партия А"
              for a in b["assignments"] if a["material_id"] == mat_id][0]
    r = owner.post(f"/api/supply/planning/assignments/{a_now2['id']}/delete",
                   json={"rev": a_now2["rev"], "op_id": "del-1"})
    board = r.json()
    check("снятие назначения вернуло метраж в свободные, материал цел",
          [x for x in board["materials"] if x["id"] == mat_id][0]["assigned"] == 45.0
          and [x for x in board["materials"] if x["id"] == mat_id][0]["qty"] == 100.0,
          str([x for x in board["materials"] if x["id"] == mat_id][0]))

    # ── 11. Повторный POST и чужая правка ─────────────────────────────────────
    print("\n== Повторный клик не применяется дважды, чужая правка не затирается ==")
    payload = {"material_id": mat_id, "batch_id": bids["Партия А"], "qty": "5",
               "op_id": "a-once"}
    first = owner.post("/api/supply/planning/assignments", json=payload).json()
    second = owner.post("/api/supply/planning/assignments", json=payload).json()
    check("тот же поступок дважды даёт одно назначение",
          [x for x in first["materials"] if x["id"] == mat_id][0]["assigned"] == 50.0
          and [x for x in second["materials"] if x["id"] == mat_id][0]["assigned"] == 50.0,
          str([x for x in second["materials"] if x["id"] == mat_id][0]["assigned"]))
    third = owner.post("/api/supply/planning/assignments",
                       json={**payload, "op_id": "a-twice"}).json()
    check("а другой поступок с теми же числами — это второе назначение",
          [x for x in third["materials"] if x["id"] == mat_id][0]["assigned"] == 55.0,
          str([x for x in third["materials"] if x["id"] == mat_id][0]["assigned"]))

    # Честный устаревший rev: сначала кто-то ДРУГОЙ правку уже сделал, и только
    # потом приходит вторая с прежней редакцией. Прислать rev, который просто
    # никогда не был текущим, значило бы проверить арифметику, а не защиту.
    seen_rev = [x for x in owner.get("/api/supply/planning").json()["materials"]
                if x["id"] == mat_id][0]["rev"]
    owner.post(f"/api/supply/planning/materials/{mat_id}/update",
               json={"title": "Ткань костюмная 100", "qty": "100",
                     "rev": seen_rev, "op_id": "other-hand"})
    stale = owner.post(f"/api/supply/planning/materials/{mat_id}/update",
                       json={"qty": "90", "rev": seen_rev, "op_id": "stale-1"})
    check("правка со старой редакцией отвергнута 409, а не применена",
          stale.status_code == 409 and "другом окне" in stale.json()["detail"],
          f"{stale.status_code} {stale.text[:100]}")
    fresh = owner.get("/api/supply/planning").json()
    check("и значение осталось прежним",
          [x for x in fresh["materials"] if x["id"] == mat_id][0]["qty"] == 100.0)
    cur_rev = [x for x in fresh["materials"] if x["id"] == mat_id][0]["rev"]
    ok = owner.post(f"/api/supply/planning/materials/{mat_id}/update",
                    json={"qty": "90", "rev": cur_rev, "op_id": "fresh-1"})
    check("с текущей редакцией та же правка проходит",
          ok.status_code == 200
          and [x for x in ok.json()["materials"] if x["id"] == mat_id][0]["qty"] == 90.0,
          str(ok.status_code))

    # ── 12. Сроки: неизвестно, ориентир, точная дата, текст ───────────────────
    print("\n== Срок: четыре вида, с источником и автором, без выдумок ==")
    board = owner.get("/api/supply/planning").json()
    b_a = [b for b in board["batches"] if b["title"] == "Партия А"][0]
    b_b = [b for b in board["batches"] if b["title"] == "Партия Б"][0]
    check("ориентировочный срок показан ориентиром и несёт источник",
          b_a["due_kind"] == "approx" and b_a["due_label"] == "ориентировочно к середине ноября"
          and b_a["due_source"] == "цех" and b_a["due_author"],
          f"{b_a['due_label']} / {b_a['due_source']} / {b_a['due_author']}")
    check("неизвестный срок так и назван — без подстановки сегодняшней даты",
          b_b["due_kind"] == "unknown" and b_b["due_label"] == "срок неизвестен"
          and b_b["due_date"] == "", str(b_b["due_label"]))

    r = owner.post(f"/api/supply/planning/batches/{b_b['id']}/update",
                   json={"due_kind": "exact", "due_date": "2026-11-14",
                         "due_source": "поставщик", "rev": b_b["rev"],
                         "op_id": "due-1"})
    b_b2 = [b for b in r.json()["batches"] if b["id"] == b_b["id"]][0]
    check("точная дата принята и показана точной",
          b_b2["due_kind"] == "exact" and b_b2["due_date"] == "2026-11-14"
          and b_b2["due_label"] == "точно 2026-11-14", str(b_b2["due_label"]))
    bad_date = owner.post(f"/api/supply/planning/batches/{b_b['id']}/update",
                          json={"due_kind": "exact", "due_date": "14 ноября",
                                "rev": b_b2["rev"], "op_id": "due-bad"})
    check("негодная дата отвергнута управляемо, а не записана как есть",
          bad_date.status_code == 400, f"{bad_date.status_code} {bad_date.text[:90]}")
    free_text = owner.post(f"/api/supply/planning/batches/{b_b['id']}/update",
                           json={"due_kind": "text", "due_text": "после праздников",
                                 "due_source": "слова цеха", "rev": b_b2["rev"],
                                 "op_id": "due-2"})
    b_b3 = [b for b in free_text.json()["batches"] if b["id"] == b_b["id"]][0]
    check("срок словами сохранён как написан",
          b_b3["due_label"] == "после праздников" and b_b3["due_date"] == "",
          str(b_b3["due_label"]))

    # ── 13. История: кто, когда и что было раньше ─────────────────────────────
    print("\n== История хранит автора, время и ПРЕЖНЕЕ значение ==")
    con = sqlite3.connect(DB_PATH)
    try:
        rows = con.execute(
            "SELECT entity_kind, action, field, old_value, new_value, author, created_at"
            " FROM supply_events ORDER BY id").fetchall()
    finally:
        con.close()
    due_events = [r for r in rows if r[2] == "due"]
    qty_events = [r for r in rows if r[0] == "material" and r[2] == "qty"
                  and r[1] == "update"]
    check("правка срока записана вместе с прежним значением",
          any(e[3] == "срок неизвестен" for e in due_events)
          and any("2026-11-14" in (e[4] or "") for e in due_events),
          str(due_events[:2]))
    check("правка количества записана с прежним значением и автором",
          any(e[3] == "100.0" and e[4] == "90.0" and e[5] for e in qty_events),
          str(qty_events[:2]))
    check("у каждой записи журнала есть время",
          all(r[6] for r in rows), f"{len(rows)} записей")

    # ── 14. Предпросмотр и план — разные носители ─────────────────────────────
    #
    # Снимок SUPPLY-2 заменяется ЦЕЛИКОМ при каждом обновлении, и это его
    # свойство. Ручное решение обязано это пережить, поэтому оно живёт в своих
    # таблицах, а не в снимке. Проверяется не «мы так решили», а факт: снимок
    # переписан дважды, в том числе с перестановкой строк, — план не изменился
    # ни в одном байте.
    print("\n== Повторный импорт и перестановка строк не трогают ручные решения ==")
    before_plan = owner.get("/api/supply/planning").json()

    def write_snapshot(rows):
        envelope = {
            "schema_version": ss.ENVELOPE_SCHEMA_VERSION,
            "parser_version": ss.PARSER_VERSION,
            "spreadsheet_id": "1AbCdEf_ghijklmnop-QRSTUV0123456789wxyz",
            "sheet_names": ["Осень 26", "НГ 26/27"],
            "content_sha256": "a" * 64,
            "last_attempt_at": "2026-09-05T10:00:00+00:00",
            "last_success_at": "2026-09-05T10:00:00+00:00",
            "fetched_at": "2026-09-05T10:00:00+00:00",
            "last_error": "",
            "last_attempt_source": {"spreadsheet_id": "1AbCdEf_ghijklmnop-QRSTUV0123456789wxyz",
                                    "sheet_names": ["Осень 26", "НГ 26/27"]},
            "schema": {}, "counts": ss.build_counts(rows, ["Осень 26", "НГ 26/27"]),
            "rows": rows,
        }
        c2 = sqlite3.connect(DB_PATH)
        try:
            row = c2.execute("SELECT id, config_json FROM connections"
                             " ORDER BY id LIMIT 1").fetchone()
            cfg = json.loads(row[1] or "{}")
            cfg[ss.ENVELOPE_KEY] = envelope
            c2.execute("UPDATE connections SET config_json = ? WHERE id = ?",
                       (json.dumps(cfg, ensure_ascii=False), row[0]))
            c2.commit()
        finally:
            c2.close()

    def snap_row(index, name):
        sizes = {"XS": 1, "S": 1, "M": 1, "L": 1, "XL": 1}
        return {
            "sheet_name": "Осень 26", "source_row": index, "anchor_row": index,
            "is_blank": False, "article_raw": f"A{index}", "name_raw": name,
            "article": f"A{index}", "name": name, "color_raw": "Чёрный",
            "qty_meters_raw": "", "sketch_raw": "", "sizes": sizes,
            "sizes_raw": {k: "1" for k in sizes}, "size_sum": 5,
            "source_total_raw": "5", "source_total": 5,
            "comments_raw": ["", "", ""], "source_status_raw": "",
            "production_raw": "", "components_raw": "", "price_raw": "",
            "unknown_raw": {}, "issues": [], "labels": [],
            "needs_review": False, "invalid": False,
        }

    write_snapshot([snap_row(3, "Позиция один"), snap_row(4, "Позиция два")])
    mid_plan = owner.get("/api/supply/planning").json()
    write_snapshot([snap_row(4, "Позиция два"), snap_row(3, "Позиция один")])
    after_plan = owner.get("/api/supply/planning").json()
    check("замена снимка не изменила план ни в одном поле",
          json.dumps(before_plan, sort_keys=True, ensure_ascii=False)
          == json.dumps(mid_plan, sort_keys=True, ensure_ascii=False),
          "первый импорт")
    check("перестановка строк источника тоже не изменила план",
          json.dumps(before_plan, sort_keys=True, ensure_ascii=False)
          == json.dumps(after_plan, sort_keys=True, ensure_ascii=False),
          "второй импорт")
    preview = owner.get("/api/supply/sheets").json()
    check("а сам предпросмотр при этом читается и живёт своей жизнью",
          preview.get("configured") is True and len(preview.get("rows", [])) == 2,
          str(len(preview.get("rows", []))))

    # ── 15. Арендаторы, роли, подписка ────────────────────────────────────────
    print("\n== Чужая организация, участник и readonly ==")
    other = client()
    register(other, "sp-other@test.io", "Бренд Два")
    other_board = other.get("/api/supply/planning").json()
    check("у чужой организации свой пустой план",
          other_board["materials"] == [] and other_board["batches"] == [],
          str(len(other_board["materials"])))
    stolen = other.get(f"/api/supply/planning/sketches/{sketch_id}")
    check("чужой эскиз не отдаётся", stolen.status_code == 404,
          f"{stolen.status_code}")
    check("и отказ не рассказывает, существует ли он вообще",
          stolen.json().get("detail") == "Эскиз не найден.",
          str(stolen.json())[:90])
    ghost = other.get("/api/supply/planning/sketches/999999")
    check("несуществующий и чужой отвечают ОДИНАКОВО",
          ghost.status_code == stolen.status_code
          and ghost.json().get("detail") == stolen.json().get("detail"),
          f"{ghost.status_code}")
    foreign = other.post("/api/supply/planning/assignments",
                         json={"material_id": mat_id, "batch_id": bids["Партия А"],
                               "qty": "1", "op_id": "x-1"})
    check("назначить чужой материал нельзя", foreign.status_code == 404,
          f"{foreign.status_code} {foreign.text[:90]}")
    foreign_upd = other.post(f"/api/supply/planning/materials/{mat_id}/update",
                             json={"qty": "1", "op_id": "x-2"})
    check("и править чужой материал тоже", foreign_upd.status_code == 404,
          f"{foreign_upd.status_code}")
    untouched = owner.get("/api/supply/planning").json()
    check("после чужих попыток свой план не изменился",
          json.dumps(untouched, sort_keys=True, ensure_ascii=False)
          == json.dumps(after_plan, sort_keys=True, ensure_ascii=False))

    anon = httpx.Client(base_url=BASE, headers={"X-Oborot-CSRF": "1"}, timeout=30.0)
    check("аноним не читает план", anon.get("/api/supply/planning").status_code == 401)
    check("и не пишет",
          anon.post("/api/supply/planning/materials",
                    json={"title": "x"}).status_code == 401)
    no_csrf = httpx.Client(base_url=BASE, timeout=30.0,
                           cookies=dict(owner.cookies))
    check("запись без CSRF-заголовка отклонена",
          no_csrf.post("/api/supply/planning/materials",
                       json={"title": "x"}).status_code == 403)
    anon.close()
    no_csrf.close()

    # Участник читает и НЕ пишет. Роль проверяется отдельно от подписки: это
    # разные запреты, и путать их нельзя — участник не пишет никогда, а владелец
    # в readonly не пишет временно.
    import bcrypt
    con = sqlite3.connect(DB_PATH)
    try:
        org_id = con.execute("SELECT id FROM orgs ORDER BY id LIMIT 1").fetchone()[0]
        pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
        cur = con.execute("INSERT INTO users (email, pw_hash, name, created_at)"
                          " VALUES (?,?,?,datetime('now'))",
                          ("sp-member@test.io", pw, "Участник"))
        con.execute("INSERT INTO memberships (user_id, org_id, role)"
                    " VALUES (?,?,'member')", (cur.lastrowid, org_id))
        con.commit()
    finally:
        con.close()
    member = client()
    member.post("/login", data={"email": "sp-member@test.io", "password": "secret123"})
    mb = member.get("/api/supply/planning")
    check("участник видит план организации", mb.status_code == 200, str(mb.status_code))
    check("и ему прямо сказано, что запись не его",
          mb.json()["can_write"] is False
          and mb.json()["next_step"]["code"] == "readonly",
          mb.json()["next_step"]["text"][:60])
    mw = member.post("/api/supply/planning/materials",
                     json={"title": "Участник пишет", "op_id": "mem-1"})
    check("участник не создаёт материал", mw.status_code == 403, str(mw.status_code))
    mm = member.post("/api/supply/planning/assignments",
                     json={"material_id": mat_id, "batch_id": bids["Партия А"],
                           "qty": "1", "op_id": "mem-2"})
    check("и не назначает метраж", mm.status_code == 403, str(mm.status_code))
    ms = member.post("/api/supply/planning/sketches",
                     files={"file": ("s.png", make_png(), "image/png")})
    check("и не грузит эскизы", ms.status_code == 403, str(ms.status_code))
    check("но чужой эскиз своей организации ему виден — он же её участник",
          member.get(f"/api/supply/planning/sketches/{sketch_id}").status_code == 200)

    # Гейт подписки: readonly закрывает запись целиком, чтение остаётся.
    print("\n== Приостановленная подписка: чтение открыто, запись закрыта ==")
    os.environ["OBOROT_SUBSCRIPTION_GATE"] = "1"
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '-5 day'),"
                    " paid_until = NULL")
        con.commit()
    finally:
        con.close()
    try:
        ro_read = owner.get("/api/supply/planning")
        ro_write = owner.post("/api/supply/planning/materials",
                              json={"title": "В readonly", "op_id": "ro-1"})
        ro_sketch = owner.post("/api/supply/planning/sketches",
                               files={"file": ("s.png", make_png(), "image/png")})
        check("в readonly план по-прежнему читается", ro_read.status_code == 200,
              str(ro_read.status_code))
        check("а запись отклонена кодом 402, а не молча", ro_write.status_code == 402,
              f"{ro_write.status_code} {ro_write.text[:80]}")
        check("эскиз в readonly тоже не грузится", ro_sketch.status_code == 402,
              str(ro_sketch.status_code))
        after_ro = owner.get("/api/supply/planning").json()
        check("и ни одной строки в readonly не появилось",
              not any(m["title"] == "В readonly" for m in after_ro["materials"]))
    finally:
        os.environ["OBOROT_SUBSCRIPTION_GATE"] = "0"
        con = sqlite3.connect(DB_PATH)
        try:
            con.execute("UPDATE orgs SET trial_ends_at = datetime('now', '+30 day')")
            con.commit()
        finally:
            con.close()

    # ── 16. Ввод: границы и мусор ─────────────────────────────────────────────
    print("\n== Ввод проверяется, а не «чинится» ==")
    cases = [
        ("пустое название", {"title": "  ", "op_id": "v1"}, 400),
        ("название длиннее предела", {"title": "я" * 201, "op_id": "v2"}, 400),
        ("количество словом", {"title": "Ткань", "qty": "много", "op_id": "v3"}, 400),
        ("отрицательное количество", {"title": "Ткань", "qty": "-5", "op_id": "v4"}, 400),
        ("количество за пределом", {"title": "Ткань", "qty": "9999999", "op_id": "v5"}, 400),
    ]
    for label, payload, expect in cases:
        resp = owner.post("/api/supply/planning/materials", json=payload)
        check(f"отклонено управляемо: {label}", resp.status_code == expect,
              f"{resp.status_code} {resp.text[:80]}")
    comma = owner.post("/api/supply/planning/materials",
                       json={"title": "Запятая", "qty": "12,5", "op_id": "v6"})
    check("запятая как разделитель принимается — это человек, а не парсер",
          comma.status_code == 200
          and [m for m in comma.json()["materials"] if m["title"] == "Запятая"][0]["qty"] == 12.5,
          comma.text[:100])
    zero_assign = owner.post("/api/supply/planning/assignments",
                             json={"material_id": mat_id, "batch_id": bids["Партия А"],
                                   "qty": "0", "op_id": "v7"})
    check("назначение нуля отклонено: это не назначение",
          zero_assign.status_code == 400, f"{zero_assign.status_code}")
    ghost_batch = owner.post("/api/supply/planning/assignments",
                             json={"material_id": mat_id, "batch_id": 999999,
                                   "qty": "1", "op_id": "v8"})
    check("назначение на несуществующую партию — 404",
          ghost_batch.status_code == 404, f"{ghost_batch.status_code}")

    # ── 17. Структурно: слой не касается партий, заказов и формул ─────────────
    #
    # Проверка по ИСХОДНИКАМ, а не по поведению: поведение показывает, что слой
    # сегодня ничего не сломал, а исходники — что сломать нечем. Ровно так же
    # проверяется граница SUPPLY-1 и SUPPLY-2.
    print("\n== Слой физически не связан с партиями, заказами и формулами ==")
    def code_only(path: Path) -> str:
        """Исходник без комментариев и строк документации.

        Искать запретные имена в СЫРОМ тексте нельзя: этот слой обязан объяснять
        в комментариях, чего он НЕ делает, — и такое объяснение краснело бы само
        от себя. Проверять надо код, поэтому дерево разбирается и обратно
        собираются только исполняемые узлы.
        """
        import ast as _ast
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if (isinstance(node, (_ast.Module, _ast.FunctionDef,
                                  _ast.AsyncFunctionDef, _ast.ClassDef))
                    and node.body and isinstance(node.body[0], _ast.Expr)
                    and isinstance(node.body[0].value, _ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [_ast.Pass()]
        return _ast.unparse(tree)

    src_plan = code_only(ROOT / "app" / "supply_planning.py")
    src_routes = code_only(ROOT / "app" / "routes_supply_planning.py")
    forbidden = ("ProductionOrder", "OrderedQty", "OrderReceipt", "OrderPlan",
                 "cc_batch_id", "CC_BATCH_ID", "new_cc_batch_id")
    hits = [w for w in forbidden if w in src_plan or w in src_routes]
    check("в КОДЕ слоя нет ни заказов, ни приёмок, ни идентификатора партии",
          not hits, str(hits))
    # Перенос строк не должен влиять на смысл проверки, поэтому текст
    # нормализуется по пробелам: иначе она краснела бы от переформатирования.
    raw_plan = " ".join(
        (ROOT / "app" / "supply_planning.py").read_text(encoding="utf-8").split())
    check("а в комментариях граница названа прямо — иначе её пришлось бы помнить",
          "CC_BATCH_ID" in raw_plan and "не выдаётся и не имитируется" in raw_plan,
          "CC_BATCH_ID … не выдаётся и не имитируется")
    net = [w for w in ("httpx", "requests", "urllib", "socket") if w in src_plan]
    check("и ни одного сетевого клиента — слой офлайновый по построению",
          not net, str(net))
    check("предпросмотр слой тоже не читает: снимок ему не нужен",
          "supply_sheets" not in src_plan and "supply_sheets" not in src_routes)
    check("пересчёта метров в штуки нет ни в одной строке кода",
          "plan_qty" in src_plan and "* qty" not in src_plan
          and "qty *" not in src_plan)

    parser_src = (ROOT / "app" / "supply_sheets.py").read_text(encoding="utf-8")
    check("версия парсера предпросмотра не тронута",
          'PARSER_VERSION = "supply-sheets-parser-4"' in parser_src,
          "supply-sheets-parser-4")
    check("и версия envelope тоже",
          "ENVELOPE_SCHEMA_VERSION = 1" in parser_src)

    con = sqlite3.connect(DB_PATH)
    try:
        orders = con.execute("SELECT COUNT(*) FROM production_orders").fetchone()[0]
        ordered = con.execute("SELECT COUNT(*) FROM ordered_qty").fetchone()[0]
    finally:
        con.close()
    check("за весь сценарий не создано ни одного заказа и ни одной строки «В заказе»",
          orders == 0 and ordered == 0, f"orders={orders} ordered_qty={ordered}")

    # ── 18. Миграция: аддитивна, идемпотентна, шагов одиннадцать ──────────────
    print("\n== Миграция: новый шаг сверху, старые десять не тронуты ==")
    from app.main import STARTUP_SCHEMA_STEPS
    check("шагов старта одиннадцать", len(STARTUP_SCHEMA_STEPS) == 11,
          str(len(STARTUP_SCHEMA_STEPS)))
    check("первые десять пар (id, позиция) не изменились",
          STARTUP_SCHEMA_STEPS[:10] == (
              ("init_db", 1), ("lessons.ensure_schema", 2),
              ("exclusions.ensure_schema", 3), ("ms_sync.ensure_schema", 4),
              ("ms_sync.reset_stale_running", 5), ("ms_writeback.ensure_schema", 6),
              ("ms_vendor.ensure_schema", 7), ("subscription.ensure_schema", 8),
              ("subscription.log_preview", 9), ("models.ensure_supply_schema", 10)),
          str(STARTUP_SCHEMA_STEPS[:10]))
    check("новый шаг дописан в конец с новым id и позицией 11",
          STARTUP_SCHEMA_STEPS[10] == ("models.ensure_supply_planning_schema", 11),
          str(STARTUP_SCHEMA_STEPS[10]))

    # «Старая» база: таблиц слоя нет вовсе — шаг обязан их создать и не упасть
    # при повторном вызове.
    from sqlalchemy import create_engine, inspect as sa_inspect
    from app import models as _models
    old_db = ROOT / "test_supply_planning_old.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(old_db) + suffix)
        if p.exists():
            p.unlink()
    eng = create_engine(f"sqlite:///{old_db}")
    _models.Base.metadata.create_all(bind=eng, tables=[
        _models.Org.__table__, _models.User.__table__])
    before_tables = set(sa_inspect(eng).get_table_names())
    check("на «старой» базе таблиц слоя нет",
          not {"supply_materials", "supply_batches"} & before_tables,
          str(sorted(before_tables)))
    _models.ensure_supply_planning_schema(bind=eng)
    after_tables = set(sa_inspect(eng).get_table_names())
    need = {"supply_materials", "supply_items", "supply_batches",
            "supply_assignments", "supply_sketches", "supply_events"}
    check("шаг создал все шесть таблиц", need <= after_tables,
          str(sorted(need - after_tables)))
    _models.ensure_supply_planning_schema(bind=eng)
    check("повторный вызов шага не падает и ничего не ломает",
          need <= set(sa_inspect(eng).get_table_names()))
    idx = {i["name"] for i in sa_inspect(eng).get_indexes("supply_events")}
    check("частичный замок повторного поступка на месте",
          "ux_supply_events_op" in idx, str(sorted(idx)))
    with eng.connect() as conn:
        from sqlalchemy import text as sa_text
        conn.execute(sa_text(
            "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
            " field, old_value, new_value, author, op_id, created_at)"
            " VALUES (1,'material',1,'create','','','','a','dup', datetime('now'))"))
        conn.commit()
        dup_failed = False
        try:
            conn.execute(sa_text(
                "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
                " field, old_value, new_value, author, op_id, created_at)"
                " VALUES (1,'material',2,'create','','','','a','dup', datetime('now'))"))
            conn.commit()
        except Exception:
            dup_failed = True
            conn.rollback()
        conn.execute(sa_text(
            "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
            " field, old_value, new_value, author, op_id, created_at)"
            " VALUES (1,'material',3,'create','','','','a','', datetime('now'))"))
        conn.execute(sa_text(
            "INSERT INTO supply_events (org_id, entity_kind, entity_id, action,"
            " field, old_value, new_value, author, op_id, created_at)"
            " VALUES (1,'material',4,'create','','','','a','', datetime('now'))"))
        conn.commit()
    check("одинаковый op_id второй раз не проходит",
          dup_failed, "второй INSERT должен был упасть")
    check("а пустых op_id может быть сколько угодно — замок частичный", True)
    eng.dispose()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(old_db) + suffix)
        if p.exists():
            p.unlink()

    # ── 19. Удаление организации уносит весь слой ────────────────────────────
    print("\n== Полнота удаления арендатора ==")
    from app.tenancy import (org_purge_models, purge_completeness_violations,
                             purge_order_violations)
    names = {m.__tablename__ for m in org_purge_models()}
    check("все шесть таблиц слоя входят в набор удаления организации",
          need <= names, str(sorted(need - names)))
    check("сторож полноты не находит нарушений",
          purge_completeness_violations() == [], str(purge_completeness_violations()))
    check("и сторож порядка тоже",
          purge_order_violations() == [], str(purge_order_violations()))

    del_c = client()
    register(del_c, "sp-doomed@test.io", "Бренд Три")
    del_c.post("/api/supply/planning/materials",
               json={"title": "Уйдёт вместе с организацией", "qty": "5", "op_id": "d1"})
    del_c.post("/api/supply/planning/sketches",
               files={"file": ("s.png", make_png(), "image/png")})
    con = sqlite3.connect(DB_PATH)
    try:
        doomed = con.execute("SELECT id FROM orgs ORDER BY id DESC LIMIT 1").fetchone()[0]
        before_cnt = con.execute("SELECT COUNT(*) FROM supply_materials"
                                 " WHERE org_id = ?", (doomed,)).fetchone()[0]
    finally:
        con.close()
    check("у обречённой организации есть строки слоя", before_cnt == 1, str(before_cnt))
    dele = del_c.post("/api/account/delete",
                      json={"password": "secret123", "confirm": "УДАЛИТЬ",
                            "mode": "org"})
    check("организация удалена", dele.status_code in (200, 204), str(dele.status_code))
    con = sqlite3.connect(DB_PATH)
    try:
        left = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE org_id = ?",
                               (doomed,)).fetchone()[0] for t in sorted(need)}
    finally:
        con.close()
    check("ни одной строки слоя от неё не осталось",
          all(v == 0 for v in left.values()), str(left))
    check("а строки другой организации на месте",
          owner.get("/api/supply/planning").json()["materials"], "план владельца цел")

    member.close()
    other.close()
    del_c.close()
    owner.close()

    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    for name in FAIL:
        print(f"  FAIL {name}")
    return 1 if FAIL else 0


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


if __name__ == "__main__":
    sys.exit(main())
