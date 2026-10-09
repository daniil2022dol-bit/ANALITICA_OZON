from datetime import date, datetime
from io import BytesIO

import openpyxl
import pytest

from ozon_analytics.storage import parse_report


def xlsx(rows):
    book = openpyxl.Workbook()
    for row in rows:
        book.active.append(row)
    target = BytesIO()
    book.save(target)
    return target.getvalue()


def test_supplies_summary_is_not_free_storage_deadline():
    data = xlsx(
        [
            ["Период 06.10.2026 - 07.10.2026"],
            [],
            [],
            [
                "SKU",
                "Номер поставки",
                "Склад поставки",
                "Дней до конца периода",
                "Дата окончания периода по поставкам",
                "06.окт",
                "07.окт",
            ],
            [1, None, None, 0, datetime(2026, 10, 7), 100, 100],
            [1, "SUP-1", "ТВЕРЬ_РФЦ", 6, datetime(2026, 10, 14), 50, 40],
            [1, "SUP-2", "ТВЕРЬ_РФЦ", -2, datetime(2026, 10, 4), 50, 60],
        ]
    )
    _, rows = parse_report(data, "supplies")
    assert rows[0]["free_until"] is None
    assert rows[0]["free_days_left"] is None
    assert (
        rows[1]["free_days_left"] == 6
    )  # Explicit Ozon counter, not date subtraction.
    assert rows[1]["quantity"] == 40
    assert rows[2]["free_days_left"] == -2
    assert rows[2]["quantity"] == 60


def test_product_fee_date_and_unknown_fields_preserved():
    data = xlsx(
        [
            [
                "Дата",
                "SKU",
                "Артикул",
                "Склад",
                "Кол-во экземпляров",
                "Кол-во платных экземпляров",
                "Начисленная стоимость размещения",
                "Новая колонка",
            ],
            [datetime(2026, 10, 7), 1, "ARTICLE", "ТВЕРЬ", 5, 2, 3.75, "kept"],
        ]
    )
    _, rows = parse_report(data, "products")
    assert str(rows[0]["row_day"]) == "2026-10-07"
    assert rows[0]["paid_quantity"] == 2
    assert str(rows[0]["fee"]) == "3.75"
    assert rows[0]["raw"]["Новая колонка"] == "kept"


def test_unknown_report_shape_rejected():
    with pytest.raises(ValueError, match="Unknown storage report"):
        parse_report(xlsx([["SKU", "Something"], [1, 5]]), "supplies")


def test_report_volume_type_and_actual_stock_are_separate_from_free_allowance():
    day = date(2026, 10, 9)
    _, products = parse_report(
        xlsx(
            [
                [
                    "Дата",
                    "SKU",
                    "Артикул",
                    "Кол-во экземпляров",
                    "Суммарный объем в миллилитрах",
                    "Платный объем в миллилитрах",
                    "Категория товара",
                    "Описательный тип",
                    "Признак товара",
                ],
                [
                    datetime(2026, 10, 9),
                    1,
                    "SKU-1",
                    1,
                    "3 530,0",
                    "—",
                    "Техника",
                    "Пластик",
                    "Особый",
                ],
            ]
        ),
        "products",
        report_day=day,
    )
    assert products[0]["volume_ml"] == 3530
    assert products[0]["paid_volume_ml"] is None
    assert products[0]["product_type"] == "Пластик"
    assert products[0]["item_feature"] == "Особый"
    assert products[0]["quantity_day"] == day
    _, supplies = parse_report(
        xlsx(
            [
                [
                    "SKU",
                    "Номер поставки",
                    "Остаток на складах на (09.10.2026)",
                    "08.окт",
                    "09.окт",
                ],
                [1, None, 90, 100, 100],
                [1, "S1", None, 150, 150],
            ]
        ),
        "supplies",
        report_day=day,
    )
    assert supplies[0]["stock_quantity"] == 90
    assert supplies[0]["quantity"] is None
    assert supplies[1]["stock_quantity"] is None
    assert supplies[1]["quantity"] == 150
    assert supplies[1]["quantity_day"] == day
