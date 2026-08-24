# -*- coding: utf-8 -*-
"""ЖИВОЙ контракт `syncId` в JSON API 1.2 — условие слияния DATA-1/DATA-2.

Зачем этот файл существует отдельно от остальных тестов.

Вся защита от дубля заказа поставщику (D-36) держится на одном обещании чужой
системы: **повторный `POST` с уже занятым `syncId` обновляет существующую
сущность, а не создаёт вторую**. В наборе `test_writeback_idempotency.py` это
обещание закреплено МОКОМ. Мок — честная модель того, как мы поняли контракт,
и он ловит регрессии нашего кода. Но доказательством поведения МойСклада он не
является: мок написали мы, и он подтвердит ровно то, что мы в него заложили.

Поэтому здесь — единственная проверка, которая может закрыть вопрос: тот же
сценарий на ЖИВОМ аккаунте. Пока она не пройдена на боевом токене, правка
DATA-1/DATA-2 остаётся с открытым гейтом (см. TECH_DEBT.md, DATA-1).

Что проверяется:
  1) POST /entity/purchaseorder с нашим syncId принимается (а не 412);
  2) GET /entity/purchaseorder?filter=syncId=… находит созданный документ;
  3) ПОВТОРНЫЙ POST с тем же syncId возвращает ТОТ ЖЕ документ (тот же id),
     а второго документа в аккаунте не появляется;
  4) то же для /entity/counterparty.

── Запуск (только вручную, только осознанно) ────────────────────────────────

Тест СОЗДАЁТ в указанном аккаунте настоящие сущности: одного контрагента и до
двух непроведённых заказов поставщику. Удалить их придётся руками — их имена
печатаются в конце. Поэтому запуск закрыт двумя переменными сразу, и ни одна
из них не выставляется ни в CI, ни в dev-окружении:

    OBOROT_LIVE_MS_TOKEN=<токен аккаунта> \\
    OBOROT_LIVE_MS_CONFIRM=я-понимаю-что-создам-документы \\
        python tests/test_ms_syncid_live.py

Без обеих переменных набор ЧЕСТНО сообщает «не выполнен» и возвращает код 2 —
не 0. Молчаливый «пропущен = зелёный» здесь недопустим: именно так открытый
гейт и превращается в закрытый на бумаге.

Аккаунт для запуска — тестовый или демонстрационный. Боевой аккаунт клиента
для этого использовать не нужно: контракт API от аккаунта не зависит.
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOKEN = os.environ.get("OBOROT_LIVE_MS_TOKEN", "")
CONFIRM = os.environ.get("OBOROT_LIVE_MS_CONFIRM", "")

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


async def run() -> int:
    from app.ms_client import MoySkladClient

    tag = uuid.uuid4().hex[:8]
    agent_sync = str(uuid.uuid4())
    doc_sync = str(uuid.uuid4())
    agent_name = f"Оборот · контрактный тест {tag}"
    created: list[str] = []

    async with MoySkladClient(TOKEN) as client:
        print("\n== Контрагент: syncId ==")
        agent = await client.create_counterparty(agent_name, agent_sync)
        agent_href = ((agent.get("meta") or {}).get("href")) or ""
        created.append(f"контрагент «{agent_name}»")
        check("POST /entity/counterparty принял syncId", bool(agent_href),
              f"href={agent_href}")
        found = await client.find_counterparty_by_sync_id(agent_sync)
        check("GET filter=syncId= находит контрагента",
              found is not None and ((found.get("meta") or {}).get("href")) == agent_href,
              f"found={found and found.get('id')}")
        again = await client.create_counterparty(agent_name, agent_sync)
        check("ПОВТОРНЫЙ POST с тем же syncId вернул ТОГО ЖЕ контрагента",
              again.get("id") == agent.get("id"),
              f"первый={agent.get('id')} второй={again.get('id')}")

        print("\n== Заказ поставщику: syncId ==")
        orgs = await client.fetch_organizations()
        if not orgs:
            check("в аккаунте есть юрлицо", False, "organization не найдено")
            return 1
        assortment = await client.fetch_assortment()
        sku = next((r for r in assortment
                    if ((r.get("meta") or {}).get("href")) and not r.get("archived")), None)
        if sku is None:
            check("в аккаунте есть хоть одна позиция ассортимента", False, "пусто")
            return 1
        meta = sku["meta"]
        payload = {
            "organization": {"meta": orgs[0].get("meta") or {}},
            "agent": {"meta": agent.get("meta") or {}},
            "positions": [{
                "assortment": {"meta": {"href": meta["href"],
                                        "type": meta.get("type") or "product",
                                        "mediaType": "application/json"}},
                "quantity": 1,
                "price": 0,
            }],
            "description": f"Оборот · контрактный тест syncId {tag}. Можно удалить.",
            "applicable": False,   # непроведённый: на остатки и отчёты не влияет
            "syncId": doc_sync,
        }
        doc = await client.create_purchase_order(payload)
        created.append(f"заказ поставщику «{doc.get('name')}»")
        check("POST /entity/purchaseorder принял syncId", bool(doc.get("id")),
              f"id={doc.get('id')} name={doc.get('name')}")
        by_sync = await client.find_purchase_orders_by_sync_id(doc_sync)
        check("GET filter=syncId= находит документ РОВНО ОДИН",
              len(by_sync) == 1 and by_sync[0].get("id") == doc.get("id"),
              f"найдено={len(by_sync)}")
        repeat = await client.create_purchase_order(payload)
        check("ПОВТОРНЫЙ POST с тем же syncId вернул ТОТ ЖЕ документ "
              "(upsert, а не второй заказ поставщику)",
              repeat.get("id") == doc.get("id"),
              f"первый={doc.get('id')} второй={repeat.get('id')}")
        after = await client.find_purchase_orders_by_sync_id(doc_sync)
        check("второго документа с этим ключом в аккаунте НЕ появилось",
              len(after) == 1, f"найдено={len(after)}")

    print("\nСозданы в аккаунте (удалите вручную): " + "; ".join(created))
    print(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


def main() -> int:
    if not TOKEN or not CONFIRM:
        print(
            "НЕ ВЫПОЛНЕН: живой контракт syncId не проверен.\n"
            "Это ОТКРЫТЫЙ ГЕЙТ слияния DATA-1/DATA-2, а не пропущенный тест.\n"
            "Нужны обе переменные: OBOROT_LIVE_MS_TOKEN и "
            "OBOROT_LIVE_MS_CONFIRM (см. докстринг файла).",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
