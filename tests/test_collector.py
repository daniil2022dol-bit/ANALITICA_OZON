from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from ozon_analytics import collector
from ozon_analytics.api import APIError
from ozon_analytics.queries import dashboard


class FakeAPI:
    def __init__(self, duplicate=False):
        self.calls = []
        self.duplicate = duplicate

    def post(self, endpoint, body):
        self.calls.append((endpoint, body))
        if endpoint == "/v2/cluster/list":
            return {"result": []}
        if endpoint == "/v3/product/list":
            return {
                "result": {
                    "items": [{"sku": 1, "offer_id": "ARTICLE"}]
                    if body["filter"]["visibility"] == "ALL"
                    else []
                }
            }
        if endpoint == "/v1/analytics/stocks":
            row = {
                "sku": 1,
                "offer_id": "ARTICLE",
                "warehouse_id": 10,
                "warehouse_name": "ТВЕРЬ",
                "cluster_id": 20,
                "cluster_name": "Центр",
                "available_stock_count": 100,
                "ads_cluster": 2,
                "placement_zone": "SORT",
            }
            return {"items": [row, row] if self.duplicate else [row]}
        if endpoint == "/v3/posting/fbo/list":
            return {
                "postings": [
                    {
                        "posting_number": "P-1",
                        "order_number": "O-1",
                        "created_at": datetime.combine(
                            collector.today() - timedelta(days=1),
                            datetime.min.time(),
                            collector.MSK,
                        ).isoformat(),
                        "status": "delivered",
                        "analytics_data": {
                            "warehouse_id": 10,
                            "warehouse_name": "ТВЕРЬ",
                        },
                        "financial_data": {
                            "cluster_from": "Центр",
                            "cluster_to": "Казань",
                        },
                        "products": [
                            {
                                "sku": 1,
                                "offer_id": "ARTICLE",
                                "quantity": 2,
                                "price": {"amount": "123.45", "currency": "RUB"},
                            }
                        ],
                    }
                ],
                "has_next": False,
            }
        if endpoint == "/v1/analytics/data":
            return {"result": {"data": []}}
        raise AssertionError(endpoint)


def test_daily_stock_idempotency_and_sales_reconciliation(db):
    api = FakeAPI()
    collector.collect_stocks(api)
    calls = len(api.calls)
    collector.collect_stocks(api)
    assert len(api.calls) == calls
    collector.collect_sales(api)
    collector.collect_sales(api)
    with db() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM ozon.posting").fetchone()["n"] == 1
        )
        row = conn.execute("SELECT * FROM ozon.v_sales_fbo").fetchone()
        assert row["ordered_amount"] == Decimal("246.90")
        assert (
            row["cluster_id"] is None
        )  # Current mapping must not relabel older orders.
        assert row["cluster_to"] == "Казань"
    data = dashboard(collector.today() - timedelta(days=7), collector.today())
    assert data["sales"][0]["units"] == 2
    assert data["stocks"][0]["available_stock_count"] == 100
    with db() as conn:
        assert conn.execute("SELECT placement_zone FROM ozon.stock_daily").fetchone()[
            "placement_zone"
        ] == ["SORT"]
    # Filters must be valid queries even when a slice has no sales.
    assert dashboard(
        collector.today(), collector.today(), sku=1, cluster=20, warehouse=10
    )["stocks"]


def test_duplicate_stock_rejects_publication(db):
    with pytest.raises(APIError, match="Duplicate"):
        collector.collect_stocks(FakeAPI(duplicate=True))
    with db() as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM ozon.published_snapshot"
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute("SELECT status FROM ozon.ingest_run").fetchone()["status"]
            == "failed"
        )


def test_overlapping_storage_reports_do_not_double_charge(db):
    day = collector.today()
    with db() as conn:
        for report_day, fee in [(day - timedelta(days=1), "3.5"), (day, "4.5")]:
            report = conn.execute(
                "INSERT INTO ozon.storage_report(client_id,day,kind,status,completed_at) VALUES(123,%s,'products','success',now()) RETURNING id",
                (report_day,),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO ozon.storage_row(report_id,row_number,sku,row_day,fee,paid_quantity,raw) VALUES(%s,1,1,%s,%s,2,'{}')",
                (report, day - timedelta(days=1), fee),
            )
    fees = dashboard(day - timedelta(days=2), day)["storage_fees"]
    assert len(fees) == 1
    assert fees[0]["fee"] == Decimal("4.5")


def test_cluster_backfill_requires_same_day_snapshot(db):
    api = FakeAPI()
    day = collector.today()
    posting = api.post("/v3/posting/fbo/list", {})["postings"][0]
    current = {
        **posting,
        "posting_number": "P-TODAY",
        "created_at": datetime.combine(
            day, datetime.min.time(), collector.MSK
        ).isoformat(),
    }
    with db() as conn:
        collector.save_postings(conn, [posting, current], {}, day)
    collector.collect_stocks(api)
    collector.reconcile_storage_mapping()
    with db() as conn:
        mapping = {
            r["posting_number"]: r["cluster_id"]
            for r in conn.execute("SELECT posting_number,cluster_id FROM ozon.posting")
        }
    assert mapping["P-TODAY"] == 20
    assert mapping["P-1"] is None
