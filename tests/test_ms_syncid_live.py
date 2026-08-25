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
сценарий на ЖИВОМ аккаунте.

Что проверяется:
  1) POST /entity/counterparty с нашим syncId принимается (а не 412);
  2) GET /entity/counterparty?filter=syncId=… находит РОВНО ЕГО;
  3) ПОВТОРНЫЙ POST с тем же syncId возвращает ТОТ ЖЕ id, и после него в
     аккаунте по этому ключу по-прежнему РОВНО ОДИН объект;
  4) то же для /entity/purchaseorder.

── Разрешение владельца ─────────────────────────────────────────────────────

Влад разрешил выполнять этот сценарий на своём аккаунте МойСклад
(OWNER_DECISION 25.08.2026, Issue #2, issuecomment-5413052421, и постоянный
мандат issuecomment-5413066608). Разрешение узкое, и границы его записаны
прямо в коде ниже, а не в памяти исполнителя:

  • создаётся РОВНО ОДИН контрагент и РОВНО ОДИН заказ поставщику,
    оба с уникальной пометкой «Оборот · контрактный тест <tag>»;
  • заказ **непроведённый** (`applicable=False`), одна позиция,
    количество 1, цена 0 — ни движения денег, ни движения остатков;
  • существующие контрагенты, товары, документы и настройки НЕ меняются;
  • ничего не удаляется — ни автоматически, ни «на всякий случай».
    Созданные сущности остаются в аккаунте, их имена, id и ссылки
    печатаются в конце и записываются в Issue, чтобы владелец увидел их
    глазами;
  • ни платежей, ни приёмок, ни отгрузок, ни списаний, ни проведения.

Всё, что выходит за эти рамки, мандатом НЕ покрыто и требует отдельного
подтверждения владельца непосредственно перед действием.

── Fail-closed ──────────────────────────────────────────────────────────────

Главное свойство этого файла — он останавливается на ПЕРВОЙ неожиданности.

Раньше здесь было иначе, и это был настоящий дефект: `check()` копил FAIL и
возвращал управление дальше, поэтому после несошедшейся проверки сценарий
преспокойно шёл к следующему POST. То есть ровно в тот момент, когда чужой API
повёл себя не так, как мы думаем, мы продолжали в нём СОЗДАВАТЬ. Если бы
`syncId` не работал как upsert, второй POST и был бы тем самым дублем
финансового документа, ради недопущения которого всё написано.

Теперь проверки, за которыми следует хоть один мутирующий вызов, идут через
`gate()`: несоответствие поднимает `LiveStop`, и следующая стадия не
начинается вовсе. Ни одного «повторим и посмотрим» здесь нет и быть не должно.

Порядок такой же намеренно: сначала ВСЁ, что можно узнать чтением, и только
потом первая запись. К моменту первого POST уже известно, что юрлицо
существует, позиция существует и не в архиве, оба наших ключа в аккаунте
свободны, а имени с нашей пометкой ещё нет.

── Журнал изменяющих попыток ────────────────────────────────────────────────

Остановиться на первой неожиданности мало: надо ещё честно сказать, что мы к
этому моменту УЖЕ отправили. Каждая изменяющая попытка ложится в append-only
журнал ДО вызова, наблюдённый ответ дописывается СРАЗУ после — раньше любой
проверки, которая может остановить сценарий.

Поэтому отчёт не может соврать в самую опасную сторону. Фраза «в аккаунте
ничего не создано» допустима ровно в одном случае: изменяющих попыток не было
ни одной. Во всех прочих печатается полный журнал и требование проверить
аккаунт чтением по пометке и syncId — включая случай, когда ответ не дошёл и
сущность могла быть создана без нашего ведома.

── Запуск (только вручную, только осознанно) ────────────────────────────────

    OBOROT_LIVE_MS_TOKEN=<токен аккаунта> \\
    OBOROT_LIVE_MS_CONFIRM=я-понимаю-что-создам-документы \\
        python tests/test_ms_syncid_live.py

Токен берётся ТОЛЬКО из переменной окружения на время запуска. В репозитории
нет и не должно появиться кода, который читает боевые ключи откуда-то ещё —
из файла, из браузера, из хранилища. Сам токен никогда не печатается: всё, что
уходит в вывод, проходит через `scrub()`.

Без обеих переменных набор ЧЕСТНО сообщает «не выполнен» и возвращает код 2 —
не 0. Молчаливый «пропущен = зелёный» здесь недопустим: именно так открытый
гейт и превращается в закрытый на бумаге.

Подтверждение сверяется РОВНО с фразой выше, а не «переменная непустая»:
`OBOROT_LIVE_MS_CONFIRM=no` — это запрет, а не разрешение, и опечатка в фразе
разрешением тоже не является. Запуск, который создаёт документы в чужом
аккаунте, обязан быть невозможен по случайности.

Проверки самого этого файла, не требующие сети, живут в блоках 15–17
`tests/test_writeback_idempotency.py`: точность фразы, разбор payload и
доказательство, что после первой ошибки следующая мутирующая стадия не
вызывается.
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

# Точная фраза подтверждения. Сравнение — равенством, а не «переменная
# непустая» (ревью Codex, P2). Разница не косметическая: под «непустая»
# подходило ВСЁ, включая `OBOROT_LIVE_MS_CONFIRM=no`, `0`, `false` и опечатку
# в самой фразе. То есть значение, которым человек пытался ЗАПРЕТИТЬ запуск,
# запуск разрешало — и набор начинал создавать настоящие сущности в чужом
# аккаунте. Осознанность подтверждается только тем, что её нельзя набрать
# случайно.
CONFIRM_PHRASE = "я-понимаю-что-создам-документы"

# Пометка, по которой владелец найдёт созданное глазами. Обязательна и у
# контрагента (в имени), и у заказа (в описании) — мандат владельца, пункт 2.
MARK = "Оборот · контрактный тест"

# Полные наборы допустимых полей. Списком РАЗРЕШЁННОГО, а не запрещённого:
# запрет перечислением не закрывает поле, о котором мы не подумали, а именно
# такое поле и способно провести документ или тронуть склад.
ORDER_KEYS = frozenset({"organization", "agent", "positions", "description",
                        "applicable", "syncId"})
AGENT_KEYS = frozenset({"name", "syncId"})
POSITION_KEYS = frozenset({"assortment", "quantity", "price"})
ASSORTMENT_TYPES = frozenset({"product", "variant"})

PASS, FAIL = [], []

# ── Журнал изменяющих попыток (append-only) ──────────────────────────────────
#
# Ревью Codex, P1 (discussion_r3855229584). Раньше список созданного
# пополнялся ТОЛЬКО после полного возврата стадии. Значит, при успешном POST и
# упавшем следом GET (или repeat, или финальной проверке уникальности) стадия
# бросала LiveStop ДО возврата, список оставался пустым — и отчёт заявлял
# «в аккаунте ничего не создано», хотя сущность в чужом аккаунте уже была.
# При обрыве связи после отправки было ещё хуже: ответ неизвестен, а мы
# уверенно сообщали, что не создали ничего.
#
# Это прямо нарушало мандат владельца: оставить сущности и записать их
# безопасные имена, id и ссылки для ручного контроля. Потерянная запись о
# созданном объекте — это объект, который владелец не найдёт, потому что не
# знает, что его надо искать.
#
# Отсюда правило: попытка попадает в журнал ДО отправки, наблюдённый ответ —
# СРАЗУ после, и ни одна запись отсюда не удаляется и не переписывается.
# Журнал ведётся вокруг ВСЕХ изменяющих вызовов, включая повторные: у повтора
# свой ответ, и если id разошлись, в журнале обязаны остаться ОБА.
LEDGER: list[dict] = []

ATTEMPTED = "ПОПЫТКА_ОТПРАВЛЕНА"
CREATED = "СОЗДАНО_ПО_ОТВЕТУ_API"
UNKNOWN = "НЕИЗВЕСТНО_ВОЗМОЖНО_СОЗДАНО"


class LiveStop(Exception):
    """Первая же неожиданность: сценарий останавливается до следующей записи."""


def is_authorized(token: str, confirm: str) -> bool:
    """Разрешён ли запуск, создающий сущности в живом аккаунте МойСклад.

    Обе переменные обязательны, и подтверждение обязано быть РОВНО фразой
    CONFIRM_PHRASE. Пробелы по краям снимаются: переменная окружения часто
    приезжает с переводом строки, и это единственная вольность, которая здесь
    допускается — «no» с пробелами остаётся «no».

    Функция отдельная и чистая, чтобы её можно было проверить, ни разу не
    тронув сеть.
    """
    return bool(token.strip()) and confirm.strip() == CONFIRM_PHRASE


def scrub(text: object) -> str:
    """Вырезает токен из любого текста, уходящего в вывод.

    Через эту функцию проходит ВСЁ печатаемое, включая тексты исключений.
    Заголовки запроса и сам токен в вывод не попадают ни при каком исходе:
    отчёт о живом прогоне уходит в Issue, а Issue читают люди, у которых
    доступа к этому аккаунту нет.
    """
    out = str(text)
    tok = TOKEN.strip()
    if tok:
        out = out.replace(tok, "<токен скрыт>")
    return out


def say(text: object = "") -> None:
    print(scrub(text))


def safe_view(obj: dict) -> dict:
    """Из ответа API берутся ТОЛЬКО безопасные поля: id, name, href.

    Списком разрешённого, а не «уберём лишнее»: ответ чужого API — не наша
    структура, и складывать его в журнал целиком значит однажды положить туда
    поле, о котором мы не думали. В Issue этот журнал читают люди, у которых
    доступа к аккаунту нет.
    """
    return {
        "id": str((obj or {}).get("id") or ""),
        "name": str((obj or {}).get("name") or ""),
        "href": str((((obj or {}).get("meta") or {}).get("href")) or ""),
    }


def ledger_reset() -> None:
    """Журнал заводится ДО первой изменяющей попытки — и только здесь."""
    LEDGER.clear()


def mutating_attempts() -> int:
    """Сколько изменяющих вызовов было ОТПРАВЛЕНО (а не сколько удалось)."""
    return len(LEDGER)


async def mutate(stage: str, tag: str, name: str, sync_id: str, call,
                 *, note: str = ""):
    """Единственный путь к изменяющему вызову — и он ведёт журнал сам.

    Порядок здесь и есть смысл функции:
      1) запись «попытка отправлена» ложится в журнал ДО вызова. Если процесс
         умрёт на сетевом вызове, запись уже существует;
      2) вызов делается РОВНО ОДИН раз. Ни ретрая, ни второго POST здесь нет
         и быть не должно: повтор после неизвестного ответа — это ровно тот
         способ завести в чужом аккаунте вторую сущность;
      3) ответ разбирается на безопасные поля и дописывается СРАЗУ, до любой
         проверки. Проверка может не сойтись и остановить сценарий — запись о
         созданном объекте обязана пережить эту остановку;
      4) исключение помечает попытку как НЕИЗВЕСТНО_ВОЗМОЖНО_СОЗДАНО и летит
         дальше. «Не получилось» и «неизвестно» — разные вещи: при обрыве
         после отправки сущность в аккаунте может быть.

    `call` — функция без аргументов, возвращающая корутину. Так вызов заведомо
    не начинается раньше, чем о нём записано.
    """
    event: dict = {"stage": stage, "tag": tag, "name": name,
                   "sync_id": sync_id, "note": note,
                   "status": ATTEMPTED, "observed": []}
    LEDGER.append(event)
    try:
        result = await call()
    except BaseException as exc:  # noqa: BLE001 — статус честный, тип называем
        event["status"] = UNKNOWN
        event["error"] = type(exc).__name__
        raise
    event["status"] = CREATED
    event["observed"].append(safe_view(result))
    return result


def ledger_lines() -> list[str]:
    """Журнал в виде безопасных строк. Секретов здесь нет по построению."""
    out: list[str] = []
    for i, ev in enumerate(LEDGER, 1):
        head = (f"  {i}. [{ev['status']}] {ev['stage']}"
                + (f" ({ev['note']})" if ev.get("note") else ""))
        if ev.get("error"):
            head += f" — прервано: {ev['error']}"
        out.append(head)
        out.append(f"       имя: {ev['name']}")
        out.append(f"       syncId: {ev['sync_id']}")
        for obs in ev["observed"]:
            out.append(f"       наблюдалось: id={obs['id']} "
                       f"name={obs['name']} href={obs['href']}")
        if not ev["observed"]:
            out.append("       наблюдалось: ответ не получен — "
                       "проверьте вручную, сущность могла быть создана")
    return out


def report_ledger(tag: str) -> None:
    """Финальный отчёт о том, что мы трогали в чужом аккаунте.

    «В аккаунте ничего не создано» разрешено ровно в одном случае: не было НИ
    ОДНОЙ изменяющей попытки. Во всех остальных печатается полный журнал и
    требование проверить аккаунт чтением — даже если каждая попытка закончилась
    исключением. Мы не вправе утверждать про чужую систему больше, чем
    наблюдали.
    """
    if mutating_attempts() == 0:
        say("\nВ аккаунте ничего не создано: изменяющих попыток не было ни одной.")
        return
    say(f"\nЖУРНАЛ ИЗМЕНЯЮЩИХ ПОПЫТОК (append-only, попыток: "
        f"{mutating_attempts()}). Ничего не удаляем — всё остаётся в аккаунте "
        f"для ручной проверки владельцем:")
    for line in ledger_lines():
        say(line)
    say(f"\nПРОВЕРЬТЕ ЧТЕНИЕМ в МойСкладе по пометке «{marking(tag)}» и по "
        f"syncId из журнала выше. Если хоть одна попытка помечена "
        f"{UNKNOWN} — сущность могла быть создана, даже когда ответ не дошёл.")


def check(name: str, cond: bool, detail: str = "") -> bool:
    """Записывает результат проверки. Управление возвращает всегда.

    Годится только там, где за проверкой НЕ следует мутирующий вызов.
    Всё остальное — через gate().
    """
    if cond:
        PASS.append(name)
        say(f"  OK   {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAIL.append(name)
        say(f"  FAIL {name}  {detail}")
    return cond


def gate(name: str, cond: bool, detail: str = "") -> bool:
    """Проверка, за которой стоит запись: не сошлось — дальше не идём.

    Это и есть fail-closed. Разница с check() не стилистическая: продолжить
    после несошедшейся проверки означает послать следующий POST в API,
    который только что повёл себя не так, как мы предполагаем.
    """
    if not check(name, cond, detail):
        raise LiveStop(name)
    return True


def marking(tag: str) -> str:
    """Уникальная человекочитаемая пометка тестовой сущности."""
    return f"{MARK} {tag}"


def validate_agent_payload(payload: dict, *, tag: str, sync_id: str) -> list[str]:
    """Разбор тела создания контрагента. Пустой список — можно отправлять.

    Чистая функция: ни сети, ни состояния. Ровно поэтому её можно прогнать в
    обычном наборе тестов и не гадать, что уедет в живой аккаунт.
    """
    bad: list[str] = []
    extra = set(payload) - AGENT_KEYS
    if extra:
        bad.append(f"лишние поля: {sorted(extra)}")
    if str(payload.get("name") or "") != marking(tag):
        bad.append(f"имя не равно пометке {marking(tag)!r}")
    if str(payload.get("syncId") or "") != sync_id:
        bad.append("syncId не тот, что проверялся на свободу")
    return bad


def validate_order_payload(payload: dict, *, tag: str, sync_id: str,
                           org_href: str, assortment_href: str,
                           agent_href: str) -> list[str]:
    """Разбор тела заказа поставщику. Пустой список — можно отправлять.

    Проверяется ровно то, чем мандат владельца ограничил живой прогон:
    непроведённый документ, одна позиция, количество 1, цена 0, уникальная
    пометка, существующие юрлицо/позиция/контрагент — и НИ ОДНОГО поля сверх
    перечисленных. Отсюда же запрет любого лишнего ключа: поля вроде
    `payments`, `store` или `moment` способны затронуть деньги, склад или
    проведение, а «мы такого не передаём» — это про намерение, а не про код.
    """
    bad: list[str] = []
    extra = set(payload) - ORDER_KEYS
    if extra:
        bad.append(f"лишние поля: {sorted(extra)}")

    # applicable — строго False, а не «ложное». Ни None, ни 0, ни "": документ
    # обязан быть непроведённым по значению, а не по совпадению приведения.
    if payload.get("applicable") is not False:
        bad.append(f"applicable={payload.get('applicable')!r}, а обязан быть False")

    desc = str(payload.get("description") or "")
    if marking(tag) not in desc:
        bad.append(f"в описании нет пометки {marking(tag)!r}")

    if str(payload.get("syncId") or "") != sync_id:
        bad.append("syncId не тот, что проверялся на свободу")

    if (((payload.get("organization") or {}).get("meta") or {}).get("href")) != org_href:
        bad.append("organization не то существующее юрлицо, что выбрано заранее")
    if (((payload.get("agent") or {}).get("meta") or {}).get("href")) != agent_href:
        bad.append("agent не тот контрагент, что создан этим прогоном")

    positions = payload.get("positions")
    if not isinstance(positions, list) or len(positions) != 1:
        bad.append(f"позиций {len(positions) if isinstance(positions, list) else '?'}, "
                   "а обязана быть ровно одна")
        return bad

    pos = positions[0]
    extra_pos = set(pos) - POSITION_KEYS
    if extra_pos:
        bad.append(f"лишние поля позиции: {sorted(extra_pos)}")
    if pos.get("quantity") != 1:
        bad.append(f"quantity={pos.get('quantity')!r}, а обязано быть 1")
    if pos.get("price") != 0:
        bad.append(f"price={pos.get('price')!r}, а обязана быть 0")
    meta = ((pos.get("assortment") or {}).get("meta") or {})
    if meta.get("href") != assortment_href:
        bad.append("assortment не та существующая позиция, что выбрана заранее")
    if meta.get("type") not in ASSORTMENT_TYPES:
        bad.append(f"тип позиции {meta.get('type')!r} не product/variant")
    return bad


async def _counterparties_by_sync_id(client, sync_id: str) -> list:
    """ВСЕ контрагенты с этим syncId — списком, а не «первый попавшийся».

    Контракт обещает уникальность ключа, и проверить надо именно её: два
    ответа означают, что обещание нарушено, и узнать об этом мы обязаны,
    а не выбрать один из двух.
    """
    if not sync_id:
        return []
    return [row async for row in client.paginate(
        "/entity/counterparty", {"filter": f"syncId={sync_id}"})
        if str(row.get("syncId") or "") == sync_id]


async def prerequisites(client, tag: str, agent_sync: str, doc_sync: str) -> dict:
    """Всё, что можно узнать ЧТЕНИЕМ, — до первой записи.

    Смысл ровно один: к моменту первого POST не должно остаться вопросов,
    ответ на которые мы собирались получить, что-нибудь создав.
    """
    say("\n== 0. Подготовка: только чтение, ни одной записи ==")

    orgs = await client.fetch_organizations()
    gate("в аккаунте есть юрлицо (organization)", bool(orgs),
         f"найдено={len(orgs)}")
    org_meta = dict(orgs[0].get("meta") or {})
    org_href = str(org_meta.get("href") or "")
    gate("выбрано РОВНО ОДНО существующее юрлицо", bool(org_href),
         f"organization={orgs[0].get('name')!r} href={org_href}")

    assortment = await client.fetch_assortment()
    sku = next((r for r in assortment
                if ((r.get("meta") or {}).get("href"))
                and not r.get("archived")
                and (r.get("meta") or {}).get("type") in ASSORTMENT_TYPES), None)
    gate("в аккаунте есть неархивная позиция product/variant", sku is not None,
         f"позиций всего={len(assortment)}")
    sku_meta = dict(sku.get("meta") or {})
    assortment_href = str(sku_meta.get("href") or "")
    gate("выбрана РОВНО ОДНА существующая неархивная позиция",
         bool(assortment_href),
         f"позиция={sku.get('name')!r} тип={sku_meta.get('type')!r} "
         f"href={assortment_href}")

    # Ключи обязаны быть СВОБОДНЫ до создания. Иначе «повторный POST вернул
    # тот же id» ничего не доказывает: он мог вернуть чужой объект, который
    # занимал ключ ещё до нас.
    agents_before = await _counterparties_by_sync_id(client, agent_sync)
    gate("наш syncId контрагента в аккаунте СВОБОДЕН", not agents_before,
         f"занято={len(agents_before)}")
    docs_before = await client.find_purchase_orders_by_sync_id(doc_sync)
    gate("наш syncId заказа в аккаунте СВОБОДЕН", not docs_before,
         f"занято={len(docs_before)}")

    same_name = await client.find_counterparties_by_name(marking(tag))
    gate("контрагента с нашей пометкой ещё нет — создадим РОВНО ОДНОГО НОВОГО",
         not same_name, f"найдено={len(same_name)}")

    return {"org_meta": org_meta, "org_href": org_href,
            "sku_meta": sku_meta, "assortment_href": assortment_href,
            "org_name": str(orgs[0].get("name") or ""),
            "sku_name": str(sku.get("name") or "")}


async def stage_counterparty(client, tag: str, agent_sync: str) -> dict:
    """Контрагент: создать одного, доказать upsert, доказать отсутствие дубля."""
    say("\n== 1. Контрагент: syncId ==")
    name = marking(tag)

    # Тело собирается явно и разбирается ДО отправки — ровно то, что клиент
    # положит в POST /entity/counterparty.
    payload = {"name": name, "syncId": agent_sync}
    bad = validate_agent_payload(payload, tag=tag, sync_id=agent_sync)
    gate("тело контрагента прошло разбор до отправки", not bad, "; ".join(bad))

    agent = await mutate("контрагент: создание", tag, name, agent_sync,
                         lambda: client.create_counterparty(name, agent_sync))
    agent_id = str(agent.get("id") or "")
    agent_href = str(((agent.get("meta") or {}).get("href")) or "")
    gate("POST /entity/counterparty ПРИНЯЛ syncId", bool(agent_id and agent_href),
         f"id={agent_id} href={agent_href}")

    found = await _counterparties_by_sync_id(client, agent_sync)
    gate("GET filter=syncId= вернул РОВНО ОДНОГО — и это он",
         len(found) == 1 and str(found[0].get("id") or "") == agent_id,
         f"найдено={len(found)} ids={[r.get('id') for r in found]}")

    # Повтор идёт ТЕМ ЖЕ телом и тем же ключом. Другого повтора здесь не
    # предусмотрено: «попробуем иначе» на живом аккаунте — это и есть способ
    # завести второй объект.
    # Ответ повтора попадает в журнал ДО сверки id. Если контракт нарушен и
    # ответ пришёл с другим id, в аккаунте лежат ДВА объекта — и оба обязаны
    # быть названы, иначе владелец пойдёт искать один.
    again = await mutate("контрагент: повтор с тем же syncId", tag, name,
                         agent_sync,
                         lambda: client.create_counterparty(name, agent_sync),
                         note="проверка upsert")
    gate("ПОВТОРНЫЙ POST с тем же syncId вернул ТОТ ЖЕ id (upsert)",
         str(again.get("id") or "") == agent_id,
         f"первый={agent_id} второй={again.get('id')}")

    after = await _counterparties_by_sync_id(client, agent_sync)
    gate("после повтора по ключу по-прежнему РОВНО ОДИН контрагент",
         len(after) == 1 and str(after[0].get("id") or "") == agent_id,
         f"найдено={len(after)} ids={[r.get('id') for r in after]}")

    return {"id": agent_id, "href": agent_href, "name": name,
            "meta": dict(agent.get("meta") or {})}


async def stage_order(client, tag: str, doc_sync: str, pre: dict,
                      agent: dict) -> dict:
    """Заказ поставщику: тот же сценарий на финансовом документе."""
    say("\n== 2. Заказ поставщику: syncId ==")

    payload = {
        "organization": {"meta": pre["org_meta"]},
        "agent": {"meta": agent["meta"]},
        "positions": [{
            "assortment": {"meta": {"href": pre["assortment_href"],
                                    "type": pre["sku_meta"].get("type") or "product",
                                    "mediaType": "application/json"}},
            "quantity": 1,
            "price": 0,
        }],
        "description": f"{marking(tag)}. Проверка контракта syncId, "
                       "документ непроведённый.",
        "applicable": False,
        "syncId": doc_sync,
    }
    bad = validate_order_payload(
        payload, tag=tag, sync_id=doc_sync, org_href=pre["org_href"],
        assortment_href=pre["assortment_href"], agent_href=agent["href"])
    gate("тело заказа прошло разбор до отправки "
         "(непроведённый, одна позиция, кол-во 1, цена 0, пометка на месте)",
         not bad, "; ".join(bad))

    doc = await mutate("заказ поставщику: создание", tag, marking(tag), doc_sync,
                       lambda: client.create_purchase_order(payload))
    doc_id = str(doc.get("id") or "")
    doc_href = str(((doc.get("meta") or {}).get("href")) or "")
    gate("POST /entity/purchaseorder ПРИНЯЛ syncId", bool(doc_id),
         f"id={doc_id} name={doc.get('name')!r}")

    by_sync = await client.find_purchase_orders_by_sync_id(doc_sync)
    gate("GET filter=syncId= вернул РОВНО ОДИН — и это он",
         len(by_sync) == 1 and str(by_sync[0].get("id") or "") == doc_id,
         f"найдено={len(by_sync)} ids={[r.get('id') for r in by_sync]}")

    repeat = await mutate("заказ поставщику: повтор с тем же syncId", tag,
                          marking(tag), doc_sync,
                          lambda: client.create_purchase_order(payload),
                          note="проверка upsert")
    gate("ПОВТОРНЫЙ POST с тем же syncId вернул ТОТ ЖЕ документ "
         "(upsert, а не второй заказ поставщику)",
         str(repeat.get("id") or "") == doc_id,
         f"первый={doc_id} второй={repeat.get('id')}")

    after = await client.find_purchase_orders_by_sync_id(doc_sync)
    gate("ВТОРОГО ДОКУМЕНТА С ЭТИМ КЛЮЧОМ НЕ ПОЯВИЛОСЬ",
         len(after) == 1 and str(after[0].get("id") or "") == doc_id,
         f"найдено={len(after)} ids={[r.get('id') for r in after]}")

    return {"id": doc_id, "href": doc_href, "name": str(doc.get("name") or "")}


async def run_contract(client) -> int:
    """Весь сценарий на готовом клиенте. Токена не знает и не печатает.

    Клиент приходит снаружи намеренно: так сценарий целиком проверяется
    подставным клиентом в обычном наборе тестов — без сети и без живого
    аккаунта.
    """
    tag = uuid.uuid4().hex[:8]
    agent_sync = str(uuid.uuid4())
    doc_sync = str(uuid.uuid4())

    # Журнал заводится ДО первой изменяющей попытки, а не по её итогам.
    # Прежний список «созданного» пополнялся после полного возврата стадии, и
    # успешный POST с упавшей следом проверкой в него не попадал вовсе.
    ledger_reset()

    say(f"\nПометка этого прогона: {marking(tag)}")
    try:
        pre = await prerequisites(client, tag, agent_sync, doc_sync)
        agent = await stage_counterparty(client, tag, agent_sync)
        await stage_order(client, tag, doc_sync, pre, agent)
    except LiveStop as stop:
        say(f"\nОСТАНОВЛЕНО на проверке: {stop}")
        say("Дальнейшие изменяющие запросы НЕ выполнялись — это правило, а не "
            "случайность: контракт повёл себя не так, как мы предполагаем.")
    except Exception as exc:  # noqa: BLE001 — сообщение чистим, тип называем
        say(f"\nОШИБКА {type(exc).__name__}: {scrub(exc)[:400]}")
        resp = getattr(exc, "response", None)
        if resp is not None:
            say(f"Ответ API: HTTP {resp.status_code} {scrub(resp.text)[:400]}")
        FAIL.append(f"{type(exc).__name__} в сценарии")
        say("Дальнейшие изменяющие запросы НЕ выполнялись.")

    # Отчёт о трогнутом в чужом аккаунте печатается ВСЕГДА и по журналу, а не
    # по тому, чем кончился сценарий. Успех и провал отличаются приговором
    # проверок, а не тем, признаём ли мы факт отправленных POST.
    report_ledger(tag)

    say(f"\nИТОГО: {len(PASS)} OK, {len(FAIL)} FAIL")
    return 1 if FAIL else 0


async def run() -> int:
    from app.ms_client import MoySkladClient

    async with MoySkladClient(TOKEN) as client:
        return await run_contract(client)


def main() -> int:
    if not is_authorized(TOKEN, CONFIRM):
        print(
            "НЕ ВЫПОЛНЕН: живой контракт syncId не проверен.\n"
            "Это ОТКРЫТЫЙ ГЕЙТ слияния DATA-1/DATA-2, а не пропущенный тест.\n"
            "Нужны обе переменные: OBOROT_LIVE_MS_TOKEN и "
            f"OBOROT_LIVE_MS_CONFIRM РОВНО со значением {CONFIRM_PHRASE!r} "
            "(см. докстринг файла). Любое другое значение подтверждением не "
            "является и запуск не разрешает.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
