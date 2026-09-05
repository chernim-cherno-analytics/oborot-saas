# -*- coding: utf-8 -*-
"""SUPPLY-3: планирование производства — материал, вещь, плановая партия.

ЧТО ЭТОТ СЛОЙ ДЕЛАЕТ. Даёт довести до конца один живой путь: купили ткань, ещё
не зная, что из неё шьют → выбрали вещь своего каталога или завели новинку с
эскизом → создали плановую партию и вручную назначили на неё метраж → отдельно
поставили план изделий и срок → увидели сводку и следующий шаг.

ЧЕГО ЭТОТ СЛОЙ НЕ ДЕЛАЕТ, И ЭТО ПРОВЕРЯЕТСЯ, А НЕ ОБЕЩАЕТСЯ:

  * не создаёт и не меняет партию «Оборота». `CC_BATCH_ID` (D-50) здесь не
    выдаётся и не имитируется: у плановой партии его нет вовсе. Модуль не
    импортирует `ProductionOrder`, `OrderedQty`, `OrderReceipt`, `OrderPlan` —
    ни одного из них, и это структурная проверка набора, а не комментарий;
  * не считает «Едет», потребность, оборачиваемость и деньги; формул (D-35,
    BUSINESS_LOGIC §0) не касается;
  * не пересчитывает метры в штуки НИГДЕ. Расход материала на изделие никто не
    объявлял, и вывести одно из другого нечем: план изделий вводится руками
    отдельным числом;
  * не пишет в МойСклад и в Google Sheets, не ходит в сеть вообще;
  * не трогает снимок предпросмотра (SUPPLY-2): его парсер, envelope и версия
    остаются как есть. Ручные решения живут В СВОИХ таблицах именно потому, что
    снимок целиком заменяется при каждом обновлении, а решение человека
    заменяться не должно.

ТРИ ПРАВИЛА, КОТОРЫЕ ЗДЕСЬ ГЛАВНЫЕ.

1. НЕИЗВЕСТНОЕ ОСТАЁТСЯ НЕИЗВЕСТНЫМ. `qty=None` — это «не знаем», и остаток
   такого материала тоже «не знаем», а не «сколько-то минус назначенное». Ноль
   означал бы «ничего нет», и это была бы выдумка (то же правило, что в D-51).

2. ПЛАН — НЕ РАСХОД. Назначить можно больше, чем известно в наличии: экран
   скажет об этом числом и словом, но не запретит и не обрежет. Запрет был бы
   бизнес-правилом, которого никто не принимал; тихое обрезание — подменой
   введённого человеком числа.

3. ЧУЖУЮ ПРАВКУ НЕ ЗАТИРАЕМ. У каждой изменяемой строки есть `rev`; клиент
   присылает тот, который видел. Разошлось — 409 и текущее состояние, чтобы
   человек увидел, что изменилось, а не узнал об этом от коллеги.
"""
from __future__ import annotations

import hashlib
import struct
from datetime import datetime, date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Product,
    SKETCH_MAX_BYTES,
    SKETCH_MAX_SIDE,
    SKETCH_MIME_TYPES,
    SUPPLY_DUE_KINDS,
    SUPPLY_ITEM_KINDS,
    SupplyAssignment,
    SupplyBatch,
    SupplyEvent,
    SupplyItem,
    SupplyMaterial,
    SupplySketch,
)

MAX_TITLE_CHARS = 200
MAX_NOTE_CHARS = 500
MAX_UNIT_CHARS = 16
MAX_OP_ID_CHARS = 64
#: Потолок количества. Не бизнес-правило, а граница представимости: число,
#: которое не помещается в double без потерь, показывается не тем, чем его
#: записали (тот же класс дефекта, что SUPPLY-2-REG п.6).
MAX_QTY = 1_000_000.0


class PlanningError(Exception):
    """Базовая ошибка слоя."""


class ValidationError(PlanningError):
    """Ввод человека негоден — 400. Носитель при этом не тронут."""


class NotFound(PlanningError):
    """Строки нет У ЭТОЙ организации — 404.

    Отдельного «нет вовсе» и «есть, но чужая» наружу не существует НАМЕРЕННО:
    разные ответы на эти два случая рассказали бы о существовании чужих строк
    (SEC-класс: перебор идентификаторов). Текст один и тот же, и он не называет
    ни имени, ни владельца.
    """


class StaleWrite(PlanningError):
    """Строку изменили, пока её правили, — 409 с текущим состоянием."""


class DuplicateOp(PlanningError):
    """Тот же поступок уже записан — повторный POST не применяется дважды."""


# ── Разбор ввода ─────────────────────────────────────────────────────────────

def clean_text(raw, field: str, *, limit: int, required: bool = False) -> str:
    """Строка человека: обрезаем края, проверяем длину, ничего не «чиним».

    Внутренние пробелы и регистр не трогаем: это его текст, а не наш.
    """
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise ValidationError(f"Поле «{field}» должно быть текстом.")
    value = raw.strip()
    if required and not value:
        raise ValidationError(f"Поле «{field}» обязательно.")
    if len(value) > limit:
        raise ValidationError(f"Поле «{field}» длиннее {limit} символов.")
    return value


def parse_qty(raw, field: str, *, allow_unknown: bool) -> float | None:
    """Количество: число, либо явное «неизвестно».

    Пустая строка и `None` означают НЕИЗВЕСТНО там, где неизвестное разрешено, —
    и это состояние, а не ноль. Ноль принимается только как явно написанный
    ноль: у него другой смысл («ничего»), и подменять им незнание нельзя.

    Запятая как десятичный разделитель принимается: человек пишет «12,5»,
    и отвергать это значило бы требовать от него раскладку, а не число.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if allow_unknown:
            return None
        raise ValidationError(f"Поле «{field}» обязательно.")
    if isinstance(raw, bool):
        raise ValidationError(f"Поле «{field}» должно быть числом.")
    if isinstance(raw, str):
        text = raw.strip().replace(",", ".").replace(" ", "").replace(" ", "")
        try:
            value = float(text)
        except ValueError:
            raise ValidationError(
                f"Поле «{field}» не читается как число: «{raw.strip()[:40]}».") from None
    elif isinstance(raw, (int, float)):
        value = float(raw)
    else:
        raise ValidationError(f"Поле «{field}» должно быть числом.")
    if value != value or value in (float("inf"), float("-inf")):
        raise ValidationError(f"Поле «{field}» должно быть конечным числом.")
    if value < 0:
        raise ValidationError(f"Поле «{field}» не может быть отрицательным.")
    if value > MAX_QTY:
        raise ValidationError(
            f"Поле «{field}» больше допустимого предела {MAX_QTY:.0f}.")
    return round(value, 3)


def parse_id(raw, field: str) -> int:
    """Идентификатор строки: целое положительное и ничего больше.

    Отдельная функция, потому что `int(x or 0)` молча превращает мусор в ноль,
    а ноль потом ищется в базе и даёт «не найдено» вместо «прислан мусор».
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValidationError(f"Не выбрано: {field}.")
    if isinstance(raw, bool):
        raise ValidationError(f"Неверно указано: {field}.")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"Неверно указано: {field}.") from None
    if value <= 0:
        raise ValidationError(f"Неверно указано: {field}.")
    return value


def parse_due(payload: dict) -> dict:
    """Срок: вид, текст, дата и ИСТОЧНИК — кто сказал.

    Источник хранится рядом со сроком не для красоты: «примерно к ноябрю» без
    автора и происхождения через месяц неотличимо от нашей собственной догадки.
    Ни одно из этих полей ни во что не считается — ни просрочки, ни SLA, ни
    подстановки «сегодня» в слое нет вовсе.
    """
    kind = clean_text(payload.get("due_kind") or "unknown", "вид срока", limit=16)
    if kind not in SUPPLY_DUE_KINDS:
        raise ValidationError("Неизвестный вид срока.")
    text = clean_text(payload.get("due_text"), "срок", limit=255)
    source = clean_text(payload.get("due_source"), "источник срока", limit=255)
    iso = clean_text(payload.get("due_date"), "дата", limit=10)
    if kind == "exact":
        if not iso:
            raise ValidationError("Для точного срока нужна дата.")
        try:
            date.fromisoformat(iso)
        except ValueError:
            raise ValidationError("Дата должна быть в виде ГГГГ-ММ-ДД.") from None
    else:
        iso = ""
    if kind in ("approx", "text") and not text:
        raise ValidationError("Для ориентировочного срока нужен текст.")
    if kind == "unknown":
        text = ""
    return {"due_kind": kind, "due_text": text, "due_date": iso, "due_source": source}


# ── Эскиз: формат проверяется по байтам ──────────────────────────────────────

def sniff_image(data: bytes) -> tuple[str, int, int]:
    """MIME и размеры — из САМИХ БАЙТОВ, а не из имени файла и заголовка.

    Клиент может назвать `.png` что угодно, и доверять его слову — значит
    хранить и отдавать под видом картинки произвольный файл. Поэтому разбор
    свой, короткий и закрытый: PNG по сигнатуре и IHDR, JPEG по маркерам SOFn.
    Ничего, кроме этих двух форматов, не принимается — в частности SVG, который
    является исполняемым документом, а картинкой только выглядит.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValidationError("Файл пуст.")
    data = bytes(data)
    if len(data) > SKETCH_MAX_BYTES:
        raise ValidationError(
            f"Файл больше {SKETCH_MAX_BYTES // (1024 * 1024)} МБ.")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ValidationError("Файл повреждён: это не читаемый PNG.")
        width, height = struct.unpack(">II", data[16:24])
        mime = "image/png"
    elif data[:2] == b"\xff\xd8":
        width = height = 0
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                break
            i += 2 + seg_len
        if not width or not height:
            raise ValidationError("Файл повреждён: это не читаемый JPEG.")
        mime = "image/jpeg"
    else:
        raise ValidationError("Принимаются только JPEG и PNG.")
    if mime not in SKETCH_MIME_TYPES:      # защита от рассинхронизации списка
        raise ValidationError("Принимаются только JPEG и PNG.")
    if width <= 0 or height <= 0 or width > SKETCH_MAX_SIDE or height > SKETCH_MAX_SIDE:
        raise ValidationError(
            f"Сторона картинки должна быть от 1 до {SKETCH_MAX_SIDE} точек.")
    return mime, width, height


def save_sketch(db: Session, org_id: int, data: bytes, author: str) -> SupplySketch:
    """Кладёт эскиз в базу. Байты — в BLOB, чтобы их забрал штатный бэкап."""
    mime, width, height = sniff_image(data)
    row = SupplySketch(
        org_id=org_id, mime=mime, byte_len=len(data), width=width, height=height,
        sha256=hashlib.sha256(data).hexdigest(), data=bytes(data), author=author,
    )
    db.add(row)
    db.flush()
    return row


def get_sketch(db: Session, org_id: int, sketch_id: int) -> SupplySketch:
    row = db.get(SupplySketch, sketch_id)
    if row is None or row.org_id != org_id:
        raise NotFound("Эскиз не найден.")
    return row


# ── Журнал и защита от повторного POST ───────────────────────────────────────

def _journal(db: Session, org_id: int, kind: str, entity_id: int, action: str,
             *, field: str = "", old: str = "", new: str = "",
             author: str = "", op_id: str = "") -> None:
    db.add(SupplyEvent(
        org_id=org_id, entity_kind=kind, entity_id=entity_id, action=action,
        field=field, old_value=str(old)[:500], new_value=str(new)[:500],
        author=author, op_id=op_id,
    ))


def check_op(db: Session, org_id: int, op_id: str) -> bool:
    """True, если такой поступок уже записан.

    Это ПРЕДВАРИТЕЛЬНАЯ проверка ради понятного ответа, а не защита: гонку
    двух одновременных вкладок закрывает частичный UNIQUE в базе, и на него же
    рассчитан перехват `IntegrityError` в ручках.
    """
    if not op_id:
        return False
    found = db.execute(
        select(SupplyEvent.id).where(SupplyEvent.org_id == org_id,
                                     SupplyEvent.op_id == op_id).limit(1)
    ).first()
    return found is not None


def parse_op_id(payload: dict) -> str:
    return clean_text(payload.get("op_id"), "идентификатор действия",
                      limit=MAX_OP_ID_CHARS)


def _rev_guard(row, payload: dict, name: str) -> None:
    """Сверяет присланную редакцию с текущей. Нет поля — правка без проверки.

    Отсутствие `rev` НЕ считается ошибкой намеренно: программный клиент, который
    правит строку без экрана, ничьей правки не перетирает вслепую — он просто не
    участвует в этой защите. Экран `rev` присылает всегда.
    """
    raw = payload.get("rev")
    if raw is None or raw == "":
        return
    try:
        rev = int(raw)
    except (TypeError, ValueError):
        raise ValidationError("Редакция должна быть целым числом.") from None
    if rev != row.rev:
        raise StaleWrite(
            f"{name} уже изменили в другом окне. Обновите страницу — "
            "показанные значения устарели.")


def _touch(row) -> None:
    row.rev = int(row.rev or 1) + 1
    row.updated_at = datetime.utcnow()


# ── Материал ─────────────────────────────────────────────────────────────────

def create_material(db: Session, org_id: int, payload: dict, author: str) -> SupplyMaterial:
    """Материал сам по себе — до того, как решено, что из него шьют.

    Именно это и есть первый шаг пути: ткань покупают партией и до дизайна,
    поэтому строка не требует ни вещи, ни партии, ни даже количества.
    """
    title = clean_text(payload.get("title"), "название материала",
                       limit=MAX_TITLE_CHARS, required=True)
    qty = parse_qty(payload.get("qty"), "количество", allow_unknown=True)
    unit = clean_text(payload.get("unit") or "м", "единица", limit=MAX_UNIT_CHARS)
    note = clean_text(payload.get("source_note"), "источник или комментарий",
                      limit=MAX_NOTE_CHARS)
    row = SupplyMaterial(org_id=org_id, title=title, qty=qty, unit=unit or "м",
                         source_note=note, author=author)
    db.add(row)
    db.flush()
    _journal(db, org_id, "material", row.id, "create", field="qty",
             old="", new="неизвестно" if qty is None else qty,
             author=author, op_id=parse_op_id(payload))
    return row


def update_material(db: Session, org_id: int, material_id: int,
                    payload: dict, author: str) -> SupplyMaterial:
    row = get_material(db, org_id, material_id)
    _rev_guard(row, payload, "Материал")
    op_id = parse_op_id(payload)
    if "title" in payload:
        new = clean_text(payload.get("title"), "название материала",
                         limit=MAX_TITLE_CHARS, required=True)
        if new != row.title:
            _journal(db, org_id, "material", row.id, "update", field="title",
                     old=row.title, new=new, author=author, op_id=op_id)
            op_id = ""          # один поступок — одна запись с этим op_id
            row.title = new
    if "qty" in payload:
        new_qty = parse_qty(payload.get("qty"), "количество", allow_unknown=True)
        if new_qty != row.qty:
            _journal(db, org_id, "material", row.id, "update", field="qty",
                     old="неизвестно" if row.qty is None else row.qty,
                     new="неизвестно" if new_qty is None else new_qty,
                     author=author, op_id=op_id)
            op_id = ""
            row.qty = new_qty
    if "source_note" in payload:
        row.source_note = clean_text(payload.get("source_note"),
                                     "источник или комментарий", limit=MAX_NOTE_CHARS)
    if "unit" in payload:
        row.unit = clean_text(payload.get("unit") or "м", "единица",
                              limit=MAX_UNIT_CHARS) or "м"
    _touch(row)
    return row


def get_material(db: Session, org_id: int, material_id: int) -> SupplyMaterial:
    row = db.get(SupplyMaterial, material_id)
    if row is None or row.org_id != org_id:
        raise NotFound("Материал не найден.")
    return row


# ── Вещь: каталожная или новинка ─────────────────────────────────────────────

def create_item(db: Session, org_id: int, payload: dict, author: str) -> SupplyItem:
    """Вещь плана. `catalog` — из своего каталога, `draft` — полноценная новинка.

    ДВА ОДИНАКОВЫХ ИМЕНИ — ДВЕ РАЗНЫЕ ВЕЩИ. Уникальности по имени здесь нет и не
    будет: имена в производстве повторяются от сезона к сезону, и склеить их
    значило бы принять за человека решение, которого он не принимал. Тождество —
    это `id` строки.

    У каталожной вещи хранится КАНОНИЧЕСКОЕ имя (`products.base_name`), а не
    `id` размерной строки: размерная строка исчезает при пересинке каталога, и
    связь, построенная на ней, исчезла бы вместе с ней.
    """
    kind = clean_text(payload.get("kind") or "draft", "вид вещи", limit=16)
    if kind not in SUPPLY_ITEM_KINDS:
        raise ValidationError("Неизвестный вид вещи.")
    note = clean_text(payload.get("note"), "заметка", limit=MAX_NOTE_CHARS)
    sketch_id = None
    raw_sketch = payload.get("sketch_id")
    if raw_sketch not in (None, "", 0):
        try:
            sketch_id = int(raw_sketch)
        except (TypeError, ValueError):
            raise ValidationError("Эскиз указан неверно.") from None
        get_sketch(db, org_id, sketch_id)      # чужой эскиз сюда не привяжется

    if kind == "catalog":
        base_name = clean_text(payload.get("base_name"), "вещь каталога",
                               limit=MAX_TITLE_CHARS, required=True)
        known = db.execute(
            select(Product.base_name).where(Product.org_id == org_id,
                                            Product.base_name == base_name).limit(1)
        ).first()
        if known is None:
            raise ValidationError(
                "Такой вещи в вашем каталоге нет. Выберите из списка или "
                "заведите новинку.")
        title = base_name
    else:
        title = clean_text(payload.get("title"), "название новинки",
                           limit=MAX_TITLE_CHARS, required=True)
        base_name = ""

    row = SupplyItem(org_id=org_id, kind=kind, base_name=base_name, title=title,
                     sketch_id=sketch_id, note=note, author=author)
    db.add(row)
    db.flush()
    _journal(db, org_id, "item", row.id, "create", field="title", old="", new=title,
             author=author, op_id=parse_op_id(payload))
    return row


def get_item(db: Session, org_id: int, item_id: int) -> SupplyItem:
    row = db.get(SupplyItem, item_id)
    if row is None or row.org_id != org_id:
        raise NotFound("Вещь не найдена.")
    return row


def catalog_options(db: Session, org_id: int, query: str = "", limit: int = 20) -> list[dict]:
    """Кандидаты каталога по каноническому имени. Только чтение.

    Это подсказка выбора, а не привязка: пока человек не нажал, связи нет.
    """
    stmt = (select(Product.base_name, func.count(Product.id))
            .where(Product.org_id == org_id, Product.archived.is_(False))
            .group_by(Product.base_name))
    needle = (query or "").strip().casefold()
    rows = db.execute(stmt).all()
    out = []
    for base_name, sizes in rows:
        if not base_name:
            continue
        if needle and needle not in base_name.casefold():
            continue
        out.append({"base_name": base_name, "sizes": int(sizes)})
    out.sort(key=lambda r: r["base_name"].casefold())
    return out[:limit]


# ── Плановая партия ──────────────────────────────────────────────────────────

def create_batch(db: Session, org_id: int, payload: dict, author: str) -> SupplyBatch:
    """Плановая партия. Не заказ и не партия «Оборота».

    `CC_BATCH_ID` тут не появляется ни в каком виде: идентификатор партии по
    D-50 выдаётся `production_orders` в момент рождения настоящей партии, а не
    намерению. Интерфейс обязан называть эту строку плановой — иначе снаружи
    она неотличима от заказа, которого нет.
    """
    item = get_item(db, org_id, parse_id(payload.get("item_id"), "вещь"))
    title = clean_text(payload.get("title"), "название партии", limit=MAX_TITLE_CHARS)
    plan_qty = parse_qty(payload.get("plan_qty"), "план изделий", allow_unknown=True)
    plan_note = clean_text(payload.get("plan_note"), "заметка к плану",
                           limit=MAX_NOTE_CHARS)
    due = parse_due(payload)
    row = SupplyBatch(
        org_id=org_id, item_id=item.id, title=title, plan_qty=plan_qty,
        plan_note=plan_note, author=author,
        due_author=author if due["due_kind"] != "unknown" else "",
        due_updated_at=datetime.utcnow() if due["due_kind"] != "unknown" else None,
        **due,
    )
    db.add(row)
    db.flush()
    _journal(db, org_id, "batch", row.id, "create", field="plan_qty", old="",
             new="неизвестно" if plan_qty is None else plan_qty,
             author=author, op_id=parse_op_id(payload))
    return row


def get_batch(db: Session, org_id: int, batch_id: int) -> SupplyBatch:
    row = db.get(SupplyBatch, batch_id)
    if row is None or row.org_id != org_id:
        raise NotFound("Плановая партия не найдена.")
    return row


def update_batch(db: Session, org_id: int, batch_id: int, payload: dict,
                 author: str) -> SupplyBatch:
    """Правка плана и срока. Прежнее значение уходит в журнал, а не пропадает."""
    row = get_batch(db, org_id, batch_id)
    _rev_guard(row, payload, "Плановая партия")
    op_id = parse_op_id(payload)
    if "title" in payload:
        row.title = clean_text(payload.get("title"), "название партии",
                               limit=MAX_TITLE_CHARS)
    if "plan_qty" in payload:
        new_qty = parse_qty(payload.get("plan_qty"), "план изделий", allow_unknown=True)
        if new_qty != row.plan_qty:
            _journal(db, org_id, "batch", row.id, "update", field="plan_qty",
                     old="неизвестно" if row.plan_qty is None else row.plan_qty,
                     new="неизвестно" if new_qty is None else new_qty,
                     author=author, op_id=op_id)
            op_id = ""
            row.plan_qty = new_qty
    if "plan_note" in payload:
        row.plan_note = clean_text(payload.get("plan_note"), "заметка к плану",
                                   limit=MAX_NOTE_CHARS)
    if any(k in payload for k in ("due_kind", "due_text", "due_date", "due_source")):
        due = parse_due(payload)
        before = describe_due(row)
        for key, value in due.items():
            setattr(row, key, value)
        after = describe_due(row)
        if after != before:
            _journal(db, org_id, "batch", row.id, "update", field="due",
                     old=before, new=after, author=author, op_id=op_id)
            op_id = ""
            row.due_author = author
            row.due_updated_at = datetime.utcnow()
    _touch(row)
    return row


def describe_due(row: SupplyBatch) -> str:
    """Срок словами — ровно тем видом, каким он задан. Без «сегодня»."""
    if row.due_kind == "exact" and row.due_date:
        return f"точно {row.due_date}"
    if row.due_kind == "approx" and row.due_text:
        return f"ориентировочно {row.due_text}"
    if row.due_kind == "text" and row.due_text:
        return row.due_text
    return "срок неизвестен"


# ── Назначения материала на плановые партии ──────────────────────────────────

def assigned_total(db: Session, org_id: int, material_id: int) -> float:
    """Сколько метража этого материала уже расписано по плановым партиям."""
    total = db.execute(
        select(func.coalesce(func.sum(SupplyAssignment.qty), 0.0))
        .where(SupplyAssignment.org_id == org_id,
               SupplyAssignment.material_id == material_id)
    ).scalar_one()
    return round(float(total or 0.0), 3)


def create_assignment(db: Session, org_id: int, payload: dict,
                      author: str) -> SupplyAssignment:
    """Назначить метраж материала на плановую партию.

    Оба конца проверяются на принадлежность организации ДО записи: назначить
    чужой материал на свою партию (или наоборот) нельзя, и наружу это выглядит
    одинаково — «не найдено», без намёка на то, что строка где-то существует.
    """
    material = get_material(db, org_id, parse_id(payload.get("material_id"), "материал"))
    batch = get_batch(db, org_id, parse_id(payload.get("batch_id"), "плановая партия"))
    qty = parse_qty(payload.get("qty"), "метраж", allow_unknown=False)
    if qty is None or qty <= 0:
        raise ValidationError("Назначить нужно число больше нуля.")
    note = clean_text(payload.get("note"), "заметка", limit=MAX_NOTE_CHARS)
    row = SupplyAssignment(org_id=org_id, material_id=material.id, batch_id=batch.id,
                           qty=qty, note=note, author=author)
    db.add(row)
    db.flush()
    _journal(db, org_id, "assignment", row.id, "create", field="qty", old="", new=qty,
             author=author, op_id=parse_op_id(payload))
    return row


def get_assignment(db: Session, org_id: int, assignment_id: int) -> SupplyAssignment:
    row = db.get(SupplyAssignment, assignment_id)
    if row is None or row.org_id != org_id:
        raise NotFound("Назначение не найдено.")
    return row


def update_assignment(db: Session, org_id: int, assignment_id: int, payload: dict,
                      author: str) -> SupplyAssignment:
    row = get_assignment(db, org_id, assignment_id)
    _rev_guard(row, payload, "Назначение")
    op_id = parse_op_id(payload)
    if "qty" in payload:
        qty = parse_qty(payload.get("qty"), "метраж", allow_unknown=False)
        if qty is None or qty <= 0:
            raise ValidationError("Назначить нужно число больше нуля.")
        if qty != row.qty:
            _journal(db, org_id, "assignment", row.id, "update", field="qty",
                     old=row.qty, new=qty, author=author, op_id=op_id)
            op_id = ""
            row.qty = qty
    if "note" in payload:
        row.note = clean_text(payload.get("note"), "заметка", limit=MAX_NOTE_CHARS)
    _touch(row)
    return row


def delete_assignment(db: Session, org_id: int, assignment_id: int, payload: dict,
                      author: str) -> dict:
    """Снять назначение целиком. Материал при этом никуда не девается."""
    row = get_assignment(db, org_id, assignment_id)
    _rev_guard(row, payload, "Назначение")
    material_id, qty = row.material_id, row.qty
    _journal(db, org_id, "assignment", row.id, "delete", field="qty",
             old=qty, new="снято", author=author, op_id=parse_op_id(payload))
    db.delete(row)
    return {"material_id": material_id, "qty": qty}


def move_assignment(db: Session, org_id: int, payload: dict, author: str) -> dict:
    """Перенести часть метража с одной плановой партии на другую.

    ОДНОЙ ОПЕРАЦИЕЙ, А НЕ ДВУМЯ. Перенос — это один поступок человека, и
    разложить его на «снять здесь» и «добавить там» значило бы допустить
    состояние между ними: отказ на втором шаге оставил бы метраж потерянным, а
    повтор первого — удвоенным. Поэтому источник и приёмник меняются в одной
    транзакции вызывающей ручки, и сумма назначенного по материалу до и после
    переноса совпадает ровно.

    Приёмник ищется среди уже существующих назначений того же материала на ту
    партию: иначе одна партия набирала бы несколько строк одного материала, и
    «сколько на неё расписано» пришлось бы складывать глазами.
    """
    src = get_assignment(db, org_id, parse_id(payload.get("assignment_id"), "назначение"))
    _rev_guard(src, payload, "Назначение")
    target_batch = get_batch(db, org_id, parse_id(payload.get("to_batch_id"),
                                                  "плановая партия"))
    if target_batch.id == src.batch_id:
        raise ValidationError("Это та же самая партия — переносить некуда.")
    qty = parse_qty(payload.get("qty"), "метраж", allow_unknown=False)
    if qty is None or qty <= 0:
        raise ValidationError("Перенести нужно число больше нуля.")
    if qty > src.qty:
        raise ValidationError(
            f"На этой партии назначено {fmt_qty(src.qty)} — перенести больше нечего.")
    op_id = parse_op_id(payload)

    dst = db.execute(
        select(SupplyAssignment).where(
            SupplyAssignment.org_id == org_id,
            SupplyAssignment.material_id == src.material_id,
            SupplyAssignment.batch_id == target_batch.id).limit(1)
    ).scalars().first()
    if dst is None:
        dst = SupplyAssignment(org_id=org_id, material_id=src.material_id,
                               batch_id=target_batch.id, qty=qty, author=author)
        db.add(dst)
    else:
        dst.qty = round(float(dst.qty) + qty, 3)
        _touch(dst)

    remainder = round(float(src.qty) - qty, 3)
    _journal(db, org_id, "assignment", src.id, "move", field="qty",
             old=src.qty, new=remainder, author=author, op_id=op_id)
    if remainder <= 0:
        db.delete(src)
    else:
        src.qty = remainder
        _touch(src)
    db.flush()
    return {"moved": qty, "material_id": dst.material_id, "to_batch_id": target_batch.id}


# ── Сводка и следующий шаг ───────────────────────────────────────────────────

def fmt_qty(value) -> str:
    """Число человеку: без хвоста из нулей и без выдуманной точности."""
    if value is None:
        return "неизвестно"
    text = f"{float(value):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def material_view(db: Session, org_id: int, row: SupplyMaterial,
                  used: float | None = None) -> dict:
    """Материал для экрана: назначено, остаток и ЧЕСТНОЕ предупреждение.

    Остаток неизвестного количества — тоже НЕИЗВЕСТЕН, а не «минус
    назначенное»: вычитать из незнания нечего. Превышение показывается числом и
    словом, но назначение не обрезается и не отменяется — это план, а не
    физический расход (см. шапку модуля, правило 2).
    """
    assigned = assigned_total(db, org_id, row.id) if used is None else used
    unknown_qty = row.qty is None
    free = None if unknown_qty else round(float(row.qty) - assigned, 3)
    over = (not unknown_qty) and assigned > float(row.qty) + 1e-9
    return {
        "id": row.id,
        "title": row.title,
        "qty": row.qty,
        "qty_known": not unknown_qty,
        "unit": row.unit,
        "source_note": row.source_note,
        "assigned": assigned,
        "free": free,
        "free_known": not unknown_qty,
        "over": over,
        "over_by": round(assigned - float(row.qty), 3) if over else 0.0,
        "warning": (
            f"Назначено больше, чем известно в наличии: на {fmt_qty(round(assigned - float(row.qty), 3))} {row.unit}. "
            "Это план, а не расход — «Оборот» ничего не списывает и не запрещает; "
            "проверьте количество материала или назначения."
        ) if over else "",
        "rev": row.rev,
        "author": row.author,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def board(db: Session, org_id: int, role: str) -> dict:
    """Весь экран планирования одним чтением. Ни одной записи.

    Собирается за фиксированное число запросов, а не по строке на элемент:
    материалов и партий у бренда десятки, но запрос на каждого — это привычка,
    которая ломается ровно тогда, когда их станут сотни.
    """
    materials = db.execute(
        select(SupplyMaterial).where(SupplyMaterial.org_id == org_id)
        .order_by(SupplyMaterial.id.desc())
    ).scalars().all()
    items = db.execute(
        select(SupplyItem).where(SupplyItem.org_id == org_id)
        .order_by(SupplyItem.id.desc())
    ).scalars().all()
    batches = db.execute(
        select(SupplyBatch).where(SupplyBatch.org_id == org_id)
        .order_by(SupplyBatch.id.desc())
    ).scalars().all()
    assignments = db.execute(
        select(SupplyAssignment).where(SupplyAssignment.org_id == org_id)
        .order_by(SupplyAssignment.id.asc())
    ).scalars().all()

    used: dict[int, float] = {}
    by_batch: dict[int, list] = {}
    for a in assignments:
        used[a.material_id] = round(used.get(a.material_id, 0.0) + float(a.qty), 3)
        by_batch.setdefault(a.batch_id, []).append(a)

    item_by_id = {i.id: i for i in items}
    mat_by_id = {m.id: m for m in materials}

    mat_views = [material_view(db, org_id, m, used=used.get(m.id, 0.0))
                 for m in materials]

    batch_views = []
    for b in batches:
        item = item_by_id.get(b.item_id)
        rows = by_batch.get(b.id, [])
        batch_views.append({
            "id": b.id,
            "title": b.title,
            "item_id": b.item_id,
            "item_title": item.title if item else "",
            "item_kind": item.kind if item else "",
            "item_sketch_id": (item.sketch_id if item else None),
            "plan_qty": b.plan_qty,
            "plan_known": b.plan_qty is not None,
            "plan_note": b.plan_note,
            "due_kind": b.due_kind,
            "due_text": b.due_text,
            "due_date": b.due_date,
            "due_source": b.due_source,
            "due_author": b.due_author,
            "due_updated_at": b.due_updated_at.isoformat() if b.due_updated_at else "",
            "due_label": describe_due(b),
            "assignments": [{
                "id": a.id,
                "material_id": a.material_id,
                "material_title": (mat_by_id[a.material_id].title
                                   if a.material_id in mat_by_id else ""),
                "unit": (mat_by_id[a.material_id].unit
                         if a.material_id in mat_by_id else ""),
                "qty": a.qty,
                "note": a.note,
                "rev": a.rev,
            } for a in rows],
            # Тот же запрет, что и в сводке: назначения складываются только
            # внутри своей единицы. Одна партия законно собирается из метров
            # ткани и килограммов фурнитуры, и «52» для такой пары было бы
            # выдумкой ровно того же сорта, что и «110 м» в сводке.
            "assigned_by_unit": group_by_unit(
                ((mat_by_id[a.material_id].unit
                  if a.material_id in mat_by_id else ""), a.qty) for a in rows),
            "rev": b.rev,
        })

    return {
        "role": role,
        "can_write": role == "owner",
        "materials": mat_views,
        "items": [{
            "id": i.id, "kind": i.kind, "title": i.title,
            "base_name": i.base_name, "sketch_id": i.sketch_id, "note": i.note,
            "rev": i.rev,
        } for i in items],
        "batches": batch_views,
        "summary": summary(mat_views, batch_views),
        "next_step": next_step(mat_views, items, batch_views, role),
        "limits": {
            "sketch_max_bytes": SKETCH_MAX_BYTES,
            "sketch_mime": list(SKETCH_MIME_TYPES),
            "max_qty": MAX_QTY,
        },
        "disclaimer": (
            "Плановые партии — это намерение, а не заказ: они не создают партию "
            "«Оборота», не получают её номер и не входят в «Едет», «В заказе», "
            "потребность и бюджет."
        ),
    }


def group_by_unit(pairs) -> list[dict]:
    """Складывает количества ТОЛЬКО внутри одной единицы.

    Исправление P1 ревью PR #49 (issuecomment-5548612500). Прежняя редакция
    складывала свободные остатки всех материалов в одно число и подписывала его
    метрами: 100 м ткани и 10 кг фурнитуры превращались в «110 м». Это не
    округление и не мелочь — это придуманный факт: коэффициента между метром и
    килограммом никто не объявлял, и вывести его неоткуда.

    Складывать разрешено только одинаковое, поэтому здесь нет ни одного
    коэффициента пересчёта и не появится: единица берётся у самого материала
    как есть, ничего не нормализуется и не переименовывается («м» и «метр»
    остаются разными подписями, потому что доказать их равенство мы не можем —
    это ввод человека, а не справочник).
    """
    totals: dict[str, float] = {}
    for unit, qty in pairs:
        if qty is None:
            continue
        key = (unit or "").strip()
        totals[key] = round(totals.get(key, 0.0) + float(qty), 3)
    return [{"unit": unit, "qty": qty}
            for unit, qty in sorted(totals.items(), key=lambda kv: kv[0])]


def summary(mat_views: list[dict], batch_views: list[dict]) -> dict:
    """Компактная сводка. Каждое число подписано тем, чем оно является.

    Метраж и штуки НЕ складываются и не пересчитываются друг в друга: это
    разные величины, и связи между ними в системе нет — расход на изделие
    никто не объявлял. Неизвестное считается отдельно и НЕ прячется в нули.

    Свободный остаток отдаётся РАЗБИТЫМ ПО ЕДИНИЦАМ и общего числа не имеет
    вовсе: единого числа для метров и килограммов не существует, и место, где
    его можно было бы случайно показать, здесь просто отсутствует.
    """
    free_by_unit = group_by_unit(
        (m["unit"], m["free"]) for m in mat_views
        if m["free_known"] and m["free"] is not None and m["free"] > 0)
    unknown_materials = sum(1 for m in mat_views if not m["qty_known"])
    over_materials = sum(1 for m in mat_views if m["over"])
    plan_known = sum(b["plan_qty"] for b in batch_views if b["plan_known"])
    plan_unknown = sum(1 for b in batch_views if not b["plan_known"])
    due_unknown = sum(1 for b in batch_views if b["due_kind"] == "unknown")
    return {
        "materials": len(mat_views),
        "batches": len(batch_views),
        "free_by_unit": free_by_unit,
        "unknown_materials": unknown_materials,
        "over_materials": over_materials,
        # Штуки — одна величина по определению: изделия считаются изделиями.
        "plan_known": round(float(plan_known), 3),
        "plan_unknown": plan_unknown,
        "due_unknown": due_unknown,
    }


def next_step(mat_views: list[dict], items: list, batch_views: list[dict],
              role: str) -> dict:
    """Один следующий шаг — тот, который сейчас имеет смысл.

    Не список советов и не «оптимизация»: на экране, куда заходят раз в день,
    полезен ровно один вопрос — что делать дальше. Порядок проверок повторяет
    порядок самого пути, поэтому подсказка не может предложить шаг, для
    которого ещё нет предыдущего.
    """
    if role != "owner":
        return {"code": "readonly",
                "text": "Планы ведёт владелец организации. Здесь они видны как есть."}
    if not mat_views:
        return {"code": "add_material",
                "text": "Начните с материала: его можно добавить до того, как решено, "
                        "что из него шьют."}
    if not items:
        return {"code": "add_item",
                "text": "Выберите вещь своего каталога или заведите новинку с эскизом."}
    if not batch_views:
        return {"code": "add_batch",
                "text": "Создайте плановую партию: что и сколько собираетесь сшить."}
    unassigned = [m for m in mat_views
                  if m["free_known"] and m["free"] is not None and m["free"] > 0]
    if unassigned:
        first = unassigned[0]
        return {"code": "assign",
                "text": f"У материала «{first['title']}» не назначено "
                        f"{fmt_qty(first['free'])} {first['unit']} — распределите "
                        "по плановым партиям."}
    over = [m for m in mat_views if m["over"]]
    if over:
        return {"code": "over",
                "text": f"У материала «{over[0]['title']}» назначено больше, чем "
                        "известно в наличии. Проверьте количество или назначения."}
    no_plan = [b for b in batch_views if not b["plan_known"]]
    if no_plan:
        return {"code": "plan_qty",
                "text": f"У партии «{no_plan[0]['title'] or no_plan[0]['item_title']}» "
                        "не задан план изделий — сколько штук собираетесь сшить."}
    no_due = [b for b in batch_views if b["due_kind"] == "unknown"]
    if no_due:
        return {"code": "due",
                "text": f"У партии «{no_due[0]['title'] or no_due[0]['item_title']}» "
                        "не указан срок. Годится и ориентир — с источником."}
    unknown = [m for m in mat_views if not m["qty_known"]]
    if unknown:
        return {"code": "qty",
                "text": f"У материала «{unknown[0]['title']}» количество неизвестно. "
                        "Когда узнаете — впишите, остаток посчитается."}
    return {"code": "ok",
            "text": "План собран: материалы распределены, у партий есть план и срок."}
