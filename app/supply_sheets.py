# -*- coding: utf-8 -*-
"""SUPPLY-2: read-only предпросмотр производственной таблицы Google Sheets.

ЧТО ЭТО ТАКОЕ И ЧЕМ ОНО НЕ ЯВЛЯЕТСЯ. Здесь живёт весь слой SUPPLY-2: разбор
ссылки, обращение к публичному CSV-endpoint Google, лимиты, парсер двух
физических header rows и хранение ОДНОГО последнего снимка. Снимок — это
staging-предпросмотр ВНЕШНЕГО источника, а не партия «Оборота»:

  * ни одного `ProductionOrder`, `CC_BATCH_ID`, `OrderedQty`, `OrderReceipt`
    и `OrderPlan` этот модуль не создаёт, не меняет и не связывает;
  * «Едет», «В заказе» (D-26), планировщик, аналитика, статусы и формулы
    (D-35, BUSINESS_LOGIC §0) им не затрагиваются вовсе;
  * ни одной записи в Google Sheets или МойСклад отсюда не уходит;
  * свободный текст источника («Отгружено», «Крой», «Сдано») хранится как
    `source_status_raw`/`comments_raw` и НИКОГДА не превращается в статус
    «Оборота»: у нас нет способа доказать, что чужое слово значит то же самое.

ПОЧЕМУ БЕЗ НОВОЙ СХЕМЫ. Снимок — это кэш чужой таблицы, а не сущность продукта.
Заводить под кэш таблицу, миграцию и одиннадцатый шаг старта значило бы платить
необратимой схемной работой за обратимое удобство. Поэтому один bounded
versioned envelope лежит под отдельным ключом `supply_sheets_v1` в `config_json`
УЖЕ СУЩЕСТВУЮЩЕЙ основной `Connection` организации. Старый код (после отката)
про этот ключ не знает и молча его игнорирует, а удаление организации уносит
носителя вместе с envelope — `Connection` давно входит в `org_purge_models()`
(`app/tenancy.py`), и добавлять к обещанию «данные стёрты» ничего не пришлось.

ПОЧЕМУ НОСИТЕЛЬ — СУЩЕСТВУЮЩАЯ СВЯЗЬ, А НЕ СВОЯ `Connection(kind=google_sheets)`.
Это не экономия строки, а требование отката. `GET /api/settings` выбирает связь
запросом `order_by(Connection.id.desc())` БЕЗ фильтра по `kind` (`app/api.py`):
новый ряд оказался бы самым свежим, и после отката релиза старая страница
настроек начала бы рассказывать про МойСклад неправду — статус, токен и время
синхронизации она прочитала бы из чужой строки. Поэтому носитель выбирается
детерминированно среди УЖЕ ЖИВУЩИХ основных связей, а нет основной связи —
честный 409 без единой мутации и без единого сетевого вызова.

ГРАНИЦА ВНЕШНЕГО ДОСТУПА. Только публично читаемый без нового credential CSV
endpoint на фиксированном хосте `docs.google.com`. Клиент НЕ передаёт URL для
запроса: он передаёт каноническую ссылку на таблицу, из которой строгим
allowlist-regex извлекается `spreadsheet_id`, а сам endpoint строит сервер.
Имена двух листов приходят отдельными полями и URL-кодируются здесь же. Только
GET, явный timeout, bounded read, проверка хоста НА КАЖДОМ редиректе и
безопасные сообщения об ошибке без тела чужого ответа.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Connection, Product

# ── Версии и ключи ───────────────────────────────────────────────────────────

#: Версия envelope. Живёт ВНУТРИ снимка, чтобы читатель мог отличить снимок,
#: сделанный прежним кодом, от своего, не гадая по набору ключей.
ENVELOPE_SCHEMA_VERSION = 1

#: Ключ в `config_json` носителя. Отдельный и узкий: всё остальное содержимое
#: `config_json` — чужое, и мы обязаны вернуть его на место байт в байт по
#: смыслу JSON.
ENVELOPE_KEY = "supply_sheets_v1"

#: Версия парсера. Входит в `content_sha256`, и это не украшение: тот же CSV,
#: разобранный ДРУГИМ парсером, — другой снимок, и выдавать его за «ничего не
#: изменилось» нельзя. Версия 2 — исправление позиционной схемы по ревью PR #47
#: (весь каркас был на колонку левее фактического). Поэтому первое обновление
#: после этой правки честно приходит как НОВЫЙ импорт, даже если байты в Google
#: не поменялись ни на один: изменилось не содержимое источника, а то, что мы в
#: нём прочитали. Снимок, сделанный прежней версией, читается по-прежнему, но
#: помечается устаревшим (`parser_stale`) — сам себя он вылечит первым же
#: обновлением.
PARSER_VERSION = "supply-sheets-parser-2"

#: Виды связей, которые считаются ОСНОВНОЙ связью организации. Список явный:
#: «любая связь» означало бы, что носитель зависит от порядка строк.
PRIMARY_CONNECTION_KINDS: tuple[str, ...] = ("moysklad", "demo")

#: Порядок предпочтения носителя. Детерминированный: при двух связях выбор не
#: должен зависеть от того, какую из них сегодня вернула база первой.
_KIND_RANK = {kind: i for i, kind in enumerate(PRIMARY_CONNECTION_KINDS)}

# ── Границы внешнего источника ───────────────────────────────────────────────

GOOGLE_HOST = "docs.google.com"
FETCH_TIMEOUT_SECONDS = 20.0
MAX_REDIRECTS = 2
SHEET_COUNT = 2

MAX_RESPONSE_BYTES = 2 * 1024 * 1024      # сырой ответ одного листа
MAX_ROWS_PER_SHEET = 5000                 # физических строк CSV
MAX_COLUMNS = 256                         # колонок в строке
MAX_CELL_CHARS = 4096                     # символов в ячейке
MAX_ENVELOPE_BYTES = 1024 * 1024          # сериализованный снимок целиком
MAX_SHEET_NAME_CHARS = 100
MAX_QUANTITY = 1_000_000                  # разумный потолок количества

#: Ссылка на таблицу. Allowlist, а не blacklist: разрешено ровно то, что
#: перечислено, и «похожий» адрес (`docs.google.com.evil.tld`, `http://`,
#: `user:pass@`) не проходит вовсе.
_SPREADSHEET_URL_RE = re.compile(
    r"^https://docs\.google\.com/spreadsheets/d/(?P<id>[A-Za-z0-9_-]{10,120})"
    r"(?:[/?#].*)?$"
)

#: Управляющие символы в имени листа. Ловим ДО URL-кодирования: закодированный
#: перевод строки выглядел бы безобидно и уехал бы в запрос как есть.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


# ── Ошибки ───────────────────────────────────────────────────────────────────

class SupplySheetsError(Exception):
    """Общий предок. Сообщение ЛЮБОЙ из этих ошибок безопасно показывать
    пользователю: тела чужих ответов, заголовков и токенов в них нет."""


class ValidationError(SupplySheetsError):
    """Пользователь прислал то, чего мы не принимаем (400)."""


class NoCarrierError(SupplySheetsError):
    """У организации нет основной связи, в которой можно хранить снимок (409)."""


class CarrierConfigError(SupplySheetsError):
    """`config_json` носителя не является JSON-объектом.

    Отдельный тип и fail closed: чужое содержимое, которое мы не смогли
    разобрать, нельзя «починить» перезаписью — это и была бы потеря данных.
    """


class SourceError(SupplySheetsError):
    """Источник не отдал того, что мы умеем читать (502).

    Один тип на сеть, HTTP-статус, HTML-страницу входа, битый CSV, дрейф
    заголовка и превышение лимитов — потому что для пользователя все они
    означают одно: снимок не обновлён, прежний остался на месте.
    """


# ── Транспорт: инъектируемый, чтобы живого Google не было в обязательном CI ──

@dataclass
class HttpResponse:
    """Ответ внешнего источника в том объёме, который нам нужен."""

    status: int
    headers: dict
    body: bytes
    url: str


def _httpx_get(method: str, url: str, timeout: float) -> HttpResponse:
    """Единственный сетевой вызов слоя: GET с bounded read и без редиректов.

    Редиректы выключены НАМЕРЕННО: их разбирает `_fetch_url` и проверяет хост
    у каждого перехода. Библиотечное `follow_redirects=True` увело бы нас на
    `accounts.google.com` молча — то есть ровно туда, куда нельзя.

    Чтение потоковое и ограниченное: `r.content` вычитал бы в память всё, что
    прислал источник, и лимит проверялся бы уже после того, как он потрачен.
    """
    import httpx

    if method != "GET":
        raise SourceError("этот слой умеет только GET")

    chunks: list[bytes] = []
    total = 0
    with httpx.Client(follow_redirects=False, timeout=timeout) as client:
        try:
            with client.stream("GET", url, headers={"Accept": "text/csv"}) as resp:
                status = resp.status_code
                headers = dict(resp.headers)
                final_url = str(resp.url)
                if status == 200:
                    for chunk in resp.iter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise SourceError(
                                f"ответ источника больше допустимых "
                                f"{MAX_RESPONSE_BYTES // 1024} КиБ"
                            )
                        chunks.append(chunk)
        except SourceError:
            raise
        except httpx.TimeoutException:
            raise SourceError("источник не ответил за отведённое время") from None
        except httpx.HTTPError:
            raise SourceError("не удалось связаться с источником") from None
    return HttpResponse(status=status, headers=headers, body=b"".join(chunks),
                        url=final_url)


#: Точка инъекции. Тест подменяет транспорт и доказывает, что запросов ровно
#: два, метод только GET, хост только docs.google.com, а МойСклад не трогается.
_transport = None


def set_transport(fn) -> None:
    """Подменить транспорт (только для тестов). `None` — вернуть настоящий."""
    global _transport
    _transport = fn


def get_transport():
    return _transport or _httpx_get


# ── Валидация входа ──────────────────────────────────────────────────────────

def parse_spreadsheet_url(raw: str) -> str:
    """`spreadsheet_id` из канонической ссылки. Иначе — `ValidationError`.

    Возвращается ИДЕНТИФИКАТОР, а не URL: дальше запрос строит сервер. Это и
    есть граница, отделяющая «пользователь показал свою таблицу» от «клиент
    заставил сервер сходить по произвольному адресу».
    """
    value = (raw or "").strip()
    if not value:
        raise ValidationError("Вставьте ссылку на таблицу Google Sheets.")
    if len(value) > 2048:
        raise ValidationError("Ссылка слишком длинная.")
    match = _SPREADSHEET_URL_RE.match(value)
    if not match:
        raise ValidationError(
            "Нужна обычная ссылка вида "
            "https://docs.google.com/spreadsheets/d/<идентификатор>/… — "
            "другие адреса этот раздел не открывает."
        )
    return match.group("id")


def validate_sheet_names(names) -> list[str]:
    """Ровно два имени листов, в присланном порядке. Порядок значим.

    Первый лист — текущий, второй — следующий: так их видит владелец, и так
    они лягут в снимок. Менять порядок «для красоты» нельзя — сравнение
    снимков идёт по хешу, в который порядок входит.
    """
    if not isinstance(names, (list, tuple)):
        raise ValidationError("Имена листов должны прийти списком из двух значений.")
    if len(names) != SHEET_COUNT:
        raise ValidationError(
            f"Нужно ровно {SHEET_COUNT} листа: текущий и следующий."
        )
    out: list[str] = []
    for raw in names:
        if not isinstance(raw, str):
            raise ValidationError("Имя листа должно быть текстом.")
        value = raw.strip()
        if not value:
            raise ValidationError("Имя листа не может быть пустым.")
        if len(value) > MAX_SHEET_NAME_CHARS:
            raise ValidationError(
                f"Имя листа длиннее {MAX_SHEET_NAME_CHARS} символов."
            )
        if _CONTROL_CHARS_RE.search(value):
            raise ValidationError("В имени листа есть управляющие символы.")
        out.append(value)
    if out[0] == out[1]:
        raise ValidationError(
            "Текущий и следующий лист должны быть разными: иначе предпросмотр "
            "покажет один и тот же лист дважды."
        )
    return out


def build_csv_url(spreadsheet_id: str, sheet_name: str) -> str:
    """Endpoint CSV. Строит СЕРВЕР, целиком, из проверенных частей.

    `headers=0` обязателен: у этой таблицы ДВЕ физические строки заголовка, и
    любое «умное» распознавание заголовка на стороне Google съело бы одну из
    них вместе с позиционным контрактом размеров.
    """
    return (
        f"https://{GOOGLE_HOST}/spreadsheets/d/{spreadsheet_id}/gviz/tq"
        f"?tqx=out:csv&headers=0&sheet={quote(sheet_name, safe='')}"
    )


def spreadsheet_link(spreadsheet_id: str) -> str:
    """Безопасная ссылка «открыть исходник» — тоже собирается сервером."""
    return f"https://{GOOGLE_HOST}/spreadsheets/d/{spreadsheet_id}/edit"


def _require_google_url(url: str) -> None:
    """Схема https и хост РОВНО docs.google.com, без userinfo."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SourceError("источник попытался увести запрос с https")
    if parsed.username or parsed.password:
        raise SourceError("в адресе источника есть учётные данные")
    if (parsed.hostname or "").lower() != GOOGLE_HOST:
        raise SourceError(
            f"источник попытался увести запрос на посторонний адрес "
            f"(ожидался {GOOGLE_HOST})"
        )


# ── Загрузка одного листа ────────────────────────────────────────────────────

def _fetch_url(url: str, timeout: float) -> HttpResponse:
    """GET с ручной обработкой редиректов и проверкой хоста на каждом шаге."""
    _require_google_url(url)
    transport = get_transport()
    current = url
    hops = 0
    while True:
        resp = transport("GET", current, timeout)
        if resp.status in (301, 302, 303, 307, 308):
            hops += 1
            if hops > MAX_REDIRECTS:
                raise SourceError("источник переадресует запрос по кругу")
            location = ""
            for key, value in (resp.headers or {}).items():
                if str(key).lower() == "location":
                    location = str(value)
                    break
            if not location:
                raise SourceError("источник ответил переадресацией без адреса")
            target = urljoin(current, location)
            _require_google_url(target)
            current = target
            continue
        return resp


def fetch_sheet_csv(spreadsheet_id: str, sheet_name: str,
                    timeout: float = FETCH_TIMEOUT_SECONDS) -> bytes:
    """Сырые байты CSV одного листа. Любая беда — `SourceError` без тела ответа."""
    resp = _fetch_url(build_csv_url(spreadsheet_id, sheet_name), timeout)

    if resp.status in (401, 403):
        raise SourceError(
            f"лист «{sheet_name}»: таблица не открыта на чтение по ссылке "
            f"(источник ответил {resp.status})"
        )
    if resp.status == 404:
        raise SourceError(f"лист «{sheet_name}»: таблица или лист не найдены (404)")
    if resp.status != 200:
        raise SourceError(f"лист «{sheet_name}»: источник ответил {resp.status}")

    content_type = ""
    for key, value in (resp.headers or {}).items():
        if str(key).lower() == "content-type":
            content_type = str(value).lower()
            break

    body = resp.body or b""
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceError(
            f"лист «{sheet_name}»: ответ больше допустимых "
            f"{MAX_RESPONSE_BYTES // 1024} КиБ"
        )
    if not body.strip():
        raise SourceError(f"лист «{sheet_name}»: источник вернул пустой ответ")

    head = body[:512].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if content_type.startswith("text/html") or head.startswith((b"<!doctype", b"<html")):
        raise SourceError(
            f"лист «{sheet_name}»: источник вернул страницу входа, а не CSV — "
            f"похоже, таблица закрыта и её просят открыть после авторизации"
        )
    return body


# ── Контракт заголовка: маленький, явный, без догадок ────────────────────────
#
# ЭТИ ПОЗИЦИИ — НАБЛЮДЕНИЕ, А НЕ ДОГАДКА, и однажды они уже были неверны.
# Первая версия слоя (отвергнутый ревью HEAD `a25e163`) задала весь каркас НА
# ОДНУ КОЛОНКУ ЛЕВЕЕ: ждала «Наименование» в колонке 2, размерную горку с 9,
# итог в 14. Синтетическая фикстура повторяла ту же ошибку — и потому весь
# набор был зелёным, а оба живых листа падали ДО первой строки данных.
# Урок записан здесь, а не только в журнале: фикстура, сочинённая по той же
# памяти, что и код, ничего не проверяет. Ниже — форма, снятая read-only с
# точных публичных байтов обоих листов 31.08.2026; живой CSV в репозиторий не
# кладётся (там PII), в тестах — две синтетические формы этого каркаса.
#
# Наблюдены ДВЕ законные формы строки 2, и обе поддержаны намеренно:
#   * «Осень 26»  — промежуточные S/M/L пусты (объединённая ячейка горки),
#                   колонки итога и цены без подписи;
#   * «НГ 26/27»  — S/M/L подписаны явно, а колонки 15 и 16 подписаны
#                   «Общее количество» и «Цена».
# Больше ничего не разрешено: третья форма — это дрейф, и он fail closed.

#: Строка 1: подписи, которые ОБЯЗАНЫ стоять ровно здесь и ровно так.
#: Ключ — номер колонки с единицы, как их видит человек в таблице.
ROW1_REQUIRED_HEADERS: dict[int, str] = {
    3: "Наименование",
    4: "Эскиз",
    5: "Цвет",
    6: "Количество в м",
    7: "Комментарии",
    8: "Комментарии",
    9: "Комментарии",
    10: "Размерная горка",
    18: "Комплектующие",
    19: "Выбранное производство",
}

#: Строка 1: колонка артикула. Своего заголовка у неё нет ни на одном листе —
#: и пустота этого заголовка входит в контракт: текст, появившийся здесь,
#: означает, что каркас переехал, и молча читать дальше нельзя.
#: (На «НГ 26/27» в этой ячейке стоит перевод строки — после `strip()` это
#: та же пустота, и отдельным случаем она не является.)
ROW1_REQUIRED_EMPTY: tuple[int, ...] = (2,)

#: Подписи, по которым держится ВЕСЬ позиционный контракт. Каждая обязана
#: встречаться в строке 1 РОВНО ОДИН РАЗ. Без этой проверки сдвиг каркаса на
#: колонку (ровно тот дефект, что был отвергнут ревью) мог бы пройти незаметно
#: там, где подпись случайно повторяется.
ROW1_UNIQUE_HEADERS: tuple[str, ...] = (
    "Наименование", "Эскиз", "Цвет", "Количество в м",
    "Размерная горка", "Комплектующие", "Выбранное производство",
)

#: Колонка «Наименование». Артикул выводится как РОВНО ОДНА колонка
#: непосредственно перед ней — собственного заголовка у него нет.
NAME_COLUMN = 3

#: Размерная горка. Позиционное сопоставление XS,S,M,L,XL разрешено ТОЛЬКО
#: при ровно пяти последовательных колонках, где первая помечена XS, а пятая XL.
SIZE_BAND_START = 10
SIZE_LABELS: tuple[str, ...] = ("XS", "S", "M", "L", "XL")

#: Итог источника и цена — сразу за размерной горкой. На одном листе они
#: подписаны в строке 2, на другом подписи нет вовсе; обе формы законны, а
#: любая ТРЕТЬЯ подпись — дрейф.
SOURCE_TOTAL_COLUMN = 15
PRICE_COLUMN = 16
ROW2_OPTIONAL_LABELS: dict[int, str] = {
    SOURCE_TOTAL_COLUMN: "Общее количество",
    PRICE_COLUMN: "Цена",
}

#: Первая физическая строка данных.
FIRST_DATA_ROW = 3

#: Именованные колонки сверх размеров и идентичности. Словарь МАЛЕНЬКИЙ и
#: явный: всё, чего здесь нет, попадает в `unknown_raw` и поднимает
#: `unknown_column` — потерять чужую колонку хуже, чем показать её без имени.
#: Колонка 1 сюда НЕ входит намеренно: на разных листах в ней стоит разное
#: (на одном пусто, на другом «Цена ткани за м»), и придумывать ей общий
#: бизнес-смысл — это ровно то угадывание, которого здесь быть не должно.
NAMED_COLUMNS: dict[int, tuple[str, str]] = {
    4: ("sketch_raw", "Эскиз"),
    5: ("color_raw", "Цвет"),
    6: ("qty_meters_raw", "Количество в м"),
    SOURCE_TOTAL_COLUMN: ("source_total_raw", "Итог источника"),
    PRICE_COLUMN: ("price_raw", "Цена (сырое)"),
    18: ("components_raw", "Комплектующие"),
    19: ("production_raw", "Выбранное производство"),
}

#: Колонки комментариев. Их три, и они схлопыванию не подлежат: в источнике
#: это три разные колонки, и человек, который ищет свою пометку, ищет её в
#: своей колонке.
COMMENT_COLUMNS: tuple[int, ...] = (7, 8, 9)

#: Значения, которыми в источнике обозначают «размера нет». Тире — не ноль:
#: ноль означал бы «решили не шить», а тире — «здесь ничего не написано».
ABSENT_MARKS = frozenset({"-", "—", "–"})

# ── Неоднозначности ──────────────────────────────────────────────────────────

ISSUE_LABELS: dict[str, str] = {
    "orphan_continuation": "Продолжение без строки-родителя",
    "identity_missing_part": "Имя позиции неполное",
    "quantity_missing": "Нет количеств",
    "invalid_quantity": "Количество не читается как число",
    "total_mismatch": "Итог источника не сходится с суммой размеров",
    "unknown_column": "Неизвестная колонка источника",
}

#: «Ошибки» — источник противоречит сам себе или не читается числом.
#: Остальное — «требуют разбора»: прочитать можно, а трактовать однозначно нет.
#: Воронка: все ⊇ требуют разбора ⊇ ошибки.
INVALID_ISSUES = frozenset({"invalid_quantity", "total_mismatch"})

QUEUES = ("all", "needs_review", "invalid")


# ── Парсер ───────────────────────────────────────────────────────────────────

def _cell(row: list[str], col: int) -> str:
    """Ячейка по НОМЕРУ КОЛОНКИ С ЕДИНИЦЫ. Нет колонки — пустая строка."""
    if col < 1 or col > len(row):
        return ""
    return row[col - 1]


def decode_csv(sheet_name: str, blob: bytes) -> list[list[str]]:
    """Байты CSV → физические строки. Лимиты проверяются здесь, fail closed."""
    try:
        text = blob.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SourceError(
            f"лист «{sheet_name}»: ответ не читается как текст UTF-8"
        ) from None
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        raw_rows = list(reader)
    except csv.Error:
        raise SourceError(f"лист «{sheet_name}»: CSV не разбирается") from None

    if len(raw_rows) > MAX_ROWS_PER_SHEET:
        raise SourceError(
            f"лист «{sheet_name}»: строк больше допустимых {MAX_ROWS_PER_SHEET}"
        )
    rows: list[list[str]] = []
    for index, raw in enumerate(raw_rows, start=1):
        if len(raw) > MAX_COLUMNS:
            raise SourceError(
                f"лист «{sheet_name}», строка {index}: колонок больше "
                f"допустимых {MAX_COLUMNS}"
            )
        cells = []
        for value in raw:
            if len(value) > MAX_CELL_CHARS:
                raise SourceError(
                    f"лист «{sheet_name}», строка {index}: ячейка длиннее "
                    f"{MAX_CELL_CHARS} символов"
                )
            cells.append(value.strip())
        rows.append(cells)
    return rows


def check_header(sheet_name: str, rows: list[list[str]]) -> dict:
    """Проверяет контракт двух header rows. Не сошлось — `SourceError`.

    Fail closed НАМЕРЕННО. Дрейф заголовка — это не «немного другой файл», это
    смена смысла колонок: колонка итога перестала быть итогом, размерная горка
    поехала на позицию, и всё, что мы покажем дальше, будет уверенной
    неправдой. Пустой экран с честной ошибкой дешевле.

    Проверяется ЧЕТЫРЕ вещи, и каждая закрывает свой способ ошибиться:
      1. подписи каркаса стоят ровно на своих номерах колонок;
      2. каждая такая подпись встречается в строке 1 РОВНО ОДИН РАЗ — иначе
         сдвиг каркаса на колонку мог бы спрятаться за случайным повтором;
      3. у колонки артикула заголовка нет;
      4. размерная горка — ровно пять последовательных колонок XS…XL, а за
         ней две колонки, подписи которых либо отсутствуют, либо в точности
         те, что наблюдались. Никакой третьей формы.
    """
    if len(rows) < 2:
        raise SourceError(
            f"лист «{sheet_name}»: нет двух строк заголовка, которых требует формат"
        )
    row1, row2 = rows[0], rows[1]

    for col, expected in sorted(ROW1_REQUIRED_HEADERS.items()):
        actual = _cell(row1, col)
        if actual != expected:
            raise SourceError(
                f"лист «{sheet_name}»: формат заголовка изменился — в колонке "
                f"{col} ожидалось «{expected}», а стоит «{actual}»"
            )

    # Уникальность подписей каркаса. «Комментарии» сюда не входит: их в
    # источнике честно три подряд, и повтор там — норма, а не дрейф.
    for label in ROW1_UNIQUE_HEADERS:
        seen = [col for col in range(1, len(row1) + 1) if _cell(row1, col) == label]
        if len(seen) != 1:
            raise SourceError(
                f"лист «{sheet_name}»: подпись «{label}» встречается в строке "
                f"заголовка {len(seen)} раз(а) (колонки {seen or '—'}), а должна "
                f"ровно один — какая из них задаёт каркас, мы не угадываем"
            )

    for col in ROW1_REQUIRED_EMPTY:
        actual = _cell(row1, col)
        if actual != "":
            raise SourceError(
                f"лист «{sheet_name}»: формат заголовка изменился — колонка "
                f"{col} читается как артикул и обязана быть без заголовка, "
                f"а в ней стоит «{actual}»"
            )

    article_column = NAME_COLUMN - 1
    if article_column < 1:
        raise SourceError(
            f"лист «{sheet_name}»: перед «Наименование» нет колонки артикула"
        )

    # Размерная горка. Пять последовательных колонок; крайние подписаны всегда,
    # три промежуточные либо пусты (объединённая ячейка), либо подписаны ровно
    # своим размером. Ни одного «похоже, что это S» здесь нет.
    inferred: list[str] = []
    for offset, label in enumerate(SIZE_LABELS):
        col = SIZE_BAND_START + offset
        actual = _cell(row2, col)
        if offset in (0, len(SIZE_LABELS) - 1):
            if actual != label:
                raise SourceError(
                    f"лист «{sheet_name}»: размерная горка не опознана — в "
                    f"колонке {col} ожидалось «{label}», а стоит «{actual}»"
                )
        elif actual == "":
            inferred.append(label)
        elif actual != label:
            raise SourceError(
                f"лист «{sheet_name}»: размерная горка не опознана — в колонке "
                f"{col} ожидалось пусто или «{label}», а стоит «{actual}»"
            )
    band = set(range(SIZE_BAND_START, SIZE_BAND_START + len(SIZE_LABELS)))
    for col in range(1, len(row2) + 1):
        if col in band:
            continue
        if _cell(row2, col) in ("XS", "XL"):
            raise SourceError(
                f"лист «{sheet_name}»: за пределами размерной горки найдена "
                f"вторая метка размера в колонке {col} — какая из них настоящая, "
                f"мы не угадываем"
            )

    # Итог и цена. Подпись либо отсутствует, либо ровно ожидаемая.
    trailing: dict[int, str] = {}
    for col, expected in sorted(ROW2_OPTIONAL_LABELS.items()):
        actual = _cell(row2, col)
        if actual not in ("", expected):
            raise SourceError(
                f"лист «{sheet_name}»: колонка {col} читается по позиции и "
                f"должна быть либо без подписи, либо подписана «{expected}» — "
                f"а в ней стоит «{actual}»"
            )
        trailing[col] = actual

    # Свободные колонки: те, которым этот слой НЕ назначает смысла. Их
    # заголовок сохраняется как наблюдение (на «НГ 26/27» колонка 1 подписана
    # «Цена ткани за м», на «Осень 26» — пуста), но бизнес-смысл из подписи не
    # выводится: значения таких колонок едут в `unknown_raw` как есть.
    known = _known_columns(article_column)
    free: dict[str, str] = {}
    for col in range(1, max(len(row1), len(row2)) + 1):
        if col in known:
            continue
        header = _cell(row1, col) or _cell(row2, col)
        if header:
            free[str(col)] = header

    return {
        "header_rows": 2,
        "name_column": NAME_COLUMN,
        "article_column": article_column,
        # Артикул выведен, а не прочитан из заголовка: собственного заголовка
        # у колонки нет. Отмечаем это прямо в метаданных схемы, чтобы вывод
        # не выдавался за прочитанный факт.
        "article_column_inferred": True,
        "size_band_start": SIZE_BAND_START,
        "size_labels": list(SIZE_LABELS),
        # Какие именно размеры выведены позиционно, а какие подписаны в
        # источнике, — зависит от листа, и это факт о конкретном листе.
        "size_labels_inferred": inferred,
        "source_total_column": SOURCE_TOTAL_COLUMN,
        "source_total_header": trailing.get(SOURCE_TOTAL_COLUMN, ""),
        "source_total_column_inferred": not trailing.get(SOURCE_TOTAL_COLUMN),
        "price_column": PRICE_COLUMN,
        "price_header": trailing.get(PRICE_COLUMN, ""),
        "price_column_inferred": not trailing.get(PRICE_COLUMN),
        "first_data_row": FIRST_DATA_ROW,
        "comment_columns": list(COMMENT_COLUMNS),
        "free_columns": free,
    }


def _known_columns(article_column: int) -> set:
    """Колонки, которым слой назначает смысл. Всё прочее — свободное."""
    known = set(NAMED_COLUMNS) | set(COMMENT_COLUMNS) | {NAME_COLUMN, article_column}
    known |= set(range(SIZE_BAND_START, SIZE_BAND_START + len(SIZE_LABELS)))
    return known


def parse_quantity(raw: str) -> tuple[int | None, bool]:
    """Количество из ячейки: (значение, было ли оно испорчено).

    Три исхода, а не два: число, ОТСУТСТВИЕ (пусто или тире) и мусор. Мусор —
    это `invalid_quantity` с сохранённым raw, а не ноль: в исходной таблице в
    колонке размера встречается «Кроим по заданию», и превратить это в 0
    значило бы соврать про количество, которое человек написал словами.
    """
    value = (raw or "").strip()
    if value == "" or value in ABSENT_MARKS:
        return None, False
    # ТОЛЬКО ASCII-цифры. `\d` в Python юникодный: «٣», «１２» и прочие
    # цифроподобные строки он принимает, а `int()` их радостно разбирает — и в
    # предпросмотре появилось бы количество, которого человек в своей таблице
    # не писал. Такие строки сохраняются сырыми и помечаются нечитаемыми.
    if not re.fullmatch(r"[0-9]+", value):
        return None, True
    number = int(value)
    if number > MAX_QUANTITY:
        return None, True
    return number, False


def _blank(cells: list[str]) -> bool:
    return not any(c.strip() for c in cells)


def parse_sheet(sheet_name: str, rows: list[list[str]]) -> tuple[list[dict], dict]:
    """Физические строки одного листа → записи предпросмотра и метаданные схемы.

    Строки НЕ схлопываются. Строка-продолжение (обе части идентичности пустые)
    остаётся отдельной записью со ссылкой `anchor_row` на свою строку-якорь:
    трассировка до физической строки чужой таблицы дороже удобства, потому что
    разбирать неоднозначность человек пойдёт именно в таблицу.
    """
    schema = check_header(sheet_name, rows)
    article_column = schema["article_column"]

    out: list[dict] = []
    anchor: dict | None = None

    for index in range(FIRST_DATA_ROW - 1, len(rows)):
        cells = rows[index]
        source_row = index + 1
        issues: list[str] = []

        if _blank(cells):
            # Полностью пустая строка — разделитель: она сбрасывает якорь.
            anchor = None
            out.append({
                "sheet_name": sheet_name,
                "source_row": source_row,
                "anchor_row": None,
                "is_blank": True,
                "article_raw": "", "name_raw": "",
                "article": "", "name": "",
                "color_raw": "", "qty_meters_raw": "", "sketch_raw": "",
                "sizes": {label: None for label in SIZE_LABELS},
                "sizes_raw": {label: "" for label in SIZE_LABELS},
                "size_sum": None,
                "source_total_raw": "", "source_total": None,
                "comments_raw": ["", "", ""],
                "source_status_raw": "",
                "price_raw": "", "components_raw": "", "production_raw": "",
                "unknown_raw": {},
                "issues": [],
            })
            continue

        article_raw = _cell(cells, article_column)
        name_raw = _cell(cells, NAME_COLUMN)
        is_anchor = bool(article_raw or name_raw)

        if is_anchor:
            anchor = {"row": source_row, "article": article_raw, "name": name_raw}
            article = article_raw
            name = name_raw
            anchor_row = source_row
            if not (article_raw and name_raw):
                # Якорь есть, но половины идентичности нет. Это не ошибка
                # источника и не повод угадывать вторую половину: это строка,
                # которую человек обязан посмотреть глазами.
                issues.append("identity_missing_part")
        elif anchor is not None:
            article = anchor["article"]
            name = anchor["name"]
            anchor_row = anchor["row"]
        else:
            article = ""
            name = ""
            anchor_row = None
            issues.append("orphan_continuation")

        sizes: dict[str, int | None] = {}
        sizes_raw: dict[str, str] = {}
        invalid_size = False
        for offset, label in enumerate(SIZE_LABELS):
            raw = _cell(cells, SIZE_BAND_START + offset)
            sizes_raw[label] = raw
            value, broken = parse_quantity(raw)
            sizes[label] = value
            invalid_size = invalid_size or broken

        valid_sizes = [v for v in sizes.values() if v is not None]
        # size_sum — ОБЪЯСНЯЮЩИЙ показатель предпросмотра, а не итог партии.
        # Он существует затем, чтобы человек увидел, откуда взялось расхождение
        # с итогом источника, — и ничего не «исправляет».
        size_sum = sum(valid_sizes) if valid_sizes else None

        source_total_raw = _cell(cells, SOURCE_TOTAL_COLUMN)
        source_total, total_broken = parse_quantity(source_total_raw)
        if invalid_size or total_broken:
            issues.append("invalid_quantity")

        if (source_total is not None and size_sum is not None
                and source_total != size_sum):
            issues.append("total_mismatch")

        if is_anchor and not valid_sizes and source_total is None:
            # Спрашиваем количества только со строки-якоря: строка-продолжение
            # — это фрагмент уже названной позиции, и требовать количеств с
            # каждого фрагмента значило бы залить очередь ложной тревогой.
            issues.append("quantity_missing")

        comments = [_cell(cells, col) for col in COMMENT_COLUMNS]
        status_raw = next((c for c in comments if c), "")

        named: dict[str, str] = {}
        for col, (key, _title) in NAMED_COLUMNS.items():
            named[key] = _cell(cells, col)

        known = _known_columns(article_column)
        unknown_raw: dict[str, str] = {}
        for col in range(1, len(cells) + 1):
            if col in known:
                continue
            value = _cell(cells, col)
            if value:
                unknown_raw[str(col)] = value
        if unknown_raw:
            issues.append("unknown_column")

        out.append({
            "sheet_name": sheet_name,
            "source_row": source_row,
            "anchor_row": anchor_row,
            "is_blank": False,
            "article_raw": article_raw, "name_raw": name_raw,
            "article": article, "name": name,
            "color_raw": named.get("color_raw", ""),
            "qty_meters_raw": named.get("qty_meters_raw", ""),
            "sizes": sizes,
            "sizes_raw": sizes_raw,
            "size_sum": size_sum,
            "source_total_raw": source_total_raw,
            "source_total": source_total,
            "comments_raw": comments,
            # Сырой текст статуса источника. Он ОСТАЁТСЯ текстом: «Отгружено»
            # в чужой таблице и статус заказа в «Обороте» — разные величины,
            # и приравнять их значило бы придумать факт.
            "source_status_raw": status_raw,
            "price_raw": named.get("price_raw", ""),
            "sketch_raw": named.get("sketch_raw", ""),
            "components_raw": named.get("components_raw", ""),
            "production_raw": named.get("production_raw", ""),
            "unknown_raw": unknown_raw,
            "issues": sorted(set(issues)),
        })

    return out, schema


# ── Снимок: хеш, сборка, счётчики ────────────────────────────────────────────

def content_hash(spreadsheet_id: str, sheet_names: list[str],
                 blobs: list[bytes]) -> str:
    """SHA-256 по идентификатору, упорядоченным именам, БАЙТАМ CSV и версии парсера.

    Кодирование length-prefixed, а не через разделитель: имя листа может
    содержать любой символ, который мы бы взяли разделителем, и тогда два
    разных источника дали бы один хеш — то есть новый импорт выдался бы за
    «ничего не изменилось». Версия парсера входит в хеш потому, что тот же CSV,
    разобранный иначе, — другой снимок.
    """
    digest = hashlib.sha256()

    def part(chunk: bytes) -> None:
        digest.update(str(len(chunk)).encode("ascii"))
        digest.update(b"\x00")
        digest.update(chunk)

    part(PARSER_VERSION.encode("utf-8"))
    part(spreadsheet_id.encode("utf-8"))
    part(str(len(sheet_names)).encode("ascii"))
    for name, blob in zip(sheet_names, blobs):
        part(name.encode("utf-8"))
        part(blob)
    return digest.hexdigest()


def _row_flags(row: dict) -> tuple[bool, bool]:
    """(требует разбора, ошибка) — по одним лишь issues строки."""
    issues = set(row.get("issues") or ())
    return bool(issues), bool(issues & INVALID_ISSUES)


def build_counts(rows: list[dict], sheet_names: list[str]) -> dict:
    """Сводка по листам, строкам и количествам. Считается один раз, при импорте."""
    per_sheet: list[dict] = []
    issues_total: dict[str, int] = {}
    for name in sheet_names:
        subset = [r for r in rows if r["sheet_name"] == name]
        data_rows = [r for r in subset if not r["is_blank"]]
        needs = sum(1 for r in data_rows if _row_flags(r)[0])
        invalid = sum(1 for r in data_rows if _row_flags(r)[1])
        quantity = sum(r["size_sum"] or 0 for r in data_rows)
        per_sheet.append({
            "sheet_name": name,
            "rows": len(subset),
            "data_rows": len(data_rows),
            "needs_review": needs,
            "invalid": invalid,
            "quantity": quantity,
        })
    data_rows = [r for r in rows if not r["is_blank"]]
    for row in data_rows:
        for code in row["issues"]:
            issues_total[code] = issues_total.get(code, 0) + 1
    return {
        "sheets": per_sheet,
        "rows": len(rows),
        "data_rows": len(data_rows),
        "needs_review": sum(1 for r in data_rows if _row_flags(r)[0]),
        "invalid": sum(1 for r in data_rows if _row_flags(r)[1]),
        "quantity": sum(r["size_sum"] or 0 for r in data_rows),
        "issues": issues_total,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dump(envelope: dict) -> str:
    """Сериализация снимка. Одна на всех, чтобы «те же строки» значило «те же байты»."""
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def _guard_envelope_size(envelope: dict) -> str:
    blob = _dump(envelope)
    size = len(blob.encode("utf-8"))
    if size > MAX_ENVELOPE_BYTES:
        raise SourceError(
            f"предпросмотр не помещается в отведённый {MAX_ENVELOPE_BYTES // 1024} "
            f"КиБ (получилось {size // 1024} КиБ) — источник вырос за пределы, "
            f"на которые рассчитан этот раздел"
        )
    return blob


# ── Носитель снимка ──────────────────────────────────────────────────────────

def ordered_carriers(db: Session, org_id: int) -> list[Connection]:
    """ВСЕ основные связи организации в едином каноническом порядке.

    ОДИН помощник на чтение и на запись — намеренно. Пока порядок чтения и
    порядок записи задавались бы в двух местах, они рано или поздно разъехались
    бы, и снимок писался бы в одну строку, а читался из другой. Здесь этого не
    может случиться конструктивно: и `select_carrier`, и `read_envelope` берут
    список отсюда.

    Порядок задаётся кодом, а не базой: сначала `moysklad`, затем `demo`,
    внутри вида — наименьший id. Порядок вставки и время создания в него не
    входят вовсе: иначе носитель «переезжал» бы между двумя обновлениями
    страницы, и снимок исчезал бы на ровном месте.
    """
    rows = db.execute(
        select(Connection).where(
            Connection.org_id == org_id,
            Connection.kind.in_(PRIMARY_CONNECTION_KINDS),
        )
    ).scalars().all()
    return sorted(rows, key=lambda c: (_KIND_RANK.get(c.kind, 99), c.id))


def select_carrier(db: Session, org_id: int) -> Connection | None:
    """Канонический носитель ЗАПИСИ. Первый в каноническом порядке.

    Запись всегда идёт сюда — независимо от того, где сегодня физически лежит
    прежний снимок. Так у снимка есть ровно одно место, которое считается
    текущим, и «куда писать» не зависит от истории организации.
    """
    rows = ordered_carriers(db, org_id)
    return rows[0] if rows else None


def _load_config(conn: Connection) -> dict:
    raw = conn.config_json or "{}"
    try:
        data = json.loads(raw)
    except ValueError:
        raise CarrierConfigError(
            "настройки подключения не читаются как JSON — предпросмотр не стал "
            "их переписывать"
        ) from None
    if not isinstance(data, dict):
        raise CarrierConfigError(
            "настройки подключения не являются объектом JSON — предпросмотр не "
            "стал их переписывать"
        )
    return data


#: Форма envelope, которую умеет читать ЭТОТ код. Ключ → допустимые типы.
#: Проверяется на чтении, а не на записи: пишем мы всегда сами, а читаем то,
#: что оставил кто-то другой — прежняя версия кода, откат, ручная правка.
_ENVELOPE_SHAPE: dict[str, tuple] = {
    "schema_version": (int,),
    "parser_version": (str,),
    "spreadsheet_id": (str,),
    "sheet_names": (list,),
    "content_sha256": (str,),
    "last_error": (str,),
    "counts": (dict,),
    "rows": (list,),
}


# ── Форма СТРОКИ снимка, которую потребляют API и страница ──────────────────
#
# Зачем это отдельно от проверки верхнего уровня. Проверять только «rows —
# список словарей» недостаточно: дальше сервер и браузер обращаются к полям
# строки по типу, а не по факту. Замечание ревью PR #47, воспроизведённое на
# точном HEAD `c6919af`: строка `{"issues": 5}` проходила читателя, а потом
# `_row_flags` делал `set(5)` и падал `TypeError` — то есть вместо обещанного
# управляемого 409 получалась 500. То же и на стороне браузера: `comments_raw`
# числом ломает `forEach`, `sizes` строкой молча превращает размеры в прочерки.
#
# Поэтому проверяются ровно те поля, которые кто-то ДЕЙСТВИТЕЛЬНО разыменовывает
# (`preview`, `_row_flags`, `filter_rows`, `build_counts`, `templates/supply.html`),
# и проверяются они по УЖЕ ВЫПУСКАЕМОЙ форме v1.
#
# ОБЩИЕ поля снимка обязаны БЫТЬ, а не только иметь верный тип, если случайно
# оказались на месте. Прежняя версия этой проверки смотрела поле, только если
# оно есть, и потому принимала `rows: [{}]` за нормальный снимок — замечание
# ревью PR #47 на HEAD `0d9c226`. Испорченный носитель показывался бы человеку
# как обычная строка: без листа, без номера строки источника, без идентичности,
# без количеств и без очереди неоднозначностей — то есть как правда.
#
# Набор общих полей не сочинён по памяти. Оба парсера запущены на своих
# фикстурах, и пересечение ФАКТИЧЕСКИ выпущенных ключей — ровно перечисленные
# ниже 22 поля; у строки-разделителя и у обычной строки набор ключей
# одинаковый, и в `parser-1`, и в `parser-2`.
#
# Различие версий остаётся явным и совместимым. `sketch_raw` появился только в
# `parser-2`: он проверяется по типу, но НЕ требуется, иначе снимок `parser-1`
# перестал бы читаться. `extra_raw`, который нёс `parser-1`, сегодня не
# разыменовывает никто — он живёт как обычное незнакомое поле. Незнакомые
# лишние поля допустимы и дальше: запрещать их значило бы ломать снимки
# будущего кода без всякой пользы.
#
# Версия envelope при этом НЕ поднимается и миграции нет. Это не новый
# контракт, а тот же уже выпускаемый v1 — он просто наконец проверяется целиком.

#: Строковые поля: идентичность, цвет, сырые ячейки, статус источника.
_ROW_STRING_FIELDS: tuple[str, ...] = (
    "sheet_name", "article_raw", "name_raw", "article", "name",
    "color_raw", "qty_meters_raw", "source_total_raw", "source_status_raw",
    "price_raw", "sketch_raw", "components_raw", "production_raw",
)
#: Целые. `bool` исключается явно: в Python он подкласс `int`, и `True`
#: проехал бы как номер строки.
_ROW_INT_FIELDS: tuple[str, ...] = ("source_row",)
#: Целое ИЛИ None — «нет якоря», «нет суммы», «итога не было».
_ROW_OPTIONAL_INT_FIELDS: tuple[str, ...] = ("anchor_row", "size_sum", "source_total")
_ROW_BOOL_FIELDS: tuple[str, ...] = ("is_blank",)
#: Списки строк: три колонки комментариев и коды неоднозначностей.
_ROW_STRING_LIST_FIELDS: tuple[str, ...] = ("comments_raw", "issues")
#: Отображения «ключ → строка»: сырые размеры и неизвестные колонки.
_ROW_STRING_MAP_FIELDS: tuple[str, ...] = ("sizes_raw", "unknown_raw")
#: Отображение «размер → целое или None».
_ROW_OPTIONAL_INT_MAP_FIELDS: tuple[str, ...] = ("sizes",)

#: Поля, которые есть НЕ во всех выпущенных версиях снимка, и потому
#: обязательными быть не могут.
_ROW_VERSION_OPTIONAL_FIELDS: frozenset[str] = frozenset({"sketch_raw"})

#: Общие обязательные поля: все проверяемые минус версионно-необязательные.
#: Считается из тех же кортежей, а не выписывается рядом вторым списком: два
#: списка одних и тех же имён однажды разъедутся, и разъедутся молча.
_ROW_REQUIRED_FIELDS: tuple[str, ...] = tuple(
    field
    for field in (_ROW_STRING_FIELDS + _ROW_INT_FIELDS + _ROW_OPTIONAL_INT_FIELDS
                  + _ROW_BOOL_FIELDS + _ROW_STRING_LIST_FIELDS
                  + _ROW_STRING_MAP_FIELDS + _ROW_OPTIONAL_INT_MAP_FIELDS)
    if field not in _ROW_VERSION_OPTIONAL_FIELDS
)


def _is_int(value) -> bool:
    return type(value) is int


def _bad_row(index: int, field: str) -> CarrierConfigError:
    """Отказ по строке снимка.

    В сообщении НЕТ самого значения — только номер строки и имя поля. Значение
    приехало из чужой таблицы и может содержать что угодно, включая личные
    данные; показывать его в тексте ошибки нельзя.
    """
    return CarrierConfigError(
        f"Сохранённый предпросмотр повреждён: у строки №{index + 1} поле "
        f"«{field}» не того вида. Он оставлен как есть и не переписан."
    )


def _missing_row_field(index: int, field: str) -> CarrierConfigError:
    """Отказ по ОТСУТСТВУЮЩЕМУ общему полю строки.

    Отдельно от `_bad_row` затем, что это разные поломки: «поле не того вида» и
    «поля нет вовсе» приводят человека в разные места. Содержимого источника
    здесь тоже нет — только номер строки и имя поля.
    """
    return CarrierConfigError(
        f"Сохранённый предпросмотр повреждён: у строки №{index + 1} нет поля "
        f"«{field}». Он оставлен как есть и не переписан."
    )


def _validate_row(row: dict, index: int) -> None:
    """Минимально достаточная проверка формы строки. Fail closed.

    Сначала общие поля обязаны БЫТЬ, потом каждое присутствующее — своего вида.
    """
    for field in _ROW_REQUIRED_FIELDS:
        if field not in row:
            raise _missing_row_field(index, field)

    for field in _ROW_STRING_FIELDS:
        if field in row and not isinstance(row[field], str):
            raise _bad_row(index, field)

    for field in _ROW_INT_FIELDS:
        if field in row and not _is_int(row[field]):
            raise _bad_row(index, field)

    for field in _ROW_OPTIONAL_INT_FIELDS:
        if field in row and row[field] is not None and not _is_int(row[field]):
            raise _bad_row(index, field)

    for field in _ROW_BOOL_FIELDS:
        if field in row and type(row[field]) is not bool:
            raise _bad_row(index, field)

    for field in _ROW_STRING_LIST_FIELDS:
        if field not in row:
            continue
        value = row[field]
        if not isinstance(value, (list, tuple)):
            raise _bad_row(index, field)
        if any(not isinstance(item, str) for item in value):
            raise _bad_row(index, field)

    for field in _ROW_STRING_MAP_FIELDS:
        if field not in row:
            continue
        value = row[field]
        if not isinstance(value, dict):
            raise _bad_row(index, field)
        if any(not isinstance(k, str) or not isinstance(v, str)
               for k, v in value.items()):
            raise _bad_row(index, field)

    for field in _ROW_OPTIONAL_INT_MAP_FIELDS:
        if field not in row:
            continue
        value = row[field]
        if not isinstance(value, dict):
            raise _bad_row(index, field)
        for key, item in value.items():
            if not isinstance(key, str):
                raise _bad_row(index, field)
            if item is not None and not _is_int(item):
                raise _bad_row(index, field)


def _validate_envelope(envelope: dict) -> dict:
    """Снимок читаемый — или отказ. Третьего исхода нет намеренно.

    До этой проверки под versioned-ключом принимался ЛЮБОЙ словарь: комментарий
    обещал различение версий, а код его не делал (замечание ревью PR #47).
    Снимок, написанный будущей версией или испорченный руками, интерпретировался
    бы сегодняшним читателем — то есть показывался бы человеку как правда.

    Проверяется и ФОРМА СТРОК, а не только верхний уровень: см. `_validate_row`
    и комментарий над ним. Иначе обещанный управляемый 409 превращался бы в 500
    при первом же обращении потребителя к полю неверного типа.

    Fail closed, и ВАЖНО: отказ читателя ничего не переписывает. Испорченный
    снимок остаётся лежать как есть, чтобы его можно было посмотреть и понять,
    а не «починить» перезаписью, потеряв улику.
    """
    version = envelope.get("schema_version")
    if type(version) is not int or version != ENVELOPE_SCHEMA_VERSION:
        raise CarrierConfigError(
            f"Сохранённый предпросмотр сделан другой версией «Оборота» "
            f"(версия снимка {version!r}, эта версия читает "
            f"{ENVELOPE_SCHEMA_VERSION}). Он оставлен как есть и не переписан."
        )
    for key, types in _ENVELOPE_SHAPE.items():
        if key not in envelope:
            raise CarrierConfigError(
                f"Сохранённый предпросмотр повреждён: нет поля «{key}». "
                f"Он оставлен как есть и не переписан."
            )
        if not isinstance(envelope[key], types) or isinstance(envelope[key], bool):
            raise CarrierConfigError(
                f"Сохранённый предпросмотр повреждён: поле «{key}» не того "
                f"вида. Он оставлен как есть и не переписан."
            )
    for index, row in enumerate(envelope["rows"]):
        if not isinstance(row, dict):
            raise CarrierConfigError(
                "Сохранённый предпросмотр повреждён: строка снимка не является "
                "записью. Он оставлен как есть и не переписан."
            )
        _validate_row(row, index)
    return envelope


def _envelope_of(conn: Connection) -> dict | None:
    """Снимок ИЗ ОДНОЙ строки: None, если ключа нет; отказ, если он нечитаем."""
    data = _load_config(conn)
    if ENVELOPE_KEY not in data:
        return None
    envelope = data[ENVELOPE_KEY]
    if not isinstance(envelope, dict):
        raise CarrierConfigError(
            "Сохранённый предпросмотр повреждён: под своим ключом лежит не "
            "запись. Он оставлен как есть и не переписан."
        )
    return _validate_envelope(envelope)


def read_envelope(db: Session, org_id: int) -> tuple[Connection | None, dict | None]:
    """Где снимок ФАКТИЧЕСКИ лежит и что в нём. Только чтение, ни одной записи.

    ЗАЧЕМ ЭТО ОТДЕЛЬНО ОТ `select_carrier`. Носитель записи — канонический
    (первый в порядке), но снимок мог быть записан РАНЬШЕ, когда канонической
    была другая строка. Живой сценарий, найденный независимой проверкой: у
    организации есть удачный снимок в `demo`; позже появляется пустая связь
    `moysklad` — и чтение «только из канонического носителя» перестало бы
    показывать снимок вовсе. Данные при этом целы и лежат рядом. Обещание
    «прежний успешный снимок остаётся видимым» — часть контракта этого слоя, и
    держаться оно должно фактом, а не удачным порядком подключений.

    Поэтому читатель идёт по ТОМУ ЖЕ каноническому порядку и возвращает первую
    строку, где ключ действительно есть.

    И ГЛАВНОЕ ОГРАНИЧЕНИЕ: первый ВСТРЕЧЕННЫЙ снимок проверяется немедленно и
    fail closed. Перепрыгнуть повреждённый или неизвестной версии снимок к
    более старому нельзя — это подменило бы правду тем, что удобнее, и человек
    увидел бы старые данные, не зная, что рядом лежит непрочитанный снимок.
    Молчаливый откат к предыдущему состоянию — это и есть тот класс ошибок,
    против которого написан весь этот слой.
    """
    for conn in ordered_carriers(db, org_id):
        data = _load_config(conn)
        if ENVELOPE_KEY not in data:
            continue
        # Валидация ровно здесь, на ПЕРВОМ встреченном: см. абзац выше.
        return conn, _envelope_of(conn)
    return None, None


def get_envelope(db: Session, org_id: int) -> dict | None:
    """Фактический снимок организации или None. Чужие ключи не трогаются."""
    return read_envelope(db, org_id)[1]


def _store(db: Session, org_id: int, envelope: dict) -> None:
    """Записать снимок ОДНОЙ транзакцией, сохранив всё остальное содержимое.

    Меняется ровно одна колонка одной строки: `connections.config_json`.
    `token_enc`, `kind`, `status`, `last_sync_at` и поля `ms_*` носителя не
    присваиваются здесь ничем и никогда — время обновления Google живёт ВНУТРИ
    снимка, а не в `last_sync_at` чужой интеграции.
    """
    _guard_envelope_size(envelope)
    conn = select_carrier(db, org_id)
    if conn is None:
        raise NoCarrierError(
            "У организации нет основного подключения, в котором мог бы жить "
            "предпросмотр. Подключите МойСклад или демо-данные."
        )
    data = _load_config(conn)
    data[ENVELOPE_KEY] = envelope
    conn.config_json = json.dumps(data, ensure_ascii=False)
    db.commit()
    # Прежний носитель, если снимок лежал не здесь, НЕ трогается: ни его
    # снимок, ни любое другое содержимое его `config_json`. Удалять оттуда
    # копию было бы разрушительной операцией ради опрятности — а опрятность не
    # стоит риска остаться вовсе без снимка, если запись сюда потом откатят.


def _skeleton(spreadsheet_id: str, sheet_names: list[str]) -> dict:
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "spreadsheet_id": "",
        "sheet_names": [],
        "content_sha256": "",
        "last_attempt_at": None,
        "last_success_at": None,
        "fetched_at": None,
        "last_error": "",
        "last_attempt_source": {"spreadsheet_id": spreadsheet_id,
                                "sheet_names": list(sheet_names)},
        "schema": {},
        "counts": build_counts([], []),
        "rows": [],
    }


def _record_failure(db: Session, org_id: int, spreadsheet_id: str,
                    sheet_names: list[str], attempt_at: str, message: str) -> None:
    """Отметить неудачу, НЕ тронув прежний успешный снимок.

    Меняются ровно три поля: время попытки, её источник и безопасный текст
    ошибки. `content_sha256`, `rows`, `fetched_at` и `last_success_at` остаются
    теми же — иначе неудачное обновление стирало бы единственное, что у
    пользователя есть, и «не получилось» превращалось бы в «данных больше нет».
    """
    existing = get_envelope(db, org_id)
    envelope = dict(existing) if existing else _skeleton(spreadsheet_id, sheet_names)
    envelope["last_attempt_at"] = attempt_at
    envelope["last_attempt_source"] = {"spreadsheet_id": spreadsheet_id,
                                       "sheet_names": list(sheet_names)}
    envelope["last_error"] = message
    _store(db, org_id, envelope)


# ── Сериализация обновлений одной организации ────────────────────────────────
#
# Замок процессный и этого достаточно ровно в тех границах, в которых сегодня
# живёт приложение: один воркер. Второй процесс он бы не удержал, и здесь это
# написано прямо, а не подразумевается.

_ORG_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _org_lock(org_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _ORG_LOCKS.get(org_id)
        if lock is None:
            lock = threading.Lock()
            _ORG_LOCKS[org_id] = lock
        return lock


# ── Обновление снимка ────────────────────────────────────────────────────────

def refresh(db: Session, org_id: int, spreadsheet_url: str, sheet_names,
            timeout: float = FETCH_TIMEOUT_SECONDS) -> dict:
    """Скачать оба листа, разобрать их и записать ОДИН последний снимок.

    Порядок здесь — не стиль, а требование: сначала проверка входа, затем
    носитель (нет носителя — 409 ДО единого сетевого вызова), затем оба листа
    целиком, и только потом единственная запись в базу. Частичного снимка
    («первый лист новый, второй прежний») не существует ни на одном шаге.
    """
    spreadsheet_id = parse_spreadsheet_url(spreadsheet_url)
    names = validate_sheet_names(sheet_names)

    if select_carrier(db, org_id) is None:
        raise NoCarrierError(
            "У организации нет основного подключения, в котором мог бы жить "
            "предпросмотр. Подключите МойСклад или демо-данные."
        )

    # Читаемость уже лежащего снимка проверяется ДО сети: снимок, который мы не
    # умеем прочитать, мы не имеем права и перезаписать, а узнать об этом после
    # двух GET значило бы сходить в чужую систему впустую. Проверяется тот
    # снимок, который ФАКТИЧЕСКИ найден (он может лежать не в каноническом
    # носителе), — и повреждённый останавливает обновление здесь же.
    get_envelope(db, org_id)

    with _org_lock(org_id):
        attempt_at = _now_iso()
        try:
            blobs = [fetch_sheet_csv(spreadsheet_id, name, timeout) for name in names]
            rows: list[dict] = []
            schema: dict[str, dict] = {}
            for name, blob in zip(names, blobs):
                parsed, sheet_schema = parse_sheet(name, decode_csv(name, blob))
                rows.extend(parsed)
                schema[name] = sheet_schema
        except SourceError as exc:
            _record_failure(db, org_id, spreadsheet_id, names, attempt_at, str(exc))
            raise

        digest = content_hash(spreadsheet_id, names, blobs)
        existing = get_envelope(db, org_id)
        if (existing and existing.get("content_sha256") == digest
                and existing.get("last_success_at")):
            # Тот же источник, те же байты, тот же парсер. Строки не
            # переписываются вовсе: честно обновляется только метаданные
            # попытки, и выдавать это за новый импорт нельзя.
            envelope = dict(existing)
            envelope["last_attempt_at"] = attempt_at
            envelope["last_attempt_source"] = {"spreadsheet_id": spreadsheet_id,
                                               "sheet_names": list(names)}
            envelope["last_error"] = ""
            _store(db, org_id, envelope)
            return {
                "unchanged": True,
                "content_sha256": digest,
                "fetched_at": envelope.get("fetched_at"),
                "last_success_at": envelope.get("last_success_at"),
                "counts": envelope.get("counts") or build_counts([], names),
            }

        now = _now_iso()
        envelope = {
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "parser_version": PARSER_VERSION,
            "spreadsheet_id": spreadsheet_id,
            "sheet_names": list(names),
            "content_sha256": digest,
            "last_attempt_at": attempt_at,
            "last_success_at": now,
            "fetched_at": now,
            "last_error": "",
            "last_attempt_source": {"spreadsheet_id": spreadsheet_id,
                                    "sheet_names": list(names)},
            "schema": schema,
            "counts": build_counts(rows, names),
            "rows": rows,
        }
        try:
            # Размер проверяется ДО записи: снимок, который не помещается в
            # отведённый предел, не должен доехать до базы вовсе — прежний
            # остаётся на месте, а пользователь получает причину.
            _guard_envelope_size(envelope)
        except SourceError as exc:
            _record_failure(db, org_id, spreadsheet_id, names, attempt_at, str(exc))
            raise
        _store(db, org_id, envelope)
        return {
            "unchanged": False,
            "content_sha256": digest,
            "fetched_at": now,
            "last_success_at": now,
            "counts": envelope["counts"],
        }


# ── Непривязанная подсказка по каталогу ──────────────────────────────────────

def suggestion_from_candidates(candidates: list[str]) -> dict:
    """Ноль / один / несколько кандидатов — и ни одной сохранённой связи.

    Даже единственное точное совпадение остаётся КАНДИДАТОМ: связь позиции
    источника с карточкой каталога — продуктовое решение, а не вывод из
    совпадения строк. `product_id` наружу не отдаётся вовсе, чтобы клиент не
    смог случайно построить на подсказке привязку.
    """
    if not candidates:
        return {"status": "none", "count": 0, "candidates": [], "linked": False,
                "label": "Совпадений в каталоге нет"}
    if len(candidates) == 1:
        return {"status": "one", "count": 1, "candidates": list(candidates),
                "linked": False, "label": "Кандидат, не привязано"}
    return {"status": "many", "count": len(candidates),
            "candidates": list(candidates), "linked": False,
            "label": "Несколько кандидатов, не привязано"}


def name_candidates(db: Session, org_id: int, names: list[str]) -> dict[str, list[str]]:
    """Точное (посимвольное) совпадение имени с `base_name` каталога организации.

    Никакого fuzzy: ни регистронезависимости, ни расстояний, ни «похоже».
    Подсказка вычисляется в GET и никуда не сохраняется.

    ЧЕСТНАЯ ОГОВОРКА: сегодня ключ каталога — сам `base_name` (TECH_DEBT DATA-9),
    поэтому множество точных совпадений по имени вырождается в ноль или один,
    и исход «несколько» на живых данных недостижим. Ветка существует и
    проверяется на уровне `suggestion_from_candidates`, потому что закрытие
    DATA-9 сделает её достижимой; выдавать её за наблюдаемую сегодня — нельзя.
    """
    wanted = sorted({n for n in names if n})
    if not wanted:
        return {}
    rows = db.execute(
        select(Product.base_name).where(
            Product.org_id == org_id,
            Product.base_name.in_(wanted),
        ).distinct()
    ).scalars().all()
    found = set(rows)
    return {name: ([name] if name in found else []) for name in wanted}


# ── Чтение снимка ────────────────────────────────────────────────────────────

def filter_rows(rows: list[dict], sheet: str | None, queue: str) -> list[dict]:
    """Отбор строк для очереди. Воронка: все ⊇ требуют разбора ⊇ ошибки.

    Пустые строки-разделители остаются только в режиме «все»: в очереди
    разбора им делать нечего, а из общей таблицы убирать их нельзя — это
    физические строки чужого листа, и нумерация без них поедет.
    """
    out = [r for r in rows if not sheet or r.get("sheet_name") == sheet]
    if queue == "needs_review":
        out = [r for r in out if not r.get("is_blank") and _row_flags(r)[0]]
    elif queue == "invalid":
        out = [r for r in out if not r.get("is_blank") and _row_flags(r)[1]]
    return out


def _attempt_view(source) -> dict:
    """Последняя ПОПЫТКА как её показывают форме: ссылка и имена листов.

    Ссылка собирается сервером из сохранённого идентификатора — в браузер не
    уезжает ни одна строка, пришедшая от пользователя, даже та, которую он же
    и прислал. Идентификатор, не прошедший бы сегодняшнюю проверку, ссылкой не
    становится вовсе.
    """
    if not isinstance(source, dict):
        return {"spreadsheet_id": "", "spreadsheet_url": "", "sheet_names": []}
    raw_id = source.get("spreadsheet_id")
    spreadsheet_id = raw_id if isinstance(raw_id, str) else ""
    names = source.get("sheet_names")
    names = [n for n in names if isinstance(n, str)] if isinstance(names, list) else []
    return {
        "spreadsheet_id": spreadsheet_id,
        "spreadsheet_url": spreadsheet_link(spreadsheet_id) if spreadsheet_id else "",
        "sheet_names": names,
    }


def preview(db: Session, org_id: int, role: str, sheet: str | None = None,
            queue: str = "all", offset: int = 0, limit: int = 50) -> dict:
    """Страница снимка для GET. Только чтение, ни одной мутации."""
    if queue not in QUEUES:
        raise ValidationError("Неизвестный фильтр очереди.")
    try:
        offset = int(offset)
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValidationError("offset и limit должны быть целыми числами.") from None
    if offset < 0:
        raise ValidationError("offset не может быть отрицательным.")
    if limit < 1 or limit > 200:
        raise ValidationError("limit должен быть от 1 до 200.")

    # Один проход по носителям: есть ли вообще где хранить и где снимок лежит
    # фактически. `read_envelope` не пишет ничего — GET остаётся строго
    # read-only, включая случай, когда снимок нашёлся не в каноническом
    # носителе: переносить его «заодно» здесь нельзя, перенос — это запись.
    carriers = ordered_carriers(db, org_id)
    envelope = read_envelope(db, org_id)[1] if carriers else None

    base = {
        "role": role,
        "can_refresh": role == "owner",
        "carrier_present": bool(carriers),
        "queue": queue,
        "sheet": sheet or "",
        "offset": offset,
        "limit": limit,
        "issue_labels": dict(ISSUE_LABELS),
        "invalid_issues": sorted(INVALID_ISSUES),
        "size_labels": list(SIZE_LABELS),
        # Подпись, которая обязана быть видна рядом с любыми числами этого
        # экрана. Она приходит с сервера, а не живёт только в вёрстке, чтобы
        # её нельзя было потерять правкой шаблона.
        "disclaimer": ("Предпросмотр источника — ещё не партия «Оборота» "
                       "и не учитывается в «Едет»."),
    }
    if not envelope:
        base.update({
            "configured": False,
            "spreadsheet_url": "", "spreadsheet_id": "", "sheet_names": [],
            "last_success_at": None, "last_attempt_at": None,
            "fetched_at": None, "last_error": "",
            "content_sha256": "", "parser_version": PARSER_VERSION,
            "schema_version": ENVELOPE_SCHEMA_VERSION,
            "parser_stale": False,
            "attempt": _attempt_view(None),
            "counts": build_counts([], []), "total": 0, "rows": [],
        })
        return base

    rows = envelope.get("rows") or []
    sheet_names = list(envelope.get("sheet_names") or [])
    if sheet and sheet not in sheet_names:
        raise ValidationError("Такого листа в предпросмотре нет.")

    selected = filter_rows(rows, sheet or None, queue)
    page = selected[offset:offset + limit]

    suggestions = name_candidates(db, org_id,
                                  [r.get("name") or "" for r in page])
    out_rows = []
    for row in page:
        item = dict(row)
        name = row.get("name") or ""
        item["labels"] = [ISSUE_LABELS.get(code, code) for code in row.get("issues", [])]
        item["needs_review"], item["invalid"] = _row_flags(row)
        item["suggestion"] = (suggestion_from_candidates(suggestions.get(name, []))
                              if name else suggestion_from_candidates([]))
        out_rows.append(item)

    spreadsheet_id = envelope.get("spreadsheet_id") or ""
    base.update({
        "configured": bool(spreadsheet_id),
        "spreadsheet_id": spreadsheet_id,
        # Ссылку на источник строит СЕРВЕР из сохранённого идентификатора —
        # в браузер не уезжает ни одна строка, пришедшая от пользователя.
        "spreadsheet_url": spreadsheet_link(spreadsheet_id) if spreadsheet_id else "",
        "sheet_names": sheet_names,
        "last_success_at": envelope.get("last_success_at"),
        "last_attempt_at": envelope.get("last_attempt_at"),
        "fetched_at": envelope.get("fetched_at"),
        "last_error": envelope.get("last_error") or "",
        "content_sha256": envelope.get("content_sha256") or "",
        "parser_version": envelope.get("parser_version") or "",
        "schema_version": envelope.get("schema_version"),
        # Снимок, сделанный прежней версией разбора, читается — но выдавать
        # его за сегодняшнее прочтение нельзя: колонки тогда понимались иначе.
        # Лечится он сам, первым же обновлением (версия парсера входит в хеш).
        "parser_stale": bool(envelope.get("parser_version"))
                        and envelope.get("parser_version") != PARSER_VERSION,
        # Что человек вводил в прошлый раз. Нужно ровно для одного случая:
        # ПЕРВОЕ обновление не удалось, успешного снимка ещё нет, и без этого
        # человек увидел бы четырёхсекундный тост и пустую форму — то есть
        # вводил бы ссылку и два имени листов заново, не понимая, что пошло не
        # так. Это НЕ объявление источника настроенным: `configured` остаётся
        # false, пока нет ни одного удачного чтения.
        "attempt": _attempt_view(envelope.get("last_attempt_source")),
        "counts": envelope.get("counts") or build_counts([], sheet_names),
        "total": len(selected),
        "rows": out_rows,
    })
    return base
