import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import openpyxl


def normalize(value):
    return re.sub(r"[^а-яa-z0-9]", "", str(value or "").casefold().replace("ё", "е"))


def number(value):
    if value is None or value == "":
        return None
    try:
        result = Decimal(
            str(value).replace(" ", "").replace("\xa0", "").replace(",", ".")
        )
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return None


def parse_report(content, kind, report_day=None):
    """Read confirmed Ozon columns; retain every row, including unknown fields.

    Supplies headers confirmed against the live report on 2026-10-07:
    'Дней до конца периода', 'Дата окончания периода по поставкам'.
    SKU summary rows are not supply deadlines and must never create alerts.
    """
    if content[:2] == b"PK":
        book = openpyxl.load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
        sheets = [list(sheet.iter_rows(values_only=True)) for sheet in book]
        book.close()
    else:
        text = content.decode("utf-8-sig")
        sheets = [
            list(
                csv.reader(
                    io.StringIO(text),
                    delimiter=";" if text.count(";") > text.count(",") else ",",
                )
            )
        ]
    headers_seen, result = [], []
    for sheet in sheets:
        header_index = next(
            (
                i
                for i, row in enumerate(sheet[:30])
                if "sku" in {normalize(x) for x in row}
                and any(normalize(x) in ("артикул", "номерпоставки") for x in row)
            ),
            None,
        )
        if header_index is None:
            raise ValueError(
                "Unknown storage report columns: SKU and article/supply are required"
            )
        headers = [str(x or "") for x in sheet[header_index]]
        headers_seen.extend(headers)
        for _line_number, values in enumerate(
            sheet[header_index + 1 :], start=header_index + 2
        ):
            raw = {
                h: (v.isoformat() if isinstance(v, (datetime, date)) else v)
                for h, v in zip(headers, values, strict=False)
                if h
            }
            norm = {normalize(h): v for h, v in zip(headers, values, strict=False)}
            sku = number(norm.get("sku"))
            if sku is None:
                continue
            supply = norm.get("номерпоставки")
            quantity = number(norm.get("колвоэкземпляров"))
            if kind == "supplies" and supply:
                # These are free placement allowances, not physical lot stock.
                dated = [
                    number(v)
                    for h, v in zip(headers, values, strict=False)
                    if re.match(r"^\d{2}\.", h)
                ]
                quantity = dated[-1] if dated else None
            free_days = number(norm.get("днейдоконцапериода")) if supply else None
            row_day = as_date(norm.get("дата"))
            stock_quantity = next(
                (
                    number(v)
                    for h, v in norm.items()
                    if h.startswith("остатокнаскладахна")
                ),
                None,
            )
            result.append(
                {
                    "row_number": len(result) + 1,
                    "sku": int(sku),
                    "offer_id": norm.get("артикул"),
                    "warehouse_name": norm.get("склад") or norm.get("складпоставки"),
                    "supply_id": str(supply) if supply else None,
                    "row_day": row_day,
                    "free_until": as_date(norm.get("датаокончанияпериодапопоставкам"))
                    if supply
                    else None,
                    "free_days_left": int(free_days) if free_days is not None else None,
                    "quantity": quantity,
                    "paid_quantity": number(norm.get("колвоплатныхэкземпляров")),
                    "fee": number(norm.get("начисленнаястоимостьразмещения")),
                    "volume_ml": number(norm.get("суммарныйобъемвмиллилитрах")),
                    "paid_volume_ml": number(norm.get("платныйобъемвмиллилитрах")),
                    "category": norm.get("категориятовара"),
                    "product_type": norm.get("описательныйтип"),
                    "item_feature": norm.get("признактовара"),
                    "quantity_day": row_day if kind == "products" else report_day,
                    "stock_quantity": stock_quantity,
                    "raw": raw,
                }
            )
    return headers_seen, result
