"""Щоденний звіт і вивантаження замовлень у Excel — чиста логіка без Telegram."""
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

import texts

EXPORT_COLUMNS = [("№", 6), ("Дата", 17), ("Статус", 14), ("Ім'я", 22), ("Телефон", 16), ("Доставка", 40),
                  ("Оплата", 22), ("Коментар", 30), ("Товари", 60), ("Сума, грн", 11), ("Telegram", 18),
                  ("Оцінка", 8)]


def stars(rating: int) -> str:
    return "⭐" * rating


def daily_report(orders: list[dict], since: datetime, until: datetime) -> str:
    title = texts.REPORT_TITLE.format(since=since.strftime("%d.%m %H:%M"), until=until.strftime("%d.%m %H:%M"))
    if not orders:
        return f"{title}\n\n{texts.REPORT_EMPTY}"
    active = [o for o in orders if o["status"] != "cancelled"]
    revenue = sum(o["total"] for o in active)
    lines = [title, "",
             f"Замовлень: {len(orders)}",
             f"Сума (без скасованих): {revenue} грн",
             f"Середній чек: {round(revenue / len(active)) if active else 0} грн", ""]
    lines += [f"№{o['id']} · {o['created'][11:]} · {o['name']} · {o['total']} грн · {texts.STATUSES[o['status']]}"
              for o in orders]
    return "\n".join(lines)


def orders_xlsx(orders: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Замовлення"
    ws.append([name for name, _ in EXPORT_COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for n, (_, width) in enumerate(EXPORT_COLUMNS):
        ws.column_dimensions[chr(ord("A") + n)].width = width
    ws.freeze_panes = "A2"
    for o in orders:
        items = "; ".join(f"{i['title']} × {i['qty']}" for i in o["items"])
        ws.append([o["id"], o["created"], texts.STATUSES[o["status"]], o["name"], o["phone"], o["delivery"],
                   o["payment"], o["comment"], items, o["total"],
                   f"@{o['username']}" if o["username"] else "", o["rating"] or ""])
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
