# -*- coding: utf-8 -*-
"""Запись решения: обстоятельства расчёта и отказ как часть решения.

Зачем. «Мастер заказа» рекомендует потратить сотни тысяч рублей. Через полгода
на вопрос «почему в августе заказали именно столько» отвечать нечем, если не
сохранены три вещи: версия алгоритма, настройки, которые тогда применялись, и
качество данных на тот момент. Настройки меняются, код меняется — восстановить
задним числом нельзя ничего.

Отдельно проверяется, что сохраняется ОТКАЗ. Раньше в базу уходили только
строки плана: система считала, почему полсотни позиций не вошли в заказ,
показывала это на экране и выбрасывала. В истории оставались одни лишь товары,
прошедшие фильтры — то есть выборка, смещённая по построению.

Проверяется:
  1) в сохранённом плане есть версия алгоритма (домен + сборка), снимок
     применённых настроек и качество данных (история, позиции без себестоимости,
     последний синк);
  2) сохраняется отсев: что не вошло, во сколько обошёлся отказ, диагностика
     пустого плана;
  3) отсев хранится в сжатом виде — без ростовок и промежуточных полей;
  4) ручная правка НЕ затирает рекомендацию системы: рядом остаётся исходное
     количество, иначе теряется сигнал «где человек исправляет алгоритм»;
  5) при удалении организации не остаётся её планов (раньше оставались
     осиротевшие строки с чужим org_id);
  6) SQLite открыт в режиме WAL — фоновый синк пишет в ту же базу, из которой
     читает веб.

Запуск из корня репозитория:  python tests/test_decision_record.py
"""
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = ROOT / "test_decision.db"
APP_PORT = int(os.environ.get("OBOROT_TEST_PORT", "8808"))

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["SCHEDULER_ENABLED"] = "0"
os.environ.setdefault("OBOROT_COMMIT", "testsha1234567")

if DB_PATH.exists():
    DB_PATH.unlink()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.db import engine  # noqa: E402
from app.main import app as oborot_app  # noqa: E402
from app.version import DOMAIN_VERSION  # noqa: E402
from sqlalchemy import text  # noqa: E402


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


def sql(query: str, *args):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(query, args).fetchall()
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
    c = httpx.Client(headers={"X-Oborot-CSRF": "1"}, base_url=base, timeout=120.0)

    print("\n== Режим базы ==")
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
    check("SQLite открыт в режиме WAL (синк пишет, веб читает — без блокировок)",
          str(mode).lower() == "wal", f"journal_mode={mode}")
    check("выставлено ожидание блокировки вместо мгновенной ошибки",
          int(busy or 0) >= 1000, f"busy_timeout={busy}")

    print("\n== Подготовка: организация с демо-данными и каналом производства ==")
    r = c.post("/register", data={"name": "Владелец", "email": "owner@test.io",
                                  "password": "secret123", "org_name": "Бренд"})
    check("регистрация", r.status_code in (200, 302, 303), f"status={r.status_code}")
    r = c.post("/api/connect/demo")
    check("демо-данные загружены", r.status_code == 200, f"status={r.status_code}")

    r = c.post("/api/productions", json={"name": "Цех"})
    pid = r.json().get("id") if r.status_code == 200 else None
    if pid:
        c.post(f"/api/productions/{pid}/setup", json={"preset": "fabric_sewing"})

    print("\n== Обстоятельства решения сохраняются ==")
    # Без production_id: демо-позиции не назначены на новый канал, и план по
    # нему вышел бы пустым — а нам нужны и строки, и отсев.
    body = {"budget": 150000, "budget_scope": "now",
            "cadence_days": 30, "safety_days": 14, "strategy": "balance"}
    r = c.post("/api/order-plan", json=body)
    check("план сохранён", r.status_code == 200, f"status={r.status_code} {r.text[:150]}")
    plan_id = r.json().get("id") if r.status_code == 200 else None

    row = sql("SELECT computed_json, result_json FROM order_plans WHERE id=?", plan_id)
    computed = json.loads(row[0][0]) if row else {}
    result = json.loads(row[0][1]) if row else {}

    algo = computed.get("algo") or {}
    check("сохранена версия домена", algo.get("domain") == DOMAIN_VERSION,
          f"algo={algo}")
    check("сохранена версия сборки (commit)", bool(algo.get("commit")),
          f"commit={algo.get('commit')}")

    st = computed.get("settings") or {}
    check("сохранено окно темпа, по которому считали", "rate_window" in st, f"settings={list(st)}")
    check("сохранён режим горизонта", "cover_mode" in st, f"settings={list(st)}")
    check("сохранены пороги классов оборачиваемости", "thresholds" in st,
          f"settings={list(st)}")

    dq = computed.get("data_quality") or {}
    check("сохранена глубина истории", int(dq.get("coverage_days") or 0) > 0, f"dq={dq}")
    check("сохранено, сколько позиций без себестоимости",
          "positions_no_cost" in dq and "positions_total" in dq, f"dq={dq}")
    check("сохранено состояние синхронизации", "sync_state" in dq, f"dq={dq}")
    check("сохранено покрытие истории (был и раньше)", "coverage" in computed,
          f"ключи={list(computed)}")

    print("\n== Отказ — тоже решение ==")
    check("сохранён список «не вошло»", isinstance(result.get("not_included"), list),
          f"тип={type(result.get('not_included')).__name__}")
    check("сохранена диагностика отсева (review)", "review" in result,
          f"ключи={list(result)}")
    check("сохранены позиции, не набравшие минимальную партию",
          "moq_skipped" in result and "moq_over_cap" in result, f"ключи={list(result)}")
    check("сохранена причина пустого плана (blocked)", "blocked" in result,
          f"ключи={list(result)}")

    ni = result.get("not_included") or []
    if ni:
        keys = set(ni[0])
        check("отсев хранится в сжатом виде — без ростовок",
              "sizes" not in keys and "rate_cover" not in keys, f"поля={sorted(keys)}")
        check("у отказа есть цена: имя, потребность и упущенная маржа",
              {"base_name", "need"} <= keys and "lost_margin" in keys,
              f"поля={sorted(keys)}")
    else:
        check("отсев пуст — проверить сжатие не на чем (не ошибка)", True,
              "not_included=[]")

    print("\n== Ручная правка не затирает рекомендацию ==")
    r = c.post("/api/order-plan/preview", json=body)
    items = (r.json().get("plan") or r.json()).get("items") or [] if r.status_code == 200 else []
    if not items:
        r = c.post("/api/order-plan", json=body)
        items = (r.json().get("plan") or {}).get("items") or []
    check("в плане есть строки для правки", bool(items), f"строк={len(items)}")
    if items:
        base_name = items[0]["base_name"]
        rec = int(items[0]["qty"])
        new_qty = max(1, rec + 7)
        body2 = dict(body, overrides={base_name: new_qty})
        r = c.post("/api/order-plan", json=body2)
        check("план с ручной правкой сохранён", r.status_code == 200,
              f"status={r.status_code}")
        pid2 = r.json().get("id") if r.status_code == 200 else None
        row2 = sql("SELECT result_json FROM order_plans WHERE id=?", pid2)
        res2 = json.loads(row2[0][0]) if row2 else {}
        edited = next((i for i in (res2.get("items") or [])
                       if i.get("base_name") == base_name), None)
        check("правка записана как финальное количество",
              edited is not None and int(edited.get("qty") or 0) == new_qty,
              f"qty={edited and edited.get('qty')} ожидали {new_qty}")
        check("рекомендация системы сохранена рядом с правкой",
              edited is not None and int(edited.get("qty_recommended", -1)) == rec,
              f"qty_recommended={edited and edited.get('qty_recommended')} ожидали {rec}")
        check("план помечен как правленный вручную",
              bool(res2.get("manual_edit")), f"manual_edit={res2.get('manual_edit')}")

    print("\n== Удаление организации не оставляет планов ==")
    before = sql("SELECT COUNT(*) FROM order_plans")[0][0]
    check("планы в базе есть", before > 0, f"строк={before}")
    r = c.post("/api/account/delete", json={
        "password": "secret123", "confirm": "УДАЛИТЬ", "mode": "purge"})
    if r.status_code != 200:
        r = c.post("/api/account/delete", json={
            "password": "secret123", "confirm": "УДАЛИТЬ"})
    check("организация удалена", r.status_code == 200,
          f"status={r.status_code} {r.text[:120]}")
    after = sql("SELECT COUNT(*) FROM order_plans")[0][0]
    check("планов удалённой организации не осталось", after == 0,
          f"было={before} стало={after}")
    for tbl in ("user_lessons", "user_prefs"):
        left = sql(f"SELECT COUNT(*) FROM {tbl}")[0][0]
        check(f"нет осиротевших строк в {tbl}", left == 0, f"строк={left}")

    c.close()
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
