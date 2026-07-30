"""Экспорт ключевых таблиц в Excel (.xlsx) — openpyxl, без pandas.

Целевая аудитория живёт в Excel: выгрузка — мостик доверия («могу проверить
и доработать у себя»). Данные НЕ пересчитываются — билдеры получают готовые
ответы analytics.build_replenish / build_turnover, analytics_extra.build_budget,
analytics_markdown.build_discounts и только раскладывают их по ячейкам.

Оформление едино для всех листов:
- A1 (объединённая через все колонки, серым) — организация + дата выгрузки;
- строка 2 — жирная шапка с заливкой, заморожена (freeze_panes = A3);
- автоширина колонок по содержимому, максимум 45 символов;
- числа: # ##0 (в xlsx — канонический код #,##0: русский Excel показывает
  пробелы-разряды), деньги — ₽ без копеек, проценты — 0%;
- итоговая строка жирным с верхней границей, где уместно.

Ручки отдают файл через xlsx_response(): StreamingResponse с русским именем
файла по RFC 5987 (filename* = UTF-8''...) и ASCII-fallback в filename=.
"""
import io
from datetime import date
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Числовые форматы (#,##0 — канонический код «# ##0» русского Excel).
FMT_INT = "#,##0"
FMT_MONEY = '#,##0" ₽"'
FMT_PCT = "0%"
FMT_NUM1 = "#,##0.0"
FMT_NUM2 = "#,##0.00"

CLS_RU = {"weak": "Слабый", "dull": "Унылый", "good": "Хороший", "best": "Бестселлер"}

_TITLE_FONT = Font(color="808080", size=9)
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
_HEADER_FILL = PatternFill("solid", fgColor="1F2B3D")  # тёмно-синий, как сайдбар
_SIZE_FONT = Font(color="6B7280", size=10)             # строки размеров — серым
_TOTAL_FONT = Font(bold=True)
_TOTAL_BORDER = Border(top=Side(style="thin", color="9CA3AF"))
_WRAP = Alignment(vertical="center", wrap_text=True)


def _fmt_date_ru(iso: str | None) -> str:
    """ISO-дата → дд.мм.гггг ('' если None)."""
    if not iso:
        return ""
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso


def _new_sheet(wb: Workbook, sheet_title: str, org_name: str, ncols: int,
               subtitle: str = "") -> Worksheet:
    """Лист с русским названием и серой строкой A1: организация + дата выгрузки."""
    ws = wb.active
    ws.title = sheet_title
    text = f"{org_name} · выгрузка от {date.today().strftime('%d.%m.%Y')}"
    if subtitle:
        text += f" · {subtitle}"
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    cell = ws.cell(row=1, column=1, value=text)
    cell.font = _TITLE_FONT
    cell.alignment = Alignment(vertical="center")
    return ws


def _write_header(ws: Worksheet, headers: list[str]) -> None:
    """Строка 2: жирная шапка с заливкой; заморозка шапки (freeze A3)."""
    for i, title in enumerate(headers, 1):
        cell = ws.cell(row=2, column=i, value=title)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _WRAP
    ws.freeze_panes = "A3"


def _write_row(ws: Worksheet, row_idx: int, values: list, formats: dict[int, str],
               font: Font | None = None) -> None:
    """Строка данных: values по колонкам (1-based formats), None → пустая ячейка."""
    for i, v in enumerate(values, 1):
        if v is None:
            v = ""
        cell = ws.cell(row=row_idx, column=i, value=v)
        fmt = formats.get(i)
        if fmt and isinstance(v, (int, float)) and not isinstance(v, bool):
            cell.number_format = fmt
        if font is not None:
            cell.font = font


def _write_total(ws: Worksheet, row_idx: int, ncols: int, values: dict[int, object],
                 formats: dict[int, str], label: str = "Итого") -> None:
    """Итоговая строка: жирным, с верхней границей по всей ширине."""
    for i in range(1, ncols + 1):
        cell = ws.cell(row=row_idx, column=i, value=values.get(i, ""))
        cell.font = _TOTAL_FONT
        cell.border = _TOTAL_BORDER
        fmt = formats.get(i)
        v = values.get(i)
        if fmt and isinstance(v, (int, float)) and not isinstance(v, bool):
            cell.number_format = fmt
    ws.cell(row=row_idx, column=1, value=label).font = _TOTAL_FONT


def _autofit(ws: Worksheet, min_width: int = 6, max_width: int = 45) -> None:
    """Автоширина колонок по содержимому (строка A1-титула не учитывается)."""
    widths: dict[str, int] = {}
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            v = cell.value
            if v is None or v == "":
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                # длина с разрядами-пробелами и возможным « ₽»
                length = len(f"{v:,.0f}") + 2
            else:
                length = max(len(part) for part in str(v).split("\n"))
            col = cell.column_letter
            widths[col] = max(widths.get(col, 0), length)
    for col, w in widths.items():
        ws.column_dimensions[col].width = min(max_width, max(min_width, w + 2))


def xlsx_response(wb: Workbook, filename_ru: str, filename_ascii: str) -> StreamingResponse:
    """Workbook → StreamingResponse с русским именем файла (RFC 5987 filename*)."""
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    disposition = (
        f'attachment; filename="{filename_ascii}"; '
        f"filename*=UTF-8''{quote(filename_ru)}"
    )
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": disposition, "Cache-Control": "no-store"},
    )


# ── «Что заказать» ────────────────────────────────────────────────────────────

def replenish_workbook(org_name: str, data: dict) -> Workbook:
    """Лист «Что заказать»: позиции с need>0 + строки размеров отступом «— S»."""
    headers = [
        "Позиция", "Категория", "Класс", "Оборачиваемость, ₽/день", "Темп, шт/день",
        "Продано за год, шт", "Остаток, шт", "Едет, шт", "Покрытие, нед",
        "Дата стокаута", "Заказать, шт", "Себестоимость за шт, ₽", "Сумма заказа, ₽",
    ]
    formats = {
        4: FMT_MONEY, 5: FMT_NUM2, 6: FMT_INT, 7: FMT_INT, 8: FMT_INT,
        9: FMT_NUM1, 11: FMT_INT, 12: FMT_MONEY, 13: FMT_MONEY,
    }
    wb = Workbook()
    ws = _new_sheet(wb, "Что заказать", org_name, len(headers),
                    subtitle=f"горизонт {data.get('horizon_days', 90)} дней")
    _write_header(ws, headers)

    row = 3
    total_need = 0
    total_sum = 0.0
    for it in data.get("items", []):
        cost = float(it.get("cost_price") or 0)
        order_sum = round(it["need"] * cost)
        sold_year = sum(round(s.get("sold365") or 0) for s in it.get("sizes", {}).values())
        total_need += it["need"]
        total_sum += order_sum
        _write_row(ws, row, [
            it["base_name"], it.get("category") or "", CLS_RU.get(it["cls"], it["cls"]),
            it["turnover"], it["rate"], sold_year, it["cs"], it.get("ordered") or 0,
            it.get("wos"), _fmt_date_ru(it.get("stockout_date")), it["need"],
            cost if cost > 0 else None, order_sum,
        ], formats)
        row += 1
        # Размерная сетка: сток / продано за год / рекомендация — отступом.
        for size, s in it.get("sizes", {}).items():
            _write_row(ws, row, [
                f"— {size}", None, None, None, None,
                round(s.get("sold365") or 0), s.get("stock") or 0, None, None, None,
                s.get("rec") or 0, None, None,
            ], formats, font=_SIZE_FONT)
            row += 1

    _write_total(
        ws, row, len(headers),
        {11: total_need, 13: round(total_sum)}, formats,
        label=f"Итого: {len(data.get('items', []))} поз.",
    )
    _autofit(ws)
    return wb


# ── «Оборачиваемость» ─────────────────────────────────────────────────────────

def turnover_workbook(org_name: str, data: dict) -> Workbook:
    """Лист «Оборачиваемость»: все колонки страницы /turnover."""
    headers = [
        "Позиция", "Категория", "Класс", "Дней в стоке", "Продано, шт",
        "Выручка, ₽", "Оборачиваемость, ₽/день", "Ср. цена, ₽", "Номинал, ₽",
        "Скидка факт.", "Остаток, шт", "Покрытие, нед", "Дата стокаута", "Архив",
    ]
    formats = {
        4: FMT_INT, 5: FMT_INT, 6: FMT_MONEY, 7: FMT_MONEY, 8: FMT_MONEY,
        9: FMT_MONEY, 10: FMT_PCT, 11: FMT_INT, 12: FMT_NUM1,
    }
    wb = Workbook()
    ws = _new_sheet(wb, "Оборачиваемость", org_name, len(headers),
                    subtitle="метрики за скользящие 365 дней")
    _write_header(ws, headers)

    row = 3
    t_nq = t_nr = t_turn = t_cs = 0
    for it in data.get("items", []):
        t_nq += it["nq"]
        t_nr += it["nr"]
        t_turn += it["turnover"]
        t_cs += it["cs"]
        _write_row(ws, row, [
            it["base_name"], it.get("category") or "", CLS_RU.get(it["cls"], it["cls"]),
            it["dis"], it["nq"], it["nr"], it["turnover"], it.get("avg_price"),
            it.get("sale_price"), it.get("discount_fact"), it["cs"], it.get("wos"),
            _fmt_date_ru(it.get("stockout_date")), "да" if it.get("archived") else "",
        ], formats)
        row += 1

    _write_total(
        ws, row, len(headers),
        {5: t_nq, 6: t_nr, 7: t_turn, 11: t_cs}, formats,
        label=f"Итого: {len(data.get('items', []))} поз.",
    )
    _autofit(ws)
    return wb


# ── «Уценка» ──────────────────────────────────────────────────────────────────

def discounts_workbook(org_name: str, data: dict) -> Workbook:
    """Лист «Уценка»: прайс уценки со скидками и причинами."""
    headers = [
        "Позиция", "Категория", "Остаток, шт", "Заморожено, ₽", "Скидка, %",
        "Старая цена, ₽", "Новая цена, ₽", "Вернёт, ₽", "Причина",
    ]
    formats = {
        3: FMT_INT, 4: FMT_MONEY, 5: FMT_PCT, 6: FMT_MONEY, 7: FMT_MONEY, 8: FMT_MONEY,
    }
    wb = Workbook()
    ws = _new_sheet(wb, "Уценка", org_name, len(headers), subtitle="прайс уценки")
    _write_header(ws, headers)

    row = 3
    t_cs = t_frozen = t_rec = 0
    for it in data.get("items", []):
        t_cs += it["cs"]
        t_frozen += it["frozen"]
        t_rec += it["expected_recovery"]
        star = " *" if it.get("no_cost") else ""
        _write_row(ws, row, [
            it["base_name"] + star, it.get("category") or "", it["cs"], it["frozen"],
            it["discount_pct"] / 100.0, it["avg_price"], it["new_price"],
            it["expected_recovery"], it.get("reason") or "",
        ], formats)
        row += 1

    _write_total(
        ws, row, len(headers),
        {3: t_cs, 4: t_frozen, 8: t_rec}, formats,
        label=f"Итого: {len(data.get('items', []))} поз.",
    )
    if any(it.get("no_cost") for it in data.get("items", [])):
        note = ws.cell(row=row + 2, column=1,
                       value="* — нет себестоимости, заморозка посчитана по средней цене продажи")
        note.font = _TITLE_FONT
    _autofit(ws)
    return wb


# ── «Бюджет закупки» ──────────────────────────────────────────────────────────

def budget_workbook(org_name: str, data: dict) -> Workbook:
    """Лист «Бюджет закупки»: распределение бюджета по оборачиваемости."""
    headers = [
        "Позиция", "Категория", "Класс", "Оборачиваемость, ₽/день", "Темп, шт/мес",
        "Остаток+едет, шт", "Запас, дн", "Потребность, шт", "Заказать, шт",
        "Себестоимость за шт, ₽", "Сумма, ₽", "Ожидаемая прибыль, ₽", "Примечание",
    ]
    formats = {
        4: FMT_MONEY, 5: FMT_NUM1, 6: FMT_INT, 7: FMT_INT, 8: FMT_INT,
        9: FMT_INT, 10: FMT_MONEY, 11: FMT_MONEY, 12: FMT_MONEY,
    }
    subtitle = (
        f"бюджет {data.get('amount', 0):,.0f} ₽".replace(",", " ")
        + f", лимит на позицию {data.get('max_share', 0)}%"
    )
    wb = Workbook()
    ws = _new_sheet(wb, "Бюджет закупки", org_name, len(headers), subtitle=subtitle)
    _write_header(ws, headers)

    row = 3
    for it in data.get("items", []):
        if it.get("over_limit"):
            note = "дорогая позиция — 1 шт сверх лимита доли"
        elif it.get("capped") and it.get("cap_reason") == "share":
            note = f"лимит доли; потребность {it['need']} шт"
        elif it.get("capped"):
            note = f"бюджет исчерпан; потребность {it['need']} шт"
        else:
            note = ""
        _write_row(ws, row, [
            it["base_name"], it.get("category") or "", CLS_RU.get(it["cls"], it["cls"]),
            it["turnover"], round(it["rate"] * 30, 1), it["stock_eff"],
            it.get("days_left"), it["need"], it["qty"], it["cost_price"], it["total"],
            it["expected_profit"], note,
        ], formats)
        row += 1

    totals = data.get("totals", {})
    _write_total(
        ws, row, len(headers),
        {
            9: totals.get("units", 0),
            11: data.get("used", 0),
            12: totals.get("expected_profit", 0),
        },
        formats,
        label=f"Итого: {totals.get('positions', 0)} поз.",
    )
    note = ws.cell(
        row=row + 2, column=1,
        value=f"Использовано {data.get('used', 0):,.0f} ₽ из {data.get('amount', 0):,.0f} ₽, "
              f"остаток {data.get('rest', 0):,.0f} ₽".replace(",", " "),
    )
    note.font = _TITLE_FONT
    _autofit(ws)
    return wb
