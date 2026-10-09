from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from ozon_analytics import web
from ozon_analytics.database import migrate
from ozon_analytics.storage_forecast import build_projection, storage_projection

DAY = date(2026, 10, 9)


def product(**changes):
    row = {
        "sku": 1,
        "row_day": DAY,
        "warehouse_name": "ТВЕРЬ",
        "warehouse_id": 10,
        "cluster_id": 20,
        "quantity": 100,
        "paid_quantity": 20,
        "fee": 40,
        "volume_ml": 200000,
        "paid_volume_ml": 40000,
        "category": "Техника",
        "product_type": "Пластик",
        "item_feature": "",
    }
    return row | changes


def supply(**changes):
    return {
        "sku": 1,
        "supply_id": "S1",
        "warehouse_name": "Склад поставки",
        "quantity": 80,
        "free_until": DAY,
        "quantity_day": DAY,
    } | changes


def projection(products, supplies, observations=None, **filters):
    return build_projection(
        products,
        supplies,
        products if observations is None else observations,
        DAY,
        **filters,
    )


def test_excess_free_capacity_is_not_stock_and_expiry_is_inclusive():
    # 150 allowance units cover 80 physically free units. The first 60 expiring
    # units are spare allowance, so tomorrow's cost must remain unchanged.
    d = projection(
        [product()],
        [
            supply(quantity=60),
            supply(supply_id="S2", quantity=90, free_until=DAY + timedelta(days=6)),
        ],
    )
    assert d["actual"]["units"] == 100
    assert d["timeline"][1]["low"] == 40
    assert d["timeline"][6]["low"] == 40
    assert d["timeline"][7]["low"] == 200
    assert d["timeline"][7]["paid_units"] == 100
    assert d["rows"][0]["first_cost_increase"] == DAY + timedelta(days=7)
    assert d["totals"]["week"]["low"] == 440


def test_actual_charge_is_calibrated_and_unknown_expiry_is_a_range():
    d = projection([product()], [supply(quantity=70)])
    assert d["actual"]["fee"] == 40
    assert d["quality"]["unknown_deadline_units"] == 10
    assert d["timeline"][0]["low"] == d["timeline"][0]["high"] == 40
    assert d["timeline"][1]["low"] == 180
    assert d["timeline"][1]["high"] == 200


def test_free_pool_is_global_before_current_warehouse_or_cluster_filter():
    rows = [
        product(quantity=50, paid_quantity=10, fee=20),
        product(
            warehouse_id=11,
            warehouse_name="ХОРУГВИНО",
            cluster_id=21,
            quantity=50,
            paid_quantity=10,
            fee=30,
        ),
    ]
    quotas = [
        supply(quantity=40),
        supply(supply_id="S2", quantity=80, free_until=DAY + timedelta(days=6)),
    ]
    whole = projection(rows, quotas)
    first = projection(rows, quotas, warehouse=10)
    second = projection(rows, quotas, cluster=21)
    assert first["timeline"][1]["low"] == 20
    assert first["timeline"][7]["low"] == 100
    assert second["timeline"][7]["low"] == 150
    assert (
        whole["timeline"][7]["low"]
        == first["timeline"][7]["low"] + second["timeline"][7]["low"]
    )
    assert len(first["batches"]) == 2  # Origin warehouse is not a stock filter.


def test_volume_and_observed_liter_rate_price_free_sku_not_overall_spend():
    free = product(
        sku=2, quantity=20, paid_quantity=0, fee=0, volume_ml=50000, paid_volume_ml=0
    )
    observed = product(
        sku=1,
        warehouse_name="ХОРУГВИНО",
        quantity=1,
        paid_quantity=1,
        fee="8.83",
        volume_ml=3530,
        paid_volume_ml=3530,
    )
    d = projection([free], [supply(sku=2, quantity=20)], [observed])
    assert d["rows"][0]["unit_price"] == Decimal("6.25")
    assert d["rows"][0]["rate_source"] == "type_estimate"
    assert d["timeline"][1]["low"] == 125
    assert d["quality"]["other_warehouse_rate_units"] == 20


def test_other_product_type_is_not_a_tariff_and_unknown_is_not_zero():
    free = product(quantity=10, paid_quantity=0, fee=0, product_type="Иной товар")
    d = projection([free], [supply(quantity=10)], [product()])
    assert d["rows"][0]["unit_price"] is None
    assert d["timeline"][1]["high"] is None
    assert d["quality"]["missing_rate_units"] == 10
    unknown = projection([product(fee=None, quantity=None)], [])
    assert unknown["actual"]["fee"] is None
    assert unknown["timeline"][0]["high"] is None


def test_zero_observed_fee_and_missing_volume_keep_their_meaning():
    zero = product(fee=0)
    d = projection([zero], [supply()], [product()])
    assert d["rows"][0]["unit_price"] == 0
    assert d["timeline"][1]["high"] == 0
    free = product(paid_quantity=0, fee=0, volume_ml=None)
    d = projection([free], [], [product(volume_ml=None, paid_volume_ml=None)])
    assert d["rows"][0]["unit_price"] == 2  # Same SKU, same warehouse observed charge.


def test_summary_and_duplicate_supplies_cannot_double_free_stock():
    quotas = [supply(), supply(), supply(supply_id=None, quantity=99999)]
    d = projection([product()], quotas)
    assert d["timeline"][1]["low"] == 200
    assert len(d["batches"]) == 1
    d = projection([product()], [supply(), supply(quantity=1000)])
    assert d["quality"]["unknown_deadline_units"] == 80
    assert d["timeline"][1]["low"] == 40
    assert d["timeline"][1]["high"] == 200


def test_one_supply_may_have_separate_free_periods_and_both_count():
    quotas = [
        supply(quantity=30),
        supply(quantity=50, free_until=DAY + timedelta(days=6)),
    ]
    d = projection([product()], quotas)
    assert d["quality"]["unknown_deadline_units"] == 0
    assert len(d["batches"]) == 2
    assert d["timeline"][1]["low"] == 100
    assert d["timeline"][7]["low"] == 200


def test_price_evidence_cannot_leak_from_the_future():
    free = product(paid_quantity=0, fee=0)
    future = product(row_day=DAY + timedelta(days=1))
    d = projection([free], [supply()], [future])
    assert d["rows"][0]["rate_source"] == "unknown"
    assert d["timeline"][1]["high"] is None


def test_conflicting_observed_tariffs_are_not_averaged_into_a_false_price():
    free = product(sku=3, paid_quantity=0, fee=0, warehouse_name="Другой склад")
    observations = [product(sku=1), product(sku=2, fee=100)]
    d = projection([free], [supply(sku=3)], observations)
    assert d["rows"][0]["unit_price"] is None
    assert d["timeline"][1]["high"] is None


def insert_report(conn, day, kind="products"):
    return conn.execute(
        "INSERT INTO ozon.storage_report(client_id,day,kind,status,completed_at) VALUES(123,%s,%s,'success',now()) RETURNING id",
        (day, kind),
    ).fetchone()["id"]


def insert_row(conn, report, row, row_number=1):
    keys = [
        "row_day",
        "sku",
        "warehouse_id",
        "warehouse_name",
        "cluster_id",
        "quantity",
        "paid_quantity",
        "fee",
        "volume_ml",
        "paid_volume_ml",
        "category",
        "product_type",
        "item_feature",
    ]
    conn.execute(
        f"INSERT INTO ozon.storage_row(report_id,row_number,{','.join(keys)},raw) VALUES(%s,%s,{','.join(['%s'] * len(keys))},%s)",
        (report, row_number, *(row.get(k) for k in keys), Jsonb({})),
    )


def test_database_history_versions_dimensions_and_authenticated_forecast(db):
    with db() as conn:
        conn.execute(
            "INSERT INTO ozon.product(client_id,sku,offer_id,name) VALUES(123,1,'ONE','Product')"
        )
        conn.execute("INSERT INTO ozon.warehouse(warehouse_id,name) VALUES(10,'ТВЕРЬ')")
        conn.execute("INSERT INTO ozon.cluster(cluster_id,name) VALUES(20,'Тверь')")
        yesterday = insert_report(conn, DAY - timedelta(days=1))
        insert_row(conn, yesterday, product(row_day=DAY - timedelta(days=1), fee=10))
        today = insert_report(conn, DAY)
        insert_row(conn, today, product(row_day=DAY - timedelta(days=1), fee=99))
        insert_row(conn, today, product(), 2)
        # Duplicate row in one source report is not a second SKU/warehouse.
        insert_row(conn, today, product(), 3)
        quota = insert_report(conn, DAY, "supplies")
        conn.execute(
            "INSERT INTO ozon.storage_row(report_id,row_number,sku,supply_id,quantity,free_until,quantity_day,raw) VALUES(%s,1,1,'S1',80,%s,%s,'{}')",
            (quota, DAY, DAY),
        )
    old = storage_projection(DAY - timedelta(days=1))
    assert old["actual"]["fee"] == 10  # New report has not erased historical one.
    current = storage_projection(DAY, cluster=20)
    assert current["actual"]["fee"] == 40
    assert len(current["rows"]) == 1
    assert current["rows"][0]["name"] == "Product"
    assert current["rows"][0]["cluster_id"] == 20
    web.attempts.clear()
    with TestClient(web.app, base_url="https://testserver") as client:
        assert client.get(f"/api/storage/forecast?as_of={DAY}").status_code == 401
        client.post(
            "/login", data={"username": "analytics", "password": "test-password"}
        )
        response = client.get(f"/api/storage/forecast?as_of={DAY}&warehouse=10")
        assert response.status_code == 200
        assert response.json()["actual"]["fee"] == 40
        assert response.json()["timeline"][1]["low"] == 200
        assert client.get("/api/storage/forecast?as_of=invalid").status_code == 422


def test_migration_backfills_volume_without_losing_unknowns_or_raw(db):
    from pathlib import Path

    with db() as conn:
        conn.execute("DROP VIEW ozon.v_storage_unit_cost")
        conn.execute(
            "ALTER TABLE ozon.storage_row DROP COLUMN volume_ml,DROP COLUMN paid_volume_ml,DROP COLUMN category,DROP COLUMN product_type,DROP COLUMN item_feature,DROP COLUMN quantity_day,DROP COLUMN stock_quantity"
        )
        conn.execute(
            "DELETE FROM public.schema_migration WHERE name='005_storage_cost_forecast.sql'"
        )
        report = insert_report(conn, DAY)
        raw = {
            "Суммарный объем в миллилитрах": "3 530,0",
            "Платный объем в миллилитрах": "—",
            "Остаток на складах на (09.10.2026)": 9,
            "Новая колонка": "keep",
        }
        conn.execute(
            "INSERT INTO ozon.storage_row(report_id,row_number,sku,row_day,raw) VALUES(%s,1,1,%s,%s)",
            (report, DAY, Jsonb(raw)),
        )
    assert Path("db/005_storage_cost_forecast.sql").exists()
    migrate()
    with db() as conn:
        row = conn.execute("SELECT * FROM ozon.storage_row").fetchone()
        assert row["volume_ml"] == 3530
        assert row["paid_volume_ml"] is None
        assert row["stock_quantity"] == 9
        assert row["quantity_day"] == DAY
        assert row["raw"] == raw
